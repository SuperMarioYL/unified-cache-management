# Kubernetes

`unified-cache-chart` 提供在已有 Kubernetes 集群中运行一个 vLLM-UC 模型所需的配置。一个 Helm Release 管理一个模型；部署其他模型时创建新的 Release。

按以下顺序使用本节：

1. 阅读本页，选择部署配置并理解资源归属。
2. 检查[集群与配置前提](kubernetes/prerequisites.md)。
3. 按[安装与部署](kubernetes/deploy.md)渲染、安装并验证 Release。

## Chart 如何部署模型

推理引擎的部署路径统一使用 **kthena**。Helm 创建 `ModelServing` 声明，由预先安装的 kthena controller 将其中的角色转换成 vLLM 入口 Pod 和 worker Pod。PD 部署还会创建 `ModelServer` 和 `ModelRoute` 声明，使请求通过集群中的 kthena-router 进入服务。

Chart 同时创建原生 Kubernetes 配套资源，包括 Service、ConfigMap、Secret、存储对象和可选的 `ServiceMonitor`。Mooncake PD 配置还会创建 Mooncake master 的 `Deployment` 和 `Service`。

[查看原尺寸架构图](../../../assets/images/kubernetes-helm-architecture.svg)。窄屏下可横向滚动查看，保持图中文字清晰。

<div style="overflow-x: auto; margin: 1.2rem 0;" markdown>

[![Helm 渲染模型声明及配套资源，kthena 创建各个 vLLM 角色的 Pod。](../../../assets/images/kubernetes-helm-architecture.svg){ style="display: block; width: 100%; min-width: 680px; max-width: none; height: auto;" }](../../../assets/images/kubernetes-helm-architecture.svg)

</div>

Chart 不会安装 kthena、Volcano、设备插件、存储驱动或 Prometheus Operator。[前提条件](kubernetes/prerequisites.md)说明了不同部署配置依赖哪些外部服务。

## 选择部署配置

Chart 包的 `models/` 目录包含 14 份部署配置。配置通过 `servingEngineSpec.modelSpec.roles[]` 及 PD 场景中的 `modelSpec.pd` 指定设备资源、模型和部署形态；它不会自动选择匹配的引擎镜像。

| 平台 | 模型 | 内置配置 |
| --- | --- | --- |
| CUDA | Qwen3-0.6B | 单节点 `1e1`、双节点 `1e2`、PD `1p1-1d1`、`2p1-2d1` 和 `2p2-2d2` |
| CUDA | DeepSeek-R1-AWQ | 单节点和多节点 |
| Ascend | Qwen3-0.6B | 单节点 `1e1`、双节点 `1e2`、PD `1p1-1d1`、`2p1-2d1` 和 `2p2-2d2` |
| Ascend | DeepSeek-V3.1 | 多节点 |
| Ascend | Qwen3-235B | 多节点 |

当 `modelSpec.replicas` 保持为 `1` 时，Qwen 配置对应以下引擎布局：

| 形态 | 角色配置 | vLLM Pod 数量 | 请求入口 |
| --- | --- | ---: | --- |
| `1e1` | 一个 engine 角色，`replicas: 1`、`workerReplicas: 0` | 1 个入口 Pod | Release Service |
| `1e2` | 一个 engine 角色，`replicas: 1`、`workerReplicas: 1` | 1 个入口 Pod + 1 个 worker | Release Service |
| `1p1-1d1` | 一个 prefill 实例和一个 decode 实例，无 worker | 2 | kthena-router |
| `2p1-2d1` | 两个 prefill 实例和两个 decode 实例，无 worker | 4 | kthena-router |
| `2p2-2d2` | 两个 prefill 实例和两个 decode 实例，每个实例带一个 worker | 8 | kthena-router |

六份内置 PD 配置均创建 Mooncake master，并将 `MooncakeConnectorV1` 与 UCM 组合使用。Chart 支持配置其他 connector 组合，但没有为这些组合提供内置部署配置。

!!! important "部署配置是示例"

    内置配置需要根据目标集群调整。基础配置与模型配置中包含存储类、NFS 路径、RDMA 资源名、资源申请和调度条件。Helm 成功渲染只能验证配置结构；GPU/NPU、RDMA、存储、Mooncake 和 kthena 是否可用，需要在目标集群中验证。

## 后续步骤

- [准备 Kubernetes 及所需外部服务](kubernetes/prerequisites.md)
- [安装、访问、验证、升级和卸载 Release](kubernetes/deploy.md)

## 参考

- [UCM GitHub Releases](https://github.com/ModelEngine-Group/unified-cache-management/releases)
- 解压后的 Chart 中的 `README.md`、`values.yaml`，以及 `models/` 下的 CUDA/Ascend 配置
