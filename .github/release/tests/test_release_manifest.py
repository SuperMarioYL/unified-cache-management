from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / ".github" / "release" / "ucm_release" / "release.py"
SPEC = importlib.util.spec_from_file_location("ucm_release_manifest", MODULE)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def _plan() -> dict[str, object]:
    return {
        "git_tag": "v0.7.62rc1",
        "release_kind": "publish",
        "version": "0.7.62rc1",
        "publish": {
            "ghcr": {"enabled": True},
            "dockerhub": {
                "enabled": False,
                "namespace": "docker.io/example",
            },
        },
        "chart": {
            "name": "ucm",
            "version": "0.7.62-rc.1",
            "app_version": "0.7.62rc1",
        },
        "wheels": [
            {
                "id": "cuda129-cp312-amd64",
                "dist_name": "uc-manager-cuda-cu129",
                "wheel_version": "0.7.62rc1",
                "python_abi": "cp312",
                "cpu_arch": "amd64",
                "backend": "cuda",
                "runtime_variant": "cu129",
                "manylinux": "manylinux_2_28",
                "builder": {
                    "repository": "ghcr.io/example/ucm-builder",
                    "tag": "cuda129-cp312-amd64",
                    "digest": "sha256:" + "c" * 64,
                    "source_image": "docker.io/pytorch/manylinux2_28-builder:cuda12.9",
                    "source_image_digest": "sha256:builder",
                },
            }
        ],
        "images": [
            {
                "id": "vllm-v023-amd64",
                "family_id": "vllm-v023",
                "wheel_id": "cuda129-cp312-amd64",
                "cpu_arch": "amd64",
                "runtime": {
                    "repository": "docker.io/vllm/vllm-openai",
                    "tag": "v0.23.0",
                    "python_abi": "cp312",
                },
            }
        ],
        "families": [
            {
                "id": "vllm-v023",
                "runtime": {
                    "repository": "docker.io/vllm/vllm-openai",
                    "tag": "v0.23.0",
                },
                "members": [
                    {
                        "image_id": "vllm-v023-amd64",
                        "cpu_arch": "amd64",
                        "reference": "ghcr.io/example/vllm:v0.23.0-ucm-amd64",
                    }
                ],
                "published_reference": "ghcr.io/example/vllm:v0.23.0-ucm-amd64",
                "create_index": False,
            }
        ],
    }


def _write_artifact_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, Path, str]:
    wheels = tmp_path / "wheels" / "one"
    wheels.mkdir(parents=True)
    filename = "uc_manager_cuda_cu129-0.7.62rc1-cp312-cp312-linux_x86_64.whl"
    (wheels / filename).write_bytes(b"wheel")
    report_text = "\n".join(
        (
            f"{filename} is consistent with the following platform tag: "
            '"linux_x86_64".',
            "",
            "The wheel references external versioned symbols in these system-provided "
            "shared libraries: libc.so.6 with versions {'GLIBC_2.2.5', 'GLIBC_2.17'}",
            "",
            'This constrains the platform tag to "manylinux_2_27_x86_64".',
            "",
            "The following external shared libraries are required by the wheel:",
            json.dumps({"libcudart.so.13": None, "libmetrics.so": None}, indent=4),
            "",
        )
    )
    report_path = wheels / "auditwheel-show.txt"
    report_path.write_text(report_text, encoding="utf-8")
    result_path = wheels / "wheel-result.json"
    result_path.write_text(
        json.dumps(
            {
                "kind": "ucm-wheel-result",
                "schema_version": 2,
                "task_id": "cuda129-cp312-amd64",
                "distribution": "uc-manager-cuda-cu129",
                "version": "0.7.62rc1",
                "python_abi": "cp312",
                "cpu_arch": "amd64",
                "filename": filename,
                "platform_tags": ["linux_x86_64"],
                "auditwheel_platform_tag": "linux_x86_64",
                "glibc_versions": ["GLIBC_2.2.5", "GLIBC_2.17"],
                "glibc_floor": "GLIBC_2.17",
                "external_libraries": ["libcudart.so.13", "libmetrics.so"],
                "auditwheel_report": {
                    "filename": report_path.name,
                    "sha256": hashlib.sha256(report_text.encode()).hexdigest(),
                    "text": report_text,
                },
            }
        ),
        encoding="utf-8",
    )
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "ucm-0.7.62-rc.1.tgz").write_bytes(b"chart")
    return tmp_path / "wheels", chart, result_path, filename


def test_artifacts_and_image_receipts_form_one_mapping(tmp_path: Path) -> None:
    wheels, chart, _, filename = _write_artifact_inputs(tmp_path)

    manifest, checksums = release.build_artifacts_manifest(_plan(), wheels, chart)
    assert manifest["release"]["status"] == "artifacts-ready"
    assert manifest["wheels"][0]["platform_tags"] == ["linux_x86_64"]
    assert manifest["wheels"][0]["auditwheel_platform_tag"] == "linux_x86_64"
    assert manifest["wheels"][0]["builder"]["source_image_digest"] == ("sha256:builder")
    assert manifest["wheels"][0]["builder"]["digest"] == "sha256:" + "c" * 64
    assert manifest["images"][0]["wheel_id"] == "cuda129-cp312-amd64"
    notes = release.render_notes(manifest)
    assert "docker.io/vllm/vllm-openai:v0.23.0" in notes
    assert f"amd64={filename}" in notes
    assert {name for _, name in checksums} == {
        filename,
        "ucm-0.7.62-rc.1.tgz",
    }

    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "member.json").write_text(
        json.dumps(
            {
                "kind": "ucm-image-member-receipt",
                "schema_version": 1,
                "id": "vllm-v023-amd64",
                "status": "published",
                "targets": [
                    {
                        "channel": "ghcr",
                        "reference": "ghcr.io/example/vllm:v0.23.0-ucm-amd64",
                        "digest": "sha256:" + "a" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    final = release.finalize_manifest(
        manifest,
        receipts,
        build_outcome="success",
        member_outcome="success",
        index_outcome="skipped",
    )
    assert final["release"]["status"] == "complete"
    assert final["images"][0]["targets"][0]["digest"] == "sha256:" + "a" * 64
    assert final["families"][0]["targets"] == final["images"][0]["targets"]


@pytest.mark.parametrize(
    ("field", "value"),
    (("kind", "wrong-wheel-result"), ("schema_version", 1)),
)
def test_artifact_manifest_rejects_wrong_wheel_result_contract(
    tmp_path: Path, field: str, value: object
) -> None:
    wheels, chart, result_path, _ = _write_artifact_inputs(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result[field] = value
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ValueError, match="ucm-wheel-result schema 2"):
        release.build_artifacts_manifest(_plan(), wheels, chart)


@pytest.mark.parametrize(
    "field",
    (
        "platform_tags",
        "auditwheel_platform_tag",
        "glibc_versions",
        "glibc_floor",
        "external_libraries",
        "auditwheel_report",
    ),
)
def test_artifact_manifest_requires_wheel_audit_fields(
    tmp_path: Path, field: str
) -> None:
    wheels, chart, result_path, _ = _write_artifact_inputs(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    del result[field]
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ValueError, match="missing audit fields"):
        release.build_artifacts_manifest(_plan(), wheels, chart)


def test_artifact_manifest_rejects_changed_auditwheel_report(tmp_path: Path) -> None:
    wheels, chart, result_path, _ = _write_artifact_inputs(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    report_path = result_path.parent / result["auditwheel_report"]["filename"]
    report_path.write_text(
        "changed after wheel-result was recorded\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="report digest does not match"):
        release.build_artifacts_manifest(_plan(), wheels, chart)


def test_artifact_manifest_requires_immutable_builder_digest(tmp_path: Path) -> None:
    wheels, chart, _, _ = _write_artifact_inputs(tmp_path)
    plan = _plan()
    del plan["wheels"][0]["builder"]["digest"]

    with pytest.raises(ValueError, match="immutable Builder digest"):
        release.build_artifacts_manifest(plan, wheels, chart)


def test_missing_receipt_keeps_artifacts_available_and_marks_images_failed() -> None:
    manifest = {
        "release": {"git_tag": "v0.7.62rc1", "status": "artifacts-ready"},
        "images": [
            {
                "id": "image-amd64",
                "family_id": "family",
                "status": "building",
                "targets": [],
            }
        ],
        "families": [
            {
                "id": "family",
                "create_index": False,
                "status": "building",
                "targets": [],
            }
        ],
    }
    result = release.finalize_manifest(
        manifest,
        Path("/does/not/exist"),
        build_outcome="failure",
        member_outcome="skipped",
        index_outcome="skipped",
    )
    assert result["release"]["status"] == "images-failed"
    assert result["images"][0]["status"] == "failed"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 999, "invalid contract"),
        ("digest", "garbage", "digest is invalid"),
        ("reference", "ghcr.io/example/wrong:tag", "planned reference"),
    ],
)
def test_published_receipt_must_match_its_schema_and_planned_target(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    wheels, chart, _, _ = _write_artifact_inputs(tmp_path)
    manifest, _ = release.build_artifacts_manifest(_plan(), wheels, chart)
    receipt = {
        "kind": "ucm-image-member-receipt",
        "schema_version": 1,
        "id": "vllm-v023-amd64",
        "status": "published",
        "targets": [
            {
                "channel": "ghcr",
                "reference": "ghcr.io/example/vllm:v0.23.0-ucm-amd64",
                "digest": "sha256:" + "b" * 64,
            }
        ],
    }
    if field == "schema_version":
        receipt[field] = value
    else:
        receipt["targets"][0][field] = value
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "member.json").write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        release.finalize_manifest(
            manifest,
            receipts,
            build_outcome="success",
            member_outcome="success",
            index_outcome="skipped",
        )
