#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Install published UCM wheels. This file is standalone; it needs Python and pip.
set -euo pipefail

exec "${PYTHON:-python3}" - "$@" <<'PYTHON'
"""Resolve a UCM meta package and backend for this interpreter, then use pip."""

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

if sys.version_info < (3, 10):
    sys.exit("UCM: select Python 3.10 or newer with PYTHON=/path/to/python.")

try:
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
    from pip._vendor.packaging.version import Version
except ImportError:
    sys.exit("UCM: this Python needs pip with its bundled packaging library.")


def diagnostic(message):
    print(f"UCM: {message}", file=sys.stderr)


def runtime_version(value, source):
    """Toolkit build suffixes do not change the published backend coordinates."""
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+)|\.RC\d+)?(?:\D|$)", value, re.I)
    if not match:
        raise ValueError(f"cannot read a Toolkit version from {source}: {value!r}")
    return tuple(int(part or 0) for part in match.groups())


def consistent_version(sources):
    versions = {runtime_version(value, source) for source, value in sources}
    if len(versions) > 1:
        raise ValueError(f"conflicting Toolkit metadata: {sources}")
    return next(iter(versions)) if versions else None


def toolkit_roots(names, defaults):
    configured = [Path(os.environ[name]) for name in names if os.environ.get(name)]
    return list(dict.fromkeys(path.resolve() for path in (configured or defaults)))


def cann_version():
    roots = toolkit_roots(
        ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME"),
        [
            Path("/usr/local/Ascend/ascend-toolkit/latest"),
            Path("/usr/local/Ascend/cann"),
        ],
    )
    sources = []
    for root in roots:
        # Only Toolkit metadata: recursive scans also find driver/ops versions.
        paths = [root / "version.cfg", root / "version.info"]
        for pattern in (
            "*-linux/ascend_toolkit_install.info",
            "*-linux/version.info",
            "*-linux/version.cfg",
        ):
            paths.extend(sorted(root.glob(pattern)))
        for path in paths:
            if path.is_file():
                for line in path.read_text().splitlines():
                    match = re.match(
                        r"\s*(?:version|cann_version)\s*[:=]\s*[\"']?([^\s\"']+)",
                        line,
                        re.I,
                    )
                    if match:
                        sources.append((str(path), match[1]))
    version = consistent_version(sources)
    if version is None and (
        any(root.exists() for root in roots)
        or any(
            os.environ.get(key) for key in ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME")
        )
    ):
        raise ValueError(
            f"CANN Toolkit metadata was not found in {roots}; specify --extra"
        )
    return version


def cuda_version():
    configured = any(os.environ.get(key) for key in ("CUDA_HOME", "CUDA_PATH"))
    nvcc = shutil.which("nvcc")
    defaults = [Path("/usr/local/cuda")]
    if nvcc:
        defaults.append(Path(nvcc).resolve().parent.parent)
    roots = toolkit_roots(("CUDA_HOME", "CUDA_PATH"), defaults)
    sources = []
    for root in roots:
        metadata = root / "version.json"
        legacy = root / "version.txt"
        if metadata.is_file():
            data = json.loads(metadata.read_text())
            sources.append((str(metadata), data["cuda"]["version"]))
        elif legacy.is_file():
            match = re.search(r"CUDA Version\s+(\S+)", legacy.read_text())
            if not match:
                raise ValueError(f"cannot read CUDA Toolkit metadata: {legacy}")
            sources.append((str(legacy), match[1]))
        elif (root / "bin/nvcc").is_file():
            result = subprocess.run(
                [str(root / "bin/nvcc"), "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
            match = re.search(r"release\s+(\d+\.\d+)", result.stdout)
            if not match:
                raise ValueError(
                    f"cannot read CUDA Toolkit version from {root / 'bin/nvcc'}"
                )
            sources.append((str(root / "bin/nvcc"), match[1]))
    version = consistent_version(sources)
    if version is None and (configured or any(root.exists() for root in roots)):
        raise ValueError(
            f"CUDA Toolkit metadata was not found in {roots}; specify --extra"
        )
    return version[:2] if version else None


def detect_accelerator():
    cann, cuda = cann_version(), cuda_version()
    if cann and cuda:
        raise ValueError("both CANN and CUDA Toolkits were found; specify --extra")
    if cuda:
        return "cuda", cuda
    if cann:
        soc = os.environ.get("SOC_VERSION", "").casefold()
        if re.fullmatch(r"ascend910b\d*", soc):
            return "a2", cann
        if re.fullmatch(r"ascend910_93\d+", soc):
            return "a3", cann
        raise ValueError(
            f"cannot identify A2/A3 from SOC_VERSION={soc!r}; specify --extra"
        )
    raise ValueError("no CANN or CUDA Toolkit was identified; specify --extra")


def probe_environment(automatic):
    markers = default_environment()
    architecture = platform.machine().lower()
    libc, glibc = platform.libc_ver()
    if (
        sys.platform != "linux"
        or architecture not in ("aarch64", "x86_64")
        or libc != "glibc"
        or not glibc
    ):
        raise ValueError(
            f"requires Linux ARM64/AMD64 with glibc; found {sys.platform}, {architecture}, {libc} {glibc}"
        )
    os_release = platform.freedesktop_os_release()
    diagnostic(
        f"Python {markers['python_full_version']}, {architecture}, glibc {glibc}, {os_release.get('PRETTY_NAME', 'Linux')}"
    )
    accelerator = detect_accelerator() if automatic else None
    if accelerator:
        diagnostic(
            f"detected {accelerator[0]} Toolkit {'.'.join(map(str, accelerator[1]))}"
        )
    return {"markers": markers, "tags": list(sys_tags()), "accelerator": accelerator}


class PyPI:
    """Official publication metadata, cached only for this invocation."""

    def __init__(self):
        self.projects = {}
        self.metadata = {}

    def read(self, url, accept="application/octet-stream"):
        try:
            with urlopen(
                Request(url, headers={"Accept": accept}), timeout=30
            ) as response:
                return response.read()
        except OSError as error:
            raise RuntimeError(f"cannot read {url}: {error}") from error

    def files(self, project):
        name = canonicalize_name(project)
        if name not in self.projects:
            url = f"https://pypi.org/simple/{quote(name, safe='')}/"
            try:
                data = self.read(url, "application/vnd.pypi.simple.v1+json")
            except RuntimeError as error:
                if (
                    isinstance(error.__cause__, HTTPError)
                    and error.__cause__.code == 404
                ):
                    self.projects[name] = []
                    return []
                raise
            self.projects[name] = json.loads(data)["files"]
        return self.projects[name]

    def wheel_metadata(self, artifact):
        url = artifact["url"]
        if url not in self.metadata:
            sidecar = artifact.get("core-metadata") or artifact.get(
                "data-dist-info-metadata"
            )
            data = self.read(url + ".metadata" if sidecar else url)
            hashes = (
                sidecar
                if isinstance(sidecar, dict)
                else ({} if sidecar else artifact.get("hashes", {}))
            )
            if (
                hashes.get("sha256")
                and hashlib.sha256(data).hexdigest() != hashes["sha256"]
            ):
                raise ValueError(f"PyPI metadata/wheel hash mismatch: {url}")
            if not sidecar:
                with zipfile.ZipFile(io.BytesIO(data)) as wheel:
                    names = [
                        name
                        for name in wheel.namelist()
                        if name.endswith(".dist-info/METADATA")
                    ]
                    if len(names) != 1:
                        raise ValueError(f"expected one wheel METADATA: {url}")
                    data = wheel.read(names[0])
            self.metadata[url] = BytesParser().parsebytes(data)
        return self.metadata[url]


def compatible_wheels(files, project, version, environment):
    ranks = {tag: index for index, tag in enumerate(environment["tags"])}
    candidates = []
    for artifact in files:
        if artifact.get("yanked") or not artifact["filename"].endswith(".whl"):
            continue
        name, wheel_version, _, tags = parse_wheel_filename(artifact["filename"])
        if name != canonicalize_name(project) or wheel_version != version:
            continue
        matching = tags.intersection(ranks)
        if matching and SpecifierSet(artifact.get("requires-python") or "").contains(
            environment["markers"]["python_full_version"], prereleases=True
        ):
            candidates.append(
                (min(ranks[tag] for tag in matching), artifact["filename"], artifact)
            )
    return [artifact for _, _, artifact in sorted(candidates)]


def usable_wheel(pypi, project, version, environment):
    for artifact in compatible_wheels(
        pypi.files(project), project, version, environment
    ):
        metadata = pypi.wheel_metadata(artifact)
        if (
            canonicalize_name(metadata["Name"] or "") != canonicalize_name(project)
            or Version(metadata["Version"]) != version
        ):
            raise ValueError(
                f"wheel metadata disagrees with filename: {artifact['filename']}"
            )
        if SpecifierSet(metadata.get("Requires-Python", "")).contains(
            environment["markers"]["python_full_version"], prereleases=True
        ):
            return artifact, metadata
    return None


def extra_runtime(extra):
    # The publisher concatenates major + single-digit minor/patch coordinates.
    cann = re.fullmatch(r"cann(\d+)(\d)(\d)-(a2|a3)", extra)
    if cann:
        return cann[4], tuple(int(part) for part in cann.groups()[:3])
    cuda = re.fullmatch(r"cu(\d+)(\d)", extra)
    if cuda:
        return "cuda", (int(cuda[1]), int(cuda[2]))
    return None


def backend_requirement(metadata, extra, markers):
    requirements = []
    for raw in metadata.get_all("Requires-Dist", []):
        requirement = Requirement(raw)
        name = canonicalize_name(requirement.name)
        if (
            name.startswith("uc-manager-")
            and name != "uc-manager-toolkit"
            and (
                requirement.marker is None
                or requirement.marker.evaluate({**markers, "extra": extra})
            )
        ):
            requirements.append(requirement)
    if not requirements:
        return None
    if len(requirements) != 1:
        raise ValueError(f"extra {extra!r} must declare exactly one UCM backend")
    requirement = requirements[0]
    pins = list(requirement.specifier)
    if (
        requirement.url
        or requirement.extras
        or len(pins) != 1
        or pins[0].operator != "=="
        or "*" in pins[0].version
    ):
        raise ValueError(
            f"extra {extra!r} does not pin a backend version: {requirement}"
        )
    return canonicalize_name(requirement.name), Version(pins[0].version)


def resolve(pypi, environment, version, extra):
    files = pypi.files("uc-manager")
    if version == "latest":
        versions = {
            parse_wheel_filename(item["filename"])[1]
            for item in files
            if item["filename"].endswith(".whl") and not item.get("yanked")
        }
        versions = sorted(
            (
                value
                for value in versions
                if not value.is_prerelease and not value.is_devrelease
            ),
            reverse=True,
        )
    else:
        versions = [Version(version)]
    for candidate in versions:
        meta_wheel = usable_wheel(pypi, "uc-manager", candidate, environment)
        if not meta_wheel:
            diagnostic(f"skip {candidate}: no compatible uc-manager wheel")
            continue
        _, metadata = meta_wheel
        candidate = Version(metadata["Version"])
        available = []
        for value in metadata.get_all("Provides-Extra", []):
            value = canonicalize_name(value)
            runtime = extra_runtime(value)
            if extra == "auto":
                if runtime is None or runtime[0] != environment["accelerator"][0]:
                    continue
            elif value != extra:
                continue
            backend = backend_requirement(metadata, value, environment["markers"])
            if backend is None:
                continue
            wheel = usable_wheel(pypi, *backend, environment)
            if wheel:
                available.append(
                    (runtime[1] if runtime else (), value, backend, wheel[0])
                )
        if not available:
            diagnostic(f"skip {candidate}: no compatible backend wheel for {extra}")
            continue
        available.sort(key=lambda item: (item[0], item[1]))
        if extra == "auto":
            detected = environment["accelerator"][1]
            lower = [item for item in available if item[0] <= detected]
            selected = lower[-1] if lower else available[0]
            diagnostic(
                f"Toolkit {'.'.join(map(str, detected))} -> {selected[1]} (package selection only; hardware compatibility is not verified)"
            )
        else:
            selected = available[0]
            diagnostic(
                f"explicit backend {extra}; automatic Toolkit detection is bypassed"
            )
        _, chosen, (name, backend_version), wheel = selected
        return {
            "version": str(candidate),
            "extra": chosen,
            "requirement": f"uc-manager[{chosen}]=={candidate}",
            "backend_requirement": f"{name}=={backend_version}",
            "wheel": wheel["filename"],
        }
    raise ValueError(
        f"no published, non-yanked UCM wheel matches version={version}, extra={extra}, Python {environment['markers']['python_full_version']} and this platform's ABI/glibc tags"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Install published UCM wheels for the current Python, architecture and Toolkit. Set PYTHON to choose the interpreter."
    )
    parser.add_argument(
        "--version",
        default="latest",
        help="latest stable (default) or an exact published version",
    )
    parser.add_argument(
        "--extra", default="auto", help="auto (default) or one published backend extra"
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="print the selection as JSON without installing",
    )
    args = parser.parse_args()
    if not args.version or not args.extra or "," in args.extra:
        parser.error("--version must be non-empty and --extra must select one backend")
    if args.version != "latest":
        Version(args.version)
    extra = canonicalize_name(args.extra)
    environment = probe_environment(extra == "auto")
    selection = resolve(PyPI(), environment, args.version, extra)
    if args.resolve:
        print(json.dumps(selection))
        return
    diagnostic(f"installing {selection['requirement']} with {sys.executable}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            selection["requirement"],
        ],
        check=True,
    )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        diagnostic(f"command failed (exit {error.returncode}): {error.cmd}")
        sys.exit(error.returncode if error.returncode > 0 else 1)
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        zipfile.BadZipFile,
    ) as error:
        diagnostic(str(error))
        sys.exit(1)
PYTHON
