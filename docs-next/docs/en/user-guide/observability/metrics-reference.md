# Read UCM metrics

Choose a metric by the operation it measures and its unit. A prefix lookup, a
shard transfer and an HTTP request are different events; adding their counts
cannot produce a meaningful hit rate or request total.

This page describes the default vLLM connector export. Use the complete
[metric catalog](https://github.com/ModelEngine-Group/unified-cache-management/blob/a336d69bc03a550d44bee3df9da7664e9edfe3a7/examples/metrics/metrics_configs.yaml)
for every configured name, description and histogram bucket. That YAML generates
`ucm/default_metrics_config.py`, which is the runtime's built-in configuration.

## Establish the export path

With `enable_metrics: true` and no custom metric configuration, the default
consumer is `vllm_connector`, using the prefix `ucm:`. Native measurements are
collected by `MetricsDispatcher`, then drained into vLLM through
`get_kv_connector_stats()`. The Prometheus bridge increments Counters, sets Gauges
and adds histogram observations or bucket deltas.

The connector adds `worker_rank` to the engine's labels. Worker ranks are strings;
the scheduler uses `scheduler`. Prometheus adds scrape labels such as `job` and
`instance`. Inspect the actual label set before combining workers or replicas.

The optional `multiproc` consumer uses a separate periodic logger and labels
including `worker_id`. It exports the same underlying measurements through another
path. Do not add its counts to connector counts as if they were additional work.

A custom `metrics_config` or `metrics_config_path` supplies a complete metric
configuration rather than an overlay on the defaults. Missing definitions or a
disabled consumer can explain missing series. The native `interval_lookup_hit_rates`
histogram is excluded from the default vLLM connector export; use the token or
block counters for an aggregate ratio.

See [Metrics setup](metrics.md) for endpoint and scrape configuration.

## Find the signal for your question

All names below use the default `ucm:` prefix.

| Question | Metrics | Interpretation |
| --- | --- | --- |
| What did prefix lookup report? | `total_prefix_query_tokens_total`, `gpu_hbm_hit_tokens_total`, `ucm_hit_tokens_total` | Tokens observed at the connector lookup boundary; HBM and UCM hits are recorded separately |
| Did the Cache stage descend to its backend? | `cache_lookup_hit_blocks_total`, `cache_lookup_miss_blocks_total` | Lookup decisions in blocks, not completed transfers |
| Where did Cache loads finish? | `cache_load_success_shards_total`, `cache_posix_load_success_shards_total`, `cache_load_failed_shards_total` | Shards delivered from ready Cache, delivered after waiting for a non-ready buffer, or not delivered |
| Is a Cache queue or transfer failing? | `cache_load_queue_full_total`, `cache_dump_queue_full_total`, `cache_h2d_errors_total`, `cache_d2h_errors_total` | Rejected submissions and device-transfer errors |
| How much task volume does Posix record? | `posix_s2h_bytes_total`, `posix_h2s_bytes_total` | Nominal load/dump task bytes recorded at completion, including failed or aborted tasks |
| Is Posix failing? | `posix_open_errors_total`, `posix_io_errors_total`, `posix_io_timeout_total`, `posix_aio_timeout_total` | Open/I/O failures and synchronous/AIO timeouts |
| Is the configured Posix capacity budget being consumed? | `posix_store_used_bytes`, `posix_store_capacity_bytes`, `posix_store_usage_ratio`, `posix_gc_running` | Logical Store estimates and GC state; not filesystem free space |
| Does Mooncake serve data or descend further? | `mooncake_load_hit_shards_total`, `mooncake_load_miss_shards_total`, `mooncake_load_bytes_total`, `mooncake_dump_bytes_total` | Stage load outcomes and byte movement |
| Is a stage accepting new work? | `posix_store_health`, `mooncake_store_health` | Circuit-breaker state, detailed in [Store health](health-metrics.md) |
| Where is elapsed time spent? | `connector_*_duration_ms`, `cache_*_duration_ms`, `posix_*_duration_ms`, `mooncake_*_duration_ms` | Histograms for the named interface or stage operation |

A **block** is the lookup unit. A **shard** is a transfer descriptor; its size and
relationship to layers depend on the cache layout. **Bytes** describe traffic,
not retained cache capacity. Preserve these distinctions when comparing pipelines
or changing layerwise operation.

`ucm_hit_tokens_total` is the matched-token result reported by lookup, bounded by
tokens not already computed by the engine. It does not certify a later Load.
Pair it with completed transfer counters and errors when verifying cache reuse.

The name `cache_posix_load_success_shards_total` retains `posix`, but its source
flag means the Cache buffer was not ready at dispatch. The fill uses the selected
backend, which can be another Store; this counter alone cannot identify Posix I/O.

## Queries for a single deployment

The examples assume a scrape job named `vllm`. Add your deployment's model and
engine selectors where one job contains several serving instances.

### External lookup share

```promql
sum by (instance, model_name, engine) (
  rate(ucm:ucm_hit_tokens_total{job="vllm"}[5m])
)
/
sum by (instance, model_name, engine) (
  rate(ucm:total_prefix_query_tokens_total{job="vllm"}[5m])
)
```

This is UCM-reported matched tokens divided by all query tokens in that window.
It is not the hit rate conditional on an HBM miss. A window with no query tokens
has no defined ratio; missing series or a zero denominator should not be presented
as a measured zero-percent hit rate. Keep HBM hits separate when comparing engine
prefix caching with external reuse.

### Posix load task volume

```promql
sum by (instance, worker_rank) (
  rate(ucm:posix_s2h_bytes_total{job="vllm"}[5m])
) / 1e9
```

The result is nominal task GB/s over wall-clock time, including idle intervals.
The current synchronous and AIO engines add the requested shard bytes in the task
completion callback, including error and abort paths. Check errors alongside this
query; it is not a measurement of successfully transferred physical bytes.
Use filesystem or device telemetry for actual I/O throughput. A per-task bandwidth
histogram also uses the task size and cannot remove this accounting limitation.

### Mean connector wait duration

```promql
sum by (instance, worker_rank) (
  rate(ucm:connector_wait_for_save_duration_ms_sum{job="vllm"}[5m])
)
/
sum by (instance, worker_rank) (
  rate(ucm:connector_wait_for_save_duration_ms_count{job="vllm"}[5m])
)
```

This is a mean in milliseconds for that connector method. Histogram `_count` is
the number of observations, not the number of user requests. For a percentile,
use the metric's `_bucket` series with `histogram_quantile`, preserving `le` while
aggregating buckets. Do not average per-worker percentiles.

The catalog also defines `save_duration` and `save_completion_wait_duration` in
milliseconds despite their unsuffixed names. Read each definition before doing a
unit conversion. Timings from nested operations can overlap; summing their means
or percentiles does not reconstruct TTFT.

### Recent storage errors

```promql
increase(ucm:posix_io_errors_total{job="vllm"}[5m])
```

Use `rate` or `increase` for counters so process resets are handled. Keep raw
Gauges for present state. For errors, retain process labels until logs identify the
failed operation; aggregate only after deciding which processes belong to the
same deployment or storage service.

## Resource estimates and missing data

Posix capacity metrics describe the configured logical budget and GC sampling
estimate. They do not reserve filesystem capacity, and they do not replace mount
or node monitoring.

YuanRong also exposes `yuanrong_*` load counters and optional resource-log metrics.
The `*_local_dram_load_hits_total`, `*_remote_load_hits_total`,
`*_local_ssd_load_hits_total` and `*_l2_load_hits_total` values are estimates derived
from its resource log. Check `yuanrong_resource_log_last_update_timestamp_seconds`
and reporter state before using those estimates in a layer breakdown.

When a series is absent or unchanged, check in this order:

1. Is `enable_metrics` enabled and is the expected consumer selected?
2. Does the loaded catalog contain this metric, with the expected exported name?
3. Has the operation actually occurred, and is the engine collecting connector statistics?
4. Do the scrape target and query selectors match the emitted labels?
5. Is a resource-log reporter or backend probe current?

Compare the same observation window, model/cache layout and process scope.
The [benchmark guide](../../benchmark/index.md) explains how to record those
conditions and separate a cache-hit observation from a performance result.
