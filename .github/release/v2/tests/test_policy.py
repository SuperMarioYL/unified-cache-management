from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
DRY_RUN_WORKFLOWS = [
    "develop-release-dry-run.yml",
    "draft-environment-dry-run.yml",
    "nightly-release-dry-run.yml",
    "pr-release-dry-run.yml",
    "release-cleanup-dry-run.yml",
    "release-control-dry-run.yml",
    "release-lifecycle-dry-run.yml",
    "repository-policy-audit-dry-run.yml",
]


def _snapshot(
    repository: str = "SuperMarioYL/unified-cache-management",
) -> dict[str, object]:
    return {
        "kind": "repository-policy-snapshot",
        "schema_version": 2,
        "mode": "read-only",
        "repository": repository,
        "default_branch": "main",
        "branches": [
            {"name": "develop", "protected": True},
            {"name": "main", "protected": True},
        ],
        "rulesets": [
            {
                "name": "release-tags",
                "target": "tag",
                "enforcement": "active",
                "tag_pattern": "v[0-9]*",
            }
        ],
        "environments": [
            {
                "name": "release-production",
                "required_reviewers": 1,
                "deployment_branch_policy": {
                    "protected_branches": True,
                    "custom_branch_policies": False,
                },
            }
        ],
        "workflows": [
            {"name": name, "permissions": {"contents": "read"}}
            for name in DRY_RUN_WORKFLOWS
        ],
    }


def _run(
    tmp_path: Path,
    snapshot: object,
    role: str = "validation",
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "snapshot.json"
    if isinstance(snapshot, str):
        path.write_text(snapshot, encoding="utf-8")
    else:
        path.write_text(json.dumps(snapshot), encoding="utf-8")
    return subprocess.run(
        [
            "python3",
            "-m",
            "ucm_release_v2",
            "repo-policy",
            "audit",
            "--snapshot",
            str(path),
            "--repository-role",
            role,
        ],
        cwd=V2_ROOT,
        env=os.environ | {"PYTHONPATH": str(V2_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def _document(
    tmp_path: Path, snapshot: object, role: str = "validation"
) -> dict[str, object]:
    completed = _run(tmp_path, snapshot, role)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_policy_audit_reports_a_fully_compliant_offline_snapshot(
    tmp_path: Path,
) -> None:
    """Catches any required branch, tag, environment, or workflow check being omitted."""
    document = _document(tmp_path, _snapshot())
    assert document["compliant"] is True
    assert document["repository_role"] == "validation"
    checks = {item["id"]: item for item in document["checks"]}
    assert set(checks) == {
        "branch.develop.exists",
        "branch.develop.protected",
        "branch.main.exists",
        "branch.main.protected",
        "default-branch.main",
        "environment.release-production.branch-policy",
        "environment.release-production.exists",
        "environment.release-production.reviewers",
        "repository.coordinate",
        "ruleset.release-tags.active-pattern",
        "workflows.expected-set",
        "workflows.permissions.read-only",
    }
    assert all(
        item["status"] == "passed" and item["evidence"] for item in checks.values()
    )
    assert set(document) == {
        "checks",
        "compliant",
        "expected_repository",
        "kind",
        "mode",
        "observed_repository",
        "repository_role",
        "schema_version",
    }


def test_policy_audit_exposes_the_current_fork_fixture_gaps_without_live_claims(
    tmp_path: Path,
) -> None:
    """Catches collapsing independently actionable fork policy gaps into one opaque failure."""
    snapshot = _snapshot()
    snapshot["default_branch"] = "develop"
    snapshot["branches"] = [{"name": "develop", "protected": False}]
    snapshot["rulesets"] = [
        {
            "name": "rc-only",
            "target": "tag",
            "enforcement": "active",
            "tag_pattern": "v0.5.0rc1",
        }
    ]
    snapshot["environments"] = [
        {
            "name": "release-production",
            "required_reviewers": 0,
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
        }
    ]
    document = _document(tmp_path, snapshot)
    gaps = {item["id"] for item in document["checks"] if item["status"] == "gap"}
    assert document["compliant"] is False
    assert {
        "default-branch.main",
        "branch.develop.protected",
        "branch.main.exists",
        "branch.main.protected",
        "ruleset.release-tags.active-pattern",
        "environment.release-production.reviewers",
        "environment.release-production.branch-policy",
    } <= gaps


def test_repository_coordinate_mismatch_is_an_auditable_gap_not_a_parser_error(
    tmp_path: Path,
) -> None:
    """Catches auditing a well-formed snapshot against the wrong configured repository silently."""
    document = _document(tmp_path, _snapshot("attacker/unified-cache-management"))
    check = next(
        item for item in document["checks"] if item["id"] == "repository.coordinate"
    )
    assert document["compliant"] is False
    assert check["status"] == "gap"
    assert document["expected_repository"] == "SuperMarioYL/unified-cache-management"
    assert document["observed_repository"] == "attacker/unified-cache-management"


@pytest.mark.parametrize(
    ("mutation", "check_id"),
    [
        (lambda value: value["branches"].pop(), "branch.main.exists"),
        (
            lambda value: value["branches"][0].__setitem__("protected", False),
            "branch.develop.protected",
        ),
        (
            lambda value: value["rulesets"][0].__setitem__("tag_pattern", "v*"),
            "ruleset.release-tags.active-pattern",
        ),
        (
            lambda value: value["rulesets"][0].__setitem__("enforcement", "disabled"),
            "ruleset.release-tags.active-pattern",
        ),
        (
            lambda value: value["environments"][0].__setitem__("required_reviewers", 0),
            "environment.release-production.reviewers",
        ),
        (
            lambda value: value["environments"][0][
                "deployment_branch_policy"
            ].__setitem__("custom_branch_policies", True),
            "environment.release-production.branch-policy",
        ),
        (
            lambda value: value["workflows"][0]["permissions"].__setitem__(
                "contents", "write"
            ),
            "workflows.permissions.read-only",
        ),
        (lambda value: value["workflows"].pop(), "workflows.expected-set"),
    ],
)
def test_policy_audit_reports_each_policy_mutation_as_a_stable_gap(
    tmp_path: Path, mutation: object, check_id: str
) -> None:
    """Catches policy drift being accepted because only repository identity was checked."""
    snapshot = _snapshot()
    mutation(snapshot)  # type: ignore[operator]
    document = _document(tmp_path, snapshot)
    checks = {item["id"]: item for item in document["checks"]}
    assert document["compliant"] is False
    assert checks[check_id]["status"] == "gap"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.__setitem__("branches", {}), "branches must be a list"),
        (
            lambda value: value["branches"].append(copy.deepcopy(value["branches"][0])),
            "duplicate branch",
        ),
        (
            lambda value: value["rulesets"].append(copy.deepcopy(value["rulesets"][0])),
            "duplicate ruleset",
        ),
        (
            lambda value: value["environments"].append(
                copy.deepcopy(value["environments"][0])
            ),
            "duplicate environment",
        ),
        (
            lambda value: value["workflows"].append(
                copy.deepcopy(value["workflows"][0])
            ),
            "duplicate workflow",
        ),
        (
            lambda value: value["workflows"][0].__setitem__(
                "permissions", ["contents:read"]
            ),
            "permissions must be a mapping",
        ),
        (lambda value: value.__setitem__("extra", True), "keys mismatch"),
    ],
)
def test_policy_audit_rejects_malicious_types_duplicates_and_unknown_fields(
    tmp_path: Path, mutation: object, message: str
) -> None:
    """Catches malformed snapshot records being interpreted as absent policy gaps."""
    snapshot = _snapshot()
    mutation(snapshot)  # type: ignore[operator]
    completed = _run(tmp_path, snapshot)
    assert completed.returncode == 2
    assert message in completed.stderr
    assert "Traceback" not in completed.stderr


def test_policy_audit_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    """Catches one visible snapshot value masking the effective repository value."""
    duplicate = (
        '{"kind":"repository-policy-snapshot","schema_version":2,"mode":"read-only",'
        '"repository":"SuperMarioYL/unified-cache-management","repository":"other/repo",'
        '"default_branch":"main","branches":[],"rulesets":[],"environments":[],"workflows":[]}'
    )
    completed = _run(tmp_path, duplicate)
    assert completed.returncode == 2
    assert "duplicate key" in completed.stderr


def test_repository_policy_report_schema_is_strict_and_requires_evidence() -> None:
    """Catches schema drift permitting unknown checks or an unbound compliance claim."""
    schema = json.loads(
        (V2_ROOT / "schemas/repository-policy-report.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    check = schema["$defs"]["check"]
    assert check["additionalProperties"] is False
    assert set(check["required"]) == {"evidence", "id", "status"}
    assert check["properties"]["status"]["enum"] == ["gap", "passed"]


def _validate_report_schema(
    tmp_path: Path, report: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    validator = shutil.which("jsonschema")
    if validator is None:
        pytest.skip("jsonschema CLI is unavailable")
    report_path = tmp_path / "repository-policy-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return subprocess.run(
        [
            validator,
            str(V2_ROOT / "schemas/repository-policy-report.schema.json"),
            "-i",
            str(report_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_repository_policy_report_schema_accepts_actual_pass_and_gap_reports(
    tmp_path: Path,
) -> None:
    """Catches a schema contract that rejects real reports produced by the auditor."""
    compliant = _document(tmp_path, _snapshot())
    gap_snapshot = _snapshot()
    gap_snapshot["default_branch"] = "develop"
    gap = _document(tmp_path, gap_snapshot)
    for report in (compliant, gap):
        completed = _validate_report_schema(tmp_path, report)
        assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "twelve-duplicates",
        "unknown-id",
        "missing-check",
        "extra-check",
        "true-with-gap",
        "false-with-all-passed",
    ],
)
def test_repository_policy_report_schema_rejects_check_set_and_overall_drift(
    tmp_path: Path, mutation: str
) -> None:
    """Catches duplicated/missing checks or compliance contradicting check statuses."""
    report = _document(tmp_path, _snapshot())
    checks = report["checks"]
    assert isinstance(checks, list)
    if mutation == "twelve-duplicates":
        report["checks"] = [copy.deepcopy(checks[0]) for _ in range(12)]
    elif mutation == "unknown-id":
        checks[0]["id"] = "arbitrary.check"
    elif mutation == "missing-check":
        checks.pop()
    elif mutation == "extra-check":
        extra = copy.deepcopy(checks[0])
        extra["id"] = "extra.check"
        checks.append(extra)
    elif mutation == "true-with-gap":
        checks[0]["status"] = "gap"
    else:
        report["compliant"] = False
    completed = _validate_report_schema(tmp_path, report)
    assert completed.returncode != 0
