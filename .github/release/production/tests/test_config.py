from __future__ import annotations

import copy
import hashlib
import json

import pytest
from conftest import PRODUCTION_ROOT, REPO_ROOT
from jsonschema import Draft202012Validator
from ucm_release_production.common import ProductionError
from ucm_release_production.config import derive_repository, load_config

CONFIG = PRODUCTION_ROOT / "production-release.json"
CONFIG_SCHEMA = PRODUCTION_ROOT / "schemas" / "production-release-config.schema.json"
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
    assert [item["build_platform"] for item in config["build_profiles"]] == [
        "cuda",
        "ascend",
        "ascend-a3",
    ]
    assert config["toolchain"]["runtime_requirements"] == [
        "packaging==24.2",
        "wrapt==1.17.2",
    ]
    assert config["channels"]["draft"]["image_visibility"] == "private"
    assert config["channels"]["rc"]["image_visibility"] == "public"
    assert config["external_channels"] == {"docker_hub": False, "pypi": False}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("distribution", "uc-manager-cann-a2"),
        ("build_platform", "ascend"),
        ("wheel_platform", "linux"),
        ("python_version", "3.11"),
        ("python_abi", "cp311"),
    ],
)
def test_production_profile_rejects_every_exact_tuple_drift(
    field: str, value: str
) -> None:
    from ucm_release_production.config import validate_config

    config = copy.deepcopy(load_config(CONFIG))
    config["build_profiles"][0][field] = value

    with pytest.raises(ProductionError, match=field):
        validate_config(config)


def test_production_config_schema_closes_profile_and_runtime_authority() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    schema = json.loads(CONFIG_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    validator.validate(config)
    config["build_profiles"][0]["wheel_platform"] = "evil"
    assert list(validator.iter_errors(config))


@pytest.mark.parametrize("inverse", [False, True])
def test_config_rejects_ambiguous_or_inverse_profile_discriminator(
    inverse: bool,
) -> None:
    from ucm_release_production.config import validate_config

    config = copy.deepcopy(load_config(CONFIG))
    profile = config["build_profiles"][0]
    if inverse:
        profile["profile_id"] = profile.pop("id")
    else:
        profile["profile_id"] = "cann900-a2"

    with pytest.raises(
        ProductionError,
        match="must contain id and must not contain profile_id",
    ):
        validate_config(config)


def test_config_accepts_only_explicit_external_channel_coordinates() -> None:
    config = copy.deepcopy(load_config(CONFIG))
    config["external_channels"] = {
        "pypi": {
            "repository": "https://upload.pypi.org/legacy/",
            "trusted_publisher": "github-oidc",
        },
        "docker_hub": {
            "namespace": "explicit-owner",
            "repositories": {
                "cuda130": "ucm-cuda",
                "cann900-a2": "ucm-cann-a2",
                "cann900-a3": "ucm-cann-a3",
            },
        },
    }

    from ucm_release_production.config import validate_config

    assert validate_config(config)["external_channels"] == config["external_channels"]
    config["external_channels"]["docker_hub"]["namespace"] = "CurrentOwner"
    with pytest.raises(ProductionError, match="namespace"):
        validate_config(config)


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
