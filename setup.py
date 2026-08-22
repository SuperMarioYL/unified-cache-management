#
# MIT License
#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

import atexit
import os
import platform as host_platform
import subprocess
import sys

from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
BUILD_CONFIG_PATH = os.getenv("UCM_BUILD_CONFIG")
ENABLE_SPARSE = os.getenv("ENABLE_SPARSE")
ENABLE_MINDIE = os.getenv("UCM_ENABLE_MINDIE", "0") not in ("", "0", "false", "False")
ENABLE_GDR = os.getenv("ENABLE_GDR", "0") not in ("", "0", "false", "False")
ASCEND_ROOT = os.getenv("ASCEND_ROOT")


def get_package_version() -> str:
    version_path = os.path.join(ROOT_DIR, "version.ini")
    try:
        with open(version_path, encoding="utf-8") as version_file:
            for line in version_file:
                key, separator, value = line.strip().partition("=")
                if separator and key == "VLLM_UC_VERSION" and value:
                    return value
    except OSError as error:
        raise RuntimeError(
            f"cannot read package version from {version_path}"
        ) from error
    raise RuntimeError(f"VLLM_UC_VERSION is missing from {version_path}")


def _load_build_config() -> tuple[dict[str, object], dict[str, object]] | None:
    if BUILD_CONFIG_PATH is None:
        return None
    release_root = os.path.join(ROOT_DIR, ".github", "release")
    if release_root not in sys.path:
        sys.path.insert(0, release_root)
    try:
        from ucm_release.wheel import load_wheel_build_config, wheel_build_profile

        config = load_wheel_build_config(BUILD_CONFIG_PATH)
        authority = config["authority"]
        source_version = get_package_version()
        if authority["wheel_version"] != source_version:
            raise ValueError(
                "authority wheel_version differs from source version.ini: "
                f"{authority['wheel_version']} != {source_version}"
            )
        return config, wheel_build_profile(authority["profile_id"])
    except (ImportError, KeyError, OSError, TypeError, ValueError) as error:
        raise RuntimeError(f"UCM_BUILD_CONFIG is invalid: {error}") from error


BUILD_INPUT = _load_build_config()
BUILD_CONFIG = BUILD_INPUT[0] if BUILD_INPUT is not None else None
BUILD_PROFILE = BUILD_INPUT[1] if BUILD_INPUT is not None else None
PLATFORM = (
    str(BUILD_CONFIG["platform"]) if BUILD_CONFIG is not None else os.getenv("PLATFORM")
)


def get_install_requires() -> list[str]:
    if BUILD_CONFIG is not None:
        return list(BUILD_CONFIG["runtime_requirements"])
    return ["wrapt==1.17.2"]


def _release_architecture() -> str:
    machine = host_platform.machine().lower()
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if architecture is None:
        raise RuntimeError(f"unsupported native release architecture: {machine}")
    return architecture


def _release_settings() -> dict[str, object] | None:
    if BUILD_CONFIG is None:
        return None
    authority = BUILD_CONFIG["authority"]
    assert isinstance(authority, dict)
    assert BUILD_PROFILE is not None
    architecture = _release_architecture()
    invoking_python = {
        "version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
    }
    comparisons = {
        "architecture": (architecture, authority["cpu_arch"]),
        "distribution": (BUILD_CONFIG["distribution"], BUILD_PROFILE["distribution"]),
        "platform": (BUILD_CONFIG["platform"], BUILD_PROFILE["build_platform"]),
        "invoking Python": (invoking_python, BUILD_CONFIG["python"]),
    }
    mismatches = [
        name for name, (actual, expected) in comparisons.items() if actual != expected
    ]
    if mismatches:
        raise RuntimeError(f"wheel build config differs from build host: {mismatches}")
    if ENABLE_MINDIE:
        raise RuntimeError("MindIE must be disabled in a release wheel build")
    if ENABLE_SPARSE is not None and ENABLE_SPARSE.lower() == "true":
        raise RuntimeError("ENABLE_SPARSE must be false in a release wheel build")
    return {
        "profile": authority["profile_id"],
        "source_sha": authority["source_sha"],
        "version": get_package_version(),
        "build_key": authority["task_sha256"],
        "source_date_epoch": authority["source_date_epoch"],
        "required": authority["required_native"],
        "forbidden": authority["forbidden_native"],
        "architecture": architecture,
        "spec_id": authority["spec_id"],
    }


RELEASE_SETTINGS = _release_settings()


def get_distribution_name() -> str:
    if BUILD_CONFIG is not None:
        return str(BUILD_CONFIG["distribution"])
    return os.getenv("UCM_DIST_NAME", "uc-manager")


def get_abi_flag_from_env() -> str:
    v = os.environ.get("UCM_CXX11_ABI")
    if v is None:
        raise RuntimeError(
            "You must set env UCM_CXX11_ABI=0 or 1 to build with MindIE.\n"
            "Example:\n"
            "  UCM_ENABLE_MINDIE=1 UCM_CXX11_ABI=0 python -m build -w\n"
            "  UCM_ENABLE_MINDIE=1 UCM_CXX11_ABI=1 python -m build -w"
        )
    if v not in ("0", "1"):
        raise RuntimeError(f"Invalid UCM_CXX11_ABI={v}, expected 0 or 1")
    return v


UCM_CXX11_ABI = get_abi_flag_from_env() if ENABLE_MINDIE else None
_warning_printed = False


def print_platform_warning():
    global _warning_printed
    if not PLATFORM and not _warning_printed:
        _warning_printed = True
        RED = "\033[91m"
        YELLOW = "\033[93m"
        BOLD = "\033[1m"
        RESET = "\033[0m"

        warning_msg = f"""
{RED}{'=' * 80}
{BOLD}⚠️  WARNING: PLATFORM environment variable is not set! ⚠️{RESET}
{RED}{'=' * 80}{RESET}
{YELLOW}Please set PLATFORM to one of: cuda, ascend, ascend-a3, musa, maca{RESET}
Example:
  {BOLD}export PLATFORM=cuda{RESET}    # For CUDA platform
{YELLOW}In CI scenarios only, you don't need to specify PLATFORM. If it's not a CI scenario, please uninstall and then reinstall with PLATFORM specified.{RESET}
{RED}{'=' * 80}{RESET}
"""
        # Use write and flush to ensure output even without -v flag
        sys.stderr.write(warning_msg)
        sys.stderr.flush()


if not PLATFORM:
    atexit.register(print_platform_warning)


def is_ascend() -> bool:
    return PLATFORM is not None and PLATFORM.startswith("ascend")


def enable_sparse() -> bool:
    return ENABLE_SPARSE is not None and ENABLE_SPARSE.lower() == "true"


def is_only_build_mode() -> bool:
    return "bdist_wheel" in sys.argv


def is_editable_mode() -> bool:
    commands = [arg.lower() for arg in sys.argv]
    return (
        "develop" in commands
        or "--editable" in commands
        or "-e" in commands
        or "editable_wheel" in commands
    )


class CMakeExtension(Extension):
    def __init__(self, name: str, source_dir: str = ""):
        super().__init__(name, sources=[])
        self.cmake_file_path = os.path.abspath(source_dir)


class CMakeBuild(build_ext):
    def run(self):
        cmake_exts = [ext for ext in self.extensions if isinstance(ext, CMakeExtension)]
        other_exts = [
            ext for ext in self.extensions if not isinstance(ext, CMakeExtension)
        ]

        build_dir = os.path.abspath(self.build_temp)
        os.makedirs(build_dir, exist_ok=True)

        for ext in cmake_exts:
            self.build_cmake(ext)

        if other_exts:
            original_exts = self.extensions
            try:
                self.extensions = other_exts
                super().run()
            finally:
                self.extensions = original_exts

        if enable_sparse() and is_ascend():
            gsa_build_script = "ucm/sparse/gsa_on_device/csrc/ascend/build.sh"
            args = []
            if PLATFORM == "ascend-a3":
                args.append("a3")
            if not is_only_build_mode():
                args.append("install")
            try:
                print(
                    f"Running {gsa_build_script} to compiling NPU custom ops for UCM..."
                )
                subprocess.check_call(["bash", gsa_build_script] + args)
                print(f"{gsa_build_script} executed successfully!")
            except subprocess.CalledProcessError as e:
                print("Error running {gsa_build_script}: {e}")
                raise SystemExit(e.returncode)

    def build_cmake(self, ext: CMakeExtension):
        build_dir = os.path.abspath(self.build_temp)
        install_dir = os.path.abspath(self.build_lib)
        if is_editable_mode():
            install_dir = ext.cmake_file_path

        cmake_args = [
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DPYTHON_EXECUTABLE={sys.executable}",
            f"-DPython_EXECUTABLE={sys.executable}",
            f"-DCMAKE_INSTALL_PREFIX={install_dir}",
        ]

        if RELEASE_SETTINGS is not None:
            cmake_args += [
                "-DUCM_RELEASE_BUILD=ON",
                f"-DUCM_RELEASE_PROFILE={RELEASE_SETTINGS['profile']}",
                f"-DUCM_RELEASE_SPEC_ID={RELEASE_SETTINGS['spec_id']}",
                f"-DUCM_RELEASE_SOURCE_SHA={RELEASE_SETTINGS['source_sha']}",
                f"-DUCM_RELEASE_VERSION={RELEASE_SETTINGS['version']}",
                f"-DUCM_RELEASE_BUILD_KEY={RELEASE_SETTINGS['build_key']}",
                f"-DSOURCE_DATE_EPOCH={RELEASE_SETTINGS['source_date_epoch']}",
                "-DUCM_RELEASE_REQUIRED_TARGETS="
                + ";".join(RELEASE_SETTINGS["required"]),
                "-DUCM_RELEASE_FORBIDDEN_TARGETS="
                + ";".join(RELEASE_SETTINGS["forbidden"]),
                "-DBUILD_UCM_MINDIE=OFF",
                "-DBUILD_UCM_SPARSE=OFF",
            ]

        if ENABLE_MINDIE:
            cmake_args += ["-DBUILD_UCM_MINDIE=ON"]
            cmake_args += [f"-DUCM_CXX11_ABI={UCM_CXX11_ABI}"]

        if enable_sparse():
            cmake_args += ["-DBUILD_UCM_SPARSE=ON"]

        if ENABLE_GDR:
            cmake_args += ["-DUCM_ENABLE_GDR_STREAM=ON"]

        if ASCEND_ROOT:
            cmake_args += [f"-DASCEND_ROOT={ASCEND_ROOT}"]

        build_cpu_arch = os.getenv("UCM_BUILD_CPU_ARCH")
        if PLATFORM == "ascend-a3" and build_cpu_arch:
            ascend_subdirectory = {
                "amd64": "x86_64-linux",
                "arm64": "aarch64-linux",
            }.get(build_cpu_arch)
            if ascend_subdirectory is None:
                raise RuntimeError(
                    f"unsupported build CPU architecture: {build_cpu_arch}"
                )
            cmake_args += [
                "-DASCEND_ARCH_DIR="
                f"/usr/local/Ascend/ascend-toolkit/latest/{ascend_subdirectory}"
            ]

        match PLATFORM:
            case "cuda":
                cmake_args += ["-DRUNTIME_ENVIRONMENT=cuda"]
            case "ascend":
                cmake_args += ["-DRUNTIME_ENVIRONMENT=ascend"]
            case "ascend-a3":
                cmake_args += ["-DRUNTIME_ENVIRONMENT=ascend-a3"]
            case "musa":
                cmake_args += ["-DRUNTIME_ENVIRONMENT=musa"]
            case "maca":
                cmake_args += ["-DRUNTIME_ENVIRONMENT=maca"]
                cmake_args += ["-DBUILD_UCM_SPARSE=OFF"]
            case _:
                cmake_args += ["-DRUNTIME_ENVIRONMENT=simu"]
                cmake_args += ["-DBUILD_UCM_SPARSE=OFF"]

        build_env = os.environ.copy()
        if RELEASE_SETTINGS is not None:
            build_env["SOURCE_DATE_EPOCH"] = str(RELEASE_SETTINGS["source_date_epoch"])
            build_env["TZ"] = "UTC"
        subprocess.check_call(
            ["cmake", *cmake_args, ext.cmake_file_path],
            cwd=build_dir,
            env=build_env,
        )
        subprocess.check_call(
            ["cmake", "--build", ".", "--config", "Release", "--", "-j8"],
            cwd=build_dir,
            env=build_env,
        )

        subprocess.check_call(
            ["cmake", "--install", ".", "--config", "Release", "--component", "ucm"],
            cwd=build_dir,
            env=build_env,
        )


def inject_pth():
    if not ("-e" in sys.argv or "develop" in sys.argv or "editable_wheel" in sys.argv):
        return

    import site

    pth_name = "ucm_patch.pth"
    source = os.path.abspath(pth_name)

    if not os.path.exists(source):
        print(f"Error: {pth_name} not found in root directory.")
        return

    try:
        try:
            site_packages = site.getsitepackages()[0]
        except AttributeError:
            from distutils.sysconfig import get_python_lib

            site_packages = get_python_lib()

        target = os.path.join(site_packages, pth_name)

        if not os.path.exists(target):
            if sys.platform == "win32":
                import shutil

                shutil.copy(source, target)
            else:
                os.symlink(source, target)
            print("Injection successful.")

    except Exception as e:
        print(f"\033[93mWarning: Failed to inject .pth for editable mode: {e}\033[0m")


setup(
    name=get_distribution_name(),
    version=get_package_version(),
    description="Unified Cache Management",
    author="Unified Cache Team",
    packages=[
        pkg
        for pkg in (find_packages() + [""])
        if ENABLE_MINDIE or not pkg.startswith("ucm.integration.mindie")
    ],
    package_dir={"": "."},
    python_requires=">=3.10",
    install_requires=get_install_requires(),
    ext_modules=[CMakeExtension(name="ucm", source_dir=ROOT_DIR)],
    cmdclass={"build_ext": CMakeBuild},
    zip_safe=False,
    include_package_data=False,
    package_data={
        "ucm": ["sparse/gsa_on_device/configs/**/*.json"],
        **({"ucm.integration.mindie": ["ucm_config.json"]} if ENABLE_MINDIE else {}),
        "": [
            "ucm_patch.pth",
            "ucm/integration/vllm/patch/**/*.patch",
        ],
    },
)
if any(arg in sys.argv for arg in ["-e", "develop", "editable_wheel"]):
    inject_pth()
