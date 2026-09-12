# Shared-store PD Disaggregation

In a shared-store setup, prefill and decode run UCM against the same external
cache. The request first visits prefill to compute and save prompt blocks, then
visits decode with the original prompt. Decode discovers reusable blocks by
lookup; it does not receive a peer address from UCM.

The repository's `ucm/pd/toy_proxy_server.py` demonstrates this sequencing.
The bundled Kubernetes PD profiles use a different path, described in
[Transport with UCM](distributed.md).

## Before starting

Use the [vLLM quickstart](../../quick_start/index.md#vllm) to validate each
engine independently. Select the engine image or package from
[Installation](../../quick_start/index.md). This guide provides shared-store configuration, both engine commands, proxy startup and request verification.

Both instances must have:

- The same weights, tokenizer, model-directory name, dtype, block size, and
  compatible KV layout. Start with identical tensor-parallel settings.
- Access to the same stored objects. Equal path strings on two local disks do
  not provide shared storage.
- Sufficient store permissions, capacity, and read-after-write visibility for
  decode to observe completed prefill writes.
- Distinct engine HTTP ports and explicit device allocation. A running process
  on the wrong device is not a second independent instance.

## 1p1d

Begin with one prefill and one decode instance, keeping their UCM configuration
identical. This example uses Pipeline Store's `Cache|Posix` path:

```yaml
ucm_connectors:
  - ucm_connector_name: UcmPipelineStore
    ucm_connector_config:
      store_pipeline: Cache|Posix
      storage_backends: /mnt/ucm-shared
      cache_buffer_capacity_gb: 32
enable_event_sync: true
use_layerwise: false
```

`/mnt/ucm-shared` is a site-supplied shared mount, not a directory this example
creates. The buffer capacity is per process and must fit the host-memory
budget. Configure direct I/O and other backend options for the actual mount;
see [Pipeline Store](../../../developer-guide/cache-configuration/pipeline.md).

Pass this file through `UCM_CONFIG_FILE` in each engine's
`kv_connector_extra_config`, using `UCMConnector` with `kv_role: kv_both`, as
shown in the quickstart. Confirm both engines' `/health` and `/v1/models`
endpoints before introducing the proxy.

### Start Prefill and Decode

Save the YAML above as `/etc/ucm/pd.yaml` in both engine environments. This single-node CUDA example needs two GPUs, each able to run the model, and shared access to `/mnt/ucm-shared`. Start services in separate terminals with the same model, dtype, TP and block size.

**Terminal 1: Prefill on GPU 0, port 8100.**

```bash
export MODEL_ID=/models/your-model
export CUDA_VISIBLE_DEVICES=0
export ENABLE_UCM_PATCH=1
vllm serve "$MODEL_ID" \
  --served-model-name ucm-pd \
  --host 127.0.0.1 --port 8100 \
  --tensor-parallel-size 1 --dtype bfloat16 \
  --max-model-len 4096 --block-size 128 --enforce-eager \
  --kv-transfer-config '{
    "kv_connector": "UCMConnector",
    "kv_connector_module_path": "ucm.integration.vllm.ucm_connector",
    "kv_role": "kv_both",
    "kv_connector_extra_config": {"UCM_CONFIG_FILE": "/etc/ucm/pd.yaml"}
  }'
```

**Terminal 2: Decode on GPU 1, port 8200.**

```bash
export MODEL_ID=/models/your-model
export CUDA_VISIBLE_DEVICES=1
export ENABLE_UCM_PATCH=1
vllm serve "$MODEL_ID" \
  --served-model-name ucm-pd \
  --host 127.0.0.1 --port 8200 \
  --tensor-parallel-size 1 --dtype bfloat16 \
  --max-model-len 4096 --block-size 128 --enforce-eager \
  --kv-transfer-config '{
    "kv_connector": "UCMConnector",
    "kv_connector_module_path": "ucm.integration.vllm.ucm_connector",
    "kv_role": "kv_both",
    "kv_connector_extra_config": {"UCM_CONFIG_FILE": "/etc/ucm/pd.yaml"}
  }'
```

Docker deployments need distinct container names, correct device access and the same shared cache mount. In an Ascend engine environment, use `ASCEND_RT_VISIBLE_DEVICES` to select exposed NPUs. Cross-platform reuse still requires the compatibility checks below.

### Start the proxy

In a third terminal, check both engines:

```bash
curl --fail http://127.0.0.1:8100/health
curl --fail http://127.0.0.1:8200/health
```

Then start the proxy from the UCM checkout root:

```bash
python ucm/pd/toy_proxy_server.py \
  --pd-disaggregation \
  --host 127.0.0.1 --port 8000 \
  --prefiller-hosts 127.0.0.1 --prefiller-ports 8100 \
  --decoder-hosts 127.0.0.1 --decoder-ports 8200
```

The proxy accepts completions and chat-completions requests. It makes a
non-streaming prefill request with `max_tokens: 1`, waits for its HTTP response,
and forwards the original request to decode. It gives both stages the same
request ID. When engine authentication is enabled, configure the proxy's
`OPENAI_API_KEY` for that engine service.

### Send a request

Keep the proxy running and submit a prompt spanning multiple complete blocks from another terminal:

```bash
python3 - <<'PYREQUEST'
import json
from pathlib import Path
Path('/tmp/ucm-pd-request.json').write_text(json.dumps({
    "model": "ucm-pd",
    "prompt": "Explain how shared storage can reuse a computed prefix. " * 128,
    "max_tokens": 32,
    "temperature": 0,
}))
PYREQUEST
curl --fail http://127.0.0.1:8000/v1/completions \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/ucm-pd-request.json
```

### Verify the handoff

1. Use a dedicated cache directory and a prompt long enough to span several
   configured blocks and meet any persistence threshold.
2. Send one deterministic request through the proxy and inspect both engine
   logs. Confirm that prefill computes and submits cache writes.
3. Check decode's UCM hit tokens and successful loads. Compare its output with
   an independently validated full-compute request using the same settings.
4. Repeat with changed prompt content to confirm that unrelated prefixes do
   not appear as full external hits.
5. Restart both engines while retaining the cache, then repeat the original
   request to separate external reuse from engine-memory hits.

A prefill HTTP response is the proxy's sequencing point; the proxy does not
query a storage durability receipt. UCM dump work has its own completion
lifecycle. If decode starts before blocks are visible, the request may still
succeed by computing a missing prefix. Measure write completion and decode
hits before claiming that this path transfers all prompt KV through storage.
`enable_event_sync` coordinates device events; it is not a cross-server
commit protocol.

## 1p1d with Different Platforms

Establish a same-platform baseline before changing hardware or parallelism.
Matching `dtype` alone does not prove cache compatibility. The ordinary vLLM
connector includes tensor-parallel size and rank in its block namespace, and
the stored representation depends on the active cache layout.

For a heterogeneous experiment, record both engine/device-runtime versions,
weight and tokenizer revisions, block layout, and model-path basename. Verify
decode-side external hits and answer correctness on that exact pair. If these
checks do not pass, use independent caches until a compatible path has been
established; do not report the response alone as a heterogeneous KV handoff.

## XpYd

The example proxy accepts multiple hosts and ports for each role. Host and
port lists must have equal lengths. It selects prefill and decode independently
in round-robin order, so every selected decode must be able to read the blocks
written by every selected prefill.

For example, after preparing Prefill engines on 8100/8110 and Decode engines on 8200/8210 using the commands above, replace the single-pair proxy with:

```bash
python3 ucm/pd/toy_proxy_server.py \
  --pd-disaggregation --host 127.0.0.1 --port 8000 \
  --prefiller-hosts 127.0.0.1 127.0.0.1 --prefiller-ports 8100 8110 \
  --decoder-hosts 127.0.0.1 127.0.0.1 --decoder-ports 8200 8210
```

Increasing replicas raises shared-store load and may lower local memory-cache
reuse. Measure store latency and capacity alongside endpoint latency. The
example proxy does not provide health-aware scheduling, automatic retry, or
in-flight request recovery. For a Kubernetes deployment with a transport-aware
router, use [the distributed path](distributed.md) and validate its different
handoff contract.

## Diagnose missing reuse

- **No writes:** check persistence thresholds, the selected connector, and
  store errors on prefill.
- **Writes but no decode hits:** check shared mount identity, write visibility,
  model/cache compatibility, and decode's effective UCM configuration.
- **Hits but failed loads:** inspect UCM load errors and backend health before
  comparing latency.
- **Fast response without external hits:** check engine-memory hits and the
  amount of recomputation; latency alone cannot identify the KV path.

Use [Metrics](../../observability/metrics.md) to collect each phase separately.
