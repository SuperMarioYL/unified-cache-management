const assert = require("node:assert/strict");
const test = require("node:test");

const Manifest = require("../docs/assets/manifest.js");
const Selector = require("../docs/assets/install.js");
const Download = require("../docs/assets/download.js");

assert.deepEqual(Selector.ROW_ORDER.slice(0, 2), ["method", "engine"]);

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

function wheel({ id, product, extra, runtime, variant, soc, architecture }) {
  const distribution =
    product === "vllm" ? "uc-manager-cuda-" + extra : "uc-manager-" + extra;
  const platformTag =
    (product === "vllm" ? "manylinux_2_28_" : "manylinux_2_34_") +
    (architecture === "amd64" ? "x86_64" : "aarch64");
  const filename =
    distribution.replaceAll("-", "_") +
    "-0.9.3-cp312-cp312-" +
    platformTag +
    ".whl";
  return {
    id,
    product,
    extra,
    accelerator: { runtime, variant, soc_version: soc },
    distribution,
    version: "0.9.3",
    python_abi: "cp312",
    architecture,
    platform_tags: [platformTag],
    filename,
    url: "https://github.com/example/ucm/releases/download/v0.9.3/" + filename,
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
      ghcr: publication("ghcr.io/example/" + id + ":v0.9.3", architectures),
      dockerhub: null,
    },
  };
}

function refreshPythonAssets(manifest) {
  manifest.python.extras = Object.fromEntries(
    manifest.wheels.map((item) => [item.extra, item.distribution])
  );
  manifest.github_release_assets = [
    "release-manifest.json",
    manifest.chart.filename,
    ...manifest.wheels.map((item) => item.filename),
    manifest.python.filename,
    ...(manifest.python.pypi === null ? [] : ["pypi-receipt.json"]),
  ].sort();
  return manifest;
}

function fixture({ pypi = true } = {}) {
  const manifest = {
    kind: "ucm-release-manifest",
    schema_version: 8,
    release: {
      tag: "v0.9.3",
      type: "stable",
      version: "0.9.3",
      url: "https://github.com/example/ucm/releases/tag/v0.9.3",
      actions_run_id: 33087700398,
    },
    python: {
      distribution: "uc-manager",
      version: "0.9.3",
      filename: "uc_manager-0.9.3-py3-none-any.whl",
      url: "https://github.com/example/ucm/releases/download/v0.9.3/uc_manager.whl",
      sha256: "b".repeat(64),
      tags: ["py3-none-any"],
      extras: {},
      pypi: pypi
        ? {
            index_url: "https://pypi.org/simple",
            project_url: "https://pypi.org/project/uc-manager/0.9.3/",
          }
        : null,
    },
    wheels: [
      wheel({
        id: "wheel-cuda-amd64",
        product: "vllm",
        extra: "cu130",
        runtime: "cuda-13.0",
        variant: "default",
        soc: "na",
        architecture: "amd64",
      }),
      wheel({
        id: "wheel-cann-arm64",
        product: "vllm-ascend",
        extra: "cann901-a2",
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
      version: "0.9.3",
      filename: "unified-cache-chart-0.9.3.tgz",
      url: "https://github.com/example/ucm/releases/download/v0.9.3/chart.tgz",
      oci: "ghcr.io/example/charts/unified-cache-chart:0.9.3",
    },
    github_release_assets: [],
  };
  return refreshPythonAssets(manifest);
}

function option(row, value) {
  return row.options.find((candidate) => candidate.value === value);
}

test("loader accepts only the exact Schema 8 public contract", () => {
  const schema7 = fixture();
  schema7.schema_version = 7;
  delete schema7.python;
  assert.throws(() => Manifest.validateManifest(schema7), /must be 8/);

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

test("Schema 8 loader validates the meta package and backend extras", () => {
  const manifest = fixture();
  assert.equal(Manifest.validateManifest(manifest).schema_version, 8);
  assert.equal(Manifest.SCHEMA_VERSION, 8);

  const wrongBackend = structuredClone(manifest);
  wrongBackend.wheels[0].distribution = "uc-manager-cuda-wrong";
  assert.throws(() => Manifest.validateManifest(wrongBackend), /Python extra/);

  const missingMeta = structuredClone(manifest);
  missingMeta.github_release_assets = missingMeta.github_release_assets.filter(
    (item) => item !== missingMeta.python.filename
  );
  assert.throws(() => Manifest.validateManifest(missingMeta), /is missing/);

  const missingReceipt = structuredClone(manifest);
  missingReceipt.github_release_assets = missingReceipt.github_release_assets.filter(
    (item) => item !== "pypi-receipt.json"
  );
  assert.throws(() => Manifest.validateManifest(missingReceipt), /PyPI receipt/);

  const wrongProject = structuredClone(manifest);
  wrongProject.python.pypi.project_url =
    "https://pypi.org/project/uc-manager/99.0/";
  assert.throws(() => Manifest.validateManifest(wrongProject), /exact release PyPI/);

  const emptyPlatform = structuredClone(manifest);
  emptyPlatform.wheels[0].platform_tags = [];
  assert.throws(() => Manifest.validateManifest(emptyPlatform), /must not be empty/);

  const mismatchedPlatform = structuredClone(manifest);
  mismatchedPlatform.wheels[0].platform_tags = ["manylinux_2_34_x86_64"];
  assert.throws(() => Manifest.validateManifest(mismatchedPlatform), /filename/);
});

test("Schema 8 Wheel shows the PyPI extra contract in official and fork docs", () => {
  const manifest = fixture();
  const model = Selector.buildSelectorModel(manifest);
  const cuda = Selector.deriveSelection(model, {
    method: "wheel",
    engine: "vllm",
  });
  const ascend = Selector.deriveSelection(model, {
    method: "wheel",
    engine: "vllm-ascend",
  });
  assert.equal(
    cuda.command,
    'pip install "uc-manager[cu130]==0.9.3"'
  );
  assert.equal(
    ascend.command,
    'pip install "uc-manager[cann901-a2]==0.9.3"'
  );
  assert.equal(cuda.command.includes("/whl/"), false);
  assert.equal(cuda.command.includes("--index-url"), false);

  const unpublished = Selector.buildSelectorModel(fixture({ pypi: false }));
  const unpublishedCuda = Selector.deriveSelection(unpublished, {
    method: "wheel",
    engine: "vllm",
  });
  const unpublishedAscend = Selector.deriveSelection(unpublished, {
    method: "wheel",
    engine: "vllm-ascend",
  });
  assert.equal(option(unpublishedCuda.rows.method, "wheel").disabled, false);
  assert.equal(unpublishedCuda.state.method, "wheel");
  assert.equal(
    unpublishedCuda.command,
    'pip install "uc-manager[cu130]==0.9.3"'
  );
  assert.equal(
    unpublishedAscend.command,
    'pip install "uc-manager[cann901-a2]==0.9.3"'
  );
  assert.equal(unpublishedCuda.command.includes("github.com"), false);
  assert.equal(unpublishedCuda.command.includes("/whl/"), false);
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
    () => Selector.buildSelectorModel(conflictingManifest),
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
    { runtime: "cann-9.0.1", extraPrefix: "cann901" },
    { runtime: "cann-9.1.0", extraPrefix: "cann910" },
  ].forEach(({ runtime, extraPrefix }) => {
    variants.forEach((variant) => {
      manifest.wheels.push(
        wheel({
          id: "wheel-" + extraPrefix + "-" + variant,
          product: "vllm-ascend",
          extra: extraPrefix + "-" + variant,
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
  refreshPythonAssets(manifest);
  const model = Selector.buildSelectorModel(manifest);

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
      assert.equal(
        wheelSelection.command,
        'pip install "uc-manager[' + wheel + "-" + variant + ']==0.9.3"'
      );
      assert.equal(
        imageSelection.command,
        "docker pull ghcr.io/example/ascend-" + version + "-" + variant + ":v0.9.3"
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
  const model = Selector.buildSelectorModel(fixture());
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
    'pip install "uc-manager[cu130]==0.9.3"'
  );

  const ascend = Selector.deriveSelection(model, {
    method: "wheel",
    engine: "vllm-ascend",
    compute: "cuda-13.0|default",
    architecture: "amd64",
  });
  assert.equal(ascend.state.compute, "a2");
  assert.equal(ascend.state.architecture, "arm64");
  assert.equal(ascend.command, 'pip install "uc-manager[cann901-a2]==0.9.3"');
  assert.deepEqual(ascend.rows.compute.options.map((item) => item.label), ["A2"]);
});

test("Image selection filters by architecture and always pulls publication.pull", () => {
  const manifest = fixture();
  manifest.images[0].publications.dockerhub = publication(
    "docker.io/example/vllm:v0.9.3",
    ["ppc64le"]
  );
  const model = Selector.buildSelectorModel(manifest);
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
  const model = Selector.buildSelectorModel(manifest);
  const selected = Selector.deriveSelection(model, { method: "helm" });
  for (const row of ["engine", "engineVersion", "compute", "os", "architecture"]) {
    assert.equal(selected.rows[row].visible, false);
  }
  assert.equal(selected.command, "helm install ucm " + manifest.chart.url);
});

test("Download inventory exposes Schema 8 meta package and PyPI extras", () => {
  const manifest = fixture();
  const inventory = Download.buildInventory(manifest);
  assert.equal(inventory.wheelCount, manifest.wheels.length);
  assert.equal(inventory.imageCount, manifest.images.length);
  assert.equal(inventory.python.distribution, "uc-manager");
  assert.deepEqual(
    inventory.wheelGroups.map((group) => ({
      extra: group.extra,
      distribution: group.distribution,
    })),
    [
      {
        extra: "cann901-a2",
        distribution: "uc-manager-cann901-a2",
      },
      {
        extra: "cu130",
        distribution: "uc-manager-cuda-cu130",
      },
    ]
  );
  assert.deepEqual(
    Download.publicationRows(manifest.images[0]).map((row) => row.channel),
    ["ghcr"]
  );
  assert.equal(inventory.chart.url, manifest.chart.url);
  assert.deepEqual(
    Download.overviewEntries(inventory, Download.TEXT.en).map((entry) => entry.title),
    ["Wheel", "Helm", "Image"]
  );
  assert.equal(Download.TEXT.en.overviewWheel.includes("Simple Index"), false);
  assert.equal(Download.TEXT.zh.overviewWheel.includes("Simple Index"), false);
});
