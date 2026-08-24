from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from conftest import PRODUCTION_ROOT
from jsonschema import Draft202012Validator
from ucm_release_production.common import ProductionError, sha256_envelope
from ucm_release_production.config import load_config
from ucm_release_production.external import (
    DockerHubPublishRequest,
    ExternalCredentials,
    PyPIPublishRequest,
    preflight_external_channels,
    publish_docker_hub,
    publish_pypi,
)
from ucm_release_production.tags import parse_tag

CONFIG = PRODUCTION_ROOT / "production-release.json"
SOURCE = "1" * 40


def _enabled() -> dict[str, Any]:
    config = copy.deepcopy(load_config(CONFIG))
    config["external_channels"] = {
        "pypi": {
            "repository": "https://upload.pypi.org/legacy/",
            "trusted_publisher": "github-oidc",
        },
        "docker_hub": {
            "namespace": "ucm-preview",
            "repositories": {
                "cuda130": "ucm-cuda",
                "cann900-a2": "ucm-cann-a2",
                "cann900-a3": "ucm-cann-a3",
            },
        },
    }
    return config


def _environment(status: str = "passed") -> dict[str, Any]:
    return sha256_envelope(
        {
            "kind": "ucm-production-environment-evidence",
            "schema_version": 1,
            "status": status,
            "source_sha": SOURCE,
        }
    )


def test_draft_and_rc_never_schedule_external_operations() -> None:
    config = _enabled()
    credentials = ExternalCredentials(
        pypi_oidc=True,
        docker_username="user",
        docker_token_present=True,
    )

    for tag in ("draft/v0.6.0-1", "v0.6.0rc1"):
        result = preflight_external_channels(
            parse_tag(tag, config),
            config,
            _environment("waived-for-preview"),
            credentials,
        )
        assert result["operations"] == []
        assert result["channels"] == {
            "pypi": "not-applicable",
            "docker_hub": "not-applicable",
        }


def test_stable_requires_same_sha_rc_real_environment_and_all_credentials() -> None:
    config = _enabled()
    intent = parse_tag("v0.6.0", config)

    with pytest.raises(ProductionError, match="environment"):
        preflight_external_channels(
            intent,
            config,
            _environment("waived-for-preview"),
            ExternalCredentials(
                pypi_oidc=True, docker_username="user", docker_token_present=True
            ),
        )
    with pytest.raises(ProductionError, match="OIDC"):
        preflight_external_channels(
            intent,
            config,
            _environment(),
            ExternalCredentials(
                pypi_oidc=False, docker_username="user", docker_token_present=True
            ),
        )
    with pytest.raises(ProductionError, match="Docker Hub"):
        preflight_external_channels(
            intent,
            config,
            _environment(),
            ExternalCredentials(
                pypi_oidc=True, docker_username=None, docker_token_present=False
            ),
        )


def test_disabled_stable_channels_require_no_external_credentials() -> None:
    config = load_config(CONFIG)
    result = preflight_external_channels(
        parse_tag("v0.6.0", config),
        config,
        _environment(),
        ExternalCredentials(
            pypi_oidc=False, docker_username=None, docker_token_present=False
        ),
    )

    assert result["operations"] == []
    assert result["channels"] == {"pypi": "disabled", "docker_hub": "disabled"}


class FakePyPI:
    def __init__(self) -> None:
        self.files: dict[tuple[str, str], dict[str, Any]] = {}
        self.operations: list[tuple[str, object]] = []

    def inspect(self, distribution: str, version: str) -> list[dict[str, Any]]:
        self.operations.append(("inspect", (distribution, version)))
        item = self.files.get((distribution, version))
        return [] if item is None else [dict(item)]

    def upload_oidc(self, path: Path, repository: str) -> None:
        self.operations.append(("upload-oidc", (path.name, repository)))
        distribution, version = path.name.split("-", 2)[:2]
        from hashlib import sha256

        self.files[(distribution.replace("_", "-"), version)] = {
            "filename": path.name,
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }


def _wheel(tmp_path: Path, distribution: str) -> Path:
    path = (
        tmp_path
        / f"{distribution.replace('-', '_')}-0.6.0-cp312-cp312-manylinux_2_28_x86_64.whl"
    )
    path.write_bytes((distribution + "-stable-wheel").encode())
    return path


def test_pypi_create_reuse_conflict_and_oidc_only(tmp_path: Path) -> None:
    transport = FakePyPI()
    request = PyPIPublishRequest.from_path(
        stage="stable",
        distribution="uc-manager-cuda",
        version="0.6.0",
        path=_wheel(tmp_path, "uc-manager-cuda"),
        repository="https://upload.pypi.org/legacy/",
    )

    first = publish_pypi(request, transport)
    before = list(transport.operations)
    second = publish_pypi(request, transport)

    assert first["decision"] == "create"
    assert second["decision"] == "reuse"
    assert not any(
        action == "upload-oidc" for action, _ in transport.operations[len(before) :]
    )
    other = dict(transport.files[(request.distribution, request.version)])
    other["filename"] = other["filename"].replace("x86_64", "aarch64")
    original_inspect = transport.inspect
    transport.inspect = lambda distribution, version: [
        *original_inspect(distribution, version),
        other,
    ]
    assert publish_pypi(request, transport)["decision"] == "reuse"
    transport.files[(request.distribution, request.version)]["sha256"] = "9" * 64
    with pytest.raises(ProductionError, match="conflict"):
        publish_pypi(request, transport)


class FakeDockerHub:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.operations: list[tuple[str, str]] = []

    def digest(self, reference: str) -> str | None:
        self.operations.append(("digest", reference))
        return self.tags.get(reference)

    def copy(self, source: str, target: str) -> None:
        self.operations.append(("copy", f"{source}->{target}"))
        self.tags[target] = source.rsplit("@", 1)[1]


def test_docker_hub_create_reuse_and_conflict() -> None:
    transport = FakeDockerHub()
    request = DockerHubPublishRequest(
        stage="stable",
        profile_id="cuda130",
        source_reference="ghcr.io/octocat/ucm-cuda@sha256:" + "1" * 64,
        target_reference="docker.io/ucm-preview/ucm-cuda:v0.6.0",
        manifest_digest="sha256:" + "1" * 64,
    )

    first = publish_docker_hub(request, transport)
    before = list(transport.operations)
    second = publish_docker_hub(request, transport)

    assert first["decision"] == "create"
    assert second["decision"] == "reuse"
    assert not any(
        action == "copy" for action, _ in transport.operations[len(before) :]
    )
    transport.tags[request.target_reference] = "sha256:" + "9" * 64
    with pytest.raises(ProductionError, match="conflict"):
        publish_docker_hub(request, transport)


def test_external_channel_records_validate_against_closed_schema(
    tmp_path: Path,
) -> None:
    schema = __import__("json").loads(
        (
            PRODUCTION_ROOT / "schemas" / "production-channel-record.schema.json"
        ).read_text()
    )
    validator = Draft202012Validator(schema)
    pypi = publish_pypi(
        PyPIPublishRequest.from_path(
            stage="stable",
            distribution="uc-manager-cuda",
            version="0.6.0",
            path=_wheel(tmp_path, "uc-manager-cuda"),
            repository="https://upload.pypi.org/legacy/",
        ),
        FakePyPI(),
    )
    docker = publish_docker_hub(
        DockerHubPublishRequest(
            stage="stable",
            profile_id="cuda130",
            source_reference="ghcr.io/octocat/ucm-cuda@sha256:" + "1" * 64,
            target_reference="docker.io/ucm-preview/ucm-cuda:v0.6.0",
            manifest_digest="sha256:" + "1" * 64,
        ),
        FakeDockerHub(),
    )

    validator.validate(pypi)
    validator.validate(docker)
