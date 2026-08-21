# fmt: off
from __future__ import annotations

import hashlib
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
BytesGet = Callable[[str], bytes]

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


def _result(
    plan: dict[str, Any],
    channel: str,
    stage: str,
    status: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "kind": "ucm-publication-result",
        "schema_version": 1,
        "channel": channel,
        "stage": stage,
        "status": status,
        "resolved_plan_sha256": plan["resolved_plan_sha256"],
        **details,
    }


def _channel(plan_path: Path, channel: str, stage: str) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    plan = _load_plan(plan_path)
    config = plan["publish"][channel]
    if not config["enabled"]:
        return plan, None, _result(plan, channel, stage, "skipped", reason="disabled")
    return plan, config, None


def _require_protected_plan(plan: dict[str, Any]) -> None:
    source = plan["source"]
    if (
        plan["fixture_only"] is not False
        or plan["lane"] != "protected-tag"
        or not source["release_tag"].startswith("v")
        or source["release_tag"] != f"v{source['ucm_version']}"
    ):
        raise ValueError(
            "publication requires a non-fixture protected-tag plan with exact v* source"
        )


def _require_release_binding(
    plan: dict[str, Any], draft_state: Path | None
) -> tuple[dict[str, Any], bool]:
    if draft_state is None:
        raise ValueError("publication write requires a GitHub Release binding")
    try:
        state = core.load_json(Path(draft_state))
    except (OSError, ValueError) as error:
        raise ValueError("GitHub Release binding is unreadable") from error
    common_keys = {
        "kind",
        "schema_version",
        "channel",
        "stage",
        "status",
        "resolved_plan_sha256",
        "release_id",
        "tag",
        "draft",
    }
    if (
        state.get("kind") != "ucm-publication-result"
        or state.get("schema_version") != 1
        or state.get("channel") != "github_release"
        or state.get("stage") != "draft"
        or state.get("status") not in {"created", "reused"}
        or state.get("resolved_plan_sha256") != plan["resolved_plan_sha256"]
        or state.get("tag") != plan["source"]["release_tag"]
        or not isinstance(state.get("release_id"), int)
        or isinstance(state.get("release_id"), bool)
        or state["release_id"] < 1
    ):
        raise ValueError("GitHub Release Draft state or public binding differs from resolved plan")
    if state.get("draft") is True:
        if set(state) != common_keys:
            raise ValueError(
                "GitHub Release Draft state or public binding differs from resolved plan"
            )
        return state, False
    if (
        state.get("draft") is False
        and state.get("status") == "reused"
        and set(state) == common_keys | {"asset_names"}
        and state.get("asset_names")
        == sorted([*_expected_wheels(plan), _expected_chart(plan)])
    ):
        return state, True
    raise ValueError(
        "GitHub Release Draft state or public binding differs from resolved plan"
    )


def _wheel_filename(task: dict[str, Any]) -> str:
    architecture = core.cpu_toolchain_authority(task["cpu_arch"]).wheel_arch
    distribution = task["dist_name"].replace("-", "_")
    return f"{distribution}-{task['wheel_version']}-{task['python_abi']}-{task['python_abi']}-{task['wheel_platform']}_{architecture}.whl"


def _expected_wheels(plan: dict[str, Any]) -> list[str]:
    names = [_wheel_filename(task) for task in plan["wheel_tasks"]]
    if len(names) != len(set(names)):
        raise ValueError(
            "publication requires one unique wheel artifact per resolved wheel task"
        )
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
    draft_state: Path | None = None,
    run: Run = _default_run,
    http_get: HttpGet = _default_http_get,
) -> dict[str, Any]:
    if stage not in {"publish", "readback"}: raise ValueError("PyPI stage must be publish or readback")  # noqa: E701,E501
    plan, config, skipped = _channel(plan_path, "pypi", stage)
    if skipped is not None: return skipped  # noqa: E701
    assert config is not None
    _require_protected_plan(plan)
    if stage == "publish":
        _binding, already_public = _require_release_binding(plan, draft_state)
    elif draft_state is not None:
        raise ValueError("PyPI readback does not accept a Draft state")
    wheel_paths = _validate_named_files(wheels, _expected_wheels(plan), "PyPI publication")
    if stage == "publish":
        if already_public:
            return _result(
                plan,
                "pypi",
                stage,
                "reused",
                filenames=[path.name for path in wheel_paths],
            )
        command = [sys.executable, "-m", "twine", "upload", "--repository-url", config["index"], *[str(path) for path in wheel_paths]]
        _invoke(run, command)
        return _result(plan, "pypi", stage, "published", filenames=[path.name for path in wheel_paths])

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
    return _result(plan, "pypi", stage, "verified", distributions=inventory, filenames=_expected_wheels(plan))


def _require_release_image_matrix(plan: dict[str, Any]) -> None:
    families = plan.get("family_tasks")
    images = plan.get("image_tasks")
    if (
        not isinstance(families, list)
        or not isinstance(images, list)
        or not all(isinstance(task, dict) for task in [*families, *images])
    ):
        raise ValueError("publication matrix family/image linkage is malformed")
    families_by_id = {family.get("task_id"): family for family in families}
    image_ids = {image.get("task_id") for image in images}
    if len(families_by_id) != len(families) or len(image_ids) != len(images):
        raise ValueError("publication matrix family/image linkage is not unique")
    for family in families:
        linked = [
            image
            for image in images
            if image.get("family_task_id") == family.get("task_id")
        ]
        declared_architectures = family.get("cpu_arch")
        declared_platforms = family.get("platform")
        declared_image_ids = family.get("image_task_ids")
        if (
            not isinstance(declared_architectures, list)
            or not isinstance(declared_platforms, list)
            or not isinstance(declared_image_ids, list)
            or len(declared_architectures) != len(declared_platforms)
            or len(linked) != len(declared_architectures)
            or sorted(zip(declared_architectures, declared_platforms, strict=True))
            != sorted((image.get("cpu_arch"), image.get("platform")) for image in linked)
            or set(declared_image_ids) != {image.get("task_id") for image in linked}
        ):
            raise ValueError(
                "publication matrix differs from exact resolved family/image linkage"
            )
    if any(image.get("family_task_id") not in families_by_id for image in images):
        raise ValueError(
            "publication matrix differs from exact resolved family/image linkage"
        )


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
    expected_platforms: Sequence[str],
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
        members: list[dict[str, str]] = []
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
            members.append(
                {
                    "platform": platform,
                    "manifest_digest": member_digest,
                    "config_digest": child["config"]["digest"],
                }
            )
        expected = sorted(expected_platforms)
        if (
            not expected
            or any(not isinstance(platform, str) or not platform for platform in expected)
            or len(expected) != len(set(expected))
        ):
            raise ValueError("OCI readback expected platform set is malformed")
        if sorted(platforms) != expected or len(platforms) != len(set(platforms)):
            raise ValueError("published OCI index differs from expected platform set")
        return {
            "reference": reference,
            "index_digest": digest,
            "platforms": sorted(platforms),
            "members": sorted(members, key=lambda item: item["platform"]),
        }


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
        inspected = inspect_oci_reference(tagged_reference, expected_platforms=family["platform"], run=run, crane_binary=crane_binary)
        results.append({"family_task_id": family["task_id"], **inspected})
    return results


def _load_member_results(
    plan: dict[str, Any], members_dir: Path
) -> dict[str, list[dict[str, Any]]]:
    members_dir = Path(members_dir)
    entries = sorted(members_dir.iterdir()) if members_dir.is_dir() else []
    expected_member_count = len(plan["image_tasks"])
    if len(entries) != expected_member_count or any(not path.is_file() or path.suffix != ".json" for path in entries):
        raise ValueError(
            "GHCR publish requires one member-result JSON file per resolved image task"
        )
    tasks_by_sha = {task["task_sha256"]: task for task in plan["image_tasks"]}
    if len(tasks_by_sha) != expected_member_count:
        raise ValueError("GHCR publish requires unique resolved image task hashes")
    by_family: dict[str, list[dict[str, Any]]] = {}
    seen_tasks: set[str] = set()
    schema = core.load_json(core.DEFAULT_SCHEMA_DIR / "release-manifest.schema.json")
    member_schema = schema["$defs"]["registryMemberRecord"]
    required = {
        "schema_version",
        "kind",
        "status",
        "resolved_plan_sha256",
        "spec_id",
        "profile_id",
        "family_id",
        "platform",
        "target_repository",
        "target_tag",
        "staging_repository",
        "staging_visibility",
        "staging_tag",
        "candidate_task_sha256",
        "publication_task_sha256",
        "member_digest",
        "config_digest",
        "manifest",
        "config",
        "source_sha",
    }
    for path in entries:
        record = core.load_json(path)
        core.validate_schema(record, member_schema, root=schema)
        expected_record_sha256 = core.sha256_value(
            {key: value for key, value in record.items() if key != "record_sha256"}
        )
        if record.get("record_sha256") != expected_record_sha256:
            raise ValueError("member record_sha256 is not reproducible")
        if not required <= set(record):
            raise ValueError(f"member result {path.name} is missing publication fields")
        task_sha = record["publication_task_sha256"]
        task = tasks_by_sha.get(task_sha)
        if task is None or task_sha in seen_tasks:
            raise ValueError("member publication task differs from resolved image tasks")
        if (
            record["schema_version"] != 1
            or record["kind"] != "ucm-registry-member-publication"
            or record["status"] != "passed"
        ):
            raise ValueError("member result identity is invalid")
        if record["resolved_plan_sha256"] != plan["resolved_plan_sha256"]:
            raise ValueError("member resolved_plan_sha256 differs from resolved plan")
        if record["candidate_task_sha256"] != task["task_sha256"]:
            raise ValueError("member candidate task differs from resolved image task")
        if (
            record["family_id"] != task["family_task_id"]
            or record["platform"] != task["platform"]
            or record["spec_id"] != task["spec_id"]
            or record["profile_id"] != task["profile_id"]
        ):
            raise ValueError("member family/platform/task differs from resolved image task")
        if (
            record["target_repository"] != task["target_repository"]
            or record["target_tag"] != task["target_tag"]
        ):
            raise ValueError("member target differs from resolved image task")
        if (
            record["source_sha"] != plan["source"]["commit"]
            or record["staging_repository"]
            != plan["source"]["staging_repository"]
            or record["staging_visibility"] != "private"
        ):
            raise ValueError("member source/staging authority differs from resolved plan")
        member_digest = record["member_digest"]
        config_digest = record["config_digest"]
        manifest = record["manifest"]
        config = record["config"]
        if (
            not isinstance(member_digest, str)
            or _DIGEST.fullmatch(member_digest) is None
            or not isinstance(config_digest, str)
            or _DIGEST.fullmatch(config_digest) is None
            or not isinstance(manifest, dict)
            or manifest.get("digest") != member_digest
            or manifest.get("media_type") not in _MANIFEST_MEDIA_TYPES
            or not isinstance(config, dict)
            or config.get("digest") != config_digest
            or config.get("blob_sha256") != config_digest
            or config.get("media_type")
            != "application/vnd.oci.image.config.v1+json"
            or not isinstance(record["staging_tag"], str)
            or re.fullmatch(r"staging-[0-9a-f]{64}", record["staging_tag"])
            is None
        ):
            raise ValueError("member staging digest/config evidence is invalid")
        content_identity = record["content_identity"]
        expected_content_identity_sha256 = core.sha256_value(
            {
                key: value
                for key, value in content_identity.items()
                if key != "content_identity_sha256"
            }
        )
        if (
            content_identity["content_identity_sha256"]
            != expected_content_identity_sha256
            or record["content_identity_sha256"]
            != expected_content_identity_sha256
            or content_identity["manifest_digest"] != member_digest
            or content_identity["config_digest"] != config_digest
            or content_identity["task_sha256"] != task["task_sha256"]
            or content_identity["build_key_sha256"] != record["build_key_sha256"]
            or content_identity["wheel_sha256"] != record["wheel_sha256"]
            or content_identity["recipe_sha256"] != record["recipe_sha256"]
            or content_identity["source"]["repository"]
            != plan["source"]["repository"]
            or content_identity["source"]["commit"] != plan["source"]["commit"]
            or record["source_sha"] != content_identity["source"]["commit"]
        ):
            raise ValueError("member content identity/provenance is inconsistent")
        expected_annotations = {
            "io.ucm.release.build-key-sha256": record["build_key_sha256"],
            "io.ucm.release.candidate-task-sha256": task["task_sha256"],
            "io.ucm.release.family-id": task["family_task_id"],
            "io.ucm.release.platform": task["platform"],
            "io.ucm.release.spec-id": task["spec_id"],
            "io.ucm.release.wheel-sha256": record["wheel_sha256"],
        }
        expected_manifest_annotations = {
            "io.ucm.release.recipe-sha256": record["recipe_sha256"],
            "io.ucm.release.task-sha256": task["task_sha256"],
        }
        expected_labels = {
            "org.opencontainers.image.source": content_identity["source"][
                "repository_url"
            ],
            "org.opencontainers.image.revision": plan["source"]["commit"],
            "io.ucm.release.source-tree": content_identity["source"]["tree"],
            "io.ucm.release.source-context-sha256": content_identity["source"][
                "context_sha256"
            ],
            "io.ucm.release.build-key-sha256": record["build_key_sha256"],
            "io.ucm.release.task-sha256": task["task_sha256"],
            "io.ucm.release.wheel-sha256": record["wheel_sha256"],
            "io.ucm.release.recipe-sha256": record["recipe_sha256"],
        }
        if (
            record["annotations"] != expected_annotations
            or record["manifest"]["annotations"] != expected_manifest_annotations
            or content_identity["annotations"] != expected_manifest_annotations
            or record["config"]["labels"] != content_identity["labels"]
            or any(
                content_identity["labels"].get(key) != value
                for key, value in expected_labels.items()
            )
            or record["member_size"] != record["manifest"]["size"]
        ):
            raise ValueError("member provenance annotations are inconsistent")
        expected_layers: list[dict[str, Any]] = []
        for layer in content_identity["layers"]:
            projected = {
                "media_type": layer["mediaType"],
                "digest": layer["digest"],
                "size": layer["size"],
                "blob_sha256": layer["digest"],
            }
            if "annotations" in layer:
                projected["annotations"] = layer["annotations"]
            expected_layers.append(projected)
        if record["layers"] != expected_layers:
            raise ValueError("member layers differ from content identity")
        staging_digest = f"{record['staging_repository']}@{member_digest}"
        staging_reference = (
            f"{record['staging_repository']}:{record['staging_tag']}"
        )
        expected_operations = [
            {
                "type": "registry-member-push-by-digest",
                "capability": "write",
                "reference": staging_digest,
            },
            {
                "type": "registry-staging-tag-create",
                "capability": "write",
                "reference": staging_reference,
            },
            {
                "type": "registry-authenticated-digest-read",
                "capability": "read",
                "reference": staging_digest,
            },
            {
                "type": "registry-authenticated-manifest-read",
                "capability": "read",
                "reference": staging_digest,
            },
            {
                "type": "registry-authenticated-config-blob-read",
                "capability": "read",
                "reference": f"{record['staging_repository']}@{config_digest}",
            },
        ]
        if (
            record["staging_tag"]
            != "staging-" + record["build_key_sha256"].removeprefix("sha256:")
            or record["operations"] != expected_operations
        ):
            raise ValueError("member staging operations are not exact")
        expected_readback_sha256 = core.sha256_value(
            {"manifest": record["manifest"], "config": record["config"]}
        )
        if record["readback_sha256"] != expected_readback_sha256:
            raise ValueError("member readback_sha256 is not reproducible")
        seen_tasks.add(task_sha)
        by_family.setdefault(task["family_task_id"], []).append(record)
    if seen_tasks != set(tasks_by_sha):
        raise ValueError("member result set differs from resolved image tasks")
    families_by_id = {family["task_id"]: family for family in plan["family_tasks"]}
    for family_id, records in by_family.items():
        records.sort(key=lambda item: item["platform"])
        family = families_by_id.get(family_id)
        if family is None or [record["platform"] for record in records] != sorted(
            family["platform"]
        ):
            raise ValueError(
                f"member family {family_id} differs from resolved family platforms"
            )
    return by_family


def publish_ghcr(
    plan_path: Path,
    *,
    stage: str,
    members_dir: Path | None = None,
    draft_state: Path | None = None,
    run: Run = _default_run,
    crane_binary: str | None = None,
) -> dict[str, Any]:
    if stage not in {"publish", "readback"}:
        raise ValueError("GHCR stage must be publish or readback")
    plan, config, skipped = _channel(plan_path, "ghcr", stage)
    if skipped is not None: return skipped  # noqa: E701
    assert config is not None
    _require_protected_plan(plan)
    _require_release_image_matrix(plan)
    if stage == "readback":
        if members_dir is not None or draft_state is not None:
            raise ValueError("GHCR readback does not accept members or Draft state")
        images = _inspect_oci_families(plan, namespace=config["namespace"], configured_targets=True, run=run, crane_binary=crane_binary)
        return _result(plan, "ghcr", stage, "verified", images=images)

    _binding, already_public = _require_release_binding(plan, draft_state)
    if already_public:
        return _result(plan, "ghcr", stage, "reused")
    if members_dir is None:
        raise ValueError("GHCR publish requires a members directory")
    records_by_family = _load_member_results(plan, members_dir)
    images: list[dict[str, Any]] = []
    for family, target in _family_references(
        plan, config["namespace"], configured_targets=True
    ):
        records = records_by_family.get(family["task_id"])
        if records is None:
            raise ValueError("member results are missing a resolved family")
        sources = [
            f"{record['staging_repository']}@{record['member_digest']}"
            for record in records
        ]
        _invoke(
            run,
            [
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                target,
                *sources,
            ],
        )
        inspected = inspect_oci_reference(
            target,
            expected_platforms=family["platform"],
            run=run,
            crane_binary=crane_binary,
        )
        expected_members = [
            {
                "platform": record["platform"],
                "manifest_digest": record["member_digest"],
                "config_digest": record["config_digest"],
            }
            for record in records
        ]
        if inspected["members"] != expected_members:
            raise ValueError("published GHCR index differs from validated member results")
        images.append({"family_task_id": family["task_id"], **inspected})
    return _result(plan, "ghcr", stage, "published", images=images)


def publish_dockerhub(
    plan_path: Path,
    *,
    stage: str,
    draft_state: Path | None = None,
    run: Run = _default_run,
    crane_binary: str | None = None,
) -> dict[str, Any]:
    if stage not in {"publish", "readback"}: raise ValueError("Docker Hub stage must be publish or readback")  # noqa: E701,E501
    plan, config, skipped = _channel(plan_path, "dockerhub", stage)
    if skipped is not None: return skipped  # noqa: E701
    assert config is not None
    _require_protected_plan(plan)
    _require_release_image_matrix(plan)
    if stage == "publish":
        _binding, already_public = _require_release_binding(plan, draft_state)
        if already_public:
            return _result(plan, "dockerhub", stage, "reused")
        crane = crane_binary or registry.resolve_pinned_crane()
        targets = _family_references(plan, config["namespace"], configured_targets=False)
        for family, target in targets:
            source = f"{family['target_repository']}:{family['target_tag']}"
            _invoke(run, [crane, "copy", source, target])
        return _result(plan, "dockerhub", stage, "published", references=[target for _, target in targets])
    if draft_state is not None:
        raise ValueError("Docker Hub readback does not accept a Draft state")
    images: list[dict[str, Any]] = []
    targets_by_family = {
        family["task_id"]: reference
        for family, reference in _family_references(
            plan, config["namespace"], configured_targets=False
        )
    }
    for family, source_reference in _family_references(
        plan,
        plan["publish"]["ghcr"]["namespace"],
        configured_targets=True,
    ):
        target_reference = targets_by_family[family["task_id"]]
        source = inspect_oci_reference(
            source_reference,
            expected_platforms=family["platform"],
            run=run,
            crane_binary=crane_binary,
        )
        target = inspect_oci_reference(
            target_reference,
            expected_platforms=family["platform"],
            run=run,
            crane_binary=crane_binary,
        )
        if source["index_digest"] != target["index_digest"]:
            raise ValueError("Docker Hub target index differs from GHCR source")
        images.append(
            {
                "family_task_id": family["task_id"],
                "source": source,
                "target": target,
            }
        )
    return _result(plan, "dockerhub", stage, "verified", images=images)


def publish_chart_oci(
    plan_path: Path,
    package: Path,
    *,
    stage: str,
    draft_state: Path | None = None,
    readback_dir: Path | None = None,
    run: Run = _default_run,
) -> dict[str, Any]:
    if stage not in {"publish", "readback"}: raise ValueError("Chart OCI stage must be publish or readback")  # noqa: E701,E501
    plan, config, skipped = _channel(plan_path, "chart_oci", stage)
    if skipped is not None: return skipped  # noqa: E701
    assert config is not None
    _require_protected_plan(plan)
    if stage == "publish":
        _binding, already_public = _require_release_binding(plan, draft_state)
        if readback_dir is not None:
            raise ValueError("Chart OCI publish does not accept a readback directory")
    elif draft_state is not None:
        raise ValueError("Chart OCI readback does not accept a Draft state")
    package = Path(package)
    expected_filename = _expected_chart(plan)
    if not package.is_file() or package.name != expected_filename:
        raise ValueError(f"Chart OCI publication requires exact package {expected_filename}")
    namespace = config["namespace"].rstrip("/")
    if stage == "publish":
        if already_public:
            return _result(
                plan,
                "chart_oci",
                stage,
                "reused",
                filename=expected_filename,
            )
        _invoke(run, ["helm", "push", str(package), f"oci://{namespace}"])
        return _result(plan, "chart_oci", stage, "published", filename=expected_filename, reference=f"oci://{namespace}/{plan['chart']['name']}:{plan['chart']['version']}")
    if readback_dir is None: raise ValueError("Chart OCI readback requires an output directory")  # noqa: E701
    readback_dir = Path(readback_dir)
    if readback_dir.exists() and any(readback_dir.iterdir()): raise ValueError("Chart OCI readback directory must be empty")  # noqa: E701,E501
    readback_dir.mkdir(parents=True, exist_ok=True)
    _invoke(run, ["helm", "pull", f"oci://{namespace}/{plan['chart']['name']}", "--version", plan["chart"]["version"], "--destination", str(readback_dir)])
    pulled = list(readback_dir.iterdir())
    if len(pulled) != 1 or not pulled[0].is_file() or pulled[0].name != expected_filename:
        raise ValueError(f"Chart OCI readback requires exact package {expected_filename}")
    if pulled[0].read_bytes() != package.read_bytes(): raise ValueError("Chart OCI readback differs from local package")  # noqa: E701,E501
    return _result(plan, "chart_oci", stage, "verified", filename=expected_filename, reference=f"oci://{namespace}/{plan['chart']['name']}:{plan['chart']['version']}")


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


def _github_release_by_id(
    plan: dict[str, Any], api: GithubApi, release_id: int
) -> dict[str, Any] | None:
    repository = plan["source"]["repository"]
    return api(
        f"repos/{repository}/releases/{release_id}",
        method="GET",
        allow_missing=False,
    )


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
        raise ValueError("public GitHub Release must contain the exact resolved assets")
    return names


def _validate_local_release_assets(plan: dict[str, Any], artifacts: Sequence[Path] | None) -> list[Path]:
    if artifacts is None: raise ValueError("GitHub Release assets stage requires exact artifact paths")  # noqa: E701
    try:
        return _validate_named_files(artifacts, [*_expected_wheels(plan), _expected_chart(plan)], "GitHub Release assets")
    except ValueError as error:
        raise ValueError("GitHub Release assets require the exact resolved asset set") from error


def _asset_manifest(paths: Sequence[Path]) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in paths
        ),
        key=lambda item: item["name"],
    )


def _validate_asset_manifest(
    plan: dict[str, Any], manifest: object
) -> list[dict[str, Any]]:
    expected_names = sorted([*_expected_wheels(plan), _expected_chart(plan)])
    if not isinstance(manifest, list) or len(manifest) != len(expected_names):
        raise ValueError(
            "GitHub Release asset manifest requires one entry per resolved asset"
        )
    normalized: list[dict[str, Any]] = []
    for item in manifest:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "size", "sha256"}
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or _DIGEST.fullmatch(item["sha256"]) is None
        ):
            raise ValueError("GitHub Release asset manifest is malformed")
        normalized.append(dict(item))
    normalized.sort(key=lambda item: item["name"])
    if [item["name"] for item in normalized] != expected_names:
        raise ValueError("GitHub Release asset manifest differs from resolved assets")
    return normalized


def _default_github_bytes(url: str) -> bytes:
    completed = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/octet-stream",
            url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode(errors="replace").strip()
        raise ValueError(f"GitHub asset download failed: {message or f'exit {completed.returncode}'}")
    return bytes(completed.stdout)


def _default_http_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read()


def _verify_release_asset_bytes(
    release: dict[str, Any],
    expected_manifest: Sequence[dict[str, Any]],
    *,
    download_bytes: BytesGet,
    public: bool,
) -> None:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("GitHub Release assets are malformed")
    by_name = {
        asset.get("name"): asset
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
    }
    if len(by_name) != len(assets):
        raise ValueError("GitHub Release assets are malformed")
    for expected in expected_manifest:
        asset = by_name.get(expected["name"])
        if asset is None:
            continue
        url_key = "browser_download_url" if public else "url"
        url = asset.get(url_key)
        if (
            not isinstance(asset.get("size"), int)
            or isinstance(asset.get("size"), bool)
            or asset["size"] != expected["size"]
            or not isinstance(url, str)
            or not url
        ):
            raise ValueError(f"GitHub Release asset bytes differ for {expected['name']}")
        content = download_bytes(url)
        observed = {
            "size": len(content),
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        }
        if observed != {"size": expected["size"], "sha256": expected["sha256"]}:
            raise ValueError(f"GitHub Release asset bytes differ for {expected['name']}")


def _require_asset_state(
    plan: dict[str, Any], asset_state: Path | None
) -> dict[str, Any]:
    if asset_state is None:
        raise ValueError("GitHub Release finalize/readback requires an asset state")
    try:
        state = core.load_json(Path(asset_state))
    except (OSError, ValueError) as error:
        raise ValueError("GitHub Release asset state is unreadable") from error
    expected_keys = {
        "kind",
        "schema_version",
        "channel",
        "stage",
        "status",
        "resolved_plan_sha256",
        "release_id",
        "tag",
        "draft",
        "asset_names",
        "asset_manifest",
    }
    expected_names = sorted([*_expected_wheels(plan), _expected_chart(plan)])
    if (
        set(state) != expected_keys
        or state.get("kind") != "ucm-publication-result"
        or state.get("schema_version") != 1
        or state.get("channel") != "github_release"
        or state.get("stage") != "assets"
        or state.get("status") not in {"uploaded", "reused"}
        or state.get("resolved_plan_sha256") != plan["resolved_plan_sha256"]
        or state.get("tag") != plan["source"]["release_tag"]
        or not isinstance(state.get("release_id"), int)
        or isinstance(state.get("release_id"), bool)
        or state["release_id"] < 1
        or not isinstance(state.get("draft"), bool)
        or state.get("asset_names") != expected_names
    ):
        raise ValueError("GitHub Release asset state differs from resolved plan")
    state["asset_manifest"] = _validate_asset_manifest(
        plan, state.get("asset_manifest")
    )
    return state


def publish_github_release(
    plan_path: Path,
    *,
    stage: str,
    artifacts: Sequence[Path] | None = None,
    draft_state: Path | None = None,
    asset_state: Path | None = None,
    api: GithubApi = _default_github_api,
    http_get: HttpGet = _default_http_get,
    download_bytes: BytesGet | None = None,
) -> dict[str, Any]:
    if stage not in {"draft", "assets", "finalize", "readback"}:
        raise ValueError("GitHub Release stage must be draft, assets, finalize, or readback")
    plan, _config, skipped = _channel(plan_path, "github_release", stage)
    if skipped is not None: return skipped  # noqa: E701
    _require_protected_plan(plan)
    if stage == "draft":
        if artifacts is not None or draft_state is not None or asset_state is not None:
            raise ValueError("GitHub Release draft does not accept artifacts or state")
        bound_draft = None
    elif stage == "assets":
        if artifacts is None or asset_state is not None:
            raise ValueError("GitHub Release assets stage requires artifact paths")
        bound_draft, _already_public = _require_release_binding(plan, draft_state)
    elif stage == "finalize":
        if artifacts is not None:
            raise ValueError("GitHub Release finalize does not accept artifacts")
        bound_draft, _already_public = _require_release_binding(plan, draft_state)
        bound_assets = _require_asset_state(plan, asset_state)
    else:
        if artifacts is not None or draft_state is not None:
            raise ValueError("GitHub Release readback does not accept artifacts or Draft state")
        bound_draft = None
        bound_assets = _require_asset_state(plan, asset_state)
    repository = plan["source"]["repository"]
    tag = plan["source"]["release_tag"]
    expected_names = sorted([*_expected_wheels(plan), _expected_chart(plan)])

    if stage == "readback":
        encoded_repository = "/".join(
            urllib.parse.quote(part, safe="") for part in repository.split("/")
        )
        encoded_tag = urllib.parse.quote(tag, safe="")
        release = http_get(
            f"https://api.github.com/repos/{encoded_repository}/releases/tags/{encoded_tag}"
        )
        names = _validate_public_release(plan, release)
        if release["id"] != bound_assets["release_id"]:
            raise ValueError("public GitHub Release differs from asset state")
        _verify_release_asset_bytes(
            release,
            bound_assets["asset_manifest"],
            download_bytes=download_bytes or _default_http_bytes,
            public=True,
        )
        return _result(
            plan,
            "github_release",
            stage,
            "verified",
            release_id=release["id"],
            tag=tag,
            draft=False,
            asset_names=names,
            asset_manifest=bound_assets["asset_manifest"],
        )

    if bound_draft is not None and bound_draft["draft"] is True:
        release = _github_release_by_id(plan, api, bound_draft["release_id"])
    else:
        release = _github_release(plan, api, allow_missing=stage == "draft")
    if stage == "draft":
        if release is None:
            body = {"tag_name": tag, "target_commitish": plan["source"]["commit"], "name": f"UCM {tag}", "body": f"UCM {tag} prerelease.", "draft": True, "prerelease": True, "make_latest": "false"}
            release = api(f"repos/{repository}/releases", method="POST", body=body)
            if not isinstance(release, dict): raise ValueError("GitHub Release create returned no release")  # noqa: E701,E501
            _validate_release_identity(plan, release)
            if release.get("draft") is not True or release.get("prerelease") is not True:
                raise ValueError("GitHub Release create did not return the expected Draft")
            return _result(plan, "github_release", stage, "created", release_id=release["id"], tag=tag, draft=True)
        _validate_release_identity(plan, release)
        if release.get("draft") is False:
            names = _validate_public_release(plan, release)
            return _result(plan, "github_release", stage, "reused", release_id=release["id"], tag=tag, draft=False, asset_names=names)
        if release.get("draft") is not True or release.get("prerelease") is not True:
            raise ValueError("existing GitHub Release is not the expected Draft")
        return _result(plan, "github_release", stage, "reused", release_id=release["id"], tag=tag, draft=True)

    if not isinstance(release, dict): raise ValueError("GitHub Release does not exist")  # noqa: E701
    _validate_release_identity(plan, release)
    if bound_draft is not None and release["id"] != bound_draft["release_id"]:
        raise ValueError("GitHub Release live Draft differs from Draft state")
    if stage == "finalize" and (
        release["id"] != bound_assets["release_id"]
        or bound_draft["draft"] != bound_assets["draft"]
    ):
        raise ValueError("GitHub Release asset state differs from release binding")
    if release.get("draft") is False:
        names = _validate_public_release(plan, release)
        if stage == "assets":
            local_assets = _validate_local_release_assets(plan, artifacts)
            manifest = _asset_manifest(local_assets)
            _verify_release_asset_bytes(
                release,
                manifest,
                download_bytes=download_bytes or _default_github_bytes,
                public=False,
            )
        if stage == "finalize":
            _verify_release_asset_bytes(
                release,
                bound_assets["asset_manifest"],
                download_bytes=download_bytes or _default_github_bytes,
                public=False,
            )
        status = "reused"
        details: dict[str, Any] = {
            "release_id": release["id"],
            "tag": tag,
            "draft": False,
            "asset_names": names,
        }
        if stage == "assets":
            details["asset_manifest"] = manifest
        elif stage == "finalize":
            details["asset_manifest"] = bound_assets["asset_manifest"]
        return _result(plan, "github_release", stage, status, **details)
    if release.get("draft") is not True or release.get("prerelease") is not True:
        raise ValueError("GitHub Release is neither the expected Draft nor public prerelease")
    if stage == "readback": raise ValueError("GitHub Release readback requires a public prerelease")  # noqa: E701

    if stage == "assets":
        local_assets = _validate_local_release_assets(plan, artifacts)
        manifest = _asset_manifest(local_assets)
        current_names = _release_asset_names(release)
        extras = sorted(set(current_names) - set(expected_names))
        if extras: raise ValueError(f"GitHub Release Draft contains unexpected assets: {extras}")  # noqa: E701,E501
        _verify_release_asset_bytes(
            release,
            [item for item in manifest if item["name"] in set(current_names)],
            download_bytes=download_bytes or _default_github_bytes,
            public=False,
        )
        current = set(current_names)
        for path in sorted(local_assets, key=lambda item: item.name):
            if path.name in current: continue  # noqa: E701
            encoded = urllib.parse.quote(path.name, safe="")
            uploaded = api(f"https://uploads.github.com/repos/{repository}/releases/{release['id']}/assets?name={encoded}", method="POST", body=path.read_bytes(), content_type="application/octet-stream")
            if not isinstance(uploaded, dict) or uploaded.get("name") != path.name:
                raise ValueError(f"GitHub Release upload did not return asset {path.name}")
        reread = _github_release_by_id(plan, api, release["id"])
        if not isinstance(reread, dict) or _release_asset_names(reread) != expected_names:
            raise ValueError(
                "GitHub Release Draft does not contain the exact resolved assets after upload"
            )
        _verify_release_asset_bytes(
            reread,
            manifest,
            download_bytes=download_bytes or _default_github_bytes,
            public=False,
        )
        return _result(plan, "github_release", stage, "uploaded", release_id=release["id"], tag=tag, draft=True, asset_names=expected_names, asset_manifest=manifest)

    names = _release_asset_names(release)
    if names != expected_names:
        raise ValueError(
            "GitHub Release Draft must contain the exact resolved assets before finalize"
        )
    _verify_release_asset_bytes(
        release,
        bound_assets["asset_manifest"],
        download_bytes=download_bytes or _default_github_bytes,
        public=False,
    )
    finalized = api(f"repos/{repository}/releases/{release['id']}", method="PATCH", body={"draft": False, "prerelease": True})
    if not isinstance(finalized, dict): raise ValueError("GitHub Release finalize returned no release")  # noqa: E701
    _validate_public_release(plan, finalized)
    return _result(plan, "github_release", stage, "finalized", release_id=release["id"], tag=tag, draft=False, asset_names=names, asset_manifest=bound_assets["asset_manifest"])


# fmt: on
