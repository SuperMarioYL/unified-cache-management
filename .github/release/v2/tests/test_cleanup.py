from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-08-12T00:00:00Z"
FILE_IDENTITY = "a" * 64
OCI_IDENTITY = "sha256:" + "b" * 64


def _object(
    object_id: str,
    *,
    kind: str = "artifact",
    channel: str = "pr",
    identity: str = FILE_IDENTITY,
    created_at: str = "2026-08-04T23:59:59Z",
    state: str = "temporary",
    coordinate: str | None = None,
) -> dict[str, object]:
    return {
        "id": object_id,
        "kind": kind,
        "channel": channel,
        "coordinate": coordinate or f"fixture/{object_id}",
        "identity": identity,
        "created_at": created_at,
        "state": state,
    }


def _inventory(*objects: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "cleanup-inventory",
        "schema_version": 2,
        "mode": "read-only",
        "objects": list(objects),
        "references": [],
        "failures": [],
    }


def _run(
    tmp_path: Path, inventory: object, *extra: str
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "inventory.json"
    if isinstance(inventory, str):
        path.write_text(inventory, encoding="utf-8")
    else:
        path.write_text(json.dumps(inventory), encoding="utf-8")
    return subprocess.run(
        [
            "python3",
            "-m",
            "ucm_release_v2",
            "cleanup",
            "plan",
            "--inventory",
            str(path),
            "--as-of",
            AS_OF,
            *extra,
        ],
        cwd=V2_ROOT,
        env=os.environ | {"PYTHONPATH": str(V2_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def _document(tmp_path: Path, inventory: object, *extra: str) -> dict[str, object]:
    completed = _run(tmp_path, inventory, *extra)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    ("created_at", "action", "reason"),
    [
        ("2026-08-04T23:59:59Z", "delete-preview", "expired"),
        ("2026-08-05T00:00:00Z", "skip", "not-expired"),
        ("2026-08-05T00:00:01Z", "skip", "not-expired"),
    ],
)
def test_cleanup_uses_strict_less_than_at_the_seven_day_boundary(
    tmp_path: Path, created_at: str, action: str, reason: str
) -> None:
    """Catches deleting an object exactly on, or newer than, its retention boundary."""
    document = _document(tmp_path, _inventory(_object("wheel", created_at=created_at)))
    assert document["operations"] == [
        {
            "action": action,
            "channel": "pr",
            "coordinate": "fixture/wheel",
            "executed": False,
            "identity": FILE_IDENTITY,
            "kind": "artifact",
            "object_id": "wheel",
            "reason": reason,
        }
    ]


@pytest.mark.parametrize(
    ("channel", "created_at", "action"),
    [
        ("develop", "2026-07-28T23:59:59Z", "delete-preview"),
        ("nightly", "2026-07-29T00:00:00Z", "skip"),
        ("draft", "2026-07-12T23:59:59Z", "delete-preview"),
        ("draft", "2026-07-13T00:00:00Z", "skip"),
    ],
)
def test_cleanup_maps_develop_nightly_and_draft_windows_from_config(
    tmp_path: Path, channel: str, created_at: str, action: str
) -> None:
    """Catches applying the wrong configured 14-day or 30-day channel window."""
    document = _document(
        tmp_path,
        _inventory(_object("candidate", channel=channel, created_at=created_at)),
    )
    assert document["operations"][0]["action"] == action


@pytest.mark.parametrize("channel", ["rc", "stable", "hotfix"])
def test_cleanup_never_proposes_deleting_protected_channels(
    tmp_path: Path, channel: str
) -> None:
    """Catches a retention lookup accidentally treating a protected release as temporary."""
    document = _document(
        tmp_path,
        _inventory(
            _object("protected", channel=channel, created_at="2020-01-01T00:00:00Z")
        ),
    )
    assert document["operations"][0]["action"] == "skip"
    assert document["operations"][0]["reason"] == "protected-channel"


def test_cleanup_protects_expired_objects_shared_with_live_or_referenced_content(
    tmp_path: Path,
) -> None:
    """Catches deleting bytes still shared by live content or an active release reference."""
    inventory = _inventory(
        _object("expired-shared"),
        _object("live-shared", created_at="2026-08-11T00:00:00Z"),
        _object("release-ref", identity="c" * 64),
    )
    inventory["references"] = [
        {
            "id": "active-release",
            "object_id": "release-ref",
            "identity": "c" * 64,
            "source": "active-release",
            "active": True,
        }
    ]
    document = _document(tmp_path, inventory)
    operations = {item["object_id"]: item for item in document["operations"]}
    assert operations["expired-shared"]["reason"] == "shared-or-live-reference"
    assert operations["release-ref"]["reason"] == "shared-or-live-reference"


def test_cleanup_models_known_delete_failure_without_executing_it(
    tmp_path: Path,
) -> None:
    """Catches hiding a known failure or recording a destructive execution."""
    inventory = _inventory(_object("blocked"))
    inventory["failures"] = [
        {"object_id": "blocked", "reason": "registry retention lock"}
    ]
    document = _document(tmp_path, inventory)
    assert document["operations"][0]["action"] == "would-fail"
    assert document["operations"][0]["reason"] == "registry retention lock"
    assert document["operations"][0]["executed"] is False
    assert document["summary"] == {
        "delete_preview": 0,
        "skip": 0,
        "total": 1,
        "would_fail": 1,
    }


def test_cleanup_is_sorted_self_digested_and_retry_deterministic(
    tmp_path: Path,
) -> None:
    """Catches input-order or retry-time drift in an offline cleanup plan."""
    inventory = _inventory(_object("z-last"), _object("a-first", identity="d" * 64))
    first = _document(tmp_path, inventory)
    second = _document(tmp_path, copy.deepcopy(inventory))
    assert first == second
    assert [item["object_id"] for item in first["operations"]] == ["a-first", "z-last"]
    unsigned = dict(first)
    digest = unsigned.pop("sha256")
    assert (
        digest
        == hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert first["mode"] == "read-only"
    assert all(item["executed"] is False for item in first["operations"])


@pytest.mark.parametrize(
    "as_of",
    [
        "2026-08-12T08:00:00+08:00",
        "2026-08-12 00:00:00Z",
        "2026-08-12T00:00:00",
        "2026-08-12T00:00:00.000Z",
        "2026-08-12T00:00:60Z",
    ],
)
def test_cleanup_rejects_noncanonical_as_of_timestamps(
    tmp_path: Path, as_of: str
) -> None:
    """Catches timezone or leap-second normalization changing retention boundaries."""
    completed = _run(tmp_path, _inventory(_object("wheel")), "--as-of", as_of)
    assert completed.returncode == 2
    assert "canonical RFC3339 UTC" in completed.stderr


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["objects"][0].__setitem__(
                "created_at", "2026-08-13T00:00:00Z"
            ),
            "future",
        ),
        (
            lambda value: value["objects"][0].__setitem__(
                "created_at", "2026-08-04T23:59:59+00:00"
            ),
            "canonical RFC3339 UTC",
        ),
        (
            lambda value: value["objects"][0].__setitem__(
                "identity", "sha256:" + "a" * 64
            ),
            "identity",
        ),
        (
            lambda value: value["objects"].append(copy.deepcopy(value["objects"][0])),
            "duplicate object id",
        ),
        (
            lambda value: value["references"].append(
                {
                    "id": "dangling",
                    "object_id": "missing",
                    "identity": FILE_IDENTITY,
                    "source": "active-draft",
                    "active": True,
                }
            ),
            "dangling reference",
        ),
        (
            lambda value: value["failures"].append(
                {"object_id": "missing", "reason": "gone"}
            ),
            "dangling failure",
        ),
        (lambda value: value.__setitem__("extra", True), "keys mismatch"),
        (lambda value: value["objects"][0].pop("created_at"), "keys mismatch"),
    ],
)
def test_cleanup_rejects_malformed_or_ambiguous_inventory(
    tmp_path: Path, mutation: object, message: str
) -> None:
    """Catches malformed inventory escaping into an ambiguous deletion preview."""
    inventory = _inventory(_object("wheel"))
    mutation(inventory)  # type: ignore[operator]
    completed = _run(tmp_path, inventory)
    assert completed.returncode == 2
    assert message in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cleanup_rejects_duplicate_json_keys_and_has_no_execute_option(
    tmp_path: Path,
) -> None:
    """Catches masked inventory data or a newly exposed deletion route."""
    duplicate = (
        '{"kind":"cleanup-inventory","kind":"other","schema_version":2,'
        '"mode":"read-only","objects":[],"references":[],"failures":[]}'
    )
    completed = _run(tmp_path, duplicate)
    execute = _run(tmp_path, _inventory(), "--execute")
    assert completed.returncode == 2
    assert "duplicate key" in completed.stderr
    assert execute.returncode == 2
    assert "unrecognized arguments: --execute" in execute.stderr


@pytest.mark.parametrize(
    "objects",
    [
        [
            _object("expired-one", coordinate="fixture/shared", identity="1" * 64),
            _object("expired-two", coordinate="fixture/shared", identity="2" * 64),
        ],
        [
            _object(
                "protected",
                coordinate="fixture/shared",
                identity="1" * 64,
                state="protected",
            ),
            _object("expired", coordinate="fixture/shared", identity="2" * 64),
        ],
        [
            _object("same-one", coordinate="fixture/shared"),
            _object("same-two", coordinate="fixture/shared"),
        ],
    ],
)
def test_cleanup_rejects_same_kind_coordinate_aliases_before_planning(
    tmp_path: Path, objects: list[dict[str, object]]
) -> None:
    """Catches one deletion target being represented by multiple logical object IDs."""
    completed = _run(tmp_path, _inventory(*objects))
    assert completed.returncode == 2
    assert "duplicate coordinate" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cleanup_namespaces_identical_coordinate_strings_by_object_kind(
    tmp_path: Path,
) -> None:
    """Catches artifact, image, and Chart API namespaces being collapsed together."""
    document = _document(
        tmp_path,
        _inventory(
            _object("artifact", coordinate="fixture/shared"),
            _object(
                "image",
                kind="image",
                coordinate="fixture/shared",
                identity=OCI_IDENTITY,
            ),
            _object(
                "chart",
                kind="chart",
                coordinate="fixture/shared",
                identity="c" * 64,
            ),
        ),
    )
    assert [item["object_id"] for item in document["operations"]] == [
        "artifact",
        "chart",
        "image",
    ]


def test_cleanup_chart_is_a_local_file_hash_and_can_be_shared_by_reference(
    tmp_path: Path,
) -> None:
    """Charts follow local manifest bytes, while only images use OCI identities."""
    chart = _object(
        "chart-old",
        kind="chart",
        identity="c" * 64,
        created_at="2026-07-01T00:00:00Z",
    )
    inventory = _inventory(chart)
    inventory["references"] = [
        {
            "id": "chart-live",
            "object_id": "chart-old",
            "identity": "c" * 64,
            "source": "active-release",
            "active": True,
        }
    ]
    document = _document(tmp_path, inventory)
    assert document["operations"][0]["action"] == "skip"
    assert document["operations"][0]["reason"] == "shared-or-live-reference"

    invalid = _inventory(_object("chart-oci", kind="chart", identity=OCI_IDENTITY))
    rejected = _run(tmp_path, invalid)
    assert rejected.returncode == 2
    assert "64 lowercase hex characters" in rejected.stderr


def test_cleanup_schema_is_strict_conditional_and_non_executing() -> None:
    """Catches schema drift that permits loose counts, mixed identities, or execution."""
    schema = json.loads(
        (V2_ROOT / "schemas/cleanup-plan.schema.json").read_text(encoding="utf-8")
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    operation = schema["$defs"]["operation"]
    assert operation["additionalProperties"] is False
    assert operation["properties"]["executed"] == {"const": False}
    assert operation["allOf"]
    assert schema["properties"]["operations"]["minItems"] == 0
