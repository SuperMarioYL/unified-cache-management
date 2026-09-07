# Pipeline Store

Use `Cache|Posix` when KV blocks should survive a serving-process restart on a
local disk or mounted filesystem, while recently accessed blocks remain in host
memory. `Cache` owns device-to-host transfers and the host buffer; `Posix` owns
files, lookup, and filesystem I/O. The engine still owns its device KV cache.

A pipeline is selected by a registered name, not assembled from arbitrary stage
strings. Its Python builder loads the corresponding native libraries at startup.
For the current vLLM integration, the public entry is `UcmPipelineStore`.

## Configure Cache and Posix

Start from [Installation](../../installation.md), or
[build from source](../../../developer-guide/build_from_source.md) in the target
engine environment. This pipeline requires the `ucmpipelinestore` extension,
`libcachestore.so`, and `libposixstore.so`. A successful package import alone does
not show that the selected pipeline can initialize.

Create a dedicated writable directory, mount it into every process that needs
access, and save this as a UCM YAML file:

```yaml
ucm_connectors:
  - ucm_connector_name: UcmPipelineStore
    ucm_connector_config:
      store_pipeline: "Cache|Posix"
      storage_backends: /mnt/ucm-cache
      cache_buffer_capacity_gb: 4
      io_direct: false
      posix_io_engine: psync
      timeout_ms: 30000
use_layerwise: true
enable_event_sync: true
enable_metrics: true
```

Connect the file through `kv_connector_extra_config.UCM_CONFIG_FILE` using the
[vLLM quickstart](../../quick_start/quickstart_vllm.md), or its
[Ascend counterpart](../../quick_start/quickstart_vllm_ascend.md). The connector
supplies device IDs, block sizes, and tensor layout; do not copy these values
from a different model's Store test.

The sample uses buffered synchronous I/O to establish a filesystem baseline.
`io_direct` defaults to `true` in the native Cache and Posix stages. Switch to
`posix_io_engine: aio` only with `io_direct: true` and a filesystem that supports
the resulting aligned I/O. Put `timeout_ms` inside `ucm_connector_config`; it is
a Store task timeout, not an HTTP request timeout.

## Budget host memory

The explicit 4 GiB sample is a starting budget, not a size valid for every model.
Cache Store requires enough space for at least
`max(1024, 2 * cache_load_exclusive_buffer_number)` shards; the default exclusive
buffer count is 1024. Initialization reports the minimum required GiB when the
chosen capacity is too small.

| Allocation path without an explicit capacity | Effective default |
| --- | --- |
| Native Cache with shared buffers | 256 GiB |
| Native Cache with `share_buffer_enable: false` | 32 GiB per worker |
| Current vLLM connector with shared buffers | 128 GiB |

The vLLM connector defaults `share_buffer_enable` to whether the model uses MLA.
A positive `cache_buffer_capacity_gb` overrides the native defaults. Shared
buffers need sufficient `/dev/shm`; unshared workers allocate separately, so
multiply their budget by the number of workers on the host. Model weights,
device KV cache, and other host allocations remain additional costs.

## Storage capacity and health

`posix_capacity_gb: 0` disables capacity-driven garbage collection. A positive
value supplies a capacity budget; it does not reserve filesystem space. The
vLLM connector assigns Posix GC to its DP0 scheduler. When multiple serving
instances share a directory, account for their combined data and retain the
Posix coordination settings unless you have validated a different ownership
arrangement.

Pipeline health checking is enabled by default, with a 10-second interval,
3-second timeout, 8-sample window, and failure threshold of 2. Posix implements
a write/read/compare/remove probe. A successful probe establishes filesystem
health; only request-level hits and completed loads establish KV reuse. See
[Health metrics](../../observability/health-metrics.md).

## Other registered pipelines

| Requirement | Selection |
| --- | --- |
| Existing vLLM NFS connector configuration | [NFS Store](nfs.md) |
| 3FS client I/O | [`Cache\|Ds3fs`](ds3fs.md) |
| Smaller stored BF16 payload, with a quality tradeoff | [`Cache\|Compress\|Posix`](compress.md) |
| Shared Mooncake memory, optionally backed by files | [`Mooncake` or `Mooncake\|Posix`](mooncake.md) |

SGLang already manages a host cache, so its adapter selects the `Posix` stage
directly. Use its [quickstart](../../quick_start/quickstart_sglang.md) rather than
passing the vLLM YAML unchanged.

## Verify writes and reads

1. Start with an empty test directory and record the model revision, KV dtype,
   parallel layout, and effective Store configuration.
2. Run a request with a reusable prefix long enough to produce complete blocks.
   Check that dumps complete and committed cache files appear.
3. Restart the serving process while retaining the directory, then replay the
   same input and cache geometry. Follow the
   [restart-and-replay procedure](../../quick_start/quickstart_vllm.md#verify-the-service-and-external-cache).
4. Require external-hit tokens and Posix read activity on replay. A faster
   second request in the same process may only have hit a memory cache.

If replay hits host memory, `cache_load_backend_only: true` can isolate the
backend lookup/load path for a diagnostic run. For errors, first check the
loaded stage names, directory permissions, buffer-size message, and Posix
health state; then use the [troubleshooting guide](../../../reference/troubleshooting.md).

## Measure the storage benefit { #historical-performance-report }

Compare uncached prefill, storage replay, and warm-memory replay separately.
Record external-hit tokens, Posix bytes read, host memory, TTFT, and throughput
with the same model and input set. These measurements answer whether storage
I/O is cheaper than recomputation for your workload. The
[benchmark guide](../../../benchmark/index.md) describes how to report that
comparison; this page does not provide performance figures for untested hardware.

## Implementation references

- `ucm/store/pipeline/connector.py` defines the registered compositions and loaded libraries.
- `ucm/store/cache/cc/cache_store.cc` owns buffer defaults and minimum-size checks.
- `ucm/store/posix/cc/posix_store.cc` owns filesystem configuration and health probes.
- `ucm/integration/vllm/ucm_connector.py` supplies layout, shared-buffer defaults, and GC ownership.
