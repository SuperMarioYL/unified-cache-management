"""RED contracts for the forward-compatible Schema v3 release authority."""

from __future__ import annotations

import copy
import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
RELEASE_PATH = RELEASE_ROOT / "release.yaml"
sys.path.insert(0, str(RELEASE_ROOT))


def _load_release_yaml() -> dict[str, Any]:
    release = yaml.safe_load(RELEASE_PATH.read_text(encoding="utf-8"))
    assert isinstance(release, dict), "release.yaml must contain a YAML object"
    return release


def _write_release_yaml(tmp_path: Path, release: dict[str, Any]) -> Path:
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")
    return path


def _canonical_product_rules() -> dict[str, dict[str, str]]:
    return {
        "cuda": {
            "accelerator": "cuda",
            "distribution": "uc-manager-cuda{runtime.compact}",
        },
        "cann": {
            "accelerator": "ascend",
            "distribution": (
                "uc-manager-cann{runtime.compact}-{variant}" "-mc{mooncake.compact}"
            ),
        },
    }


def _require_public_module(name: str) -> ModuleType:
    """Turn a not-yet-created public module into an assertion-stage RED."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"required public module {name!r} is not implemented: {exc}")


def _require_public_callable(module: ModuleType, name: str) -> Callable[..., Any]:
    function = getattr(module, name, None)
    message = f"required public API {module.__name__}.{name} is missing"
    assert callable(function), message
    return function


def test_repository_release_authority_is_schema_v3_without_v2_product_lists() -> None:
    """The release.yaml v3 migration removes both fixed v2 product lists."""
    release = _load_release_yaml()
    publish = release.get("publish")
    pypi = publish.get("pypi") if isinstance(publish, dict) else None
    violations: list[str] = []

    if release.get("schema_version") != 3:
        violations.append("schema_version must be integer 3")
    if "wheel_profiles" in release:
        violations.append("wheel_profiles must not remain in Schema v3")
    if isinstance(pypi, dict) and "dists" in pypi:
        violations.append("publish.pypi.dists must not remain in Schema v3")

    assert not violations, "release authority is not Schema v3:\n- " + "\n- ".join(
        violations
    )


def test_load_catalog_accepts_repository_schema_v3_authority() -> None:
    """core.load_catalog must accept the complete repository v3 authority."""
    core = _require_public_module("ucm_release.core")

    try:
        loaded = core.load_catalog(RELEASE_PATH)
    except ValueError as exc:
        pytest.fail(f"repository Schema v3 authority was rejected: {exc}")

    assert loaded["kind"] == "release-config"
    assert loaded["schema_version"] == 3


def test_load_catalog_rejects_schema_v2_instead_of_migrating_it(
    tmp_path: Path,
) -> None:
    """core.load_catalog must reject v2 rather than add a compatibility path."""
    core = _require_public_module("ucm_release.core")
    release = _load_release_yaml()
    release["schema_version"] = 2
    path = _write_release_yaml(tmp_path, release)

    with pytest.raises(ValueError, match=r"(?i)schema(?:_version)?[^\n]*3"):
        core.load_catalog(path)


def test_load_catalog_rejects_residual_wheel_profiles_in_schema_v3(
    tmp_path: Path,
) -> None:
    """core.load_catalog must identify wheel_profiles as an illegal v2 residue."""
    core = _require_public_module("ucm_release.core")
    authority = _load_release_yaml()
    assert (
        authority.get("schema_version") == 3
    ), "repository authority must migrate to Schema v3 before residue mutation"
    release = copy.deepcopy(authority)
    release["wheel_profiles"] = [{"id": "legacy-fixed-profile"}]
    path = _write_release_yaml(tmp_path, release)

    with pytest.raises(ValueError, match=r"wheel_profiles"):
        core.load_catalog(path)


def test_load_catalog_rejects_pypi_dists_in_schema_v3(tmp_path: Path) -> None:
    """core.load_catalog must reject the explicit PyPI Distribution residue."""
    core = _require_public_module("ucm_release.core")
    authority = _load_release_yaml()
    assert (
        authority.get("schema_version") == 3
    ), "repository authority must migrate to Schema v3 before residue mutation"
    release = copy.deepcopy(authority)
    pypi = release["publish"]["pypi"]
    pypi["dists"] = ["uc-manager-cuda"]
    path = _write_release_yaml(tmp_path, release)

    with pytest.raises(ValueError, match=r"dists"):
        core.load_catalog(path)


@pytest.mark.parametrize(
    ("family", "template"),
    [
        pytest.param(
            "cuda",
            "uc-manager-cuda{runtime.compact}",
            id="cuda",
        ),
        pytest.param(
            "cann",
            "uc-manager-cann{runtime.compact}-{variant}-mc{mooncake.compact}",
            id="cann",
        ),
    ],
)
def test_compile_distribution_template_accepts_canonical_templates(
    family: str,
    template: str,
) -> None:
    """products.compile_distribution_template accepts both approved forms."""
    products = _require_public_module("ucm_release.products")
    compile_template = _require_public_callable(
        products, "compile_distribution_template"
    )

    assert compile_template(family, template) is not None


@pytest.mark.parametrize(
    ("family", "template"),
    [
        pytest.param(
            "cuda",
            "uc-manager-cuda{runtime.compact",
            id="unclosed-placeholder",
        ),
        pytest.param(
            "cuda",
            "uc-manager-cuda{runtime.compact}-{runtime.major}",
            id="unknown-variable",
        ),
        pytest.param(
            "cuda",
            "uc-manager-cuda",
            id="cuda-missing-runtime-compact",
        ),
        pytest.param(
            "cann",
            "uc-manager-cann-{variant}-mc{mooncake.compact}",
            id="cann-missing-runtime-compact",
        ),
        pytest.param(
            "cann",
            "uc-manager-cann{runtime.compact}-mc{mooncake.compact}",
            id="cann-missing-variant",
        ),
        pytest.param(
            "cann",
            "uc-manager-cann{runtime.compact}-{variant}",
            id="cann-missing-mooncake-compact",
        ),
    ],
)
def test_compile_distribution_template_rejects_invalid_contracts(
    family: str,
    template: str,
) -> None:
    """products.compile_distribution_template owns template validation."""
    products = _require_public_module("ucm_release.products")
    compile_template = _require_public_callable(
        products, "compile_distribution_template"
    )

    with pytest.raises(ValueError):
        compile_template(family, template)


def test_expand_distribution_names_returns_deterministic_distinct_names() -> None:
    """products.expand_distribution_names projects canonical names in order."""
    products = _require_public_module("ucm_release.products")
    expand_names = _require_public_callable(products, "expand_distribution_names")
    capability_contexts = [
        {
            "accelerator": "cuda",
            "accelerator_runtime": "cuda-13.0",
        },
        {
            "accelerator": "ascend",
            "accelerator_runtime": "cann-9.1.0",
            "variant": "a3",
            "mooncake_version": "0.3.11.post1",
        },
    ]

    expanded = expand_names(
        product_rules=_canonical_product_rules(),
        capability_contexts=capability_contexts,
    )

    assert expanded == (
        "uc-manager-cann910-a3-mc0311post1",
        "uc-manager-cuda130",
    )


def test_expand_distribution_names_rejects_duplicate_names() -> None:
    """products.expand_distribution_names rejects normalization collisions."""
    products = _require_public_module("ucm_release.products")
    expand_names = _require_public_callable(products, "expand_distribution_names")
    product_rules = {"cuda": _canonical_product_rules()["cuda"]}
    capability_contexts = [
        {
            "accelerator": "cuda",
            "accelerator_runtime": "cuda-13.0",
        },
        {
            "accelerator": "cuda",
            "accelerator_runtime": "cuda-1.30",
        },
    ]

    with pytest.raises(ValueError):
        expand_names(
            product_rules=product_rules,
            capability_contexts=capability_contexts,
        )


@pytest.mark.parametrize(
    ("distribution_template", "capability_context"),
    [
        pytest.param(
            "uc-manager-cann{runtime.compact}-{variant}-mc{mooncake.compact}",
            {
                "accelerator": "ascend",
                "accelerator_runtime": "cann-9.1.0",
                "variant": "",
                "mooncake_version": "0.3.11.post1",
            },
            id="empty-variant-expansion",
        ),
        pytest.param(
            "UC Manager {runtime.compact}",
            {
                "accelerator": "cuda",
                "accelerator_runtime": "cuda-13.0",
            },
            id="non-pep503-distribution",
        ),
    ],
)
def test_expand_distribution_names_rejects_invalid_rendered_names(
    distribution_template: str,
    capability_context: dict[str, object],
) -> None:
    """products.expand_distribution_names rejects empty or invalid output."""
    products = _require_public_module("ucm_release.products")
    expand_names = _require_public_callable(products, "expand_distribution_names")
    family = "cann" if capability_context["accelerator"] == "ascend" else "cuda"
    product_rules = {
        family: {
            "accelerator": capability_context["accelerator"],
            "distribution": distribution_template,
        }
    }

    with pytest.raises(ValueError):
        expand_names(
            product_rules=product_rules,
            capability_contexts=[capability_context],
        )


@pytest.mark.parametrize(
    ("function_name", "raw_version", "expected"),
    [
        ("compact_accelerator_runtime", "cuda-13.0", "130"),
        ("compact_accelerator_runtime", "cann-9.1.0", "910"),
        ("compact_mooncake_version", "0.3.11.post1", "0311post1"),
    ],
)
def test_capability_version_normalizers_return_canonical_compact_values(
    function_name: str,
    raw_version: str,
    expected: str,
) -> None:
    """capabilities.py is the sole authority for compact version tokens."""
    capabilities = _require_public_module("ucm_release.capabilities")
    normalizer = _require_public_callable(capabilities, function_name)

    assert normalizer(raw_version) == expected


@pytest.mark.parametrize(
    ("function_name", "raw_version"),
    [
        pytest.param(
            "compact_accelerator_runtime",
            "cuda-13/0",
            id="cuda-arbitrary-slash",
        ),
        pytest.param(
            "compact_accelerator_runtime",
            "cann-9!1!0",
            id="cann-arbitrary-punctuation",
        ),
        pytest.param(
            "compact_mooncake_version",
            "0.3/11.post1",
            id="mooncake-arbitrary-slash",
        ),
        pytest.param(
            "compact_mooncake_version",
            "0.3.11@post1",
            id="mooncake-arbitrary-at",
        ),
    ],
)
def test_capability_version_normalizers_reject_unparseable_versions(
    function_name: str,
    raw_version: str,
) -> None:
    """Version normalizers must parse versions before compacting them."""
    capabilities = _require_public_module("ucm_release.capabilities")
    normalizer = _require_public_callable(capabilities, function_name)

    with pytest.raises(ValueError):
        normalizer(raw_version)
