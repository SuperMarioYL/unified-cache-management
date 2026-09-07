# UCM 健康指标

UCM Pipeline Store 默认对支持健康检查的 Store 阶段启用探测和熔断；可通过 `store_health.enabled: false` 关闭。健康指标分别回答两个问题：

- 一段时间内有多少次健康探测成功或失败？
- 当前是否有 Store 因熔断而停止接收新请求？

Counter 和 Gauge 分别描述这两类信息，需要使用不同的聚合方法。本页说明 Posix 与 Mooncake 的探测方式、指标含义及 PromQL 聚合方式。

## 1. 健康探测

每个 UCM connector 实例独立探测所拥有的 Store，并为每个支持健康检查的 Store 维护熔断器。Worker Store 的 `worker_rank` 为数值，scheduler Store 的标签为 `worker_rank="scheduler"`。Scheduler 虽然不是分布式 rank，但同样拥有 Store，因此纳入 Store 数量统计。

目前以下远端 Store 实现了健康检查和熔断，其他 Store 不受影响。

### 1.1 Posix Store

Posix 在每个健康检查路径上执行完整的小文件 I/O：创建文件、写入 4 KiB 测试数据、按需要同步、读回并校验数据，最后删除文件。任一路径上的打开、读、写、同步、校验或删除操作失败，都会使该次探测失败。

因此，探测检查的是真实 I/O 路径，而不仅是目录是否存在。对 NFS 等远程文件系统，它也能够发现挂载、网络或远端存储故障。

### 1.2 Mooncake Store

Mooncake 使用专用临时 key，依次执行小规模 Put、Get、内容校验和 Remove。客户端不可用、操作失败或读回内容不一致，都会使探测失败。

## 2. 健康指标

默认配置包含六个健康指标：

| 指标 | 类型 | 含义 | 更新方式 |
| --- | --- | --- | --- |
| `ucm:posix_healthy_count_total` | Counter | Posix 健康探测成功次数 | 每次成功后加 1 |
| `ucm:posix_unhealthy_count_total` | Counter | Posix 健康探测失败或超时次数 | 每次失败后加 1 |
| `ucm:posix_store_health` | Gauge | Posix 熔断器的有效状态：1 为可用，0 为熔断 | 启动时及每次探测后更新 |
| `ucm:mooncake_healthy_count_total` | Counter | Mooncake 健康探测成功次数 | 每次成功后加 1 |
| `ucm:mooncake_unhealthy_count_total` | Counter | Mooncake 健康探测失败或超时次数 | 每次失败后加 1 |
| `ucm:mooncake_store_health` | Gauge | Mooncake 熔断器的有效状态：1 为可用，0 为熔断 | 启动时及每次探测后更新 |

使用 Gauge 判断 Store 当前是否熔断；结合成功和失败 Counter 分析一段时间内的探测质量。目前没有专门记录熔断或恢复状态迁移次数的 Counter。

指标名区分 Store 类型，标签区分 vLLM 实例和 UCM 进程。Connector 指标带有 `model_name`、`engine` 和 `worker_rank`；Prometheus 另加 `job` 和 `instance`。完整标签定义见 [UCM 指标](metrics.md)。

### 2.1 Connector 模式下的同步延迟

UCM 内部健康线程按配置的间隔持续运行，但只有 vLLM 调用 `get_kv_connector_stats()` 时，connector 指标才同步到 `/metrics`。没有推理请求时，即使后台探测结果发生变化，Prometheus 看到的健康指标也不会更新。

## 3. 聚合方式

以下示例省略了部分选择条件。生产查询至少应限定 `job`、`instance`、`model_name` 和 `engine`，并按需要筛选 `worker_rank`。

- 仅 scheduler：`worker_rank="scheduler"`
- 仅 worker：`worker_rank!="scheduler"`
- 不筛选 `worker_rank`：包含 scheduler 和所有 worker

### 3.1 查看各 Store 的当前状态

```promql
ucm:posix_store_health{
  job="vllm",
  instance="10.0.0.8:8000",
  model_name="Qwen3-32B"
}
```

值为 1 表示对应 `worker_rank` 的 Store 可用，为 0 表示已熔断。该查询可直接定位异常的 worker Store 或 scheduler Store。

### 3.2 统计健康与熔断 Store 数量

Gauge 只取 0 或 1，因此用 `sum` 统计健康数量，用 `count - sum` 统计熔断数量：

```promql
# Healthy Store count
sum by (job, instance, model_name, engine) (
  ucm:posix_store_health
)
```

```promql
# Fused Store count
clamp_min(
  count by (job, instance, model_name, engine) (
    ucm:posix_store_health
  )
  -
  sum by (job, instance, model_name, engine) (
    ucm:posix_store_health
  ),
  0
)
```

vLLM 仪表盘底部 **Store Health Metrics** 分组中的健康状态数量面板也采用这一基本聚合方式。Posix 和 Mooncake 应分别计算，避免混合不同后端状态。

#### 理解 Store 数量

| 部署形态 | 指标中可见的 Store 数量 | 说明 |
| --- | ---: | --- |
| DP1、TP1 | 2 | 一个 worker Store 和一个 scheduler Store |
| DP1、多个 TP rank | `TP + 1` | TP 个 worker Store 和一个 scheduler Store |
| 多个 DP rank | `DP × (TP + 1)` | 每个 DP rank 分别创建 worker Store 与 scheduler Store |

DeepSeek V4 每个 worker 实际有两个 Store，但指标会合并它们的状态，显示为一个健康或不健康值。

### 3.3 计算健康 Store 比例

```promql
sum by (job, instance, model_name, engine) (
  ucm:posix_store_health
)
/
clamp_min(
  count by (job, instance, model_name, engine) (
    ucm:posix_store_health
  ),
  1
)
```

分子是健康 Store 数，分母是已上报的 Store 总数。由于 `posix_store_health` 只取 0 或 1，表达式等价于对 Gauge 求 `avg()`，但显式写成“健康数量 / 总数量”更容易理解。例如，8 个 Store 中有 2 个熔断，结果为 0.75。

不筛选 `worker_rank` 时，分子和分母都包含 scheduler Store。DeepSeek V4/HMA/FAWA 路径中的两个 Store 合并为一个 Gauge，因此该查询得到的是可见健康状态的比例，并非底层 FA 和 WA Store 的精确健康比例。

### 3.4 计算探测失败比例

先分别聚合成功与失败次数，再计算比例。不要先计算每个 Store 的失败比例，再取算术平均。

```promql
(
  sum by (job, instance, model_name, engine) (
    rate(ucm:posix_unhealthy_count_total[5m])
  )
  or
  0 * sum by (job, instance, model_name, engine) (
    rate({__name__=~"ucm:posix_(healthy|unhealthy)_count_total"}[5m])
  )
)
/
clamp_min(
  sum by (job, instance, model_name, engine) (
    rate({__name__=~"ucm:posix_(healthy|unhealthy)_count_total"}[5m])
  ),
  1e-12
)
```

该表达式按探测次数加权。`or 0 * ...` 在失败时间序列尚未出现时补 0，避免健康 Store 显示 No data。查询 Mooncake 时，将 `posix` 前缀替换为 `mooncake`。

用 `increase()` 统计时间窗口内的失败探测次数：

```promql
(
  sum by (job, instance, model_name, engine) (
    increase(ucm:posix_unhealthy_count_total[15m])
  )
  or
  0 * sum by (job, instance, model_name, engine) (
    increase({__name__=~"ucm:posix_(healthy|unhealthy)_count_total"}[15m])
  )
)
```

`rate()` 和 `increase()` 会处理进程重启造成的 Counter 归零。不要直接相减原始 Counter 值，也不要将 Counter 当成当前健康状态。

默认探测间隔是 10 秒，但 connector 同步依赖请求。低流量服务可以使用 5–15 分钟等较长窗口，降低延迟同步和样本数过少造成的波动。

## 4. 多实例聚合

| 监控目标 | 建议方式 | 不适用的方式 |
| --- | --- | --- |
| 判断一个 Store 是否熔断 | 保留 `worker_rank` 查看 Gauge | 将 Gauge 求和后作为布尔值 |
| 判断实例内是否有 Store 熔断 | 按实例对 Gauge 求 `min` | 求 `avg` 后仅判断是否大于 0 |
| 统计实例内健康和熔断数量 | 使用 `sum` 与 `count - sum` | 随时间累加 Gauge 状态 |
| 计算窗口内探测失败比例 | 先聚合 Counter 的分子和分母，再相除 | 对各 Store 失败比例取算术平均 |
| 统计独立物理后端故障 | 结合后端标识、日志或外部监控去重 | 直接累加所有 Store 的失败 Counter |

需要按集群、节点或存储故障域聚合时，在 Prometheus target 配置中添加稳定标签：

```yaml
static_configs:
  - targets:
      - "10.0.0.8:8000"
    labels:
      cluster: "production-a"
      node: "inference-01"
      storage_domain: "posix-cluster-a"
```

将这些标签加入 `by (...)`。只有确认选中的时间序列属于同一故障域，聚合结果才有意义。UCM 目前不会从存储路径或端点自动推导这些标签。

## 5. 告警建议

### 5.1 Posix 熔断持续存在

```promql
min by (job, instance, model_name, engine) (
  ucm:posix_store_health
) == 0
```

配置合适的 `for` 时长，例如 30 秒，避免观测侧的短暂波动立即触发通知。熔断器本身已通过滑动窗口过滤单次探测失败，告警延迟不能代替熔断逻辑。

### 5.2 探测失败比例持续偏高

使用第 3.4 节的失败比例，同时要求窗口内有足够的失败样本。例如，15 分钟内失败比例超过 20%，且至少发生 3 次失败探测时告警。阈值应结合探测间隔、Store 数量和服务容忍度调整。

## 6. 故障排查

当 Gauge 为 0 或失败 Counter 增长时：

1. 用 `instance`、`engine` 和 `worker_rank` 定位进程。
2. 在 UCM 日志中搜索 `Store health check` 和 `transitioned to UNHEALTHY/HEALTHY`。
3. 对 Posix，检查挂载状态、目录权限、可用空间，以及读、写、删除操作。
4. 对 Mooncake，检查客户端、metadata/master 服务、网络及 Put/Get/Remove 路径。
5. 确认请求正在触发 connector 指标同步，且 Prometheus target 为 UP。
6. 后端恢复后，观察连续成功探测，确认 Gauge 恢复为 1。

在 Grafana 中导入 `examples/metrics/grafana_vllm.json`，即可在底部 **Store Health Metrics** 分组查看健康/熔断数量及 Posix/Mooncake 探测趋势。
