const assert = require("node:assert/strict");
const test = require("node:test");

const Manifest = require("../docs/assets/manifest.js");
const Selector = require("../docs/assets/install.js");
const Download = require("../docs/assets/download.js");

assert.deepEqual(Selector.ROW_ORDER.slice(0, 2), ["engine", "method"]);

function publication(reference, architectures) {
  return {
    pull: reference,
    multi_arch: architectures.length > 1,
    members: architectures.map((architecture) => ({
      architecture,
      reference:
        architectures.length === 1 ? reference : reference + "-" + architecture,
    })),
  };
}

function wheel({ id, product, channel, runtime, variant, soc, architecture }) {
  const local = channel.replace("-", ".");
  return {
    id,
    product,
    channel,
    accelerator: { runtime, variant, soc_version: soc },
    distribution: "uc-manager",
    version: "0.9.0+" + local,
    python_abi: "cp312",
    architecture,
    filename: id + ".whl",
    url: "https://github.com/example/ucm/releases/download/v0.9.0/" + id + ".whl",
    sha256: "a".repeat(64),
    dependencies: ["wrapt==1.17.2"],
  };
}

function image({
  id,
  product,
  runtime,
  variant,
  soc,
  architectures,
  version = "0.10.2",
  channel = "stable",
  osId = "ubuntu",
  osVersion = "22.04",
}) {
  return {
    id,
    product,
    upstream: { version, channel },
    accelerator: { runtime, variant, soc_version: soc },
    os: { id: osId, version: osVersion },
    publications: {
      ghcr: publication("ghcr.io/example/" + id + ":v0.9.0", architectures),
      dockerhub: null,
    },
  };
}

function fixture() {
  return {
    kind: "ucm-release-manifest",
    schema_version: 7,
    release: {
      tag: "v0.9.0",
      type: "stable",
      version: "0.9.0",
      url: "https://github.com/example/ucm/releases/tag/v0.9.0",
      actions_run_id: 33087700398,
    },
    wheels: [
      wheel({
        id: "wheel-cuda-amd64",
        product: "vllm",
        channel: "cu130",
        runtime: "cuda-13.0",
        variant: "default",
        soc: "na",
        architecture: "amd64",
      }),
      wheel({
        id: "wheel-cann-arm64",
        product: "vllm-ascend",
        channel: "cann901-a2",
        runtime: "cann-9.0.1",
        variant: "a2",
        soc: "ascend910b1",
        architecture: "arm64",
      }),
    ],
    images: [
      image({
        id: "vllm-cuda",
        product: "vllm",
        runtime: "cuda-13.0",
        variant: "default",
        soc: "na",
        architectures: ["amd64", "arm64"],
      }),
      image({
        id: "vllm-ascend-a2",
        product: "vllm-ascend",
        runtime: "cann-9.0.1",
        variant: "a2",
        soc: "ascend910b1",
        architectures: ["arm64"],
      }),
    ],
    chart: {
      name: "unified-cache-chart",
      version: "0.9.0",
      filename: "unified-cache-chart-0.9.0.tgz",
      url: "https://github.com/example/ucm/releases/download/v0.9.0/chart.tgz",
      oci: "ghcr.io/example/charts/unified-cache-chart:0.9.0",
    },
    github_release_assets: [
      "release-manifest.json",
      "wheel-cuda-amd64.whl",
      "wheel-cann-arm64.whl",
      "unified-cache-chart-0.9.0.tgz",
    ],
  };
}

function option(row, value) {
  return row.options.find((candidate) => candidate.value === value);
}

test("Schema 7 loader enforces the exact public contract", () => {
  assert.equal(Manifest.validateManifest(fixture()).schema_version, 7);
  const withoutChartOci = fixture();
  withoutChartOci.chart.oci = null;
  assert.equal(Manifest.validateManifest(withoutChartOci).chart.oci, null);
  const invalid = fixture();
  invalid.release.legacy_catalog = "install-catalog.json";
  assert.throws(() => Manifest.validateManifest(invalid), /unexpected fields/);
  const invalidSingleArchitecture = fixture();
  invalidSingleArchitecture.images[1].publications.ghcr.members[0].reference +=
    "-different";
  assert.throws(
    () => Manifest.validateManifest(invalidSingleArchitecture),
    /must equal the single member reference/
  );
});

test("Compute platform labels hide internal SoC identifiers", () => {
  const accelerator = {
    runtime: "cann-9.0.1",
    variant: "a3",
    soc_version: "ascend910_9391",
  };
  assert.equal(Manifest.acceleratorKey(accelerator), "cann-9.0.1|a3");
  assert.equal(Manifest.acceleratorLabel(accelerator), "CANN 9.0.1 / A3");
  assert.equal(Manifest.acceleratorLabel(accelerator).includes("9391"), false);
  assert.deepEqual(Selector.computeLabelParts("CANN 9.0.1 / A3"), {
    primary: "CANN 9.0.1",
    secondary: "A3",
  });
  assert.deepEqual(Selector.computeLabelParts("CUDA 13.0"), {
    primary: "CUDA 13.0",
    secondary: null,
  });
});

test("Image engine versions use published endpoints as ranges", () => {
  const combinations = [
    {
      engine: "vllm-ascend",
      engineVersion: "0.23.0",
      engineChannel: "stable",
      engineRuntime: "cann-9.1.0",
    },
    {
      engine: "vllm-ascend",
      engineVersion: "0.24.0rc0",
      engineChannel: "nightly",
      engineRuntime: "cann-9.0.1",
    },
    {
      engine: "vllm-ascend",
      engineVersion: "0.25.1rc0",
      engineChannel: "nightly",
      engineRuntime: "cann-9.0.1",
    },
    {
      engine: "vllm-ascend",
      engineVersion: "0.26.0rc0",
      engineChannel: "nightly",
      engineRuntime: "cann-9.1.0",
    },
    {
      engine: "vllm",
      engineVersion: "0.28.0",
      engineChannel: "stable",
      engineRuntime: "cuda-13.0",
    },
  ];
  assert.deepEqual(Selector.engineVersionOptions(combinations, "vllm-ascend"), [
    { value: "0.23.0", label: "\u2264 0.23.0 / Stable \u00b7 CANN 9.1.0" },
    { value: "0.24.0rc0", label: "0.24.0 / Nightly \u00b7 CANN 9.0.1" },
    { value: "0.25.1rc0", label: "0.25.1 / Nightly \u00b7 CANN 9.0.1" },
    { value: "0.26.0rc0", label: "\u2265 0.26.0 / Nightly \u00b7 CANN 9.1.0" },
  ]);
});

test("Engine version rejects ambiguous channel or CANN mappings", () => {
  assert.throws(
    () =>
      Selector.engineVersionOptions(
        [
          {
            engine: "vllm-ascend",
            engineVersion: "0.24.0rc0",
            engineChannel: "stable",
            engineRuntime: "cann-9.0.1",
          },
          {
            engine: "vllm-ascend",
            engineVersion: "0.24.0rc0",
            engineChannel: "nightly",
            engineRuntime: "cann-9.0.1",
          },
        ],
        "vllm-ascend"
      ),
    /conflicting release channels/
  );
  assert.throws(
    () =>
      Selector.engineVersionOptions(
        [
          {
            engine: "vllm-ascend",
            engineVersion: "0.24.0rc0",
            engineChannel: "nightly",
            engineRuntime: "cann-9.0.1",
          },
          {
            engine: "vllm-ascend",
            engineVersion: "0.24.0rc0",
            engineChannel: "nightly",
            engineRuntime: "cann-9.1.0",
          },
        ],
        "vllm-ascend"
      ),
    /conflicting CANN runtimes/
  );

  const conflictingManifest = fixture();
  const conflictingImage = structuredClone(conflictingManifest.images[1]);
  conflictingImage.id = "vllm-ascend-a2-cann910";
  conflictingImage.accelerator.runtime = "cann-9.1.0";
  conflictingManifest.images.push(conflictingImage);
  assert.throws(
    () =>
      Selector.buildSelectorModel(
        conflictingManifest,
        "https://docs.example/latest/release-manifest.json"
      ),
    /conflicting CANN runtimes/
  );
});

test("Ascend engine version selects the matching CANN Wheel and Image", () => {
  const versions = [
    { version: "0.23.0", runtime: "cann-9.1.0", wheel: "cann910" },
    { version: "0.24.0rc0", runtime: "cann-9.0.1", wheel: "cann901" },
    { version: "0.25.1rc0", runtime: "cann-9.0.1", wheel: "cann901" },
    { version: "0.26.0rc0", runtime: "cann-9.1.0", wheel: "cann910" },
  ];
  const variants = ["a2", "a3"];
  const manifest = fixture();
  manifest.wheels = [];
  manifest.images = [];
  [
    { runtime: "cann-9.0.1", channel: "cann901" },
    { runtime: "cann-9.1.0", channel: "cann910" },
  ].forEach(({ runtime, channel }) => {
    variants.forEach((variant) => {
      manifest.wheels.push(
        wheel({
          id: "wheel-" + channel + "-" + variant,
          product: "vllm-ascend",
          channel: channel + "-" + variant,
          runtime,
          variant,
          soc: variant === "a2" ? "ascend910b1" : "ascend910_9391",
          architecture: "arm64",
        })
      );
    });
  });
  versions.forEach(({ version, runtime }) => {
    variants.forEach((variant) => {
      manifest.images.push(
        image({
          id: "ascend-" + version + "-" + variant,
          product: "vllm-ascend",
          version,
          channel: version === "0.23.0" ? "stable" : "nightly",
          runtime,
          variant,
          soc: variant === "a2" ? "ascend910b1" : "ascend910_9391",
          architectures: ["arm64"],
        })
      );
    });
  });
  manifest.images.push(
    image({
      id: "ascend-0.23.0-a2-amd64-openeuler",
      product: "vllm-ascend",
      version: "0.23.0",
      channel: "stable",
      runtime: "cann-9.1.0",
      variant: "a2",
      soc: "ascend910b1",
      architectures: ["amd64"],
      osId: "openeuler",
      osVersion: "22.03",
    })
  );
  manifest.github_release_assets = [
    "release-manifest.json",
    ...manifest.wheels.map((item) => item.filename),
    manifest.chart.filename,
  ];
  const model = Selector.buildSelectorModel(
    manifest,
    "https://docs.example/latest/release-manifest.json"
  );

  versions.forEach(({ version, wheel }) => {
    variants.forEach((variant) => {
      const wheelSelection = Selector.deriveSelection(model, {
        method: "wheel",
        engine: "vllm-ascend",
        engineVersion: version,
        compute: variant,
        architecture: "arm64",
      });
      const imageSelection = Selector.deriveSelection(model, {
        method: "image",
        engine: "vllm-ascend",
        engineVersion: version,
        compute: variant,
        os: "ubuntu|22.04",
        architecture: "arm64",
      });
      assert.match(wheelSelection.command, new RegExp("0\\.9\\.0\\+" + wheel + "\\." + variant));
      assert.equal(
        imageSelection.command,
        "docker pull ghcr.io/example/ascend-" + version + "-" + variant + ":v0.9.0"
      );
    });
  });

  const latest = Selector.deriveSelection(model, {
    method: "image",
    engine: "vllm-ascend",
    engineVersion: "0.26.0rc0",
    compute: "a2",
    os: "openeuler|22.03",
    architecture: "amd64",
  });
  assert.equal(option(latest.rows.os, "openeuler|22.03").disabled, true);
  assert.equal(option(latest.rows.architecture, "amd64").disabled, true);
  assert.equal(latest.state.os, "ubuntu|22.04");
  assert.equal(latest.state.architecture, "arm64");
  assert.deepEqual(latest.rows.compute.options.map((item) => item.label), ["A2", "A3"]);
  assert.deepEqual(latest.rows.engineVersion.options.map((item) => item.label), [
    "\u2264 0.23.0 / Stable \u00b7 CANN 9.1.0",
    "0.24.0 / Nightly \u00b7 CANN 9.0.1",
    "0.25.1 / Nightly \u00b7 CANN 9.0.1",
    "\u2265 0.26.0 / Nightly \u00b7 CANN 9.1.0",
  ]);
});

test("Wheel selection auto-corrects dependent fields and emits one exact command", () => {
  const model = Selector.buildSelectorModel(
    fixture(),
    "https://docs.example/0.9.0/release-manifest.json"
  );
  const initial = Selector.deriveSelection(model, { method: "wheel" });
  assert.equal(initial.rows.engineVersion.visible, true);
  assert.deepEqual(initial.state, {
    method: "wheel",
    engine: "vllm",
    engineVersion: "0.10.2",
    compute: "cuda-13.0|default",
    os: null,
    architecture: "amd64",
  });
  assert.deepEqual(initial.rows.compute.options.map((item) => item.label), ["CUDA 13.0"]);
  assert.equal(
    initial.command,
    'python -m pip install "uc-manager==0.9.0+cu130" --index-url https://docs.example/whl/cu130/'
  );

  const ascend = Selector.deriveSelection(model, {
    method: "wheel",
    engine: "vllm-ascend",
    compute: "cuda-13.0|default",
    architecture: "amd64",
  });
  assert.equal(ascend.state.compute, "a2");
  assert.equal(ascend.state.architecture, "arm64");
  assert.match(ascend.command, /uc-manager==0\.9\.0\+cann901\.a2/);
  assert.deepEqual(ascend.rows.compute.options.map((item) => item.label), ["A2"]);
});

test("Image selection filters by architecture and always pulls publication.pull", () => {
  const manifest = fixture();
  manifest.images[0].publications.dockerhub = publication(
    "docker.io/example/vllm:v0.9.0",
    ["ppc64le"]
  );
  const model = Selector.buildSelectorModel(
    manifest,
    "https://docs.example/latest/release-manifest.json"
  );
  const selected = Selector.deriveSelection(model, {
    method: "image",
    engine: "vllm",
    architecture: "arm64",
  });
  assert.equal(selected.rows.os.visible, true);
  assert.equal(selected.rows.engineVersion.visible, true);
  assert.equal(selected.state.engineVersion, "0.10.2");
  assert.equal(selected.state.architecture, "arm64");
  assert.equal(selected.command, "docker pull " + manifest.images[0].publications.ghcr.pull);
  assert.equal((selected.command.match(/docker pull/g) || []).length, 1);
  const dockerhubOnlyArchitecture = Selector.deriveSelection(model, {
    method: "image",
    engine: "vllm",
    architecture: "ppc64le",
  });
  assert.equal(
    dockerhubOnlyArchitecture.command,
    "docker pull " + manifest.images[0].publications.dockerhub.pull
  );
});

test("Helm hides unrelated rows and emits only the Release Chart command", () => {
  const manifest = fixture();
  const model = Selector.buildSelectorModel(
    manifest,
    "https://docs.example/latest/release-manifest.json"
  );
  const selected = Selector.deriveSelection(model, { method: "helm" });
  for (const row of ["engine", "engineVersion", "compute", "os", "architecture"]) {
    assert.equal(selected.rows[row].visible, false);
  }
  assert.equal(selected.command, "helm install ucm " + manifest.chart.url);
});

test("Download inventory shares Schema 7 and exposes every artifact", () => {
  const manifest = fixture();
  const inventory = Download.buildInventory(
    manifest,
    "https://docs.example/0.9.0/release-manifest.json"
  );
  assert.equal(inventory.wheelCount, manifest.wheels.length);
  assert.equal(inventory.imageCount, manifest.images.length);
  assert.equal(inventory.wheelGroups[0].indexUrl, "https://docs.example/whl/cann901-a2/");
  assert.deepEqual(
    Download.publicationRows(manifest.images[0]).map((row) => row.channel),
    ["ghcr"]
  );
  assert.equal(inventory.chart.url, manifest.chart.url);
  assert.deepEqual(
    Download.overviewEntries(inventory, Download.TEXT.en).map((entry) => entry.title),
    ["Wheel", "Helm", "Image"]
  );
});
