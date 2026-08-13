"""Fail-closed GHCR image/index/Chart publication with fresh readback."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .common import (
    ProductionError,
    canonical_bytes,
    decode_json,
    require_exact_keys,
    require_lower_commit_sha,
    require_sha256_digest,
    require_string,
    sha256_envelope,
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_REPOSITORY = re.compile(
    r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+",
    re.ASCII,
)
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", re.ASCII)
_SPEC = re.compile(r"(cuda130|cann900-a2|cann900-a3)-(amd64|arm64)", re.ASCII)
_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
_OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
_OCI_LAYER_PREFIX = "application/vnd.oci.image.layer.v1"
_HELM_CONFIG = "application/vnd.cncf.helm.config.v1+json"
_HELM_LAYER = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
_MAX_JSON = 16 * 1024 * 1024
_MAX_BLOB = 4 * 1024 * 1024 * 1024


class AuthorizationDenied(ProductionError):
    """Registry authentication or authorization was explicitly denied."""


class RegistryResponseLost(ProductionError):
    """A write may have completed but its response was not observed."""


class VisibilityConfigurationRequired(ProductionError):
    """Content exists and authenticates, but the package is not yet public."""

    def __init__(self, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


class RegistryTransport(Protocol):
    def digest(self, reference: str, *, anonymous: bool = False) -> str | None: ...

    def manifest(self, reference: str, *, anonymous: bool = False) -> bytes: ...

    def blob(self, reference: str, *, anonymous: bool = False) -> bytes: ...

    def push_layout(
        self, layout: Path, target: str, *, index: bool = False
    ) -> None: ...

    def tag(self, digest_reference: str, tag: str) -> None: ...

    def helm_push(self, chart: Path, repository: str) -> None: ...


def _repository(value: object) -> str:
    repository = require_string(value, "registry repository")
    if _REPOSITORY.fullmatch(repository) is None:
        raise ProductionError("registry repository must be a canonical GHCR path")
    return repository


def _tag(value: object) -> str:
    tag = require_string(value, "registry tag")
    if _TAG.fullmatch(tag) is None:
        raise ProductionError("registry reference tag is not canonical")
    return tag


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ProductionError(f"{label} must be sha256:<64 lowercase hex>")
    return value


def _reference(value: object, *, require_digest: bool | None = None) -> str:
    reference = require_string(value, "registry reference")
    if "@" in reference:
        parts = reference.split("@")
        if len(parts) != 2:
            raise ProductionError("registry reference has multiple digest separators")
        repository = _repository(parts[0])
        digest = _digest(parts[1], "registry reference digest")
        if require_digest is False:
            raise ProductionError("registry reference must be tagged")
        return f"{repository}@{digest}"
    slash = reference.rfind("/")
    colon = reference.rfind(":")
    if colon <= slash:
        raise ProductionError("registry reference must include a tag or digest")
    repository = _repository(reference[:colon])
    tag = _tag(reference[colon + 1 :])
    if require_digest is True:
        raise ProductionError("registry reference must use a content digest")
    return f"{repository}:{tag}"


def _reference_repository(reference: str) -> str:
    if "@" in reference:
        return reference.rsplit("@", 1)[0]
    slash = reference.rfind("/")
    colon = reference.rfind(":")
    return reference[:colon] if colon > slash else reference


def _bounded(raw: bytes, label: str, limit: int) -> bytes:
    if not isinstance(raw, bytes):
        raise ProductionError(f"{label} must return bytes")
    if len(raw) > limit:
        raise ProductionError(f"{label} exceeds the byte limit")
    return raw


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    value = decode_json(_bounded(raw, label, _MAX_JSON), label)
    if not isinstance(value, dict):
        raise ProductionError(f"{label} must contain a JSON object")
    return value


def _descriptor(
    value: object,
    *,
    label: str,
    media_types: set[str] | None = None,
    require_platform: bool = False,
    allow_annotations: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionError(f"{label} descriptor must be an object")
    allowed = {"mediaType", "digest", "size"}
    if require_platform:
        allowed.add("platform")
    if require_platform or allow_annotations:
        allowed.add("annotations")
    if set(value) - allowed or not {"mediaType", "digest", "size"} <= set(value):
        raise ProductionError(f"{label} descriptor fields are invalid")
    media_type = value["mediaType"]
    if not isinstance(media_type, str) or (
        media_types is not None and media_type not in media_types
    ):
        raise ProductionError(f"{label} descriptor media type is invalid")
    _digest(value["digest"], f"{label} descriptor digest")
    if type(value["size"]) is not int or not 1 <= value["size"] <= _MAX_BLOB:
        raise ProductionError(f"{label} descriptor size is invalid")
    if require_platform:
        platform = value.get("platform")
        if not isinstance(platform, dict) or set(platform) != {"os", "architecture"}:
            raise ProductionError(f"{label} descriptor platform is invalid")
        if platform["os"] != "linux" or platform["architecture"] not in {
            "amd64",
            "arm64",
        }:
            raise ProductionError(f"{label} descriptor platform is invalid")
    if require_platform or allow_annotations:
        annotations = value.get("annotations")
        if annotations is not None and (
            not isinstance(annotations, dict)
            or any(
                not isinstance(key, str) or not isinstance(item, str)
                for key, item in annotations.items()
            )
        ):
            raise ProductionError(f"{label} descriptor annotations are invalid")
    return dict(value)


def _verify_bytes(raw: bytes, descriptor: Mapping[str, Any], label: str) -> None:
    if len(raw) != descriptor["size"]:
        raise ProductionError(f"{label} bytes differ from descriptor size")
    if "sha256:" + hashlib.sha256(raw).hexdigest() != descriptor["digest"]:
        raise ProductionError(f"{label} bytes differ from descriptor digest")


def _read_blob(
    transport: RegistryTransport,
    repository: str,
    descriptor: Mapping[str, Any],
    *,
    label: str,
    anonymous: bool,
) -> bytes:
    raw = _bounded(
        transport.blob(f"{repository}@{descriptor['digest']}", anonymous=anonymous),
        label,
        _MAX_BLOB,
    )
    _verify_bytes(raw, descriptor, label)
    return raw


def _single_readback(
    reference: str, transport: RegistryTransport, *, anonymous: bool
) -> dict[str, Any]:
    canonical = _reference(reference)
    digest = transport.digest(canonical, anonymous=anonymous)
    if digest is None:
        raise ProductionError(
            f"registry reference is absent during readback: {canonical}"
        )
    digest = _digest(digest, "registry readback digest")
    repository = _reference_repository(canonical)
    digest_reference = f"{repository}@{digest}"
    manifest_raw = _bounded(
        transport.manifest(digest_reference, anonymous=anonymous),
        "registry manifest",
        _MAX_JSON,
    )
    if "sha256:" + hashlib.sha256(manifest_raw).hexdigest() != digest:
        raise ProductionError("registry manifest raw bytes differ from resolved digest")
    manifest = _json_object(manifest_raw, "registry manifest")
    if manifest.get("schemaVersion") != 2:
        raise ProductionError("registry manifest schemaVersion must be 2")
    media_type = manifest.get("mediaType")
    result: dict[str, Any] = {
        "reference": canonical,
        "digest": digest,
        "media_type": media_type,
        "manifest_size": len(manifest_raw),
    }
    if media_type == _OCI_MANIFEST:
        allowed = {"schemaVersion", "mediaType", "config", "layers", "annotations"}
        if set(manifest) - allowed or not {
            "schemaVersion",
            "mediaType",
            "config",
            "layers",
        } <= set(manifest):
            raise ProductionError("OCI manifest fields are invalid")
        config_descriptor = _descriptor(
            manifest["config"],
            label="registry config",
            media_types={_OCI_CONFIG, _HELM_CONFIG},
        )
        config_raw = _read_blob(
            transport,
            repository,
            config_descriptor,
            label="registry config",
            anonymous=anonymous,
        )
        config = _json_object(config_raw, "registry config")
        layer_values = manifest["layers"]
        if not isinstance(layer_values, list) or not layer_values:
            raise ProductionError("OCI manifest must contain non-empty layers")
        layers: list[dict[str, Any]] = []
        for position, value in enumerate(layer_values):
            layer = _descriptor(
                value,
                label=f"registry layer {position}",
                allow_annotations=True,
            )
            if not (
                layer["mediaType"].startswith(_OCI_LAYER_PREFIX)
                or layer["mediaType"] == _HELM_LAYER
            ):
                raise ProductionError("registry layer media type is invalid")
            raw = _read_blob(
                transport,
                repository,
                layer,
                label=f"registry layer {position}",
                anonymous=anonymous,
            )
            layers.append({**layer, "blob_sha256": _digest(raw_digest(raw), "layer")})
        annotations = manifest.get("annotations", {})
        if not isinstance(annotations, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in annotations.items()
        ):
            raise ProductionError("registry manifest annotations are invalid")
        result.update(
            {
                "manifest": manifest,
                "config": {
                    **config_descriptor,
                    "json": config,
                    "blob_sha256": raw_digest(config_raw),
                },
                "layers": layers,
                "annotations": annotations,
            }
        )
    elif media_type == _OCI_INDEX:
        if set(manifest) - {
            "schemaVersion",
            "mediaType",
            "manifests",
            "annotations",
        } or not {"schemaVersion", "mediaType", "manifests"} <= set(manifest):
            raise ProductionError("OCI index fields are invalid")
        descriptors = manifest["manifests"]
        if not isinstance(descriptors, list) or not descriptors:
            raise ProductionError("OCI index must contain child manifests")
        children: list[dict[str, Any]] = []
        for position, value in enumerate(descriptors):
            child = _descriptor(
                value,
                label=f"index child {position}",
                media_types={_OCI_MANIFEST},
                require_platform=True,
            )
            raw = _bounded(
                transport.manifest(
                    f"{repository}@{child['digest']}", anonymous=anonymous
                ),
                f"index child {position}",
                _MAX_JSON,
            )
            _verify_bytes(raw, child, f"index child {position}")
            parsed = _json_object(raw, f"index child {position}")
            if parsed.get("mediaType") != _OCI_MANIFEST:
                raise ProductionError("index child does not reopen as an OCI manifest")
            children.append(child)
        annotations = manifest.get("annotations", {})
        if not isinstance(annotations, dict):
            raise ProductionError("OCI index annotations are invalid")
        result.update(
            {"manifest": manifest, "children": children, "annotations": annotations}
        )
    else:
        raise ProductionError("registry manifest media type is unsupported")
    result["closure_sha256"] = (
        "sha256:" + hashlib.sha256(canonical_bytes(result)).hexdigest()
    )
    return result


def raw_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def readback_reference(
    reference: str, visibility: str, transport: RegistryTransport
) -> dict[str, Any]:
    """Authenticate full closure, then prove public success or private denial."""

    if visibility not in {"private", "public"}:
        raise ProductionError("registry visibility must be private or public")
    authenticated = _single_readback(reference, transport, anonymous=False)
    try:
        anonymous = _single_readback(reference, transport, anonymous=True)
    except AuthorizationDenied:
        if visibility == "public":
            raise
        anonymous = {"status": "authorization-denied"}
    else:
        if visibility == "private":
            raise ProductionError("private registry reference is anonymously readable")
        if anonymous != authenticated:
            raise ProductionError(
                "anonymous registry closure differs from authenticated readback"
            )
    return sha256_envelope(
        {
            "kind": "ucm-production-registry-readback",
            "schema_version": 1,
            "reference": _reference(reference),
            "visibility": visibility,
            "authenticated_readback": authenticated,
            "anonymous_readback": anonymous,
        }
    )


def _layout_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        raise ProductionError("OCI layout must be a real directory")
    result: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ProductionError("OCI layout contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ProductionError("OCI layout contains a non-regular member")
        relative = path.relative_to(root).as_posix()
        if any(ord(char) < 32 or ord(char) == 127 for char in relative):
            raise ProductionError("OCI layout contains a control character path")
        result[relative] = path
    return result


def _layout_blob(root: Path, descriptor: Mapping[str, Any], label: str) -> bytes:
    digest = _digest(descriptor["digest"], f"{label} digest")
    path = root / "blobs" / "sha256" / digest.removeprefix("sha256:")
    if not path.is_file() or path.is_symlink():
        raise ProductionError(f"{label} blob is absent from OCI layout")
    raw = _bounded(path.read_bytes(), label, _MAX_BLOB)
    _verify_bytes(raw, descriptor, label)
    return raw


def _validate_member_layout(request: MemberPublishRequest) -> dict[str, Any]:
    root = request.layout
    files = _layout_files(root)
    if "oci-layout" not in files or "index.json" not in files:
        raise ProductionError("OCI layout markers are missing")
    if _json_object(files["oci-layout"].read_bytes(), "OCI layout marker") != {
        "imageLayoutVersion": "1.0.0"
    }:
        raise ProductionError("OCI layout marker is invalid")
    index = _json_object(files["index.json"].read_bytes(), "OCI layout index")
    if (
        index.get("schemaVersion") != 2
        or index.get("mediaType") != _OCI_INDEX
        or not isinstance(index.get("manifests"), list)
        or len(index["manifests"]) != 1
    ):
        raise ProductionError("member OCI layout index is invalid")
    descriptor = _descriptor(
        index["manifests"][0],
        label="member layout manifest",
        media_types={_OCI_MANIFEST},
        require_platform=True,
    )
    expected_arch = request.spec_id.rsplit("-", 1)[1]
    if descriptor["platform"] != {"os": "linux", "architecture": expected_arch}:
        raise ProductionError("member OCI layout platform differs from spec")
    manifest_raw = _layout_blob(root, descriptor, "member manifest")
    manifest = _json_object(manifest_raw, "member manifest")
    if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") != _OCI_MANIFEST:
        raise ProductionError("member manifest identity is invalid")
    config_descriptor = _descriptor(
        manifest.get("config"), label="member config", media_types={_OCI_CONFIG}
    )
    config_raw = _layout_blob(root, config_descriptor, "member config")
    config = _json_object(config_raw, "member config")
    layer_values = manifest.get("layers")
    if not isinstance(layer_values, list) or not layer_values:
        raise ProductionError("member manifest layers are invalid")
    layers: list[dict[str, Any]] = []
    for position, value in enumerate(layer_values):
        layer = _descriptor(
            value,
            label=f"member layer {position}",
            allow_annotations=True,
        )
        if not layer["mediaType"].startswith(_OCI_LAYER_PREFIX):
            raise ProductionError("member layer media type is invalid")
        _layout_blob(root, layer, f"member layer {position}")
        layers.append(layer)
    closure = request.closure
    required_closure = {
        "spec_id",
        "platform",
        "source_sha",
        "manifest_digest",
        "manifest_size",
        "config_digest",
        "config_size",
        "layers",
        "annotations",
    }
    require_exact_keys(closure, required_closure, "member closure")
    source_sha = require_lower_commit_sha(closure["source_sha"], "member source SHA")
    rootfs = config.get("rootfs")
    labels = (
        config.get("config", {}).get("Labels")
        if isinstance(config.get("config"), dict)
        else None
    )
    if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
        raise ProductionError("member config rootfs is invalid")
    diff_ids = rootfs.get("diff_ids")
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
        raise ProductionError("member config diff-ID closure is invalid")
    expected_layers = [
        {
            "digest": layer["digest"],
            "diff_id": _digest(diff_ids[position], f"member diff-ID {position}"),
            "size": layer["size"],
        }
        for position, layer in enumerate(layers)
    ]
    if (
        closure["spec_id"] != request.spec_id
        or closure["platform"] != f"linux/{expected_arch}"
        or closure["manifest_digest"] != descriptor["digest"]
        or closure["manifest_size"] != descriptor["size"]
        or closure["config_digest"] != config_descriptor["digest"]
        or closure["config_size"] != config_descriptor["size"]
        or closure["layers"] != expected_layers
        or closure["annotations"] != manifest.get("annotations", {})
        or not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != source_sha
        or labels.get("io.ucm.release.spec-id") != request.spec_id
    ):
        raise ProductionError("member OCI bytes differ from candidate closure")
    expected_files = {
        "oci-layout",
        "index.json",
        *(
            f"blobs/sha256/{value.removeprefix('sha256:')}"
            for value in [
                descriptor["digest"],
                config_descriptor["digest"],
                *(layer["digest"] for layer in layers),
            ]
        ),
    }
    if set(files) != expected_files:
        raise ProductionError("member OCI layout exact file set differs")
    return {
        "platform": closure["platform"],
        "source_sha": source_sha,
        "manifest_digest": descriptor["digest"],
        "manifest_size": descriptor["size"],
        "config_digest": config_descriptor["digest"],
        "config_size": config_descriptor["size"],
        "layers": expected_layers,
        "annotations": manifest.get("annotations", {}),
    }


@dataclass(frozen=True)
class MemberPublishRequest:
    stage: str
    spec_id: str
    repository: str
    tag: str
    layout: Path
    closure: dict[str, Any]
    visibility: str

    def __post_init__(self) -> None:
        if self.stage not in {"draft", "rc", "stable", "hotfix"}:
            raise ProductionError("member stage is invalid")
        if _SPEC.fullmatch(self.spec_id) is None:
            raise ProductionError("member spec_id is invalid")
        object.__setattr__(self, "repository", _repository(self.repository))
        object.__setattr__(self, "tag", _tag(self.tag))
        object.__setattr__(self, "layout", Path(self.layout))
        if not isinstance(self.closure, dict):
            raise ProductionError("member closure must be an object")
        if self.visibility not in {"private", "public"}:
            raise ProductionError("member visibility is invalid")

    @property
    def tagged_reference(self) -> str:
        return f"{self.repository}:{self.tag}"


def _write_operation(
    operations: list[dict[str, str]], action: str, reference: str, outcome: str
) -> None:
    operations.append({"action": action, "reference": reference, "outcome": outcome})


def _push_layout(
    transport: RegistryTransport,
    layout: Path,
    repository: str,
    digest: str,
    operations: list[dict[str, str]],
    *,
    index: bool,
) -> str:
    target = f"{repository}@{digest}"
    observed = transport.digest(target)
    if observed is not None and observed != digest:
        raise ProductionError("digest coordinate returned impossible content drift")
    if observed == digest:
        return "reuse"
    outcome = "completed"
    try:
        transport.push_layout(layout, target, index=index)
    except RegistryResponseLost:
        if transport.digest(target) != digest:
            raise ProductionError(
                "registry push response was lost without exact readback"
            ) from None
        outcome = "response-loss-recovered"
    if transport.digest(target) != digest:
        raise ProductionError("registry push post-write digest readback drifted")
    _write_operation(
        operations, "push-index" if index else "push-member", target, outcome
    )
    return "create"


def _tag_digest(
    transport: RegistryTransport,
    repository: str,
    tag: str,
    digest: str,
    operations: list[dict[str, str]],
) -> str:
    tagged = f"{repository}:{tag}"
    observed = transport.digest(tagged)
    if observed is not None and observed != digest:
        raise ProductionError(f"registry tag collision for {tagged}")
    if observed == digest:
        return "reuse"
    # Fresh prewrite read closes the gap between planning and mutation.
    if transport.digest(tagged) is not None:
        raise ProductionError(f"registry tag changed before create: {tagged}")
    outcome = "completed"
    try:
        transport.tag(f"{repository}@{digest}", tag)
    except RegistryResponseLost:
        if transport.digest(tagged) != digest:
            raise ProductionError(
                "registry tag response was lost without exact readback"
            ) from None
        outcome = "response-loss-recovered"
    if transport.digest(tagged) != digest:
        raise ProductionError("registry tag post-write digest readback drifted")
    _write_operation(operations, "create-tag", tagged, outcome)
    return "create"


def _visibility_record(
    base: dict[str, Any], reference: str, visibility: str, transport: RegistryTransport
) -> dict[str, Any]:
    try:
        readback = readback_reference(reference, visibility, transport)
    except AuthorizationDenied:
        if visibility != "public":
            raise
        authenticated = _single_readback(reference, transport, anonymous=False)
        record = sha256_envelope(
            {
                **base,
                "status": "visibility-configuration-required",
                "authenticated_readback": authenticated,
                "anonymous_readback": {"status": "authorization-denied"},
            }
        )
        raise VisibilityConfigurationRequired(
            f"public visibility configuration is required for {reference}", record
        ) from None
    return sha256_envelope(
        {
            **base,
            "status": "complete",
            "authenticated_readback": readback["authenticated_readback"],
            "anonymous_readback": readback["anonymous_readback"],
        }
    )


def publish_member(
    request: MemberPublishRequest, transport: RegistryTransport
) -> dict[str, Any]:
    """Create/reuse one content-addressed OCI member and prove visibility."""

    closure = _validate_member_layout(request)
    expected_digest = closure["manifest_digest"]
    observed = transport.digest(request.tagged_reference)
    if observed is not None and observed != expected_digest:
        raise ProductionError(f"registry tag collision for {request.tagged_reference}")
    operations: list[dict[str, str]] = []
    if observed == expected_digest:
        decision = "reuse"
    else:
        _push_layout(
            transport,
            request.layout,
            request.repository,
            expected_digest,
            operations,
            index=False,
        )
        _tag_digest(
            transport,
            request.repository,
            request.tag,
            expected_digest,
            operations,
        )
        decision = "create"
    base = {
        "kind": "ucm-production-channel-record",
        "schema_version": 1,
        "channel": "ghcr-member",
        "stage": request.stage,
        "spec_id": request.spec_id,
        "repository": request.repository,
        "reference": request.tagged_reference,
        "visibility": request.visibility,
        "decision": decision,
        **closure,
        "operations": operations,
    }
    return _visibility_record(
        base, request.tagged_reference, request.visibility, transport
    )


@dataclass(frozen=True)
class IndexPublishRequest:
    stage: str
    profile_id: str
    repository: str
    tag: str
    source_sha: str
    members: tuple[dict[str, Any], dict[str, Any]]
    visibility: str

    def __post_init__(self) -> None:
        if self.stage not in {"draft", "rc", "stable", "hotfix"}:
            raise ProductionError("index stage is invalid")
        if self.profile_id not in {"cuda130", "cann900-a2", "cann900-a3"}:
            raise ProductionError("index profile_id is invalid")
        object.__setattr__(self, "repository", _repository(self.repository))
        object.__setattr__(self, "tag", _tag(self.tag))
        require_lower_commit_sha(self.source_sha, "index source SHA")
        if not isinstance(self.members, tuple) or len(self.members) != 2:
            raise ProductionError("index requires exactly two member records")
        if self.visibility not in {"private", "public"}:
            raise ProductionError("index visibility is invalid")

    @property
    def tagged_reference(self) -> str:
        return f"{self.repository}:{self.tag}"


def _index_members(request: IndexPublishRequest) -> list[dict[str, Any]]:
    by_platform: dict[str, dict[str, Any]] = {}
    for item in request.members:
        if not isinstance(item, dict):
            raise ProductionError("index members must be channel records")
        platform = item.get("platform")
        expected_prefix = request.profile_id + "-"
        if (
            item.get("kind") != "ucm-production-channel-record"
            or item.get("channel") != "ghcr-member"
            or item.get("status") != "complete"
            or item.get("stage") != request.stage
            or item.get("repository") != request.repository
            or item.get("source_sha") != request.source_sha
            or not str(item.get("spec_id", "")).startswith(expected_prefix)
            or platform not in {"linux/amd64", "linux/arm64"}
            or platform in by_platform
        ):
            raise ProductionError("index members do not form one exact family closure")
        by_platform[platform] = item
    if set(by_platform) != {"linux/amd64", "linux/arm64"}:
        raise ProductionError("index members must be exact amd64 and arm64 records")
    return [by_platform["linux/amd64"], by_platform["linux/arm64"]]


def _materialize_index(
    root: Path, request: IndexPublishRequest, members: list[dict[str, Any]]
) -> tuple[str, bytes, list[dict[str, Any]]]:
    descriptors = [
        {
            "mediaType": _OCI_MANIFEST,
            "digest": item["manifest_digest"],
            "size": item["manifest_size"],
            "platform": {
                "os": "linux",
                "architecture": item["platform"].split("/", 1)[1],
            },
            "annotations": {"io.ucm.release.spec-id": item["spec_id"]},
        }
        for item in members
    ]
    manifest = {
        "schemaVersion": 2,
        "mediaType": _OCI_INDEX,
        "manifests": descriptors,
        "annotations": {
            "io.ucm.release.profile-id": request.profile_id,
            "io.ucm.release.source-sha": request.source_sha,
        },
    }
    raw = canonical_bytes(manifest)
    digest = raw_digest(raw)
    blobs = root / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (root / "oci-layout").write_bytes(b'{"imageLayoutVersion":"1.0.0"}\n')
    (blobs / digest.removeprefix("sha256:")).write_bytes(raw)
    index = {
        "schemaVersion": 2,
        "mediaType": _OCI_INDEX,
        "manifests": [
            {
                "mediaType": _OCI_INDEX,
                "digest": digest,
                "size": len(raw),
            }
        ],
    }
    (root / "index.json").write_bytes(canonical_bytes(index) + b"\n")
    return digest, raw, descriptors


def publish_index(
    request: IndexPublishRequest, transport: RegistryTransport
) -> dict[str, Any]:
    """Create/reuse the sole ordered amd64/arm64 index and read it back."""

    members = _index_members(request)
    operations: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="ucm-production-index-") as directory:
        root = Path(directory)
        digest, _raw, descriptors = _materialize_index(root, request, members)
        observed = transport.digest(request.tagged_reference)
        if observed is not None and observed != digest:
            raise ProductionError(
                f"registry tag collision for {request.tagged_reference}"
            )
        if observed == digest:
            decision = "reuse"
        else:
            _push_layout(
                transport,
                root,
                request.repository,
                digest,
                operations,
                index=True,
            )
            _tag_digest(
                transport,
                request.repository,
                request.tag,
                digest,
                operations,
            )
            decision = "create"
    base = {
        "kind": "ucm-production-channel-record",
        "schema_version": 1,
        "channel": "ghcr-index",
        "stage": request.stage,
        "profile_id": request.profile_id,
        "repository": request.repository,
        "reference": request.tagged_reference,
        "visibility": request.visibility,
        "decision": decision,
        "source_sha": request.source_sha,
        "index_digest": digest,
        "members": [
            {
                "spec_id": item["spec_id"],
                "platform": item["platform"],
                "manifest_digest": item["manifest_digest"],
            }
            for item in members
        ],
        "index_descriptors": descriptors,
        "operations": operations,
    }
    return _visibility_record(
        base, request.tagged_reference, request.visibility, transport
    )


@dataclass(frozen=True)
class ChartPublishRequest:
    stage: str
    name: str
    version: str
    chart: Path
    helm_repository: str
    reference: str
    file_sha256: str
    visibility: str

    def __post_init__(self) -> None:
        if self.stage not in {"rc", "stable", "hotfix"}:
            raise ProductionError("Chart OCI stage must be rc, stable, or hotfix")
        if self.name != "unified-cache-pd":
            raise ProductionError("Chart name is invalid")
        if (
            re.fullmatch(
                r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                r"(?:-rc\.[1-9][0-9]*)?",
                self.version,
                re.ASCII,
            )
            is None
        ):
            raise ProductionError("Chart version is invalid")
        object.__setattr__(self, "chart", Path(self.chart))
        repository = require_string(self.helm_repository, "Helm OCI repository")
        if not repository.startswith("oci://"):
            raise ProductionError("Helm repository must use oci://")
        _repository(repository.removeprefix("oci://") + "/" + self.name)
        expected = f"{repository.removeprefix('oci://')}/{self.name}:{self.version}"
        if _reference(self.reference, require_digest=False) != expected:
            raise ProductionError(
                "Chart reference differs from Helm repository/version"
            )
        require_sha256_digest(self.file_sha256, "Chart file SHA256")
        if self.visibility != "public":
            raise ProductionError("Chart OCI visibility must be public")


def _verify_chart_readback(
    request: ChartPublishRequest, readback: dict[str, Any]
) -> None:
    authenticated = readback["authenticated_readback"]
    if authenticated.get("media_type") != _OCI_MANIFEST:
        raise ProductionError("Chart readback media type differs")
    config = authenticated.get("config", {})
    if config.get("mediaType") != _HELM_CONFIG or config.get("json") != {
        "name": request.name,
        "version": request.version,
    }:
        raise ProductionError("Chart readback config identity differs")
    layers = authenticated.get("layers")
    if (
        not isinstance(layers, list)
        or len(layers) != 1
        or layers[0].get("mediaType") != _HELM_LAYER
        or layers[0].get("digest") != request.file_sha256
    ):
        raise ProductionError("Chart readback content layer differs")


def publish_chart(
    request: ChartPublishRequest, transport: RegistryTransport
) -> dict[str, Any]:
    """Push or reuse an immutable Helm OCI Chart after exact layer comparison."""

    if not request.chart.is_file() or request.chart.is_symlink():
        raise ProductionError("Chart package must be a regular file")
    if raw_digest(request.chart.read_bytes()) != request.file_sha256:
        raise ProductionError("Chart package bytes differ from sealed SHA256")
    operations: list[dict[str, str]] = []
    observed = transport.digest(request.reference)
    if observed is not None:
        try:
            existing = readback_reference(request.reference, "public", transport)
            _verify_chart_readback(request, existing)
        except (AuthorizationDenied, ProductionError) as error:
            raise ProductionError(f"Chart coordinate conflict: {error}") from None
        decision = "reuse"
        readback = existing
    else:
        if transport.digest(request.reference) is not None:
            raise ProductionError("Chart coordinate changed before create")
        outcome = "completed"
        try:
            transport.helm_push(request.chart, request.helm_repository)
        except RegistryResponseLost:
            if transport.digest(request.reference) is None:
                raise ProductionError(
                    "Chart push response lost without remote object"
                ) from None
            outcome = "response-loss-recovered"
        _write_operation(operations, "helm-push", request.reference, outcome)
        try:
            readback = readback_reference(request.reference, "public", transport)
            _verify_chart_readback(request, readback)
        except AuthorizationDenied:
            authenticated = _single_readback(
                request.reference, transport, anonymous=False
            )
            partial = sha256_envelope(
                {
                    "kind": "ucm-production-channel-record",
                    "schema_version": 1,
                    "channel": "chart-oci",
                    "status": "visibility-configuration-required",
                    "stage": request.stage,
                    "name": request.name,
                    "version": request.version,
                    "reference": request.reference,
                    "visibility": request.visibility,
                    "decision": "create",
                    "file_sha256": request.file_sha256,
                    "authenticated_readback": authenticated,
                    "anonymous_readback": {"status": "authorization-denied"},
                    "operations": operations,
                }
            )
            raise VisibilityConfigurationRequired(
                f"public visibility configuration is required for {request.reference}",
                partial,
            ) from None
        decision = "create"
    authenticated = readback["authenticated_readback"]
    return sha256_envelope(
        {
            "kind": "ucm-production-channel-record",
            "schema_version": 1,
            "channel": "chart-oci",
            "status": "complete",
            "stage": request.stage,
            "name": request.name,
            "version": request.version,
            "reference": request.reference,
            "visibility": request.visibility,
            "decision": decision,
            "file_sha256": request.file_sha256,
            "manifest_digest": authenticated["digest"],
            "config_digest": authenticated["config"]["digest"],
            "layer_digest": authenticated["layers"][0]["digest"],
            "authenticated_readback": authenticated,
            "anonymous_readback": readback["anonymous_readback"],
            "operations": operations,
        }
    )


_Execute = Callable[[tuple[str, ...], bool], tuple[int, bytes, bytes]]


class CommandRegistryTransport:
    """Closed argv-only adapter for the pinned crane and Helm commands."""

    def __init__(self, *, execute: _Execute | None = None) -> None:
        self._execute = execute or self._subprocess_execute

    @staticmethod
    def _subprocess_execute(
        argv: tuple[str, ...], anonymous: bool
    ) -> tuple[int, bytes, bytes]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "DOCKER_CONFIG",
                "HOME",
                "PATH",
                "SSL_CERT_DIR",
                "SSL_CERT_FILE",
                "TMPDIR",
            }
        }
        if anonymous:
            with tempfile.TemporaryDirectory(
                prefix="ucm-production-anonymous-registry-"
            ) as directory:
                (Path(directory) / "config.json").write_bytes(b'{"auths":{}}\n')
                result = subprocess.run(
                    argv,
                    env={**environment, "DOCKER_CONFIG": directory},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
        else:
            result = subprocess.run(
                argv,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        return result.returncode, result.stdout, result.stderr

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        anonymous: bool = False,
        missing_ok: bool = False,
        response_loss_possible: bool = False,
    ) -> bytes:
        if not argv or argv[0] not in {"crane", "helm"}:
            raise ProductionError("registry transport executable is not approved")
        if any(
            not isinstance(item, str)
            or not item
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
            for item in argv
        ):
            raise ProductionError("registry transport argv is malformed")
        try:
            returncode, stdout, stderr = self._execute(argv, anonymous)
        except (OSError, TimeoutError) as error:
            if response_loss_possible:
                raise RegistryResponseLost(str(error)) from None
            raise ProductionError(
                f"registry command failed to execute: {error}"
            ) from None
        stdout = _bounded(stdout, "registry stdout", _MAX_BLOB)
        stderr = _bounded(stderr, "registry stderr", _MAX_JSON)
        if returncode == 0:
            return stdout
        detail = (stderr + b"\n" + stdout).decode("utf-8", errors="replace")
        lowered = detail.lower()
        if any(marker in lowered for marker in ("unauthorized", "denied", "forbidden")):
            raise AuthorizationDenied("registry authorization denied")
        if missing_ok and any(
            marker in lowered
            for marker in ("manifest unknown", "manifest_unknown", "not found", "404")
        ):
            return b""
        if response_loss_possible and any(
            marker in lowered
            for marker in (
                "timeout",
                "connection reset",
                "unexpected eof",
                "broken pipe",
            )
        ):
            raise RegistryResponseLost("registry write response was lost")
        raise ProductionError(
            f"registry command failed: {detail.strip() or returncode}"
        )

    def digest(self, reference: str, *, anonymous: bool = False) -> str | None:
        canonical = _reference(reference)
        raw = self._run(
            ("crane", "digest", canonical),
            anonymous=anonymous,
            missing_ok=True,
        )
        if not raw:
            return None
        try:
            value = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise ProductionError("crane digest output is not ASCII") from None
        return _digest(value, "crane digest output")

    def manifest(self, reference: str, *, anonymous: bool = False) -> bytes:
        canonical = _reference(reference, require_digest=True)
        return self._run(("crane", "manifest", canonical), anonymous=anonymous)

    def blob(self, reference: str, *, anonymous: bool = False) -> bytes:
        canonical = _reference(reference, require_digest=True)
        return self._run(("crane", "blob", canonical), anonymous=anonymous)

    def push_layout(self, layout: Path, target: str, *, index: bool = False) -> None:
        canonical = _reference(target, require_digest=True)
        root = Path(layout)
        if not root.is_dir() or root.is_symlink():
            raise ProductionError("crane push layout must be a real directory")
        argv = ("crane", "push", str(root.resolve()), canonical)
        if index:
            argv = (*argv, "--index")
        self._run(argv, response_loss_possible=True)

    def tag(self, digest_reference: str, tag: str) -> None:
        canonical = _reference(digest_reference, require_digest=True)
        self._run(("crane", "tag", canonical, _tag(tag)), response_loss_possible=True)

    def helm_push(self, chart: Path, repository: str) -> None:
        path = Path(chart)
        if not path.is_file() or path.is_symlink() or path.suffix != ".tgz":
            raise ProductionError("Helm push requires one regular tgz")
        target = require_string(repository, "Helm repository")
        if not target.startswith("oci://"):
            raise ProductionError("Helm repository must use oci://")
        _repository(target.removeprefix("oci://") + "/placeholder")
        self._run(
            ("helm", "push", str(path.resolve()), target),
            response_loss_possible=True,
        )
