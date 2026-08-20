"""Fail-closed topology and byte-level audit for production workflows.

The production release graph is intentionally small and review-sensitive.  A
workflow change must therefore update both its tests and the reviewed digest in
this module.  This closes mutation classes that are easy to miss with a
deny-list scanner: moved trust steps, broader permissions, new executable
steps, expression-bearing shells, and alternate publishing paths.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PRODUCTION_WORKFLOWS = (
    "production-tag-candidate.yml",
    "_production-build-wheel.yml",
    "_production-build-image.yml",
    "production-release-controller.yml",
    "_production-release-controller.yml",
    "_production-publish-image-member.yml",
)

_REVIEWED_SHA256 = {
    "production-tag-candidate.yml": (
        "e8587a4a18fc3e71cc5afd87c537207f8690c696dacce4bcac8711d26b1a634d"
    ),
    "_production-build-wheel.yml": (
        "6096ba555e807327edcdfe1e49761b8a30813a0e88d2e3a98eed7766848a3552"
    ),
    "_production-build-image.yml": (
        "db110794a0ce0f18ba4ea6e35f61331b5f6451413c91adebeb8c44ace03fc8c7"
    ),
    "production-release-controller.yml": (
        "f13f42c0b7f5efb701f12b8e39c6334dcdbfb97b3f45ff8baa3134f067633ee0"
    ),
    "_production-release-controller.yml": (
        "1c2e75ffefe00415d647d68d9bfc486cfef6c67a82782d59ebd001be0a86b7b5"
    ),
    "_production-publish-image-member.yml": (
        "96aa06a9a316082cac9e58c8c90a175b0bc618bd8e0f61f2e657eb34dc550b35"
    ),
}


def _sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def audit_workflow_source(source: str, name: str) -> list[str]:
    """Return findings when *source* differs from the reviewed workflow."""

    expected = _REVIEWED_SHA256.get(name)
    if expected is None:
        return [f"unexpected production workflow: {name}"]
    observed = _sha256(source)
    if observed != expected:
        return [
            f"production workflow differs from reviewed bytes: {name} " f"({observed})"
        ]
    return []


def audit_repository(repository: Path) -> list[str]:
    """Audit the exact production workflow file set beneath *repository*."""

    workflow_root = repository / ".github" / "workflows"
    findings: list[str] = []
    observed_names = {
        path.name
        for pattern in ("production-*.yml", "_production-*.yml")
        for path in workflow_root.glob(pattern)
    }
    expected_names = set(PRODUCTION_WORKFLOWS)
    for name in sorted(expected_names - observed_names):
        findings.append(f"missing production workflow: {name}")
    for name in sorted(observed_names - expected_names):
        findings.append(f"unexpected production workflow: {name}")
    for name in PRODUCTION_WORKFLOWS:
        path = workflow_root / name
        if not path.is_file() or path.is_symlink():
            continue
        findings.extend(audit_workflow_source(path.read_text(encoding="utf-8"), name))
    return findings
