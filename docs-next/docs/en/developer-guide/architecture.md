# Architecture

UCM connects an inference engine's KV-cache lifecycle to external storage.
The engine retains ownership of request scheduling, attention execution, and
device KV-cache allocation. UCM locates reusable blocks, loads them into the
engine's buffers, and persists completed blocks for later requests.

## Follow one request

1. The engine loads its UCM integration: vLLM's `UCMConnector`, SGLang's dynamic
   `UnifiedCacheStore`, or MindIE's patched mempool adapter.
2. The integration reads configuration and derives block IDs, tensor layout,
   device addresses, and process roles from the engine. The engine-specific
   adapter owns this knowledge; storage backends should not infer it from models.
3. The connector asks the Store for matching blocks. In the vLLM path the
   scheduler decides how much computation can be reused and passes transfer
   metadata to workers.
4. Workers submit loads into engine-provided buffers and wait for the required
   transfers before consuming data. Completed KV blocks are submitted for dump;
   completion and error handling remain part of the connector lifecycle.
5. Store metrics and connector metrics expose lookup, transfer, and failure
   behavior. In vLLM they are synchronized to the engine's `/metrics` endpoint.

## Responsibility boundaries

| Layer | Responsibility | Source entry point |
| --- | --- | --- |
| Engine integration | Engine hooks, layout, block identity, and transfer scheduling | `ucm/integration/vllm/ucm_connector.py`, `ucm/integration/sglang/unifiedcache_store.py`, `ucm/integration/mindie/unifiedcache_mempool.py` |
| Store contract and factory | Lookup, prefetch, transfer tasks, completion, and backend construction | `ucm/store/ucmstore_v1.py`, `ucm/store/factory_v1.py` |
| Pipeline composition | Registered stage chains, native library loading, and supported Store health wrappers | `ucm/store/pipeline/connector.py`, `ucm/store/pipeline/cpy/pipeline_store.py.cc` |
| Native Stores | Memory cache, filesystem or remote-store I/O, and backend-specific resource lifetime | `ucm/store/cache/`, `ucm/store/posix/`, and other Store directories |

For `Cache|Posix`, Cache owns host buffering and device/host transfer while
Posix owns host/filesystem I/O. SGLang already owns the host cache, so its
adapter constructs the `Posix` pipeline without another UCM Cache stage.

## Configuration and lifecycle

Connector options and Store options are distinct. For example, vLLM's
`use_layerwise` changes transfer scheduling, while
`ucm_connectors[].ucm_connector_config.timeout_ms` configures Store tasks.
The connector supplies runtime geometry rather than asking a user to manually
calculate device pointers or tensor sizes.

A successful lookup does not establish that a later load completed: data can
be missing, a backend can become unhealthy, or a transfer can fail. The Store
contract exposes task completion and errors so the integration can handle the
engine's lifecycle correctly. See the
[Integration API](../reference/api-parameters.md) and
[Pipeline Store](../user-guide/capabilities/prefix-cache/pipeline.md).

## Read further

- [Capability principles](capability-principles.md) explains the distinction
  between prefix reuse, sparse attention, and PD transfer.
- [Extending Store](extending-store.md) describes the backend extension path.
- [Metrics](../user-guide/observability/metrics.md) explains operational evidence.
- [DeepWiki](https://deepwiki.com/ModelEngine-Group/unified-cache-management)
  provides supplementary code exploration; verify its descriptions against the
  repository revision you are reading.
