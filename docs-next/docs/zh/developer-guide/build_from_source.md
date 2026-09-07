# 从源码构建 UCM

开发、定制，或[安装](../user-guide/installation.md)中没有所需引擎与后端组合时，可以从源码构建。请在目标推理环境中构建，确保编译器、设备工具链、Python、PyTorch 和推理引擎彼此兼容。

## 准备源码

显式选择 UCM 分支或标签，并记录最终提交：

```bash
export UCM_REF=your-branch-or-tag
git clone --branch "$UCM_REF" https://github.com/ModelEngine-Group/unified-cache-management.git
cd unified-cache-management
git rev-parse HEAD
python --version
python -m pip install "setuptools>=64" wheel "cmake>=3.18"
```

当前源码要求 Python 3.10 或更新版本。构建 UCM 前先安装引擎及其设备依赖。下面的 `--no-build-isolation` 会使用当前环境，不会自动准备兼容的引擎或工具链。

## vLLM（CUDA） { #vllm-cuda-platform }

为所用模型和 CUDA 设备准备好 vLLM 环境，然后构建：

```bash
export PLATFORM=cuda
python -m pip install -v -e . --no-build-isolation
export ENABLE_UCM_PATCH=1
```

继续按 [vLLM 快速开始](../user-guide/quick_start/quickstart_vllm.md)配置、启动服务，并验证外部缓存。

## vLLM-Ascend { #vllm-ascend-ascend-platform }

准备相互匹配的 vLLM-Ascend、PyTorch、CANN 和驱动环境，并显式选择目标平台：

```bash
export PLATFORM=ascend
# For Atlas A3, use: export PLATFORM=ascend-a3
python -m pip install -v -e . --no-build-isolation
export ENABLE_UCM_PATCH=1
```

继续阅读 [vLLM-Ascend 快速开始](../user-guide/quick_start/quickstart_vllm_ascend.md)。

## SGLang（CUDA） { #sglang-cuda-platform }

当前 HiCache 适配示例使用 SGLang 0.5.9 及其零拷贝 V1 存储接口。在该引擎环境中构建 UCM：

```bash
export PLATFORM=cuda
python -m pip install -v -e . --no-build-isolation
```

继续阅读 [SGLang 快速开始](../user-guide/quick_start/quickstart_sglang.md)。历史文件 `Dockerfile.ucm-sglang-cuda-v0.5.5` 对应另一引擎版本和补丁路径，成功构建它不代表验证了 0.5.9 的 HiCache 接入方式。

## MindIE-LLM（昇腾） { #mindie-llm-ascend-platform }

准备 MindIE-LLM 2.3.0 和对应的 Ascend runtime。只有在构建时设置 `UCM_ENABLE_MINDIE=1` 才会包含 MindIE 集成。在同一环境中检查目标 PyTorch 的 C++ ABI：

```bash
python -c "import torch; print(int(torch._C._GLIBCXX_USE_CXX11_ABI))"
```

将 `UCM_CXX11_ABI` 设为输出的 `0` 或 `1`，并确认它也匹配 MindIE 发行包，再执行安装：

```bash
export PLATFORM=ascend
export UCM_ENABLE_MINDIE=1
export UCM_CXX11_ABI=1  # Replace with the matching target ABI.
python -m pip install -v -e . --no-build-isolation
```

安装后的 hook 在首次导入 `mindie_llm` 时为 MindIE Python 模块应用补丁。此操作会修改已安装的引擎包，因此请使用专门的引擎环境。继续按 [MindIE 快速开始](../user-guide/quick_start/quickstart_mindie_llm.md)配置服务并验证 KV 写入和读取。

## 可选的稀疏注意力构建

默认不启用 Sparse Attention。在受支持的引擎和平台上，源码构建前设置 `ENABLE_SPARSE=true`。构建参数解析不区分 `true` 的大小写；`ENABLE_SPARSE=1` 不是源码构建开关。

运行时配置取决于具体算法。请按[稀疏注意力](../user-guide/capabilities/sparse-attention/index.md)或 [ReRoPE](../user-guide/capabilities/rerope.md)中的版本要求操作，不要将旧补丁应用到任意引擎版本。

## 从当前源码构建镜像

列出所选修订版本中实际存在的 Dockerfile，检查与目标引擎和加速器对应的文件：

```bash
ls docker/Dockerfile.*
```

文件的 `FROM`、镜像参数和构建脚本决定引擎环境。选择其中一个明确版本的文件，在仓库根目录构建：

```bash
export UCM_DOCKERFILE=docker/your-selected-Dockerfile
docker build -t ucm-local:dev -f "$UCM_DOCKERFILE" .
```

当前修订提供基于 MindIE 2.3.0 的 `docker/Dockerfile.ucm-mindie-ascend.a2-v2`。它构建 MindIE 集成，并在镜像构建时应用补丁；需要时可用 `--build-arg UCM_CXX11_ABI=0` 覆盖默认 ABI。

仓库 Dockerfile 和本地构建用于开发。只有[安装](../user-guide/installation.md)中列出的制品才有对应的发布记录。本地构建成功或成功导入 UCM，仍需在目标硬件上完成服务启动和外部缓存验证。
