# CacheBlend

CacheBlend reuses KV for document chunks even when those chunks appear at a
new position in a RAG prompt. UCM's implementation loads the cached chunks,
corrects their rotary positions and recomputes selected tokens before
continuing the model forward pass. Use it to experiment with repeated document
content whose preceding context changes between requests.

## Integration boundary

This is an experimental CUDA path built from two cooperating components:
`UCMBlendConnector` manages chunk caching and `Blend` selects recomputation
work. Selecting `Blend` with the ordinary `UCMConnector` does not provide the
chunk metadata that the algorithm expects.

The repository includes manual sparse patches for vLLM 0.9.2 and 0.11.0;
the current automatic sparse hook path targets 0.11.0. Its model-forward
patches cover the `llama` and `qwen2` modules. The connector also accesses
`model.model.layers[0].self_attn.rotary_emb.cos_sin_cache`. Check these paths
for the actual model class before testing another architecture. There is no
basis here for claiming every Llama, Qwen or DeepSeek model is supported.

Blend uses CUDA NVTX and a Triton position-correction kernel. This guide does
not establish an Ascend path. Native HBM prefix caching must be disabled:
the connector asserts that `num_computed_tokens` is zero. Chunked prefill is
not supported by its metadata construction; configure the scheduler so that
the tested prompt fits in one prefill step. Begin with eager execution.

## Prepare chunk caching

Follow [Installation](../../installation.md) or
[Build from Source](../../../developer-guide/build_from_source.md), then the
[sparse build and import settings](index.md#engine-and-build-prerequisites).
Keep `VLLM_HASH_ATTENTION` unset for Blend; that switch selects GSA's hash-cache
layout. Configure `UCMBlendConnector` from
`ucm.integration.vllm.blend_connector`, with `kv_role=kv_both`, and a real
[Store](../prefix-cache/index.md) accessible to its workers.

The algorithm configuration sits in `kv_connector_extra_config`. This fragment
illustrates a recomputation rule for a model exposing the named layer:

```json
{
  "ucm_sparse_config": {
    "Blend": {
      "chunk_end_token_id": 0,
      "compute_meta": {
        "model.layers.1.self_attn.attn": {"ratio": 0.2}
      }
    }
  }
}
```

Replace `0` with the delimiter ID chosen for your tokenizer and input builder;
it is not a universal delimiter. The layer name must match an actual attention
layer. `ratio` determines the fraction of cached candidate tokens selected for
recomputation at that layer. The value above is an experimental starting value,
not an accuracy guarantee.

## Construct requests in the order the connector expects

1. Tokenize each reusable document chunk and pad it to the engine's block
   size. End the chunk with `chunk_end_token_id` at a block boundary. The
   connector inspects only the last token of each block for chunk delimiters.
2. Submit those chunks individually to populate the Store. A block-aligned
   request ending with the delimiter takes the chunk-cache build path.
3. Build a query from the same padded chunk token sequences followed by a
   question suffix. Preserve token IDs and boundaries; tokenizing a concatenated
   text string again may change the chunk identity.
4. Submit the combined request. The connector first checks the reusable prefix,
   then looks up chunk hashes independently of their new positions. The current
   minimum is **16 chunk-cache hit blocks**; fewer hits take the ordinary
   prefix-cache path instead of blending.

Use the same model, tokenizer, block size and Store across population and
querying. An `Empty` pipeline cannot provide chunk hits. Do not mix this cache
namespace with data produced under different model or positional settings.

## What happens on a hit

The connector sends chunk boundaries, hit masks and position offsets to the
worker through KV connector metadata. After loading KV, it corrects cached
keys for their new rotary positions. `Blend` compares cached and recomputed
keys at each configured layer and selects the tokens with the largest absolute
key differences.

The resulting compute mask includes selected cache-hit tokens, all missing
chunk tokens and the question suffix. Attention metadata and later layer/FFN
inputs are shortened accordingly. These intermediate tensors require the
sparse model hooks as well as the connector; external KV loading alone does
not perform the blend.

## Verify reuse and quality

- In connector logs, inspect `req_stage`, `first chunk prefix hit`, and
  `chunks cache total hit`. Confirm a query enters `CACHE_BLEND` after
  population and has enough chunk hits.
- Look for `[blend-attn] reduce attn tokens from ... to ...` at a configured
  layer. A cache-hit log without token reduction does not prove recomputation
  selection ran.
- Compare cold requests, repeated documents in a different order, and partial
  cache misses with full recomputation. Check answer quality and time to first
  token using the same tokenized inputs and decoding settings.
- Test the intended batch and prompt sizes. A successful short example does
  not validate chunked prefill, another model layout or another accelerator.

## Implementation references

- [Chunk parsing, cache lookup and position correction](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/integration/vllm/blend_connector.py)
- [Recomputation mask and model hooks](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/ucm/sparse/blend/blend.py)
- [Tokenized chunk construction example](https://github.com/SuperMarioYL/unified-cache-management/blob/a4fc5ab41ab100366325b0f06498a49e4af27d38/examples/offline_inference_blend.py)
