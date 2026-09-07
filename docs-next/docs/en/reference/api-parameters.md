# Integration API

UCM plugs into an inference engine and its KV-cache lifecycle. The engine owns
HTTP routing, authentication, request/response schemas, and model generation.
UCM does not add a separate OpenAI-compatible HTTP service or require additional
fields in completion requests.

## vLLM and vLLM-Ascend

Pass `--kv-transfer-config` to the engine with these fields:

| Field | Value or meaning |
| --- | --- |
| `kv_connector` | `UCMConnector` |
| `kv_connector_module_path` | `ucm.integration.vllm.ucm_connector` |
| `kv_role` | `kv_both` for a single serving instance that saves and loads |
| `kv_connector_extra_config.UCM_CONFIG_FILE` | Absolute path to the UCM YAML file, readable by the engine |

Set `ENABLE_UCM_PATCH=1` before engine startup to enable the UCM runtime patch
hook. This setting does not choose a CUDA/Ascend backend or install dependencies.
The [Installation selector](../user-guide/installation.md) supplies the matching
backend package. `PLATFORM` is a source-build choice, not a replacement for a
backend extra when installing a released Wheel.

The YAML root owns connector-wide behavior such as `use_layerwise`,
`enable_event_sync`, and metrics. `ucm_connectors[].ucm_connector_config` owns
Store settings such as `storage_backends`, `timeout_ms`, and `store_health`.
See [Configuration parameters](config-parameters.md) and the
[vLLM quickstart](../user-guide/quick_start/quickstart_vllm.md).

## SGLang

Pass `--hicache-storage-backend dynamic` and
`--hicache-storage-backend-extra-config` containing `backend_name`,
`module_path`, `class_name`, `interface_v1`, and `kv_connector_extra_config` as
shown in the [SGLang quickstart](../user-guide/quick_start/quickstart_sglang.md).
The class is `UnifiedCacheStore` in
`ucm.integration.sglang.unifiedcache_store`; it requires the `page_first`
host-memory layout and the zero-copy `batch_get_v1` / `batch_set_v1` API.

When inline `kv_connector_extra_config` is absent, `UNIFIEDCACHE_CONFIG_FILE`
can point to a YAML file containing that configuration. This is a SGLang
adapter input, distinct from the vLLM `UCM_CONFIG_FILE` JSON field. The adapter
sets the pipeline to `Posix` because SGLang already owns the host cache.

## MindIE-LLM

Configure `BackendConfig.kvPoolConfig.backend` as `unifiedcache` and
`configPath` as the absolute UCM JSON configuration path. `asyncWrite` controls
MindIE's asynchronous cache writes. The MindIE-specific build patches its
Python integration modules on import; follow the
[MindIE quickstart](../user-guide/quick_start/quickstart_mindie_llm.md) for build
flags and service configuration. Its `storage_backends` is a JSON list, unlike
the colon-separated paths in the vLLM/SGLang adapters.

## Store extension contract

`UcmKVStoreBaseV1` defines the Store-facing Python contract. The connector
factory resolves a registered Store name and optional module path. Pipeline
Store implements lookup, prefetch, asynchronous `load` / `dump` (and address-based
`load_data` / `dump_data`), then `check` / `wait` for completion. A submitted task
is not evidence that data is already durable or available for reuse.

These are internal integration interfaces, not a general-purpose stable HTTP
cache API. See [Extending Store](../developer-guide/extending-store.md) and
[Architecture](../developer-guide/architecture.md) before adding a backend.
