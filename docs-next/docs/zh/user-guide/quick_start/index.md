---
hide:
  - toc
---

# 快速开始

先确认引擎、设备和后端，在下方选择对应的 Wheel、镜像或 Chart，并复制完整命令。选择器显示实际制品版本；可安装组合以本页加载的发布清单为准。需要定制或没有匹配制品时，使用[源码构建](../../developer-guide/build_from_source.md)。

安装 Wheel 时，使用独立的 Python 环境，并且只安装一个 backend extra。不同后端共用 `ucm` 导入命名空间。

<div id="ucm-install-app" class="ucm-install" data-locale="zh">
  <p class="ucm-install__status" data-install-status aria-live="polite">
    正在加载当前发布清单……
  </p>
  <div class="ucm-selector" data-install-selector></div>
  <section class="ucm-install__output" data-install-output aria-live="polite"></section>
</div>

<noscript>
  请启用 JavaScript，以加载发布清单并生成安装命令。
</noscript>

## 配置说明

下方按引擎给出安装、最小配置、启动和验证步骤。调整后端或参数时，按需要查阅：

| 要了解的内容 | 文档入口 | 说明范围 |
| --- | --- | --- |
| 选择并配置存储后端 | 开发者指南：[缓存配置](../../developer-guide/cache-configuration/index.md) | 后端选择、依赖准备和配置方法 |
| 查询具体参数 | 参考：[配置参数](../../reference/config-parameters.md) | 字段归属、类型、默认值和取值约束 |

=== "vLLM (CUDA)"

    ## vLLM（CUDA） {#vllm}

    本指南介绍如何在 CUDA 平台上安装 UCM，并接入 vLLM。

    ### 安装 UCM {#vllm-ucm}

    下面两种方式任选其一。

    #### 方式一：使用 Docker {#vllm-docker}

    镜像地址见 [UCM Releases](https://github.com/ModelEngine-Group/unified-cache-management/releases)。将 `<image_tag>` 替换为发布页列出的完整标签，其中包含引擎版本和 UCM 版本。

    使用以下命令启动容器。
    ```bash
    # Use `--ipc=host` to make sure the shared memory is large enough.
    docker run --rm \
        --gpus all \
        --network=host \
        --ipc=host \
        -v <path_to_your_models>:/home/model \
        -v <path_to_your_storage>:/home/storage \
        --name <name_of_your_container> \
        -it ghcr.io/modelengine-group/vllm-openai:<image_tag>

    ```

    从源码构建 UCM Docker 镜像，参见[从源码构建和安装 UCM](../../developer-guide/build_from_source.md)。

    #### 方式二：使用 pip 安装 {#vllm-wheel}

    从 [PyPI](https://pypi.org/project/uc-manager/) 安装 `uc-manager`：

    ```bash
    export PLATFORM=cuda
    pip install uc-manager
    ```

    PyPI 当前版本为 0.5.0，自动补丁支持 vLLM / vLLM-Ascend 0.11.0 和 0.18.0。PyPI 提供源码包，安装前需要准备对应引擎和平台编译环境。

    安装后按下面的步骤创建配置文件。需要从仓库构建时，参见[源码构建](../../developer-guide/build_from_source.md)。

    ### 配置缓存 {#vllm-_1}

    创建可写的 `/home/storage` 目录；使用容器时将其挂载到持久存储。把以下配置保存为引擎可读取的 `/etc/ucm/ucm.yaml`：

    ```bash
    mkdir -p /etc/ucm /home/storage
    cat > /etc/ucm/ucm.yaml <<'YAML'
    ucm_connectors:
      - ucm_connector_name: UcmPipelineStore
        ucm_connector_config:
          store_pipeline: "Cache|Posix"
          storage_backends: /home/storage
          cache_buffer_capacity_gb: 4
          timeout_ms: 30000
          io_direct: false
    enable_metrics: true
    YAML
    ```

    这里显式分配 4 GiB 主机缓存，适用于小规模示例，并非运行时默认值。未共享 buffer 的 worker 各自分配内存；启用共享 buffer 时需按共享分配方式预留容量。`io_direct: false` 便于先验证文件系统路径；实际部署的 Direct I/O 和容量配置参见 [Pipeline Store](../../developer-guide/cache-configuration/pipeline.md)。

    ### 启动服务 {#vllm-_2}

    将 `MODEL_ID` 设为本地模型路径或模型仓库 ID，并根据模型的硬件需求调整张量并行度和最大上下文长度。

    ```bash
    export MODEL_ID=/home/model/your-model
    export ENABLE_UCM_PATCH=1
    vllm serve "$MODEL_ID" \
      --served-model-name ucm-example \
      --tensor-parallel-size 1 \
      --max-model-len 4096 \
      --block-size 128 \
      --port 7800 \
      --enforce-eager \
      --kv-transfer-config '{
        "kv_connector": "UCMConnector",
        "kv_connector_module_path": "ucm.integration.vllm.ucm_connector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {"UCM_CONFIG_FILE": "/etc/ucm/ucm.yaml"}
      }'
    ```

    运行时 patch hook 和 connector 都需要启用。检查启动日志是否包含 `create UcmPipelineStore with config:` 以及预期的存储路径。

    ### 验证服务与外部缓存 {#vllm-verify-the-service-and-external-cache}

    HTTP 服务就绪说明服务已启动；还需要单独确认 UCM 缓存命中。

    ```bash
    curl --fail http://127.0.0.1:7800/health
    curl --fail http://127.0.0.1:7800/v1/models
    ```

    在另一宿主机终端生成可重复使用的提示词，使其长度超过数个 128-token 块，然后发送请求：

    ```bash
    python3 - <<'PYREQUEST'
    import json
    from pathlib import Path
    Path('/tmp/ucm-request.json').write_text(json.dumps({
        "model": "ucm-example",
        "prompt": "Explain how a shared external cache reuses previous computation. " * 128,
        "max_tokens": 32,
        "temperature": 0,
    }))
    PYREQUEST
    curl --fail http://127.0.0.1:7800/v1/completions \
      -H 'Content-Type: application/json' --data-binary @/tmp/ucm-request.json
    curl --fail http://127.0.0.1:7800/metrics | grep '^ucm:'
    ```

    等待保存完成后，保留测试缓存并以相同配置重启，重放同一请求。确认外部命中、加载完成和输出正确性，具体信号及判断方法见[验证外部缓存](../observability/verify-cache.md)。

    没有命中或加载失败时，按[故障排查](../../reference/troubleshooting.md)检查配置、存储和内存预算。

    ### 使用其他模型 {#vllm-_3}

    在[模型教程](../model-tour/index.md)中找到引擎官方配置，保留模型特定的引擎参数，再加入上面的 UCM connector 配置。

=== "vLLM-Ascend (NPU)"

    ## vLLM-Ascend（NPU） {#vllm-ascend}

    本指南介绍如何在昇腾平台上安装 UCM，并接入 vLLM-Ascend。

    ### 安装 UCM {#vllm-ascend-ucm}

    下面两种方式任选其一。

    #### 方式一：使用 pip 安装 {#vllm-ascend-wheel}

    从 [PyPI](https://pypi.org/project/uc-manager/) 安装 `uc-manager`：

    ```bash
    export PLATFORM=ascend
    pip install uc-manager
    ```

    PyPI 当前版本为 0.5.0，自动补丁支持 vLLM / vLLM-Ascend 0.11.0 和 0.18.0。PyPI 提供源码包，安装前需要准备对应引擎和平台编译环境。

    安装后按下面的步骤创建配置文件。需要从仓库构建时，参见[源码构建](../../developer-guide/build_from_source.md)。

    #### 方式二：使用 Docker {#vllm-ascend-docker}

    ##### 预构建镜像 {#vllm-ascend-_1}

    镜像地址见 [UCM Releases](https://github.com/ModelEngine-Group/unified-cache-management/releases)。将 `<image_tag>` 替换为与设备和 CANN 版本匹配的完整标签。

    ```bash
    # Update DEVICE according to your device (/dev/davinci[0-7])
    export DEVICE=/dev/davinci7
    # Update the vllm-ascend image
    docker run --rm \
        --network=host \
        --device $DEVICE \
        --device /dev/davinci_manager \
        --device /dev/devmm_svm \
        --device /dev/hisi_hdc \
        -v /usr/local/dcmi:/usr/local/dcmi \
        -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
        -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
        -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
        -v /etc/ascend_install.info:/etc/ascend_install.info \
        -v /root/.cache:/root/.cache \
        -v <path_to_your_models>:/app/model \
        -v <path_to_your_storage>:/app/storage \
        --name <name_of_your_container> \
        -it ghcr.io/modelengine-group/vllm-ascend:<image_tag> bash
    ```

    ### 配置缓存 {#vllm-ascend-_2}

    创建可写的 `/app/storage` 目录；使用容器时将其挂载到持久存储。把以下配置保存为引擎可读取的 `/etc/ucm/ucm.yaml`：

    ```bash
    mkdir -p /etc/ucm /app/storage
    cat > /etc/ucm/ucm.yaml <<'YAML'
    ucm_connectors:
      - ucm_connector_name: UcmPipelineStore
        ucm_connector_config:
          store_pipeline: "Cache|Posix"
          storage_backends: /app/storage
          cache_buffer_capacity_gb: 4
          timeout_ms: 30000
          io_direct: false
    enable_metrics: true
    YAML
    ```

    这里显式分配 4 GiB 主机缓存，适用于小规模示例，并非运行时默认值。未共享 buffer 的 worker 各自分配内存；启用共享 buffer 时需按共享分配方式预留容量。`io_direct: false` 便于先验证文件系统路径；实际部署的 Direct I/O 和容量配置参见 [Pipeline Store](../../developer-guide/cache-configuration/pipeline.md)。

    ### 启动服务 {#vllm-ascend-_3}

    将 `MODEL_ID` 设为本地模型路径或模型仓库 ID，并根据模型的硬件需求调整张量并行度和最大上下文长度。

    ```bash
    export MODEL_ID=/app/model/your-model
    export ENABLE_UCM_PATCH=1
    vllm serve "$MODEL_ID" \
      --served-model-name ucm-example \
      --tensor-parallel-size 1 \
      --max-model-len 4096 \
      --block-size 128 \
      --port 7800 \
      --enforce-eager \
      --kv-transfer-config '{
        "kv_connector": "UCMConnector",
        "kv_connector_module_path": "ucm.integration.vllm.ucm_connector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {"UCM_CONFIG_FILE": "/etc/ucm/ucm.yaml"}
      }'
    ```

    运行时 patch hook 和 connector 都需要启用。检查启动日志是否包含 `create UcmPipelineStore with config:` 以及预期的存储路径。

    ### 验证服务与外部缓存 {#vllm-ascend-verify-the-service-and-external-cache}

    HTTP 服务就绪说明服务已启动；还需要单独确认 UCM 缓存命中。

    ```bash
    curl --fail http://127.0.0.1:7800/health
    curl --fail http://127.0.0.1:7800/v1/models
    ```

    在另一宿主机终端生成可重复使用的提示词，使其长度超过数个 128-token 块，然后发送请求：

    ```bash
    python3 - <<'PYREQUEST'
    import json
    from pathlib import Path
    Path('/tmp/ucm-request.json').write_text(json.dumps({
        "model": "ucm-example",
        "prompt": "Explain how a shared external cache reuses previous computation. " * 128,
        "max_tokens": 32,
        "temperature": 0,
    }))
    PYREQUEST
    curl --fail http://127.0.0.1:7800/v1/completions \
      -H 'Content-Type: application/json' --data-binary @/tmp/ucm-request.json
    curl --fail http://127.0.0.1:7800/metrics | grep '^ucm:'
    ```

    等待保存完成后，保留测试缓存并以相同配置重启，重放同一请求。确认外部命中、加载完成和输出正确性，具体信号及判断方法见[验证外部缓存](../observability/verify-cache.md)。

    没有命中或加载失败时，按[故障排查](../../reference/troubleshooting.md)检查配置、存储和内存预算。

    ### 使用其他模型 {#vllm-ascend-_4}

    在[模型教程](../model-tour/index.md)中找到引擎官方配置，保留模型特定的引擎参数，再加入上面的 UCM connector 配置。

=== "SGLang (CUDA)"

    ## SGLang（CUDA） {#sglang}

    本指南介绍如何在 CUDA 平台上安装 UCM，并接入 SGLang。

    ### 安装 UCM {#sglang-ucm}

    #### 方式一：使用 Docker {#sglang-docker}

    ##### SGLang 镜像 {#sglang-sglang}

    当前 [UCM Release](https://github.com/ModelEngine-Group/unified-cache-management/releases)未提供 SGLang 镜像。使用下面的官方 SGLang 镜像，进入容器后按下一节从 PyPI 安装 UCM。

    ```bash
    docker pull lmsysorg/sglang:v0.5.9
    ```

    然后使用以下命令启动容器。
    ```bash
    # Use `--ipc=host` to make sure the shared memory is large enough.
    docker run --rm \
        --gpus all \
        --network=host \
        --ipc=host \
        -v <path_to_your_models>:/home/model \
        -v <path_to_your_storage>:/home/storage \
        --name <name_of_your_container> \
        -it lmsysorg/sglang:v0.5.9
    ```

    从源码构建 UCM Docker 镜像，参见[从源码构建和安装 UCM](../../developer-guide/build_from_source.md)。

    #### 方式二：使用 pip 安装 {#sglang-wheel}

    从 [PyPI](https://pypi.org/project/uc-manager/) 安装 `uc-manager`：

    ```bash
    export PLATFORM=cuda
    pip install uc-manager
    ```

    先准备 SGLang 0.5.9 环境。PyPI 当前提供 UCM 0.5.0 源码包，安装时需要 CUDA 编译环境。

    安装后按下面的步骤创建配置文件。需要从仓库构建时，参见[源码构建](../../developer-guide/build_from_source.md)。

    ### 配置 HiCache {#sglang-hicache}

    创建可写的持久目录 `/home/storage`。适配器要求 `page_first` 主机内存布局和 `interface_v1`，请勿改成旧版需要拷贝的存储 API。

    ```bash
    export MODEL_ID=/home/model/your-model
    HICACHE_CONFIG='{
      "backend_name": "unifiedcache",
      "module_path": "ucm.integration.sglang.unifiedcache_store",
      "class_name": "UnifiedCacheStore",
      "interface_v1": 1,
      "kv_connector_extra_config": {
        "ucm_connector_name": "UcmPipelineStore",
        "ucm_connector_config": {
          "storage_backends": "/home/storage",
          "io_direct": false,
          "timeout_ms": 30000
        }
      }
    }'
    ```

    ### 启动在线服务 {#sglang-online-inference}

    ```bash
    python3 -m sglang.launch_server \
      --model-path "$MODEL_ID" \
      --served-model-name ucm-example \
      --tensor-parallel-size 1 \
      --page-size 128 \
      --port 7800 \
      --enable-hierarchical-cache \
      --hicache-mem-layout page_first \
      --hicache-write-policy write_through \
      --hicache-storage-backend dynamic \
      --hicache-storage-prefetch-policy wait_complete \
      --hicache-storage-backend-extra-config "$HICACHE_CONFIG"
    ```

    按可用 GPU 调整模型与并行度。适配器负责提供块布局参数并选择 `Posix`；这套输入接口与包含 `ucm_connectors` 的 vLLM YAML 配置不同。

    ### 验证服务与外部缓存 {#sglang-verify-the-service-and-external-cache}

    ```bash
    curl --fail http://127.0.0.1:7800/health
    curl --fail http://127.0.0.1:7800/v1/models
    python3 - <<'PYREQUEST'
    import json
    from pathlib import Path
    Path('/tmp/ucm-request.json').write_text(json.dumps({
        "model": "ucm-example",
        "prompt": "Explain how a shared external cache reuses previous computation. " * 128,
        "max_tokens": 32,
        "temperature": 0,
    }))
    PYREQUEST
    curl --fail http://127.0.0.1:7800/v1/completions \
      -H 'Content-Type: application/json' --data-binary @/tmp/ucm-request.json
    ```

    等待 HiCache write-through 任务完成，结合引擎存储写入日志，检查配置目录中的持久化 KV 块文件。正常停止服务并保留目录，再使用相同的模型、tokenizer、page size 和并行度重启。重放相同请求，确认 HiCache 报告从存储预取的 token，或报告已完成的 UCM 存储读取。通过重启，可以区分外部存储复用和进程内 HiCache 命中。该 UCM 适配器未实现 `clear()`，因此本项验证应通过重启清除易失状态。

    如果没有存储读取记录，检查 HiCache 预取与写入错误、目录权限和提示词长度。不能只用请求延迟缩短判断缓存有效。参见[故障排查](../../reference/troubleshooting.md)和 [Pipeline Store](../../developer-guide/cache-configuration/pipeline.md)。UCM 的 vLLM `/metrics` 示例不表示 SGLang 提供同名指标。

    验证完成后停止服务，并按存储策略保留或删除本次专用测试缓存目录。

    ### 离线批量推理 {#sglang-offline-inference}

    也可以在同一环境中使用 SGLang 的 [v0.5.9 离线示例](https://github.com/sgl-project/sglang/blob/v0.5.9/examples/runtime/engine/offline_batch_inference.py)。它通过 `ServerArgs` 接收与在线服务相同的 HiCache 参数。先停止占用这些设备的在线服务，在上面定义过 `MODEL_ID` 和 `HICACHE_CONFIG` 的 Shell 中执行：

    ```bash
    curl --fail --location \
      https://raw.githubusercontent.com/sgl-project/sglang/v0.5.9/examples/runtime/engine/offline_batch_inference.py \
      --output /tmp/sglang-offline-batch.py
    python3 /tmp/sglang-offline-batch.py \
      --model-path "$MODEL_ID" \
      --tensor-parallel-size 1 --page-size 128 \
      --enable-hierarchical-cache \
      --hicache-mem-layout page_first \
      --hicache-write-policy write_through \
      --hicache-storage-backend dynamic \
      --hicache-storage-prefetch-policy wait_complete \
      --hicache-storage-backend-extra-config "$HICACHE_CONFIG"
    ```

    示例打印每个提示词的生成结果。自带短提示词用于检查离线调用；验证外部缓存时，将其替换为覆盖多个完整块的重复长前缀，并按本页验证步骤检查读写。

=== "MindIE (NPU)"

    ## MindIE-LLM 快速开始 {#mindie}

    本指南说明如何安装支持 MindIE-LLM 的 UCM、应用所需 Python 模块补丁、配置 `mindie_llm`，并在昇腾平台启动使用 UCM KV cache 后端的 `mindie_llm_server`。

    ### 前提条件 {#mindie-_1}

    - MindIE-LLM 2.3.0
    - Python >= 3.10
    - 已安装 Ascend runtime/toolkit，且当前环境能够使用。详情参见 [MindIE-LLM 官方文档](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/quick_start/quick_start.md)。

    ### 第 1 步：安装 UCM {#mindie-1-ucm}

    在 MindIE-LLM 环境中从 [PyPI](https://pypi.org/project/uc-manager/) 安装 UCM：

    ```bash
    export PLATFORM=ascend
    export UCM_ENABLE_MINDIE=1
    export UCM_CXX11_ABI=1
    pip install uc-manager
    ```

    `UCM_CXX11_ABI` 必须与 MindIE/PyTorch 一致；上面的 `1` 按实际环境调整。PyPI 当前提供 0.5.0 源码包，安装时需要对应的编译环境。需要从仓库构建时，参见[源码构建](../../developer-guide/build_from_source.md#mindie-llm-ascend-platform)。

    ### 第 2 步：准备 UCM 配置 {#mindie-2-ucm}

    UCM 通过打补丁后的 MindIE-LLM 模块提供 Prefix Cache 集成。可以使用包内的 `ucm/integration/mindie/ucm_config.json`，也可以提供自己的配置文件。

    最小示例：

    ```json
    {
      "storage_backends": ["/path/to/kvcache"],
      "mindie_config_path": "/usr/local/lib/python3.11/site-packages/mindie_llm/conf/config.json",
      "block_elem_size": 2
    }
    ```

    将其保存为 `ucm_config.json`，并替换为实际环境中的路径。

    注意以下行为：

    - 安装 UCM 后，默认在首次导入 `mindie_llm` 时应用补丁。
    - hook 将修改后的 `uc_utils.py`、`unifiedcache_mempool.py` 和 `prefix_cache_plugin.py` 复制到已安装的 `mindie_llm` 包中。
    - MindIE 专用镜像可能在镜像构建时应用补丁。请核实其启动行为，不能假定任意 UCM 镜像都包含此集成。

    ### 第 3 步：启动带 UCM 的 MindIE-LLM {#mindie-3-ucm-mindie-llm}

    定位已安装的 `mindie_llm` 包：

    ```bash
    python -c "import mindie_llm, os; print(os.path.dirname(mindie_llm.__file__))"
    ```

    然后找到该目录下的 `conf/config.json`。

    确保服务用户可以读取配置文件，例如：

    ```bash
    chmod 640 <path-to-mindie_llm>/conf/config.json
    ```

    在 `config.json` 的 `BackendConfig` 下新增或更新 `kvPoolConfig`：

    ```json
    "BackendConfig": {
      "kvPoolConfig": {
        "backend": "unifiedcache",
        "configPath": "/path/to/your/ucm_config.json",
        "asyncWrite": true
      }
    }
    ```

    同时设置服务 IP、端口、模型路径及 MindIE-LLM 要求的其他配置。

    运行 `mindie_llm_server` 启动服务。以下日志表示服务启动成功：

    ```bash
    Daemon start success!
    ```

    启动后检查 MindIE-LLM 日志，确认成功加载 `unifiedcache` 后端。

    ### 验证服务与外部缓存 {#mindie-verify-the-service-and-external-cache}

    确认日志中包含 `[UC]: Initialize unifiedcache success.` 以及预期的后端路径。根据服务配置使用正确的 TLS 和鉴权设置，访问 MindIE 服务地址的 `/v1/models`，再通过其支持的推理端点发送补全请求。

    验证缓存时，使用长度超过数个 KV 块的相同提示词。等待异步写入完成，确认存储目录内存在持久化 KV 文件。正常停止服务、保留存储，以相同的模型和缓存块布局重启，再发送相同提示词。在诊断运行前设置 `MINDIE_UC_TIME_STAT=1`，以显示适配器各项操作的耗时；确认首次运行有成功的 `put` 活动，第二次运行有 `get` 活动，且不存在 dump/load 异常。这样可将外部缓存复用与引擎内存中的前缀缓存区分开。

    健康响应成功或第二次请求变快都不足以单独证明外部命中。应结合 MindIE 服务日志、缓存统计和 UCM 操作日志判断；vLLM Prometheus 指标示例不适用于 MindIE 端点。参见[故障排查](../../reference/troubleshooting.md)。完成后停止测试服务，并仅处理本次专用测试缓存目录。

    ### 故障排查 {#mindie-_2}

    #### MindIE-LLM 代码未应用补丁 {#mindie-mindie-llm_1}

    - 确认安装前已设置 `UCM_ENABLE_MINDIE=1`。
    - 确认安装前正确设置了 `UCM_CXX11_ABI=0` 或 `1`。
    - 重新安装 UCM。
    - 确认已安装 `mindie_llm`。

    #### 找不到 `configPath` {#mindie-configpath}

    - 使用绝对路径。
    - 确保服务用户有文件读取权限。

    #### 服务已启动，但 UCM 后端未启用 {#mindie-ucm}

    - 核对 `BackendConfig.kvPoolConfig`。
    - 检查 MindIE-LLM 启动日志。

## 后续步骤

- [Helm 部署](../frameworks/kubernetes/deploy.md)
- [从源码构建](../../developer-guide/build_from_source.md)
