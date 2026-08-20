"""Seal and reopen the exact read-only production candidate bundle."""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .build import compare_wheel_candidates
from .common import (
    ProductionError,
    canonical_bytes,
    load_json,
    require_exact_keys,
    require_lower_commit_sha,
    require_posix_path,
    require_string,
    sha256_envelope,
    verify_envelope,
)

EXPECTED_IMAGE_SPECS = (
    "cuda130-amd64",
    "cuda130-arm64",
    "cann900-a2-amd64",
    "cann900-a2-arm64",
    "cann900-a3-amd64",
    "cann900-a3-arm64",
)
_PROFILES = ("cuda130", "cann900-a2", "cann900-a3")
_ARCH_BY_SPEC = {spec_id: spec_id.rsplit("-", 1)[1] for spec_id in EXPECTED_IMAGE_SPECS}
_DISTRIBUTIONS = {
    "cuda130": "uc-manager-cuda",
    "cann900-a2": "uc-manager-cann-a2",
    "cann900-a3": "uc-manager-cann-a3",
}
_INTENT_KEYS = {
    "kind",
    "schema_version",
    "stage",
    "tag_name",
    "version",
    "wheel_version",
    "chart_version",
    "image_tag",
    "release_branch",
    "draft_number",
    "rc_number",
    "sha256",
}
_RUN_KEYS = {
    "kind",
    "schema_version",
    "repository",
    "repository_id",
    "workflow_id",
    "workflow_path",
    "event",
    "run_id",
    "run_attempt",
    "source_date_epoch",
    "head_sha",
    "tag_name",
    "artifact_name",
    "sha256",
}
_SOURCE_KEYS = {
    "kind",
    "schema_version",
    "repository",
    "repository_id",
    "stage",
    "tag_name",
    "tag_object_sha",
    "source_commit_sha",
    "source_branch",
    "tagger",
    "tagged_at",
    "tag_message_sha256",
    "control_default_branch",
    "control_sha",
    "lineage",
    "sha256",
}
_WHEEL_RECORD_KEYS = {
    "kind",
    "schema_version",
    "spec_id",
    "distribution",
    "version",
    "filename",
    "file_sha256",
    "task_sha256",
    "source_sha",
    "runtime_requirements",
    "sha256",
}
_CHART_RECORD_KEYS = {
    "kind",
    "schema_version",
    "name",
    "version",
    "filename",
    "file_sha256",
    "content_tree_sha256",
    "lint",
    "template",
    "source_sha",
    "sha256",
}
_IMAGE_MEMBER_KEYS = {
    "kind",
    "schema_version",
    "spec_id",
    "platform",
    "source_sha",
    "task_sha256",
    "wheel_sha256",
    "recipe_sha256",
    "manifest_digest",
    "manifest_size",
    "config_digest",
    "config_size",
    "layers",
    "annotations",
    "sha256",
}
_INDEX_KEYS = {
    "kind",
    "schema_version",
    "profile_id",
    "image_tag",
    "members",
    "sha256",
}
_EXPECTED_KEYS = {
    "repository",
    "repository_id",
    "tag_name",
    "tag_object_sha",
    "source_sha",
    "run_id",
    "run_attempt",
    "artifact_name",
}
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_MAX_MEMBERS = 32
_MAX_MEMBER_BYTES = 1024 * 1024 * 1024
_MAX_TOTAL_BYTES = 6 * 1024 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ProductionError(
            f"cannot hash candidate file {path.name}: {error}"
        ) from None
    return "sha256:" + digest.hexdigest()


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionError(f"{label} must be an object")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ProductionError(f"{label} must be a positive integer")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_DIGEST.fullmatch(value) is None:
        raise ProductionError(f"{label} must be sha256:<64 lowercase hex>")
    return value


def _profile(spec_id: str) -> str:
    for profile in _PROFILES:
        if spec_id.startswith(profile + "-"):
            return profile
    raise ProductionError(f"unknown production image spec: {spec_id}")


def _validate_intent(value: object) -> dict[str, Any]:
    intent = verify_envelope(
        value,
        kind="ucm-production-tag-intent",
        schema_version=1,
        exact_keys=_INTENT_KEYS,
    )
    for key in ("stage", "tag_name", "version", "wheel_version", "chart_version"):
        require_string(intent[key], f"tag intent {key}")
    return intent


def _validate_run(value: object) -> dict[str, Any]:
    run = verify_envelope(
        value,
        kind="ucm-production-candidate-run",
        schema_version=1,
        exact_keys=_RUN_KEYS,
    )
    require_string(run["repository"], "candidate run repository")
    _positive_int(run["repository_id"], "candidate run repository_id")
    _positive_int(run["workflow_id"], "candidate run workflow_id")
    _positive_int(run["run_id"], "candidate run run_id")
    _positive_int(run["run_attempt"], "candidate run run_attempt")
    if (
        type(run["source_date_epoch"]) is not int
        or not 315532800 <= run["source_date_epoch"] <= 4354819199
    ):
        raise ProductionError("candidate run source_date_epoch is invalid")
    if run["workflow_path"] != ".github/workflows/production-tag-candidate.yml":
        raise ProductionError("candidate run workflow path is invalid")
    if run["event"] != "push":
        raise ProductionError("candidate run event must be push")
    require_lower_commit_sha(run["head_sha"], "candidate run head_sha")
    require_string(run["tag_name"], "candidate run tag_name")
    require_string(run["artifact_name"], "candidate run artifact_name")
    return run


def candidate_run_document(
    *,
    repository: str,
    repository_id: int,
    workflow_id: int,
    run_id: int,
    run_attempt: int,
    source_sha: str,
    tag_name: str,
    tag_object_sha: str,
    source_date_epoch: int,
) -> dict[str, Any]:
    """Build the only candidate run projection accepted by the aggregate sealer."""

    require_string(repository, "candidate run repository")
    _positive_int(repository_id, "candidate run repository_id")
    _positive_int(workflow_id, "candidate run workflow_id")
    _positive_int(run_id, "candidate run run_id")
    _positive_int(run_attempt, "candidate run run_attempt")
    require_lower_commit_sha(source_sha, "candidate run source SHA")
    require_lower_commit_sha(tag_object_sha, "candidate run Tag object SHA")
    require_string(tag_name, "candidate run Tag")
    if (
        type(source_date_epoch) is not int
        or not 315532800 <= source_date_epoch <= 4354819199
    ):
        raise ProductionError("candidate run source_date_epoch is invalid")
    artifact_name = (
        f"ucm-production-candidate-{repository_id}-{tag_object_sha}-"
        f"{source_sha}-{run_id}-{run_attempt}"
    )
    return sha256_envelope(
        {
            "kind": "ucm-production-candidate-run",
            "schema_version": 1,
            "repository": repository,
            "repository_id": repository_id,
            "workflow_id": workflow_id,
            "workflow_path": ".github/workflows/production-tag-candidate.yml",
            "event": "push",
            "run_id": run_id,
            "run_attempt": run_attempt,
            "source_date_epoch": source_date_epoch,
            "head_sha": source_sha,
            "tag_name": tag_name,
            "artifact_name": artifact_name,
        }
    )


def _validate_source(value: object) -> dict[str, Any]:
    source = verify_envelope(
        value,
        kind="ucm-production-source-identity",
        schema_version=1,
        exact_keys=_SOURCE_KEYS,
    )
    require_string(source["repository"], "source repository")
    _positive_int(source["repository_id"], "source repository_id")
    for key in ("tag_object_sha", "source_commit_sha", "control_sha"):
        require_lower_commit_sha(source[key], f"source {key}")
    return source


def _candidate_paths(root: Path, *, ignore_envelope: bool = False) -> dict[str, Path]:
    if not root.is_dir():
        raise ProductionError("candidate root must be a directory")
    result: dict[str, Path] = {}
    try:
        paths = sorted(root.rglob("*"), key=lambda item: item.as_posix())
    except OSError as error:
        raise ProductionError(f"cannot enumerate candidate root: {error}") from None
    for path in paths:
        if path.is_symlink():
            raise ProductionError(f"candidate contains a symbolic link: {path.name}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ProductionError(
                f"candidate member is not a regular file: {path.name}"
            )
        relative = path.relative_to(root).as_posix()
        require_posix_path(relative, "candidate member path")
        if ignore_envelope and relative == "candidate-envelope.json":
            continue
        result[relative] = path
    return result


def _load_envelope(
    path: Path, *, kind: str, keys: set[str], label: str
) -> dict[str, Any]:
    return verify_envelope(
        load_json(path, label),
        kind=kind,
        schema_version=1,
        exact_keys=keys,
    )


def _validate_wheels(
    root: Path, files: dict[str, Path], intent: dict[str, Any], source_sha: str
) -> tuple[list[dict[str, Any]], set[str]]:
    results: list[dict[str, Any]] = []
    expected_paths: set[str] = set()
    for spec_id in EXPECTED_IMAGE_SPECS:
        record_relative = f"wheels/{spec_id}/record.json"
        if record_relative not in files:
            raise ProductionError(f"wheel record is missing for {spec_id}")
        record = _load_envelope(
            files[record_relative],
            kind="ucm-production-wheel-record",
            keys=_WHEEL_RECORD_KEYS,
            label=f"wheel record {spec_id}",
        )
        profile = _profile(spec_id)
        filename = require_posix_path(record["filename"], "wheel filename")
        if "/" in filename or not filename.endswith(".whl"):
            raise ProductionError(f"wheel filename is invalid for {spec_id}")
        wheel_relative = f"wheels/{spec_id}/{filename}"
        if wheel_relative not in files:
            raise ProductionError(f"wheel bytes are missing for {spec_id}")
        if (
            record["spec_id"] != spec_id
            or record["distribution"] != _DISTRIBUTIONS[profile]
            or record["version"] != intent["wheel_version"]
            or record["source_sha"] != source_sha
        ):
            raise ProductionError(
                f"wheel record semantic identity differs for {spec_id}"
            )
        file_digest = _digest(record["file_sha256"], "wheel file_sha256")
        _digest(record["task_sha256"], "wheel task_sha256")
        if _sha256(files[wheel_relative]) != file_digest:
            raise ProductionError(f"wheel file digest differs for {spec_id}")
        results.append(
            {
                "spec_id": spec_id,
                "distribution": record["distribution"],
                "version": record["version"],
                "path": wheel_relative,
                "file_sha256": file_digest,
                "record_path": record_relative,
                "record_sha256": "sha256:" + record["sha256"],
                "task_sha256": record["task_sha256"],
                "runtime_requirements": record["runtime_requirements"],
            }
        )
        expected_paths.update({record_relative, wheel_relative})
    return results, expected_paths


def _validate_chart(
    files: dict[str, Path], intent: dict[str, Any], source_sha: str
) -> tuple[dict[str, Any], set[str]]:
    record_relative = "chart/record.json"
    if record_relative not in files:
        raise ProductionError("chart record is missing")
    record = _load_envelope(
        files[record_relative],
        kind="ucm-production-chart-record",
        keys=_CHART_RECORD_KEYS,
        label="chart record",
    )
    filename = require_posix_path(record["filename"], "chart filename")
    if "/" in filename or not filename.endswith(".tgz"):
        raise ProductionError("chart filename is invalid")
    chart_relative = f"chart/{filename}"
    if chart_relative not in files:
        raise ProductionError("chart package is missing")
    if (
        record["name"] != "unified-cache-pd"
        or record["version"] != intent["chart_version"]
        or record["source_sha"] != source_sha
        or record["lint"] != "passed"
        or record["template"] != "passed"
    ):
        raise ProductionError("chart record semantic identity differs")
    file_digest = _digest(record["file_sha256"], "chart file_sha256")
    _digest(record["content_tree_sha256"], "chart content_tree_sha256")
    if _sha256(files[chart_relative]) != file_digest:
        raise ProductionError("chart file digest differs")
    return (
        {
            "name": record["name"],
            "version": record["version"],
            "path": chart_relative,
            "file_sha256": file_digest,
            "content_tree_sha256": record["content_tree_sha256"],
            "record_path": record_relative,
            "record_sha256": "sha256:" + record["sha256"],
        },
        {record_relative, chart_relative},
    )


def _validate_layers(value: object, spec_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ProductionError(f"image layers must be a non-empty array for {spec_id}")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        layer = _object(item, f"image layer {index}")
        require_exact_keys(layer, {"digest", "diff_id", "size"}, "image layer")
        _digest(layer["digest"], "image layer digest")
        _digest(layer["diff_id"], "image layer diff_id")
        _positive_int(layer["size"], "image layer size")
        result.append(dict(layer))
    return result


def _validate_image_members(
    files: dict[str, Path],
    wheels: list[dict[str, Any]],
    intent: dict[str, Any],
    source_sha: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    wheel_by_spec = {item["spec_id"]: item for item in wheels}
    results: list[dict[str, Any]] = []
    paths: set[str] = set()
    for spec_id in EXPECTED_IMAGE_SPECS:
        relative = f"images/{spec_id}/closure.json"
        if relative not in files:
            raise ProductionError(f"image member closure is missing for {spec_id}")
        record = _load_envelope(
            files[relative],
            kind="ucm-production-image-member-closure",
            keys=_IMAGE_MEMBER_KEYS,
            label=f"image member closure {spec_id}",
        )
        architecture = _ARCH_BY_SPEC[spec_id]
        if (
            record["spec_id"] != spec_id
            or record["platform"] != f"linux/{architecture}"
            or record["source_sha"] != source_sha
            or record["task_sha256"] != wheel_by_spec[spec_id]["task_sha256"]
            or record["wheel_sha256"] != wheel_by_spec[spec_id]["file_sha256"]
        ):
            raise ProductionError(
                f"image member source or task identity differs for {spec_id}"
            )
        for key in (
            "task_sha256",
            "wheel_sha256",
            "recipe_sha256",
            "manifest_digest",
            "config_digest",
        ):
            _digest(record[key], f"image member {key}")
        _positive_int(record["manifest_size"], "image manifest size")
        _positive_int(record["config_size"], "image config size")
        layers = _validate_layers(record["layers"], spec_id)
        annotations = _object(record["annotations"], "image annotations")
        require_exact_keys(
            annotations,
            {"org.opencontainers.image.revision", "org.opencontainers.image.version"},
            "image annotations",
        )
        if (
            annotations["org.opencontainers.image.revision"] != source_sha
            or annotations["org.opencontainers.image.version"] != intent["image_tag"]
        ):
            raise ProductionError(f"image annotations differ for {spec_id}")
        results.append(
            {
                "spec_id": spec_id,
                "profile_id": _profile(spec_id),
                "platform": record["platform"],
                "manifest_digest": record["manifest_digest"],
                "manifest_size": record["manifest_size"],
                "config_digest": record["config_digest"],
                "config_size": record["config_size"],
                "layers": layers,
                "recipe_sha256": record["recipe_sha256"],
                "path": relative,
                "record_sha256": "sha256:" + record["sha256"],
            }
        )
        paths.add(relative)
    return results, paths


def _validate_indexes(
    files: dict[str, Path], members: list[dict[str, Any]], intent: dict[str, Any]
) -> tuple[list[dict[str, Any]], set[str]]:
    by_profile = {
        profile: [
            {
                "spec_id": item["spec_id"],
                "platform": item["platform"],
                "manifest_digest": item["manifest_digest"],
            }
            for item in members
            if item["profile_id"] == profile
        ]
        for profile in _PROFILES
    }
    results: list[dict[str, Any]] = []
    paths: set[str] = set()
    for profile in _PROFILES:
        relative = f"indexes/{profile}/index.json"
        if relative not in files:
            raise ProductionError(f"image index identity is missing for {profile}")
        record = _load_envelope(
            files[relative],
            kind="ucm-production-image-index-identity",
            keys=_INDEX_KEYS,
            label=f"image index identity {profile}",
        )
        if (
            record["profile_id"] != profile
            or record["image_tag"] != intent["image_tag"]
            or record["members"] != by_profile[profile]
        ):
            raise ProductionError(f"image index member closure differs for {profile}")
        results.append(
            {
                "profile_id": profile,
                "image_tag": record["image_tag"],
                "members": record["members"],
                "path": relative,
                "record_sha256": "sha256:" + record["sha256"],
            }
        )
        paths.add(relative)
    return results, paths


def seal_candidate(
    root: Path, intent_value: object, run_value: object
) -> dict[str, Any]:
    """Validate an exact candidate tree and bind every child into one envelope."""

    root = Path(root)
    intent = _validate_intent(intent_value)
    run = _validate_run(run_value)
    files = _candidate_paths(root, ignore_envelope=True)
    source_relative = "source-identity.json"
    if source_relative not in files:
        raise ProductionError("source identity is missing")
    source = _validate_source(load_json(files[source_relative], "source identity"))
    source_sha = source["source_commit_sha"]
    if (
        source["repository"] != run["repository"]
        or source["repository_id"] != run["repository_id"]
        or source["stage"] != intent["stage"]
        or source["tag_name"] != intent["tag_name"]
        or source_sha != run["head_sha"]
        or run["tag_name"] != intent["tag_name"]
    ):
        raise ProductionError("candidate source, run, and Tag intent do not agree")
    artifact_name = (
        f"ucm-production-candidate-{run['repository_id']}-"
        f"{source['tag_object_sha']}-{source_sha}-{run['run_id']}-{run['run_attempt']}"
    )
    if run["artifact_name"] != artifact_name:
        raise ProductionError("candidate run artifact name is not identity-bound")

    wheels, wheel_paths = _validate_wheels(root, files, intent, source_sha)
    chart, chart_paths = _validate_chart(files, intent, source_sha)
    image_members, member_paths = _validate_image_members(
        files, wheels, intent, source_sha
    )
    image_indexes, index_paths = _validate_indexes(files, image_members, intent)
    expected_paths = {
        source_relative,
        *wheel_paths,
        *chart_paths,
        *member_paths,
        *index_paths,
    }
    if set(files) != expected_paths:
        raise ProductionError(
            "candidate exact file set differs; "
            f"missing={sorted(expected_paths - set(files))}; "
            f"extra={sorted(set(files) - expected_paths)}"
        )
    file_records = [
        {
            "path": relative,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for relative, path in sorted(files.items())
    ]
    return sha256_envelope(
        {
            "kind": "ucm-production-candidate-envelope",
            "schema_version": 1,
            "repository": run["repository"],
            "repository_id": run["repository_id"],
            "stage": intent["stage"],
            "tag_name": intent["tag_name"],
            "tag_object_sha": source["tag_object_sha"],
            "source_sha": source_sha,
            # This is the candidate's declared control snapshot. A write-capable
            # controller must never trust it; GitHub API identity supplies the
            # current default-branch control SHA independently.
            "control_sha": source["control_sha"],
            "run_id": run["run_id"],
            "run_attempt": run["run_attempt"],
            "artifact_name": artifact_name,
            "intent": intent,
            "source_identity": source,
            "run": run,
            "files": file_records,
            "wheels": wheels,
            "chart": chart,
            "image_members": image_members,
            "image_indexes": image_indexes,
        }
    )


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def pack_candidate(root: Path, envelope: object, output: Path) -> None:
    """Write deterministic candidate archive bytes without overwriting output."""

    value = _object(envelope, "candidate envelope")
    intent = value.get("intent")
    run = value.get("run")
    rebuilt = seal_candidate(Path(root), intent, run)
    if rebuilt != value:
        raise ProductionError("candidate envelope differs from the validated tree")
    output = Path(output)
    if output.exists():
        raise ProductionError("candidate archive output already exists")
    files = _candidate_paths(Path(root), ignore_envelope=True)
    try:
        with zipfile.ZipFile(
            output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            archive.writestr(
                _zip_info("candidate-envelope.json"), canonical_bytes(value) + b"\n"
            )
            for relative, path in sorted(files.items()):
                archive.writestr(_zip_info(relative), path.read_bytes())
    except (OSError, zipfile.BadZipFile) as error:
        if output.exists():
            output.unlink()
        raise ProductionError(f"cannot create candidate archive: {error}") from None


def _safe_zip_name(name: str) -> bool:
    if not name or name.endswith("/") or "\\" in name:
        return False
    try:
        require_posix_path(name, "candidate archive member")
    except ProductionError:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(
        part not in {"", ".", ".."} for part in path.parts
    )


def _validate_zip_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    names = [item.filename for item in infos]
    if len(infos) > _MAX_MEMBERS:
        raise ProductionError("candidate archive exceeds the member count limit")
    if len(names) != len(set(names)):
        raise ProductionError("candidate archive contains duplicate members")
    if any(not _safe_zip_name(name) for name in names):
        raise ProductionError("candidate archive contains an unsafe member path")
    total = 0
    for item in infos:
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ProductionError("candidate archive contains a symbolic link")
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG}:
            raise ProductionError("candidate archive contains a non-regular member")
        if item.file_size > _MAX_MEMBER_BYTES:
            raise ProductionError("candidate archive member exceeds the size limit")
        total += item.file_size
        if total > _MAX_TOTAL_BYTES:
            raise ProductionError("candidate archive exceeds the total size limit")
    return infos


def _extract_archive(
    archive: zipfile.ZipFile, root: Path, infos: list[zipfile.ZipInfo]
) -> None:
    for item in infos:
        destination = root / item.filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with archive.open(item, "r") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        except OSError as error:
            raise ProductionError(f"cannot extract candidate member: {error}") from None


@dataclass
class CandidateBundle:
    """Only validated paths from one private extraction directory."""

    envelope: dict[str, Any]
    root: Path
    wheel_paths: dict[str, Path]
    chart_path: Path
    _temporary: tempfile.TemporaryDirectory[str]

    def close(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> CandidateBundle:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def reopen_candidate(zip_path: Path, expected_value: object) -> CandidateBundle:
    """Safely extract, rehash, and semantically reopen a candidate archive."""

    expected = _object(expected_value, "candidate expected identity")
    require_exact_keys(expected, _EXPECTED_KEYS, "candidate expected identity")
    temporary = tempfile.TemporaryDirectory(prefix="ucm-production-candidate-")
    root = Path(temporary.name)
    try:
        try:
            with zipfile.ZipFile(zip_path) as archive:
                infos = _validate_zip_members(archive)
                if "candidate-envelope.json" not in {item.filename for item in infos}:
                    raise ProductionError("candidate envelope member is missing")
                _extract_archive(archive, root, infos)
        except zipfile.BadZipFile:
            raise ProductionError("candidate archive is not a valid ZIP") from None
        envelope_path = root / "candidate-envelope.json"
        envelope = verify_envelope(
            load_json(envelope_path, "candidate envelope"),
            kind="ucm-production-candidate-envelope",
            schema_version=1,
        )
        identity = {
            "repository": envelope.get("repository"),
            "repository_id": envelope.get("repository_id"),
            "tag_name": envelope.get("tag_name"),
            "tag_object_sha": envelope.get("tag_object_sha"),
            "source_sha": envelope.get("source_sha"),
            "run_id": envelope.get("run_id"),
            "run_attempt": envelope.get("run_attempt"),
            "artifact_name": envelope.get("artifact_name"),
        }
        for key in sorted(_EXPECTED_KEYS):
            if identity[key] != expected[key]:
                raise ProductionError(f"candidate {key} differs from expected identity")
        actual_files = _candidate_paths(root, ignore_envelope=True)
        file_records = envelope.get("files")
        if not isinstance(file_records, list):
            raise ProductionError("candidate envelope files must be an array")
        expected_files: dict[str, dict[str, Any]] = {}
        for item in file_records:
            record = _object(item, "candidate file record")
            require_exact_keys(
                record, {"path", "size", "sha256"}, "candidate file record"
            )
            relative = require_posix_path(record["path"], "candidate file path")
            if relative in expected_files:
                raise ProductionError(
                    "candidate envelope contains duplicate file paths"
                )
            if type(record["size"]) is not int or record["size"] < 0:
                raise ProductionError(
                    "candidate file size must be a non-negative integer"
                )
            _digest(record["sha256"], "candidate file digest")
            expected_files[relative] = record
        if set(actual_files) != set(expected_files):
            raise ProductionError("candidate archive file set differs from envelope")
        for relative, path in actual_files.items():
            record = expected_files[relative]
            if (
                path.stat().st_size != record["size"]
                or _sha256(path) != record["sha256"]
            ):
                raise ProductionError(f"candidate file digest differs for {relative}")
        rebuilt = seal_candidate(root, envelope.get("intent"), envelope.get("run"))
        if rebuilt != envelope:
            raise ProductionError(
                "candidate envelope semantic content differs on reopen"
            )
        wheel_paths = {
            item["spec_id"]: root / item["path"] for item in envelope["wheels"]
        }
        chart_path = root / envelope["chart"]["path"]
        return CandidateBundle(envelope, root, wheel_paths, chart_path, temporary)
    except Exception:
        temporary.cleanup()
        raise


def compare_trusted_rebuild(
    bundle: CandidateBundle, trusted_root: Path
) -> dict[str, Any]:
    """Require exactly six trusted wheels and byte equality with the candidate."""

    trusted_root = Path(trusted_root)
    if not trusted_root.is_dir():
        raise ProductionError("trusted rebuild root must be a directory")
    candidate_records = {item["spec_id"]: item for item in bundle.envelope["wheels"]}
    trusted_files = _candidate_paths(trusted_root)
    expected: dict[str, Path] = {}
    results: list[dict[str, Any]] = []
    for spec_id in EXPECTED_IMAGE_SPECS:
        record = candidate_records[spec_id]
        filename = Path(record["path"]).name
        relative = f"{spec_id}/{filename}"
        if relative not in trusted_files:
            raise ProductionError(f"trusted rebuild wheel is missing for {spec_id}")
        expected[relative] = trusted_files[relative]
        task = {
            "distribution": record["distribution"],
            "wheel_version": record["version"],
            "cpu_arch": _ARCH_BY_SPEC[spec_id],
            "sha256": record["task_sha256"].removeprefix("sha256:"),
            "runtime_requirements": record["runtime_requirements"],
        }
        compared = compare_wheel_candidates(
            bundle.wheel_paths[spec_id], trusted_files[relative], task
        )
        results.append({"spec_id": spec_id, **compared})
    if set(trusted_files) != set(expected):
        raise ProductionError("trusted rebuild exact file set differs")
    return sha256_envelope(
        {
            "kind": "ucm-production-trusted-wheel-comparison",
            "schema_version": 1,
            "candidate_sha256": bundle.envelope["sha256"],
            "identical": True,
            "wheels": results,
        }
    )
