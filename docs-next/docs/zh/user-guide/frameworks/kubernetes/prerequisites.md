# Kubernetes 前提条件

安装 Chart 前，先准备集群与站点配置。[Kubernetes 概览](../kubernetes.md)介绍资源归属和内置部署形态，本页说明这些配置依赖的条件。

## 集群服务

| 要求 | 用途 | Chart 调整方式 |
| --- | --- | --- |
| Kubernetes `>=1.19` 和 Helm 3 | Chart 声明的运行环境 | 用 `kubectl version` 和 `helm version` 确认 |
| kthena controller 和 CRD | 调谐 `ModelServing` 并创建引擎 Pod | 安装本 Chart 前先安装 kthena |
| PD 或路由配置所需的 kthena-router | 处理 `ModelServer`、`ModelRoute` 并接收客户端流量 | 使用已有 kthena 安装提供的网关 |
| 匹配的 GPU/NPU 驱动与设备插件 | 提供部署配置请求的加速器资源 | 只保留目标节点实际提供的资源键 |
| 引擎镜像和拉取凭据 | 在目标平台运行带 UCM 集成的 vLLM | 设置 `images.image` 或 `modelSpec.image`；需要时设置 `imagePullSecret` |
| 模型与 UCM 存储 | 让容器能够访问 `modelPath` 和缓存后端 | 替换示例 NFS 与 StorageClass 配置 |

Chart 不包含这些服务或 CRD，只渲染引用它们的对象。

## 默认值引入的依赖

`servingEngineSpec.schedulerName` 默认为 `volcano`。保留此值时需要安装 Volcano。设为空字符串会移除调度器名称，使 Kubernetes 使用默认调度器；此时不具备 Volcano 的 gang scheduling 行为。

`servingEngineSpec.serviceMonitor.enabled` 默认为 `true`。若集群未安装 Prometheus Operator 的 `ServiceMonitor` CRD，请在安装前设为 `false`。

## 配置依赖 {#configuration-dependencies}

内置配置给出了部署形态、资源示例和存储占位值。请在 `values-site.yaml` 中配置以下项目：

| 配置项 | 需要确定的内容 |
| --- | --- |
| 引擎镜像 | 选择与 CUDA 或昇腾平台兼容的镜像；模型配置不会自动选择镜像。 |
| 模型标识 | 将 `modelSpec.modelPath` 设为容器可见的模型路径，`modelName` 设为 API 模型名。 |
| 模型挂载 | 在默认为空的 `storage.extraStorage` 列表中添加支持的 `hostPath`、PVC、CSI 或 NFS 存储源。 |
| UCM 存储 | 替换 `unifiedcacheStorage` 的 StorageClass、访问模式和容量示例。 |
| 加速器资源 | 核对 `nvidia.com/gpu` 或 `huawei.com/Ascend910`，以及 RDMA、CPU、内存、runtime class、标签和容忍度。 |
| 调度和监控 | 确认集群是否提供 Volcano 和 Prometheus Operator。 |
| 网络拓扑 | 确认自动探测选择了正确网络；否则设置 `nodeTopologyConfig` 或 `forceInterface`。 |

基础 Chart 本身不会启用 UCM。只有 `modelSpec.unifiedcacheConfig.config` 非空且 `enabled` 不为 `false` 时，UCM 配置才生效。有效配置还需要 `storage.unifiedcacheStorage`，以及 `config.ucm_connectors` 中至少一个 connector。

## 容量、端口与安全要求

Chart 默认使用 `hostNetwork: true`、`hostIPC: true`，并为每个 vLLM 容器使用主机端口 `8000`。即使同一集群运行多个 Release，每个引擎 Pod 仍需要一个端口 `8000` 空闲的节点。多节点和 PD 配置因此需要足够的合适节点容纳所有入口 Pod 与 worker Pod。

模型配置还会申请加速器与 RDMA 资源，并使用较宽松的容器安全上下文。请确认节点提供对应资源，集群准入策略（包括已启用的 Pod Security admission）允许渲染出的 Pod 配置。

## 继续部署前

确认以下条件：

- 所需 kthena CRD 已安装；
- 调度器与 `ServiceMonitor` 配置和集群服务一致；
- 目标架构可以拉取并运行引擎镜像；
- 模型与 UCM 存储路径可用；
- 设备、RDMA、节点、端口和安全约束均可满足。

然后进入[安装与部署](deploy.md)。

## 参考

- [kthena 文档](https://kthena.volcano.sh/)
- [Volcano 文档](https://volcano.sh/)
