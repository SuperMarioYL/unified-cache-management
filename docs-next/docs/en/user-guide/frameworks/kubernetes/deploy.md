# Install and deploy with Helm

Complete the [Kubernetes prerequisites](prerequisites.md) before using these
commands. This workflow starts from a packaged Chart profile, renders the exact
objects for review, installs one model release, and then verifies the selected
request path.

## Get and unpack the Chart

Open the matching
[GitHub Release](https://github.com/ModelEngine-Group/unified-cache-management/releases),
copy the `unified-cache-chart` asset URL, and let Helm download and unpack it:

```bash
helm pull "<chart-url>" --untar
cd unified-cache-chart
```

When working from a source checkout instead, enter
`charts/unified-cache-chart` directly. Helm loads the Chart's root
`values.yaml` automatically. That file cannot be deployed by itself because
it does not define `modelSpec.roles[]`.

## Create site-specific values

Start from the smallest profile that matches your accelerator:

=== "CUDA"

    ```bash
    cp models/cuda/values-qwen3-0p6b-1e1.yaml values-site.yaml
    ```

=== "Ascend"

    ```bash
    cp models/ascend/values-qwen3-0p6b-1e1.yaml values-site.yaml
    ```

Edit `values-site.yaml` using the
[configuration dependency checklist](prerequisites.md#configuration-dependencies).
At minimum, replace the engine image, model path and mount, UCM storage,
accelerator/RDMA resources, and any scheduler, monitoring, or network values
that do not exist in your cluster.

Helm replaces list values instead of merging their individual elements. Edit
the copied profile as a complete deployment file rather than layering a
partial `roles[]` or storage list on top of it.

## Render and install

Set a release name and namespace, then validate the site values before
changing the cluster:

```bash
export UCM_RELEASE=qwen3
export UCM_NAMESPACE=ucm

helm lint --strict . -f values-site.yaml
helm template "$UCM_RELEASE" . \
  --namespace "$UCM_NAMESPACE" \
  -f values-site.yaml \
  > /tmp/ucm-rendered.yaml
```

Inspect `/tmp/ucm-rendered.yaml`. A non-PD profile should contain
`ModelServing`, a release Service, and the configured support resources. A PD
profile should additionally contain `ModelServer` and `ModelRoute`; bundled PD
profiles also contain a Mooncake master `Deployment` and `Service`.

When the namespace and required CRDs already exist, ask the Kubernetes API to
validate the rendered objects without persisting them:

```bash
kubectl apply --dry-run=server -f /tmp/ucm-rendered.yaml
```

Install only after the local render and optional server-side validation match
the intended platform and topology:

```bash
helm upgrade --install "$UCM_RELEASE" . \
  --namespace "$UCM_NAMESPACE" \
  --create-namespace \
  -f values-site.yaml
```

## Verify the release

Start with the resources owned or declared by the release:

```bash
kubectl -n "$UCM_NAMESPACE" get modelserving
kubectl -n "$UCM_NAMESPACE" get pod,pvc,service -o wide
```

For a PD profile, also inspect the routing declarations and Mooncake master:

```bash
kubectl -n "$UCM_NAMESPACE" get modelserver,modelroute
kubectl -n "$UCM_NAMESPACE" get deployment,service
```

### Single-node and multi-node access

For non-PD profiles, the Chart creates a Service named
`<release>-<modelSpec.name>`. Its Service port is `80` and its vLLM target port
is `8000`:

```bash
export UCM_MODEL_RESOURCE=qwen3-0p6b-1e1
export UCM_MODEL_NAME=Qwen3-0.6B

kubectl -n "$UCM_NAMESPACE" port-forward \
  "svc/${UCM_RELEASE}-${UCM_MODEL_RESOURCE}" 8000:80
```

In another terminal, check the API and send one request:

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/v1/models

curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"${UCM_MODEL_NAME}\",
    \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}],
    \"max_tokens\": 128
  }"
```

`UCM_MODEL_RESOURCE` is `modelSpec.name`; `UCM_MODEL_NAME` is
`modelSpec.modelName`.

### PD access

PD traffic must enter through the gateway exposed by the pre-installed
kthena-router. This Chart creates the `ModelServer` and `ModelRoute`
declarations but does not create or name that gateway Service. Use the
endpoint documented by your kthena installation.

The release's engine Service still exists in a PD deployment so
`ServiceMonitor` can discover entry Pods. It selects both prefill and decode
entries and is not the PD client traffic endpoint.

### Confirm the UCM runtime configuration

When UCM is enabled, inspect the runtime file in an engine Pod. For PD, select
a prefill Pod because the decode role uses only the transfer connector:

```bash
export UCM_ENGINE_POD="replace-with-engine-pod-name"

kubectl -n "$UCM_NAMESPACE" exec "$UCM_ENGINE_POD" -- \
  cat /vllm-workspace/UnifiedCache/config/ucm_config.runtime.yaml
```

These checks prove different things:

| Check | Evidence |
| --- | --- |
| `helm lint` and `helm template` | The values satisfy the Chart's local rendering contract. |
| `kubectl apply --dry-run=server` | The current Kubernetes API and installed CRDs accept the rendered objects. |
| Ready Pods, bound storage, and routing objects | kthena, scheduling, devices, images, and storage converged in this cluster. |
| A successful chat request | The selected access path and model work end to end. |

## Upgrade and uninstall

After changing `values-site.yaml`, rerun the render checks and apply the same
`helm upgrade --install` command.

Remove the release with:

```bash
helm uninstall "$UCM_RELEASE" --namespace "$UCM_NAMESPACE"
```

Helm requests deletion of the PVC and static PV objects created by this
release. For dynamically provisioned storage, the PV and stored data then
follow the PV or StorageClass reclaim policy. A PVC referenced through
`persistentVolumeClaim` is not created by this Chart and is therefore not
deleted with the release. Confirm the storage and backup policy before
uninstalling.

Return to the [Kubernetes overview](../kubernetes.md) to choose another profile
or deployment shape.
