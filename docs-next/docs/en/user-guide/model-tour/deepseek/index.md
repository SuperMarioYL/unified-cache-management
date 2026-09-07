# DeepSeek

DeepSeek model tutorials currently published by vLLM Ascend.

## Models

| Model | vLLM Ascend latest guide |
| --- | --- |
| DeepSeek-V3 & 3.1 | [Official guide](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/DeepSeek-V3.1.html) |
| DeepSeek-V3.2 | [Official guide](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/DeepSeek-V3.2.html) |
| DeepSeek-V4-Flash | [Official guide](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/DeepSeek-V4-Flash.html) |
| DeepSeek-V4-Pro | [Official guide](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/DeepSeek-V4-Pro.html) |
| DeepSeek-R1 | [Official guide](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/DeepSeek-R1.html) |
| DeepSeek-OCR-2 | [Official guide](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/DeepSeekOCR2.html) |

## Enable UCM with this model

Use the official recipe above for the model's engine settings, then follow the
[vLLM](../../quick_start/quickstart_vllm.md),
[vLLM-Ascend](../../quick_start/quickstart_vllm_ascend.md), or
[SGLang](../../quick_start/quickstart_sglang.md) integration guide. Confirm the
model and feature in the [support matrix](../../support-matrix/index.md).
An official engine tutorial establishes engine usage; it does not independently
verify UCM external-cache behavior for that model.
