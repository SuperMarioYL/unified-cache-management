# Sparse Attention

UCM's sparse integration lets an algorithm change which tokens participate in
model execution. Use it when evaluating attention selection or partial prefill
recomputation. For reuse of an unchanged prompt prefix, start with
[Prefix Cache](../prefix-cache/index.md).

## Choose an implementation

| Task | Implementation | What it changes |
| --- | --- | --- |
| Reduce attention work during long-context decoding | [GSAOnDevice](gsa.md) | Selects KV blocks using query/key hashes while retaining the full device KV cache |
| Reuse document chunks at different positions in a RAG request | [CacheBlend](cacheblend.md) | Loads chunk KV and recomputes selected tokens during prefill |
| Develop another sparse method | `UcmSparseBase` and `UcmSparseFactory` | Scheduler, model-runner, attention, layer and FFN hooks |

The factory registers `ESA`, `GSAOnDevice`, `KVStarMultiStep`, and `Blend`.
The separate `GSA` registration is disabled. A source directory or a registered
name alone does not establish a tested engine/model/platform combination.

## Engine and build prerequisites

The automatic sparse patch path in this revision targets **vLLM 0.11.0** and
the corresponding **vLLM-Ascend 0.11.0** integration. Newer versions handled by
UCM's prefix-cache patches do not receive these sparse hooks.

Use [Installation](../../installation.md) to inspect published artifacts. For
development, follow [Build from Source](../../../developer-guide/build_from_source.md)
in an environment with the intended engine and accelerator toolchain. Sparse
native extensions are optional: set `ENABLE_SPARSE=true` before the build.
The build parser accepts the word `true` case-insensitively; `1` does not
enable `BUILD_UCM_SPARSE` through `setup.py`.

For the automatic runtime path, set both variables **before importing vLLM**:

```bash
export ENABLE_UCM_PATCH=1
export ENABLE_SPARSE=true
```

Then select one algorithm in `kv_connector_extra_config.ucm_sparse_config`.
For example, the GSAOnDevice selection is:

```json
{
  "ucm_sparse_config": {
    "GSAOnDevice": {}
  }
}
```

This is a fragment of the KV transfer configuration. Each algorithm page
describes the connector and additional settings it needs. The factory selects
the first entry, so this mapping is not an algorithm-composition interface.

## Architecture

The patched scheduler and each worker initialize their own sparse agent from
the same configuration. The scheduler asks the algorithm for its allocation
budget and reports request creation and completion. The worker prepares
per-step metadata from scheduler output, input batches and attention metadata,
then invokes algorithm hooks around model execution.

Attention hooks can replace the tensors or block tables used by an attention
call. Layer and FFN hooks also let Blend reduce later computation to selected
tokens. Request-finished hooks release algorithm state when requests leave the
batch. These hooks depend on the version-specific engine patch; adding a
configuration key to an unpatched engine does not create the execution path.

Storage behavior belongs to the selected algorithm and connector. In
particular, GSAOnDevice uses device-resident hashes and KV, whereas Blend uses
the Store to reload document chunks. Sparse selection is not a general promise
of KV offloading or a smaller allocation.

## Verify an integration

1. Record the UCM revision, engine versions, model, accelerator and build
   options. Confirm the startup log includes `UCM patching vllm for sparse`
   and `Creating sparse method with name:` for the intended method.
2. Run the algorithm-specific activation case. A generated response only
   proves that inference ran; short requests, cache misses or low concurrency
   can leave the sparse path inactive.
3. Compare against dense execution with identical inputs, sampling settings
   and concurrency. Measure output quality, prefill/decode latency and device
   memory separately.
4. Exercise request completion, repeated requests and any batching mode you
   intend to use before treating the integration as deployable.

## Implementation references

- [Patch selection and runtime switches](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/integration/vllm/patch/apply_patch.py)
- [Sparse factory](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/sparse/factory.py), [state initialization](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/sparse/state.py) and [hook interface](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/sparse/base.py)
