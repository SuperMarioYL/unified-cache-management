# PD Disaggregation

Prefill computes the prompt's KV cache; decode uses that cache to generate the
remaining tokens. Running these stages in separate instances introduces two
independent requirements: route each request through the correct instances and
make the prefill result available to decode. UCM provides external prefix-cache
lookup, load, and save. The deployment determines how KV moves between P and D.

## Choose the KV path

The repository contains two different integration paths. Choose one before
configuring workers or interpreting cache-hit measurements.

| Path | Prefill side | Decode side | Request coordination |
| --- | --- | --- | --- |
| [Shared store](centralized.md) | UCM writes reusable prompt blocks to storage | UCM looks up and loads compatible blocks from the same store | The example proxy sends the prompt to P, then D |
| [Transport with UCM](distributed.md) | A Mooncake producer transfers KV; a composed UCM connector provides external prefix reuse | A Mooncake consumer receives the request's KV | The Kubernetes deployment declares the transport to kthena-router |

A shared-store handoff requires the blocks to be visible before decode's
lookup. A transport handoff requires a working producer/consumer connection.
Enabling UCM on prefill does not turn the second path into the first one.

## What the current Chart builds

The bundled PD profiles use transport with UCM. Their configuration separates
three responsibilities:

- `ModelServing` defines the prefill/decode roles, replicas, workers, resources,
  and recovery units.
- `ModelServer` selects those roles through labels and declares the KV connector
  protocol that the router should use.
- `ModelRoute` maps the public model name to the `ModelServer`.

The Chart creates these declarations; kthena and its router must already be
installed. Client traffic enters the router's gateway. The engine Service used
for monitoring selects both roles and is not the PD request endpoint.

`pd.kvTransfer.connector` selects an engine transport, while `routerType`
selects its matching router protocol. Current Chart validation accepts:

| Engine connector | Router type | UCM composition in this Chart |
| --- | --- | --- |
| `MooncakeConnectorV1` | `mooncake` | Prefill only |
| `MooncakeHybridConnector` | `mooncake` | Prefill only |
| `NixlConnector` | `nixl` | Not supported; effective UCM configuration is rejected |

This is a Chart configuration contract, not a certification of every engine,
model, device, and connector combination. Select published artifacts through
[Installation](../../installation.md) and check the
[support matrix](../../support-matrix/index.md) for the engine path first.

## Prefix reuse and request transfer are separate evidence

For the transport path, a cold prompt can succeed with no UCM hit: prefill
computes its KV and the transport sends it to decode. For a repeated prompt,
UCM can supply reusable blocks to prefill before the same P-to-D transfer.
A successful response therefore proves neither external reuse nor its benefit.

The vLLM connector separately records engine-memory hit tokens and UCM hit
tokens. Inspect those counters together with load/save activity and storage
errors. For a controlled reuse test, restart serving processes while retaining
external storage and use the same model and cache settings. See
[Metrics](../../observability/metrics.md) and the
[benchmark method](../../../benchmark/index.md).

Cached blocks are not interchangeable just because two instances expose the
same public model name. The ordinary vLLM UCM key incorporates model-directory
name, tensor parallelism, dtype, rank, and additional execution settings.
Tokenizer, weights, block layout, and engine compatibility also need to match.
Treat a different P/D layout as a compatibility question for the selected path,
not as an automatic consequence of shared storage.

## Start small, then scale

1. Verify a single engine with the intended model, runtime image, and UCM
   backend. Preserve its answer-correctness and external-reuse evidence.
2. For shared storage, validate [one P and one D](centralized.md#1p1d), including
   actual decode-side external hits and write visibility.
3. For the Chart path, use the smallest platform-specific PD profile and follow
   the [Kubernetes deployment workflow](../../frameworks/kubernetes/deploy.md).
4. Increase P/D replicas only after routing and transfer work. Introduce
   multi-node DP or EP separately, following
   [large-scale parallelism](large-scale-ep.md).

These steps isolate an engine failure, a store failure, a transfer failure,
and a routing failure before they become one cluster-wide symptom.

## Implementation entry points

The boundaries above can be traced in `ucm/pd/toy_proxy_server.py`,
`ucm/integration/vllm/ucm_connector.py`, and the Chart's `templates/kthena/`
resources. The Chart's `_helpers.tpl` owns the accepted transport combinations;
`files/resolve-kv-transfer-config.py` resolves logical instance identity at
Pod startup. UCM does not implement the router's scheduling algorithm in these
files, and this guide does not assume automatic role switching, request
migration, or transparent recovery of in-flight generation.
