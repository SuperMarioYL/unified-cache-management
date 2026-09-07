# 部署

UCM 通过运行时 KV connector 或存储适配接入不同推理引擎。各引擎的快速开始指南分别说明安装、配置、启动推理和验证外部缓存的方法。

## 选择推理引擎

根据实际使用的引擎选择指南。请先阅读[安装](../installation.md)，获取与引擎、平台及存储后端匹配的制品。

<div class="grid cards" markdown>

-   :material-home-account: **vLLM（CUDA）**

    ---

    在 CUDA GPU 上将 UCM 接入 vLLM，配置前缀缓存并验证缓存复用。

    [:octicons-arrow-right-24: vLLM 快速开始](quickstart_vllm.md)

-   :material-chip: **vLLM-Ascend（NPU）**

    ---

    在昇腾 NPU 上将 UCM 接入 vLLM-Ascend，并配置对应的设备环境。

    [:octicons-arrow-right-24: vLLM-Ascend 快速开始](quickstart_vllm_ascend.md)

-   :material-tools: **SGLang（CUDA）**

    ---

    通过分层缓存配置，在 CUDA GPU 上将 UCM 接入 SGLang。

    [:octicons-arrow-right-24: SGLang 快速开始](quickstart_sglang.md)

-   :material-server: **MindIE（昇腾 NPU）**

    ---

    在昇腾 NPU 上使用 MindIE-LLM，完成 UCM 集成和 `mindie_llm_server` 配置。

    [:octicons-arrow-right-24: MindIE 快速开始](quickstart_mindie_llm.md)

</div>

## 从源码构建

需要开发、定制，或目标引擎没有对应发布制品时，请参阅[从源码构建和安装 UCM](../../developer-guide/build_from_source.md)。
