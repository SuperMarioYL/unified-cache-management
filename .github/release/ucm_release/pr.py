"""Aggregate probed PR runtimes into the shared selection and Builder catalog."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from . import builders, runtime, upstream


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected a mapping")
    return value


def _source_repository(value: str, configured: str) -> str:
    normalized = value.strip().removesuffix(".git")
    match = re.fullmatch(r"https?://github\.com/([^/]+/[^/]+)", normalized)
    if match is not None:
        normalized = match.group(1)
    if not normalized:
        return configured
    if normalized != configured:
        raise ValueError(
            f"OCI source {normalized!r} differs from configured source {configured!r}"
        )
    return normalized


def _build_from_registry_record(record: Mapping[str, object]) -> dict[str, object]:
    excluded = {"target_repository", "target_tag", "created", "checked", "checks"}
    result = {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key not in excluded
    }
    result["sync_mode"] = "registry-only"
    return result


def _runtime_variant(probe: Mapping[str, object]) -> tuple[str, str]:
    backend = str(probe["backend"])
    if backend == "cuda":
        version = str(probe["accelerator_runtime"]).removeprefix("cuda-")
        return f"cu{version.replace('.', '')}", "default"
    if backend.startswith("cann-"):
        return backend.removeprefix("cann-"), backend.removeprefix("cann-")
    raise ValueError(f"unsupported probed backend {backend!r}")


def _failure(
    *, stage: str, reason: str, detail: str, probe: Mapping[str, object]
) -> dict[str, object]:
    return {
        "stage": stage,
        "reason": reason,
        "detail": detail,
        "probe_id": probe["probe_id"],
        "request_id": probe["request_id"],
        "runtime_ref": probe["runtime_ref"],
    }


def resolve_pr_request(
    formal_policy: Mapping[str, object],
    runtime_probe: Mapping[str, object],
    registry_inventory: Mapping[str, object],
    *,
    pr_number: int | str,
    author: str,
    run_id: int | str,
    exact_build_resolver: (
        Callable[[str, str, Sequence[Mapping[str, object]]], list[dict[str, object]]]
        | None
    ) = None,
) -> dict[str, object]:
    """Prefer exact source recipes, otherwise match checked Registry Builders."""
    probe_document = _mapping(runtime_probe, "runtime probe")
    if (
        probe_document.get("kind") != "ucm-runtime-probe"
        or probe_document.get("schema_version") != 1
    ):
        raise ValueError("runtime probe has an unsupported contract")
    probes = probe_document.get("probes")
    if not isinstance(probes, list) or not probes:
        raise ValueError("runtime probe must contain probes")
    inventory = _mapping(registry_inventory, "Builder Registry inventory")
    if inventory.get("kind") != "ucm-builder-registry":
        raise ValueError("Builder Registry inventory has an unsupported contract")
    records = inventory.get("builders")
    if not isinstance(records, list):
        raise ValueError("Builder Registry inventory builders must be a list")

    exact_groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    fallback_probes: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    backend_policies = _mapping(
        formal_policy.get("backends"), "formal backend policies"
    )
    for raw_probe in probes:
        probe = _mapping(raw_probe, "runtime probe item")
        backend = str(probe["backend"])
        backend_policy = _mapping(
            backend_policies.get(backend), f"formal backend policy {backend}"
        )
        if backend_policy.get("status") == "blocked":
            failures.append(
                _failure(
                    stage="platform-policy",
                    reason="blocked-backend",
                    detail=str(backend_policy.get("reason", "backend is blocked")),
                    probe=probe,
                )
            )
            continue
        revision = str(probe.get("oci_revision", ""))
        if not revision:
            fallback_probes.append(probe)
            continue
        configured = str(probe.get("configured_source_repository", ""))
        try:
            _source_repository(str(probe.get("oci_source", "")), configured)
        except ValueError as error:
            failures.append(
                _failure(
                    stage="exact-source",
                    reason="source-repository-mismatch",
                    detail=str(error),
                    probe=probe,
                )
            )
            continue
        exact_groups.setdefault((str(probe["product_id"]), revision), []).append(probe)

    exact_builds: dict[str, dict[str, object]] = {}
    exact_matches: list[dict[str, object]] = []
    for (product_id, revision), group in exact_groups.items():
        try:
            resolved = (
                exact_build_resolver(product_id, revision, group)
                if exact_build_resolver is not None
                else upstream.resolve_revision_wheel_builds(
                    formal_policy,
                    product_id=product_id,
                    source_ref=revision,
                    probes=group,
                )
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            failures.extend(
                _failure(
                    stage="exact-source",
                    reason="unresolvable-exact-recipe",
                    detail=str(error),
                    probe=probe,
                )
                for probe in group
            )
            continue
        by_capability = {
            (
                str(item["backend"]),
                str(item["accelerator_runtime"]),
                str(item["soc_version"]),
                str(item["python_abi"]),
                str(item["cpu_arch"]),
            ): item
            for item in resolved
        }
        for probe in group:
            key = (
                str(probe["backend"]),
                str(probe["accelerator_runtime"]),
                str(probe["soc_version"]),
                str(probe["python_abi"]),
                str(probe["cpu_arch"]),
            )
            build = by_capability.get(key)
            if build is None:
                failures.append(
                    _failure(
                        stage="exact-source",
                        reason="missing-exact-capability",
                        detail=f"exact source has no Wheel build for {key}",
                        probe=probe,
                    )
                )
                continue
            existing = exact_builds.get(str(build["id"]))
            selected_build = existing or build
            exact_builds[str(build["id"])] = selected_build
            exact_matches.append(
                {
                    "probe_id": probe["probe_id"],
                    "request_id": probe["request_id"],
                    "runtime_ref": probe["runtime_ref"],
                    "cpu_arch": probe["cpu_arch"],
                    "wheel_id": selected_build["id"],
                    "capability": {
                        key: probe[key]
                        for key in (
                            "backend",
                            "accelerator_runtime",
                            "soc_version",
                            "python_version",
                            "python_abi",
                            "cpu_arch",
                        )
                    },
                    "builder": {"id": selected_build["id"]},
                }
            )

    fallback_document = {
        "kind": "ucm-runtime-probe",
        "schema_version": 1,
        "probes": fallback_probes,
    }
    fallback_matches = (
        runtime.match_runtime_builders(fallback_document, records)
        if fallback_probes
        else {
            "kind": "ucm-runtime-builder-matches",
            "schema_version": 1,
            "ok": True,
            "matches": [],
            "problems": [],
        }
    )
    failures.extend(copy.deepcopy(fallback_matches["problems"]))
    all_matches = exact_matches + copy.deepcopy(fallback_matches["matches"])
    match_document = {
        "kind": "ucm-runtime-builder-matches",
        "schema_version": 1,
        "ok": not failures,
        "matches": all_matches,
        "problems": failures,
    }
    if failures:
        return {
            "kind": "ucm-pr-resolution",
            "schema_version": 1,
            "ok": False,
            "builder_matches": match_document,
            "problems": failures,
        }

    publication = runtime.project_pr_publication(
        probe_document,
        match_document,
        pr_number=pr_number,
        author=author,
        run_id=run_id,
    )
    fallback_records = {
        str(match["wheel_id"]): _build_from_registry_record(match["builder_record"])
        for match in fallback_matches["matches"]
    }
    wheel_builds = {**fallback_records, **exact_builds}
    match_by_probe = {str(item["probe_id"]): item for item in all_matches}
    family_by_request = {str(item["id"]): item for item in publication["families"]}
    grouped: dict[str, list[dict[str, object]]] = {}
    for probe in probes:
        grouped.setdefault(str(probe["request_id"]), []).append(probe)
    runtimes: list[dict[str, object]] = []
    for request_id, group in grouped.items():
        first = group[0]
        family = family_by_request[request_id]
        source_refs = {
            str(item.get("oci_revision", ""))
            for item in group
            if str(item.get("oci_revision", ""))
        }
        if len(source_refs) > 1:
            raise ValueError(
                f"{request_id}: runtime members have different source refs"
            )
        source_ref = next(iter(source_refs)) if source_refs else "registry-capability"
        source_repository = _source_repository(
            str(first.get("oci_source", "")),
            str(first.get("configured_source_repository", "")),
        )
        runtime_variant, variant = _runtime_variant(first)
        architectures = [str(item["cpu_arch"]) for item in group]
        member_references = {
            str(item["cpu_arch"]): str(item["image_reference"]) for item in group
        }
        wheel_build_ids = {
            str(item["cpu_arch"]): str(
                match_by_probe[str(item["probe_id"])]["wheel_id"]
            )
            for item in group
        }
        runtimes.append(
            {
                "id": request_id,
                "product_id": first["product_id"],
                "source_repository": source_repository,
                "source_ref": source_ref,
                "runtime_repository": first["repository"],
                "runtime_tag": first["tag"],
                "runtime_variant": runtime_variant,
                "backend": first["backend"],
                "accelerator_runtime": first["accelerator_runtime"],
                "variant": variant,
                "soc_version": first["soc_version"],
                "python_version": first["python_version"],
                "python_abi": first["python_abi"],
                "os_id": first["os_id"],
                "os_version": first["os_version"],
                "architectures": architectures,
                "member_references": member_references,
                "wheel_build_ids": wheel_build_ids,
                "version": str(first["tag"]),
                "channel": "pinned",
                "target_repository": family["target_repository"],
                "target_tag": family["target_tag"],
            }
        )
    selection = upstream.validate_selection(
        {
            "kind": upstream.SELECTION_KIND,
            "schema_version": upstream.SELECTION_SCHEMA_VERSION,
            "wheel_builds": sorted(
                wheel_builds.values(), key=lambda item: str(item["id"])
            ),
            "runtimes": sorted(runtimes, key=lambda item: str(item["id"])),
            "problems": [],
        }
    )
    builder_catalog = builders.catalog_from_selection(
        selection,
        owner=str(formal_policy.get("repository", "")).split("/", 1)[0] or None,
        formal_policy=formal_policy,
    )
    catalog_by_id = {str(item["id"]): item for item in builder_catalog["builders"]}
    for match in fallback_matches["matches"]:
        selected = catalog_by_id[str(match["wheel_id"])]
        record = match["builder_record"]
        if (
            selected["target_repository"] != record["target_repository"]
            or selected["target_tag"] != record["target_tag"]
        ):
            raise ValueError("matched Registry Builder coordinate is not reproducible")
    return {
        "kind": "ucm-pr-resolution",
        "schema_version": 1,
        "ok": True,
        "selection": selection,
        "builder_catalog": builder_catalog,
        "builder_matches": match_document,
        "publication": publication,
        "problems": [],
    }
