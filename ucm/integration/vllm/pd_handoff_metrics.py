"""Request-level UCM handoff observations owned by one Decode scheduler."""

import time
from dataclasses import dataclass, field
from typing import Any, Callable


PD_VISIBILITY_WAITING = "pd_visibility_waiting_requests"
PD_WORKER_READY_SPREAD = "pd_worker_ready_spread_seconds"
PD_SCHEDULER_METRICS = frozenset(
    {
        PD_VISIBILITY_WAITING,
        PD_WORKER_READY_SPREAD,
        *(
            f"pd_visibility_deferred_{reason}_seconds"
            for reason in (
                "external_hit",
                "timeout",
                "cancelled",
                "no_external_load",
                "other",
            )
        ),
    }
)


@dataclass
class _Handoff:
    prefill_done_ns: int | None
    first_lookup: float | None = None
    visible: bool = False
    finished: bool = False
    retire_after_outcome: bool = False
    workers: int = 0
    # A zero timestamp acknowledges a rank with no local KV to load.
    ready_by_rank: dict[int, int] = field(default_factory=dict)


class PDHandoffMetrics:
    """Emit at most one terminal outcome per D request across scheduler steps."""

    def __init__(self, record: Callable[[dict], None]):
        self._record = record
        self._requests: dict[str, _Handoff] = {}
        self.waiting_changed(0)

    def waiting_changed(self, count: int) -> None:
        self._record({PD_VISIBILITY_WAITING: count})

    def end_defer(self, reason: str, elapsed: float, waiting: int) -> None:
        """Observe one continuous interval in which D deferred a request for shared KV."""
        self._record(
            {
                PD_VISIBILITY_WAITING: waiting,
                f"pd_visibility_deferred_{reason}_seconds": elapsed,
            }
        )

    def register(self, request_id: str, params: dict[str, Any]) -> _Handoff:
        if request_id in self._requests:
            return self._requests[request_id]
        stamp = params.get("pd_prefill_done_unix_ns")
        if type(stamp) is not int or stamp <= 0:
            stamp = None
        request = self._requests[request_id] = _Handoff(stamp)
        return request

    def start_lookup(self, request_id: str, params: dict[str, Any]) -> None:
        request = self.register(request_id, params)
        if request.first_lookup is None:
            request.first_lookup = time.monotonic()

    def visible(self, request_id: str) -> None:
        request = self._requests.get(request_id)
        if (
            request
            and request.first_lookup is not None
            and not request.visible
            and not request.finished
        ):
            request.visible = True
            self._record(
                {"pd_visibility_wait_seconds": time.monotonic() - request.first_lookup}
            )

    def expect_workers(self, request_id: str, count: int) -> bool:
        request = self._requests.get(request_id)
        if request is None or request.finished:
            return False
        request.workers = count
        return True

    def worker_report(self, request_id: str, ready_by_rank: dict[int, int]) -> None:
        request = self._requests.get(request_id)
        if request is None or request.finished or not request.workers:
            return
        for rank, timestamp in ready_by_rank.items():
            request.ready_by_rank.setdefault(rank, timestamp)
        if len(request.ready_by_rank) < request.workers:
            return
        ready_ns = max(request.ready_by_rank.values())
        if len(request.ready_by_rank) != request.workers:
            self.outcome(request_id, "incomplete")
        elif not ready_ns:
            self.outcome(request_id, "no_external_load")
        elif request.prefill_done_ns is None:
            self.outcome(request_id, "missing_timestamp")
        elif any(
            timestamp and timestamp < request.prefill_done_ns
            for timestamp in request.ready_by_rank.values()
        ):
            self.outcome(request_id, "clock_order")
        else:
            elapsed = (ready_ns - request.prefill_done_ns) / 1e9
            self.outcome(request_id, "observed", elapsed)

    def outcome(
        self, request_id: str, reason: str, elapsed: float | None = None
    ) -> None:
        request = self._requests.get(request_id)
        if request is not None and not request.finished:
            request.finished = True
            stats = {f"pd_handoff_{reason}_total": 1.0}
            if elapsed is not None:
                stats["pd_kv_handoff_seconds"] = elapsed
                loaded = [
                    stamp for stamp in request.ready_by_rank.values() if stamp > 0
                ]
                if len(loaded) >= 2:
                    stats["pd_worker_ready_spread_seconds"] = (
                        max(loaded) - min(loaded)
                    ) / 1e9
            request.ready_by_rank.clear()
            self._record(stats)
            if request.retire_after_outcome:
                self._requests.pop(request_id, None)

    def request_finished(self, request_id: str) -> None:
        request = self._requests.get(request_id)
        if request is None:
            return
        if request.finished or not request.workers:
            self.retire(request_id)
        else:
            # One-token D requests can finish before their current step returns
            # its worker metadata. Release state when that report arrives.
            request.retire_after_outcome = True

    def preempt(self, request_id: str) -> None:
        request = self._requests.get(request_id)
        if request and request.workers:
            # A later dispatch must not mix rank reports from two load attempts.
            self.outcome(request_id, "incomplete")

    def retire(self, request_id: str) -> None:
        self.outcome(request_id, "incomplete")
        self._requests.pop(request_id, None)
