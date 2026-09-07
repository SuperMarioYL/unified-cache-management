from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DOCS_ROOT / "tools"))
import release_manifest as manifests


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


def test_manifest_validation_accepts_only_schema_8() -> None:
    manifest = _manifest([])
    assert manifests.validate_manifest(manifest) == manifest

    for unsupported_schema in (6, 7):
        unsupported = json.loads(json.dumps(manifest))
        unsupported["schema_version"] = unsupported_schema
        with pytest.raises(manifests.ManifestError, match="schema_version must be 8"):
            manifests.validate_manifest(unsupported)

    extra_field = json.loads(json.dumps(manifest))
    extra_field["release"]["commit"] = "a8c0d7ef"
    with pytest.raises(manifests.ManifestError, match="fields differ"):
        manifests.validate_manifest(extra_field)

    missing_self_asset = json.loads(json.dumps(manifest))
    missing_self_asset["github_release_assets"].remove("release-manifest.json")
    with pytest.raises(manifests.ManifestError, match="must list itself"):
        manifests.validate_manifest(missing_self_asset)

    legacy_asset = json.loads(json.dumps(manifest))
    legacy_asset["github_release_assets"].append("install-catalog.json")
    with pytest.raises(manifests.ManifestError, match="must not list"):
        manifests.validate_manifest(legacy_asset)

    missing_chart = json.loads(json.dumps(manifest))
    missing_chart["github_release_assets"].remove("unified-cache-chart-0.9.0.tgz")
    with pytest.raises(manifests.ManifestError, match="assets are missing"):
        manifests.validate_manifest(missing_chart)


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
    assert manifests.validate_manifest(manifest) == manifest

    fork = json.loads(
        json.dumps(manifest)
        .replace("uc-manager", "supermarioyl-uc-manager")
        .replace("uc_manager", "supermarioyl_uc_manager")
        .replace("https://pypi.org/", "https://test.pypi.org/")
    )
    assert manifests.validate_manifest(fork) == fork

    wrong_backend = json.loads(json.dumps(manifest))
    wrong_backend["wheels"][0]["distribution"] = "uc-manager-cuda-wrong"
    with pytest.raises(manifests.ManifestError, match="declared Python extra"):
        manifests.validate_manifest(wrong_backend)

    missing_meta = json.loads(json.dumps(manifest))
    missing_meta["github_release_assets"].remove(missing_meta["python"]["filename"])
    with pytest.raises(manifests.ManifestError, match="assets are missing"):
        manifests.validate_manifest(missing_meta)

    missing_receipt = json.loads(json.dumps(manifest))
    missing_receipt["github_release_assets"].remove("pypi-receipt.json")
    with pytest.raises(manifests.ManifestError, match="PyPI receipt"):
        manifests.validate_manifest(missing_receipt)

    wrong_project = json.loads(json.dumps(manifest))
    wrong_project["python"]["pypi"][
        "project_url"
    ] = "https://pypi.org/project/uc-manager/99.0/"
    with pytest.raises(manifests.ManifestError, match="PyPI URLs"):
        manifests.validate_manifest(wrong_project)

    empty_platform = json.loads(json.dumps(manifest))
    empty_platform["wheels"][0]["platform_tags"] = []
    with pytest.raises(manifests.ManifestError, match="must not be empty"):
        manifests.validate_manifest(empty_platform)

    mismatched_platform = json.loads(json.dumps(manifest))
    mismatched_platform["wheels"][0]["platform_tags"] = ["manylinux_2_34_x86_64"]
    with pytest.raises(manifests.ManifestError, match="filename and platform"):
        manifests.validate_manifest(mismatched_platform)


def test_schema_8_publication_validation_distinguishes_index_and_member() -> None:
    manifest = _manifest([])
    publication = manifest["images"][0]["publications"]["ghcr"]
    assert publication is not None
    publication["multi_arch"] = False
    with pytest.raises(manifests.ManifestError, match="single-architecture"):
        manifests.validate_manifest(manifest)

    publication["members"] = [publication["members"][0]]
    publication["pull"] = publication["members"][0]["reference"]
    assert manifests.validate_manifest(manifest) == manifest


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
    with pytest.raises(manifests.ManifestError):
        manifests.require_stable_manifest(manifest)


def test_bilingual_installation_uses_one_schema_8_manifest_contract() -> None:
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
    assert not (DOCS_ROOT / "docs" / "en" / "download").exists()
    assert not (DOCS_ROOT / "docs" / "zh" / "download").exists()
    assert not (DOCS_ROOT / "docs" / "assets" / "download.js").exists()
    assert "release-manifest.json" in loader
    assert "ucm-release-manifest" in loader
    assert "schema_version" in loader
    assert "install-catalog.json" not in javascript
    assert 'assets.has("install-catalog.json")' in loader
    assert "pip install" in javascript
    assert "python -m pip install" not in javascript
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
    assert "  - Download:" not in mkdocs
    assert "assets/download.js" not in mkdocs
    assert "../../download/" not in english + chinese
    assert "assets/manifest.js" in mkdocs
    assert "assets/install.js" in mkdocs
    assert "assets/install.css" in mkdocs
    for asset in ("manifest.js", "install.js", "install.css"):
        assert f"assets/{asset}?v=20260829-schema8-4" in mkdocs
