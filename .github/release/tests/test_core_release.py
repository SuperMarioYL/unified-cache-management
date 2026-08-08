from __future__ import annotations

import base64
import csv
import hashlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = ROOT / ".github" / "release"
PYTHONPATH = str(RELEASE_ROOT)
sys.path.insert(0, PYTHONPATH)

_deterministic_repack = importlib.import_module(
    "ucm_release.chart"
)._deterministic_repack
derive_chart_version = importlib.import_module("ucm_release.core").derive_chart_version


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": PYTHONPATH}
    return subprocess.run(
        [sys.executable, "-m", "ucm_release", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def _fixture_wheel(directory: Path, *, version: str = "0.5.0rc1") -> Path:
    filename = f"uc_manager-{version}-cp312-cp312-manylinux_2_17_x86_64.whl"
    wheel = directory / filename
    dist_info = f"uc_manager-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\n"
            "Name: uc-manager\n"
            f"Version: {version}\n"
            "Requires-Dist: wrapt==1.17.2\n\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Root-Is-Purelib: false\n"
            "Tag: cp312-cp312-manylinux_2_17_x86_64\n\n",
        )
        archive.writestr("ucm/__init__.py", "__version__ = 'fixture'\n")
    return wheel


def _production_shaped_wheel(
    directory: Path,
    *,
    include_native: bool,
    runtime: str = "cuda-12.9",
) -> Path:
    version = "0.5.0rc1"
    filename = f"uc_manager-{version}-cp312-cp312-manylinux_2_17_x86_64.whl"
    wheel = directory / filename
    dist_info = f"uc_manager-{version}.dist-info"
    entries: dict[str, bytes] = {
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\nName: uc-manager\n"
            f"Version: {version}\nRequires-Dist: wrapt==1.17.2\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
            "Tag: cp312-cp312-manylinux_2_17_x86_64\n\n"
        ).encode(),
        f"{dist_info}/ucm-build.json": json.dumps(
            {
                "schema_version": 1,
                "spec_id": "cuda-cu129-ubuntu2204-amd64-cp312-release-default-sm75-sm80-sm86-sm89-sm90",
                "source_commit": "d" * 40,
                "build_context_digest": "sha256:" + "e" * 64,
                "accelerator": "cuda",
                "accelerator_runtime": runtime,
                "npu_arch_or_na": "na",
                "os": "ubuntu-22.04",
                "cpu_arch": "amd64",
                "python_abi": "cp312",
                "binary_profile_id": "release-default-sm75-sm80-sm86-sm89-sm90",
            },
            sort_keys=True,
        ).encode(),
        "ucm/__init__.py": b"__version__ = 'fixture'\n",
    }
    if include_native:
        entries["ucm/ucm_custom_ops.cpython-312-linux.so"] = b"\x7fELFsynthetic"
    rows: list[list[str]] = []
    for name, data in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rows.append([name, f"sha256={digest}", str(len(data))])
    rows.append([f"{dist_info}/RECORD", "", ""])
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries[f"{dist_info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return wheel


def _write_configs(
    directory: Path,
    release: dict,
    compatibility: dict | None = None,
) -> tuple[Path, Path]:
    release_path = directory / "release.yaml"
    compatibility_path = directory / "compatibility.yaml"
    release_path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")
    compatibility_path.write_text(
        yaml.safe_dump(
            compatibility
            or yaml.safe_load(
                (RELEASE_ROOT / "compatibility.yaml").read_text(encoding="utf-8")
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return release_path, compatibility_path


def _write_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)


def test_config_is_strict_and_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    valid = json.loads(_run("config", "validate").stdout)
    assert valid == {
        "compatibility_rules": 2,
        "schema_version": 1,
        "wheel_profiles": 6,
    }

    release_config = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    release_config["unexpected"] = True
    bad_release = tmp_path / "release.yaml"
    bad_release.write_text(yaml.safe_dump(release_config), encoding="utf-8")
    rejected = _run(
        "config",
        "validate",
        "--release",
        str(bad_release),
        check=False,
    )
    assert rejected.returncode == 2
    assert "Additional properties are not allowed" in rejected.stderr

    schemas = tmp_path / "schemas"
    shutil.copytree(RELEASE_ROOT / "schemas", schemas)
    schema = schemas / "config.schema.json"
    text = schema.read_text(encoding="utf-8")
    schema.write_text(text.replace('"$schema":', '"$schema": "duplicate",\n  "$schema":', 1))
    duplicate = _run(
        "config",
        "validate",
        "--schema-dir",
        str(schemas),
        check=False,
    )
    assert duplicate.returncode == 2
    assert "duplicate JSON key" in duplicate.stderr


def test_schema_validation_is_operational_for_configs_and_manifest(tmp_path: Path) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    assert release["kind"] == "release-config"
    compatibility = yaml.safe_load((RELEASE_ROOT / "compatibility.yaml").read_text())
    assert compatibility["kind"] == "compatibility-config"
    schema = json.loads((RELEASE_ROOT / "schemas/config.schema.json").read_text())
    assert schema["oneOf"] == [
        {"$ref": "#/$defs/release"},
        {"$ref": "#/$defs/compatibility"},
    ]

    release["schema_version"] = "1"
    release_path, compatibility_path = _write_configs(tmp_path, release, compatibility)
    wrong_type = _run(
        "config", "validate", "--release", str(release_path),
        "--compatibility", str(compatibility_path), check=False,
    )
    assert wrong_type.returncode == 2
    assert "expected integer" in wrong_type.stderr

    schemas = tmp_path / "schemas"
    shutil.copytree(RELEASE_ROOT / "schemas", schemas)
    manifest_schema = json.loads((schemas / "release-manifest.schema.json").read_text())
    manifest_schema["properties"]["kind"]["const"] = "schema-was-not-applied"
    (schemas / "release-manifest.schema.json").write_text(
        json.dumps(manifest_schema), encoding="utf-8"
    )
    rejected_manifest = _run(
        "core", "plan", "--schema-dir", str(schemas), check=False
    )
    assert rejected_manifest.returncode == 2
    assert "schema-was-not-applied" in rejected_manifest.stderr


def test_schema_refs_enforce_required_enum_pattern_and_unique_items(tmp_path: Path) -> None:
    original = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    mutations: list[tuple[dict, str]] = []

    missing = json.loads(json.dumps(original))
    del missing["chart"]["name"]
    mutations.append((missing, "missing required properties"))

    wrong_enum = json.loads(json.dumps(original))
    wrong_enum["chart"]["validation_cases"][0]["name"] = "gpu"
    mutations.append((wrong_enum, "expected one of"))

    wrong_pattern = json.loads(json.dumps(original))
    wrong_pattern["chart"]["validation_cases"][0]["image_digest"] = "sha256:not-a-digest"
    mutations.append((wrong_pattern, "does not match pattern"))

    duplicate_array = json.loads(json.dumps(original))
    duplicate_array["wheel_profiles"][0]["cpu_arch"] = ["amd64", "amd64"]
    mutations.append((duplicate_array, "array items must be unique"))

    for index, (release, message) in enumerate(mutations):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        release_path, compatibility_path = _write_configs(case_dir, release)
        rejected = _run(
            "config", "validate", "--release", str(release_path),
            "--compatibility", str(compatibility_path), check=False,
        )
        assert rejected.returncode == 2
        assert message in rejected.stderr


def test_compatibility_and_profile_references_cannot_drift(tmp_path: Path) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    compatibility = yaml.safe_load((RELEASE_ROOT / "compatibility.yaml").read_text())
    compatibility["rules"][0]["accelerator_runtimes"] = ["cuda-12.9"]
    release_path, compatibility_path = _write_configs(tmp_path, release, compatibility)
    drift = _run(
        "config", "validate", "--release", str(release_path),
        "--compatibility", str(compatibility_path), check=False,
    )
    assert drift.returncode == 2
    assert "compatibility/profile drift" in drift.stderr


def test_lock_subjects_and_immutable_resolution_are_fail_closed(tmp_path: Path) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    release["wheel_profiles"][0]["locks"][0]["status"] = "resolved"
    release_path, compatibility_path = _write_configs(tmp_path, release)
    relabeled = _run(
        "core", "plan", "--release", str(release_path),
        "--compatibility", str(compatibility_path), check=False,
    )
    assert relabeled.returncode == 2
    assert "resolved builder lock requires immutable oci identity" in relabeled.stderr

    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    release["wheel_profiles"][0]["locks"][0].update(
        {
            "status": "resolved",
            "identity": "package://wrong-subject@sha256:" + "a" * 64,
        }
    )
    release_path, compatibility_path = _write_configs(tmp_path, release)
    wrong_subject = _run(
        "config", "validate", "--release", str(release_path),
        "--compatibility", str(compatibility_path), check=False,
    )
    assert wrong_subject.returncode == 2
    assert "resolved builder lock requires immutable oci identity" in wrong_subject.stderr

    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    schemes = {"builder": "oci", "toolchain": "toolchain"}
    for lock in release["wheel_profiles"][0]["locks"]:
        lock.update(
            {
                "status": "resolved",
                "identity": f"{schemes[lock['subject']]}://review-fixture/{lock['subject']}@sha256:"
                + "d" * 64,
            }
        )
    release["wheel_profiles"][0]["runner"].update(
        {
            "status": "resolved",
            "identity": "runner://review-fixture/cuda@sha256:" + "e" * 64,
        }
    )
    release_path, compatibility_path = _write_configs(tmp_path, release)
    resolved = _run(
        "core", "plan", "--release", str(release_path),
        "--compatibility", str(compatibility_path),
    )
    assert json.loads(resolved.stdout)["eligible_wheel_count"] == 2

    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    release["wheel_profiles"][2]["locks"] = [
        lock for lock in release["wheel_profiles"][2]["locks"] if lock["subject"] != "atb"
    ]
    release_path, compatibility_path = _write_configs(tmp_path, release)
    missing = _run(
        "config", "validate", "--release", str(release_path),
        "--compatibility", str(compatibility_path), check=False,
    )
    assert missing.returncode == 2
    assert "exact lock subjects" in missing.stderr
    assert "atb" in missing.stderr


def test_core_plan_keeps_all_specs_but_fails_publishable_closed(tmp_path: Path) -> None:
    output = tmp_path / "release-manifest.json"
    planned = _run("core", "plan", "--output", str(output))
    manifest = json.loads(planned.stdout)
    assert manifest == json.loads(output.read_text(encoding="utf-8"))
    assert manifest["ucm_version"] == "0.5.0rc1"
    assert manifest["declared_wheel_count"] == 36
    assert manifest["eligible_wheel_count"] == 0
    assert len(manifest["wheel_specs"]) == 36
    assert {item["accelerator"] for item in manifest["wheel_specs"]} == {
        "cuda",
        "ascend",
    }
    assert all(not item["build_eligible"] for item in manifest["wheel_specs"])
    assert all(item["blocked_reasons"] for item in manifest["wheel_specs"])
    assert {
        reason.split(":", 1)[0]
        for item in manifest["wheel_specs"]
        for reason in item["blocked_reasons"]
    } == {"unresolved-lock", "unresolved-runner"}
    assert manifest["publication"]["target"] == "github-release"
    assert {item["type"] for item in manifest["publication"]["assets"]} == {
        "wheel",
        "helm-chart",
    }
    assert "wrapt" not in json.dumps(manifest).lower()

    blocked = _run("core", "plan", "--require-publishable", check=False)
    assert blocked.returncode == 2
    assert "0 of 36 wheel specs are eligible" in blocked.stderr


def test_setup_chart_and_configuration_share_version_authority() -> None:
    version = (ROOT / "version.ini").read_text(encoding="utf-8").strip().split("=", 1)[1]
    setup_version = subprocess.run(
        [sys.executable, "setup.py", "--version"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    release_config = yaml.safe_load(
        (RELEASE_ROOT / "release.yaml").read_text(encoding="utf-8")
    )
    chart = yaml.safe_load((ROOT / "charts" / "ucm" / "Chart.yaml").read_text())
    assert setup_version == version == release_config["ucm_version"]
    assert str(chart["appVersion"]) == version
    assert chart["version"] == "0.5.0-rc.1"
    assert release_config["python_runtime_dependencies"] == ["wrapt==1.17.2"]
    assert derive_chart_version(version) == "0.5.0-rc.1"


def test_coordinated_config_version_drift_is_rejected(tmp_path: Path) -> None:
    release = yaml.safe_load((RELEASE_ROOT / "release.yaml").read_text())
    compatibility = yaml.safe_load((RELEASE_ROOT / "compatibility.yaml").read_text())
    release["ucm_version"] = release["chart"]["app_version"] = "0.5.0rc2"
    release["chart"]["version"] = "0.5.0-rc.2"
    compatibility["ucm_version"] = "0.5.0rc2"
    release_path, compatibility_path = _write_configs(tmp_path, release, compatibility)
    drift = _run(
        "config", "validate", "--release", str(release_path),
        "--compatibility", str(compatibility_path), check=False,
    )
    assert drift.returncode == 2
    assert "does not match version.ini" in drift.stderr


def test_wheel_inspection_binds_sha_metadata_version_and_spec(tmp_path: Path) -> None:
    wheel = _fixture_wheel(tmp_path)
    digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
    inspected = json.loads(
        _run(
            "wheel",
            "inspect",
            str(wheel),
            "--spec-id",
            "cuda-cu129-ubuntu2204-amd64-cp312-release-default-sm75-sm80-sm86-sm89-sm90",
            "--expected-sha256",
            digest,
            "--source-kind",
            "fixture",
        ).stdout
    )
    assert inspected["sha256"] == digest
    assert inspected["distribution"] == "uc-manager"
    assert inspected["version"] == "0.5.0rc1"
    assert inspected["requires_dist"] == ["wrapt==1.17.2"]
    assert inspected["python_abi"] == "cp312"
    assert inspected["cpu_arch"] == "amd64"
    assert inspected["status"] == "fixture-only"
    assert inspected["publication_eligible"] is False

    wrong = _run(
        "wheel",
        "inspect",
        str(wheel),
        "--spec-id",
        inspected["spec_id"],
        "--expected-sha256",
        "sha256:" + "0" * 64,
        "--source-kind",
        "fixture",
        check=False,
    )
    assert wrong.returncode == 2
    assert "wheel SHA256 mismatch" in wrong.stderr


def test_synthetic_wheel_cannot_cross_the_production_boundary(tmp_path: Path) -> None:
    spec_id = "cuda-cu129-ubuntu2204-amd64-cp312-release-default-sm75-sm80-sm86-sm89-sm90"
    synthetic = _fixture_wheel(tmp_path)
    digest = "sha256:" + hashlib.sha256(synthetic.read_bytes()).hexdigest()
    missing_record = _run(
        "wheel", "inspect", str(synthetic), "--spec-id", spec_id,
        "--expected-sha256", digest, "--source-kind", "production", check=False,
    )
    assert missing_record.returncode == 2
    assert "RECORD" in missing_record.stderr

    no_native = _production_shaped_wheel(tmp_path, include_native=False)
    digest = "sha256:" + hashlib.sha256(no_native.read_bytes()).hexdigest()
    missing_native = _run(
        "wheel", "inspect", str(no_native), "--spec-id", spec_id,
        "--expected-sha256", digest, "--source-kind", "production", check=False,
    )
    assert missing_native.returncode == 2
    assert "native custom-op shared object" in missing_native.stderr

    wrong_binding = _production_shaped_wheel(
        tmp_path, include_native=True, runtime="cuda-13.0"
    )
    digest = "sha256:" + hashlib.sha256(wrong_binding.read_bytes()).hexdigest()
    mismatched = _run(
        "wheel", "inspect", str(wrong_binding), "--spec-id", spec_id,
        "--expected-sha256", digest, "--source-kind", "production", check=False,
    )
    assert mismatched.returncode == 2
    assert "embedded build binding accelerator_runtime" in mismatched.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm 3 is required")
def test_chart_package_runs_cuda_a2_a3_and_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    record_a = json.loads(_run("chart", "package", "--output-dir", str(first)).stdout)
    record_b = json.loads(_run("chart", "package", "--output-dir", str(second)).stdout)
    assert record_a == record_b
    assert record_a["rendered_cases"] == ["cuda", "a2", "a3"]
    assert record_a["rendered_evidence"] == {
        "cuda": {
            "image": "registry.invalid/ucm/fixture-cuda@sha256:" + "a" * 64,
            "resource": "nvidia.com/gpu",
        },
        "a2": {
            "image": "registry.invalid/ucm/fixture-ascend-a2@sha256:" + "b" * 64,
            "resource": "huawei.com/Ascend910",
        },
        "a3": {
            "image": "registry.invalid/ucm/fixture-ascend-a3@sha256:" + "c" * 64,
            "resource": "huawei.com/Ascend910",
        },
    }
    assert record_a["checks"] == {
        "helm_lint": "passed",
        "helm_package": "passed",
        "helm_template": "passed",
    }
    assert record_a["publication_target"] == "github-release"
    archive_a = first / record_a["filename"]
    archive_b = second / record_b["filename"]
    assert archive_a.read_bytes() == archive_b.read_bytes()
    assert record_a["sha256"] == "sha256:" + hashlib.sha256(archive_a.read_bytes()).hexdigest()

    with tarfile.open(archive_a, "r:gz") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(member.name for member in members)
    assert all(member.uid == member.gid == 0 for member in members)
    assert all(member.uname == member.gname == "root" for member in members)
    assert all(member.mtime == 0 for member in members)
    assert all(member.mode == (0o755 if member.isdir() else 0o644) for member in members)

    provenance = json.loads(
        (ROOT / "charts" / "ucm" / "SOURCE_PROVENANCE.json").read_text(
            encoding="utf-8"
        )
    )
    assert provenance["source"] == {
        "commit": "33ac2a37f146a4515e232e4d7a8abaa14d8ef1d7",
        "remote": "https://github.com/SuperMarioYL/uc-stack.git",
        "tree_sha256": "sha256:5a0aa3113c14931e30c88c7f8508b3c742f985e5ede4a8ec48cac77c195c5a2e",
    }
    assert "/Users/" not in json.dumps(provenance)


@pytest.mark.parametrize(
    "member",
    ["/absolute", "chart/../escape", "chart//duplicate", "./chart/file"],
)
def test_chart_repacker_rejects_unsafe_or_noncanonical_paths(
    tmp_path: Path, member: str
) -> None:
    source = tmp_path / "unsafe.tgz"
    info = tarfile.TarInfo(member)
    info.size = 1
    _write_tar(source, [(info, b"x")])
    with pytest.raises(ValueError, match="unsafe or noncanonical Chart member"):
        _deterministic_repack(source, tmp_path / "out.tgz")


def test_chart_repacker_rejects_duplicate_and_non_regular_members(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.tgz"
    first = tarfile.TarInfo("chart/file")
    first.size = 1
    second = tarfile.TarInfo("chart/file")
    second.size = 1
    _write_tar(duplicate, [(first, b"a"), (second, b"b")])
    with pytest.raises(ValueError, match="duplicate Chart member"):
        _deterministic_repack(duplicate, tmp_path / "duplicate-out.tgz")

    linked = tmp_path / "linked.tgz"
    symlink = tarfile.TarInfo("chart/link")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "file"
    _write_tar(linked, [(symlink, None)])
    with pytest.raises(ValueError, match="unsupported member"):
        _deterministic_repack(linked, tmp_path / "linked-out.tgz")
