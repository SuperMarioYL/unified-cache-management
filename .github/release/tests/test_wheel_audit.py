from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

RELEASE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELEASE_ROOT))
wheel_audit = importlib.import_module("ucm_release.wheel_audit")


def _report(
    external: dict[str, str | None],
    *,
    direct: list[str],
    dependencies: dict[str, list[str]],
) -> str:
    tree = {
        "ucm/test-extension.so": {
            "needed": direct,
            "libraries": {
                soname: {"needed": required}
                for soname, required in dependencies.items()
            },
        }
    }
    return "\n".join(
        (
            "DEBUG:auditwheel.wheel_abi:full_elftree:",
            json.dumps(tree, sort_keys=True),
            "The following external shared libraries are required by the wheel:",
            json.dumps(external, sort_keys=True),
        )
    )


def test_path_boundary_derives_actual_roots_and_vendor_transitives() -> None:
    external = {
        "libroot_a.so": "/usr/local/Ascend/cann/lib64/libroot_a.so",
        "libroot_b.so": "/usr/local/Ascend/cann/lib64/libroot_b.so",
        "libchild.so": "/usr/local/Ascend/cann/lib64/libchild.so",
        "libdriver.so": None,
    }
    report = _report(
        external,
        direct=["libroot_a.so", "libroot_b.so"],
        dependencies={
            "libroot_a.so": ["libchild.so"],
            "libroot_b.so": ["libchild.so"],
            "libchild.so": ["libdriver.so", "libc.so.6"],
            "libdriver.so": [],
            "libc.so.6": [],
        },
    )

    closure = wheel_audit.validate_external_library_closure(
        report,
        expected_patterns=["/usr/local/Ascend/*"],
        allowed_deferred_libraries=["libdriver.so"],
    )

    assert closure["external_library_roots"] == ["libroot_a.so", "libroot_b.so"]
    assert closure["external_libraries"] == sorted(external)
    assert closure["runtime_deferred_libraries"] == ["libdriver.so"]
    assert closure["deferred_external_libraries"] == ["libdriver.so"]


def test_closure_does_not_defer_an_unresolved_direct_root() -> None:
    report = _report(
        {"libcudart.so.13": None},
        direct=["libcudart.so.13"],
        dependencies={"libcudart.so.13": []},
    )

    closure = wheel_audit.validate_external_library_closure(
        report,
        expected_patterns=["libcudart.so.13"],
        allowed_deferred_libraries=[],
    )

    assert closure["external_library_roots"] == ["libcudart.so.13"]
    assert closure["deferred_external_libraries"] == []


def test_closure_rejects_a_direct_library_outside_provider_boundary() -> None:
    report = _report(
        {
            "libroot.so": "/usr/local/Ascend/cann/lib64/libroot.so",
            "libunrelated.so": "/other/libunrelated.so",
        },
        direct=["libroot.so", "libunrelated.so"],
        dependencies={"libroot.so": [], "libunrelated.so": []},
    )

    with pytest.raises(ValueError, match="outside the provider boundary"):
        wheel_audit.validate_external_library_closure(
            report,
            expected_patterns=["/usr/local/Ascend/*"],
            allowed_deferred_libraries=[],
        )


def test_closure_rejects_a_removed_transitive_edge() -> None:
    report = _report(
        {
            "libroot.so": "/usr/local/Ascend/cann/lib64/libroot.so",
            "libchild.so": "/usr/local/Ascend/cann/lib64/libchild.so",
        },
        direct=["libroot.so"],
        dependencies={"libroot.so": [], "libchild.so": []},
    )

    with pytest.raises(ValueError, match="outside the provider closure"):
        wheel_audit.validate_external_library_closure(
            report,
            expected_patterns=["/usr/local/Ascend/*"],
            allowed_deferred_libraries=[],
        )


def test_closure_rejects_ucm_owned_metrics_library() -> None:
    report = _report(
        {
            "libroot.so": "/usr/local/Ascend/cann/lib64/libroot.so",
            "libmetrics.so": "/wheel/ucm/shared/metrics/libmetrics.so",
        },
        direct=["libroot.so"],
        dependencies={
            "libroot.so": ["libmetrics.so"],
            "libmetrics.so": [],
        },
    )

    with pytest.raises(ValueError, match="UCM-owned libmetrics"):
        wheel_audit.validate_external_library_closure(
            report,
            expected_patterns=["/usr/local/Ascend/*"],
            allowed_deferred_libraries=[],
        )


def test_closure_fails_when_provider_pattern_matches_no_direct_library() -> None:
    report = _report(
        {"libroot.so": "/opt/Ascend/lib/libroot.so"},
        direct=["libroot.so"],
        dependencies={"libroot.so": []},
    )

    with pytest.raises(ValueError, match="matched no direct external"):
        wheel_audit.validate_external_library_closure(
            report,
            expected_patterns=["/usr/local/Ascend/*"],
            allowed_deferred_libraries=[],
        )


def test_unresolved_transitive_library_requires_explicit_runtime_policy() -> None:
    report = _report(
        {
            "libroot.so": "/usr/local/Ascend/cann/lib64/libroot.so",
            "libunknown.so": None,
        },
        direct=["libroot.so"],
        dependencies={"libroot.so": ["libunknown.so"], "libunknown.so": []},
    )

    with pytest.raises(ValueError, match="not explicitly deferred"):
        wheel_audit.validate_external_library_closure(
            report,
            expected_patterns=["/usr/local/Ascend/*"],
            allowed_deferred_libraries=[],
        )


@pytest.mark.parametrize("pattern", ["/*", "/usr/../Ascend/*", "lib*.so"])
def test_external_runtime_exclude_pattern_rejects_broad_or_relative_values(
    pattern: str,
) -> None:
    with pytest.raises(ValueError, match="invalid external runtime exclude pattern"):
        wheel_audit.validate_exclude_pattern(pattern)
