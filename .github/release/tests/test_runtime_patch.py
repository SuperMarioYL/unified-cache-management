from __future__ import annotations

import importlib.util
import builtins
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".github/release"))
from ucm_release import core as release_core  # noqa: E402


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


def test_patch_disabled_module_import_does_not_require_packaging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-release imports stay usable when optional patch routing is disabled."""
    ucm_package = types.ModuleType("ucm")
    ucm_package.__path__ = [str(ROOT / "ucm")]
    logger_module = types.ModuleType("ucm.logger")
    logger_module.init_logger = lambda _name: _Logger()
    monkeypatch.setitem(sys.modules, "ucm", ucm_package)
    monkeypatch.setitem(sys.modules, "ucm.logger", logger_module)
    monkeypatch.delenv("ENABLE_UCM_PATCH", raising=False)
    original_import = builtins.__import__

    def block_packaging(name: str, *args: object, **kwargs: object):
        if name == "packaging" or name.startswith("packaging."):
            raise ImportError("packaging intentionally unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_packaging)
    spec = importlib.util.spec_from_file_location(
        "task4_patch_disabled_without_packaging",
        ROOT / "ucm/integration/vllm/patch/apply_patch.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert module.ENABLE_UCM_PATCH is False


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


def test_apply_uses_installed_distribution_versions_and_declared_import_order(
    patch_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(
        _rule(
            "ordered",
            10,
            "vllm",
            ">=0.21,<0.22",
            channel="stable",
            variant="default",
            imports=[
                {"module": "ucm.integration.vllm.patch.first"},
                {
                    "module": "ucm.integration.vllm.patch.sparse",
                    "when": {"sparse": True},
                },
                {"module": "ucm.integration.vllm.patch.last"},
            ],
        )
    )
    imported: list[str] = []

    def distribution_version(distribution: str) -> str:
        if distribution == "vllm":
            return "0.21.4+vendor"
        raise patch_module.metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(patch_module, "ENABLE_UCM_PATCH", True)
    monkeypatch.setattr(patch_module, "ENABLE_SPARSE", True)
    monkeypatch.setenv("UCM_RUNTIME_PATCH_VARIANTS", '{"vllm":"default"}')
    monkeypatch.setattr(patch_module, "_load_runtime_patch_manifest", lambda: manifest)
    monkeypatch.setattr(patch_module.metadata, "version", distribution_version)
    monkeypatch.setattr(
        patch_module.importlib,
        "import_module",
        lambda name: imported.append(name) or types.ModuleType(name),
    )

    patch_module.apply_all_patches()

    assert imported == [
        "ucm.integration.vllm.patch.logger_patch",
        "ucm.integration.vllm.patch.first",
        "ucm.integration.vllm.patch.sparse",
        "ucm.integration.vllm.patch.last",
    ]


@pytest.mark.parametrize("ascend_variant", ["a2", "a3"])
def test_apply_all_patches_selects_a_product_specific_variant_for_each_distribution(
    patch_module, monkeypatch: pytest.MonkeyPatch, ascend_variant: str
) -> None:
    """An Ascend image must not pass its A2/A3 variant to base vLLM."""
    manifest = _manifest(
        _rule(
            "vllm-default",
            10,
            "vllm",
            ">=0.21,<0.22",
            channel="stable",
            variant="default",
            imports=[{"module": "ucm.integration.vllm.patch.base"}],
        ),
        _rule(
            f"ascend-{ascend_variant}",
            20,
            "vllm-ascend",
            ">=0.22.1rc1,<0.23",
            channel="rc",
            variant=ascend_variant,
            imports=[{"module": "ucm.integration.vllm.patch.ascend"}],
        ),
    )
    versions = {"vllm": "0.21.4", "vllm-ascend": "0.22.1rc1"}
    imported: list[str] = []
    monkeypatch.setattr(patch_module, "ENABLE_UCM_PATCH", True)
    monkeypatch.delenv("UCM_RUNTIME_VARIANT", raising=False)
    monkeypatch.setenv(
        "UCM_RUNTIME_PATCH_VARIANTS",
        f'{{"vllm":"default","vllm-ascend":"{ascend_variant}"}}',
    )
    monkeypatch.setattr(patch_module, "_load_runtime_patch_manifest", lambda: manifest)
    monkeypatch.setattr(
        patch_module.metadata, "version", lambda distribution: versions[distribution]
    )
    monkeypatch.setattr(
        patch_module.importlib,
        "import_module",
        lambda name: imported.append(name) or types.ModuleType(name),
    )

    patch_module.apply_all_patches()

    assert imported == [
        "ucm.integration.vllm.patch.logger_patch",
        "ucm.integration.vllm.patch.base",
        "ucm.integration.vllm.patch.ascend",
    ]


@pytest.mark.parametrize(
    "runtime_patch_variants",
    [
        '{"vllm-ascend":"a2"}',
        '{"foreign":"a2","vllm":"a2","vllm-ascend":"a2"}',
    ],
)
def test_apply_all_patches_rejects_missing_or_foreign_product_variant_maps(
    patch_module,
    monkeypatch: pytest.MonkeyPatch,
    runtime_patch_variants: str,
) -> None:
    """The enabled runtime map must name exactly the installed products."""
    manifest = _manifest(
        _rule(
            "vllm-a2",
            10,
            "vllm",
            ">=0.21,<0.22",
            channel="stable",
            variant="a2",
            imports=[{"module": "ucm.integration.vllm.patch.base"}],
        ),
        _rule(
            "ascend-a2",
            20,
            "vllm-ascend",
            ">=0.22.1rc1,<0.23",
            channel="rc",
            variant="a2",
            imports=[{"module": "ucm.integration.vllm.patch.ascend"}],
        ),
    )
    versions = {"vllm": "0.21.4", "vllm-ascend": "0.22.1rc1"}
    monkeypatch.setattr(patch_module, "ENABLE_UCM_PATCH", True)
    monkeypatch.setenv("UCM_RUNTIME_PATCH_VARIANTS", runtime_patch_variants)
    monkeypatch.setattr(patch_module, "_load_runtime_patch_manifest", lambda: manifest)
    monkeypatch.setattr(
        patch_module.metadata, "version", lambda distribution: versions[distribution]
    )
    monkeypatch.setattr(
        patch_module.importlib, "import_module", lambda name: types.ModuleType(name)
    )

    with pytest.raises(ValueError, match="runtime patch variant map"):
        patch_module.apply_all_patches()


def test_explicit_none_is_the_only_noop_for_an_installed_unknown_version(
    patch_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    none_rule = _rule(
        "future_none",
        90,
        "vllm",
        ">=9,<10",
        channel="stable",
        variant="default",
        strategy="none",
        imports=[],
    )
    imported: list[str] = []
    monkeypatch.setattr(patch_module, "ENABLE_UCM_PATCH", True)
    monkeypatch.setenv("UCM_RUNTIME_PATCH_VARIANTS", '{"vllm":"default"}')
    monkeypatch.setattr(
        patch_module, "_load_runtime_patch_manifest", lambda: _manifest(none_rule)
    )
    monkeypatch.setattr(
        patch_module.metadata,
        "version",
        lambda distribution: (
            "9.1"
            if distribution == "vllm"
            else (_ for _ in ()).throw(
                patch_module.metadata.PackageNotFoundError(distribution)
            )
        ),
    )
    monkeypatch.setattr(
        patch_module.importlib,
        "import_module",
        lambda name: imported.append(name) or types.ModuleType(name),
    )

    patch_module.apply_all_patches()

    assert imported == ["ucm.integration.vllm.patch.logger_patch"]


@pytest.mark.parametrize(
    "product, version, channel_variant, strategy, modules",
    [
        (
            "vllm",
            "0.11.0",
            "default",
            "imports",
            [
                "ucm.integration.vllm.patch.v0110.vllm.pc_patch",
                "ucm.integration.vllm.patch.v0110.vllm.sparse_patch",
            ],
        ),
        ("vllm", "0.17.0", "default", "none", []),
        (
            "vllm",
            "0.18.0.post1",
            "default",
            "imports",
            [
                "ucm.integration.vllm.patch.v0180.vllm.pc_patch",
                "ucm.integration.vllm.patch.load_failure_patch",
            ],
        ),
        (
            "vllm",
            "0.19.1",
            "default",
            "imports",
            [
                "ucm.integration.vllm.patch.v0191.vllm.pc_patch",
                "ucm.integration.vllm.patch.load_failure_patch",
            ],
        ),
        (
            "vllm",
            "0.20.2",
            "default",
            "imports",
            ["ucm.integration.vllm.patch.load_failure_patch"],
        ),
        (
            "vllm",
            "0.21.9+vendor",
            "default",
            "imports",
            ["ucm.integration.vllm.patch.load_failure_patch"],
        ),
        (
            "vllm",
            "0.22.1",
            "default",
            "imports",
            ["ucm.integration.vllm.patch.load_failure_patch"],
        ),
        (
            "vllm",
            "0.23.0",
            "default",
            "imports",
            ["ucm.integration.vllm.patch.load_failure_patch"],
        ),
        (
            "vllm-ascend",
            "0.11.0",
            "a2",
            "imports",
            [
                "ucm.integration.vllm.patch.v0110.vllm_ascend.pc_ascend_patch",
                "ucm.integration.vllm.patch.v0110.vllm_ascend.sparse_ascend_patch",
            ],
        ),
        (
            "vllm-ascend",
            "0.17.0",
            "a2",
            "imports",
            ["ucm.integration.vllm.patch.v0180.vllm_ascend.ucm_connector_patch"],
        ),
        (
            "vllm-ascend",
            "0.18.0.post1",
            "a2",
            "imports",
            [
                "ucm.integration.vllm.patch.ucm_connector_registration_patch",
                "ucm.integration.vllm.patch.v0180.vllm_ascend.pc_ascend_patch",
            ],
        ),
        (
            "vllm-ascend",
            "0.19.1",
            "a2",
            "imports",
            [
                "ucm.integration.vllm.patch.ucm_connector_registration_patch",
                "ucm.integration.vllm.patch.v0191.vllm_ascend.cpu_binding_patch",
                "ucm.integration.vllm.patch.v0191.vllm_ascend.pc_ascend_patch",
            ],
        ),
        (
            "vllm-ascend",
            "0.20.2",
            "a2",
            "imports",
            [
                "ucm.integration.vllm.patch.ucm_connector_registration_patch",
                "ucm.integration.vllm.patch.v0202.vllm_ascend.ascend_hybrid_cache_patch",
                "ucm.integration.vllm.patch.v0202.vllm_ascend.cpu_binding_patch",
            ],
        ),
        (
            "vllm-ascend",
            "0.21.0",
            "a2",
            "imports",
            [
                "ucm.integration.vllm.patch.v0210.vllm_ascend.ascend_hybrid_cache_patch",
                "ucm.integration.vllm.patch.v0210.vllm_ascend.cpu_binding_patch",
            ],
        ),
        (
            "vllm-ascend",
            "0.22.1rc3+vendor",
            "a3",
            "imports",
            [
                "ucm.integration.vllm.patch.ucm_connector_registration_patch",
                "ucm.integration.vllm.patch.v0221.vllm_ascend.ascend_hybrid_cache_patch",
                "ucm.integration.vllm.patch.v0221.vllm_ascend.cpu_binding_patch",
            ],
        ),
        (
            "vllm-ascend",
            "0.23.0",
            "a3",
            "imports",
            [
                "ucm.integration.vllm.patch.ucm_connector_registration_patch",
                "ucm.integration.vllm.patch.v0230.vllm_ascend.ascend_hybrid_cache_patch",
                "ucm.integration.vllm.patch.v0230.vllm_ascend.cpu_binding_patch",
                "ucm.integration.vllm.patch.v0230.vllm_ascend.sfa_kv_transfer_patch",
            ],
        ),
    ],
)
def test_release_manifest_preserves_declared_runtime_patch_routes(
    patch_module,
    product: str,
    version: str,
    channel_variant: str,
    strategy: str,
    modules: list[str],
) -> None:
    manifest = release_core.runtime_patch_manifest(release_core.load_catalog())

    rule = patch_module.select_runtime_patch_rule(
        manifest, product, version, variant=channel_variant
    )

    assert rule["strategy"] == strategy
    assert [item["module"] for item in rule["imports"]] == modules
    for declaration in rule["imports"]:
        if "sparse" in declaration["module"]:
            assert declaration["when"] == {"sparse": True}
        else:
            assert "when" not in declaration
