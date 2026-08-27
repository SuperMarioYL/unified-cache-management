from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import zipfile
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = DOCS_ROOT / "tools" / "pages.py"
SPEC = importlib.util.spec_from_file_location("ucm_docs_pages", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pages = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pages
SPEC.loader.exec_module(pages)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wheel(
    path: Path, distribution: str, version: str, dependencies: tuple[str, ...] = ()
) -> None:
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {version}",
    ]
    metadata.extend(f"Requires-Dist: {dependency}" for dependency in dependencies)
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(f"{normalized}/__init__.py", "")
        wheel.writestr(f"{dist_info}/METADATA", "\n".join(metadata) + "\n")
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: ucm-pages-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        wheel.writestr(f"{dist_info}/RECORD", "")


def _wheel_record(
    *, channel: str, version: str, filename: str, url: str, sha256: str
) -> dict[str, object]:
    return {
        "channel": channel,
        "distribution": "uc-manager",
        "version": version,
        "python_abi": "py3",
        "cpu_arch": "any",
        "filename": filename,
        "url": url,
        "sha256": sha256,
        "dependencies": ["wrapt==1.17.2"],
    }


def _catalog(wheels: list[dict[str, object]], version: str = "0.9.0") -> dict:
    return {
        "kind": "ucm-install-catalog",
        "schema_version": 1,
        "release": {
            "tag": f"v{version}",
            "version": version,
            "url": f"https://github.com/example/ucm/releases/tag/v{version}",
        },
        "wheels": wheels,
        "images": [
            {
                "id": "vllm-v1-cu130",
                "product": "vllm",
                "upstream_version": "1.0.0",
                "upstream_channel": "stable",
                "accelerator_runtime": "cuda-13.0",
                "variant": "cuda",
                "soc_version": "cuda",
                "os_id": "ubuntu",
                "os_version": "24.04",
                "architectures": ["amd64", "arm64"],
                "references": {"ghcr": "ghcr.io/example/vllm:v1-ucm-0.9.0"},
            }
        ],
        "chart": {
            "name": "unified-cache-chart",
            "version": version,
            "filename": f"unified-cache-chart-{version}.tgz",
            "url": f"https://github.com/example/ucm/releases/download/v{version}/chart.tgz",
            "oci": f"ghcr.io/example/charts/unified-cache-chart:{version}",
        },
    }


def _freeze(root: Path, catalog: dict) -> None:
    path = root / "catalogs" / catalog["release"]["version"] / "install-catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog), encoding="utf-8")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _serve(directory: Path):
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_cuda_simple_index_performs_real_pip_dependency_resolution(
    tmp_path: Path,
) -> None:
    files = tmp_path / "files"
    files.mkdir()
    ucm_name = "uc_manager-0.9.0+cu130-py3-none-any.whl"
    wrapt_name = "wrapt-1.17.2-py3-none-any.whl"
    ucm = files / ucm_name
    wrapt = files / wrapt_name
    _write_wheel(ucm, "uc-manager", "0.9.0+cu130", ("wrapt==1.17.2",))
    _write_wheel(wrapt, "wrapt", "1.17.2")

    with _serve(tmp_path) as base_url:
        catalog = _catalog(
            [
                _wheel_record(
                    channel="cu130",
                    version="0.9.0+cu130",
                    filename=ucm_name,
                    url=f"{base_url}/files/{ucm_name}",
                    sha256=_sha256(ucm),
                )
            ]
        )
        _freeze(tmp_path, catalog)
        pages.build_simple_indexes(
            tmp_path,
            fetch_wrapt_files=lambda version: [
                pages.PyPIWheel(
                    filename=wrapt_name,
                    url=f"{base_url}/files/{wrapt_name}",
                    sha256=_sha256(wrapt),
                )
            ],
        )
        destination = tmp_path / "download"
        environment = os.environ.copy()
        environment.pop("PIP_EXTRA_INDEX_URL", None)
        environment.pop("PIP_NO_INDEX", None)
        environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--trusted-host",
                "127.0.0.1",
                "--index-url",
                f"{base_url}/whl/cu130/",
                "--dest",
                str(destination),
                "uc-manager==0.9.0",
            ],
            text=True,
            capture_output=True,
            env=environment,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert {path.name for path in destination.glob("*.whl")} == {
            ucm_name,
            wrapt_name,
        }


def test_cann_channel_does_not_contain_cuda_wheels(tmp_path: Path) -> None:
    cuda_name = "uc_manager-0.9.0+cu130-py3-none-any.whl"
    cann_name = "uc_manager-0.9.0+cann901.a2-py3-none-any.whl"
    catalog = _catalog(
        [
            _wheel_record(
                channel="cu130",
                version="0.9.0+cu130",
                filename=cuda_name,
                url=f"https://example.invalid/{cuda_name}",
                sha256="a" * 64,
            ),
            _wheel_record(
                channel="cann901-a2",
                version="0.9.0+cann901.a2",
                filename=cann_name,
                url=f"https://example.invalid/{cann_name}",
                sha256="b" * 64,
            ),
        ]
    )
    _freeze(tmp_path, catalog)
    pages.build_simple_indexes(
        tmp_path,
        fetch_wrapt_files=lambda version: [
            pages.PyPIWheel(
                filename="wrapt-1.17.2-py3-none-any.whl",
                url="https://example.invalid/wrapt.whl",
                sha256="c" * 64,
            )
        ],
    )
    cann_index = (
        tmp_path / "whl" / "cann901-a2" / "uc-manager" / "index.html"
    ).read_text(encoding="utf-8")
    assert cann_name in cann_index
    assert cuda_name not in cann_index
    assert "#sha256=" + "b" * 64 in cann_index


def test_latest_uses_current_stable_frozen_catalog(tmp_path: Path) -> None:
    catalog = _catalog([])
    _freeze(tmp_path, catalog)
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

    assert pages.inject_latest_catalog(tmp_path) == "0.9.0"
    injected = json.loads(
        (tmp_path / "latest" / "install-catalog.json").read_text(encoding="utf-8")
    )
    assert injected == catalog


def test_catalog_sync_does_not_create_latest_without_documentation(
    tmp_path: Path,
) -> None:
    catalog = _catalog([])
    _freeze(tmp_path, catalog)
    (tmp_path / "versions.json").write_text(
        json.dumps([{"version": "0.9.0", "title": "0.9.0", "aliases": ["stable"]}]),
        encoding="utf-8",
    )

    assert pages.inject_latest_catalog(tmp_path) is None
    assert not (tmp_path / "latest").exists()


def test_latest_publish_preserves_indexes_and_pushes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "latest").mkdir()
    (tmp_path / "versions.json").write_text("[]", encoding="utf-8")
    existing_index = tmp_path / "whl" / "cu130" / "index.html"
    existing_index.parent.mkdir(parents=True)
    existing_index.write_text("frozen", encoding="utf-8")
    mike_calls: list[list[str]] = []
    pushes: list[bool] = []

    @contextmanager
    def fake_worktree(message: str):
        yield tmp_path

    monkeypatch.setattr(pages, "_prepare_pages_branch", lambda repository: None)
    monkeypatch.setattr(pages, "_read_branch_file", lambda path: "docs.example\n")
    monkeypatch.setattr(
        pages, "_mike", lambda arguments, site_url: mike_calls.append(list(arguments))
    )
    monkeypatch.setattr(pages, "_pages_worktree", fake_worktree)
    monkeypatch.setattr(pages, "_assert_cname_preserved", lambda original: None)
    monkeypatch.setattr(pages, "_push_pages_branch", lambda: pushes.append(True))
    monkeypatch.setattr(
        pages,
        "build_simple_indexes",
        lambda root: pytest.fail("latest must not rebuild the Stable Simple Index"),
    )

    pages.publish_latest("example/ucm")

    assert existing_index.read_text(encoding="utf-8") == "frozen"
    assert len(pushes) == 1
    assert len(mike_calls) == 1
    assert mike_calls[0][:2] == ["deploy", "latest"]
    assert "--push" not in mike_calls[0]


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
def test_stable_publish_rejects_non_stable_catalog(version: str, tag: str) -> None:
    catalog = _catalog([], version=version)
    catalog["release"]["tag"] = tag
    with pytest.raises(pages.PagesError):
        pages.require_stable_catalog(catalog)


@pytest.mark.parametrize("already_exists", [False, True])
def test_first_and_repeated_stable_publish_mike_decision_and_one_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_exists: bool
) -> None:
    catalog = _catalog([])
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    branch_tree = tmp_path / "branch"
    (branch_tree / "0.9.0").mkdir(parents=True)
    (branch_tree / "latest").mkdir()
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
    monkeypatch.setattr(pages, "build_simple_indexes", lambda root: None)
    monkeypatch.setattr(pages, "_assert_cname_preserved", lambda original: None)
    monkeypatch.setattr(pages, "_push_pages_branch", lambda: pushes.append(True))

    pages.publish_stable("example/ucm", catalog_path)

    assert len(pushes) == 1
    assert (
        json.loads(
            (branch_tree / "latest" / "install-catalog.json").read_text(
                encoding="utf-8"
            )
        )
        == catalog
    )
    if already_exists:
        assert mike_calls == []
    else:
        assert len(mike_calls) == 2
        assert mike_calls[0][:4] == ["deploy", "0.9.0", "stable", "--update-aliases"]
        assert "--push" not in mike_calls[0]
        assert mike_calls[1][:2] == ["set-default", "stable"]
        assert "--push" not in mike_calls[1]


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


def test_bilingual_install_pages_use_one_shared_catalog_renderer() -> None:
    english = (DOCS_ROOT / "docs" / "en" / "user-guide" / "installation.md").read_text(
        encoding="utf-8"
    )
    chinese = (DOCS_ROOT / "docs" / "zh" / "user-guide" / "installation.md").read_text(
        encoding="utf-8"
    )
    javascript = (DOCS_ROOT / "docs" / "assets" / "install.js").read_text(
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
    assert "install-catalog.json" in javascript
    assert 'python -m pip install "uc-manager==' in javascript
    assert "docker pull" in javascript
    assert "assets/install.js" in mkdocs
    assert "assets/install.css" in mkdocs
