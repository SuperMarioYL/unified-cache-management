# 快速一键部署 Unified Cache

Unified Cache 提供了一套 **可模板化、可扩展、支持多平台的一键部署方案**，通过 Helm 即可快速完成从模型配置到推理服务上线的完整流程。

本文档用于指导在 **Kubernetes 环境** 中，通过 **Helm** 快速部署 **Unified Cache Server（vLLM-UC）**。当前 Chart 已切换为 **kthena-only**：所有推理负载统一渲染为 kthena 的 `ModelServing` / `ModelServer` / `ModelRoute` CRD，**不再生成 native 的 Deployment / StatefulSet / Ray**。部署形态完全由 `servingEngineSpec.modelSpec.roles[]` 的形状决定，支持三类：

* **单机**：1 个 role，`workerReplicas: 0`（单 pod / 单节点）
* **多机**：1 个 role，`workerReplicas: ≥1`（entry + worker，引擎内跨节点 TP/PP/DP）
* **PD 分离 / xPyD**：≥2 个 role（prefill + decode）+ `modelSpec.pd`（Mooncake / NIXL KV 传输）

支持 **CUDA / Ascend** 等多种芯片，资源键由用户在 `roles[].resources` 中直接书写（chip-agnostic）。

---

## 1. Helm 打包

在 Unified Cache Helm Chart 根目录执行：

```bash
helm package .
```

成功后将生成类似文件：

```text
Unified-Cache-Server-x.x.x.tgz
```

该 tgz 文件即为 **可分发的一键部署安装包**。

---

## 2. 安装环境准备

在开始部署前，请确保以下环境已准备完成。

### 2.1 基础组件要求

* Kubernetes 集群已部署并可用
* Helm ≥ v3
* **kthena 控制面**（controller-manager + kthena-router + `ModelServing`/`ModelServer`/`ModelRoute` CRDs）——本 Chart **不安装** kthena，需集群侧预先就绪，否则渲染出的 CR 无人 reconcile
* **Volcano 调度器**（多机 / PD gang 调度依赖；根 `values.yaml` 默认 `schedulerName: "volcano"`）
* Ascend Device Plugin（Ascend 场景必需）
* 节点时间同步（建议开启 chrony / ntpd）

```bash
kubectl get nodes
helm version

# 确认 kthena CRD 已注册
kubectl get crd | grep -E 'modelserving|modelserver|modelroute'

# 确认 Volcano 已就绪
kubectl get pod -n volcano-system
```

---

### 2.2 镜像准备

部署所需镜像需提前准备完成，并可被集群正常拉取，包括但不限于：

* `vllm-uc`（Unified Cache vLLM 引擎镜像，对应 `images.image`）

> 建议使用 **私有镜像仓库（Harbor）**，并提前完成 `imagePullSecret` 配置。

---

### 2.3 存储依赖（Huawei CSI）

Unified Cache 依赖 **Huawei CSI** 提供 PVC 存储能力，请确认：

* CSI Controller / Node 插件均为 `Running`
* `CSIDriver` 已注册成功

```bash
kubectl get pod -A | grep csi
```

示意如下：

确认 CSI 相关 Pod 均处于 `Running` 状态即可。

---

### 2.4 模型准备

默认情况下，宿主机模型路径为：

```text
/mnt/model
```

挂载到容器内 `/mnt/model` 目录下。请提前将模型下载并放置到对应目录，例如：

```text
/mnt/model/Qwen3-0.6B
/mnt/model/Qwen3-235B-A22B-W8A8
```

模型加载路径由模型模板里的 `modelSpec.modelPath` 指定（即 `vllm serve <modelPath>`）。如需使用 **自定义模型目录**：

* 修改对应 `models/<chip>/values-xxx.yaml` 中的 `modelSpec.modelPath`
* 或调整 `storage.extraStorage`（根 `values.yaml` 中定义的 NFS / hostPath 挂载）

---

### 2.5 解压 Helm 包

下载 Helm Package 后执行：

```bash
tar xzvf Unified-Cache-Server-0.0.1.tgz
cd Unified-Cache-Server
```

---

## 3. Helm 参数与配置说明

### 3.1 模型模板配置（必选）

Chart 内已内置多套模型模板，按芯片平台与部署形态命名，位于：

```text
models/
├── cuda/
│   ├── values-qwen3-0p6b-1e1.yaml      # 单机
│   ├── values-qwen3-0p6b-1e2.yaml       # 多机
│   ├── values-qwen3-0p6b-1p1-1d1.yaml     # PD 1P1D
│   ├── values-qwen3-0p6b-2p1-2d1.yaml     # PD 2P2D
│   ├── values-qwen3-0p6b-2p2-2d2.yaml # PD 2P2D，每个 P/D 实例两机
│   ├── values-deepseek-r1-awq-single.yaml
│   ├── values-deepseek-r1-awq-multi.yaml
│   └── ...
└── ascend/
    ├── values-qwen3-0p6b-1e1.yaml           # 单机
    ├── values-qwen3-0p6b-1e2.yaml            # 多机
    ├── values-qwen3-0p6b-1p1-1d1.yaml          # PD 1P1D
    ├── values-qwen3-0p6b-2p1-2d1.yaml          # PD 2P2D
    ├── values-qwen3-0p6b-2p2-2d2.yaml # PD 2P2D，每个 P/D 实例两机
    ├── values-deepseek-v3p1-multi.yaml
    ├── values-qwen3-235b-multi.yaml
    └── ...
```

#### 3.1.1 配置分层

当前 Chart 的配置分层如下（优先级从低到高）：

* `servingEngineSpec.configs`：全局环境变量（通过 `<release>-configs` ConfigMap 注入）
* `servingEngineSpec.modelSpec.env`：模型级环境变量（优先级高于 `configs`）
* `nodeTopologyConfig`（顶层）：节点级网络变量（多机网络，见 §3.2）
* `servingEngineSpec.modelSpec.roles[].vllmArgs`：原生 `vllm serve` 启动参数块（**flags-only，每行一个 flag**）

> vLLM 的启动参数 **只通过 `roles[].vllmArgs` 配置**，每行一个原生 CLI flag。不存在 `startupMode` / `vllmConfig` / `config.properties` 等旧机制。

#### 3.1.2 modelSpec 关键字段

| 字段 | 说明 |
|---|---|
| `modelSpec.name` | 实例名，参与资源命名（CR 名 = `<release>-<name>`）；仅支持小写字母、数字、`-`，起止为字母或数字；`<release>-<name>` 总长需 <= 63 |
| `modelSpec.modelPath` | 模型加载路径 / 仓库 ID → `vllm serve <此>`（chart 管理，勿写进 vllmArgs） |
| `modelSpec.modelName` | **对外服务名**：统一注入 `--served-model-name`、`ModelRoute.modelName`、`ModelServer.model`；**留空则回退用 `modelPath`** |
| `modelSpec.roles[]` | 部署形态的唯一来源（见 §3.1.3） |
| `modelSpec.pd` | PD 配对（含 prefill/decode role 时填，见 §4.3） |
| `modelSpec.router` | 可选网关（渲染 ModelServer + ModelRoute；省略则只 Service 直连） |
| `modelSpec.unifiedcacheConfig` | 可选 UCM 缓存配置；`enabled: false`（默认 `true`）= 不使用 UCM |

#### 3.1.3 部署形态 = roles[] 形状

形态完全由 `roles[]` 的形状决定，**没有 `deployMode` / `replicaCount` 开关**：

| 形态 | roles[] 写法 | 渲染结果 |
|---|---|---|
| 单机 | 1 个 role，`workerReplicas: 0` | ModelServing（1 role，1 pod）+ Service 直连 |
| 多机 | 1 个 role，`workerReplicas: ≥1` | ModelServing（entry + N worker，引擎内跨节点并行）+ Service 直连 |
| PD / xPyD | 2 个 role（prefill/decode）+ `modelSpec.pd` | ModelServing（2 role）+ ModelServer（pdGroup + kvConnector）+ ModelRoute |

* 多引擎扩缩用 `modelSpec.replicas`（= ServingGroup 数，各自独立 gang / 恢复单元）。
* PD 中每个 role 的 `replicas` 即 xPyD 的 x / y（如 `prefill.replicas: 2` + `decode.replicas: 2` = 2P2D）。

#### 3.1.4 单机模板示例

以 `models/cuda/values-qwen3-0p6b-1e1.yaml` 为例（1 role `engine`，`workerReplicas: 0`，TP1）：

```yaml
servingEngineSpec:
  enableEngine: true
  containerPort: 8000
  modelSpec:
    name: "qwen3-0p6b-1e1"        # K8s 资源名片段：小写字母/数字/-，起止字母或数字；<release>-<name> <= 63
    modelPath: "/mnt/model/Qwen3-0.6B"
    modelName: "Qwen3-0.6B"            # 对外名 → API 的 "model" 字段
    shmSize: "32Gi"
    roles:
      - name: engine              # role 名：小写字母/数字/-，起止字母或数字，最长 12
        replicas: 1
        workerReplicas: 0               # 0 = 单机单 pod
        resources:
          limits:   { nvidia.com/gpu: "1", cpu: "8", memory: 64Gi }
          requests: { nvidia.com/gpu: "1", cpu: "8", memory: 64Gi }
        vllmArgs: |                     # flags-only，每行一个原生 flag
          --tensor-parallel-size 1
          --data-parallel-size 1
          --pipeline-parallel-size 1
          --max-model-len 20000
          --block-size 128
          --gpu-memory-utilization 0.85
          --distributed-executor-backend mp
          --compilation-config '{"cudagraph_mode":"FULL_AND_PIECEWISE"}'
          --no-enable-prefix-caching
          --trust-remote-code
    env:
      - name: VLLM_SERVER_DEV_MODE
        value: "1"
```

Ascend 单机（`models/ascend/values-qwen3-0p6b-1e1.yaml`）写法相同，仅资源键改为 `huawei.com/Ascend910`：

```yaml
    roles:
      - name: engine              # role 名：小写字母/数字/-，起止字母或数字，最长 12
        replicas: 1
        workerReplicas: 0
        resources:
          limits:   { huawei.com/Ascend910: "4", cpu: "64", memory: 256Gi }
          requests: { huawei.com/Ascend910: "4", cpu: "64", memory: 256Gi }
        vllmArgs: |
          --tensor-parallel-size 4
          --max-model-len 20000
          --trust-remote-code
```

#### 3.1.5 Chart 自动接管的参数（不要写进 vllmArgs）

以下 flag 由 Chart / entrypoint 自动注入，写进 `vllmArgs` 会导致 **渲染失败**（`chart.validateVllmArgs` 校验）：

* `--host`、`--port`、`--headless`
* `--data-parallel-address`、`--data-parallel-rpc-port`、`--data-parallel-start-rank`
* `--served-model-name`（取自 `modelSpec.modelName`，留空回退 `modelPath`）
* `--kv-transfer-config`（PD / UCM 场景由 Pod 启动时的统一 resolver 生成）
* `--config`（外部 YAML 内容无法在 Helm 渲染期校验，因此不允许使用）

此外 **`modelPath` 位置参数也是 chart 管理的**（来自 `modelSpec.modelPath`，无需在 vllmArgs 里写）。`vllmArgs` 里也不要写环境变量，环境变量请放 `modelSpec.env` 或 `servingEngineSpec.configs`；其中 `VLLM_ARGS_FILE` 是 Chart 保留变量，不可覆盖。

常用可写 flag：`--tensor-parallel-size/-tp`、`--data-parallel-size`、`--data-parallel-size-local`、`--pipeline-parallel-size`、`--max-model-len`、`--gpu-memory-utilization`、`--block-size`、`--distributed-executor-backend`、`--enable-expert-parallel`、`--quantization`、`--enforce-eager`、`--no-enable-prefix-caching`、`--trust-remote-code` 等。

如需自定义模型模板，可参考：

👉 [README.md](../README.md)

---

### 3.2 多机环境配置（多机 / PD 场景建议）

在多机部署场景下，需要配置 **nodeTopologyConfig / 网卡探测**，用于：

* 节点间通信
* HCCL / NCCL 初始化
* vLLM 引擎内跨节点并行（TP / PP / DP）

相关配置位置：

```text
values.yaml（顶层 nodeTopologyConfig / autoDetectInterface，与 servingEngineSpec 同级）
```

> 多机 / PD 的副本与调度语义来自 `roles[].replicas`/`workerReplicas` + Volcano gang（`gangPolicy.minRoleReplicas`），**不再依赖 `replicaCount` 或 Deployment/StatefulSet 区分**。同主机端口冲突由唯一的 KV identity/端口基址和 Chart 自动注入的 hostname `podAntiAffinity` 共同防护（PD 或 `workerReplicas>0` 时）。

#### 3.2.1 配置规则

`nodeTopologyConfig` 是一个 **map 结构**：

* key：Kubernetes 节点名（必须与 `kubectl get nodes` 输出一致）
* value：该节点需要注入的环境变量（`ENV_NAME: "value"`）

网卡类变量（`*_IFNAME` 等）默认由顶层 `autoDetectInterface`（默认开，与 `nodeTopologyConfig` 同级）按 `HOST_IP` 自动探测并扇出到 GLOO/HCCL/NCCL/`TP_SOCKET_IFNAME` 等；`nodeTopologyConfig` 为**可选显式覆盖**，下方为显式写法。

> 自动探测与 hostNetwork 强相关：`autoDetectInterface` 需要 `hostNetwork: true` 才能看到宿主机网卡。

#### 3.2.2 NVIDIA（NCCL）示例

```yaml
nodeTopologyConfig:
  gpu-node-1:
    GLOO_SOCKET_IFNAME: "ens10f0"
    NCCL_SOCKET_IFNAME: "ens10f0"
    NCCL_IB_DISABLE: "1"
    VLLM_NETWORK_INTERFACE: "ens10f0"
    VLLM_USE_NETIF: "ens10f0"
    VLLM_DETECT_MULTI_IP: "0"
  gpu-node-2:
    GLOO_SOCKET_IFNAME: "ens10f0"
    NCCL_SOCKET_IFNAME: "ens10f0"
    NCCL_IB_DISABLE: "1"
    VLLM_NETWORK_INTERFACE: "ens10f0"
    VLLM_USE_NETIF: "ens10f0"
    VLLM_DETECT_MULTI_IP: "0"
```

InfiniBand 场景可按需增加：`NCCL_IB_HCA`、`NCCL_IB_GID_INDEX`、`NCCL_NET_GDR_LEVEL`。

#### 3.2.3 Ascend（HCCL）示例

```yaml
nodeTopologyConfig:
  ascend-node-1:
    GLOO_SOCKET_IFNAME: "enp189s0f0"
    HCCL_IF_NAME: "enp189s0f0"
    HCCL_SOCKET_IFNAME: "enp189s0f0"
    VLLM_NETWORK_INTERFACE: "enp189s0f0"
    VLLM_USE_NETIF: "enp189s0f0"
    VLLM_DETECT_MULTI_IP: "0"
  ascend-node-2:
    GLOO_SOCKET_IFNAME: "enp189s0f0"
    HCCL_IF_NAME: "enp189s0f0"
    HCCL_SOCKET_IFNAME: "enp189s0f0"
    VLLM_NETWORK_INTERFACE: "enp189s0f0"
    VLLM_USE_NETIF: "enp189s0f0"
    VLLM_DETECT_MULTI_IP: "0"
```

#### 3.2.4 部署前自检（建议执行）

```bash
# 1) 确认节点名（必须和 nodeTopologyConfig 的 key 一致）
kubectl get nodes

# 2) 渲染检查（确认镜像、亲和性、环境变量、CR 形态）
helm template ucstack . -f values.yaml -f <your-model-values>.yaml | less
```

若 `nodeTopologyConfig` 配置了多个节点，但实际仅拉起部分节点 Pod，属于正常行为；未使用到的节点配置不会导致部署失败。

---

## 4. Helm 服务部署

### 4.1 快速单机部署（CUDA 示例）

以 **Qwen3-0.6B 单机部署** 为例：

```bash
helm install qwen3-0p6b-1e1 \
  --namespace yulei-test \
  --create-namespace \
  . \
  -f values.yaml \
  -f models/cuda/values-qwen3-0p6b-1e1.yaml
```

部署完成后将生成一个 `ModelServing` CR（+ Service），可查看：

```bash
kubectl -n yulei-test get modelserving
kubectl -n yulei-test get pod
kubectl -n yulei-test logs <pod-name>
```

> Ascend 单机同理，换成 `-f models/ascend/values-qwen3-0p6b-1e1.yaml`。

---

### 4.2 快速多机部署（Ascend 示例）

以 **Qwen3-0.6B 多机 Ascend 部署** 为例（1 role `engine`，`workerReplicas: ≥1`，引擎内跨节点 TP/PP/DP）：

```bash
helm install qwen3-0p6b-1e2 \
  --namespace yulei-test \
  --create-namespace \
  . \
  -f values.yaml \
  -f models/ascend/values-qwen3-0p6b-1e2.yaml
```

多机模板的关键点是 1 个 role + `workerReplicas: ≥1` + `recoveryPolicy: ServingGroupRecreate` + `gangPolicy`：

```yaml
    recoveryPolicy: ServingGroupRecreate   # 整组一损俱损、一起重建
    gangPolicy:
      minRoleReplicas:
        engine: 1
    roles:
      - name: engine              # role 名：小写字母/数字/-，起止字母或数字，最长 12
        replicas: 1
        workerReplicas: 1                  # ≥1 = entry + worker 跨节点
        resources:
          limits:   { huawei.com/Ascend910: "1", cpu: "16", memory: 64Gi }
          requests: { huawei.com/Ascend910: "1", cpu: "16", memory: 64Gi }
        vllmArgs: |
          --tensor-parallel-size 1
          --data-parallel-size 2
          --data-parallel-size-local 1
          --trust-remote-code
```

> 多机的 master/worker 接线、`--data-parallel-address`/`--data-parallel-start-rank` 全部由 entrypoint 在运行时按 kthena 注入的 `ENTRY_ADDRESS` / `WORKER_INDEX` 加上 chart 注入的 `REPLICA_COUNT`（=1+workerReplicas）自动计算，**用户无需手写**。

建议在部署后重点关注：

* Pod 是否全部 `Running`
* `nodeTopologyConfig` / 网卡探测是否正确生效
* 多机通信是否正常
* `ModelServing` 是否 Ready
* UCM 运行时配置是否生成到 `/vllm-workspace/UnifiedCache/config/ucm_config.runtime.yaml`

---

### 4.3 PD 分离部署（xPyD，新增形态）

PD 分离把 prefill 与 decode 拆成两个 role，通过 `modelSpec.pd` 配对，使用 Mooncake（或 NIXL）做 KV 传输；渲染结果是 `ModelServing`（2 role）+ `ModelServer` + `ModelRoute`，**没有 Service 直连**，客户端走 kthena-router。

以 **Qwen3-0.6B PD 1P1D（CUDA）** 为例：

```bash
helm install qwen3-0p6b-1p1-1d1 \
  --namespace yulei-test \
  --create-namespace \
  . \
  -f values.yaml \
  -f models/cuda/values-qwen3-0p6b-1p1-1d1.yaml
```

模板核心结构（节选自 `models/cuda/values-qwen3-0p6b-1p1-1d1.yaml`）：

```yaml
mooncakeMaster:
  enabled: true
  create: true
  resources:
    limits:   { nvidia.com/gpu: "1", cpu: "2", memory: 4Gi }
    requests: { nvidia.com/gpu: "1", cpu: "1", memory: 2Gi }
  client:
    config: |
      {
        "metadata_server": "P2PHANDSHAKE",
        "global_segment_size": "80GB",
        "local_buffer_size": "4GB",
        "protocol": "rdma",
        "device_name": ""
      }

servingEngineSpec:
  enableEngine: true
  containerPort: 8000
  modelSpec:
    name: "qwen3-0p6b-1p1-1d1"    # K8s 资源名片段：小写字母/数字/-，起止字母或数字；<release>-<name> <= 63
    modelPath: "/mnt/model/Qwen3-0.6B"
    modelName: "Qwen3-0.6B"
    shmSize: "32Gi"
    recoveryPolicy: ServingGroupRecreate  # 可选：ServingGroupRecreate（整组）| RoleRecreate（单 Role，默认）| None（Pod/Deployment 默认行为）
    restartGracePeriodSeconds: 60
    gangPolicy:
      minRoleReplicas:
        prefill: 1
        decode: 1
    pd:
      kvTransfer:
        connector: MooncakeConnectorV1    # 可选（区分大小写）：MooncakeConnectorV1 | MooncakeHybridConnector | NixlConnector
        routerType: mooncake              # Mooncake 两种 connector 对应 mooncake；NixlConnector 对应 nixl
        identity:
          engineIdBase: 0
          kvPortBase: 20001
          instanceStride: 100
      mooncake:
        master:
          enabled: true                   # 本次 PD 使用顶层 mooncakeMaster
      antiAffinity: true
      prefill: prefill                    # 引用 roles[].name（小写字母/数字/-，最长 12）
      decode: decode                      # 引用 roles[].name（小写字母/数字/-，最长 12）
    router:
      enabled: true
      inferenceEngine: vLLM
      trafficPolicy:
        timeout: 60s
    roles:
      - name: prefill                     # role 名：小写字母/数字/-，起止字母或数字，最长 12
        replicas: 1                       # xPyD 的 x
        workerReplicas: 0
        resources:
          limits:   { nvidia.com/gpu: "1", cpu: "16", memory: 64Gi }
          requests: { nvidia.com/gpu: "1", cpu: "16", memory: 64Gi }
        vllmArgs: |
          --tensor-parallel-size 1
          --max-model-len 20000
          --block-size 128
          --gpu-memory-utilization 0.85
          --trust-remote-code
      - name: decode                      # role 名：小写字母/数字/-，起止字母或数字，最长 12
        replicas: 1                       # xPyD 的 y
        workerReplicas: 0
        resources:
          limits:   { nvidia.com/gpu: "1", cpu: "16", memory: 64Gi }
          requests: { nvidia.com/gpu: "1", cpu: "16", memory: 64Gi }
        vllmArgs: |
          --tensor-parallel-size 1
          --max-model-len 20000
          --block-size 128
          --gpu-memory-utilization 0.85
          --trust-remote-code
          --no-enable-prefix-caching
```

> * Ascend 的 PD 用 `-f models/ascend/values-qwen3-0p6b-1p1-1d1.yaml`，资源键改为 `huawei.com/Ascend910`；Ascend 示例使用 `kvPortBase: 36000`、`instanceStride: 100`，避开 AscendDirectTransport 的 `20000` 起动态端口段。
> * 想要 2P2D，只需把 prefill / decode 两个 role 的 `replicas` 都改成 2（或直接用 `values-qwen3-0p6b-2p1-2d1.yaml`）。
> * 想要“每个 P/D 实例两机”的 2P2D，直接用 `values-qwen3-0p6b-2p2-2d2.yaml`；它保持 `prefill.replicas=2` / `decode.replicas=2`，并把两个 role 的 `workerReplicas` 都设为 1。
> * `--kv-transfer-config` 在 Pod 启动时由统一 resolver 根据 `group-name` / `role-id` 生成，**用户不要手写**。每个逻辑 P/D 实例取得唯一 `engine_id` 和 Mooncake `kv_port`，同实例的 entry/worker 共享身份。
> * 首版精确支持 `MooncakeConnectorV1+mooncake`、`MooncakeHybridConnector+mooncake`、`NixlConnector+nixl`；connector 大小写、routerType 配对错误或未知 connector 均直接失败。`NixlConnector` 不支持与 UCM 组合。
> * `unifiedcacheConfig.enabled` 是唯一 UCM 开关（旧别名 `enable` 仍兼容）：配置生效时 Mooncake producer 自动叠加 `UCMConnector`，decode 仍为纯 Mooncake。`pd.connector`、`pd.mooncakePort`、`pd.ucm` 已删除。
> * 现有 PD 示例通过顶层 `mooncakeMaster.enabled=true, create=true` 自创建 Mooncake master。`MOONCAKE_CONFIG_PATH` 固定由 Helm 指向 `/vllm-workspace/UnifiedCache/mooncake/mooncake.json`，不要在 env 里手写。

部署后查看：

```bash
kubectl -n yulei-test get modelserving,modelserver,modelroute
kubectl -n yulei-test get pod -o wide
```

---

## 5. 部署验证（推荐）

### 5.1 Pod 与 CR / 服务状态

```bash
kubectl -n yulei-test get pod -o wide

# 单机 / 多机（无 router）：有 Service 直连
kubectl -n yulei-test get modelserving,svc

# PD / 启用 router：走 ModelServer + ModelRoute
kubectl -n yulei-test get modelserving,modelserver,modelroute
```

### 5.2 服务健康检查

```bash
# 单机 / 多机：直接转发 Service
kubectl -n yulei-test port-forward svc/<service-name> 8000:8000
curl http://127.0.0.1:8000/health
```

### 5.3 模型 API 调用示例

```bash
# 先把服务转发到本地（PD/启用 router 时转发 kthena-router 网关）
kubectl -n yulei-test port-forward svc/<service-name> 8000:8000

# 查看当前加载的模型
curl http://127.0.0.1:8000/v1/models

# OpenAI-compatible Chat Completions 示例
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen3-0.6B",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "用一句话介绍 Unified Cache。"}
    ],
    "max_tokens": 128,
    "temperature": 0.7
  }'
```

> `"model"` 字段填 `modelSpec.modelName`（本例为 `Qwen3-0.6B`）；若模板里 `modelName` 留空，则填 `modelPath`。这个名字是唯一来源，同时驱动 `ModelRoute.modelName`、`ModelServer.model` 与引擎的 `--served-model-name`，三者保证一致。

### 5.4 配置文件 / CR 检查

```bash
# kthena CR 状态
kubectl -n yulei-test get modelserving,modelserver,modelroute -o wide

# UCM 运行时配置（启用 UCM 时）
kubectl -n yulei-test exec <pod-name> -- \
  cat /vllm-workspace/UnifiedCache/config/ucm_config.runtime.yaml
```

---

## 6. Helm 服务卸载

如需卸载服务：

```bash
helm uninstall qwen3-0p6b-1e1 --namespace yulei-test
```

> ⚠️ 注意：
>
> * 默认会删除 `ModelServing` / `ModelServer` / `ModelRoute` / `Service` 等本 Chart 渲染的资源
> * PVC 是否保留取决于 StorageClass `reclaimPolicy`

---

## 7. 常见问题与建议

* **CR 一直不 Ready**：确认集群侧 kthena 控制面与 Volcano 已就绪（`kubectl get crd | grep modelserving`、`kubectl get pod -n volcano-system`）
* **多机部署失败**：优先检查 `nodeTopologyConfig` 与网络连通性，并确认 `hostNetwork: true`
* **PD KV 不通**：检查 `hostNetwork` / `hostIPC`，并核对 resolver 输出的每实例 `engine_id` / Mooncake `kv_port` 是否唯一且可达；Ascend 避免使用 `20000` 起动态端口段，示例从 `kvPortBase: 36000` 起按 `instanceStride: 100` 分配
* **PVC 挂载异常**：确认 Huawei CSI Node 插件状态
* **磁盘压力 / Eviction**：建议提前配置 kubelet root-dir 与日志轮转
* **大模型性能问题**：检查 TP / DP / PP / shm / 显存利用率配置

---

## 8. 推荐阅读

* [README.md](../README.md)
* [uc-stack-kthena-values.md](uc-stack-kthena-values.md)（kthena values schema 参考）
* vLLM 官方文档
* Ascend MindIE / HCCL 使用指南

---
