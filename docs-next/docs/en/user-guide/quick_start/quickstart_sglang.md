# SGLang on CUDA

UCM provides a dynamic HiCache storage backend using SGLang's zero-copy V1
interface. SGLang owns device-to-host transfer; this adapter selects the UCM
`Posix` pipeline for host-to-storage I/O.

## Install

Prepare the SGLang environment supported by the UCM revision you are using; the
existing integration recipe targets SGLang 0.5.9. Check
[Installation](../installation.md) for an explicitly published SGLang combination.
If it is absent, [build UCM from source](../../developer-guide/build_from_source.md#sglang-cuda-platform)
in that environment. A vLLM image or a bare frontend package is not a SGLang
runtime. Do not substitute an unversioned `latest` image.

## Configure and launch

Create a writable, persistent `/mnt/ucm-cache` directory. The adapter requires
`page_first` host layout and `interface_v1`; do not change them to the older
copy-based storage API.

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

Adjust the model and parallelism to the available GPUs. The adapter supplies
block geometry and selects `Posix`; a vLLM YAML file with `ucm_connectors` is not
the same configuration interface.

## Verify the service and external cache

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

Wait for HiCache write-through tasks to finish and inspect persistent KV block
files in the configured directory, alongside the engine's storage write logs.
Stop the server normally, retain the directory, and restart with identical
model, tokenizer, page size, and parallelism. Replay the same request and
confirm HiCache reports storage-prefetched tokens or completed UCM storage
reads. This separates external storage reuse from an in-process HiCache hit.
The `clear()` method is not implemented by this UCM adapter; restarting is the
appropriate way to clear volatile state for this check.

If no storage read is reported, inspect HiCache prefetch/write errors, directory
permissions, and prompt length. Do not use shorter latency as the only cache
verification. See [Troubleshooting](../../reference/troubleshooting.md) and
[Pipeline Store](../capabilities/prefix-cache/pipeline.md). UCM's vLLM `/metrics`
examples are not a promise that SGLang exposes the same metric names.

Stop the service after the check. Keep or remove only its dedicated test-cache
directory according to your storage policy.
