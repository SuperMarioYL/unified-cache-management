# Transport with UCM on Ascend

The current Ascend PD Chart profiles compose two connectors on prefill:
Mooncake sends the request's KV to decode, while UCM loads and saves reusable
prefix blocks in external storage. Decode runs the transport consumer alone.
This allows prefix reuse on prefill without making decode read the UCM store.

## Request and cache flow

1. The router identifies prefill and decode endpoints using `ModelServer`
   labels and its configured `mooncake` protocol.
2. Prefill's UCM connector looks up reusable prompt blocks and loads available
   KV; the engine computes the remaining prompt tokens.
3. The producer transport makes the request's KV available to the decode
   consumer. The router and engine transport own this handoff.
4. Decode generates output. UCM save activity on prefill supplies cache blocks
   for future requests; it is separate from the current transfer.

A UCM hit is optional for a valid cold request. A functioning UCM store cannot
compensate for an unreachable consumer, mismatched connector protocol, or
incorrect transport identity.

## Choose a profile

Start from `models/ascend/values-qwen3-0p6b-1p1-1d1.yaml` in the unpacked Chart.
It defines one prefill and one decode role, a Mooncake master, and routing
resources. Use the [Kubernetes prerequisites](../../frameworks/kubernetes/prerequisites.md)
and [deployment workflow](../../frameworks/kubernetes/deploy.md) to prepare
site values, render, and install.

The profile is a configuration example. Replace the engine image using
[Installation](../../installation.md), mount the intended model, and set
resources, storage, networking, and scheduler values for the target cluster.
The profile's placeholder StorageClass must be replaced. Its host mounts and
RDMA resource names also require actual cluster support.

## Keep the configuration responsibilities separate

The following fields live under `servingEngineSpec.modelSpec`:

| Field | Responsibility |
| --- | --- |
| `roles[]` | P/D replica counts, workers, device resources, and model arguments |
| `pd.prefill`, `pd.decode` | Identify the role names used by routing |
| `pd.kvTransfer.connector` | Select the engine transport |
| `pd.kvTransfer.routerType` | Select the corresponding router protocol |
| `pd.kvTransfer.identity` | Reserve engine IDs and transport port ranges |
| `unifiedcacheConfig` | Enable and configure UCM on prefill |
| `storage.unifiedcacheStorage` | Supply UCM mounts; mount paths populate `storage_backends` |

For `MooncakeConnectorV1` or `MooncakeHybridConnector`, the matching router type
is `mooncake`. The Chart composes `MultiConnector` on prefill when UCM is
effective, and retains a transport-only consumer on decode. Do not put a
second `--kv-transfer-config` into `roles[].vllmArgs`: the Chart owns that flag.

`NixlConnector` uses router type `nixl`, but this Chart currently rejects its
combination with effective UCM configuration. Selecting an accepted transport
name does not establish that the chosen Ascend image implements it; verify the
image's connector support before changing the bundled Mooncake profile.

Disabling `unifiedcacheConfig.enabled` removes the UCM connector and managed
cache mounts while retaining the PD transport. The image's `ENABLE_UCM_PATCH`
environment variable is independently configured. Record it explicitly in
baseline comparisons.

## Resolve identities at startup

The Chart emits a KV template and metadata for each role. At Pod startup,
`resolve-kv-transfer-config.py` uses the serving-group and role-replica labels
to produce concrete engine IDs and Mooncake port bases. Entry and worker
processes belonging to one logical role instance share that identity.

`engineIdBase`, `kvPortBase`, and `instanceStride` are allocation inputs, not
HTTP port settings. The stride must cover the configured parallel-port span.
The Chart validates this bound and the final port range. Do not replace the
resolver with offsets derived from Pod names or worker indices.

For several releases sharing host networking, assign non-overlapping ranges
where instances can coexist on one host. Inspect the resolved arguments and
Pod placement when diagnosing a bind failure; a successful Helm render does
not prove that runtime endpoints are reachable.

## Validate in three stages

**Serving and transfer.** Check prefill/decode readiness and routing objects,
then send a cold request through the pre-installed kthena-router gateway.
Inspect the selected roles and transport errors. The release's engine Service
selects both roles for monitoring and is not the PD client endpoint.

**External reuse.** Populate the UCM store with a fixed prompt, verify completed
writes, restart serving processes while preserving storage, and replay that
prompt. Inspect prefill's UCM hit tokens and successful load activity separately
from engine-memory hits. Decode need not show UCM hits in this topology.

**Performance.** Replay the same traffic with UCM disabled, UCM cold, and UCM
externally warm. Keep transport, P/D counts, output lengths, and generation
settings fixed. Collect failures, answer correctness, client TTFT, TPOT,
throughput, and store/transport timings. See [Benchmark](../../../benchmark/index.md).

## Diagnose by boundary

| Symptom | First evidence to inspect |
| --- | --- |
| Roles are not Ready | Device allocation, model mounts, runtime compatibility, collective initialization |
| Engines work separately but gateway requests fail | `ModelRoute`, role labels, router protocol, transport address/port reachability |
| Cold PD works but repeated prompts have no UCM hits | Prefill connector composition, persistence threshold, cache contents and key compatibility |
| Hits increase but TTFT does not improve | Successful load latency, prefill compute saved, transfer time, router/engine queueing |

For multi-node role instances or MoE models, continue with
[parallelism and scaling](large-scale-ep.md). Avoid changing the model,
parallel layout, and cache backend in one step; each changes a different part
of the evidence needed to explain a result.
