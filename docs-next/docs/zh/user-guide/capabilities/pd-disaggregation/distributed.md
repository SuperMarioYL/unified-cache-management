# Ascend 上的传输连接器与 UCM 组合

当前 Ascend PD Chart 配置在 Prefill 侧组合两个 connector：Mooncake 将当前请求的 KV 发送给 Decode，UCM 从外部存储加载并保存可复用的前缀块。Decode 只运行传输 consumer。这样，Prefill 可以复用前缀，而不要求 Decode 读取 UCM 存储。

请求顺序与初始化标识的原理见[PD 集成原理](../../../developer-guide/pd-integration.md)。


## 原手工部署指南

不使用 Helm 时，可以阅读[此源码修订保留的完整手工部署步骤](https://github.com/ModelEngine-Group/unified-cache-management/blob/a336d69bc03a550d44bee3df9da7664e9edfe3a7/docs/source/user-guide/pd-disaggregation/distributed_pd.md)：包括 Mooncake master、配置文件、Prefill/Decode 启动脚本、多 DP 进程启动和代理命令。下面的集群配置说明与这条手工路线分别使用。

原文命令绑定其模型、网络和引擎环境；迁移到其他 vLLM-Ascend 版本时，需要核对连接器及并行参数。当前文档没有验证这些历史脚本在新版引擎上的兼容性。

## 选择部署配置

从解压后 Chart 中的 `models/ascend/values-qwen3-0p6b-1p1-1d1.yaml` 开始。它定义一个 Prefill 角色、一个 Decode 角色、Mooncake master 和路由资源。按 [Helm 部署](../../frameworks/kubernetes/deploy.md)准备集群和站点 values，完成渲染和安装。

该文件是配置示例。需要根据[安装](../../quick_start/index.md)替换引擎镜像，挂载目标模型，并为目标集群设置资源、存储、网络和调度器参数。必须替换示例中的 StorageClass 占位值；主机挂载和 RDMA 资源名也需要实际集群支持。

## 明确各配置项的职责

以下字段位于 `servingEngineSpec.modelSpec` 下：

| 字段 | 职责 |
| --- | --- |
| `roles[]` | P/D 副本数、worker、设备资源和模型参数 |
| `pd.prefill`, `pd.decode` | 指定路由使用的角色名 |
| `pd.kvTransfer.connector` | 选择引擎传输连接器 |
| `pd.kvTransfer.routerType` | 选择对应路由协议 |
| `pd.kvTransfer.identity` | 预留 engine ID 和传输端口范围 |
| `unifiedcacheConfig` | 在 Prefill 侧启用并配置 UCM |
| `storage.unifiedcacheStorage` | 提供 UCM 挂载；挂载路径用于填充 `storage_backends` |

`MooncakeConnectorV1` 和 `MooncakeHybridConnector` 对应的路由器类型是 `mooncake`。UCM 配置有效时，Chart 在 Prefill 侧组合 `MultiConnector`，Decode 侧仍只保留传输 consumer。不要在 `roles[].vllmArgs` 中再添加一份 `--kv-transfer-config`，该参数由 Chart 管理。

`NixlConnector` 使用 `nixl` 路由器类型，但当前 Chart 不允许它与有效 UCM 配置组合。传输名称通过 Chart 校验，不代表所选 Ascend 镜像实现了该连接器；修改内置 Mooncake 配置前，先验证镜像的 connector 支持情况。

禁用 `unifiedcacheConfig.enabled` 会移除 UCM connector 及其管理的缓存挂载，同时保留 PD 传输。镜像的 `ENABLE_UCM_PATCH` 环境变量独立配置，进行基线比较时应显式记录。

## 分三个阶段验证

**服务与传输。**检查 Prefill/Decode 就绪状态和路由对象，再通过已安装的 kthena-router 网关发送冷请求。检查选中的角色和传输错误。Release 的引擎 Service 同时选择两个角色，用于监控，不是 PD 客户端入口。

**外部复用。**使用固定提示词填充 UCM 存储，确认写入完成，保留存储并重启服务进程，再重放该提示词。分别检查 Prefill 的 UCM 命中 token、成功加载活动和引擎内存命中。在这一拓扑中，Decode 不需要出现 UCM 命中。

**性能。**分别在禁用 UCM、UCM 冷缓存和外部缓存已预热的状态下重放同一批流量。保持传输配置、P/D 数量、输出长度和生成设置不变。采集失败、输出正确性、客户端 TTFT、TPOT、吞吐以及存储与传输耗时。

## 按职责边界排查

| 症状 | 首先检查的证据 |
| --- | --- |
| 角色未达到 Ready | 设备分配、模型挂载、运行时兼容性、集合通信初始化 |
| 引擎独立运行正常，但网关请求失败 | `ModelRoute`、角色标签、路由协议、传输地址与端口可达性 |
| 冷 PD 请求正常，但重复提示词没有 UCM 命中 | Prefill connector 组合、持久化阈值、缓存内容和 key 兼容性 |
| 命中增加，但 TTFT 没有改善 | 成功加载的延迟、节省的 Prefill 计算量、传输时间、路由器与引擎排队 |

多节点角色实例或 MoE 模型可继续阅读[并行与扩展](large-scale-ep.md)。不要在同一步同时更换模型、并行布局和缓存后端，因为解释结果时，它们分别需要不同的验证证据。
