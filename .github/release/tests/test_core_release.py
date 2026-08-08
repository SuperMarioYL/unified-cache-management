from __future__ import annotations

import hashlib
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
        ).stdout
    )
    assert inspected["sha256"] == digest
    assert inspected["distribution"] == "uc-manager"
    assert inspected["version"] == "0.5.0rc1"
    assert inspected["requires_dist"] == ["wrapt==1.17.2"]
    assert inspected["python_abi"] == "cp312"
    assert inspected["cpu_arch"] == "amd64"

    wrong = _run(
        "wheel",
        "inspect",
        str(wheel),
        "--spec-id",
        inspected["spec_id"],
        "--expected-sha256",
        "sha256:" + "0" * 64,
        check=False,
    )
    assert wrong.returncode == 2
    assert "wheel SHA256 mismatch" in wrong.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm 3 is required")
def test_chart_package_runs_cuda_a2_a3_and_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    record_a = json.loads(_run("chart", "package", "--output-dir", str(first)).stdout)
    record_b = json.loads(_run("chart", "package", "--output-dir", str(second)).stdout)
    assert record_a == record_b
    assert record_a["rendered_cases"] == ["cuda", "a2", "a3"]
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
