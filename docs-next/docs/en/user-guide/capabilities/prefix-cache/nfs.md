# NFS Store

Use this connector to save KV blocks through an existing filesystem path. The
path can be on a local disk or an NFS mount; UCM does not mount NFS or operate an
NFS server. Remote sharing therefore depends on the mount and permissions that
you provide to each serving process.

For the current vLLM integration, `UcmNfsStore` resolves through the V1 factory
to `UcmPcStoreV1` and the native `ucmpcstore` extension. The older
`ucm.store.nfsstore.nfsstore_connector.UcmNfsStore` is a separate API. Its
configuration names and defaults must not be used to interpret the V1 path.

## Choose the filesystem path

Choose `UcmNfsStore` when retaining an existing configuration for this direct
filesystem connector. For a new deployment requiring a host cache, Posix health
probes, and capacity-driven GC, follow [Pipeline Store](pipeline.md) and select
`Cache|Posix`. An NFS mount works with either choice; the mount type does not
select the UCM implementation.

The V1 NFS wrapper requires equal tensor sizes in `tensor_size_list`. It raises
`PcStore does not support different tensor sizes.` for a mixed-size layout.
The engine adapter supplies that list. Changing YAML values to conceal a layout
mismatch does not make the model compatible.

Install the matching UCM artifact from [Installation](../../installation.md), or
[build from source](../../../developer-guide/build_from_source.md) in the target
engine/device environment. `ucmpcstore` is built by `ucm/store/pcstore/CMakeLists.txt`
and uses UCM's device transfer implementation; no NFS-specific Python package is
required by this connector.

## Configure UCM for Prefix Caching

Prepare `/mnt/ucm-nfs` with read, write, directory traversal, and remove access
for the serving user. For an NFS deployment, verify the mount inside the same
container that will run inference before creating cache files.

```yaml
ucm_connectors:
  - ucm_connector_name: UcmNfsStore
    ucm_connector_config:
      storage_backends: /mnt/ucm-nfs
      io_direct: false
      timeout_ms: 30000
use_layerwise: true
enable_event_sync: true
enable_metrics: true
```

`storage_backends` is a path string in the vLLM YAML; the adapter converts it to
the list consumed by the Store. Keep backend settings inside
`ucm_connector_config`. Begin with one dedicated directory so that the writer
and reader use the same namespace during validation.

### Optional parameters

| Parameter | V1 default | Effect |
| --- | --- | --- |
| `io_direct` | `false` | Select direct filesystem I/O when supported by the mount and alignment. |
| `stream_number` | `8` | Number of device transfer streams. |
| `buffer_number` | `4096` | Number of intermediate transfer buffers. |
| `timeout_ms` | `30000` | Wait limit for Store transfers, in milliseconds. |
| `shard_data_dir` | `true` | Distribute cache files across subdirectories. |

The buffer count is not a GiB limit: memory consumption also depends on the
tensor transfer size. `cache_buffer_capacity_gb`, `posix_capacity_gb`, and
`store_health` configure Pipeline stages; this wrapper does not map those keys
to native PcStore options.

## Launching Inference

Follow the [vLLM quickstart](../../quick_start/quickstart_vllm.md) or
[vLLM-Ascend quickstart](../../quick_start/quickstart_vllm_ascend.md), keeping the
engine-facing `UCMConnector` selection and pointing `UCM_CONFIG_FILE` at this
YAML. The model, parallelism, and block size come from that engine setup.

Confirm that the logs select `UcmNfsStore` and initialize `ucmpcstore`. An HTTP
health check establishes that the model server is ready; it does not establish
that any KV block has reached NFS.

## Verify persistent reuse

Run the [external-cache check](../../quick_start/quickstart_vllm.md#verify-the-service-and-external-cache)
with an empty dedicated directory. Wait for writes to finish, retain the files,
restart the serving process, and replay the exact prompt and model geometry.
Require both external-cache hits and completed loads. If testing sharing across
hosts, repeat replay on the second host with the same model/configuration and
access to the same mount.

Distinguish these outcomes when diagnosing the result:

| Observation | Next check |
| --- | --- |
| `Failed to initialize ucmpcstore` | Native extension/device dependencies, path permissions, and the effective configuration. |
| Files appear on only one host | The actual container mounts and backing export; identical path strings may name different filesystems. |
| `PcStore does not support different tensor sizes.` | The engine's KV tensor layout and the selected connector. |
| Files exist but no external hits | Identical tokenized prefix, model revision, dtype, parallel layout, and completed blocks. |
| Transfer timeout | Mount accessibility and storage latency before increasing timeout or concurrency. |

For performance evaluation, separate a cold external read from the operating
system's page cache and engine memory cache. Report NFS traffic and external
hits alongside TTFT; replay latency alone cannot identify the cache layer used.

## Implementation references

- `ucm/store/factory_v1.py` maps the public connector name to `UcmPcStoreV1`.
- `ucm/store/pcstore/pcstore_connector_v1.py` defines the accepted key mapping and tensor-size constraint.
- `ucm/store/pcstore/cc/api/pcstore.h` defines native transfer defaults.
