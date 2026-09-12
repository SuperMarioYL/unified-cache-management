# 多节点并行下的 PD 分离

扩展 PD 部署包含两个独立选择：运行多少个独立 Prefill/Decode 实例，以及每个实例如何将模型分布到设备和节点上。UCM 在所选引擎集成中提供前缀复用，不负责选择模型的专家划分或协调集合通信。

先建立正常工作的[基于传输连接器的 PD 部署](distributed.md)。本页说明当前 Chart 如何表达更大的拓扑，以及认定扩展带来性能提升之前需要收集哪些证据。

## 原手工部署指南

不使用 Helm 时，可以阅读[此源码修订保留的完整手工部署步骤](https://github.com/ModelEngine-Group/unified-cache-management/blob/a336d69bc03a550d44bee3df9da7664e9edfe3a7/docs/source/user-guide/pd-disaggregation/large_scale_ep.md)：包括 Mooncake master、配置文件、Prefill/Decode 启动脚本、多 DP 进程启动和代理命令。下面的集群配置说明与这条手工路线分别使用。

原文命令绑定其模型、网络和引擎环境；迁移到其他 vLLM-Ascend 版本时，需要核对连接器及并行参数。当前文档没有验证这些历史脚本在新版引擎上的兼容性。

## 修改参数前先描述拓扑

三个副本设置的含义不同：

| 设置 | 含义 |
| --- | --- |
| `modelSpec.replicas` | 独立 serving group 的数量 |
| `roles[].replicas` | 每个 group 内该角色的逻辑实例数量 |
| `roles[].workerReplicas` | 每个逻辑实例额外包含的 worker Pod 数量 |

一个 serving group 的引擎 Pod 总数，等于各角色 `role replicas × (1 + workerReplicas)` 之和。设备数量再由每个 Pod 的资源请求决定。这里不包含路由器、Mooncake master 或其他控制面 Pod。

例如，内置 `values-qwen3-0p6b-2p2-2d2.yaml` 定义两个 Prefill 实例和两个 Decode 实例，每个实例带一个 worker，因此每个 serving group 请求八个引擎 Pod，而不是四个。默认按 hostname 设置的反亲和也会影响所需的可调度节点数量。应查看渲染后的放置规则，不要从配置文件名推断节点数。

## 由引擎管理 DP、TP 和 EP

张量并行、数据并行和专家并行都是引擎执行设置。将引擎支持的模型与并行参数放到各角色的 `vllmArgs` 中，并确认该引擎版本接受目标组合。多节点 DP 的全局与本地 DP 大小，必须与实际分配给角色实例的 Pod 和设备数匹配。

Chart 管理 HTTP 绑定、DP 地址与 rank 协调、对外模型名以及 KV 传输参数。不要重复添加这些参数，也不要把另一套多进程启动脚本粘贴到配置里。沿用现有入口，确保渲染配置与实际启动的进程对应。

仓库包含 DeepSeek-V3.1 和 Qwen3-235B 的 Ascend 多节点配置，但这些文件定义的是单角色服务布局。它们可以用于参考模型参数结构，不是开箱即用的 EP PD 配方。面向具体模型的 EP 部署，仍然需要验证过的引擎配置、集合通信网络和足够的目标硬件内存。

把 P 和 D 改为不同的 TP 或 EP 布局，也会改变传输兼容性要求。需要验证所选传输连接器支持的布局转换。对于共享存储 PD，常规 UCM key 包含 TP 大小和 rank，不能假定新布局可以复用旧布局的块。

## 选择 HTTP 服务模式

Chart 的 `dataParallelMode` 控制多节点角色实例如何暴露 HTTP 端点：

- `standard`：一个入口 HTTP 端点，worker 为 headless；`ModelServer` 选择入口 Pod。
- `hybrid`：多节点角色的入口和 worker 节点都暴露 HTTP，路由器选择这些端点。该模式要求启用 PD 和路由器，并且至少一个角色包含 worker。

Chart 会注入对应的 vLLM LB 参数。不要独立设置 `--data-parallel-hybrid-lb`、`--headless` 或 Chart 管理的 rank 参数。应同时核对最终命令和 `ModelServer.workloadSelector`；健康但未被 selector 选中的 worker 不会接收到路由器流量。

## 预留传输与缓存资源

每个逻辑 P/D 实例获得独立的引擎标识，其入口和 worker 根据 serving-group 与角色标签解析出同一标识。Mooncake 的 `instanceStride` 必须覆盖配置中 DP、TP、PP 和上下文并行所需的端口跨度。Chart 会检查保守边界，并拒绝超过 65535 的端口范围。

集合通信网络、KV 传输和外部存储应分别计算主机及网络资源开销。对于 UCM，需要按进程规划主机缓冲，并为需要共享前缀的 Prefill 实例提供可达存储。当前 Chart 的 Decode 路径仅使用传输 consumer，因此增加 Decode 副本不会增加 UCM 读取者。

按 [Kubernetes 部署](../../frameworks/kubernetes/deploy.md)渲染站点 values，检查生成的 `ModelServing`、`ModelServer` 和 `ModelRoute`。渲染只能验证配置约束，不会在硬件上测试集合通信、传输吞吐或专家放置。

## 每次验证一个变化

1. 在一个 P 和一个 D 上建立输出正确的冷请求基线。
2. 增加所需 worker 和引擎并行配置，保持 UCM 后端与负载不变，确认 rank 初始化和请求传输。
3. 保留存储并重启引擎进程后，验证 Prefill 侧外部复用；将 UCM 命中和成功加载与 HBM 命中分开检查。
4. 增加角色副本或 serving group，确认每个新 Prefill 实例的路由分布、标识唯一性和存储访问。
5. 前一布局正常后，再进行 EP 专项调优；保留足够结果，将差异归因到当前变化。

输入/输出长度分布和请求到达计划应能代表目标负载。报告完成请求数、正确性失败、客户端 TTFT、TPOT、吞吐和资源消耗。如果平均延迟下降的同时失败更多，或者输出 token 更少，就不能视为等价比较。

## 解读结果

Prefill UCM 命中率很高，端到端 TTFT 仍可能很差，原因可能是路由器排队、存储读取慢，或传输在等待 Decode。同样，增加专家并行容量可能提高计算利用率，却增加通信开销。应结合分阶段计时和端点指标确定哪个阶段发生了变化，不能把所有加速都归因于 UCM。