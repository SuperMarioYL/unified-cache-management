from __future__ import annotations

import hashlib
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=V2_ROOT,
        env=os.environ | {"PYTHONPATH": str(V2_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def _plan(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    path = tmp_path / "lifecycle-plan.json"
    result = _run(
        "lifecycle",
        "plan",
        "--stage",
        "nightly",
        "--trigger",
        "schedule",
        "--ref",
        "refs/heads/develop",
        "--source-sha",
        SHA,
        "--repository-role",
        "validation",
        "--date",
        "2026-08-12",
        "--output",
        str(path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    return path, json.loads(path.read_text(encoding="utf-8"))


def _resign(value: dict[str, object]) -> dict[str, object]:
    unsigned = dict(value)
    unsigned.pop("sha256", None)
    unsigned["sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return unsigned


def _plan_for_stage(tmp_path: Path, stage: str) -> Path:
    args = [
        "lifecycle",
        "plan",
        "--stage",
        stage,
        "--source-sha",
        SHA,
        "--config",
        "release.yaml",
    ]
    if stage == "pr":
        args.extend(
            [
                "--trigger",
                "pull_request",
                "--ref",
                "refs/pull/42/head",
                "--repository-role",
                "validation",
                "--pr-number",
                "42",
            ]
        )
    elif stage == "develop":
        args.extend(
            [
                "--trigger",
                "push",
                "--ref",
                "refs/heads/develop",
                "--repository-role",
                "validation",
                "--run-number",
                "1",
            ]
        )
    else:  # pragma: no cover - this helper intentionally limits this regression matrix.
        raise AssertionError(f"unsupported test stage: {stage}")
    path = tmp_path / f"{stage}-plan.json"
    result = _run(*args, "--output", str(path))
    assert result.returncode == 0, result.stderr
    return path


def _records(base: Path, plan: dict[str, object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for product in plan["products"]:  # type: ignore[index]
        assert isinstance(product, dict)
        if product["kind"] == "image":
            records.append(
                {
                    "kind": "image",
                    "name": product["name"],
                    "coordinate": product["coordinate"],
                    "digest": "sha256:" + "b" * 64,
                    "platforms": [
                        {"platform": "linux/arm64", "digest": "sha256:" + "c" * 64},
                        {"platform": "linux/amd64", "digest": "sha256:" + "d" * 64},
                    ],
                }
            )
        else:
            relative = f"artifacts/{product['name']}.bin"
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"{product['kind']}:{product['coordinate']}".encode())
            records.append(
                {
                    "kind": product["kind"],
                    "name": product["name"],
                    "coordinate": product["coordinate"],
                    "path": relative,
                }
            )
    return records


def _collect(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    plan_path, plan = _plan(tmp_path)
    base = tmp_path / "base"
    records_path = tmp_path / "records.json"
    records_path.write_text(
        json.dumps(list(reversed(_records(base, plan)))), encoding="utf-8"
    )
    manifest_path = tmp_path / "artifact-manifest.json"
    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(plan_path),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--output",
        str(manifest_path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    return manifest_path, base, json.loads(manifest_path.read_text(encoding="utf-8"))


def test_collect_and_validate_bind_exact_products_to_content_addresses(
    tmp_path: Path,
) -> None:
    """Collecting a plan's seven products remains offline and revalidates their bytes."""
    manifest_path, base, manifest = _collect(tmp_path)

    assert manifest["kind"] == "artifact-manifest"
    assert manifest["validation"] == {
        "file_bytes": "passed",
        "lifecycle_plan": "passed",
        "oci_identity": "passed",
        "product_closure": "passed",
        "registry_readback": "unexecuted",
        "runtime": "unexecuted",
    }
    artifacts = manifest["artifacts"]
    assert artifacts == sorted(artifacts, key=lambda item: (item["kind"], item["name"]))
    file_item = next(item for item in artifacts if item["kind"] == "wheel")
    assert (
        file_item["sha256"]
        == hashlib.sha256((base / file_item["path"]).read_bytes()).hexdigest()
    )

    result = _run(
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(tmp_path / "lifecycle-plan.json"),
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "passed"


def test_collect_rejects_missing_or_extra_plan_products(tmp_path: Path) -> None:
    """Records cannot silently omit a product or introduce an unplanned one."""
    plan_path, plan = _plan(tmp_path)
    base = tmp_path / "base"
    records = _records(base, plan)
    records.pop()
    records.append(
        {
            "kind": "chart",
            "name": "other",
            "coordinate": "other@1",
            "path": "artifacts/other.bin",
        }
    )
    (base / "artifacts/other.bin").write_bytes(b"other")
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(records), encoding="utf-8")

    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(plan_path),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "product closure" in result.stderr


def test_collect_rejects_bad_oci_identity_and_duplicate_product(tmp_path: Path) -> None:
    """OCI declarations require two exact platforms and no conflicting duplicate coordinate."""
    plan_path, plan = _plan(tmp_path)
    base = tmp_path / "base"
    records = _records(base, plan)
    image = next(item for item in records if item["kind"] == "image")
    image["digest"] = "sha256:" + "A" * 64
    image["platforms"] = [{"platform": "linux/amd64", "digest": "sha256:" + "b" * 64}]
    records.append(dict(records[0]))
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(records), encoding="utf-8")

    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(plan_path),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "digest" in result.stderr


def test_collect_rejects_platform_gaps_and_conflicting_duplicate_coordinate(
    tmp_path: Path,
) -> None:
    """A duplicate logical product cannot hide a different file or OCI platform closure."""
    plan_path, plan = _plan(tmp_path)
    base = tmp_path / "base"
    records = _records(base, plan)
    image = next(item for item in records if item["kind"] == "image")
    image["platforms"] = [{"platform": "linux/amd64", "digest": "sha256:" + "b" * 64}]
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(records), encoding="utf-8")
    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(plan_path),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "platforms" in result.stderr

    records = _records(base, plan)
    duplicate = dict(next(item for item in records if item["kind"] == "wheel"))
    replacement = base / "artifacts/replacement.bin"
    replacement.write_bytes(b"conflicting content")
    duplicate["path"] = "artifacts/replacement.bin"
    records.append(duplicate)
    records_path.write_text(json.dumps(records), encoding="utf-8")
    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(plan_path),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "duplicate" in result.stderr


def test_validate_rejects_plan_digest_file_checksum_and_stage_version_drift(
    tmp_path: Path,
) -> None:
    """A manifest is invalid when its plan envelope, content, or release identity changes."""
    manifest_path, base, manifest = _collect(tmp_path)
    file_item = next(item for item in manifest["artifacts"] if item["kind"] == "chart")
    (base / file_item["path"]).write_bytes(b"tampered")
    result = _run(
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(tmp_path / "lifecycle-plan.json"),
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "checksum" in result.stderr

    # Restore byte content then mutate a signed manifest identity and its envelope digest.
    (base / file_item["path"]).write_bytes(f"chart:{file_item['coordinate']}".encode())
    unsigned = dict(manifest)
    unsigned["stage"] = "stable"
    unsigned["sha256"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in unsigned.items() if k != "sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(unsigned), encoding="utf-8")
    result = _run(
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(tmp_path / "lifecycle-plan.json"),
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "stage" in result.stderr or "identity" in result.stderr


def test_validate_rejects_a_manifest_bound_to_a_different_plan_digest(
    tmp_path: Path,
) -> None:
    """The manifest cannot be replayed against another lifecycle plan envelope."""
    manifest_path, base, manifest = _collect(tmp_path)
    manifest["lifecycle_plan_sha256"] = "e" * 64
    manifest["sha256"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in manifest.items() if k != "sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run(
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(tmp_path / "lifecycle-plan.json"),
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "lifecycle_plan_sha256" in result.stderr


def test_collect_is_reproducible_and_refuses_path_escape(tmp_path: Path) -> None:
    """Record ordering does not affect output, and file records cannot escape base-dir."""
    first_path, base, first = _collect(tmp_path)
    plan = json.loads((tmp_path / "lifecycle-plan.json").read_text(encoding="utf-8"))
    records = _records(base, plan)
    records_path = tmp_path / "records-second.json"
    records_path.write_text(json.dumps(records), encoding="utf-8")
    second_path = tmp_path / "second.json"
    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(tmp_path / "lifecycle-plan.json"),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--output",
        str(second_path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    assert first == json.loads(second_path.read_text(encoding="utf-8"))

    records[0]["path"] = "../escape.bin"
    records_path.write_text(json.dumps(records), encoding="utf-8")
    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(tmp_path / "lifecycle-plan.json"),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "path" in result.stderr


def test_collect_rejects_resigned_plan_with_invalid_stage_or_stage_version(
    tmp_path: Path,
) -> None:
    """A matching envelope hash cannot turn a semantically invalid plan into input."""
    base = tmp_path / "base"
    base.mkdir()
    records_path = tmp_path / "records.json"
    records_path.write_text("[]", encoding="utf-8")

    path, plan = _plan(tmp_path)
    plan["stage"] = plan["channel"] = "invalid"
    path.write_text(json.dumps(_resign(plan)), encoding="utf-8")
    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(path),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "stage" in result.stderr

    for stage, drifted_version in (
        ("pr", "0.6.0.dev0+pr.43.g.aaaaaaaaaaaa"),
        ("develop", "0.6.0.dev1+develop.g.bbbbbbbbbbbb"),
    ):
        path = _plan_for_stage(tmp_path, stage)
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan["version"] = drifted_version
        path.write_text(json.dumps(_resign(plan)), encoding="utf-8")
        result = _run(
            "artifacts",
            "collect",
            "--lifecycle-plan",
            str(path),
            "--records-json",
            str(records_path),
            "--base-dir",
            str(base),
            "--config",
            "release.yaml",
        )
        assert result.returncode != 0
        assert "version" in result.stderr


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("source_sha", 7),
        ("source_sha", {"sha": SHA}),
        ("stage", 7),
        ("trigger", {"trigger": "schedule"}),
        ("ref", 7),
        ("repository", {"repository": "validation"}),
        ("repository_role", 7),
        ("version", {"version": "0.6.0"}),
    ],
)
def test_artifacts_cli_rejects_resigned_malformed_plan_fields_without_traceback(
    tmp_path: Path,
    field: str,
    malformed: object,
) -> None:
    """Untrusted lifecycle JSON types always fail at the public CLI boundary."""
    plan_path, plan = _plan(tmp_path)
    base = tmp_path / "base"
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(_records(base, plan)), encoding="utf-8")
    plan[field] = malformed
    plan_path.write_text(json.dumps(_resign(plan)), encoding="utf-8")

    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(plan_path),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr

    manifest_path, manifest_base, _ = _collect(tmp_path / "validation")
    validation_plan = tmp_path / "validation" / "lifecycle-plan.json"
    validation_plan_data = json.loads(validation_plan.read_text(encoding="utf-8"))
    validation_plan_data[field] = malformed
    validation_plan.write_text(
        json.dumps(_resign(validation_plan_data)), encoding="utf-8"
    )
    result = _run(
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(validation_plan),
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(manifest_base),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_collect_rejects_all_noncanonical_and_symlink_escape_paths(
    tmp_path: Path,
) -> None:
    """File artifacts never accept lexical or resolved paths outside the declared base."""
    plan_path, plan = _plan(tmp_path)
    base = tmp_path / "base"
    records_path = tmp_path / "records.json"
    for unsafe in (
        "/tmp/absolute.bin",
        "./artifacts/chart.bin",
        "artifacts/../chart.bin",
        "artifacts/.",
        "artifacts/..",
        "artifacts//chart.bin",
        "artifacts/",
        "artifacts\\chart.bin",
    ):
        records = _records(base, plan)
        records[0]["path"] = unsafe
        records_path.write_text(json.dumps(records), encoding="utf-8")
        result = _run(
            "artifacts",
            "collect",
            "--lifecycle-plan",
            str(plan_path),
            "--records-json",
            str(records_path),
            "--base-dir",
            str(base),
            "--config",
            "release.yaml",
        )
        assert result.returncode != 0
        assert "path" in result.stderr

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = base / "artifacts" / "escape-link"
    link.symlink_to(outside)
    records = _records(base, plan)
    records[0]["path"] = "artifacts/escape-link"
    records_path.write_text(json.dumps(records), encoding="utf-8")
    result = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(plan_path),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "escapes" in result.stderr


def test_validate_rejects_manifest_cardinality_kind_and_platform_closure(
    tmp_path: Path,
) -> None:
    """Schema-counted artifact and platform closures are also enforced at runtime."""
    manifest_path, base, manifest = _collect(tmp_path)
    wheel = next(item for item in manifest["artifacts"] if item["kind"] == "wheel")
    image = next(item for item in manifest["artifacts"] if item["kind"] == "image")

    eight_items = deepcopy(manifest)
    eight_items["artifacts"].append(deepcopy(wheel))
    manifest_path.write_text(json.dumps(_resign(eight_items)), encoding="utf-8")
    result = _run(
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(tmp_path / "lifecycle-plan.json"),
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "count" in result.stderr

    four_wheels = deepcopy(manifest)
    replacement_index = four_wheels["artifacts"].index(image)
    four_wheels["artifacts"][replacement_index] = deepcopy(wheel)
    four_wheels["artifacts"].sort(key=lambda item: (item["kind"], item["name"]))
    manifest_path.write_text(json.dumps(_resign(four_wheels)), encoding="utf-8")
    result = _run(
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(tmp_path / "lifecycle-plan.json"),
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "count" in result.stderr

    duplicate_amd64 = deepcopy(manifest)
    image = next(
        item for item in duplicate_amd64["artifacts"] if item["kind"] == "image"
    )
    image["platforms"] = [
        {"platform": "linux/amd64", "digest": "sha256:" + "e" * 64},
        {"platform": "linux/amd64", "digest": "sha256:" + "f" * 64},
    ]
    manifest_path.write_text(json.dumps(_resign(duplicate_amd64)), encoding="utf-8")
    result = _run(
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(tmp_path / "lifecycle-plan.json"),
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(base),
        "--config",
        "release.yaml",
    )
    assert result.returncode != 0
    assert "platforms" in result.stderr


def test_artifact_schema_is_strict_and_distinguishes_file_and_oci_evidence() -> None:
    """The published contract does not allow extra fields or blur local/registry/runtime evidence."""
    schema = json.loads(
        (V2_ROOT / "schemas" / "artifact-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    artifact = schema["$defs"]["artifact"]
    assert {item["$ref"] for item in artifact["oneOf"]} == {
        "#/$defs/file_artifact",
        "#/$defs/oci_artifact",
    }
    assert schema["properties"]["validation"]["$ref"] == "#/$defs/validation"
    artifacts = schema["properties"]["artifacts"]
    assert artifacts["minItems"] == artifacts["maxItems"] == 7
    assert {
        (
            item["contains"]["properties"]["kind"]["const"],
            item["minContains"],
            item["maxContains"],
        )
        for item in artifacts["allOf"]
    } == {("wheel", 3, 3), ("image", 3, 3), ("chart", 1, 1)}
    platforms = schema["$defs"]["oci_artifact"]["properties"]["platforms"]
    assert {
        (
            item["contains"]["properties"]["platform"]["const"],
            item["minContains"],
            item["maxContains"],
        )
        for item in platforms["allOf"]
    } == {("linux/amd64", 1, 1), ("linux/arm64", 1, 1)}
    path = schema["$defs"]["file_artifact"]["properties"]["path"]
    assert path["allOf"]
    assert any("[^/]+(?:/[^/]+)*" in item.get("pattern", "") for item in path["allOf"])
