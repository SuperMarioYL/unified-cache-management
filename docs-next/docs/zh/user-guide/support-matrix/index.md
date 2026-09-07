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

下表说明当前代码中存在的集成路径。模型名称匹配和被补丁修改的模型类只用于确定验证入口，不代表某个模型系列已经完成硬件验收。

| 实现 | 引擎集成路径 | 模型与执行边界 |
| --- | --- | --- |
| [GSAOnDevice](../capabilities/sparse-attention/gsa.md) | vLLM / vLLM-Ascend 0.11.0 的自动稀疏补丁 | 有 CUDA/NPU 路径；按名称为 DeepSeek R1/V2、Qwen3 4B/32B/Coder 30B A3B、QwQ 32B 选择配置；仍须检查实际模型维度和激活阈值 |
| [CacheBlend](../capabilities/sparse-attention/cacheblend.md) | vLLM 0.11.0 的自动稀疏钩子；另有 0.9.2 手动稀疏补丁 | 实验性 CUDA 路径，依赖 `llama`/`qwen2` 前向钩子及兼容的旋转位置缓存布局；不支持 chunked prefill 或原生 HBM Prefix Cache 复用 |
| [ReRoPE](../capabilities/rerope.md) | vLLM 0.9.2 / 0.11.0 手动补丁 | 依赖 Triton attention 和经过补丁修改的 `qwen2`/`qwen3`/`qwen3_moe` 类；不会自动启用 ReRoPE，未建立 Ascend 集成路径 |

较新引擎的 Prefix Cache 支持不代表这些注意力改动也受支持。请按对应指南准备环境，并验证具体的模型、引擎、平台和负载组合。

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
