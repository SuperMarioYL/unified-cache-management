# syntax=docker/dockerfile:1.12.1@sha256:93bfd3b68c109427185cd78b4779fc82b484b0b7618e36d0f104d4d801e66d25
ARG SOURCE_DATE_EPOCH=0
ARG BASE_IMAGE=registry.invalid/ucm/base@sha256:0000000000000000000000000000000000000000000000000000000000000000
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG SOURCE_DATE_EPOCH
ARG TARGETPLATFORM
ARG UCM_WHEEL

COPY image-recipe.json /usr/local/share/ucm-release/image-recipe.json
COPY image-metadata.json /usr/local/share/ucm-release/image-metadata.json
COPY verify_base_image.py /usr/local/bin/verify_base_image.py
COPY install_ucm.py /usr/local/bin/install_ucm.py
COPY inspect_runtime.py /usr/local/bin/inspect_runtime.py
COPY ${UCM_WHEEL} /tmp/${UCM_WHEEL}

RUN python /usr/local/bin/verify_base_image.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --base-image "${BASE_IMAGE}" \
      --target-platform "${TARGETPLATFORM}" \
      --output /usr/local/share/ucm-release/base-verification.json
RUN python /usr/local/bin/install_ucm.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --metadata /usr/local/share/ucm-release/image-metadata.json \
      --wheel "/tmp/${UCM_WHEEL}" \
      --output /usr/local/share/ucm-release/install-result.json
RUN python /usr/local/bin/inspect_runtime.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --install-result /usr/local/share/ucm-release/install-result.json \
      --output /usr/local/share/ucm-release/runtime-inspection.json
