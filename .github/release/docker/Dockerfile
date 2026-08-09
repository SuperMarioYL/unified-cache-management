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
ARG UCM_RELEASE_PROFILE
ARG UCM_RELEASE_SOURCE_SHA
ARG UCM_RELEASE_VERSION
ARG UCM_RELEASE_BUILD_KEY
ARG UCM_RELEASE_REQUIRED_TARGETS
ARG UCM_RELEASE_FORBIDDEN_TARGETS
ARG BUILD_VERSION
ARG BUILD_FILENAME
ARG BUILD_SHA256
ARG PYPROJECT_HOOKS_VERSION
ARG PYPROJECT_HOOKS_FILENAME
ARG PYPROJECT_HOOKS_SHA256
ARG PACKAGING_VERSION
ARG PACKAGING_FILENAME
ARG PACKAGING_SHA256
ARG SETUPTOOLS_VERSION
ARG SETUPTOOLS_FILENAME
ARG SETUPTOOLS_SHA256
ARG WHEEL_VERSION
ARG WHEEL_FILENAME
ARG WHEEL_SHA256
ARG PYYAML_VERSION
ARG PYYAML_FILENAME
ARG PYYAML_SHA256
ARG CMAKE_VERSION
ARG CMAKE_FILENAME
ARG CMAKE_SHA256

SHELL ["/bin/bash", "-euxo", "pipefail", "-c"]
ENV TZ=UTC PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=30 PIP_RETRIES=2 \
    PYTHONDONTWRITEBYTECODE=1
ARG LD_LIBRARY_PATH
ENV LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
RUN ldconfig /usr/local/lib

RUN if [[ -x /opt/python/cp312-cp312/bin/python ]]; then \
      ln -s /opt/python/cp312-cp312/bin/python /usr/local/bin/ucm-python; \
    elif command -v python3.12 >/dev/null; then \
      ln -s "$(command -v python3.12)" /usr/local/bin/ucm-python; \
    elif command -v python3 >/dev/null && [[ "$(python3 -c 'import sys; print(sys.version_info[:2])')" == "(3, 12)" ]]; then \
      ln -s "$(command -v python3)" /usr/local/bin/ucm-python; \
    else \
      echo 'immutable builder lacks CPython 3.12' >&2; exit 1; \
    fi

RUN <<EOF
test "${UCM_BUILDER_IMAGE}" != "${UCM_BUILDER_IMAGE%@sha256:*}"
mkdir -p /wheelhouse
ucm-python -m pip download --dest /wheelhouse --no-deps --only-binary=:all: \
  "build==${BUILD_VERSION}" \
  "pyproject-hooks==${PYPROJECT_HOOKS_VERSION}" \
  "packaging==${PACKAGING_VERSION}" \
  "setuptools==${SETUPTOOLS_VERSION}" \
  "wheel==${WHEEL_VERSION}" \
  "PyYAML==${PYYAML_VERSION}" \
  "cmake==${CMAKE_VERSION}"
check_wheel() {
  local filename="$1" expected="${2#sha256:}" actual
  test -f "/wheelhouse/${filename}"
  actual="$(sha256sum "/wheelhouse/${filename}" | cut -d' ' -f1)"
  test "${actual}" = "${expected}"
}
check_wheel "${BUILD_FILENAME}" "${BUILD_SHA256}"
check_wheel "${PYPROJECT_HOOKS_FILENAME}" "${PYPROJECT_HOOKS_SHA256}"
check_wheel "${PACKAGING_FILENAME}" "${PACKAGING_SHA256}"
check_wheel "${SETUPTOOLS_FILENAME}" "${SETUPTOOLS_SHA256}"
check_wheel "${WHEEL_FILENAME}" "${WHEEL_SHA256}"
check_wheel "${PYYAML_FILENAME}" "${PYYAML_SHA256}"
check_wheel "${CMAKE_FILENAME}" "${CMAKE_SHA256}"
cat > /tmp/requirements.lock <<LOCK
build==${BUILD_VERSION} --hash=${BUILD_SHA256}
pyproject-hooks==${PYPROJECT_HOOKS_VERSION} --hash=${PYPROJECT_HOOKS_SHA256}
packaging==${PACKAGING_VERSION} --hash=${PACKAGING_SHA256}
setuptools==${SETUPTOOLS_VERSION} --hash=${SETUPTOOLS_SHA256}
wheel==${WHEEL_VERSION} --hash=${WHEEL_SHA256}
PyYAML==${PYYAML_VERSION} --hash=${PYYAML_SHA256}
cmake==${CMAKE_VERSION} --hash=${CMAKE_SHA256}
LOCK
ucm-python -m pip install --no-index --find-links=/wheelhouse --require-hashes \
  --only-binary=:all: -r /tmp/requirements.lock
EOF

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

FROM wheel-base AS wheel-build

RUN <<'EOF'
test -n "${TARGETARCH}"
test -n "${PLATFORM}"
test -n "${UCM_RELEASE_PROFILE}"
test -n "${UCM_RELEASE_SOURCE_SHA}"
test -n "${UCM_RELEASE_VERSION}"
test -n "${UCM_RELEASE_BUILD_KEY}"
test -n "${UCM_RELEASE_REQUIRED_TARGETS}"
test -n "${UCM_RELEASE_FORBIDDEN_TARGETS}"
test "$(ucm-python -c 'import sysconfig; print(sysconfig.get_config_var("SOABI").split("-")[0])')" = cpython
test "$(ucm-python -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")')" = cp312
locked_cmake="$(ucm-python -c 'import sysconfig; print(sysconfig.get_path("scripts"))')/cmake"
test -x "${locked_cmake}"
test "$("${locked_cmake}" --version | awk 'NR == 1 {print $3}')" = "${CMAKE_VERSION}"
command -v git
command -v g++
command -v make
command -v readelf
case "${TARGETARCH}:$(uname -m)" in
  amd64:x86_64|arm64:aarch64) ;;
  *) echo "TARGETARCH does not match native immutable root" >&2; exit 1 ;;
esac
case "${PLATFORM}" in
  cuda)
    command -v nvcc
    nvcc --version | grep -F 'release 13.0, V13.0.88'
    test -f /usr/local/cuda/include/cuda.h
    ;;
  ascend|ascend-a3)
    test -f /usr/local/Ascend/ascend-toolkit/latest/include/acl/acl.h
    test -f /usr/local/Ascend/ascend-toolkit/latest/lib64/libascendcl.so
    test -d /vllm-workspace/Mooncake/mooncake-store/include
    test -f /usr/local/include/transfer_engine.h
    test -f /usr/local/lib/libmooncake_store.so
    readelf -h -d /usr/local/lib/libmooncake_store.so
    ! ldd /usr/local/lib/libmooncake_store.so | grep -F 'not found'
    ;;
  *) echo "unsupported release PLATFORM=${PLATFORM}" >&2; exit 1 ;;
esac
EOF

RUN PYTHONPATH=/workspace/ucm/.github/release \
    ucm-python -m ucm_release wheel authority \
      --spec-id "${UCM_RELEASE_PROFILE}-${TARGETARCH}" \
      --source-sha "${UCM_RELEASE_SOURCE_SHA}" \
      --source-date-epoch "${SOURCE_DATE_EPOCH}" \
      --builder-coordinate "${UCM_BUILDER_IMAGE}" \
      --wheelhouse /wheelhouse \
      --source-archive /tmp/ucm-source.tar \
      --source-commit-payload /tmp/source-commit.payload \
      --source-manifest /tmp/source-context.json \
      --source-root /workspace/ucm \
      --output /tmp/build-authority.json

RUN PATH="$(ucm-python -c 'import sysconfig; print(sysconfig.get_path("scripts"))'):${PATH}" \
    UCM_RELEASE_BUILD=1 \
    PLATFORM="${PLATFORM}" \
    UCM_RELEASE_PROFILE="${UCM_RELEASE_PROFILE}" \
    UCM_RELEASE_SOURCE_SHA="${UCM_RELEASE_SOURCE_SHA}" \
    UCM_RELEASE_VERSION="${UCM_RELEASE_VERSION}" \
    UCM_RELEASE_BUILD_KEY="${UCM_RELEASE_BUILD_KEY}" \
    UCM_RELEASE_REQUIRED_TARGETS="${UCM_RELEASE_REQUIRED_TARGETS}" \
    UCM_RELEASE_FORBIDDEN_TARGETS="${UCM_RELEASE_FORBIDDEN_TARGETS}" \
    UCM_RELEASE_AUTHORITY_FILE=/tmp/build-authority.json \
    SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}" \
    ucm-python -m build --no-isolation --wheel --outdir /tmp/raw-wheel

RUN PYTHONPATH=/workspace/ucm/.github/release \
    ucm-python -m ucm_release wheel closure /tmp/raw-wheel/*.whl \
      --spec-id "${UCM_RELEASE_PROFILE}-${TARGETARCH}" \
      --authority-file /tmp/build-authority.json \
      --output /tmp/dependency-closure.json

RUN PYTHONPATH=/workspace/ucm/.github/release \
    ucm-python -m ucm_release wheel seal /tmp/raw-wheel/*.whl \
      --spec-id "${UCM_RELEASE_PROFILE}-${TARGETARCH}" \
      --source-sha "${UCM_RELEASE_SOURCE_SHA}" \
      --build-key "${UCM_RELEASE_BUILD_KEY}" \
      --source-date-epoch "${SOURCE_DATE_EPOCH}" \
      --authority-file /tmp/build-authority.json \
      --dependency-closure /tmp/dependency-closure.json \
      --output-dir /out

FROM wheel-build AS wheel-cuda-build
RUN test "${PLATFORM}" = cuda

FROM scratch AS wheel-cuda
COPY --from=wheel-cuda-build /out /

FROM wheel-build AS wheel-ascend-build
RUN test "${PLATFORM}" = ascend -o "${PLATFORM}" = ascend-a3

FROM scratch AS wheel-ascend
COPY --from=wheel-ascend-build /out /

FROM ${BASE_IMAGE} AS runtime-install

ARG BASE_IMAGE
ARG SOURCE_DATE_EPOCH
ARG TARGETPLATFORM
ARG UCM_WHEEL

ARG LD_LIBRARY_PATH
ENV LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
RUN ldconfig /usr/local/lib

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

FROM runtime-install AS runtime

FROM ${BASE_IMAGE} AS runtime-real-install

ARG BASE_IMAGE
ARG SOURCE_DATE_EPOCH
ARG TARGETPLATFORM
ARG UCM_WHEEL
ARG WRAPT_WHEEL
ARG UCM_SOURCE_SHA
ARG UCM_SOURCE_TREE
ARG UCM_SOURCE_CONTEXT_SHA256
ARG UCM_TASK_SHA256
ARG UCM_BUILD_KEY_SHA256
ARG UCM_WHEEL_SHA256
ARG UCM_RECIPE_SHA256

ARG LD_LIBRARY_PATH
ENV LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
RUN ldconfig /usr/local/lib

LABEL org.opencontainers.image.revision="${UCM_SOURCE_SHA}" \
      io.ucm.release.source-tree="${UCM_SOURCE_TREE}" \
      io.ucm.release.source-context-sha256="${UCM_SOURCE_CONTEXT_SHA256}" \
      io.ucm.release.task-sha256="${UCM_TASK_SHA256}" \
      io.ucm.release.build-key-sha256="${UCM_BUILD_KEY_SHA256}" \
      io.ucm.release.wheel-sha256="${UCM_WHEEL_SHA256}" \
      io.ucm.release.recipe-sha256="${UCM_RECIPE_SHA256}"

COPY image-recipe.json /usr/local/share/ucm-release/image-recipe.json
COPY image-authority.json /usr/local/share/ucm-release/image-authority.json
COPY verify_base_image.py /usr/local/bin/verify_base_image.py
COPY install_ucm.py /usr/local/bin/install_ucm.py
COPY inspect_runtime.py /usr/local/bin/inspect_runtime.py

RUN python /usr/local/bin/verify_base_image.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --base-image "${BASE_IMAGE}" \
      --target-platform "${TARGETPLATFORM}" \
      --output /usr/local/share/ucm-release/base-verification.json
RUN --mount=type=bind,source=${UCM_WHEEL},target=/wheelhouse/${UCM_WHEEL},readonly \
    --mount=type=bind,source=${WRAPT_WHEEL},target=/wheelhouse/${WRAPT_WHEEL},readonly \
    --mount=type=bind,source=requirements.lock,target=/wheelhouse/requirements.lock,readonly \
    python /usr/local/bin/install_ucm.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --authority /usr/local/share/ucm-release/image-authority.json \
      --wheelhouse /wheelhouse \
      --lock /wheelhouse/requirements.lock \
      --output /usr/local/share/ucm-release/install-result.json
RUN python /usr/local/bin/inspect_runtime.py \
      --recipe /usr/local/share/ucm-release/image-recipe.json \
      --install-result /usr/local/share/ucm-release/install-result.json \
      --output /usr/local/share/ucm-release/runtime-inspection.json

FROM runtime-real-install AS runtime-real
