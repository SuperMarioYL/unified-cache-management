# SGLang（CUDA）

UCM 通过 SGLang 的零拷贝 V1 接口提供动态 HiCache 存储后端。SGLang 负责设备与主机之间的数据传输；UCM 适配器选择 `Posix` 管线，负责主机与存储之间的 I/O。

## 安装

准备当前 UCM 修订版本支持的 SGLang 环境；现有集成示例使用 SGLang 0.5.9。在[安装](../installation.md)中检查是否有明确标记的 SGLang 发布组合。若没有，请在对应环境中[从源码构建 UCM](../../developer-guide/build_from_source.md#sglang-cuda-platform)。vLLM 镜像或单独的前端包不能代替 SGLang 运行环境，也不要使用未固定版本的 `latest` 镜像。

## 配置与启动

创建可写的持久目录 `/mnt/ucm-cache`。适配器要求 `page_first` 主机内存布局和 `interface_v1`，请勿改成旧版需要拷贝的存储 API。

```bash
export MODEL_ID=/models/your-model
HICACHE_CONFIG='{
  "backend_name": "unifiedcache",
  "module_path": "ucm.integration.sglang.unifiedcache_store",
  "class_name": "UnifiedCacheStore",
  "interface_v1": 1,
  "kv_connector_extra_config": {
    "ucm_connector_name": "UcmPipelineStore",
    "ucm_connector_config": {
      "storage_backends": "/mnt/ucm-cache",
      "io_direct": false,
      "timeout_ms": 30000
    }
  }
}'
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

## 验证服务与外部缓存 { #verify-the-service-and-external-cache }

```bash
curl --fail http://127.0.0.1:7800/health
curl --fail http://127.0.0.1:7800/v1/models
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
```

等待 HiCache write-through 任务完成，结合引擎存储写入日志，检查配置目录中的持久化 KV 块文件。正常停止服务并保留目录，再使用相同的模型、tokenizer、page size 和并行度重启。重放相同请求，确认 HiCache 报告从存储预取的 token，或报告已完成的 UCM 存储读取。通过重启，可以区分外部存储复用和进程内 HiCache 命中。该 UCM 适配器未实现 `clear()`，因此本项验证应通过重启清除易失状态。

如果没有存储读取记录，检查 HiCache 预取与写入错误、目录权限和提示词长度。不能只用请求延迟缩短判断缓存有效。参见[故障排查](../../reference/troubleshooting.md)和 [Pipeline Store](../capabilities/prefix-cache/pipeline.md)。UCM 的 vLLM `/metrics` 示例不表示 SGLang 提供同名指标。

验证完成后停止服务，并按存储策略保留或删除本次专用测试缓存目录。
