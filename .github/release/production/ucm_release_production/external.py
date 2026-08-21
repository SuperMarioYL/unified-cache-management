"""Explicit Stable/Hotfix PyPI and Docker Hub publication adapters."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .common import (
    ProductionError,
    require_lower_commit_sha,
    require_string,
    sha256_envelope,
    verify_envelope,
)
from .config import validate_config
from .tags import TagIntent

_DISTRIBUTIONS = ("uc-manager-cuda", "uc-manager-cann-a2", "uc-manager-cann-a3")
_PROFILES = ("cuda130", "cann900-a2", "cann900-a3")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
    re.ASCII,
)
_PYPI_REPOSITORY = "https://upload.pypi.org/legacy/"
_GHCR_DIGEST = re.compile(
    r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*@sha256:[0-9a-f]{64}",
    re.ASCII,
)
_DOCKER_TAG = re.compile(
    r"docker\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*:v"
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)",
    re.ASCII,
)


@dataclass(frozen=True)
class ExternalCredentials:
    pypi_oidc: bool
    docker_username: str | None
    docker_token_present: bool

    def __post_init__(self) -> None:
        if (
            type(self.pypi_oidc) is not bool
            or type(self.docker_token_present) is not bool
        ):
            raise ProductionError("external credential presence flags must be booleans")
        if self.docker_username is not None:
            username = require_string(self.docker_username, "Docker Hub username")
            if (
                re.fullmatch(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*", username, re.ASCII)
                is None
            ):
                raise ProductionError("Docker Hub username is invalid")


def _environment_status(value: object) -> tuple[str, str]:
    evidence = verify_envelope(
        value,
        kind="ucm-production-environment-evidence",
        schema_version=1,
    )
    status = evidence.get("status")
    if status not in {"passed", "waived-for-preview", "failed", "blocked"}:
        raise ProductionError("production environment status is invalid")
    source_sha = require_lower_commit_sha(
        evidence.get("source_sha"), "production environment source SHA"
    )
    return status, source_sha


def preflight_external_channels(
    intent: TagIntent,
    config_value: object,
    environment_value: object,
    credentials: ExternalCredentials,
) -> dict[str, Any]:
    """Return the only allowed external operations, or fail before any write."""

    config = validate_config(config_value)
    if not isinstance(intent, TagIntent):
        raise ProductionError("external channel intent is invalid")
    if not isinstance(credentials, ExternalCredentials):
        raise ProductionError("external channel credential projection is invalid")
    environment_status, source_sha = _environment_status(environment_value)
    if intent.stage in {"draft", "rc"}:
        return sha256_envelope(
            {
                "kind": "ucm-production-external-preflight",
                "schema_version": 1,
                "stage": intent.stage,
                "tag_name": intent.tag_name,
                "source_sha": source_sha,
                "environment_status": environment_status,
                "channels": {
                    "pypi": "not-applicable",
                    "docker_hub": "not-applicable",
                },
                "operations": [],
            }
        )
    if environment_status != "passed":
        raise ProductionError(
            "Stable/Hotfix external publication requires passed environment evidence"
        )
    external = config["external_channels"]
    if external["pypi"] is not False and not credentials.pypi_oidc:
        raise ProductionError("PyPI GitHub OIDC identity is missing")
    if external["docker_hub"] is not False and (
        credentials.docker_username is None or not credentials.docker_token_present
    ):
        raise ProductionError("Docker Hub Environment credentials are missing")
    operations: list[dict[str, str]] = []
    channels: dict[str, str] = {}
    if external["pypi"] is False:
        channels["pypi"] = "disabled"
    else:
        channels["pypi"] = "enabled"
        for distribution in _DISTRIBUTIONS:
            operations.append(
                {
                    "action": "publish-pypi-oidc",
                    "coordinate": f"pypi://{distribution}/{intent.wheel_version}",
                }
            )
    if external["docker_hub"] is False:
        channels["docker_hub"] = "disabled"
    else:
        channels["docker_hub"] = "enabled"
        docker = external["docker_hub"]
        for profile_id in _PROFILES:
            operations.append(
                {
                    "action": "publish-docker-hub",
                    "coordinate": (
                        f"docker.io/{docker['namespace']}/"
                        f"{docker['repositories'][profile_id]}:{intent.image_tag}"
                    ),
                }
            )
    return sha256_envelope(
        {
            "kind": "ucm-production-external-preflight",
            "schema_version": 1,
            "stage": intent.stage,
            "tag_name": intent.tag_name,
            "source_sha": source_sha,
            "environment_status": environment_status,
            "channels": channels,
            "operations": operations,
        }
    )


class PyPITransport(Protocol):
    def inspect(self, distribution: str, version: str) -> list[dict[str, Any]]: ...

    def upload_oidc(self, path: Path, repository: str) -> None: ...


@dataclass(frozen=True)
class PyPIPublishRequest:
    stage: str
    distribution: str
    version: str
    path: Path
    repository: str
    sha256: str
    size: int

    @classmethod
    def from_path(
        cls,
        *,
        stage: str,
        distribution: str,
        version: str,
        path: Path,
        repository: str,
    ) -> PyPIPublishRequest:
        candidate = Path(path)
        if not candidate.is_file() or candidate.is_symlink():
            raise ProductionError("PyPI distribution must be a regular file")
        raw = candidate.read_bytes()
        return cls(
            stage,
            distribution,
            version,
            candidate,
            repository,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
        )

    def __post_init__(self) -> None:
        if self.stage not in {"stable", "hotfix"}:
            raise ProductionError("PyPI publication is Stable/Hotfix only")
        if (
            self.distribution not in _DISTRIBUTIONS
            or _VERSION.fullmatch(self.version) is None
        ):
            raise ProductionError("PyPI distribution identity is invalid")
        object.__setattr__(self, "path", Path(self.path))
        if self.repository != _PYPI_REPOSITORY:
            raise ProductionError("PyPI repository is not the production endpoint")
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256, re.ASCII) is None:
            raise ProductionError("PyPI file SHA256 is invalid")
        if type(self.size) is not int or self.size < 1:
            raise ProductionError("PyPI file size is invalid")
        prefix = f"{self.distribution.replace('-', '_')}-{self.version}-"
        if not self.path.name.startswith(prefix) or not self.path.name.endswith(".whl"):
            raise ProductionError("PyPI wheel filename differs from coordinate")
        raw = self.path.read_bytes()
        if len(raw) != self.size or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ProductionError("PyPI wheel bytes differ from sealed request")


def _pypi_snapshot(
    request: PyPIPublishRequest, transport: PyPITransport
) -> dict[str, Any] | None:
    values = transport.inspect(request.distribution, request.version)
    if not isinstance(values, list) or any(
        not isinstance(item, dict) for item in values
    ):
        raise ProductionError("PyPI inventory response is malformed")
    names: set[str] = set()
    matches: list[dict[str, Any]] = []
    for value in values:
        item = dict(value)
        if set(item) != {"filename", "sha256", "size"}:
            raise ProductionError("PyPI inventory fields are malformed")
        name = item["filename"]
        if not isinstance(name, str) or name in names:
            raise ProductionError("PyPI coordinate has duplicate files")
        names.add(name)
        if name == request.path.name:
            matches.append(item)
    if not matches:
        return None
    if len(matches) != 1:
        raise ProductionError("PyPI coordinate has duplicate files")
    item = matches[0]
    if item["sha256"] != request.sha256 or item["size"] != request.size:
        raise ProductionError("PyPI coordinate conflict")
    return item


def publish_pypi(
    request: PyPIPublishRequest, transport: PyPITransport
) -> dict[str, Any]:
    """Create or reuse one immutable PyPI wheel through OIDC-only transport."""

    request.__post_init__()
    existing = _pypi_snapshot(request, transport)
    operations: list[dict[str, str]] = []
    if existing is None:
        if _pypi_snapshot(request, transport) is not None:
            raise ProductionError("PyPI coordinate changed before upload")
        transport.upload_oidc(request.path, request.repository)
        existing = _pypi_snapshot(request, transport)
        if existing is None:
            raise ProductionError("PyPI upload has no exact remote readback")
        decision = "create"
        operations.append(
            {
                "action": "upload-oidc",
                "reference": f"pypi://{request.distribution}/{request.version}",
                "outcome": "completed",
            }
        )
    else:
        decision = "reuse"
    return sha256_envelope(
        {
            "kind": "ucm-production-channel-record",
            "schema_version": 1,
            "channel": "pypi",
            "status": "complete",
            "stage": request.stage,
            "reference": f"pypi://{request.distribution}/{request.version}",
            "visibility": "public",
            "decision": decision,
            "distribution": request.distribution,
            "version": request.version,
            "filename": request.path.name,
            "file_sha256": "sha256:" + request.sha256,
            "authenticated_readback": existing,
            "anonymous_readback": existing,
            "operations": operations,
        }
    )


class DockerHubTransport(Protocol):
    def digest(self, reference: str) -> str | None: ...

    def copy(self, source: str, target: str) -> None: ...


@dataclass(frozen=True)
class DockerHubPublishRequest:
    stage: str
    profile_id: str
    source_reference: str
    target_reference: str
    manifest_digest: str

    def __post_init__(self) -> None:
        if self.stage not in {"stable", "hotfix"} or self.profile_id not in _PROFILES:
            raise ProductionError("Docker Hub publication identity is invalid")
        if _GHCR_DIGEST.fullmatch(self.source_reference) is None:
            raise ProductionError("Docker Hub source must be one GHCR digest")
        if _DOCKER_TAG.fullmatch(self.target_reference) is None:
            raise ProductionError("Docker Hub target reference is invalid")
        if _DIGEST.fullmatch(
            self.manifest_digest
        ) is None or not self.source_reference.endswith("@" + self.manifest_digest):
            raise ProductionError("Docker Hub source digest differs")


def publish_docker_hub(
    request: DockerHubPublishRequest, transport: DockerHubTransport
) -> dict[str, Any]:
    """Copy one GHCR digest to an immutable Docker Hub Tag and reread it."""

    request.__post_init__()
    observed = transport.digest(request.target_reference)
    operations: list[dict[str, str]] = []
    if observed is not None and observed != request.manifest_digest:
        raise ProductionError("Docker Hub coordinate conflict")
    if observed is None:
        if transport.digest(request.target_reference) is not None:
            raise ProductionError("Docker Hub coordinate changed before copy")
        transport.copy(request.source_reference, request.target_reference)
        if transport.digest(request.target_reference) != request.manifest_digest:
            raise ProductionError("Docker Hub copy has no exact remote readback")
        decision = "create"
        operations.append(
            {
                "action": "copy-digest",
                "reference": request.target_reference,
                "outcome": "completed",
            }
        )
    else:
        decision = "reuse"
    readback = {
        "reference": request.target_reference,
        "manifest_digest": request.manifest_digest,
    }
    return sha256_envelope(
        {
            "kind": "ucm-production-channel-record",
            "schema_version": 1,
            "channel": "docker-hub",
            "status": "complete",
            "stage": request.stage,
            "reference": request.target_reference,
            "visibility": "public",
            "decision": decision,
            "profile_id": request.profile_id,
            "source_reference": request.source_reference,
            "manifest_digest": request.manifest_digest,
            "authenticated_readback": readback,
            "anonymous_readback": readback,
            "operations": operations,
        }
    )
