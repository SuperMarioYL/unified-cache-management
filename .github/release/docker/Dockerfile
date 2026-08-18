# syntax=docker/dockerfile:1.12.1@sha256:93bfd3b68c109427185cd78b4779fc82b484b0b7618e36d0f104d4d801e66d25
# check=skip=SecretsUsedInArgOrEnv
ARG SOURCE_DATE_EPOCH=0
ARG BASE_IMAGE=registry.invalid/ucm/base@sha256:0000000000000000000000000000000000000000000000000000000000000000
ARG UCM_BUILDER_IMAGE=registry.invalid/ucm/builder@sha256:0000000000000000000000000000000000000000000000000000000000000000

FROM ${UCM_BUILDER_IMAGE} AS wheel-base

ARG UCM_BUILDER_IMAGE
ARG SOURCE_DATE_EPOCH
ARG TARGETARCH
ARG PLATFORM
ARG UCM_RELEASE_TASK_ID
ARG UCM_RELEASE_SPEC_ID
ARG UCM_RELEASE_PROFILE
ARG UCM_RELEASE_SOURCE_SHA
ARG UCM_RELEASE_VERSION
ARG UCM_RELEASE_BUILD_KEY
ARG UCM_RELEASE_PYTHON_VERSION
ARG UCM_RELEASE_PYTHON_ABI
ARG UCM_RELEASE_WHEEL_PLATFORM
ARG UCM_RELEASE_DIST_NAME
ARG UCM_RELEASE_BUILD_SETTINGS
ARG UCM_RUNTIME_PATCH_MANIFEST_SHA256
ARG UCM_RELEASE_REQUIRED_TARGETS
ARG UCM_RELEASE_FORBIDDEN_TARGETS

SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]
ENV TZ=UTC PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=30 PIP_RETRIES=2 \
    PYTHONDONTWRITEBYTECODE=1
ARG LD_LIBRARY_PATH
ENV LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

RUN python_candidate="/opt/python/${UCM_RELEASE_PYTHON_ABI}-${UCM_RELEASE_PYTHON_ABI}/bin/python"; \
    if [[ -x "${python_candidate}" ]]; then \
      ln -s "${python_candidate}" /usr/local/bin/ucm-python; \
    elif command -v "python${UCM_RELEASE_PYTHON_VERSION}" >/dev/null; then \
      ln -s "$(command -v "python${UCM_RELEASE_PYTHON_VERSION}")" /usr/local/bin/ucm-python; \
    elif command -v python3 >/dev/null && \
         [[ "$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" == "${UCM_RELEASE_PYTHON_VERSION}" ]]; then \
      ln -s "$(command -v python3)" /usr/local/bin/ucm-python; \
    else \
      echo "immutable builder lacks ${UCM_RELEASE_PYTHON_ABI}" >&2; exit 1; \
    fi

COPY wheel-task.json /tmp/wheel-task.json
COPY build-wheels /wheelhouse
COPY build-requirements.lock /tmp/build-requirements.lock
RUN test "${UCM_BUILDER_IMAGE}" != "${UCM_BUILDER_IMAGE%@sha256:*}" && \
    ucm-python -m pip install --no-index --find-links=/wheelhouse \
      --require-hashes --only-binary=:all: -r /tmp/build-requirements.lock

WORKDIR /workspace/ucm
COPY ucm-source.tar /tmp/ucm-source.tar
COPY source-commit.payload /tmp/source-commit.payload
COPY source-context.json /tmp/source-context.json
RUN ucm-python - <<'PY'
import tarfile
with tarfile.open('/tmp/ucm-source.tar', mode='r:') as archive:
    archive.extractall('/workspace/ucm', filter='data')
PY
RUN PYTHONPATH=/workspace/ucm/.github/release \
    ucm-python -m ucm_release wheel verify-context \
      --archive /tmp/ucm-source.tar \
      --commit-payload /tmp/source-commit.payload \
      --expected-source-sha "${UCM_RELEASE_SOURCE_SHA}" \
      --manifest /tmp/source-context.json \
      --source-root /workspace/ucm

FROM wheel-base AS builder

# Mooncake is prebuilt into UCM_BUILDER_IMAGE (the ghcr builder published by
# .github/workflows/_prepare-builders.yml via Dockerfile.builder). Assert the
# native contract paths on Ascend; cuda130 builders carry no Mooncake
# (forbidden by native-contract), so the guard skips them.
RUN if [ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ]; then \
      test -f /usr/local/lib/libmooncake_store.so && \
      test -f /usr/local/include/transfer_engine.h && \
      test -d /vllm-workspace/Mooncake/mooncake-store/include; fi

FROM builder AS wheel-build

RUN <<'EOF'
test -n "${TARGETARCH}"
test -n "${PLATFORM}"
test -n "${UCM_RELEASE_TASK_ID}"
test -n "${UCM_RELEASE_SPEC_ID}"
test -n "${UCM_RELEASE_PROFILE}"
test -n "${UCM_RELEASE_SOURCE_SHA}"
test -n "${UCM_RELEASE_VERSION}"
test -n "${UCM_RELEASE_BUILD_KEY}"
test -n "${UCM_RELEASE_PYTHON_VERSION}"
test -n "${UCM_RELEASE_PYTHON_ABI}"
test -n "${UCM_RELEASE_WHEEL_PLATFORM}"
test -n "${UCM_RELEASE_BUILD_SETTINGS}"
test -n "${UCM_RUNTIME_PATCH_MANIFEST_SHA256}"
test -n "${UCM_RELEASE_REQUIRED_TARGETS}"
test -n "${UCM_RELEASE_FORBIDDEN_TARGETS}"
locked_cmake="$(ucm-python -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/cmake"
test -x "${locked_cmake}"
export PATH="$(ucm-python -c 'import sysconfig; print(sysconfig.get_path("scripts"))'):${PATH}"
case "${TARGETARCH}:$(uname -m)" in
  amd64:x86_64|arm64:aarch64) ;;
  *) echo "TARGETARCH does not match native immutable root" >&2; exit 1 ;;
esac
PYTHONPATH=/workspace/ucm/.github/release ucm-python -m ucm_release \
  wheel check-environment \
  --task /tmp/wheel-task.json \
  --python-executable /usr/local/bin/ucm-python \
  >/tmp/builder-environment-check.json
EOF

RUN PYTHONPATH=/workspace/ucm/.github/release \
    ucm-python -m ucm_release wheel authority \
      --spec-id "${UCM_RELEASE_SPEC_ID}" \
      --source-sha "${UCM_RELEASE_SOURCE_SHA}" \
      --source-date-epoch "${SOURCE_DATE_EPOCH}" \
      --builder-coordinate "${UCM_BUILDER_IMAGE}" \
      --wheelhouse /wheelhouse \
      --source-archive /tmp/ucm-source.tar \
      --source-commit-payload /tmp/source-commit.payload \
      --source-manifest /tmp/source-context.json \
      --source-root /workspace/ucm \
      --task-file /tmp/wheel-task.json \
      --output /tmp/build-authority.json

# Rewrite [project].name to the per-profile dist_name in the extracted source
# tree so `python -m build` emits a wheel whose filename/METADATA/dist-info
# carry it natively. setuptools forbids a dynamic [project].name, so the
# static line is patched at build time from the frozen-plan UCM_DIST_NAME input.
RUN UCM_DIST_NAME="${UCM_RELEASE_DIST_NAME}" ucm-python -c 'import os, re, pathlib; p = pathlib.Path("/workspace/ucm/pyproject.toml"); t = p.read_text(); name = os.environ.get("UCM_DIST_NAME"); assert name, "UCM_DIST_NAME required to patch project name"; q = chr(34); t, c = re.subn(r"(?m)^name[ \t]*=.*", "name = " + q + name + q, t); assert c == 1, "expected one [project] name line, replaced " + str(c); p.write_text(t)'

RUN PATH="$(ucm-python -c 'import sysconfig; print(sysconfig.get_path("scripts"))'):${PATH}" \
    UCM_RELEASE_BUILD=1 \
    PLATFORM="${PLATFORM}" \
    UCM_RELEASE_TASK_ID="${UCM_RELEASE_TASK_ID}" \
    UCM_RELEASE_SPEC_ID="${UCM_RELEASE_SPEC_ID}" \
    UCM_RELEASE_PROFILE="${UCM_RELEASE_PROFILE}" \
    UCM_RELEASE_SOURCE_SHA="${UCM_RELEASE_SOURCE_SHA}" \
    UCM_RELEASE_VERSION="${UCM_RELEASE_VERSION}" \
    UCM_RELEASE_BUILD_KEY="${UCM_RELEASE_BUILD_KEY}" \
    UCM_RELEASE_PYTHON_VERSION="${UCM_RELEASE_PYTHON_VERSION}" \
    UCM_RELEASE_PYTHON_ABI="${UCM_RELEASE_PYTHON_ABI}" \
    UCM_RELEASE_WHEEL_PLATFORM="${UCM_RELEASE_WHEEL_PLATFORM}" \
    UCM_DIST_NAME="${UCM_RELEASE_DIST_NAME}" \
    UCM_RELEASE_BUILD_SETTINGS="${UCM_RELEASE_BUILD_SETTINGS}" \
    UCM_RUNTIME_PATCH_MANIFEST_SHA256="${UCM_RUNTIME_PATCH_MANIFEST_SHA256}" \
    UCM_RELEASE_REQUIRED_TARGETS="${UCM_RELEASE_REQUIRED_TARGETS}" \
    UCM_RELEASE_FORBIDDEN_TARGETS="${UCM_RELEASE_FORBIDDEN_TARGETS}" \
    UCM_RELEASE_AUTHORITY_FILE=/tmp/build-authority.json \
    SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
    ucm-python -m build --no-isolation --wheel --outdir /tmp/raw-wheel

RUN PYTHONPATH=/workspace/ucm/.github/release \
    ucm-python -m ucm_release wheel closure /tmp/raw-wheel/*.whl \
      --spec-id "${UCM_RELEASE_SPEC_ID}" \
      --authority-file /tmp/build-authority.json \
      --task-file /tmp/wheel-task.json \
      --output /tmp/dependency-closure.json

RUN PYTHONPATH=/workspace/ucm/.github/release \
    ucm-python -m ucm_release wheel seal /tmp/raw-wheel/*.whl \
      --spec-id "${UCM_RELEASE_SPEC_ID}" \
      --source-sha "${UCM_RELEASE_SOURCE_SHA}" \
      --build-key "${UCM_RELEASE_BUILD_KEY}" \
      --source-date-epoch "${SOURCE_DATE_EPOCH}" \
      --authority-file /tmp/build-authority.json \
      --dependency-closure /tmp/dependency-closure.json \
      --task-file /tmp/wheel-task.json \
      --output-dir /out

FROM scratch AS wheel
COPY --from=wheel-build /out /

FROM ${BASE_IMAGE} AS runtime-install

ARG BASE_IMAGE
ARG SOURCE_DATE_EPOCH
ARG TARGETPLATFORM
ARG UCM_WHEEL

ARG LD_LIBRARY_PATH
ENV LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
RUN ldconfig /usr/local/lib && rm -f /var/cache/ldconfig/aux-cache

COPY image-recipe.json /usr/local/share/ucm-release/image-recipe.json
COPY image-metadata.json /usr/local/share/ucm-release/image-metadata.json
COPY verify_base_image.py /usr/local/bin/verify_base_image.py
COPY install_ucm.py /usr/local/bin/install_ucm.py
COPY inspect_runtime.py /usr/local/bin/inspect_runtime.py
COPY ${UCM_WHEEL} /tmp/${UCM_WHEEL}

RUN python3 /usr/local/bin/verify_base_image.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --base-image "${BASE_IMAGE}" \
      --target-platform "${TARGETPLATFORM}" \
      --output /usr/local/share/ucm-release/base-verification.json
RUN python3 /usr/local/bin/install_ucm.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --metadata /usr/local/share/ucm-release/image-metadata.json \
      --wheel "/tmp/${UCM_WHEEL}" \
      --output /usr/local/share/ucm-release/install-result.json
RUN python3 /usr/local/bin/inspect_runtime.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --install-result /usr/local/share/ucm-release/install-result.json \
      --output /usr/local/share/ucm-release/runtime-inspection.json

FROM runtime-install AS runtime

FROM ${BASE_IMAGE} AS runtime-real-install

ARG BASE_IMAGE
ARG SOURCE_DATE_EPOCH
ARG TARGETPLATFORM
ARG UCM_WHEEL
ARG UCM_SOURCE_SHA
ARG UCM_SOURCE_REPOSITORY_URL
ARG UCM_SOURCE_TREE
ARG UCM_SOURCE_CONTEXT_SHA256
ARG UCM_TASK_SHA256
ARG UCM_BUILD_KEY_SHA256
ARG UCM_WHEEL_SHA256
ARG UCM_RECIPE_SHA256
ARG UCM_RUNTIME_PATCH_VARIANTS

ARG LD_LIBRARY_PATH
ENV LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
ENV UCM_RUNTIME_PATCH_VARIANTS="${UCM_RUNTIME_PATCH_VARIANTS}"
RUN ldconfig /usr/local/lib && rm -f /var/cache/ldconfig/aux-cache

LABEL org.opencontainers.image.source="${UCM_SOURCE_REPOSITORY_URL}" \
      org.opencontainers.image.revision="${UCM_SOURCE_SHA}" \
      io.ucm.release.source-tree="${UCM_SOURCE_TREE}" \
      io.ucm.release.source-context-sha256="${UCM_SOURCE_CONTEXT_SHA256}" \
      io.ucm.release.task-sha256="${UCM_TASK_SHA256}" \
      io.ucm.release.build-key-sha256="${UCM_BUILD_KEY_SHA256}" \
      io.ucm.release.wheel-sha256="${UCM_WHEEL_SHA256}" \
      io.ucm.release.recipe-sha256="${UCM_RECIPE_SHA256}"

COPY image-recipe.json /usr/local/share/ucm-release/image-recipe.json
COPY verify_base_image.py /usr/local/bin/verify_base_image.py
COPY install_ucm.py /usr/local/bin/install_ucm.py
COPY inspect_runtime.py /usr/local/bin/inspect_runtime.py

RUN python3 /usr/local/bin/verify_base_image.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --base-image "${BASE_IMAGE}" \
      --target-platform "${TARGETPLATFORM}" \
      --output /usr/local/share/ucm-release/base-verification.json
RUN --mount=type=bind,target=/wheelhouse,readonly \
    python3 /usr/local/bin/install_ucm.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --wheelhouse /wheelhouse \
      --lock /wheelhouse/requirements.lock \
      --output /usr/local/share/ucm-release/install-result.json
RUN python3 /usr/local/bin/inspect_runtime.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --install-result /usr/local/share/ucm-release/install-result.json \
      --output /usr/local/share/ucm-release/runtime-inspection.json

FROM runtime-real-install AS runtime-real
