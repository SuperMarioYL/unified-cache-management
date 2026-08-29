from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = DOCS_ROOT / "tools" / "pages.py"
SPEC = importlib.util.spec_from_file_location("ucm_docs_pages", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pages = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pages
SPEC.loader.exec_module(pages)


def _wheel_record(
    *, channel: str, version: str, filename: str, url: str, sha256: str
) -> dict[str, object]:
    return {
        "id": filename.removesuffix(".whl"),
        "product": "vllm",
        "channel": channel,
        "accelerator": {
            "runtime": "cuda-13.0" if channel.startswith("cu") else "cann-9.0.1",
            "variant": "cuda" if channel.startswith("cu") else "a2",
            "soc_version": "cuda" if channel.startswith("cu") else "Ascend910B",
        },
        "distribution": "uc-manager",
        "version": version,
        "python_abi": "py3",
        "architecture": "any",
        "filename": filename,
        "url": url,
        "sha256": sha256,
        "dependencies": ["wrapt==1.17.2"],
    }


def _manifest_structure(wheels: list[dict[str, object]], version: str) -> dict:
    manifest = {
        "kind": "ucm-release-manifest",
        "schema_version": 8,
        "release": {
            "tag": f"v{version}",
            "type": "stable",
            "version": version,
            "url": f"https://github.com/example/ucm/releases/tag/v{version}",
            "actions_run_id": 33087700398,
        },
        "wheels": wheels,
        "images": [
            {
                "id": "vllm-v1-cu130",
                "product": "vllm",
                "upstream": {"version": "1.0.0", "channel": "stable"},
                "accelerator": {
                    "runtime": "cuda-13.0",
                    "variant": "cuda",
                    "soc_version": "cuda",
                },
                "os": {"id": "ubuntu", "version": "24.04"},
                "publications": {
                    "ghcr": {
                        "pull": "ghcr.io/example/vllm:v1-ucm-0.9.0",
                        "multi_arch": True,
                        "members": [
                            {
                                "architecture": "amd64",
                                "reference": "ghcr.io/example/vllm:v1-amd64-ucm-0.9.0",
                            },
                            {
                                "architecture": "arm64",
                                "reference": "ghcr.io/example/vllm:v1-arm64-ucm-0.9.0",
                            },
                        ],
                    },
                    "dockerhub": None,
                },
            }
        ],
        "chart": {
            "name": "unified-cache-chart",
            "version": version,
            "filename": f"unified-cache-chart-{version}.tgz",
            "url": f"https://github.com/example/ucm/releases/download/v{version}/chart.tgz",
            "oci": f"ghcr.io/example/charts/unified-cache-chart:{version}",
        },
        "github_release_assets": [
            "release-manifest.json",
            f"unified-cache-chart-{version}.tgz",
            *(str(wheel["filename"]) for wheel in wheels),
        ],
    }
    return manifest


def _manifest_v8(wheels: list[dict[str, object]], version: str = "0.9.3") -> dict:
    manifest = _manifest_structure([], version)
    if not wheels:
        wheels = [
            _wheel_record(
                channel="cu130",
                version=version,
                filename="placeholder.whl",
                url="https://example.invalid/placeholder.whl",
                sha256="a" * 64,
            )
        ]
    extras: dict[str, str] = {}
    backend_wheels: list[dict[str, object]] = []
    for raw_wheel in wheels:
        wheel = json.loads(json.dumps(raw_wheel))
        extra = str(wheel.pop("channel"))
        distribution = (
            f"uc-manager-cuda-{extra}"
            if extra.startswith("cu")
            else f"uc-manager-{extra}"
        )
        platform_tag = (
            "manylinux_2_28_x86_64"
            if extra.startswith("cu")
            else "manylinux_2_34_x86_64"
        )
        filename = (
            f"{distribution.replace('-', '_')}-{version}-"
            f"cp312-cp312-{platform_tag}.whl"
        )
        wheel.update(
            {
                "extra": extra,
                "distribution": distribution,
                "version": version,
                "python_abi": "cp312",
                "architecture": "amd64",
                "filename": filename,
                "url": (
                    "https://github.com/example/ucm/releases/download/"
                    f"v{version}/{filename}"
                ),
                "platform_tags": [platform_tag],
            }
        )
        extras[extra] = distribution
        backend_wheels.append(wheel)
    manifest["python"] = {
        "distribution": "uc-manager",
        "version": version,
        "filename": f"uc_manager-{version}-py3-none-any.whl",
        "url": (
            "https://github.com/example/ucm/releases/download/"
            f"v{version}/uc_manager-{version}-py3-none-any.whl"
        ),
        "sha256": "b" * 64,
        "tags": ["py3-none-any"],
        "extras": extras,
        "pypi": {
            "index_url": "https://pypi.org/simple",
            "project_url": f"https://pypi.org/project/uc-manager/{version}/",
        },
    }
    manifest["wheels"] = backend_wheels
    manifest["github_release_assets"] = sorted(
        [
            "release-manifest.json",
            "pypi-receipt.json",
            manifest["chart"]["filename"],
            manifest["python"]["filename"],
            *(str(wheel["filename"]) for wheel in backend_wheels),
        ]
    )
    return manifest


def _manifest(wheels: list[dict[str, object]], version: str = "0.9.0") -> dict:
    return _manifest_v8(wheels, version)


def _freeze(root: Path, manifest: dict) -> None:
    path = root / "manifests" / manifest["release"]["version"] / "release-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_manifest_validation_accepts_only_schema_8() -> None:
    manifest = _manifest([])
    assert pages.validate_manifest(manifest) == manifest

    for unsupported_schema in (6, 7):
        unsupported = json.loads(json.dumps(manifest))
        unsupported["schema_version"] = unsupported_schema
        with pytest.raises(pages.PagesError, match="schema_version must be 8"):
            pages.validate_manifest(unsupported)

    extra_field = json.loads(json.dumps(manifest))
    extra_field["release"]["commit"] = "a8c0d7ef"
    with pytest.raises(pages.PagesError, match="fields differ"):
        pages.validate_manifest(extra_field)

    missing_self_asset = json.loads(json.dumps(manifest))
    missing_self_asset["github_release_assets"].remove("release-manifest.json")
    with pytest.raises(pages.PagesError, match="must list itself"):
        pages.validate_manifest(missing_self_asset)

    legacy_asset = json.loads(json.dumps(manifest))
    legacy_asset["github_release_assets"].append("install-catalog.json")
    with pytest.raises(pages.PagesError, match="must not list"):
        pages.validate_manifest(legacy_asset)

    missing_chart = json.loads(json.dumps(manifest))
    missing_chart["github_release_assets"].remove("unified-cache-chart-0.9.0.tgz")
    with pytest.raises(pages.PagesError, match="assets are missing"):
        pages.validate_manifest(missing_chart)


def test_schema_8_manifest_validation_is_exact() -> None:
    wheel = _wheel_record(
        channel="cu130",
        version="0.9.3",
        filename="uc_manager_cuda_cu130-0.9.3-cp312-manylinux_x86_64.whl",
        url=(
            "https://github.com/example/ucm/releases/download/v0.9.3/"
            "uc_manager_cuda_cu130.whl"
        ),
        sha256="a" * 64,
    )
    manifest = _manifest_v8([wheel])
    assert pages.validate_manifest(manifest) == manifest

    fork = json.loads(
        json.dumps(manifest)
        .replace("uc-manager", "supermarioyl-uc-manager")
        .replace("uc_manager", "supermarioyl_uc_manager")
        .replace("https://pypi.org/", "https://test.pypi.org/")
    )
    assert pages.validate_manifest(fork) == fork

    wrong_backend = json.loads(json.dumps(manifest))
    wrong_backend["wheels"][0]["distribution"] = "uc-manager-cuda-wrong"
    with pytest.raises(pages.PagesError, match="declared Python extra"):
        pages.validate_manifest(wrong_backend)

    missing_meta = json.loads(json.dumps(manifest))
    missing_meta["github_release_assets"].remove(missing_meta["python"]["filename"])
    with pytest.raises(pages.PagesError, match="assets are missing"):
        pages.validate_manifest(missing_meta)

    missing_receipt = json.loads(json.dumps(manifest))
    missing_receipt["github_release_assets"].remove("pypi-receipt.json")
    with pytest.raises(pages.PagesError, match="PyPI receipt"):
        pages.validate_manifest(missing_receipt)

    wrong_project = json.loads(json.dumps(manifest))
    wrong_project["python"]["pypi"][
        "project_url"
    ] = "https://pypi.org/project/uc-manager/99.0/"
    with pytest.raises(pages.PagesError, match="PyPI URLs"):
        pages.validate_manifest(wrong_project)

    empty_platform = json.loads(json.dumps(manifest))
    empty_platform["wheels"][0]["platform_tags"] = []
    with pytest.raises(pages.PagesError, match="must not be empty"):
        pages.validate_manifest(empty_platform)

    mismatched_platform = json.loads(json.dumps(manifest))
    mismatched_platform["wheels"][0]["platform_tags"] = ["manylinux_2_34_x86_64"]
    with pytest.raises(pages.PagesError, match="filename and platform"):
        pages.validate_manifest(mismatched_platform)


def test_schema_8_publication_validation_distinguishes_index_and_member() -> None:
    manifest = _manifest([])
    publication = manifest["images"][0]["publications"]["ghcr"]
    assert publication is not None
    publication["multi_arch"] = False
    with pytest.raises(pages.PagesError, match="single-architecture"):
        pages.validate_manifest(manifest)

    publication["members"] = [publication["members"][0]]
    publication["pull"] = publication["members"][0]["reference"]
    assert pages.validate_manifest(manifest) == manifest


def test_latest_uses_current_stable_frozen_manifest(tmp_path: Path) -> None:
    manifest = _manifest([])
    _freeze(tmp_path, manifest)
    (tmp_path / "latest").mkdir()
    (tmp_path / "versions.json").write_text(
        json.dumps(
            [
                {"version": "latest", "title": "latest", "aliases": []},
                {"version": "0.9.0", "title": "0.9.0", "aliases": ["stable"]},
            ]
        ),
        encoding="utf-8",
    )

    assert pages.inject_latest_manifest(tmp_path) == "0.9.0"
    injected = json.loads(
        (tmp_path / "latest" / "release-manifest.json").read_text(encoding="utf-8")
    )
    assert injected == manifest


def test_manifest_sync_does_not_create_latest_without_documentation(
    tmp_path: Path,
) -> None:
    manifest = _manifest([])
    _freeze(tmp_path, manifest)
    (tmp_path / "versions.json").write_text(
        json.dumps([{"version": "0.9.0", "title": "0.9.0", "aliases": ["stable"]}]),
        encoding="utf-8",
    )

    assert pages.inject_latest_manifest(tmp_path) is None
    assert not (tmp_path / "latest").exists()


def test_latest_publish_preserves_indexes_and_pushes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest([])
    _freeze(tmp_path, manifest)
    (tmp_path / "latest").mkdir()
    (tmp_path / "versions.json").write_text(
        json.dumps(
            [
                {"version": "latest", "aliases": []},
                {"version": "0.9.0", "aliases": ["stable"]},
            ]
        ),
        encoding="utf-8",
    )
    existing_index = tmp_path / "whl" / "cu130" / "index.html"
    existing_index.parent.mkdir(parents=True)
    existing_index.write_text("frozen", encoding="utf-8")
    mike_calls: list[list[str]] = []
    pushes: list[bool] = []

    @contextmanager
    def fake_worktree(message: str):
        yield tmp_path

    monkeypatch.setattr(pages, "_prepare_pages_branch", lambda repository: None)
    monkeypatch.setattr(
        pages,
        "_branch_versions",
        lambda: [{"version": "0.9.0", "aliases": ["stable"]}],
    )
    monkeypatch.setattr(
        pages,
        "_read_branch_file",
        lambda path: (
            "docs.example\n"
            if path == "CNAME"
            else (
                json.dumps(manifest)
                if path == "manifests/0.9.0/release-manifest.json"
                else None
            )
        ),
    )
    monkeypatch.setattr(
        pages, "_mike", lambda arguments, site_url: mike_calls.append(list(arguments))
    )
    monkeypatch.setattr(pages, "_pages_worktree", fake_worktree)
    monkeypatch.setattr(pages, "_assert_cname_preserved", lambda original: None)
    monkeypatch.setattr(pages, "_push_pages_branch", lambda: pushes.append(True))
    pages.publish_latest("example/ucm")

    assert existing_index.read_text(encoding="utf-8") == "frozen"
    assert len(pushes) == 1
    assert len(mike_calls) == 1
    assert mike_calls[0][:2] == ["deploy", "latest"]
    assert "--push" not in mike_calls[0]


def test_latest_publish_is_noop_without_a_frozen_stable_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "latest" / "install-catalog.json"
    legacy.parent.mkdir()
    legacy.write_text("still public", encoding="utf-8")
    prepares: list[str] = []

    monkeypatch.setattr(
        pages, "_prepare_pages_branch", lambda repository: prepares.append(repository)
    )
    monkeypatch.setattr(
        pages,
        "_branch_versions",
        lambda: [{"version": "0.9.0", "aliases": ["stable"]}],
    )
    monkeypatch.setattr(pages, "_read_branch_file", lambda path: None)
    monkeypatch.setattr(
        pages,
        "_mike",
        lambda *args, **kwargs: pytest.fail("latest body must not be deployed"),
    )
    monkeypatch.setattr(
        pages,
        "_pages_worktree",
        lambda *args, **kwargs: pytest.fail("Pages tree must not be edited"),
    )
    monkeypatch.setattr(
        pages,
        "_push_pages_branch",
        lambda: pytest.fail("gh-pages must not be pushed"),
    )

    pages.publish_latest("example/ucm")

    assert prepares == ["example/ucm"]
    assert legacy.read_text(encoding="utf-8") == "still public"


def test_final_push_is_one_ordinary_branch_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(pages, "_run", fake_run)
    pages._push_pages_branch()
    assert commands == [["git", "push", "origin", "gh-pages:gh-pages"]]


def test_pages_site_url_prefers_cname_and_normalizes_owner_host() -> None:
    assert pages._site_url("ModelEngine-Group/unified-cache-management", None) == (
        "https://modelengine-group.github.io/unified-cache-management/"
    )
    assert pages._site_url(
        "SuperMarioYL/unified-cache-management", "docs.example\n"
    ) == ("https://docs.example/")


@pytest.mark.parametrize(
    ("versions", "expected"),
    [
        ([], False),
        ([{"version": "0.9.0", "aliases": ["stable"]}], True),
        ([{"version": "0.9.0rc1", "aliases": []}], False),
    ],
)
def test_stable_body_deploy_decision(versions: list[dict], expected: bool) -> None:
    assert pages.version_exists(versions, "0.9.0") is expected


@pytest.mark.parametrize(
    ("version", "tag"),
    [
        ("0.9.0rc1", "v0.9.0rc1"),
        ("0.9.0.dev1", "v0.9.0.dev1"),
        ("0.9.0+cu130", "v0.9.0+cu130"),
        ("0.9.0", "draft/v0.9.0"),
    ],
)
def test_stable_publish_rejects_non_stable_manifest(version: str, tag: str) -> None:
    manifest = _manifest([], version=version)
    manifest["release"]["tag"] = tag
    with pytest.raises(pages.PagesError):
        pages.require_stable_manifest(manifest)


@pytest.mark.parametrize("already_exists", [False, True])
def test_first_and_repeated_stable_publish_mike_decision_and_one_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_exists: bool
) -> None:
    manifest = _manifest([])
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    branch_tree = tmp_path / "branch"
    (branch_tree / "0.9.0").mkdir(parents=True)
    (branch_tree / "latest").mkdir()
    (branch_tree / "catalogs" / "0.9.0").mkdir(parents=True)
    (branch_tree / "catalogs" / "0.9.0" / "install-catalog.json").write_text(
        "legacy", encoding="utf-8"
    )
    (branch_tree / "0.9.0" / "install-catalog.json").write_text(
        "legacy", encoding="utf-8"
    )
    (branch_tree / "latest" / "install-catalog.json").write_text(
        "legacy", encoding="utf-8"
    )
    (branch_tree / "whl" / "cu130").mkdir(parents=True)
    legacy_index = branch_tree / "whl" / "cu130" / "index.html"
    legacy_index.write_text("legacy index", encoding="utf-8")
    (branch_tree / "versions.json").write_text(
        json.dumps([{"version": "0.9.0", "title": "0.9.0", "aliases": ["stable"]}]),
        encoding="utf-8",
    )
    mike_calls: list[list[str]] = []
    pushes: list[bool] = []

    @contextmanager
    def fake_worktree(message: str):
        yield branch_tree

    monkeypatch.setattr(pages, "_prepare_pages_branch", lambda repository: None)
    monkeypatch.setattr(pages, "_read_branch_file", lambda path: "docs.example\n")
    monkeypatch.setattr(
        pages,
        "_branch_versions",
        lambda: (
            [{"version": "0.9.0", "aliases": ["stable"]}] if already_exists else []
        ),
    )
    monkeypatch.setattr(
        pages, "_mike", lambda arguments, site_url: mike_calls.append(list(arguments))
    )
    monkeypatch.setattr(pages, "_pages_worktree", fake_worktree)
    monkeypatch.setattr(pages, "_assert_cname_preserved", lambda original: None)
    monkeypatch.setattr(pages, "_push_pages_branch", lambda: pushes.append(True))

    pages.publish_stable("example/ucm", manifest_path)

    assert len(pushes) == 1
    assert legacy_index.read_text(encoding="utf-8") == "legacy index"
    assert not (branch_tree / "catalogs").exists()
    assert not (branch_tree / "0.9.0" / "install-catalog.json").exists()
    assert not (branch_tree / "latest" / "install-catalog.json").exists()
    assert (
        json.loads(
            (branch_tree / "manifests" / "0.9.0" / "release-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        == manifest
    )
    assert (
        json.loads(
            (branch_tree / "0.9.0" / "release-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        == manifest
    )
    assert (
        json.loads(
            (branch_tree / "latest" / "release-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        == manifest
    )
    if already_exists:
        assert mike_calls == []
    else:
        assert len(mike_calls) == 2
        assert mike_calls[0][:4] == ["deploy", "0.9.0", "stable", "--update-aliases"]
        assert "--push" not in mike_calls[0]
        assert mike_calls[1][:2] == ["set-default", "stable"]
        assert "--push" not in mike_calls[1]


def test_replace_existing_stable_redeploys_body_and_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest([])
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    branch_tree = tmp_path / "branch"
    (branch_tree / "0.9.0").mkdir(parents=True)
    (branch_tree / "latest").mkdir()
    (branch_tree / "versions.json").write_text(
        json.dumps([{"version": "0.9.0", "aliases": ["stable"]}]),
        encoding="utf-8",
    )
    mike_calls: list[list[str]] = []

    @contextmanager
    def fake_worktree(message: str):
        yield branch_tree

    monkeypatch.setattr(pages, "_prepare_pages_branch", lambda repository: None)
    monkeypatch.setattr(pages, "_read_branch_file", lambda path: "docs.example\n")
    monkeypatch.setattr(
        pages,
        "_branch_versions",
        lambda: [{"version": "0.9.0", "aliases": ["stable"]}],
    )
    monkeypatch.setattr(
        pages, "_mike", lambda arguments, site_url: mike_calls.append(list(arguments))
    )
    monkeypatch.setattr(pages, "_pages_worktree", fake_worktree)
    monkeypatch.setattr(pages, "_assert_cname_preserved", lambda original: None)
    monkeypatch.setattr(pages, "_push_pages_branch", lambda: None)

    pages.publish_stable("example/ucm", manifest_path, replace_existing=True)

    assert len(mike_calls) == 2
    assert mike_calls[0][:4] == ["deploy", "0.9.0", "stable", "--update-aliases"]
    assert mike_calls[1][:2] == ["set-default", "stable"]


def test_replace_existing_requires_an_existing_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "release-manifest.json"
    manifest_path.write_text(json.dumps(_manifest([])), encoding="utf-8")
    monkeypatch.setattr(pages, "_prepare_pages_branch", lambda repository: None)
    monkeypatch.setattr(pages, "_read_branch_file", lambda path: "docs.example\n")
    monkeypatch.setattr(pages, "_branch_versions", lambda: [])

    with pytest.raises(pages.PagesError, match="requires an existing Stable"):
        pages.publish_stable("example/ucm", manifest_path, replace_existing=True)


def test_initialize_preserves_cname_and_removes_trial_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "CNAME").write_text("docs.example\n", encoding="utf-8")
    (tmp_path / "old-version").mkdir()
    (tmp_path / "old-version" / "index.html").write_text("old", encoding="utf-8")
    (tmp_path / ".idea").mkdir()
    pushes: list[bool] = []

    @contextmanager
    def fake_worktree(message: str):
        yield tmp_path

    monkeypatch.setattr(pages, "_prepare_pages_branch", lambda repository: None)
    monkeypatch.setattr(pages, "_read_branch_file", lambda path: "docs.example\n")
    monkeypatch.setattr(pages, "_pages_worktree", fake_worktree)
    monkeypatch.setattr(pages, "_assert_cname_preserved", lambda original: None)
    monkeypatch.setattr(pages, "_push_pages_branch", lambda: pushes.append(True))

    pages.initialize("example/ucm")

    assert sorted(path.name for path in tmp_path.iterdir()) == [".nojekyll", "CNAME"]
    assert (tmp_path / "CNAME").read_text(encoding="utf-8") == "docs.example\n"
    assert len(pushes) == 1


def test_bilingual_install_and_download_pages_use_one_manifest_contract() -> None:
    english = (DOCS_ROOT / "docs" / "en" / "user-guide" / "installation.md").read_text(
        encoding="utf-8"
    )
    chinese = (DOCS_ROOT / "docs" / "zh" / "user-guide" / "installation.md").read_text(
        encoding="utf-8"
    )
    javascript = (DOCS_ROOT / "docs" / "assets" / "install.js").read_text(
        encoding="utf-8"
    )
    loader = (DOCS_ROOT / "docs" / "assets" / "manifest.js").read_text(encoding="utf-8")
    inventory = (DOCS_ROOT / "docs" / "assets" / "download.js").read_text(
        encoding="utf-8"
    )
    stylesheet = (DOCS_ROOT / "docs" / "assets" / "install.css").read_text(
        encoding="utf-8"
    )
    mkdocs = (DOCS_ROOT / "mkdocs.yml").read_text(encoding="utf-8")

    for page, locale in ((english, "en"), (chinese, "zh")):
        assert f'data-locale="{locale}"' in page
        assert "ucm-install-app" in page
        assert "<script" not in page
        assert "SuperMarioYL" not in page
        assert "0.7.58" not in page
        assert "0.5.0" not in page
        assert "SGLang" not in page
        assert "A5" not in page
        assert "data-install-source" not in page
    for locale in ("en", "zh"):
        for page, kind in (
            ("index.md", "overview"),
            ("whl.md", "wheel"),
            ("helm.md", "helm"),
            ("image.md", "image"),
        ):
            download_page = (DOCS_ROOT / "docs" / locale / "download" / page).read_text(
                encoding="utf-8"
            )
            assert f'data-locale="{locale}"' in download_page
            assert f'data-download-kind="{kind}"' in download_page
            assert "data-ucm-download" in download_page
    assert "release-manifest.json" in loader
    assert "ucm-release-manifest" in loader
    assert "schema_version" in loader
    assert "install-catalog.json" not in javascript + inventory
    assert 'assets.has("install-catalog.json")' in loader
    assert "pip install" in javascript
    assert 'pip install "' in inventory
    assert "python -m pip install" not in javascript + inventory
    assert "docker pull" in javascript
    assert "helm install ucm" in javascript
    assert 'method !== "helm"' in javascript
    assert 'element("label", "ucm-selector__option")' in javascript
    assert 'element("button", "ucm-selector__option"' not in javascript
    assert "computeLabelParts" in javascript
    assert "engineVersionOptions" in javascript
    assert 'var ROW_ORDER = [\n    "method",\n    "engine"' in javascript
    assert "ucm-selector__option-text--stacked" in javascript
    assert "controls.appendChild(output)" in javascript
    assert "ucm-install__output-value" in javascript
    assert 'element("h2", "ucm-install__output-title"' not in javascript
    assert "flex-wrap: nowrap" in stylesheet
    assert "flex: 1 1 0" in stylesheet
    assert "font-weight: 500" in stylesheet
    assert "overflow: hidden" in stylesheet
    assert "Manifest.validateManifest" in inventory
    assert mkdocs.index("  - Toolkit:") < mkdocs.index("  - Download:")
    download_nav = mkdocs[mkdocs.index("  - Download:") :]
    assert download_nav.index("- Overview:") < download_nav.index("- Wheel:")
    assert download_nav.index("- Wheel:") < download_nav.index("- Helm:")
    assert download_nav.index("- Helm:") < download_nav.index("- Image:")
    assert "assets/manifest.js" in mkdocs
    assert "assets/install.js" in mkdocs
    assert "assets/download.js" in mkdocs
    assert "assets/install.css" in mkdocs
    for asset in ("manifest.js", "install.js", "download.js", "install.css"):
        assert f"assets/{asset}?v=20260829-schema8-3" in mkdocs
