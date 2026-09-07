# 模型与能力支持矩阵

本页汇总 UCM 在不同模型和推理框架中的兼容范围。它是仓库报告的兼容性参考；`main` 列描述集成开发状态，并非固定 Release 的测试矩阵。部署前仍需确认所选发布制品和引擎指南，本次文档迁移不代表进行了新的硬件或模型验证。

## 图例

| 标记 | 说明 |
|--------|-------------|
| ✅ | 所列集成报告为支持 |
| ❌ | 不支持 |
| 🟡 | 尚未测试或验证 |

## 模型与能力兼容性

### Prefix Cache 支持

下表列出不同推理框架中报告的模型前缀缓存支持情况，供选择部署组合时参考。

| 模型 | vLLM<br>(main) | vLLM-Ascend<br>(main) | SGLang<br>(main) |
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

> 表中仅列出部分代表性模型。更多说明参见 [Prefix Cache](../capabilities/prefix-cache/index.md)。

### 推理增强能力

下表列出 Sparse Attention、ReRoPE 和 CacheBlend 在特定模型与框架版本中的支持情况。

| 模型 | GsaOn设备<br>vLLM / vLLM-Ascend 0.11.0 | ReRoPE<br>vLLM 0.11.0 | CacheBlend<br>vLLM 0.9.2 |
|-------|:-------------------------:|:------------------------:|:---------------------:|
| DeepSeek V3/3.1 | ✅ | ✅ | ✅ |
| DeepSeek R1 | ✅ | ✅ | ✅ |
| DeepSeek V3.2 | ✅ | ✅ | ✅ |
| Qwen2.5 | ✅ | ✅ | ✅ |
| Qwen3 | ✅ | ✅ | ✅ |

> 对应版本的使用方式参见[稀疏注意力](../capabilities/sparse-attention/index.md)和 [ReRoPE](../capabilities/rerope.md)。

## 计算平台与设备

| 计算平台 | 厂商 | 设备 |
|:----------------:|:------:|:------:|
| CANN | Ascend | 910C, 910B |
| CUDA | NVIDIA | H100, H20, L40, L20 |
| MUSA | Mthreads | S5000 |
| MACA | MetaX | C500 |

> 表中仅列出部分平台。

## 说明与限制

- 矩阵仅用于参考本页所列配置的兼容范围。
- 实际行为可能受硬件、运行参数、后端变化和模型变体影响。
- 支持矩阵持续更新；最新集成进展请参阅 GitHub Issue 和 Pull Request。
