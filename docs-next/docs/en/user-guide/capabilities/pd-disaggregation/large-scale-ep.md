# PD with Multi-node Parallelism

Scaling a PD deployment involves two separate choices: how many independent
prefill/decode instances to run, and how each instance distributes its model
across devices and nodes. UCM provides prefix reuse within the selected engine
integration. It does not choose the model's expert partition or coordinate
collective communication.

Start with a functioning [transport-based PD deployment](distributed.md).
This guide explains how the current Chart represents a larger topology and
which evidence to collect before treating it as a performance improvement.

## Describe the topology before changing arguments

Three replica settings have different meanings:

| Setting | Meaning |
| --- | --- |
| `modelSpec.replicas` | Number of independent serving groups |
| `roles[].replicas` | Number of logical instances of that role within each group |
| `roles[].workerReplicas` | Additional worker Pods belonging to each logical instance |

Within one serving group, the engine Pod count is the sum of
`role replicas × (1 + workerReplicas)` across its roles. Device counts then
follow each Pod's resource requests. These counts do not include the router,
Mooncake master, or other control-plane Pods.

For example, the bundled `values-qwen3-0p6b-2p2-2d2.yaml` profile has two
prefill instances and two decode instances, each with one worker. It therefore
requests eight engine Pods per serving group. It is not a four-Pod profile.
Default hostname anti-affinity also affects the number of schedulable nodes.
Inspect the rendered placement rules instead of inferring node count from the
profile filename.

## Assign DP, TP, and EP to the engine

Tensor parallelism, data parallelism, and expert parallelism are engine
execution settings. Put supported model/parallel arguments in each role's
`vllmArgs`, and verify that the engine version accepts the intended combination.
When using multi-node DP, align global and local DP sizes with the number of
Pods and devices actually allocated to the role instance.

The Chart owns HTTP binding, DP address/rank coordination, served model name,
and KV transfer arguments. Do not duplicate those flags or paste a separate
multi-process launch script into the profile. Use the existing entrypoint to
make the rendered configuration correspond to the process that starts.

The repository includes Ascend multi-node profiles for DeepSeek-V3.1 and
Qwen3-235B, but those files define single-role serving layouts. They are useful
for checking model argument structure; they are not ready-made EP PD recipes.
A model-specific EP deployment still needs a validated engine configuration,
collective-network setup, and sufficient memory on the target hardware.

Changing P and D to different TP or EP layouts also changes the transfer
compatibility question. Validate the selected transport's supported layout
conversion. For shared-store PD, the ordinary UCM key includes TP size and
rank, so a new layout cannot be assumed to reuse an old layout's blocks.

## Select the HTTP serving mode

The Chart's `dataParallelMode` controls how multi-node role instances expose
HTTP endpoints:

- `standard`: one entry HTTP endpoint with headless workers. `ModelServer`
  selects entry Pods.
- `hybrid`: entry and worker nodes of multi-node roles expose HTTP, and the
  router selects those endpoints. This requires PD, an enabled router, and at
  least one role with workers.

The Chart injects the corresponding vLLM LB arguments. Do not independently
set `--data-parallel-hybrid-lb`, `--headless`, or managed rank flags. Check the
final command and `ModelServer.workloadSelector` together; a healthy worker
outside the selector does not receive router traffic.

## Reserve the transfer and cache resources

Each logical P/D instance receives a distinct engine identity. Its entry and
workers resolve that same identity from the serving-group and role labels.
Mooncake `instanceStride` must cover the configured DP, TP, PP, and context
parallel port span. The Chart checks the conservative bound and rejects port
ranges beyond 65535.

Treat collective networking, KV transfer, and external storage as separate
consumers of host and network resources. For UCM, budget host buffers per
process and provision storage reachable by the prefill instances that should
share prefixes. Adding decode replicas does not add UCM readers in the current
Chart's transport-only decode path.

Use [Kubernetes deployment](../../frameworks/kubernetes/deploy.md) to render
site values and inspect the resulting `ModelServing`, `ModelServer`, and
`ModelRoute`. A render confirms the configuration contract; it does not test
collectives, transfer throughput, or expert placement on hardware.

## Validate one change at a time

1. Establish a cold-request baseline on one P and one D with correct output.
2. Add the required workers and engine parallelism without changing UCM's
   backend or workload. Confirm rank initialization and request transfer.
3. Validate prefill-side external reuse after restarting engine processes while
   retaining storage. Check UCM hits and successful loads separately from HBM
   hits.
4. Increase role replicas or serving groups. Confirm routing distribution,
   identity uniqueness, and storage access for every new prefill instance.
5. Apply EP-specific tuning only after the previous layout works, preserving
   enough results to attribute any difference to that change.

Choose an input/output length distribution and arrival schedule representative
of the intended workload. Report completed requests, correctness failures,
client TTFT, TPOT, throughput, and resource consumption. A mean latency gain
with more failures or fewer output tokens is not an equivalent comparison.

## Interpret the result

A high prefill UCM hit rate can coexist with poor end-to-end TTFT when requests
queue at the router, storage reads are slow, or transfer waits for decode.
Likewise, increasing expert-parallel capacity can improve compute utilization
while increasing communication cost. Use per-stage timing and endpoint metrics
to identify which stage changed; do not attribute every speedup to UCM.

The [four-node GLM evaluation design](../../../benchmark/glm-5.1-a3-4node-pd.md)
shows how to record an experiment with a fixed hardware budget. It supplies a
measurement protocol, not a measured EP result or a universal model recipe.
