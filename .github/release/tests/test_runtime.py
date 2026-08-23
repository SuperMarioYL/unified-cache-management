"""Behavior contracts for arbitrary runtime-tag inspection and projection."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
sys.path.insert(0, str(RELEASE_ROOT))

runtime = importlib.import_module("ucm_release.runtime")

RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
PRODUCTS = [
    {
        "id": "vllm",
        "runtime_repository": "docker.io/vllm/vllm-openai",
        "target_repository": "ghcr.io/release-org/vllm-openai",
        "source_repository": "vllm-project/vllm",
        "accelerator": "cuda",
        "backend": "cuda",
    },
    {
        "id": "vllm-ascend",
        "runtime_repository": "quay.io/ascend/vllm-ascend",
        "target_repository": "ghcr.io/release-org/vllm-ascend",
        "source_repository": "vllm-project/vllm-ascend",
        "accelerator": "ascend",
        "backend_by_soc": {
            "ascend910b1": "cann-a2",
            "ascend910_9391": "cann-a3",
            "ascend950": "cann-a5",
        },
    },
]

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DIGESTS = {
    "amd64": "sha256:" + "1" * 64,
    "arm64": "sha256:" + "2" * 64,
    "windows": "sha256:" + "3" * 64,
}


def _index(*architectures: str) -> dict[str, object]:
    descriptors = []
    for architecture in architectures:
        operating_system = "windows" if architecture == "windows" else "linux"
        descriptors.append(
            {
                "mediaType": MANIFEST,
                "digest": DIGESTS[architecture],
                "platform": {
                    "os": operating_system,
                    "architecture": (
                        "amd64" if architecture == "windows" else architecture
                    ),
                },
            }
        )
    return {"mediaType": INDEX, "manifests": descriptors}


def _config(
    architecture: str,
    *,
    source: str = "https://github.com/vllm-project/vllm",
    revision: str = "a" * 40,
) -> dict[str, object]:
    return {
        "os": "linux",
        "architecture": architecture,
        "config": {
            "Labels": {
                "org.opencontainers.image.source": source,
                "org.opencontainers.image.revision": revision,
                "ai.vllm.build.commit": revision[:12],
            }
        },
    }


def _inspect(
    reference: str,
    architectures: tuple[str, ...] = ("amd64", "arm64"),
) -> dict[str, object]:
    repository = reference.rpartition(":")[0]
    manifest = _index(*architectures)
    configs = {
        f"{repository}@{DIGESTS[architecture]}": _config(architecture)
        for architecture in architectures
        if architecture != "windows"
    }
    return runtime.inspect_runtime_references(
        [reference],
        products=PRODUCTS,
        runners=RUNNERS,
        manifest_loader=lambda _reference: manifest,
        config_loader=lambda member: configs[member],
    )


def _raw_cuda_probes(inspection: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "probe_id": item["probe_id"],
            "python_version": "Python 3.12.8",
            "os_id": '"Ubuntu"',
            "os_version": '"24.04"',
            "glibc_version": "ldd (Ubuntu GLIBC 2.39) 2.39",
            "cuda_version": "CUDA Version 12.9.1",
            "soc_version": "",
            "oci_revision": "a" * 40,
        }
        for item in inspection["probe_matrix"]["include"]
    ]


def _aggregate_cuda(
    architectures: tuple[str, ...] = ("amd64", "arm64"),
    tag: str = "cu129-nightly-deadbeef",
) -> tuple[dict[str, object], dict[str, object]]:
    inspection = _inspect(
        f"docker.io/vllm/vllm-openai:{tag}", architectures=architectures
    )
    return inspection, runtime.aggregate_runtime_probes(
        inspection, _raw_cuda_probes(inspection)
    )


def _builder(
    *,
    architecture: str,
    manylinux: str = "manylinux_2_28",
    created: str = "2026-08-23T08:00:00Z",
    checked: bool = True,
    tag: str | None = None,
) -> dict[str, object]:
    return {
        "id": tag or f"cuda129-{architecture}-{manylinux}",
        "backend": "cuda",
        "accelerator_runtime": "cuda-12.9",
        "soc_version": "na",
        "python_version": "3.12.9",
        "python_abi": "cp312",
        "cpu_arch": architecture,
        "manylinux": manylinux,
        "target_repository": "ghcr.io/release-org/ucm-builder-vllm",
        "target_tag": tag or f"cuda129-{architecture}-{manylinux}",
        "created": created,
        "checked": checked,
    }


def _matches(probe: dict[str, object]) -> dict[str, object]:
    builders = [_builder(architecture=item["cpu_arch"]) for item in probe["probes"]]
    return runtime.match_runtime_builders(probe, builders)


def test_inspection_treats_tags_as_opaque_and_filters_actual_platforms() -> None:
    reference = "docker.io/vllm/vllm-openai:v0.21.0-cu129-ubuntu2404"
    repository = reference.rpartition(":")[0]
    configs = {
        f"{repository}@{DIGESTS['amd64']}": _config("amd64"),
        f"{repository}@{DIGESTS['arm64']}": _config("arm64"),
    }

    inspection = runtime.inspect_runtime_references(
        [reference],
        products=PRODUCTS,
        runners=RUNNERS,
        manifest_loader=lambda _reference: _index("windows", "arm64", "amd64"),
        config_loader=lambda member: configs[member],
    )

    assert inspection["runtimes"] == [
        {
            "request_id": "runtime-001",
            "product_id": "vllm",
            "runtime_ref": reference,
            "repository": repository,
            "tag": "v0.21.0-cu129-ubuntu2404",
            "target_repository": "ghcr.io/release-org/vllm-openai",
            "architectures": ["amd64", "arm64"],
            "probe_ids": ["runtime-001-amd64", "runtime-001-arm64"],
        }
    ]
    include = inspection["probe_matrix"]["include"]
    assert [item["cpu_arch"] for item in include] == ["amd64", "arm64"]
    assert [item["runner"] for item in include] == [
        "ubuntu-24.04",
        "ubuntu-24.04-arm",
    ]
    assert all(item["oci_revision"] == "a" * 40 for item in include)


def test_inspection_accepts_one_single_manifest_architecture_without_index() -> None:
    reference = "docker.io/vllm/vllm-openai:nightly"

    inspection = runtime.inspect_runtime_references(
        [reference],
        products=PRODUCTS,
        runners=RUNNERS,
        manifest_loader=lambda _reference: {
            "mediaType": MANIFEST,
            "config": {"digest": "sha256:" + "4" * 64},
        },
        config_loader=lambda _reference: _config("arm64"),
        digest_loader=lambda _reference: "sha256:" + "5" * 64,
    )

    assert inspection["runtimes"][0]["architectures"] == ["arm64"]
    assert [item["probe_id"] for item in inspection["probe_matrix"]["include"]] == [
        "runtime-001-arm64"
    ]
    assert inspection["probe_matrix"]["include"][0]["image_reference"].endswith(
        "@sha256:" + "5" * 64
    )


def test_inspection_rejects_unconfigured_repository_and_duplicate_arch() -> None:
    with pytest.raises(ValueError, match="not configured"):
        runtime.inspect_runtime_references(
            ["docker.io/external/runtime:nightly"],
            products=PRODUCTS,
            runners=RUNNERS,
            manifest_loader=lambda _reference: _index("amd64"),
            config_loader=lambda _reference: _config("amd64"),
        )

    with pytest.raises(ValueError, match="duplicate linux/amd64"):
        runtime.inspect_runtime_references(
            ["docker.io/vllm/vllm-openai:nightly"],
            products=PRODUCTS,
            runners=RUNNERS,
            manifest_loader=lambda _reference: _index("amd64", "amd64"),
            config_loader=lambda _reference: _config("amd64"),
        )


def test_probe_aggregation_normalizes_cuda_python_os_glibc_and_labels() -> None:
    inspection, probe = _aggregate_cuda(("amd64",))

    assert probe == {
        "kind": "ucm-runtime-probe",
        "schema_version": 1,
        "probes": [
            {
                "probe_id": "runtime-001-amd64",
                "request_id": "runtime-001",
                "product_id": "vllm",
                "runtime_ref": "docker.io/vllm/vllm-openai:cu129-nightly-deadbeef",
                "repository": "docker.io/vllm/vllm-openai",
                "tag": "cu129-nightly-deadbeef",
                "target_repository": "ghcr.io/release-org/vllm-openai",
                "configured_source_repository": "vllm-project/vllm",
                "cpu_arch": "amd64",
                "platform": "linux/amd64",
                "runner": "ubuntu-24.04",
                "image_reference": inspection["probe_matrix"]["include"][0][
                    "image_reference"
                ],
                "backend": "cuda",
                "accelerator_runtime": "cuda-12.9",
                "soc_version": "na",
                "python_version": "3.12",
                "python_abi": "cp312",
                "os_id": "ubuntu",
                "os_version": "24.04",
                "glibc_version": "2.39",
                "oci_labels": inspection["probe_matrix"]["include"][0]["oci_labels"],
                "oci_source": "https://github.com/vllm-project/vllm",
                "oci_revision": "a" * 40,
            }
        ],
    }


def test_probe_aggregation_maps_ascend_soc_to_backend() -> None:
    reference = "quay.io/ascend/vllm-ascend:nightly-releases-v0.23.0-a3-openeuler"
    repository = reference.rpartition(":")[0]
    inspection = runtime.inspect_runtime_references(
        [reference],
        products=PRODUCTS,
        runners=RUNNERS,
        manifest_loader=lambda _reference: _index("arm64"),
        config_loader=lambda _reference: _config(
            "arm64",
            source="https://github.com/vllm-project/vllm-ascend",
            revision="b" * 40,
        ),
    )
    raw = {
        "probe_id": "runtime-001-arm64",
        "python_version": "3.11.11",
        "os_id": "openEuler",
        "os_version": "24.03",
        "glibc_version": "2.38",
        "cann_version": "CANN 9.0.0",
        "soc_version": "ASCEND910_9391",
        "oci_source": "https://github.com/vllm-project/vllm-ascend",
        "oci_revision": "b" * 40,
    }

    probe = runtime.aggregate_runtime_probes(inspection, [raw])["probes"][0]

    assert probe["runtime_ref"] == f"{repository}:nightly-releases-v0.23.0-a3-openeuler"
    assert probe["backend"] == "cann-a3"
    assert probe["accelerator_runtime"] == "cann-9.0.0"
    assert probe["soc_version"] == "ascend910_9391"
    assert probe["python_abi"] == "cp311"
    assert probe["os_id"] == "openeuler"


def test_probe_aggregation_requires_exact_expected_member_set() -> None:
    inspection = _inspect("docker.io/vllm/vllm-openai:nightly")

    with pytest.raises(ValueError, match="missing=.*arm64"):
        runtime.aggregate_runtime_probes(inspection, _raw_cuda_probes(inspection)[:1])


def test_builder_match_uses_highest_compatible_floor_then_newest_checked() -> None:
    _, probe = _aggregate_cuda(("amd64",))
    probe["probes"][0]["glibc_version"] = "2.35"
    builders = [
        _builder(
            architecture="amd64",
            manylinux="manylinux_2_17",
            created="2026-08-23T12:00:00Z",
            tag="floor17-new",
        ),
        _builder(
            architecture="amd64",
            manylinux="manylinux_2_28",
            created="2026-08-22T12:00:00Z",
            tag="floor28-old",
        ),
        _builder(
            architecture="amd64",
            manylinux="manylinux_2_28",
            created="2026-08-23T11:00:00Z",
            tag="floor28-new",
        ),
        _builder(
            architecture="amd64",
            manylinux="manylinux_2_28",
            created="2026-08-23T13:00:00Z",
            checked=False,
            tag="floor28-unchecked",
        ),
        _builder(
            architecture="amd64",
            manylinux="manylinux_2_39",
            created="2026-08-23T14:00:00Z",
            tag="floor39-too-new",
        ),
    ]

    selected = runtime.match_runtime_builders(probe, builders)

    assert selected["ok"] is True
    assert selected["problems"] == []
    assert selected["matches"][0]["builder"] == {
        "id": "floor28-new",
        "repository": "ghcr.io/release-org/ucm-builder-vllm",
        "tag": "floor28-new",
        "manylinux": "manylinux_2_28",
        "created": "2026-08-23T11:00:00Z",
    }


def test_builder_match_reports_complete_missing_capability() -> None:
    _, probe = _aggregate_cuda(("arm64",))
    builders = [_builder(architecture="amd64")]

    result = runtime.match_runtime_builders(probe, builders)

    assert result["ok"] is False
    problem = result["problems"][0]
    assert problem["reason"] == "missing-compatible-builder"
    assert problem["capability"] == {
        "backend": "cuda",
        "accelerator_runtime": "cuda-12.9",
        "soc_version": "na",
        "python_version": "3.12",
        "python_abi": "cp312",
        "cpu_arch": "arm64",
    }
    assert "runtime_glibc=2.39" in problem["detail"]
    assert "cpu_arch=arm64" in problem["detail"]


def test_multiple_opaque_tags_with_the_same_capability_reuse_one_wheel() -> None:
    references = [
        "docker.io/vllm/vllm-openai:nightly",
        "docker.io/vllm/vllm-openai:v0.21.0-ubuntu2404",
    ]
    repository = "docker.io/vllm/vllm-openai"
    member_reference = f"{repository}@{DIGESTS['amd64']}"
    inspection = runtime.inspect_runtime_references(
        references,
        products=PRODUCTS,
        runners=RUNNERS,
        manifest_loader=lambda _reference: _index("amd64"),
        config_loader=lambda reference: {member_reference: _config("amd64")}[reference],
    )
    probe = runtime.aggregate_runtime_probes(inspection, _raw_cuda_probes(inspection))

    matches = runtime.match_runtime_builders(probe, [_builder(architecture="amd64")])

    assert len(matches["matches"]) == 2
    assert len({item["wheel_id"] for item in matches["matches"]}) == 1
    publication = runtime.project_pr_publication(
        probe, matches, pr_number=42, author="release-author", run_id=998877
    )
    assert [family["runtime_ref"] for family in publication["families"]] == references
    assert len({family["wheel_ids"][0] for family in publication["families"]}) == 1


def test_publication_projects_single_member_without_index() -> None:
    _, probe = _aggregate_cuda(("arm64",), tag="nightly")
    matches = _matches(probe)

    publication = runtime.project_pr_publication(
        probe, matches, pr_number=42, author="Release-Author", run_id=998877
    )

    members = publication["member_matrix"]["include"]
    assert len(members) == 1
    assert members[0]["target_tag"] == "pr-42-release-author-run-998877-nightly-arm64"
    assert publication["index_matrix"]["include"] == []
    assert publication["families"][0]["has_index"] is False
    assert publication["families"][0]["final_refs"] == [members[0]["target_ref"]]


def test_publication_projects_dynamic_dual_arch_index_and_deduplicates_wheels() -> None:
    _, probe = _aggregate_cuda()
    matches = _matches(probe)

    publication = runtime.project_pr_publication(
        probe, matches, pr_number="42", author="Release-Author", run_id="998877"
    )

    members = publication["member_matrix"]["include"]
    indexes = publication["index_matrix"]["include"]
    assert [member["cpu_arch"] for member in members] == ["amd64", "arm64"]
    assert len(indexes) == 1
    assert indexes[0]["members"] == [member["target_ref"] for member in members]
    assert indexes[0]["target_tag"] == (
        "pr-42-release-author-run-998877-cu129-nightly-deadbeef"
    )
    assert len(publication["families"][0]["wheel_ids"]) == 2


def test_pr_tag_sanitizes_and_truncates_with_run_identity() -> None:
    first = runtime.project_pr_tag(
        "nightly/feature+" + "x" * 160,
        pr_number=9,
        author="Feature_Author",
        run_id=123,
    )
    second = runtime.project_pr_tag(
        "nightly/feature+" + "x" * 160,
        pr_number=9,
        author="Feature_Author",
        run_id=124,
    )

    assert first.startswith("pr-9-feature_author-run-123-nightly-feature-")
    assert len(first + "-amd64") <= 128
    assert first != second


def test_receipt_carries_source_capability_wheel_and_final_tag_mapping() -> None:
    inspection, probe = _aggregate_cuda(("amd64",), tag="nightly")
    matches = _matches(probe)
    publication = runtime.project_pr_publication(
        probe, matches, pr_number=42, author="release-author", run_id=998877
    )

    receipt = runtime.build_receipt(
        requested_refs=["docker.io/vllm/vllm-openai:nightly"],
        stage_results={
            "inspect": "success",
            "probe": "success",
            "aggregate": "success",
            "build": "success",
            "member": "success",
            "index": "skipped",
        },
        inspection=inspection,
        runtime_probe=probe,
        builder_matches=matches,
        publication=publication,
        run_url="https://github.example/actions/runs/998877",
    )

    assert receipt["status"] == "success"
    row = receipt["runtimes"][0]
    assert row["runtime_ref"] == "docker.io/vllm/vllm-openai:nightly"
    assert row["architectures"] == ["amd64"]
    assert row["capabilities"][0]["accelerator_runtime"] == "cuda-12.9"
    assert row["capabilities"][0]["wheel_id"] == row["wheel_ids"][0]
    assert row["final_refs"] == publication["families"][0]["final_refs"]


def test_receipt_survives_early_failure_and_includes_builder_gap() -> None:
    inspection, probe = _aggregate_cuda(("arm64",), tag="nightly")
    matches = runtime.match_runtime_builders(probe, [_builder(architecture="amd64")])

    receipt = runtime.build_receipt(
        requested_refs=["docker.io/vllm/vllm-openai:nightly"],
        stage_results={
            "inspect": "success",
            "probe": "success",
            "aggregate": "failure",
            "build": "skipped",
        },
        inspection=inspection,
        runtime_probe=probe,
        builder_matches=matches,
    )

    assert receipt["status"] == "failure"
    assert receipt["problems"][0]["stage"] == "builder-match"
    assert receipt["problems"][0]["runtime_ref"].endswith(":nightly")
    assert receipt["runtimes"][0]["final_refs"] == []

    markdown = runtime.render_receipt_markdown(receipt)
    assert "`failure`" in markdown
    assert "missing-compatible-builder" in markdown
    assert "docker.io/vllm/vllm-openai:nightly" in markdown


def test_receipt_preserves_malformed_raw_reference_as_inspect_problem() -> None:
    receipt = runtime.build_receipt(
        requested_refs=["not-a-runtime-reference"],
        stage_results={"inspect": "failure", "probe": "skipped"},
    )

    assert receipt["status"] == "failure"
    assert receipt["runtimes"][0]["runtime_ref"] == "not-a-runtime-reference"
    assert receipt["problems"][0]["reason"] == "invalid-runtime-reference"
    assert "must be repository:tag" in receipt["problems"][0]["detail"]


def test_receipt_never_claims_publication_when_member_job_failed() -> None:
    inspection, probe = _aggregate_cuda(("amd64",), tag="nightly")
    matches = _matches(probe)
    publication = runtime.project_pr_publication(
        probe, matches, pr_number=42, author="release-author", run_id=998877
    )

    receipt = runtime.build_receipt(
        requested_refs=["docker.io/vllm/vllm-openai:nightly"],
        stage_results={"member": "failure", "index": "skipped"},
        inspection=inspection,
        runtime_probe=probe,
        builder_matches=matches,
        publication=publication,
    )

    assert receipt["runtimes"][0]["member_refs"] == []
    assert receipt["runtimes"][0]["final_refs"] == []
    assert "Published images" not in runtime.render_receipt_markdown(receipt)
