"""Reusable static read-only audit for the v2 control plane and workflows."""

from __future__ import annotations

import ast
import hashlib
import json
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
        "_backend_guard_loader_is_exact",
        "_curl_argv",
        "_extract_canonical_heredocs",
        "_normalized_context_sha256",
        "_permissions",
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
_WORKFLOW_ROOT_CONTEXT_SHA256 = {
    "develop-release-dry-run.yml": "495ee94bafd436906a5fa98352cdcf72aa74e727aad280234836b7ccc073ead5",
    "draft-environment-dry-run.yml": "71b9432d78f32014aeb7eb9dff3b00563749be6c839a320ca04e24e80437fe11",
    "nightly-release-dry-run.yml": "cc224e399395b6c3a291c9a14c5a5af6add36ce16ecb56ae909becc91bea13b6",
    "pr-release-dry-run.yml": "9e3334fd84ca5f792ce7a30226900e5b6793a3e76dbe2a664e84631d4b3b8bdb",
    "release-cleanup-dry-run.yml": "dceb7fab44f3b91cd164281dfa58b3ed2ad0a30e8c62da0078f6c9f1996aaeb9",
    "release-control-dry-run.yml": "05a6548780ee605cc1810a236185c9f5703d9cc8e933764b92a2ce48ffc31a39",
    "release-lifecycle-dry-run.yml": "927516474ab2a6d31a46a85eb6b8e220b2f0a1a90a374b3aa2fe16f95f1bbf26",
    "repository-policy-audit-dry-run.yml": "a3c417a6a1b416166245bf831b213fdefa15096d15a3ea016422f9e6f9b757ff",
}
_WORKFLOW_JOB_CONTEXT_SHA256 = {
    (
        "release-control-dry-run.yml",
        "control",
    ): "e70acb2037dfa0762d05a72a3a09e0ab38cfaa862652aaa271018d88f7016c5d",
    (
        "release-control-dry-run.yml",
        "simulated-environment",
    ): "d635781efa1a87d9c73850d52c1e6d5f0b121ad222f475b3eeed224ad4ba9112",
    (
        "release-control-dry-run.yml",
        "release-preview",
    ): "0bb6e32c8e1a48444e120a00c99d57866f4b024c5d5857312a1c0f441638ebcf",
    (
        "release-control-dry-run.yml",
        "cleanup-preview",
    ): "c5c23def7fa2fa1bdd7fc7cd7a615f452755554032b02deb320bc8f54998dca9",
    (
        "release-control-dry-run.yml",
        "policy-audit",
    ): "3a365ccc4265aefe353814cd7ccdecfcbf0637307f2be29632106f7fa8468553",
    (
        "develop-release-dry-run.yml",
        "develop-preview",
    ): "c66916d6c28770a3df90edf0cb4c05edfcc6aa1ee805fb76a0ce4ce4d478499b",
    (
        "draft-environment-dry-run.yml",
        "simulated-environment",
    ): "c66916d6c28770a3df90edf0cb4c05edfcc6aa1ee805fb76a0ce4ce4d478499b",
    (
        "nightly-release-dry-run.yml",
        "nightly-preview",
    ): "c66916d6c28770a3df90edf0cb4c05edfcc6aa1ee805fb76a0ce4ce4d478499b",
    (
        "pr-release-dry-run.yml",
        "pull-request-preview",
    ): "21b33fe86324ba77d3b3e7b1484027f69cff775bd803b2975b47e21199beab6c",
    (
        "pr-release-dry-run.yml",
        "issue-comment-preview",
    ): "69485ec91f90f88e00009ee08fdd304da1278e12ebf34e76f11ba5147baafe32",
    (
        "release-cleanup-dry-run.yml",
        "cleanup-preview",
    ): "c66916d6c28770a3df90edf0cb4c05edfcc6aa1ee805fb76a0ce4ce4d478499b",
    (
        "release-lifecycle-dry-run.yml",
        "release-preview",
    ): "c66916d6c28770a3df90edf0cb4c05edfcc6aa1ee805fb76a0ce4ce4d478499b",
    (
        "repository-policy-audit-dry-run.yml",
        "policy-audit",
    ): "c66916d6c28770a3df90edf0cb4c05edfcc6aa1ee805fb76a0ce4ce4d478499b",
}
_WORKFLOW_JOB_STEP_SEQUENCE_SHA256 = {
    (
        "develop-release-dry-run.yml",
        "develop-preview",
    ): "f8d4e4ace99c317d758baebc066331e99bf76ce6afd401423b2de5ade1c83040",
    (
        "nightly-release-dry-run.yml",
        "nightly-preview",
    ): "47c349bd0c0c42724ed2006c0e7a72fe74b7f81615cbc540085c335b1291d14a",
    (
        "pr-release-dry-run.yml",
        "pull-request-preview",
    ): "ad1cef939a705e74281ab5e9a773e3e94941e870e1e91bd40bff3cb0b03157a0",
    (
        "pr-release-dry-run.yml",
        "issue-comment-preview",
    ): "13efc165abd8ca4ffbd2a15f02a84d6cd838b93ec857acab289c7a532366163b",
    (
        "release-control-dry-run.yml",
        "control",
    ): "abeb6a0eb4121bfbb8f2c4730ca3f7ad9cb73ed6a46fbc9e2a9b0067e1b902ab",
    (
        "release-control-dry-run.yml",
        "simulated-environment",
    ): "90a11f2f5a4d6f0e12a625dddb33e3d6b6c12189d988069e537d25624a4d7755",
    (
        "release-control-dry-run.yml",
        "release-preview",
    ): "f6464c9d5abb931f4787a53a6540e1ea992dd0c21017939173cf69f2d17ef900",
    (
        "release-control-dry-run.yml",
        "cleanup-preview",
    ): "44e5aaf28be095ad3bcc9e1945d494e5f3a838ffff31c7311c8fcc6d4c892d14",
    (
        "release-control-dry-run.yml",
        "policy-audit",
    ): "9838c8ea90e827704467b5d1240b694085541923b14713c8d50ac825a0cc2c4e",
}
_RUN_STEP_CONTEXT_SHA256 = {
    (
        "release-control-dry-run.yml",
        "Fetch first trusted main control ref",
    ): "d0cf1f7bd5f505c39bf8d93ec2797786d0e92bea7c4734d2abb3a03018bbe8c6",
    (
        "release-control-dry-run.yml",
        "Fetch second trusted main control ref",
    ): "1a852c0c35d6e09a16ea90857f8eceadacf94bed5610f7dd7ec941589ae4adae",
    (
        "release-control-dry-run.yml",
        "Validate exact trusted reusable controller identity",
    ): "9fe1ac0b3d405e044ebe653f6788539417f4ace31ba23f4006f2ea56c129a7ac",
    (
        "release-control-dry-run.yml",
        "Validate immutable manual identity",
    ): "83fa715973c9d49c4a49639f6a93315769f7416ba05b8fead9dc8716feb32233",
    (
        "release-control-dry-run.yml",
        "Generate Draft plan",
    ): "7bc6685cab78fd02ac08b0812243838fc9544be7dac12ae02c0238e579447a8a",
    (
        "release-control-dry-run.yml",
        "Create deterministic offline artifact fixtures",
    ): "996aa9349456ec9408e1d50d1e3cc5e02d673c660211a850e317df30c40e01a1",
    (
        "release-control-dry-run.yml",
        "Export simulate and verify environment evidence",
    ): "81323a54d2f1b75225d68ef27950784b54e0ddbef14326221c95a1844860ce81",
    (
        "release-control-dry-run.yml",
        "Write simulated evidence summary",
    ): "206b5d22b3e4085dc2df9b3d0a2e5dad8d55b738a55b099b8e27b490e0f50436",
    (
        "release-control-dry-run.yml",
        "Normalize untrusted JSON inputs without shell interpolation",
    ): "1f2dc6c608dee184b13f0d75e62e6dda82c6fe326a0bb5ae5e6e2b4671168185",
    (
        "release-control-dry-run.yml",
        "Generate and reopen protected lifecycle plan",
    ): "a78a76135d2d4506ef4ae5c58558604039e58917b4a3d363c13361343ae85ddf",
    (
        "release-control-dry-run.yml",
        "Reconcile the offline inventory without execution",
    ): "454d3b86744129f21fbff0d96a74de9c00526ee2fdbb483ed6a5465b82963c30",
    (
        "release-control-dry-run.yml",
        "Render release preview Markdown",
    ): "c30f2b15475961fc1984f55abcabbff2b8b4eda0e5be58a8dc880d87d2df991f",
    (
        "release-control-dry-run.yml",
        "Write release preview summary",
    ): "3d12a65ac2ebaa606354572c015562866cd03d72c211e96c711730eb8ecfc32d",
    (
        "release-control-dry-run.yml",
        "Normalize strict offline inventory",
    ): "bb47a4e651a2b7f239bb2949b3cebea1f4b2840d7304c85b55823c81390965d2",
    (
        "release-control-dry-run.yml",
        "Generate non-executing cleanup plan",
    ): "ebf21b9cc52ba9ec6eda1896bbddc1f27a6812e98fed7f4b0363d07420582f0b",
    (
        "release-control-dry-run.yml",
        "Write cleanup preview summary",
    ): "ae8e0d90f302b3e17af246b3f3515064fd21fff9fede7f020e4f356291abf66c",
    (
        "release-control-dry-run.yml",
        "Normalize strict offline policy snapshot",
    ): "92f6b081b778fc3a4c0f53f60921bbd4bb92f90e6cd11883e0928a734c95a3f3",
    (
        "release-control-dry-run.yml",
        "Audit repository policy snapshot offline",
    ): "196deac76c1be75ae761ca3977dc88f65de559d3dc7f3faa323b8f66fea3439a",
    (
        "release-control-dry-run.yml",
        "Write repository policy audit summary",
    ): "56e268351da6dbd5de6d2504ffd4d9f92f6228dc829c6875392575f109b3ade4",
    (
        "develop-release-dry-run.yml",
        "Fetch first trusted main control ref",
    ): "d0cf1f7bd5f505c39bf8d93ec2797786d0e92bea7c4734d2abb3a03018bbe8c6",
    (
        "develop-release-dry-run.yml",
        "Fetch second trusted main control ref",
    ): "1a852c0c35d6e09a16ea90857f8eceadacf94bed5610f7dd7ec941589ae4adae",
    (
        "develop-release-dry-run.yml",
        "Validate trusted controller and develop source event",
    ): "b148be08e76166bf6384af604134ea129f26a36ac649d042cdc3ac75c204f038",
    (
        "develop-release-dry-run.yml",
        "Generate and validate develop lifecycle preview",
    ): "2d103b914de6cbd11ecf061bfd052ca0832c78403b88fedee135e0a95c8f1cd7",
    (
        "develop-release-dry-run.yml",
        "Write develop preview summary",
    ): "4e4e64a29647b415c101087fbea349261f4f1010f5f87be28c112b724d3f1632",
    (
        "draft-environment-dry-run.yml",
        "Fetch first trusted main control ref",
    ): "d0cf1f7bd5f505c39bf8d93ec2797786d0e92bea7c4734d2abb3a03018bbe8c6",
    (
        "draft-environment-dry-run.yml",
        "Fetch second trusted main control ref",
    ): "1a852c0c35d6e09a16ea90857f8eceadacf94bed5610f7dd7ec941589ae4adae",
    (
        "draft-environment-dry-run.yml",
        "Validate exact default-main control identity",
    ): "e3db1eff76224527da74a995a71a587d6fe2a811435051237cdc2282b3653130",
    (
        "draft-environment-dry-run.yml",
        "Validate immutable manual identity",
    ): "83fa715973c9d49c4a49639f6a93315769f7416ba05b8fead9dc8716feb32233",
    (
        "draft-environment-dry-run.yml",
        "Generate Draft plan",
    ): "7bc6685cab78fd02ac08b0812243838fc9544be7dac12ae02c0238e579447a8a",
    (
        "draft-environment-dry-run.yml",
        "Create deterministic offline artifact fixtures",
    ): "996aa9349456ec9408e1d50d1e3cc5e02d673c660211a850e317df30c40e01a1",
    (
        "draft-environment-dry-run.yml",
        "Export simulate and verify environment evidence",
    ): "81323a54d2f1b75225d68ef27950784b54e0ddbef14326221c95a1844860ce81",
    (
        "draft-environment-dry-run.yml",
        "Write simulated evidence summary",
    ): "206b5d22b3e4085dc2df9b3d0a2e5dad8d55b738a55b099b8e27b490e0f50436",
    (
        "nightly-release-dry-run.yml",
        "Validate trusted control and derive configured identities",
    ): "a0557cca7e4276e8c78948cb29a234be4b3f02fe188bdd36f9dee041fede7353",
    (
        "nightly-release-dry-run.yml",
        "Fetch first develop ref observation with read-only GET",
    ): "5e78bbcd847791bc95499f287805996951003e076b1475c1ba1235472e111417",
    (
        "nightly-release-dry-run.yml",
        "Fetch second develop ref observation with read-only GET",
    ): "e62bfe4f08696ba78875212537932636929880cbbe7682445dbe22736c0c3b3c",
    (
        "nightly-release-dry-run.yml",
        "Validate stable develop source observations",
    ): "4c4676f1403a7ec2c3226e14b79b185cb2dd8e7f001634984d57a6e36d625746",
    (
        "nightly-release-dry-run.yml",
        "Generate and validate Nightly lifecycle preview",
    ): "0191cce0a85cce543dea0dbc490ab430781074a33f04a51908e063b819297d9e",
    (
        "nightly-release-dry-run.yml",
        "Write Nightly preview summary",
    ): "f0de344ab99a148489b6463b2956915e52f6a5c8bf5e697fecebbf556edcbac7",
    (
        "pr-release-dry-run.yml",
        "Generate and validate PR lifecycle preview",
    ): "706613baeee3a9ccc239ca757c6beca349c86f4b6683d9715d05a0b4be4a3422",
    (
        "pr-release-dry-run.yml",
        "Write PR preview summary",
    ): "1ef329e06cba63a87082bf4f817dfc04adff405489500ce19dd7a6452b933880",
    (
        "pr-release-dry-run.yml",
        "Fetch observed PR identity with read-only GET",
    ): "d896cf68f7bf62776641c1e3da4de8b91967d0825247e8dad088cb0bf6a06da5",
    (
        "pr-release-dry-run.yml",
        "Fetch current PR identity with read-only GET",
    ): "0e0aa6454d5f230824b437cfba144cfeab31627b9f04e885e3fbbc99a3319517",
    (
        "pr-release-dry-run.yml",
        "Validate and separate observed, current, base, and control identity",
    ): "5e466dd9059c9da798e81f55b7d6db08dcf196929be432e5b9670d913ee82046",
    (
        "pr-release-dry-run.yml",
        "Parse command without shell interpolation",
    ): "34b44e2011e63c71e056e69526baed54ce6c1ff5c6b1e1c6cbd9e4dd2b9ef24a",
    (
        "pr-release-dry-run.yml",
        "Generate and validate authorized build preview",
    ): "e0b79bc120dca619fd635a0da7af72101cb2094f529b38c75337fb383d314dd5",
    (
        "pr-release-dry-run.yml",
        "Write comment command summary",
    ): "7226ac945a87f10346ba54ee3a9f44c04bf9e8bfffd18f66108efa0dcd5b984c",
    (
        "release-cleanup-dry-run.yml",
        "Fetch first trusted main control ref",
    ): "d0cf1f7bd5f505c39bf8d93ec2797786d0e92bea7c4734d2abb3a03018bbe8c6",
    (
        "release-cleanup-dry-run.yml",
        "Fetch second trusted main control ref",
    ): "1a852c0c35d6e09a16ea90857f8eceadacf94bed5610f7dd7ec941589ae4adae",
    (
        "release-cleanup-dry-run.yml",
        "Validate exact default-main control identity",
    ): "e3db1eff76224527da74a995a71a587d6fe2a811435051237cdc2282b3653130",
    (
        "release-cleanup-dry-run.yml",
        "Normalize strict offline inventory",
    ): "bb47a4e651a2b7f239bb2949b3cebea1f4b2840d7304c85b55823c81390965d2",
    (
        "release-cleanup-dry-run.yml",
        "Generate non-executing cleanup plan",
    ): "ebf21b9cc52ba9ec6eda1896bbddc1f27a6812e98fed7f4b0363d07420582f0b",
    (
        "release-cleanup-dry-run.yml",
        "Write cleanup preview summary",
    ): "ae8e0d90f302b3e17af246b3f3515064fd21fff9fede7f020e4f356291abf66c",
    (
        "release-lifecycle-dry-run.yml",
        "Fetch first trusted main control ref",
    ): "d0cf1f7bd5f505c39bf8d93ec2797786d0e92bea7c4734d2abb3a03018bbe8c6",
    (
        "release-lifecycle-dry-run.yml",
        "Fetch second trusted main control ref",
    ): "1a852c0c35d6e09a16ea90857f8eceadacf94bed5610f7dd7ec941589ae4adae",
    (
        "release-lifecycle-dry-run.yml",
        "Validate exact default-main control identity",
    ): "e3db1eff76224527da74a995a71a587d6fe2a811435051237cdc2282b3653130",
    (
        "release-lifecycle-dry-run.yml",
        "Normalize untrusted JSON inputs without shell interpolation",
    ): "1f2dc6c608dee184b13f0d75e62e6dda82c6fe326a0bb5ae5e6e2b4671168185",
    (
        "release-lifecycle-dry-run.yml",
        "Generate and reopen protected lifecycle plan",
    ): "a78a76135d2d4506ef4ae5c58558604039e58917b4a3d363c13361343ae85ddf",
    (
        "release-lifecycle-dry-run.yml",
        "Create deterministic offline artifact fixtures",
    ): "996aa9349456ec9408e1d50d1e3cc5e02d673c660211a850e317df30c40e01a1",
    (
        "release-lifecycle-dry-run.yml",
        "Reconcile the offline inventory without execution",
    ): "454d3b86744129f21fbff0d96a74de9c00526ee2fdbb483ed6a5465b82963c30",
    (
        "release-lifecycle-dry-run.yml",
        "Render release preview Markdown",
    ): "c30f2b15475961fc1984f55abcabbff2b8b4eda0e5be58a8dc880d87d2df991f",
    (
        "release-lifecycle-dry-run.yml",
        "Write release preview summary",
    ): "3d12a65ac2ebaa606354572c015562866cd03d72c211e96c711730eb8ecfc32d",
    (
        "repository-policy-audit-dry-run.yml",
        "Fetch first trusted main control ref",
    ): "d0cf1f7bd5f505c39bf8d93ec2797786d0e92bea7c4734d2abb3a03018bbe8c6",
    (
        "repository-policy-audit-dry-run.yml",
        "Fetch second trusted main control ref",
    ): "1a852c0c35d6e09a16ea90857f8eceadacf94bed5610f7dd7ec941589ae4adae",
    (
        "repository-policy-audit-dry-run.yml",
        "Validate exact default-main control identity",
    ): "e3db1eff76224527da74a995a71a587d6fe2a811435051237cdc2282b3653130",
    (
        "repository-policy-audit-dry-run.yml",
        "Normalize strict offline policy snapshot",
    ): "92f6b081b778fc3a4c0f53f60921bbd4bb92f90e6cd11883e0928a734c95a3f3",
    (
        "repository-policy-audit-dry-run.yml",
        "Audit repository policy snapshot offline",
    ): "196deac76c1be75ae761ca3977dc88f65de559d3dc7f3faa323b8f66fea3439a",
    (
        "repository-policy-audit-dry-run.yml",
        "Write repository policy audit summary",
    ): "56e268351da6dbd5de6d2504ffd4d9f92f6228dc829c6875392575f109b3ade4",
}
_TRUST_CRITICAL_RUN_BODY_SHA256 = {
    (
        "release-control-dry-run.yml",
        "control",
        2,
        "Validate exact trusted reusable controller identity",
    ): "2bc4d92740c905240d635081aa8ec5c66a4098c638138e9dc3f90e7dff855cef",
    (
        "develop-release-dry-run.yml",
        "develop-preview",
        2,
        "Validate trusted controller and develop source event",
    ): "43b344e7453bf456be10fc0ab7fcd85b968751a97aea050502965c9c3489585d",
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


def _normalized_context_sha256(value: object) -> str:
    content = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


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
    root_context = {
        "name": workflow.get("name"),
        "on": events,
        "concurrency": workflow.get("concurrency"),
    }
    expected_root_context = _WORKFLOW_ROOT_CONTEXT_SHA256.get(name)
    if (
        expected_root_context is None
        or _normalized_context_sha256(root_context) != expected_root_context
    ):
        findings.append(
            Finding(
                name, "workflow trigger/concurrency context differs from closed policy"
            )
        )
    if isinstance(events, dict) and "pull_request_target" in events:
        findings.append(Finding(name, "pull_request_target is forbidden"))
    if not _permissions(workflow.get("permissions")):
        findings.append(Finding(name, "root permissions must be contents: read"))
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return findings + [Finding(name, "jobs must be an object")]
    expected_job_names = {
        job_name
        for workflow_name, job_name in _WORKFLOW_JOB_CONTEXT_SHA256
        if workflow_name == name
    }
    if name in _REUSABLE_JOB_POLICY:
        expected_job_names = {"invoke-trusted-main-controller"}
    if set(jobs) != expected_job_names:
        findings.append(Finding(name, "workflow job names differ from closed policy"))
    expected_step_job_names = {
        job_name
        for workflow_name, job_name in _WORKFLOW_JOB_STEP_SEQUENCE_SHA256
        if workflow_name == name
    }
    observed_step_job_names = {
        job_name
        for job_name, job in jobs.items()
        if isinstance(job, dict) and "steps" in job
    }
    if observed_step_job_names != expected_step_job_names:
        findings.append(Finding(name, "executable job step-sequence coverage differs"))
    trust_critical_step_names = {
        step_name
        for workflow_name, _, _, step_name in _TRUST_CRITICAL_RUN_BODY_SHA256
        if workflow_name == name
    }
    trust_critical_step_counts = {
        step_name: 0
        for workflow_name, _, _, step_name in _TRUST_CRITICAL_RUN_BODY_SHA256
        if workflow_name == name
    }
    observed_trust_critical: list[tuple[str, int, str, str]] = []
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
        expected_job_context = _WORKFLOW_JOB_CONTEXT_SHA256.get((name, job_name))
        job_context = {
            key: job[key] for key in ("if", "runs-on", "needs", "outputs") if key in job
        }
        expected_job_keys = {"permissions", "steps"} | set(job_context)
        if set(job) != expected_job_keys:
            findings.append(
                Finding(name, f"job keys differ from closed policy: {job_name}")
            )
        if (
            expected_job_context is None
            or _normalized_context_sha256(job_context) != expected_job_context
        ):
            findings.append(
                Finding(name, f"job runs-on/if context differs: {job_name}")
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
        ordered_step_identities: list[dict[str, object]] = []
        for step in steps:
            if not isinstance(step, dict):
                ordered_step_identities.append({"name": None, "type": "invalid"})
                continue
            has_action = "uses" in step
            has_run = "run" in step
            if has_action and has_run:
                step_type = "action+run"
            elif has_action:
                step_type = "action"
            elif has_run:
                step_type = "run"
            else:
                step_type = "other"
            ordered_step_identities.append(
                {"name": step.get("name"), "type": step_type}
            )
        expected_step_sequence = _WORKFLOW_JOB_STEP_SEQUENCE_SHA256.get(
            (name, job_name)
        )
        if (
            expected_step_sequence is None
            or _normalized_context_sha256(ordered_step_identities)
            != expected_step_sequence
        ):
            findings.append(Finding(name, f"ordered step sequence differs: {job_name}"))
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                findings.append(Finding(name, "workflow step must be an object"))
                continue
            candidate_step_name = step.get("name")
            if (
                isinstance(candidate_step_name, str)
                and candidate_step_name in trust_critical_step_names
            ):
                trust_critical_step_counts[candidate_step_name] += 1
                observed_trust_critical.append(
                    (
                        job_name,
                        step_index,
                        candidate_step_name,
                        _normalized_context_sha256(str(step.get("run", ""))),
                    )
                )
            if "uses" in step:
                findings.extend(_audit_action(step, name))
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
            step_context = {
                key: step[key] for key in ("name", "id", "if", "env") if key in step
            }
            expected_step_context = _RUN_STEP_CONTEXT_SHA256.get((name, step_name))
            if (
                expected_step_context is None
                or _normalized_context_sha256(step_context) != expected_step_context
            ):
                findings.append(
                    Finding(
                        name,
                        f"run step context differs from closed policy: {step_name}",
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
            expected_trust_body = _TRUST_CRITICAL_RUN_BODY_SHA256.get(
                (name, job_name, step_index, step_name)
            )
            if (
                expected_trust_body is not None
                and _normalized_context_sha256(run) != expected_trust_body
            ):
                findings.append(
                    Finding(
                        name,
                        f"trust-critical validator body differs: {step_name}",
                    )
                )
            if not run:
                continue
            shell_run, embedded_python, heredoc_findings = _extract_canonical_heredocs(
                run, name
            )
            findings.extend(heredoc_findings)
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
    for step_name, count in trust_critical_step_counts.items():
        if count != 1:
            findings.append(
                Finding(
                    name,
                    "trust-critical validator step must appear exactly once: "
                    f"{step_name}",
                )
            )
    expected_trust_critical = sorted(
        (job_name, step_index, step_name, body_digest)
        for (
            workflow_name,
            job_name,
            step_index,
            step_name,
        ), body_digest in _TRUST_CRITICAL_RUN_BODY_SHA256.items()
        if workflow_name == name
    )
    if sorted(observed_trust_critical) != expected_trust_critical:
        findings.append(Finding(name, "trust-critical validator location/body differs"))
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
