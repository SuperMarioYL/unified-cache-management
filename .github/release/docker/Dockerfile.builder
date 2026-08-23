# syntax=docker/dockerfile:1.12.1
# Builder-only extension for official Ascend manylinux images discovered by
# sync-builders.yml. The project builder pool records the source image and the
# canonical Mooncake-bearing target tag; release wheel jobs consume that pool.
ARG CANN_BASE=registry.invalid/ucm/required-cann-base:invalid
FROM ${CANN_BASE}

ARG MOONCAKE_TAG
SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]

COPY tools/mooncake_installer.sh /vllm-workspace/mooncake_installer.sh
COPY tools/gflags-config.cmake /usr/local/lib64/cmake/gflags/gflags-config.cmake

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
    boost_lockfree=/usr/include/boost/lockfree/detail/parameter.hpp && \
    if grep -Fq 'allocator_arg::template rebind<T>::other' "${boost_lockfree}"; then \
      sed -i \
        's/typename allocator_arg::template rebind<T>::other/typename std::allocator_traits<allocator_arg>::template rebind_alloc<T>/' \
        "${boost_lockfree}"; \
    fi && \
    ! grep -Fq 'allocator_arg::template rebind<T>::other' "${boost_lockfree}" && \
    boost_queue=/usr/include/boost/lockfree/queue.hpp && \
    if grep -Fq 'node_allocator::template rebind<U>::other' "${boost_queue}"; then \
      sed -i \
        's/typename node_allocator::template rebind<U>::other/typename std::allocator_traits<node_allocator>::template rebind_alloc<U>/g' \
        "${boost_queue}"; \
    fi && \
    ! grep -Fq 'node_allocator::template rebind<U>::other' "${boost_queue}" && \
    test -f /usr/local/Ascend/ascend-toolkit/set_env.sh && \
    source /usr/local/Ascend/ascend-toolkit/set_env.sh && \
    cmake -S . -B build \
      -DUSE_ASCEND_DIRECT=ON \
      -DBUILD_UNIT_TESTS=OFF \
      -DBUILD_EXAMPLES=OFF && \
    cmake --build build --parallel "$(nproc)" && \
    cmake --install build && \
    rm -rf build && \
    yum clean all && \
    rm -rf /var/cache/yum && \
    ldconfig /usr/local/lib
