(function (root, factory) {
  "use strict";

  var manifestApi = root.UcmReleaseManifest;
  if (!manifestApi && typeof module === "object" && module.exports) {
    manifestApi = require("./manifest.js");
  }
  var api = factory(manifestApi);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.UcmInstallSelector = api;

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", api.initialize);
    } else {
      api.initialize();
    }
    if (typeof document$ !== "undefined") document$.subscribe(api.initialize);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (Manifest) {
  "use strict";

  if (!Manifest) throw new Error("UcmReleaseManifest must be loaded before install.js");

  var COPY_ICON = "\u2398";
  var METHOD_ORDER = ["wheel", "image", "helm"];
  var ENGINE_ORDER = ["vllm", "vllm-ascend"];
  var ROW_ORDER = [
    "method",
    "engine",
    "engineVersion",
    "compute",
    "os",
    "architecture",
  ];
  var TEXT = {
    en: {
      loading: "Loading the current release manifest...",
      unavailable: "No release manifest is published for this documentation version yet.",
      invalid: "The published release manifest is invalid.",
      release: "Release",
      method: "Install Method",
      engine: "Engine",
      engineVersion: "Engine Version",
      compute: "Compute Platform",
      os: "OS",
      architecture: "Architecture",
      wheel: "Wheel",
      image: "Image",
      helm: "Helm",
      command: "Install command",
      copy: "Copy",
      copied: "Copied",
      copyFailed: "Copy failed",
      noCombination: "No published artifact matches this selection.",
    },
    zh: {
      loading: "正在加载当前版本的 Release Manifest……",
      unavailable: "此文档版本尚未发布 Release Manifest。",
      invalid: "已发布的 Release Manifest 格式无效。",
      release: "Release",
      method: "安装方式",
      engine: "推理引擎",
      engineVersion: "引擎版本",
      compute: "计算平台",
      os: "操作系统",
      architecture: "架构",
      wheel: "Wheel",
      image: "Image",
      helm: "Helm",
      command: "安装命令",
      copy: "复制",
      copied: "已复制",
      copyFailed: "复制失败",
      noCombination: "没有与当前选项匹配的已发布制品。",
    },
  };

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function architectureLabel(architecture) {
    if (architecture === "amd64") return "x86_64";
    if (architecture === "arm64") return "aarch64";
    return architecture;
  }

  function computeLabelParts(label) {
    var parts = String(label).split(" / ");
    return parts.length === 2
      ? { primary: parts[0], secondary: parts[1] }
      : { primary: String(label), secondary: null };
  }

  function displayEngineVersion(version) {
    return String(version).replace(/rc0$/, "");
  }

  function titleCase(value) {
    var text = String(value);
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function runtimeLabel(runtime) {
    return String(runtime).replace("-", " ").toUpperCase();
  }

  function engineVersionOptions(combinations, engine) {
    var versions = {};
    combinations.forEach(function (combination) {
      if (combination.engine === engine && combination.engineVersion !== null) {
        if (!versions[combination.engineVersion]) {
          versions[combination.engineVersion] = { channels: {}, runtimes: {} };
        }
        versions[combination.engineVersion].channels[combination.engineChannel] = true;
        versions[combination.engineVersion].runtimes[combination.engineRuntime] = true;
      }
    });
    var values = Object.keys(versions).sort(function (left, right) {
      return left.localeCompare(right, undefined, {
        numeric: true,
        sensitivity: "base",
      });
    });
    return values.map(function (value, index) {
      var label = displayEngineVersion(value);
      if (values.length > 1 && index === 0) label = "\u2264 " + label;
      if (values.length > 1 && index === values.length - 1) label = "\u2265 " + label;
      var metadata = versions[value];
      var channels = Object.keys(metadata.channels).sort();
      if (channels.length !== 1) {
        throw new TypeError(
          engine +
            " " +
            value +
            " maps to conflicting release channels: " +
            channels.join(", ")
        );
      }
      var runtimes = Object.keys(metadata.runtimes).sort();
      if (engine === "vllm-ascend" && runtimes.length !== 1) {
        throw new TypeError(
          engine +
            " " +
            value +
            " maps to conflicting CANN runtimes: " +
            runtimes.join(", ")
        );
      }
      var details = [];
      if (engine === "vllm-ascend") {
        details.push(titleCase(channels[0]));
        details.push(runtimeLabel(runtimes[0]));
      } else if (channels[0] !== "stable") {
        details.push(titleCase(channels[0]));
      }
      if (details.length) label += " / " + details.join(" \u00b7 ");
      return { value: value, label: label };
    });
  }

  function selectedComputeValue(combination, engine) {
    return engine === "vllm-ascend" ? combination.computeVariant : combination.compute;
  }

  function unsupportedVariant(artifact) {
    return [artifact.accelerator.variant, artifact.accelerator.soc_version].some(
      function (value) {
        return /(^|[-_.])a5($|[-_.])/i.test(String(value));
      }
    );
  }

  function selectableProduct(artifact) {
    return (
      (artifact.product === "vllm" || artifact.product === "vllm-ascend") &&
      !unsupportedVariant(artifact)
    );
  }

  function wheelIndexUrl(manifestUrl, channel) {
    return new URL("../whl/" + encodeURIComponent(channel) + "/", manifestUrl).href;
  }

  function wheelCombination(wheel, manifest, manifestUrl) {
    var legacy = manifest.schema_version === Manifest.LEGACY_SCHEMA_VERSION;
    var command;
    if (legacy) {
      command =
        'python -m pip install "' +
        wheel.distribution +
        "==" +
        wheel.version +
        '" --index-url ' +
        wheelIndexUrl(manifestUrl, wheel.channel);
    } else {
      command =
        'python -m pip install "' +
        manifest.python.distribution +
        "[" +
        wheel.extra +
        "]==" +
        manifest.python.version +
        '"';
    }
    return {
      method: "wheel",
      engine: wheel.product,
      engineVersion: null,
      compute: Manifest.acceleratorKey(wheel.accelerator),
      computeVariant: wheel.accelerator.variant,
      computeLabel: Manifest.acceleratorLabel(wheel.accelerator),
      os: null,
      osLabel: null,
      architecture: wheel.architecture,
      architectureLabel: architectureLabel(wheel.architecture),
      artifact: wheel,
      command: command,
    };
  }

  function imageCombinations(image) {
    var targets = ["ghcr", "dockerhub"]
      .filter(function (channel) {
        return image.publications[channel] !== null;
      })
      .map(function (channel) {
        return { channel: channel, publication: image.publications[channel] };
      });
    var targetByArchitecture = {};
    targets.forEach(function (target) {
      target.publication.members.forEach(function (member) {
        if (!targetByArchitecture[member.architecture]) {
          targetByArchitecture[member.architecture] = target;
        }
      });
    });
    return Object.keys(targetByArchitecture).map(function (architecture) {
      var target = targetByArchitecture[architecture];
      return {
        method: "image",
        engine: image.product,
        engineVersion: image.upstream.version,
        engineChannel: image.upstream.channel,
        engineRuntime: image.accelerator.runtime,
        compute: Manifest.acceleratorKey(image.accelerator),
        computeVariant: image.accelerator.variant,
        computeLabel: Manifest.acceleratorLabel(image.accelerator),
        os: Manifest.osKey(image.os),
        osLabel: Manifest.osLabel(image.os),
        architecture: architecture,
        architectureLabel: architectureLabel(architecture),
        artifact: image,
        publicationChannel: target.channel,
        command: "docker pull " + target.publication.pull,
      };
    });
  }

  function helmCombination(chart) {
    return {
      method: "helm",
      engine: null,
      engineVersion: null,
      engineChannel: null,
      engineRuntime: null,
      compute: null,
      computeVariant: null,
      computeLabel: null,
      os: null,
      osLabel: null,
      architecture: null,
      architectureLabel: null,
      artifact: chart,
      command: "helm install ucm " + chart.url,
    };
  }

  function buildSelectorModel(manifest, manifestUrl) {
    Manifest.validateManifest(manifest);
    var target = new URL(String(manifestUrl || Manifest.defaultManifestUrl()));
    var combinations = [];
    manifest.wheels.filter(selectableProduct).forEach(function (wheel) {
      combinations.push(wheelCombination(wheel, manifest, target));
    });
    manifest.images.filter(selectableProduct).forEach(function (image) {
      combinations = combinations.concat(imageCombinations(image));
    });
    var publishedImageCombinations = combinations.filter(function (combination) {
      return combination.method === "image";
    });
    ENGINE_ORDER.forEach(function (engine) {
      engineVersionOptions(publishedImageCombinations, engine);
    });
    combinations.push(helmCombination(manifest.chart));
    return { manifest: manifest, manifestUrl: target, combinations: combinations };
  }

  function orderedUnique(combinations, field, labelField, preferredOrder) {
    var labels = {};
    combinations.forEach(function (combination) {
      var value = combination[field];
      if (value === null || value === undefined) return;
      labels[value] = combination[labelField] || String(value);
    });
    var values = Object.keys(labels);
    values.sort(function (left, right) {
      if (preferredOrder) {
        var leftIndex = preferredOrder.indexOf(left);
        var rightIndex = preferredOrder.indexOf(right);
        if (leftIndex !== -1 || rightIndex !== -1) {
          if (leftIndex === -1) return 1;
          if (rightIndex === -1) return -1;
          if (leftIndex !== rightIndex) return leftIndex - rightIndex;
        }
      }
      return labels[left].localeCompare(labels[right]);
    });
    return values.map(function (value) {
      return { value: value, label: labels[value] };
    });
  }

  function choose(requested, options) {
    var available = options.filter(function (option) {
      return !option.disabled;
    });
    if (!available.length) return null;
    return available.some(function (option) {
      return option.value === requested;
    })
      ? requested
      : available[0].value;
  }

  function optionList(universe, enabled) {
    return universe.map(function (option) {
      return {
        value: option.value,
        label: option.label,
        disabled: !enabled(option.value),
      };
    });
  }

  function deriveSelection(model, requestedState) {
    var requested = requestedState || {};
    var all = model.combinations;
    var methodUniverse = METHOD_ORDER.map(function (method) {
      return { value: method, label: method };
    });
    var methodOptions = optionList(methodUniverse, function (method) {
      return all.some(function (item) {
        return item.method === method;
      });
    });
    var method = choose(requested.method, methodOptions);
    var methodCombinations = all.filter(function (item) {
      return item.method === method;
    });

    var rows = {
      method: { visible: true, options: methodOptions },
      engine: { visible: method !== "helm", options: [] },
      engineVersion: { visible: method !== "helm", options: [] },
      compute: { visible: method !== "helm", options: [] },
      os: { visible: method === "image", options: [] },
      architecture: { visible: method !== "helm", options: [] },
    };
    if (method === "helm") {
      return {
        state: {
          method: method,
          engine: null,
          engineVersion: null,
          compute: null,
          os: null,
          architecture: null,
        },
        rows: rows,
        combination: methodCombinations[0] || null,
        command: methodCombinations.length ? methodCombinations[0].command : null,
      };
    }

    var engineUniverse = orderedUnique(
      methodCombinations,
      "engine",
      "engine",
      ENGINE_ORDER
    ).map(function (option) {
      return { value: option.value, label: Manifest.productLabel(option.value) };
    });
    rows.engine.options = optionList(engineUniverse, function (engineValue) {
      return methodCombinations.some(function (item) {
        return item.engine === engineValue;
      });
    });
    var engine = choose(requested.engine, rows.engine.options);

    var engineVersion = null;
    var versionComputes = {};
    if (method !== "helm") {
      var publishedImageCombinations = all.filter(function (item) {
        return item.method === "image";
      });
      rows.engineVersion.options = optionList(
        engineVersionOptions(publishedImageCombinations, engine),
        function (versionValue) {
          return publishedImageCombinations.some(function (item) {
            return item.engine === engine && item.engineVersion === versionValue;
          });
        }
      );
      engineVersion = choose(requested.engineVersion, rows.engineVersion.options);
      publishedImageCombinations.forEach(function (item) {
        if (item.engine === engine && item.engineVersion === engineVersion) {
          versionComputes[item.compute] = true;
        }
      });
    }

    var computeLabels = {};
    methodCombinations.forEach(function (item) {
      if (item.engine !== engine) return;
      var value = selectedComputeValue(item, engine);
      computeLabels[value] =
        engine === "vllm-ascend" ? item.computeVariant.toUpperCase() : item.computeLabel;
    });
    var computeUniverse = Object.keys(computeLabels)
      .sort(function (left, right) {
        return computeLabels[left].localeCompare(computeLabels[right], undefined, {
          numeric: true,
        });
      })
      .map(function (value) {
        return { value: value, label: computeLabels[value] };
      });
    rows.compute.options = optionList(computeUniverse, function (computeValue) {
      return methodCombinations.some(function (item) {
        return (
          item.engine === engine &&
          (method === "image"
            ? item.engineVersion === engineVersion
            : versionComputes[item.compute] === true) &&
          selectedComputeValue(item, engine) === computeValue
        );
      });
    });
    var compute = choose(requested.compute, rows.compute.options);

    var os = null;
    if (method === "image") {
      var osUniverse = orderedUnique(methodCombinations, "os", "osLabel");
      rows.os.options = optionList(osUniverse, function (osValue) {
        return methodCombinations.some(function (item) {
          return (
            item.engine === engine &&
            item.engineVersion === engineVersion &&
            selectedComputeValue(item, engine) === compute &&
            item.os === osValue
          );
        });
      });
      os = choose(requested.os, rows.os.options);
    }

    var architectureUniverse = orderedUnique(
      methodCombinations,
      "architecture",
      "architectureLabel"
    );
    rows.architecture.options = optionList(
      architectureUniverse,
      function (architectureValue) {
        return methodCombinations.some(function (item) {
          return (
            item.engine === engine &&
            (method === "image"
              ? item.engineVersion === engineVersion
              : versionComputes[item.compute] === true) &&
            selectedComputeValue(item, engine) === compute &&
            (method !== "image" || item.os === os) &&
            item.architecture === architectureValue
          );
        });
      }
    );
    var architecture = choose(requested.architecture, rows.architecture.options);
    var combination = methodCombinations.find(function (item) {
      return (
        item.engine === engine &&
        (method === "image"
          ? item.engineVersion === engineVersion
          : versionComputes[item.compute] === true) &&
        selectedComputeValue(item, engine) === compute &&
        (method !== "image" || item.os === os) &&
        item.architecture === architecture
      );
    });

    return {
      state: {
        method: method,
        engine: engine,
        engineVersion: engineVersion,
        compute: compute,
        os: os,
        architecture: architecture,
      },
      rows: rows,
      combination: combination || null,
      command: combination ? combination.command : null,
    };
  }

  function commandBlock(command, messages) {
    var wrapper = element("div", "ucm-install__command");
    var pre = element("pre", "ucm-install__pre");
    pre.appendChild(element("code", "", command));
    var button = element("button", "ucm-install__copy");
    button.type = "button";
    button.setAttribute("aria-label", messages.copy);
    button.appendChild(element("span", "ucm-install__copy-icon", COPY_ICON));
    button.appendChild(element("span", "ucm-install__copy-label", messages.copy));
    button.addEventListener("click", function () {
      var label = button.querySelector(".ucm-install__copy-label");
      var operation =
        navigator.clipboard && navigator.clipboard.writeText
          ? navigator.clipboard.writeText(command)
          : Promise.reject(new Error("Clipboard API unavailable"));
      operation.then(
        function () {
          label.textContent = messages.copied;
        },
        function () {
          label.textContent = messages.copyFailed;
        }
      );
      window.setTimeout(function () {
        label.textContent = messages.copy;
      }, 1600);
    });
    wrapper.appendChild(pre);
    wrapper.appendChild(button);
    return wrapper;
  }

  function directLink(url, label) {
    var link = element("a", "ucm-install__link", label);
    link.href = url;
    link.rel = "noopener";
    return link;
  }

  function renderRow(name, row, selected, messages, onSelect) {
    var wrapper = element("div", "ucm-selector__row");
    wrapper.dataset.selectorRow = name;
    if (!row.visible) wrapper.hidden = true;
    wrapper.appendChild(element("div", "ucm-selector__label", messages[name]));
    var options = element("div", "ucm-selector__options");
    options.setAttribute("role", "radiogroup");
    options.setAttribute("aria-label", messages[name]);
    row.options.forEach(function (option) {
      var labelText = name === "method" ? messages[option.value] : option.label;
      var optionLabel = element("label", "ucm-selector__option");
      var input = element("input", "ucm-selector__input");
      input.type = "radio";
      input.name = "ucm-selector-" + name;
      input.value = option.value;
      input.dataset.selectorOption = option.value;
      input.setAttribute("aria-label", labelText);
      input.checked = option.value === selected;
      input.disabled = option.disabled;
      optionLabel.title = labelText;
      if (input.checked) optionLabel.classList.add("ucm-selector__option--selected");
      if (input.disabled) optionLabel.classList.add("ucm-selector__option--disabled");
      input.addEventListener("change", function () {
        if (input.checked) onSelect(name, option.value);
      });
      optionLabel.appendChild(input);
      var labelParts =
        name === "compute" || name === "engineVersion"
          ? computeLabelParts(labelText)
          : { primary: labelText, secondary: null };
      var optionText = element("span", "ucm-selector__option-text");
      optionText.appendChild(
        element("span", "ucm-selector__option-primary", labelParts.primary)
      );
      if (labelParts.secondary !== null) {
        optionText.classList.add("ucm-selector__option-text--stacked");
        optionText.appendChild(
          element("span", "ucm-selector__option-secondary", labelParts.secondary)
        );
      }
      optionLabel.appendChild(optionText);
      options.appendChild(optionLabel);
    });
    wrapper.appendChild(options);
    return wrapper;
  }

  function renderSelector(app, model, requestedState, messages) {
    var selection = deriveSelection(model, requestedState);
    var controls = app.querySelector("[data-install-selector]");
    var output = app.querySelector("[data-install-output]");
    controls.replaceChildren();

    function select(name, value) {
      var next = Object.assign({}, selection.state);
      next[name] = value;
      renderSelector(app, model, next, messages);
    }

    ROW_ORDER.forEach(function (name) {
      controls.appendChild(
        renderRow(name, selection.rows[name], selection.state[name], messages, select)
      );
    });
    output.replaceChildren();
    output.appendChild(
      element(
        "div",
        "ucm-selector__label ucm-install__output-title",
        messages.command
      )
    );
    var outputValue = element("div", "ucm-install__output-value");
    if (selection.command) {
      outputValue.appendChild(commandBlock(selection.command, messages));
    } else {
      outputValue.appendChild(element("p", "ucm-install__empty", messages.noCombination));
    }
    output.appendChild(outputValue);
    controls.appendChild(output);
    app.dataset.selectedMethod = selection.state.method || "";
    return selection;
  }

  function initialize() {
    var app = document.getElementById("ucm-install-app");
    if (!app || app.dataset.manifestLoading === "1" || app.dataset.manifestReady === "1") {
      return;
    }
    app.dataset.manifestLoading = "1";
    var locale = app.dataset.locale === "zh" ? "zh" : "en";
    var messages = TEXT[locale];
    var status = app.querySelector("[data-install-status]");
    status.textContent = messages.loading;
    status.className = "ucm-install__status";
    var manifestUrl = Manifest.defaultManifestUrl();

    Manifest.loadManifest(manifestUrl)
      .then(function (manifest) {
        var model = buildSelectorModel(manifest, manifestUrl);
        status.replaceChildren(
          document.createTextNode(messages.release + ": "),
          directLink(manifest.release.url, manifest.release.version)
        );
        status.className = "ucm-install__status ucm-install__status--ready";
        renderSelector(app, model, {}, messages);
        app.dataset.manifestReady = "1";
      })
      .catch(function (error) {
        status.textContent =
          error instanceof TypeError ? messages.invalid : messages.unavailable;
        status.className = "ucm-install__status ucm-install__status--unavailable";
      })
      .finally(function () {
        app.dataset.manifestLoading = "0";
      });
  }

  return {
    TEXT: TEXT,
    ROW_ORDER: ROW_ORDER.slice(),
    architectureLabel: architectureLabel,
    computeLabelParts: computeLabelParts,
    displayEngineVersion: displayEngineVersion,
    engineVersionOptions: engineVersionOptions,
    selectedComputeValue: selectedComputeValue,
    buildSelectorModel: buildSelectorModel,
    deriveSelection: deriveSelection,
    initialize: initialize,
  };
});
