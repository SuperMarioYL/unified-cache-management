# vLLM-Ascend on NPU

Run a model with UCM's `Cache|Posix` pipeline, then verify that a second process
can read KV blocks persisted by the first process.

## Prepare the environment

Start with a working Ascend inference environment and a model that fits your
device memory. Check the [support matrix](../support-matrix/index.md) for the
integration scope; an engine supporting a model does not by itself verify every
UCM feature.

Open [Installation](../installation.md) and copy the exact Wheel or Image command
for the published combination you need. Choose the Ascend backend, architecture, and matching vLLM-Ascend runtime. The host driver, CANN environment, and device mounts must match that runtime. Wheel installation must include
the selected backend extra; do not replace the generated command with a bare
`pip install uc-manager`. The selector also preserves Fork package names and
package-index settings. For a combination absent from the release, use
[Build from source](../../developer-guide/build_from_source.md).

## Configure the cache

Create a writable `/mnt/ucm-cache` directory, mounted persistently if running in
a container. Save the following as `/etc/ucm/ucm.yaml`, readable by the engine:

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

The 4 GiB host cache is an explicit small-example setting, not the runtime
default. Allow for one buffer per unshared worker, or shared-buffer allocation
when the model uses it. `io_direct: false` keeps this initial filesystem check
simple; select direct I/O and capacity settings for your actual storage using
[Pipeline Store](../capabilities/prefix-cache/pipeline.md).

## Start the server

Set `MODEL_ID` to your local model path or model repository ID. Adjust tensor
parallelism and maximum model length to its hardware requirements.

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

The runtime patch hook and connector are both required. Confirm the startup log
contains `create UcmPipelineStore with config:` and the expected storage path.
Sparse Attention requires its separately supported build and runtime settings;
this example enables Prefix Cache only.

## Verify the service and external cache { #verify-the-service-and-external-cache }

A ready HTTP server confirms service startup; it does not prove a UCM cache hit.

```bash
curl --fail http://127.0.0.1:7800/health
curl --fail http://127.0.0.1:7800/v1/models
```

Generate a repeatable prompt longer than several 128-token blocks, then send it:

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

Check the storage directory for persisted KV block files after writes finish.
Health probes create short-lived files too, so directory activity alone is not
proof of KV persistence. Look for dump task observations in
`ucm:posix_dump_task_duration_ms_count` and inspect the engine logs for errors.
Task-duration observations show activity; require completed writes without
errors and successful replay before treating them as persistence evidence.

Stop the server normally, preserve `/mnt/ucm-cache`, then restart with the same
model, tokenizer, tensor parallelism, block size, and UCM configuration. Send
the same request again. Restarting removes engine HBM and host-memory hits from
this check. Confirm `ucm:ucm_hit_tokens_total` increases and Posix load activity
appears in `ucm:posix_load_task_duration_ms_count` or
`ucm:cache_posix_load_success_shards_total`. Counters restart with the process;
compare changes within each run, not absolute values across restarts.

A shorter second request latency alone does not establish an external cache hit.
See [Metrics](../observability/metrics.md) for request-driven metric synchronization
and [Troubleshooting](../../reference/troubleshooting.md) for missing hits,
permissions, and shared-memory failures. Stop the service when finished; retain
or remove only the dedicated test-cache directory according to your storage policy.


## Use another model

Use the [Model Tour](../model-tour/index.md) for official engine recipes, then
add the UCM connector configuration above without discarding model-specific
engine settings.
