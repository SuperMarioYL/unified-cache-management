# Build UCM from source

Use a source build for development or an engine/backend combination that is
not published in [Installation](../user-guide/installation.md). Build inside
the target inference environment: the compiler, device toolkit, Python,
PyTorch, and engine must be compatible with one another.

## Prepare a checkout

Select a UCM branch or tag explicitly and record the resulting commit:

```bash
export UCM_REF=your-branch-or-tag
git clone --branch "$UCM_REF" https://github.com/ModelEngine-Group/unified-cache-management.git
cd unified-cache-management
git rev-parse HEAD
python --version
python -m pip install "setuptools>=64" wheel "cmake>=3.18"
```

Python 3.10 or newer is required by this source tree. Install the engine and
its device dependencies before building UCM. `--no-build-isolation` below
uses that environment; it does not install a compatible engine/toolkit for you.

## vLLM on CUDA { #vllm-cuda-platform }

Prepare a vLLM environment for the model and CUDA device you use, then build:

```bash
export PLATFORM=cuda
python -m pip install -v -e . --no-build-isolation
export ENABLE_UCM_PATCH=1
```

Continue with the [vLLM quickstart](../user-guide/quick_start/quickstart_vllm.md)
for configuration, service startup, and external-cache verification.

## vLLM-Ascend { #vllm-ascend-ascend-platform }

Prepare a matching vLLM-Ascend, PyTorch, CANN, and driver environment. Choose
the platform for the actual target rather than relying on autodetection:

```bash
export PLATFORM=ascend
# For Atlas A3, use: export PLATFORM=ascend-a3
python -m pip install -v -e . --no-build-isolation
export ENABLE_UCM_PATCH=1
```

Continue with the [vLLM-Ascend quickstart](../user-guide/quick_start/quickstart_vllm_ascend.md).

## SGLang on CUDA { #sglang-cuda-platform }

The current HiCache adapter recipe targets SGLang 0.5.9 and its zero-copy V1
storage interface. Build UCM in that engine environment:

```bash
export PLATFORM=cuda
python -m pip install -v -e . --no-build-isolation
```

Continue with the [SGLang quickstart](../user-guide/quick_start/quickstart_sglang.md).
The historical `Dockerfile.ucm-sglang-cuda-v0.5.5` targets a different engine
and patch path; building it does not validate the 0.5.9 HiCache recipe.

## MindIE-LLM on Ascend { #mindie-llm-ascend-platform }

Prepare MindIE-LLM 2.3.0 and its Ascend runtime. MindIE integration is included
only when `UCM_ENABLE_MINDIE=1` is set during the build. Determine the target
PyTorch C++ ABI in that same environment:

```bash
python -c "import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))"
```

Set `UCM_CXX11_ABI` to the reported `0` or `1` and confirm it also matches the
MindIE distribution, then install:

```bash
export PLATFORM=ascend
export UCM_ENABLE_MINDIE=1
export UCM_CXX11_ABI=1  # Replace with the matching target ABI.
python -m pip install -v -e . --no-build-isolation
```

The installed hook patches MindIE's Python modules when `mindie_llm` is first
imported. This modifies the installed engine package; use a dedicated engine
environment. Continue with the
[MindIE quickstart](../user-guide/quick_start/quickstart_mindie_llm.md) to configure
the service and verify KV writes and reads.

## Optional sparse build

Sparse Attention is not enabled by default. On a supported engine/platform,
set `ENABLE_SPARSE=true` before the source build. The build parser accepts
`true` case-insensitively; `ENABLE_SPARSE=1` is not the source-build switch.
Runtime configuration is algorithm-specific. Follow
[Sparse Attention](../user-guide/capabilities/sparse-attention/index.md) or
[ReRoPE](../user-guide/capabilities/rerope.md) with its recorded version constraints.
Do not apply an old patch to an arbitrary engine version.

## Build an image from the checkout

List the actual Dockerfiles in the selected revision and inspect the one for
your engine and accelerator:

```bash
ls docker/Dockerfile.*
```

Its `FROM`/image arguments and build script determine the engine environment.
Select a versioned file from that list, then build from the repository root:

```bash
export UCM_DOCKERFILE=docker/your-selected-Dockerfile
docker build -t ucm-local:dev -f "$UCM_DOCKERFILE" .
```

For MindIE, this revision provides
`docker/Dockerfile.ucm-mindie-ascend.a2-v2`, based on MindIE 2.3.0. It builds the
MindIE integration and applies the patch during image construction;
`--build-arg UCM_CXX11_ABI=0` overrides its ABI default when required.

Repository Dockerfiles and local builds are development inputs. Only artifacts
listed by [Installation](../user-guide/installation.md) have the corresponding
release publication record. Building or importing UCM locally does not replace
a service startup and external-cache check on the target hardware.
