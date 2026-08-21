from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

V2_ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"PYTHONPATH": str(V2_ROOT)}
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=V2_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_strict_config_declares_roles_products_and_retention() -> None:
    """Catches an incomplete control-plane configuration."""
    result = _run("config", "validate", "--config", "release.yaml")

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["repositories"] == {
        "production": "ModelEngine-Group/unified-cache-management",
        "validation": "SuperMarioYL/unified-cache-management",
    }
    assert config["branches"] == {"develop": "develop", "main": "main"}
    assert [wheel["distribution"] for wheel in config["products"]["wheels"]] == [
        "uc-manager-cuda",
        "uc-manager-cann-a2",
        "uc-manager-cann-a3",
    ]
    assert config["retention_days"] == {
        "develop": 14,
        "draft": 30,
        "hotfix": None,
        "nightly": 14,
        "pr": 7,
        "rc": None,
        "stable": None,
    }
    assert config["repository_policy"] == {
        "default_branch": "main",
        "protected_branches": ["develop", "main"],
        "tag_pattern": "v[0-9]*",
        "production_environment": {
            "name": "release-production",
            "minimum_required_reviewers": 1,
            "deployment_branch_policy": {
                "protected_branches": True,
                "custom_branch_policies": False,
            },
        },
        "dry_run_workflows": [
            "develop-release-dry-run.yml",
            "draft-environment-dry-run.yml",
            "nightly-release-dry-run.yml",
            "pr-release-dry-run.yml",
            "release-cleanup-dry-run.yml",
            "release-control-dry-run.yml",
            "release-lifecycle-dry-run.yml",
            "repository-policy-audit-dry-run.yml",
        ],
    }


def test_repository_config_is_strict_json_despite_retaining_yaml_filename() -> None:
    """A non-JSON repository config would force the clean workflow onto PyYAML fallback."""
    config = json.loads((V2_ROOT / "release.yaml").read_text(encoding="utf-8"))
    assert config["kind"] == "ucm-release-lifecycle-config"


def test_config_and_reconcile_run_when_yaml_import_is_blocked(tmp_path: Path) -> None:
    """An eager or default PyYAML import would break the stdlib-only workflow path."""
    source_sha = "a" * 40
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
        source_sha,
        "--repository-role",
        "production",
        "--intent-json",
        json.dumps({"source_sha": source_sha, "stage": "rc", "version": "0.6.0rc1"}),
        "--output",
        str(plan_path),
        "--config",
        "release.yaml",
    )
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    base = tmp_path / "base"
    records: list[dict[str, object]] = []
    for product in plan["products"]:
        if product["kind"] == "image":
            records.append(
                {
                    **product,
                    "digest": "sha256:" + "b" * 64,
                    "platforms": [
                        {"platform": "linux/amd64", "digest": "sha256:" + "c" * 64},
                        {"platform": "linux/arm64", "digest": "sha256:" + "d" * 64},
                    ],
                }
            )
        else:
            relative = f"files/{product['name']}.fixture"
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(str(product["coordinate"]).encode())
            records.append({**product, "path": relative})
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(records), encoding="utf-8")
    manifest_path = tmp_path / "artifact-manifest.json"
    collected = _run(
        "artifacts",
        "collect",
        "--lifecycle-plan",
        str(plan_path),
        "--records-json",
        str(records_path),
        "--base-dir",
        str(base),
        "--output",
        str(manifest_path),
        "--config",
        "release.yaml",
    )
    assert collected.returncode == 0, collected.stderr
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
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    blocker.joinpath("yaml.py").write_text(
        "raise RuntimeError('PyYAML import is forbidden in the JSON workflow path')\n",
        encoding="utf-8",
    )
    env = os.environ | {"PYTHONPATH": f"{blocker}{os.pathsep}{V2_ROOT}"}

    validated = subprocess.run(
        [
            "python3",
            "-m",
            "ucm_release_v2",
            "config",
            "validate",
            "--config",
            "release.yaml",
        ],
        cwd=V2_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    reconciled = subprocess.run(
        [
            "python3",
            "-m",
            "ucm_release_v2",
            "reconcile",
            "plan",
            "--lifecycle-plan",
            str(plan_path),
            "--manifest",
            str(manifest_path),
            "--inventory",
            str(inventory_path),
            "--config",
            "release.yaml",
        ],
        cwd=V2_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert validated.returncode == 0, validated.stderr
    assert reconciled.returncode == 0, reconciled.stderr
    assert json.loads(reconciled.stdout)["kind"] == "reconcile-plan"


def test_json_config_rejects_duplicate_keys(tmp_path: Path) -> None:
    """Falling through JSON duplicates would let one visible value mask another effective value."""
    path = tmp_path / "release.yaml"
    path.write_text('{"mode":"dry-run","mode":"execute"}', encoding="utf-8")
    result = _run("config", "validate", "--config", str(path))
    assert result.returncode == 2
    assert "duplicate key" in result.stderr
    assert "Traceback" not in result.stderr


def test_yaml_config_rejects_nested_duplicate_keys_without_traceback(
    tmp_path: Path,
) -> None:
    """Catches PyYAML silently accepting the last nested security-sensitive key."""
    path = tmp_path / "duplicate.yaml"
    path.write_text(
        "mode: dry-run\nrepositories:\n  production: owner/first\n  production: owner/second\n",
        encoding="utf-8",
    )

    result = _run("config", "validate", "--config", str(path))

    assert result.returncode == 2
    assert "duplicate key" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_rejects_any_mode_other_than_dry_run(tmp_path: Path) -> None:
    """Catches a configuration change that could enable a write path."""
    path = tmp_path / "release.yaml"
    path.write_text("mode: execute\n", encoding="utf-8")

    result = _run("config", "validate", "--config", str(path))

    assert result.returncode != 0
    assert "mode must be dry-run" in result.stderr


def test_retention_lookup_rejects_unknown_class() -> None:
    """Catches silently defaulting an unreviewed retention policy."""
    result = _run("config", "retention", "forever", "--config", "release.yaml")

    assert result.returncode != 0
    assert "unknown retention class" in result.stderr


def test_python_modules_do_not_embed_repository_owner() -> None:
    """Catches accidentally turning configured repository coordinates into code."""
    package = V2_ROOT / "ucm_release_v2"
    files = list(package.glob("*.py"))
    assert files, "the lifecycle package must exist"
    source = "\n".join(path.read_text(encoding="utf-8") for path in files)

    assert "ModelEngine-Group/unified-cache-management" not in source
    assert "yulei/unified-cache-management" not in source


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda config: config.__setitem__("version", "banana"),
            "version must be a base x.y.z version",
        ),
        (
            lambda config: config["repositories"].__setitem__("production", 1),
            "repositories.production must be a non-empty string",
        ),
        (
            lambda config: config["products"]["images"][0].pop("repository"),
            "products.images[0] keys mismatch",
        ),
        (
            lambda config: config["products"]["images"][0].__setitem__(
                "platforms", ["linux/amd64", "linux/amd64"]
            ),
            "products.images[0].platforms must be exactly",
        ),
        (
            lambda config: config["products"]["images"].append(
                dict(config["products"]["images"][0])
            ),
            "products.images must declare exactly three unique families",
        ),
    ],
)
def test_config_rejects_malformed_fields_before_the_planner_can_consume_them(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    """Catches malformed configured values escaping as planner KeyError or TypeError."""
    config = yaml.safe_load((V2_ROOT / "release.yaml").read_text(encoding="utf-8"))
    mutate(config)  # type: ignore[operator]
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    result = _run("config", "validate", "--config", str(path))

    assert result.returncode != 0
    assert message in result.stderr
