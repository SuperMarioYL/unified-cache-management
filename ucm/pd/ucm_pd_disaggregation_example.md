# P/D 分离部署与压测指南

Prefill（P）计算提示词的 KV 缓存，Decode（D）用它继续生成 token。两种部署都由 proxy 先调用 P、再调用 D，区别在于 **D 从哪里取得 KV**。

| 交接方式 | KV 怎么到 D | proxy 做什么 |
| --- | --- | --- |
| UCM 共享存储 | P 将提示词的 KV 块写入共享缓存。D 按提示词前缀查找并加载可见的块，缺失的部分可能需要重算。 | 将原始请求发给 D，并设置 `do_remote_prefill=true`。 |
| Mooncake 直传（pull） | P 暂时保留待传的 KV 块。D 根据 P 返回的远端引擎和块信息，通过 Mooncake 从 P 拉取所需的 KV。 | 把 P 返回的 `kv_transfer_params` 转交给 D。 |

## 用 Prometheus 和 Grafana 对比 UCM 与 Mooncake

两组使用本仓库的同一版 proxy 和相同的流式请求。先看 proxy 的 TTFT，再看从发出 D 请求到收到首个有效 token 的时间。这两个指标只使用 proxy 的单调时钟，原版 vLLM 和 vLLM Ascend 即可使用。后一个区间包含 D 排队、KV 加载、首次执行以及响应返回。

**UCM 的 KV 交接耗时**由 UCM connector 采集。UCM 模式下，proxy 在单输入、单输出请求的 P 参数里自动设置 `ucm_pd_relay=true`。P 在正常结束 prefill 的 connector 回调入口取一次 Unix 纳秒时间，并通过标准 `kv_transfer_params` 返回。proxy 将这次 P 响应的时间戳转交 D。D 收齐 worker 回报后，取实际加载 KV 的最后一个 worker 声明就绪的时刻，每次成功交接只计一个样本。分层加载取最后一个所需层。跨机测量前后应检查 P 和所有 D worker 的时钟偏差。

如使用自有 proxy，在发给 UCM P 的 `kv_transfer_params` 中同时设置 `do_remote_decode=true` 和 `ucm_pd_relay=true`。发给 D 时设置 `do_remote_prefill=true`，并从本次 P 响应复制 `pd_prefill_done_unix_ns`。重算时使用新 P 响应，批量输入或多输出时省略时间戳。

1. UCM 组在 P 和 D 安装同一版 UCM，开启 `enable_metrics` 和 `vllm_connector` 导出，保留 vLLM stats。自定义白名单应包含 `pd_*`、Store task 耗时和完成字节指标。[指标配置示例](../../examples/metrics/metrics_configs.yaml)
2. Mooncake 组使用 `MooncakeConnectorV1` pull 和 Classic Transfer Engine 0.3.14，在 D worker 上开启下节的原生指标出口。两组分别启动 proxy，UCM 组加 `--ucm-pd` 或 `--ucm-config`，Mooncake 组使用默认模式。独立安装 proxy 时需安装 `prometheus-client`。
3. 将 Prometheus 抓取示例中的主机名改成实际地址。对两组部署分别抓取 proxy、P 和 D 的 vLLM API，角色分别标记为 `proxy`、`p`、`d`。同一份引擎 registry 只配置一个抓取目标，以免重复累计 token 和等待人数。Mooncake 另抓每个 D worker 的独立 TE 端点，标记 `pd_role=d-te`。按 UCM D 生效的模式填写 `pd_backend=ucm-sync` 或 `ucm-async`。在 Grafana 导入大盘，选择 Prometheus 数据源与对应测试 job。[Prometheus 抓取配置](../../examples/metrics/prometheus_pd.yaml) · [Grafana 大盘](../../examples/metrics/grafana_pd.json)

大盘保留 28 个独立指标面板。点击任一面板标题旁的链接图标，打开该指标的说明页，可以看到原面板名、采集端、统计口径，以及每个查询使用的 Prometheus 序列。在说明页点击标题旁的「查看原图」回到该指标。即使当前没有流量，也可以打开说明。

对比两种后端时，先选相同负载的 job 和时段，查看逻辑请求完成速率、流式 TTFT P95，再查看 P HTTP、P 回包到发 D、发 D 到首 token。分段 P95 不可相加。同一后端勾选多个 job 时会合并样本。趋势图的每个点取滚动窗口；统计卡和横条取值的时间范围见各自的指标说明。

标明「所选时段」的 UCM 交接计数用 Prometheus 的 `increase()`，外推到时间窗边界时可能出现小数。短压测要得到精确增量，可以在确认进程未重启后，用压测前后两次抓取的原始计数求差。首次事件才创建的序列，窗口里只有一次抓取时无法计算增量。空白表示当前查询没有可用观测。

| 看什么 | 如何读数 |
| --- | --- |
| 两组的首 token | 用 `n=1, stream=true` 发相同负载，比较 TTFT、P HTTP、P 回包到发 D、发 D 到首 token。 |
| 请求负载和结局 | proxy 的 `pd_proxy_requests_total` 按逻辑请求计数。观察完成 QPS 和失败占比，同时看有多少请求在重试或重算后成功。每个请求只记一个结局，重算优先于重试。 |
| P、D 的 prompt token 来源 | P 和 D 各有一张图，每根堆叠横条对应一个后端。横条显示所选结束时刻的滚动窗口里，`vllm:prompt_tokens_by_source_total` 的本地计算、本地缓存命中、外部 KV 三类占比。没有 prompt token 的窗口留空。 |
| P、D 调度排队 | 用当前等待请求数发现积压，用从入队到首次调度的 P95 看已完成观测的等待时间。D 的首次调度等待可能包含 KV 等待，因此与交接耗时有重叠。P95 根据引擎提供的桶插值，样本很少时优先查看旁边的均值。 |
| UCM 的请求交接 | 三张卡分别显示 `ucm:pd_kv_handoff_seconds` 的 P50、P95、P99；下方独立显示有效样本数和未计时原因。全 HBM 命中计入 `no_external_load`。缺失 P 时间戳、时钟顺序异常、失败及取消分别计数。UniProc 部署中 scheduler 与 worker 共用缓冲区，查询应汇总全部 `worker_rank`。 |
| UCM 慢在哪一段 | P 的 Store 写入与 D 的 Store 读取按成功 task 计时，完成字节按实际 KV 数据列累计。D 可见性等待从首次查询到外部 KV 命中满足调度条件。写入、等待和读取可以重叠。 |
| UCM Store 的工作量 | P 的 dump 和 D 的 load 分开画。成功 task/s 来自各自时延直方图的 `_count`，平均 payload 来自同窗成功字节增量除以 task 数。对标准 Store 布局，这一比值表示每个成功任务平均实际搬运的字节数。一个 Store task 可以覆盖多个请求或层。 |
| Mooncake D 实际读了多少、花了多久 | TE 面板展示成功 read task 的耗时、完成字节速率，以及终态任务、失败和超时。一次 KV 拉取可以拆为多个并行 task。请求级比较使用两组共用的 proxy 面板。 |

使用多个 connector 时，由负责实际传输的 connector 返回交接参数。vLLM 0.18 等版本的 `MultiConnector` 只接受一个子 connector 返回非空参数。在 Mooncake 直传、UCM 仅提供前缀缓存的组合部署中，proxy 应使用 Mooncake 模式，由 Mooncake 返回传输参数。

验收时先发一条覆盖多个完整 KV 块的冷请求，核对 UCM D 的成功读取字节和交接 `_count` 都增加。再重复相同提示，检查实际搬运量和 `no_external_load`。最后保持模型、并行配置、输入长度和到达率一致，对比两组的首 token 指标。

例如，在有效块大小为 128 的 Qwen3-0.6B 测试中，重复同一条 129-token 输入后，UCM 和 Mooncake 的 D 都命中了 128 个本地缓存 token。UCM 在本地计算最后 1 个 token，读取 0 B。Mooncake 从外部取最后 1 个 token，实际读了一个 14 MiB 的 KV 块。将 D 的 token 来源和完成字节数放在一起看，可以分辨延迟变化是由搬运量变化还是搬运变慢引起的。

### 区分 D 等块和多 rank 慢尾

`ucm:pd_visibility_waiting_requests` 是 D scheduler 当前因共享 KV 命中不足而被推迟的请求数。请求第一次被推迟时进入，后续反复 lookup 仍只占一个位置。一旦可以调度、超时回退或终止，就从等待数中移除。启用这些指标时，vLLM API 会为 scheduler 预先注册零值序列，首次等待也能与先前的抓取比较。Grafana 抓取的是一个时刻的值，两次抓取之间结束的短等待可以从耗时直方图的 `_count` 查看。

`ucm:pd_visibility_deferred_<outcome>_seconds` 按连续等待区间采样，从实际推迟计到退出等待，分为 `external_hit`（外部命中足够）、`no_external_load`（可以在本地继续）、`timeout`（回退到本地 prefill）、`cancelled` 和 `other`（其他退场）。同一请求离开后再次进入等待，会产生新的区间。已有的 `ucm:pd_visibility_wait_seconds` 仍从首次 lookup 计到满足调度条件的外部命中，包含首次就命中的请求。观察当前积压时看 gauge，分析已退出的请求时按结局比较等待时间。

`ucm:pd_worker_ready_spread_seconds` 每次有有效交接样本且至少两个 rank 实际加载了 KV 时记录一次，取最后与最先就绪的两个时刻之差。跳过加载的 rank 不参与计算。TP1 或仅一个 rank 实际加载时该直方图样本数为零，延迟面板留空。跨机部署需要校准各 D worker 的时钟。面板同时给出均值和按桶估算的 P95。若窗内只有一次有效交接，均值就是该次实际就绪时间差。若要进一步区分 Cache|Posix 的任务排队、后端读写及设备拷贝，可打开 [Store 大盘](../../examples/metrics/grafana_store.json)。

### 开启 Mooncake 0.3.14 的原生指标

P、D 使用同一版本的 Ascend NPU wheel `mooncake-transfer-engine-npu==0.3.14`。在运行引擎的 Python 环境中用 `python -m pip show mooncake-transfer-engine-npu` 核对版本。以下示例的 D 只有一个 TE 进程。在启动 D 之前设置环境变量，然后照常启动使用 `MooncakeConnectorV1` 的 D 服务。[官方 NPU wheel](https://pypi.org/project/mooncake-transfer-engine-npu/0.3.14/#files)

```bash
export MC_TE_METRIC=1
export MC_TE_METRIC_HTTP_PORT=9201
```

D 是 pull 的发起端。TE 通过自己的 HTTP 端口导出指标。例如将 `mooncake-d:9201` 设为 Prometheus target，附上 `pd_backend=mooncake-pull` 和 `pd_role=d-te`。在 Prometheus 所在机器执行以下命令，发一条冷请求前后分别读取，确认成功读样本数和字节数均增加。

```bash
curl -fsS http://mooncake-d:9201/metrics | grep -E '^mooncake_te_read_(bytes_total|latency_us_count)'
```

同一网络命名空间内，每个 D worker 的 TE 进程需要不同的监听端口。由启动器分别传入端口，例如 `9201`、`9202`，并将各地址逐一加入抓取列表。独立 Pod IP 可以使用相同端口。共用端口时可能只有一个进程成功监听。对比速率前，核对大盘中的已配置、可达、实际暴露 TE 指标的目标数，三者都应等于本轮参与拉取的 D worker 数。漏采 worker 会使汇总吞吐偏低。一次基准轮次使用专用 D 实例，避免其他业务读操作进入计数。

`mooncake_te_read_latency_us` 原始单位为微秒，从 task 提交计到 TE 观察到完成，可能包含轮询等待。`mooncake_te_read_bytes_total` 只累计成功字节。`mooncake_te_read_requests_total` 包含成功、失败、取消及超时，`failures_total` 包含取消，超时由 `timeouts_total` 单列。大盘先合并各 worker 的桶，再计算分位数并将微秒换算为毫秒。TE 0.3.13 的部署可继续使用共用 proxy 面板。[Mooncake 0.3.14 指标定义](https://github.com/kvcache-ai/Mooncake/blob/v0.3.14/docs/source/design/transfer-engine/index.md#metrics)

例如，Qwen3-0.6B 在两张 910B3 上以 P/D 各 TP1 运行时，一条 129-token 冷请求使 D 的 `mooncake_te_read_latency_us_count` 增加 112、`mooncake_te_read_latency_us_sum` 增加 3,453,638 µs、`mooncake_te_read_bytes_total` 增加 28 MiB。由此得到每个成功 task 平均 30.84 ms、256 KiB。一个请求产生的多个 task 可以并行。短轮次可以直接用前后两次原始计数求均值，长时间走势用大盘的速率和分位数。

## 启动 proxy

先确认引擎就绪。下面以一个 P 实例和一个 D 实例为例，在 proxy 主机执行命令，替换为实际的服务地址。

```bash
curl -fsS http://192.0.2.10:8100/health
curl -fsS http://192.0.2.20:8200/health

python ucm/pd/ucm_pd_disaggregation_example.py \
  --host 0.0.0.0 --port 9000 --workers 2 \
  --prefiller-hosts 192.0.2.10 --prefiller-ports 8100 \
  --decoder-hosts 192.0.2.20 --decoder-ports 8200 \
  --ucm-pd
```

Mooncake 直传使用同一命令，去掉 `--ucm-pd`。已有 UCM 部署也可以沿用 `--ucm-config /etc/ucm/pd.yaml` 启用中转。D 的引擎配置决定同步或异步加载。proxy 可以在无共享目录的机器上运行，Prometheus 从各服务的 HTTP 端点抓取指标。

迁移旧启动命令时，删除 `--prefiller-logs`、`--decoder-logs` 及在线 `--timing-*` 参数。`--proxy-log` 仍可用于保存常规服务日志。此前基于 `pd_request_timing` 的逐请求分析只适用于历史归档。

用一条单输出流式请求检查链路，然后查看 proxy 的计数。示例中将 `served-model` 换成实际服务的模型名。

```bash
curl -N http://127.0.0.1:9000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"served-model","messages":[{"role":"user","content":"你好"}],"n":1,"stream":true,"max_tokens":8}'
curl -fsS http://127.0.0.1:9000/metrics | grep '^pd_proxy_ttft_seconds_count'
```

运行 AIPerf 时，使用独占测试入口并保存客户端的 profiling records。在轮次开始和结束各抓取一次 proxy、P 和 D 的 `/metrics`，比较同一端点的桶和计数增量。原版 Mooncake 的请求级 KV 交接在指标报表中显示为 NA，两套部署都能比较从发出 D 请求到首个有效 token 的延迟。

## 离线估算 Mooncake 交接窗口

需要分析原版 `MooncakeConnectorV1` 的某轮传输时，在 P 保留 vLLM INFO 日志，在 D 保留 scheduler DEBUG 日志，并校准两台主机的时钟。P 的 `Delaying free of … blocks` 表示保留待传块，D 的 `Finished recving KV transfer` 表示 scheduler 收到完成通知，两者之间可能包含排队和调度时间。日志时间使用 UTC JSON 或带小数的 Unix 秒。测试结束后在能读取这两份文件的机器运行：

```bash
python ucm/pd/ucm_pd_disaggregation_example.py compare-handoff \
  --mooncake-p mooncake-p.log --mooncake-d mooncake-d.log > handoff.csv
```

CSV 包含匹配状态，仅 `observed` 行有耗时。使用多份引擎日志时，每个输入文件传一次。分析含旧 UCM 事件的历史日志时，可追加 `--ucm-p`、`--ucm-d`，同步加载加 `--ucm-sync`。在线 proxy 和日常压测均独立于这个命令。
