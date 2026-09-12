# Mooncake Store

Mooncake 流水线将可复用 KV 块放入共享内存服务。UCM 连接已有 Mooncake master，通过客户端传输数据。如果 KV 块还需要文件系统后备存储层，可以再组合 Posix。

| 流水线 | 读写行为 |
| --- | --- |
| `Mooncake` | 读写 Mooncake，不包含 UCM 文件系统后备存储层。 |
| `Mooncake|Posix` | 优先读取 Mooncake，未命中时交给 Posix；dump 同时写入 Posix。 |

这与首阶段为本地主机缓冲的 [Cache|Posix](pipeline.md) 不同。推理进程重启后，仍在运行的 Mooncake 服务可以保留内存对象，但这不保证服务故障后数据仍然持久化。`Mooncake|Posix` 的持久化范围取决于配置的文件系统，以及已经完成的后备存储写入。

## 前提条件

当前源码树中的原生 UCM Mooncake 目标需要 Ascend ACL 头文件、`libascendcl` 和 `libmooncake_store`。缺少其中任何一项，CMake 都会跳过该目标。这是一条 Ascend 集成路径；更换 Mooncake 传输名称不会让这个 UCM 目标成为 CUDA 后端。

准备 Mooncake 后，在匹配的 [Ascend 环境](../build_from_source.md#vllm-ascend-ascend-platform)中构建 UCM。CMake 在 `/usr/local/Ascend/ascend-toolkit/latest` 下的 `include` 和 `lib64` 目录搜索 toolkit 依赖，并在 `/usr/local/lib` 搜索 Mooncake 库。`MOONCAKE_STORE_INCLUDE_DIR` 指定 Mooncake 头文件树，当前回退路径为 `/vllm-workspace/Mooncake/mooncake-store/include`。头文件修订必须与链接的客户端库匹配。

启动服务前，确认已安装 `ucm/store/mooncakestore/libmooncakestore.so` 及其依赖。按部署环境的服务配置启动 Mooncake master，验证每个服务进程到 master 的连通性；选择后备存储层时，还需准备可写的 Posix 目录。UCM 不会代为启动 master。

## 启动 Mooncake master

在已经安装匹配 Mooncake 二进制的服务主机上执行：

```bash
mooncake_master --port 50088
```

保持服务运行，将下面的 `master_server_address` 改为这台主机可访问的 IP 与 `50088` 端口。已有 master 时直接使用其地址。容量、租约和回收参数以所安装版本的 `mooncake_master --help` 为准；客户端和服务端需使用兼容版本。

## 配置前缀缓存

下面是用于带 Ascend 设备、且能访问 Mooncake master 的主机的初始配置。请替换两个地址和存储目录：

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

`local_hostname` 为必填项，需要按传输方式正确标识服务主机。只有相关服务都在同一主机或网络命名空间内时，才适合使用回环地址。保留 `enable_event_sync: true`：原生 dump 路径在访问新计算的 KV 之前，会等待前置事件。按 [vLLM-Ascend 快速开始](../../user-guide/quick_start/index.md#vllm-ascend)，通过 `UCM_CONFIG_FILE` 传入此文件并启动模型服务。

如果使用不带文件系统后备层的 Mooncake，设置 `store_pipeline: Mooncake`，并移除 `storage_backends` 和 `posix_io_engine`。这条 V1 路径不要设置 `ucm_connector_name: UcmMooncakeStore`，当前工厂通过 `UcmPipelineStore` 注册它。

## 规划内存与并发

为进行初步测试，示例显式调低了原生默认的 30 GiB global segment 和 64 GiB shared-buffer 容量。两者是不同的内存分配，应根据实际 worker 数和 KV 张量大小配置。原生 local buffer 默认是 1 GiB。正数 `_gb` 设置优先于对应的、以字节为单位的 global/local 设置。

`stream_number` 默认是 4，取值必须在 1 到 32 之间。如果没有显式设置 `host_buf_pool_size`，私有主机缓冲池大小由 stream 数量和张量大小推导。`replica_num` 默认是 1，必须为正数；副本数量不是持久化策略。

vLLM 适配层和 Mooncake 阶段使用不同的共享缓冲设置：`share_buffer_capacity_gb` 属于 Mooncake。对于适配层启用共享缓冲的模型，当前预检查仍读取 `cache_buffer_capacity_gb`；示例显式设置该值，以避免适配层隐式执行 128 GiB 检查。该值不决定 Mooncake 缓冲大小。完成初始请求后、扩大规模前，应查看实际启动配置和主机内存。

## 分别验证每一层

1. 记录模型修订、dtype、并行布局和 Mooncake 命名空间，运行新前缀请求并等待 dump 完成。
2. 保持 Mooncake 服务运行，通过重启后的推理进程重放。必须观察到外部命中和 Mooncake 读取、命中指标。
3. 对于 `Mooncake|Posix`，确认后备文件存在且 Posix dump 已完成。要测试后备读取，在隔离测试环境中只驱逐 Mooncake 内的测试对象，保留文件后再次重放。
4. 最后一种情况必须观察到 Posix 读取和 Mooncake backend-load 指标。仅 Mooncake 命中不能证明文件系统层正常。

加载命中、未命中、后端和字节计数器见[指标](../../user-guide/observability/metrics.md)，主动探针见[健康指标](../../user-guide/observability/health-metrics.md)。Mooncake 健康检查执行小规模写入、读取、删除操作，不能替代双层请求测试。

## 定位故障所在环节

| 症状 | 下一步检查 |
| --- | --- |
| 缺少原生库或存在未解析符号 | 可选 CMake 目标、Ascend 库，以及匹配的 Mooncake 头文件和库。 |
| Setup 或 lookup 无法访问服务 | Master 地址、local hostname、传输配置和服务日志。 |
| 预期有 Posix 命中，但实际没有 | 后备写入是否完成、存储路径是否匹配，以及 Posix 健康状态。 |
| 队列拒绝或加载延迟持续增加 | 队列与阶段指标、主机缓冲需求、服务容量和网络流量。 |

性能比较需要区分命中的具体层。分别报告内存服务命中和文件系统命中；不能因为启用了这条流水线就预设会有加速。

[实现与扩展说明](../extending-store.md#backend-entrypoints)。
