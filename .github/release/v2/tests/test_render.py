from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "a" * 40
VERSION = "0.6.0rc1"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=V2_ROOT,
        env=os.environ | {"PYTHONPATH": str(V2_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def _signed(value: dict[str, object]) -> dict[str, object]:
    unsigned = dict(value)
    unsigned.pop("sha256", None)
    result = dict(unsigned)
    result["sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def _release_package(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    plan_path = tmp_path / "lifecycle-plan.json"
    planned = _run(
        "lifecycle",
        "plan",
        "--stage",
        "rc",
        "--trigger",
        "workflow_dispatch",
        "--ref",
        "refs/heads/main",
        "--source-sha",
        SOURCE_SHA,
        "--repository-role",
        "production",
        "--intent-json",
        json.dumps({"source_sha": SOURCE_SHA, "stage": "rc", "version": VERSION}),
        "--output",
        str(plan_path),
        "--config",
        "release.yaml",
    )
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    artifacts: list[dict[str, object]] = []
    for index, product in enumerate(plan["products"]):
        if product["kind"] == "image":
            artifacts.append(
                {
                    **product,
                    "version": VERSION,
                    "digest": "sha256:" + f"{index + 1:x}" * 64,
                    "platforms": [
                        {"platform": "linux/amd64", "digest": "sha256:" + "c" * 64},
                        {"platform": "linux/arm64", "digest": "sha256:" + "d" * 64},
                    ],
                }
            )
        else:
            suffix = ".tgz" if product["kind"] == "chart" else ".whl"
            artifacts.append(
                {
                    **product,
                    "version": VERSION,
                    "path": f"files/{product['name']}-{VERSION}{suffix}",
                    "sha256": f"{index + 1:x}" * 64,
                    "size": index + 1,
                }
            )
    manifest = _signed(
        {
            "artifacts": artifacts,
            "kind": "artifact-manifest",
            "lifecycle_plan_sha256": plan["sha256"],
            "mode": "dry-run",
            "schema_version": 2,
            "source_sha": SOURCE_SHA,
            "stage": "rc",
            "validation": {
                "file_bytes": "passed",
                "lifecycle_plan": "passed",
                "oci_identity": "passed",
                "product_closure": "passed",
                "registry_readback": "unexecuted",
                "runtime": "unexecuted",
            },
            "version": VERSION,
        }
    )
    manifest_path = tmp_path / "artifact-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        json.dumps(
            {
                "kind": "release-inventory",
                "mode": "read-only",
                "schema_version": 2,
                "targets": [],
            }
        ),
        encoding="utf-8",
    )
    reconcile_path = tmp_path / "reconcile-plan.json"
    reconciled = _run(
        "reconcile",
        "plan",
        "--lifecycle-plan",
        str(plan_path),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory_path),
        "--output",
        str(reconcile_path),
        "--config",
        "release.yaml",
    )
    assert reconciled.returncode == 0, reconciled.stderr
    return plan_path, manifest_path, inventory_path, reconcile_path


def _render(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    plan, manifest, inventory, reconcile = _release_package(tmp_path)
    return _run(
        "release",
        "render",
        "--lifecycle-plan",
        str(plan),
        "--manifest",
        str(manifest),
        "--inventory",
        str(inventory),
        "--reconcile-plan",
        str(reconcile),
        *extra,
        "--config",
        "release.yaml",
    )


def test_release_render_matches_the_complete_user_facing_golden(tmp_path: Path) -> None:
    """Removing an install path, identity, evidence boundary, or blocker changes the golden."""
    result = _render(tmp_path)

    assert result.returncode == 0, result.stderr
    expected = f"""# UCM Release Preview - DRY-RUN

> **DO NOT INSTALL OR PULL:** This is a read-only DRY-RUN. Every coordinate below is planned, not published, and has no Registry/Release readback. Commands are copyable previews only.

## Release identity

- Stage: `rc`
- Version: `{VERSION}`
- Source SHA: `{SOURCE_SHA}`

## Wheel install preview

Choose exactly one backend Wheel. Do not mix-install these distributions in one environment.

```bash
python -m pip install 'uc-manager-cuda=={VERSION}'
python -m pip install 'uc-manager-cann-a2=={VERSION}'
python -m pip install 'uc-manager-cann-a3=={VERSION}'
```

## Image pull preview

| Family | Planned coordinate | Index digest | linux/amd64 member digest | linux/arm64 member digest |
| --- | --- | --- | --- | --- |
| cuda | `ghcr.io/ModelEngine-Group/unified-cache-management/ucm-cuda:{VERSION}` | `sha256:{'4' * 64}` | `sha256:{'c' * 64}` | `sha256:{'d' * 64}` |
| cann-a2 | `ghcr.io/ModelEngine-Group/unified-cache-management/ucm-cann-a2:{VERSION}` | `sha256:{'2' * 64}` | `sha256:{'c' * 64}` | `sha256:{'d' * 64}` |
| cann-a3 | `ghcr.io/ModelEngine-Group/unified-cache-management/ucm-cann-a3:{VERSION}` | `sha256:{'3' * 64}` | `sha256:{'c' * 64}` | `sha256:{'d' * 64}` |

```bash
docker pull 'ghcr.io/ModelEngine-Group/unified-cache-management/ucm-cuda:{VERSION}@sha256:{'4' * 64}'
docker pull 'ghcr.io/ModelEngine-Group/unified-cache-management/ucm-cann-a2:{VERSION}@sha256:{'2' * 64}'
docker pull 'ghcr.io/ModelEngine-Group/unified-cache-management/ucm-cann-a3:{VERSION}@sha256:{'3' * 64}'
```

## Chart install preview

- Planned coordinate: `unified-cache-pd@{VERSION}`
- Local artifact SHA256: `{'1' * 64}`

```bash
helm upgrade --install ucm './files/unified-cache-pd-{VERSION}.tgz'
```

## Compatibility

| Backend | Choose this Wheel | Matching image | Supported platforms |
| --- | --- | --- | --- |
| CUDA | `uc-manager-cuda=={VERSION}` | `ghcr.io/ModelEngine-Group/unified-cache-management/ucm-cuda:{VERSION}` | `linux/amd64`, `linux/arm64` |
| Ascend A2 | `uc-manager-cann-a2=={VERSION}` | `ghcr.io/ModelEngine-Group/unified-cache-management/ucm-cann-a2:{VERSION}` | `linux/amd64`, `linux/arm64` |
| Ascend A3 | `uc-manager-cann-a3=={VERSION}` | `ghcr.io/ModelEngine-Group/unified-cache-management/ucm-cann-a3:{VERSION}` | `linux/amd64`, `linux/arm64` |

## Evidence matrix

| Evidence layer | State | What it proves |
| --- | --- | --- |
| Local artifact bytes | passed | File SHA256 and size were recorded before this transport preview. |
| Declarative OCI identity | passed | Index/member digests are declarations only; no registry was queried. |
| Simulated environment | not-provided | Simulated evidence cannot satisfy a production gate. |
| Registry/Release readback | unexecuted | Planned coordinates are not proven published or pullable. |
| Runtime | unexecuted | No import, service, or inference runtime was exercised here. |
| Hardware | unexecuted | No CUDA or Ascend device result was collected here. |
| Cluster | unexecuted | No Kubernetes installation or acceptance was performed here. |

## Known issues

- None declared.

## Reconciliation

- Status: `conflict-free-preview`
- Production ready: `false`
- Create previews: `7`
- Identical skips: `0`
- Conflicts: `0`
- Promotion evidence: `not-provided`
- Blockers:
  - `external-environment-evidence-required`

Promotion evidence, when present, is an offline declaration. This preview is not Registry readback, a GitHub Release, runtime evidence, hardware evidence, or cluster acceptance.
"""
    assert result.stdout == expected


def test_known_issues_are_strict_normalized_and_cannot_inject_markdown_or_html(
    tmp_path: Path,
) -> None:
    """Raw newlines, links, and HTML in issue data must remain inert one-line text."""
    issues = tmp_path / "known-issues.json"
    issues.write_text(
        json.dumps(
            [
                "first line\r\nsecond line",
                "<script>alert(1)</script>",
                "[click](https://invalid.example)",
            ]
        ),
        encoding="utf-8",
    )
    result = _render(tmp_path, "--known-issues-json", str(issues))

    assert result.returncode == 0, result.stderr
    assert "- first line second line\n" in result.stdout
    assert "<script>" not in result.stdout
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in result.stdout
    assert "\\[click\\](https://invalid.example)" in result.stdout

    issues.write_text(json.dumps(["valid", 7]), encoding="utf-8")
    rejected = _render(tmp_path / "malformed", "--known-issues-json", str(issues))
    assert rejected.returncode == 2
    assert "string array" in rejected.stderr
    assert "Traceback" not in rejected.stderr


def test_render_refuses_overwrite_and_rejects_a_mismatched_reconcile_envelope(
    tmp_path: Path,
) -> None:
    """Rendering cannot overwrite a review file or mix a plan from another release."""
    output = tmp_path / "release-preview.md"
    first = _render(tmp_path / "first", "--output", str(output))
    assert first.returncode == 0, first.stderr
    original = output.read_text(encoding="utf-8")
    second = _render(tmp_path / "second", "--output", str(output))
    assert second.returncode == 2
    assert "refusing to overwrite" in second.stderr
    assert output.read_text(encoding="utf-8") == original

    plan, manifest, inventory, reconcile = _release_package(tmp_path / "mismatch")
    document = json.loads(reconcile.read_text(encoding="utf-8"))
    document["artifact_manifest_sha256"] = "f" * 64
    reconcile.write_text(json.dumps(_signed(document)), encoding="utf-8")
    rejected = _run(
        "release",
        "render",
        "--lifecycle-plan",
        str(plan),
        "--manifest",
        str(manifest),
        "--inventory",
        str(inventory),
        "--reconcile-plan",
        str(reconcile),
        "--config",
        "release.yaml",
    )
    assert rejected.returncode == 2
    assert "does not match" in rejected.stderr
    assert "Traceback" not in rejected.stderr


def test_render_rejects_malformed_reconcile_types_without_traceback(
    tmp_path: Path,
) -> None:
    """An unhashable blocker value must fail at the CLI boundary instead of crashing."""
    plan, manifest, inventory, reconcile = _release_package(tmp_path)
    document = json.loads(reconcile.read_text(encoding="utf-8"))
    document["blockers"] = [["external-environment-evidence-required"]]
    reconcile.write_text(json.dumps(_signed(document)), encoding="utf-8")

    rejected = _run(
        "release",
        "render",
        "--lifecycle-plan",
        str(plan),
        "--manifest",
        str(manifest),
        "--inventory",
        str(inventory),
        "--reconcile-plan",
        str(reconcile),
        "--config",
        "release.yaml",
    )

    assert rejected.returncode == 2
    assert "blockers" in rejected.stderr
    assert "Traceback" not in rejected.stderr


@pytest.mark.parametrize("mutation", ["action", "identity", "blocker", "inventory-sha"])
def test_render_rebuilds_and_rejects_resigned_reconcile_semantic_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    """A validly re-signed reconcile document cannot override reconstruction from raw inputs."""
    plan, manifest, inventory, reconcile = _release_package(tmp_path)
    document = json.loads(reconcile.read_text(encoding="utf-8"))
    if mutation == "action":
        document["operations"][0]["action"] = "skip-identical"
    elif mutation == "identity":
        document["operations"][0]["identity"] = "9" * 64
    elif mutation == "blocker":
        document["blockers"].append("manual-policy-blocker")
        document["blockers"].sort()
        document["status"] = "blocked"
    else:
        document["inventory_sha256"] = "9" * 64
    reconcile.write_text(json.dumps(_signed(document)), encoding="utf-8")

    rejected = _run(
        "release",
        "render",
        "--lifecycle-plan",
        str(plan),
        "--manifest",
        str(manifest),
        "--inventory",
        str(inventory),
        "--reconcile-plan",
        str(reconcile),
        "--config",
        "release.yaml",
    )

    assert rejected.returncode == 2
    assert "reconstructed" in rejected.stderr or "match" in rejected.stderr
    assert "Traceback" not in rejected.stderr


def test_render_rejects_reconcile_built_from_a_different_inventory(
    tmp_path: Path,
) -> None:
    """Supplying a new inventory cannot reuse a reconcile result from the empty snapshot."""
    plan, manifest_path, _, reconcile = _release_package(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    targets = [
        {
            "coordinate": artifact["coordinate"],
            "identity": (
                artifact["digest"]
                if artifact["kind"] == "image"
                else artifact["sha256"]
            ),
            "kind": artifact["kind"],
            "name": artifact["name"],
        }
        for artifact in manifest["artifacts"]
    ]
    different_inventory = tmp_path / "different-inventory.json"
    different_inventory.write_text(
        json.dumps(
            {
                "kind": "release-inventory",
                "mode": "read-only",
                "schema_version": 2,
                "targets": targets,
            }
        ),
        encoding="utf-8",
    )

    rejected = _run(
        "release",
        "render",
        "--lifecycle-plan",
        str(plan),
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(different_inventory),
        "--reconcile-plan",
        str(reconcile),
        "--config",
        "release.yaml",
    )

    assert rejected.returncode == 2
    assert "reconstructed" in rejected.stderr or "match" in rejected.stderr
    assert "Traceback" not in rejected.stderr
