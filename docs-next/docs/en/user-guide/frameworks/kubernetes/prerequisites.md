# Kubernetes prerequisites

Prepare the cluster and the site-specific values before installing the Chart.
The [Kubernetes overview](../kubernetes.md) explains the ownership model and
bundled deployment profiles; this page identifies the dependencies those
profiles assume.

## Cluster services

| Requirement | Why it is needed | How to adapt the Chart |
| --- | --- | --- |
| Kubernetes `>=1.19` and Helm 3 | Declared Chart runtime | Verify with `kubectl version` and `helm version` |
| kthena controller and CRDs | Reconciles `ModelServing` and creates the engine Pods | Install kthena before this Chart |
| kthena-router for PD or routed profiles | Resolves `ModelServer` and `ModelRoute` and accepts client traffic | Use the gateway from the existing kthena installation |
| Matching GPU/NPU driver and device plugin | Supplies the accelerator resources requested by the profile | Keep only resource keys that exist on the target nodes |
| Engine image and pull credentials | Runs vLLM with UCM integration for the selected platform | Set `images.image` or `modelSpec.image`, plus `imagePullSecret` when required |
| Model and UCM storage | Makes `modelPath` and cache backends visible inside the container | Replace the example NFS and StorageClass values |

The Chart does not bundle those services or CRDs. It only renders objects that
refer to them.

## Defaults that add dependencies

`servingEngineSpec.schedulerName` defaults to `volcano`. Install Volcano when
you keep that value. Setting it to an empty string removes the scheduler name
and lets Kubernetes use its default scheduler, but it does not preserve
Volcano's gang-scheduling behavior.

`servingEngineSpec.serviceMonitor.enabled` defaults to `true`. If the cluster
does not have the Prometheus Operator `ServiceMonitor` CRD, set it to `false`
before installation.

## Configuration dependencies

The bundled profiles show deployment shapes with resource examples and storage
placeholders. Configure the following in your `values-site.yaml`:

| Configuration | Required decision |
| --- | --- |
| Engine image | Select an image compatible with CUDA or Ascend; profiles do not choose one automatically. |
| Model identity | Set `modelSpec.modelPath` to a container-visible path and `modelName` to the API model name. |
| Model mount | Populate the empty `storage.extraStorage` list with a supported `hostPath`, PVC, CSI, or NFS source. |
| UCM storage | Replace the example `unifiedcacheStorage` StorageClass, access mode, and capacity. |
| Accelerator resources | Verify `nvidia.com/gpu` or `huawei.com/Ascend910`, RDMA, CPU, memory, runtime class, labels, and tolerations. |
| Scheduling and monitoring | Decide whether the cluster supplies Volcano and Prometheus Operator. |
| Network topology | Keep automatic interface detection only when it selects the correct fabric; otherwise set `nodeTopologyConfig` or `forceInterface`. |

The base Chart does not enable UCM by itself. UCM becomes effective when
`modelSpec.unifiedcacheConfig.config` is non-empty and its `enabled` switch is
not `false`. An effective UCM configuration also needs
`storage.unifiedcacheStorage` and at least one entry in `config.ucm_connectors`.

## Capacity, ports, and security

The Chart defaults to `hostNetwork: true`, `hostIPC: true`, and host port
`8000` for every vLLM container. Each engine Pod needs a node where port
`8000` is free, including when several releases share the cluster. Multi-node
and PD profiles therefore need enough eligible nodes for every entry and
worker Pod.

Profiles also request accelerator and RDMA resources and use a permissive
container security context. Confirm that the target nodes expose the requested
resources and that the cluster admission policy, including Pod Security
admission when enabled, accepts the rendered Pod specification.

## Before continuing

Confirm all of the following:

- the required kthena CRDs are installed;
- the scheduler and `ServiceMonitor` choices match installed cluster services;
- the engine image is pullable on the target architecture;
- the model and UCM storage paths are available;
- the requested device, RDMA, node, port, and security constraints can be satisfied.

Then continue to [Install and deploy](deploy.md).

## References

- [kthena documentation](https://kthena.volcano.sh/)
- [Volcano documentation](https://volcano.sh/)
