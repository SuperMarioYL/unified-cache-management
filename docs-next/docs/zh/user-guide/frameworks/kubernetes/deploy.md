# 使用 Helm 安装与部署

运行命令前先完成 [Kubernetes 前提条件](prerequisites.md)。本流程从 Chart 包中的模型配置开始，渲染资源以供检查，然后安装单模型 Release，并验证所选请求路径。

## 获取并解压 Chart

打开[安装](../../installation.md)，选择 Helm，从完整发布清单中复制准确的 Chart 地址和版本。使用生成命令中的版本拉取 Chart，加上 `--untar` 解压，然后进入 `unified-cache-chart`。同时从安装页选择运行时镜像，将模型配置中的镜像设为该准确的发布地址。

从源码工作区部署时，直接进入 `charts/unified-cache-chart`。Helm 自动加载 Chart 根目录的 `values.yaml`，但该文件没有定义 `modelSpec.roles[]`，不能单独用于部署。

## 创建站点配置

从匹配加速器的最小配置开始：

=== "CUDA"

    ```bash
    cp models/cuda/values-qwen3-0p6b-1e1.yaml values-site.yaml
    ```

=== "Ascend"

    ```bash
    cp models/ascend/values-qwen3-0p6b-1e1.yaml values-site.yaml
    ```

按[配置依赖](prerequisites.md#configuration-dependencies)修改 `values-site.yaml`。至少替换引擎镜像、模型路径及挂载、UCM 存储、加速器/RDMA 资源，以及目标集群中不适用的调度、监控和网络配置。

Helm 会整体替换列表，不会合并列表中的元素。请将复制的模型配置作为完整部署文件编辑，不要叠加局部 `roles[]` 或存储列表。

## 渲染与安装

设置 Release 名称和命名空间，在改变集群前验证站点配置：

```bash
export UCM_RELEASE=qwen3
export UCM_NAMESPACE=ucm

helm lint --strict . -f values-site.yaml
helm template "$UCM_RELEASE" . \
  --namespace "$UCM_NAMESPACE" \
  -f values-site.yaml \
  > /tmp/ucm-rendered.yaml
```

检查 `/tmp/ucm-rendered.yaml`。非 PD 配置应包含 `ModelServing`、Release Service 和已配置的配套资源。PD 配置还应包含 `ModelServer` 和 `ModelRoute`；内置 PD 配置另包含 Mooncake master 的 `Deployment` 和 `Service`。

命名空间和所需 CRD 已存在时，可以让 Kubernetes API 验证资源，而不持久化对象：

```bash
kubectl apply --dry-run=server -f /tmp/ucm-rendered.yaml
```

本地渲染和可选的服务端验证符合目标平台及拓扑后，再安装：

```bash
helm upgrade --install "$UCM_RELEASE" . \
  --namespace "$UCM_NAMESPACE" \
  --create-namespace \
  -f values-site.yaml
```

## 验证 Release

先检查 Release 所拥有或声明的资源：

```bash
kubectl -n "$UCM_NAMESPACE" get modelserving
kubectl -n "$UCM_NAMESPACE" get pod,pvc,service -o wide
```

PD 配置还需要检查路由声明和 Mooncake master：

```bash
kubectl -n "$UCM_NAMESPACE" get modelserver,modelroute
kubectl -n "$UCM_NAMESPACE" get deployment,service
```

### 单节点与多节点访问

非 PD 配置会创建名为 `<release>-<modelSpec.name>` 的 Service。Service 端口为 `80`，vLLM 目标端口为 `8000`：

```bash
export UCM_MODEL_RESOURCE=qwen3-0p6b-1e1
export UCM_MODEL_NAME=Qwen3-0.6B

kubectl -n "$UCM_NAMESPACE" port-forward \
  "svc/${UCM_RELEASE}-${UCM_MODEL_RESOURCE}" 8000:80
```

在另一个终端检查 API 并发送请求：

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/v1/models

curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"${UCM_MODEL_NAME}\",
    \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}],
    \"max_tokens\": 128
  }"
```

`UCM_MODEL_RESOURCE` 对应 `modelSpec.name`，`UCM_MODEL_NAME` 对应 `modelSpec.modelName`。

验证 UCM 存储复用时，使用专用测试 Release，按[重启并重放请求](../../quick_start/quickstart_vllm.md#verify-the-service-and-external-cache)的流程操作。重启引擎 Pod 时保留 PVC，并保持运行时设置一致。在引擎指标中检查外部命中 token 和 Posix I/O；单次聊天请求成功只证明服务路径可用。

### PD 访问

PD 流量必须经过已安装 kthena-router 暴露的网关。Chart 创建 `ModelServer` 和 `ModelRoute` 声明，但不创建或命名网关 Service。请使用当前 kthena 安装文档规定的端点。

PD 部署中仍会保留引擎 Service，供 `ServiceMonitor` 发现入口 Pod。该 Service 同时选择 prefill 和 decode 入口，不是 PD 客户端的请求入口。

### 确认 UCM 运行时配置

启用 UCM 后，检查引擎 Pod 中的运行时配置文件。PD 场景应选择 prefill Pod，因为 decode 角色只使用传输 connector：

```bash
export UCM_ENGINE_POD="replace-with-engine-pod-name"

kubectl -n "$UCM_NAMESPACE" exec "$UCM_ENGINE_POD" -- \
  cat /vllm-workspace/UnifiedCache/config/ucm_config.runtime.yaml
```

各项检查证明的范围不同：

| 检查 | 证据 |
| --- | --- |
| `helm lint` 和 `helm template` | 配置满足 Chart 的本地渲染约束。 |
| `kubectl apply --dry-run=server` | 当前 Kubernetes API 和已安装 CRD 接受渲染对象。 |
| Pod Ready、存储绑定及路由资源就绪 | kthena、调度、设备、镜像和存储在该集群中完成收敛。 |
| 聊天请求成功 | 所选访问路径和模型完成端到端推理。 |

## 升级与卸载

更改 `values-site.yaml` 后，重新运行渲染检查，再执行相同的 `helm upgrade --install` 命令。

卸载 Release：

```bash
helm uninstall "$UCM_RELEASE" --namespace "$UCM_NAMESPACE"
```

Helm 请求删除此 Release 创建的 PVC 和静态 PV 对象。动态供应存储中的 PV 和数据后续如何处理，取决于 PV 或 StorageClass 的回收策略。通过 `persistentVolumeClaim` 引用的已有 PVC 不由 Chart 创建，因此不会随 Release 卸载。卸载前应确认存储和备份策略。

返回 [Kubernetes 概览](../kubernetes.md)，选择其他模型配置或部署形态。
