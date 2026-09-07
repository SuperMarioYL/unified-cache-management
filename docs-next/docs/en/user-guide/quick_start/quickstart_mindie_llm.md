# Quickstart-MindIE-LLM

This guide shows how to install UCM with MindIE-LLM support, patch the required MindIE-LLM Python modules, configure `mindie_llm`, and launch `mindie_llm_server` with UCM as the KV cache backend on Ascend.

## Prerequisites

- MindIE-LLM 2.3.0
- Python >= 3.10
- Ascend runtime/toolkit installed and available in the environment. For details, refer to the [MindIE-LLM official documentation](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/quick_start/quick_start.md).

## Step 1: Install UCM

Check [Installation](../installation.md) for an explicitly published MindIE
artifact. If none is listed, follow the MindIE-LLM section of
[Build from source](../../developer-guide/build_from_source.md#mindie-llm-ascend-platform)
and set `UCM_ENABLE_MINDIE=1` and the matching `UCM_CXX11_ABI` before the build.
Generic vLLM wheels/images do not establish MindIE integration support.

If you want to build UCM from source code (e.g. for development or customization), see [Building and Installing UCM from Source](../../developer-guide/build_from_source.md).

## Step 2: Prepare the UCM configuration

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

## Step 3: Launch MindIE-LLM with UCM

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

## Verify the service and external cache

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

## Troubleshooting

### MindIE-LLM code is not patched

* Confirm that `UCM_ENABLE_MINDIE=1` was set before installation.
* Confirm that `UCM_CXX11_ABI=0` or `1` was set correctly before installation.
* Reinstall UCM.
* Verify that `mindie_llm` is already installed.

### `configPath` not found

* Use an absolute path.
* Ensure the file is readable by the service user.

### Service starts but the UCM backend is not enabled

* Recheck `BackendConfig.kvPoolConfig`.
* Inspect MindIE-LLM startup logs.
