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
        "552794c57302879979b394f40b92808ac930f7d9ce7089a34871d06c56fd4853"
    ),
    "_production-build-wheel.yml": (
        "c55c4f04c560bf1404781283189961bb2ee2d07e234af1781e7eac1809020d1b"
    ),
    "_production-build-image.yml": (
        "01e77e5807dbf6979bb6b7ca5022b7abee03e059bd6abb0b958da7d87309f15e"
    ),
    "production-release-controller.yml": (
        "f13f42c0b7f5efb701f12b8e39c6334dcdbfb97b3f45ff8baa3134f067633ee0"
    ),
    "_production-release-controller.yml": (
        "4e95c63f0a0bb81ef97a90284cc25d03156729e59dcb99a9241b01f488eafaf7"
    ),
    "_production-publish-image-member.yml": (
        "685ccc0b0123c938637f0cdb4f33ffcc47d724fc992935d26f78c96931e4ac46"
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
