from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from ucm_release_v2.artifacts import ArtifactError, _artifact_file
from ucm_release_v2.cleanup import build_cleanup_plan
from ucm_release_v2.config import load_config
from ucm_release_v2.render import _known_issues
from ucm_release_v2.security import audit_repository

V2_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = V2_ROOT.parents[2]
CONFIG = V2_ROOT / "release.yaml"
FIXTURES = V2_ROOT / "tests/fixtures"
SHA = "a" * 40
FILE_SHA = "b" * 64
OCI_SHA = "sha256:" + "c" * 64
WORKFLOWS = (
    "develop-release-dry-run.yml",
    "draft-environment-dry-run.yml",
    "nightly-release-dry-run.yml",
    "pr-release-dry-run.yml",
    "release-cleanup-dry-run.yml",
    "release-control-dry-run.yml",
    "release-lifecycle-dry-run.yml",
    "repository-policy-audit-dry-run.yml",
)
SCHEMAS_BY_KIND = {
    "artifact-manifest": "artifact-manifest.schema.json",
    "artifact-manifest-validation": "artifact-manifest-validation.schema.json",
    "cleanup-plan": "cleanup-plan.schema.json",
    "environment-test-request": "environment-test-request.schema.json",
    "environment-test-result": "environment-test-result.schema.json",
    "environment-verification": "environment-verification.schema.json",
    "lifecycle-plan": "lifecycle-plan.schema.json",
    "lifecycle-plan-validation": "lifecycle-plan-validation.schema.json",
    "reconcile-plan": "reconcile-plan.schema.json",
    "release-command": "release-command.schema.json",
    "repository-policy-report": "repository-policy-report.schema.json",
    "retention-policy": "retention-policy.schema.json",
    "ucm-release-lifecycle-config": "release-config.schema.json",
    "wheel-environment-check": "wheel-environment-check.schema.json",
    "wheel-plan": "wheel-plan.schema.json",
}


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": str(V2_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def _cleanup_inventory() -> dict[str, object]:
    return {
        "kind": "cleanup-inventory",
        "schema_version": 2,
        "mode": "read-only",
        "objects": [
            {
                "id": "pr-old",
                "kind": "artifact",
                "channel": "pr",
                "coordinate": "artifact/pr-old.whl",
                "identity": FILE_SHA,
                "created_at": "2026-08-01T00:00:00Z",
                "state": "temporary",
            },
            {
                "id": "draft-old",
                "kind": "image",
                "channel": "draft",
                "coordinate": "ghcr.io/example/ucm:draft",
                "identity": OCI_SHA,
                "created_at": "2026-07-01T00:00:00Z",
                "state": "temporary",
            },
        ],
        "references": [
            {
                "id": "draft-live",
                "object_id": "draft-old",
                "identity": OCI_SHA,
                "source": "active-draft",
                "active": False,
            },
            {
                "id": "pr-history",
                "object_id": "pr-old",
                "identity": FILE_SHA,
                "source": "shared-object",
                "active": False,
            },
        ],
        "failures": [
            {"object_id": "draft-old", "reason": "offline inventory failure"},
            {"object_id": "pr-old", "reason": "offline artifact failure"},
        ],
    }


def _stage_args(stage: str) -> list[str]:
    common = [
        "lifecycle",
        "plan",
        "--stage",
        stage,
        "--source-sha",
        SHA,
        "--config",
        str(CONFIG),
    ]
    if stage == "pr":
        return common + [
            "--trigger",
            "pull_request",
            "--ref",
            "refs/pull/42/head",
            "--repository-role",
            "validation",
            "--pr-number",
            "42",
        ]
    if stage == "develop":
        return common + [
            "--trigger",
            "push",
            "--ref",
            "refs/heads/develop",
            "--repository-role",
            "validation",
            "--run-number",
            "17",
        ]
    if stage == "nightly":
        return common + [
            "--trigger",
            "schedule",
            "--ref",
            "refs/heads/develop",
            "--repository-role",
            "validation",
            "--date",
            "2026-08-12",
        ]
    version = {
        "draft": "0.6.0rc1",
        "rc": "0.6.0rc1",
        "stable": "0.6.0",
        "hotfix": "0.6.1",
    }[stage]
    intent = {"source_sha": SHA, "stage": stage, "version": version}
    return common + [
        "--trigger",
        "workflow_dispatch",
        "--ref",
        "refs/heads/main",
        "--repository-role",
        "production",
        "--intent-json",
        json.dumps(intent, sort_keys=True),
    ]


def _plan_file(tmp_path: Path, stage: str) -> tuple[Path, dict[str, Any]]:
    path = tmp_path / f"{stage}-lifecycle-plan.json"
    completed = _run(tmp_path, *_stage_args(stage), "--output", str(path))
    assert completed.returncode == 0, completed.stderr
    return path, json.loads(path.read_text(encoding="utf-8"))


def _manifest(tmp_path: Path, stage: str) -> tuple[Path, Path, Path, dict[str, Any]]:
    plan_path, plan = _plan_file(tmp_path, stage)
    base = tmp_path / f"{stage}-artifacts"
    base.mkdir()
    records: list[dict[str, Any]] = []
    for product in plan["products"]:
        label = f"{product['kind']}:{product['name']}:{product['coordinate']}"
        if product["kind"] == "image":
            records.append(
                {
                    **product,
                    "digest": "sha256:"
                    + hashlib.sha256((label + ":index").encode()).hexdigest(),
                    "platforms": [
                        {
                            "platform": "linux/arm64",
                            "digest": "sha256:"
                            + hashlib.sha256((label + ":arm64").encode()).hexdigest(),
                        },
                        {
                            "platform": "linux/amd64",
                            "digest": "sha256:"
                            + hashlib.sha256((label + ":amd64").encode()).hexdigest(),
                        },
                    ],
                }
            )
        else:
            relative = f"files/{product['name']}.bin"
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((label + "\n").encode())
            records.append({**product, "path": relative})
    records_path = tmp_path / f"{stage}-artifact-records.json"
    records_path.write_text(json.dumps(list(reversed(records))), encoding="utf-8")
    manifest_path = tmp_path / f"{stage}-artifact-manifest.json"
    completed = _run(
        tmp_path,
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
        str(CONFIG),
    )
    assert completed.returncode == 0, completed.stderr
    return (
        plan_path,
        manifest_path,
        base,
        json.loads(manifest_path.read_text(encoding="utf-8")),
    )


def _resign(document: dict[str, Any]) -> dict[str, Any]:
    unsigned = deepcopy(document)
    unsigned.pop("sha256", None)
    unsigned["sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return unsigned


def _environment_result(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    plan_path, manifest_path, _, _ = _manifest(tmp_path, "draft")
    request_path = tmp_path / "scenario-environment-request.json"
    exported = _run(
        tmp_path,
        "environment",
        "export",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--environment",
        "yellow",
        "--nonce",
        "d" * 32,
        "--output",
        str(request_path),
        "--config",
        str(CONFIG),
    )
    assert exported.returncode == 0, exported.stderr
    result_path = tmp_path / "scenario-environment-result.json"
    simulated = _run(
        tmp_path,
        "environment",
        "simulate",
        "--request",
        str(request_path),
        "--verdict",
        "passed",
        "--output",
        str(result_path),
        "--config",
        str(CONFIG),
    )
    assert simulated.returncode == 0, simulated.stderr
    return (
        request_path,
        result_path,
        json.loads(result_path.read_text(encoding="utf-8")),
    )


def test_nightly_rejects_an_impossible_calendar_date_from_a_clean_cwd(
    tmp_path: Path,
) -> None:
    """Catches a YYYY-MM-DD-shaped value that is not a real calendar date."""
    completed = _run(
        tmp_path,
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
        "2026-99-99",
        "--config",
        str(CONFIG),
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "valid calendar date" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_draft_workflow_rejects_nested_duplicate_intent_keys(
    tmp_path: Path,
) -> None:
    """Catches any nested duplicate JSON field being silently last-wins decoded."""
    workflow = yaml.safe_load(
        (REPOSITORY_ROOT / ".github/workflows/release-control-dry-run.yml").read_text(
            encoding="utf-8"
        )
    )
    run = next(
        step["run"]
        for step in workflow["jobs"]["simulated-environment"]["steps"]
        if step.get("name") == "Validate immutable manual identity"
    )
    match = re.search(r"python - <<'PY'\n(?P<script>.*)\nPY\s*$", run, re.DOTALL)
    assert match is not None
    (tmp_path / "preview").mkdir()
    completed = subprocess.run(
        ["python3", "-c", match.group("script")],
        cwd=tmp_path,
        env=os.environ
        | {
            "INTENT_JSON": (
                '{"source_sha":"'
                + SHA
                + '","stage":"draft","version":{"candidate":1,"candidate":2}}'
            ),
            "NONCE": "d" * 32,
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "duplicate key" in completed.stderr
    assert not (tmp_path / "preview/release-intent.json").exists()


def test_environment_result_schema_has_only_internal_references() -> None:
    """Catches standalone offline validation depending on a sibling schema registry."""
    schema = json.loads(
        (V2_ROOT / "schemas/environment-test-result.schema.json").read_text(
            encoding="utf-8"
        )
    )

    def references(value: object) -> list[str]:
        if isinstance(value, dict):
            return [
                child
                for key, item in value.items()
                for child in (([item] if key == "$ref" else references(item)))
            ]
        if isinstance(value, list):
            return [child for item in value for child in references(item)]
        return []

    assert references(schema)
    assert all(reference.startswith("#/") for reference in references(schema))


def test_v2_uses_content_addressed_not_signed_terminology() -> None:
    """Catches a recomputable self-digest being presented as a cryptographic signature."""
    files = sorted((V2_ROOT / "ucm_release_v2").glob("*.py")) + [V2_ROOT / "README.md"]
    occurrences = {
        path.relative_to(V2_ROOT).as_posix(): sorted(
            set(
                re.findall(
                    r"\bsign(?:ed|ature|ing)?\b", path.read_text(encoding="utf-8"), re.I
                )
            )
        )
        for path in files
        if re.search(
            r"\bsign(?:ed|ature|ing)?\b", path.read_text(encoding="utf-8"), re.I
        )
    }

    assert occurrences == {}


def test_known_issues_escape_markdown_block_and_inline_controls(tmp_path: Path) -> None:
    """Catches issue text escaping its list item through Markdown control syntax."""
    path = tmp_path / "known-issues.json"
    path.write_text(
        json.dumps(["# heading > quote ~~strike~~ ![alt](url) ```fence```"]),
        encoding="utf-8",
    )

    escaped = _known_issues(path)[0]

    assert escaped == (
        r"\# heading &gt; quote \~\~strike\~\~ \!\[alt\](url) " r"\`\`\`fence\`\`\`"
    )


@pytest.mark.parametrize(
    "unsafe", ["artifacts/bad\nname.whl", "artifacts/bad\rname.whl"]
)
def test_artifact_paths_reject_line_controls_even_for_existing_files(
    tmp_path: Path, unsafe: str
) -> None:
    """Catches a valid POSIX filename injecting a Markdown or shell line boundary."""
    target = tmp_path.joinpath(*unsafe.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fixture")

    with pytest.raises(ArtifactError, match="safe POSIX path"):
        _artifact_file(tmp_path, unsafe)


def test_cleanup_cli_reports_year_underflow_without_traceback(tmp_path: Path) -> None:
    """Catches datetime retention subtraction leaking OverflowError at year one."""
    inventory = _cleanup_inventory()
    inventory["objects"] = [
        {
            "id": "year-one",
            "kind": "artifact",
            "channel": "pr",
            "coordinate": "artifact/year-one.whl",
            "identity": FILE_SHA,
            "created_at": "0001-01-01T00:00:00Z",
            "state": "temporary",
        }
    ]
    inventory["references"] = []
    inventory["failures"] = []
    inventory_path = tmp_path / "cleanup-inventory.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    completed = _run(
        tmp_path,
        "cleanup",
        "plan",
        "--inventory",
        str(inventory_path),
        "--as-of",
        "0001-01-01T00:00:00Z",
        "--config",
        str(CONFIG),
    )

    assert completed.returncode == 2
    assert "retention boundary" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_cleanup_plan_is_identical_across_all_inventory_array_reorders() -> None:
    """Catches an input-order-dependent inventory digest changing an otherwise equal plan."""
    config = load_config(CONFIG)
    original = _cleanup_inventory()
    reordered = deepcopy(original)
    for key in ("objects", "references", "failures"):
        assert isinstance(reordered[key], list)
        reordered[key].reverse()

    first = build_cleanup_plan(config, original, "2026-08-12T00:00:00Z")
    second = build_cleanup_plan(config, reordered, "2026-08-12T00:00:00Z")

    assert first == second
    assert first["sha256"] == second["sha256"]
    assert first["inventory_sha256"] == second["inventory_sha256"]


@pytest.mark.parametrize(
    "stage", ["pr", "develop", "nightly", "draft", "rc", "stable", "hotfix"]
)
def test_all_seven_stages_run_as_real_subprocesses_from_a_clean_cwd(
    tmp_path: Path, stage: str
) -> None:
    """Catches a lifecycle route depending on repository-relative cwd or mutable defaults."""
    first_path, first = _plan_file(tmp_path, stage)
    first_bytes = first_path.read_bytes()
    first_path.unlink()
    second_path, second = _plan_file(tmp_path, stage)

    assert first == second
    assert first_bytes == second_path.read_bytes()
    assert first["stage"] == stage
    assert first["mode"] == "dry-run"
    assert all(operation["executed"] is False for operation in first["operations"])


def test_adversarial_cli_scenario_matrix_fails_closed_from_a_clean_cwd(
    tmp_path: Path,
) -> None:
    """Catches stale identity, mixed installs, byte drift, replay, and policy gaps in one flow."""
    stale = _run(
        tmp_path,
        "command",
        "parse",
        "--body",
        f"/release build {SHA}",
        "--actor",
        "reviewer",
        "--author-association",
        "MEMBER",
        "--observed-source-sha",
        SHA,
        "--current-source-sha",
        "b" * 40,
    )
    assert stale.returncode == 0, stale.stderr
    assert json.loads(stale.stdout)["reason"] == "stale-pr-sha"

    mixed = _run(
        tmp_path,
        "wheel",
        "check-environment",
        "--installed-json",
        str(FIXTURES / "installed-mixed-all.json"),
        "--config",
        str(CONFIG),
    )
    assert mixed.returncode == 2
    assert "uc-manager-cann-a3" in mixed.stderr
    assert "Traceback" not in mixed.stderr

    plan_path, manifest_path, base, manifest = _manifest(tmp_path, "draft")
    file_artifact = next(item for item in manifest["artifacts"] if "path" in item)
    (base / file_artifact["path"]).write_bytes(b"drift")
    drift = _run(
        tmp_path,
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(base),
        "--config",
        str(CONFIG),
    )
    assert drift.returncode == 2
    assert "checksum or size" in drift.stderr

    for fixture, expected in (
        ("repository-policy-compliant.json", True),
        ("repository-policy-fork-gaps.json", False),
    ):
        audited = _run(
            tmp_path,
            "repo-policy",
            "audit",
            "--snapshot",
            str(FIXTURES / fixture),
            "--repository-role",
            "validation",
            "--config",
            str(CONFIG),
        )
        assert audited.returncode == 0, audited.stderr
        assert json.loads(audited.stdout)["compliant"] is expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_sha256", "f" * 64),
        ("nonce", "e" * 32),
        ("source_sha", "b" * 40),
        ("version", "0.7.0rc1.dev0+draft.g." + "a" * 12),
    ],
)
def test_environment_result_rejects_replay_nonce_source_and_release_line_drift(
    tmp_path: Path, field: str, value: str
) -> None:
    """Catches a self-digested result being replayed across request or release identity."""
    request_path, result_path, result = _environment_result(tmp_path)
    result[field] = value
    result_path.write_text(json.dumps(_resign(result)), encoding="utf-8")

    completed = _run(
        tmp_path,
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--config",
        str(CONFIG),
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr


def test_environment_result_rejects_forged_production_evidence(tmp_path: Path) -> None:
    """Catches dry-run evidence acquiring an undeclared production-success field."""
    request_path, result_path, result = _environment_result(tmp_path)
    result["production_evidence"] = {"status": "passed"}
    result_path.write_text(json.dumps(_resign(result)), encoding="utf-8")

    completed = _run(
        tmp_path,
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--config",
        str(CONFIG),
    )
    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr


def _inventory_target(artifact: dict[str, Any]) -> dict[str, str]:
    return {
        "coordinate": artifact["coordinate"],
        "identity": artifact.get("digest", artifact.get("sha256")),
        "kind": artifact["kind"],
        "name": artifact["name"],
    }


def test_reconcile_is_byte_stable_allskip_after_apply_and_reverse_conflict_safe(
    tmp_path: Path,
) -> None:
    """Catches rerun drift, non-idempotent applied state, or reverse coordinate occupancy."""
    plan_path, manifest_path, _, manifest = _manifest(tmp_path, "stable")
    empty_inventory = FIXTURES / "release-inventory-empty.json"
    args = [
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(empty_inventory),
        "--config",
        str(CONFIG),
    ]
    first = _run(tmp_path, *args)
    second = _run(tmp_path, *args)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout

    applied_inventory = {
        "kind": "release-inventory",
        "schema_version": 2,
        "mode": "read-only",
        "targets": [
            _inventory_target(item) for item in reversed(manifest["artifacts"])
        ],
    }
    applied_path = tmp_path / "release-inventory-applied.json"
    applied_path.write_text(json.dumps(applied_inventory), encoding="utf-8")
    applied = _run(
        tmp_path,
        *[
            str(applied_path) if argument == str(empty_inventory) else argument
            for argument in args
        ],
    )
    assert applied.returncode == 0, applied.stderr
    applied_plan = json.loads(applied.stdout)
    assert {operation["action"] for operation in applied_plan["operations"]} == {
        "skip-identical"
    }

    wheels = [item for item in manifest["artifacts"] if item["kind"] == "wheel"]
    reverse_target = _inventory_target(wheels[0])
    reverse_target["coordinate"] = wheels[1]["coordinate"]
    conflict_inventory = {
        "kind": "release-inventory",
        "schema_version": 2,
        "mode": "read-only",
        "targets": [reverse_target],
    }
    conflict_path = tmp_path / "release-inventory-reverse-conflict.json"
    conflict_path.write_text(json.dumps(conflict_inventory), encoding="utf-8")
    conflict = _run(
        tmp_path,
        *[
            str(conflict_path) if argument == str(empty_inventory) else argument
            for argument in args
        ],
    )
    assert conflict.returncode == 0, conflict.stderr
    conflict_plan = json.loads(conflict.stdout)
    assert conflict_plan["status"] == "blocked"
    assert any(
        operation["action"] == "conflict" for operation in conflict_plan["operations"]
    )


def test_cleanup_scenario_enforces_7_14_30_boundaries_and_live_protection(
    tmp_path: Path,
) -> None:
    """Catches inclusive-boundary deletion or removal of shared/live/protected content."""
    completed = _run(
        tmp_path,
        "cleanup",
        "plan",
        "--inventory",
        str(FIXTURES / "cleanup-inventory.json"),
        "--as-of",
        "2026-08-12T00:00:00Z",
        "--config",
        str(CONFIG),
    )
    assert completed.returncode == 0, completed.stderr
    operations = {
        operation["object_id"]: operation
        for operation in json.loads(completed.stdout)["operations"]
    }
    assert operations["pr-boundary"]["reason"] == "not-expired"
    assert operations["develop-boundary"]["reason"] == "not-expired"
    assert operations["draft-boundary"]["reason"] == "not-expired"
    assert operations["develop-old"]["action"] == "delete-preview"
    assert operations["draft-old"]["action"] == "delete-preview"
    assert operations["nightly-live"]["reason"] == "shared-or-live-reference"
    assert operations["stable-live"]["reason"] == "protected-channel"


def test_release_render_is_byte_stable_and_escapes_adversarial_known_issues(
    tmp_path: Path,
) -> None:
    """Catches Markdown control injection or a preview implying planned artifacts exist."""
    plan_path, manifest_path, _, _ = _manifest(tmp_path, "stable")
    inventory = FIXTURES / "release-inventory-empty.json"
    reconcile_path = tmp_path / "render-reconcile-plan.json"
    reconciled = _run(
        tmp_path,
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory),
        "--output",
        str(reconcile_path),
        "--config",
        str(CONFIG),
    )
    assert reconciled.returncode == 0, reconciled.stderr
    args = [
        "release",
        "render",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory),
        "--reconcile-plan",
        str(reconcile_path),
        "--known-issues-json",
        str(FIXTURES / "known-issues-adversarial.json"),
        "--config",
        str(CONFIG),
    ]
    first = _run(tmp_path, *args)
    second = _run(tmp_path, *args)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert "DO NOT INSTALL OR PULL" in first.stdout
    assert "planned, not published" in first.stdout
    assert r"\# heading &gt; quote \~\~strike\~\~" in first.stdout
    assert r"\!\[alt\](https://example.invalid/image.png)" in first.stdout


def test_static_security_audit_accepts_only_closed_current_read_only_surface() -> None:
    """Catches a local executor, publisher, deleter, network client, or mutable workflow route."""
    workflows = [REPOSITORY_ROOT / ".github/workflows" / name for name in WORKFLOWS]
    assert audit_repository(V2_ROOT, workflows) == []


def test_every_json_cli_contract_has_a_strict_draft_2020_12_schema() -> None:
    """Catches an emitted JSON kind lacking a closed, exact local contract."""
    schemas = V2_ROOT / "schemas"
    expected_schemas = set(SCHEMAS_BY_KIND.values()) | {"release-intent.schema.json"}
    assert {path.name for path in schemas.glob("*.schema.json")} == expected_schemas
    for filename in expected_schemas:
        schema = json.loads((schemas / filename).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False

        def assert_closed(value: object, *, conditional: bool = False) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object" and not conditional:
                    assert value.get("additionalProperties") is False
                for key, child in value.items():
                    assert_closed(
                        child,
                        conditional=conditional
                        or key in {"contains", "if", "then", "else"},
                    )
            elif isinstance(value, list):
                for child in value:
                    assert_closed(child, conditional=conditional)

        assert_closed(schema)


def test_retention_cli_emits_a_versioned_policy_contract_from_clean_cwd(
    tmp_path: Path,
) -> None:
    """Catches the retention response being the only unversioned JSON CLI document."""
    completed = _run(
        tmp_path,
        "config",
        "retention",
        "draft",
        "--config",
        str(CONFIG),
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "days": 30,
        "kind": "retention-policy",
        "mode": "dry-run",
        "retention_class": "draft",
        "schema_version": 2,
    }


def test_actual_generated_json_documents_validate_with_standalone_cli_schemas(
    tmp_path: Path,
) -> None:
    """Catches a schema that is syntactically valid but rejects the real CLI document."""
    validator = shutil.which("jsonschema")
    if validator is None:
        pytest.skip("jsonschema CLI is unavailable")

    documents: list[dict[str, Any]] = []

    def capture(*args: str) -> dict[str, Any]:
        completed = _run(tmp_path, *args)
        assert completed.returncode == 0, completed.stderr
        document = json.loads(completed.stdout)
        documents.append(document)
        return document

    capture("config", "validate", "--config", str(CONFIG))
    capture("config", "retention", "pr", "--config", str(CONFIG))

    nightly_path, nightly = _plan_file(tmp_path, "nightly")
    documents.append(nightly)
    capture(
        "lifecycle",
        "validate",
        "--plan",
        str(nightly_path),
        "--config",
        str(CONFIG),
    )
    capture(
        "command",
        "parse",
        "--body",
        "/release status",
        "--actor",
        "reviewer",
        "--author-association",
        "MEMBER",
    )
    capture(
        "wheel",
        "plan",
        "--lifecycle-plan",
        str(nightly_path),
        "--config",
        str(CONFIG),
    )
    capture(
        "wheel",
        "check-environment",
        "--installed-json",
        str(FIXTURES / "installed-single.json"),
        "--config",
        str(CONFIG),
    )

    artifact_plan, artifact_manifest, artifact_base, manifest = _manifest(
        tmp_path, "pr"
    )
    documents.append(manifest)
    capture(
        "artifacts",
        "validate",
        "--lifecycle-plan",
        str(artifact_plan),
        "--manifest",
        str(artifact_manifest),
        "--base-dir",
        str(artifact_base),
        "--config",
        str(CONFIG),
    )

    draft_plan, draft_manifest, _, _ = _manifest(tmp_path, "draft")
    request_path = tmp_path / "environment-test-request.json"
    request_completed = _run(
        tmp_path,
        "environment",
        "export",
        "--lifecycle-plan",
        str(draft_plan),
        "--manifest",
        str(draft_manifest),
        "--environment",
        "blue",
        "--nonce",
        "d" * 32,
        "--output",
        str(request_path),
        "--config",
        str(CONFIG),
    )
    assert request_completed.returncode == 0, request_completed.stderr
    request = json.loads(request_path.read_text(encoding="utf-8"))
    documents.append(request)
    result_path = tmp_path / "environment-test-result.json"
    result_completed = _run(
        tmp_path,
        "environment",
        "simulate",
        "--request",
        str(request_path),
        "--verdict",
        "passed",
        "--output",
        str(result_path),
        "--config",
        str(CONFIG),
    )
    assert result_completed.returncode == 0, result_completed.stderr
    documents.append(json.loads(result_path.read_text(encoding="utf-8")))
    capture(
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--config",
        str(CONFIG),
    )

    stable_plan, stable_manifest, _, _ = _manifest(tmp_path, "stable")
    reconcile_path = tmp_path / "reconcile-plan.json"
    reconciled = _run(
        tmp_path,
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(stable_plan),
        "--manifest",
        str(stable_manifest),
        "--inventory",
        str(FIXTURES / "release-inventory-empty.json"),
        "--output",
        str(reconcile_path),
        "--config",
        str(CONFIG),
    )
    assert reconciled.returncode == 0, reconciled.stderr
    documents.append(json.loads(reconcile_path.read_text(encoding="utf-8")))
    capture(
        "cleanup",
        "plan",
        "--inventory",
        str(FIXTURES / "cleanup-inventory.json"),
        "--as-of",
        "2026-08-12T00:00:00Z",
        "--config",
        str(CONFIG),
    )
    capture(
        "repo-policy",
        "audit",
        "--snapshot",
        str(FIXTURES / "repository-policy-compliant.json"),
        "--repository-role",
        "validation",
        "--config",
        str(CONFIG),
    )

    intent = {"source_sha": SHA, "stage": "stable", "version": "0.6.0"}
    intent_path = tmp_path / "release-intent.json"
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    validated_kinds = {document["kind"] for document in documents}
    assert validated_kinds == set(SCHEMAS_BY_KIND)

    for index, document in enumerate(documents):
        instance = tmp_path / f"instance-{index}-{document['kind']}.json"
        instance.write_text(json.dumps(document), encoding="utf-8")
        completed = subprocess.run(
            [
                validator,
                str(V2_ROOT / "schemas" / SCHEMAS_BY_KIND[document["kind"]]),
                "-i",
                str(instance),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, (
            document["kind"],
            completed.stdout,
            completed.stderr,
        )
    intent_validation = subprocess.run(
        [
            validator,
            str(V2_ROOT / "schemas/release-intent.schema.json"),
            "-i",
            str(intent_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert intent_validation.returncode == 0, intent_validation.stderr


def test_schema_mutations_reject_control_paths_and_kind_mismatched_identities(
    tmp_path: Path,
) -> None:
    """Catches schema-only regressions that runtime validation would otherwise mask."""
    validator = shutil.which("jsonschema")
    if validator is None:
        pytest.skip("jsonschema CLI is unavailable")

    plan_path, manifest_path, _, manifest = _manifest(tmp_path, "stable")
    file_artifact = next(item for item in manifest["artifacts"] if "path" in item)
    file_artifact["path"] = "files/escape\n```json"
    invalid_manifest = tmp_path / "invalid-control-path.json"
    invalid_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    path_check = subprocess.run(
        [
            validator,
            str(V2_ROOT / "schemas/artifact-manifest.schema.json"),
            "-i",
            str(invalid_manifest),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert path_check.returncode != 0

    reconcile_path = tmp_path / "valid-reconcile-plan.json"
    reconciled = _run(
        tmp_path,
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(FIXTURES / "release-inventory-empty.json"),
        "--output",
        str(reconcile_path),
        "--config",
        str(CONFIG),
    )
    assert reconciled.returncode == 0, reconciled.stderr
    plan = json.loads(reconcile_path.read_text(encoding="utf-8"))
    for operation in plan["operations"]:
        operation["identity"] = (
            "f" * 64 if operation["target"]["kind"] == "image" else "sha256:" + "f" * 64
        )
    invalid_reconcile = tmp_path / "invalid-reconcile-identities.json"
    invalid_reconcile.write_text(json.dumps(plan), encoding="utf-8")
    identity_check = subprocess.run(
        [
            validator,
            str(V2_ROOT / "schemas/reconcile-plan.schema.json"),
            "-i",
            str(invalid_reconcile),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert identity_check.returncode != 0
