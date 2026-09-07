# UCM Configuration Parameters

---

## Top-Level Parameters

> Global parameters written directly at YAML root level.

| Parameter | Required | Type | Value Range | Description |
|---|---|---|---|---|
| `use_layerwise` | Optional | bool | Default: `true` | Enable layer-wise (per-layer) load/save mode. Recommended `true`; DeepSeek V4 series recommends `false`. |
| `enable_event_sync` | Optional | bool | Default: `true` | Performance optimization switch. Recommended to enable. |
| `persist_token_threshold` | Optional | int | `0` | When request length < `persist_token_threshold`, UCM does not process the request. |
| `wa_dump_block_wise` | Optional | bool | `true` | Only used in FAWA connector. `true`: every block's WA cache is dumped (high frequency); `false`: only dump last block's WA cache of each chunk prefill (low frequency). |
| `load_tokens_threshold` | Optional | int | `0` | Minimum token threshold for triggering KV cache loading. Only effective for DeepSeek V4 series. When external hit tokens > `load_tokens_threshold`, triggers KV Cache loading. |
| `enable_record_traces` | Optional | bool | `false` | Record request information (timestamps, input length, output length, etc.). |
| `enable_metrics` | Optional | bool | Default: `true` | Whether to enable metrics collection. |
| `use_lite` | Optional | bool | `false` | Enable UCM Lite. Does not save/load KV Cache data, only saves and queries metadata. Used to evaluate KV Cache hit rate — no acceleration effect. |
| `metrics_config_path` | Optional | string | User-configured | Custom metrics config file path. Enables UCM online monitoring via toolkit. Reference config: `examples/metrics/metrics_configs.yaml`. |

---

## ucm_connector_config (Storage Backend Configuration)

> Parameters under `ucm_connectors[0].ucm_connector_config`.

| Parameter | Required | Type | Value Range | Description |
|---|---|---|---|---|
| `store_pipeline` | **Required for Pipeline Store** | string | See valid values below | Registered pipeline name; set it explicitly in vLLM YAML. SGLang selects `Posix` in its adapter. |
| `timeout_ms` | Optional | int | Default: `30000` | Cache transfer and Posix I/O task timeout in milliseconds. Place it inside `ucm_connector_config`, not at YAML root. |
| `storage_backends` | **Required** | string | User-configured, multiple mount points separated by `:` | Local directory or mount point. Multiple mount points are separated by colons. |
| `io_direct` | Optional | bool | Default: `true` | Enable Direct I/O (bypass OS page cache). `false`: uses PageCache; `true`: skips PageCache. |
| `posix_io_engine` | Optional | string | Default: `psync` | File I/O mode. `psync`: synchronous; `aio`: asynchronous, requires `io_direct=true`. |
| `posix_data_trans_concurrency` | Optional | int | Default: `128` | Read/write threads per card in `psync` mode. NFS over RDMA: 128/card. Not used in `aio` mode. |
| `posix_open_concurrency` | Optional | int | Default: `32` | File open threads in `aio` mode. Not applicable in `psync`. |
| `posix_commit_concurrency` | Optional | int | Default: `4` | File rename threads in `aio` mode. Not applicable in `psync`. |
| `posix_lookup_concurrency` | Optional | int | Default: `16` | Threads for checking file existence at mount point. |
| `cache_buffer_capacity_gb` | Optional | int | See description | Native Cache Store defaults to 256 GiB with shared buffers, or 32 GiB per worker when `share_buffer_enable` is false. The vLLM connector supplies 128 GiB when shared buffers are enabled and capacity is omitted. Shared buffers default on for MLA. A positive explicit capacity overrides the native defaults; budget all unshared workers on the host. |
| `cache_sdma_direct` | Optional | bool | Depends on build env: `true` when `PLATFORM=ascend-a3`, `false` otherwise | Enable SDMA H2D/D2H transfer. Only effective on A3 devices. Recommended to disable. |
| `cache_load_backend_only` | Optional | bool | Default: `false` | Force load from SSD even on cache hit. Test only. |
| `cache_io_aggregation` | Optional | bool | Default: `false`, auto-enabled when `PLATFORM=ascend` and model is V4 | Enable IO aggregation H2D transfer. Only effective on A2 devices. |
| `share_buffer_enable` | Optional | bool | MLA: default enabled; GQA: default disabled | Enable shared memory. MLA without shm or GQA with shm causes performance degradation. |
| `posix_capacity_gb` | Optional | int | Default: `0` (no GC); must not exceed mounted filesystem available capacity | Max disk storage capacity (GB). Triggers GC when used >= `posix_capacity_gb * posix_gc_trigger_threshold_ratio`. In multi-instance deployments sharing the same filesystem, only one instance should enable GC; others should not. |
| `posix_gc_trigger_threshold_ratio` | Conditional | float | Default: `0.7`, 0~1. Not set when `posix_capacity_gb` is not configured | GC trigger threshold ratio. Used with `posix_capacity_gb`. |
| `posix_gc_recycle_percent` | Optional | float | Default: `0.1`, 0~1. Not set when `posix_capacity_gb` is not configured | Ratio of current capacity deleted per GC round. |
| `posix_gc_max_recycle_count_per_shard` | Optional | int | Default: `50000`, >0. Not recommended to modify. Not set when `posix_capacity_gb` is not configured | Max file deletion count per directory per GC round. |
| `posix_gc_shard_sample_ratio` | Optional | float | Default: `0.1`, 0~1. Not set when `posix_capacity_gb` is not configured | Sample 10% directories to estimate total capacity. |
| `posix_gc_check_interval_sec` | Optional | int | Default: `30`, >0. Not set when `posix_capacity_gb` is not configured | GC sampling and trigger interval. |
| `posix_gc_concurrency` | Optional | int | Default: `16`, >0. Not set when `posix_capacity_gb` is not configured | GC thread pool worker count. |
| `posix_gc_task_timeout_ms` | Optional | int | Default: `300000`, >0. Not set when `posix_capacity_gb` is not configured | Single directory task timeout watchdog. `0` = disabled. |
| `posix_gc_precise_mode` | Optional | bool | Default: `true`. Not set when `posix_capacity_gb` is not configured | `true`: precise mode (global coldest); `false`: performance mode (per-directory coldest). |

### store_pipeline Valid Values

| Value | Description |
|---|---|
| `Cache\|Posix` | Normal use case |
| `Cache\|Empty` | MLA pure Cache test |
| `Cache\|Fake` | MLA/GQA pure Cache test (precision risk) |
| `Empty` | All Store interfaces empty; engine never hits |
| `Fake` | No actual load/dump, stores metadata only; tests peak performance |
| `Mooncake` | Mooncake memory pool; vllm-ascend only |
| `Mooncake\|Posix` | Mooncake memory pool with disk persistence |
| `YuanRong` | YuanRong memory pool |
| `YuanRong\|Posix` | YuanRong memory pool with disk persistence |

!!! note "storage_backends Note"
    Set capacity only when this deployment owns garbage collection for the storage namespace. A mounted filesystem is not inherently incompatible with GC; coordinate ownership across instances and size the limit for the actual filesystem.

---

## store_health (ucm_connector_config.store_health)

> Parameters under `ucm_connector_config.store_health`.

| Parameter | Required | Type | Value Range | Description |
|---|---|---|---|---|
| `enabled` | Optional | bool | Default: `true` | Master switch for storage isolation. Adds circuit breaker for disk KV cache. |
| `health_check_interval_s` | Optional | number | Default: `10` | Disk health check interval (sec). Must be >0 and > `health_check_timeout_s`. |
| `health_check_timeout_s` | Optional | number | Default: `3` | Single probe timeout (sec). Must be >0 and < `health_check_interval_s`. |
| `health_window_size` | Optional | int | Default: `8` | Fault statistics window. Must be positive and >= `failure_threshold`. |
| `failure_threshold` | Optional | int | Default: `2` | Fault trigger threshold. Must be positive and <= `health_window_size`. |



These defaults follow `ucm/store/cache/cc/global_config.h`,
`ucm/store/posix/cc/global_config.h`, and
`ucm/store/pipeline/cc/store_health_config.h`. The vLLM connector applies its
shared-buffer override before constructing the Store. See
[Pipeline Store](../user-guide/capabilities/prefix-cache/pipeline.md) for a complete
example and [Health metrics](../user-guide/observability/health-metrics.md) for
probe behavior and circuit-breaker state.
