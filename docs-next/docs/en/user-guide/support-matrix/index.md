# Feature and Model Support Matrix

This page provides an overview of UCM (Unified Cache Manager) compatibility across different models and inference frameworks.
Use this matrix as the repository's reported compatibility reference. The `main`
columns describe integration development, not a pinned release test matrix.
Check the selected release artifacts and engine recipe before deployment; this
documentation migration does not constitute new hardware or model validation.

## Legend

| Symbol | Description |
|--------|-------------|
| ✅ | Reported supported for the listed integration |
| ❌ | Not supported |
| 🟡 | Not tested or verified |

## Model Support and Feature Compatibility

### Prefix Cache Support

This section presents prefix cache support for each model across the supported inference frameworks.
This information serves as a reference for evaluating framework compatibility in deployments that require prefix cache.

| Model | vLLM<br>(main) | vLLM-Ascend<br>(main) | SGLang<br>(main) |
|-------|:-----------:|:------------------:|:------:|
| DeepSeek V3/3.1 | ✅ | ✅ | ✅ |
| DeepSeek R1 | ✅ | ✅ | ✅ |
| DeepSeek V3.2 | ✅ | ✅ | ✅ |
| DeepSeek V4 Pro | ✅ | ✅ | ❌ |
| DeepSeek V4 Flash | ✅ | ✅ | ❌ |
| Qwen2.5 | ✅ | ✅ | ✅ |
| Qwen3 | ✅ | ✅ | ✅ |
| Qwen3-MoE | ✅ | ✅ | ✅ |
| Qwen3-Next | ✅ | ✅ | ❌ |
| Qwen3.5 | ✅ | ✅ | ❌ |
| Qwen3.6 | ✅ | ✅ | ❌ |
| Qwen3.8 | ✅ | ✅ | ❌ |
| Qwen3.8-Flash-Next | 🟡 | 🟡 | ❌ |
| GLM-4.x | ✅ | ✅ | ✅ |
| GLM-5 | ✅ | ✅ | ❌ |
| GLM-5.1 | ✅ | ✅ | ❌ |
| GLM-5.2 | ✅ | ✅ | ❌ |
| GLM-5.3-Flash | 🟡 | 🟡 | ❌ |
| MiniMax-M2.5 | ✅ | ✅ | ✅ |
| MiniMax-M2.7 | ✅ | ✅ | ✅ |
| MiniMax-M3 | 🟡 | 🟡 | ❌ |
| Kimi-K2.5 | ✅ | ✅ | ❌ |
| Kimi-K3 | 🟡 | 🟡 | ❌ |

> **Note**: The table lists a selected set of representative models.
> See [**Prefix Cache**](../capabilities/prefix-cache/index.md) for more details.

### Inference Enhancement Features

These entries describe the integration paths present in this revision. Model
routing and patched model classes identify where to begin validation; they
are not hardware acceptance results for every model in a family.

| Implementation | Engine integration | Model and execution boundary |
| --- | --- | --- |
| [GSAOnDevice](../capabilities/sparse-attention/gsa.md) | Automatic sparse patches for vLLM / vLLM-Ascend 0.11.0 | CUDA/NPU paths; name-based configuration selection for DeepSeek R1/V2, Qwen3 4B/32B/Coder 30B A3B, and QwQ 32B; verify actual model dimensions and activation thresholds |
| [CacheBlend](../capabilities/sparse-attention/cacheblend.md) | Automatic sparse hooks for vLLM 0.11.0; manual sparse patches also exist for 0.9.2 | Experimental CUDA path with `llama`/`qwen2` forward hooks and a compatible rotary-cache layout; no chunked prefill or native HBM prefix-cache reuse |
| [ReRoPE](../capabilities/rerope.md) | Manual patches for vLLM 0.9.2 / 0.11.0 | Triton attention and patched `qwen2`/`qwen3`/`qwen3_moe` classes; no automatic ReRoPE activation or established Ascend path |

Prefix-cache support on a newer engine does not imply support for these
attention changes. Follow the linked guide for prerequisites and verify the
specific model, engine, platform and workload combination.

## Supported Compute Platforms and Devices

This section presents the currently supported compute platforms and devices.

| Compute Platform | Vendor | Device |
|:----------------:|:------:|:------:|
| CANN | Ascend | 910C, 910B |
| CUDA | NVIDIA | H100, H20, L40, L20 |
| MUSA | Mthreads | S5000 |
| MACA | MetaX | C500 |

> **Note**: The table shows only selected platforms.

## Notes and Limitations

- This matrix is provided as a compatibility reference for the configurations listed on this page.
- Actual behavior may vary depending on hardware, runtime settings, backend changes, and model variants.
- This support matrix is continuously updated. **For the latest information, please refer to the GitHub issues and pull requests.**
