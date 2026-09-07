# GSAOnDevice

GSAOnDevice selects KV blocks for long-context attention using compact hashes
of queries and keys. It retains the full KV cache on the accelerator and adds
key-hash buffers; the selected block table limits attention work during decode.
Use this implementation to evaluate decode latency and answer quality under a
smaller attention budget.

## Check whether the model can use it

The runtime name is **`GSAOnDevice`**, with that exact capitalization. The
separate `GSA` implementation is not registered. The active automatic patch
path is vLLM 0.11.0, including vLLM-Ascend 0.11.0 for NPU execution.
The constructor accepts only `cuda` and `npu` device types.

Configuration selection uses the model name or local model path, lowercased:

| Name/path contains | Configuration selected |
| --- | --- |
| `deepseek` and `r1` | DeepSeek R1 AWQ |
| `deepseek` and `v2` | DeepSeek V2 Lite |
| `qwen3` and `32b`, without `coder` | Qwen3 32B |
| `qwen3`, `30b`, and `coder` | Qwen3 Coder 30B A3B |
| `qwen3` and `4b` | Qwen3 4B |
| `qwq` and `32b` | QwQ 32B |

Other names raise `Unsupported model for gsa_on_device`. A matching substring
does not verify the architecture or weights: check the selected JSON against
the actual model's layers, attention heads and MLA dimensions. Renaming an
unrelated model directory is not a supported-model extension.

## Prepare the execution path

Start from [Installation](../../installation.md) or
[Build from Source](../../../developer-guide/build_from_source.md), and apply
the [sparse build and import settings](index.md#engine-and-build-prerequisites).
CUDA execution imports the compiled Hamming-distance extension; NPU execution
imports `ucm_custom_ops` and uses its Ascend operators. Installing Python
sources alone does not supply missing native operators.

Set `VLLM_HASH_ATTENTION=1` before importing the engine. It enables the patched
KV allocation/registration path that carries `(kv_cache, k_hash)` together.
Select `UCMConnector` from `ucm.integration.vllm.ucm_connector`, with
`kv_role=kv_both`, and add this to its extra configuration:

```json
{
  "ucm_sparse_config": {
    "GSAOnDevice": {}
  }
}
```

Configure the Store independently. For an attention-only experiment the
repository example uses `UcmPipelineStore` with an `Empty` pipeline. That
choice isolates attention selection; it cannot demonstrate external KV
persistence. Use a real Store for a separate prefix-cache reuse experiment.

The bundled configuration is loaded from
`ucm/sparse/gsa_on_device/configs/` in the source tree or installed package.
There is no config-path override consumed from the empty `GSAOnDevice` mapping.
Changing the model routing or configuration requires a controlled source
change and a new validation run.

## Attention budget and activation

`vllm_hash_attention_topk` is the token budget used by the current runtime.
It must be divisible by the engine block size, no larger than `max_model_len`,
and no larger than the selected platform's sequence-length threshold.
The Qwen3 4B JSON, for example, uses a 2,048-token budget and threshold, with
a concurrency threshold of four qualifying requests.

At each step, GSA counts requests whose sequence length reaches the threshold.
Selection is enabled only when that count reaches the concurrency threshold.
Layer skip and rollback settings further control which layers select or
restore block tables. A low-concurrency smoke test may therefore execute
without sparse attention even after successful initialization.

The worker hashes keys into device buffers and derives the selected block
tables from query hashes. Prefix-cache hits require hashes to be rebuilt from
the loaded KV. The allocator still reserves full-context KV blocks; its hash-cache accounting
reduces the total available block count to make room for hashes. A smaller
top-k budget must not be used as an estimate of memory freed for other requests.

### Hash weights

The current `HashEncoder` initializes a random projection using QR
decomposition. Although the configuration class has `fixed` weight fields
and setters, `GSAOnDevice` does not pass those fields into its encoders.
Supplying trained weights in JSON alone does not activate them. A custom-weight
experiment must connect weight loading to encoder initialization, preserve
shape/dtype/device requirements, and verify the loaded weights before use.

## Validate a run

- Confirm the logged model configuration path and the `GSAOnDevice initialized`
  GQA/MLA variant match the intended model.
- Begin with eager execution to inspect the activation state and selected
  block tables. Compare cases below and above both activation thresholds.
- Compare dense and sparse output quality on the same long-context inputs;
  record the token budget, concurrency, decode latency and allocated memory.
- If enabling graph execution, verify capture and replay for GSA-enabled and
  GSA-disabled batches separately. Successful eager execution does not test
  the additional graph path.

## Implementation references

- [Model routing, activation, cache layout and attention hooks](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/sparse/gsa_on_device/gsa_on_device.py)
- [Bundled configurations](https://github.com/SuperMarioYL/unified-cache-management/tree/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/sparse/gsa_on_device/configs) and [hash encoder](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/sparse/gsa_on_device/hash_encoder.py)
