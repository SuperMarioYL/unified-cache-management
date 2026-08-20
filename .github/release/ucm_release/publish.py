# fmt: off
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import core, registry

Run = Callable[..., subprocess.CompletedProcess[Any]]
HttpGet = Callable[[str], dict[str, Any]]
GithubApi = Callable[..., dict[str, Any] | None]

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


def _default_run(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        list(command),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=input_bytes is None,
        env=dict(env) if env is not None else None,
        check=False,
    )


def _invoke(
    run: Run,
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    try:
        completed = run(list(command), env=env, input_bytes=input_bytes)
    except OSError as error:
        raise ValueError(f"failed to execute {command[0]}: {error}") from error
    if not isinstance(completed, subprocess.CompletedProcess):
        raise ValueError(f"{command[0]} runner returned an invalid result")
    if completed.returncode != 0:
        stderr = completed.stderr.decode(errors="replace") if isinstance(completed.stderr, bytes) else str(completed.stderr or "")
        stdout = completed.stdout.decode(errors="replace") if isinstance(completed.stdout, bytes) else str(completed.stdout or "")
        raise ValueError(f"{command[0]} {command[1]} failed: {(stderr.strip() or stdout.strip() or f'exit {completed.returncode}')}")
    return completed


def _stdout(completed: subprocess.CompletedProcess[Any]) -> str:
    value = completed.stdout
    return value.decode(encoding="utf-8") if isinstance(value, bytes) else str(value or "")


def _unique_object(text: str, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result: raise ValueError(f"{label} contains duplicate JSON key {key!r}")  # noqa: E701
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict): raise ValueError(f"{label} must be a JSON object")  # noqa: E701
    return value


def _load_plan(plan_path: Path) -> dict[str, Any]:
    plan = core.load_json(plan_path)
    registry.validate_resolved_plan(plan)
    # Reconstruct the normalized block so an adapter never operates on a stale
    # or partially projected channel decision.
    if plan["publish"] != core.compute_publish_plan({"publish": plan["publish"]}):
        raise ValueError("resolved plan publish authority is malformed")
    return plan


def _result(channel: str, stage: str, status: str, **details: Any) -> dict[str, Any]:
    return {
        "kind": "ucm-publication-result",
        "schema_version": 1,
        "channel": channel,
        "stage": stage,
        "status": status,
        **details,
    }


def _channel(plan_path: Path, channel: str, stage: str) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    plan = _load_plan(plan_path)
    config = plan["publish"][channel]
    if not config["enabled"]:
        return plan, None, _result(channel, stage, "skipped", reason="disabled")
    return plan, config, None


def _wheel_filename(task: dict[str, Any]) -> str:
    architecture = core.cpu_toolchain_authority(task["cpu_arch"]).wheel_arch
    distribution = task["dist_name"].replace("-", "_")
    return f"{distribution}-{task['wheel_version']}-{task['python_abi']}-{task['python_abi']}-{task['wheel_platform']}_{architecture}.whl"


def _expected_wheels(plan: dict[str, Any]) -> list[str]:
    names = [_wheel_filename(task) for task in plan["wheel_tasks"]]
    if len(names) != 6 or len(set(names)) != 6:
        raise ValueError("publication requires exactly six unique wheel artifacts")
    return sorted(names)


def _expected_chart(plan: dict[str, Any]) -> str:
    return f"{plan['chart']['name']}-{plan['chart']['version']}.tgz"


def _validate_named_files(paths: Sequence[Path], expected: Sequence[str], label: str) -> list[Path]:
    normalized = [Path(path) for path in paths]
    names = [path.name for path in normalized]
    if len(names) != len(set(names)) or sorted(names) != sorted(expected) or any(not path.is_file() for path in normalized):
        raise ValueError(f"{label} requires the exact expected file set")
    return normalized


def expected_release_asset_names(plan_path: Path) -> list[str]:
    plan = _load_plan(plan_path)
    return sorted([*_expected_wheels(plan), _expected_chart(plan)])


def _default_http_get(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return _unique_object(response.read().decode("utf-8"), f"HTTP response from {url}")


def publish_pypi(
    plan_path: Path,
    wheels: Sequence[Path],
    *,
    stage: str,
    run: Run = _default_run,
    http_get: HttpGet = _default_http_get,
) -> dict[str, Any]:
    if stage not in {"publish", "readback"}: raise ValueError("PyPI stage must be publish or readback")  # noqa: E701,E501
    plan, config, skipped = _channel(plan_path, "pypi", stage)
    if skipped is not None: return skipped  # noqa: E701
    assert config is not None
    wheel_paths = _validate_named_files(wheels, _expected_wheels(plan), "PyPI publication")
    if stage == "publish":
        command = [sys.executable, "-m", "twine", "upload", "--repository-url", config["index"], *[str(path) for path in wheel_paths]]
        _invoke(run, command)
        return _result("pypi", stage, "published", filenames=[path.name for path in wheel_paths])

    expected_by_dist = {
        dist: sorted(name for name in _expected_wheels(plan) if name.startswith(dist.replace("-", "_") + "-"))
        for dist in config["dists"]
    }
    inventory: list[str] = []
    version = plan["source"]["ucm_version"]
    for dist in config["dists"]:
        response = http_get(f"https://pypi.org/pypi/{urllib.parse.quote(dist, safe='')}/{urllib.parse.quote(version, safe='')}/json")
        info = response.get("info")
        urls = response.get("urls")
        filenames = sorted(item.get("filename") for item in urls if isinstance(item, dict)) if isinstance(urls, list) else []
        if not isinstance(info, dict) or info.get("name") != dist or info.get("version") != version or filenames != expected_by_dist[dist]:
            raise ValueError(f"PyPI inventory differs for {dist}=={version}")
        inventory.append(f"{dist}=={version}")
    return _result("pypi", stage, "verified", distributions=inventory, filenames=_expected_wheels(plan))


def _require_release_image_matrix(plan: dict[str, Any]) -> None:
    if len(plan["family_tasks"]) != 3 or len(plan["image_tasks"]) != 6:
        raise ValueError("publication requires exactly 3 family tasks and 6 image tasks")
    for family in plan["family_tasks"]:
        linked = [image for image in plan["image_tasks"] if image["family_task_id"] == family["task_id"]]
        if len(linked) != 2 or {image["platform"] for image in linked} != {"linux/amd64", "linux/arm64"}:
            raise ValueError("each publication family requires exact linux/amd64 and linux/arm64 image tasks")


def _family_references(plan: dict[str, Any], namespace: str, *, configured_targets: bool) -> list[tuple[dict[str, Any], str]]:
    references: list[tuple[dict[str, Any], str]] = []
    for family in plan["family_tasks"]:
        if configured_targets:
            repository = family["target_repository"]
            if not repository.startswith(namespace + "/"):
                raise ValueError("resolved image target lies outside the configured GHCR namespace")
        else:
            repository = namespace.rstrip("/") + "/" + family["target_repository"].rsplit("/", 1)[-1]
        references.append((family, f"{repository}:{family['target_tag']}"))
    return sorted(references, key=lambda item: item[1])


def inspect_oci_reference(
    reference: str,
    *,
    run: Run = _default_run,
    crane_binary: str | None = None,
    insecure: bool = False,
) -> dict[str, Any]:
    if not isinstance(reference, str) or ":" not in reference.rsplit("/", 1)[-1]:
        raise ValueError("OCI readback requires an exact tagged reference")
    crane = crane_binary or registry.resolve_pinned_crane()

    def command(operation: str, *arguments: str) -> list[str]:
        value = [crane, operation, *arguments]
        if insecure: value.append("--insecure")  # noqa: E701
        return value

    with tempfile.TemporaryDirectory(prefix="ucm-empty-docker-config-") as docker_config:
        environment = os.environ.copy()
        environment["DOCKER_CONFIG"] = docker_config
        digest = _stdout(_invoke(run, command("digest", reference), env=environment)).strip()
        if _DIGEST.fullmatch(digest) is None: raise ValueError("registry readback returned a malformed index digest")  # noqa: E701,E501
        repository = reference.rsplit(":", 1)[0]
        immutable_reference = f"{repository}@{digest}"
        index = _unique_object(_stdout(_invoke(run, command("manifest", immutable_reference), env=environment)), "published OCI index")
        if index.get("mediaType") not in _INDEX_MEDIA_TYPES or not isinstance(index.get("manifests"), list):
            raise ValueError("published tag does not resolve to an OCI index")
        platforms: list[str] = []
        for descriptor in index["manifests"]:
            if not isinstance(descriptor, dict) or not isinstance(descriptor.get("platform"), dict) or descriptor.get("mediaType") not in _MANIFEST_MEDIA_TYPES:
                raise ValueError("published OCI index contains a malformed platform descriptor")
            platform = f"{descriptor['platform'].get('os')}/{descriptor['platform'].get('architecture')}"
            member_digest = descriptor.get("digest")
            if not isinstance(member_digest, str) or _DIGEST.fullmatch(member_digest) is None:
                raise ValueError("published OCI member digest is malformed")
            child = _unique_object(_stdout(_invoke(run, command("manifest", f"{repository}@{member_digest}"), env=environment)), f"published OCI member {platform}")
            if child.get("mediaType") != descriptor["mediaType"] or not isinstance(child.get("config"), dict) or _DIGEST.fullmatch(child["config"].get("digest", "")) is None:
                raise ValueError(f"published OCI member {platform} is malformed")
            config = _unique_object(_stdout(_invoke(run, command("config", "--platform", platform, immutable_reference), env=environment)), f"published OCI config {platform}")
            if config.get("os") != descriptor["platform"].get("os") or config.get("architecture") != descriptor["platform"].get("architecture"):
                raise ValueError(f"published OCI config differs from {platform}")
            platforms.append(platform)
        if sorted(platforms) != ["linux/amd64", "linux/arm64"] or len(platforms) != len(set(platforms)):
            raise ValueError("published OCI index must contain exact linux/amd64 and linux/arm64 platforms")
        return {"reference": reference, "index_digest": digest, "platforms": sorted(platforms)}


def _inspect_oci_families(
    plan: dict[str, Any],
    *,
    namespace: str,
    configured_targets: bool,
    run: Run,
    crane_binary: str | None,
) -> list[dict[str, Any]]:
    _require_release_image_matrix(plan)
    results: list[dict[str, Any]] = []
    for family, tagged_reference in _family_references(plan, namespace, configured_targets=configured_targets):
        inspected = inspect_oci_reference(tagged_reference, run=run, crane_binary=crane_binary)
        results.append({"family_task_id": family["task_id"], **inspected})
    return results


def readback_ghcr(
    plan_path: Path,
    *,
    run: Run = _default_run,
    crane_binary: str | None = None,
) -> dict[str, Any]:
    plan, config, skipped = _channel(plan_path, "ghcr", "readback")
    if skipped is not None: return skipped  # noqa: E701
    assert config is not None
    images = _inspect_oci_families(plan, namespace=config["namespace"], configured_targets=True, run=run, crane_binary=crane_binary)
    return _result("ghcr", "readback", "verified", images=images)


def publish_dockerhub(
    plan_path: Path,
    *,
    stage: str,
    run: Run = _default_run,
    crane_binary: str | None = None,
) -> dict[str, Any]:
    if stage not in {"publish", "readback"}: raise ValueError("Docker Hub stage must be publish or readback")  # noqa: E701,E501
    plan, config, skipped = _channel(plan_path, "dockerhub", stage)
    if skipped is not None: return skipped  # noqa: E701
    assert config is not None
    _require_release_image_matrix(plan)
    if stage == "publish":
        crane = crane_binary or registry.resolve_pinned_crane()
        targets = _family_references(plan, config["namespace"], configured_targets=False)
        for family, target in targets:
            source = f"{family['target_repository']}:{family['target_tag']}"
            _invoke(run, [crane, "copy", source, target])
        return _result("dockerhub", stage, "published", references=[target for _, target in targets])
    images = _inspect_oci_families(plan, namespace=config["namespace"], configured_targets=False, run=run, crane_binary=crane_binary)
    return _result("dockerhub", stage, "verified", images=images)


def publish_chart_oci(
    plan_path: Path,
    package: Path,
    *,
    stage: str,
    readback_dir: Path | None = None,
    run: Run = _default_run,
) -> dict[str, Any]:
    if stage not in {"publish", "readback"}: raise ValueError("Chart OCI stage must be publish or readback")  # noqa: E701,E501
    plan, config, skipped = _channel(plan_path, "chart_oci", stage)
    if skipped is not None: return skipped  # noqa: E701
    assert config is not None
    package = Path(package)
    expected_filename = _expected_chart(plan)
    if not package.is_file() or package.name != expected_filename:
        raise ValueError(f"Chart OCI publication requires exact package {expected_filename}")
    namespace = config["namespace"].rstrip("/")
    if stage == "publish":
        _invoke(run, ["helm", "push", str(package), f"oci://{namespace}"])
        return _result("chart_oci", stage, "published", filename=expected_filename, reference=f"oci://{namespace}/{plan['chart']['name']}:{plan['chart']['version']}")
    if readback_dir is None: raise ValueError("Chart OCI readback requires an output directory")  # noqa: E701
    readback_dir = Path(readback_dir)
    if readback_dir.exists() and any(readback_dir.iterdir()): raise ValueError("Chart OCI readback directory must be empty")  # noqa: E701,E501
    readback_dir.mkdir(parents=True, exist_ok=True)
    _invoke(run, ["helm", "pull", f"oci://{namespace}/{plan['chart']['name']}", "--version", plan["chart"]["version"], "--destination", str(readback_dir)])
    pulled = list(readback_dir.iterdir())
    if len(pulled) != 1 or not pulled[0].is_file() or pulled[0].name != expected_filename:
        raise ValueError(f"Chart OCI readback requires exact package {expected_filename}")
    if pulled[0].read_bytes() != package.read_bytes(): raise ValueError("Chart OCI readback differs from local package")  # noqa: E701,E501
    return _result("chart_oci", stage, "verified", filename=expected_filename, reference=f"oci://{namespace}/{plan['chart']['name']}:{plan['chart']['version']}")


def _default_github_api(
    path: str,
    *,
    method: str = "GET",
    body: object | None = None,
    content_type: str | None = None,
    allow_missing: bool = False,
) -> dict[str, Any] | None:
    command = ["gh", "api", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
    if content_type is not None: command.extend(["-H", f"Content-Type: {content_type}"])  # noqa: E701
    if method != "GET": command.extend(["--method", method])  # noqa: E701
    command.append(path)
    payload = None
    temporary: str | None = None
    if body is not None:
        payload = body if isinstance(body, bytes) else core.canonical_bytes(body)
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(payload)
            temporary = handle.name
        command.extend(["--input", temporary])
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    finally:
        if temporary is not None: Path(temporary).unlink(missing_ok=True)  # noqa: E701
    if completed.returncode != 0:
        message = completed.stderr.decode(encoding="utf-8", errors="replace").strip()
        if allow_missing and ("not found" in message.casefold() or "http 404" in message.casefold()): return None  # noqa: E701,E501
        raise ValueError(f"gh api {path} failed: {message or f'exit {completed.returncode}'}")
    output = completed.stdout.decode(encoding="utf-8").strip()
    return _unique_object(output, f"GitHub API {path}") if output else {}


def _github_release(plan: dict[str, Any], api: GithubApi, *, allow_missing: bool) -> dict[str, Any] | None:
    repository = plan["source"]["repository"]
    tag = plan["source"]["release_tag"]
    return api(f"repos/{repository}/releases/tags/{tag}", method="GET", allow_missing=allow_missing)


def _release_asset_names(release: dict[str, Any]) -> list[str]:
    assets = release.get("assets")
    if not isinstance(assets, list) or any(not isinstance(asset, dict) or not isinstance(asset.get("name"), str) for asset in assets):
        raise ValueError("GitHub Release assets are malformed")
    names = [asset["name"] for asset in assets]
    if len(names) != len(set(names)): raise ValueError("GitHub Release contains duplicate assets")  # noqa: E701
    return sorted(names)


def _validate_release_identity(plan: dict[str, Any], release: dict[str, Any]) -> None:
    if release.get("tag_name") != plan["source"]["release_tag"] or release.get("target_commitish") != plan["source"]["commit"]:
        raise ValueError("GitHub Release tag/source differs from resolved plan")
    if not isinstance(release.get("id"), int) or isinstance(release.get("id"), bool):
        raise ValueError("GitHub Release ID is malformed")


def _validate_public_release(plan: dict[str, Any], release: dict[str, Any]) -> list[str]:
    _validate_release_identity(plan, release)
    if release.get("draft") is not False or release.get("prerelease") is not True:
        raise ValueError("GitHub Release is not the expected public prerelease")
    names = _release_asset_names(release)
    if names != sorted([*_expected_wheels(plan), _expected_chart(plan)]):
        raise ValueError("public GitHub Release must contain exact seven assets")
    return names


def _validate_local_release_assets(plan: dict[str, Any], artifacts: Sequence[Path] | None) -> list[Path]:
    if artifacts is None: raise ValueError("GitHub Release assets stage requires exact artifact paths")  # noqa: E701
    try:
        return _validate_named_files(artifacts, [*_expected_wheels(plan), _expected_chart(plan)], "GitHub Release assets")
    except ValueError as error:
        raise ValueError("GitHub Release assets require exact seven release assets") from error


def publish_github_release(
    plan_path: Path,
    *,
    stage: str,
    artifacts: Sequence[Path] | None = None,
    api: GithubApi = _default_github_api,
) -> dict[str, Any]:
    if stage not in {"draft", "assets", "finalize", "readback"}:
        raise ValueError("GitHub Release stage must be draft, assets, finalize, or readback")
    plan, _config, skipped = _channel(plan_path, "github_release", stage)
    if skipped is not None: return skipped  # noqa: E701
    repository = plan["source"]["repository"]
    tag = plan["source"]["release_tag"]
    expected_names = sorted([*_expected_wheels(plan), _expected_chart(plan)])

    release = _github_release(plan, api, allow_missing=stage == "draft")
    if stage == "draft":
        if release is None:
            body = {"tag_name": tag, "target_commitish": plan["source"]["commit"], "name": f"UCM {tag}", "body": f"UCM {tag} prerelease.", "draft": True, "prerelease": True, "make_latest": "false"}
            release = api(f"repos/{repository}/releases", method="POST", body=body)
            if not isinstance(release, dict): raise ValueError("GitHub Release create returned no release")  # noqa: E701,E501
            _validate_release_identity(plan, release)
            if release.get("draft") is not True or release.get("prerelease") is not True:
                raise ValueError("GitHub Release create did not return the expected Draft")
            return _result("github_release", stage, "created", release_id=release["id"], tag=tag, draft=True)
        _validate_release_identity(plan, release)
        if release.get("draft") is False:
            names = _validate_public_release(plan, release)
            return _result("github_release", stage, "reused", release_id=release["id"], tag=tag, draft=False, asset_names=names)
        if release.get("draft") is not True or release.get("prerelease") is not True:
            raise ValueError("existing GitHub Release is not the expected Draft")
        return _result("github_release", stage, "reused", release_id=release["id"], tag=tag, draft=True)

    if not isinstance(release, dict): raise ValueError("GitHub Release does not exist")  # noqa: E701
    _validate_release_identity(plan, release)
    if release.get("draft") is False:
        names = _validate_public_release(plan, release)
        status = "verified" if stage == "readback" else "reused"
        return _result("github_release", stage, status, release_id=release["id"], tag=tag, draft=False, asset_names=names)
    if release.get("draft") is not True or release.get("prerelease") is not True:
        raise ValueError("GitHub Release is neither the expected Draft nor public prerelease")
    if stage == "readback": raise ValueError("GitHub Release readback requires a public prerelease")  # noqa: E701

    if stage == "assets":
        local_assets = _validate_local_release_assets(plan, artifacts)
        current_names = _release_asset_names(release)
        extras = sorted(set(current_names) - set(expected_names))
        if extras: raise ValueError(f"GitHub Release Draft contains unexpected assets: {extras}")  # noqa: E701,E501
        current = set(current_names)
        for path in sorted(local_assets, key=lambda item: item.name):
            if path.name in current: continue  # noqa: E701
            encoded = urllib.parse.quote(path.name, safe="")
            uploaded = api(f"https://uploads.github.com/repos/{repository}/releases/{release['id']}/assets?name={encoded}", method="POST", body=path.read_bytes(), content_type="application/octet-stream")
            if not isinstance(uploaded, dict) or uploaded.get("name") != path.name:
                raise ValueError(f"GitHub Release upload did not return asset {path.name}")
        reread = _github_release(plan, api, allow_missing=False)
        if not isinstance(reread, dict) or _release_asset_names(reread) != expected_names:
            raise ValueError("GitHub Release Draft does not contain exact seven assets after upload")
        return _result("github_release", stage, "uploaded", release_id=release["id"], tag=tag, draft=True, asset_names=expected_names)

    names = _release_asset_names(release)
    if names != expected_names:
        raise ValueError("GitHub Release Draft must contain exact seven assets before finalize")
    finalized = api(f"repos/{repository}/releases/{release['id']}", method="PATCH", body={"draft": False, "prerelease": True})
    if not isinstance(finalized, dict): raise ValueError("GitHub Release finalize returned no release")  # noqa: E701
    _validate_public_release(plan, finalized)
    return _result("github_release", stage, "finalized", release_id=release["id"], tag=tag, draft=False, asset_names=names)


# fmt: on
