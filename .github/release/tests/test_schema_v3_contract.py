"""RED contracts for the forward-compatible Schema v3 release authority."""

from __future__ import annotations

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


def _load_release_yaml() -> dict[str, object]:
    release = yaml.safe_load(RELEASE_PATH.read_text(encoding="utf-8"))
    assert isinstance(release, dict), "release.yaml must contain a YAML object"
    return release


def _write_release_yaml(tmp_path: Path, release: dict[str, object]) -> Path:
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")
    return path


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
    release = _load_release_yaml()
    release["schema_version"] = 3
    release["wheel_profiles"] = [{"id": "legacy-fixed-profile"}]
    path = _write_release_yaml(tmp_path, release)

    with pytest.raises(ValueError, match=r"wheel_profiles"):
        core.load_catalog(path)


@pytest.mark.parametrize(
    ("family", "template", "expected_reason"),
    [
        ("cuda", "uc-manager-cuda{runtime.compact", "malformed"),
        (
            "cuda",
            "uc-manager-cuda{runtime.compact}-{runtime.major}",
            "unknown",
        ),
        ("cuda", "uc-manager-cuda", "runtime.compact"),
        (
            "cann",
            "uc-manager-cann{runtime.compact}-{variant}",
            "mooncake.compact",
        ),
    ],
)
def test_compile_distribution_template_rejects_invalid_contracts(
    family: str,
    template: str,
    expected_reason: str,
) -> None:
    """products.compile_distribution_template owns template validation."""
    products = _require_public_module("ucm_release.products")
    compile_template = _require_public_callable(
        products, "compile_distribution_template"
    )

    with pytest.raises(ValueError) as caught:
        compile_template(family, template)

    assert expected_reason in str(caught.value).lower()


def test_expand_distributions_rejects_duplicate_expanded_coordinates() -> None:
    """products.expand_distributions must reject normalization collisions."""
    products = _require_public_module("ucm_release.products")
    expand_distributions = _require_public_callable(products, "expand_distributions")
    product_rules = {
        "cuda": {
            "accelerator": "cuda",
            "distribution": "uc-manager-cuda{runtime.compact}",
        }
    }
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

    with pytest.raises(ValueError, match=r"(?i)duplicate.*distribution"):
        expand_distributions(product_rules, capability_contexts)


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
