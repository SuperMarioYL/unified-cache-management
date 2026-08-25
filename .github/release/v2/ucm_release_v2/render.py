"""Render a user-facing Markdown preview from strictly validated release inputs."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .reconcile import (
    ReconcileError,
    build_reconcile_plan,
    load_json,
    load_release_inputs,
    validate_reconcile_plan,
)


class RenderError(ValueError):
    """Raised when a release preview cannot be rendered safely."""


_FAMILIES = (
    ("cuda", "CUDA", "uc-manager-cuda"),
    ("cann-a2", "Ascend A2", "uc-manager-cann-a2"),
    ("cann-a3", "Ascend A3", "uc-manager-cann-a3"),
)


def _known_issues(path: Path | None) -> list[str]:
    if path is None:
        return []
    value = load_json(path, "known issues")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RenderError("known issues must be a JSON string array")
    issues: list[str] = []
    for item in value:
        normalized = " ".join(item.replace("\r\n", "\n").replace("\r", "\n").split())
        if not normalized:
            raise RenderError(
                "known issues must be a JSON string array of non-empty issues"
            )
        escaped = html.escape(normalized, quote=False)
        escaped = escaped.replace("\\", "\\\\")
        for character in ("`", "*", "_", "[", "]", "|", "#", ">", "~", "!"):
            escaped = escaped.replace(character, "\\" + character)
        issues.append(escaped)
    return issues


def _platform_digest(image: dict[str, Any], platform: str) -> str:
    return next(
        item["digest"] for item in image["platforms"] if item["platform"] == platform
    )


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def render_release_preview(
    config: dict[str, Any],
    lifecycle_path: Path,
    manifest_path: Path,
    inventory_path: Path,
    reconcile_path: Path,
    known_issues_path: Path | None = None,
    promotion_path: Path | None = None,
    promotion_source_lifecycle_path: Path | None = None,
    promotion_source_manifest_path: Path | None = None,
    environment_lifecycle_path: Path | None = None,
    environment_manifest_path: Path | None = None,
    environment_request_path: Path | None = None,
    environment_result_path: Path | None = None,
) -> str:
    """Return Markdown only after all content-addressed release documents agree."""
    try:
        plan, manifest = load_release_inputs(config, lifecycle_path, manifest_path)
        expected_reconcile = build_reconcile_plan(
            config,
            lifecycle_path,
            manifest_path,
            inventory_path,
            promotion_path,
            promotion_source_lifecycle_path,
            promotion_source_manifest_path,
            environment_lifecycle_path,
            environment_manifest_path,
            environment_request_path,
            environment_result_path,
        )
        reconcile = validate_reconcile_plan(
            load_json(reconcile_path, "reconcile plan"), plan, manifest
        )
        if reconcile != expected_reconcile:
            raise RenderError(
                "reconcile plan does not match the reconstructed raw inputs"
            )
    except ReconcileError as error:
        raise RenderError(str(error)) from error
    issues = _known_issues(known_issues_path)
    artifacts = manifest["artifacts"]
    by_identity = {(item["kind"], item["name"]): item for item in artifacts}
    version = plan["version"]
    lines = [
        "# UCM Release Preview - DRY-RUN",
        "",
        "> **DO NOT INSTALL OR PULL:** This is a read-only DRY-RUN. Every coordinate below is planned, not published, and has no Registry/Release readback. Commands are copyable previews only.",
        "",
        "## Release identity",
        "",
        f"- Stage: `{plan['stage']}`",
        f"- Version: `{version}`",
        f"- Source SHA: `{plan['source_sha']}`",
        "",
        "## Wheel install preview",
        "",
        "Choose exactly one backend Wheel. Do not mix-install these distributions in one environment.",
        "",
        "```bash",
    ]
    for _, _, wheel_name in _FAMILIES:
        wheel = by_identity[("wheel", wheel_name)]
        lines.append(f"python -m pip install {_shell_quote(wheel['coordinate'])}")
    lines.extend(
        [
            "```",
            "",
            "## Image pull preview",
            "",
            "| Family | Planned coordinate | Index digest | linux/amd64 member digest | linux/arm64 member digest |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for family, _, _ in _FAMILIES:
        image = by_identity[("image", family)]
        lines.append(
            f"| {family} | `{image['coordinate']}` | `{image['digest']}` | "
            f"`{_platform_digest(image, 'linux/amd64')}` | "
            f"`{_platform_digest(image, 'linux/arm64')}` |"
        )
    lines.extend(["", "```bash"])
    for family, _, _ in _FAMILIES:
        image = by_identity[("image", family)]
        lines.append(
            f"docker pull {_shell_quote(image['coordinate'] + '@' + image['digest'])}"
        )
    chart = next(item for item in artifacts if item["kind"] == "chart")
    chart_path = "./" + chart["path"]
    lines.extend(
        [
            "```",
            "",
            "## Chart install preview",
            "",
            f"- Planned coordinate: `{chart['coordinate']}`",
            f"- Local artifact SHA256: `{chart['sha256']}`",
            "",
            "```bash",
            f"helm upgrade --install ucm {_shell_quote(chart_path)}",
            "```",
            "",
            "## Compatibility",
            "",
            "| Backend | Choose this Wheel | Matching image | Supported platforms |",
            "| --- | --- | --- | --- |",
        ]
    )
    for family, label, wheel_name in _FAMILIES:
        wheel = by_identity[("wheel", wheel_name)]
        image = by_identity[("image", family)]
        lines.append(
            f"| {label} | `{wheel['coordinate']}` | `{image['coordinate']}` | "
            "`linux/amd64`, `linux/arm64` |"
        )
    lines.extend(
        [
            "",
            "## Evidence matrix",
            "",
            "| Evidence layer | State | What it proves |",
            "| --- | --- | --- |",
            "| Local artifact bytes | passed | File SHA256 and size were recorded before this transport preview. |",
            "| Declarative OCI identity | passed | Index/member digests are declarations only; no registry was queried. |",
            f"| Simulated environment | {reconcile['simulated_environment']} | Simulated evidence cannot satisfy a production gate. |",
            "| Registry/Release readback | unexecuted | Planned coordinates are not proven published or pullable. |",
            "| Runtime | unexecuted | No import, service, or inference runtime was exercised here. |",
            "| Hardware | unexecuted | No CUDA or Ascend device result was collected here. |",
            "| Cluster | unexecuted | No Kubernetes installation or acceptance was performed here. |",
            "",
            "## Known issues",
            "",
        ]
    )
    if issues:
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("- None declared.")
    counts = {
        action: sum(
            operation["action"] == action for operation in reconcile["operations"]
        )
        for action in ("create-preview", "skip-identical", "conflict")
    }
    promotion = reconcile["promotion_evidence_sha256"] or "not-provided"
    lines.extend(
        [
            "",
            "## Reconciliation",
            "",
            f"- Status: `{reconcile['status']}`",
            "- Production ready: `false`",
            f"- Create previews: `{counts['create-preview']}`",
            f"- Identical skips: `{counts['skip-identical']}`",
            f"- Conflicts: `{counts['conflict']}`",
            f"- Promotion evidence: `{promotion}`",
            "- Blockers:",
        ]
    )
    lines.extend(f"  - `{blocker}`" for blocker in reconcile["blockers"])
    lines.extend(
        [
            "",
            "Promotion evidence, when present, is an offline declaration. This preview is not Registry readback, a GitHub Release, runtime evidence, hardware evidence, or cluster acceptance.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = ["RenderError", "render_release_preview"]
