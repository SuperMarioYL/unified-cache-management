# Pipeline Store

`UcmPipelineStore` composes registered Store implementations. The common
`Cache|Posix` pipeline moves KV data between device memory, a host-memory cache,
and a POSIX filesystem. Its Posix stage can use local SSDs or an existing NFS
mount; Pipeline Store is not an in-memory-only backend.

## Configure Cache and Posix

In a vLLM UCM YAML file, connector-wide options belong at the root and storage
options belong in `ucm_connector_config`:

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
      store_health:
        enabled: true
        health_check_interval_s: 10
        health_check_timeout_s: 3
        health_window_size: 8
        failure_threshold: 2
use_layerwise: true
enable_event_sync: true
enable_metrics: true
```

Create the storage directory and give the serving process read, write, and
remove access. In a container, mount the directory persistently. Pass the YAML
path through `kv_connector_extra_config.UCM_CONFIG_FILE`, as shown in the
[vLLM quickstart](../../quick_start/quickstart_vllm.md).

The 4 GiB capacity and buffered I/O above are explicit initial-test choices.
Native Cache Store defaults to 256 GiB; the vLLM connector supplies 128 GiB when
shared buffers are enabled and no capacity is set. Unshared workers allocate
independently. Shared buffers default on for MLA, and their allocation must fit
`/dev/shm`. Size the buffer for your topology instead of treating a default as
available memory.

`io_direct` defaults to `true` in the native Cache and Posix Stores. Enable it
when the filesystem and I/O alignment support it; `posix_io_engine: aio`
requires direct I/O. `timeout_ms` defaults to 30000 inside the Store config;
placing it at YAML root does not configure Store task timeouts.

## Storage capacity and health

`posix_capacity_gb: 0` is the default and disables capacity-driven garbage
collection. A positive value defines the Store's capacity budget and triggers
GC using the configured thresholds. Plan ownership when several instances
share the storage namespace; account for files written by all of them. The
vLLM connector selects its DP0 scheduler as the GC owner within an instance.
Capacity configuration does not reserve filesystem space.

Pipeline health probes and circuit breaking are enabled by default for Store
stages that support health checks. The default interval is 10 seconds, timeout
3 seconds, window 8 samples, and failure threshold 2. Posix probes perform real
small-file I/O, so a successful probe is stronger evidence than directory
existence, but it is not evidence of a request's KV-cache hit. See
[Health metrics](../../observability/health-metrics.md).

## Other registered pipelines

Pipeline names are registered in `ucm/store/pipeline/connector.py`; they are not
arbitrary combinations of stage names. The registry includes Cache/Posix,
DS3FS, compression, Mooncake, YuanRong, and test pipelines. Each requires its
own built components and configuration. Consult the relevant
[backend guide](index.md#storage-backends).

SGLang supplies its own host cache and its UCM adapter selects `Posix` directly.
Its configuration layout is described in the
[SGLang quickstart](../../quick_start/quickstart_sglang.md).

## Verify writes and reads

Follow the [restart-and-replay check](../../quick_start/quickstart_vllm.md#verify-the-service-and-external-cache):
write KV blocks on the first process, keep storage, restart with the same model
and cache geometry, then confirm external-hit tokens and Posix reads on the
second process. Distinguish cache files from temporary health-probe files.
Use the [configuration reference](../../../reference/config-parameters.md) for
other Store parameters and [troubleshooting](../../../reference/troubleshooting.md)
when the cache is not populated or reused.

## Historical performance report { #historical-performance-report }

The following tables are retained from the original Pipeline Store guide.
They describe its recorded models, hardware, and 80% SSD-hit workload, not
measurements of this documentation version. The source does not pin a complete
UCM/engine revision pair, so reproduce with an explicitly recorded environment
before using the numbers for a deployment decision.

Source: [original Pipeline Store report](https://github.com/ModelEngine-Group/unified-cache-management/blob/a336d69bc03a550d44bee3df9da7664e9edfe3a7/docs/source/user-guide/prefix-cache/pipeline_store.md).


### Overview
The following are the multi-concurrency performance test results of UCM in the Prefix Cache scenario under a CUDA environment, showing the performance improvements of UCM.
During the tests, HBM cache was disabled, and KV Cache was retrieved and matched only from SSD.

Here, Full Compute refers to pure VLLM inference, while SSD80% indicates that after UCM pooling, the SSD hit rate of the KV cache is 80%.

The following table shows the results on the QwQ-32B model(**4 x H100 GPUs**):

|      **QwQ-32B** |                |                      |                |               |
| ---------------: | -------------: | -------------------: | -------------: | :------------ |
| **Input length** | **Concurrent** | **Full Compute (ms)** | **SSD80% (ms)** | **Speedup (%)** |
|            4 000 |              1 |              223.05 |         156.54 | **+42.5%**   |
|            8 000 |              1 |              350.47 |         228.27 | **+53.5%**   |
|           16 000 |              1 |              708.94 |         349.17 | **+103.0%**  |
|           32 000 |              1 |             1512.04 |         635.18 | **+138.0%**  |
|            4 000 |              8 |              908.52 |         625.92 | **+45.1%**   |
|            8 000 |              8 |             1578.72 |         955.25 | **+65.3%**   |
|           16 000 |              8 |             3139.03 |        1647.72 | **+90.5%**   |
|           32 000 |              8 |             6735.25 |        3025.23 | **+122.6%**  |
|            4 000 |             16 |             1509.79 |         919.53 | **+64.2%**   |
|            8 000 |             16 |             2602.34 |        1480.30 | **+75.8%**   |
|           16 000 |             16 |             5732.49 |        2393.54 | **+139.5%**  |
|           32 000 |             16 |            11891.61 |        4790.00 | **+148.3%**  |


The following table shows the results on the DeepSeek-R1-awq model (**8 × H100 GPUs**):

|**DeepSeek-R1-awq**|                |                      |                |               |
| -----------------:| -------------: | -------------------: | -------------: | :------------ |
| **Input length**  | **Concurrent** | **Full Compute (ms)** | **SSD80% (ms)** | **Speedup (%)** |
|             4 000 |              1 |               429.30 |        261.34 | **+64.3%**   |
|             8 000 |              1 |               762.23 |        363.37 | **+109.8%**  |
|            16 000 |              1 |              1426.06 |        586.17 | **+143.3%**  |
|            32 000 |              1 |              3086.85 |       1073.25 | **+187.6%**  |
|             4 000 |              8 |              1823.55 |       1017.72 | **+79.2%**   |
|             8 000 |              8 |              3214.76 |       1511.16 | **+112.7%**  |
|            16 000 |              8 |              6417.81 |       2596.70 | **+147.2%**  |
|            32 000 |              8 |             14278.00 |       5111.67 | **+179.3%**  |
|             4 000 |             16 |              3205.22 |       1534.00 | **+108.9%**  |
|             8 000 |             16 |              5813.09 |       2208.60 | **+163.2%**  |
|            16 000 |             16 |             11752.48 |       4000.46 | **+193.8%**  |
|            32 000 |             16 |             38643.73 |      19910.41 | **+94.1%**   |
