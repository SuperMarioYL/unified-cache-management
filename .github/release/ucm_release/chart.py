"""Validate and deterministically package the product Helm Chart."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from .core import DEFAULT_COMPATIBILITY, DEFAULT_RELEASE, DEFAULT_SCHEMA_DIR, REPO_ROOT, canonical_bytes, validate_config


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode:
        raise ValueError(
            f"command failed ({' '.join(command)}):\n{completed.stdout}{completed.stderr}"
        )
    return completed.stdout


def _tree_sha(entries: list[dict[str, str]], digest_field: str) -> str:
    normalized = [
        {"path": item["path"], "sha256": item[digest_field]}
        for item in sorted(entries, key=lambda item: item["path"])
    ]
    return "sha256:" + hashlib.sha256(canonical_bytes(normalized)).hexdigest()


def verify_provenance(chart_dir: Path) -> dict[str, Any]:
    provenance_path = chart_dir / "SOURCE_PROVENANCE.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if "/Users/" in json.dumps(provenance):
        raise ValueError("Chart provenance contains an absolute local path")
    expected_source = {
        "commit": "33ac2a37f146a4515e232e4d7a8abaa14d8ef1d7",
        "remote": "https://github.com/SuperMarioYL/uc-stack.git",
        "tree_sha256": "sha256:5a0aa3113c14931e30c88c7f8508b3c742f985e5ede4a8ec48cac77c195c5a2e",
    }
    if provenance.get("source") != expected_source:
        raise ValueError("Chart provenance does not retain the immutable source identity")
    if provenance.get("source_tree_sha256") != expected_source["tree_sha256"]:
        raise ValueError("Chart source tree digest disagrees with immutable source identity")
    source_files = provenance.get("source_files", [])
    additions = provenance.get("release_additions", [])
    entries = source_files + additions
    actual_paths = {
        path.relative_to(chart_dir).as_posix()
        for path in chart_dir.rglob("*")
        if path.is_file() and path.name != "SOURCE_PROVENANCE.json"
    }
    expected_paths = {item["path"] for item in entries}
    if actual_paths != expected_paths:
        raise ValueError(
            "Chart provenance file set mismatch: "
            f"missing={sorted(expected_paths - actual_paths)}, "
            f"unexpected={sorted(actual_paths - expected_paths)}"
        )
    for item in entries:
        digest = "sha256:" + hashlib.sha256((chart_dir / item["path"]).read_bytes()).hexdigest()
        if digest != item["imported_sha256"]:
            raise ValueError(f"Chart provenance digest mismatch: {item['path']}")
    if _tree_sha(source_files, "source_sha256") != provenance["source_tree_sha256"]:
        raise ValueError("Chart source tree digest is not reproducible")
    if _tree_sha(source_files, "imported_sha256") != provenance["imported_tree_sha256"]:
        raise ValueError("Chart imported tree digest is not reproducible")
    if _tree_sha(entries, "imported_sha256") != provenance["release_tree_sha256"]:
        raise ValueError("Chart release tree digest is not reproducible")
    return provenance


def _deterministic_repack(source: Path, destination: Path) -> None:
    records: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(source, "r:gz") as archive:
        for member in archive.getmembers():
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"Chart package contains unsupported member: {member.name}")
            data = archive.extractfile(member).read() if member.isfile() else None
            records.append((member, data))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as output:
                for original, data in sorted(records, key=lambda item: item[0].name):
                    normalized = tarfile.TarInfo(original.name)
                    normalized.type = original.type
                    normalized.mode = original.mode
                    normalized.size = len(data) if data is not None else 0
                    normalized.uid = 0
                    normalized.gid = 0
                    normalized.uname = "root"
                    normalized.gname = "root"
                    normalized.mtime = 0
                    output.addfile(normalized, io.BytesIO(data) if data is not None else None)


def package_chart(
    output_dir: Path,
    *,
    release_path: Path = DEFAULT_RELEASE,
    compatibility_path: Path = DEFAULT_COMPATIBILITY,
    schema_dir: Path = DEFAULT_SCHEMA_DIR,
) -> dict[str, Any]:
    release, _ = validate_config(release_path, compatibility_path, schema_dir)
    chart_config = release["chart"]
    chart_dir = REPO_ROOT / chart_config["source"]
    provenance = verify_provenance(chart_dir)
    rendered_cases: list[str] = []
    _run(["helm", "lint", str(chart_dir)])
    for case in chart_config["validation_cases"]:
        values = REPO_ROOT / case["values"]
        args = ["--values", str(values)]
        _run(["helm", "lint", str(chart_dir), *args])
        rendered = _run(
            ["helm", "template", f"ucm-{case['name']}", str(chart_dir), *args]
        )
        if "kind: ModelServing" not in rendered:
            raise ValueError(f"Chart case {case['name']} did not render ModelServing")
        rendered_cases.append(case["name"])
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{chart_config['name']}-{chart_config['version']}.tgz"
    destination = output_dir / filename
    with tempfile.TemporaryDirectory() as temporary:
        helm_output = Path(temporary)
        _run(
            [
                "helm",
                "package",
                str(chart_dir),
                "--destination",
                str(helm_output),
                "--version",
                chart_config["version"],
                "--app-version",
                chart_config["app_version"],
            ]
        )
        source_archive = helm_output / filename
        _deterministic_repack(source_archive, destination)
    _run(["helm", "lint", str(destination)])
    digest = "sha256:" + hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "kind": "ucm-chart-candidate",
        "filename": filename,
        "sha256": digest,
        "chart_name": chart_config["name"],
        "chart_version": chart_config["version"],
        "app_version": chart_config["app_version"],
        "source_commit": provenance["source"]["commit"],
        "source_tree_sha256": provenance["source"]["tree_sha256"],
        "release_tree_sha256": provenance["release_tree_sha256"],
        "rendered_cases": rendered_cases,
        "checks": {
            "helm_lint": "passed",
            "helm_package": "passed",
            "helm_template": "passed",
        },
        "publication_target": "github-release",
        "status": "candidate-verified",
    }
