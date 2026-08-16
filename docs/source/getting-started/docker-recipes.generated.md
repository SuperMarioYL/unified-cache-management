# Repository Docker recipes

<!-- Generated from .github/release/release.yaml; do not edit manually. -->

These repository recipes are compatibility source builds. They do not own the formal release flow, which uses `.github/release/docker/Dockerfile` with a sealed wheel.

| ID | Recipe | Base image | Status | Lanes | Build mode | Formal-release boundary |
| --- | --- | --- | --- | --- | --- | --- |
| `mindie-a2-v2-source` | `docker/Dockerfile.ucm-mindie-ascend.a2-v2` | `swr.cn-south-1.myhuaweicloud.com/ascendhub/mindie:2.3.0-800I-A2-py311-openeuler24.03-lts` | specialized | pr-smoke, manual | legacy-source-build | Specialized MindIE source build is not a sealed-wheel formal release recipe. |
| `sglang-cuda-055-source` | `docker/Dockerfile.ucm-sglang-cuda-v0.5.5` | `lmsysorg/sglang:v0.5.5.post3` | specialized | pr-smoke, manual | legacy-source-build | Specialized SGLang source build is not a sealed-wheel formal release recipe. |
| `vllm-ascend-a2-0180-glm51-source` | `docker/Dockerfile.ucm-vllm-ascend.a2-v0.18.0glm5.1` | `quay.io/ascend/vllm-ascend:v0.18.0` | specialized | manual | legacy-source-build | Specialized GLM source build has no sealed formal-release authority. |
| `vllm-ascend-a2-0180-hardware` | `docker/Dockerfile.ucm-vllm-ascend.a2-v0.18.0` | `quay.io/ascend/vllm-ascend:v0.18.0` | legacy | hardware-e2e, manual | legacy-source-build | Hardware regression source build is separate from formal sealed-wheel release. |
| `vllm-ascend-a2-0202rc1-smoke` | `docker/Dockerfile.ucm-vllm-ascend.a2-v0.20.2rc1` | `quay.io/ascend/vllm-ascend:v0.20.2rc1` | legacy | pr-smoke, manual | legacy-source-build | PR compatibility source build is not the generic sealed-wheel release flow. |
| `vllm-ascend-a2-0210rc1-source` | `docker/Dockerfile.ucm-vllm-ascend.a2-v0.21.0rc1` | `quay.io/ascend/vllm-ascend:v0.21.0rc1` | legacy | manual | legacy-source-build | Historical source-build recipe is excluded from formal release. |
| `vllm-ascend-a2-0221rc1-source` | `docker/Dockerfile.ucm-vllm-ascend.a2-v0.22.1rc1` | `quay.io/ascend/vllm-ascend:v0.22.1rc1` | legacy | manual | legacy-source-build | Repository source build overlaps a formal target but is not its sealed-wheel authority. |
| `vllm-ascend-a2-0230rc1-source` | `docker/Dockerfile.ucm-vllm-ascend.a2-v0.23.0rc1` | `quay.io/ascend/vllm-ascend:v0.23.0rc1` | legacy | manual | legacy-source-build | Historical source-build recipe is excluded from formal release. |
| `vllm-ascend-a2-nightly-source` | `docker/Dockerfile.ucm-vllm-ascend.a2-latest` | `quay.io/ascend/vllm-ascend:nightly-main-openeuler` | nightly | manual | legacy-source-build | Mutable nightly source build is excluded from formal release. |
| `vllm-ascend-a3-0180-glm51-source` | `docker/Dockerfile.ucm-vllm-ascend.a3-v0.18.0glm5.1` | `quay.io/ascend/vllm-ascend:v0.18.0-a3` | specialized | manual | legacy-source-build | Specialized GLM source build has no sealed formal-release authority. |
| `vllm-ascend-a3-0202rc1-source` | `docker/Dockerfile.ucm-vllm-ascend.a3-v0.20.2rc1` | `quay.io/ascend/vllm-ascend:v0.20.2rc1-a3` | legacy | manual | legacy-source-build | Historical source-build recipe is excluded from formal release. |
| `vllm-ascend-a3-0210rc1-source` | `docker/Dockerfile.ucm-vllm-ascend.a3-v0.21.0rc1` | `quay.io/ascend/vllm-ascend:v0.21.0rc1-a3` | legacy | manual | legacy-source-build | Historical source-build recipe is excluded from formal release. |
| `vllm-ascend-a3-0221rc1-source` | `docker/Dockerfile.ucm-vllm-ascend.a3-v0.22.1rc1` | `quay.io/ascend/vllm-ascend:v0.22.1rc1-a3` | legacy | manual | legacy-source-build | Repository source build overlaps a formal target but is not its sealed-wheel authority. |
| `vllm-ascend-a3-0230rc1-source` | `docker/Dockerfile.ucm-vllm-ascend.a3-v0.23.0rc1` | `quay.io/ascend/vllm-ascend:v0.23.0rc1-a3` | legacy | manual | legacy-source-build | Historical source-build recipe is excluded from formal release. |
| `vllm-cuda-0180-source` | `docker/Dockerfile.ucm-vllm-cuda-v0.18.0` | `vllm/vllm-openai:v0.18.0` | legacy | manual | legacy-source-build | Historical source-build recipe is excluded from formal release. |
| `vllm-cuda-0202-smoke` | `docker/Dockerfile.ucm-vllm-cuda-v0.20.2` | `vllm/vllm-openai:v0.20.2` | legacy | pr-smoke, manual | legacy-source-build | PR compatibility source build is not the generic sealed-wheel release flow. |
| `vllm-cuda-0210-source` | `docker/Dockerfile.ucm-vllm-cuda-v0.21.0` | `vllm/vllm-openai:v0.21.0` | legacy | manual | legacy-source-build | Repository source build overlaps a formal target but is not its sealed-wheel authority. |
| `vllm-cuda-nightly-source` | `docker/Dockerfile.ucm-vllm-cuda-latest` | `vllm/vllm-openai:nightly` | nightly | manual | legacy-source-build | Mutable nightly source build is excluded from formal release. |
