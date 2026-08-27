# Installation

This page reads the install catalog published by the current Stable UCM
Release. Every Wheel, image, and Chart shown below is taken from that catalog;
combinations that were not published are omitted.

<div id="ucm-install-app" class="ucm-install" data-locale="en">
  <p class="ucm-install__status" data-install-status aria-live="polite">
    Loading the current Stable release catalog...
  </p>

  <section class="ucm-install__section" aria-labelledby="ucm-wheels-heading">
    <h2 id="ucm-wheels-heading">Python wheels</h2>
    <p>Choose a capability channel, then use its isolated pip Simple Index.</p>
    <div class="ucm-install__grid" data-install-wheels></div>
  </section>

  <section class="ucm-install__section" aria-labelledby="ucm-images-heading">
    <h2 id="ucm-images-heading">Runtime images</h2>
    <p>Pull an image that was published for the selected upstream runtime and platform.</p>
    <div class="ucm-install__grid" data-install-images></div>
  </section>

  <section class="ucm-install__section" aria-labelledby="ucm-chart-heading">
    <h2 id="ucm-chart-heading">Helm Chart</h2>
    <p>Install the Chart asset attached to the same Stable Release.</p>
    <div class="ucm-install__grid" data-install-chart></div>
  </section>

  <section class="ucm-install__section" aria-labelledby="ucm-source-heading">
    <h2 id="ucm-source-heading">Build from source</h2>
    <p>Check out the immutable source Tag associated with this catalog.</p>
    <div class="ucm-install__grid" data-install-source></div>
  </section>
</div>

<noscript>
  Enable JavaScript to load the install catalog and generate commands. Release
  assets remain available from the project Releases page.
</noscript>

## Next steps

- [vLLM on CUDA](quick_start/quickstart_vllm.md)
- [vLLM-Ascend on NPU](quick_start/quickstart_vllm_ascend.md)
- [Kubernetes deployment](frameworks/kubernetes.md)
- [Build from source](../../developer-guide/build_from_source.md)
