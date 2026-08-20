from __future__ import annotations

import copy

import pytest
from conftest import PRODUCTION_ROOT
from jsonschema import Draft202012Validator
from ucm_release_production.common import ProductionError, sha256_envelope
from ucm_release_production.evidence import assemble_evidence, render_summary


def _record(channel: str, status: str = "complete") -> dict[str, object]:
    return sha256_envelope(
        {
            "kind": "ucm-production-channel-record",
            "schema_version": 1,
            "channel": channel,
            "stage": "rc",
            "status": status,
            "reference": f"example:{channel}",
            "decision": "create" if status == "complete" else "blocked",
            "operations": [],
        }
    )


def _identity() -> dict[str, object]:
    return sha256_envelope(
        {
            "kind": "ucm-production-trusted-run-identity",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "default_branch": "develop",
            "control_sha": "1" * 40,
            "run_id": 101,
            "run_attempt": 1,
            "source_sha": "2" * 40,
            "tag_name": "v0.6.0rc1",
            "tag_object_sha": "3" * 40,
        }
    )


def _candidate() -> dict[str, object]:
    return sha256_envelope(
        {
            "kind": "ucm-production-candidate-envelope",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "stage": "rc",
            "tag_name": "v0.6.0rc1",
            "source_sha": "2" * 40,
            "wheels": [{"path": "wheel.whl", "file_sha256": "sha256:" + "4" * 64}],
            "chart": {"path": "chart.tgz", "file_sha256": "sha256:" + "5" * 64},
            "image_members": [],
            "image_indexes": [],
        }
    )


def _environment(status: str = "waived-for-preview") -> dict[str, object]:
    return sha256_envelope(
        {
            "kind": "ucm-production-environment-evidence",
            "schema_version": 1,
            "status": status,
            "source_sha": "2" * 40,
            "environment": "release-production",
            "deployment_id": 99,
            "approval_actor": "SuperMarioYL",
        }
    )


def test_evidence_complete_partial_and_blocked_states() -> None:
    complete = assemble_evidence(
        _identity(),
        _candidate(),
        _environment(),
        [_record("ghcr-index"), _record("github-release")],
    )
    partial = assemble_evidence(
        _identity(),
        _candidate(),
        _environment(),
        [_record("ghcr-index"), _record("github-release", "blocked")],
    )
    blocked = assemble_evidence(_identity(), _candidate(), _environment(), [])

    assert complete["status"] == "complete"
    assert partial["status"] == "partial-publication"
    assert blocked["status"] == "blocked"


def test_evidence_schema_and_cross_identity_are_strict() -> None:
    evidence = assemble_evidence(
        _identity(), _candidate(), _environment(), [_record("github-release")]
    )
    schema = __import__("json").loads(
        (
            PRODUCTION_ROOT / "schemas" / "production-release-evidence.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(evidence)
    candidate = copy.deepcopy(_candidate())
    candidate["source_sha"] = "9" * 40
    candidate["sha256"] = sha256_envelope(
        {k: v for k, v in candidate.items() if k != "sha256"}
    )["sha256"]
    with pytest.raises(ProductionError, match="identity"):
        assemble_evidence(_identity(), candidate, _environment(), [])


def test_markdown_summary_escapes_external_injection() -> None:
    evidence = assemble_evidence(
        _identity(), _candidate(), _environment(), [_record("github-release")]
    )
    evidence["blockers"] = ["</details>\n```bash\ncurl evil\n``` | table"]
    evidence["sha256"] = sha256_envelope(
        {k: v for k, v in evidence.items() if k != "sha256"}
    )["sha256"]

    summary = render_summary(evidence)

    assert "<" not in summary
    assert "```" not in summary
    assert "curl evil" in summary
    assert "\\|" in summary
