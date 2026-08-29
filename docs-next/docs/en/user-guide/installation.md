# Installation

Choose only from artifacts published for this documentation version. Changing
an option selects the first valid published combination and produces one exact
install command. Use the site version menu to switch UCM releases.

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

## Next steps

- [vLLM on CUDA](quick_start/quickstart_vllm.md)
- [vLLM-Ascend on NPU](quick_start/quickstart_vllm_ascend.md)
- [Kubernetes deployment](frameworks/kubernetes.md)
- [Build from source](../../developer-guide/build_from_source.md)
