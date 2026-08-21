# uc-stack values 配置（kthena-only）

> 本 chart 已是 **kthena-only**：只渲染 `ModelServing`[+`ModelServer`/`ModelRoute`]，**无 native（Deployment/StatefulSet/Ray）**。形态由 `servingEngineSpec.modelSpec.roles[]` 形状决定。kthena 原生字段语义见 [kthena-native-pd-multinode.md](kthena-native-pd-multinode.md)；改造计划见 [plan/uc-stack-kthena-only-2026-06-22.md](../plan/uc-stack-kthena-only-2026-06-22.md)。
>
> 前置（集群级，本 chart 不装）：kthena 控制面（controller-manager + kthena-router + CRDs）+ 可选 Volcano（PD/多机 gang）。

---

## 1. 形态 = roles[] 形状

| 形态 | roles[] | 渲染 |
|---|---|---|
| 单机 | 1 个 role（`workerReplicas: 0`） | ModelServing(1 role, 1 pod) + Service 直连 |
| 双机 | 1 个 role（`workerReplicas: 1`） | ModelServing(entry+worker=2 节点) + Service 直连 |
| PD | 2 个 role（prefill/decode）+ `modelSpec.pd` | ModelServing(2 role) + ModelServer(pdGroup+kvConnector) + ModelRoute |

- 多引擎扩缩用 `modelSpec.replicas`（= ServingGroup 数，各自独立 gang/恢复单元）。
- “双机挂掉同时拉起” = `recoveryPolicy: ServingGroupRecreate`（整 ServingGroup 一起重建）。
- 角色名仅支持小写字母、数字、`-`，起止为字母或数字，最长 12（DNS pattern）；PD 用 `pd.prefill`/`pd.decode` 引用 role 名。

## 2. values 总 schema

```yaml
images:
  registry: ""                        # 全局仓库前缀；留空不加
  pullPolicy: "Always"                # 默认镜像拉取策略
  image: "registry.dev.huawei.com/flash_stor/ucm-vllm-cuda:v25.5.0"
  mooncakeMasterImage: ""             # 空值复用 images.image；imagePullPolicy 走 images.pullPolicy

mooncakeMaster:
  enabled: true                       # PD 示例默认自创建 master；普通单机/多机保持 false
  create: true                        # false 时填写 external.rpcAddress 复用外部 master
  # 自创建 master 启动前会先 export LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH:-}"
  args: |                             # mooncake_master 原生启动参数
    --port 50088
    --metrics_port 9003
    --eviction_high_watermark_ratio 0.9
    --eviction_ratio 0.1
    --default_kv_lease_ttl 11000
  service:
    rpcPort: 50088                    # K8s Service 暴露端口；需与 args 保持一致
    metricsPort: 9003
  external:
    rpcAddress: ""                    # 例："mooncake-master.kv-system.svc:50088"
  client:
    config: |                         # 放在具体模型 values；下方为 CUDA/GPU 示例，Ascend/NPU 填 protocol=ascend
      {
        "metadata_server": "P2PHANDSHAKE",
        "protocol": "rdma",
        "device_name": "",
        "global_segment_size": "80GB",
        "local_buffer_size": "4GB",
        "preferred_segment": false,
        "prefer_alloc_in_same_node": true
      }
    env: []                           # 追加到使用 master 的 P/D Pod；MOONCAKE_* 托管变量勿写
  resources:                          # CUDA/GPU 示例；Ascend/NPU 模板改为 huawei.com/Ascend910
    limits:   { nvidia.com/gpu: "1", cpu: "2", memory: 4Gi }
    requests: { nvidia.com/gpu: "1", cpu: "1", memory: 2Gi }

servingEngineSpec:
  enableEngine: true
  containerPort: 8000
  schedulerName: "volcano"            # → ModelServing.spec.schedulerName（gang 依赖）
  hostNetwork: true                   # 跨机 RDMA/HCCL/DP/Mooncake 需要
  hostIPC: true                       # PD 跨进程共享内存
  dnsPolicy: "ClusterFirstWithHostNet"
  configs: { ... }                    # → envFrom configs（可选）
  probes:                             # 仅 entry/leader 容器渲染（worker --headless 无 HTTP→不配探针）；可 modelSpec.probes 覆盖
    healthPath: "/health"             # exec 探 http://127.0.0.1:<containerPort><healthPath>
    startup:   { enabled: true, initialDelaySeconds: 300, periodSeconds: 20, failureThreshold: 180 }
    liveness:  { enabled: true, initialDelaySeconds: 60,  periodSeconds: 60, failureThreshold: 5 }
    readiness: { enabled: true, initialDelaySeconds: 60,  periodSeconds: 20, failureThreshold: 3 }

  modelSpec:
    name: "..."                       # K8s/kthena 资源名片段：小写字母/数字/-，起止字母或数字；<release>-<name> <= 63
    modelPath: "..."                  # vllm serve <此>（模型加载路径/仓库 ID）
    modelName: "..."                  # 对外名：统一注入 --served-model-name + ModelRoute.modelName + ModelServer.model；留空回退 modelPath

    # ModelServing.spec 顶层（可选，省略走默认）
    replicas: 1                       # → spec.replicas（ServingGroup 数）
    recoveryPolicy: ServingGroupRecreate  # ServingGroupRecreate|RoleRecreate(默认)|None
    restartGracePeriodSeconds: 60
    rolloutStrategy: { type: ServingGroupRollingUpdate, rollingUpdateConfiguration: { maxUnavailable: 1, partition: 0 } }
    gangPolicy: { minRoleReplicas: { prefill: 1, decode: 1 } }   # map[role]int，不可变
    networkTopology: { groupPolicy: { mode: hard, highestTierAllowed: 1 } }  # 需 Volcano
    plugins: [ { name: x, type: BuiltIn, config: {}, scope: { roles: [], target: All } } ]

    # 角色列表（唯一形态来源）
    roles:
      - name: engine                  # role 名：小写字母/数字/-，起止字母或数字，最长 12
        replicas: 1                   # → roles[].replicas（xPyD 的 x/y）
        workerReplicas: 0             # 0=单机；>=1=跨机（entry+worker）
        resources:                    # raw k8s ResourceRequirements（资源键自己写；rdma/hugepages 也写这里）
          limits:   { nvidia.com/gpu: "1", cpu: "16", memory: 64Gi }
          requests: { nvidia.com/gpu: "1", cpu: "16", memory: 64Gi }
        vllmArgs: |                   # flags-only；勿写 --kv-transfer-config（运行时 resolver 生成）、--served-model-name（取自 modelName）、6 个托管网络参数
          --tensor-parallel-size 1
          --trust-remote-code
        # 可选 raw pod 字段：runtimeClassName / nodeSelector / tolerations / affinity

    # PD 配对（含 prefill/decode role 时填；省略 = 聚合/单双机）
    pd:
      kvTransfer:
        connector: MooncakeConnectorV1  # vLLM 原始 connector 类名；也支持 MooncakeHybridConnector / NixlConnector
        routerType: mooncake             # → ModelServer.kvConnector.type；仅 mooncake | nixl
        identity:
          engineIdBase: 0                # 每个逻辑 P/D 实例由 resolver 递增生成唯一 engine_id
          kvPortBase: 20001              # Mooncake 必填；Ascend 示例使用 36000
          instanceStride: 100             # Mooncake 必填，且需覆盖 DP×TP×PP×CP 的保守跨度
      mooncake:
        master:
          enabled: true               # 本次 PD 使用顶层 mooncakeMaster；false=仅用选定的 Mooncake connector P2P
      antiAffinity: true              # 默认 hostname podAntiAffinity（同 role 互斥同节点）
      prefill: prefill                # 引用 roles[].name（小写字母/数字/-，最长 12）→ pdGroup.prefillLabels（= producer 侧）
      decode: decode                  # 引用 roles[].name（小写字母/数字/-，最长 12）→ pdGroup.decodeLabels（= consumer 侧）

    # router（渲染 ModelServer + ModelRoute；有 pd 或 router.enabled 才渲染，否则只 Service 直连）
    router:
      enabled: true
      inferenceEngine: vLLM           # → ModelServer.inferenceEngine（vLLM|SGLang）
      # 对外名不在此设：ModelServer.model 与 ModelRoute.modelName 统一取自 modelSpec.modelName（空则 modelPath）
      workloadProtocol: http          # → workloadPort.protocol
      trafficPolicy: { timeout: 120s, retry: { attempts: 2, retryInterval: 100ms } }
      loraAdapters: [ ... ]           # → ModelRoute.loraAdapters（maxItems 10）
      parentRefs: [ { name: gw, kind: Gateway, group: gateway.networking.k8s.io, port: 80 } ]
      rules: [ ... ]                  # → ModelRoute.rules[]（省略=chart 生成 default 规则指向本 ModelServer）
      rateLimit: { unit: minute, inputTokensPerUnit: 100000, outputTokensPerUnit: 50000 }

    # router 直连引擎 8000、KV 走引擎自带连接器；不需要 runtime sidecar（如 kthena webhook 自动注入则由其托管，chart 不再提供该选项）。

    # 复用：env / storage(extraStorage,unifiedcacheStorage) / shmSize / hf_token / unifiedcacheConfig(可选 UCM)
```

> 字段映射、约束（枚举/必填/maxLen 等）详见 [kthena-native-pd-multinode.md](kthena-native-pd-multinode.md)。

## 3. 三类示例（见 models/）

| 文件 | 形态 |
|---|---|
| `models/{cuda,ascend}/values-qwen3-0p6b-1e1.yaml`、`models/cuda/values-deepseek-r1-awq-single.yaml` | 单机（1 role workerReplicas:0）+ UCM |
| `models/{cuda,ascend}/values-qwen3-0p6b-1e2.yaml`、`models/ascend/values-{deepseek-v3p1,qwen3-235b}-multi.yaml`、`models/cuda/values-deepseek-r1-awq-multi.yaml` | 双机（1 role workerReplicas:1，DP×TP，ServingGroupRecreate + 反亲和）+ UCM |
| `models/{cuda,ascend}/values-qwen3-0p6b-{1p1-1d1,2p1-2d1}.yaml` | PD（prefill/decode 两 role）+ Mooncake + UCM |
| `models/{cuda,ascend}/values-qwen3-0p6b-2p2-2d2.yaml` | PD 2P2D，且每个 P/D 实例为 entry+worker 双机 |

> 以上为现有模型示例（均含 UCM 缓存）。**PD 分离**（prefill/decode 两 role + `modelSpec.pd` + Mooncake/NIXL；仅 Mooncake producer 可叠 UCM MultiConnector）是按需写法：给某模型加 `pd.kvTransfer` + `pd.{prefill,decode}` 和两个 role；如需 Mooncake master，再加顶层 `mooncakeMaster` 与 `pd.mooncake.master.enabled`。

部署：`helm install <rel> -n <ns> . -f values.yaml -f models/<chip>/<file>.yaml`

## 4. chart 渲染要点（实现细节）

- **6 个托管网络参数**（`--host/--port/--headless/--data-parallel-address/--data-parallel-rpc-port/--data-parallel-start-rank`）由 entrypoint（`args-entrypoint.sh`）注入，**禁止写进 vllmArgs**（`chart.validateVllmArgs` 校验）。
- **多机身份**：kthena 注入 `ENTRY_ADDRESS/WORKER_INDEX/GROUP_SIZE`；`node-topology-setup.sh` 短路成 `MASTER_IP=getent(ENTRY_ADDRESS)/NODE_RANK=WORKER_INDEX`；`REPLICA_COUNT` 由 pod 注入（=1+workerReplicas）。entry/worker 共用一份 pod spec，运行时按 `WORKER_INDEX` 分流。
- **per-role vllmArgs**：共享脚本 ConfigMap `<rel>-vllm-args` + 每 role 一个 `<rel>-<role>-vllm-args`（仅 `vllm.args`），pod 投影卷合并。
- **探针**（`chart.kthenaProbe`，参数取自 `servingEngineSpec.probes`，可被 `modelSpec.probes` 按模型覆盖）：**仅 entry/leader** 渲染，exec 自探 `127.0.0.1:$POD_PORT<healthPath>`；**worker（--headless 无本地 HTTP）不配探针**（避免跨 pod liveness 反模式）。worker 失联靠进程自退 + `recoveryPolicy: ServingGroupRecreate`+gang 整组重建；探针只暴露不健康。各探针可 `enabled: false` 单独关闭。
- **chip-agnostic**：资源键 raw 写、后端由镜像 + vLLM 自动探测；`chipType` / `storage.chipExtraStorage` **已删除**（渲染上等同纯 `extraStorage`）；昇腾驱动挂载靠运行时或写进 `storage.extraStorage`。
- **PD KV（运行时生成，用户勿写）**：每个 role 的 ConfigMap 提供 `kv-transfer.template.json` 与 `kv-transfer.meta.json`，Pod 启动时的统一 resolver 根据 Downward API 注入的 `group-name` / `role-id` 生成唯一身份，再追加唯一一个 `--kv-transfer-config`：
  - `instanceIndex = groupOrdinal × (prefillReplicas + decodeReplicas) + (prefill ? roleOrdinal : prefillReplicas + roleOrdinal)`；`engine_id = engineIdBase + instanceIndex`；Mooncake `kv_port = kvPortBase + instanceIndex × instanceStride`。
  - 同一逻辑 role replica 的 entry/worker 共用 `role-id`，因此共享 engine 和端口基址；`WORKER_INDEX` 不参与身份计算。
  - UCM 生效时 Mooncake producer → `MultiConnector{ selected connector + UCMConnector(kv_both) }`，decode → 纯 selected connector；NIXL + UCM 直接失败。
  - `MooncakeConnectorV1` 保留 producer/consumer 的 `kv_rank=0/1`；Hybrid 不继承 V1 专用字段。两种 Mooncake 的 P/D 并行布局均从双方 `vllmArgs` 自动解析。
  - MultiConnector 的唯一 `engine_id` 位于根节点，Mooncake 子项只含唯一 `kv_port`，UCM 子项不重复身份字段；纯 Mooncake 的 engine/port 位于根节点，NIXL 根节点只有 engine。
  - resolver 在启动 vLLM 前校验标签、ordinal、端口跨度/越界、sentinel 与 connector registry；Hybrid 不存在时明确失败，不降级 V1。
  - `pd.mooncake.master.enabled=true` 时额外生成 `mooncake.json`，并向 P/D Pod 注入 `MOONCAKE_MASTER`、`MOONCAKE_CONFIG_PATH`、`MOONCAKE_GLOBAL_SEGMENT_SIZE`。
  - UCM 配置由 `unifiedcacheConfig` 渲染 `configmap-ucm`，**仅挂在 prefill(producer) 侧**；NIXL 发现走 env（`VLLM_NIXL_SIDE_CHANNEL_HOST/PORT`+`UCX_NET_DEVICES`，role.env 提供）。
- **UCM 可选**：`unifiedcacheConfig.config` 非空且 `unifiedcacheConfig.enabled` 未设为 `false`（默认 `true`）时启用 UCM 缓存；这是唯一开关，旧别名 `enable` 仍兼容。`pd.ucm` 已删除。

首版 connector / router 能力矩阵：

| `pd.kvTransfer.connector` | `routerType` | identity | UCM |
|---|---|---|---|
| `MooncakeConnectorV1` | `mooncake` | engine + port | 支持 |
| `MooncakeHybridConnector` | `mooncake` | engine + port | 支持，启动时做 registry 门禁 |
| `NixlConnector` | `nixl` | 仅 engine | 不支持组合 |

connector 名精确且区分大小写；未知名称、routerType 不匹配、Mooncake 端口跨度不合法都会直接失败。`MultiConnector` / `UCMConnector` 不可作为用户 connector。旧 `pd.connector`、`pd.mooncakePort`、`pd.ucm` 不做兼容映射，即使为空或 `false` 也会给出迁移错误。

## 5. 待 Phase 0 真集群实证
- kthena-router 是否实现 Mooncake PD proxy（否则 NPU 外挂 `load_balance_proxy_server`）。
- controller 是否自动给 pod 打 `modelserving.volcano.sh/{name,group-name,role}` label（pdGroup/Service 选择依赖；否则 chart 在 `entryTemplate.metadata.labels` 显式打）。
- `ENTRY_ADDRESS/WORKER_INDEX/GROUP_SIZE` 实际注入名（`kubectl exec` 核）。
- `recoveryPolicy` 触发条件（liveness 失败的容器重启是否触发整组重建）。
- metrics：PD（无 Service、走 router）下 ServiceMonitor 抓不到，需 PodMonitor 或 metrics Service。
