"""Small closed projections used to bridge trusted production Workflow jobs."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from .common import (
    ProductionError,
    load_json,
    sha256_envelope,
    verify_envelope,
    write_json,
)
from .config import derive_repository, validate_config
from .tags import reopen_intent


_SPECS = (
    "cuda130-amd64",
    "cuda130-arm64",
    "cann900-a2-amd64",
    "cann900-a2-arm64",
    "cann900-a3-amd64",
    "cann900-a3-arm64",
)
_PROFILES = ("cuda130", "cann900-a2", "cann900-a3")


def _digest(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise ProductionError(f"required production file is absent: {path.name}")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate(value: object) -> dict[str, Any]:
    return verify_envelope(
        value,
        kind="ucm-production-candidate-envelope",
        schema_version=1,
    )


def candidate_outputs(candidate_value: object) -> dict[str, str]:
    """Project only scalar Workflow outputs from a reopened candidate."""

    candidate = _candidate(candidate_value)
    source = candidate.get("source_identity")
    run = candidate.get("run")
    if not isinstance(source, dict) or not isinstance(run, dict):
        raise ProductionError("candidate source/run projections are invalid")
    return {
        "stage": str(candidate["stage"]),
        "candidate_sha256": str(candidate["sha256"]),
        "source_identity_b64": base64.b64encode(
            (json.dumps(source, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).decode(),
        "source_date_epoch": str(run["source_date_epoch"]),
    }


def _member_repository(
    config: dict[str, Any], candidate: dict[str, Any], spec_id: str
) -> str:
    profile = spec_id.rsplit("-", 1)[0]
    products = {item["profile_id"]: item for item in config["products"]["images"]}
    product = products[profile]
    basename = (
        product["draft_basename"]
        if candidate["stage"] == "draft"
        else product["basename"]
    )
    owner = candidate["repository"].split("/", 1)[0].lower()
    return f"ghcr.io/{owner}/{basename}"


def member_request(
    config_value: object,
    candidate_value: object,
    *,
    spec_id: str,
    layout: Path,
    candidate_root: Path,
) -> dict[str, Any]:
    """Build one registry member request from trusted config and candidate data."""

    if spec_id not in _SPECS:
        raise ProductionError("member request spec_id is invalid")
    config = validate_config(config_value)
    candidate = _candidate(candidate_value)
    intent = reopen_intent(candidate["intent"], config)
    members = [
        item for item in candidate["image_members"] if item["spec_id"] == spec_id
    ]
    if len(members) != 1:
        raise ProductionError("candidate member request is not unique")
    closure_path = Path(candidate_root) / members[0]["path"]
    closure = load_json(closure_path, "candidate member closure")
    if "sha256" not in closure:
        raise ProductionError("candidate member closure is not sealed")
    return {
        "stage": intent.stage,
        "spec_id": spec_id,
        "repository": _member_repository(config, candidate, spec_id),
        "tag": f"{intent.image_tag}-{spec_id.rsplit('-', 1)[1]}",
        "layout": str(Path(layout).resolve()),
        "closure": str(closure_path.resolve()),
        "visibility": config["channels"][intent.stage]["image_visibility"],
    }


def channel_requests(
    config_value: object,
    candidate_value: object,
    candidate_root: Path,
    member_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Create exact index and optional Chart requests from complete member records."""

    config = validate_config(config_value)
    candidate = _candidate(candidate_value)
    intent = reopen_intent(candidate["intent"], config)
    derived = derive_repository(
        config,
        repository=candidate["repository"],
        repository_id=candidate["repository_id"],
        default_branch=candidate["source_identity"]["control_default_branch"],
    )
    member_root = Path(member_root)
    records: dict[str, Path] = {}
    for spec_id in _SPECS:
        matches = list(member_root.rglob(f"{spec_id}.json"))
        if len(matches) != 1:
            raise ProductionError(f"member channel record is not unique: {spec_id}")
        record = verify_envelope(
            load_json(matches[0], f"member record {spec_id}"),
            kind="ucm-production-channel-record",
            schema_version=1,
        )
        if record.get("channel") != "ghcr-member" or record.get("status") != "complete":
            raise ProductionError(f"member channel is incomplete: {spec_id}")
        records[spec_id] = matches[0]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    index_requests: list[str] = []
    for position, profile in enumerate(_PROFILES):
        request = {
            "stage": intent.stage,
            "profile_id": profile,
            "repository": (
                derived["draft_image_repositories"][position]
                if intent.stage == "draft"
                else derived["image_repositories"][position]
            ),
            "tag": intent.image_tag,
            "source_sha": candidate["source_sha"],
            "member_records": [
                str(records[f"{profile}-amd64"].resolve()),
                str(records[f"{profile}-arm64"].resolve()),
            ],
            "visibility": config["channels"][intent.stage]["image_visibility"],
        }
        path = output_dir / f"index-{profile}.json"
        write_json(path, request, f"index request {profile}")
        index_requests.append(str(path.resolve()))
    result: dict[str, Any] = {"index_requests": index_requests, "chart_request": None}
    if config["channels"][intent.stage]["publish_chart"]:
        chart = candidate["chart"]
        chart_path = Path(candidate_root) / chart["path"]
        request = {
            "stage": intent.stage,
            "name": "unified-cache-pd",
            "version": intent.chart_version,
            "chart": str(chart_path.resolve()),
            "helm_repository": derived["chart_repository"].rsplit("/", 1)[0],
            "reference": f"{derived['chart_repository'].removeprefix('oci://')}:{intent.chart_version}",
            "file_sha256": chart["file_sha256"],
            "visibility": "public",
        }
        path = output_dir / "chart.json"
        write_json(path, request, "Chart publish request")
        result["chart_request"] = str(path.resolve())
    return result


def release_request(
    candidate_value: object,
    candidate_root: Path,
    environment_value: object,
    channel_root: Path,
    assets_dir: Path,
    output: Path,
) -> dict[str, Any]:
    """Build the exact GitHub Release request and deterministic support assets."""

    candidate = _candidate(candidate_value)
    environment = verify_envelope(
        environment_value,
        kind="ucm-production-environment-evidence",
        schema_version=1,
    )
    if environment.get("source_sha") != candidate["source_sha"]:
        raise ProductionError("release Environment source differs from candidate")
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=False)
    asset_paths: list[Path] = []
    for item in candidate["wheels"]:
        source = Path(candidate_root) / item["path"]
        target = assets_dir / source.name
        target.write_bytes(source.read_bytes())
        if _digest(target) != item["file_sha256"]:
            raise ProductionError("release wheel asset digest differs")
        asset_paths.append(target)
    chart_source = Path(candidate_root) / candidate["chart"]["path"]
    chart_target = assets_dir / chart_source.name
    chart_target.write_bytes(chart_source.read_bytes())
    if _digest(chart_target) != candidate["chart"]["file_sha256"]:
        raise ProductionError("release Chart asset digest differs")
    asset_paths.append(chart_target)
    write_json(
        assets_dir / "ucm-production-manifest.json",
        candidate,
        "production manifest asset",
    )
    write_json(
        assets_dir / "ucm-production-sbom.json",
        sha256_envelope(
            {
                "kind": "ucm-production-dependency-evidence",
                "schema_version": 1,
                "source_sha": candidate["source_sha"],
                "wheels": [
                    {
                        "spec_id": item["spec_id"],
                        "distribution": item["distribution"],
                        "file_sha256": item["file_sha256"],
                    }
                    for item in candidate["wheels"]
                ],
            }
        ),
        "production dependency evidence",
    )
    write_json(
        assets_dir / "ucm-production-environment.json",
        environment,
        "production Environment asset",
    )
    support = [
        assets_dir / "ucm-production-manifest.json",
        assets_dir / "ucm-production-sbom.json",
        assets_dir / "ucm-production-environment.json",
    ]
    checks = sorted([*asset_paths, *support], key=lambda path: path.name)
    (assets_dir / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in checks
        ),
        encoding="ascii",
    )
    asset_paths.extend([assets_dir / "SHA256SUMS", *support])
    records = []
    for path in sorted(Path(channel_root).rglob("*.json")):
        record = load_json(path, "production channel record")
        if (
            isinstance(record, dict)
            and record.get("kind") == "ucm-production-channel-record"
        ):
            records.append(str(path.resolve()))
    request = {
        "stage": candidate["stage"],
        "repository": candidate["repository"],
        "repository_id": candidate["repository_id"],
        "tag_name": candidate["tag_name"],
        "source_sha": candidate["source_sha"],
        "version": candidate["intent"]["version"],
        "candidate_sha256": candidate["sha256"],
        "environment_status": environment["status"],
        "assets": [str(path.resolve()) for path in asset_paths],
        "channel_records": records,
    }
    write_json(output, request, "GitHub Release request")
    return request


def channels_list(channel_root: Path, release_record: Path, output: Path) -> list[Any]:
    """Write the exact channel record list used by final evidence assembly."""

    records: list[Any] = []
    for path in sorted(Path(channel_root).rglob("*.json")):
        value = load_json(path, "production channel record")
        if (
            isinstance(value, dict)
            and value.get("kind") == "ucm-production-channel-record"
        ):
            records.append(value)
    records.append(load_json(release_record, "GitHub Release channel record"))
    write_json(output, records, "production channel list")
    return records
