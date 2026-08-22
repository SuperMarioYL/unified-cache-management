"""Task 4A2 RED contracts for exact dependency resolution."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import sys
from collections.abc import Callable, Mapping
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
from packaging.utils import canonicalize_name, parse_wheel_filename

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ROOT = REPO_ROOT / ".github" / "release"
SELECTION_FIXTURE = RELEASE_ROOT / "tests" / "fixtures" / "task4-candidate-input.json"
PEP691_FIXTURE_DIR = RELEASE_ROOT / "tests" / "fixtures" / "task4-pypi-simple"
PYPI_SIMPLE_INDEX = "https://pypi.org/simple/"
PEP691_MEDIA_TYPE = "application/vnd.pypi.simple.v1+json"
EXPECTED_CP314T_FILENAMES = {
    "packaging": "packaging-24.2-cp314-cp314t-manylinux_2_28_x86_64.whl",
    "pyyaml": "PyYAML-6.0.2-cp314-cp314t-manylinux_2_28_x86_64.whl",
    "wrapt": "wrapt-1.17.2-cp314-cp314t-manylinux_2_28_x86_64.whl",
}
COMPATIBLE_CP314T_PACKAGING_2_24_FILENAME = (
    "packaging-24.2-cp314-cp314t-manylinux_2_24_x86_64.whl"
)
COMPATIBLE_CP314T_PACKAGING_2_17_COMPRESSED_FILENAME = (
    "packaging-24.2-cp314-cp314t-" "manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
)
COMPATIBLE_CP314T_PACKAGING_2014_ALIAS_FILENAME = (
    "packaging-24.2-cp314-cp314t-manylinux2014_x86_64.whl"
)
EXPECTED_CP316T_FILENAMES = {
    "packaging": "packaging-24.2-cp316-cp316t-manylinux_2_28_x86_64.whl",
    "pyyaml": "PyYAML-6.0.2-cp316-cp316t-manylinux_2_28_x86_64.whl",
    "wrapt": "wrapt-1.17.2-cp316-cp316t-manylinux_2_28_x86_64.whl",
}
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
PEP691_REQUIRED_FIELDS = {"meta", "name", "files"}
PEP691_REQUIRED_FILE_FIELDS = {"filename", "url", "hashes"}


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


def _pep691_responses() -> dict[str, dict[str, Any]]:
    responses = {}
    for path in sorted(PEP691_FIXTURE_DIR.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(value, dict)
        name = canonicalize_name(value["name"])
        assert name not in responses
        responses[name] = value
    return responses


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


def _http_json_reader(
    responses: Mapping[str, Mapping[str, Any]],
    calls: list[tuple[str, dict[str, str]]] | None = None,
) -> Callable[..., Mapping[str, Any]]:
    def read_json(url: str, *, headers: Mapping[str, str]) -> Mapping[str, Any]:
        assert url.startswith(PYPI_SIMPLE_INDEX)
        assert url.endswith("/")
        assert headers["Accept"] == PEP691_MEDIA_TYPE
        if calls is not None:
            calls.append((url, dict(headers)))
        project = url.removeprefix(PYPI_SIMPLE_INDEX).removesuffix("/")
        assert canonicalize_name(project) == project
        return copy.deepcopy(responses[project])

    return read_json


def _resolve(
    selection: dict[str, Any],
    responses: dict[str, dict[str, Any]] | None = None,
    *,
    config: dict[str, Any] | None = None,
    http_json_read: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    resolve = _public_callable(_dependencies(), "resolve_dependency_resolution")
    result = resolve(
        copy.deepcopy(config or _config()),
        copy.deepcopy(selection),
        http_json_read=http_json_read
        or _http_json_reader(responses or _pep691_responses()),
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


def _future_pep691_responses() -> dict[str, dict[str, Any]]:
    responses = _pep691_responses()
    for project in ("packaging", "pyyaml", "wrapt"):
        exact = next(
            item
            for item in responses[project]["files"]
            if "cp314-cp314t-manylinux_2_28_x86_64" in item["filename"]
        )
        future = copy.deepcopy(exact)
        for field in ("filename", "url"):
            future[field] = future[field].replace("cp314", "cp316")
        future["hashes"]["sha256"] = hashlib.sha256(project.encode()).hexdigest()
        responses[project]["files"].append(future)
    return responses


def _requirement(selection: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return next(
        item
        for item in selection["dependency_requests"][0]["requirements"]
        if item["name"] == name
    )


def _failure_resolution(selection: dict[str, Any], *names: str) -> dict[str, Any]:
    responses = _pep691_responses()
    for name in names or ("pyyaml",):
        responses[name]["files"] = []
    result = _resolve(selection, responses)
    assert result["requests"][0]["status"] == "failure"
    return result


def _compressed_older_packaging_resolution() -> tuple[dict[str, Any], dict[str, Any]]:
    selection, _ = _selection()
    responses = _pep691_responses()
    allowed_filenames = {
        COMPATIBLE_CP314T_PACKAGING_2_17_COMPRESSED_FILENAME,
        "packaging-24.2-py314-none-manylinux_2_28_x86_64.whl",
        "packaging-24.2-py3-none-any.whl",
    }
    responses["packaging"]["files"] = [
        item
        for item in responses["packaging"]["files"]
        if item["filename"] in allowed_filenames
    ]
    return selection, _resolve(selection, responses)


def test_task4_dependency_fixture_is_raw_pep691_project_json() -> None:
    responses = _pep691_responses()

    assert set(responses) == set(EXPECTED_CP314T_FILENAMES)
    for project, response in responses.items():
        assert PEP691_REQUIRED_FIELDS <= set(response)
        assert response["meta"] == {"api-version": "1.0"}
        assert canonicalize_name(response["name"]) == project
        assert "versions" not in response
        assert response["files"]
        for file_record in response["files"]:
            assert PEP691_REQUIRED_FILE_FIELDS <= set(file_record)
            assert file_record["url"].startswith("https://")
            sha256 = file_record["hashes"]["sha256"]
            assert len(sha256) == 64
            assert set(sha256) <= set("0123456789abcdef")
            requires_python = file_record.get("requires-python")
            assert requires_python is None or isinstance(requires_python, str)
            if file_record["filename"].endswith(".whl"):
                name, _, _, _ = parse_wheel_filename(file_record["filename"])
                assert canonicalize_name(name) == project
        assert EXPECTED_CP314T_FILENAMES[project] in {
            item["filename"] for item in response["files"]
        }


def test_dependency_resolution_public_seams_are_owned_by_dependencies_module() -> None:
    module = _dependencies()

    _public_callable(module, "resolve_dependency_resolution")
    _public_callable(module, "validate_dependency_resolution")
    assert not hasattr(products, "resolve_dependency_resolution")


def test_resolver_calls_public_selection_validator_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency_module = _dependencies()
    selection, _ = _selection()
    events: list[str] = []

    class HttpReadReached(Exception):
        pass

    def validate(value: object) -> object:
        events.append("selection-validator")
        assert value == selection
        return copy.deepcopy(value)

    def read_json(url: str, *, headers: Mapping[str, str]) -> Mapping[str, Any]:
        events.append("http-read")
        raise HttpReadReached

    if hasattr(dependency_module, "validate_candidate_selection"):
        assert (
            dependency_module.validate_candidate_selection
            is products.validate_candidate_selection
        )
        monkeypatch.setattr(dependency_module, "validate_candidate_selection", validate)
    else:
        monkeypatch.setattr(products, "validate_candidate_selection", validate)
        monkeypatch.setattr(dependency_module, "products", products, raising=False)

    with pytest.raises(HttpReadReached):
        _resolve(selection, http_json_read=read_json)
    assert events == ["selection-validator", "http-read"]


def test_products_never_imports_dependencies() -> None:
    product_tree = ast.parse(Path(products.__file__).read_text(encoding="utf-8"))

    assert not any(
        (
            isinstance(node, ast.ImportFrom)
            and (
                (node.module or "").endswith("dependencies")
                or any(item.name == "dependencies" for item in node.names)
            )
        )
        or (
            isinstance(node, ast.Import)
            and any(item.name.endswith(".dependencies") for item in node.names)
        )
        for node in ast.walk(product_tree)
    )


def test_dependency_resolver_does_not_use_host_sys_tags() -> None:
    dependency_module = _dependencies()
    dependency_tree = ast.parse(
        Path(dependency_module.__file__).read_text(encoding="utf-8")
    )

    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "packaging.tags"
        and any(item.name in {"sys_tags", "*"} for item in node.names)
        for node in ast.walk(dependency_tree)
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "sys_tags"
        for node in ast.walk(dependency_tree)
    )


def test_resolver_rejects_resealed_invalid_selection_before_index_read() -> None:
    selection, _ = _selection()
    request = selection["dependency_requests"][0]
    request["coordinate"]["manylinux"] = "manylinux_2_31"
    request["request_id"] = _canonical_digest(
        {
            "coordinate": request["coordinate"],
            "requirements": request["requirements"],
        }
    )
    _reseal(selection, "selection_sha256")
    reads: list[tuple[str, dict[str, str]]] = []

    def reader(url: str, *, headers: Mapping[str, str]) -> Mapping[str, Any]:
        reads.append((url, dict(headers)))
        raise AssertionError("index reader must not run after invalid Selection")

    with pytest.raises(ValueError):
        _resolve(selection, http_json_read=reader)
    assert reads == []


def test_resolver_rejects_config_digest_drift_before_index_read() -> None:
    selection, _ = _selection()
    config = _config()
    config["image_revision"] += 1
    reads: list[tuple[str, dict[str, str]]] = []

    def reader(url: str, *, headers: Mapping[str, str]) -> Mapping[str, Any]:
        reads.append((url, dict(headers)))
        raise AssertionError("index reader must not run after config identity drift")

    with pytest.raises(ValueError, match="config|digest"):
        _resolve(selection, config=config, http_json_read=reader)
    assert reads == []


def test_resolver_rejects_request_fanout_limit_before_index_read() -> None:
    selection, _ = _selection(_future_abi_fixture())
    config = _config()
    request_count = len(selection["dependency_requests"])
    assert request_count > 1
    config["discovery"]["matrix_limits"]["max_wheel_tasks"] = request_count - 1
    selection["config_sha256"] = _canonical_digest(config)
    _reseal(selection, "selection_sha256")
    reads: list[tuple[str, dict[str, str]]] = []

    def reader(url: str, *, headers: Mapping[str, str]) -> Mapping[str, Any]:
        reads.append((url, dict(headers)))
        raise AssertionError("index reader must not run after request limit overflow")

    with pytest.raises(ValueError, match="max_wheel_tasks|request limit"):
        _resolve(selection, config=config, http_json_read=reader)
    assert reads == []


def test_dependency_resolution_is_closed_exact_and_self_digesting() -> None:
    selection, _ = _selection()
    responses = _pep691_responses()
    calls: list[tuple[str, dict[str, str]]] = []
    resolution = _resolve(
        selection,
        responses,
        http_json_read=_http_json_reader(responses, calls),
    )
    validate = _public_callable(_dependencies(), "validate_dependency_resolution")

    assert set(resolution) == RESOLUTION_FIELDS
    assert resolution["kind"] == "ucm-dependency-resolution"
    assert resolution["schema_version"] == 3
    assert resolution["index_url"] == PYPI_SIMPLE_INDEX
    for field in ("source_sha", "config_sha256", "catalog_sha256", "selection_sha256"):
        assert resolution[field] == selection[field]
    assert resolution["resolution_sha256"] == _canonical_digest(
        {key: value for key, value in resolution.items() if key != "resolution_sha256"}
    )
    assert (
        validate(copy.deepcopy(resolution), _config(), copy.deepcopy(selection))
        == resolution
    )
    assert resolution["requests"] == sorted(
        resolution["requests"], key=lambda item: item["request_id"]
    )
    assert [item["request_id"] for item in resolution["requests"]] == [
        item["request_id"] for item in selection["dependency_requests"]
    ]
    expected_projects = [
        item["name"] for item in selection["dependency_requests"][0]["requirements"]
    ]
    expected_calls = {
        (f"{PYPI_SIMPLE_INDEX}{project}/", PEP691_MEDIA_TYPE)
        for project in expected_projects
    }
    actual_calls = {(url, headers["Accept"]) for url, headers in calls}
    assert actual_calls == expected_calls
    assert len(calls) == len(actual_calls)
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
        assert {item["requirement_id"] for item in request["resolved"]} == {
            item["requirement_id"] for item in request["requirements"]
        }
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
            assert resolved["filename"] == EXPECTED_CP314T_FILENAMES[resolved["name"]]
            assert resolved["wheel_tags"] == sorted(set(resolved["wheel_tags"]))
            source_record = next(
                item
                for item in responses[resolved["name"]]["files"]
                if item["filename"] == resolved["filename"]
            )
            assert resolved["url"] == source_record["url"]
            assert resolved["sha256"] == ("sha256:" + source_record["hashes"]["sha256"])
            assert resolved["requires_python"] == source_record.get("requires-python")


def test_pep691_extensions_are_ignored_and_requires_python_is_optional() -> None:
    selection, _ = _selection()
    responses = _pep691_responses()
    response = responses["wrapt"]
    response["meta"]["extension-meta"] = {"version": 1}
    response["extension-project"] = {"opaque": True}
    selected_file = next(
        item
        for item in response["files"]
        if item["filename"] == EXPECTED_CP314T_FILENAMES["wrapt"]
    )
    assert "requires-python" not in selected_file
    selected_file["extension-file"] = ["opaque"]

    resolution = _resolve(selection, responses)
    resolved = next(
        item
        for item in resolution["requests"][0]["resolved"]
        if item["name"] == "wrapt"
    )

    assert resolved["filename"] == EXPECTED_CP314T_FILENAMES["wrapt"]
    assert resolved["url"] == selected_file["url"]
    assert resolved["sha256"] == ("sha256:" + selected_file["hashes"]["sha256"])
    assert resolved["requires_python"] is None


def test_dependency_resolution_uses_foreign_target_tag_rank_not_file_order() -> None:
    selection, _ = _selection()
    base = _pep691_responses()
    ranked_filenames = {
        EXPECTED_CP314T_FILENAMES["packaging"],
        COMPATIBLE_CP314T_PACKAGING_2_24_FILENAME,
        COMPATIBLE_CP314T_PACKAGING_2_17_COMPRESSED_FILENAME,
        "packaging-24.2-py314-none-manylinux_2_28_x86_64.whl",
        "packaging-24.2-py3-none-any.whl",
    }
    ranked = [
        item
        for item in base["packaging"]["files"]
        if item["filename"] in ranked_filenames
    ]
    assert {item["filename"] for item in ranked} == ranked_filenames

    canonical_resolution: dict[str, Any] | None = None
    for ordered in permutations(ranked):
        responses = copy.deepcopy(base)
        responses["packaging"]["files"] = list(ordered)
        resolution = _resolve(selection, responses)
        selected = next(
            item
            for item in resolution["requests"][0]["resolved"]
            if item["name"] == "packaging"
        )
        assert selected["filename"] == EXPECTED_CP314T_FILENAMES["packaging"]
        if canonical_resolution is None:
            canonical_resolution = resolution
        else:
            assert resolution == canonical_resolution


def test_dependency_resolution_prefers_highest_compatible_older_manylinux_floor() -> (
    None
):
    selection, _ = _selection()
    base = _pep691_responses()
    ranked_filenames = {
        COMPATIBLE_CP314T_PACKAGING_2_24_FILENAME,
        COMPATIBLE_CP314T_PACKAGING_2_17_COMPRESSED_FILENAME,
        "packaging-24.2-py314-none-manylinux_2_28_x86_64.whl",
        "packaging-24.2-py3-none-any.whl",
    }
    ranked = [
        item
        for item in base["packaging"]["files"]
        if item["filename"] in ranked_filenames
    ]
    assert {item["filename"] for item in ranked} == ranked_filenames

    canonical_resolution: dict[str, Any] | None = None
    for ordered in permutations(ranked):
        responses = copy.deepcopy(base)
        responses["packaging"]["files"] = list(ordered)
        resolution = _resolve(selection, responses)
        selected = next(
            item
            for item in resolution["requests"][0]["resolved"]
            if item["name"] == "packaging"
        )
        assert selected["filename"] == COMPATIBLE_CP314T_PACKAGING_2_24_FILENAME
        assert selected["wheel_tags"] == [
            "cp314-cp314t-manylinux_2_24_x86_64",
        ]
        if canonical_resolution is None:
            canonical_resolution = resolution
        else:
            assert resolution == canonical_resolution


def test_dependency_resolution_selects_middle_rank_without_exact_native() -> None:
    selection, _ = _selection()
    responses = _pep691_responses()
    responses["packaging"]["files"] = [
        item
        for item in responses["packaging"]["files"]
        if item["filename"]
        not in {
            EXPECTED_CP314T_FILENAMES["packaging"],
            COMPATIBLE_CP314T_PACKAGING_2_24_FILENAME,
            COMPATIBLE_CP314T_PACKAGING_2_17_COMPRESSED_FILENAME,
            COMPATIBLE_CP314T_PACKAGING_2014_ALIAS_FILENAME,
        }
    ]

    resolution = _resolve(selection, responses)
    selected = next(
        item
        for item in resolution["requests"][0]["resolved"]
        if item["name"] == "packaging"
    )

    assert selected["filename"] == (
        "packaging-24.2-py314-none-manylinux_2_28_x86_64.whl"
    )


def test_dependency_resolution_rejects_same_highest_wheel_tag_rank_tie() -> None:
    selection, _ = _selection()
    responses = _pep691_responses()
    exact = next(
        item
        for item in responses["pyyaml"]["files"]
        if item["filename"] == EXPECTED_CP314T_FILENAMES["pyyaml"]
    )
    tied = copy.deepcopy(exact)
    tied["filename"] = tied["filename"].replace("-cp314-", "-1-cp314-")
    tied["url"] = tied["url"].replace("-cp314-", "-1-cp314-")
    tied["hashes"]["sha256"] = "c" * 64
    responses["pyyaml"]["files"].append(tied)

    with pytest.raises(ValueError, match="ambiguous|highest|rank"):
        _resolve(selection, responses)


@pytest.mark.parametrize(
    "files",
    [
        pytest.param(
            lambda response: [
                item
                for item in response["files"]
                if item["filename"].endswith(".tar.gz")
            ],
            id="no-sdist",
        ),
        pytest.param(
            lambda response: [
                item
                for item in response["files"]
                if item["filename"].startswith("packaging-24.1-")
            ],
            id="no-version-fallback",
        ),
        pytest.param(
            lambda response: [
                item
                for item in response["files"]
                if "manylinux_2_28_aarch64" in item["filename"]
            ],
            id="no-environment-fallback",
        ),
    ],
)
def test_dependency_resolution_fails_without_compatible_exact_binary(
    files: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    selection, _ = _selection()
    responses = _pep691_responses()
    responses["packaging"]["files"] = files(responses["packaging"])

    resolution = _resolve(selection, responses)
    request = resolution["requests"][0]
    requirement = _requirement(selection, "packaging")

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
        "response-name",
        "filename-name",
        "filename-version",
        "http-url",
        "relative-url",
        "missing-sha256",
        "malformed-sha256",
        "malformed-requires-python",
    ],
)
def test_resolver_rejects_malformed_exact_file_evidence(mutation: str) -> None:
    selection, _ = _selection()
    responses = _pep691_responses()
    record = copy.deepcopy(
        next(
            item
            for item in responses["packaging"]["files"]
            if item["filename"] == EXPECTED_CP314T_FILENAMES["packaging"]
        )
    )
    responses["packaging"]["files"] = [record]
    if mutation == "response-name":
        responses["packaging"]["name"] = "other-project"
    elif mutation == "filename-name":
        record["filename"] = "other_project-24.2-py3-none-any.whl"
        record["url"] = f"https://files.example.invalid/{record['filename']}"
    elif mutation == "filename-version":
        record["filename"] = "packaging-24.3-py3-none-any.whl"
        record["url"] = f"https://files.example.invalid/{record['filename']}"
    elif mutation == "http-url":
        record["url"] = record["url"].replace("https://", "http://")
    elif mutation == "relative-url":
        record["url"] = f"files/{record['filename']}"
    elif mutation == "missing-sha256":
        record["hashes"].pop("sha256")
    elif mutation == "malformed-sha256":
        record["hashes"]["sha256"] = "g" * 64
    else:
        record["requires-python"] = "not a specifier"

    resolution = _resolve(selection, responses)
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
    responses = _pep691_responses()
    exact = next(
        item
        for item in responses["pyyaml"]["files"]
        if item["filename"] == EXPECTED_CP314T_FILENAMES["pyyaml"]
    )
    exact["requires-python"] = "<3.14"

    resolution = _resolve(selection, responses)
    resolved = next(
        item
        for item in resolution["requests"][0]["resolved"]
        if item["name"] == "pyyaml"
    )

    assert resolved["filename"] == "PyYAML-6.0.2-py3-none-any.whl"
    assert resolved["requires_python"] == ">=3.8"


def test_requires_python_excludes_all_compatible_files() -> None:
    selection, _ = _selection()
    responses = _pep691_responses()
    generic = next(
        item
        for item in responses["packaging"]["files"]
        if item["filename"] == "packaging-24.2-py3-none-any.whl"
    )
    generic["requires-python"] = "<3.14"
    responses["packaging"]["files"] = [generic]

    resolution = _resolve(selection, responses)
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


def test_failed_request_freezes_exact_sorted_multi_failure_set() -> None:
    selection, _ = _selection()
    resolution = _failure_resolution(selection, "pyyaml", "wrapt")
    request = resolution["requests"][0]
    expected = sorted(
        [
            {"code": "binary-wheel-unavailable", **_requirement(selection, name)}
            for name in ("pyyaml", "wrapt")
        ],
        key=lambda item: item["requirement_id"],
    )

    assert request["status"] == "failure"
    assert request["resolved"] == []
    assert request["failures"] == expected
    validate = _public_callable(_dependencies(), "validate_dependency_resolution")
    assert validate(resolution, _config(), selection) == resolution


def test_dependency_resolution_grows_for_future_free_threaded_abi() -> None:
    selection, _ = _selection(_future_abi_fixture())
    responses = _future_pep691_responses()
    resolution = _resolve(selection, responses)
    validate = _public_callable(_dependencies(), "validate_dependency_resolution")

    assert {
        request["coordinate"]["python_abi"] for request in resolution["requests"]
    } == {"cp314t", "cp316t"}
    for request in resolution["requests"]:
        assert request["coordinate"]["python_tag"] == request["coordinate"][
            "python_abi"
        ].removesuffix("t")
        assert request["status"] == "success"
        assert len(request["resolved"]) == len(request["requirements"])
        assert {item["requirement_id"] for item in request["resolved"]} == {
            item["requirement_id"] for item in request["requirements"]
        }
        expected = (
            EXPECTED_CP316T_FILENAMES
            if request["coordinate"]["python_abi"] == "cp316t"
            else EXPECTED_CP314T_FILENAMES
        )
        assert {
            item["name"]: item["filename"] for item in request["resolved"]
        } == expected
    assert validate(resolution, _config(), selection) == resolution


def test_dependency_resolution_validator_accepts_compressed_older_manylinux_wheel() -> (
    None
):
    selection, resolution = _compressed_older_packaging_resolution()
    validate = _public_callable(_dependencies(), "validate_dependency_resolution")
    resolved = next(
        item
        for item in resolution["requests"][0]["resolved"]
        if item["name"] == "packaging"
    )

    assert resolved["filename"] == COMPATIBLE_CP314T_PACKAGING_2_17_COMPRESSED_FILENAME
    assert resolved["wheel_tags"] == [
        "cp314-cp314t-manylinux2014_x86_64",
        "cp314-cp314t-manylinux_2_17_x86_64",
    ]
    assert validate(copy.deepcopy(resolution), _config(), selection) == resolution


def test_dependency_resolution_accepts_legacy_manylinux2014_alias_without_modern_tag() -> (
    None
):
    selection, _ = _selection()
    responses = _pep691_responses()
    allowed_filenames = {
        COMPATIBLE_CP314T_PACKAGING_2014_ALIAS_FILENAME,
        "packaging-24.2-py314-none-manylinux_2_28_x86_64.whl",
        "packaging-24.2-py3-none-any.whl",
    }
    responses["packaging"]["files"] = [
        item
        for item in responses["packaging"]["files"]
        if item["filename"] in allowed_filenames
    ]
    resolution = _resolve(selection, responses)
    validate = _public_callable(_dependencies(), "validate_dependency_resolution")
    resolved = next(
        item
        for item in resolution["requests"][0]["resolved"]
        if item["name"] == "packaging"
    )

    assert resolved["filename"] == COMPATIBLE_CP314T_PACKAGING_2014_ALIAS_FILENAME
    assert resolved["wheel_tags"] == ["cp314-cp314t-manylinux2014_x86_64"]
    assert validate(copy.deepcopy(resolution), _config(), selection) == resolution


@pytest.mark.parametrize(
    "incompatible_platform",
    [
        "manylinux_2_31_x86_64",
        "manylinux_2_17_aarch64.manylinux2014_aarch64",
    ],
)
def test_dependency_resolution_validator_rejects_incompatible_manylinux_platform(
    incompatible_platform: str,
) -> None:
    selection, resolution = _compressed_older_packaging_resolution()
    validate = _public_callable(_dependencies(), "validate_dependency_resolution")
    resolved = next(
        item
        for item in resolution["requests"][0]["resolved"]
        if item["name"] == "packaging"
    )
    assert resolved["filename"] == COMPATIBLE_CP314T_PACKAGING_2_17_COMPRESSED_FILENAME
    resolved["filename"] = f"packaging-24.2-cp314-cp314t-{incompatible_platform}.whl"
    resolved["url"] = "https://files.example.invalid/packaging/" + resolved["filename"]
    _, _, _, wheel_tags = parse_wheel_filename(resolved["filename"])
    resolved["wheel_tags"] = sorted(str(tag) for tag in wheel_tags)
    _reseal(resolution, "resolution_sha256")

    with pytest.raises(ValueError):
        validate(resolution, _config(), selection)


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
        "kind",
        "schema-version",
        "missing-request",
        "duplicate-request",
        "unexpected-request",
        "request-order",
        "request-missing-field",
        "request-extra-field",
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
        "resolved-coherent-incompatible-wheel",
        "resolved-requires-python-incompatible",
        "resolved-requires-python-invalid",
        "resolved-missing-field",
        "resolved-extra-field",
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
        resolution = _resolve(selection, _future_pep691_responses())
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
    elif mutation == "kind":
        resolution["kind"] = "not-a-resolution"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "schema-version":
        resolution["schema_version"] = 4
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
    elif mutation == "request-missing-field":
        request.pop("coordinate")
        _reseal(resolution, "resolution_sha256")
    elif mutation == "request-extra-field":
        request["extra"] = None
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
    elif mutation == "resolved-coherent-incompatible-wheel":
        resolved = next(
            item for item in request["resolved"] if item["name"] == "packaging"
        )
        resolved["filename"] = "packaging-24.2-cp314-cp314t-manylinux_2_28_aarch64.whl"
        resolved["url"] = (
            "https://files.example.invalid/packaging/" + resolved["filename"]
        )
        resolved["wheel_tags"] = ["cp314-cp314t-manylinux_2_28_aarch64"]
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-requires-python-incompatible":
        request["resolved"][0]["requires_python"] = "<3.14"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-requires-python-invalid":
        request["resolved"][0]["requires_python"] = "not a specifier"
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-missing-field":
        request["resolved"][0].pop("url")
        _reseal(resolution, "resolution_sha256")
    elif mutation == "resolved-extra-field":
        request["resolved"][0]["extra"] = None
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
    "mutation",
    [
        "empty-failures",
        "duplicate-failure",
        "unexpected-failure",
        "failure-order",
        "bad-code",
        "coherent-identity-drift",
        "failure-with-resolved",
        "failure-extra-field",
    ],
)
def test_dependency_resolution_validator_rejects_multi_failure_closure_drift(
    mutation: str,
) -> None:
    selection, _ = _selection()
    resolution = _failure_resolution(selection, "pyyaml", "wrapt")
    request = resolution["requests"][0]
    if mutation == "empty-failures":
        request["failures"] = []
    elif mutation == "duplicate-failure":
        request["failures"].append(copy.deepcopy(request["failures"][0]))
    elif mutation == "unexpected-failure":
        identity = {
            "scope": "runtime",
            "name": "other-project",
            "version": "1.0",
        }
        request["failures"].append(
            {
                "code": "binary-wheel-unavailable",
                "requirement_id": _canonical_digest(identity),
                **identity,
            }
        )
        request["failures"].sort(key=lambda item: item["requirement_id"])
    elif mutation == "failure-order":
        request["failures"].reverse()
    elif mutation == "bad-code":
        request["failures"][0]["code"] = "temporary-error"
    elif mutation == "coherent-identity-drift":
        failure = request["failures"][0]
        identity = {
            "scope": failure["scope"],
            "name": "other-project",
            "version": failure["version"],
        }
        failure.update(identity, requirement_id=_canonical_digest(identity))
        request["failures"].sort(key=lambda item: item["requirement_id"])
    elif mutation == "failure-with-resolved":
        successful = _resolve(selection)["requests"][0]["resolved"][0]
        request["resolved"] = [successful]
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
