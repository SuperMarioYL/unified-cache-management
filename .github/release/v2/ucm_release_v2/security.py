"""Reusable static read-only audit for the v2 control plane and workflows."""

from __future__ import annotations

import ast
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml


@dataclass(frozen=True)
class Finding:
    """One fail-closed security audit finding."""

    source: str
    message: str
    line: int | None = None


_ACTIONS = {
    "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683": "checkout",
    "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065": "setup-python",
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02": "upload-artifact",
}
_ALLOWED_IMPORTS = {
    "argparse",
    "ast",
    "collections.abc",
    "copy",
    "dataclasses",
    "datetime",
    "hashlib",
    "html",
    "importlib.metadata",
    "importlib.util",
    "json",
    "os",
    "pathlib",
    "re",
    "shlex",
    "typing",
    "yaml",
}
_ALLOWED_FROM_IMPORTS = {
    "__future__": {"annotations"},
    "collections.abc": {"Iterable", "Mapping"},
    "copy": {"deepcopy"},
    "dataclasses": {"dataclass"},
    "datetime": {"date", "datetime", "timedelta", "timezone"},
    "pathlib": {"Path", "PurePosixPath"},
    "typing": {"Any", "Iterable"},
    "ucm_release_v2.github_readonly": {
        "ReadOnlyGitHubError",
        "validate_control_identity",
    },
}
_ALLOWED_MODULE_APIS = {
    "argparse": {"ArgumentParser"},
    "ast": {"dump", "parse", "walk"},
    "hashlib": {"sha256"},
    "html": {"escape"},
    "importlib.metadata": {"distributions"},
    "importlib.util": {"module_from_spec", "spec_from_file_location"},
    "json": {"dumps", "loads"},
    "re": {"compile", "escape", "findall", "fullmatch", "match", "search", "sub"},
    "shlex": {"split"},
    "yaml": {"load", "safe_load"},
}
_ALLOWED_BUILTIN_CALLS = {
    "SystemExit",
    "ValueError",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "frozenset",
    "int",
    "isinstance",
    "len",
    "list",
    "next",
    "ord",
    "print",
    "reversed",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
}
_ALLOWED_METHOD_CALLS = {
    "add",
    "add_argument",
    "add_constructor",
    "add_mutually_exclusive_group",
    "add_parser",
    "add_subparsers",
    "append",
    "as_posix",
    "check_environment",
    "compile",
    "count",
    "construct_object",
    "date",
    "distributions",
    "dumps",
    "encode",
    "endswith",
    "error",
    "escape",
    "exists",
    "exit",
    "extend",
    "findall",
    "finditer",
    "fromisoformat",
    "fullmatch",
    "get",
    "group",
    "hexdigest",
    "index",
    "installed_distributions",
    "is_absolute",
    "is_dir",
    "is_file",
    "isascii",
    "isoformat",
    "items",
    "join",
    "joinpath",
    "loads",
    "lower",
    "match",
    "mkdir",
    "now",
    "open",
    "parse",
    "parse_args",
    "pop",
    "read_bytes",
    "read_text",
    "recovery_guidance",
    "relative_to",
    "replace",
    "resolve",
    "rglob",
    "safe_load",
    "search",
    "setdefault",
    "sha256",
    "sort",
    "split",
    "splitlines",
    "startswith",
    "strip",
    "strptime",
    "sub",
    "update",
    "values",
    "walk",
    "write",
    "write_bytes",
    "write_text",
}
_ALLOWED_DEFINITIONS_BY_SUFFIX = {
    "packaging/backend_guard.py": {
        "BackendConflictError",
        "GuardError",
        "MetadataError",
        "_declares_ucm",
        "_looks_like_ucm_family",
        "_metadata_error",
        "_normalize_records",
        "_validated_version",
        "canonicalize_name",
        "check_environment",
        "installed_distributions",
        "recovery_guidance",
        "require_single_backend",
    },
    "ucm_release_v2/artifacts.py": {
        "ArtifactError",
        "_artifact_file",
        "_base_dir",
        "_digest",
        "_exact_mapping",
        "_lifecycle_plan",
        "_load_json",
        "_manifest_item",
        "_platforms",
        "_product",
        "_records",
        "_string",
        "collect_artifacts",
        "reject_duplicates",
        "validate_artifacts",
    },
    "ucm_release_v2/cleanup.py": {
        "CleanupError",
        "_exact",
        "_identity",
        "_list",
        "_mapping",
        "_reference_identity",
        "_retention",
        "_safe_string",
        "_timestamp",
        "build_cleanup_plan",
    },
    "ucm_release_v2/cli.py": {
        "_command_body",
        "_config_argument",
        "_emit",
        "_intent",
        "_read_json_object",
        "build_parser",
        "main",
        "reject_duplicate_keys",
        "run",
    },
    "ucm_release_v2/commands.py": {
        "CommandError",
        "_identity",
        "_source_identity",
        "parse_command",
    },
    "ucm_release_v2/common.py": {
        "SafePathError",
        "canonical_json",
        "safe_posix_path",
        "sha256_envelope",
    },
    "ucm_release_v2/config.py": {
        "ConfigError",
        "RejectDuplicateSafeLoader",
        "_exact_mapping",
        "_mapping",
        "_nonempty_string",
        "_validate_products",
        "_validate_repository_policy",
        "construct_unique_mapping",
        "load_config",
        "reject_duplicates",
        "retention_days",
    },
    "ucm_release_v2/environment.py": {
        "EnvironmentError",
        "_artifact",
        "_artifacts",
        "_choice",
        "_digest",
        "_exact",
        "_expected_products",
        "_hex",
        "_literal",
        "_load_json",
        "_path",
        "_request_checks",
        "_trimmed",
        "_validate_request",
        "export_request",
        "reject_duplicates",
        "simulate_result",
        "verify_result",
    },
    "ucm_release_v2/github_readonly.py": {
        "ReadOnlyGitHubError",
        "_observed_commit",
        "load_json",
        "main",
        "reject_duplicates",
        "validate_control_identity",
        "validate_develop_workflow_run",
        "validate_develop_reads",
        "validate_reusable_control_reads",
    },
    "ucm_release_v2/lifecycle.py": {
        "LifecycleError",
        "_intent",
        "_plan_gates",
        "_plan_operations",
        "_plan_version",
        "_products",
        "_require_sha",
        "_route",
        "_version",
        "build_plan",
        "validate_plan",
        "verify_envelope",
    },
    "ucm_release_v2/policy.py": {
        "PolicyError",
        "_boolean",
        "_check",
        "_exact",
        "_list",
        "_mapping",
        "_pattern",
        "_string",
        "_unique",
        "audit_repository_policy",
    },
    "ucm_release_v2/reconcile.py": {
        "ReconcileError",
        "_artifact",
        "_artifacts",
        "_choice",
        "_digest",
        "_environment_replay",
        "_exact",
        "_expected_draft_version",
        "_hex",
        "_identity",
        "_inventory",
        "_operations",
        "_path",
        "_promotion",
        "_promotion_blockers",
        "_promotion_lineage",
        "_trimmed",
        "_verify_read_only_envelope",
        "build_reconcile_plan",
        "load_json",
        "load_release_inputs",
        "reject_duplicates",
        "validate_reconcile_plan",
    },
    "ucm_release_v2/render.py": {
        "RenderError",
        "_known_issues",
        "_platform_digest",
        "_shell_quote",
        "render_release_preview",
    },
    "ucm_release_v2/security.py": {
        "Finding",
        "_attribute_path",
        "_audit_action",
        "_audit_python_command",
        "_audit_shell",
        "_audit_triggers",
        "_backend_guard_loader_is_exact",
        "_curl_argv",
        "_develop_inline_validator_is_semantic",
        "_expression_matches",
        "_extract_canonical_heredocs",
        "_failing_guard_index",
        "_is_os_environ",
        "_observed_function_is_semantic",
        "_output_block_index",
        "_permissions",
        "_single_assignment_index",
        "_wheels_path_provenance_is_exact",
        "audit_python_source",
        "audit_repository",
        "audit_workflow_source",
        "python_audit_paths",
    },
    "ucm_release_v2/wheels.py": {
        "WheelError",
        "_guard_module",
        "_installed_fixture",
        "_lifecycle_plan",
        "_load_json",
        "_metadata",
        "build_wheel_plan",
        "check_environment",
        "reject_duplicates",
    },
    "develop-release-dry-run.yml": {"observed", "reject_duplicates"},
    "release-control-dry-run.yml": {
        "digest",
        "observed",
        "reject_duplicates",
        "strict_object",
        "loads_strict",
    },
}
_ALLOWED_ENV_EXPRESSIONS = {
    "${{ github.actor }}",
    "${{ github.event.comment.author_association }}",
    "${{ github.event.comment.body }}",
    "${{ github.event.issue.number }}",
    "${{ github.event.pull_request.base.sha }}",
    "${{ github.event.pull_request.head.sha }}",
    "${{ github.event.pull_request.number }}",
    "${{ github.event.repository.default_branch }}",
    "${{ github.event.workflow_run.conclusion }}",
    "${{ github.event.workflow_run.event }}",
    "${{ github.event.workflow_run.head_branch }}",
    "${{ github.event.workflow_run.head_repository.full_name }}",
    "${{ github.event.workflow_run.head_sha }}",
    "${{ github.event.workflow_run.name }}",
    "${{ github.event.workflow_run.path }}",
    "${{ github.ref_name }}",
    "${{ github.repository }}",
    "${{ github.run_number }}",
    "${{ github.sha }}",
    "${{ github.token }}",
    "${{ github.workflow_sha }}",
    "${{ toJSON(job) }}",
    "${{ inputs.as_of }}",
    "${{ inputs.environment }}",
    "${{ inputs.intent_json }}",
    "${{ inputs.inventory_json }}",
    "${{ inputs.known_issues_json }}",
    "${{ inputs.nonce }}",
    "${{ inputs.operation }}",
    "${{ inputs.promotion_json }}",
    "${{ inputs.promotion_source_lifecycle_plan_json }}",
    "${{ inputs.promotion_source_manifest_json }}",
    "${{ inputs.repository_role }}",
    "${{ inputs.snapshot_json }}",
    "${{ inputs.source_sha }}",
    "${{ inputs.stage }}",
    "${{ runner.temp }}/pr-comment-preview",
    "${{ runner.temp }}/pr-release-preview",
    "${{ needs.control.outputs.control_sha }}",
    "${{ steps.develop.outputs.source_sha }}",
    "${{ steps.control.outputs.control_sha }}",
    "${{ steps.control.outputs.source_sha }}",
    "${{ steps.identity.outputs.base_sha }}",
    "${{ steps.identity.outputs.control_sha }}",
    "${{ steps.identity.outputs.current_sha }}",
    "${{ steps.identity.outputs.develop_branch }}",
    "${{ steps.identity.outputs.observed_sha }}",
    "${{ steps.identity.outputs.source_sha }}",
    "${{ steps.identity.outputs.utc_date }}",
}
_PR_ENDPOINT = "https://api.github.com/repos/${REPOSITORY}/pulls/${PR_NUMBER}"
_NIGHTLY_ENDPOINT = (
    "https://api.github.com/repos/${REPOSITORY}/git/ref/heads/${DEVELOP_BRANCH}"
)
_MAIN_ENDPOINT = (
    "https://api.github.com/repos/${REPOSITORY}/git/ref/heads/${CONFIGURED_MAIN}"
)
_ALLOWED_BRACED_SHELL_EXPANSIONS = {
    "CONFIGURED_MAIN",
    "DEVELOP_BRANCH",
    "GH_TOKEN",
    "PR_NUMBER",
    "REPOSITORY",
    "reconcile_args[@]",
    "render_args[@]",
}
_ALLOWED_SIMPLE_SHELL_EXPANSIONS = {
    "ACTOR",
    "AS_OF",
    "AUTHOR_ASSOCIATION",
    "CURRENT_SOURCE_SHA",
    "DEVELOP_BRANCH",
    "ENVIRONMENT",
    "GITHUB_OUTPUT",
    "NONCE",
    "OBSERVED_SOURCE_SHA",
    "OUTPUT_DIR",
    "PR_NUMBER",
    "REPOSITORY_ROLE",
    "RUNNER_TEMP",
    "RUN_NUMBER",
    "SOURCE_SHA",
    "STAGE",
    "UTC_DATE",
}
_ACTION_STEP_POLICY: dict[tuple[str, str], tuple[str, dict[str, object]]] = {
    (
        "develop-release-dry-run.yml",
        "Checkout immutable trusted default-main control revision",
    ): (
        "checkout",
        {
            "ref": "${{ steps.control.outputs.control_sha }}",
            "path": "control",
            "persist-credentials": False,
        },
    ),
    ("develop-release-dry-run.yml", "Set up Python"): (
        "setup-python",
        {"python-version": "3.12"},
    ),
    ("develop-release-dry-run.yml", "Upload develop preview"): (
        "upload-artifact",
        {
            "name": "develop-release-dry-run-${{ github.run_number }}-${{ steps.control.outputs.source_sha }}",
            "path": "preview/",
            "retention-days": 14,
            "if-no-files-found": "error",
        },
    ),
    ("release-control-dry-run.yml", "Checkout trusted workflow control tree"): (
        "checkout",
        {
            "ref": "${{ needs.control.outputs.control_sha }}",
            "path": "control",
            "persist-credentials": False,
        },
    ),
    (
        "release-control-dry-run.yml",
        "Checkout immutable trusted workflow control revision",
    ): (
        "checkout",
        {
            "ref": "${{ needs.control.outputs.control_sha }}",
            "path": "control",
            "persist-credentials": False,
        },
    ),
    ("release-control-dry-run.yml", "Set up Python"): (
        "setup-python",
        {"python-version": "3.12"},
    ),
    ("release-control-dry-run.yml", "Upload simulated environment package"): (
        "upload-artifact",
        {
            "name": "draft-environment-${{ inputs.environment }}-${{ steps.identity.outputs.source_sha }}-${{ inputs.nonce }}",
            "path": "preview/lifecycle-plan.json\npreview/artifact-manifest.json\npreview/environment-test-request.json\npreview/environment-test-result.json\npreview/environment-verification.json\npreview/summary.md\n",
            "retention-days": 30,
            "if-no-files-found": "error",
        },
    ),
    ("release-control-dry-run.yml", "Upload protected release preview package"): (
        "upload-artifact",
        {
            "name": "protected-release-dry-run-${{ inputs.stage }}-${{ inputs.source_sha }}-${{ github.run_number }}",
            "path": "preview/lifecycle-plan.json\npreview/artifact-manifest.json\npreview/reconcile-plan.json\npreview/release-preview.md\npreview/release-inventory.json\npreview/known-issues.json\npreview/promotion-evidence.json\npreview/promotion-source-lifecycle-plan.json\npreview/promotion-source-manifest.json\n",
            "retention-days": 30,
            "if-no-files-found": "error",
        },
    ),
    ("release-control-dry-run.yml", "Upload cleanup preview"): (
        "upload-artifact",
        {
            "name": "release-cleanup-dry-run-${{ github.run_id }}-${{ github.run_attempt }}",
            "path": "preview/cleanup-inventory.json\npreview/cleanup-plan.json\n",
            "retention-days": 30,
            "if-no-files-found": "error",
        },
    ),
    ("release-control-dry-run.yml", "Upload repository policy report"): (
        "upload-artifact",
        {
            "name": "repository-policy-audit-dry-run-${{ github.run_id }}-${{ github.run_attempt }}",
            "path": "preview/repository-policy-snapshot.json\npreview/repository-policy-report.json\n",
            "retention-days": 30,
            "if-no-files-found": "error",
        },
    ),
    ("draft-environment-dry-run.yml", "Checkout trusted workflow control tree"): (
        "checkout",
        {
            "ref": "${{ steps.control.outputs.control_sha }}",
            "path": "control",
            "persist-credentials": False,
        },
    ),
    ("draft-environment-dry-run.yml", "Set up Python"): (
        "setup-python",
        {"python-version": "3.12"},
    ),
    ("draft-environment-dry-run.yml", "Upload simulated environment package"): (
        "upload-artifact",
        {
            "name": "draft-environment-${{ inputs.environment }}-${{ steps.identity.outputs.source_sha }}-${{ inputs.nonce }}",
            "path": "preview/lifecycle-plan.json\npreview/artifact-manifest.json\npreview/environment-test-request.json\npreview/environment-test-result.json\npreview/environment-verification.json\npreview/summary.md\n",
            "retention-days": 30,
            "if-no-files-found": "error",
        },
    ),
    (
        "nightly-release-dry-run.yml",
        "Checkout immutable trusted default-branch control revision",
    ): (
        "checkout",
        {"ref": "${{ github.workflow_sha }}", "persist-credentials": False},
    ),
    ("nightly-release-dry-run.yml", "Set up Python"): (
        "setup-python",
        {"python-version": "3.12"},
    ),
    ("nightly-release-dry-run.yml", "Upload Nightly preview"): (
        "upload-artifact",
        {
            "name": "nightly-release-dry-run-${{ steps.identity.outputs.utc_date }}-${{ github.run_number }}-${{ steps.develop.outputs.source_sha }}",
            "path": "preview/",
            "retention-days": 14,
            "if-no-files-found": "error",
        },
    ),
    (
        "pr-release-dry-run.yml",
        "Checkout immutable trusted workflow control revision",
    ): (
        "checkout",
        {
            "ref": "${{ github.workflow_sha }}",
            "path": "control",
            "persist-credentials": False,
        },
    ),
    ("pr-release-dry-run.yml", "Checkout immutable trusted control revision"): (
        "checkout",
        {
            "ref": "${{ github.workflow_sha }}",
            "path": "control",
            "persist-credentials": False,
        },
    ),
    ("pr-release-dry-run.yml", "Set up Python"): (
        "setup-python",
        {"python-version": "3.12"},
    ),
    ("pr-release-dry-run.yml", "Upload PR preview"): (
        "upload-artifact",
        {
            "name": "pr-release-dry-run-${{ github.event.pull_request.number }}-${{ github.event.pull_request.head.sha }}",
            "path": "${{ runner.temp }}/pr-release-preview/",
            "retention-days": 7,
            "if-no-files-found": "error",
        },
    ),
    ("pr-release-dry-run.yml", "Upload comment command preview"): (
        "upload-artifact",
        {
            "name": "pr-command-dry-run-${{ github.event.issue.number }}-${{ github.run_id }}",
            "path": "${{ runner.temp }}/pr-comment-preview/",
            "retention-days": 7,
            "if-no-files-found": "warn",
        },
    ),
    (
        "release-cleanup-dry-run.yml",
        "Checkout immutable trusted workflow control revision",
    ): (
        "checkout",
        {
            "ref": "${{ steps.control.outputs.control_sha }}",
            "path": "control",
            "persist-credentials": False,
        },
    ),
    ("release-cleanup-dry-run.yml", "Set up Python"): (
        "setup-python",
        {"python-version": "3.12"},
    ),
    ("release-cleanup-dry-run.yml", "Upload cleanup preview"): (
        "upload-artifact",
        {
            "name": "release-cleanup-dry-run-${{ github.run_id }}-${{ github.run_attempt }}",
            "path": "preview/cleanup-inventory.json\npreview/cleanup-plan.json\n",
            "retention-days": 30,
            "if-no-files-found": "error",
        },
    ),
    ("release-lifecycle-dry-run.yml", "Checkout trusted workflow control tree"): (
        "checkout",
        {
            "ref": "${{ steps.control.outputs.control_sha }}",
            "path": "control",
            "persist-credentials": False,
        },
    ),
    ("release-lifecycle-dry-run.yml", "Set up Python"): (
        "setup-python",
        {"python-version": "3.12"},
    ),
    ("release-lifecycle-dry-run.yml", "Upload protected release preview package"): (
        "upload-artifact",
        {
            "name": "protected-release-dry-run-${{ inputs.stage }}-${{ inputs.source_sha }}-${{ github.run_number }}",
            "path": "preview/lifecycle-plan.json\npreview/artifact-manifest.json\npreview/reconcile-plan.json\npreview/release-preview.md\npreview/release-inventory.json\npreview/known-issues.json\npreview/promotion-evidence.json\npreview/promotion-source-lifecycle-plan.json\npreview/promotion-source-manifest.json\n",
            "retention-days": 30,
            "if-no-files-found": "error",
        },
    ),
    (
        "repository-policy-audit-dry-run.yml",
        "Checkout immutable trusted workflow control revision",
    ): (
        "checkout",
        {
            "ref": "${{ steps.control.outputs.control_sha }}",
            "path": "control",
            "persist-credentials": False,
        },
    ),
    ("repository-policy-audit-dry-run.yml", "Set up Python"): (
        "setup-python",
        {"python-version": "3.12"},
    ),
    ("repository-policy-audit-dry-run.yml", "Upload repository policy report"): (
        "upload-artifact",
        {
            "name": "repository-policy-audit-dry-run-${{ github.run_id }}-${{ github.run_attempt }}",
            "path": "preview/repository-policy-snapshot.json\npreview/repository-policy-report.json\n",
            "retention-days": 30,
            "if-no-files-found": "error",
        },
    ),
}
_ACTION_IF_POLICY = {
    (
        "pr-release-dry-run.yml",
        "Upload comment command preview",
    ): "${{ always() }}",
}
_TRUSTED_CONTROLLER_USES = (
    "SuperMarioYL" + "/" + "unified-cache-management/"
    ".github/workflows/release-control-dry-run.yml@main"
)
_REUSABLE_JOB_POLICY: dict[str, dict[str, object]] = {
    "draft-environment-dry-run.yml": {
        "permissions": {"contents": "read"},
        "uses": _TRUSTED_CONTROLLER_USES,
        "with": {
            "operation": "draft-environment",
            "environment": "${{ inputs.environment }}",
            "intent_json": "${{ inputs.intent_json }}",
            "nonce": "${{ inputs.nonce }}",
        },
    },
    "release-lifecycle-dry-run.yml": {
        "permissions": {"contents": "read"},
        "uses": _TRUSTED_CONTROLLER_USES,
        "with": {
            "operation": "protected-lifecycle",
            "stage": "${{ inputs.stage }}",
            "source_sha": "${{ inputs.source_sha }}",
            "intent_json": "${{ inputs.intent_json }}",
            "inventory_json": "${{ inputs.inventory_json }}",
            "promotion_json": "${{ inputs.promotion_json }}",
            "promotion_source_lifecycle_plan_json": "${{ inputs.promotion_source_lifecycle_plan_json }}",
            "promotion_source_manifest_json": "${{ inputs.promotion_source_manifest_json }}",
            "known_issues_json": "${{ inputs.known_issues_json }}",
        },
    },
    "release-cleanup-dry-run.yml": {
        "permissions": {"contents": "read"},
        "uses": _TRUSTED_CONTROLLER_USES,
        "with": {
            "operation": "cleanup",
            "as_of": "${{ inputs.as_of }}",
            "inventory_json": "${{ inputs.inventory_json }}",
        },
    },
    "repository-policy-audit-dry-run.yml": {
        "permissions": {"contents": "read"},
        "uses": _TRUSTED_CONTROLLER_USES,
        "with": {
            "operation": "policy-audit",
            "repository_role": "${{ inputs.repository_role }}",
            "snapshot_json": "${{ inputs.snapshot_json }}",
        },
    },
}

_WORKFLOW_EVENT_POLICY = {
    "develop-release-dry-run.yml": {"workflow_run"},
    "draft-environment-dry-run.yml": {"workflow_dispatch"},
    "nightly-release-dry-run.yml": {"schedule"},
    "pr-release-dry-run.yml": {"pull_request", "issue_comment"},
    "release-cleanup-dry-run.yml": {"workflow_dispatch"},
    "release-control-dry-run.yml": {"workflow_call"},
    "release-lifecycle-dry-run.yml": {"workflow_dispatch"},
    "repository-policy-audit-dry-run.yml": {"workflow_dispatch"},
}
_DEVELOP_WORKFLOW_RUN_POLICY = {
    "workflows": ["Push Commit Checks"],
    "branches": ["develop"],
    "types": ["completed"],
}
_PR_EVENT_POLICY = {
    "pull_request": {"types": ["opened", "reopened", "synchronize"]},
    "issue_comment": {"types": ["created"]},
}
_TRUSTED_CHECKOUT_PREDECESSORS = {
    (
        "develop-release-dry-run.yml",
        "Checkout immutable trusted default-main control revision",
    ): "Validate trusted controller and develop source event",
}


def python_audit_paths(v2_root: Path) -> list[Path]:
    """List every shipped Python file, including nested packaging code."""
    return sorted(
        path
        for path in v2_root.rglob("*.py")
        if "tests" not in path.relative_to(v2_root).parts
    )


def _attribute_path(node: ast.expr) -> tuple[str, ...] | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


_BACKEND_GUARD_FUNCTION_SOURCE = """
def _guard_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ucm_release_v2_backend_guard", _PACKAGING_ROOT / "backend_guard.py"
    )
    if spec is None or spec.loader is None:
        raise WheelError("cannot load reusable backend guard")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
"""
_BACKEND_GUARD_FUNCTION_AST = ast.dump(
    ast.parse(_BACKEND_GUARD_FUNCTION_SOURCE).body[0], include_attributes=False
)
_WHEELS_PATH_BINDINGS_SOURCE = """
_V2_ROOT = Path(__file__).resolve().parents[1]
_PACKAGING_ROOT = _V2_ROOT / "packaging"
"""
_WHEELS_PATH_BINDINGS_AST = ast.parse(_WHEELS_PATH_BINDINGS_SOURCE).body


def _wheels_path_provenance_is_exact(tree: ast.AST, name: str) -> bool:
    if not name.endswith("/ucm_release_v2/wheels.py"):
        return True
    if not isinstance(tree, ast.Module):
        return False
    binding_names = {"_V2_ROOT", "_PACKAGING_ROOT"}
    bindings: list[ast.Assign] = []
    positions: list[int] = []
    for index, candidate in enumerate(tree.body):
        if (
            isinstance(candidate, ast.Assign)
            and len(candidate.targets) == 1
            and isinstance(candidate.targets[0], ast.Name)
            and candidate.targets[0].id in binding_names
        ):
            bindings.append(candidate)
            positions.append(index)
    if len(bindings) != 2 or positions[1] != positions[0] + 1:
        return False
    if [ast.dump(binding, include_attributes=False) for binding in bindings] != [
        ast.dump(binding, include_attributes=False)
        for binding in _WHEELS_PATH_BINDINGS_AST
    ]:
        return False
    approved_targets = [binding.targets[0] for binding in bindings]
    writes = [
        candidate
        for candidate in ast.walk(tree)
        if isinstance(candidate, ast.Name)
        and candidate.id in binding_names
        and isinstance(candidate.ctx, (ast.Store, ast.Del))
    ]
    return len(writes) == 2 and all(
        any(write is target for target in approved_targets) for write in writes
    )


def _backend_guard_loader_is_exact(tree: ast.AST, node: ast.Call, name: str) -> bool:
    if not name.endswith("/ucm_release_v2/wheels.py"):
        return False
    functions = [
        candidate
        for candidate in ast.walk(tree)
        if isinstance(candidate, ast.FunctionDef) and candidate.name == "_guard_module"
    ]
    if len(functions) != 1:
        return False
    function = functions[0]
    if not any(candidate is node for candidate in ast.walk(function)):
        return False
    return ast.dump(function, include_attributes=False) == _BACKEND_GUARD_FUNCTION_AST


def _expression_matches(node: ast.AST, source: str) -> bool:
    expected = ast.parse(source, mode="eval").body
    return ast.dump(node, include_attributes=False) == ast.dump(
        expected, include_attributes=False
    )


def _single_assignment_index(
    statements: list[ast.stmt], target: str, expression: str
) -> int | None:
    matches: list[int] = []
    for index, statement in enumerate(statements):
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == target
            and _expression_matches(statement.value, expression)
        ):
            matches.append(index)
    return matches[0] if len(matches) == 1 else None


def _failing_guard_index(statements: list[ast.stmt], test: str) -> int | None:
    matches: list[int] = []
    for index, statement in enumerate(statements):
        if (
            not isinstance(statement, ast.If)
            or statement.orelse
            or not _expression_matches(statement.test, test)
            or len(statement.body) != 1
            or not isinstance(statement.body[0], ast.Raise)
        ):
            continue
        exception = statement.body[0].exc
        if (
            isinstance(exception, ast.Call)
            and isinstance(exception.func, ast.Name)
            and exception.func.id == "SystemExit"
        ):
            matches.append(index)
    return matches[0] if len(matches) == 1 else None


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr == "environ"
    )


def _observed_function_is_semantic(tree: ast.Module) -> bool:
    functions = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.FunctionDef) and statement.name == "observed"
    ]
    if len(functions) != 1:
        return False
    body = functions[0].body
    if len(body) != 7:
        return False
    value = _single_assignment_index(
        body,
        "value",
        'json.loads(path.read_text(encoding="utf-8"), '
        "object_pairs_hook=reject_duplicates)",
    )
    main_ref = _failing_guard_index(
        body,
        'not isinstance(value, dict) or value.get("ref") != "refs/heads/main"',
    )
    target = _single_assignment_index(body, "target", 'value.get("object")')
    commit = _failing_guard_index(
        body,
        'not isinstance(target, dict) or target.get("type") != "commit"',
    )
    sha = _single_assignment_index(body, "sha", 'target.get("sha")')
    sha_format = _failing_guard_index(
        body,
        'not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)',
    )
    returned = body[6]
    return (
        [value, main_ref, target, commit, sha, sha_format] == [0, 1, 2, 3, 4, 5]
        and isinstance(returned, ast.Return)
        and isinstance(returned.value, ast.Name)
        and returned.value.id == "sha"
    )


def _output_block_index(statements: list[ast.stmt]) -> int | None:
    expected_context = 'Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8")'
    expected_body = ast.parse(
        'output.write(f"control_sha={first}\\n")\n'
        'output.write(f"source_sha={source_sha}\\n")\n'
    ).body
    matches: list[int] = []
    for index, statement in enumerate(statements):
        if (
            not isinstance(statement, ast.With)
            or len(statement.items) != 1
            or not _expression_matches(
                statement.items[0].context_expr, expected_context
            )
            or not isinstance(statement.items[0].optional_vars, ast.Name)
            or statement.items[0].optional_vars.id != "output"
            or len(statement.body) != len(expected_body)
            or any(
                ast.dump(observed, include_attributes=False)
                != ast.dump(expected, include_attributes=False)
                for observed, expected in zip(statement.body, expected_body)
            )
        ):
            continue
        matches.append(index)
    return matches[0] if len(matches) == 1 else None


def _develop_inline_validator_is_semantic(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    if not _observed_function_is_semantic(tree):
        return False
    body = tree.body
    default_main = _failing_guard_index(
        body,
        'os.environ["CONFIGURED_MAIN"] != "main" '
        'or os.environ["DEFAULT_BRANCH"] != "main" '
        'or os.environ["GITHUB_REF"] != "refs/heads/main" '
        'or os.environ["GITHUB_REF_NAME"] != "main"',
    )
    event_repository = _failing_guard_index(
        body,
        'os.environ["EVENT_REPOSITORY"] ' '!= "SuperMarioYL/unified-cache-management"',
    )
    workflow_name = _failing_guard_index(
        body, 'os.environ["WORKFLOW_NAME"] != "Push Commit Checks"'
    )
    workflow_event = _failing_guard_index(
        body, 'os.environ["WORKFLOW_EVENT"] != "push"'
    )
    workflow_path = _failing_guard_index(
        body,
        'os.environ["WORKFLOW_PATH"] ' '!= ".github/workflows/push-check.yml@develop"',
    )
    conclusion = _failing_guard_index(
        body, 'os.environ["WORKFLOW_CONCLUSION"] != "success"'
    )
    develop_head = _failing_guard_index(
        body,
        'os.environ["HEAD_BRANCH"] != "develop" '
        'or os.environ["HEAD_REPOSITORY"] '
        '!= os.environ["EVENT_REPOSITORY"]',
    )
    source_sha = _single_assignment_index(body, "source_sha", 'os.environ["HEAD_SHA"]')
    source_format = _failing_guard_index(
        body, 'not re.fullmatch(r"[0-9a-f]{40}", source_sha)'
    )
    first = _single_assignment_index(
        body,
        "first",
        'observed(Path(os.environ["RUNNER_TEMP"]) / "main-ref-first.json", "first")',
    )
    second = _single_assignment_index(
        body,
        "second",
        'observed(Path(os.environ["RUNNER_TEMP"]) / "main-ref-second.json", "second")',
    )
    stable_control = _failing_guard_index(
        body,
        'first != second or first != os.environ["WORKFLOW_SHA"]',
    )
    outputs = _output_block_index(body)
    positions = [
        default_main,
        event_repository,
        workflow_name,
        workflow_event,
        workflow_path,
        conclusion,
        develop_head,
        source_sha,
        source_format,
        first,
        second,
        stable_control,
        outputs,
    ]
    if any(position is None for position in positions) or positions != sorted(
        positions
    ):
        return False
    output_block = body[outputs]
    if not isinstance(output_block, ast.With):
        return False
    output_references = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "GITHUB_OUTPUT"
    ]
    approved_output_names = [
        output_block.items[0].optional_vars,
        output_block.body[0].value.func.value,
        output_block.body[1].value.func.value,
    ]
    approved_output_calls = [
        output_block.items[0].context_expr,
        output_block.body[0].value,
        output_block.body[1].value,
    ]
    observed_output_names = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "output"
    ]
    if len(output_references) != 1 or (
        len(observed_output_names) != len(approved_output_names)
        or any(
            not any(observed is approved for approved in approved_output_names)
            for observed in observed_output_names
        )
    ):
        return False
    output_methods = {"open", "write", "write_bytes", "write_text", "writelines"}
    if any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in output_methods
        and not any(node is approved for approved in approved_output_calls)
        for node in ast.walk(tree)
    ):
        return False
    mutating_environment_methods = {
        "__delitem__",
        "__setitem__",
        "clear",
        "pop",
        "popitem",
        "setdefault",
        "update",
    }
    for node in ast.walk(tree):
        if (
            (
                isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                and _is_os_environ(node.value)
            )
            or (
                isinstance(node, ast.Subscript)
                and _is_os_environ(node.value)
                and isinstance(node.ctx, (ast.Store, ast.Del))
            )
            or (_is_os_environ(node) and isinstance(node.ctx, (ast.Store, ast.Del)))
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _is_os_environ(node.func.value)
                and node.func.attr in mutating_environment_methods
            )
        ):
            return False
    critical_writes = {
        name: [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ]
        for name in ("source_sha", "first", "second")
    }
    return all(len(writes) == 1 for writes in critical_writes.values())


def audit_python_source(source: str, name: str) -> list[Finding]:
    """Audit the current closed Python import and callable capability surface."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [
            Finding(name, f"embedded Python syntax error: {error.msg}", error.lineno)
        ]
    findings: list[Finding] = []
    if not _wheels_path_provenance_is_exact(tree, name):
        findings.append(
            Finding(
                name,
                "backend guard path provenance differs from closed module policy",
            )
        )
    module_aliases: dict[str, str] = {}
    imported_aliases: dict[str, tuple[str, str]] = {}
    callable_aliases: dict[str, str] = {}
    defined_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    definition_key = name.split(":embedded-python-", 1)[0]
    allowed_definitions: set[str] = set()
    for suffix, definitions in _ALLOWED_DEFINITIONS_BY_SUFFIX.items():
        if definition_key.endswith(suffix):
            allowed_definitions.update(definitions)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name not in allowed_definitions:
                findings.append(
                    Finding(
                        name,
                        f"Python definition is outside closed allowlist: {node.name}",
                        node.lineno,
                    )
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
            node.value, ast.Name
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    callable_aliases[target.id] = node.value.id
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name not in _ALLOWED_IMPORTS:
                    findings.append(
                        Finding(
                            name,
                            f"Python import is outside closed allowlist: {imported.name}",
                            node.lineno,
                        )
                    )
                module_aliases[imported.asname or imported.name.split(".")[0]] = (
                    imported.name
                )
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            allowed = (
                module.startswith(".")
                or node.module in _ALLOWED_FROM_IMPORTS
                and all(
                    imported.name in _ALLOWED_FROM_IMPORTS[node.module]
                    for imported in node.names
                )
            )
            if not allowed:
                findings.append(
                    Finding(
                        name,
                        f"Python from-import is outside closed allowlist: {module}",
                        node.lineno,
                    )
                )
            for imported in node.names:
                imported_aliases[imported.asname or imported.name] = (
                    module,
                    imported.name,
                )
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call = node.func.id
                allowed = (
                    call in _ALLOWED_BUILTIN_CALLS
                    or call in defined_names
                    or call in imported_aliases
                )
                if not allowed:
                    capability = callable_aliases.get(call, call)
                    label = (
                        "dynamic import"
                        if capability == "__import__"
                        else "Python callable"
                    )
                    findings.append(
                        Finding(
                            name,
                            f"{label} is outside closed allowlist: {call} -> {capability}",
                            node.lineno,
                        )
                    )
                continue
            path = _attribute_path(node.func)
            if path is None:
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in _ALLOWED_METHOD_CALLS
                ):
                    continue
                method = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else "dynamic callee"
                )
                findings.append(
                    Finding(
                        name,
                        f"Python method is outside closed allowlist: {method}",
                        node.lineno,
                    )
                )
                continue
            root, method = path[0], path[-1]
            module = module_aliases.get(root)
            if module is not None:
                module_tail = tuple(module.split(".")[1:])
                api_parts = path[1:]
                if module_tail and api_parts[: len(module_tail)] == module_tail:
                    api_parts = api_parts[len(module_tail) :]
                api = ".".join(api_parts)
                if api not in _ALLOWED_MODULE_APIS.get(module, set()):
                    label = (
                        "dynamic import"
                        if module == "importlib" and api == "import_module"
                        else "Python module API"
                    )
                    findings.append(
                        Finding(
                            name,
                            f"{label} is outside closed allowlist: {module}.{api}",
                            node.lineno,
                        )
                    )
            elif method == "exec_module":
                if not _backend_guard_loader_is_exact(tree, node, name):
                    findings.append(
                        Finding(
                            name, "dynamic loader exec_module is forbidden", node.lineno
                        )
                    )
            elif method not in _ALLOWED_METHOD_CALLS:
                findings.append(
                    Finding(
                        name,
                        f"Python method is outside closed allowlist: {method}",
                        node.lineno,
                    )
                )
    return findings


def _permissions(value: object) -> bool:
    return value == {"contents": "read"}


def _audit_triggers(events: object, name: str) -> list[Finding]:
    if not isinstance(events, dict):
        return [Finding(name, "workflow trigger policy requires an object")]
    expected_events = _WORKFLOW_EVENT_POLICY.get(name)
    if expected_events is None or set(events) != expected_events:
        return [Finding(name, "workflow trigger policy differs")]
    if (
        name == "develop-release-dry-run.yml"
        and events.get("workflow_run") != _DEVELOP_WORKFLOW_RUN_POLICY
    ):
        return [Finding(name, "workflow_run trigger policy differs")]
    if name == "pr-release-dry-run.yml" and events != _PR_EVENT_POLICY:
        return [Finding(name, "pull request trigger policy differs")]
    return []


def _audit_action(step: dict[object, object], name: str) -> list[Finding]:
    uses = str(step.get("uses", ""))
    if uses not in _ACTIONS:
        return [Finding(name, f"action allowlist rejected: {uses}")]
    options = step.get("with", {})
    if not isinstance(options, dict):
        return [Finding(name, "Action with block must be an object")]
    if _ACTIONS[uses] == "checkout" and any(
        marker in str(options.get(field, ""))
        for marker in ("pull_request.head", "inputs.")
        for field in ("path", "ref")
    ):
        return [Finding(name, "head-controlled checkout path/ref is forbidden")]
    step_name = step.get("name")
    if not isinstance(step_name, str):
        return [Finding(name, "Action step name is required by closed policy")]
    expected_if = _ACTION_IF_POLICY.get((name, step_name))
    expected_keys = {"name", "uses", "with"} | ({"if"} if expected_if else set())
    if set(step) != expected_keys or step.get("if") != expected_if:
        return [Finding(name, f"Action step keys/context differ: {step_name}")]
    policy = _ACTION_STEP_POLICY.get((name, step_name))
    if policy is None:
        return [Finding(name, f"Action step is outside closed policy: {step_name}")]
    expected_kind, expected_options = policy
    if _ACTIONS.get(uses) != expected_kind:
        return [Finding(name, f"action allowlist rejected: {uses}")]
    if options != expected_options:
        label = "checkout path/ref" if expected_kind == "checkout" else expected_kind
        return [Finding(name, f"{label} arguments differ from closed step policy")]
    return []


def _extract_canonical_heredocs(
    run: str, name: str
) -> tuple[str, list[str], list[Finding]]:
    lines = run.splitlines()
    shell: list[str] = []
    blocks: list[str] = []
    findings: list[Finding] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if "<<" not in stripped:
            shell.append(lines[index])
            index += 1
            continue
        if stripped != "python - <<'PY'":
            findings.append(
                Finding(name, f"heredoc launcher is outside closed policy: {stripped}")
            )
            shell.append(lines[index])
            index += 1
            continue
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip() != "PY":
            body.append(lines[index])
            index += 1
        if index == len(lines):
            findings.append(Finding(name, "heredoc is missing its exact PY terminator"))
            break
        blocks.append("\n".join(body) + "\n")
        index += 1
    return "\n".join(shell), blocks, findings


def _curl_argv(
    endpoint: str, output: str, *, no_redirect: bool = False
) -> tuple[str, ...]:
    return (
        "curl",
        "--request",
        "GET",
        "--fail",
        "--silent",
        "--show-error",
        "--retry",
        "3",
        *(("--max-redirs", "0") if no_redirect else ()),
        "--header",
        "Accept: application/vnd.github+json",
        "--header",
        "Authorization: Bearer ${GH_TOKEN}",
        "--header",
        "X-GitHub-Api-Version: 2022-11-28",
        endpoint,
        "--output",
        output,
    )


_ALLOWED_CURL_ARGV = {
    (
        "pr-release-dry-run.yml",
        _curl_argv(_PR_ENDPOINT, "$RUNNER_TEMP/observed-pr.json"),
    ),
    ("pr-release-dry-run.yml", _curl_argv(_PR_ENDPOINT, "$OUTPUT_DIR/current-pr.json")),
    (
        "nightly-release-dry-run.yml",
        _curl_argv(_NIGHTLY_ENDPOINT, "preview/develop-ref-first.json"),
    ),
    (
        "nightly-release-dry-run.yml",
        _curl_argv(_NIGHTLY_ENDPOINT, "preview/develop-ref-second.json"),
    ),
    *{
        (
            workflow,
            _curl_argv(
                _MAIN_ENDPOINT,
                f"$RUNNER_TEMP/main-ref-{observation}.json",
                no_redirect=True,
            ),
        )
        for workflow in (
            "develop-release-dry-run.yml",
            "release-control-dry-run.yml",
        )
        for observation in ("first", "second")
    },
}
_CLI_OPERATIONS = {
    ("artifacts", "collect"),
    ("artifacts", "validate"),
    ("cleanup", "plan"),
    ("command", "parse"),
    ("config", "validate"),
    ("environment", "export"),
    ("environment", "simulate"),
    ("environment", "verify"),
    ("lifecycle", "plan"),
    ("lifecycle", "validate"),
    ("reconcile", "plan"),
    ("release", "render"),
    ("repo-policy", "audit"),
}


def _audit_python_command(argv: list[str], name: str) -> list[Finding]:
    findings: list[Finding] = []
    if len(argv) < 5 or argv[1:3] != ["python", "-m"]:
        return [Finding(name, "Python executable argv is outside closed policy")]
    module = argv[3]
    arguments = argv[4:]
    if module == "ucm_release_v2.github_readonly":
        required = {"--first", "--second", "--branch", "--github-output"}
        if set(arguments[::2]) != required or len(arguments) != 8:
            findings.append(
                Finding(name, "GitHub readback argv is outside closed policy")
            )
        return findings
    if module != "ucm_release_v2" or len(arguments) < 2:
        return [Finding(name, "Python module is outside closed policy")]
    if tuple(arguments[:2]) not in _CLI_OPERATIONS:
        findings.append(Finding(name, "UCM CLI operation is outside closed policy"))
    if any(
        token in {"|", "||", "&&", ";"}
        or token.startswith(("http://", "https://"))
        or token in {"-c", "-m"}
        for token in arguments[2:]
    ):
        findings.append(Finding(name, "UCM CLI argv is outside closed policy"))
    if ">" in arguments:
        redirect = arguments.index(">")
        if redirect != len(arguments) - 2 or not re.fullmatch(
            r"(?:preview|\$OUTPUT_DIR)/[A-Za-z0-9][A-Za-z0-9_.-]*\.json",
            arguments[-1],
        ):
            findings.append(Finding(name, "UCM CLI output redirect is unsafe"))
    return findings


def _audit_shell(run: str, name: str) -> list[Finding]:
    collapsed = re.sub(r"\\\n[ \t]*", " ", run)
    findings: list[Finding] = []
    for raw_line in collapsed.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"eval(?:\s|$)", line):
            findings.append(
                Finding(name, f"shell eval/executable is outside closed policy: {line}")
            )
            continue
        if line in {"set -euo pipefail", "mkdir -p preview", 'mkdir -p "$OUTPUT_DIR"'}:
            continue
        if line in {
            "reconcile_args=(",
            "render_args=(",
            ")",
            "if [[ -f preview/promotion-evidence.json ]]; then",
            "if [[ -f preview/promotion-source-lifecycle-plan.json ]]; then",
            "reconcile_args+=(--promotion preview/promotion-evidence.json)",
            "reconcile_args+=(--promotion-source-lifecycle-plan preview/promotion-source-lifecycle-plan.json)",
            "reconcile_args+=(--promotion-source-manifest preview/promotion-source-manifest.json)",
            "render_args+=(--promotion preview/promotion-evidence.json)",
            "render_args+=(--promotion-source-lifecycle-plan preview/promotion-source-lifecycle-plan.json)",
            "render_args+=(--promotion-source-manifest preview/promotion-source-manifest.json)",
            "fi",
        } or re.fullmatch(
            r"--(?:lifecycle-plan|manifest|inventory|output|config|reconcile-plan|known-issues-json) [A-Za-z0-9_./-]+",
            line,
        ):
            continue
        if any(marker in line for marker in ("$(", "`", "<(", ">(")):
            findings.append(
                Finding(name, "shell command/process substitution is forbidden")
            )
            continue
        braced_expansions = re.findall(r"\$\{([^}]*)\}", line)
        simple_expansions = re.findall(r"\$(?![({])[A-Za-z_][A-Za-z0-9_]*", line)
        if any(
            expansion not in _ALLOWED_BRACED_SHELL_EXPANSIONS
            for expansion in braced_expansions
        ) or any(
            expansion[1:] not in _ALLOWED_SIMPLE_SHELL_EXPANSIONS
            for expansion in simple_expansions
        ):
            findings.append(Finding(name, "shell expansion is outside closed grammar"))
            continue
        redirections = re.findall(r"(?:<<<|[0-9]*>>?|[0-9]*<|[0-9]*>&[0-9]+)", line)
        safe_stdout = re.search(
            r" > (?:preview|\"?\$OUTPUT_DIR)/[A-Za-z0-9][A-Za-z0-9_.-]*\.json\"?$",
            line,
        )
        if redirections and not (redirections == [">"] and safe_stdout is not None):
            findings.append(
                Finding(name, "shell redirection is outside closed grammar")
            )
            continue
        if "|" in line or ";" in line or re.search(r"(?:^|\s)&(?:\s|$)", line):
            findings.append(
                Finding(name, "shell pipeline/metacharacter/operator is forbidden")
            )
            continue
        try:
            argv = shlex.split(line, posix=True)
        except ValueError as error:
            findings.append(Finding(name, f"shell argv is malformed: {error}"))
            continue
        if not argv:
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]) and not argv[
            0
        ].startswith("PYTHONPATH="):
            findings.append(
                Finding(name, "variable command executable capability is forbidden")
            )
            continue
        executable = argv[0]
        if executable == "curl":
            if (name, tuple(argv)) not in _ALLOWED_CURL_ARGV:
                findings.append(
                    Finding(
                        name, "curl network endpoint/argv is outside exact GET policy"
                    )
                )
            continue
        if executable.startswith("PYTHONPATH="):
            if executable not in {
                "PYTHONPATH=.github/release/v2",
                "PYTHONPATH=control/.github/release/v2",
            }:
                findings.append(Finding(name, "PYTHONPATH is outside closed policy"))
                continue
            if (
                executable == "PYTHONPATH=.github/release/v2"
                and name != "nightly-release-dry-run.yml"
            ):
                findings.append(
                    Finding(name, "workflow control PYTHONPATH is outside trusted tree")
                )
                continue
            findings.extend(_audit_python_command(argv, name))
            continue
        if executable == "rm":
            message = f"destructive shell executable is outside closed policy: {line}"
        elif executable == "eval":
            message = f"shell eval/executable is outside closed policy: {line}"
        else:
            message = f"shell executable is outside closed policy: {line}"
        findings.append(Finding(name, message))
    return findings


def audit_workflow_source(source: str, name: str) -> list[Finding]:
    """Audit one workflow, its shell commands, and all embedded Python blocks."""
    try:
        workflow = yaml.safe_load(source)
    except yaml.YAMLError as error:
        return [Finding(name, f"workflow YAML is invalid: {error}")]
    if not isinstance(workflow, dict):
        return [Finding(name, "workflow must be an object")]
    findings: list[Finding] = []
    events = workflow.get("on", workflow.get(True, {}))
    expected_root_keys = {"name", True, "permissions", "jobs"}
    if name != "release-control-dry-run.yml":
        expected_root_keys.add("concurrency")
    if set(workflow) != expected_root_keys:
        findings.append(Finding(name, "workflow keys differ from closed policy"))
    findings.extend(_audit_triggers(events, name))
    if isinstance(events, dict) and "pull_request_target" in events:
        findings.append(Finding(name, "pull_request_target is forbidden"))
    concurrency = workflow.get("concurrency")
    if name != "release-control-dry-run.yml" and (
        not isinstance(concurrency, dict)
        or concurrency.get("cancel-in-progress") is not False
    ):
        findings.append(
            Finding(name, "workflow concurrency must not cancel in progress")
        )
    if not _permissions(workflow.get("permissions")):
        findings.append(Finding(name, "root permissions must be contents: read"))
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return findings + [Finding(name, "jobs must be an object")]
    if name in _REUSABLE_JOB_POLICY:
        if set(jobs) != {"invoke-trusted-main-controller"}:
            findings.append(Finding(name, "data-only reusable job set differs"))
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            findings.append(Finding(name, f"job {job_name} must be an object"))
            continue
        reusable_policy = _REUSABLE_JOB_POLICY.get(name)
        if reusable_policy is not None:
            if job_name != "invoke-trusted-main-controller" or job != reusable_policy:
                findings.append(
                    Finding(name, "data-only reusable job differs from closed policy")
                )
            continue
        job_context = {
            key: job[key] for key in ("if", "runs-on", "needs", "outputs") if key in job
        }
        expected_job_keys = {"permissions", "steps"} | set(job_context)
        if set(job) != expected_job_keys:
            findings.append(
                Finding(name, f"job keys differ from closed policy: {job_name}")
            )
        if job.get("runs-on") != "ubuntu-24.04":
            findings.append(Finding(name, f"job runs-on is not fixed: {job_name}"))
        if not _permissions(job.get("permissions")):
            findings.append(
                Finding(name, f"job {job_name} permissions are not read-only")
            )
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            findings.append(Finding(name, f"job {job_name} steps must be an array"))
            continue
        seen_step_names: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                findings.append(Finding(name, "workflow step must be an object"))
                continue
            candidate_step_name = step.get("name")
            if "uses" in step:
                predecessor = (
                    _TRUSTED_CHECKOUT_PREDECESSORS.get((name, candidate_step_name))
                    if isinstance(candidate_step_name, str)
                    else None
                )
                if predecessor is not None and predecessor not in seen_step_names:
                    findings.append(
                        Finding(name, "trusted checkout requires prior validation")
                    )
                findings.extend(_audit_action(step, name))
                if isinstance(candidate_step_name, str):
                    seen_step_names.add(candidate_step_name)
                continue
            if "run" not in step:
                findings.append(
                    Finding(name, "workflow step has no reviewed action or run")
                )
                continue
            step_name = step.get("name")
            if not isinstance(step_name, str):
                findings.append(Finding(name, "run step name is required"))
                step_name = "<missing>"
            allowed_run_keys = {"name", "id", "if", "env", "run"}
            if not set(step) <= allowed_run_keys:
                findings.append(
                    Finding(
                        name, f"run step keys differ from closed policy: {step_name}"
                    )
                )
            environment = step.get("env", {})
            if not isinstance(environment, dict):
                findings.append(Finding(name, "step env must be an object"))
            else:
                for value in environment.values():
                    if (
                        isinstance(value, str)
                        and "${{" in value
                        and value not in _ALLOWED_ENV_EXPRESSIONS
                    ):
                        findings.append(
                            Finding(
                                name,
                                f"env expression is outside closed policy: {value}",
                            )
                        )
            run = str(step.get("run", ""))
            seen_step_names.add(step_name)
            if not run:
                continue
            shell_run, embedded_python, heredoc_findings = _extract_canonical_heredocs(
                run, name
            )
            findings.extend(heredoc_findings)
            if (
                name == "develop-release-dry-run.yml"
                and step_name == "Validate trusted controller and develop source event"
                and (
                    len(embedded_python) != 1
                    or not _develop_inline_validator_is_semantic(embedded_python[0])
                )
            ):
                findings.append(
                    Finding(name, "develop inline validator contract differs")
                )
            if "${{" in shell_run:
                findings.append(
                    Finding(name, "GitHub expression bypasses env boundary")
                )
            findings.extend(_audit_shell(shell_run, name))
            for index, python_source in enumerate(embedded_python, start=1):
                findings.extend(
                    audit_python_source(
                        python_source, f"{name}:embedded-python-{index}"
                    )
                )
    return findings


def audit_repository(v2_root: Path, workflows: Iterable[Path]) -> list[Finding]:
    """Audit every shipped v2 Python module and each declared dry-run workflow."""
    findings: list[Finding] = []
    for path in python_audit_paths(v2_root):
        findings.extend(
            audit_python_source(path.read_text(encoding="utf-8"), str(path))
        )
    for path in sorted(workflows):
        findings.extend(
            audit_workflow_source(path.read_text(encoding="utf-8"), path.name)
        )
    return findings
