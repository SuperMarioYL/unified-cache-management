(function () {
  "use strict";

  var scriptSource = document.currentScript && document.currentScript.src;
  var scriptUrl = new URL(scriptSource || "assets/install.js", document.baseURI);
  var catalogUrl = new URL("../install-catalog.json", scriptUrl);

  var COPY_ICON = "\u2398";
  var TEXT = {
    en: {
      loading: "Loading the current Stable release catalog...",
      unavailable:
        "No Stable install catalog is published for this documentation version yet.",
      invalid: "The published install catalog is invalid.",
      wheelsEmpty: "No published Python wheels are available.",
      imagesEmpty: "No published runtime images are available.",
      channel: "Channel",
      localVersion: "Wheel version",
      pythonAbi: "Python ABI",
      architecture: "Architecture",
      download: "Download wheel",
      copy: "Copy",
      copied: "Copied",
      copyFailed: "Copy failed",
      release: "Release",
      upstream: "Upstream",
      accelerator: "Accelerator",
      platform: "Platform",
      imageReference: "Published image",
      chartDownload: "GitHub Release chart",
      chartOci: "OCI chart",
      sourceUnavailable: "The Release URL does not identify a GitHub repository.",
    },
    zh: {
      loading: "正在加载当前 Stable 版本目录……",
      unavailable: "此文档版本暂时没有可用的 Stable 安装目录。",
      invalid: "已发布的安装目录格式无效。",
      wheelsEmpty: "当前没有已发布的 Python Wheel。",
      imagesEmpty: "当前没有已发布的运行时镜像。",
      channel: "Channel",
      localVersion: "Wheel 版本",
      pythonAbi: "Python ABI",
      architecture: "架构",
      download: "下载 Wheel",
      copy: "复制",
      copied: "已复制",
      copyFailed: "复制失败",
      release: "Release",
      upstream: "上游版本",
      accelerator: "加速运行时",
      platform: "运行平台",
      imageReference: "已发布镜像",
      chartDownload: "GitHub Release Chart",
      chartOci: "OCI Chart",
      sourceUnavailable: "Release 地址无法识别为 GitHub 仓库。",
    },
  };

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function definition(label, value) {
    var wrapper = element("div", "ucm-install__fact");
    wrapper.appendChild(element("dt", "ucm-install__term", label));
    wrapper.appendChild(element("dd", "ucm-install__value", value));
    return wrapper;
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
      var operation = navigator.clipboard && navigator.clipboard.writeText
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

  function directLink(url, label, className) {
    var link = element("a", className || "ucm-install__link", label);
    link.href = url;
    link.rel = "noopener";
    return link;
  }

  function groupBy(items, field) {
    var groups = {};
    items.forEach(function (item) {
      var key = String(item[field]);
      if (!groups[key]) groups[key] = [];
      groups[key].push(item);
    });
    return groups;
  }

  function renderWheels(root, catalog, messages) {
    root.replaceChildren();
    if (!catalog.wheels.length) {
      root.appendChild(element("p", "ucm-install__empty", messages.wheelsEmpty));
      return;
    }
    var groups = groupBy(catalog.wheels, "channel");
    Object.keys(groups)
      .sort()
      .forEach(function (channel) {
        var card = element("article", "ucm-install__card");
        var heading = element("div", "ucm-install__card-heading");
        heading.appendChild(element("h3", "ucm-install__card-title", channel));
        heading.appendChild(
          element("span", "ucm-install__badge", groups[channel].length + " Wheel")
        );
        card.appendChild(heading);
        var indexUrl = new URL(
          "../whl/" + encodeURIComponent(channel) + "/",
          catalogUrl
        ).href;
        card.appendChild(
          commandBlock(
            'python -m pip install "uc-manager==' +
              groups[channel][0].version +
              '" --index-url ' +
              indexUrl,
            messages
          )
        );
        var table = element("div", "ucm-install__wheel-list");
        groups[channel]
          .slice()
          .sort(function (left, right) {
            return (
              String(left.python_abi).localeCompare(String(right.python_abi)) ||
              String(left.cpu_arch).localeCompare(String(right.cpu_arch)) ||
              String(left.filename).localeCompare(String(right.filename))
            );
          })
          .forEach(function (wheel) {
            var row = element("div", "ucm-install__wheel");
            var facts = element("dl", "ucm-install__facts");
            facts.appendChild(definition(messages.localVersion, wheel.version));
            facts.appendChild(definition(messages.pythonAbi, wheel.python_abi));
            facts.appendChild(definition(messages.architecture, wheel.cpu_arch));
            row.appendChild(facts);
            row.appendChild(
              directLink(wheel.url, messages.download, "ucm-install__download")
            );
            table.appendChild(row);
          });
        card.appendChild(table);
        root.appendChild(card);
      });
  }

  function visibleImage(image) {
    var product = String(image.product).toLowerCase();
    var unsupportedVariant = [image.variant, image.soc_version].some(function (value) {
      return /(^|[-_.])a5($|[-_.])/i.test(String(value));
    });
    return (product === "vllm" || product === "vllm-ascend") && !unsupportedVariant;
  }

  function preferredReference(references) {
    if (references.ghcr) return references.ghcr;
    if (references.dockerhub) return references.dockerhub;
    var channels = Object.keys(references).sort();
    return channels.length ? references[channels[0]] : null;
  }

  function productLabel(product) {
    return product === "vllm-ascend" ? "vLLM-Ascend" : "vLLM";
  }

  function renderImages(root, catalog, messages) {
    root.replaceChildren();
    var images = catalog.images.filter(visibleImage);
    if (!images.length) {
      root.appendChild(element("p", "ucm-install__empty", messages.imagesEmpty));
      return;
    }
    images.forEach(function (image) {
      var reference = preferredReference(image.references);
      if (!reference) return;
      var card = element("article", "ucm-install__card");
      var heading = element("div", "ucm-install__card-heading");
      heading.appendChild(
        element("h3", "ucm-install__card-title", productLabel(image.product))
      );
      heading.appendChild(element("span", "ucm-install__badge", image.variant));
      card.appendChild(heading);
      var facts = element("dl", "ucm-install__facts ucm-install__facts--image");
      facts.appendChild(
        definition(
          messages.upstream,
          image.upstream_version + " / " + image.upstream_channel
        )
      );
      facts.appendChild(
        definition(
          messages.accelerator,
          image.accelerator_runtime + " / " + image.soc_version
        )
      );
      facts.appendChild(
        definition(
          messages.platform,
          image.os_id + " " + image.os_version + " / " + image.architectures.join(", ")
        )
      );
      facts.appendChild(definition(messages.imageReference, reference));
      card.appendChild(facts);
      card.appendChild(commandBlock("docker pull " + reference, messages));
      root.appendChild(card);
    });
  }

  function renderChart(root, catalog, messages) {
    root.replaceChildren();
    var chart = catalog.chart;
    var card = element("article", "ucm-install__card");
    var facts = element("dl", "ucm-install__facts");
    facts.appendChild(definition(messages.release, chart.version));
    if (chart.oci) facts.appendChild(definition(messages.chartOci, chart.oci));
    card.appendChild(facts);
    card.appendChild(commandBlock("helm install ucm " + chart.url, messages));
    card.appendChild(
      directLink(chart.url, messages.chartDownload, "ucm-install__download")
    );
    root.appendChild(card);
  }

  function repositoryFromRelease(releaseUrl) {
    try {
      var parsed = new URL(releaseUrl);
      if (parsed.hostname !== "github.com") return null;
      var parts = parsed.pathname.split("/").filter(Boolean);
      var releases = parts.indexOf("releases");
      if (releases !== 2) return null;
      return parsed.origin + "/" + parts[0] + "/" + parts[1] + ".git";
    } catch (error) {
      return null;
    }
  }

  function renderSource(root, catalog, messages) {
    root.replaceChildren();
    var repository = repositoryFromRelease(catalog.release.url);
    if (!repository) {
      root.appendChild(element("p", "ucm-install__empty", messages.sourceUnavailable));
      return;
    }
    var command =
      "git clone --branch " + catalog.release.tag + " " + repository +
      "\ncd " + repository.split("/").pop().replace(/\.git$/, "");
    var card = element("article", "ucm-install__card");
    card.appendChild(commandBlock(command, messages));
    card.appendChild(
      directLink(catalog.release.url, messages.release, "ucm-install__download")
    );
    root.appendChild(card);
  }

  function validCatalog(catalog) {
    return (
      catalog &&
      catalog.kind === "ucm-install-catalog" &&
      catalog.schema_version === 1 &&
      catalog.release &&
      typeof catalog.release.version === "string" &&
      Array.isArray(catalog.wheels) &&
      Array.isArray(catalog.images) &&
      catalog.chart
    );
  }

  function initialize() {
    var app = document.getElementById("ucm-install-app");
    if (!app || app.dataset.catalogLoading === "1") return;
    app.dataset.catalogLoading = "1";
    var locale = app.dataset.locale === "zh" ? "zh" : "en";
    var messages = TEXT[locale];
    var status = app.querySelector("[data-install-status]");
    status.textContent = messages.loading;
    status.className = "ucm-install__status";

    fetch(catalogUrl.href, { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("Catalog unavailable");
        return response.json();
      })
      .then(function (catalog) {
        if (!validCatalog(catalog)) throw new TypeError("Invalid Catalog");
        status.replaceChildren(
          document.createTextNode(messages.release + ": "),
          directLink(catalog.release.url, catalog.release.tag)
        );
        status.className = "ucm-install__status ucm-install__status--ready";
        renderWheels(app.querySelector("[data-install-wheels]"), catalog, messages);
        renderImages(app.querySelector("[data-install-images]"), catalog, messages);
        renderChart(app.querySelector("[data-install-chart]"), catalog, messages);
        renderSource(app.querySelector("[data-install-source]"), catalog, messages);
      })
      .catch(function (error) {
        status.textContent =
          error instanceof TypeError ? messages.invalid : messages.unavailable;
        status.className = "ucm-install__status ucm-install__status--unavailable";
      })
      .finally(function () {
        app.dataset.catalogLoading = "0";
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize);
  } else {
    initialize();
  }
  if (typeof document$ !== "undefined") document$.subscribe(initialize);
})();
