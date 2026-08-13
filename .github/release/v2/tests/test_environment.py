from __future__ import annotations

import hashlib
import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SHA = "a" * 40
NONCE = "0123456789abcdef0123456789abcdef"
CHECKS = [
    "abi",
    "accelerator",
    "chart-render",
    "cluster",
    "image-pull",
    "import",
    "install",
    "runtime",
    "smoke",
]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=V2_ROOT,
        env=os.environ | {"PYTHONPATH": str(V2_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def _resign(value: dict[str, object]) -> dict[str, object]:
    unsigned = deepcopy(value)
    unsigned.pop("sha256", None)
    unsigned["sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return unsigned


def _draft_plan(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    plan_path = tmp_path / "lifecycle-plan.json"
    intent = {"source_sha": SHA, "stage": "draft", "version": "0.6.0rc1"}
    result = _run(
        "lifecycle",
        "plan",
        "--stage",
        "draft",
        "--trigger",
        "workflow_dispatch",
        "--ref",
        "refs/heads/main",
        "--source-sha",
        SHA,
        "--repository-role",
        "production",
        "--intent-json",
        json.dumps(intent),
        "--output",
        str(plan_path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    return plan_path, json.loads(plan_path.read_text(encoding="utf-8"))


def _records(base: Path, plan: dict[str, object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for index, product in enumerate(plan["products"]):  # type: ignore[index]
        assert isinstance(product, dict)
        if product["kind"] == "image":
            records.append(
                {
                    **product,
                    "digest": "sha256:" + f"{index + 1:064x}",
                    "platforms": [
                        {
                            "platform": "linux/amd64",
                            "digest": "sha256:" + f"{index + 11:064x}",
                        },
                        {
                            "platform": "linux/arm64",
                            "digest": "sha256:" + f"{index + 21:064x}",
                        },
                    ],
                }
            )
        else:
            relative = f"artifacts/{product['name']}.fixture"
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"{product['kind']}:{product['coordinate']}".encode())
            records.append({**product, "path": relative})
    return records


def _draft_manifest(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    plan_path, plan = _draft_plan(tmp_path)
    base = tmp_path / "base"
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(_records(base, plan)), encoding="utf-8")
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
    return (
        plan_path,
        manifest_path,
        json.loads(manifest_path.read_text(encoding="utf-8")),
    )


def _export(
    tmp_path: Path, environment: str = "blue"
) -> tuple[Path, Path, Path, dict[str, object]]:
    plan_path, manifest_path, _ = _draft_manifest(tmp_path)
    request_path = tmp_path / "environment-test-request.json"
    result = _run(
        "environment",
        "export",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--environment",
        environment,
        "--nonce",
        NONCE,
        "--output",
        str(request_path),
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    return (
        plan_path,
        manifest_path,
        request_path,
        json.loads(request_path.read_text(encoding="utf-8")),
    )


def _simulate(
    tmp_path: Path, *, verdict: str = "passed", fail_check: str | None = None
) -> tuple[Path, Path, dict[str, object]]:
    _, _, request_path, _ = _export(tmp_path)
    result_path = tmp_path / "environment-test-result.json"
    args = [
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
    if fail_check is not None:
        args.extend(["--fail-check", fail_check])
    result = _run(*args)
    assert result.returncode == 0, result.stderr
    return (
        request_path,
        result_path,
        json.loads(result_path.read_text(encoding="utf-8")),
    )


def test_export_binds_a_draft_product_closure_to_a_simulated_environment(
    tmp_path: Path,
) -> None:
    """Catches requests omitting a product or presenting simulation as observed evidence."""
    plan_path, manifest_path, request_path, request = _export(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert request["kind"] == "environment-test-request"
    assert request["environment"] == "blue"
    assert request["evidence_level"] == "simulated"
    assert request["nonce"] == NONCE
    assert request["source_sha"] == SHA
    assert request["stage"] == "draft"
    assert request["version"] == plan["version"]
    assert request["lifecycle_plan_sha256"] == plan["sha256"]
    assert request["artifact_manifest_sha256"] == manifest["sha256"]
    assert request["artifacts"] == manifest["artifacts"]
    assert [item["name"] for item in request["required_checks"]] == CHECKS
    assert all(
        item["evidence_level"] == "simulated" for item in request["required_checks"]
    )
    assert all(item["verified"] is False for item in request["required_checks"])
    assert all(item["executed"] is False for item in request["operations"])
    assert request_path.read_text(encoding="utf-8").endswith("\n")


def test_environment_rejects_resigned_control_character_artifact_paths(
    tmp_path: Path,
) -> None:
    """A self-digested envelope cannot make an unsafe POSIX path trustworthy."""
    request_path, result_path, result = _simulate(tmp_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    file_index = next(
        index
        for index, item in enumerate(request["artifacts"])
        if item["kind"] != "image"
    )
    request["artifacts"][file_index]["path"] = "artifacts/control\nname.whl"
    request = _resign(request)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    result["artifacts"][file_index]["path"] = "artifacts/control\nname.whl"
    result["request_sha256"] = request["sha256"]
    result_path.write_text(json.dumps(_resign(result)), encoding="utf-8")

    rejected = _run(
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--config",
        "release.yaml",
    )
    assert rejected.returncode == 2
    assert "canonical safe POSIX path" in rejected.stderr
    assert "Traceback" not in rejected.stderr


@pytest.mark.parametrize("environment", ["green", "Blue", "", "production"])
def test_export_rejects_unsupported_environment_names(
    tmp_path: Path, environment: str
) -> None:
    """Catches an environment name escaping the configured blue/yellow dry-run set."""
    plan_path, manifest_path, _ = _draft_manifest(tmp_path)
    result = _run(
        "environment",
        "export",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--environment",
        environment,
        "--nonce",
        NONCE,
        "--config",
        "release.yaml",
    )
    assert result.returncode == 2


@pytest.mark.parametrize(
    "mutation", ["plan-stage", "plan-role", "manifest-source", "missing-product"]
)
def test_export_rejects_invalid_plan_or_manifest_identity(
    tmp_path: Path, mutation: str
) -> None:
    """Catches resigned plan/manifest drift and incomplete artifact product closure."""
    plan_path, manifest_path, manifest = _draft_manifest(tmp_path)
    if mutation.startswith("plan-"):
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if mutation == "plan-stage":
            plan["stage"] = plan["channel"] = "rc"
        else:
            plan["repository_role"] = "validation"
        plan_path.write_text(json.dumps(_resign(plan)), encoding="utf-8")
    else:
        if mutation == "manifest-source":
            manifest["source_sha"] = "b" * 40
        else:
            manifest["artifacts"].pop()  # type: ignore[union-attr]
        manifest_path.write_text(json.dumps(_resign(manifest)), encoding="utf-8")

    result = _run(
        "environment",
        "export",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--environment",
        "yellow",
        "--nonce",
        NONCE,
        "--config",
        "release.yaml",
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "artifact-kind-list",
        "artifacts-object",
        "artifact-entry-number",
        "source-number",
        "plan-digest-object",
    ],
)
def test_export_rejects_malformed_manifest_types_without_traceback(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Catches untrusted manifest scalar/container types escaping the CLI error boundary."""
    plan_path, manifest_path, manifest = _draft_manifest(tmp_path)
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    if mutation == "artifact-kind-list":
        assert isinstance(artifacts[0], dict)
        artifacts[0]["kind"] = ["chart"]
    elif mutation == "artifacts-object":
        manifest["artifacts"] = {"items": artifacts}
    elif mutation == "artifact-entry-number":
        artifacts[0] = 7
    elif mutation == "source-number":
        manifest["source_sha"] = 7
    else:
        manifest["lifecycle_plan_sha256"] = {"digest": "f" * 64}
    manifest_path.write_text(json.dumps(_resign(manifest)), encoding="utf-8")

    result = _run(
        "environment",
        "export",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--environment",
        "blue",
        "--nonce",
        NONCE,
        "--config",
        "release.yaml",
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "environment-list",
        "environment-object",
        "environment-bool",
        "artifact-kind-list",
        "artifacts-object",
        "artifact-entry-number",
        "source-number",
        "plan-digest-list",
        "checks-object",
        "evidence-object",
    ],
)
def test_simulate_rejects_malformed_request_types_without_traceback(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Catches malformed signed requests causing TypeError before EnvironmentError."""
    _, _, request_path, request = _export(tmp_path)
    if mutation.startswith("environment-"):
        request["environment"] = {
            "environment-list": ["blue"],
            "environment-object": {"name": "blue"},
            "environment-bool": True,
        }[mutation]
    elif mutation == "artifact-kind-list":
        artifacts = request["artifacts"]
        assert isinstance(artifacts, list) and isinstance(artifacts[0], dict)
        artifacts[0]["kind"] = ["chart"]
    elif mutation == "artifacts-object":
        request["artifacts"] = {"items": request["artifacts"]}
    elif mutation == "artifact-entry-number":
        artifacts = request["artifacts"]
        assert isinstance(artifacts, list)
        artifacts[0] = 7
    elif mutation == "source-number":
        request["source_sha"] = 7
    elif mutation == "plan-digest-list":
        request["lifecycle_plan_sha256"] = ["f" * 64]
    elif mutation == "checks-object":
        request["required_checks"] = {"checks": request["required_checks"]}
    else:
        request["evidence_level"] = {"level": "simulated"}
    request_path.write_text(json.dumps(_resign(request)), encoding="utf-8")

    result = _run(
        "environment",
        "simulate",
        "--request",
        str(request_path),
        "--verdict",
        "passed",
        "--config",
        "release.yaml",
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_simulate_and_verify_accept_passed_simulation_but_block_production(
    tmp_path: Path,
) -> None:
    """Catches a simulated pass being promoted into production evidence or a production pass."""
    request_path, result_path, simulated = _simulate(tmp_path)
    verified = _run(
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--config",
        "release.yaml",
    )
    document = json.loads(verified.stdout)

    assert simulated["verdict"] == "passed"
    assert [item["name"] for item in simulated["checks"]] == CHECKS
    assert all(item["status"] == "passed" for item in simulated["checks"])
    assert verified.returncode == 0, verified.stderr
    assert document == {
        "gates": {
            "production": {"status": "blocked"},
            "simulated_environment": {"status": "passed"},
        },
        "kind": "environment-verification",
        "mode": "dry-run",
        "production_gate": "blocked",
        "reason": "simulated-evidence-cannot-satisfy-production-gate",
        "schema_version": 2,
        "simulated_verdict": "passed",
        "status": "accepted",
    }


def test_simulate_and_verify_preserve_a_failed_simulated_verdict(
    tmp_path: Path,
) -> None:
    """Catches failed checks being hidden behind a passed or rejected verification summary."""
    request_path, result_path, simulated = _simulate(
        tmp_path, verdict="failed", fail_check="smoke"
    )
    verified = _run(
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--config",
        "release.yaml",
    )
    document = json.loads(verified.stdout)

    assert simulated["verdict"] == "failed"
    assert (
        next(item for item in simulated["checks"] if item["name"] == "smoke")["status"]
        == "failed"
    )
    assert verified.returncode == 0, verified.stderr
    assert document["status"] == "accepted"
    assert document["simulated_verdict"] == "failed"
    assert document["gates"]["simulated_environment"]["status"] == "failed"
    assert document["production_gate"] == "blocked"


@pytest.mark.parametrize(
    "mutation",
    [
        "request-digest",
        "nonce",
        "environment",
        "source",
        "plan",
        "manifest",
        "artifacts",
    ],
)
def test_verify_rejects_replayed_or_identity_drifted_results(
    tmp_path: Path, mutation: str
) -> None:
    """Catches replay of a signed result against any different request identity."""
    request_path, result_path, result = _simulate(tmp_path)
    field_values: dict[str, object] = {
        "request-digest": "f" * 64,
        "nonce": "f" * 32,
        "environment": "yellow",
        "source": "b" * 40,
        "plan": "e" * 64,
        "manifest": "d" * 64,
    }
    field_names = {
        "request-digest": "request_sha256",
        "nonce": "nonce",
        "environment": "environment",
        "source": "source_sha",
        "plan": "lifecycle_plan_sha256",
        "manifest": "artifact_manifest_sha256",
    }
    if mutation == "artifacts":
        result["artifacts"] = list(reversed(result["artifacts"]))  # type: ignore[arg-type]
    else:
        result[field_names[mutation]] = field_values[mutation]
    result_path.write_text(json.dumps(_resign(result)), encoding="utf-8")

    verified = _run(
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
    )
    assert verified.returncode == 2
    assert verified.stdout == ""
    assert "Traceback" not in verified.stderr


@pytest.mark.parametrize(
    "mutation", ["duplicate", "missing", "extra", "inconsistent-verdict"]
)
def test_verify_rejects_non_exact_checks_and_inconsistent_verdicts(
    tmp_path: Path, mutation: str
) -> None:
    """Catches duplicate/missing/extra checks and verdicts that contradict check status."""
    request_path, result_path, result = _simulate(tmp_path)
    checks = result["checks"]
    assert isinstance(checks, list)
    if mutation == "duplicate":
        checks[-1] = deepcopy(checks[0])
    elif mutation == "missing":
        checks.pop()
    elif mutation == "extra":
        checks.append({"name": "network", "status": "passed"})
    else:
        result["verdict"] = "failed"
    result_path.write_text(json.dumps(_resign(result)), encoding="utf-8")

    verified = _run(
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
    )
    assert verified.returncode == 2
    assert "checks" in verified.stderr or "verdict" in verified.stderr


@pytest.mark.parametrize(
    "field", ["production_evidence", "external_evidence", "hardware_evidence"]
)
def test_verify_rejects_forged_non_simulated_evidence(
    tmp_path: Path, field: str
) -> None:
    """Catches production, external, or hardware evidence being smuggled into a simulation result."""
    request_path, result_path, result = _simulate(tmp_path)
    result[field] = {"status": "passed"}
    result_path.write_text(json.dumps(_resign(result)), encoding="utf-8")

    verified = _run(
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
    )
    assert verified.returncode == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "check-name-list",
        "check-status-list",
        "check-status-object",
        "check-status-number",
        "verdict-list",
        "verdict-bool",
        "checks-object",
        "check-entry-number",
        "artifacts-object",
        "source-number",
        "mode-list",
        "evidence-object",
    ],
)
def test_verify_rejects_malformed_result_types_without_traceback(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Catches malformed result containers and scalars escaping as CLI tracebacks."""
    request_path, result_path, result = _simulate(tmp_path)
    checks = result["checks"]
    assert isinstance(checks, list) and isinstance(checks[0], dict)
    if mutation == "check-name-list":
        checks[0]["name"] = ["abi"]
    elif mutation.startswith("check-status-"):
        checks[0]["status"] = {
            "check-status-list": ["passed"],
            "check-status-object": {"status": "passed"},
            "check-status-number": 1,
        }[mutation]
    elif mutation == "verdict-list":
        result["verdict"] = ["passed"]
    elif mutation == "verdict-bool":
        result["verdict"] = True
    elif mutation == "checks-object":
        result["checks"] = {"checks": checks}
    elif mutation == "check-entry-number":
        checks[0] = 7
    elif mutation == "artifacts-object":
        result["artifacts"] = {"items": result["artifacts"]}
    elif mutation == "source-number":
        result["source_sha"] = 7
    elif mutation == "mode-list":
        result["mode"] = ["dry-run"]
    else:
        result["evidence_level"] = {"level": "simulated"}
    result_path.write_text(json.dumps(_resign(result)), encoding="utf-8")

    verified = _run(
        "environment",
        "verify",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--config",
        "release.yaml",
    )
    assert verified.returncode == 2
    assert "Traceback" not in verified.stderr


def test_environment_schemas_are_strict_and_count_exact_contracts() -> None:
    """Catches schemas allowing unknown evidence or under-counted products and checks."""
    request = json.loads(
        (V2_ROOT / "schemas/environment-test-request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    result = json.loads(
        (V2_ROOT / "schemas/environment-test-result.schema.json").read_text(
            encoding="utf-8"
        )
    )

    for schema in (request, result):
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
        assert schema["properties"]["artifacts"]["minItems"] == 7
        assert schema["properties"]["artifacts"]["maxItems"] == 7
    assert request["properties"]["required_checks"]["minItems"] == len(CHECKS)
    assert request["properties"]["required_checks"]["maxItems"] == len(CHECKS)
    assert result["properties"]["checks"]["minItems"] == len(CHECKS)
    assert result["properties"]["checks"]["maxItems"] == len(CHECKS)
    assert result["allOf"]


def test_draft_environment_workflow_is_manual_trusted_and_read_only() -> None:
    """Catches the fixture workflow contacting a cluster or executing untrusted control code."""
    import yaml

    path = REPOSITORY_ROOT / ".github/workflows/draft-environment-dry-run.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    events = workflow.get("on", workflow.get(True))
    dispatch = events["workflow_dispatch"]["inputs"]
    assert set(dispatch) == {"environment", "intent_json", "nonce"}
    assert dispatch["environment"]["options"] == ["blue", "yellow"]
    assert dispatch["intent_json"]["required"] is True
    assert dispatch["nonce"]["required"] is True
    assert workflow["permissions"] == {"contents": "read"}
    wrapper = workflow["jobs"]["invoke-trusted-main-controller"]
    assert set(wrapper) == {"permissions", "uses", "with"}
    assert wrapper["uses"].endswith("release-control-dry-run.yml@main")
    controller_path = REPOSITORY_ROOT / ".github/workflows/release-control-dry-run.yml"
    controller = yaml.safe_load(controller_path.read_text(encoding="utf-8"))
    source = "\n".join(
        str(step.get("run", ""))
        for job in controller["jobs"].values()
        for step in job.get("steps", [])
    )
    checkout = next(
        step
        for job in controller["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"] == {
        "ref": "${{ needs.control.outputs.control_sha }}",
        "path": "control",
        "persist-credentials": False,
    }
    assert source.count("curl --request GET") == 2
    assert source.count("--max-redirs 0") == 2
    assert (
        source.count(
            '"https://api.github.com/repos/${REPOSITORY}/git/ref/heads/${CONFIGURED_MAIN}"'
        )
        == 2
    )
    assert 'job_context["workflow_sha"]' in source
    assert "environment export" in source
    assert "environment simulate" in source
    assert "environment verify" in source
    assert "artifacts collect" in source
    assert '--source-sha "$SOURCE_SHA"' in source
    assert 're.fullmatch(r"[0-9a-f]{40}", source_sha)' in source
    assert 're.fullmatch(r"[0-9a-f]{32}", nonce)' in source
    assert "GITHUB_STEP_SUMMARY" in source
    upload = next(
        step
        for job in controller["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert "preview/summary.md" in upload["with"]["path"]
    assert "kubectl" not in source.lower()
    assert "docker login" not in source.lower()
