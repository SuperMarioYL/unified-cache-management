"""Runtime patch rule selection basics.

The suite covers PEP 440 range/rc/post/local matching, fail-closed selection,
the required-variant contract, and the concrete routes that preserve upstream
runtime patches when the manifest-driven dispatcher replaces direct imports.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".github/release"))

ALIAS = "ucm.integration.vllm.patch.ucm_connector_registration_patch"
BIND_MEMORY = "ucm.integration.vllm.patch.bind_memory_patch"
KIMI_K3 = (
    "ucm.integration.vllm.patch.v0270.vllm.models.kimi_k3.nvidia."
    "kimi_k3_mla_kv_hook_patch"
)
LOAD_FAILURE = "ucm.integration.vllm.patch.load_failure_patch"
MAMBA_COPY_ORDER = "ucm.integration.vllm.patch.v0210.vllm_ascend.mamba_copy_order_patch"


class _Logger:
    def info(self, *_args: object, **_kwargs: object) -> None:
        pass

    def warning(self, *_args: object, **_kwargs: object) -> None:
        pass

    def error(self, *_args: object, **_kwargs: object) -> None:
        pass


@pytest.fixture
def patch_module(monkeypatch: pytest.MonkeyPatch):
    ucm_package = types.ModuleType("ucm")
    ucm_package.__path__ = [str(ROOT / "ucm")]
    logger_module = types.ModuleType("ucm.logger")
    logger_module.init_logger = lambda _name: _Logger()
    monkeypatch.setitem(sys.modules, "ucm", ucm_package)
    monkeypatch.setitem(sys.modules, "ucm.logger", logger_module)
    name = "task4_runtime_patch_under_test"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "ucm/integration/vllm/patch/apply_patch.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _manifest(*rules: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "ucm-runtime-patch-rules",
        "rules": list(rules),
    }


def _source_manifest() -> dict[str, object]:
    source = yaml.safe_load(
        (ROOT / "ucm/integration/runtime-patches.yaml").read_text(encoding="utf-8")
    )
    return _manifest(*source["runtime_patch_rules"])


def _modules(rule: dict[str, object]) -> list[str]:
    return [declaration["module"] for declaration in rule["imports"]]


def _rule(
    rule_id: str,
    order: int,
    product: str,
    version_specifier: str,
    *,
    channel: str,
    variant: str,
    strategy: str = "imports",
    imports: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": rule_id,
        "order": order,
        "product": product,
        "version_specifier": version_specifier,
        "channels": [channel],
        "variants": [variant],
        "strategy": strategy,
        "imports": (
            imports
            if imports is not None
            else [{"module": f"ucm.integration.vllm.patch.{rule_id}"}]
        ),
    }


def test_pep440_selection_handles_products_ranges_rc_post_and_local(
    patch_module,
) -> None:
    manifest = _manifest(
        _rule(
            "vllm_range",
            10,
            "vllm",
            ">=0.21,<0.22",
            channel="stable",
            variant="default",
        ),
        _rule(
            "ascend_rc",
            20,
            "vllm-ascend",
            ">=0.22.1rc1,<0.23",
            channel="rc",
            variant="a2",
        ),
    )

    assert (
        patch_module.select_runtime_patch_rule(
            manifest, "vllm", "0.21.7.post1+vendor", variant="default"
        )["id"]
        == "vllm_range"
    )
    assert (
        patch_module.select_runtime_patch_rule(
            manifest, "vllm-ascend", "0.22.1rc2+vendor", variant="a2"
        )["id"]
        == "ascend_rc"
    )


def test_runtime_manifest_fails_on_zero_overlap_and_malformed_rules(
    patch_module,
) -> None:
    base = _rule(
        "base",
        10,
        "vllm",
        ">=0.21,<0.22",
        channel="stable",
        variant="default",
    )
    with pytest.raises(ValueError, match="no runtime patch rule"):
        patch_module.select_runtime_patch_rule(
            _manifest(base), "vllm", "0.23.0", variant="default"
        )

    overlap = dict(base)
    overlap.update({"id": "overlap", "order": 20})
    with pytest.raises(ValueError, match="overlapping runtime patch rules"):
        patch_module.select_runtime_patch_rule(
            _manifest(base, overlap), "vllm", "0.21.4", variant="default"
        )

    malformed = dict(base)
    malformed["imports"] = [{"module": "os", "python": "raise SystemExit()"}]
    with pytest.raises(ValueError, match="malformed runtime patch manifest"):
        patch_module.select_runtime_patch_rule(
            _manifest(malformed), "vllm", "0.21.4", variant="default"
        )


def test_runtime_variant_is_required_and_selects_one_split_rule(patch_module) -> None:
    """A missing image variant cannot silently select a multi-variant strategy."""
    manifest = _manifest(
        _rule(
            "ascend_a2",
            10,
            "vllm-ascend",
            ">=0.22.1rc1,<0.23",
            channel="rc",
            variant="a2",
        ),
        _rule(
            "ascend_a3",
            20,
            "vllm-ascend",
            ">=0.22.1rc1,<0.23",
            channel="rc",
            variant="a3",
        ),
    )

    with pytest.raises(ValueError, match="runtime variant"):
        patch_module.select_runtime_patch_rule(manifest, "vllm-ascend", "0.22.1rc1")
    assert (
        patch_module.select_runtime_patch_rule(
            manifest, "vllm-ascend", "0.22.1rc1", variant="a3"
        )["id"]
        == "ascend_a3"
    )


def test_source_manifest_preserves_latest_upstream_patch_routes(patch_module) -> None:
    manifest = _source_manifest()
    vllm_routes = {
        "0.24.0": [LOAD_FAILURE],
        "0.25.1": [LOAD_FAILURE],
        "0.26.0": [LOAD_FAILURE],
        "0.27.0": [LOAD_FAILURE, KIMI_K3],
        "0.28.0": [LOAD_FAILURE, KIMI_K3],
    }
    for version, expected in vllm_routes.items():
        rule = patch_module.select_runtime_patch_rule(
            manifest, "vllm", version, variant="default"
        )
        assert _modules(rule) == expected

    ascend_routes = {
        "0.24.0": [
            ALIAS,
            BIND_MEMORY,
            "ucm.integration.vllm.patch.v0240.vllm_ascend.ascend_hybrid_cache_patch",
            "ucm.integration.vllm.patch.v0240.vllm_ascend.cpu_binding_patch",
            MAMBA_COPY_ORDER,
        ],
        "0.25.1": [
            ALIAS,
            BIND_MEMORY,
            "ucm.integration.vllm.patch.v0251.vllm_ascend.ascend_hybrid_cache_patch",
            "ucm.integration.vllm.patch.v0251.vllm_ascend.cpu_binding_patch",
            MAMBA_COPY_ORDER,
        ],
        "0.26.0": [
            ALIAS,
            BIND_MEMORY,
            "ucm.integration.vllm.patch.v0260.vllm_ascend.cpu_binding_patch",
            MAMBA_COPY_ORDER,
        ],
    }
    for (version, variant), expected in zip(
        (("0.24.0", "a2"), ("0.25.1", "a3"), ("0.26.0", "a2")),
        ascend_routes.values(),
        strict=True,
    ):
        rule = patch_module.select_runtime_patch_rule(
            manifest, "vllm-ascend", version, variant=variant
        )
        assert _modules(rule) == expected

    for rule in manifest["rules"]:
        if rule["product"] != "vllm-ascend":
            continue
        modules = _modules(rule)
        assert BIND_MEMORY in modules
        cpu_bindings = [
            index
            for index, module in enumerate(modules)
            if module.endswith("cpu_binding_patch")
        ]
        assert not cpu_bindings or modules.index(BIND_MEMORY) < min(cpu_bindings)

    for rule_id in (
        "vllm-ascend-0210",
        "vllm-ascend-0221",
        "vllm-ascend-0230",
    ):
        rule = next(rule for rule in manifest["rules"] if rule["id"] == rule_id)
        assert _modules(rule)[-1] == MAMBA_COPY_ORDER
