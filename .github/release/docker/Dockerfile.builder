# syntax=docker/dockerfile:1.12.1@sha256:93bfd3b68c109427185cd78b4779fc82b484b0b7618e36d0f104d4d801e66d25
# Builder-only extension for official Ascend manylinux images discovered by
# sync-builders.yml. The project builder pool records the source image and the
# canonical Mooncake-bearing target tag; release wheel jobs consume that pool.
ARG CANN_BASE=registry.invalid/ucm/required-cann-base:invalid
FROM ${CANN_BASE}

ARG MOONCAKE_TAG
SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]

COPY tools/mooncake_installer.sh /vllm-workspace/mooncake_installer.sh

# Official Ascend manylinux builders are yum based. Bootstrap only what is
# needed to fetch Mooncake; the existing installer owns the Mooncake and UCM
# native dependency set for both supported architectures.
RUN test -n "${MOONCAKE_TAG}" && \
    command -v yum && \
    yum install -y ca-certificates curl git && \
    git clone --depth 1 --branch "${MOONCAKE_TAG}" \
      https://github.com/kvcache-ai/Mooncake /vllm-workspace/Mooncake && \
    mv /vllm-workspace/mooncake_installer.sh /vllm-workspace/Mooncake/ && \
    cd /vllm-workspace/Mooncake && \
    bash mooncake_installer.sh -y && \
    test -f /usr/local/Ascend/ascend-toolkit/set_env.sh && \
    source /usr/local/Ascend/ascend-toolkit/set_env.sh && \
    cmake -S . -B build -DUSE_ASCEND_DIRECT=ON && \
    cmake --build build --parallel "$(nproc)" && \
    cmake --install build && \
    rm -rf build && \
    yum clean all && \
    rm -rf /var/cache/yum && \
    ldconfig /usr/local/lib
