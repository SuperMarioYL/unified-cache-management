"""RED workflow and staging-safety contract for the slim release lane."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
EXPECTED_RELEASE_WORKFLOWS = {
    "_build-image.yml",
    "_build-wheel.yml",
    "release-ucm.yml",
    "release-vllm-images.yml",
}
ALLOWED_NON_RELEASE_WORKFLOWS = {
    "lint-and-test.yml",
    "pull-request.yml",
    "push-check.yml",
}
WORKFLOW_SUFFIXES = {".yml", ".yaml"}
SAFE_FORK_ACTIONS = {
    "actions/cache",
    "actions/checkout",
    "actions/download-artifact",
    "actions/setup-python",
    "actions/upload-artifact",
    "azure/setup-helm",
    "docker/setup-buildx-action",
    "docker/setup-qemu-action",
    "sigstore/cosign-installer",
}
CHANGED_WORKFLOWS = EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS
FULL_ACTION_SHA = re.compile(r"^[^@]+@[0-9a-f]{40}$")
FORBIDDEN_STAGED_PATHS = {
    "ucm/store/compress/cc/compress_lib/tunstall_bf16.cc",
    "ucm/store/compress/cc/compress_lib/tunstall_bf16.h",
    "ucm/store/compress/cc/compressor_action.cc",
}
FIXTURE_PROFILE = (
    "cuda-cu129-ubuntu2204-amd64-cp312-release-default-sm75-sm80-sm86-sm89-sm90"
)


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True, encoding="utf-8"
    )


def _release_modules() -> tuple[object, object, object]:
    release_root = REPO_ROOT / ".github" / "release"
    sys.path.insert(0, str(release_root))
    return (
        importlib.import_module("ucm_release.core"),
        importlib.import_module("ucm_release.wheel"),
        importlib.import_module("ucm_release.verify"),
    )


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _strings(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _strings(nested)]
    return [str(value)]


def _workflow_set_violations(workflow_dir: Path) -> list[str]:
    actual = {path.name for path in _workflow_paths(workflow_dir)}
    expected = EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS
    if actual == expected:
        return []
    return [
        f"workflow file set must be exactly {sorted(expected)}, found {sorted(actual)}"
    ]


def _workflow_paths(workflow_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in workflow_dir.iterdir()
        if path.is_file() and path.suffix in WORKFLOW_SUFFIXES
    )


def _release_workflow_documents(workflow_dir: Path) -> dict[str, object]:
    """Audit expected release files and any unallowlisted workflow extension."""
    documents: dict[str, object] = {}
    for path in _workflow_paths(workflow_dir):
        if (
            path.name in EXPECTED_RELEASE_WORKFLOWS
            or path.name not in ALLOWED_NON_RELEASE_WORKFLOWS
        ):
            documents[path.name] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return documents


def _load_workflow(path: Path) -> dict[str, object]:
    """Load Actions YAML without letting YAML 1.1 turn ``on`` into ``True``."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path.name} must contain a YAML object")
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def _trigger(document: dict[str, object]) -> dict[str, object]:
    value = document.get("on")
    assert isinstance(value, dict)
    return value


def _jobs(document: dict[str, object]) -> dict[str, dict[str, object]]:
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    assert all(isinstance(job, dict) for job in jobs.values())
    return jobs  # type: ignore[return-value]


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps  # type: ignore[return-value]


def _uses_in(document: dict[str, object]) -> list[str]:
    uses: list[str] = []
    for job in _jobs(document).values():
        if isinstance(job.get("uses"), str):
            uses.append(str(job["uses"]))
        for step in _steps(job):
            if isinstance(step.get("uses"), str):
                uses.append(str(step["uses"]))
    return uses


def _artifact_uploads(document: dict[str, object]) -> list[dict[str, object]]:
    return [
        step
        for job in _jobs(document).values()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]


def _has_upstream_guard(job: dict[object, object]) -> bool:
    condition = str(job.get("if", ""))
    return bool(
        re.search(
            r"github\.repository\s*==\s*['\"]ModelEngine-Group/unified-cache-management['\"]",
            condition,
        )
    )


def _truthy(value: object) -> bool:
    return value is True or str(value).lower() == "true"


def _effective_permissions(
    workflow_permissions: object, job: dict[object, object]
) -> tuple[object, bool]:
    """GitHub job permissions replace workflow permissions when explicitly set."""
    if "permissions" in job:
        return job["permissions"], False
    return workflow_permissions, True


def _permissions_grant_write(permissions: object) -> bool:
    if isinstance(permissions, dict):
        return any(str(value).lower() == "write" for value in permissions.values())
    if isinstance(permissions, str):
        normalized = permissions.lower().replace(" ", "")
        return normalized == "write-all" or bool(
            re.search(r"(?:^|,)\w+:write(?:,|$)", normalized)
        )
    return False


def _action_operation(uses: object, inputs: object) -> str | None:
    if not isinstance(uses, str) or not uses:
        return None
    action = uses.split("@", 1)[0].lower()
    if action in SAFE_FORK_ACTIONS:
        return None
    if action == "docker/build-push-action":
        if isinstance(inputs, dict) and _truthy(inputs.get("push")):
            return "container publishing action"
        return None
    if action == "docker/login-action":
        return "registry credential action"
    if action.startswith("./.github/workflows/"):
        workflow_name = Path(action).name
        if workflow_name in EXPECTED_RELEASE_WORKFLOWS:
            return None
    return f"unapproved action {action}"


def _dangerous_job_operations(
    workflow_permissions: object, job: dict[object, object]
) -> list[str]:
    """Return publication-capable operations that must be upstream-gated."""
    operations: list[str] = []
    if job.get("secrets") == "inherit":
        operations.append("secrets: inherit")
    permissions, inherited = _effective_permissions(workflow_permissions, job)
    if _permissions_grant_write(permissions):
        label = (
            "workflow-inherited write permission" if inherited else "write permission"
        )
        operations.append(label)
    if "environment" in job:
        operations.append("protected environment")

    job_text = "\n".join(_strings(job)).lower()
    if "self-hosted" in job_text:
        operations.append("self-hosted runner")
    command_patterns = {
        r"\b(?:docker|crane)\s+(?:login|push|copy)\b": "registry login or publication",
        r"\bbuildx\s+build\b[^\n]*--push\b": "Buildx publication",
        r"\bgh\s+workflow\s+run\b": "workflow dispatch",
        r"\bgh\s+api\b[^\n]*(?:/dispatches\b|workflow_dispatch\b)": "GitHub dispatch API",
        r"\b(?:curl|wget)\b[^\n]*(?:/dispatches\b|workflow_dispatch\b)": "HTTP dispatch",
    }
    for pattern, label in command_patterns.items():
        if re.search(pattern, job_text):
            operations.append(label)

    job_action = _action_operation(job.get("uses"), job.get("with"))
    if job_action:
        operations.append(job_action)
    for step in job.get("steps", []) if isinstance(job.get("steps"), list) else []:
        if not isinstance(step, dict):
            continue
        action_operation = _action_operation(step.get("uses"), step.get("with"))
        if action_operation:
            operations.append(action_operation)
    return sorted(set(operations))


def _fork_isolation_violations(documents: dict[str, object]) -> list[str]:
    """Audit entry and locally reusable release workflows for a fork path escape."""
    violations: list[str] = []
    for filename, document in documents.items():
        if not isinstance(document, dict):
            continue
        jobs = document.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        workflow_permissions = document.get("permissions")
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            operations = _dangerous_job_operations(workflow_permissions, job)
            if operations and not _has_upstream_guard(job):
                violations.append(
                    f"{filename}:{job_name} exposes fork candidates to "
                    f"{', '.join(operations)} without an upstream repository guard"
                )
    return violations


def test_release_workflows_are_compact_and_fork_candidate_is_read_only() -> None:
    """Demand a closed workflow set and no fork-to-publish escape path."""
    violations = _workflow_set_violations(WORKFLOW_DIR)

    entrypoint = WORKFLOW_DIR / "release-ucm.yml"
    document = (
        yaml.safe_load(entrypoint.read_text(encoding="utf-8"))
        if entrypoint.exists()
        else {}
    )
    jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
    candidate = jobs.get("fork-candidate") if isinstance(jobs, dict) else None
    if not isinstance(candidate, dict):
        violations.append("release-ucm.yml must define a fork-candidate job")
    else:
        if candidate.get("permissions") != {"contents": "read"}:
            violations.append(
                "fork-candidate permissions must be exactly {'contents': 'read'}"
            )
        candidate_text = "\n".join(_strings(candidate)).lower()
        if "environment" in candidate:
            violations.append("fork-candidate must not use protected environments")
        banned_fragments = {
            "secrets.": "secrets",
            "self-hosted": "self-hosted runners",
        }
        for fragment, label in banned_fragments.items():
            if fragment in candidate_text:
                violations.append(f"fork-candidate must not use {label}")
        if re.search(r"\b(?:docker|crane)\s+(?:login|push)\b", candidate_text):
            violations.append(
                "fork-candidate must not log in to or push a container registry"
            )
        if re.search(r"\bgh\s+api\b.*\bdispatch", candidate_text):
            violations.append("fork-candidate must not dispatch workflows")

    documents = _release_workflow_documents(WORKFLOW_DIR)
    violations.extend(_fork_isolation_violations(documents))

    assert not violations, "release workflow safety contract failed:\n- " + "\n- ".join(
        violations
    )


def test_existing_cpp_changes_are_explicitly_forbidden_from_the_stage() -> None:
    """Keep the three pre-existing C++ edits visible but outside this release commit."""
    assert all((REPO_ROOT / path).is_file() for path in FORBIDDEN_STAGED_PATHS)
    staged = set(filter(None, _git("diff", "--cached", "--name-only").splitlines()))
    assert not staged & FORBIDDEN_STAGED_PATHS, json.dumps(
        {"forbidden_staged_paths": sorted(staged & FORBIDDEN_STAGED_PATHS)}, indent=2
    )


def test_workflow_set_rejects_an_arbitrary_publish_workflow(tmp_path: Path) -> None:
    """An unrecognised YAML workflow cannot evade the four-workflow budget."""
    for filename in EXPECTED_RELEASE_WORKFLOWS | ALLOWED_NON_RELEASE_WORKFLOWS:
        (tmp_path / filename).write_text("name: allowed\n")
    (tmp_path / "publish.yaml").write_text("name: bypass\n")

    violations = _workflow_set_violations(tmp_path)

    assert len(violations) == 1
    assert "publish.yaml" in violations[0]


def test_fork_isolation_rejects_reusable_workflow_publish_mutations() -> None:
    """Reusable workflow mutations must be rejected even when entry job is clean."""
    documents = {
        "release-ucm.yml": {
            "jobs": {
                "fork-candidate": {
                    "permissions": {"contents": "read"},
                    "runs-on": "ubuntu-24.04",
                    "steps": [{"run": "python -m ucm_release core plan"}],
                }
            }
        },
        "_build-image.yml": {
            "jobs": {
                "mutated-reusable": {
                    "secrets": "inherit",
                    "runs-on": "self-hosted",
                    "steps": [
                        {"uses": "docker/login-action@v3"},
                        {
                            "uses": "docker/build-push-action@v6",
                            "with": {"push": True},
                        },
                        {"uses": "softprops/action-gh-release@v2"},
                        {
                            "run": (
                                "docker buildx build --push .\n"
                                "crane copy source target\n"
                                "gh workflow run child.yml\n"
                                "gh api --method POST repos/x/dispatches\n"
                                "curl -X POST https://api.github.com/repos/x/dispatches"
                            )
                        },
                    ],
                }
            }
        },
    }

    violations = _fork_isolation_violations(documents)

    assert len(violations) == 1
    violation = violations[0]
    for operation in (
        "secrets: inherit",
        "self-hosted runner",
        "registry credential action",
        "container publishing action",
        "unapproved action softprops/action-gh-release",
        "Buildx publication",
        "registry login or publication",
        "workflow dispatch",
        "GitHub dispatch API",
        "HTTP dispatch",
    ):
        assert operation in violation


def test_fork_isolation_allows_a_read_only_reusable_build() -> None:
    """A normal hosted build and artifact upload remain valid fork operations."""
    documents = {
        "_build-wheel.yml": {
            "jobs": {
                "build": {
                    "permissions": {"contents": "read"},
                    "runs-on": "ubuntu-24.04",
                    "steps": [
                        {"uses": "actions/checkout@full-sha"},
                        {"run": "docker buildx build --output type=oci,dest=out.tar ."},
                        {"uses": "actions/upload-artifact@full-sha"},
                    ],
                }
            }
        }
    }

    assert _fork_isolation_violations(documents) == []


def test_release_workflow_topology_runs_the_four_files_at_the_pushed_sha() -> None:
    """The feature push must reach wheel, Chart, image, and evidence jobs locally."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    triggers = _trigger(entry)
    push = triggers.get("push")
    assert isinstance(push, dict)
    assert push.get("branches") == ["feature/**"]
    assert push.get("tags") == ["v*"]
    assert "workflow_call" in triggers

    entry_jobs = _jobs(entry)
    assert entry_jobs["fork-candidate"]["permissions"] == {"contents": "read"}
    local_calls = {
        str(job["uses"])
        for job in entry_jobs.values()
        if isinstance(job.get("uses"), str)
    }
    assert local_calls == {
        "./.github/workflows/_build-wheel.yml",
        "./.github/workflows/release-vllm-images.yml",
    }
    assert all("@" not in reference for reference in local_calls)
    assert any("chart package" in value for value in _strings(entry))

    image_release = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    image_triggers = _trigger(image_release)
    assert set(image_triggers) == {
        "workflow_call",
        "schedule",
        "repository_dispatch",
        "workflow_dispatch",
    }
    image_calls = {
        str(job["uses"])
        for job in _jobs(image_release).values()
        if isinstance(job.get("uses"), str)
    }
    assert image_calls == {
        "./.github/workflows/_build-wheel.yml",
        "./.github/workflows/_build-image.yml",
    }
    image_jobs = _jobs(image_release)
    assert image_jobs["standalone-wheel"]["uses"] == (
        "./.github/workflows/_build-wheel.yml"
    )
    assert image_jobs["build-image"]["uses"] == ("./.github/workflows/_build-image.yml")
    assert set(image_jobs["final-reconcile"]["needs"]) == {
        "select-input",
        "reconcile-fixture",
        "build-image",
    }


def test_reusable_workflow_inputs_outputs_and_artifacts_are_exact() -> None:
    """Reusable boundaries must carry immutable identities, not implicit state."""
    wheel = _load_workflow(WORKFLOW_DIR / "_build-wheel.yml")
    wheel_call = _trigger(wheel)["workflow_call"]
    assert isinstance(wheel_call, dict)
    assert set(wheel_call.get("inputs", {})) == {
        "source_sha",
        "profile_id",
        "validation_lane",
    }
    assert {
        "wheel_artifact",
        "wheel_sha256",
        "inspection_sha256",
    } <= set(wheel_call.get("outputs", {}))
    wheel_text = "\n".join(_strings(wheel))
    assert "Requires-Dist wrapt==1.17.2" in wheel_text
    assert "wheel fixture-build" in wheel_text
    assert "fixture-only" in wheel_text

    image = _load_workflow(WORKFLOW_DIR / "_build-image.yml")
    image_call = _trigger(image)["workflow_call"]
    assert isinstance(image_call, dict)
    assert set(image_call.get("inputs", {})) == {
        "source_sha",
        "wheel_artifact",
        "image_input_artifact",
        "validation_lane",
    }
    assert {"image_artifact", "image_result_sha256", "oci_digest"} <= set(
        image_call.get("outputs", {})
    )
    image_text = "\n".join(_strings(image)).lower()
    assert "type=oci" in image_text
    assert "image verify" in image_text
    assert "--push" not in image_text
    assert not re.search(r"\b(?:cmake|ninja|gcc|g\+\+|clang|pip wheel)\b", image_text)

    for filename in EXPECTED_RELEASE_WORKFLOWS:
        document = _load_workflow(WORKFLOW_DIR / filename)
        uploads = _artifact_uploads(document)
        for upload in uploads:
            inputs = upload.get("with")
            assert isinstance(inputs, dict)
            assert inputs.get("retention-days") == 3


def test_candidate_evidence_binds_the_real_two_reconcile_closure() -> None:
    """The final artifact must bind actual build output and all required scenarios."""
    entry_text = (WORKFLOW_DIR / "release-ucm.yml").read_text(encoding="utf-8")
    image_text = (WORKFLOW_DIR / "release-vllm-images.yml").read_text(encoding="utf-8")
    policy_text = (REPO_ROOT / ".github/release/ucm_release/verify.py").read_text(
        encoding="utf-8"
    )
    combined = entry_text + "\n" + image_text + "\n" + policy_text
    required_fields = {
        "release-loop-evidence.json",
        "payload_sha256",
        "workflow_refs",
        "source_sha",
        "wheel_sha256",
        "chart_sha256",
        "chart_tree_sha256",
        "upstream_index_digest",
        "oci_digest",
        "image_result_sha256",
        "first_reconcile_sha256",
        "second_reconcile_sha256",
        "publication",
        "write_audit",
    }
    assert not {field for field in required_fields if field not in combined}
    for scenario in (
        "new-input-one-task",
        "identical-input-zero-tasks",
        "tag-digest-drift-r2",
        "complete-digest-chain",
        "required-failures-block",
        "fixture-candidate-full-zero-reconcile",
    ):
        assert scenario in combined
    for accepted in ("a2", "a3"):
        assert f'"{accepted}"' in combined
    for rejected in ("310p", "a5"):
        assert rejected in combined
    assert "loop complete" in image_text
    assert "loop aggregate" in entry_text
    assert '"status": "blocked"' in policy_text
    assert '"attempted": False' in policy_text


def test_workflows_only_orchestrate_tested_cli_and_standalone_runs_full_closure() -> (
    None
):
    """Rules stay in Python while every public entry reaches the same fixture loop."""
    release_documents = {
        name: _load_workflow(WORKFLOW_DIR / name) for name in EXPECTED_RELEASE_WORKFLOWS
    }
    for name, document in release_documents.items():
        text = "\n".join(_strings(document))
        assert "python - <<" not in text, name
        assert "python3 - <<" not in text, name
    wheel_text = (WORKFLOW_DIR / "_build-wheel.yml").read_text(encoding="utf-8")
    image_text = (WORKFLOW_DIR / "_build-image.yml").read_text(encoding="utf-8")
    reconcile_text = (WORKFLOW_DIR / "release-vllm-images.yml").read_text(
        encoding="utf-8"
    )
    entry_text = (WORKFLOW_DIR / "release-ucm.yml").read_text(encoding="utf-8")
    assert "wheel fixture-build" in wheel_text
    assert "image prepare" in image_text and "image verify" in image_text
    assert "loop prepare" in reconcile_text and "loop complete" in reconcile_text
    assert "loop aggregate" in entry_text
    assert "standalone-wheel:" in reconcile_text
    assert "live-probes.json" in image_text
    assert 'identity_input":false' in image_text
    assert "CRANE_VERSION: v0.20.3" in image_text
    assert "version: v0.19.2" in image_text
    assert re.search(r"CRANE_ARCHIVE_SHA256: [0-9a-f]{64}", image_text)
    assert "crane digest docker.io/vllm/vllm-openai:v0.10.2" in image_text
    assert "crane digest quay.io/ascend/vllm-ascend:v0.9.1" in image_text


def test_clean_image_build_rewrites_timestamps_without_disabling_dependencies() -> None:
    """Fresh runners must emit the same OCI bytes while pip still resolves wrapt."""
    workflow = (WORKFLOW_DIR / "_build-image.yml").read_text(encoding="utf-8")
    installer = (REPO_ROOT / ".github/release/docker/install_ucm.py").read_text(
        encoding="utf-8"
    )
    verifier = (REPO_ROOT / ".github/release/ucm_release/image.py").read_text(
        encoding="utf-8"
    )

    assert "type=oci,dest=out/image.oci.tar,rewrite-timestamp=true" in workflow
    assert '"--no-cache-dir"' in installer
    assert '"--disable-pip-version-check"' in installer
    assert '"--only-binary=:all:"' in installer
    assert '"--no-deps"' not in installer
    assert '"--disable-pip-version-check"' in verifier
    assert '"--no-cache-dir"' in verifier


def test_every_hosted_cli_job_installs_the_locked_runtime_dependencies_first() -> None:
    """setup-python provides Python and pip, not PyYAML or packaging."""
    expected_jobs = {
        ("_build-wheel.yml", "build"),
        ("_build-image.yml", "build"),
        ("release-ucm.yml", "package-chart"),
        ("release-ucm.yml", "aggregate-evidence"),
        ("release-vllm-images.yml", "reconcile-fixture"),
        ("release-vllm-images.yml", "final-reconcile"),
    }
    observed_jobs: set[tuple[str, str]] = set()
    for filename in EXPECTED_RELEASE_WORKFLOWS:
        document = _load_workflow(WORKFLOW_DIR / filename)
        for job_name, job in _jobs(document).items():
            steps = _steps(job)
            cli_indexes = [
                index
                for index, step in enumerate(steps)
                if "python -m ucm_release" in str(step.get("run", ""))
            ]
            if not cli_indexes:
                continue
            observed_jobs.add((filename, job_name))
            prior_commands = "\n".join(
                str(step.get("run", "")) for step in steps[: min(cli_indexes)]
            )
            for required in (
                "python -m pip install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--only-binary=:all:",
                "PyYAML==6.0.2",
                "packaging==24.2",
            ):
                assert required in prior_commands, (filename, job_name, required)
    assert observed_jobs == expected_jobs


def test_reusable_image_router_uses_inputs_not_inherited_event_name() -> None:
    """A nested reusable workflow inherits the caller event (for example push)."""
    document = _load_workflow(WORKFLOW_DIR / "release-vllm-images.yml")
    jobs = _jobs(document)
    standalone_condition = str(jobs["standalone-wheel"]["if"])
    select_condition = str(jobs["select-input"]["if"])
    select_environment = jobs["select-input"]["steps"][0]["env"]

    assert "inputs.source_sha == ''" in standalone_condition
    assert "inputs.source_sha != ''" in select_condition
    assert "github.event_name == 'workflow_call'" not in standalone_condition
    assert "github.event_name == 'workflow_call'" not in select_condition
    assert "inputs.source_sha != ''" in str(select_environment["SOURCE_SHA"])
    assert "inputs.wheel_artifact != ''" in str(select_environment["WHEEL_ARTIFACT"])


def test_reusable_release_entry_uses_input_lane_not_inherited_event_name() -> None:
    """The core reusable entry must recognize a call even when the event is push."""
    document = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    condition = str(_jobs(document)["fork-candidate"]["if"])

    assert "inputs.validation_lane == 'fork-candidate'" in condition
    assert "github.event_name == 'workflow_call'" not in condition


def test_fork_v_tag_runs_the_read_only_candidate_instead_of_green_noop() -> None:
    """A fork tag must prove blocked candidate behavior without entering production."""
    document = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    condition = str(_jobs(document)["fork-candidate"]["if"])

    assert "refs/tags/v" in condition
    assert (
        "github.repository != 'ModelEngine-Group/unified-cache-management'" in condition
    )


def test_production_and_unsupported_callable_lanes_fail_closed() -> None:
    """A caller must get an explicit failure, never a green no-op production run."""
    entry = _load_workflow(WORKFLOW_DIR / "release-ucm.yml")
    entry_jobs = _jobs(entry)
    blocked = entry_jobs["production-external-required"]
    blocked_condition = str(blocked["if"])
    assert "ModelEngine-Group/unified-cache-management" in blocked_condition
    assert "refs/tags/v" in blocked_condition
    assert blocked["environment"] == "ucm-production-release"
    assert "exit 2" in "\n".join(_strings(blocked))

    for filename in ("release-ucm.yml", "release-vllm-images.yml"):
        document = _load_workflow(WORKFLOW_DIR / filename)
        rejected = _jobs(document)["unsupported-validation-lane"]
        condition = str(rejected["if"])
        assert "inputs.validation_lane != 'fork-candidate'" in condition
        assert rejected["permissions"] == {"contents": "read"}
        assert "exit 2" in "\n".join(_strings(rejected))


def test_changed_workflows_pin_actions_and_keep_fork_jobs_read_only() -> None:
    """Every edited action is immutable and every edited workflow defaults read-only."""
    violations: list[str] = []
    for filename in sorted(CHANGED_WORKFLOWS):
        document = _load_workflow(WORKFLOW_DIR / filename)
        if document.get("permissions") != {"contents": "read"}:
            violations.append(
                f"{filename}: workflow permissions are not contents: read"
            )
        for job_name, job in _jobs(document).items():
            if job.get("permissions") != {"contents": "read"}:
                violations.append(
                    f"{filename}:{job_name} permissions are not explicit contents: read"
                )
        for uses in _uses_in(document):
            if uses.startswith("./.github/workflows/"):
                if "@" in uses:
                    violations.append(
                        f"{filename}: local workflow is not same-SHA: {uses}"
                    )
            elif FULL_ACTION_SHA.fullmatch(uses) is None:
                violations.append(f"{filename}: action is not SHA-pinned: {uses}")
    assert violations == []


def test_push_and_pull_request_callers_are_explicitly_read_only() -> None:
    """Normal fork validation callers must not inherit the repository token default."""
    push = _load_workflow(WORKFLOW_DIR / "push-check.yml")
    pull_request = _load_workflow(WORKFLOW_DIR / "pull-request.yml")
    assert push["permissions"] == {"contents": "read"}
    assert _jobs(push)["lint-and-unit-tests"]["permissions"] == {"contents": "read"}
    assert pull_request["permissions"] == {"contents": "read"}
    for job_name, job in _jobs(pull_request).items():
        permissions, _ = _effective_permissions(pull_request["permissions"], job)
        assert permissions == {"contents": "read"}, job_name


def test_lint_workflow_explicitly_runs_compact_release_tests() -> None:
    """The normal push checks must execute the focused release suite on GitHub."""
    lint = _load_workflow(WORKFLOW_DIR / "lint-and-test.yml")
    text = "\n".join(_strings(lint))
    assert "pytest -q .github/release/tests" in text


def test_pin_audit_rejects_a_nested_unpinned_action() -> None:
    """An unpinned action in a reusable workflow must not pass the SHA audit."""
    mutated = {
        "permissions": {"contents": "read"},
        "jobs": {
            "build": {
                "runs-on": "ubuntu-24.04",
                "steps": [{"uses": "actions/checkout@v4"}],
            }
        },
    }
    assert any(
        not uses.startswith("./.github/workflows/")
        and FULL_ACTION_SHA.fullmatch(uses) is None
        for uses in _uses_in(mutated)
    )


def test_fixture_wheel_builder_is_deterministic_unpublished_and_source_bound(
    tmp_path: Path,
) -> None:
    """Workflow wheel generation belongs in tested Python, not inline YAML."""
    _, wheel_module, _ = _release_modules()
    source_sha = "a" * 40

    first = wheel_module.build_fixture_wheel(
        tmp_path / "first", source_sha, FIXTURE_PROFILE
    )
    second = wheel_module.build_fixture_wheel(
        tmp_path / "second", source_sha, FIXTURE_PROFILE
    )

    assert (
        Path(first["wheel_path"]).read_bytes()
        == Path(second["wheel_path"]).read_bytes()
    )
    assert first["wheel_sha256"] == second["wheel_sha256"]
    assert first["inspection_sha256"] == second["inspection_sha256"]
    assert first["build_record"] == second["build_record"]
    assert first["inspection"]["requires_dist"] == ["wrapt==1.17.2"]
    assert first["inspection"]["status"] == "fixture-only"
    assert first["inspection"]["published"] is False
    assert first["build_record"]["source_sha"] == source_sha
    assert first["build_record"]["publication_status"] == "unpublished"


def test_loop_orchestration_prepares_completes_and_aggregates_canonical_evidence(
    tmp_path: Path,
) -> None:
    """The CLI-owned workflow rules must close the same two-reconcile loop."""
    core, wheel_module, verify_module = _release_modules()
    source_sha = "b" * 40
    fixture = wheel_module.build_fixture_wheel(
        tmp_path / "wheel", source_sha, FIXTURE_PROFILE
    )
    prepared = verify_module.prepare_candidate_loop(
        fixture["build_record"],
        fixture["inspection"],
        source_sha=source_sha,
        run={"id": "17", "attempt": 1},
    )

    candidate = prepared["candidate"]
    image_payload = {
        "fixture_only": True,
        "unpublished": True,
        "publication_attempted": False,
        "status": "fixture-verified-unpublished",
        "build_key_sha256": candidate["build_key_sha256"],
        "wheel": {"sha256": fixture["wheel_sha256"]},
        "oci": {"digest": "sha256:" + "9" * 64},
        "gates": {
            "base_verified": "passed",
            "wheel_verified": "passed",
            "install": "passed",
            "pip_check": "passed",
            "direct_url": "passed",
            "ucm_import": "passed",
            "wrapt_import": "passed",
            "abi": "passed",
        },
        "runtime_validation": "external-required",
        "device_validation": "external-required",
    }
    image_result = {
        **image_payload,
        "result_sha256": core.sha256_value(image_payload),
    }
    completed = verify_module.complete_candidate_loop(
        prepared,
        image_result,
        source_sha=source_sha,
        run={"id": "17", "attempt": 1},
    )
    assert completed["second_reconcile"]["task_count"] == 0
    assert completed["evidence"]["payload"]["compatibility"] == {
        "accepted": ["a2", "a3"],
        "rejected": ["310p", "a5"],
    }
    assert completed["evidence"]["payload"]["publication"] == {
        "status": "blocked",
        "attempted": False,
    }

    chart = {
        "sha256": "sha256:" + "7" * 64,
        "release_tree_sha256": "sha256:" + "8" * 64,
        "rendered_cases": ["cuda", "a2", "a3"],
        "status": "candidate-verified",
    }
    aggregate = verify_module.aggregate_release_evidence(
        fixture["build_record"],
        fixture["inspection"],
        chart,
        completed["evidence"],
        repository="SuperMarioYL/unified-cache-management",
        ref="refs/heads/feature/cicd",
        source_sha=source_sha,
        run={"id": "17", "attempt": 1},
    )
    assert aggregate["payload"]["source_sha"] == source_sha
    assert aggregate["payload"]["must_green"] == {
        "fixture_wheel": True,
        "helm_cuda_a2_a3": True,
        "install_only_image": True,
        "second_reconcile_zero": True,
    }
    assert (
        aggregate["payload"]["artifact_digests"]["wheel_sha256"]
        == fixture["wheel_sha256"]
    )
    assert aggregate["payload"]["write_audit"] == {
        "pull_request": False,
        "tag": False,
        "release": False,
        "package": False,
        "upstream": False,
    }
    rerun = verify_module.aggregate_release_evidence(
        fixture["build_record"],
        fixture["inspection"],
        chart,
        completed["evidence"],
        repository="SuperMarioYL/unified-cache-management",
        ref="refs/heads/feature/cicd",
        source_sha=source_sha,
        run={"id": "17", "attempt": 2},
    )
    assert aggregate["payload_sha256"] == rerun["payload_sha256"]


def test_loop_orchestration_rejects_wrong_source_or_published_image(
    tmp_path: Path,
) -> None:
    """A workflow caller cannot relabel bytes or a publication as fixture evidence."""
    core, wheel_module, verify_module = _release_modules()
    source_sha = "c" * 40
    fixture = wheel_module.build_fixture_wheel(
        tmp_path / "wheel", source_sha, FIXTURE_PROFILE
    )
    with __import__("pytest").raises(ValueError, match="source"):
        verify_module.prepare_candidate_loop(
            fixture["build_record"], fixture["inspection"], source_sha="d" * 40, run={}
        )
    prepared = verify_module.prepare_candidate_loop(
        fixture["build_record"], fixture["inspection"], source_sha=source_sha, run={}
    )
    payload = {
        "fixture_only": True,
        "unpublished": False,
        "publication_attempted": True,
        "status": "published",
        "build_key_sha256": prepared["candidate"]["build_key_sha256"],
        "wheel": {"sha256": fixture["wheel_sha256"]},
        "oci": {"digest": "sha256:" + "9" * 64},
        "gates": {},
        "runtime_validation": "external-required",
        "device_validation": "external-required",
    }
    published = {**payload, "result_sha256": core.sha256_value(payload)}
    with __import__("pytest").raises(ValueError, match="unpublished"):
        verify_module.complete_candidate_loop(
            prepared, published, source_sha=source_sha, run={}
        )


def test_image_context_bundle_reopens_fixed_base_descriptor_bytes(
    tmp_path: Path,
) -> None:
    """Base fetching stays orchestration; descriptor validation stays tested Python."""
    _, wheel_module, verify_module = _release_modules()
    image_module = importlib.import_module("ucm_release.image")
    source_sha = "e" * 40
    fixture = wheel_module.build_fixture_wheel(
        tmp_path / "wheel", source_sha, FIXTURE_PROFILE
    )
    prepared = verify_module.prepare_candidate_loop(
        fixture["build_record"],
        fixture["inspection"],
        source_sha=source_sha,
        run={},
    )

    def encoded(value: object) -> tuple[bytes, str]:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return raw, "sha256:" + hashlib.sha256(raw).hexdigest()

    config_raw, config_digest = encoded({"architecture": "amd64", "os": "linux"})
    manifest_raw, manifest_digest = encoded(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config_raw),
            },
            "layers": [],
        }
    )
    index_raw, index_digest = encoded(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest,
                    "size": len(manifest_raw),
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ],
        }
    )
    blobs = {}
    for label, raw in (
        ("index", index_raw),
        ("manifest", manifest_raw),
        ("config", config_raw),
    ):
        path = tmp_path / f"{label}.json"
        path.write_bytes(raw)
        blobs[label] = path
    context = tmp_path / "context"
    recipe = image_module.prepare_context_bundle(
        prepared["image_input"],
        wheel_dir=tmp_path / "wheel",
        base_repository="docker.io/library/python",
        base_index_path=blobs["index"],
        base_manifest_path=blobs["manifest"],
        base_config_path=blobs["config"],
        expected_index_digest=index_digest,
        expected_manifest_digest=manifest_digest,
        expected_config_digest=config_digest,
        output_dir=context,
    )
    assert recipe["payload"]["base"]["subject"] == (
        "docker.io/library/python@" + manifest_digest
    )
    assert (
        sorted(path.name for path in context.iterdir())
        == recipe["payload"]["context_files"]
    )
    blobs["config"].write_bytes(config_raw + b"\n")
    with __import__("pytest").raises(ValueError, match="config digest"):
        image_module.prepare_context_bundle(
            prepared["image_input"],
            wheel_dir=tmp_path / "wheel",
            base_repository="docker.io/library/python",
            base_index_path=blobs["index"],
            base_manifest_path=blobs["manifest"],
            base_config_path=blobs["config"],
            expected_index_digest=index_digest,
            expected_manifest_digest=manifest_digest,
            expected_config_digest=config_digest,
            output_dir=tmp_path / "bad-context",
        )


def test_compact_cli_owns_fixture_build_and_loop_preparation(tmp_path: Path) -> None:
    """Hosted jobs should invoke small CLI commands instead of inline policy code."""
    release_root = REPO_ROOT / ".github" / "release"
    environment = {**__import__("os").environ, "PYTHONPATH": str(release_root)}
    source_sha = "f" * 40
    wheel_dir = tmp_path / "wheel"
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "ucm_release",
            "wheel",
            "fixture-build",
            "--output-dir",
            str(wheel_dir),
            "--source-sha",
            source_sha,
            "--profile-id",
            FIXTURE_PROFILE,
        ],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    prepared_dir = tmp_path / "prepared"
    prepared = subprocess.run(
        [
            sys.executable,
            "-m",
            "ucm_release",
            "loop",
            "prepare",
            "--build-record",
            str(wheel_dir / "fixture-build.json"),
            "--wheel-inspection",
            str(wheel_dir / "wheel-inspection.json"),
            "--source-sha",
            source_sha,
            "--output-dir",
            str(prepared_dir),
            "--run-id",
            "local",
            "--attempt",
            "1",
        ],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    assert {
        "prepared-loop.json",
        "image-input.json",
        "candidate.json",
        "first-reconcile.json",
        "first-reconcile.sha256",
        "loop-verification.json",
    } == {path.name for path in prepared_dir.iterdir()}


def test_yaml_workflow_inherits_write_permissions_and_rejects_unknown_actions(
    tmp_path: Path,
) -> None:
    """Both permission inheritance and unknown action capability apply to .yaml."""
    (tmp_path / "publish.yaml").write_text(
        """
permissions: write-all
jobs:
  inherited-permission:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/create-release@v1
  job-permission:
    permissions:
      contents: write
    runs-on: ubuntu-24.04
    steps:
      - run: echo publish
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "copy.yaml").write_text(
        """
permissions:
  contents: write
jobs:
  inherited-map-permission:
    runs-on: ubuntu-24.04
    steps:
      - run: echo publish
""".lstrip(),
        encoding="utf-8",
    )

    violations = _fork_isolation_violations(_release_workflow_documents(tmp_path))

    assert len(violations) == 3
    assert any(
        "publish.yaml:inherited-permission" in violation
        and "workflow-inherited write permission" in violation
        and "unapproved action actions/create-release" in violation
        for violation in violations
    )
    assert any(
        "publish.yaml:job-permission" in violation and "write permission" in violation
        for violation in violations
    )
    assert any(
        "copy.yaml:inherited-map-permission" in violation
        and "workflow-inherited write permission" in violation
        for violation in violations
    )
