from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "a" * 40


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=V2_ROOT,
        env=os.environ | {"PYTHONPATH": str(V2_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def _signed(value: dict[str, object]) -> dict[str, object]:
    unsigned = dict(value)
    unsigned.pop("sha256", None)
    result = dict(unsigned)
    result["sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def _production_plan(
    tmp_path: Path,
    stage: str = "rc",
    version: str = "0.6.0rc1",
    source_sha: str = SOURCE_SHA,
) -> tuple[Path, dict[str, object]]:
    path = tmp_path / f"{stage}-lifecycle-plan.json"
    intent = json.dumps({"source_sha": source_sha, "stage": stage, "version": version})
    result = _run(
        "lifecycle",
        "plan",
        "--stage",
        stage,
        "--trigger",
        "workflow_dispatch",
        "--ref",
        "refs/heads/main",
        "--source-sha",
        source_sha,
        "--repository-role",
        "production",
        "--intent-json",
        intent,
        "--output",
        str(path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    return path, json.loads(path.read_text(encoding="utf-8"))


def _manifest(
    tmp_path: Path, plan: dict[str, object]
) -> tuple[Path, dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    for index, product in enumerate(plan["products"]):  # type: ignore[index]
        assert isinstance(product, dict)
        if product["kind"] == "image":
            artifacts.append(
                {
                    **product,
                    "version": plan["version"],
                    "digest": "sha256:" + f"{index + 1:x}" * 64,
                    "platforms": [
                        {"platform": "linux/amd64", "digest": "sha256:" + "c" * 64},
                        {"platform": "linux/arm64", "digest": "sha256:" + "d" * 64},
                    ],
                }
            )
        else:
            artifacts.append(
                {
                    **product,
                    "version": plan["version"],
                    "path": f"files/{product['name']}.fixture",
                    "sha256": f"{index + 1:x}" * 64,
                    "size": index + 1,
                }
            )
    manifest = _signed(
        {
            "artifacts": artifacts,
            "kind": "artifact-manifest",
            "lifecycle_plan_sha256": plan["sha256"],
            "mode": "dry-run",
            "schema_version": 2,
            "source_sha": plan["source_sha"],
            "stage": plan["stage"],
            "validation": {
                "file_bytes": "passed",
                "lifecycle_plan": "passed",
                "oci_identity": "passed",
                "product_closure": "passed",
                "registry_readback": "unexecuted",
                "runtime": "unexecuted",
            },
            "version": plan["version"],
        }
    )
    path = tmp_path / "artifact-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def _inventory(tmp_path: Path, targets: list[dict[str, object]]) -> Path:
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps(
            {
                "kind": "release-inventory",
                "schema_version": 2,
                "mode": "read-only",
                "targets": targets,
            }
        ),
        encoding="utf-8",
    )
    return path


def _targets(manifest: dict[str, object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for artifact in manifest["artifacts"]:  # type: ignore[index]
        assert isinstance(artifact, dict)
        result.append(
            {
                "kind": artifact["kind"],
                "name": artifact["name"],
                "coordinate": artifact["coordinate"],
                "identity": (
                    artifact["digest"]
                    if artifact["kind"] == "image"
                    else artifact["sha256"]
                ),
            }
        )
    return result


def _promotion(
    tmp_path: Path,
    *,
    source_stage: str,
    source_version: str,
    target_stage: str,
    target_version: str,
    accepted: bool = True,
) -> Path:
    path = tmp_path / "promotion.json"
    path.write_text(
        json.dumps(
            _signed(
                {
                    "accepted": accepted,
                    "kind": "promotion-evidence",
                    "mode": "read-only",
                    "reason": "offline reviewer declaration",
                    "schema_version": 2,
                    "source_artifact_manifest_sha256": "e" * 64,
                    "source_lifecycle_plan_sha256": "f" * 64,
                    "source_sha": "b" * 40,
                    "source_stage": source_stage,
                    "source_version": source_version,
                    "target_stage": target_stage,
                    "target_version": target_version,
                }
            )
        ),
        encoding="utf-8",
    )
    return path


def _draft_environment_evidence(
    tmp_path: Path,
    rc_version: str,
    *,
    source_sha: str = SOURCE_SHA,
    verdict: str = "passed",
) -> tuple[Path, Path]:
    draft_dir = tmp_path / f"draft-{rc_version}-{source_sha[:4]}"
    plan_path, plan = _production_plan(draft_dir, "draft", rc_version, source_sha)
    manifest_path, _ = _manifest(draft_dir, plan)
    request_path = draft_dir / "environment-test-request.json"
    exported = _run(
        "environment",
        "export",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--environment",
        "blue",
        "--nonce",
        "d" * 32,
        "--output",
        str(request_path),
        "--config",
        "release.yaml",
    )
    assert exported.returncode == 0, exported.stderr
    result_path = draft_dir / "environment-test-result.json"
    simulate_args = [
        "environment",
        "simulate",
        "--request",
        str(request_path),
        "--verdict",
        verdict,
        "--output",
        str(result_path),
        "--config",
        "release.yaml",
    ]
    if verdict == "failed":
        simulate_args.extend(["--fail-check", "smoke"])
    simulated = _run(*simulate_args)
    assert simulated.returncode == 0, simulated.stderr
    return request_path, result_path


def _draft_environment_package(
    tmp_path: Path,
    rc_version: str,
    *,
    source_sha: str = SOURCE_SHA,
    verdict: str = "passed",
) -> tuple[Path, Path, Path, Path]:
    request_path, result_path = _draft_environment_evidence(
        tmp_path,
        rc_version,
        source_sha=source_sha,
        verdict=verdict,
    )
    directory = request_path.parent
    return (
        directory / "draft-lifecycle-plan.json",
        directory / "artifact-manifest.json",
        request_path,
        result_path,
    )


def _anchored_promotion(
    tmp_path: Path,
    target_plan: dict[str, object],
    *,
    source_stage: str,
    source_version: str,
    target_stage: str,
    target_version: str,
    source_sha: str | None = None,
    accepted: bool = True,
) -> tuple[Path, Path, Path]:
    source_dir = tmp_path / f"promotion-source-{source_stage}-{source_version}"
    source_sha = source_sha or str(target_plan["source_sha"])
    source_plan_path, source_plan = _production_plan(
        source_dir, source_stage, source_version, source_sha
    )
    source_manifest_path, source_manifest = _manifest(source_dir, source_plan)
    promotion_path = tmp_path / "anchored-promotion.json"
    promotion_path.write_text(
        json.dumps(
            _signed(
                {
                    "accepted": accepted,
                    "kind": "promotion-evidence",
                    "mode": "read-only",
                    "reason": "offline anchored reviewer declaration",
                    "schema_version": 2,
                    "source_artifact_manifest_sha256": source_manifest["sha256"],
                    "source_lifecycle_plan_sha256": source_plan["sha256"],
                    "source_sha": source_sha,
                    "source_stage": source_stage,
                    "source_version": source_version,
                    "target_stage": target_stage,
                    "target_version": target_version,
                }
            )
        ),
        encoding="utf-8",
    )
    return promotion_path, source_plan_path, source_manifest_path


def test_absent_targets_produce_only_unexecuted_create_previews(tmp_path: Path) -> None:
    """Removing the absent branch would hide all seven creation previews."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, _ = _manifest(tmp_path, plan)
    inventory_path = _inventory(tmp_path, [])

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory_path),
        "--config",
        "release.yaml",
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "conflict-free-preview"
    assert document["production_ready"] is False
    assert document["blockers"] == ["external-environment-evidence-required"]
    assert len(document["operations"]) == 7
    assert {operation["action"] for operation in document["operations"]} == {
        "create-preview"
    }
    assert all(operation["executed"] is False for operation in document["operations"])
    assert [operation["target"] for operation in document["operations"]] == plan[
        "products"
    ]


def test_identical_inventory_skips_and_reconciled_create_inventory_converges(
    tmp_path: Path,
) -> None:
    """Changing identity equality would break the second-run all-skip fixed point."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, manifest = _manifest(tmp_path, plan)
    empty = _inventory(tmp_path, [])
    first = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(empty),
        "--config",
        "release.yaml",
    )
    assert first.returncode == 0, first.stderr
    assert (
        first.stdout
        == _run(
            "reconcile",
            "plan",
            "--lifecycle-plan",
            str(plan_path),
            "--manifest",
            str(manifest_path),
            "--inventory",
            str(empty),
            "--config",
            "release.yaml",
        ).stdout
    )

    reconciled = _inventory(tmp_path, _targets(manifest))
    second = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(reconciled),
        "--config",
        "release.yaml",
    )
    assert second.returncode == 0, second.stderr
    document = json.loads(second.stdout)
    assert {operation["action"] for operation in document["operations"]} == {
        "skip-identical"
    }
    assert all(operation["executed"] is False for operation in document["operations"])
    assert document["status"] == "conflict-free-preview"


@pytest.mark.parametrize("mutation", ["identity", "coordinate"])
def test_partial_family_conflict_blocks_only_the_conflicting_target(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Changing conflict classification would overwrite an occupied logical target."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, manifest = _manifest(tmp_path, plan)
    targets = _targets(manifest)
    image = next(
        item for item in targets if item["kind"] == "image" and item["name"] == "cuda"
    )
    if mutation == "identity":
        image["identity"] = "sha256:" + "9" * 64
    else:
        image["coordinate"] = str(image["coordinate"]) + "-occupied"
    inventory_path = _inventory(tmp_path, [image])

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory_path),
        "--config",
        "release.yaml",
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    actions = [operation["action"] for operation in document["operations"]]
    assert actions.count("conflict") == 1
    assert actions.count("create-preview") == 6
    assert document["status"] == "blocked"
    assert "target-conflict:image:cuda" in document["blockers"]
    assert document["production_ready"] is False


def test_reverse_coordinate_occupancy_marks_both_planned_logicals_as_conflicts(
    tmp_path: Path,
) -> None:
    """Ignoring the coordinate index would preview creation over another logical product."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, manifest = _manifest(tmp_path, plan)
    targets = _targets(manifest)
    cuda = next(
        item for item in targets if item["kind"] == "image" and item["name"] == "cuda"
    )
    cann_a2 = next(
        item
        for item in targets
        if item["kind"] == "image" and item["name"] == "cann-a2"
    )
    cann_a2["coordinate"] = cuda["coordinate"]

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [cann_a2])),
        "--config",
        "release.yaml",
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    by_name = {
        operation["target"]["name"]: operation for operation in document["operations"]
    }
    assert by_name["cuda"]["action"] == "conflict"
    assert by_name["cann-a2"]["action"] == "conflict"
    assert "target-conflict:image:cuda" in document["blockers"]
    assert "target-conflict:image:cann-a2" in document["blockers"]
    assert [operation["action"] for operation in document["operations"]].count(
        "conflict"
    ) == 2


def test_same_kind_shared_coordinate_is_a_two_sided_conflict_not_a_parse_error(
    tmp_path: Path,
) -> None:
    """Rejecting or overwriting a shared coordinate would hide one side of the collision."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, manifest = _manifest(tmp_path, plan)
    targets = _targets(manifest)
    cuda = next(
        item for item in targets if item["kind"] == "image" and item["name"] == "cuda"
    )
    cann_a2 = next(
        item
        for item in targets
        if item["kind"] == "image" and item["name"] == "cann-a2"
    )
    cann_a2["coordinate"] = cuda["coordinate"]

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [cuda, cann_a2])),
        "--config",
        "release.yaml",
    )

    assert result.returncode == 0, result.stderr
    by_name = {
        operation["target"]["name"]: operation["action"]
        for operation in json.loads(result.stdout)["operations"]
    }
    assert by_name["cuda"] == "conflict"
    assert by_name["cann-a2"] == "conflict"


def test_coordinate_occupancy_is_namespaced_by_kind(tmp_path: Path) -> None:
    """A Chart string matching an image coordinate must not occupy the image namespace."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, manifest = _manifest(tmp_path, plan)
    targets = _targets(manifest)
    cuda = next(
        item for item in targets if item["kind"] == "image" and item["name"] == "cuda"
    )
    chart = next(item for item in targets if item["kind"] == "chart")
    chart["coordinate"] = cuda["coordinate"]

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [chart])),
        "--config",
        "release.yaml",
    )

    assert result.returncode == 0, result.stderr
    by_name = {
        operation["target"]["name"]: operation["action"]
        for operation in json.loads(result.stdout)["operations"]
    }
    assert by_name["cuda"] == "create-preview"
    assert by_name["unified-cache-pd"] == "conflict"


@pytest.mark.parametrize(
    ("source_version", "accepted", "blocker"),
    [
        ("0.6.0rc2", False, "promotion-not-accepted"),
        ("0.6.1rc1", True, "promotion-source-version-mismatch"),
    ],
)
def test_stable_requires_an_accepted_rc_for_the_same_release(
    tmp_path: Path,
    source_version: str,
    accepted: bool,
    blocker: str,
) -> None:
    """Weakening Stable ancestry would allow an unaccepted or different RC."""
    plan_path, plan = _production_plan(tmp_path, "stable", "0.6.0")
    manifest_path, _ = _manifest(tmp_path, plan)
    inventory_path = _inventory(tmp_path, [])
    promotion_path, source_plan_path, source_manifest_path = _anchored_promotion(
        tmp_path,
        plan,
        source_stage="rc",
        source_version=source_version,
        target_stage="stable",
        target_version="0.6.0",
        accepted=accepted,
    )

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory_path),
        "--promotion",
        str(promotion_path),
        "--promotion-source-lifecycle-plan",
        str(source_plan_path),
        "--promotion-source-manifest",
        str(source_manifest_path),
        "--config",
        "release.yaml",
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert blocker in document["blockers"]
    assert document["status"] == "blocked"


def test_accepted_matching_rc_is_a_declaration_but_not_external_readback(
    tmp_path: Path,
) -> None:
    """Treating promotion declaration as registry readback would incorrectly mark production ready."""
    plan_path, plan = _production_plan(tmp_path, "stable", "0.6.0")
    manifest_path, _ = _manifest(tmp_path, plan)
    promotion_path, source_plan_path, source_manifest_path = _anchored_promotion(
        tmp_path,
        plan,
        source_stage="rc",
        source_version="0.6.0rc9",
        target_stage="stable",
        target_version="0.6.0",
    )
    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--promotion",
        str(promotion_path),
        "--promotion-source-lifecycle-plan",
        str(source_plan_path),
        "--promotion-source-manifest",
        str(source_manifest_path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["status"] == "conflict-free-preview"
    assert document["blockers"] == ["external-environment-evidence-required"]
    assert (
        document["promotion_evidence_sha256"]
        == json.loads(promotion_path.read_text(encoding="utf-8"))["sha256"]
    )
    assert document["production_ready"] is False


@pytest.mark.parametrize("source_version", ["0.6.0", "0.5.1", "0.6.2"])
def test_hotfix_requires_the_immediately_previous_same_minor_stable(
    tmp_path: Path,
    source_version: str,
) -> None:
    """Weakening the Hotfix base check would permit skipped patches or another minor line."""
    plan_path, plan = _production_plan(tmp_path, "hotfix", "0.6.2")
    manifest_path, _ = _manifest(tmp_path, plan)
    promotion_path, source_plan_path, source_manifest_path = _anchored_promotion(
        tmp_path,
        plan,
        source_stage="stable",
        source_version=source_version,
        target_stage="hotfix",
        target_version="0.6.2",
    )
    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--promotion",
        str(promotion_path),
        "--promotion-source-lifecycle-plan",
        str(source_plan_path),
        "--promotion-source-manifest",
        str(source_manifest_path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    expected_blocked = source_version != "0.6.1"
    assert (
        "promotion-source-version-mismatch" in document["blockers"]
    ) is expected_blocked
    assert document["status"] == (
        "blocked" if expected_blocked else "conflict-free-preview"
    )


@pytest.mark.parametrize(
    ("stage", "version", "draft_rc", "promotion_source"),
    [
        ("rc", "0.6.0rc1", "0.6.0rc1", None),
        ("stable", "0.6.0", "0.6.0rc9", "0.6.0rc9"),
        ("hotfix", "0.6.2", "0.6.2rc3", "0.6.1"),
    ],
)
def test_real_draft_request_result_replay_is_line_bound_but_never_opens_production(
    tmp_path: Path,
    stage: str,
    version: str,
    draft_rc: str,
    promotion_source: str | None,
) -> None:
    """Skipping Task 5 replay or release-line checks would accept unrelated simulation evidence."""
    plan_path, plan = _production_plan(tmp_path, stage, version)
    manifest_path, _ = _manifest(tmp_path, plan)
    (
        environment_plan_path,
        environment_manifest_path,
        request_path,
        result_path,
    ) = _draft_environment_package(tmp_path, draft_rc)
    promotion_args: list[str] = []
    if stage == "stable":
        promotion_path, source_plan_path, source_manifest_path = _anchored_promotion(
            tmp_path,
            plan,
            source_stage="rc",
            source_version=promotion_source or "",
            target_stage="stable",
            target_version=version,
        )
        promotion_args = [
            "--promotion",
            str(promotion_path),
            "--promotion-source-lifecycle-plan",
            str(source_plan_path),
            "--promotion-source-manifest",
            str(source_manifest_path),
        ]
    elif stage == "hotfix":
        promotion_path, source_plan_path, source_manifest_path = _anchored_promotion(
            tmp_path,
            plan,
            source_stage="stable",
            source_version=promotion_source or "",
            target_stage="hotfix",
            target_version=version,
        )
        promotion_args = [
            "--promotion",
            str(promotion_path),
            "--promotion-source-lifecycle-plan",
            str(source_plan_path),
            "--promotion-source-manifest",
            str(source_manifest_path),
        ]
    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--environment-request",
        str(request_path),
        "--environment-result",
        str(result_path),
        "--environment-lifecycle-plan",
        str(environment_plan_path),
        "--environment-manifest",
        str(environment_manifest_path),
        *promotion_args,
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    simulated = json.loads(result_path.read_text(encoding="utf-8"))
    assert document["simulated_environment"] == "draft-passed"
    assert document["environment_request_sha256"] == request["sha256"]
    assert document["environment_result_sha256"] == simulated["sha256"]
    assert "draft-simulated-evidence-only" in document["blockers"]
    assert "external-environment-evidence-required" in document["blockers"]
    assert document["production_ready"] is False


@pytest.mark.parametrize("mutation", ["nonce", "request-digest", "check", "verdict"])
def test_reconcile_delegates_environment_replay_mutations_to_task5_verifier(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Bypassing verify_result would accept a result that no longer replays its Draft request."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, _ = _manifest(tmp_path, plan)
    (
        environment_plan_path,
        environment_manifest_path,
        request_path,
        result_path,
    ) = _draft_environment_package(tmp_path, "0.6.0rc1")
    simulated = json.loads(result_path.read_text(encoding="utf-8"))
    if mutation == "nonce":
        simulated["nonce"] = "e" * 32
    elif mutation == "request-digest":
        simulated["request_sha256"] = "e" * 64
    elif mutation == "check":
        simulated["checks"][0]["status"] = "failed"
    else:
        simulated["verdict"] = "failed"
    result_path.write_text(json.dumps(_signed(simulated)), encoding="utf-8")

    rejected = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--environment-request",
        str(request_path),
        "--environment-result",
        str(result_path),
        "--environment-lifecycle-plan",
        str(environment_plan_path),
        "--environment-manifest",
        str(environment_manifest_path),
        "--config",
        "release.yaml",
    )
    assert rejected.returncode == 2
    assert "environment" in rejected.stderr
    assert "Traceback" not in rejected.stderr


def test_reconcile_rejects_resigned_environment_path_controls(tmp_path: Path) -> None:
    """Reconciliation must replay runtime path validation after every digest is recomputed."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, _ = _manifest(tmp_path, plan)
    request_path, result_path = _draft_environment_evidence(tmp_path, "0.6.0rc1")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    index = next(
        index
        for index, item in enumerate(request["artifacts"])
        if item["kind"] == "chart"
    )
    request["artifacts"][index]["path"] = "files/chart\rbreak.tgz"
    request = _signed(request)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    result["artifacts"][index]["path"] = "files/chart\rbreak.tgz"
    result["request_sha256"] = request["sha256"]
    result_path.write_text(json.dumps(_signed(result)), encoding="utf-8")

    rejected = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--environment-request",
        str(request_path),
        "--environment-result",
        str(result_path),
        "--config",
        "release.yaml",
    )
    assert rejected.returncode == 2
    assert "canonical safe POSIX path" in rejected.stderr
    assert "Traceback" not in rejected.stderr


@pytest.mark.parametrize(
    ("source_sha", "draft_rc"),
    [("b" * 40, "0.6.0rc1"), (SOURCE_SHA, "0.6.0rc2")],
)
def test_draft_evidence_rejects_cross_source_or_release_line(
    tmp_path: Path,
    source_sha: str,
    draft_rc: str,
) -> None:
    """A valid Task 5 replay from another source or RC line cannot justify this preview."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, _ = _manifest(tmp_path, plan)
    request_path, result_path = _draft_environment_evidence(
        tmp_path, draft_rc, source_sha=source_sha
    )
    rejected = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--environment-request",
        str(request_path),
        "--environment-result",
        str(result_path),
        "--config",
        "release.yaml",
    )
    assert rejected.returncode == 2
    assert "source" in rejected.stderr or "release line" in rejected.stderr
    assert "Traceback" not in rejected.stderr


@pytest.mark.parametrize("provided", ["request", "result"])
def test_environment_request_and_result_must_be_provided_as_a_pair(
    tmp_path: Path,
    provided: str,
) -> None:
    """A half replay package must not be interpreted as verified Draft evidence."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, _ = _manifest(tmp_path, plan)
    request_path, result_path = _draft_environment_evidence(tmp_path, "0.6.0rc1")
    pair_args = (
        ["--environment-request", str(request_path)]
        if provided == "request"
        else ["--environment-result", str(result_path)]
    )
    rejected = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        *pair_args,
        "--config",
        "release.yaml",
    )
    assert rejected.returncode == 2
    assert "together" in rejected.stderr
    assert "Traceback" not in rejected.stderr


def test_stable_draft_evidence_requires_promotion_to_define_the_rc_line(
    tmp_path: Path,
) -> None:
    """Missing Stable ancestry must preserve a blocked preview without an internal assertion."""
    plan_path, plan = _production_plan(tmp_path, "stable", "0.6.0")
    manifest_path, _ = _manifest(tmp_path, plan)
    (
        environment_plan_path,
        environment_manifest_path,
        request_path,
        result_path,
    ) = _draft_environment_package(tmp_path, "0.6.0rc1")
    rejected = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--environment-request",
        str(request_path),
        "--environment-result",
        str(result_path),
        "--environment-lifecycle-plan",
        str(environment_plan_path),
        "--environment-manifest",
        str(environment_manifest_path),
        "--config",
        "release.yaml",
    )
    assert rejected.returncode == 0, rejected.stderr
    document = json.loads(rejected.stdout)
    assert document["simulated_environment"] == "not-eligible"
    assert "promotion-evidence-required" in document["blockers"]
    assert "draft-environment-promotion-ineligible" in document["blockers"]
    assert document["environment_request_sha256"] is not None
    assert document["environment_result_sha256"] is not None


@pytest.mark.parametrize(
    ("mutation", "expected_blocker"),
    [
        ("unaccepted", "promotion-not-accepted"),
        ("target-version", "promotion-target-version-mismatch"),
        ("source-stage", "promotion-source-stage-mismatch"),
        ("source-version", "promotion-source-version-mismatch"),
    ],
)
def test_stable_ineligible_promotion_never_classifies_real_draft_pair_as_passed(
    tmp_path: Path,
    mutation: str,
    expected_blocker: str,
) -> None:
    """Using arbitrary promotion source_version would falsely label Draft replay as eligible."""
    plan_path, plan = _production_plan(tmp_path, "stable", "0.6.0")
    manifest_path, _ = _manifest(tmp_path, plan)
    source_stage = "rc"
    source_version = "0.6.0rc9"
    target_version = "0.6.0"
    accepted = True
    if mutation == "unaccepted":
        accepted = False
    elif mutation == "target-version":
        target_version = "0.6.1"
    elif mutation == "source-stage":
        source_stage = "stable"
        source_version = "0.6.0"
    else:
        source_version = "0.6.1rc1"
    promotion_path, source_plan_path, source_manifest_path = _anchored_promotion(
        tmp_path,
        plan,
        source_stage=source_stage,
        source_version=source_version,
        target_stage="stable",
        target_version=target_version,
        accepted=accepted,
    )
    (
        environment_plan_path,
        environment_manifest_path,
        request_path,
        result_path,
    ) = _draft_environment_package(
        tmp_path, source_version if source_stage == "rc" else "0.6.0rc9"
    )

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--promotion",
        str(promotion_path),
        "--promotion-source-lifecycle-plan",
        str(source_plan_path),
        "--promotion-source-manifest",
        str(source_manifest_path),
        "--environment-request",
        str(request_path),
        "--environment-result",
        str(result_path),
        "--environment-lifecycle-plan",
        str(environment_plan_path),
        "--environment-manifest",
        str(environment_manifest_path),
        "--config",
        "release.yaml",
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["simulated_environment"] == "not-eligible"
    assert "draft-environment-promotion-ineligible" in document["blockers"]
    assert expected_blocker in document["blockers"]
    assert document["simulated_environment"] != "draft-passed"
    assert document["production_ready"] is False


def test_failed_real_draft_replay_is_preserved_as_simulated_only(
    tmp_path: Path,
) -> None:
    """A valid failed Task 5 result must remain visible without changing the production gate."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, _ = _manifest(tmp_path, plan)
    (
        environment_plan_path,
        environment_manifest_path,
        request_path,
        result_path,
    ) = _draft_environment_package(tmp_path, "0.6.0rc1", verdict="failed")
    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--environment-request",
        str(request_path),
        "--environment-result",
        str(result_path),
        "--environment-lifecycle-plan",
        str(environment_plan_path),
        "--environment-manifest",
        str(environment_manifest_path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["simulated_environment"] == "draft-failed"
    assert "draft-simulated-evidence-only" in document["blockers"]
    assert document["production_ready"] is False


def test_rc_rejects_promotion_and_execute_is_not_a_command_surface(
    tmp_path: Path,
) -> None:
    """Adding an execution flag or RC ancestry input would broaden this read-only interface."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, _ = _manifest(tmp_path, plan)
    inventory_path = _inventory(tmp_path, [])
    promotion_path = _promotion(
        tmp_path,
        source_stage="rc",
        source_version="0.6.0rc1",
        target_stage="rc",
        target_version="0.6.0rc1",
    )
    rejected = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory_path),
        "--promotion",
        str(promotion_path),
        "--config",
        "release.yaml",
    )
    assert rejected.returncode == 2
    assert "RC" in rejected.stderr or "rc" in rejected.stderr

    attempted = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory_path),
        "--execute",
        "--config",
        "release.yaml",
    )
    assert attempted.returncode == 2
    assert "unrecognized arguments: --execute" in attempted.stderr
    assert 'executed":true' not in attempted.stdout.replace(" ", "")


@pytest.mark.parametrize(
    "mutation",
    ["duplicate-key", "duplicate-target", "unknown-target", "bad-identity-type"],
)
def test_inventory_rejects_duplicates_unknown_targets_and_malformed_types_without_traceback(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Relaxing inventory parsing would make conflict decisions ambiguous."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, manifest = _manifest(tmp_path, plan)
    inventory_path = tmp_path / "inventory.json"
    targets = _targets(manifest)
    if mutation == "duplicate-key":
        inventory_path.write_text(
            '{"kind":"release-inventory","kind":"release-inventory",'
            '"schema_version":2,"mode":"read-only","targets":[]}',
            encoding="utf-8",
        )
    elif mutation == "duplicate-target":
        inventory_path = _inventory(tmp_path, [targets[0], targets[0]])
    elif mutation == "unknown-target":
        targets[0]["name"] = "unknown-product"
        inventory_path = _inventory(tmp_path, [targets[0]])
    else:
        targets[0]["identity"] = ["not", "a", "digest"]
        inventory_path = _inventory(tmp_path, [targets[0]])

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory_path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_reconcile_schema_is_strict_and_all_operations_are_non_executing() -> None:
    """A schema mutation must not add write-capable operation fields or loose counts."""
    schema = json.loads(
        (V2_ROOT / "schemas/reconcile-plan.schema.json").read_text(encoding="utf-8")
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["environment_request_sha256"]["oneOf"]
    assert schema["properties"]["environment_result_sha256"]["oneOf"]
    assert "not-eligible" in schema["properties"]["simulated_environment"]["enum"]
    operations = schema["properties"]["operations"]
    assert operations["minItems"] == operations["maxItems"] == 7
    operation = schema["$defs"]["operation"]
    assert operation["additionalProperties"] is False
    assert set(operation["required"]) == {"action", "executed", "identity", "target"}
    assert operation["properties"]["executed"] == {"const": False}


def test_stable_promotion_is_eligible_only_with_full_anchored_rc_lineage(
    tmp_path: Path,
) -> None:
    """Catches a self-signed promotion declaration replacing its RC plan and manifest."""
    plan_path, plan = _production_plan(tmp_path, "stable", "0.6.0")
    manifest_path, _ = _manifest(tmp_path, plan)
    promotion, source_plan, source_manifest = _anchored_promotion(
        tmp_path,
        plan,
        source_stage="rc",
        source_version="0.6.0rc9",
        target_stage="stable",
        target_version="0.6.0",
    )

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--promotion",
        str(promotion),
        "--promotion-source-lifecycle-plan",
        str(source_plan),
        "--promotion-source-manifest",
        str(source_manifest),
        "--config",
        "release.yaml",
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["blockers"] == ["external-environment-evidence-required"]
    assert (
        document["promotion_source_lifecycle_plan_sha256"]
        == json.loads(source_plan.read_text(encoding="utf-8"))["sha256"]
    )
    assert (
        document["promotion_source_manifest_sha256"]
        == json.loads(source_manifest.read_text(encoding="utf-8"))["sha256"]
    )
    assert document["production_ready"] is False


def test_unanchored_promotion_is_an_explicit_blocker_and_partial_anchor_errors(
    tmp_path: Path,
) -> None:
    """Catches promotion evidence becoming eligible when either lineage input is absent."""
    plan_path, plan = _production_plan(tmp_path, "stable", "0.6.0")
    manifest_path, _ = _manifest(tmp_path, plan)
    promotion, source_plan, _ = _anchored_promotion(
        tmp_path,
        plan,
        source_stage="rc",
        source_version="0.6.0rc1",
        target_stage="stable",
        target_version="0.6.0",
    )
    common = [
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--promotion",
        str(promotion),
    ]

    unanchored = _run(*common, "--config", "release.yaml")
    partial = _run(
        *common,
        "--promotion-source-lifecycle-plan",
        str(source_plan),
        "--config",
        "release.yaml",
    )

    assert unanchored.returncode == 0, unanchored.stderr
    assert "promotion-unanchored" in json.loads(unanchored.stdout)["blockers"]
    assert partial.returncode == 2
    assert "together" in partial.stderr
    assert "Traceback" not in partial.stderr


@pytest.mark.parametrize(
    "mutation", ["plan-digest", "manifest-digest", "source-sha", "stable-cross-sha"]
)
def test_promotion_anchor_rejects_digest_field_and_stable_sha_drift(
    tmp_path: Path, mutation: str
) -> None:
    """Catches re-signed promotion fields drifting from the reopened source lineage."""
    plan_path, plan = _production_plan(tmp_path, "stable", "0.6.0")
    manifest_path, _ = _manifest(tmp_path, plan)
    source_sha = "b" * 40 if mutation == "stable-cross-sha" else SOURCE_SHA
    promotion, source_plan, source_manifest = _anchored_promotion(
        tmp_path,
        plan,
        source_stage="rc",
        source_version="0.6.0rc1",
        target_stage="stable",
        target_version="0.6.0",
        source_sha=source_sha,
    )
    evidence = json.loads(promotion.read_text(encoding="utf-8"))
    if mutation == "plan-digest":
        evidence["source_lifecycle_plan_sha256"] = "e" * 64
    elif mutation == "manifest-digest":
        evidence["source_artifact_manifest_sha256"] = "f" * 64
    elif mutation == "source-sha":
        evidence["source_sha"] = "c" * 40
    promotion.write_text(json.dumps(_signed(evidence)), encoding="utf-8")

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--promotion",
        str(promotion),
        "--promotion-source-lifecycle-plan",
        str(source_plan),
        "--promotion-source-manifest",
        str(source_manifest),
        "--config",
        "release.yaml",
    )

    if mutation == "stable-cross-sha":
        assert result.returncode == 0, result.stderr
        assert "promotion-source-sha-mismatch" in json.loads(result.stdout)["blockers"]
    else:
        assert result.returncode == 2
        assert "promotion" in result.stderr
        assert "Traceback" not in result.stderr


def test_hotfix_anchor_allows_previous_stable_to_have_a_different_source_sha(
    tmp_path: Path,
) -> None:
    """Catches incorrectly forcing Hotfix code SHA to equal its previous Stable anchor."""
    plan_path, plan = _production_plan(tmp_path, "hotfix", "0.6.2", SOURCE_SHA)
    manifest_path, _ = _manifest(tmp_path, plan)
    promotion, source_plan, source_manifest = _anchored_promotion(
        tmp_path,
        plan,
        source_stage="stable",
        source_version="0.6.1",
        target_stage="hotfix",
        target_version="0.6.2",
        source_sha="b" * 40,
    )

    result = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--promotion",
        str(promotion),
        "--promotion-source-lifecycle-plan",
        str(source_plan),
        "--promotion-source-manifest",
        str(source_manifest),
        "--config",
        "release.yaml",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["blockers"] == [
        "external-environment-evidence-required"
    ]


@pytest.mark.parametrize(
    ("stage", "version", "draft_version", "promotion_stage", "promotion_version"),
    [
        ("rc", "0.6.0rc1", "0.6.0rc1", None, None),
        ("stable", "0.6.0", "0.6.0rc9", "rc", "0.6.0rc9"),
        ("hotfix", "0.6.2", "0.6.2rc3", "stable", "0.6.1"),
    ],
)
def test_environment_replay_requires_and_accepts_original_draft_anchors(
    tmp_path: Path,
    stage: str,
    version: str,
    draft_version: str,
    promotion_stage: str | None,
    promotion_version: str | None,
) -> None:
    """Catches a request/result self-digest being mistaken for proof of Draft origin."""
    plan_path, plan = _production_plan(tmp_path, stage, version)
    manifest_path, _ = _manifest(tmp_path, plan)
    environment_plan, environment_manifest, request, result_path = (
        _draft_environment_package(tmp_path, draft_version)
    )
    extra: list[str] = []
    if promotion_stage is not None:
        promotion, source_plan, source_manifest = _anchored_promotion(
            tmp_path,
            plan,
            source_stage=promotion_stage,
            source_version=promotion_version or "",
            target_stage=stage,
            target_version=version,
            source_sha=("b" * 40 if stage == "hotfix" else SOURCE_SHA),
        )
        extra = [
            "--promotion",
            str(promotion),
            "--promotion-source-lifecycle-plan",
            str(source_plan),
            "--promotion-source-manifest",
            str(source_manifest),
        ]

    completed = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--environment-lifecycle-plan",
        str(environment_plan),
        "--environment-manifest",
        str(environment_manifest),
        "--environment-request",
        str(request),
        "--environment-result",
        str(result_path),
        *extra,
        "--config",
        "release.yaml",
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["simulated_environment"] == "draft-passed"
    assert (
        document["environment_lifecycle_plan_sha256"]
        == json.loads(environment_plan.read_text(encoding="utf-8"))["sha256"]
    )
    assert (
        document["environment_manifest_sha256"]
        == json.loads(environment_manifest.read_text(encoding="utf-8"))["sha256"]
    )
    assert document["production_ready"] is False


def test_internally_resigned_environment_pair_without_origin_is_unanchored(
    tmp_path: Path,
) -> None:
    """Catches internally consistent e/f digest substitution being called Draft-passed."""
    plan_path, plan = _production_plan(tmp_path)
    manifest_path, _ = _manifest(tmp_path, plan)
    _, _, request_path, result_path = _draft_environment_package(tmp_path, "0.6.0rc1")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    request["lifecycle_plan_sha256"] = "e" * 64
    request["artifact_manifest_sha256"] = "f" * 64
    request = _signed(request)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    result["lifecycle_plan_sha256"] = "e" * 64
    result["artifact_manifest_sha256"] = "f" * 64
    result["request_sha256"] = request["sha256"]
    result_path.write_text(json.dumps(_signed(result)), encoding="utf-8")

    completed = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(_inventory(tmp_path, [])),
        "--environment-request",
        str(request_path),
        "--environment-result",
        str(result_path),
        "--config",
        "release.yaml",
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["simulated_environment"] == "unanchored-simulation"
    assert "draft-environment-unanchored" in document["blockers"]
    assert document["simulated_environment"] != "draft-passed"
