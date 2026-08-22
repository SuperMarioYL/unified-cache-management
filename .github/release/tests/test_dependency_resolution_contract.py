"""Task 4A2 RED contracts for exact dependency resolution."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from packaging import tags as packaging_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
SELECTION_FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "task4-candidate-input.json"
INDEX_FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "task4-dependency-index.json"
PYPI_SIMPLE_INDEX = "https://pypi.org/simple/"
sys.path.insert(0, str(RELEASE_ROOT))

capabilities = importlib.import_module("ucm_release.capabilities")
cli = importlib.import_module("ucm_release.cli")
core = importlib.import_module("ucm_release.core")
products = importlib.import_module("ucm_release.products")

RESOLUTION_FIELDS = {
    "kind",
    "schema_version",
    "source_sha",
    "config_sha256",
    "catalog_sha256",
    "selection_sha256",
    "index_url",
    "requests",
    "resolution_sha256",
}
RESOLUTION_REQUEST_FIELDS = {
    "request_id",
    "coordinate",
    "requirements",
    "status",
    "resolved",
    "failures",
}
RESOLVED_FIELDS = {
    "requirement_id",
    "scope",
    "name",
    "version",
    "filename",
    "url",
    "sha256",
    "requires_python",
    "wheel_tags",
}
FAILURE_FIELDS = {"code", "requirement_id", "scope", "name", "version"}
INDEX_FILE_FIELDS = {
    "name",
    "version",
    "filename",
    "url",
    "sha256",
    "requires_python",
    "wheel_tags",
}


def _dependencies() -> Any:
    return importlib.import_module("ucm_release.dependencies")


def _public_callable(module: object, name: str) -> Callable[..., Any]:
    function = getattr(module, name, None)
    assert callable(
        function
    ), f"required public API {module.__name__}.{name} is missing"
    return function


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _reseal(value: dict[str, Any], field: str) -> None:
    projection = copy.deepcopy(value)
    projection.pop(field, None)
    value[field] = _canonical_digest(projection)


def _raw_selection_fixture() -> dict[str, Any]:
    value = json.loads(SELECTION_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _index_fixture() -> dict[str, Any]:
    value = json.loads(INDEX_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _config() -> dict[str, Any]:
    config = core.load_catalog(version_override="0.8.0rc1")
    by_runtime_product = {
        item["runtime_product"]: item for item in config["upstream_products"]
    }
    by_runtime_product["vllm"]["runtime_tag_selectors"] = [
        "v{version}",
        "v{version}-cu{runtime.major_minor.compact}",
    ]
    by_runtime_product["vllm-ascend"]["runtime_tag_selectors"] = [
        "v{version}",
        "v{version}-{variant}",
    ]
    return config


def _selection(
    fixture: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = fixture or _raw_selection_fixture()
    catalog = capabilities.assemble_capability_catalog(
        builder_discovery=raw["builder_discovery"],
        runtime_discovery=raw["runtime_discovery"],
        python_probes=raw["python_probes"],
        mooncake_probes=raw["mooncake_probes"],
        python_requires=raw["python_requires"],
    )
    selection = products.prepare_candidate_selection(
        _config(),
        capabilities.validate_capability_catalog(catalog),
        copy.deepcopy(raw["expected_current_builder_authority"]),
        route="pr",
        source_sha=raw["source_sha"],
    )
    return selection, raw


def _reader(
    fixture: Mapping[str, Any],
) -> Callable[[str, str], Sequence[Mapping[str, Any]]]:
    projects = fixture["projects"]

    def read_project(name: str, version: str) -> Sequence[Mapping[str, Any]]:
        assert canonicalize_name(name) == name
        assert isinstance(version, str) and version
        return copy.deepcopy(projects.get(name, []))

    return read_project


def _resolve(
    selection: dict[str, Any],
    fixture: dict[str, Any] | None = None,
    *,
    config: dict[str, Any] | None = None,
    index_reader: Callable[[str, str], Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    resolve = _public_callable(_dependencies(), "resolve_dependency_resolution")
    result = resolve(
        copy.deepcopy(config or _config()),
        copy.deepcopy(selection),
        index_reader=index_reader or _reader(fixture or _index_fixture()),
    )
    assert isinstance(result, dict)
    return result


def _future_abi_fixture() -> dict[str, Any]:
    fixture = _raw_selection_fixture()
    probe = copy.deepcopy(fixture["python_probes"]["probes"][0])
    probe.update(
        interpreter_path="/opt/python/cp316-cp316t/bin/python",
        python_version="3.16",
        python_abi="cp316t",
        soabi="cpython-316t-x86_64-linux-gnu",
        wheel_tag="cp316-cp316t-manylinux_2_28_x86_64",
    )
    fixture["python_probes"]["probes"].append(probe)
    return fixture


def _future_index_fixture() -> dict[str, Any]:
    fixture = _index_fixture()
    for project in ("pyyaml", "wrapt"):
        exact = next(
            item
            for item in fixture["projects"][project]
            if "cp314-cp314t-manylinux_2_28_x86_64" in item["wheel_tags"]
        )
        future = copy.deepcopy(exact)
        for field in ("filename", "url"):
            future[field] = future[field].replace("cp314", "cp316")
        future["sha256"] = "sha256:" + hashlib.sha256(project.encode()).hexdigest()
        future["wheel_tags"] = [
            "cp316-cp316t-manylinux_2_28_x86_64"
        ]
        fixture["projects"][project].append(future)
    return fixture


def _requirement(
    selection: Mapping[str, Any], name: str
) -> Mapping[str, Any]:
    return next(
        item
        for item in selection["dependency_requests"][0]["requirements"]
        if item["name"] == name
    )


def _failure_resolution(
    selection: dict[str, Any], name: str = "pyyaml"
) -> dict[str, Any]:
    fixture = _index_fixture()
    fixture["projects"][name] = []
    result = _resolve(selection, fixture)
    assert result["requests"][0]["status"] == "failure"
    return result


def test_task4_dependency_index_fixture_is_raw_binary_evidence() -> None:
    fixture = _index_fixture()

    assert fixture["kind"] == "task4-dependency-index-fixture"
    assert fixture["schema_version"] == 3
    assert fixture["index_url"] == PYPI_SIMPLE_INDEX
    assert set(fixture["projects"]) == set(fixture["expected_cp314t"])
    for project, files in fixture["projects"].items():
        assert files
        for record in files:
            assert set(record) == INDEX_FILE_FIELDS
            assert canonicalize_name(record["name"]) == project
            assert record["url"].endswith(record["filename"])
            assert record["sha256"].startswith("sha256:")
            assert len(record["sha256"]) == 71
            assert record["requires_python"] is None or isinstance(
                record["requires_python"], str
            )
            if not record["filename"].endswith(".whl"):
                assert record["wheel_tags"] == []
                continue
            _, version, _, tags = parse_wheel_filename(record["filename"])
            assert str(version) == record["version"]
            assert sorted(str(tag) for tag in tags) == record["wheel_tags"]
        assert any(
            item["filename"] == fixture["expected_cp314t"][project]
            for item in files
        )


def test_dependency_resolution_public_seams_are_owned_by_dependencies_module() -> None:
    module = _dependencies()

    _public_callable(module, "resolve_dependency_resolution")
    _public_callable(module, "validate_dependency_resolution")
    assert not hasattr(products, "resolve_dependency_resolution")


def test_resolver_rejects_config_digest_drift_before_index_read() -> None:
    selection, _ = _selection()
    config = _config()
    config["image_revision"] += 1
    reads: list[tuple[str, str]] = []

    def reader(name: str, version: str) -> Sequence[Mapping[str, Any]]:
        reads.append((name, version))
        raise AssertionError("index reader must not run after config identity drift")

    with pytest.raises(ValueError, match="config|digest"):
        _resolve(selection, config=config, index_reader=reader)
    assert reads == []


def test_resolver_rejects_request_fanout_limit_before_index_read() -> None:
    selection, _ = _selection(_future_abi_fixture())
    config = _config()
    request_count = len(selection["dependency_requests"])
    assert request_count > 1
    config["discovery"]["matrix_limits"]["max_wheel_tasks"] = request_count - 1
    selection["config_sha256"] = _canonical_digest(config)
    _reseal(selection, "selection_sha256")
    reads: list[tuple[str, str]] = []

    def reader(name: str, version: str) -> Sequence[Mapping[str, Any]]:
        reads.append((name, version))
        raise AssertionError("index reader must not run after request limit overflow")

    with pytest.raises(ValueError, match="max_wheel_tasks|request limit"):
        _resolve(selection, config=config, index_reader=reader)
    assert reads == []


def test_dependency_resolution_is_closed_exact_and_self_digesting() -> None:
    selection, _ = _selection()
    fixture = _index_fixture()
    resolution = _resolve(selection, fixture)
    validate = _public_callable(_dependencies(), "validate_dependency_resolution")

    assert set(resolution) == RESOLUTION_FIELDS
    assert resolution["kind"] == "ucm-dependency-resolution"
    assert resolution["schema_version"] == 3
    assert resolution["index_url"] == PYPI_SIMPLE_INDEX
    for field in ("source_sha", "config_sha256", "catalog_sha256", "selection_sha256"):
        assert resolution[field] == selection[field]
    assert resolution["resolution_sha256"] == _canonical_digest(
        {
            key: value
            for key, value in resolution.items()
            if key != "resolution_sha256"
        }
    )
    assert (
        validate(
            copy.deepcopy(resolution), _config(), copy.deepcopy(selection)
        )
        == resolution
    )
    assert resolution["requests"] == sorted(
        resolution["requests"], key=lambda item: item["request_id"]
    )
    assert [item["request_id"] for item in resolution["requests"]] == [
        item["request_id"] for item in selection["dependency_requests"]
    ]
    for request, selected_request in zip(
        resolution["requests"], selection["dependency_requests"], strict=True
    ):
        assert set(request) == RESOLUTION_REQUEST_FIELDS
        assert request["coordinate"] == selected_request["coordinate"]
        assert request["requirements"] == selected_request["requirements"]
        assert request["status"] == "success"
        assert request["failures"] == []
        assert request["resolved"] == sorted(
            request["resolved"], key=lambda item: item["requirement_id"]
        )
        assert len(request["resolved"]) == len(request["requirements"])
        assert {
            item["requirement_id"] for item in request["resolved"]
        } == {item["requirement_id"] for item in request["requirements"]}
        for resolved in request["resolved"]:
            requirement = next(
                item
                for item in request["requirements"]
                if item["requirement_id"] == resolved["requirement_id"]
            )
            assert set(resolved) == RESOLVED_FIELDS
            assert {
                key: resolved[key]
                for key in ("requirement_id", "scope", "name", "version")
            } == requirement
            assert resolved["filename"] == fixture["expected_cp314t"][resolved["name"]]
            assert resolved["url"].endswith(resolved["filename"])
            assert resolved["wheel_tags"] == sorted(set(resolved["wheel_tags"]))
            source_record = next(
                item
                for item in fixture["projects"][resolved["name"]]
                if item["filename"] == resolved["filename"]
            )
            assert resolved["sha256"] == source_record["sha256"]
            assert resolved["requires_python"] == source_record["requires_python"]


def test_dependency_resolution_uses_wheel_tag_rank_not_file_order() -> None:
    selection, _ = _selection()
    forward = _index_fixture()
    reverse = copy.deepcopy(forward)
    for files in reverse["projects"].values():
        files.reverse()

    first = _resolve(selection, forward)
    second = _resolve(selection, reverse)

    assert first == second
    selected = {
        item["name"]: item["filename"]
        for request in first["requests"]
        for item in request["resolved"]
    }
    assert selected == forward["expected_cp314t"]


def test_dependency_resolution_rejects_same_highest_wheel_tag_rank_tie() -> None:
    selection, _ = _selection()
    fixture = _index_fixture()
    exact = next(
        item
        for item in fixture["projects"]["pyyaml"]
        if item["filename"] == fixture["expected_cp314t"]["pyyaml"]
    )
    tied = copy.deepcopy(exact)
    tied["filename"] = tied["filename"].replace("-cp314-", "-1-cp314-")
    tied["url"] = tied["url"].replace("-cp314-", "-1-cp314-")
    tied["sha256"] = "sha256:" + "a" * 64
    fixture["projects"]["pyyaml"].append(tied)

    with pytest.raises(ValueError, match="ambiguous|highest|rank"):
        _resolve(selection, fixture)


@pytest.mark.parametrize(
    ("case", "files"),
    [
        pytest.param(
            "sdist",
            lambda fixture: [
                item
                for item in fixture["projects"]["packaging"]
                if item["filename"].endswith(".tar.gz")
            ],
            id="no-sdist",
        ),
        pytest.param(
            "version-fallback",
            lambda fixture: [
                item
                for item in fixture["projects"]["packaging"]
                if item["version"] == "24.1"
            ],
            id="no-version-fallback",
        ),
        pytest.param(
            "incompatible",
            lambda fixture: [
                item
                for item in fixture["projects"]["packaging"]
                if item["wheel_tags"]
                == ["cp312-cp312-manylinux_2_28_x86_64"]
            ],
            id="no-environment-fallback",
        ),
    ],
)
def test_dependency_resolution_fails_without_compatible_exact_binary(
    case: str,
    files: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    selection, _ = _selection()
    fixture = _index_fixture()
    fixture["projects"]["packaging"] = files(fixture)

    resolution = _resolve(selection, fixture)
    request = resolution["requests"][0]
    requirement = _requirement(selection, "packaging")

    assert case in {"sdist", "version-fallback", "incompatible"}
    assert request["status"] == "failure"
    assert request["resolved"] == []
    assert request["failures"] == [
        {
            "code": "binary-wheel-unavailable",
            **requirement,
        }
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "declared-name",
        "declared-version",
        "filename-name",
        "filename-version",
        "declared-tags",
        "http-url",
        "relative-url",
        "malformed-sha256",
        "malformed-requires-python",
    ],
)
def test_resolver_rejects_malformed_exact_file_evidence(mutation: str) -> None:
    selection, _ = _selection()
    fixture = _index_fixture()
    record = copy.deepcopy(
        next(
            item
            for item in fixture["projects"]["packaging"]
            if item["filename"] == fixture["expected_cp314t"]["packaging"]
        )
    )
    fixture["projects"]["packaging"] = [record]
    if mutation == "declared-name":
        record["name"] = "other-project"
    elif mutation == "declared-version":
        record["version"] = "24.3"
    elif mutation == "filename-name":
        record["filename"] = "other_project-24.2-py3-none-any.whl"
        record["url"] = f"https://files.example.invalid/{record['filename']}"
    elif mutation == "filename-version":
        record["filename"] = "packaging-24.3-py3-none-any.whl"
        record["url"] = f"https://files.example.invalid/{record['filename']}"
    elif mutation == "declared-tags":
        record["wheel_tags"] = ["cp312-cp312-manylinux_2_28_x86_64"]
    elif mutation == "http-url":
        record["url"] = record["url"].replace("https://", "http://")
    elif mutation == "relative-url":
        record["url"] = f"files/{record['filename']}"
    elif mutation == "malformed-sha256":
        record["sha256"] = "sha256:" + "g" * 64
    else:
        record["requires_python"] = "not a specifier"

    resolution = _resolve(selection, fixture)
    request = resolution["requests"][0]
    assert request["status"] == "failure"
    assert request["resolved"] == []
    assert request["failures"] == [
        {
            "code": "binary-wheel-unavailable",
            **_requirement(selection, "packaging"),
        }
    ]


def test_requires_python_excludes_best_tag_then_uses_next_compatible() -> None:
    selection, _ = _selection()
    fixture = _index_fixture()
    exact = next(
        item
        for item in fixture["projects"]["pyyaml"]
        if item["filename"] == fixture["expected_cp314t"]["pyyaml"]
    )
    exact["requires_python"] = "<3.14"

    resolution = _resolve(selection, fixture)
    resolved = next(
        item
        for item in resolution["requests"][0]["resolved"]
        if item["name"] == "pyyaml"
    )

    assert resolved["filename"] == "PyYAML-6.0.2-py3-none-any.whl"
    assert resolved["requires_python"] == ">=3.8"


def test_requires_python_excludes_all_compatible_files() -> None:
    selection, _ = _selection()
    fixture = _index_fixture()
    generic = next(
        item
        for item in fixture["projects"]["packaging"]
        if item["filename"] == fixture["expected_cp314t"]["packaging"]
    )
    generic["requires_python"] = "<3.14"
    fixture["projects"]["packaging"] = [generic]

    resolution = _resolve(selection, fixture)
    request = resolution["requests"][0]

    assert request["status"] == "failure"
    assert request["resolved"] == []
    assert request["failures"] == [
        {
            "code": "binary-wheel-unavailable",
            **_requirement(selection, "packaging"),
        }
    ]


def test_failed_request_discards_other_successful_requirement_records() -> None:
    selection, _ = _selection()
    resolution = _failure_resolution(selection)
    request = resolution["requests"][0]

    assert request["status"] == "failure"
    assert request["resolved"] == []
    assert request["failures"] == [
        {
            "code": "binary-wheel-unavailable",
            **_requirement(selection, "pyyaml"),
        }
    ]
    assert set(request["failures"][0]) == FAILURE_FIELDS


def test_dependency_resolution_grows_for_future_free_threaded_abi_without_host_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_host_tags(*args: object, **kwargs: object) -> object:
        raise AssertionError("resolver must derive target tags from request coordinate")

    monkeypatch.setattr(packaging_tags, "sys_tags", forbidden_host_tags)
    dependency_module = _dependencies()
    if hasattr(dependency_module, "sys_tags"):
        monkeypatch.setattr(dependency_module, "sys_tags", forbidden_host_tags)
    selection, _ = _selection(_future_abi_fixture())
    resolution = _resolve(selection, _future_index_fixture())

    assert {
        request["coordinate"]["python_abi"] for request in resolution["requests"]
    } == {"cp314t", "cp316t"}
    for request in resolution["requests"]:
        assert request["coordinate"]["python_tag"] == request["coordinate"][
            "python_abi"
        ].removesuffix("t")
        assert request["status"] == "success"
        if request["coordinate"]["python_abi"] == "cp316t":
            native = {
                item["name"]: item["filename"]
                for item in request["resolved"]
                if item["name"] in {"pyyaml", "wrapt"}
            }
            assert all("cp316-cp316t" in filename for filename in native.values())


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-field",
        "resolution-hash",
        "source-identity",
        "config-identity",
        "catalog-identity",
        "selection-identity",
        "index-url",
        "missing-request",
        "duplicate-request",
        "unexpected-request",
        "request-order",
        "request-id",
        "coordinate-drift",
        "requirements-drift",
        "requirements-order",
        "invalid-status",
        "success-with-failure",
        "success-missing-resolved",
        "duplicate-resolved",
        "unexpected-resolved",
        "resolved-name-drift",
        "resolved-version-drift",
        "resolved-url-http",
        "resolved-url-relative",
        "resolved-sha256",
        "resolved-filename-name-drift",
        "resolved-filename-version-drift",
        "resolved-filename-tag-drift",
        "resolved-requires-python-incompatible",
        "resolved-requires-python-invalid",
        "duplicate-wheel-tag",
        "resolved-sdist",
        "resolved-order",
    ],
)
def test_dependency_resolution_validator_rejects_resealed_closure_drift(
    mutation: str,
) -> None:
    if mutation == "request-order":
        selection, _ = _selection(_future_abi_fixture())
        resolution = _resolve(selection, _future_index_fixture())
    else:
        selection, _ = _selection()
        resolution = _resolve(selection)
    request = resolution["requests"][0]
    if mutation == "extra-field":
        resolution["extra"] = None
    elif mutation == "resolution-hash":
        resolution["resolution_sha256"] = "sha256:" + "f" * 64
    elif mutation.endswith("-identity"):
        field = mutation.removesuffix("-identity") + "_sha256"
        if field == "source_sha256":
            field = "source_sha"
            resolution[field] = "f" * 40
        else:
            resolution[field] = "sha256:" + "f" * 64
        _reseal(resolution, "resolution_sha256")
    elif mutation == "index-url":
        resolution["index_url"] = "https://mirror.example.invalid/simple/"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "missing-request":
        resolution["requests"] = []
        _reseal(resolution, "resolution_sha256")
    elif mutation == "duplicate-request":
        resolution["requests"].append(copy.deepcopy(request))
        _reseal(resolution, "resolution_sha256")
    elif mutation == "unexpected-request":
        unexpected = copy.deepcopy(request)
        unexpected["coordinate"]["python_abi"] = "cp315"
        unexpected["request_id"] = _canonical_digest(
            {
                "coordinate": unexpected["coordinate"],
                "requirements": unexpected["requirements"],
            }
        )
        resolution["requests"].append(unexpected)
        resolution["requests"].sort(key=lambda item: item["request_id"])
        _reseal(resolution, "resolution_sha256")
    elif mutation == "request-order":
        resolution["requests"].reverse()
        _reseal(resolution, "resolution_sha256")
    elif mutation == "request-id":
        request["request_id"] = "sha256:" + "f" * 64
        _reseal(resolution, "resolution_sha256")
    elif mutation == "coordinate-drift":
        request["coordinate"]["manylinux"] = "manylinux_2_31"
        request["request_id"] = _canonical_digest(
            {
                "coordinate": request["coordinate"],
                "requirements": request["requirements"],
            }
        )
        _reseal(resolution, "resolution_sha256")
    elif mutation == "requirements-drift":
        request["requirements"][0]["scope"] = "runtime"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "requirements-order":
        request["requirements"].reverse()
        _reseal(resolution, "resolution_sha256")
    elif mutation == "invalid-status":
        request["status"] = "partial"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "success-with-failure":
        request["failures"] = [
            {"code": "binary-wheel-unavailable", **request["requirements"][0]}
        ]
        _reseal(resolution, "resolution_sha256")
    elif mutation == "success-missing-resolved":
        request["resolved"].pop()
        _reseal(resolution, "resolution_sha256")
    elif mutation == "duplicate-resolved":
        request["resolved"].append(copy.deepcopy(request["resolved"][0]))
        _reseal(resolution, "resolution_sha256")
    elif mutation == "unexpected-resolved":
        extra = copy.deepcopy(request["resolved"][0])
        extra["requirement_id"] = "sha256:" + "f" * 64
        request["resolved"].append(extra)
        request["resolved"].sort(key=lambda item: item["requirement_id"])
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-name-drift":
        request["resolved"][0]["name"] = "other-project"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-version-drift":
        request["resolved"][0]["version"] = "999"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-url-http":
        resolved = request["resolved"][0]
        resolved["url"] = resolved["url"].replace("https://", "http://")
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-url-relative":
        resolved = request["resolved"][0]
        resolved["url"] = f"files/{resolved['filename']}"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-sha256":
        request["resolved"][0]["sha256"] = "sha256:" + "g" * 64
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-filename-name-drift":
        request["resolved"][0]["filename"] = "other-24.2-py3-none-any.whl"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-filename-version-drift":
        request["resolved"][0]["filename"] = "packaging-24.3-py3-none-any.whl"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-filename-tag-drift":
        request["resolved"][0]["wheel_tags"] = ["cp312-cp312-any"]
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-requires-python-incompatible":
        request["resolved"][0]["requires_python"] = "<3.14"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-requires-python-invalid":
        request["resolved"][0]["requires_python"] = "not a specifier"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "duplicate-wheel-tag":
        request["resolved"][0]["wheel_tags"] *= 2
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-sdist":
        request["resolved"][0]["filename"] = "packaging-24.2.tar.gz"
        request["resolved"][0]["wheel_tags"] = []
        _reseal(resolution, "resolution_sha256")
    else:
        request["resolved"].reverse()
        _reseal(resolution, "resolution_sha256")

    validate = _public_callable(_dependencies(), "validate_dependency_resolution")
    with pytest.raises(ValueError):
        validate(resolution, _config(), selection)


@pytest.mark.parametrize(
    "mutation", ["failure-with-resolved", "failure-without-failures", "failure-shape"]
)
def test_dependency_resolution_validator_rejects_failed_request_cardinality_drift(
    mutation: str,
) -> None:
    selection, _ = _selection()
    resolution = _failure_resolution(selection)
    request = resolution["requests"][0]
    if mutation == "failure-with-resolved":
        successful = _resolve(selection)["requests"][0]["resolved"][0]
        request["resolved"] = [successful]
    elif mutation == "failure-without-failures":
        request["failures"] = []
    else:
        request["failures"][0]["detail"] = "unstable free text"
    _reseal(resolution, "resolution_sha256")

    validate = _public_callable(_dependencies(), "validate_dependency_resolution")
    with pytest.raises(ValueError):
        validate(resolution, _config(), selection)


def test_dependencies_resolve_cli_loads_selection_resolves_once_and_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependency_module = _dependencies()
    monkeypatch.setattr(cli, "dependencies", dependency_module, raising=False)
    parser = cli.build_parser()
    release_path = tmp_path / "release.yaml"
    schema_dir = tmp_path / "schemas"
    repository_root = tmp_path / "repository"
    selection_path = tmp_path / "selection.json"
    output_path = tmp_path / "resolution.json"
    argv = [
        "dependencies",
        "resolve",
        "--release",
        str(release_path),
        "--schema-dir",
        str(schema_dir),
        "--repository-root",
        str(repository_root),
        "--selection",
        str(selection_path),
        "--output",
        str(output_path),
    ]
    default_arguments = parser.parse_args(
        [
            "dependencies",
            "resolve",
            "--selection",
            str(selection_path),
            "--output",
            str(output_path),
        ]
    )
    assert default_arguments.release == core.DEFAULT_RELEASE
    assert default_arguments.schema_dir == core.DEFAULT_SCHEMA_DIR
    assert default_arguments.repository_root == core.REPO_ROOT
    for forbidden in (
        ["--allow-sdist"],
        ["--fallback-version"],
        ["--index-url", "https://mirror.example.invalid/simple/"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args([*argv, *forbidden])
    arguments = parser.parse_args(argv)
    config = {"config": "normalized"}
    selection = {"selection": "validated-by-resolver"}
    resolution = {"resolution": "written"}
    calls: list[tuple[object, ...]] = []
    writes: list[tuple[Path, object]] = []

    def fake_load_catalog(*args: object, **kwargs: object) -> object:
        assert args[:2] == (release_path, schema_dir)
        assert kwargs == {"repository_root": repository_root}
        return config

    def fake_load(path: Path) -> object:
        assert path == selection_path
        return selection

    def fake_resolve(config_value: object, selection_value: object) -> object:
        calls.append((config_value, selection_value))
        return resolution

    monkeypatch.setattr(core, "load_catalog", fake_load_catalog)
    monkeypatch.setattr(core, "load_json", fake_load)
    monkeypatch.setattr(
        dependency_module, "resolve_dependency_resolution", fake_resolve
    )
    monkeypatch.setattr(cli, "_write", lambda path, value: writes.append((path, value)))

    assert arguments.func(arguments) is resolution
    assert calls == [(config, selection)]
    assert writes == [(output_path, resolution)]
