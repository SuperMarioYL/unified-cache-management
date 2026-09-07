# vLLM（CUDA）

使用 UCM 的 `Cache|Posix` 管线运行模型，再验证新进程能否读取前一个进程持久化的 KV 块。

## 准备环境

先准备可运行的CUDA推理环境，选择能够放入设备内存的模型。[支持矩阵](../support-matrix/index.md)说明集成范围；引擎支持某个模型，并不代表该模型的所有 UCM 能力都已验证。

打开[安装](../installation.md)，复制目标发布组合对应的 Wheel 或 Image 命令。选择 CUDA 后端与匹配的 vLLM 运行环境。安装 Wheel 时必须包含所选后端的 extra，不能将生成命令简化为裸 `pip install uc-manager`。选择器也会保留 Fork 包名和包索引设置。发布中没有所需组合时，请[从源码构建](../../developer-guide/build_from_source.md)。

## 配置缓存

创建可写的 `/mnt/ucm-cache` 目录；使用容器时将其挂载到持久存储。把以下配置保存为引擎可读取的 `/etc/ucm/ucm.yaml`：

```yaml
ucm_connectors:
  - ucm_connector_name: UcmPipelineStore
    ucm_connector_config:
      store_pipeline: "Cache|Posix"
      storage_backends: /mnt/ucm-cache
      cache_buffer_capacity_gb: 4
      timeout_ms: 30000
      io_direct: false
enable_metrics: true
```

这里显式分配 4 GiB 主机缓存，适用于小规模示例，并非运行时默认值。未共享 buffer 的 worker 各自分配内存；启用共享 buffer 时需按共享分配方式预留容量。`io_direct: false` 便于先验证文件系统路径；实际部署的 Direct I/O 和容量配置参见 [Pipeline Store](../capabilities/prefix-cache/pipeline.md)。

## 启动服务

将 `MODEL_ID` 设为本地模型路径或模型仓库 ID，并根据模型的硬件需求调整张量并行度和最大上下文长度。

```bash
export MODEL_ID=/models/your-model
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

运行时 patch hook 和 connector 都需要启用。检查启动日志是否包含 `create UcmPipelineStore with config:` 以及预期的存储路径。Sparse Attention 需要对应的构建与运行时配置，本示例仅启用 Prefix Cache。

## 验证服务与外部缓存 { #verify-the-service-and-external-cache }

HTTP 服务就绪说明服务已启动；还需要单独确认 UCM 缓存命中。

```bash
curl --fail http://127.0.0.1:7800/health
curl --fail http://127.0.0.1:7800/v1/models
```

生成可重复使用的提示词，使其长度超过数个 128-token 块，然后发送请求：

```bash
python - <<'PYREQUEST'
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

等待写入完成后，检查存储目录中的持久化 KV 块文件。健康探测也会创建临时文件，因此仅观察目录活动不足以证明 KV 持久化成功。检查 `ucm:posix_dump_task_duration_ms_count` 中的 dump 任务活动。任务耗时计数也可能记录失败任务，不能单独作为写入成功的证据；还需确认日志中写入完成且没有错误，并通过后续重启重放验证持久化。

正常停止服务，保留 `/mnt/ucm-cache`，使用相同的模型、tokenizer、张量并行度、块大小和 UCM 配置重启，再发送同一请求。重启可排除引擎 HBM 和主机内存命中的影响。确认 `ucm:ucm_hit_tokens_total` 增长，并在 `ucm:posix_load_task_duration_ms_count` 或 `ucm:cache_posix_load_success_shards_total` 中观察到 Posix 读取活动。计数器随进程重启而归零，应比较同一次运行中的增量，不能直接跨重启比较绝对值。

第二次请求更快不能单独证明外部缓存命中。[指标](../observability/metrics.md)说明由请求驱动的指标同步方式；命中缺失、目录权限和共享内存问题参见[故障排查](../../reference/troubleshooting.md)。验证完成后停止服务，并按存储策略保留或删除本次专用测试缓存目录。

## 使用其他模型

在[模型示例](../model-tour/index.md)中找到引擎官方配置，保留模型特定的引擎参数，再加入上面的 UCM connector 配置。
