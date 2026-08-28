(function (root, factory) {
  "use strict";

  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.UcmReleaseManifest = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var KIND = "ucm-release-manifest";
  var SCHEMA_VERSION = 7;
  var PATH_COMPONENT = /^[a-z0-9][a-z0-9.+-]*$/;
  var SHA256 = /^[0-9a-f]{64}$/;
  var manifestScriptSource =
    typeof document !== "undefined" && document.currentScript
      ? document.currentScript.src
      : null;
  var manifestKeys = [
    "kind",
    "schema_version",
    "release",
    "wheels",
    "images",
    "chart",
    "github_release_assets",
  ];

  function fail(context, message) {
    throw new TypeError(context + " " + message);
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function exactKeys(value, keys, context) {
    if (!isObject(value)) fail(context, "must be an object");
    var actual = Object.keys(value).sort();
    var expected = keys.slice().sort();
    if (actual.length !== expected.length) fail(context, "has unexpected fields");
    for (var index = 0; index < expected.length; index += 1) {
      if (actual[index] !== expected[index]) fail(context, "has unexpected fields");
    }
  }

  function string(value, context) {
    if (typeof value !== "string" || !value.length || value.trim() !== value) {
      fail(context, "must be a string");
    }
  }

  function stringArray(value, context) {
    if (!Array.isArray(value)) fail(context, "must be an array");
    value.forEach(function (item, index) {
      string(item, context + "[" + index + "]");
    });
  }

  function validateAccelerator(value, context) {
    exactKeys(value, ["runtime", "variant", "soc_version"], context);
    string(value.runtime, context + ".runtime");
    string(value.variant, context + ".variant");
    string(value.soc_version, context + ".soc_version");
  }

  function validatePublication(value, context) {
    if (value === null) return;
    exactKeys(value, ["pull", "multi_arch", "members"], context);
    string(value.pull, context + ".pull");
    if (typeof value.multi_arch !== "boolean") {
      fail(context + ".multi_arch", "must be a boolean");
    }
    if (!Array.isArray(value.members) || !value.members.length) {
      fail(context + ".members", "must be a non-empty array");
    }
    var architectures = {};
    var references = {};
    value.members.forEach(function (member, index) {
      var memberContext = context + ".members[" + index + "]";
      exactKeys(member, ["architecture", "reference"], memberContext);
      string(member.architecture, memberContext + ".architecture");
      string(member.reference, memberContext + ".reference");
      if (architectures[member.architecture]) {
        fail(context + ".members", "must use unique architectures");
      }
      if (references[member.reference]) {
        fail(context + ".members", "must use unique references");
      }
      architectures[member.architecture] = true;
      references[member.reference] = true;
    });
    if (value.multi_arch && value.members.length < 2) {
      fail(context, "multi_arch requires at least two members");
    }
    if (!value.multi_arch) {
      if (value.members.length !== 1) {
        fail(context, "single-architecture publication requires one member");
      }
      if (value.pull !== value.members[0].reference) {
        fail(context + ".pull", "must equal the single member reference");
      }
    }
  }

  function validateManifest(manifest) {
    exactKeys(manifest, manifestKeys, "manifest");
    if (manifest.kind !== KIND) fail("manifest.kind", "must be " + KIND);
    if (manifest.schema_version !== SCHEMA_VERSION) {
      fail("manifest.schema_version", "must be " + SCHEMA_VERSION);
    }

    exactKeys(
      manifest.release,
      ["tag", "type", "version", "url", "actions_run_id"],
      "manifest.release"
    );
    ["tag", "type", "version", "url"].forEach(function (field) {
      string(manifest.release[field], "manifest.release." + field);
    });
    if (["stable", "prerelease", "draft", "nightly"].indexOf(manifest.release.type) < 0) {
      fail("manifest.release.type", "is invalid");
    }
    if (!PATH_COMPONENT.test(manifest.release.version)) {
      fail("manifest.release.version", "must be path-safe");
    }
    if (!Number.isInteger(manifest.release.actions_run_id) || manifest.release.actions_run_id <= 0) {
      fail("manifest.release.actions_run_id", "must be a positive integer");
    }

    if (!Array.isArray(manifest.wheels)) fail("manifest.wheels", "must be an array");
    var wheelIds = new Set();
    var wheelFilenames = new Set();
    manifest.wheels.forEach(function (wheel, index) {
      var context = "manifest.wheels[" + index + "]";
      exactKeys(
        wheel,
        [
          "id",
          "product",
          "channel",
          "accelerator",
          "distribution",
          "version",
          "python_abi",
          "architecture",
          "filename",
          "url",
          "sha256",
          "dependencies",
        ],
        context
      );
      [
        "id",
        "product",
        "channel",
        "distribution",
        "version",
        "python_abi",
        "architecture",
        "filename",
        "url",
        "sha256",
      ].forEach(function (field) {
        string(wheel[field], context + "." + field);
      });
      validateAccelerator(wheel.accelerator, context + ".accelerator");
      stringArray(wheel.dependencies, context + ".dependencies");
      if (wheelIds.has(wheel.id) || wheelFilenames.has(wheel.filename)) {
        fail("manifest.wheels", "must use unique IDs and filenames");
      }
      wheelIds.add(wheel.id);
      wheelFilenames.add(wheel.filename);
      if (wheel.distribution !== "uc-manager") {
        fail(context + ".distribution", "must be uc-manager");
      }
      if (!PATH_COMPONENT.test(wheel.channel)) {
        fail(context + ".channel", "must be path-safe");
      }
      if (wheel.filename.indexOf("/") !== -1) {
        fail(context + ".filename", "must not contain a path");
      }
      if (!SHA256.test(wheel.sha256)) {
        fail(context + ".sha256", "must contain 64 lowercase hex digits");
      }
      var normalizedDependencies = Array.from(new Set(wheel.dependencies)).sort();
      if (
        normalizedDependencies.length !== wheel.dependencies.length ||
        normalizedDependencies.some(function (dependency, dependencyIndex) {
          return dependency !== wheel.dependencies[dependencyIndex];
        })
      ) {
        fail(context + ".dependencies", "must be sorted and unique");
      }
    });

    if (!Array.isArray(manifest.images)) fail("manifest.images", "must be an array");
    var imageIds = new Set();
    manifest.images.forEach(function (image, index) {
      var context = "manifest.images[" + index + "]";
      exactKeys(
        image,
        ["id", "product", "upstream", "accelerator", "os", "publications"],
        context
      );
      string(image.id, context + ".id");
      string(image.product, context + ".product");
      if (imageIds.has(image.id)) fail("manifest.images", "must use unique IDs");
      imageIds.add(image.id);
      exactKeys(image.upstream, ["version", "channel"], context + ".upstream");
      string(image.upstream.version, context + ".upstream.version");
      string(image.upstream.channel, context + ".upstream.channel");
      validateAccelerator(image.accelerator, context + ".accelerator");
      exactKeys(image.os, ["id", "version"], context + ".os");
      string(image.os.id, context + ".os.id");
      string(image.os.version, context + ".os.version");
      exactKeys(image.publications, ["ghcr", "dockerhub"], context + ".publications");
      validatePublication(image.publications.ghcr, context + ".publications.ghcr");
      validatePublication(
        image.publications.dockerhub,
        context + ".publications.dockerhub"
      );
      if (image.publications.ghcr === null && image.publications.dockerhub === null) {
        fail(context + ".publications", "must contain a published target");
      }
    });

    exactKeys(
      manifest.chart,
      ["name", "version", "filename", "url", "oci"],
      "manifest.chart"
    );
    ["name", "version", "filename", "url"].forEach(function (field) {
      string(manifest.chart[field], "manifest.chart." + field);
    });
    if (manifest.chart.oci !== null) {
      string(manifest.chart.oci, "manifest.chart.oci");
    }
    if (manifest.chart.filename.indexOf("/") !== -1) {
      fail("manifest.chart.filename", "must not contain a path");
    }
    if (wheelFilenames.has(manifest.chart.filename)) {
      fail("manifest.chart.filename", "must be unique from Wheel filenames");
    }
    stringArray(manifest.github_release_assets, "manifest.github_release_assets");
    var assets = new Set(manifest.github_release_assets);
    if (assets.size !== manifest.github_release_assets.length) {
      fail("manifest.github_release_assets", "must be unique");
    }
    if (!assets.has("release-manifest.json")) {
      fail("manifest.github_release_assets", "must include release-manifest.json");
    }
    if (assets.has("install-catalog.json")) {
      fail("manifest.github_release_assets", "must not include install-catalog.json");
    }
    var requiredAssets = [manifest.chart.filename].concat(Array.from(wheelFilenames));
    requiredAssets.forEach(function (asset) {
      if (!assets.has(asset)) {
        fail("manifest.github_release_assets", "is missing " + asset);
      }
    });
    return manifest;
  }

  function defaultManifestUrl(scriptSource) {
    var source = scriptSource;
    if (!source && typeof document !== "undefined" && document.currentScript) {
      source = document.currentScript.src;
    }
    var base =
      typeof document !== "undefined" ? document.baseURI : "https://example.invalid/assets/";
    var scriptUrl = new URL(source || manifestScriptSource || "assets/manifest.js", base);
    return new URL("../release-manifest.json", scriptUrl);
  }

  function loadManifest(url, fetchImplementation) {
    var target = url ? new URL(String(url), defaultManifestUrl()) : defaultManifestUrl();
    var request = fetchImplementation || (typeof fetch === "function" ? fetch : null);
    if (!request) return Promise.reject(new Error("Fetch API unavailable"));
    return request(target.href, { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("Release Manifest unavailable");
        return response.json();
      })
      .then(validateManifest);
  }

  function acceleratorKey(accelerator) {
    return [accelerator.runtime, accelerator.variant, accelerator.soc_version].join("|");
  }

  function acceleratorLabel(accelerator) {
    var runtime = accelerator.runtime.replace("-", " ").toUpperCase();
    var details = [];
    if (accelerator.variant !== "default") details.push(accelerator.variant.toUpperCase());
    if (accelerator.soc_version !== "na") details.push(accelerator.soc_version);
    return details.length ? runtime + " / " + details.join(" / ") : runtime;
  }

  function osKey(os) {
    return os.id + "|" + os.version;
  }

  function osLabel(os) {
    return os.id.charAt(0).toUpperCase() + os.id.slice(1) + " " + os.version;
  }

  function productLabel(product) {
    if (product === "vllm-ascend") return "vLLM-Ascend";
    if (product === "vllm") return "vLLM";
    return product;
  }

  function preferredPublication(image) {
    if (image.publications.ghcr) {
      return { channel: "ghcr", publication: image.publications.ghcr };
    }
    if (image.publications.dockerhub) {
      return { channel: "dockerhub", publication: image.publications.dockerhub };
    }
    return null;
  }

  return {
    KIND: KIND,
    SCHEMA_VERSION: SCHEMA_VERSION,
    validateManifest: validateManifest,
    defaultManifestUrl: defaultManifestUrl,
    loadManifest: loadManifest,
    acceleratorKey: acceleratorKey,
    acceleratorLabel: acceleratorLabel,
    osKey: osKey,
    osLabel: osLabel,
    productLabel: productLabel,
    preferredPublication: preferredPublication,
  };
});
