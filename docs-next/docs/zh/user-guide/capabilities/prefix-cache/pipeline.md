# Pipeline Store

`UcmPipelineStore` 组合已注册的 Store 实现。常用的 `Cache|Posix` 管线在设备内存、主机缓存和 POSIX 文件系统之间传输 KV 数据。Posix 阶段可使用本地 SSD 或已有 NFS 挂载，因此 Pipeline Store 也支持文件系统持久化。

## 配置 Cache 与 Posix

在 vLLM 的 UCM YAML 中，connector 级选项放在根节点，存储参数放在 `ucm_connector_config`：

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

创建存储目录，并允许推理服务进程读取、写入和删除文件。使用容器时，将目录挂载到持久存储。通过 `kv_connector_extra_config.UCM_CONFIG_FILE` 传入 YAML 路径，详见 [vLLM 快速开始](../../quick_start/quickstart_vllm.md)。

示例中的 4 GiB 容量和 buffered I/O 是首次测试的显式设置。原生 Cache Store 默认分配 256 GiB；启用共享 buffer 且未指定容量时，vLLM connector 会设为 128 GiB。未共享的 worker 各自独立分配。MLA 默认启用共享 buffer，共享分配必须能放入 `/dev/shm`。请按实际部署拓扑计算容量，不能把默认值视为环境中可用的内存。

原生 Cache 和 Posix Store 的 `io_direct` 默认为 `true`。文件系统和 I/O 对齐满足要求时可启用；`posix_io_engine: aio` 要求使用 Direct I/O。`timeout_ms` 在 Store 配置中默认为 30000，放在 YAML 根节点不会设置 Store 任务超时。

## 存储容量与健康检查

`posix_capacity_gb` 默认为 `0`，表示不启用按容量触发的垃圾回收。正值定义 Store 的容量预算，并按所配置阈值触发 GC。多个实例共享同一存储命名空间时，需要明确回收归属，并统计所有实例写入的文件。vLLM connector 在单实例内选择 DP0 scheduler 作为 GC 执行者。容量配置不会为文件系统预留空间。

对于支持健康检查的 Store 阶段，Pipeline 默认启用健康探测和熔断。默认探测间隔为 10 秒、超时为 3 秒、窗口为 8 个样本、失败阈值为 2。Posix 探测执行真实的小文件 I/O，比仅确认目录存在更能反映存储路径是否可用，但它仍不能证明某个请求命中了 KV cache。详见[健康指标](../../observability/health-metrics.md)。

## 其他已注册管线

管线名称在 `ucm/store/pipeline/connector.py` 中注册，不能任意拼接阶段名称。注册表包含 Cache/Posix、DS3FS、压缩、Mooncake、YuanRong 以及测试管线，各自需要对应的构建组件和配置。参见相应的[后端指南](index.md#storage-backends)。

SGLang 已提供主机缓存，因此 UCM 适配器直接选择 `Posix`。配置布局参见 [SGLang 快速开始](../../quick_start/quickstart_sglang.md)。

## 验证写入与读取

按[重启并重放请求](../../quick_start/quickstart_vllm.md#verify-the-service-and-external-cache)的步骤验证：第一个进程写入 KV 块，保留存储，以相同的模型和缓存块布局重启，再确认第二个进程出现外部命中 token 和 Posix 读取。注意区分 KV 缓存文件与临时健康探测文件。其他参数参见[配置参考](../../../reference/config-parameters.md)；缓存未写入或未复用时，参见[故障排查](../../../reference/troubleshooting.md)。

## 历史性能报告 { #historical-performance-report }

以下表格保留自原 Pipeline Store 指南，描述当时记录的模型、硬件和 80% SSD 命中负载，并非本次文档版本的新测试结果。原文未固定完整的 UCM 与引擎版本组合；用于部署决策前，应记录实际环境并重新测量。

来源：[原 Pipeline Store 报告](https://github.com/ModelEngine-Group/unified-cache-management/blob/a336d69bc03a550d44bee3df9da7664e9edfe3a7/docs/source/user-guide/prefix-cache/pipeline_store.md)。

### 测试概览

以下是原报告在 CUDA 环境中，对 Prefix Cache 场景进行不同并发度测试的结果。测试禁用 HBM 缓存，仅从 SSD 查询和匹配 KV Cache。

Full Compute 表示纯 vLLM 计算；SSD80% 表示启用 UCM 池化后，KV cache 的 SSD 命中率为 80%。

下表为 QwQ-32B 模型的结果（**4 张 H100 GPU**）：

|      **QwQ-32B** |                |                      |                |               |
| ---------------: | -------------: | -------------------: | -------------: | :------------ |
| **输入长度** | **并发数** | **Full Compute (ms)** | **SSD80% (ms)** | **加速比例（%）** |
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


下表为 DeepSeek-R1-awq 模型的结果（**8 张 H100 GPU**）：

|**DeepSeek-R1-awq**|                |                      |                |               |
| -----------------:| -------------: | -------------------: | -------------: | :------------ |
| **输入长度**  | **并发数** | **Full Compute (ms)** | **SSD80% (ms)** | **加速比例（%）** |
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
