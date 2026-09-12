---
hide:
  - toc
---

# Quickstart

Select the engine, device and backend, then choose a Wheel, image or Chart below and copy the complete command. The selector displays the artifact version and combinations available in its release manifest. Use a [source build](../../developer-guide/build_from_source.md) for customization or unavailable combinations.

For Wheel installation, use a fresh Python environment and install only one
backend extra. Backend extras share the same `ucm` import namespace.

<div id="ucm-install-app" class="ucm-install" data-locale="en">
  <p class="ucm-install__status" data-install-status aria-live="polite">
    Loading the current release manifest...
  </p>
  <div class="ucm-selector" data-install-selector></div>
  <section class="ucm-install__output" data-install-output aria-live="polite"></section>
</div>

<noscript>
  Enable JavaScript to load the release manifest and generate an install
  command.
</noscript>

## Configuration guidance

The engine tabs below cover installation, minimal configuration, startup and verification. To change backends or settings, use the guide for your task:

| Task | Documentation | Scope |
| --- | --- | --- |
| Choose and configure storage | Developer guide: [Cache Configuration](../../developer-guide/cache-configuration/index.md) | Backend selection, prerequisites and configuration methods |
| Look up a parameter | Reference: [Configuration Parameters](../../reference/config-parameters.md) | Field ownership, types, defaults and value constraints |

=== "vLLM (CUDA)"

    ## vLLM on CUDA {#vllm}

    This guide explains how to install UCM with vLLM on CUDA.

    ### UCM Installation {#vllm-ucm-installation}

    Choose one of the following installation methods.

    #### Option 1: Setup from docker {#vllm-docker}

    Get the image tag from [UCM Releases](https://github.com/ModelEngine-Group/unified-cache-management/releases). Replace `<image_tag>` with the full tag, including the engine and UCM versions.

    Run your container using the following command.
    ```bash
    # Use `--ipc=host` to make sure the shared memory is large enough.
    docker run --rm \
        --gpus all \
        --network=host \
        --ipc=host \
        -v <path_to_your_models>:/home/model \
        -v <path_to_your_storage>:/home/storage \
        --name <name_of_your_container> \
        -it ghcr.io/modelengine-group/vllm-openai:<image_tag>

    ```

    To build the UCM Docker image from source code, see [Building and Installing UCM from Source](../../developer-guide/build_from_source.md).

    #### Option 2: Install by pip {#vllm-wheel}

    Install `uc-manager` from [PyPI](https://pypi.org/project/uc-manager/):

    ```bash
    export PLATFORM=cuda
    pip install uc-manager
    ```

    The current PyPI version is 0.5.0. Its automatic patches support vLLM / vLLM-Ascend 0.11.0 and 0.18.0. PyPI provides a source package, so prepare the matching engine and platform build environment first.

    Create the configuration file below after installation. For a repository build, see [Build from source](../../developer-guide/build_from_source.md).

    ### Configure the cache {#vllm-configure-the-cache}

    Create a writable `/home/storage` directory, mounted persistently if running in
    a container. Save the following as `/etc/ucm/ucm.yaml`, readable by the engine:

    ```bash
    mkdir -p /etc/ucm /home/storage
    cat > /etc/ucm/ucm.yaml <<'YAML'
    ucm_connectors:
      - ucm_connector_name: UcmPipelineStore
        ucm_connector_config:
          store_pipeline: "Cache|Posix"
          storage_backends: /home/storage
          cache_buffer_capacity_gb: 4
          timeout_ms: 30000
          io_direct: false
    enable_metrics: true
    YAML
    ```

    The 4 GiB host cache is an explicit small-example setting, not the runtime
    default. Allow for one buffer per unshared worker, or shared-buffer allocation
    when the model uses it. `io_direct: false` keeps this initial filesystem check
    simple; select direct I/O and capacity settings for your actual storage using
    [Pipeline Store](../../developer-guide/cache-configuration/pipeline.md).

    ### Start the server {#vllm-start-the-server}

    Set `MODEL_ID` to your local model path or model repository ID. Adjust tensor
    parallelism and maximum model length to its hardware requirements.

    ```bash
    export MODEL_ID=/home/model/your-model
    export ENABLE_UCM_PATCH=1
    vllm serve "$MODEL_ID" \
      --served-model-name ucm-example \
      --tensor-parallel-size 1 \
      --max-model-len 4096 \
      --block-size 128 \
      --port 7800 \
      --enforce-eager \
      --kv-transfer-config '{
        "kv_connector": "UCMConnector",
        "kv_connector_module_path": "ucm.integration.vllm.ucm_connector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {"UCM_CONFIG_FILE": "/etc/ucm/ucm.yaml"}
      }'
    ```

    The runtime patch hook and connector are both required. Confirm the startup log
    contains `create UcmPipelineStore with config:` and the expected storage path.


    ### Verify the service and external cache {#vllm-verify-the-service-and-external-cache}

    A ready HTTP server confirms service startup; it does not prove a UCM cache hit.

    ```bash
    curl --fail http://127.0.0.1:7800/health
    curl --fail http://127.0.0.1:7800/v1/models
    ```

    In another host terminal, generate a repeatable prompt longer than several 128-token blocks, then send it:

    ```bash
    python3 - <<'PYREQUEST'
    import json
    from pathlib import Path
    Path('/tmp/ucm-request.json').write_text(json.dumps({
        "model": "ucm-example",
        "prompt": "Explain how a shared external cache reuses previous computation. " * 128,
        "max_tokens": 32,
        "temperature": 0,
    }))
    PYREQUEST
    curl --fail http://127.0.0.1:7800/v1/completions \
      -H 'Content-Type: application/json' --data-binary @/tmp/ucm-request.json
    curl --fail http://127.0.0.1:7800/metrics | grep '^ucm:'
    ```


    Wait for saves to complete, retain the test cache, restart with the same settings and replay the request. Check external hits, completed loads and correct output using [external-cache verification](../observability/verify-cache.md).

    For missing hits or failed loads, check configuration, storage and memory budgets through [troubleshooting](../../reference/troubleshooting.md).

    ### Use other models {#vllm-use-other-models}

    Keep model-specific engine settings from [Model Tour](../model-tour/index.md), then add the UCM Connector configuration above.

=== "vLLM-Ascend (NPU)"

    ## vLLM-Ascend on NPU {#vllm-ascend}

    This guide explains how to install UCM with vLLM-Ascend on Ascend.

    ### UCM Installation {#vllm-ascend-ucm-installation}

    Choose one of the following installation methods.

    #### Option 1: Install by pip {#vllm-ascend-wheel}

    Install `uc-manager` from [PyPI](https://pypi.org/project/uc-manager/):

    ```bash
    export PLATFORM=ascend
    pip install uc-manager
    ```

    The current PyPI version is 0.5.0. Its automatic patches support vLLM / vLLM-Ascend 0.11.0 and 0.18.0. PyPI provides a source package, so prepare the matching engine and platform build environment first.

    Create the configuration file below after installation. For a repository build, see [Build from source](../../developer-guide/build_from_source.md).

    #### Option 2: Setup from docker {#vllm-ascend-docker}

    ##### Pre-built image {#vllm-ascend-pre-built-image}

    Get the image tag from [UCM Releases](https://github.com/ModelEngine-Group/unified-cache-management/releases). Replace `<image_tag>` with the full tag matching your device and CANN version.

    ```bash
    # Update DEVICE according to your device (/dev/davinci[0-7])
    export DEVICE=/dev/davinci7
    # Update the vllm-ascend image
    docker run --rm \
        --network=host \
        --device $DEVICE \
        --device /dev/davinci_manager \
        --device /dev/devmm_svm \
        --device /dev/hisi_hdc \
        -v /usr/local/dcmi:/usr/local/dcmi \
        -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
        -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
        -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
        -v /etc/ascend_install.info:/etc/ascend_install.info \
        -v /root/.cache:/root/.cache \
        -v <path_to_your_models>:/app/model \
        -v <path_to_your_storage>:/app/storage \
        --name <name_of_your_container> \
        -it ghcr.io/modelengine-group/vllm-ascend:<image_tag> bash
    ```

    ### Configure the cache {#vllm-ascend-configure-the-cache}

    Create a writable `/app/storage` directory, mounted persistently if running in
    a container. Save the following as `/etc/ucm/ucm.yaml`, readable by the engine:

    ```bash
    mkdir -p /etc/ucm /app/storage
    cat > /etc/ucm/ucm.yaml <<'YAML'
    ucm_connectors:
      - ucm_connector_name: UcmPipelineStore
        ucm_connector_config:
          store_pipeline: "Cache|Posix"
          storage_backends: /app/storage
          cache_buffer_capacity_gb: 4
          timeout_ms: 30000
          io_direct: false
    enable_metrics: true
    YAML
    ```

    The 4 GiB host cache is an explicit small-example setting, not the runtime
    default. Allow for one buffer per unshared worker, or shared-buffer allocation
    when the model uses it. `io_direct: false` keeps this initial filesystem check
    simple; select direct I/O and capacity settings for your actual storage using
    [Pipeline Store](../../developer-guide/cache-configuration/pipeline.md).

    ### Start the server {#vllm-ascend-start-the-server}

    Set `MODEL_ID` to your local model path or model repository ID. Adjust tensor
    parallelism and maximum model length to its hardware requirements.

    ```bash
    export MODEL_ID=/app/model/your-model
    export ENABLE_UCM_PATCH=1
    vllm serve "$MODEL_ID" \
      --served-model-name ucm-example \
      --tensor-parallel-size 1 \
      --max-model-len 4096 \
      --block-size 128 \
      --port 7800 \
      --enforce-eager \
      --kv-transfer-config '{
        "kv_connector": "UCMConnector",
        "kv_connector_module_path": "ucm.integration.vllm.ucm_connector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {"UCM_CONFIG_FILE": "/etc/ucm/ucm.yaml"}
      }'
    ```

    The runtime patch hook and connector are both required. Confirm the startup log
    contains `create UcmPipelineStore with config:` and the expected storage path.


    ### Verify the service and external cache {#vllm-ascend-verify-the-service-and-external-cache}

    A ready HTTP server confirms service startup; it does not prove a UCM cache hit.

    ```bash
    curl --fail http://127.0.0.1:7800/health
    curl --fail http://127.0.0.1:7800/v1/models
    ```

    In another host terminal, generate a repeatable prompt longer than several 128-token blocks, then send it:

    ```bash
    python3 - <<'PYREQUEST'
    import json
    from pathlib import Path
    Path('/tmp/ucm-request.json').write_text(json.dumps({
        "model": "ucm-example",
        "prompt": "Explain how a shared external cache reuses previous computation. " * 128,
        "max_tokens": 32,
        "temperature": 0,
    }))
    PYREQUEST
    curl --fail http://127.0.0.1:7800/v1/completions \
      -H 'Content-Type: application/json' --data-binary @/tmp/ucm-request.json
    curl --fail http://127.0.0.1:7800/metrics | grep '^ucm:'
    ```


    Wait for saves to complete, retain the test cache, restart with the same settings and replay the request. Check external hits, completed loads and correct output using [external-cache verification](../observability/verify-cache.md).

    For missing hits or failed loads, check configuration, storage and memory budgets through [troubleshooting](../../reference/troubleshooting.md).

    ### Use other models {#vllm-ascend-use-other-models}

    Keep model-specific engine settings from [Model Tour](../model-tour/index.md), then add the UCM Connector configuration above.

=== "SGLang (CUDA)"

    ## SGLang on CUDA {#sglang}

    This guide explains how to install UCM with SGLang on CUDA.

    ### UCM Installation {#sglang-ucm-installation}

    #### Option 1: Setup from docker {#sglang-docker}

    ##### SGLang image {#sglang-sglang-image}

    The current [UCM Release](https://github.com/ModelEngine-Group/unified-cache-management/releases) does not provide a SGLang image. Use the official SGLang image below, then install UCM from PyPI inside the container.

    ```bash
    docker pull lmsysorg/sglang:v0.5.9
    ```

    Then run your container using following command.
    ```bash
    # Use `--ipc=host` to make sure the shared memory is large enough.
    docker run --rm \
        --gpus all \
        --network=host \
        --ipc=host \
        -v <path_to_your_models>:/home/model \
        -v <path_to_your_storage>:/home/storage \
        --name <name_of_your_container> \
        -it lmsysorg/sglang:v0.5.9
    ```

    To build the UCM Docker image from source code, see [Building and Installing UCM from Source](../../developer-guide/build_from_source.md).

    #### Option 2: Install by pip {#sglang-wheel}

    Install `uc-manager` from [PyPI](https://pypi.org/project/uc-manager/):

    ```bash
    export PLATFORM=cuda
    pip install uc-manager
    ```

    Prepare SGLang 0.5.9 first. PyPI currently provides UCM 0.5.0 as a source package, which requires a CUDA build environment.

    Create the configuration file below after installation. For a repository build, see [Build from source](../../developer-guide/build_from_source.md).

    ### Configure HiCache {#sglang-configure-hicache}

    Create a writable, persistent `/home/storage` directory. The adapter requires
    `page_first` host layout and `interface_v1`; do not change them to the older
    copy-based storage API.

    ```bash
    export MODEL_ID=/home/model/your-model
    HICACHE_CONFIG='{
      "backend_name": "unifiedcache",
      "module_path": "ucm.integration.sglang.unifiedcache_store",
      "class_name": "UnifiedCacheStore",
      "interface_v1": 1,
      "kv_connector_extra_config": {
        "ucm_connector_name": "UcmPipelineStore",
        "ucm_connector_config": {
          "storage_backends": "/home/storage",
          "io_direct": false,
          "timeout_ms": 30000
        }
      }
    }'
    ```

    ### Start the online server {#sglang-online-inference}

    ```bash
    python3 -m sglang.launch_server \
      --model-path "$MODEL_ID" \
      --served-model-name ucm-example \
      --tensor-parallel-size 1 \
      --page-size 128 \
      --port 7800 \
      --enable-hierarchical-cache \
      --hicache-mem-layout page_first \
      --hicache-write-policy write_through \
      --hicache-storage-backend dynamic \
      --hicache-storage-prefetch-policy wait_complete \
      --hicache-storage-backend-extra-config "$HICACHE_CONFIG"
    ```

    Adjust the model and parallelism to the available GPUs. The adapter supplies
    block geometry and selects `Posix`; a vLLM YAML file with `ucm_connectors` is not
    the same configuration interface.

    ### Verify the service and external cache {#sglang-verify-the-service-and-external-cache}

    ```bash
    curl --fail http://127.0.0.1:7800/health
    curl --fail http://127.0.0.1:7800/v1/models
    python3 - <<'PYREQUEST'
    import json
    from pathlib import Path
    Path('/tmp/ucm-request.json').write_text(json.dumps({
        "model": "ucm-example",
        "prompt": "Explain how a shared external cache reuses previous computation. " * 128,
        "max_tokens": 32,
        "temperature": 0,
    }))
    PYREQUEST
    curl --fail http://127.0.0.1:7800/v1/completions \
      -H 'Content-Type: application/json' --data-binary @/tmp/ucm-request.json
    ```

    Wait for HiCache write-through tasks to finish and inspect persistent KV block
    files in the configured directory, alongside the engine's storage write logs.
    Stop the server normally, retain the directory, and restart with identical
    model, tokenizer, page size, and parallelism. Replay the same request and
    confirm HiCache reports storage-prefetched tokens or completed UCM storage
    reads. This separates external storage reuse from an in-process HiCache hit.
    The `clear()` method is not implemented by this UCM adapter; restarting is the
    appropriate way to clear volatile state for this check.

    If no storage read is reported, inspect HiCache prefetch/write errors, directory
    permissions, and prompt length. Do not use shorter latency as the only cache
    verification. See [Troubleshooting](../../reference/troubleshooting.md) and
    [Pipeline Store](../../developer-guide/cache-configuration/pipeline.md). UCM's vLLM `/metrics`
    examples are not a promise that SGLang exposes the same metric names.

    Stop the service after the check. Keep or remove only its dedicated test-cache
    directory according to your storage policy.

    ### Offline batch inference {#sglang-offline-inference}

    Use SGLang's [v0.5.9 offline example](https://github.com/sgl-project/sglang/blob/v0.5.9/examples/runtime/engine/offline_batch_inference.py) in the same environment. Its `ServerArgs` accepts the same HiCache settings as the server. Stop the online service using these devices, then run in the shell where `MODEL_ID` and `HICACHE_CONFIG` were defined:

    ```bash
    curl --fail --location \
      https://raw.githubusercontent.com/sgl-project/sglang/v0.5.9/examples/runtime/engine/offline_batch_inference.py \
      --output /tmp/sglang-offline-batch.py
    python3 /tmp/sglang-offline-batch.py \
      --model-path "$MODEL_ID" \
      --tensor-parallel-size 1 --page-size 128 \
      --enable-hierarchical-cache \
      --hicache-mem-layout page_first \
      --hicache-write-policy write_through \
      --hicache-storage-backend dynamic \
      --hicache-storage-prefetch-policy wait_complete \
      --hicache-storage-backend-extra-config "$HICACHE_CONFIG"
    ```

    The example prints generated text for each prompt. Its built-in short prompts check offline invocation; replace them with repeated long prefixes spanning complete blocks when verifying external storage I/O.

=== "MindIE (NPU)"

    ## Quickstart-MindIE-LLM {#mindie}

    This guide shows how to install UCM with MindIE-LLM support, patch the required MindIE-LLM Python modules, configure `mindie_llm`, and launch `mindie_llm_server` with UCM as the KV cache backend on Ascend.

    ### Prerequisites {#mindie-prerequisites}

    - MindIE-LLM 2.3.0
    - Python >= 3.10
    - Ascend runtime/toolkit installed and available in the environment. For details, refer to the [MindIE-LLM official documentation](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/quick_start/quick_start.md).

    ### Step 1: Install UCM {#mindie-step-1-install-ucm}

    Install UCM from [PyPI](https://pypi.org/project/uc-manager/) in the MindIE-LLM environment:

    ```bash
    export PLATFORM=ascend
    export UCM_ENABLE_MINDIE=1
    export UCM_CXX11_ABI=1
    pip install uc-manager
    ```

    `UCM_CXX11_ABI` must match MindIE/PyTorch; adjust the example value `1` for your environment. PyPI currently provides the 0.5.0 source package, which needs the corresponding build environment. For a repository build, see [Build from source](../../developer-guide/build_from_source.md#mindie-llm-ascend-platform).

    ### Step 2: Prepare the UCM configuration {#mindie-step-2-prepare-the-ucm-configuration}

    UCM for MindIE-LLM provides Prefix Cache integration through patched MindIE-LLM modules. You can use the packaged config at `ucm/integration/mindie/ucm_config.json` or provide your own config file.

    Minimal example:

    ```json
    {
      "storage_backends": ["/path/to/kvcache"],
      "mindie_config_path": "/usr/local/lib/python3.11/site-packages/mindie_llm/conf/config.json",
      "block_elem_size": 2
    }
    ```

    Save this file as `ucm_config.json` and replace the example paths with paths from your environment.

    Key notes:

    * By default, the patch is applied when `mindie_llm` is first imported after UCM is installed.
    * The hook copies the patched `uc_utils.py`, `unifiedcache_mempool.py`, and `prefix_cache_plugin.py` files into the installed `mindie_llm` package.
    * A MindIE-specific image may apply the patch during image build; verify its startup behavior rather than assuming an arbitrary UCM image contains it.

    ### Step 3: Launch MindIE-LLM with UCM {#mindie-step-3-launch-mindie-llm-with-ucm}

    Locate the installed `mindie_llm` package:

    ```bash
    python -c "import mindie_llm, os; print(os.path.dirname(mindie_llm.__file__))"
    ```

    Then locate `conf/config.json` under that directory.

    Ensure the service user can read the configuration file. For example:

    ```bash
    chmod 640 <path-to-mindie_llm>/conf/config.json
    ```

    In `config.json`, add or update the `kvPoolConfig` section under `BackendConfig`:

    ```json
    "BackendConfig": {
      "kvPoolConfig": {
        "backend": "unifiedcache",
        "configPath": "/path/to/your/ucm_config.json",
        "asyncWrite": true
      }
    }
    ```

    Update `config.json` with the service IP, port, model path, and any other required MindIE-LLM settings.

    Run `mindie_llm_server` to start the service.

    If the following message is displayed, the service has started successfully:

    ```bash
    Daemon start success!
    ```

    After startup, inspect the MindIE-LLM logs to confirm that the `unifiedcache` backend is loaded successfully.

    ### Verify the service and external cache {#mindie-verify-the-service-and-external-cache}

    Confirm the log contains `[UC]: Initialize unifiedcache success.` and the
    expected backend path. Query `/v1/models` on the configured MindIE service
    address using the TLS and authentication settings from your service config,
    then send a completion request through its supported inference endpoint.

    For a cache check, use an identical prompt longer than several configured KV
    blocks. Wait for asynchronous writes to finish and verify persistent KV files
    in the storage directory. Stop the service normally, preserve storage, and
    restart with the same model and cache geometry. Send the same prompt again.
    Set `MINDIE_UC_TIME_STAT=1` before starting a diagnostic run to expose the
    adapter's timed operations; confirm successful `put` activity on the first run
    and `get` activity on the second, with no dump/load exceptions. This checks
    external reuse separately from the engine's in-memory prefix cache.

    A successful health response or faster second request is insufficient by itself.
    Use MindIE's service logs and cache statistics alongside the UCM operation logs;
    the vLLM Prometheus metric examples do not describe a MindIE endpoint. See
    [Troubleshooting](../../reference/troubleshooting.md). Stop the test service
    when finished and retain or remove only its dedicated test-cache directory.

    ### Troubleshooting {#mindie-troubleshooting}

    #### MindIE-LLM code is not patched {#mindie-mindie-llm-code-is-not-patched}

    * Confirm that `UCM_ENABLE_MINDIE=1` was set before installation.
    * Confirm that `UCM_CXX11_ABI=0` or `1` was set correctly before installation.
    * Reinstall UCM.
    * Verify that `mindie_llm` is already installed.

    #### `configPath` not found {#mindie-configpath-not-found}

    * Use an absolute path.
    * Ensure the file is readable by the service user.

    #### Service starts but the UCM backend is not enabled {#mindie-service-starts-but-the-ucm-backend-is-not-enabled}

    * Recheck `BackendConfig.kvPoolConfig`.
    * Inspect MindIE-LLM startup logs.

## Next steps

- [Helm deployment](../frameworks/kubernetes/deploy.md)
- [Build from source](../../developer-guide/build_from_source.md)
