# Kubernetes

The `unified-cache-chart` packages the configuration needed to run one
vLLM-UC model on an existing Kubernetes cluster. One Helm release owns one
model; deploy another release when you need another model.

Use this section in order:

1. Read this overview to choose a deployment profile and understand ownership.
2. Check the [cluster and configuration prerequisites](kubernetes/prerequisites.md).
3. Follow [Install and deploy](kubernetes/deploy.md) to render, install, and verify the release.

## How the Chart deploys a model

The inference-engine path is **kthena-only**. Helm creates a `ModelServing`
declaration, and the pre-installed kthena controller turns its roles into vLLM
entry and worker Pods. A PD deployment also creates `ModelServer` and
`ModelRoute` declarations so requests can enter through the cluster's
kthena-router.

The whole Chart is not CRD-only. Helm also creates native Kubernetes support
resources such as Services, ConfigMaps, Secrets, storage objects, and an
optional `ServiceMonitor`. A Mooncake PD profile can additionally create a
Mooncake master `Deployment` and `Service`.

[Open the architecture diagram at full size](../../../assets/images/kubernetes-helm-architecture.svg).
On narrow screens, scroll horizontally to inspect it without shrinking the labels.

<div style="overflow-x: auto; margin: 1.2rem 0;" markdown>

[![Helm renders one model declaration and its support resources, while kthena creates the vLLM role Pods.](../../../assets/images/kubernetes-helm-architecture.svg){ style="display: block; width: 100%; min-width: 680px; max-width: none; height: auto;" }](../../../assets/images/kubernetes-helm-architecture.svg)

</div>

The Chart does not install kthena, Volcano, device plugins, storage drivers,
or Prometheus Operator. The [prerequisites](kubernetes/prerequisites.md) page
identifies which of those services the selected profile and values require.

## Choose a deployment profile

The Chart archive includes 14 profiles under `models/`. A profile selects a
platform's device resources, a model, and a deployment shape by filling
`servingEngineSpec.modelSpec.roles[]` and, for PD, `modelSpec.pd`. It does not
select a matching engine image.

| Platform | Model | Included profiles |
| --- | --- | --- |
| CUDA | Qwen3-0.6B | single-node `1e1`, two-node `1e2`, PD `1p1-1d1`, `2p1-2d1`, and `2p2-2d2` |
| CUDA | DeepSeek-R1-AWQ | single-node and multi-node |
| Ascend | Qwen3-0.6B | single-node `1e1`, two-node `1e2`, PD `1p1-1d1`, `2p1-2d1`, and `2p2-2d2` |
| Ascend | DeepSeek-V3.1 | multi-node |
| Ascend | Qwen3-235B | multi-node |

The current Qwen profiles render these engine layouts when
`modelSpec.replicas` remains `1`:

| Shape | Role configuration | vLLM Pods | Request path |
| --- | --- | ---: | --- |
| `1e1` | one engine role, `replicas: 1`, `workerReplicas: 0` | 1 entry | release Service |
| `1e2` | one engine role, `replicas: 1`, `workerReplicas: 1` | 1 entry + 1 worker | release Service |
| `1p1-1d1` | one prefill and one decode instance, no workers | 2 | kthena-router |
| `2p1-2d1` | two prefill and two decode instances, no workers | 4 | kthena-router |
| `2p2-2d2` | two prefill and two decode instances, each with one worker | 8 | kthena-router |

All six bundled PD profiles currently create a Mooncake master and use
`MooncakeConnectorV1` with UCM. Other connector combinations are Chart
configuration capabilities, not packaged deployment profiles.

!!! important "Profiles are deployment examples"

    The bundled profiles are not portable, ready-to-run values. The combined
    base and profile configuration contains cluster-specific storage classes,
    NFS paths, RDMA resource names, resource requests, and scheduling
    assumptions. A successful Helm render does not prove GPU/NPU, RDMA,
    storage, Mooncake, or kthena cluster acceptance in your environment.

## Next steps

- [Prepare Kubernetes and the required external services](kubernetes/prerequisites.md)
- [Install, access, verify, upgrade, and uninstall the release](kubernetes/deploy.md)

## References

- [UCM GitHub Releases](https://github.com/ModelEngine-Group/unified-cache-management/releases)
- In the unpacked Chart: `README.md`, `values.yaml`, and the CUDA/Ascend files under `models/`
