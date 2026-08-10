"""Deterministic, fixture-only evidence for the registry reconciliation loop."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import stat
import tempfile
import urllib.parse
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from . import chart, image, wheel
from .core import (
    build_matrix,
    build_release_manifest,
    canonical_bytes,
    sha256_value,
    validate_config,
)
from .registry import (
    STAGING_REPOSITORY,
    TARGET_REPOSITORIES,
    RegistryBlocker,
    build_candidate,
    canonical_registry_contract,
    inventory_digest,
    parse_upstream_tag,
    reconcile,
    scan_registry,
    validate_public_tag,
    validate_snapshot,
)

EXPECTED_BLOCKERS = [
    "duplicate-conflicting-inventory",
    "missing-linux-arm64",
    "production-wheel-unpublished",
]
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY_RE = re.compile(
    r"[a-z0-9]+(?:[._:-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+"
)
OPERATION_CONTRACTS = MappingProxyType(
    {
        "fixture-read": ("read", "upstream-tag"),
        "crane-digest": ("read", "upstream-tag"),
        "crane-manifest": ("read", "upstream-digest"),
        "registry-inventory-read": ("read", "digest"),
        "build-plan": ("plan", "target-tag"),
        "registry-member-push-by-digest": ("write", "staging-digest"),
        "registry-staging-tag-create": ("write", "staging-tag"),
        "registry-index-create": ("write", "public-target"),
        "registry-authenticated-digest-read": (
            "read",
            "registry-read-tag-or-digest",
        ),
        "registry-authenticated-manifest-read": ("read", "registry-read-digest"),
        "registry-authenticated-config-blob-read": (
            "read",
            "registry-read-digest",
        ),
        "registry-authenticated-layer-blob-read": (
            "read",
            "registry-read-digest",
        ),
        "registry-anonymous-digest-read": ("read", "registry-read-tag"),
        "registry-anonymous-manifest-read": ("read", "registry-read-digest"),
        "registry-anonymous-config-blob-read": ("read", "registry-read-digest"),
        "registry-anonymous-layer-blob-read": ("read", "registry-read-digest"),
        "registry-anonymous-prewrite-visibility-read": ("read", "staging-tag"),
        "registry-authenticated-staging-prewrite-read": (
            "read",
            "staging-tag",
        ),
        "registry-anonymous-visibility-read": ("read", "staging-tag"),
        "registry-authenticated-recursive-validate": (
            "read",
            "registry-read-digest",
        ),
        "registry-anonymous-recursive-validate": (
            "read",
            "registry-read-digest",
        ),
    }
)
KNOWN_WRITE_OPERATION_TYPES = frozenset(
    {
        "registry-push",
        "registry-copy",
        "registry-tag",
        "crane-push",
        "crane-copy",
        "crane-tag",
        "registry-member-push-by-digest",
        "registry-staging-tag-create",
        "registry-index-create",
    }
)
WORKFLOW_REFS = [
    "release-ucm.yml",
    "_build-wheel.yml",
    "release-vllm-images.yml",
    "_build-image.yml",
]
REQUIRED_SCENARIOS = [
    "new-input-one-task",
    "identical-input-zero-tasks",
    "tag-digest-drift-r2",
    "complete-digest-chain",
    "required-failures-block",
    "fixture-candidate-full-zero-reconcile",
]
REQUIRED_IMAGE_GATES = {
    "base_verified",
    "wheel_verified",
    "install",
    "pip_check",
    "direct_url",
    "ucm_import",
    "wrapt_import",
    "abi",
}


def _source_sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("source SHA must be a full lowercase Git commit")
    return value


def run_bound_artifact_name(
    logical_name: object, run_id: object, run_attempt: object
) -> str:
    """Bind physical Actions Artifact identity to one workflow run attempt."""
    if (
        not isinstance(logical_name, str)
        or not logical_name
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", logical_name) is None
    ):
        raise ValueError("logical artifact name is invalid")
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"[1-9][0-9]*", run_id) is None
        or not isinstance(run_attempt, int)
        or isinstance(run_attempt, bool)
        or run_attempt < 1
    ):
        raise ValueError("artifact run identity is invalid")
    return f"{logical_name}-run-{run_id}-attempt-{run_attempt}"


def validate_run_bound_artifact_name(
    physical_name: object, logical_name: object, run: object
) -> str:
    """Reject stale/cross-attempt Artifact names before reopening payload bytes."""
    if not isinstance(run, dict) or set(run) != {"run_id", "run_attempt"}:
        raise ValueError("artifact run envelope is invalid")
    expected = run_bound_artifact_name(logical_name, run["run_id"], run["run_attempt"])
    if physical_name != expected:
        raise ValueError("physical artifact name is not bound to this run attempt")
    return expected


def resolve_run_bound_artifact_directories(
    root: Path,
    logical_names: object,
    *,
    run: object,
    label: str,
) -> dict[str, Path]:
    """Resolve an exact set of downloaded Artifact directories for one attempt."""
    root = Path(root)
    if (
        not root.is_dir()
        or root.is_symlink()
        or not isinstance(logical_names, list)
        or not logical_names
        or any(not isinstance(item, str) for item in logical_names)
        or len(set(logical_names)) != len(logical_names)
        or not isinstance(label, str)
        or not label
    ):
        raise ValueError(f"{label or 'run-bound'} artifact root/set is invalid")
    physical_by_logical = {
        logical_name: validate_run_bound_artifact_name(
            run_bound_artifact_name(
                logical_name,
                run.get("run_id") if isinstance(run, dict) else None,
                run.get("run_attempt") if isinstance(run, dict) else None,
            ),
            logical_name,
            run,
        )
        for logical_name in logical_names
    }
    observed = {
        path.name for path in root.iterdir() if path.is_dir() and not path.is_symlink()
    }
    expected = set(physical_by_logical.values())
    if observed != expected:
        raise ValueError(
            f"{label} artifact directories are missing, stale, or extra for this attempt"
        )
    return {
        logical_name: root / physical_name
        for logical_name, physical_name in physical_by_logical.items()
    }


def github_release_authority(source_sha: object) -> dict[str, Any]:
    """Return the non-configurable GitHub prerelease identity for this release."""
    source_sha = _source_sha(source_sha)
    return {
        "tag_name": "v0.5.0rc1",
        "target_commitish": source_sha,
        "name": "UCM v0.5.0rc1",
        "body": (
            "Protected UCM v0.5.0rc1 release from reviewed source commit "
            + source_sha
            + "."
        ),
        "draft": True,
        "prerelease": True,
        "make_latest": "false",
    }


def plan_github_release(
    remote: object | None,
    source_sha: object,
    *,
    just_created: bool = False,
) -> dict[str, Any]:
    """Plan create-or-reopen without replacing a foreign or conflicting release."""
    authority = github_release_authority(source_sha)
    if not isinstance(just_created, bool):
        raise ValueError("release creation marker must be boolean")
    if remote is None:
        if just_created:
            raise ValueError("just-created release is missing")
        payload = {
            "schema_version": 1,
            "kind": "ucm-github-release-state-plan",
            "decision": "create",
            "authority": authority,
            "release_id": None,
            "upload_url": None,
            "asset_count": 0,
        }
        return {**payload, "plan_sha256": sha256_value(payload)}
    if not isinstance(remote, dict):
        raise ValueError("GitHub release state must be an object or null")
    release_id = remote.get("id")
    assets = remote.get("assets")
    upload_url = remote.get("upload_url")
    api_url = remote.get("url")
    assets_url = remote.get("assets_url")
    author = remote.get("author")
    expected_api_url = (
        "https://api.github.com/repos/SuperMarioYL/unified-cache-management/"
        f"releases/{release_id}"
    )
    expected_upload_url = (
        "https://uploads.github.com/repos/SuperMarioYL/"
        f"unified-cache-management/releases/{release_id}/assets{{?name,label}}"
    )
    expected_assets_url = expected_api_url + "/assets"
    if (
        not isinstance(release_id, int)
        or isinstance(release_id, bool)
        or release_id < 1
        or not isinstance(assets, list)
        or any(not isinstance(item, dict) for item in assets)
        or api_url != expected_api_url
        or assets_url != expected_assets_url
        or upload_url != expected_upload_url
    ):
        raise ValueError("GitHub release transport identity is malformed")
    if (
        not isinstance(author, dict)
        or author.get("login") != "github-actions[bot]"
        or author.get("type") != "Bot"
    ):
        raise ValueError("GitHub release author identity is malformed")
    exact_fields = {
        "tag_name": authority["tag_name"],
        "name": authority["name"],
        "body": authority["body"],
    }
    if (
        any(remote.get(key) != value for key, value in exact_fields.items())
        or remote.get("prerelease") is not True
    ):
        raise ValueError("GitHub release differs from exact protected authority")
    draft = remote.get("draft")
    if not isinstance(draft, bool):
        raise ValueError("GitHub release draft state is malformed")
    if just_created and (not draft or assets):
        raise ValueError("a just-created GitHub release must be an empty draft")
    if just_created:
        decision = "reuse-draft"
    elif draft:
        decision = "resume-draft"
    else:
        decision = "inspect-published-prerelease"
    payload = {
        "schema_version": 1,
        "kind": "ucm-github-release-state-plan",
        "decision": decision,
        "authority": authority,
        "release_id": release_id,
        "upload_url": expected_upload_url.removesuffix("{?name,label}"),
        "assets_url": expected_assets_url,
        "author": author["login"],
        "asset_count": len(assets),
    }
    return {**payload, "plan_sha256": sha256_value(payload)}


def _canonical_release_asset_authorities() -> list[dict[str, Any]]:
    tasks = build_matrix("protected-tag")["tasks"]
    authorities: list[dict[str, Any]] = []
    for task in tasks:
        architecture = "x86_64" if task["cpu_arch"] == "amd64" else "aarch64"
        authorities.append(
            {
                "spec_id": task["spec_id"],
                "profile_id": task["profile_id"],
                "platform": task["platform"],
                "name": (
                    f"uc_manager-{task['wheel_version']}-{task['python_abi']}-"
                    f"{task['python_abi']}-{task['wheel_platform']}_{architecture}.whl"
                ),
                "type": "wheel",
            }
        )
    release, _ = validate_config()
    chart_authority = release.get("chart")
    if not isinstance(chart_authority, dict):
        raise ValueError("release Chart authority is missing")
    authorities.append(
        {
            "spec_id": "helm-chart",
            "profile_id": None,
            "platform": None,
            "name": f"{chart_authority['name']}-{chart_authority['version']}.tgz",
            "type": "helm-chart",
        }
    )
    return authorities


def _stream_sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def _release_asset_identity_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest["schema_version"],
        "kind": manifest["kind"],
        "source_sha": manifest["source_sha"],
        "assets": [
            {key: copy.deepcopy(value) for key, value in asset.items() if key != "path"}
            for asset in manifest["assets"]
        ],
    }


def build_release_asset_manifest(
    *,
    wheel_dir: Path,
    chart_result_path: Path,
    chart_package_path: Path,
    output_dir: Path,
    source_sha: object,
    run: object,
) -> dict[str, Any]:
    """Reopen current-attempt wheel/Chart artifacts and stage exact seven files."""
    source_sha = _source_sha(source_sha)
    wheel_root = Path(wheel_dir)
    output_root = Path(output_dir)
    if output_root.exists():
        if (
            output_root.is_symlink()
            or not output_root.is_dir()
            or any(output_root.iterdir())
        ):
            raise ValueError("release asset output directory must be empty")
    else:
        output_root.mkdir(parents=True)
    reviewed = build_matrix("protected-tag")["tasks"]
    reviewed_by_spec = {item["spec_id"]: item for item in reviewed}
    logical_names = [f"ucm-wheel-{item['spec_id']}-{source_sha}" for item in reviewed]
    directories = resolve_run_bound_artifact_directories(
        wheel_root,
        logical_names,
        run=run,
        label="release wheel",
    )
    task_records: dict[str, dict[str, Any]] = {}
    for logical_name in logical_names:
        task = _load_canonical_json(
            directories[logical_name] / "hosted-task.json",
            "release hosted wheel task",
        )
        spec_id = task.get("spec_id")
        if not isinstance(spec_id, str) or spec_id in task_records:
            raise ValueError("release hosted wheel task is duplicated or malformed")
        task_records[spec_id] = task
    epochs = {item.get("source_date_epoch") for item in task_records.values()}
    if len(epochs) != 1:
        raise ValueError("release hosted wheel tasks disagree on source epoch")
    expected_matrix = hosted_build_matrix(source_sha, next(iter(epochs)))
    expected_tasks = {item["spec_id"]: item for item in expected_matrix["tasks"]}
    if task_records != expected_tasks:
        raise ValueError("release hosted wheel tasks differ from reviewed matrix")
    authorities = _canonical_release_asset_authorities()
    authority_by_spec = {item["spec_id"]: item for item in authorities}
    assets: list[dict[str, Any]] = []
    for spec_id in [item["spec_id"] for item in reviewed]:
        task = expected_tasks[spec_id]
        directory = directories[task["wheel_artifact"]]
        wheel_path = _one_file(directory, "*.whl", f"{spec_id} release wheel")
        authority = authority_by_spec[spec_id]
        if wheel_path.name != authority["name"]:
            raise ValueError(f"{spec_id} wheel filename is noncanonical")
        inspection_path = directory / "wheel-inspection.json"
        seal_path = directory / "wheel-seal.json"
        source_context_path = directory / "source-context.json"
        inspection = _load_canonical_json(
            inspection_path, f"{spec_id} release wheel inspection"
        )
        seal = _load_canonical_json(seal_path, f"{spec_id} release wheel seal")
        source_context = _load_canonical_json(
            source_context_path, f"{spec_id} release source context"
        )
        wheel_sha256, wheel_size = _stream_sha256_and_size(wheel_path)
        reopened = wheel.inspect_wheel(
            wheel_path, spec_id, wheel_sha256, "builder-candidate"
        )
        builder = reopened.get("builder_evidence")
        if (
            inspection != reopened
            or seal.get("source_kind") != "builder-candidate"
            or seal.get("publication_status") != "unpublished"
            or seal.get("publication_eligible") is not False
            or seal.get("spec_id") != spec_id
            or seal.get("source_sha") != source_sha
            or seal.get("build_key") != task["task_sha256"]
            or seal.get("wheel_sha256") != wheel_sha256
            or seal.get("inspection_sha256") != _file_sha256(inspection_path)
            or not isinstance(builder, dict)
            or builder.get("source_commit") != source_sha
            or builder.get("build_key") != task["task_sha256"]
            or builder.get("source_date_epoch") != expected_matrix["source_date_epoch"]
            or source_context.get("source_sha") != source_sha
            or source_context.get("build_context_sha256")
            != builder.get("build_context_digest")
            or reviewed_by_spec[spec_id]["profile_id"] != authority["profile_id"]
            or reviewed_by_spec[spec_id]["platform"] != authority["platform"]
        ):
            raise ValueError(f"{spec_id} release wheel closure does not reopen")
        destination = output_root / wheel_path.name
        shutil.copyfile(wheel_path, destination)
        assets.append(
            {
                **copy.deepcopy(authority),
                "sha256": wheel_sha256,
                "size": wheel_size,
                "path": str(destination),
            }
        )
    chart_summary = _real_chart_summary(
        Path(chart_result_path), Path(chart_package_path)
    )
    chart_authority = authority_by_spec["helm-chart"]
    if chart_summary.get("filename") != chart_authority["name"] or chart_summary.get(
        "sha256"
    ) != _file_sha256(Path(chart_package_path)):
        raise ValueError("release Chart artifact differs from canonical authority")
    chart_destination = output_root / chart_authority["name"]
    shutil.copyfile(Path(chart_package_path), chart_destination)
    assets.append(
        {
            **copy.deepcopy(chart_authority),
            "sha256": chart_summary["sha256"],
            "size": chart_destination.stat().st_size,
            "path": str(chart_destination),
        }
    )
    manifest = {
        "schema_version": 1,
        "kind": "ucm-github-release-assets",
        "source_sha": source_sha,
        "assets": assets,
    }
    manifest["assets_sha256"] = sha256_value(_release_asset_identity_payload(manifest))
    return validate_release_asset_manifest(manifest, allowed_root=output_root)


def validate_release_asset_manifest(
    manifest: object, *, allowed_root: Path
) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "kind",
        "source_sha",
        "assets",
        "assets_sha256",
    }:
        raise ValueError("release asset manifest fields are noncanonical")
    if (
        not isinstance(manifest["schema_version"], int)
        or isinstance(manifest["schema_version"], bool)
        or manifest["schema_version"] != 1
        or manifest["kind"] != "ucm-github-release-assets"
    ):
        raise ValueError("release asset manifest identity is invalid")
    _source_sha(manifest["source_sha"])
    assets = manifest["assets"]
    if not isinstance(assets, list) or len(assets) != 7:
        raise ValueError("release asset manifest requires exactly seven assets")
    root = Path(allowed_root)
    try:
        root_stat = root.lstat()
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ValueError("release asset allowlisted root is missing") from error
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
        raise ValueError("release asset allowlisted root must be a real directory")
    authorities = _canonical_release_asset_authorities()
    if [item.get("spec_id") for item in assets] != [
        item["spec_id"] for item in authorities
    ]:
        raise ValueError(
            "release assets do not cover the canonical six specs and Chart"
        )
    names: set[str] = set()
    for asset, authority in zip(assets, authorities, strict=True):
        if not isinstance(asset, dict) or set(asset) != {
            "spec_id",
            "profile_id",
            "platform",
            "name",
            "sha256",
            "size",
            "type",
            "path",
        }:
            raise ValueError("release asset record fields are noncanonical")
        name = asset["name"]
        if (
            any(asset.get(key) != value for key, value in authority.items())
            or not isinstance(name, str)
            or not name
            or "/" in name
            or "\\" in name
            or name in names
            or DIGEST_RE.fullmatch(str(asset["sha256"])) is None
            or not isinstance(asset["size"], int)
            or isinstance(asset["size"], bool)
            or asset["size"] < 1
            or not isinstance(asset["path"], str)
            or not asset["path"]
        ):
            raise ValueError("release asset identity is invalid")
        path = Path(asset["path"])
        try:
            path_stat = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"release asset path is missing: {name}") from error
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path.is_symlink()
            or path.name != name
            or resolved.parent != resolved_root
        ):
            raise ValueError("release asset path is outside the allowlisted root")
        actual_sha256, actual_size = _stream_sha256_and_size(path)
        if actual_sha256 != asset["sha256"] or actual_size != asset["size"]:
            raise ValueError(f"release asset bytes differ from manifest: {name}")
        names.add(name)
    payload = _release_asset_identity_payload(manifest)
    if manifest["assets_sha256"] != sha256_value(payload):
        raise ValueError("release asset manifest digest mismatch")
    return copy.deepcopy(manifest)


def _validate_remote_release_asset(
    item: dict[str, Any],
    *,
    expected: dict[str, Any],
    release_id: int,
) -> dict[str, Any]:
    required = {
        "release_id",
        "asset_id",
        "name",
        "size",
        "state",
        "digest",
        "api_url",
        "browser_download_url",
        "uploader",
        "download_sha256",
        "download_size",
    }
    if set(item) != required:
        raise ValueError("observed release asset fields are noncanonical")
    asset_id = item["asset_id"]
    if (
        not isinstance(item["release_id"], int)
        or isinstance(item["release_id"], bool)
        or item["release_id"] != release_id
        or not isinstance(asset_id, int)
        or isinstance(asset_id, bool)
        or asset_id < 1
        or item["name"] != expected["name"]
        or item["state"] != "uploaded"
        or not isinstance(item["size"], int)
        or isinstance(item["size"], bool)
        or item["size"] != expected["size"]
        or item["digest"] != expected["sha256"]
        or item["download_sha256"] != expected["sha256"]
        or not isinstance(item["download_size"], int)
        or isinstance(item["download_size"], bool)
        or item["download_size"] != expected["size"]
    ):
        raise ValueError(f"GitHub Release asset conflict: {item.get('name')}")
    expected_api_url = (
        "https://api.github.com/repos/SuperMarioYL/unified-cache-management/"
        f"releases/assets/{asset_id}"
    )
    browser = urllib.parse.urlsplit(str(item["browser_download_url"]))
    expected_prefix = (
        "/SuperMarioYL/unified-cache-management/releases/download/v0.5.0rc1/"
    )
    if (
        item["api_url"] != expected_api_url
        or browser.scheme != "https"
        or browser.netloc != "github.com"
        or not browser.path.startswith(expected_prefix)
        or urllib.parse.unquote(browser.path.removeprefix(expected_prefix))
        != expected["name"]
        or browser.query
        or browser.fragment
    ):
        raise ValueError("GitHub Release asset transport identity is invalid")
    uploader = item["uploader"]
    if (
        not isinstance(uploader, dict)
        or set(uploader) != {"login", "type"}
        or uploader.get("type") != "Bot"
        or uploader.get("login") != "github-actions[bot]"
    ):
        raise ValueError("GitHub Release asset uploader identity is invalid")
    return copy.deepcopy(item)


def plan_release_assets(
    expected_manifest: object,
    observed_assets: object,
    *,
    release_id: int,
    allowed_root: Path,
    release_published: bool = False,
) -> dict[str, Any]:
    """Reuse exact bytes, upload only absences, and reject every foreign/conflict."""
    if (
        not isinstance(release_id, int)
        or isinstance(release_id, bool)
        or release_id < 1
        or not isinstance(release_published, bool)
    ):
        raise ValueError("release asset plan identity is invalid")
    manifest = validate_release_asset_manifest(
        expected_manifest, allowed_root=allowed_root
    )
    if not isinstance(observed_assets, list) or any(
        not isinstance(item, dict) for item in observed_assets
    ):
        raise ValueError("observed release assets must be an array of objects")
    expected = {item["name"]: item for item in manifest["assets"]}
    observed: dict[str, dict[str, Any]] = {}
    observed_ids: set[int] = set()
    for item in observed_assets:
        name = item["name"]
        if name not in expected:
            raise ValueError(f"foreign GitHub Release asset: {name}")
        if name in observed:
            raise ValueError(f"duplicate GitHub Release asset: {name}")
        validated = _validate_remote_release_asset(
            item, expected=expected[name], release_id=release_id
        )
        if validated["asset_id"] in observed_ids:
            raise ValueError("duplicate GitHub Release asset id")
        observed_ids.add(validated["asset_id"])
        observed[name] = validated
    ordered_names = [item["name"] for item in manifest["assets"]]
    upload_names = [name for name in ordered_names if name not in observed]
    if release_published and upload_names:
        raise ValueError("published prerelease reuse requires exact seven assets")
    payload = {
        "schema_version": 1,
        "kind": "ucm-github-release-asset-plan",
        "source_sha": manifest["source_sha"],
        "assets_sha256": manifest["assets_sha256"],
        "asset_count": 7,
        "release_id": release_id,
        "release_published": release_published,
        "reuse_names": [name for name in ordered_names if name in observed],
        "reuse_assets": [observed[name] for name in ordered_names if name in observed],
        "upload_names": upload_names,
    }
    return {**payload, "plan_sha256": sha256_value(payload)}


def verify_release_assets(
    expected_manifest: object,
    observed_assets: object,
    *,
    release_id: int,
    allowed_root: Path,
) -> dict[str, Any]:
    """Require exact seven API records and downloaded byte hashes before publish/reuse."""
    plan = plan_release_assets(
        expected_manifest,
        observed_assets,
        release_id=release_id,
        allowed_root=allowed_root,
        release_published=True,
    )
    payload = {
        "schema_version": 1,
        "kind": "ucm-github-release-assets-verification",
        "release_id": release_id,
        "assets_sha256": plan["assets_sha256"],
        "verified_names": copy.deepcopy(plan["reuse_names"]),
        "remote_assets_sha256": sha256_value(plan["reuse_assets"]),
    }
    return {**payload, "verification_sha256": sha256_value(payload)}


def select_github_release_pages(pages: object, source_sha: object) -> dict[str, Any]:
    """Select the unique protected tag from a complete paginated REST listing."""
    source_sha = _source_sha(source_sha)
    if not isinstance(pages, list) or any(
        not isinstance(page, list) or any(not isinstance(item, dict) for item in page)
        for page in pages
    ):
        raise ValueError("GitHub Release pages are malformed")
    matches = [
        item for page in pages for item in page if item.get("tag_name") == "v0.5.0rc1"
    ]
    if len(matches) > 1:
        raise ValueError("duplicate protected GitHub Release tag")
    remote = copy.deepcopy(matches[0]) if matches else None
    payload = {
        "schema_version": 1,
        "kind": "ucm-github-release-list-selection",
        "source_sha": source_sha,
        "remote": remote,
        "plan_request": {
            "remote": copy.deepcopy(remote),
            "source_sha": source_sha,
            "just_created": False,
        },
        "create_request": github_release_authority(source_sha),
        "operations": [
            {
                "type": "github-release-list",
                "capability": "read",
                "reference": (
                    "https://api.github.com/repos/SuperMarioYL/"
                    "unified-cache-management/releases"
                ),
                "authenticated": True,
            }
        ],
    }
    return {**payload, "selection_sha256": sha256_value(payload)}


def _flatten_release_asset_pages(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("GitHub Release asset pages must be an array")
    if not value:
        return []
    if all(isinstance(item, list) for item in value):
        raw = [entry for page in value for entry in page]
    elif all(isinstance(item, dict) for item in value):
        raw = list(value)
    else:
        raise ValueError("GitHub Release asset pages are mixed or malformed")
    if any(not isinstance(item, dict) for item in raw):
        raise ValueError("GitHub Release asset entry is malformed")
    return copy.deepcopy(raw)


def plan_release_asset_downloads(
    expected_manifest: object,
    raw_assets: object,
    *,
    release_id: int,
    allowed_root: Path,
    require_complete: bool,
) -> dict[str, Any]:
    """Validate REST metadata before downloading canonical asset names or URLs."""
    if not isinstance(require_complete, bool):
        raise ValueError("release asset download completeness is malformed")
    manifest = validate_release_asset_manifest(
        expected_manifest, allowed_root=allowed_root
    )
    raw = _flatten_release_asset_pages(raw_assets)
    expected = {item["name"]: item for item in manifest["assets"]}
    by_name: dict[str, dict[str, Any]] = {}
    ids: set[int] = set()
    for item in raw:
        name = item.get("name")
        if not isinstance(name, str) or name not in expected:
            raise ValueError(f"foreign GitHub Release asset: {name}")
        if name in by_name:
            raise ValueError(f"duplicate GitHub Release asset: {name}")
        asset_id = item.get("id")
        raw_uploader = item.get("uploader")
        if not isinstance(raw_uploader, dict):
            raise ValueError("GitHub Release asset uploader is malformed")
        candidate = {
            "release_id": release_id,
            "asset_id": asset_id,
            "name": name,
            "size": item.get("size"),
            "state": item.get("state"),
            "digest": item.get("digest"),
            "api_url": item.get("url"),
            "browser_download_url": item.get("browser_download_url"),
            "uploader": {
                "login": raw_uploader.get("login"),
                "type": raw_uploader.get("type"),
            },
            "download_sha256": expected[name]["sha256"],
            "download_size": expected[name]["size"],
        }
        validated = _validate_remote_release_asset(
            candidate, expected=expected[name], release_id=release_id
        )
        if validated["asset_id"] in ids:
            raise ValueError("duplicate GitHub Release asset id")
        ids.add(validated["asset_id"])
        by_name[name] = validated
    ordered_names = [item["name"] for item in manifest["assets"]]
    if require_complete and set(by_name) != set(ordered_names):
        raise ValueError("GitHub Release download plan requires exact seven assets")
    downloads = [
        {
            "release_id": release_id,
            "asset_id": by_name[name]["asset_id"],
            "name": name,
            "size": by_name[name]["size"],
            "state": by_name[name]["state"],
            "digest": by_name[name]["digest"],
            "api_url": by_name[name]["api_url"],
            "browser_download_url": by_name[name]["browser_download_url"],
            "uploader": copy.deepcopy(by_name[name]["uploader"]),
            "expected_sha256": expected[name]["sha256"],
            "expected_size": expected[name]["size"],
        }
        for name in ordered_names
        if name in by_name
    ]
    payload = {
        "schema_version": 1,
        "kind": "ucm-github-release-asset-download-plan",
        "release_id": release_id,
        "assets_sha256": manifest["assets_sha256"],
        "require_complete": require_complete,
        "downloads": downloads,
    }
    return {**payload, "plan_sha256": sha256_value(payload)}


def complete_release_asset_downloads(
    download_plan: object, download_root: Path
) -> list[dict[str, Any]]:
    """Rehash downloaded bytes and emit canonical REST observations."""
    if (
        not isinstance(download_plan, dict)
        or set(download_plan)
        != {
            "schema_version",
            "kind",
            "release_id",
            "assets_sha256",
            "require_complete",
            "downloads",
            "plan_sha256",
        }
        or download_plan.get("schema_version") != 1
        or isinstance(download_plan.get("schema_version"), bool)
        or download_plan.get("kind") != "ucm-github-release-asset-download-plan"
        or download_plan.get("plan_sha256")
        != sha256_value(
            {key: value for key, value in download_plan.items() if key != "plan_sha256"}
        )
        or not isinstance(download_plan.get("downloads"), list)
    ):
        raise ValueError("release asset download plan is invalid")
    root = Path(download_root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("release asset download root is invalid")
    expected_names = [item.get("name") for item in download_plan["downloads"]]
    if any(not isinstance(name, str) for name in expected_names) or len(
        expected_names
    ) != len(set(expected_names)):
        raise ValueError("release asset download names are invalid")
    entries = list(root.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ValueError(
            "release asset download directory contains a non-regular entry"
        )
    observed_names = {path.name for path in entries}
    if observed_names != set(expected_names):
        raise ValueError("release asset download directory is incomplete or foreign")
    normalized: list[dict[str, Any]] = []
    for item in download_plan["downloads"]:
        if not isinstance(item, dict):
            raise ValueError("release asset download entry is malformed")
        path = root / item["name"]
        digest, size = _stream_sha256_and_size(path)
        if digest != item.get("expected_sha256") or size != item.get("expected_size"):
            raise ValueError(
                f"GitHub Release downloaded bytes conflict: {item['name']}"
            )
        normalized.append(
            {
                "release_id": item["release_id"],
                "asset_id": item["asset_id"],
                "name": item["name"],
                "size": item["size"],
                "state": item["state"],
                "digest": item["digest"],
                "api_url": item["api_url"],
                "browser_download_url": item["browser_download_url"],
                "uploader": copy.deepcopy(item["uploader"]),
                "download_sha256": digest,
                "download_size": size,
            }
        )
    return normalized


def refresh_release_asset_metadata(
    expected_manifest: object,
    prior_assets: object,
    raw_assets: object,
    *,
    release_id: int,
    allowed_root: Path,
) -> list[dict[str, Any]]:
    """Bind a fresh metadata listing to the previously rehashed exact bytes."""
    prior = plan_release_assets(
        expected_manifest,
        prior_assets,
        release_id=release_id,
        allowed_root=allowed_root,
        release_published=True,
    )["reuse_assets"]
    plan = plan_release_asset_downloads(
        expected_manifest,
        raw_assets,
        release_id=release_id,
        allowed_root=allowed_root,
        require_complete=True,
    )
    prior_by_id = {item["asset_id"]: item for item in prior}
    refreshed: list[dict[str, Any]] = []
    for item in plan["downloads"]:
        expected = prior_by_id.get(item["asset_id"])
        candidate = {
            "release_id": item["release_id"],
            "asset_id": item["asset_id"],
            "name": item["name"],
            "size": item["size"],
            "state": item["state"],
            "digest": item["digest"],
            "api_url": item["api_url"],
            "browser_download_url": item["browser_download_url"],
            "uploader": copy.deepcopy(item["uploader"]),
            "download_sha256": item["expected_sha256"],
            "download_size": item["expected_size"],
        }
        if expected != candidate:
            raise ValueError("prepublish GitHub Release asset metadata changed")
        refreshed.append(candidate)
    return refreshed


def rebase_release_asset_manifest(
    manifest: object, *, allowed_root: Path
) -> dict[str, Any]:
    """Rebind stable asset identities to a fresh current-job transport root."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("assets"), list):
        raise ValueError("release asset manifest is malformed")
    rebased = copy.deepcopy(manifest)
    root = Path(allowed_root)
    for asset in rebased["assets"]:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise ValueError("release asset manifest entry is malformed")
        asset["path"] = str(root / asset["name"])
    return validate_release_asset_manifest(rebased, allowed_root=root)


def verify_release_upload_prefix(
    expected_manifest: object,
    initial_asset_plan: object,
    uploaded_assets: object,
    current_assets: object,
    *,
    next_name: object,
    release_id: int,
    allowed_root: Path,
) -> dict[str, Any]:
    """Fail closed if the live draft changes between planning and each upload."""
    if (
        not isinstance(initial_asset_plan, dict)
        or initial_asset_plan.get("kind") != "ucm-github-release-asset-plan"
        or initial_asset_plan.get("release_id") != release_id
        or initial_asset_plan.get("plan_sha256")
        != sha256_value(
            {
                key: value
                for key, value in initial_asset_plan.items()
                if key != "plan_sha256"
            }
        )
        or not isinstance(initial_asset_plan.get("upload_names"), list)
        or not isinstance(initial_asset_plan.get("reuse_assets"), list)
        or not isinstance(uploaded_assets, list)
        or any(not isinstance(item, dict) for item in uploaded_assets)
        or not isinstance(next_name, str)
    ):
        raise ValueError("GitHub Release upload prefix input is malformed")
    uploaded_plan = plan_release_asset_downloads(
        expected_manifest,
        uploaded_assets,
        release_id=release_id,
        allowed_root=allowed_root,
        require_complete=False,
    )
    uploaded_names = [item["name"] for item in uploaded_plan["downloads"]]
    expected_uploads = initial_asset_plan["upload_names"]
    if (
        uploaded_names != expected_uploads[: len(uploaded_names)]
        or len(uploaded_names) >= len(expected_uploads)
        or next_name != expected_uploads[len(uploaded_names)]
    ):
        raise ValueError("GitHub Release upload order is noncanonical")
    current_plan = plan_release_asset_downloads(
        expected_manifest,
        current_assets,
        release_id=release_id,
        allowed_root=allowed_root,
        require_complete=False,
    )
    current_names = [item["name"] for item in current_plan["downloads"]]
    expected_current_names = [
        *initial_asset_plan["reuse_names"],
        *uploaded_names,
    ]
    if (
        current_names
        != [
            item["name"]
            for item in validate_release_asset_manifest(
                expected_manifest, allowed_root=allowed_root
            )["assets"]
            if item["name"] in set(expected_current_names)
        ]
        or next_name in current_names
    ):
        raise ValueError("live GitHub Release assets changed before upload")

    def transport_projection(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key not in {"expected_sha256", "expected_size"}
        }

    current_by_name = {
        item["name"]: transport_projection(item) for item in current_plan["downloads"]
    }
    prior_by_name = {
        item["name"]: {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key not in {"download_sha256", "download_size"}
        }
        for item in initial_asset_plan["reuse_assets"]
    }
    prior_by_name.update(
        {
            item["name"]: transport_projection(item)
            for item in uploaded_plan["downloads"]
        }
    )
    if current_by_name != prior_by_name:
        raise ValueError("live GitHub Release asset metadata changed before upload")
    payload = {
        "schema_version": 1,
        "kind": "ucm-github-release-upload-prefix",
        "release_id": release_id,
        "next_name": next_name,
        "completed_upload_names": uploaded_names,
        "current_asset_ids": [item["asset_id"] for item in current_plan["downloads"]],
        "current_assets": copy.deepcopy(current_plan["downloads"]),
    }
    return {**payload, "prefix_sha256": sha256_value(payload)}


def record_release_upload_response(
    expected_manifest: object,
    raw_response: object,
    *,
    expected_name: object,
    release_id: int,
    allowed_root: Path,
) -> dict[str, Any]:
    """Canonicalize one successful upload response before the next mutation."""
    if not isinstance(raw_response, dict) or not isinstance(expected_name, str):
        raise ValueError("GitHub Release upload response is malformed")
    plan = plan_release_asset_downloads(
        expected_manifest,
        [raw_response],
        release_id=release_id,
        allowed_root=allowed_root,
        require_complete=False,
    )
    if len(plan["downloads"]) != 1 or plan["downloads"][0]["name"] != expected_name:
        raise ValueError("GitHub Release upload response differs from requested name")
    payload = {
        "schema_version": 1,
        "kind": "ucm-github-release-upload-response",
        "release_id": release_id,
        "name": expected_name,
        "asset": copy.deepcopy(plan["downloads"][0]),
    }
    return {**payload, "response_sha256": sha256_value(payload)}


def validate_release_upload_transcript(
    expected_manifest: object,
    initial_asset_plan: object,
    transcript: object,
    *,
    source_sha: object,
    release_id: int,
    allowed_root: Path,
) -> list[dict[str, Any]]:
    """Replay every draft observation and upload response from persisted evidence."""
    source_sha = _source_sha(source_sha)
    manifest = validate_release_asset_manifest(
        expected_manifest, allowed_root=allowed_root
    )
    if (
        not isinstance(initial_asset_plan, dict)
        or initial_asset_plan.get("kind") != "ucm-github-release-asset-plan"
        or initial_asset_plan.get("release_id") != release_id
        or initial_asset_plan.get("plan_sha256")
        != sha256_value(
            {
                key: value
                for key, value in initial_asset_plan.items()
                if key != "plan_sha256"
            }
        )
        or not isinstance(transcript, list)
    ):
        raise ValueError("GitHub Release upload transcript input is malformed")
    upload_names = initial_asset_plan.get("upload_names")
    if not isinstance(upload_names, list) or len(transcript) != len(upload_names):
        raise ValueError("GitHub Release upload transcript count is invalid")
    expected_by_name = {item["name"]: item for item in manifest["assets"]}

    def as_transport(item: dict[str, Any]) -> dict[str, Any]:
        expected = expected_by_name[item["name"]]
        return {
            "release_id": item["release_id"],
            "asset_id": item["asset_id"],
            "name": item["name"],
            "size": item["size"],
            "state": item["state"],
            "digest": item["digest"],
            "api_url": item["api_url"],
            "browser_download_url": item["browser_download_url"],
            "uploader": copy.deepcopy(item["uploader"]),
            "expected_sha256": expected["sha256"],
            "expected_size": expected["size"],
        }

    current_by_name = {
        item["name"]: as_transport(item)
        for item in initial_asset_plan.get("reuse_assets", [])
    }
    uploaded_ids: set[int] = set()
    validated: list[dict[str, Any]] = []
    for ordinal, (entry, expected_name) in enumerate(
        zip(transcript, upload_names, strict=True)
    ):
        if (
            not isinstance(entry, dict)
            or set(entry) != {"ordinal", "name", "release", "prefix", "response"}
            or entry.get("ordinal") != ordinal
            or isinstance(entry.get("ordinal"), bool)
            or entry.get("name") != expected_name
        ):
            raise ValueError("GitHub Release upload transcript entry is noncanonical")
        release_plan = plan_github_release(entry["release"], source_sha)
        if (
            release_plan["decision"] != "resume-draft"
            or release_plan["release_id"] != release_id
        ):
            raise ValueError("GitHub Release changed before an asset upload")
        prefix = entry["prefix"]
        if (
            not isinstance(prefix, dict)
            or set(prefix)
            != {
                "schema_version",
                "kind",
                "release_id",
                "next_name",
                "completed_upload_names",
                "current_asset_ids",
                "current_assets",
                "prefix_sha256",
            }
            or prefix.get("schema_version") != 1
            or isinstance(prefix.get("schema_version"), bool)
            or prefix.get("kind") != "ucm-github-release-upload-prefix"
            or prefix.get("prefix_sha256")
            != sha256_value(
                {key: value for key, value in prefix.items() if key != "prefix_sha256"}
            )
            or prefix.get("release_id") != release_id
            or prefix.get("next_name") != expected_name
            or prefix.get("completed_upload_names") != upload_names[:ordinal]
        ):
            raise ValueError("GitHub Release upload prefix evidence is invalid")
        ordered_current = [
            current_by_name[item["name"]]
            for item in manifest["assets"]
            if item["name"] in current_by_name
        ]
        if prefix.get("current_assets") != ordered_current or prefix.get(
            "current_asset_ids"
        ) != [item["asset_id"] for item in ordered_current]:
            raise ValueError("GitHub Release upload prefix does not reopen")
        release_assets = entry["release"].get("assets")
        release_asset_ids = (
            [item.get("id") for item in release_assets]
            if isinstance(release_assets, list)
            and all(isinstance(item, dict) for item in release_assets)
            else None
        )
        if (
            not isinstance(release_asset_ids, list)
            or any(
                not isinstance(asset_id, int)
                or isinstance(asset_id, bool)
                or asset_id < 1
                for asset_id in release_asset_ids
            )
            or len(release_asset_ids) != len(set(release_asset_ids))
            or set(release_asset_ids) != set(prefix["current_asset_ids"])
        ):
            raise ValueError("live Release and asset-list observations differ")
        response = entry["response"]
        if (
            not isinstance(response, dict)
            or set(response)
            != {
                "schema_version",
                "kind",
                "release_id",
                "name",
                "asset",
                "response_sha256",
            }
            or response.get("schema_version") != 1
            or isinstance(response.get("schema_version"), bool)
            or response.get("kind") != "ucm-github-release-upload-response"
            or response.get("release_id") != release_id
            or response.get("name") != expected_name
            or response.get("response_sha256")
            != sha256_value(
                {
                    key: value
                    for key, value in response.items()
                    if key != "response_sha256"
                }
            )
            or not isinstance(response.get("asset"), dict)
        ):
            raise ValueError("GitHub Release upload response evidence is invalid")
        asset = response["asset"]
        if (
            asset.get("name") != expected_name
            or asset.get("asset_id") in uploaded_ids
            or asset.get("asset_id")
            in {item["asset_id"] for item in current_by_name.values()}
            or as_transport(
                {
                    **asset,
                    "download_sha256": asset.get("expected_sha256"),
                    "download_size": asset.get("expected_size"),
                }
            )
            != asset
        ):
            raise ValueError("GitHub Release upload response asset is invalid")
        uploaded_ids.add(asset["asset_id"])
        current_by_name[expected_name] = copy.deepcopy(asset)
        validated.append(copy.deepcopy(entry))
    return validated


def build_github_release_operation_ledger(
    *,
    prepare_initial_plan: object,
    initial_release: object,
    initial_asset_plan: object,
    authenticated_assets: object,
    upload_transcript: object,
    source_sha: object,
) -> list[dict[str, Any]]:
    """Build the exact audited REST sequence from validated branch observations."""
    source_sha = _source_sha(source_sha)
    if not isinstance(prepare_initial_plan, dict) or not isinstance(
        initial_asset_plan, dict
    ):
        raise ValueError("GitHub Release operation plans are malformed")
    prepare_decision = prepare_initial_plan.get("decision")
    if prepare_decision not in {
        "create",
        "resume-draft",
        "inspect-published-prerelease",
    }:
        raise ValueError("GitHub Release prepare branch is invalid")
    initial_state = plan_github_release(initial_release, source_sha)
    release_id = initial_state["release_id"]
    if (
        initial_asset_plan.get("kind") != "ucm-github-release-asset-plan"
        or initial_asset_plan.get("release_id") != release_id
        or initial_asset_plan.get("plan_sha256")
        != sha256_value(
            {
                key: value
                for key, value in initial_asset_plan.items()
                if key != "plan_sha256"
            }
        )
        or not isinstance(initial_asset_plan.get("reuse_assets"), list)
        or not isinstance(initial_asset_plan.get("upload_names"), list)
        or not isinstance(authenticated_assets, list)
        or not isinstance(upload_transcript, list)
        or len(upload_transcript) != len(initial_asset_plan.get("upload_names", []))
    ):
        raise ValueError("GitHub Release asset operation plan is invalid")
    api_root = "https://api.github.com/repos/SuperMarioYL/unified-cache-management"
    release_url = f"{api_root}/releases/{release_id}"
    assets_url = release_url + "/assets"
    operations: list[dict[str, Any]] = [
        {
            "type": "github-release-list",
            "capability": "read",
            "reference": api_root + "/releases",
            "authenticated": True,
        }
    ]
    if prepare_decision == "create":
        operations.extend(
            [
                {
                    "type": "github-release-create",
                    "capability": "write",
                    "reference": api_root + "/releases",
                    "authenticated": True,
                },
                {
                    "type": "github-release-read",
                    "capability": "read",
                    "reference": release_url,
                    "authenticated": True,
                },
            ]
        )
    operations.extend(
        [
            {
                "type": "github-release-read",
                "capability": "read",
                "reference": release_url,
                "authenticated": True,
            },
            {
                "type": "github-release-assets-list",
                "capability": "read",
                "reference": assets_url,
                "authenticated": True,
            },
        ]
    )
    operations.extend(
        {
            "type": "github-release-asset-download",
            "capability": "read",
            "reference": item["api_url"],
            "authenticated": True,
        }
        for item in initial_asset_plan["reuse_assets"]
    )
    for ordinal, (name, transcript_entry) in enumerate(
        zip(initial_asset_plan["upload_names"], upload_transcript, strict=True)
    ):
        if (
            not isinstance(name, str)
            or not isinstance(transcript_entry, dict)
            or transcript_entry.get("ordinal") != ordinal
            or transcript_entry.get("name") != name
        ):
            raise ValueError("GitHub Release upload name is invalid")
        operations.extend(
            [
                {
                    "type": "github-release-read",
                    "capability": "read",
                    "reference": release_url,
                    "authenticated": True,
                },
                {
                    "type": "github-release-assets-list",
                    "capability": "read",
                    "reference": assets_url,
                    "authenticated": True,
                },
                {
                    "type": "github-release-asset-upload",
                    "capability": "write",
                    "reference": (
                        "https://uploads.github.com/repos/SuperMarioYL/"
                        f"unified-cache-management/releases/{release_id}/assets?name="
                        + urllib.parse.quote(name, safe="")
                    ),
                    "authenticated": True,
                },
            ]
        )
    operations.append(
        {
            "type": "github-release-assets-list",
            "capability": "read",
            "reference": assets_url,
            "authenticated": True,
        }
    )
    operations.extend(
        {
            "type": "github-release-asset-download",
            "capability": "read",
            "reference": item["api_url"],
            "authenticated": True,
        }
        for item in authenticated_assets
    )
    if initial_state["decision"] == "resume-draft":
        operations.extend(
            [
                {
                    "type": "github-release-read",
                    "capability": "read",
                    "reference": release_url,
                    "authenticated": True,
                },
                {
                    "type": "github-release-assets-list",
                    "capability": "read",
                    "reference": assets_url,
                    "authenticated": True,
                },
                {
                    "type": "github-release-publish",
                    "capability": "write",
                    "reference": release_url,
                    "authenticated": True,
                },
            ]
        )
    operations.extend(
        [
            {
                "type": "github-release-read",
                "capability": "read",
                "reference": release_url,
                "authenticated": True,
            },
            {
                "type": "github-release-tag-read",
                "capability": "read",
                "reference": api_root + "/releases/tags/v0.5.0rc1",
                "authenticated": False,
            },
            {
                "type": "github-release-assets-list",
                "capability": "read",
                "reference": assets_url,
                "authenticated": False,
            },
        ]
    )
    operations.extend(
        {
            "type": "github-release-asset-download",
            "capability": "read",
            "reference": item["api_url"],
            "authenticated": False,
        }
        for item in authenticated_assets
    )
    return operations


_GITHUB_RELEASE_OPERATION_CONTRACTS = MappingProxyType(
    {
        "github-release-list": ("read", True),
        "github-release-create": ("write", True),
        "github-release-assets-list": ("read", None),
        "github-release-asset-download": ("read", None),
        "github-release-asset-upload": ("write", True),
        "github-release-publish": ("write", True),
        "github-release-read": ("read", None),
        "github-release-tag-read": ("read", False),
    }
)


def _validate_github_release_operations(
    operations: object,
    *,
    release_id: int,
    remote_assets: list[dict[str, Any]],
    expected_operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Audit the exact GitHub REST reads/writes used for one prerelease."""
    if not isinstance(operations, list):
        raise ValueError("GitHub Release operation ledger must be an array")
    if operations != expected_operations:
        raise ValueError("GitHub Release operation ledger order or branch is invalid")
    api_root = "https://api.github.com/repos/SuperMarioYL/unified-cache-management"
    uploads_root = (
        "https://uploads.github.com/repos/SuperMarioYL/unified-cache-management/"
        f"releases/{release_id}/assets"
    )
    release_url = f"{api_root}/releases/{release_id}"
    assets_url = release_url + "/assets"
    asset_urls = {item["api_url"] for item in remote_assets}
    asset_names = {item["name"] for item in remote_assets}
    downloads: dict[bool, set[str]] = {True: set(), False: set()}
    uploads: set[str] = set()
    counts: dict[tuple[str, bool], int] = {}
    writes: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {
            "type",
            "capability",
            "reference",
            "authenticated",
        }:
            raise ValueError("GitHub Release operation fields are noncanonical")
        operation_type = operation["type"]
        contract = _GITHUB_RELEASE_OPERATION_CONTRACTS.get(operation_type)
        authenticated = operation["authenticated"]
        if (
            contract is None
            or not isinstance(authenticated, bool)
            or operation["capability"] != contract[0]
            or (contract[1] is not None and authenticated is not contract[1])
            or not isinstance(operation["reference"], str)
        ):
            raise ValueError("GitHub Release operation authority is invalid")
        reference = operation["reference"]
        counts[(operation_type, authenticated)] = (
            counts.get((operation_type, authenticated), 0) + 1
        )
        if operation_type == "github-release-list":
            valid_reference = reference == api_root + "/releases"
        elif operation_type == "github-release-create":
            valid_reference = reference == api_root + "/releases"
        elif operation_type == "github-release-assets-list":
            valid_reference = reference == assets_url
        elif operation_type == "github-release-asset-download":
            valid_reference = reference in asset_urls
            if valid_reference:
                downloads[authenticated].add(reference)
        elif operation_type == "github-release-asset-upload":
            parsed = urllib.parse.urlsplit(reference)
            query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
            valid_reference = (
                urllib.parse.urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, "", "")
                )
                == uploads_root
                and set(query) == {"name"}
                and len(query["name"]) == 1
                and query["name"][0] in asset_names
            )
            if valid_reference:
                name = query["name"][0]
                uploads.add(name)
        elif operation_type in {"github-release-publish", "github-release-read"}:
            valid_reference = reference == release_url
        elif operation_type == "github-release-tag-read":
            valid_reference = reference == api_root + "/releases/tags/v0.5.0rc1"
        else:  # pragma: no cover - immutable mapping owns this branch.
            valid_reference = False
        if not valid_reference:
            raise ValueError("GitHub Release operation reference is invalid")
        if operation["capability"] == "write":
            writes.append(copy.deepcopy(operation))
    if (
        counts.get(("github-release-list", True), 0) < 1
        or counts.get(("github-release-assets-list", True), 0) < 1
        or counts.get(("github-release-read", True), 0) < 1
        or counts.get(("github-release-read", False), 0) != 0
        or counts.get(("github-release-tag-read", False), 0) != 1
        or counts.get(("github-release-assets-list", False), 0) != 1
        or downloads[True] != asset_urls
        or downloads[False] != asset_urls
        or counts.get(("github-release-create", True), 0) > 1
        or counts.get(("github-release-publish", True), 0) > 1
    ):
        raise ValueError("GitHub Release operation ledger is incomplete")
    return {
        "operation_count": len(operations),
        "write_count": len(writes),
        "write_operations": writes,
        "authenticated_download_count": len(downloads[True]),
        "anonymous_download_count": len(downloads[False]),
        "ledger_sha256": sha256_value(operations),
    }


def github_release_publication_evidence(
    *,
    protected_registry: object,
    asset_manifest: object,
    allowed_root: Path,
    prepare_initial_plan: object,
    prepare_release: object,
    initial_release: object,
    initial_assets: object,
    initial_asset_plan: object,
    upload_transcript: object,
    prepublish_release: object,
    prepublish_assets: object,
    authenticated_release: object,
    authenticated_assets: object,
    anonymous_release: object,
    anonymous_assets: object,
    operations: object,
    source_sha: object,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind final anonymous GitHub Release readback to protected Registry state."""
    source_sha = _source_sha(source_sha)
    if not isinstance(run, dict) or set(run) != {"run_id", "run_attempt"}:
        raise ValueError("GitHub Release evidence run identity is invalid")
    run_bound_artifact_name("release-evidence", run["run_id"], run["run_attempt"])
    if (
        not isinstance(protected_registry, dict)
        or set(protected_registry) != {"payload", "payload_sha256", "github"}
        or not isinstance(protected_registry["payload"], dict)
        or protected_registry["payload_sha256"]
        != sha256_value(protected_registry["payload"])
        or protected_registry["github"] != run
    ):
        raise ValueError("protected Registry publication envelope is invalid")
    protected_payload = protected_registry["payload"]
    if (
        protected_payload.get("kind") != "ucm-protected-registry-publication-payload"
        or protected_payload.get("source_sha") != source_sha
        or protected_payload.get("publication")
        != {
            "registry": "published",
            "anonymous": "passed",
            "github_release": "pending",
        }
    ):
        raise ValueError("protected Registry publication is not final")
    rebuilt = protected_registry_publication_evidence(
        member_records=protected_payload.get("member_records"),
        member_collection=protected_payload.get("member_collection"),
        finalized_indexes=protected_payload.get("finalized_indexes"),
        provisional_collection=protected_payload.get("provisional_collection"),
        parent_plans=protected_payload.get("parent_plans"),
        source_sha=source_sha,
        run=run,
    )
    if rebuilt != protected_registry:
        raise ValueError("protected Registry publication does not reopen")
    manifest = validate_release_asset_manifest(
        asset_manifest, allowed_root=allowed_root
    )
    if manifest["source_sha"] != source_sha:
        raise ValueError("GitHub Release assets differ from protected source")
    if not isinstance(prepare_initial_plan, dict):
        raise ValueError("prepare Release plan is malformed")
    prepare_decision = prepare_initial_plan.get("decision")
    if prepare_decision == "create":
        if prepare_initial_plan != plan_github_release(None, source_sha):
            raise ValueError("prepare Release create plan does not reopen")
        prepared_state = plan_github_release(
            prepare_release, source_sha, just_created=True
        )
        branch = "create"
    else:
        expected_prepare = plan_github_release(prepare_release, source_sha)
        if prepare_initial_plan != expected_prepare or prepare_decision not in {
            "resume-draft",
            "inspect-published-prerelease",
        }:
            raise ValueError("prepare Release reopen plan does not reopen")
        prepared_state = expected_prepare
        branch = prepare_decision
    initial_state = plan_github_release(initial_release, source_sha)
    if (
        prepared_state["release_id"] != initial_state["release_id"]
        or (
            branch in {"create", "resume-draft"}
            and initial_state["decision"] != "resume-draft"
        )
        or (
            branch == "inspect-published-prerelease"
            and initial_state["decision"] != "inspect-published-prerelease"
        )
    ):
        raise ValueError("live Release state changed across the protected barrier")
    release_id = initial_state["release_id"]
    expected_initial_asset_plan = plan_release_assets(
        manifest,
        initial_assets,
        release_id=release_id,
        allowed_root=allowed_root,
        release_published=initial_state["decision"] == "inspect-published-prerelease",
    )
    if initial_asset_plan != expected_initial_asset_plan:
        raise ValueError("initial GitHub Release asset plan does not reopen")
    validated_upload_transcript = validate_release_upload_transcript(
        manifest,
        expected_initial_asset_plan,
        upload_transcript,
        source_sha=source_sha,
        release_id=release_id,
        allowed_root=allowed_root,
    )
    if not isinstance(initial_release, dict):
        raise ValueError("initial GitHub Release state is malformed")
    embedded_asset_ids = {item.get("id") for item in initial_release.get("assets", [])}
    observed_initial_ids = {
        item["asset_id"] for item in expected_initial_asset_plan["reuse_assets"]
    }
    if embedded_asset_ids != observed_initial_ids:
        raise ValueError("initial GitHub Release assets differ from live Release state")
    prepublish_plan = plan_github_release(prepublish_release, source_sha)
    if (
        prepublish_plan["release_id"] != release_id
        or (
            initial_state["decision"] == "resume-draft"
            and prepublish_plan["decision"] != "resume-draft"
        )
        or (
            initial_state["decision"] == "inspect-published-prerelease"
            and prepublish_plan["decision"] != "inspect-published-prerelease"
        )
    ):
        raise ValueError("prepublish GitHub Release state changed")
    prepublish_assets_plan = plan_release_assets(
        manifest,
        prepublish_assets,
        release_id=release_id,
        allowed_root=allowed_root,
        release_published=True,
    )
    auth_plan = plan_github_release(authenticated_release, source_sha)
    anon_plan = plan_github_release(anonymous_release, source_sha)
    if (
        auth_plan["decision"] != "inspect-published-prerelease"
        or anon_plan["decision"] != "inspect-published-prerelease"
        or auth_plan["release_id"] != anon_plan["release_id"]
    ):
        raise ValueError("GitHub Release is not one exact published prerelease")
    if auth_plan["release_id"] != release_id:
        raise ValueError("published GitHub Release id changed")
    auth_verification = verify_release_assets(
        manifest,
        authenticated_assets,
        release_id=release_id,
        allowed_root=allowed_root,
    )
    anon_verification = verify_release_assets(
        manifest,
        anonymous_assets,
        release_id=release_id,
        allowed_root=allowed_root,
    )
    auth_assets = plan_release_assets(
        manifest,
        authenticated_assets,
        release_id=release_id,
        allowed_root=allowed_root,
        release_published=True,
    )["reuse_assets"]
    anon_assets = plan_release_assets(
        manifest,
        anonymous_assets,
        release_id=release_id,
        allowed_root=allowed_root,
        release_published=True,
    )["reuse_assets"]
    if auth_assets != anon_assets:
        raise ValueError(
            "anonymous GitHub Release assets differ from authenticated state"
        )
    if prepublish_assets_plan["reuse_assets"] != auth_assets:
        raise ValueError("prepublish GitHub Release assets differ from rehashed state")
    uploaded_by_name = {
        entry["name"]: entry["response"]["asset"]
        for entry in validated_upload_transcript
    }
    final_by_name = {item["name"]: item for item in auth_assets}
    for name, uploaded in uploaded_by_name.items():
        final = final_by_name.get(name)
        expected_uploaded = (
            {
                **{
                    key: copy.deepcopy(value)
                    for key, value in final.items()
                    if key not in {"download_sha256", "download_size"}
                },
                "expected_sha256": final["download_sha256"],
                "expected_size": final["download_size"],
            }
            if final is not None
            else None
        )
        if uploaded != expected_uploaded:
            raise ValueError("uploaded GitHub Release asset changed before publication")

    def embedded_ids(remote: object, label: str) -> set[int]:
        if not isinstance(remote, dict) or not isinstance(remote.get("assets"), list):
            raise ValueError(f"{label} embedded assets are malformed")
        values = [item.get("id") for item in remote["assets"] if isinstance(item, dict)]
        if (
            len(values) != len(remote["assets"])
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in values
            )
            or len(values) != len(set(values))
        ):
            raise ValueError(f"{label} embedded asset ids are malformed")
        return set(values)

    final_asset_ids = {item["asset_id"] for item in auth_assets}
    if (
        embedded_ids(prepublish_release, "prepublish") != final_asset_ids
        or embedded_ids(authenticated_release, "authenticated") != final_asset_ids
        or embedded_ids(anonymous_release, "anonymous") != final_asset_ids
    ):
        raise ValueError("GitHub Release embedded assets differ from exact seven")
    expected_operations = build_github_release_operation_ledger(
        prepare_initial_plan=prepare_initial_plan,
        initial_release=initial_release,
        initial_asset_plan=expected_initial_asset_plan,
        authenticated_assets=auth_assets,
        upload_transcript=validated_upload_transcript,
        source_sha=source_sha,
    )
    operation_audit = _validate_github_release_operations(
        operations,
        release_id=release_id,
        remote_assets=auth_assets,
        expected_operations=expected_operations,
    )
    payload = {
        "schema_version": 1,
        "kind": "ucm-github-release-publication",
        "source_sha": source_sha,
        "tag_name": "v0.5.0rc1",
        "release_id": release_id,
        "protected_registry_payload_sha256": protected_registry["payload_sha256"],
        "assets_sha256": manifest["assets_sha256"],
        "asset_count": 7,
        "release_branch": branch,
        "prepare_initial_plan": copy.deepcopy(prepare_initial_plan),
        "prepare_release": copy.deepcopy(prepare_release),
        "initial_release": copy.deepcopy(initial_release),
        "initial_asset_plan": copy.deepcopy(expected_initial_asset_plan),
        "upload_transcript": copy.deepcopy(validated_upload_transcript),
        "prepublish_release": copy.deepcopy(prepublish_release),
        "prepublish_assets": copy.deepcopy(prepublish_assets_plan["reuse_assets"]),
        "authenticated_release": copy.deepcopy(authenticated_release),
        "authenticated_assets": auth_assets,
        "authenticated_verification": auth_verification,
        "anonymous_release": copy.deepcopy(anonymous_release),
        "anonymous_assets": anon_assets,
        "anonymous_verification": anon_verification,
        "operations": copy.deepcopy(operations),
        "operation_audit": operation_audit,
        "publication": "published-prerelease",
    }
    return _envelope(payload, run)


def extract_index_publication_record(
    envelope: object, parent_plans: object, family_id: object
) -> dict[str, Any]:
    """Reopen Task 4's create envelope before extracting its strict record."""
    from . import registry

    if not isinstance(envelope, dict) or not isinstance(family_id, str):
        raise ValueError("index publication envelope is malformed")
    extras = {
        "verification_sha256",
        "decision",
        "postwrite_manifest_sha256",
        "preflight_sha256",
        "matrix_sha256",
    }
    if set(envelope) != registry.INDEX_RECORD_KEYS | extras:
        raise ValueError("index publication envelope fields are noncanonical")
    for field in (
        "verification_sha256",
        "postwrite_manifest_sha256",
        "preflight_sha256",
        "matrix_sha256",
    ):
        if DIGEST_RE.fullmatch(str(envelope[field])) is None:
            raise ValueError(f"index publication envelope {field} is invalid")
    record = {key: copy.deepcopy(envelope[key]) for key in registry.INDEX_RECORD_KEYS}
    record = registry.validate_index_record(record, parent_plans=parent_plans)
    plans = parent_plans.get("plans") if isinstance(parent_plans, dict) else None
    matches = (
        [item for item in plans if item.get("family_id") == family_id]
        if isinstance(plans, list) and all(isinstance(item, dict) for item in plans)
        else []
    )
    if len(matches) != 1 or record["family_id"] != family_id:
        raise ValueError("index publication family is not parent-bound")
    parent_decision = matches[0].get("decision")
    decision = envelope["decision"]
    if decision not in {"create", "reuse"}:
        raise ValueError("index publication decision is invalid")
    if parent_decision == "reuse" and decision != "reuse":
        raise ValueError("an index reuse plan cannot become a create")
    expected_operations = (
        [
            {
                "type": "registry-index-create",
                "capability": "write",
                "reference": f"{record['target_repository']}:{record['target_tag']}",
            }
        ]
        if decision == "create"
        else []
    )
    if record["operations"] != expected_operations:
        raise ValueError("index publication decision differs from its operation ledger")
    if envelope["postwrite_manifest_sha256"] != record["index_digest"]:
        raise ValueError("index post-write bytes differ from the published digest")
    return record


def validate_index_readbacks(
    readbacks: object,
    index_records: object,
    *,
    parent_plans: object,
    anonymous: bool,
) -> list[dict[str, Any]]:
    """Bind three Registry readbacks to exact final-tag index records."""
    from . import registry

    if not isinstance(anonymous, bool):
        raise ValueError("index readback authentication mode must be boolean")
    if not isinstance(readbacks, list) or not isinstance(index_records, list):
        raise ValueError("index readback closure requires two arrays")
    if len(readbacks) != 3 or len(index_records) != 3:
        raise ValueError("index readback closure requires exactly three families")
    parent = registry.validate_index_plans(parent_plans)
    contracts = registry.canonical_registry_contract()["indexes"]
    records = [
        registry.validate_index_record(item, parent_plans=parent)
        for item in index_records
    ]
    by_family = {
        item.get("family_id"): item for item in records if isinstance(item, dict)
    }
    if set(by_family) != {item["family_id"] for item in contracts}:
        raise ValueError("index records do not cover the canonical three families")
    validated: list[dict[str, Any]] = []
    for authority, readback in zip(contracts, readbacks, strict=True):
        record = by_family[authority["family_id"]]
        plans = [
            item
            for item in parent["plans"]
            if item["family_id"] == authority["family_id"]
        ]
        if len(plans) != 1:
            raise ValueError("index readback family does not resolve in parent plans")
        validated.append(
            registry._validate_prepared_index_readback(
                readback,
                plan=plans[0],
                expected_digest=record["index_digest"],
                authenticated=not anonymous,
            )
        )
    return validated


def _validated_publication_members(
    *,
    member_records: object,
    parent_plans: object,
    source_sha: object,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    from . import registry

    source_sha = _source_sha(source_sha)
    if not isinstance(member_records, list):
        raise ValueError("protected publication member records must be an array")
    parent = registry.validate_index_plans(parent_plans)
    members = [registry.validate_member_record(item) for item in member_records]
    contract = registry.canonical_registry_contract()
    member_order = [item["spec_id"] for item in contract["members"]]
    by_spec = {item["spec_id"]: item for item in members}
    if set(by_spec) != set(member_order) or len(members) != 6:
        raise ValueError("protected publication requires exact six members")
    members = [by_spec[spec_id] for spec_id in member_order]
    if any(item["source_sha"] != source_sha for item in members):
        raise ValueError("protected member source SHA differs from the tag")
    if members != parent["member_records"] or parent["source_sha"] != source_sha:
        raise ValueError("protected members differ from their exact parent plans")
    family_order = [item["family_id"] for item in contract["indexes"]]
    return parent, members, family_order


def _publication_operation_audit(
    operation_batches: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    audits = [
        audit_operations(batch, lane="protected-tag") for batch in operation_batches
    ]
    return {
        "batch_count": len(audits),
        "operation_count": sum(item["operation_count"] for item in audits),
        "write_count": sum(item["write_count"] for item in audits),
        "ledger_sha256": sha256_value(operation_batches),
        "batches": audits,
    }


def _validate_member_artifact_collection(
    collection: object,
    members: list[dict[str, Any]],
    *,
    source_sha: str,
) -> dict[str, Any]:
    record_sha256s = {item["spec_id"]: item["record_sha256"] for item in members}
    normalized_keys = {
        "schema_version",
        "kind",
        "source_sha",
        "member_record_sha256s",
        "member_preflight_sha256s",
        "collection_sha256",
    }
    if isinstance(collection, dict) and set(collection) == normalized_keys:
        preflight_sha256s = collection.get("member_preflight_sha256s")
        if (
            collection.get("schema_version") != 1
            or isinstance(collection.get("schema_version"), bool)
            or collection.get("kind") != "ucm-member-artifact-collection"
            or collection.get("source_sha") != source_sha
            or collection.get("member_record_sha256s") != record_sha256s
            or not isinstance(preflight_sha256s, dict)
            or set(preflight_sha256s) != set(record_sha256s)
            or any(
                DIGEST_RE.fullmatch(str(value)) is None
                for value in preflight_sha256s.values()
            )
            or collection.get("collection_sha256")
            != sha256_value(
                {
                    key: value
                    for key, value in collection.items()
                    if key != "collection_sha256"
                }
            )
        ):
            raise ValueError("normalized member artifact collection is invalid")
        return copy.deepcopy(collection)
    if (
        not isinstance(collection, dict)
        or set(collection)
        != {
            "schema_version",
            "kind",
            "source_sha",
            "member_records",
            "member_record_sha256s",
            "member_preflight_sha256s",
            "collection_sha256",
        }
        or collection.get("schema_version") != 1
        or isinstance(collection.get("schema_version"), bool)
        or collection.get("kind") != "ucm-member-artifact-collection"
        or collection.get("source_sha") != source_sha
        or not isinstance(collection.get("member_records"), list)
        or not all(
            isinstance(path, str) and path for path in collection["member_records"]
        )
        or collection.get("collection_sha256")
        != sha256_value(
            {
                key: value
                for key, value in collection.items()
                if key not in {"member_records", "collection_sha256"}
            }
        )
    ):
        raise ValueError("member artifact collection evidence is invalid")
    preflight_sha256s = collection.get("member_preflight_sha256s")
    if (
        collection.get("member_record_sha256s") != record_sha256s
        or not isinstance(preflight_sha256s, dict)
        or set(preflight_sha256s) != set(record_sha256s)
        or any(
            DIGEST_RE.fullmatch(str(value)) is None
            for value in preflight_sha256s.values()
        )
        or len(collection["member_records"]) != len(members)
        or [Path(path).name for path in collection["member_records"]]
        != [f"{item['spec_id']}.json" for item in members]
    ):
        raise ValueError("member artifact collection differs from six records")
    return {
        "schema_version": 1,
        "kind": collection["kind"],
        "source_sha": source_sha,
        "member_record_sha256s": copy.deepcopy(record_sha256s),
        "member_preflight_sha256s": copy.deepcopy(preflight_sha256s),
        "collection_sha256": collection["collection_sha256"],
    }


def _validate_provisional_artifact_collection(
    collection: object,
    provisionals: list[dict[str, Any]],
    *,
    parent: dict[str, Any],
    source_sha: str,
) -> dict[str, Any]:
    provisional_sha256s = {
        item["family_id"]: item["provisional_sha256"] for item in provisionals
    }
    preflight_sha256s = {
        item["family_id"]: item["preflight_sha256"] for item in provisionals
    }
    normalized_keys = {
        "schema_version",
        "kind",
        "source_sha",
        "parent_plans_sha256",
        "provisional_sha256s",
        "provisional_preflight_sha256s",
        "collection_sha256",
    }
    if isinstance(collection, dict) and set(collection) == normalized_keys:
        if (
            collection.get("schema_version") != 1
            or isinstance(collection.get("schema_version"), bool)
            or collection.get("kind") != "ucm-provisional-artifact-collection"
            or collection.get("source_sha") != source_sha
            or collection.get("parent_plans_sha256") != parent["plans_sha256"]
            or collection.get("provisional_sha256s") != provisional_sha256s
            or collection.get("provisional_preflight_sha256s") != preflight_sha256s
            or collection.get("collection_sha256")
            != sha256_value(
                {
                    key: value
                    for key, value in collection.items()
                    if key != "collection_sha256"
                }
            )
        ):
            raise ValueError("normalized provisional artifact collection is invalid")
        return copy.deepcopy(collection)
    if (
        not isinstance(collection, dict)
        or set(collection)
        != {
            "schema_version",
            "kind",
            "source_sha",
            "parent_plans_sha256",
            "provisional_indexes",
            "provisional_sha256s",
            "provisional_preflight_sha256s",
            "collection_sha256",
        }
        or collection.get("schema_version") != 1
        or isinstance(collection.get("schema_version"), bool)
        or collection.get("kind") != "ucm-provisional-artifact-collection"
        or collection.get("source_sha") != source_sha
        or collection.get("parent_plans_sha256") != parent["plans_sha256"]
        or not isinstance(collection.get("provisional_indexes"), list)
        or not all(
            isinstance(path, str) and path for path in collection["provisional_indexes"]
        )
        or collection.get("collection_sha256")
        != sha256_value(
            {
                key: value
                for key, value in collection.items()
                if key not in {"provisional_indexes", "collection_sha256"}
            }
        )
    ):
        raise ValueError("provisional artifact collection evidence is invalid")
    if (
        collection.get("provisional_sha256s") != provisional_sha256s
        or collection.get("provisional_preflight_sha256s") != preflight_sha256s
        or len(collection["provisional_indexes"]) != len(provisionals)
        or [Path(path).name for path in collection["provisional_indexes"]]
        != [f"{item['family_id']}.json" for item in provisionals]
    ):
        raise ValueError("provisional artifact collection differs from three indexes")
    return {
        "schema_version": 1,
        "kind": collection["kind"],
        "source_sha": source_sha,
        "parent_plans_sha256": parent["plans_sha256"],
        "provisional_sha256s": copy.deepcopy(provisional_sha256s),
        "provisional_preflight_sha256s": copy.deepcopy(preflight_sha256s),
        "collection_sha256": collection["collection_sha256"],
    }


def authenticated_registry_publication_evidence(
    *,
    member_records: object,
    member_collection: object,
    provisional_indexes: object,
    provisional_collection: object,
    parent_plans: object,
    source_sha: object,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate 6/3 authenticated state without claiming anonymous publication."""
    from . import registry

    parent, members, family_order = _validated_publication_members(
        member_records=member_records,
        parent_plans=parent_plans,
        source_sha=source_sha,
    )
    if not isinstance(provisional_indexes, list):
        raise ValueError("authenticated publication provisionals must be an array")
    provisionals = [
        registry.validate_provisional_index(item, parent_plans=parent)
        for item in provisional_indexes
    ]
    by_family = {item["family_id"]: item for item in provisionals}
    if len(provisionals) != 3 or set(by_family) != set(family_order):
        raise ValueError("authenticated publication requires exact three provisionals")
    provisionals = [by_family[family_id] for family_id in family_order]
    member_collection_evidence = _validate_member_artifact_collection(
        member_collection, members, source_sha=parent["source_sha"]
    )
    provisional_collection_evidence = _validate_provisional_artifact_collection(
        provisional_collection,
        provisionals,
        parent=parent,
        source_sha=parent["source_sha"],
    )
    batches: list[list[dict[str, Any]]] = [
        *[copy.deepcopy(item["operations"]) for item in members],
        *[copy.deepcopy(item["operations"]) for item in provisionals],
        *[
            copy.deepcopy(item["authenticated_readback"]["operations"])
            for item in provisionals
        ],
        *[
            [copy.deepcopy(item["authenticated_closure"]["operation"])]
            for item in provisionals
        ],
    ]
    payload = {
        "schema_version": 1,
        "kind": "ucm-authenticated-registry-publication-payload",
        "source_sha": parent["source_sha"],
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "wheel_sha256s": [item["wheel_sha256"] for item in members],
        "member_records": copy.deepcopy(members),
        "member_collection": member_collection_evidence,
        "parent_plans": copy.deepcopy(parent),
        "provisional_indexes": copy.deepcopy(provisionals),
        "provisional_collection": provisional_collection_evidence,
        "parent_plans_sha256": parent["plans_sha256"],
        "operation_audit": _publication_operation_audit(batches),
        "publication": {
            "registry": "authenticated-passed",
            "anonymous": "pending",
            "github_release": "pending",
        },
    }
    return _envelope(payload, run)


def protected_registry_publication_evidence(
    *,
    member_records: object,
    member_collection: object,
    finalized_indexes: object,
    provisional_collection: object,
    parent_plans: object,
    source_sha: object,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build final deterministic evidence only after exact anonymous closure."""
    from . import registry
    from .core import DEFAULT_SCHEMA_DIR, load_json, validate_schema

    parent, members, family_order = _validated_publication_members(
        member_records=member_records,
        parent_plans=parent_plans,
        source_sha=source_sha,
    )
    if not isinstance(finalized_indexes, list):
        raise ValueError("protected finalizations must be an array")
    finalizations = [
        registry.validate_finalized_index(item, parent_plans=parent)
        for item in finalized_indexes
    ]
    final_by_family = {item["family_id"]: item for item in finalizations}
    if len(finalizations) != 3 or set(final_by_family) != set(family_order):
        raise ValueError("protected publication requires exact three finalizations")
    finalizations = [final_by_family[family_id] for family_id in family_order]
    provisionals = [item["provisional"] for item in finalizations]
    member_collection_evidence = _validate_member_artifact_collection(
        member_collection, members, source_sha=source_sha
    )
    provisional_collection_evidence = _validate_provisional_artifact_collection(
        provisional_collection,
        provisionals,
        parent=parent,
        source_sha=source_sha,
    )
    indexes = [item["record"] for item in finalizations]
    by_family = {item["family_id"]: item for item in indexes}
    indexes = [by_family[family_id] for family_id in family_order]
    manifest = build_release_manifest()
    registry_payload = {
        "status": "published",
        "candidate_task_sha256": sha256_value(
            [item["candidate_task_sha256"] for item in members]
        ),
        "publication_task_sha256": sha256_value(
            [item["publication_task_sha256"] for item in members]
        ),
        "member_records": members,
        "index_records": indexes,
    }
    manifest["publication"]["registry"] = registry_payload
    schema = load_json(DEFAULT_SCHEMA_DIR / "release-manifest.schema.json")
    validate_schema(manifest, schema)
    operation_batches: list[list[dict[str, Any]]] = [
        *[copy.deepcopy(item["operations"]) for item in members],
        *[copy.deepcopy(item["record"]["operations"]) for item in finalizations],
        *[
            copy.deepcopy(item["authenticated_readback"]["operations"])
            for item in finalizations
        ],
        *[
            [copy.deepcopy(item["provisional"]["authenticated_closure"]["operation"])]
            for item in finalizations
        ],
        *[
            copy.deepcopy(item["anonymous_readback"]["operations"])
            for item in finalizations
        ],
        *[
            [copy.deepcopy(item["anonymous_closure"]["operation"])]
            for item in finalizations
        ],
    ]
    payload = {
        "schema_version": 1,
        "kind": "ucm-protected-registry-publication-payload",
        "source_sha": source_sha,
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "wheel_sha256s": [item["wheel_sha256"] for item in members],
        "member_records": copy.deepcopy(members),
        "member_collection": member_collection_evidence,
        "members": [
            {
                "spec_id": item["spec_id"],
                "build_key_sha256": item["build_key_sha256"],
                "member_digest": item["member_digest"],
                "record_sha256": item["record_sha256"],
            }
            for item in members
        ],
        "indexes": [
            {
                "family_id": item["family_id"],
                "index_build_key_sha256": item["index_build_key_sha256"],
                "index_digest": item["index_digest"],
                "record_sha256": item["record_sha256"],
            }
            for item in indexes
        ],
        "index_records": copy.deepcopy(indexes),
        "parent_plans": copy.deepcopy(parent),
        "finalized_indexes": copy.deepcopy(finalizations),
        "provisional_collection": provisional_collection_evidence,
        "parent_plans_sha256": parent["plans_sha256"],
        "release_manifest_sha256": sha256_value(manifest),
        "operation_audit": _publication_operation_audit(operation_batches),
        "publication": {
            "registry": "published",
            "anonymous": "passed",
            "github_release": "pending",
        },
    }
    return _envelope(payload, run)


def _envelope(payload: dict[str, Any], run: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "payload": payload,
        "payload_sha256": sha256_value(payload),
        "github": copy.deepcopy(run or {}),
    }


def _file_sha256(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"release artifact is not a regular file: {path}")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_canonical_json(path: Path, label: str) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"{label} is not a regular file: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    if raw != canonical_bytes(value) + b"\n":
        raise ValueError(f"{label} must use canonical JSON bytes")
    return value


def hosted_build_matrix(
    source_sha: str,
    source_date_epoch: int,
) -> dict[str, Any]:
    """Project the reviewed matrix into the only hosted wheel/image task records."""
    source_sha = _source_sha(source_sha)
    if (
        not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
        or not 315532800 <= source_date_epoch <= 4354819199
    ):
        raise ValueError("hosted source date epoch is outside the ZIP timestamp range")
    release, _ = validate_config()
    reviewed = build_matrix("feature-candidate")
    if len(reviewed["tasks"]) != 6:
        raise ValueError("hosted build matrix requires exactly six reviewed tasks")
    platform_values = {
        "cuda130": ("cuda", "wheel-cuda"),
        "cann900-a2": ("ascend", "wheel-ascend"),
        "cann900-a3": ("ascend-a3", "wheel-ascend"),
    }
    package_arg_names = {
        "build": "BUILD",
        "pyproject-hooks": "PYPROJECT_HOOKS",
        "packaging": "PACKAGING",
        "setuptools": "SETUPTOOLS",
        "wheel": "WHEEL",
    }
    tasks: list[dict[str, Any]] = []
    for reviewed_task in reviewed["tasks"]:
        profile_id = reviewed_task["profile_id"]
        if profile_id not in platform_values:
            raise ValueError(f"hosted task has unsupported profile: {profile_id}")
        platform_arg, docker_target = platform_values[profile_id]
        root = reviewed_task["builder"]["root"]
        build_args = {
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
            "UCM_BUILDER_IMAGE": f"{root['repository']}@{root['manifest_digest']}",
            "PLATFORM": platform_arg,
            "UCM_RELEASE_PROFILE": profile_id,
            "UCM_RELEASE_SOURCE_SHA": source_sha,
            "UCM_RELEASE_VERSION": reviewed_task["wheel_version"],
            "UCM_RELEASE_BUILD_KEY": reviewed_task["task_sha256"],
            "UCM_RELEASE_REQUIRED_TARGETS": ",".join(reviewed_task["required_native"]),
            "UCM_RELEASE_FORBIDDEN_TARGETS": ",".join(
                reviewed_task["forbidden_native"]
            ),
        }
        for package_name, argument_prefix in package_arg_names.items():
            package = release["python_build_lock"]["packages"][package_name]
            build_args[f"{argument_prefix}_VERSION"] = package["version"]
            build_args[f"{argument_prefix}_FILENAME"] = package["filename"]
            build_args[f"{argument_prefix}_SHA256"] = package["sha256"]
        cmake = release["python_build_lock"]["cmake"]
        cmake_artifact = cmake["artifacts"][reviewed_task["cpu_arch"]]
        pyyaml = release["python_build_lock"]["pyyaml"]
        pyyaml_artifact = pyyaml["artifacts"][reviewed_task["cpu_arch"]]
        build_args.update(
            {
                "PYYAML_VERSION": pyyaml["version"],
                "PYYAML_FILENAME": pyyaml_artifact["filename"],
                "PYYAML_SHA256": pyyaml_artifact["sha256"],
                "CMAKE_VERSION": cmake["version"],
                "CMAKE_FILENAME": cmake_artifact["filename"],
                "CMAKE_SHA256": cmake_artifact["sha256"],
            }
        )
        spec_id = reviewed_task["spec_id"]
        task = {
            "spec_id": spec_id,
            "profile_id": profile_id,
            "cpu_arch": reviewed_task["cpu_arch"],
            "platform": reviewed_task["platform"],
            "runner": reviewed_task["runner"],
            "task_sha256": reviewed_task["task_sha256"],
            "builder_coordinate": build_args["UCM_BUILDER_IMAGE"],
            "docker_target": docker_target,
            "source_sha": source_sha,
            "source_date_epoch": source_date_epoch,
            "wheel_artifact": f"ucm-wheel-{spec_id}-{source_sha}",
            "image_artifact": f"ucm-image-{spec_id}-{source_sha}",
            "build_args": dict(sorted(build_args.items())),
        }
        task["hosted_task_sha256"] = sha256_value(task)
        tasks.append(task)
    payload = {
        "schema_version": 1,
        "kind": "ucm-real-hosted-build-matrix",
        "source_sha": source_sha,
        "source_date_epoch": source_date_epoch,
        "reviewed_matrix_sha256": reviewed["matrix_sha256"],
        "github_matrix": {"include": [{"spec_id": item["spec_id"]} for item in tasks]},
        "tasks": tasks,
    }
    return {**payload, "hosted_matrix_sha256": sha256_value(payload)}


def build_real_family_plans(
    image_results: list[dict[str, Any]],
    *,
    source_sha: str,
) -> dict[str, Any]:
    """Create three deterministic unpublished dual-architecture index plans."""
    source_sha = _source_sha(source_sha)
    if not isinstance(image_results, list) or len(image_results) != 6:
        raise ValueError("real candidate planning requires exactly six image results")
    reviewed = build_matrix("feature-candidate")
    expected = {task["spec_id"]: task for task in reviewed["tasks"]}
    observed: dict[str, dict[str, Any]] = {}
    for result in image_results:
        if not isinstance(result, dict):
            raise ValueError("real image result must be an object")
        spec_id = result.get("spec_id")
        if spec_id not in expected or spec_id in observed:
            raise ValueError("real image results contain an unknown or duplicate task")
        task = expected[spec_id]
        source = result.get("source")
        oci = result.get("oci")
        required_identity = (
            result.get("candidate_kind") == "real-candidate"
            and result.get("fixture_only") is False
            and result.get("unpublished") is True
            and result.get("publication_attempted") is False
            and result.get("status") == "real-verified-unpublished"
            and result.get("family_id") == task["profile_id"]
            and result.get("profile_id") == task["profile_id"]
            and result.get("target_platform") == task["platform"]
            and result.get("target_repository") == task["target_repository"]
            and result.get("target_tag") == task["target_tag"]
            and result.get("task_key") == task["task_sha256"]
            and isinstance(source, dict)
            and source.get("commit") == source_sha
            and isinstance(oci, dict)
            and oci.get("platform") == task["platform"]
            and oci.get("published") is False
        )
        if not required_identity:
            raise ValueError(f"real image result differs from reviewed task: {spec_id}")
        for field in (
            "build_key_sha256",
            "result_sha256",
            "content_identity_sha256",
        ):
            if DIGEST_RE.fullmatch(str(result.get(field))) is None:
                raise ValueError(f"real image result {field} is invalid")
        if DIGEST_RE.fullmatch(str(oci.get("digest"))) is None:
            raise ValueError("real image OCI digest is invalid")
        observed[spec_id] = result
    if set(observed) != set(expected):
        raise ValueError("real image results do not match exactly six reviewed tasks")

    families: list[dict[str, Any]] = []
    for family_id in sorted({task["profile_id"] for task in expected.values()}):
        family_tasks = sorted(
            (task for task in expected.values() if task["profile_id"] == family_id),
            key=lambda item: item["platform"],
        )
        if [task["platform"] for task in family_tasks] != [
            "linux/amd64",
            "linux/arm64",
        ]:
            raise ValueError(f"real image family is not dual architecture: {family_id}")
        members = [
            {
                "platform": task["platform"],
                "spec_id": task["spec_id"],
                "task_sha256": task["task_sha256"],
                "manifest_digest": observed[task["spec_id"]]["oci"]["digest"],
                "build_key_sha256": observed[task["spec_id"]]["build_key_sha256"],
                "content_identity_sha256": observed[task["spec_id"]][
                    "content_identity_sha256"
                ],
                "image_result_sha256": observed[task["spec_id"]]["result_sha256"],
            }
            for task in family_tasks
        ]
        family_payload = {
            "schema_version": 1,
            "kind": "ucm-real-candidate-index-plan",
            "family_id": family_id,
            "target_repository": family_tasks[0]["target_repository"],
            "target_tag": family_tasks[0]["target_tag"],
            "members": members,
            "unpublished": True,
            "publication_attempted": False,
        }
        families.append({**family_payload, "plan_sha256": sha256_value(family_payload)})
    inventory_payload = {
        "schema_version": 1,
        "kind": "ucm-real-candidate-inventory",
        "families": copy.deepcopy(families),
    }
    return {
        "families": families,
        "candidate_inventory": {
            **inventory_payload,
            "inventory_sha256": sha256_value(inventory_payload),
        },
        "second_reconcile": {
            "decision": "already-present",
            "task_count": 0,
            "tasks": [],
        },
    }


def _artifact_directory(root: Path, artifact_name: str, label: str) -> Path:
    root = Path(root)
    direct = root / artifact_name
    matches = [direct] if direct.is_dir() else []
    if root.is_dir() and root.name == artifact_name:
        matches.append(root)
    unique = sorted(set(matches))
    if len(unique) != 1 or any(path.is_symlink() for path in unique):
        raise ValueError(f"{label} artifact directory is missing or ambiguous")
    return unique[0]


def _one_file(directory: Path, pattern: str, label: str) -> Path:
    matches = sorted(
        path
        for path in Path(directory).glob(pattern)
        if path.is_file() and not path.is_symlink()
    )
    if len(matches) != 1:
        raise ValueError(f"{label} requires exactly one file matching {pattern}")
    return matches[0]


def _real_chart_summary(result_path: Path, package_path: Path) -> dict[str, Any]:
    result = _load_canonical_json(result_path, "real hosted Chart result")
    with tempfile.TemporaryDirectory() as temporary:
        expected_dir = Path(temporary) / "chart"
        expected = chart.package_chart(expected_dir)
        expected_package = expected_dir / expected["filename"]
        if result != expected:
            raise ValueError("real hosted Chart result differs from fresh packaging")
        if (
            not Path(package_path).is_file()
            or Path(package_path).is_symlink()
            or Path(package_path).name != expected["filename"]
            or Path(package_path).read_bytes() != expected_package.read_bytes()
        ):
            raise ValueError("real hosted Chart package differs from fresh packaging")
    return {
        "filename": result["filename"],
        "sha256": result["sha256"],
        "release_tree_sha256": result["release_tree_sha256"],
        "rendered_cases": copy.deepcopy(result["rendered_cases"]),
        "status": result["status"],
    }


def aggregate_real_hosted_evidence(
    *,
    wheel_dir: Path,
    image_dir: Path,
    source_sha: str,
    repository: str,
    ref: str,
    chart_result_path: Path | None = None,
    chart_package_path: Path | None = None,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reopen six real wheels/images and derive deterministic feature evidence."""
    source_sha = _source_sha(source_sha)
    if (chart_result_path is None) != (chart_package_path is None):
        raise ValueError(
            "real hosted Chart result and package must be supplied together"
        )
    wheel_root = Path(wheel_dir)
    image_root = Path(image_dir)
    if not wheel_root.is_dir() or not image_root.is_dir():
        raise ValueError("real hosted wheel and image artifact roots must exist")

    reviewed_specs = [
        item["spec_id"] for item in build_matrix("feature-candidate")["tasks"]
    ]
    wheel_logical_names = [
        f"ucm-wheel-{spec_id}-{source_sha}" for spec_id in reviewed_specs
    ]
    wheel_artifacts = resolve_run_bound_artifact_directories(
        wheel_root, wheel_logical_names, run=run, label="real hosted wheel"
    )
    task_records: dict[str, dict[str, Any]] = {}
    for logical_name in wheel_logical_names:
        task_path = wheel_artifacts[logical_name] / "hosted-task.json"
        task = _load_canonical_json(task_path, "real hosted task record")
        spec_id = task.get("spec_id")
        if not isinstance(spec_id, str) or spec_id in task_records:
            raise ValueError("real hosted task records are duplicated or malformed")
        task_records[spec_id] = task
    if len(task_records) != 6:
        raise ValueError(
            "real hosted aggregate requires exactly six wheel task records"
        )
    epochs = {task.get("source_date_epoch") for task in task_records.values()}
    if len(epochs) != 1:
        raise ValueError("real hosted tasks disagree on source date epoch")
    source_date_epoch = next(iter(epochs))
    expected_matrix = hosted_build_matrix(source_sha, source_date_epoch)
    expected_tasks = {item["spec_id"]: item for item in expected_matrix["tasks"]}
    if task_records != expected_tasks:
        raise ValueError("real hosted task records differ from reviewed matrix")

    image_artifacts = resolve_run_bound_artifact_directories(
        image_root,
        [item["image_artifact"] for item in expected_matrix["tasks"]],
        run=run,
        label="real hosted image",
    )
    wheel_summaries: list[dict[str, Any]] = []
    image_results: list[dict[str, Any]] = []
    image_summaries: list[dict[str, Any]] = []
    for spec_id in [item["spec_id"] for item in expected_matrix["tasks"]]:
        task = expected_tasks[spec_id]
        wheel_artifact = wheel_artifacts[task["wheel_artifact"]]
        task_path = wheel_artifact / "hosted-task.json"
        if _load_canonical_json(task_path, f"{spec_id} hosted wheel task") != task:
            raise ValueError(f"{spec_id} wheel artifact task record differs")
        wheel_path = _one_file(wheel_artifact, "*.whl", f"{spec_id} wheel")
        inspection_path = wheel_artifact / "wheel-inspection.json"
        seal_path = wheel_artifact / "wheel-seal.json"
        source_context_path = wheel_artifact / "source-context.json"
        inspection = _load_canonical_json(
            inspection_path, f"{spec_id} wheel inspection"
        )
        seal = _load_canonical_json(seal_path, f"{spec_id} wheel seal")
        source_context = _load_canonical_json(
            source_context_path, f"{spec_id} source context"
        )
        wheel_sha256 = _file_sha256(wheel_path)
        reopened = wheel.inspect_wheel(
            wheel_path, spec_id, wheel_sha256, "builder-candidate"
        )
        builder = reopened.get("builder_evidence")
        if (
            inspection != reopened
            or seal.get("source_kind") != "builder-candidate"
            or seal.get("publication_status") != "unpublished"
            or seal.get("publication_eligible") is not False
            or seal.get("spec_id") != spec_id
            or seal.get("source_sha") != source_sha
            or seal.get("build_key") != task["task_sha256"]
            or seal.get("wheel_sha256") != wheel_sha256
            or seal.get("inspection_sha256") != _file_sha256(inspection_path)
            or not isinstance(builder, dict)
            or builder.get("source_commit") != source_sha
            or builder.get("build_key") != task["task_sha256"]
            or builder.get("source_date_epoch") != source_date_epoch
            or source_context.get("source_sha") != source_sha
            or source_context.get("build_context_sha256")
            != builder.get("build_context_digest")
        ):
            raise ValueError(f"{spec_id} real wheel closure does not reopen")
        wheel_summaries.append(
            {
                "spec_id": spec_id,
                "task_sha256": task["task_sha256"],
                "hosted_task_sha256": task["hosted_task_sha256"],
                "artifact": task["wheel_artifact"],
                "filename": wheel_path.name,
                "wheel_sha256": wheel_sha256,
                "wheel_size": wheel_path.stat().st_size,
                "inspection_sha256": _file_sha256(inspection_path),
                "source_tree": source_context.get("source_tree"),
                "source_context_sha256": source_context.get("build_context_sha256"),
            }
        )

        image_artifact = image_artifacts[task["image_artifact"]]
        if (
            _load_canonical_json(
                image_artifact / "hosted-task.json", f"{spec_id} hosted image task"
            )
            != task
        ):
            raise ValueError(f"{spec_id} image artifact task record differs")
        result_path = image_artifact / "image-result.json"
        recipe_path = image_artifact / "image-recipe.json"
        result = image.validate_image_result(
            _load_canonical_json(result_path, f"{spec_id} image result")
        )
        recipe = _load_canonical_json(recipe_path, f"{spec_id} image recipe")
        compact = image.validate_real_compact_oci_evidence(
            image_artifact / "oci-evidence",
            image_result=result,
            recipe=recipe,
        )
        if (
            result.get("spec_id") != spec_id
            or result.get("task_key") != task["task_sha256"]
            or result.get("source", {}).get("commit") != source_sha
            or result.get("wheel", {}).get("sha256") != wheel_sha256
            or compact.get("manifest_digest") != result.get("oci", {}).get("digest")
        ):
            raise ValueError(f"{spec_id} image result differs from wheel/task closure")
        image_results.append(result)
        image_summaries.append(
            {
                "spec_id": spec_id,
                "task_sha256": task["task_sha256"],
                "artifact": task["image_artifact"],
                "manifest_digest": result["oci"]["digest"],
                "build_key_sha256": result["build_key_sha256"],
                "content_identity_sha256": result["content_identity_sha256"],
                "image_result_sha256": result["result_sha256"],
                "image_result_file_sha256": _file_sha256(result_path),
                "recipe_sha256": result["recipe_sha256"],
                "compact_closure_sha256": compact["closure_sha256"],
            }
        )

    planned = build_real_family_plans(image_results, source_sha=source_sha)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "ucm-real-hosted-image-loop-payload",
        "mode": "feature-candidate",
        "repository": repository,
        "ref": ref,
        "source_sha": source_sha,
        "source_date_epoch": source_date_epoch,
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "reviewed_matrix_sha256": expected_matrix["reviewed_matrix_sha256"],
        "hosted_matrix_sha256": expected_matrix["hosted_matrix_sha256"],
        "wheels": wheel_summaries,
        "images": image_summaries,
        "families": planned["families"],
        "candidate_inventory": planned["candidate_inventory"],
        "second_reconcile": planned["second_reconcile"],
        "publication": {"status": "blocked", "attempted": False},
    }
    if chart_result_path is not None and chart_package_path is not None:
        payload["kind"] = "ucm-real-hosted-release-loop-payload"
        payload["chart"] = _real_chart_summary(chart_result_path, chart_package_path)
    return _envelope(payload, run)


def prepare_candidate_loop(
    build_record: dict[str, Any],
    wheel_record: dict[str, Any],
    *,
    source_sha: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the actual fixture candidate, first task, and six-scenario proof."""
    source_sha = _source_sha(source_sha)
    required_build = {
        "schema_version",
        "kind",
        "fixture_only",
        "publication_status",
        "publication_eligible",
        "source_sha",
        "profile_id",
        "wheel_sha256",
        "inspection_sha256",
    }
    if not isinstance(build_record, dict) or set(build_record) != required_build:
        raise ValueError("fixture wheel build record fields are noncanonical")
    if (
        build_record["schema_version"] != 1
        or build_record["kind"] != "ucm-fixture-wheel-build"
        or build_record["fixture_only"] is not True
        or build_record["publication_status"] != "unpublished"
        or build_record["publication_eligible"] is not False
        or build_record["source_sha"] != source_sha
    ):
        raise ValueError("fixture wheel build record does not bind the source")
    inspection_sha256 = (
        "sha256:" + hashlib.sha256(canonical_bytes(wheel_record) + b"\n").hexdigest()
    )
    if (
        build_record["wheel_sha256"] != wheel_record.get("sha256")
        or build_record["inspection_sha256"] != inspection_sha256
        or build_record["profile_id"] != wheel_record.get("spec_id")
        or wheel_record.get("fixture_binding")
        != {
            "source_commit": source_sha,
            "profile_id": build_record["profile_id"],
            "marker_status": "passed",
        }
        or wheel_record.get("status") != "fixture-only"
        or wheel_record.get("trust_level") != "fixture-only"
        or wheel_record.get("published") is not False
        or wheel_record.get("publication_eligible") is not False
    ):
        raise ValueError("fixture wheel inspection does not match its build record")

    manifest = build_release_manifest()
    _, compatibility = validate_config()
    snapshot = {
        "schema_version": 1,
        "kind": "upstream-registry-snapshot",
        "repository": "docker.io/vllm/vllm-openai",
        "upstream_tag": "v0.10.2",
        "index_digest": "sha256:" + "1" * 64,
        "platforms": [
            {
                "os": "linux",
                "architecture": "amd64",
                "manifest_digest": "sha256:" + "2" * 64,
                "config_digest": "sha256:" + "3" * 64,
            },
            {
                "os": "linux",
                "architecture": "arm64",
                "manifest_digest": "sha256:" + "4" * 64,
                "config_digest": "sha256:" + "5" * 64,
            },
        ],
    }
    source_case = {
        "release_manifest": manifest,
        "wheel_records": [copy.deepcopy(wheel_record)],
        "spec_id": wheel_record["spec_id"],
        "upstream_snapshot": snapshot,
        "compatibility": compatibility,
        "compatibility_rule_id": "cuda-supported",
        "implementation_digest": image.implementation_digests()["aggregate_sha256"],
    }
    candidate = build_candidate(**source_case, fixture_mode=True)
    inventory = _inventory()
    first = reconcile(candidate, inventory)
    if first["task_count"] != 1 or first["tasks"][0]["revision"] != 1:
        raise ValueError("new fixture input must schedule exactly one r1 task")
    loop = verify_loop(source_case, run=run)
    scenarios = loop["payload"]["scenarios"]
    if (
        [item["name"] for item in scenarios] != REQUIRED_SCENARIOS
        or not all(item["passed"] is True for item in scenarios)
        or loop["payload"]["publication_attempted"] is not False
    ):
        raise ValueError("fixture loop did not pass all deterministic scenarios")
    return {
        "source_sha": source_sha,
        "source_case": source_case,
        "candidate": candidate,
        "inventory": inventory,
        "first_reconcile": first,
        "image_input": {
            "source_case": source_case,
            "candidate": candidate,
            "task": first["tasks"][0],
            "inventory": inventory,
            "target_platform": "linux/amd64",
        },
        "loop_verification": loop,
    }


def complete_candidate_loop(
    prepared: dict[str, Any],
    image_result: dict[str, Any],
    *,
    source_sha: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize a verified local result and require the second reconcile to be zero."""
    source_sha = _source_sha(source_sha)
    required_prepared = {
        "source_sha",
        "source_case",
        "candidate",
        "inventory",
        "first_reconcile",
        "image_input",
        "loop_verification",
    }
    if not isinstance(prepared, dict) or set(prepared) != required_prepared:
        raise ValueError("prepared loop fields are noncanonical")
    if prepared["source_sha"] != source_sha:
        raise ValueError("prepared loop source SHA mismatch")
    candidate = prepared["candidate"]
    first = prepared["first_reconcile"]
    image_input = prepared["image_input"]
    if (
        image_input.get("candidate") != candidate
        or image_input.get("inventory") != prepared["inventory"]
        or first.get("tasks") != [image_input.get("task")]
    ):
        raise ValueError("prepared image input is not the exact first reconcile task")
    if not isinstance(image_result, dict):
        raise ValueError("image result must be an object")
    if (
        image_result.get("fixture_only") is not True
        or image_result.get("unpublished") is not True
        or image_result.get("publication_attempted") is not False
        or image_result.get("status") != "fixture-verified-unpublished"
    ):
        raise ValueError("image result must remain fixture-only and unpublished")
    image_result = image.validate_image_result(image_result)
    if (
        image_result.get("fixture_only") is not True
        or image_result.get("unpublished") is not True
        or image_result.get("publication_attempted") is not False
        or image_result.get("status") != "fixture-verified-unpublished"
    ):
        raise ValueError("image result must remain fixture-only and unpublished")
    build_inputs = candidate["build_inputs"]
    wheel_input = build_inputs["wheel"]
    wheel_records = prepared["source_case"].get("wheel_records")
    if not isinstance(wheel_records, list) or len(wheel_records) != 1:
        raise ValueError("prepared loop must retain one exact wheel inspection")
    wheel_record = wheel_records[0]
    expected_wheel = {
        "filename": wheel_record["filename"],
        "sha256": wheel_record["sha256"],
        "size": wheel_record["size"],
        "spec_id": wheel_input["spec_id"],
        "declaration_sha256": wheel_input["declaration_sha256"],
        "version": wheel_input["version"],
        "python_abi": wheel_input["python_abi"],
        "cpu_arch": wheel_input["cpu_arch"],
        "accelerator": wheel_input["accelerator"],
        "accelerator_runtime": wheel_input["accelerator_runtime"],
        "npu_arch_or_na": wheel_input["npu_arch_or_na"],
        "os": wheel_input["os"],
        "binary_profile_id": wheel_input["binary_profile_id"],
        "requires_dist": ["wrapt==1.17.2"],
    }
    target_platform = image_input["target_platform"]
    target_architecture = target_platform.split("/", 1)[1]
    upstream = build_inputs["upstream"]
    upstream_platforms = [
        item
        for item in upstream["platforms"]
        if item["architecture"] == target_architecture
    ]
    if len(upstream_platforms) != 1:
        raise ValueError("candidate does not have one exact target platform")
    upstream_platform = upstream_platforms[0]
    manifest = prepared["source_case"]["release_manifest"]
    expected_source = {
        "release_manifest_sha256": build_inputs["release_manifest_sha256"],
        "config_sha256": manifest["config_sha256"],
        "compatibility_sha256": manifest["compatibility_sha256"],
        "compatibility_rule_id": build_inputs["compatibility_rule_id"],
        "compatibility_rule_sha256": build_inputs["compatibility_rule_sha256"],
        "upstream_repository": upstream["repository"],
        "upstream_index_digest": upstream["index_digest"],
        "upstream_platform_manifest_digest": upstream_platform["manifest_digest"],
        "upstream_platform_config_digest": upstream_platform["config_digest"],
    }
    if (
        image_result["build_key_sha256"] != candidate["build_key_sha256"]
        or image_result["task_key"] != sha256_value(image_input["task"])
        or image_result["ucm_version"] != candidate["ucm_version"]
        or image_result["target_platform"] != target_platform
        or image_result["wheel"] != expected_wheel
        or image_result["source"] != expected_source
        or image_result["implementation"]["aggregate_sha256"]
        != build_inputs["implementation_digest"]
    ):
        raise ValueError("image result does not bind the exact candidate input closure")
    gates = image_result.get("gates")
    if (
        not isinstance(gates, dict)
        or set(gates) != REQUIRED_IMAGE_GATES
        or any(value != "passed" for value in gates.values())
    ):
        raise ValueError("image result required gates did not all pass")
    if (
        image_result.get("runtime_validation") != "external-required"
        or image_result.get("device_validation") != "external-required"
    ):
        raise ValueError(
            "fixture runtime and device validation must remain external-required"
        )
    oci_digest = image_result.get("oci", {}).get("digest")
    if not isinstance(oci_digest, str) or DIGEST_RE.fullmatch(oci_digest) is None:
        raise ValueError("image result OCI digest is invalid")

    task = image_input["task"]
    entry = {
        "repository": candidate["target_repository"],
        "tag": task["tag"],
        "build_key_sha256": candidate["build_key_sha256"],
        "observed_digest": oci_digest,
        "evidence_digest": oci_digest,
    }
    inventory = _inventory([entry])
    second = reconcile(candidate, inventory)
    if second["task_count"] != 0 or second["decision"] != "already-present":
        raise ValueError("completed fixture candidate did not reconcile to zero")

    accepted = {
        "a2": parse_upstream_tag("vllm-ascend", "v0.10.2")["npu_arch"],
        "a3": parse_upstream_tag("vllm-ascend", "v0.10.2-a3")["npu_arch"],
    }
    rejected: list[str] = []
    for suffix in ("310p", "a5"):
        try:
            parse_upstream_tag("vllm-ascend", f"v0.10.2-{suffix}")
        except ValueError:
            rejected.append(suffix)
    if accepted != {"a2": "a2", "a3": "a3"} or rejected != ["310p", "a5"]:
        raise ValueError("Ascend compatibility boundary is not A2/A3 only")
    loop = prepared["loop_verification"]
    if not isinstance(loop, dict) or set(loop) != {
        "schema_version",
        "kind",
        "run",
        "payload",
        "payload_sha256",
    }:
        raise ValueError("prepared loop verification envelope is noncanonical")
    recomputed_loop = verify_loop(prepared["source_case"], run=loop["run"])
    if loop != recomputed_loop:
        raise ValueError("prepared loop verification does not match recomputation")
    scenarios = loop.get("payload", {}).get("scenarios", [])
    if (
        [item.get("name") for item in scenarios] != REQUIRED_SCENARIOS
        or not all(item.get("passed") is True for item in scenarios)
        or loop.get("payload", {}).get("must_green") is not True
    ):
        raise ValueError("prepared deterministic scenario evidence is incomplete")
    source_batches = loop["payload"].get("operation_batches")
    if not isinstance(source_batches, list):
        raise ValueError("prepared loop is missing its operation ledger")
    if audit_operation_batches(source_batches) != loop["payload"].get(
        "zero_write_audit"
    ):
        raise ValueError("prepared zero-write audit does not match its ledger")
    operation_batches = copy.deepcopy(source_batches) + [
        copy.deepcopy(second["operations"])
    ]
    operation_audit = audit_operation_batches(operation_batches)
    if operation_audit["write_count"] != 0:
        raise ValueError("completed candidate attempted a write")
    write_audit = {
        **operation_audit,
        "ledger_sha256": sha256_value(operation_batches),
    }
    publication_attempted = (
        image_result["publication_attempted"] or write_audit["write_count"] != 0
    )
    if publication_attempted or image_result["unpublished"] is not True:
        raise ValueError("completed fixture candidate must remain unpublished")
    payload = {
        "schema_version": 1,
        "kind": "ucm-vllm-candidate-loop-payload",
        "source_sha": source_sha,
        "candidate_identity": {
            "repository": candidate["target_repository"],
            "tag": task["tag"],
            "build_key_sha256": candidate["build_key_sha256"],
        },
        "upstream_index_digest": candidate["build_inputs"]["upstream"]["index_digest"],
        "first_reconcile_sha256": sha256_value(first),
        "second_reconcile_sha256": sha256_value(second),
        "first_task_count": first["task_count"],
        "second_task_count": second["task_count"],
        "image_result_sha256": image_result["result_sha256"],
        "oci_digest": oci_digest,
        "loop_payload_sha256": loop["payload_sha256"],
        "scenarios": copy.deepcopy(scenarios),
        "compatibility": {"accepted": ["a2", "a3"], "rejected": ["310p", "a5"]},
        "required_gates": copy.deepcopy(gates),
        "runtime_validation": image_result["runtime_validation"],
        "device_validation": image_result["device_validation"],
        "expected_blocked": copy.deepcopy(loop["payload"]["expected_blockers"]),
        "publication": {
            "status": "blocked" if image_result["unpublished"] else "invalid",
            "attempted": publication_attempted,
        },
        "operation_batches": operation_batches,
        "write_audit": write_audit,
    }
    return {"second_reconcile": second, "evidence": _envelope(payload, run)}


def aggregate_release_evidence(
    *,
    build_record_path: Path,
    wheel_record_path: Path,
    wheel_path: Path,
    chart_result_path: Path,
    chart_package_path: Path,
    image_result_path: Path,
    oci_evidence_dir: Path,
    image_recipe_path: Path,
    image_metadata_path: Path,
    image_prepare_path: Path,
    buildkit_metadata_path: Path,
    image_archive_sha256_path: Path,
    completed_loop_path: Path,
    second_reconcile_path: Path,
    image_loop_path: Path,
    repository: str,
    ref: str,
    source_sha: str,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reopen every release artifact and recompute the exact candidate closure."""
    source_sha = _source_sha(source_sha)
    build_record_path = Path(build_record_path)
    wheel_record_path = Path(wheel_record_path)
    wheel_path = Path(wheel_path)
    chart_result_path = Path(chart_result_path)
    chart_package_path = Path(chart_package_path)
    image_result_path = Path(image_result_path)
    oci_evidence_dir = Path(oci_evidence_dir)
    image_recipe_path = Path(image_recipe_path)
    image_metadata_path = Path(image_metadata_path)
    image_prepare_path = Path(image_prepare_path)
    buildkit_metadata_path = Path(buildkit_metadata_path)
    image_archive_sha256_path = Path(image_archive_sha256_path)
    completed_loop_path = Path(completed_loop_path)
    second_reconcile_path = Path(second_reconcile_path)
    image_loop_path = Path(image_loop_path)

    build_record = _load_canonical_json(build_record_path, "wheel build record")
    wheel_record = _load_canonical_json(wheel_record_path, "wheel inspection")
    actual_wheel_sha256 = _file_sha256(wheel_path)
    with tempfile.TemporaryDirectory() as temporary:
        expected_fixture = wheel.build_fixture_wheel(
            Path(temporary) / "wheel",
            source_sha,
            build_record.get("profile_id"),
        )
        expected_wheel_path = Path(expected_fixture["wheel_path"])
        if (
            wheel_path.read_bytes() != expected_wheel_path.read_bytes()
            or wheel_record != expected_fixture["inspection"]
            or build_record != expected_fixture["build_record"]
        ):
            raise ValueError(
                "actual fixture wheel/build/inspection differs from authoritative rebuild"
            )
    if wheel_path.name != wheel_record.get("filename"):
        raise ValueError("wheel filename does not match its inspection")
    inspected = wheel.inspect_wheel(
        wheel_path,
        build_record.get("profile_id"),
        actual_wheel_sha256,
        "fixture",
    )
    if inspected != wheel_record:
        raise ValueError("actual wheel does not match its canonical inspection")
    if build_record.get("inspection_sha256") != _file_sha256(wheel_record_path):
        raise ValueError("wheel build record does not bind inspection bytes")
    prepared = prepare_candidate_loop(
        build_record,
        wheel_record,
        source_sha=source_sha,
        run={},
    )

    chart_result = _load_canonical_json(chart_result_path, "Chart result")
    with tempfile.TemporaryDirectory() as temporary:
        expected_chart_dir = Path(temporary) / "chart"
        expected_chart_result = chart.package_chart(expected_chart_dir)
        expected_chart_package = expected_chart_dir / expected_chart_result["filename"]
        if chart_result != expected_chart_result:
            raise ValueError("Chart result does not match fresh validation")
        if chart_package_path.name != expected_chart_result["filename"]:
            raise ValueError("Chart package filename is noncanonical")
        if not chart_package_path.is_file():
            raise ValueError("Chart package is not a regular file")
        if chart_package_path.read_bytes() != expected_chart_package.read_bytes():
            raise ValueError("Chart package bytes do not match fresh validation")

    image_result = image.validate_image_result(
        _load_canonical_json(image_result_path, "image result")
    )
    if (
        image.require_fixture_base_authority(
            image_result["base"], image_result["target_platform"]
        )
        != image_result["base"]
    ):
        raise ValueError("image result base is not the authoritative fixture base")
    if not buildkit_metadata_path.is_file():
        raise ValueError("BuildKit metadata is not a regular file")
    try:
        buildkit_metadata = json.loads(buildkit_metadata_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"BuildKit metadata is invalid JSON: {error}") from error
    compact_oci = image.validate_compact_oci_evidence(
        oci_evidence_dir,
        image_result=image_result,
        image_recipe_path=image_recipe_path,
        image_metadata_path=image_metadata_path,
        image_prepare_path=image_prepare_path,
        wheel_path=wheel_path,
        buildkit_metadata=buildkit_metadata,
    )
    if compact_oci["wheel_sha256"] != actual_wheel_sha256:
        raise ValueError("compact OCI evidence does not bind the actual wheel")
    if not image_archive_sha256_path.is_file():
        raise ValueError("OCI archive digest record is not a regular file")
    archive_record = image_archive_sha256_path.read_text(encoding="utf-8").strip()
    archive_parts = archive_record.split()
    if (
        len(archive_parts) != 2
        or "sha256:" + archive_parts[0] != compact_oci["archive_sha256"]
        or Path(archive_parts[1]).name != "image.oci.tar"
    ):
        raise ValueError("OCI archive digest record does not match compact evidence")
    completed = _load_canonical_json(completed_loop_path, "completed loop")
    second_reconcile = _load_canonical_json(second_reconcile_path, "second reconcile")
    image_loop = _load_canonical_json(image_loop_path, "image loop evidence")
    if set(completed) != {"second_reconcile", "evidence"}:
        raise ValueError("completed loop fields are noncanonical")
    completed_evidence = completed.get("evidence")
    if not isinstance(completed_evidence, dict) or set(completed_evidence) != {
        "payload",
        "payload_sha256",
        "github",
    }:
        raise ValueError("completed loop evidence fields are noncanonical")
    recomputed = complete_candidate_loop(
        prepared,
        image_result,
        source_sha=source_sha,
        run=completed_evidence["github"],
    )
    if completed != recomputed:
        raise ValueError("completed loop does not match full recomputation")
    if second_reconcile != recomputed["second_reconcile"]:
        raise ValueError("standalone second reconcile disagrees with completed loop")
    if image_loop != recomputed["evidence"]:
        raise ValueError("image loop envelope disagrees with completed loop")

    image_payload = recomputed["evidence"]["payload"]
    scenarios = image_payload["scenarios"]
    operation_batches = image_payload["operation_batches"]
    derived_operation_audit = audit_operation_batches(operation_batches)
    derived_write_audit = {
        **derived_operation_audit,
        "ledger_sha256": sha256_value(operation_batches),
    }
    if image_payload["write_audit"] != derived_write_audit:
        raise ValueError("completed write audit does not match its operation ledger")
    must_green = {
        "fixture_wheel": (
            build_record["wheel_sha256"] == actual_wheel_sha256
            and inspected["status"] == "fixture-only"
            and inspected["published"] is False
        ),
        "helm_cuda_a2_a3": (
            chart_result["rendered_cases"] == ["cuda", "a2", "a3"]
            and set(chart_result["checks"].values()) == {"passed"}
            and chart_result["status"] == "candidate-verified"
        ),
        "install_only_image": (
            set(image_result["gates"]) == REQUIRED_IMAGE_GATES
            and set(image_result["gates"].values()) == {"passed"}
            and image_result["status"] == "fixture-verified-unpublished"
        ),
        "second_reconcile_zero": (
            second_reconcile["task_count"] == 0
            and second_reconcile["decision"] == "already-present"
        ),
    }
    if (
        not all(must_green.values())
        or [item.get("name") for item in scenarios] != REQUIRED_SCENARIOS
        or not all(item.get("passed") is True for item in scenarios)
        or image_payload["publication"] != {"status": "blocked", "attempted": False}
        or derived_write_audit["write_count"] != 0
    ):
        raise ValueError("aggregate candidate closure did not pass every required gate")
    payload = {
        "mode": "fork-dry-run",
        "repository": repository,
        "ref": ref,
        "source_sha": source_sha,
        "workflow_refs": copy.deepcopy(WORKFLOW_REFS),
        "must_green": must_green,
        "scenarios": copy.deepcopy(scenarios),
        "compatibility": copy.deepcopy(image_payload["compatibility"]),
        "candidate_identity": copy.deepcopy(image_payload["candidate_identity"]),
        "artifact_digests": {
            "wheel_sha256": wheel_record["sha256"],
            "wheel_inspection_sha256": build_record["inspection_sha256"],
            "chart_sha256": chart_result["sha256"],
            "chart_tree_sha256": chart_result["release_tree_sha256"],
            "upstream_index_digest": image_payload["upstream_index_digest"],
            "oci_digest": image_payload["oci_digest"],
            "image_result_sha256": image_payload["image_result_sha256"],
            "first_reconcile_sha256": image_payload["first_reconcile_sha256"],
            "second_reconcile_sha256": image_payload["second_reconcile_sha256"],
            "image_loop_payload_sha256": image_loop["payload_sha256"],
            "oci_evidence_closure_sha256": compact_oci["closure_sha256"],
            "oci_manifest_digest": compact_oci["oci_digest"],
            "oci_config_digest": compact_oci["config_digest"],
            "oci_archive_sha256": compact_oci["archive_sha256"],
            "build_record_file_sha256": _file_sha256(build_record_path),
            "image_result_file_sha256": _file_sha256(image_result_path),
            "second_reconcile_file_sha256": _file_sha256(second_reconcile_path),
        },
        "required_gates": copy.deepcopy(image_payload["required_gates"]),
        "expected_blocked": [
            "production-wheel-builders",
            "accelerator-runtime",
            "cuda-device",
            "ascend-a2-device",
            "ascend-a3-device",
            "protected-environment",
            "registry-publication-and-readback",
        ],
        "publication": copy.deepcopy(image_payload["publication"]),
        "operation_batches": copy.deepcopy(operation_batches),
        "write_audit": copy.deepcopy(derived_write_audit),
    }
    evidence = _envelope(payload, run)
    evidence["github"]["non_deterministic_artifact_file_sha256"] = {
        "completed_loop": _file_sha256(completed_loop_path),
        "image_loop": _file_sha256(image_loop_path),
        "buildkit_metadata": _file_sha256(buildkit_metadata_path),
    }
    return evidence


def _inventory(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    inventory = {
        "schema_version": 1,
        "kind": "registry-inventory",
        "repositories": [
            "ghcr.io/modelengine-group/vllm-ascend",
            "ghcr.io/modelengine-group/vllm-openai",
        ],
        "entries": entries or [],
    }
    inventory["inventory_sha256"] = inventory_digest(inventory)
    return inventory


def _entry(
    candidate: dict[str, Any],
    digest: str,
    *,
    revision: int = 1,
    observed_digest: str | None = None,
) -> dict[str, Any]:
    return {
        "repository": candidate["target_repository"],
        "tag": f"{candidate['tag_base']}-r{revision}",
        "build_key_sha256": candidate["build_key_sha256"],
        "observed_digest": observed_digest or digest,
        "evidence_digest": digest,
    }


def expect_blocker(code: str, operation: Callable[[], object]) -> str:
    """Accept only the exact typed blocker expected by a verification scenario."""
    try:
        operation()
    except RegistryBlocker as error:
        if error.code != code:
            raise ValueError(f"expected blocker {code}, got {error.code}") from error
        return error.code
    raise ValueError(f"expected blocker {code} was not raised")


def _validate_operation_reference(reference_kind: str, reference: object) -> None:
    if not isinstance(reference, str):
        raise ValueError("operation has malformed reference")
    if reference_kind == "digest":
        valid = DIGEST_RE.fullmatch(reference) is not None
    elif reference_kind == "upstream-digest":
        repository, separator, digest = reference.rpartition("@")
        valid = (
            separator == "@"
            and REPOSITORY_RE.fullmatch(repository) is not None
            and repository.rsplit("/", 1)[-1] in TARGET_REPOSITORIES
            and DIGEST_RE.fullmatch(digest) is not None
        )
    elif reference_kind == "upstream-tag":
        repository, separator, tag = reference.rpartition(":")
        valid = separator == ":" and REPOSITORY_RE.fullmatch(repository) is not None
        if valid:
            try:
                parse_upstream_tag(repository.rsplit("/", 1)[-1], tag)
            except ValueError:
                valid = False
    elif reference_kind == "target-tag":
        matching = [
            repository
            for repository in TARGET_REPOSITORIES.values()
            if reference.startswith(repository + ":")
        ]
        valid = len(matching) == 1
        if valid:
            try:
                validate_public_tag(reference.removeprefix(matching[0] + ":"))
            except ValueError:
                valid = False
    elif reference_kind == "staging-digest":
        repository, separator, digest = reference.rpartition("@")
        valid = (
            separator == "@"
            and repository == STAGING_REPOSITORY
            and DIGEST_RE.fullmatch(digest) is not None
        )
    elif reference_kind == "staging-tag":
        prefix = STAGING_REPOSITORY + ":staging-"
        valid = (
            reference.startswith(prefix)
            and re.fullmatch(r"[0-9a-f]{64}", reference.removeprefix(prefix))
            is not None
        )
    elif reference_kind == "public-target":
        valid = reference in {
            f"{item['target_repository']}:{item['target_tag']}"
            for item in canonical_registry_contract()["indexes"]
        }
    elif reference_kind in {"registry-read-tag", "registry-read-tag-or-digest"}:
        public_tags = {
            f"{item['target_repository']}:{item['target_tag']}"
            for item in canonical_registry_contract()["indexes"]
        }
        staging_prefix = STAGING_REPOSITORY + ":staging-"
        valid = reference in public_tags or (
            reference.startswith(staging_prefix)
            and re.fullmatch(r"[0-9a-f]{64}", reference.removeprefix(staging_prefix))
            is not None
        )
        if not valid and reference_kind == "registry-read-tag-or-digest":
            repository, separator, digest = reference.rpartition("@")
            valid = (
                separator == "@"
                and repository
                in {
                    STAGING_REPOSITORY,
                    *{
                        item["target_repository"]
                        for item in canonical_registry_contract()["indexes"]
                    },
                }
                and DIGEST_RE.fullmatch(digest) is not None
            )
    elif reference_kind == "registry-read-digest":
        repository, separator, digest = reference.rpartition("@")
        valid = (
            separator == "@"
            and repository
            in {
                STAGING_REPOSITORY,
                *{
                    item["target_repository"]
                    for item in canonical_registry_contract()["indexes"]
                },
            }
            and DIGEST_RE.fullmatch(digest) is not None
        )
    else:  # pragma: no cover - immutable mapping owns this branch.
        raise ValueError(f"unknown operation reference contract: {reference_kind}")
    if not valid:
        if reference_kind in {"staging-digest", "staging-tag", "public-target"}:
            raise ValueError(
                f"operation reference is outside the exact allowlist: {reference}"
            )
        raise ValueError(
            f"operation has malformed reference for {reference_kind}: {reference}"
        )


def audit_operations(
    operations: list[dict[str, Any]], *, lane: str | None = None
) -> dict[str, Any]:
    """Derive zero-write evidence from emitted operation ledgers."""
    if not isinstance(operations, list):
        raise ValueError("operation ledger must be an array")
    if lane not in {None, "feature-candidate", "protected-tag"}:
        raise ValueError(f"unknown operation audit lane: {lane}")
    operation_types: set[str] = set()
    identities: set[tuple[str, str]] = set()
    write_capable_operations: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {
            "type",
            "capability",
            "reference",
        }:
            raise ValueError(
                "malformed ledger entry: expected exactly type/capability/reference"
            )
        operation_type = operation["type"]
        if operation_type in KNOWN_WRITE_OPERATION_TYPES and lane in {
            None,
            "feature-candidate",
        }:
            if lane == "feature-candidate":
                raise ValueError(
                    f"feature-candidate rejects write-capable operation: {operation_type}"
                )
            raise ValueError(
                f"write-capable operation type is forbidden: {operation_type}"
            )
        if operation_type not in OPERATION_CONTRACTS:
            raise ValueError(f"unknown operation type: {operation_type}")
        expected_capability, reference_kind = OPERATION_CONTRACTS[operation_type]
        if operation["capability"] != expected_capability:
            raise ValueError(
                f"operation capability mismatch for {operation_type}: "
                f"expected {expected_capability}, got {operation['capability']}"
            )
        _validate_operation_reference(reference_kind, operation["reference"])
        if operation_type in KNOWN_WRITE_OPERATION_TYPES:
            write_capable_operations.append(copy.deepcopy(operation))
        identity = (operation_type, operation["reference"])
        if identity in identities:
            raise ValueError(f"duplicate operation identity: {identity}")
        identities.add(identity)
        operation_types.add(operation_type)
    return {
        "operation_count": len(operations),
        "operation_types": sorted(operation_types),
        "write_capable_operations": write_capable_operations,
        "write_count": len(write_capable_operations),
    }


def audit_operation_batches(
    operation_batches: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    """Audit each producer ledger independently, then aggregate proven summaries."""
    if not isinstance(operation_batches, list):
        raise ValueError("operation ledger batches must be an array")
    audits = [audit_operations(batch) for batch in operation_batches]
    return {
        "operation_count": sum(audit["operation_count"] for audit in audits),
        "operation_types": sorted(
            {
                operation_type
                for audit in audits
                for operation_type in audit["operation_types"]
            }
        ),
        "write_capable_operations": [],
        "write_count": 0,
    }


def _required_blockers(case: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    snapshot = copy.deepcopy(case["upstream_snapshot"])
    snapshot["platforms"] = [
        item for item in snapshot["platforms"] if item["architecture"] != "arm64"
    ]
    stable = _entry(candidate, case["upstream_snapshot"]["index_digest"])
    conflicting = copy.deepcopy(stable)
    conflicting["observed_digest"] = "sha256:" + "f" * 64
    production_case = copy.deepcopy(case)
    results = [
        expect_blocker(
            "duplicate-conflicting-inventory",
            lambda: reconcile(candidate, _inventory([stable, conflicting])),
        ),
        expect_blocker("missing-linux-arm64", lambda: validate_snapshot(snapshot)),
        expect_blocker(
            "production-wheel-unpublished",
            lambda: build_candidate(**production_case, fixture_mode=False),
        ),
    ]
    return sorted(results)


def _artifact_digests(
    candidate: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    return {
        "release_manifest_sha256": candidate["build_inputs"]["release_manifest_sha256"],
        "wheel": copy.deepcopy(candidate["build_inputs"]["wheel"]),
        "upstream": {
            "index_digest": snapshot["index_digest"],
            "platforms": copy.deepcopy(snapshot["platforms"]),
        },
        "implementation_digest": candidate["build_inputs"]["implementation_digest"],
        "compatibility_rule_sha256": candidate["build_inputs"][
            "compatibility_rule_sha256"
        ],
        "build_key_sha256": candidate["build_key_sha256"],
        "tag_family_sha256": candidate["tag_family_sha256"],
    }


def verify_loop(
    case: dict[str, Any], *, run: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Exercise six fixture scenarios and hash only their deterministic payload."""
    if not isinstance(case, dict):
        raise ValueError("loop verification input must be an object")
    required = {
        "release_manifest",
        "wheel_records",
        "spec_id",
        "upstream_snapshot",
        "compatibility",
        "compatibility_rule_id",
        "implementation_digest",
    }
    if set(case) != required:
        raise ValueError(
            "loop verification fields mismatch: "
            f"missing={sorted(required - set(case))}, extra={sorted(set(case) - required)}"
        )
    requested_snapshot = validate_snapshot(case["upstream_snapshot"])
    scan_result = scan_registry(
        requested_snapshot["repository"],
        requested_snapshot["upstream_tag"],
        fixture=requested_snapshot,
    )
    fixture_case = {**case, "upstream_snapshot": scan_result["snapshot"]}
    candidate = build_candidate(**fixture_case, fixture_mode=True)
    snapshot = scan_result["snapshot"]
    digest = snapshot["index_digest"]

    new_result = reconcile(candidate, _inventory())
    stable_inventory = _inventory([_entry(candidate, digest)])
    same_result = reconcile(candidate, stable_inventory)
    drift_inventory = _inventory(
        [
            _entry(
                candidate,
                digest,
                observed_digest="sha256:" + "f" * 64,
            )
        ]
    )
    drift_result = reconcile(candidate, drift_inventory)
    blockers = _required_blockers(fixture_case, candidate)

    first_fixture_result = reconcile(candidate, _inventory())
    completed_entry = _entry(candidate, digest)
    final_fixture_result = reconcile(candidate, _inventory([completed_entry]))
    operation_batches = [
        scan_result["operations"],
        *[
            result["operations"]
            for result in (
                new_result,
                same_result,
                drift_result,
                first_fixture_result,
                final_fixture_result,
            )
        ],
    ]
    zero_write_audit = audit_operation_batches(operation_batches)
    digest_chain = _artifact_digests(candidate, snapshot)
    platforms = digest_chain["upstream"]["platforms"]
    complete_chain = (
        len(platforms) == 2
        and {item["architecture"] for item in platforms} == {"amd64", "arm64"}
        and all(item["manifest_digest"] and item["config_digest"] for item in platforms)
    )

    scenarios = [
        {
            "name": "new-input-one-task",
            "passed": new_result["task_count"] == 1
            and new_result["tasks"][0]["revision"] == 1,
            "task_count": new_result["task_count"],
            "task_tags": [item["tag"] for item in new_result["tasks"]],
        },
        {
            "name": "identical-input-zero-tasks",
            "passed": same_result["task_count"] == 0,
            "task_count": same_result["task_count"],
            "task_tags": [],
        },
        {
            "name": "tag-digest-drift-r2",
            "passed": drift_result["task_count"] == 1
            and drift_result["tasks"][0]["revision"] == 2
            and drift_result["inventory"] == drift_inventory,
            "task_count": drift_result["task_count"],
            "task_tags": [item["tag"] for item in drift_result["tasks"]],
        },
        {
            "name": "complete-digest-chain",
            "passed": complete_chain,
            "platform_count": len(platforms),
        },
        {
            "name": "required-failures-block",
            "passed": blockers == EXPECTED_BLOCKERS,
            "blockers": blockers,
        },
        {
            "name": "fixture-candidate-full-zero-reconcile",
            "passed": first_fixture_result["task_count"] == 1
            and final_fixture_result["task_count"] == 0,
            "initial_task_count": first_fixture_result["task_count"],
            "final_task_count": final_fixture_result["task_count"],
        },
    ]
    payload = {
        "schema_version": 1,
        "kind": "ucm-release-loop-verification-payload",
        "must_green": all(item["passed"] for item in scenarios),
        "scenarios": scenarios,
        "artifact_digests": digest_chain,
        "compatibility_rule_id": case["compatibility_rule_id"],
        "expected_blockers": {
            "scenario_codes": copy.deepcopy(EXPECTED_BLOCKERS),
            "production": copy.deepcopy(case["release_manifest"]["blockers"]),
        },
        "fixture_only": True,
        "unpublished": True,
        "publication_attempted": zero_write_audit["write_count"] != 0,
        "operation_batches": copy.deepcopy(operation_batches),
        "zero_write_audit": zero_write_audit,
    }
    return {
        "schema_version": 1,
        "kind": "ucm-release-loop-verification-envelope",
        "run": copy.deepcopy(run or {}),
        "payload": payload,
        "payload_sha256": sha256_value(payload),
    }
