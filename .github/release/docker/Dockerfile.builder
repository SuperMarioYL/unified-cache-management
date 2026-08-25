# syntax=docker/dockerfile:1.12.1@sha256:93bfd3b68c109427185cd78b4779fc82b484b0b7618e36d0f104d4d801e66d25
ARG CANN_BASE=registry.invalid/ucm/required-cann-base:invalid
ARG MOONCAKE_RUNTIME_IMAGE

FROM ${MOONCAKE_RUNTIME_IMAGE} AS mooncake-runtime

RUN mkdir -p /mooncake-libs && \
    find /usr/local/lib -maxdepth 1 \( -type f -o -type l \) \
      \( -name '*mooncake*' -o -name 'libtransfer_engine.so*' -o -name 'libascend_transport.so*' \) \
      -exec cp -a '{}' /mooncake-libs/ \; && \
    test -n "$(find /mooncake-libs -maxdepth 1 \( -type f -o -type l \) -print -quit)"

FROM ${CANN_BASE}

COPY --from=mooncake-runtime /usr/local/include/mooncake /usr/local/include/mooncake
COPY --from=mooncake-runtime /mooncake-libs/ /usr/local/lib/

RUN test -d /usr/local/include/mooncake && ldconfig /usr/local/lib
