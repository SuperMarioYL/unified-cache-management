# MiniMax

MiniMax model tutorials currently published by vLLM Ascend.

## Models

| Model | vLLM Ascend latest guide |
| --- | --- |
| MiniMax-M2 | [Official guide](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/MiniMax-M2.html) |
| MiniMax-M3 | [Official guide](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/MiniMax-M3.html) |

## Enable UCM with this model

Use the official recipe above for the model's engine settings, then follow the
[vLLM](../../quick_start/quickstart_vllm.md),
[vLLM-Ascend](../../quick_start/quickstart_vllm_ascend.md), or
[SGLang](../../quick_start/quickstart_sglang.md) integration guide. Confirm the
model and feature in the [support matrix](../../support-matrix/index.md).
An official engine tutorial establishes engine usage; it does not independently
verify UCM external-cache behavior for that model.
