from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from conftest import PRODUCTION_ROOT
from ucm_release_production.candidate import (
    EXPECTED_IMAGE_SPECS,
    CandidateBundle,
    compare_trusted_rebuild,
    pack_candidate,
    reopen_candidate,
    seal_candidate,
)
from ucm_release_production.common import (
    ProductionError,
    sha256_envelope,
    write_json,
)
from ucm_release_production.config import load_config
from ucm_release_production.tags import intent_document, parse_tag

CONFIG = PRODUCTION_ROOT / "production-release.json"
SOURCE = "1" * 40
TAG_OBJECT = "2" * 40
CONTROL = "3" * 40
RUN_ID = 1001
ATTEMPT = 1
PROFILES = ("cuda130", "cann900-a2", "cann900-a3")
ARCHES = ("amd64", "arm64")
DISTRIBUTIONS = {
    "cuda130": "uc-manager-cuda",
    "cann900-a2": "uc-manager-cann-a2",
    "cann900-a3": "uc-manager-cann-a3",
}


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _elf(architecture: str) -> bytes:
    machine = {"amd64": 62, "arm64": 183}[architecture]
    raw = bytearray(64)
    raw[:16] = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    raw[16:18] = (3).to_bytes(2, "little")
    raw[18:20] = machine.to_bytes(2, "little")
    raw[20:24] = (1).to_bytes(4, "little")
    return bytes(raw)


def _wheel(path: Path, distribution: str, version: str, architecture: str) -> None:
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    tag_arch = "x86_64" if architecture == "amd64" else "aarch64"
    wheel_platform = "manylinux_2_28" if distribution == "uc-manager-cuda" else "linux"
    entries = {
        "ucm/__init__.py": b"",
        "ucm/native.so": _elf(architecture),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n"
            "Requires-Dist: packaging==24.2\n"
            "Requires-Dist: wrapt==1.17.2\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: candidate-test\n"
            f"Root-Is-Purelib: false\nTag: cp312-cp312-{wheel_platform}_{tag_arch}\n\n"
        ).encode(),
    }
    record_name = f"{dist_info}/RECORD"
    rows: list[list[str]] = []
    for name, raw in sorted(entries.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
        rows.append([name, f"sha256={digest.decode()}", str(len(raw))])
    rows.append([record_name, "", ""])
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries[record_name] = record.getvalue().encode()
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(entries.items()):
            archive.writestr(name, raw)


def _intent() -> dict[str, object]:
    return intent_document(parse_tag("v0.6.0rc1", load_config(CONFIG)))


def _source() -> dict[str, object]:
    return sha256_envelope(
        {
            "kind": "ucm-production-source-identity",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "stage": "rc",
            "tag_name": "v0.6.0rc1",
            "tag_object_sha": TAG_OBJECT,
            "source_commit_sha": SOURCE,
            "source_branch": "0.6.0-release",
            "tagger": "release-operator",
            "tagged_at": "2026-08-13T08:00:00Z",
            "tag_message_sha256": "4" * 64,
            "control_default_branch": "develop",
            "control_sha": CONTROL,
            "lineage": {
                "accepted": True,
                "stage": "draft",
                "version": "0.6.0",
                "tag_name": "draft/v0.6.0-1",
                "source_commit_sha": SOURCE,
                "evidence_sha256": "5" * 64,
            },
        }
    )


def _run() -> dict[str, object]:
    return sha256_envelope(
        {
            "kind": "ucm-production-candidate-run",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "workflow_id": 77,
            "workflow_path": ".github/workflows/production-tag-candidate.yml",
            "event": "push",
            "run_id": RUN_ID,
            "run_attempt": ATTEMPT,
            "source_date_epoch": 1786608000,
            "head_sha": SOURCE,
            "tag_name": "v0.6.0rc1",
            "artifact_name": (
                f"ucm-production-candidate-42-{TAG_OBJECT}-{SOURCE}-{RUN_ID}-{ATTEMPT}"
            ),
        }
    )


def _record(path: Path, value: dict[str, object]) -> None:
    write_json(path, sha256_envelope(value), path.name)


def _candidate_root(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    root.mkdir()
    write_json(root / "source-identity.json", _source(), "source identity")
    for profile in PROFILES:
        for architecture in ARCHES:
            spec_id = f"{profile}-{architecture}"
            directory = root / "wheels" / spec_id
            filename = (
                f"{DISTRIBUTIONS[profile].replace('-', '_')}-0.6.0rc1-"
                f"cp312-cp312-manylinux_2_28_"
                f"{'x86_64' if architecture == 'amd64' else 'aarch64'}.whl"
            )
            wheel = directory / filename
            _wheel(wheel, DISTRIBUTIONS[profile], "0.6.0rc1", architecture)
            _record(
                directory / "record.json",
                {
                    "kind": "ucm-production-wheel-record",
                    "schema_version": 1,
                    "spec_id": spec_id,
                    "distribution": DISTRIBUTIONS[profile],
                    "version": "0.6.0rc1",
                    "filename": filename,
                    "file_sha256": _digest(wheel.read_bytes()),
                    "task_sha256": "sha256:"
                    + hashlib.sha256(spec_id.encode()).hexdigest(),
                    "source_sha": SOURCE,
                    "python_abi": "cp312",
                    "wheel_platform": (
                        "manylinux_2_28" if profile == "cuda130" else "linux"
                    ),
                    "runtime_requirements": [
                        "packaging==24.2",
                        "wrapt==1.17.2",
                    ],
                },
            )
    chart = root / "chart" / "unified-cache-pd-0.6.0-rc.1.tgz"
    chart.parent.mkdir(parents=True)
    chart.write_bytes(b"deterministic-chart")
    _record(
        root / "chart" / "record.json",
        {
            "kind": "ucm-production-chart-record",
            "schema_version": 1,
            "name": "unified-cache-pd",
            "version": "0.6.0-rc.1",
            "filename": chart.name,
            "file_sha256": _digest(chart.read_bytes()),
            "content_tree_sha256": "sha256:" + "6" * 64,
            "lint": "passed",
            "template": "passed",
            "source_sha": SOURCE,
        },
    )
    for profile in PROFILES:
        members: list[dict[str, object]] = []
        for architecture in ARCHES:
            spec_id = f"{profile}-{architecture}"
            wheel_record = json.loads(
                (root / "wheels" / spec_id / "record.json").read_text()
            )
            manifest = (
                "sha256:" + hashlib.sha256(f"manifest:{spec_id}".encode()).hexdigest()
            )
            closure = sha256_envelope(
                {
                    "kind": "ucm-production-image-member-closure",
                    "schema_version": 1,
                    "spec_id": spec_id,
                    "platform": f"linux/{architecture}",
                    "source_sha": SOURCE,
                    "task_sha256": wheel_record["task_sha256"],
                    "wheel_sha256": wheel_record["file_sha256"],
                    "recipe_sha256": "sha256:"
                    + hashlib.sha256(f"recipe:{spec_id}".encode()).hexdigest(),
                    "manifest_digest": manifest,
                    "manifest_size": 321,
                    "config_digest": "sha256:"
                    + hashlib.sha256(f"config:{spec_id}".encode()).hexdigest(),
                    "config_size": 123,
                    "layers": [
                        {
                            "digest": "sha256:"
                            + hashlib.sha256(f"layer:{spec_id}".encode()).hexdigest(),
                            "diff_id": "sha256:"
                            + hashlib.sha256(f"diff:{spec_id}".encode()).hexdigest(),
                            "size": 123,
                        }
                    ],
                    "annotations": {
                        "org.opencontainers.image.revision": SOURCE,
                        "org.opencontainers.image.version": "v0.6.0rc1",
                    },
                }
            )
            path = root / "images" / spec_id / "closure.json"
            path.parent.mkdir(parents=True)
            write_json(path, closure, "image closure")
            members.append(
                {
                    "spec_id": spec_id,
                    "platform": f"linux/{architecture}",
                    "manifest_digest": manifest,
                }
            )
        index = sha256_envelope(
            {
                "kind": "ucm-production-image-index-identity",
                "schema_version": 1,
                "profile_id": profile,
                "image_tag": "v0.6.0rc1",
                "members": members,
            }
        )
        path = root / "indexes" / profile / "index.json"
        path.parent.mkdir(parents=True)
        write_json(path, index, "image index")
    return root


def _archive(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = _candidate_root(tmp_path)
    envelope = seal_candidate(root, _intent(), _run())
    output = tmp_path / "candidate.zip"
    pack_candidate(root, envelope, output)
    return output, envelope


def _expected() -> dict[str, object]:
    return {
        "repository": "OctoCat/unified-cache-management",
        "repository_id": 42,
        "tag_name": "v0.6.0rc1",
        "tag_object_sha": TAG_OBJECT,
        "source_sha": SOURCE,
        "run_id": RUN_ID,
        "run_attempt": ATTEMPT,
        "artifact_name": f"ucm-production-candidate-42-{TAG_OBJECT}-{SOURCE}-{RUN_ID}-{ATTEMPT}",
    }


def test_candidate_bundle_has_exact_product_closure(tmp_path: Path) -> None:
    archive, envelope = _archive(tmp_path)

    assert [item["spec_id"] for item in envelope["wheels"]] == list(
        EXPECTED_IMAGE_SPECS
    )
    assert len(envelope["image_members"]) == 6
    assert len(envelope["image_indexes"]) == 3
    assert envelope["chart"]["name"] == "unified-cache-pd"

    with reopen_candidate(archive, _expected()) as bundle:
        assert isinstance(bundle, CandidateBundle)
        assert tuple(bundle.wheel_paths) == EXPECTED_IMAGE_SPECS
        assert bundle.chart_path.name.endswith(".tgz")
        assert bundle.envelope == envelope


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("wheels/cuda130-amd64/record.json", "wheel"),
        ("images/cuda130-amd64/closure.json", "image"),
        ("indexes/cuda130/index.json", "index"),
        ("chart/record.json", "chart"),
        ("source-identity.json", "source"),
    ],
)
def test_seal_rejects_missing_product_members(
    tmp_path: Path, relative: str, message: str
) -> None:
    root = _candidate_root(tmp_path)
    (root / relative).unlink()

    with pytest.raises(ProductionError, match=message):
        seal_candidate(root, _intent(), _run())


def test_seal_rejects_extra_files_and_symlinks(tmp_path: Path) -> None:
    root = _candidate_root(tmp_path)
    (root / "unexpected.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(ProductionError, match="exact file set"):
        seal_candidate(root, _intent(), _run())

    (root / "unexpected.txt").unlink()
    link = root / "linked-record.json"
    link.symlink_to(root / "source-identity.json")
    with pytest.raises(ProductionError, match="symbolic link"):
        seal_candidate(root, _intent(), _run())


def test_seal_rejects_resigned_semantic_drift(tmp_path: Path) -> None:
    root = _candidate_root(tmp_path)
    path = root / "images" / "cuda130-amd64" / "closure.json"
    value = json.loads(path.read_text())
    value.pop("sha256")
    value["source_sha"] = "9" * 40
    path.unlink()
    write_json(path, sha256_envelope(value), "drifted closure")

    with pytest.raises(ProductionError, match="source"):
        seal_candidate(root, _intent(), _run())


@pytest.mark.parametrize(
    ("member", "external_attr", "message"),
    [
        ("../escape", 0o100644 << 16, "unsafe"),
        ("/absolute", 0o100644 << 16, "unsafe"),
        ("escape\nname", 0o100644 << 16, "unsafe"),
        ("linked", (stat.S_IFLNK | 0o777) << 16, "symbolic link"),
    ],
)
def test_reopen_rejects_zip_slip_controls_and_symlinks(
    tmp_path: Path, member: str, external_attr: int, message: str
) -> None:
    archive, _ = _archive(tmp_path)
    attack = tmp_path / "attack.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(attack, "w") as output:
        for item in source.infolist():
            output.writestr(item, source.read(item.filename))
        info = zipfile.ZipInfo(member)
        info.create_system = 3
        info.external_attr = external_attr
        output.writestr(info, b"attack")

    with pytest.raises(ProductionError, match=message):
        reopen_candidate(attack, _expected())


def test_reopen_rejects_duplicate_zip_members(tmp_path: Path) -> None:
    archive, _ = _archive(tmp_path)
    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(duplicate, "w") as output:
        for item in source.infolist():
            output.writestr(item, source.read(item.filename))
        output.writestr("candidate-envelope.json", b"{}")

    with pytest.raises(ProductionError, match="duplicate"):
        reopen_candidate(duplicate, _expected())


def test_reopen_rejects_cross_attempt_and_artifact_reuse(tmp_path: Path) -> None:
    archive, _ = _archive(tmp_path)
    expected = _expected()
    expected["run_attempt"] = 2

    with pytest.raises(ProductionError, match="run_attempt"):
        reopen_candidate(archive, expected)


def test_reopen_rejects_file_byte_drift_even_with_original_envelope(
    tmp_path: Path,
) -> None:
    archive, _ = _archive(tmp_path)
    drift = tmp_path / "drift.zip"
    target = "chart/unified-cache-pd-0.6.0-rc.1.tgz"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(drift, "w") as output:
        for item in source.infolist():
            raw = b"changed" if item.filename == target else source.read(item.filename)
            output.writestr(item, raw)

    with pytest.raises(ProductionError, match="digest"):
        reopen_candidate(drift, _expected())


def test_trusted_rebuild_requires_all_six_wheels_byte_equal(tmp_path: Path) -> None:
    archive, _ = _archive(tmp_path)
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    with reopen_candidate(archive, _expected()) as bundle:
        for spec_id, path in bundle.wheel_paths.items():
            destination = trusted / spec_id / path.name
            destination.parent.mkdir()
            destination.write_bytes(path.read_bytes())

        result = compare_trusted_rebuild(bundle, trusted)
        assert result["identical"] is True
        assert len(result["wheels"]) == 6

        first = (
            trusted
            / EXPECTED_IMAGE_SPECS[0]
            / bundle.wheel_paths[EXPECTED_IMAGE_SPECS[0]].name
        )
        first.write_bytes(first.read_bytes() + b"drift")
        with pytest.raises(ProductionError, match="byte-for-byte"):
            compare_trusted_rebuild(bundle, trusted)
