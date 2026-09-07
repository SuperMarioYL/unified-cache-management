# Pipeline Store

需要让 KV 块在推理进程重启后继续从本地磁盘或挂载文件系统复用，同时将近期访问的块保存在主机内存中时，可以选择 `Cache|Posix`。`Cache` 负责设备与主机之间的数据搬运及主机缓冲区，`Posix` 负责文件、查询和文件系统 I/O；设备上的 KV Cache 仍由推理引擎管理。

管线通过已注册的名称选择，不能任意拼接阶段名。Python 构建函数会在启动时加载对应的原生库。当前 vLLM 集成使用的入口是 `UcmPipelineStore`。

## 配置 Cache 与 Posix

先从[安装页面](../../installation.md)选择制品，或在目标引擎环境中[从源码构建](../../../developer-guide/build_from_source.md)。这条管线需要 `ucmpipelinestore` 扩展、`libcachestore.so` 和 `libposixstore.so`。仅能导入 Python 包，还不能说明所选管线能够初始化。

准备独立且可写的目录，并将其挂载到需要访问它的进程中。将下面的配置保存为 UCM YAML 文件：

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

按 [vLLM 快速开始](../../quick_start/quickstart_vllm.md)或 [Ascend 快速开始](../../quick_start/quickstart_vllm_ascend.md)，通过 `kv_connector_extra_config.UCM_CONFIG_FILE` 指向该文件。Connector 会提供设备 ID、块大小和张量布局，不要复制其他模型 Store 测试中的这些数值。

示例使用带缓冲的同步 I/O，便于先建立文件系统读写基线。原生 Cache 和 Posix 阶段的 `io_direct` 默认值为 `true`。只有在设置 `io_direct: true`，且文件系统支持对应的对齐 I/O 时，才切换到 `posix_io_engine: aio`。`timeout_ms` 应放在 `ucm_connector_config` 内，它是 Store 任务超时，不是 HTTP 请求超时。

## 规划主机内存 { #budget-host-memory }

示例中的 4 GiB 是初始预算，并非适用于所有模型的固定值。Cache Store 至少需要容纳 `max(1024, 2 * cache_load_exclusive_buffer_number)` 个 shard，独占缓冲数量默认是 1024。如果容量太小，初始化错误会给出至少需要多少 GiB。

| 未显式设置容量时的分配路径 | 实际默认值 |
| --- | --- |
| 原生 Cache，启用共享缓冲 | 256 GiB |
| 原生 Cache，`share_buffer_enable: false` | 每个 worker 32 GiB |
| 当前 vLLM Connector，启用共享缓冲 | 128 GiB |

vLLM Connector 根据模型是否使用 MLA 决定 `share_buffer_enable` 的默认值。正数 `cache_buffer_capacity_gb` 会覆盖原生默认值。共享缓冲需要足够的 `/dev/shm`；非共享 worker 分别分配内存，因此要按同一主机上的 worker 数量汇总预算。模型权重、设备 KV Cache 和其他主机内存开销需要另外计入。

## 存储容量与健康检查

`posix_capacity_gb: 0` 表示不启用基于容量的垃圾回收。正数值提供容量预算，但不会预留文件系统空间。vLLM Connector 将 Posix GC 交给 DP0 scheduler。多个推理实例共用目录时，需要统计它们共同写入的数据；没有验证其他回收归属方案之前，应保留 Posix 的协调设置。

管线健康检查默认开启，检查间隔为 10 秒、超时为 3 秒、窗口为 8 个样本、失败阈值为 2。Posix 探针实际执行写入、读取、比较和删除。探针成功说明文件系统健康，只有请求级命中和完成的加载才能说明 KV 被复用。详见[健康指标](../../observability/health-metrics.md)。

## 其他已注册管线

| 需求 | 选择 |
| --- | --- |
| 保留已有 vLLM NFS Connector 配置 | [NFS Store](nfs.md) |
| 使用 3FS 客户端 I/O | [`Cache\|Ds3fs`](ds3fs.md) |
| 缩小 BF16 存储载荷，并接受精度权衡 | [`Cache\|Compress\|Posix`](compress.md) |
| 使用共享 Mooncake 内存，可选文件后端 | [`Mooncake` 或 `Mooncake\|Posix`](mooncake.md) |

SGLang 已经管理主机缓存，因此其适配器直接选择 `Posix` 阶段。请使用 [SGLang 快速开始](../../quick_start/quickstart_sglang.md)，不要直接套用 vLLM YAML。

## 验证写入与读取

1. 使用空的测试目录，记录模型 revision、KV dtype、并行布局和实际生效的 Store 配置。
2. 发送带有可复用前缀的请求，长度应足以产生完整块。确认 dump 完成，且已提交的缓存文件出现。
3. 保留目录，重启推理进程，再使用完全相同的输入及缓存几何参数重放。操作步骤见[重启与重放检查](../../quick_start/quickstart_vllm.md#verify-the-service-and-external-cache)。
4. 重放时应同时看到外部缓存命中 token 和 Posix 读取活动。同一进程内第二次请求更快，可能只命中了内存缓存。

如果重放被主机缓存满足，可以在诊断运行中设置 `cache_load_backend_only: true`，单独检查后端查询和加载路径。发生错误时，先核对已加载的阶段名称、目录权限、缓冲区大小提示和 Posix 健康状态，再参照[故障排查](../../../reference/troubleshooting.md)。

## 测量存储收益 { #historical-performance-report }

分别比较无缓存 prefill、存储重放和内存热缓存重放。保持模型与输入集相同，记录外部命中 token、Posix 读取字节数、主机内存、TTFT 和吞吐，判断当前负载下存储 I/O 是否比重新计算更划算。[基准测试指南](../../../benchmark/index.md)说明如何报告这一比较；本页不提供未经目标硬件验证的性能数字。

## 实现依据

- `ucm/store/pipeline/connector.py` 定义注册管线及加载的原生库。
- `ucm/store/cache/cc/cache_store.cc` 管理缓冲默认值和最小容量检查。
- `ucm/store/posix/cc/posix_store.cc` 管理文件系统配置与健康探针。
- `ucm/integration/vllm/ucm_connector.py` 提供布局、共享缓冲默认值和 GC 归属。
