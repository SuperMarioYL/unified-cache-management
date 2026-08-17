# syntax=docker/dockerfile:1.12.1@sha256:93bfd3b68c109427185cd78b4779fc82b484b0b7618e36d0f104d4d801e66d25
# Pre-published CANN + Mooncake builder. Produced by .github/workflows/
# _prepare-builders.yml and referenced by toolchain.lock.yaml
# builders.{cann900-a2,cann900-a3}.{amd64,arm64}.root so wheel builds
# FROM ${UCM_BUILDER_IMAGE} directly without recompiling Mooncake each run.
# The output must satisfy the cann900 builder checks in toolchain.lock.yaml
# (libmooncake_store.so, transfer_engine.h, Mooncake include directory).
ARG CANN_BASE=quay.io/ascend/cann:9.0.0-910b-ubuntu22.04-py3.12
ARG MOONCAKE_TAG=v0.3.9
FROM ${CANN_BASE}

# Re-declare the global ARG so the stage RUN can see ${MOONCAKE_TAG}; without
# this Docker scopes pre-FROM ARGs out of the build stage and -u makes the
# reference unbound.
ARG MOONCAKE_TAG

SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]

COPY tools/mooncake_installer.sh /vllm-workspace/
# Skip on non-Ascend builders: they lack the CANN toolkit. cuda130 forbids
# mooncakestore by native-contract, so this Dockerfile is only invoked for the
# cann900-a2 / cann900-a3 profiles in _prepare-builders.yml.
RUN if [ ! -f /usr/local/Ascend/ascend-toolkit/set_env.sh ]; then exit 0; fi && \
    apt-get update -y && \
    apt-get install -y git vim wget net-tools gcc g++ cmake numactl libnuma-dev libjemalloc2 clang-15 && \
    update-alternatives --install /usr/bin/clang clang /usr/bin/clang-15 20 && \
    update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-15 20 && \
    git clone --depth 1 --branch ${MOONCAKE_TAG} https://github.com/kvcache-ai/Mooncake /vllm-workspace/Mooncake && \
    mv /vllm-workspace/mooncake_installer.sh /vllm-workspace/Mooncake/ && \
    cd /vllm-workspace/Mooncake && bash mooncake_installer.sh -y && \
    source /usr/local/Ascend/ascend-toolkit/set_env.sh && \
    mkdir -p build && cd build && cmake .. -DUSE_ASCEND_DIRECT=ON && \
    make -j$(nproc) && make install && \
    rm -rf /vllm-workspace/Mooncake/build && \
    rm -rf /var/cache/apt/* && rm -rf /var/lib/apt/lists/* && \
    ldconfig /usr/local/lib
