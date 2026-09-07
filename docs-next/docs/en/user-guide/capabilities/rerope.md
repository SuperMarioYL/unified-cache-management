# Rectified Rotary Position Embeddings

ReRoPE changes how attention represents the distance between tokens beyond a
configured window. UCM contains a Triton implementation and engine patches
for experiments with longer-context inference. It changes attention and KV
layout; increasing a model's configured context length alone does not enable it.

## Decide whether this integration fits

The repository has **manual ReRoPE patches for vLLM 0.9.2 and 0.11.0**.
Those patches modify the Qwen2, Qwen3 and Qwen3-MoE model modules, the Triton
attention backend and KV allocation. They are version-specific development
inputs, not a compatibility statement for all Qwen-derived models.

The automatic UCM patch dispatcher does not activate ReRoPE. Neither
`ENABLE_UCM_PATCH=1` nor a `ucm_sparse_config` entry provides this path.
Use a dedicated engine checkout matching a supplied patch, or treat support
for another engine version as an implementation task before attempting a run.
This guide does not establish a vLLM-Ascend integration.

## How the attention calculation changes

For token pairs within `REROPE_WINDOW`, the kernel uses queries and keys with
their normal rotary positions. Beyond the window, it uses an alternate query
rotated at the window position together with an unrotated key. The Triton
kernel selects the score path according to the relative token distance.

`process_qkv` prepares those additional tensors and scales queries using the
logarithm of position relative to `TRAINING_LENGTH`, clipped so the factor is
at least one. The model runner records whether each request's initial prompt
length exceeds the window and passes the batch state through attention
metadata. This is not a generic switch that activates only when decoding later
crosses the window.

The patch stores an extra key representation. For the patched full-attention
layout, page-size accounting changes from two tensor components to three.
Budget for this additional KV memory as well as the longer input. Context
extension does not remove device-memory or model-quality limits.

## Prepare an isolated experiment

Use [Installation](../installation.md) to inspect released artifacts and
[Build from Source](../../developer-guide/build_from_source.md) for the source
environment. Package availability alone does not prove the engine contains
the ReRoPE patch.

Before applying a patch, check that the dedicated vLLM checkout is at the exact
matching version and has no unrelated modifications. Use `git apply --check`
with that version's `vllm-adapt-rerope.patch`, inspect its model and cache
changes, then build the patched engine. Do not apply both the standalone
ReRoPE patch and a combined patch containing the same changes. Keep the
automatic UCM patch path disabled in this manually patched environment to
avoid overlapping replacements.

Set the runtime variables before importing vLLM. Their meanings are:

| Setting | Meaning |
| --- | --- |
| `VLLM_USE_REROPE=true` | Enable the ReRoPE branch added by the engine patch |
| `REROPE_WINDOW` | Relative-distance boundary selecting the two score paths |
| `TRAINING_LENGTH` | Reference length for query scaling; use the model's training configuration |
| `VLLM_ATTENTION_BACKEND` | `TRITON_ATTN_VLLM_V1` for [vLLM 0.9.2](https://github.com/vllm-project/vllm/blob/v0.9.2/vllm/platforms/interface.py); `TRITON_ATTN` for [0.11.0](https://github.com/vllm-project/vllm/blob/v0.11.0/vllm/platforms/interface.py) |
| `ENABLE_UCM_PATCH=0` | Keep automatic UCM replacements out of this manual patch path |

The patches default the window and training length to 32,768. Those values are
not inferred from the model, so choose them explicitly from the experiment's
model and positional settings. Configure the engine's accepted context length
and memory budget separately; a `max_position_embeddings` override only
changes the limit and does not establish quality at that length.

Start with eager execution and one request. If adding external prefix-cache
reuse, configure a [Store](prefix-cache/index.md) only after the patched
attention path runs correctly, and use a separate cache namespace for the
ReRoPE model/position configuration.

## Verify the path before measuring it

1. Confirm that the installed engine exposes the patched ReRoPE environment
   fields, uses the selected Triton backend and allocates the extra key
   representation. A successful import of `ucm` does not check these facts.
2. Run prompts shorter than, equal to, and longer than the configured window.
   Inspect the request/batch `use_rerope` state and confirm the long-prompt
   case reaches `unified_attention_rerope`.
3. Compare short-context output against the unmodified attention baseline,
   then evaluate the intended long-context task with fixed sampling settings.
   Record prompt length, window, training length, latency and peak KV memory.
4. Validate batching and cache reload separately before enabling them in the
   experiment. They add request-state and tensor-layout paths beyond a single
   eager forward pass.

This implementation does not provide a universal maximum usable context
length or a measured speedup for your model. Establish both from the target
workload instead of treating engine acceptance of a long prompt as validation.

## Implementation references

- [vLLM 0.11.0 ReRoPE engine patch](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/integration/vllm/patch/0.11.0/vllm-adapt-rerope.patch) and [vLLM 0.9.2 patch](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/integration/vllm/patch/0.9.2/vllm-adapt-rerope.patch)
- [Query/key preparation](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/sparse/rerope/attn_forward_utils.py) and [Triton attention kernel](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/sparse/rerope/triton_unified_attention_rerope.py)
