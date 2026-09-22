# Integrate the wheel installer

Use UCM's standalone `scripts/install_ucm.sh` to install a published wheel in a user environment or a downstream project's image build. UCM owns environment detection and package selection; the integrating project only downloads the script, passes parameters, and chooses when to run it.

The script needs Bash, Python 3.10 or newer, and pip in that interpreter. It uses pip's bundled packaging library and does not need a checkout, an existing UCM installation, a GPU/NPU, or Docker. Installation targets use Linux and glibc; architecture availability is determined by **actually published, compatible wheel tags**, not an architecture allowlist. AMD64/ARM64 on Ubuntu and openEuler are covered by the installation checks. For example, a package's `Requires-Python: >=3.10` does not make its `cp312` backend wheel usable with Python 3.10.

## Obtain one file

Pin the installer revision separately from the UCM package version. Replace `INSTALLER_REF` with the full commit SHA of the installer you have reviewed. During fork validation, use the fork repository shown below; use the upstream repository after the installer has been merged there.

```bash
INSTALLER_REPOSITORY=SuperMarioYL/unified-cache-management
INSTALLER_REF='<full-commit-sha>'
curl --fail --location \
  "https://raw.githubusercontent.com/${INSTALLER_REPOSITORY}/${INSTALLER_REF}/scripts/install_ucm.sh" \
  --output install_ucm.sh
bash install_ucm.sh
```

The default is `--version latest --extra auto`. `latest` means the highest published UCM version with compatible meta and backend wheels for the current environment, including prereleases such as RC. Development/nightly versions and yanked files are excluded. Versions follow PEP 440 ordering: `0.7.0 < 0.8.0rc1 < 0.8.0rc2 < 0.8.0`. Source tags do not determine availability, and the installer never builds from source.

## Choose parameters

```bash
# Use another interpreter and its pip configuration.
PYTHON=/path/to/python3.12 bash install_ucm.sh

# Fix the UCM version and published backend extra.
bash install_ucm.sh --version 0.7.0 --extra cann910-a2

# Resolve without installing anything.
bash install_ucm.sh --resolve > ucm-selection.json
```

An exact version may be a published prerelease. It is never replaced with another version if unavailable. Yanked files are excluded even for an exact version. `--extra` accepts one published backend extra, such as `cann910-a3` or `cu129`; it is not a list of optional features such as `toolkit`.

`PYTHON` names one executable; the default is `python3`. Both detection and installation use that interpreter. Package installation runs its `pip install --only-binary=:all:` with a pinned requirement and inherits pip's index, proxy, certificate and other configuration. Discovery always reads official PyPI. If a configured mirror lacks the selected packages, pip fails; the installer does not change indexes or select an older version to hide the failure. Ordinary dependency resolution remains pip's responsibility.

`--resolve` writes one JSON object to stdout. Its stable fields are:

| Field | Meaning |
| --- | --- |
| `version` | Selected UCM meta-package version |
| `extra` | Selected backend extra |
| `requirement` | Pinned `uc-manager[extra]==version` requirement |
| `backend_requirement` | Backend distribution and version from the published dependency declaration |
| `wheel` | Compatible backend wheel filename |

Diagnostics go to stderr. Failures return a nonzero exit code. This JSON records the selection; it does not lock every transitive dependency or prove hardware compatibility.

## Environment and backend selection

Python/ABI, CPU architecture and glibc are checked against wheel tags and `Requires-Python`. The operating-system name is diagnostic information, not an allowlist. The installer reads available versions, extras, dependencies and wheel metadata from official PyPI; it does not consult UCM's vLLM-Ascend support declarations.

For Ascend, configure the active Toolkit environment before running the installer. `ASCEND_HOME_PATH` and `ASCEND_TOOLKIT_HOME` locate it; otherwise the script checks the standard Toolkit locations. The version comes from Toolkit installation metadata, never an image tag, a directory name, `CANN_VERSION`, driver metadata or `npu-smi`. `SOC_VERSION` identifies A2 (`ascend910b…`) or A3 (`ascend910_93…`). Missing or unknown SoC information requires an explicit extra.

For CUDA, `CUDA_HOME` or `CUDA_PATH` identifies the active Toolkit. Otherwise the script checks `/usr/local/cuda` and the Toolkit containing `nvcc` on `PATH`. It reads Toolkit version metadata, using that Toolkit's `nvcc --version` when needed. It does not use `nvidia-smi` or the driver's advertised CUDA capability.

Automatic selection fails if neither Toolkit is identifiable, metadata conflicts, or both accelerator families are present. An explicit extra bypasses automatic Toolkit/SoC detection; Python, architecture, glibc and wheel availability are still checked. This is useful for device-less build environments whose intended backend is known to the caller.

Within the selected release, the installer first retains backends with compatible wheels. Ascend candidates must stay within the detected A2/A3 family. It then chooses:

| Detected Toolkit versus available backend versions | Selection |
| --- | --- |
| Exact match | Exact backend |
| Below the available range | Lowest backend |
| Between available versions | Highest backend not above the detected version |
| Above the available range | Highest backend |

CANN compares major/minor/patch; CUDA compares major/minor and follows the same range rule, including possible selection across CUDA major versions. These rules select a package only. They do not install or change a Toolkit or driver, and do not establish that native libraries will load or that GPU/NPU operations will work. The selected mapping is printed for review.

When maintaining the installer, keep environment facts, PyPI access, candidate discovery and backend ranking separate. `candidate_versions` defines the release policy; `compatible_backends` follows published dependencies and checks wheel compatibility; `select_backend` applies the range rule without network or installation side effects. Backend candidates distinguish the Toolkit version from the UCM package version explicitly.

Toolkit paths, SoC family mappings and extra-name encoding remain external interface conventions. PyPI currently does not provide a SoC mapping or a separate Toolkit-version field: `extra_runtime` decodes compact names such as `cann910-a2` and `cu129`, whose published minor/patch components currently use one digit. Changing that encoding requires a publication-contract change, not a new hardcoded version list. Fixed CI images are reproducible test inputs and do not participate in package selection.

## Call from a Docker build

Download a reviewed installer revision before building, then copy that single file into the build context:

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG UCM_VERSION=latest
ARG UCM_EXTRA=auto
COPY install_ucm.sh /tmp/install_ucm.sh
RUN bash /tmp/install_ucm.sh --version "${UCM_VERSION}" --extra "${UCM_EXTRA}" \
    && rm /tmp/install_ucm.sh
```

The base image must provide Bash, Python/pip and, for automatic detection, readable Toolkit metadata and the intended Ascend `SOC_VERSION`. Set up its normal Toolkit environment first if necessary. No device mounts are needed for selection or installation.

Motor and other callers retain their own build arguments, cache policy, architecture builds and manifest assembly. They should not copy the selection rules. Independent `latest` executions can select different versions, and a cached Docker layer does not re-run discovery. Pass fixed version/extra values when builds must use the same selection, and manage cache invalidation in the calling project.

The root-level UCM `install.sh` remains a post-wheel installation hook; it is unrelated to this standalone entry point.

## Verification boundaries

Run script tests without an installed UCM or accelerator:

```bash
bash -n scripts/install_ucm.sh
shellcheck scripts/install_ucm.sh
python3 -m pytest -q scripts/tests
```

The installer CI separately installs real published wheels in AMD64/ARM64 Ubuntu/openEuler Ascend environments and a CUDA Toolkit environment, without accelerator devices. It checks installed distribution versions against the resolved requirements. These checks do not constitute native-library or inference acceptance.

In a prepared target environment, explicitly check versions and imports after installation:

```bash
python3 -c 'from importlib.metadata import version; print(version("uc-manager"))'
python3 -c 'import ucm; from ucm.store.pipeline import ucmpipelinestore; print(ucm.__file__)'
```

Check the distribution named by `backend_requirement` separately. To test native linking, load the libraries used by your deployment, for example:

```python
import ctypes
import os
from pathlib import Path
import ucm

for relative in ("cache/libcachestore.so", "posix/libposixstore.so"):
    ctypes.CDLL(str(Path(ucm.__file__).parent / "store" / relative),
                mode=os.RTLD_NOW | os.RTLD_LOCAL)
```

Native linking requires the corresponding runtime libraries. Real GPU/NPU acceptance additionally requires hardware, drivers, the target engine and a UCM cache save/load or inference workload. Report script tests, package installation, native loading and hardware execution as separate results.
