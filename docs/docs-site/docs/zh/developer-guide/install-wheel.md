# 接入 wheel 安装脚本

使用 UCM 独立脚本 `scripts/install_ucm.sh`，在用户环境或接入项目的镜像构建中安装已发布 wheel。环境探测和选包逻辑由 UCM 维护，接入项目只负责获取脚本、传递参数和安排调用位置。

脚本需要 Bash、Python 3.10 及以上，以及该解释器中的 pip。它复用 pip 随附的 packaging，不需要克隆仓库、预装 UCM、GPU/NPU 或 Docker。安装目标使用 Linux 和 glibc，CPU 架构是否可用由**实际发布的兼容 wheel 标签**决定，不另设架构白名单。安装检查覆盖 Ubuntu、openEuler 的 AMD64/ARM64 环境。例如，包声明 `Requires-Python: >=3.10`，不代表它的 `cp312` 后端 wheel 可以用于 Python 3.10。

## 获取单个脚本

脚本版本与要安装的 UCM 包版本分别固定。将 `INSTALLER_REF` 替换为已评审脚本的完整 commit SHA。Fork 验证阶段使用下面的 fork 仓库；安装器合入 upstream 后，再使用官方仓库地址。

```bash
INSTALLER_REPOSITORY=SuperMarioYL/unified-cache-management
INSTALLER_REF='<full-commit-sha>'
curl --fail --location \
  "https://raw.githubusercontent.com/${INSTALLER_REPOSITORY}/${INSTALLER_REF}/scripts/install_ucm.sh" \
  --output install_ucm.sh
bash install_ucm.sh
```

默认参数为 `--version latest --extra auto`。`latest` 指当前环境具有兼容元包和后端 wheel 的最高已发布 UCM 版本，包含 RC 等预发布版本；排除 dev/nightly 版本和撤回文件。版本按 PEP 440 排序，例如 `0.7.0 < 0.8.0rc1 < 0.8.0rc2 < 0.8.0`。源码标签不决定包是否可用，脚本始终不进行源码构建。

## 使用参数

```bash
# 使用指定解释器及其 pip 配置。
PYTHON=/path/to/python3.12 bash install_ucm.sh

# 固定 UCM 版本和已发布的后端 extra。
bash install_ucm.sh --version 0.7.0 --extra cann910-a2

# 只解析，不安装任何包。
bash install_ucm.sh --resolve > ucm-selection.json
```

精确版本可以是已发布的预发布版本；不可用时不会换成其他版本。即使精确指定版本，也会排除撤回的文件。`--extra` 接受单个后端名称，例如 `cann910-a3`、`cu129`，不接受 extra 列表或仅安装 `toolkit` 等辅助功能。

`PYTHON` 指向一个可执行文件，默认是 `python3`。环境探测和安装始终使用同一个解释器。安装通过该解释器的 `pip install --only-binary=:all:` 执行固定 requirement，继承 pip 的软件源、代理、证书等配置；版本发现始终访问官方 PyPI。如果配置的镜像源缺少选中的包，pip 会失败，脚本不会更换软件源或退回旧版本掩盖问题。普通依赖解析仍由 pip 完成。

`--resolve` 向 stdout 输出一个 JSON 对象，固定字段如下：

| 字段 | 含义 |
| --- | --- |
| `version` | 选中的 UCM 元包版本 |
| `extra` | 选中的后端 extra |
| `requirement` | 固定的 `uc-manager[extra]==version` |
| `backend_requirement` | 发布依赖声明中的后端包名和版本 |
| `wheel` | 匹配当前环境的后端 wheel 文件名 |

诊断输出到 stderr，失败返回非零退出码。JSON 用于记录选包结果，不是全部传递依赖的锁文件，也不代表硬件兼容性已经验证。

## 环境识别与选包规则

脚本将实际 Python/ABI、CPU 架构和 glibc 与 wheel 标签、`Requires-Python` 比较。操作系统名称用于诊断，不作为允许安装的白名单。版本、extras、依赖和 wheel 元数据均来自官方 PyPI，不使用 UCM 的 vLLM-Ascend 支持声明过滤。

Ascend 环境应先配置活动 Toolkit。`ASCEND_HOME_PATH`、`ASCEND_TOOLKIT_HOME` 用于定位它；未设置时检查标准 Toolkit 位置。CANN 版本从 Toolkit 安装元数据读取，不采用镜像 tag、目录名称、`CANN_VERSION`、驱动元数据或 `npu-smi`。`SOC_VERSION` 用于区分 A2（`ascend910b…`）和 A3（`ascend910_93…`）；SoC 缺失或无法识别时，需要显式指定 extra。

CUDA 通过 `CUDA_HOME`、`CUDA_PATH` 定位活动 Toolkit；未设置时检查 `/usr/local/cuda` 和 `PATH` 中 `nvcc` 所属的 Toolkit。优先读取 Toolkit 版本元数据，必要时执行该 Toolkit 的 `nvcc --version`，不使用 `nvidia-smi` 或驱动宣称支持的 CUDA 版本。

自动模式下，无法识别 Toolkit、元数据冲突或同时存在两类加速器运行时，都会报错。显式 extra 跳过自动 Toolkit/SoC 探测，但仍检查 Python、架构、glibc 和 wheel 是否可用，适用于调用方已明确目标后端的无设备构建环境。

对于每个候选 UCM 版本，脚本先保留有兼容 wheel 的后端；Ascend 还必须保持同一 A2/A3 类型，然后按下表选择：

| 当前 Toolkit 与可选后端版本的关系 | 选择 |
| --- | --- |
| 精确匹配 | 精确版本对应的后端 |
| 低于可选范围 | 最低档 |
| 位于两档之间 | 不高于当前版本的最高档 |
| 高于可选范围 | 最高档 |

CANN 比较主、次、补丁版本；CUDA 比较主、次版本，采用相同区间规则，也可能跨 CUDA 主版本选档。这些规则只决定安装哪个包，不安装或改变 Toolkit、驱动，也不保证原生库能加载或 GPU/NPU 功能能运行。脚本会输出实际选档关系，便于检查。

维护脚本时，环境事实、PyPI 读取、候选发现和后端选档分别负责自己的逻辑。`candidate_versions` 定义发布版本策略；`compatible_backends` 跟随发布依赖并检查 wheel 兼容性；`select_backend` 只执行区间选档，不联网、不安装。后端候选用明确字段区分 Toolkit 版本与 UCM 包版本。

Toolkit 路径、SoC 家族映射和 extra 名称编码仍属于外部接口约定。PyPI 当前没有提供 SoC 对应关系或独立的 Toolkit 版本字段：`extra_runtime` 解析 `cann910-a2`、`cu129` 这样的紧凑名称，现有发布名称中的次版本和补丁版本使用一位数字。修改这种编码需要调整发布协议，不能靠新增版本白名单解决。CI 中固定的镜像只是可复现的测试输入，不参与安装时的选包。

## 在 Docker 构建中调用

构建前下载已评审版本的脚本，将该单文件放入构建上下文：

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG UCM_VERSION=latest
ARG UCM_EXTRA=auto
COPY install_ucm.sh /tmp/install_ucm.sh
RUN bash /tmp/install_ucm.sh --version "${UCM_VERSION}" --extra "${UCM_EXTRA}" \
    && rm /tmp/install_ucm.sh
```

基础镜像应提供 Bash 和 Python/pip；自动模式还需要可读的 Toolkit 元数据，以及 Ascend 目标的 `SOC_VERSION`。如果镜像需要初始化 Toolkit 环境，先按镜像既有方式完成初始化。探测和安装不需要挂载设备。

Motor 等接入项目继续负责自己的构建参数、缓存策略、各架构构建和 manifest 合并，不复制选包规则。不同环境独立执行 `latest` 可能得到不同版本；命中 Docker 缓存的安装层也不会重新发现新版本。需要一致结果时传入固定版本和 extra，缓存更新由调用项目控制。

UCM 根目录的 `install.sh` 仍是安装 wheel 后执行的钩子，与这个独立入口无关。

## 分层验收

脚本测试不需要安装 UCM 或接入加速卡：

```bash
bash -n scripts/install_ucm.sh
shellcheck scripts/install_ucm.sh
python3 -m pytest -q scripts/tests
```

安装器 CI 另外在 AMD64/ARM64 的 Ubuntu/openEuler Ascend 环境及 CUDA Toolkit 环境中安装真实发布的 wheel，不挂载加速卡，并核对安装后的包版本与解析结果。这些检查不等于原生库加载或推理验收。

在准备好的目标环境中，可以显式执行版本与导入检查：

```bash
python3 -c 'from importlib.metadata import version; print(version("uc-manager"))'
python3 -c 'import ucm; from ucm.store.pipeline import ucmpipelinestore; print(ucm.__file__)'
```

还应单独检查 `backend_requirement` 对应的后端包版本。原生链接检查应加载实际部署使用的库，例如：

```python
import ctypes
import os
from pathlib import Path
import ucm

for relative in ("cache/libcachestore.so", "posix/libposixstore.so"):
    ctypes.CDLL(str(Path(ucm.__file__).parent / "store" / relative),
                mode=os.RTLD_NOW | os.RTLD_LOCAL)
```

原生链接检查需要对应的运行库。真实 GPU/NPU 验收还需要硬件、驱动、目标引擎，以及 UCM 缓存读写或推理工作负载。脚本测试、包安装、原生加载和硬件运行应分别报告。
