# 模型教程

通过 Docker 部署带有 UCM 的模型，再发送第一条 API 请求。每篇教程都提供
UCM 配置和完整的容器启动命令，可在 CUDA 与 Ascend 标签之间切换。

## Docker 教程

| 模型 | 平台 | 教程 |
| --- | --- | --- |
| Qwen3.8-27B | CUDA / Ascend | [部署并调用 Qwen3.8-27B](qwen3/index.md) |
| DeepSeek-V4-Flash | CUDA / Ascend | [部署并调用 DeepSeek-V4-Flash](deepseek/index.md) |

按教程指定的引擎版本和硬件，在[安装页面](../installation.md)选择对应的 UCM 镜像。

## 其他模型资料

[GLM](glm/index.md)、[MiniMax](minimax/index.md) 和 [Kimi](kimi/index.md)
页面收录了官方引擎指南，目前尚未提供对应的 UCM Docker 教程。

完整的上游模型目录见
[vLLM Ascend Model Tutorials](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/)。
