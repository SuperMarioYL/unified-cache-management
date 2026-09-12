# 基于共享存储的 PD 分离

共享存储部署中，Prefill 和 Decode 通过 UCM 使用同一份外部缓存。请求先经过 Prefill，计算并保存提示词块，再携带原始提示词进入 Decode。Decode 通过查找发现可复用块，UCM 不会向它提供对端实例地址。

仓库中的 `ucm/pd/toy_proxy_server.py` 演示了这一顺序。内置 Kubernetes PD 配置采用另一条路径，详见[传输连接器与 UCM 组合](distributed.md)。

## 开始之前

先按 [vLLM 快速开始](../../quick_start/index.md#vllm)分别验证每个引擎，并从[安装](../../quick_start/index.md)选择引擎镜像或软件包。本页给出共享存储配置、两侧引擎、代理启动和请求验证步骤。

两个实例必须满足以下条件：

- 权重、tokenizer、模型目录名、dtype、块大小相同，KV 布局兼容。起步时使用相同的张量并行设置。
- 能访问同一批存储对象。两块本地磁盘上的路径字符串相同，不代表存储是共享的。
- 存储权限、容量和写后读可见性满足要求，让 Decode 能够看到 Prefill 已完成的写入。
- 使用不同的引擎 HTTP 端口，并显式分配设备。在错误设备上启动一个进程，不能算作第二个独立实例。

## 1p1d

从一个 Prefill 和一个 Decode 实例开始，两者使用相同的 UCM 配置。下面采用 Pipeline Store 的 `Cache|Posix` 路径：

```yaml
ucm_connectors:
  - ucm_connector_name: UcmPipelineStore
    ucm_connector_config:
      store_pipeline: Cache|Posix
      storage_backends: /mnt/ucm-shared
      cache_buffer_capacity_gb: 32
enable_event_sync: true
use_layerwise: false
```

`/mnt/ucm-shared` 是部署环境提供的共享挂载，本示例不会创建它。缓冲容量按进程计算，需要符合主机内存预算。Direct I/O 和其他后端选项应按实际挂载配置，参见[存储流水线](../../../developer-guide/cache-configuration/pipeline.md)。

按快速开始中的方式，在每个引擎的 `kv_connector_extra_config` 中通过 `UCM_CONFIG_FILE` 传入此文件，使用 `UCMConnector` 和 `kv_role: kv_both`。引入代理前，先确认两个引擎的 `/health` 和 `/v1/models` 接口都正常。

### 启动 Prefill 与 Decode

将上面的 YAML 保存为两个引擎环境内的 `/etc/ucm/pd.yaml`。以下单节点 CUDA 示例需要两张可以分别运行该模型的 GPU，共享 `/mnt/ucm-shared`。在两个终端分别启动服务，保持模型、dtype、TP 和块大小一致：

**终端一：Prefill，GPU 0，端口 8100。**

```bash
export MODEL_ID=/models/your-model
export CUDA_VISIBLE_DEVICES=0
export ENABLE_UCM_PATCH=1
vllm serve "$MODEL_ID" \
  --served-model-name ucm-pd \
  --host 127.0.0.1 --port 8100 \
  --tensor-parallel-size 1 --dtype bfloat16 \
  --max-model-len 4096 --block-size 128 --enforce-eager \
  --kv-transfer-config '{
    "kv_connector": "UCMConnector",
    "kv_connector_module_path": "ucm.integration.vllm.ucm_connector",
    "kv_role": "kv_both",
    "kv_connector_extra_config": {"UCM_CONFIG_FILE": "/etc/ucm/pd.yaml"}
  }'
```

**终端二：Decode，GPU 1，端口 8200。**

```bash
export MODEL_ID=/models/your-model
export CUDA_VISIBLE_DEVICES=1
export ENABLE_UCM_PATCH=1
vllm serve "$MODEL_ID" \
  --served-model-name ucm-pd \
  --host 127.0.0.1 --port 8200 \
  --tensor-parallel-size 1 --dtype bfloat16 \
  --max-model-len 4096 --block-size 128 --enforce-eager \
  --kv-transfer-config '{
    "kv_connector": "UCMConnector",
    "kv_connector_module_path": "ucm.integration.vllm.ucm_connector",
    "kv_role": "kv_both",
    "kv_connector_extra_config": {"UCM_CONFIG_FILE": "/etc/ucm/pd.yaml"}
  }'
```

Docker 部署需要两个不同的容器名、正确的设备透传和同一共享缓存挂载。Ascend 在对应引擎环境中使用 `ASCEND_RT_VISIBLE_DEVICES` 选择已透传的 NPU；不同硬件间复用仍需遵循下文的兼容性检查。

### 启动代理

在第三个终端确认两侧引擎就绪：

```bash
curl --fail http://127.0.0.1:8100/health
curl --fail http://127.0.0.1:8200/health
```

再从 UCM 源码根目录启动代理：

```bash
python ucm/pd/toy_proxy_server.py \
  --pd-disaggregation \
  --host 127.0.0.1 --port 8000 \
  --prefiller-hosts 127.0.0.1 --prefiller-ports 8100 \
  --decoder-hosts 127.0.0.1 --decoder-ports 8200
```

代理接受 completions 和 chat-completions 请求。它先发送 `max_tokens: 1` 的非流式 Prefill 请求，等待 HTTP 响应，再将原始请求转发给 Decode。两个阶段使用同一个 request ID。引擎启用认证时，需要为代理配置对应引擎服务的 `OPENAI_API_KEY`。

### 发送请求

代理保持运行，在另一个终端发送覆盖多个完整块的请求：

```bash
python3 - <<'PYREQUEST'
import json
from pathlib import Path
Path('/tmp/ucm-pd-request.json').write_text(json.dumps({
    "model": "ucm-pd",
    "prompt": "Explain how shared storage can reuse a computed prefix. " * 128,
    "max_tokens": 32,
    "temperature": 0,
}))
PYREQUEST
curl --fail http://127.0.0.1:8000/v1/completions \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/ucm-pd-request.json
```

### 验证交接

1. 使用专用缓存目录，提示词长度应覆盖多个配置块，并满足持久化阈值。
2. 通过代理发送一个确定性请求，查看两个引擎的日志，确认 Prefill 执行计算并提交缓存写入。
3. 检查 Decode 的 UCM 命中 token 和成功加载情况。用相同设置，与独立验证过的完整计算请求比较输出。
4. 修改提示词内容后重试，确认无关前缀不会表现为完整外部命中。
5. 保留缓存并重启两个引擎，再重放原始请求，以区分外部复用和引擎内存命中。

代理以 Prefill HTTP 响应作为阶段切换点，不会查询存储持久化回执。UCM dump 有独立的完成生命周期。如果 Decode 启动时块尚不可见，请求仍可能通过重算缺失前缀成功。宣称这条路径通过存储传递了全部提示词 KV 前，需要测量写入完成情况和 Decode 命中。`enable_event_sync` 协调设备事件，不是跨服务器提交协议。

## 不同平台的 1p1d

更换硬件或并行配置前，先建立同平台基线。仅 `dtype` 一致不足以证明缓存兼容。常规 vLLM connector 将张量并行大小和 rank 纳入块命名空间，存储表示也取决于当前缓存布局。

异构实验需要记录两侧引擎与设备运行时版本、权重与 tokenizer 修订、块布局以及模型路径的目录名。在这一确切组合上验证 Decode 侧外部命中和输出正确性。如果检查未通过，在建立兼容路径之前应使用独立缓存；不能仅凭请求响应就认定完成了异构 KV 交接。

## XpYd

示例代理允许为每个角色提供多个主机和端口，主机列表与端口列表长度必须相等。它分别按轮询顺序选择 Prefill 和 Decode，因此每个可能被选中的 Decode 都必须能够读取每个 Prefill 写入的块。

例如，已经按上述方式准备好端口为 8100、8110 的两个 Prefill，以及 8200、8210 的两个 Decode 后，可以用以下命令替换单组代理：

```bash
python3 ucm/pd/toy_proxy_server.py \
  --pd-disaggregation --host 127.0.0.1 --port 8000 \
  --prefiller-hosts 127.0.0.1 127.0.0.1 --prefiller-ports 8100 8110 \
  --decoder-hosts 127.0.0.1 127.0.0.1 --decoder-ports 8200 8210
```

增加副本会提高共享存储负载，也可能降低本地内存缓存复用率。测量接口延迟时，应同时测量存储延迟和容量。示例代理不提供基于健康状态的调度、自动重试或进行中请求的恢复。需要具备传输协议感知能力的 Kubernetes 路由器时，使用[分布式路径](distributed.md)，并验证其不同的交接约束。

## 排查复用缺失

- **没有写入：**检查 Prefill 的持久化阈值、所选 connector 和存储错误。
- **有写入，但 Decode 没有命中：**检查共享挂载是否指向同一存储、写入可见性、模型与缓存兼容性，以及 Decode 实际生效的 UCM 配置。
- **有命中，但加载失败：**比较延迟前，先查看 UCM 加载错误和后端健康状态。
- **响应很快，但没有外部命中：**检查引擎内存命中和重计算量；仅凭延迟无法确定 KV 路径。

通过[指标](../../observability/metrics.md)分别采集各阶段的数据。
