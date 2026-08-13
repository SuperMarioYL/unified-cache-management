from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from ucm_release_production.common import ProductionError, canonical_bytes
from ucm_release_production.registry import (
    AuthorizationDenied,
    ChartPublishRequest,
    CommandRegistryTransport,
    IndexPublishRequest,
    MemberPublishRequest,
    RegistryResponseLost,
    VisibilityConfigurationRequired,
    publish_chart,
    publish_index,
    publish_member,
    readback_reference,
)

SOURCE = "1" * 40


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _layout(
    tmp_path: Path, spec_id: str = "cuda130-amd64"
) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / spec_id
    blobs = root / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    layer_raw = b"production-layer-" + spec_id.encode()
    layer_digest = _digest(layer_raw)
    diff_id = _digest(b"uncompressed-" + spec_id.encode())
    config = {
        "architecture": "amd64" if spec_id.endswith("amd64") else "arm64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": SOURCE,
                "io.ucm.release.spec-id": spec_id,
            }
        },
    }
    config_raw = canonical_bytes(config)
    config_digest = _digest(config_raw)
    annotations = {
        "org.opencontainers.image.revision": SOURCE,
        "org.opencontainers.image.version": "v0.6.0rc1",
    }
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": len(config_raw),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": layer_digest,
                "size": len(layer_raw),
            }
        ],
        "annotations": annotations,
    }
    manifest_raw = canonical_bytes(manifest)
    manifest_digest = _digest(manifest_raw)
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": manifest["mediaType"],
                "digest": manifest_digest,
                "size": len(manifest_raw),
                "platform": {
                    "os": "linux",
                    "architecture": config["architecture"],
                },
            }
        ],
    }
    (root / "oci-layout").write_text(
        '{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8"
    )
    (root / "index.json").write_bytes(canonical_bytes(index) + b"\n")
    for digest, raw in (
        (layer_digest, layer_raw),
        (config_digest, config_raw),
        (manifest_digest, manifest_raw),
    ):
        (blobs / digest.removeprefix("sha256:")).write_bytes(raw)
    closure = {
        "spec_id": spec_id,
        "platform": f"linux/{config['architecture']}",
        "source_sha": SOURCE,
        "manifest_digest": manifest_digest,
        "manifest_size": len(manifest_raw),
        "config_digest": config_digest,
        "config_size": len(config_raw),
        "layers": [
            {
                "digest": layer_digest,
                "diff_id": diff_id,
                "size": len(layer_raw),
            }
        ],
        "annotations": annotations,
    }
    return root, closure


class FakeRegistry:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.manifests: dict[str, bytes] = {}
        self.blobs: dict[str, bytes] = {}
        self.private_repositories: set[str] = set()
        self.operations: list[tuple[str, str, bool]] = []
        self.lose_after_push = False
        self.lose_after_tag = False
        self.lose_after_chart = False

    @staticmethod
    def repository(reference: str) -> str:
        if "@" in reference:
            return reference.rsplit("@", 1)[0]
        slash = reference.rfind("/")
        colon = reference.rfind(":")
        return reference[:colon] if colon > slash else reference

    def digest(self, reference: str, *, anonymous: bool = False) -> str | None:
        self.operations.append(("digest", reference, anonymous))
        repository = self.repository(reference)
        if anonymous and repository in self.private_repositories:
            raise AuthorizationDenied("private")
        if "@" in reference:
            digest = reference.rsplit("@", 1)[1]
            return digest if f"{repository}@{digest}" in self.manifests else None
        return self.tags.get(reference)

    def manifest(self, reference: str, *, anonymous: bool = False) -> bytes:
        self.operations.append(("manifest", reference, anonymous))
        repository = self.repository(reference)
        if anonymous and repository in self.private_repositories:
            raise AuthorizationDenied("private")
        return self.manifests[reference]

    def blob(self, reference: str, *, anonymous: bool = False) -> bytes:
        self.operations.append(("blob", reference, anonymous))
        repository = self.repository(reference)
        if anonymous and repository in self.private_repositories:
            raise AuthorizationDenied("private")
        return self.blobs[reference]

    def push_layout(self, layout: Path, target: str, *, index: bool = False) -> None:
        self.operations.append(("push-index" if index else "push", target, False))
        index_value = json.loads((layout / "index.json").read_text())
        descriptor = index_value["manifests"][0]
        digest = descriptor["digest"]
        repository = self.repository(target)
        source_blobs = layout / "blobs" / "sha256"
        for path in source_blobs.iterdir():
            self.blobs[f"{repository}@sha256:{path.name}"] = path.read_bytes()
        self.manifests[f"{repository}@{digest}"] = self.blobs[f"{repository}@{digest}"]
        if self.lose_after_push:
            self.lose_after_push = False
            raise RegistryResponseLost("push response lost")

    def tag(self, digest_reference: str, tag: str) -> None:
        repository, digest = digest_reference.rsplit("@", 1)
        reference = f"{repository}:{tag}"
        self.operations.append(("tag", reference, False))
        self.tags[reference] = digest
        if self.lose_after_tag:
            self.lose_after_tag = False
            raise RegistryResponseLost("tag response lost")

    def helm_push(self, chart: Path, repository: str) -> None:
        self.operations.append(("helm-push", repository, False))
        metadata = json.loads(chart.with_suffix(".metadata.json").read_text())
        chart_raw = chart.read_bytes()
        layer_digest = _digest(chart_raw)
        config_raw = canonical_bytes(
            {"name": metadata["name"], "version": metadata["version"]}
        )
        config_digest = _digest(config_raw)
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.cncf.helm.config.v1+json",
                "digest": config_digest,
                "size": len(config_raw),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip",
                    "digest": layer_digest,
                    "size": len(chart_raw),
                }
            ],
        }
        raw = canonical_bytes(manifest)
        digest = _digest(raw)
        target_repository = repository.removeprefix("oci://") + "/" + metadata["name"]
        reference = f"{target_repository}:{metadata['version']}"
        self.manifests[f"{target_repository}@{digest}"] = raw
        self.blobs[f"{target_repository}@{config_digest}"] = config_raw
        self.blobs[f"{target_repository}@{layer_digest}"] = chart_raw
        self.tags[reference] = digest
        if self.lose_after_chart:
            self.lose_after_chart = False
            raise RegistryResponseLost("helm response lost")


def _member_request(
    tmp_path: Path, *, visibility: str = "public"
) -> MemberPublishRequest:
    layout, closure = _layout(tmp_path)
    return MemberPublishRequest(
        stage="rc",
        spec_id="cuda130-amd64",
        repository="ghcr.io/octocat/ucm-cuda",
        tag="v0.6.0rc1-amd64",
        layout=layout,
        closure=closure,
        visibility=visibility,
    )


def test_member_absent_create_and_public_readback(tmp_path: Path) -> None:
    transport = FakeRegistry()
    request = _member_request(tmp_path)

    record = publish_member(request, transport)

    assert record["status"] == "complete"
    assert record["decision"] == "create"
    assert record["manifest_digest"] == request.closure["manifest_digest"]
    assert (
        record["authenticated_readback"]["digest"] == request.closure["manifest_digest"]
    )
    assert record["anonymous_readback"]["digest"] == request.closure["manifest_digest"]
    assert [item[0] for item in transport.operations].count("push") == 1
    assert [item[0] for item in transport.operations].count("tag") == 1


def test_member_identical_rerun_reuses_without_write(tmp_path: Path) -> None:
    transport = FakeRegistry()
    request = _member_request(tmp_path)
    first = publish_member(request, transport)
    transport.operations.clear()

    second = publish_member(request, transport)

    assert first["manifest_digest"] == second["manifest_digest"]
    assert second["decision"] == "reuse"
    assert not any(item[0] in {"push", "tag"} for item in transport.operations)


def test_member_conflict_blocks_before_any_write(tmp_path: Path) -> None:
    transport = FakeRegistry()
    request = _member_request(tmp_path)
    transport.tags[request.tagged_reference] = "sha256:" + "9" * 64

    with pytest.raises(ProductionError, match="collision"):
        publish_member(request, transport)

    assert not any(item[0] in {"push", "tag"} for item in transport.operations)


@pytest.mark.parametrize("loss", ["push", "tag"])
def test_member_recovers_only_when_fresh_read_proves_response_loss(
    tmp_path: Path, loss: str
) -> None:
    transport = FakeRegistry()
    setattr(transport, f"lose_after_{loss}", True)

    record = publish_member(_member_request(tmp_path), transport)

    assert record["status"] == "complete"
    assert record["decision"] == "create"
    assert any(
        item["outcome"] == "response-loss-recovered" for item in record["operations"]
    )


def test_private_member_requires_authenticated_success_and_anonymous_denial(
    tmp_path: Path,
) -> None:
    transport = FakeRegistry()
    request = _member_request(tmp_path, visibility="private")
    transport.private_repositories.add(request.repository)

    record = publish_member(request, transport)

    assert record["status"] == "complete"
    assert record["anonymous_readback"] == {"status": "authorization-denied"}


def test_public_member_reports_first_package_visibility_hold(tmp_path: Path) -> None:
    transport = FakeRegistry()
    request = _member_request(tmp_path, visibility="public")
    transport.private_repositories.add(request.repository)

    with pytest.raises(VisibilityConfigurationRequired, match="public") as raised:
        publish_member(request, transport)

    assert raised.value.record["status"] == "visibility-configuration-required"
    assert (
        raised.value.record["authenticated_readback"]["digest"]
        == request.closure["manifest_digest"]
    )


def test_authenticated_denial_is_not_treated_as_absence(tmp_path: Path) -> None:
    class Denied(FakeRegistry):
        def digest(self, reference: str, *, anonymous: bool = False) -> str | None:
            raise AuthorizationDenied("token denied")

    with pytest.raises(AuthorizationDenied):
        publish_member(_member_request(tmp_path), Denied())


def test_index_create_closes_exact_ordered_amd64_arm64_members(tmp_path: Path) -> None:
    transport = FakeRegistry()
    amd64 = _member_request(tmp_path / "amd64")
    arm_layout, arm_closure = _layout(tmp_path / "arm64", "cuda130-arm64")
    arm64 = MemberPublishRequest(
        stage="rc",
        spec_id="cuda130-arm64",
        repository=amd64.repository,
        tag="v0.6.0rc1-arm64",
        layout=arm_layout,
        closure=arm_closure,
        visibility="public",
    )
    amd64_record = publish_member(amd64, transport)
    arm64_record = publish_member(arm64, transport)

    record = publish_index(
        IndexPublishRequest(
            stage="rc",
            profile_id="cuda130",
            repository=amd64.repository,
            tag="v0.6.0rc1",
            source_sha=SOURCE,
            members=(amd64_record, arm64_record),
            visibility="public",
        ),
        transport,
    )

    assert record["status"] == "complete"
    assert [item["platform"] for item in record["members"]] == [
        "linux/amd64",
        "linux/arm64",
    ]
    assert record["anonymous_readback"]["digest"] == record["index_digest"]
    assert [item[0] for item in transport.operations].count("push-index") == 1


def test_index_rejects_duplicate_or_wrong_family_members(tmp_path: Path) -> None:
    transport = FakeRegistry()
    request = _member_request(tmp_path)
    record = publish_member(request, transport)

    with pytest.raises(ProductionError, match="members"):
        publish_index(
            IndexPublishRequest(
                stage="rc",
                profile_id="cuda130",
                repository=request.repository,
                tag="v0.6.0rc1",
                source_sha=SOURCE,
                members=(record, record),
                visibility="public",
            ),
            transport,
        )


def _chart_request(tmp_path: Path) -> ChartPublishRequest:
    chart = tmp_path / "unified-cache-pd-0.6.0-rc.1.tgz"
    chart.write_bytes(b"deterministic-chart")
    chart.with_suffix(".metadata.json").write_text(
        json.dumps({"name": "unified-cache-pd", "version": "0.6.0-rc.1"}),
        encoding="utf-8",
    )
    return ChartPublishRequest(
        stage="rc",
        name="unified-cache-pd",
        version="0.6.0-rc.1",
        chart=chart,
        helm_repository="oci://ghcr.io/octocat/charts",
        reference="ghcr.io/octocat/charts/unified-cache-pd:0.6.0-rc.1",
        file_sha256=_digest(chart.read_bytes()),
        visibility="public",
    )


def test_chart_absent_create_readback_and_identical_reuse(tmp_path: Path) -> None:
    transport = FakeRegistry()
    request = _chart_request(tmp_path)

    first = publish_chart(request, transport)
    transport.operations.clear()
    second = publish_chart(request, transport)

    assert first["status"] == second["status"] == "complete"
    assert first["manifest_digest"] == second["manifest_digest"]
    assert second["decision"] == "reuse"
    assert not any(item[0] == "helm-push" for item in transport.operations)


def test_chart_response_loss_recovers_from_exact_remote_layer(tmp_path: Path) -> None:
    transport = FakeRegistry()
    transport.lose_after_chart = True

    record = publish_chart(_chart_request(tmp_path), transport)

    assert record["status"] == "complete"
    assert any(
        item["outcome"] == "response-loss-recovered" for item in record["operations"]
    )


def test_chart_conflict_blocks_without_helm_push(tmp_path: Path) -> None:
    transport = FakeRegistry()
    request = _chart_request(tmp_path)
    transport.tags[request.reference] = "sha256:" + "9" * 64
    transport.manifests[request.reference.rsplit(":", 1)[0] + "@sha256:" + "9" * 64] = (
        b"{}"
    )

    with pytest.raises(ProductionError, match="conflict"):
        publish_chart(request, transport)

    assert not any(item[0] == "helm-push" for item in transport.operations)


def test_command_transport_has_closed_argv_and_no_shell() -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []

    def execute(argv: tuple[str, ...], anonymous: bool) -> tuple[int, bytes, bytes]:
        calls.append((argv, anonymous))
        if argv[1] == "digest":
            return 0, ("sha256:" + "1" * 64 + "\n").encode(), b""
        return 0, b"{}", b""

    transport = CommandRegistryTransport(execute=execute)
    assert (
        transport.digest("ghcr.io/octocat/ucm-cuda:v0.6.0rc1") == "sha256:" + "1" * 64
    )
    transport.manifest("ghcr.io/octocat/ucm-cuda@sha256:" + "1" * 64, anonymous=True)

    assert calls == [
        (("crane", "digest", "ghcr.io/octocat/ucm-cuda:v0.6.0rc1"), False),
        (("crane", "manifest", "ghcr.io/octocat/ucm-cuda@sha256:" + "1" * 64), True),
    ]
    with pytest.raises(ProductionError, match="reference"):
        transport.digest("ghcr.io/octocat/ucm-cuda:$(curl evil)")


def test_channel_schema_rejects_unknown_top_level_fields(tmp_path: Path) -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "production-channel-record.schema.json"
        ).read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    record = publish_member(_member_request(tmp_path), FakeRegistry())

    validator.validate(record)
    record["unexpected"] = "not evidence"
    assert any(
        error.validator == "additionalProperties"
        for error in validator.iter_errors(record)
    )


def test_readback_rejects_wrong_media_type_and_digest(tmp_path: Path) -> None:
    transport = FakeRegistry()
    request = _member_request(tmp_path)
    record = publish_member(request, transport)
    digest_ref = f"{request.repository}@{record['manifest_digest']}"
    transport.manifests[digest_ref] = canonical_bytes(
        {"schemaVersion": 2, "mediaType": "text/html"}
    )

    with pytest.raises(ProductionError, match="digest|media type"):
        readback_reference(digest_ref, "public", transport)
