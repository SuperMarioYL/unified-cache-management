# 核心能力

根据需要减少的计算或传输选择能力。可用范围取决于引擎、模型、后端构建和运行时版本，详见[支持矩阵](../support-matrix/index.md)。

| 能力 | 作用 | 使用指南 |
| --- | --- | --- |
| Prefix Cache | 跨请求复用相同 token 前缀的 KV 块 | [前缀缓存与存储后端](prefix-cache/index.md) |
| Sparse Attention | 选择部分上下文 KV 数据参与注意力计算 | [稀疏注意力](sparse-attention/index.md) |
| PD 分离 | 分别运行 prefill 与 decode，并在两者间传输 KV 状态 | [PD 分离](pd-disaggregation/index.md) |
| ReRoPE | 调整旋转位置处理方式以扩展上下文 | [ReRoPE](rerope.md) |

组合能力前，请先完成[安装](../installation.md)和[引擎快速开始](../quick_start/index.md)。各篇指南说明当前实现、所需的引擎补丁与配置，以及针对实际负载验证该能力的方法。

实现边界参见[能力设计原则](../../developer-guide/capability-principles.md)。
