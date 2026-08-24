#
# MIT License
#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
"""
Monkey patching module for vLLM to apply UCM patches automatically.
This replaces the need for manual `git apply` commands.
"""

import importlib
import json
import os
import re
from importlib import metadata, resources
from typing import Any, Optional

from ucm.logger import init_logger

logger = init_logger(__name__)

import os  # noqa: E402 - preserve the dispatcher import order

ENABLE_SPARSE = os.getenv("ENABLE_SPARSE", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
ENABLE_UCM_PATCH = os.environ.get("ENABLE_UCM_PATCH", "").lower() in ("1", "true")


def get_vllm_ascend_version_full() -> Optional[str]:
    """Detect the installed vLLM-Ascend distribution version."""
    try:
        return metadata.version("vllm-ascend")
    except metadata.PackageNotFoundError:
        return None


def get_vllm_ascend_version() -> Optional[str]:
    """Backward-compatible alias preserving the full PEP 440 version."""
    return get_vllm_ascend_version_full()


def get_vllm_version() -> Optional[str]:
    """Detect the installed vLLM distribution version."""
    try:
        return metadata.version("vllm")
    except metadata.PackageNotFoundError:
        return None


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _validate_runtime_patch_manifest(manifest: object) -> dict[str, Any]:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet

    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "kind", "rules"}
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != "ucm-runtime-patch-rules"
        or not isinstance(manifest.get("rules"), list)
        or not manifest["rules"]
    ):
        raise ValueError("malformed runtime patch manifest")
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    previous_order = -1
    for rule in manifest["rules"]:
        fields = {
            "id",
            "order",
            "product",
            "version_specifier",
            "channels",
            "variants",
            "strategy",
            "imports",
        }
        if not isinstance(rule, dict) or set(rule) != fields:
            raise ValueError("malformed runtime patch manifest rule")
        identifier = rule["id"]
        order = rule["order"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in seen_ids
            or not isinstance(order, int)
            or isinstance(order, bool)
            or order < 0
            or order in seen_orders
            or order < previous_order
            or rule["product"] not in {"vllm", "vllm-ascend"}
        ):
            raise ValueError("malformed runtime patch manifest identity")
        seen_ids.add(identifier)
        seen_orders.add(order)
        previous_order = order
        try:
            SpecifierSet(str(rule["version_specifier"]))
        except InvalidSpecifier as error:
            raise ValueError("malformed runtime patch manifest specifier") from error
        if any(
            not isinstance(values, list)
            or not values
            or len(values) != len(set(values))
            for values in (rule["channels"], rule["variants"])
        ) or not set(rule["channels"]).issubset({"stable", "rc"}):
            raise ValueError("malformed runtime patch manifest applicability")
        imports = rule["imports"]
        if (
            rule["strategy"] not in {"imports", "none"}
            or not isinstance(imports, list)
            or (rule["strategy"] == "none") != (imports == [])
        ):
            raise ValueError("malformed runtime patch manifest strategy")
        for declaration in imports:
            if not isinstance(declaration, dict) or set(declaration) not in (
                {"module"},
                {"module", "when"},
            ):
                raise ValueError("malformed runtime patch manifest import")
            module = declaration["module"]
            if (
                not isinstance(module, str)
                or re.fullmatch(r"ucm(?:\.[A-Za-z_][A-Za-z0-9_]*)+", module) is None
            ):
                raise ValueError("malformed runtime patch manifest module")
            if "when" in declaration and (
                not isinstance(declaration["when"], dict)
                or set(declaration["when"]) != {"sparse"}
                or not isinstance(declaration["when"]["sparse"], bool)
            ):
                raise ValueError("malformed runtime patch manifest condition")
    return manifest


def _load_runtime_patch_manifest() -> dict[str, Any]:
    try:
        raw = (
            resources.files(__package__)
            .joinpath("runtime_patch_rules.json")
            .read_bytes()
        )
        manifest = json.loads(raw)
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("runtime patch manifest is missing or invalid") from error
    validated = _validate_runtime_patch_manifest(manifest)
    if raw != _canonical_bytes(validated) + b"\n":
        raise ValueError("runtime patch manifest is noncanonical")
    return validated


def _runtime_channel(version: Any) -> str:
    return "rc" if version.pre is not None and version.pre[0] == "rc" else "stable"


def select_runtime_patch_rule(
    manifest: object,
    product: str,
    version: str,
    *,
    variant: str | None = None,
) -> dict[str, Any]:
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    validated = _validate_runtime_patch_manifest(manifest)
    if not isinstance(variant, str) or not variant:
        raise ValueError(f"runtime variant is required for {product}")
    try:
        parsed = Version(version)
    except InvalidVersion as error:
        raise ValueError(
            f"installed {product} version is not valid PEP 440: {version}"
        ) from error
    channel = _runtime_channel(parsed)
    matches = [
        rule
        for rule in validated["rules"]
        if rule["product"] == product
        and channel in rule["channels"]
        and variant in rule["variants"]
        and SpecifierSet(rule["version_specifier"]).contains(parsed, prereleases=True)
    ]
    if not matches:
        raise ValueError(
            f"no runtime patch rule for {product} {version} channel={channel} "
            f"variant={variant}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"overlapping runtime patch rules for {product} {version}: "
            + ", ".join(rule["id"] for rule in matches)
        )
    return matches[0]


def _apply_rule(rule: dict[str, Any]) -> None:
    if rule["strategy"] == "none":
        return
    for declaration in rule["imports"]:
        condition = declaration.get("when")
        if condition is not None and condition["sparse"] != ENABLE_SPARSE:
            continue
        logger.info("UCM importing runtime patch adapter %s", declaration["module"])
        importlib.import_module(declaration["module"])


def _runtime_patch_variants(installed_products: set[str]) -> dict[str, str]:
    raw = os.getenv("UCM_RUNTIME_PATCH_VARIANTS")
    try:
        variants = json.loads(raw) if raw is not None else None
    except json.JSONDecodeError as error:
        raise ValueError("runtime patch variant map is invalid JSON") from error
    if (
        not isinstance(variants, dict)
        or set(variants) != installed_products
        or any(
            not isinstance(product, str) or not isinstance(variant, str) or not variant
            for product, variant in variants.items()
        )
    ):
        raise ValueError(
            "runtime patch variant map must name exactly the installed products"
        )
    if raw != _canonical_bytes(variants).decode("utf-8"):
        raise ValueError("runtime patch variant map must use canonical JSON")
    return variants


def apply_all_patches() -> None:
    """Apply only the ordered adapters declared by the packaged manifest."""
    try:
        from ucm.integration.vllm.patch.logger_patch import patch_logger  # noqa: F401

        if not ENABLE_UCM_PATCH:
            return
        manifest = _load_runtime_patch_manifest()
        vllm_version = get_vllm_version()
        if vllm_version is None:
            raise ValueError("Could not detect vLLM version")
        ascend_version = get_vllm_ascend_version_full()
        versions = {"vllm": vllm_version}
        if ascend_version is not None:
            versions["vllm-ascend"] = ascend_version
        variants = _runtime_patch_variants(set(versions))
        for product, version in versions.items():
            _apply_rule(
                select_runtime_patch_rule(
                    manifest,
                    product,
                    version,
                    variant=variants[product],
                )
            )

        ascend_version = get_vllm_ascend_version()
        # UCM PATCH: vllm-ascend registers UCMConnector as an alias for the
        # concrete UCMConnectorV1 class used by MultiConnector metrics.
        if ascend_version in {
            "0.18.0",
            "0.19.1",
            "0.20.2",
            "0.22.1",
            "0.23.0",
            "0.24.0",
            "0.25.1",
            "0.26.0",
        }:
            logger.info("UCM patching vllm-ascend UCM connector metrics alias...")
            import ucm.integration.vllm.patch.ucm_connector_registration_patch

        # Apply vllm/vllm-ascend version-specific patches
        # vllm patches
        match version:
            case "0.11.0":
                logger.info("UCM patching vllm for pc...")
                import ucm.integration.vllm.patch.v0110.vllm.pc_patch

                if ENABLE_SPARSE:
                    logger.info("UCM patching vllm for sparse...")
                    import ucm.integration.vllm.patch.v0110.vllm.sparse_patch
            case "0.18.0":
                logger.info("UCM patching vllm for pc...")
                import ucm.integration.vllm.patch.v0180.vllm.pc_patch
            case "0.19.1":
                logger.info("UCM patching vllm for pc...")
                import ucm.integration.vllm.patch.v0191.vllm.pc_patch
            case _:
                pass

        major, minor, *_ = version.split(".")
        if (int(major), int(minor)) >= (0, 18):
            logger.info("UCM patching vllm for load-failure recovery...")
            import ucm.integration.vllm.patch.load_failure_patch

        # vllm_ascend patches
        # Disable CpuAlloc.bind_memory BEFORE any cpu_binding_patch so that
        # bind_memory is a no-op before bind_threads replacement is installed.
        logger.info("UCM patching vllm-ascend bind_memory to no-op...")
        import ucm.integration.vllm.patch.bind_memory_patch

        match ascend_version:
            case "0.11.0":
                logger.info("UCM patching vllm-ascend for pc...")
                import ucm.integration.vllm.patch.v0110.vllm_ascend.pc_ascend_patch

                if ENABLE_SPARSE:
                    logger.info("UCM patching vllm-ascend for sparse...")
                    import ucm.integration.vllm.patch.v0110.vllm_ascend.sparse_ascend_patch
            case "0.18.0":
                logger.info("UCM patching vllm-ascend for pc...")
                import ucm.integration.vllm.patch.v0180.vllm_ascend.pc_ascend_patch
            case "0.17.0":
                logger.info(f"UCM patching vllm-ascend {ascend_version} for pc...")
                import ucm.integration.vllm.patch.v0180.vllm_ascend.ucm_connector_patch
            case "0.19.1":
                logger.info(f"UCM patching vllm-ascend {ascend_version} for pc...")
                import ucm.integration.vllm.patch.v0191.vllm_ascend.cpu_binding_patch
                import ucm.integration.vllm.patch.v0191.vllm_ascend.pc_ascend_patch
            case "0.20.2":
                logger.info(
                    "UCM patching vllm-ascend 0.20.2 for hybrid cache recovery..."
                )
                import ucm.integration.vllm.patch.v0202.vllm_ascend.ascend_hybrid_cache_patch
                import ucm.integration.vllm.patch.v0202.vllm_ascend.cpu_binding_patch
            case "0.21.0":
                logger.info(
                    "UCM patching vllm-ascend 0.21.0 for hybrid cache recovery..."
                )
                import ucm.integration.vllm.patch.v0210.vllm_ascend.ascend_hybrid_cache_patch
                import ucm.integration.vllm.patch.v0210.vllm_ascend.cpu_binding_patch
            case "0.22.1":
                logger.info(
                    "UCM patching vllm-ascend 0.22.1 for hybrid cache "
                    "recovery and CPU affinity..."
                )
                import ucm.integration.vllm.patch.v0221.vllm_ascend.ascend_hybrid_cache_patch
                import ucm.integration.vllm.patch.v0221.vllm_ascend.cpu_binding_patch
            case "0.23.0":
                logger.info(
                    "UCM patching vllm-ascend 0.23.0 for hybrid cache "
                    "recovery, CPU affinity, and SFA KV transfer..."
                )
                import ucm.integration.vllm.patch.v0230.vllm_ascend.ascend_hybrid_cache_patch
                import ucm.integration.vllm.patch.v0230.vllm_ascend.cpu_binding_patch
                import ucm.integration.vllm.patch.v0230.vllm_ascend.sfa_kv_transfer_patch
            case "0.24.0":
                logger.info(
                    "UCM patching vllm-ascend 0.24.0 for hybrid cache "
                    "recovery and CPU affinity..."
                )
                import ucm.integration.vllm.patch.v0240.vllm_ascend.ascend_hybrid_cache_patch
                import ucm.integration.vllm.patch.v0240.vllm_ascend.cpu_binding_patch
            case "0.25.1":
                logger.info(
                    "UCM patching vllm-ascend 0.25.1 for hybrid cache "
                    "recovery and CPU affinity..."
                )
                import ucm.integration.vllm.patch.v0251.vllm_ascend.ascend_hybrid_cache_patch
                import ucm.integration.vllm.patch.v0251.vllm_ascend.cpu_binding_patch
            case "0.26.0":
                logger.info("UCM patching vllm-ascend 0.26.0 for CPU affinity...")
                import ucm.integration.vllm.patch.v0260.vllm_ascend.cpu_binding_patch
            case _:
                pass

        # Fix: vllm-ascend >= 0.21.0 defers do_mamba_copy_block to after
        # start_load_kv, overwriting UCM-loaded data. @when_imported is
        # self-guarding (only fires when the module exists).
        import ucm.integration.vllm.patch.v0210.vllm_ascend.mamba_copy_order_patch

        # Fix: vLLM >= 0.27.0 Kimi-K3's MLA bypasses @maybe_transfer_kv_layer,
        # so wait_for_layer_load/save_kv_layer are never called. @when_imported
        # only fires when vllm.models.kimi_k3.nvidia.mla is imported.
        import ucm.integration.vllm.patch.v0270.vllm.models.kimi_k3.nvidia.kimi_k3_mla_kv_hook_patch  # noqa: F401,E501

        logger.info("UCM patch initialization completed!")
    except Exception as e:
        logger.error(f"Failed to apply vLLM patches: {e}\n")
        raise
