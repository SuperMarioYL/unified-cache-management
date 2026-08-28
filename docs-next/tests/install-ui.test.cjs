const assert = require("node:assert/strict");
const test = require("node:test");

const Manifest = require("../docs/assets/manifest.js");
const Selector = require("../docs/assets/install.js");
const Download = require("../docs/assets/download.js");

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

function image({ id, product, runtime, variant, soc, architectures }) {
  return {
    id,
    product,
    upstream: { version: "0.10.2", channel: "stable" },
    accelerator: { runtime, variant, soc_version: soc },
    os: { id: "ubuntu", version: "22.04" },
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

test("Wheel selection auto-corrects dependent fields and emits one exact command", () => {
  const model = Selector.buildSelectorModel(
    fixture(),
    "https://docs.example/0.9.0/release-manifest.json"
  );
  const initial = Selector.deriveSelection(model, { method: "wheel" });
  assert.deepEqual(initial.state, {
    method: "wheel",
    engine: "vllm",
    compute: "cuda-13.0|default|na",
    os: null,
    architecture: "amd64",
  });
  assert.equal(option(initial.rows.compute, "cann-9.0.1|a2|ascend910b1").disabled, true);
  assert.equal(
    initial.command,
    'python -m pip install "uc-manager==0.9.0+cu130" --index-url https://docs.example/whl/cu130/'
  );

  const ascend = Selector.deriveSelection(model, {
    method: "wheel",
    engine: "vllm-ascend",
    compute: "cuda-13.0|default|na",
    architecture: "amd64",
  });
  assert.equal(ascend.state.compute, "cann-9.0.1|a2|ascend910b1");
  assert.equal(ascend.state.architecture, "arm64");
  assert.match(ascend.command, /uc-manager==0\.9\.0\+cann901\.a2/);
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
  for (const row of ["engine", "compute", "os", "architecture"]) {
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
