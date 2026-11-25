import asyncio
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

PD = Path(__file__).resolve().parents[2] / "ucm" / "pd"


def send_through_proxy(
    monkeypatch,
    backend,
    request_id="ucm-test",
    payload=None,
    ucm_pd=False,
):
    # The proxy also runs standalone on a host without the UCM engine libraries.
    monkeypatch.syspath_prepend(str(PD))
    import ucm_pd_disaggregation_example as proxy

    argv = ["proxy", "--ucm-pd"] if ucm_pd else ["proxy"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(proxy, "global_args", proxy.parse_args())
    scheduler = proxy.SharedProxyScheduler(
        [("prefill", 8000)], [("decode", 8000)], backend="ucm" if ucm_pd else "mooncake"
    )
    runtime = proxy.WorkerRuntime(scheduler)
    monkeypatch.setattr(proxy, "runtime", runtime)

    async def run():
        transport = httpx.MockTransport(backend)
        async with (
            httpx.AsyncClient(
                base_url="http://prefill", transport=transport
            ) as prefill,
            httpx.AsyncClient(base_url="http://decode", transport=transport) as decode,
            httpx.AsyncClient(
                base_url="http://proxy", transport=httpx.ASGITransport(app=proxy.app)
            ) as client,
        ):

            async def get_client(role, key):
                return prefill if role == proxy.ServerRole.PREFILL else decode

            monkeypatch.setattr(runtime, "get_client", get_client)
            body = payload or {"prompt": "hello", "max_tokens": 8}
            return await client.post(
                "/v1/chat/completions" if "messages" in body else "/v1/completions",
                json=body,
                headers={"X-Request-Id": request_id} if request_id else {},
            )

    return asyncio.run(run()), scheduler


def sse(choice):
    return (
        b"data: "
        + json.dumps({"choices": [choice]}, ensure_ascii=False).encode()
        + b"\r\n\r\n"
    )


def test_coalesced_usage_rewrite_preserves_other_sse_fields(monkeypatch):
    first = b": heartbeat\r\n\r\n" + sse({"text": "hello"})
    fields = b"id: 9\r\nevent: message\r\n"
    usage = b'data: {"choices":[],\r\ndata: "usage":{"completion_tokens":1}}\r\n\r\n'
    done = b"data: [DONE]\n\n"

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first + fields + usage[:16]
            yield usage[16:] + done

    def backend(request):
        if request.url.host == "prefill":
            return httpx.Response(
                200, json={"usage": {"prompt_tokens_details": {"cached_tokens": 9}}}
            )
        return httpx.Response(200, stream=Stream())

    response, _ = send_through_proxy(
        monkeypatch, backend, payload={"prompt": "p", "stream": True}
    )
    response.raise_for_status()
    prefix = first + fields + b"data: "
    assert response.content.startswith(prefix)
    assert response.content.endswith(b"\r\n\r\n" + done)
    rewritten = json.loads(
        response.content.removeprefix(prefix).removesuffix(b"\r\n\r\n" + done)
    )
    assert rewritten["usage"] == {
        "completion_tokens": 1,
        "prompt_tokens_details": {"cached_tokens": 9},
    }


def test_recompute_observes_one_logical_stream(monkeypatch):
    replies = iter([{"text": "one"}, {"text": "two"}])
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def aclose(self):
            closed.append(self)

        async def __aiter__(self):
            choice = next(replies)
            if choice["text"] == "one":
                output = sse(choice)
                yield sse({"delta": {"role": "assistant"}}) + output[:12]
                yield (
                    output[12:]
                    + sse({"text": "", "stop_reason": "recomputed"})
                    + sse({"text": "obsolete"})
                    + b"data: [DONE]\n\n"
                )
            else:
                yield sse(choice)
            yield b"data: [DONE]\n\n"

    def backend(request):
        if request.url.host == "prefill":
            if json.loads(request.content)["prompt"] != "p":
                assert len(closed) == 1
            return httpx.Response(200, json={})
        return httpx.Response(200, stream=Stream())

    response, scheduler = send_through_proxy(
        monkeypatch, backend, payload={"prompt": "p", "stream": True}
    )
    assert "two" in response.text
    assert "obsolete" not in response.text and "recomputed" not in response.text
    assert response.content.count(b"[DONE]") == 1
    assert (
        b'pd_proxy_ttft_seconds_count{backend="mooncake",outcome="recomputed"} 1.0'
        in scheduler.render_metrics()
    )


@pytest.mark.parametrize("ucm_pd", [False, True])
def test_recompute_uses_only_current_handoff(monkeypatch, ucm_pd):
    first_handoff = {
        "remote_block_ids": [7],
        "do_remote_prefill": False,
        "pd_prefill_done_unix_ns": 101,
    }
    p_replies = iter(
        [
            {"kv_transfer_params": first_handoff},
            {"kv_transfer_params": {"pd_prefill_done_unix_ns": 202}},
            {},
        ]
    )
    d_bodies = []

    def backend(request):
        if request.url.host == "prefill":
            prefill_params = json.loads(request.content)["kv_transfer_params"]
            assert ("ucm_pd_relay" in prefill_params) == ucm_pd
            return httpx.Response(200, json=next(p_replies))
        d_bodies.append(json.loads(request.content))
        stop = "recomputed" if len(d_bodies) < 3 else "stop"
        return httpx.Response(
            200,
            json={
                "choices": [{"text": "x", "stop_reason": stop}],
                "usage": {"completion_tokens": 1},
            },
        )

    body = {"prompt": "hello", "max_tokens": 8}
    if ucm_pd:
        body["kv_transfer_params"] = {
            "client_hint": "keep",
            "pd_prefill_done_unix_ns": 999,
        }
    response, _ = send_through_proxy(monkeypatch, backend, payload=body, ucm_pd=ucm_pd)
    response.raise_for_status()
    first, second, legacy = d_bodies
    assert first["kv_transfer_params"] == {
        "remote_block_ids": [7],
        "do_remote_prefill": ucm_pd,
        **({"pd_prefill_done_unix_ns": 101} if ucm_pd else {}),
    }
    if ucm_pd:
        assert second["kv_transfer_params"] == {
            "client_hint": "keep",
            "do_remote_prefill": True,
            "pd_prefill_done_unix_ns": 202,
        }
        assert legacy["kv_transfer_params"] == {
            "client_hint": "keep",
            "do_remote_prefill": True,
        }
    else:
        assert "kv_transfer_params" not in second
        assert "kv_transfer_params" not in legacy


@pytest.mark.parametrize(
    "body,ucm_pd,forward_timestamp",
    [
        ({"prompt": ["first", "second"]}, True, False),
        ({"prompt": [[1, 2], [3, 4]]}, False, False),
        ({"messages": [{"role": "user", "content": "hi"}], "n": 2}, False, False),
        ({"prompt": "hi", "best_of": 2}, True, False),
        ({"prompt": [1, 2]}, True, True),
        ({"prompt": [1, 2]}, False, False),
    ],
)
def test_handoff_timestamp_requires_one_sequence(
    monkeypatch, body, ucm_pd, forward_timestamp
):
    decode_params = []

    def backend(request):
        if request.url.host == "prefill":
            params = json.loads(request.content)["kv_transfer_params"]
            assert params.get("ucm_pd_relay", False) is forward_timestamp
            handoff = {"pd_prefill_done_unix_ns": 123}
            if not ucm_pd:
                handoff["remote_request_id"] = "producer-child"
            return httpx.Response(200, json={"kv_transfer_params": handoff})
        decode_params.append(json.loads(request.content)["kv_transfer_params"])
        return httpx.Response(
            200, json={"choices": [{"text": "ok", "finish_reason": "stop"}]}
        )

    payload = {
        **body,
        "kv_transfer_params": {"client_hint": "keep", "pd_prefill_done_unix_ns": 999},
    }
    response, _ = send_through_proxy(
        monkeypatch, backend, payload=payload, ucm_pd=ucm_pd
    )
    response.raise_for_status()
    expected = (
        {"do_remote_prefill": True, "client_hint": "keep"}
        if ucm_pd
        else {"remote_request_id": "producer-child"}
    )
    if forward_timestamp:
        expected["pd_prefill_done_unix_ns"] = 123
    assert decode_params == [expected]


@pytest.mark.parametrize("choice", [{"text": "x"}, {"delta": {"role": "assistant"}}])
def test_streaming_metrics_require_generated_output_for_ttft(monkeypatch, choice):
    monkeypatch.syspath_prepend(str(PD))
    import ucm_pd_disaggregation_example as proxy

    def backend(request):
        if request.url.host == "prefill":
            return httpx.Response(200, json={})
        return httpx.Response(200, content=sse(choice) + b"data: [DONE]\n\n")

    response, _ = send_through_proxy(
        monkeypatch,
        backend,
        request_id="private-request",
        payload={"prompt": "p", "stream": True},
        ucm_pd=True,
    )
    response.raise_for_status()

    async def scrape():
        async with httpx.AsyncClient(
            base_url="http://proxy", transport=httpx.ASGITransport(app=proxy.app)
        ) as client:
            return await client.get("/metrics")

    metrics = asyncio.run(scrape())
    metrics.raise_for_status()
    assert metrics.headers["content-type"].startswith("text/plain")
    assert "private-request" not in metrics.text
    for stage in (
        "prefill_http",
        "p_response_to_d_send",
        "ttft",
        "d_send_to_first_token",
    ):
        sample = (
            f'pd_proxy_{stage}_seconds_count{{backend="ucm",outcome="success"}} 1.0'
        )
        expected = stage in ("prefill_http", "p_response_to_d_send") or "text" in choice
        assert (sample in metrics.text) == expected


def test_nonstream_usage_and_retry_have_no_first_token(monkeypatch):
    attempts = []

    class JsonBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"choices":[{"text":"ok"}],'
            yield b'"usage":{"completion_tokens":2}}'

    def backend(request):
        if request.url.host == "prefill":
            return httpx.Response(200, json={"usage": {"prompt_tokens": 5}})
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, json={"error": "retry"})
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=JsonBody()
        )

    response, scheduler = send_through_proxy(monkeypatch, backend)
    response.raise_for_status()
    assert response.json()["usage"]["prompt_tokens_details"]["cached_tokens"] == 0
    assert len(attempts) == 2
    samples = scheduler.render_metrics()
    assert (
        b'pd_proxy_requests_total{backend="mooncake",outcome="retried"} 1.0' in samples
    )
    assert b"pd_proxy_ttft_seconds_count" not in samples


def test_failed_recompute_releases_both_reservations(monkeypatch):
    calls = []

    def backend(request):
        calls.append((request.url.host, request.headers["X-Request-Id"]))
        if len(calls) == 1:
            return httpx.Response(200, json={})
        if request.url.host == "decode":
            return httpx.Response(
                200, json={"choices": [{"stop_reason": "recomputed", "text": "x"}]}
            )
        assert json.loads(request.content)["prompt"] == "hellox"
        return httpx.Response(400, json={"error": {"message": "failed after reassign"}})

    _, scheduler = send_through_proxy(
        monkeypatch, backend, request_id=None, ucm_pd=True
    )
    assert [host for host, _ in calls] == ["prefill", "decode", "prefill"]
    assert calls[0][1] and len({request_id for _, request_id in calls}) == 1
    assert scheduler.request_num == 0
    assert scheduler.prefillers["prefill:8000"].active_kv_cache == 0
    assert scheduler.decoders["decode:8000"].active_tokens == 0


@contextmanager
def running_backend(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("mode", ["async", "sync", "legacy-sync"])
def test_comparison_joins_engine_ids_and_rejects_incomplete_handoffs(tmp_path, mode):
    sync = mode != "async"
    script = tmp_path / "ucm_pd_disaggregation_example.py"
    script.write_bytes((PD / script.name).read_bytes())
    mp, md, up, ud = (tmp_path / name for name in ("mp", "md", "up", "ud"))
    samples = ["ok", "missing", "retry", "failed"]
    mp.write_text(
        "".join(
            f"1790000000.010000 INFO Delaying free of 4 blocks for request cmpl-{s}-0-11223344\n"
            for s in samples
        )
    )
    md.write_text(
        "".join(
            f"1790000000.020000 DEBUG Finished recving KV transfer for request cmpl-{s}-0-55667788\n"
            for s in samples
        )
        + "ERROR Failed to transfer KV cache for request cmpl-failed-0-11223344: timeout\n"
    )
    up.write_text(
        "".join(
            f"ucm_pd_prefill_done request_id=cmpl-{s}-0-aabbccdd ts_ns=1790000000010000000\n"
            for s in samples
        )
        + "ucm_pd_prefill_done request_id=cmpl-retry-0-00ff00ff ts_ns=1790000000040000000\n"
    )
    if sync:
        count = " workers=2" if mode == "sync" else ""
        ud.write_text(
            "".join(
                f"ucm_pd_sync_load_done request_id=cmpl-{s}-0-ff112233 rank={r} ts_ns={1790000000020000000 + r * 4000000}{count}\n"
                for s in samples
                for r in ([0] if s == "missing" else [1, 0])
            )
        )
    else:
        ud.write_text(
            "".join(
                f"1790000000.024000 DEBUG Finished recving KV transfer for request cmpl-{s}-0-ff112233\n"
                for s in samples
                if s != "missing"
            )
        )
    args = [
        sys.executable,
        "-I",
        "-S",  # The copied script can compare logs with just the standard library.
        script,
        "compare-handoff",
        "--mooncake-p",
        mp,
        "--mooncake-d",
        md,
        "--ucm-p",
        up,
        "--ucm-d",
        ud,
    ]
    if mode == "sync":
        args.append("--ucm-sync")
    elif mode == "legacy-sync":
        args.extend(["--ucm-sync-ranks", "0", "1"])
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    rows = {r["request_id"]: r for r in csv.DictReader(result.stdout.splitlines())}
    ucm_column = "ucm_sync_rank_ms" if sync else "ucm_scheduler_ms"
    assert rows["cmpl-ok-0"]["mooncake_scheduler_ms"] == "10.000"
    assert rows["cmpl-ok-0"][ucm_column] == "14.000"
    assert rows["cmpl-ok-0"]["ucm_minus_mooncake_ms"] == "4.000"
    assert rows["cmpl-missing-0"]["ucm_status"] == (
        "rank_set_mismatch" if sync else "missing_decode"
    )
    assert rows["cmpl-retry-0"]["ucm_status"] == "ambiguous_attempts"
    assert rows["cmpl-failed-0"]["mooncake_status"] == "failed_or_aborted"
    for key in ("missing", "retry", "failed"):
        assert rows[f"cmpl-{key}-0"]["ucm_minus_mooncake_ms"] == ""


def test_sync_timing_requires_every_worker_but_uses_only_actual_load_times(monkeypatch):
    monkeypatch.syspath_prepend(str(PD))
    import ucm_pd_disaggregation_example as proxy

    def result(rank, outcome, ns, workers):
        line = (
            f"ucm_pd_sync_load_{outcome} request_id=r rank={rank} ts_ns={ns}{workers}"
        )
        return proxy.parse_handoff_event(line, proxy.UCM_RANK)[1]

    start = [proxy.HandoffEvent("r", 1_000_000, None)]
    loaded = result(4, "done", 3_000_000, " workers=2")
    idle = result(5, "skipped", 8_000_000, " workers=2")
    assert proxy.handoff_duration(start, [loaded]) == (None, "rank_set_mismatch")
    assert proxy.handoff_duration(start, [idle, loaded]) == (2.0, "observed")
    assert proxy.handoff_duration(start, [idle, idle]) == (None, "repeated_completion")
    other_idle = result(4, "skipped", 3_000_000, " workers=2")
    assert proxy.handoff_duration(start, [other_idle, idle]) == (
        None,
        "no_external_load",
    )
    legacy = [result(4, "done", 3_000_000, "")]
    assert proxy.handoff_duration(start, legacy) == (None, "missing_worker_count")
    assert proxy.handoff_duration(start, legacy, {4}) == (2.0, "observed")


def test_offline_comparison_accepts_only_mooncake_logs(tmp_path):
    prefill, decode = tmp_path / "p.log", tmp_path / "d.log"
    prefill.write_text(
        "1790000000.010 INFO Delaying free of 2 blocks for request req\n"
    )
    decode.write_text(
        "1790000000.025 DEBUG Finished recving KV transfer for request req\n"
    )
    command = [
        sys.executable,
        "-I",
        "-S",
        PD / "ucm_pd_disaggregation_example.py",
        "compare-handoff",
        "--mooncake-p",
        prefill,
        "--mooncake-d",
        decode,
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    assert list(csv.DictReader(result.stdout.splitlines())) == [
        {
            "request_id": "req",
            "mooncake_scheduler_ms": "15.000",
            "mooncake_status": "observed",
        }
    ]
    partial = subprocess.run(
        command + ["--ucm-d", decode], check=False, capture_output=True, text=True
    )
    assert partial.returncode != 0
    assert "--ucm-p" in partial.stderr


@pytest.mark.parametrize("workers,backend", [(1, "mooncake"), (2, "ucm")])
def test_http_workers_expose_one_registry_and_release_cancelled_requests(
    tmp_path, workers, backend
):
    holding_prefill = threading.Event()
    release_prefill = threading.Event()

    class Engine(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def respond(self, body, content_type="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # Each worker keeps its own HTTP connection pool to this engine.
            self.respond(json.dumps({"peer_port": self.client_address[1]}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.headers["X-Request-Id"] == "cancel":
                holding_prefill.set()
                release_prefill.wait(10)
                self.close_connection = True
                return
            if body["max_tokens"] == 1:
                self.respond(b'{"kv_transfer_params":{"remote_request_id":"prefill"}}')
            else:
                self.respond(
                    sse({"text": "x", "finish_reason": "stop"}) + b"data: [DONE]\n\n",
                    "text/event-stream",
                )

    with ExitStack() as stack:
        engine_port = stack.enter_context(running_backend(Engine))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        command = [
            sys.executable,
            PD / "ucm_pd_disaggregation_example.py",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            str(workers),
            "--prefiller-hosts",
            "127.0.0.1",
            "--prefiller-ports",
            str(engine_port),
            "--decoder-hosts",
            "127.0.0.1",
            "--decoder-ports",
            str(engine_port),
        ]
        if backend == "ucm":
            config = tmp_path / "ucm.yaml"
            config.write_text("use_request_async_load: false\n")
            command += ["--ucm-config", config]
        console = tmp_path / "console.log"
        log = stack.enter_context(console.open("w"))
        proc = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            clients = {}
            deadline = time.monotonic() + 15
            while len(clients) < workers:
                assert proc.poll() is None, console.read_text()
                assert time.monotonic() < deadline, console.read_text()
                candidate = httpx.Client(base_url=url, timeout=4, trust_env=False)
                try:
                    identity = candidate.get("/v1/models")
                    identity.raise_for_status()
                    port_id = identity.json()["peer_port"]
                    if port_id not in clients:
                        clients[port_id] = candidate
                        stack.callback(candidate.close)
                        continue
                except httpx.ConnectError:
                    time.sleep(0.05)
                finally:
                    if candidate not in clients.values():
                        candidate.close()

            initial = next(iter(clients.values())).get("/metrics")
            initial.raise_for_status()
            for outcome in (
                "success", "retried", "recomputed", "cancelled", "upstream_error",
                "interrupted", "unresolved_recompute",
            ):
                assert (
                    f'pd_proxy_requests_total{{backend="{backend}",outcome="{outcome}"}} 0.0'
                    in initial.text
                )

            for port_id, client in clients.items():
                response = client.post(
                    "/v1/completions",
                    json={"prompt": "p", "max_tokens": 2, "stream": True},
                )
                response.raise_for_status()
                assert response.content.count(b"[DONE]") == 1
                assert client.get("/v1/models").json()["peer_port"] == port_id
            for client in clients.values():
                metrics = client.get("/metrics")
                metrics.raise_for_status()
                for metric in (
                    "pd_proxy_requests_total",
                    "pd_proxy_ttft_seconds_count",
                ):
                    assert (
                        f'{metric}{{backend="{backend}",outcome="success"}} {workers}.0'
                        in metrics.text
                    )

            # Disconnect while P still owns a reservation, before D has been selected.
            body = b'{"prompt":"cancel","max_tokens":2}'
            with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
                sock.sendall(
                    f"POST /v1/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Request-Id: cancel\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                assert holding_prefill.wait(4)
            deadline = time.monotonic() + 4
            cancelled = f'pd_proxy_requests_total{{backend="{backend}",outcome="cancelled"}} 1.0'
            while cancelled not in client.get("/metrics").text:
                assert time.monotonic() < deadline, console.read_text()
                time.sleep(0.05)
            assert client.get("/healthcheck").json()["request_num"] == 0
        finally:
            release_prefill.set()
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
