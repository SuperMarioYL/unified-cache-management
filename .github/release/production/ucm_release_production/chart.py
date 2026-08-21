"""Production-versioned deterministic Helm Chart package."""

from __future__ import annotations

import gzip
import hashlib
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .common import ProductionError, canonical_bytes, sha256_envelope


def _run(argv: list[str]) -> None:
    completed = subprocess.run(argv, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise ProductionError(f"Helm validation failed: {completed.stderr.strip()}")


def _tree(root: Path) -> str:
    records = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]
    if not records:
        raise ProductionError("Chart source tree is empty")
    return "sha256:" + hashlib.sha256(canonical_bytes(records)).hexdigest()


def _repack(source: Path, destination: Path) -> None:
    members: list[tuple[str, int, bytes]] = []
    seen: set[str] = set()
    with tarfile.open(source, "r:gz") as archive:
        for item in archive.getmembers():
            name = item.name.rstrip("/")
            parsed = PurePosixPath(name)
            if (
                not name
                or parsed.is_absolute()
                or "\\" in name
                or any(part in {"", ".", ".."} for part in parsed.parts)
                or name in seen
                or not (item.isfile() or item.isdir())
            ):
                raise ProductionError("Helm package contains an unsafe member")
            seen.add(name)
            stream = archive.extractfile(item) if item.isfile() else None
            members.append((name, item.type, stream.read() if stream else b""))
    with destination.open("xb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as out:
                for name, kind, data in sorted(members):
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
                    info.size = len(data)
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = "root"
                    out.addfile(info, io.BytesIO(data) if data else None)


def package_chart(
    chart_dir: Path,
    output_dir: Path,
    *,
    chart_version: str,
    app_version: str,
    source_sha: str,
) -> dict[str, Any]:
    chart_dir = Path(chart_dir)
    output_dir = Path(output_dir)
    if not chart_dir.is_dir() or chart_dir.is_symlink():
        raise ProductionError("Chart source must be a real directory")
    validation_values = chart_dir / "models" / "cuda" / "values-qwen3-0p6b-1e1.yaml"
    if not validation_values.is_file() or validation_values.is_symlink():
        raise ProductionError("Chart validation values must be a real file")
    validation_args = ["--values", str(validation_values)]
    _run(["helm", "lint", str(chart_dir), *validation_args])
    _run(
        [
            "helm",
            "template",
            "ucm-production",
            str(chart_dir),
            *validation_args,
        ]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"unified-cache-pd-{chart_version}.tgz"
    destination = output_dir / filename
    with tempfile.TemporaryDirectory(prefix="ucm-production-chart-") as temporary:
        _run(
            [
                "helm",
                "package",
                str(chart_dir),
                "--destination",
                temporary,
                "--version",
                chart_version,
                "--app-version",
                app_version,
            ]
        )
        _repack(Path(temporary) / filename, destination)
    _run(["helm", "lint", str(destination), *validation_args])
    record = sha256_envelope(
        {
            "kind": "ucm-production-chart-record",
            "schema_version": 1,
            "name": "unified-cache-pd",
            "version": chart_version,
            "filename": filename,
            "file_sha256": "sha256:"
            + hashlib.sha256(destination.read_bytes()).hexdigest(),
            "content_tree_sha256": _tree(chart_dir),
            "lint": "passed",
            "template": "passed",
            "source_sha": source_sha,
        }
    )
    (output_dir / "record.json").write_bytes(canonical_bytes(record) + b"\n")
    return record
