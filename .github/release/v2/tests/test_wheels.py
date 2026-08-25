from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the public v2 CLI against only files supplied by the test."""
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=V2_ROOT,
        env=os.environ | {"PYTHONPATH": str(V2_ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )


def _lifecycle_plan(stage: str = "nightly") -> dict[str, object]:
    stage_arguments = {
        "nightly": [
            "--trigger",
            "schedule",
            "--ref",
            "refs/heads/develop",
            "--repository-role",
            "validation",
            "--date",
            "2026-08-12",
        ],
        "rc": [
            "--trigger",
            "workflow_dispatch",
            "--ref",
            "refs/heads/main",
            "--repository-role",
            "production",
            "--intent-json",
            json.dumps({"source_sha": SHA, "stage": "rc", "version": "0.6.0rc1"}),
        ],
    }[stage]
    result = _run(
        "lifecycle",
        "plan",
        "--stage",
        stage,
        "--source-sha",
        SHA,
        *stage_arguments,
        "--config",
        "release.yaml",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _resign(value: dict[str, object]) -> dict[str, object]:
    unsigned = dict(value)
    unsigned.pop("sha256", None)
    value["sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return value


def _guard() -> object:
    path = V2_ROOT / "packaging" / "backend_guard.py"
    spec = importlib.util.spec_from_file_location("ucm_backend_guard_strict", path)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


@pytest.mark.parametrize(
    "mutation",
    [
        "product-version",
        "image-owner",
        "source-version",
        "intent-source",
        "intent-version",
    ],
)
def test_wheel_plan_rejects_resigned_schema_valid_lifecycle_semantic_drift(
    tmp_path: Path, mutation: str
) -> None:
    """Wheel planning must enforce the runtime lifecycle contract, not only its digest."""
    lifecycle = _lifecycle_plan("rc" if mutation.startswith("intent-") else "nightly")
    if mutation == "product-version":
        lifecycle["products"][0][
            "coordinate"
        ] = "unified-cache-pd@0.6.0.dev20260813+nightly.g.aaaaaaaaaaaa"
    elif mutation == "image-owner":
        image = next(item for item in lifecycle["products"] if item["kind"] == "image")
        image["coordinate"] = image["coordinate"].replace(
            "ghcr.io/SuperMarioYL/", "ghcr.io/attacker/"
        )
    elif mutation == "source-version":
        lifecycle["source_sha"] = "b" * 40
    elif mutation == "intent-source":
        lifecycle["release_intent"]["source_sha"] = "b" * 40
    else:
        lifecycle["release_intent"]["version"] = "0.6.0rc2"
    path = _write_json(tmp_path / f"{mutation}.json", _resign(lifecycle))

    result = _run("wheel", "plan", "--lifecycle-plan", str(path))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert "lifecycle plan" in result.stderr


def test_wheel_plan_binds_all_three_explicit_backend_coordinates(
    tmp_path: Path,
) -> None:
    """Catches a wheel plan that drifts from its lifecycle version or omits a backend."""
    lifecycle_path = _write_json(tmp_path / "lifecycle-plan.json", _lifecycle_plan())

    first = _run("wheel", "plan", "--lifecycle-plan", str(lifecycle_path))
    second = _run("wheel", "plan", "--lifecycle-plan", str(lifecycle_path))

    assert first.returncode == second.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    plan = json.loads(first.stdout)
    assert plan["kind"] == "wheel-plan"
    assert plan["schema_version"] == 2
    assert plan["mode"] == "dry-run"
    assert plan["lifecycle_plan"] == {
        "sha256": _lifecycle_plan()["sha256"],
        "source_sha": SHA,
        "version": "0.6.0.dev20260812+nightly.g.aaaaaaaaaaaa",
    }
    assert plan["distributions"] == [
        {
            "backend": "cuda",
            "conflicts": ["uc-manager", "uc-manager-cann-a2", "uc-manager-cann-a3"],
            "distribution": "uc-manager-cuda",
            "import_name": "ucm",
            "version": "0.6.0.dev20260812+nightly.g.aaaaaaaaaaaa",
        },
        {
            "backend": "cann-a2",
            "conflicts": ["uc-manager", "uc-manager-cuda", "uc-manager-cann-a3"],
            "distribution": "uc-manager-cann-a2",
            "import_name": "ucm",
            "version": "0.6.0.dev20260812+nightly.g.aaaaaaaaaaaa",
        },
        {
            "backend": "cann-a3",
            "conflicts": ["uc-manager", "uc-manager-cuda", "uc-manager-cann-a2"],
            "distribution": "uc-manager-cann-a3",
            "import_name": "ucm",
            "version": "0.6.0.dev20260812+nightly.g.aaaaaaaaaaaa",
        },
    ]


@pytest.mark.parametrize("mutation", ["sha256", "source_sha", "products"])
def test_wheel_plan_rejects_untrusted_or_incomplete_lifecycle_plan(
    tmp_path: Path, mutation: str
) -> None:
    """Catches a planner accepting an altered envelope, source, or wheel closure."""
    lifecycle = _lifecycle_plan()
    if mutation == "sha256":
        lifecycle["sha256"] = "0" * 64
    elif mutation == "source_sha":
        lifecycle["source_sha"] = "b" * 40
    else:
        lifecycle["products"] = [
            item
            for item in lifecycle["products"]
            if item["name"] != "uc-manager-cann-a3"
        ]
        unsigned = dict(lifecycle)
        unsigned.pop("sha256")
        lifecycle["sha256"] = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    path = _write_json(tmp_path / "lifecycle-plan.json", lifecycle)

    result = _run("wheel", "plan", "--lifecycle-plan", str(path))

    assert result.returncode != 0
    assert "lifecycle plan" in result.stderr


@pytest.mark.parametrize(
    ("installed", "expected_status"),
    [([], "absent"), ([{"name": "UC_Manager_CUDA", "version": "0.6.0"}], "compatible")],
)
def test_environment_check_uses_pep503_names_for_absent_or_single_backend(
    tmp_path: Path, installed: list[dict[str, str]], expected_status: str
) -> None:
    """Catches a checker that misses normalized metadata or rejects one valid backend."""
    path = _write_json(tmp_path / "installed.json", installed)

    result = _run("wheel", "check-environment", "--installed-json", str(path))

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["kind"] == "wheel-environment-check"
    assert report["mode"] == "dry-run"
    assert report["status"] == expected_status


@pytest.mark.parametrize(
    "installed",
    [
        [
            {"name": "uc-manager", "version": "0.5.0"},
            {"name": "uc-manager-cuda", "version": "0.6.0"},
        ],
        [
            {"name": "uc-manager-cuda", "version": "0.6.0"},
            {"name": "uc-manager-cann-a2", "version": "0.6.0"},
        ],
        [
            {"name": "uc-manager-cann-a2", "version": "0.6.0"},
            {"name": "uc-manager-cann-a3", "version": "0.6.0"},
        ],
        [
            {"name": "uc-manager-cuda", "version": "0.6.0"},
            {"name": "uc-manager-cuda", "version": "0.6.1"},
        ],
        [
            {"name": "uc-manager-cuda", "version": "0.6.0"},
            {"name": "uc-manager-cann-a2", "version": "0.6.0"},
            {"name": "uc-manager-cann-a3", "version": "0.6.0"},
        ],
        [
            {"name": "uc-manager-cuda", "version": "0.6.0"},
            {"name": "uc-manager-cuda", "version": "0.6.0"},
        ],
    ],
)
def test_environment_check_fails_closed_for_legacy_mixed_or_duplicate_installs(
    tmp_path: Path, installed: list[dict[str, str]]
) -> None:
    """Catches an installer state that can leave competing UCM imports on sys.path."""
    path = _write_json(tmp_path / "installed.json", installed)

    result = _run("wheel", "check-environment", "--installed-json", str(path))

    assert result.returncode != 0
    assert "python -m pip uninstall" in result.stderr
    assert "python -m pip install" in result.stderr


def test_legacy_distribution_is_never_v2_compatible_even_when_installed_alone(
    tmp_path: Path,
) -> None:
    """Catches the legacy uc-manager distribution being labelled v2-compatible."""
    path = _write_json(
        tmp_path / "legacy.json", [{"name": "uc-manager", "version": "0.5.0"}]
    )

    result = _run("wheel", "check-environment", "--installed-json", str(path))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "unsafe" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "records",
    [
        [{"name": "unknown-ucm-provider", "version": "1.0", "top_level": ["ucm"]}],
        [
            {"name": "uc-manager-cuda", "version": "0.6.0", "top_level": ["ucm"]},
            {"name": "unknown-ucm-provider", "version": "1.0", "top_level": ["ucm"]},
        ],
    ],
)
def test_host_guard_rejects_every_unapproved_distribution_that_provides_ucm(
    records: list[dict[str, object]],
) -> None:
    """Catches an unknown top_level ucm provider being ignored beside an approved backend."""
    guard = _guard()

    with pytest.raises(
        guard.MetadataError, match="unapproved.*ucm|python -m pip uninstall"
    ):
        guard.check_environment(records, strict_metadata=False)


@pytest.mark.parametrize(
    ("installed", "expected_status"),
    [
        ([], "absent"),
        ([{"name": "uc-manager-cuda", "version": "0.6.0"}], "compatible"),
        ([{"name": "uc-manager", "version": "0.5.0"}], None),
        (
            [
                {
                    "name": "unknown-ucm-provider",
                    "version": "1.0",
                    "top_level": ["ucm"],
                }
            ],
            None,
        ),
        (
            [
                {"name": "uc-manager-cuda", "version": "0.6.0"},
                {
                    "name": "unknown-ucm-provider",
                    "version": "1.0",
                    "top_level": ["ucm"],
                },
            ],
            None,
        ),
    ],
)
def test_actual_environment_cli_outputs_are_schema_valid_or_fail_closed(
    tmp_path: Path,
    installed: list[dict[str, object]],
    expected_status: str | None,
) -> None:
    """Catches a rejected environment returning schema-invalid success JSON."""
    path = _write_json(tmp_path / "installed-schema.json", installed)
    result = _run("wheel", "check-environment", "--installed-json", str(path))

    if expected_status is None:
        assert result.returncode == 2
        assert result.stdout == ""
        assert "Traceback" not in result.stderr
        return
    assert result.returncode == 0, result.stderr
    report_path = _write_json(tmp_path / "report.json", json.loads(result.stdout))
    validated = subprocess.run(
        [
            "jsonschema",
            str(V2_ROOT / "schemas/wheel-environment-check.schema.json"),
            "-i",
            str(report_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert validated.returncode == 0, validated.stderr
    assert json.loads(result.stdout)["status"] == expected_status


@pytest.mark.parametrize(
    "installed", [{}, [{"name": "uc-manager-cuda"}], "not-an-array"]
)
def test_environment_check_rejects_malformed_fixture_metadata(
    tmp_path: Path, installed: object
) -> None:
    """Catches malformed installed-distribution metadata being silently ignored."""
    path = _write_json(tmp_path / "installed.json", installed)

    result = _run("wheel", "check-environment", "--installed-json", str(path))

    assert result.returncode != 0
    assert "installed-json" in result.stderr
    assert "python -m pip uninstall" in result.stderr
    assert "python -m pip install" in result.stderr


@pytest.mark.parametrize(
    "installed",
    [
        [{"name": "uc-manager-cuda ", "version": "0.6.0"}],
        [{"name": "uc-manager-cuda", "version": ""}],
        [{"name": "uc-manager-cuda", "version": "not/a/version"}],
        [{"name": "unrelated-package", "version": "1.0", "extra": "not-permitted"}],
    ],
)
def test_environment_check_rejects_invalid_fixture_metadata_with_recovery(
    tmp_path: Path, installed: object
) -> None:
    """Catches malformed fixture records silently becoming an absent environment."""
    path = _write_json(tmp_path / "installed.json", installed)

    result = _run("wheel", "check-environment", "--installed-json", str(path))

    assert result.returncode != 0
    assert "python -m pip uninstall" in result.stderr
    assert "python -m pip install" in result.stderr


def test_backend_guard_is_reusable_and_never_requires_hardware_detection() -> None:
    """Catches a future wheel guard that permits conflicting normalized distributions."""
    path = V2_ROOT / "packaging" / "backend_guard.py"
    spec = importlib.util.spec_from_file_location("ucm_backend_guard", path)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    report = guard.check_environment([{"name": "uc_manager_cuda", "version": "0.6.0"}])
    assert report["status"] == "compatible"
    with pytest.raises(guard.BackendConflictError, match="python -m pip uninstall"):
        guard.check_environment(
            [
                {"name": "uc-manager-cuda", "version": "0.6.0"},
                {"name": "uc-manager-cann-a3", "version": "0.6.0"},
            ]
        )


def test_backend_guard_runs_without_the_packaging_dependency() -> None:
    """Catches a wheel guard that imports an undeclared third-party dependency."""
    path = V2_ROOT / "packaging" / "backend_guard.py"
    script = """
import builtins
import importlib.util
import json
import sys

original_import = builtins.__import__
def blocked_import(name, *args, **kwargs):
    if name == "packaging" or name.startswith("packaging."):
        raise ImportError("packaging is unavailable")
    return original_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
spec = importlib.util.spec_from_file_location("isolated_backend_guard", sys.argv[1])
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
assert guard.check_environment([{"name": "uc-manager-cuda", "version": "0.6.0"}])["status"] == "compatible"
try:
    guard.check_environment([{"name": "uc-manager-cuda", "version": "not/a/version"}])
except guard.MetadataError as error:
    assert "python -m pip uninstall" in str(error)
else:
    raise AssertionError("invalid version must fail closed")
print(json.dumps({"status": "passed"}))
"""

    result = subprocess.run(
        ["python3", "-c", script, str(path)],
        cwd=V2_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "passed"}


@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        ("1.0a1", True),
        ("1.0b1", True),
        ("1.0rc1", True),
        ("1.0rc", True),
        ("1.0c1", True),
        ("1.0pre1", True),
        ("1.0preview1", True),
        ("1.0-alpha1", True),
        ("1.0_beta1", True),
        ("1.0.rc1", True),
        ("1.0rev1", True),
        ("1.0.post1", True),
        ("1.0post", True),
        ("1.0dev", True),
        ("1.0.dev1+nightly.1", True),
        ("1.0releasecandidate1", False),
    ],
)
def test_backend_guard_accepts_common_pep440_aliases_and_rejects_invalid_versions(
    version: str, accepted: bool
) -> None:
    """Catches common PEP 440 aliases being rejected or malformed versions accepted."""
    path = V2_ROOT / "packaging" / "backend_guard.py"
    spec = importlib.util.spec_from_file_location("ucm_backend_guard_versions", path)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    records = [{"name": "uc-manager-cuda", "version": version}]
    if accepted:
        assert guard.check_environment(records)["status"] == "compatible"
    else:
        with pytest.raises(guard.MetadataError, match="python -m pip uninstall"):
            guard.check_environment(records)


@pytest.mark.parametrize("version", ["1.0-", "1.0rc-", "1.0post_", "1.0dev."])
def test_backend_guard_rejects_dangling_pep440_separators(version: str) -> None:
    """Catches a separator being accepted without a following token or digit."""
    path = V2_ROOT / "packaging" / "backend_guard.py"
    spec = importlib.util.spec_from_file_location("ucm_backend_guard_separators", path)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    with pytest.raises(guard.MetadataError) as error:
        guard.check_environment([{"name": "uc-manager-cuda", "version": version}])

    assert "python -m pip uninstall" in str(error.value)
    assert "python -m pip install" in str(error.value)


def test_backend_guard_rejects_collector_metadata_that_claims_ucm_without_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a broken installed metadata record hiding a provider of import ucm."""
    path = V2_ROOT / "packaging" / "backend_guard.py"
    spec = importlib.util.spec_from_file_location("ucm_backend_guard_collector", path)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    class MetadataWithoutName:
        metadata: dict[str, str] = {}
        version = "0.6.0"

        @staticmethod
        def read_text(name: str) -> str | None:
            return "ucm\n" if name == "top_level.txt" else None

    monkeypatch.setattr(
        guard.importlib.metadata, "distributions", lambda: [MetadataWithoutName()]
    )

    with pytest.raises(guard.MetadataError, match="python -m pip uninstall"):
        guard.check_environment(guard.installed_distributions(), strict_metadata=False)


def test_distribution_metadata_has_one_shared_contract_for_all_backends() -> None:
    """Catches wheel metadata whose keys or conflicts differ across backend distributions."""
    metadata = [
        json.loads(
            (V2_ROOT / "packaging" / backend / "distribution.json").read_text(
                encoding="utf-8"
            )
        )
        for backend in ("cuda", "cann-a2", "cann-a3")
    ]

    assert [set(item) for item in metadata] == [
        {"backend", "conflicts", "distribution", "import_name"}
    ] * 3
    assert [item["backend"] for item in metadata] == ["cuda", "cann-a2", "cann-a3"]
    assert all(item["import_name"] == "ucm" for item in metadata)
    assert all("uc-manager" in item["conflicts"] for item in metadata)
