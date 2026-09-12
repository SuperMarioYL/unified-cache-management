# Mooncake Store

Use the Mooncake pipeline to put reusable KV blocks in a shared memory service.
UCM connects to an existing Mooncake master and transfers data through its
client. Add Posix when KV blocks also need a filesystem backing tier.

| Pipeline | Read and write behavior |
| --- | --- |
| `Mooncake` | Reads and writes Mooncake; there is no UCM filesystem backing tier. |
| `Mooncake|Posix` | Reads Mooncake first and sends misses to Posix; dumps also write to Posix. |

This differs from [Cache|Posix](pipeline.md), whose first stage is a local host
buffer. A live Mooncake service can retain memory objects when an inference
process restarts, but that is not a guarantee of durable storage across service
failure. The persistence boundary of `Mooncake|Posix` is its configured
filesystem and completed backing writes.

## Prerequisites

The native UCM Mooncake target in this source tree requires Ascend ACL headers,
`libascendcl`, and `libmooncake_store`. Its CMake file skips the target when any
of these are missing. Treat this as an Ascend integration; a different
Mooncake transport name does not make this UCM target a CUDA backend.

Build UCM in the matching [Ascend environment](../build_from_source.md#vllm-ascend-ascend-platform)
after preparing Mooncake. CMake searches the toolkit's `include` and `lib64`
directories under `/usr/local/Ascend/ascend-toolkit/latest`, and the Mooncake
library under `/usr/local/lib`. `MOONCAKE_STORE_INCLUDE_DIR` selects the
Mooncake header tree; its current fallback is
`/vllm-workspace/Mooncake/mooncake-store/include`. Use the header revision that
matches the linked client library.

Before serving, confirm the installed `ucm/store/mooncakestore/libmooncakestore.so`
and its dependencies. Start a Mooncake master using your deployment's service
configuration, verify connectivity from every serving process, and prepare a
writable Posix directory if selecting the backing tier. UCM does not launch
the master for you.

## Start Mooncake master

On the service host with matching Mooncake binaries installed, run:

```bash
mooncake_master --port 50088
```

Keep it running and set `master_server_address` below to the reachable host IP and port `50088`. Reuse an existing master if available. Check the installed `mooncake_master --help` for capacity, lease and eviction settings; client and server versions must be compatible.

## Configuration for Prefix Caching

The following is an initial configuration for a host with an Ascend device and
a reachable Mooncake master. Replace both addresses and the storage directory:

```yaml
ucm_connectors:
  - ucm_connector_name: UcmPipelineStore
    ucm_connector_config:
      store_pipeline: "Mooncake|Posix"
      local_hostname: 192.0.2.10
      master_server_address: "192.0.2.20:50088"
      metadata_server: P2PHANDSHAKE
      protocol: ascend
      global_segment_size_gb: 4
      local_buffer_size_gb: 1
      share_buffer_capacity_gb: 4
      cache_buffer_capacity_gb: 4
      replica_num: 1
      storage_backends: /mnt/ucm-mooncake
      io_direct: false
      posix_io_engine: psync
      timeout_ms: 30000
use_layerwise: false
enable_event_sync: true
enable_metrics: true
```

`local_hostname` is required and must identify the serving host appropriately
for the transport. Loopback addresses only apply when all relevant services
share that host/network namespace. Keep `enable_event_sync: true`: the native
dump path waits on the prerequisite event before accessing newly computed KV.
Use the [vLLM-Ascend quickstart](../../user-guide/quick_start/index.md#vllm-ascend) to
supply this file through `UCM_CONFIG_FILE` and start the model server.

For Mooncake without filesystem backing, set `store_pipeline: Mooncake` and
remove `storage_backends` and `posix_io_engine`. Do not set
`ucm_connector_name: UcmMooncakeStore` for this V1 path: the current factory
registers it through `UcmPipelineStore`.

## Budget memory and concurrency

The sample explicitly reduces the native 30 GiB global segment and 64 GiB
shared-buffer capacity for an initial test. Size them for the actual number of
workers and KV tensor sizes; these are distinct allocations. The native local
buffer default is 1 GiB. Positive `_gb` settings take precedence over the
corresponding byte-valued global/local settings.

`stream_number` defaults to 4 and must be between 1 and 32. The private host
buffer pool is derived from stream count and tensor size, unless
`host_buf_pool_size` is explicitly set. `replica_num` defaults to 1 and must be
positive. Replica count is not a persistence policy.

The vLLM adapter and Mooncake stage have different shared-buffer settings:
`share_buffer_capacity_gb` belongs to Mooncake. For models where the adapter
enables shared buffers, its current preflight still reads
`cache_buffer_capacity_gb`; the sample sets this explicitly to avoid the
adapter's implicit 128 GiB check. That value does not size Mooncake's buffer.
Inspect effective startup configuration and host memory before scaling beyond
the initial request.

## Verify each tier separately

1. Record the model revision, dtype, parallel layout, and Mooncake namespace.
   Run a fresh-prefix request and wait for dumps to complete.
2. Replay through a restarted inference process while leaving the Mooncake
   service running. Require external hits and Mooncake read/hit metrics.
3. For `Mooncake|Posix`, confirm backing files and completed Posix dumps. To
   exercise backing reads, evict only the test objects from Mooncake in an
   isolated test environment, retain the files, and replay again.
4. Require Posix reads and Mooncake backend-load metrics for that last case.
   A Mooncake hit alone does not verify the filesystem tier.

See [Metrics](../../user-guide/observability/metrics.md) for load-hit, load-miss, backend,
and byte counters, and [Health metrics](../../user-guide/observability/health-metrics.md)
for the active probes. Mooncake health checks perform a small write/read/remove
operation; they do not replace the two-tier request test.

## Diagnose the failing boundary

| Symptom | Next check |
| --- | --- |
| Native library missing or unresolved symbols | The optional CMake target, Ascend libraries, and matching Mooncake headers/library. |
| Setup or lookup cannot reach the service | Master address, local hostname, transport configuration, and the service logs. |
| Posix hits are expected but absent | Backing writes completed, matching storage paths, and Posix health. |
| Queue rejections or growing load latency | Queue and stage metrics, host buffer demand, service capacity, and network traffic. |

Keep performance comparisons specific to the tier hit. Report memory-service
hits and filesystem hits separately; no speedup is assumed by enabling
this pipeline.

[Implementation and extension](../extending-store.md#backend-entrypoints).
