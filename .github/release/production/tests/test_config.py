from __future__ import annotations

import hashlib
import json

import pytest

from ucm_release_production.common import ProductionError
from ucm_release_production.config import derive_repository, load_config

from conftest import PRODUCTION_ROOT, REPO_ROOT

CONFIG = PRODUCTION_ROOT / "production-release.json"
FINGERPRINTS = PRODUCTION_ROOT / "tests" / "fixtures" / "legacy-workflow-sha256.json"

LEGACY_WORKFLOWS = {
    "_build-image.yml",
    "_build-wheel.yml",
    "_publish-image-member.yml",
    "lint-and-test.yml",
    "pull-request.yml",
    "push-check.yml",
    "release-ucm.yml",
    "release-vllm-images-protected.yml",
    "release-vllm-images.yml",
}


def _workflow_hashes() -> dict[str, str]:
    root = REPO_ROOT / ".github" / "workflows"
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in sorted(LEGACY_WORKFLOWS)
    }


def test_config_derives_current_repository_without_owner_constants() -> None:
    config = load_config(CONFIG)

    resolved = derive_repository(
        config,
        repository="OctoCat/unified-cache-management",
        repository_id=42,
        default_branch="develop",
    )

    assert resolved["repository"] == "OctoCat/unified-cache-management"
    assert resolved["repository_id"] == 42
    assert resolved["default_branch"] == "develop"
    assert resolved["ghcr_namespace"] == "octocat"
    assert resolved["release_branch"] == "0.6.0-release"
    assert resolved["image_repositories"] == [
        "ghcr.io/octocat/ucm-cuda",
        "ghcr.io/octocat/ucm-cann-a2",
        "ghcr.io/octocat/ucm-cann-a3",
    ]
    assert "SuperMarioYL" not in json.dumps(resolved)
    assert "ModelEngine-Group" not in json.dumps(resolved)


def test_config_has_exact_product_and_profile_closure() -> None:
    config = load_config(CONFIG)

    assert config["release_line"] == "0.6"
    assert config["base_version"] == "0.6.0"
    assert [item["distribution"] for item in config["products"]["wheels"]] == [
        "uc-manager-cuda",
        "uc-manager-cann-a2",
        "uc-manager-cann-a3",
    ]
    assert [item["id"] for item in config["build_profiles"]] == [
        "cuda130",
        "cann900-a2",
        "cann900-a3",
    ]
    assert all(
        item["cpu_arch"] == ["amd64", "arm64"] for item in config["build_profiles"]
    )
    assert config["channels"]["draft"]["image_visibility"] == "private"
    assert config["channels"]["rc"]["image_visibility"] == "public"
    assert config["external_channels"] == {"docker_hub": False, "pypi": False}


@pytest.mark.parametrize(
    ("repository", "repository_id", "default_branch", "message"),
    [
        ("owner/repo/extra", 1, "develop", "repository"),
        ("owner/repo", True, "develop", "repository_id"),
        ("owner/repo", 1, "refs/heads/develop", "default_branch"),
        ("owner/repo", 1, "main\nnext", "default_branch"),
    ],
)
def test_repository_identity_is_strict(
    repository: str, repository_id: int, default_branch: str, message: str
) -> None:
    with pytest.raises(ProductionError, match=message):
        derive_repository(
            load_config(CONFIG),
            repository=repository,
            repository_id=repository_id,
            default_branch=default_branch,
        )


def test_legacy_workflow_fingerprints_are_frozen() -> None:
    expected = json.loads(FINGERPRINTS.read_text(encoding="utf-8"))

    assert set(expected) == LEGACY_WORKFLOWS
    assert _workflow_hashes() == expected
