(function (root, factory) {
  "use strict";

  var manifestApi = root.UcmReleaseManifest;
  if (!manifestApi && typeof module === "object" && module.exports) {
    manifestApi = require("./manifest.js");
  }
  var api = factory(manifestApi);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.UcmDownloadInventory = api;

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

  if (!Manifest) throw new Error("UcmReleaseManifest must be loaded before download.js");

  var TEXT = {
    en: {
      loading: "Loading the current release manifest...",
      unavailable: "No release manifest is published for this documentation version yet.",
      invalid: "The published release manifest is invalid.",
      release: "Release",
      wheel: "Wheel",
      wheels: "Wheels",
      image: "Image",
      images: "Image families",
      helm: "Helm",
      overviewWheel: "Browse PyPI extras, backend Wheels, ABIs, architectures, and Release assets.",
      overviewImage: "Browse every published image family and registry publication.",
      overviewHelm: "Download the Release Chart and use its OCI reference when published.",
      extra: "Extra",
      backend: "Backend package",
      platformTags: "Wheel platform",
      metaPackage: "Python meta package",
      pypiProject: "PyPI project",
      pypiUnavailable: "This release was not published to PyPI. Use the Release assets below.",
      freshEnvironment: "Install only one backend extra in a fresh Python environment.",
      version: "Version",
      pythonAbi: "Python ABI",
      architecture: "Architecture",
      compute: "Compute",
      platform: "Platform",
      upstream: "Upstream",
      publication: "Publication",
      members: "Architecture members",
      download: "Download",
      chartAsset: "Release Chart",
      chartOci: "OCI reference",
      empty: "No published artifacts are available.",
    },
    zh: {
      loading: "正在加载当前版本的 Release Manifest……",
      unavailable: "此文档版本尚未发布 Release Manifest。",
      invalid: "已发布的 Release Manifest 格式无效。",
      release: "Release",
      wheel: "Wheel",
      wheels: "Wheel",
      image: "镜像",
      images: "镜像 Family",
      helm: "Helm",
      overviewWheel: "查看 PyPI Extra、Backend Wheel、ABI、架构和 Release 资产。",
      overviewImage: "查看全部已发布镜像 Family 及其 Registry 发布结果。",
      overviewHelm: "下载 Release Chart；如已发布，也可以使用 OCI 引用。",
      extra: "Extra",
      backend: "Backend 包",
      platformTags: "Wheel 平台",
      metaPackage: "Python Meta 包",
      pypiProject: "PyPI 项目",
      pypiUnavailable: "此 Release 未发布到 PyPI，请使用下方 Release 资产。",
      freshEnvironment: "请在全新的 Python 环境中只安装一个 Backend Extra。",
      version: "版本",
      pythonAbi: "Python ABI",
      architecture: "架构",
      compute: "计算平台",
      platform: "运行平台",
      upstream: "上游版本",
      publication: "发布位置",
      members: "架构成员",
      download: "下载",
      chartAsset: "Release Chart",
      chartOci: "OCI 引用",
      empty: "当前没有已发布制品。",
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

  function directLink(url, label, className) {
    var link = element("a", className || "ucm-install__link", label);
    link.href = url;
    link.rel = "noopener";
    return link;
  }

  function definition(label, value) {
    var wrapper = element("div", "ucm-install__fact");
    wrapper.appendChild(element("dt", "ucm-install__term", label));
    wrapper.appendChild(element("dd", "ucm-install__value", value));
    return wrapper;
  }

  function linkDefinition(label, url, value) {
    var wrapper = element("div", "ucm-install__fact");
    wrapper.appendChild(element("dt", "ucm-install__term", label));
    var description = element("dd", "ucm-install__value");
    description.appendChild(directLink(url, value));
    wrapper.appendChild(description);
    return wrapper;
  }

  function pypiProjectUrl(distribution, version) {
    return (
      "https://pypi.org/project/" +
      encodeURIComponent(distribution) +
      "/" +
      encodeURIComponent(version) +
      "/"
    );
  }

  function command(command) {
    var wrapper = element("div", "ucm-install__command");
    var pre = element("pre", "ucm-install__pre ucm-install__pre--inventory");
    pre.appendChild(element("code", "", command));
    wrapper.appendChild(pre);
    return wrapper;
  }

  function buildInventory(manifest) {
    Manifest.validateManifest(manifest);
    var wheelGroups = {};
    manifest.wheels.forEach(function (wheel) {
      var groupName = wheel.extra;
      if (!wheelGroups[groupName]) {
        wheelGroups[groupName] = {
          extra: groupName,
          distribution: wheel.distribution,
          wheels: [],
        };
      }
      wheelGroups[groupName].wheels.push(wheel);
    });
    var groups = Object.keys(wheelGroups)
      .sort()
      .map(function (channel) {
        var group = wheelGroups[channel];
        group.wheels.sort(function (left, right) {
          return (
            left.python_abi.localeCompare(right.python_abi) ||
            left.architecture.localeCompare(right.architecture) ||
            left.filename.localeCompare(right.filename)
          );
        });
        return group;
      });
    var images = manifest.images.slice().sort(function (left, right) {
      return (
        left.product.localeCompare(right.product) ||
        left.upstream.version.localeCompare(right.upstream.version) ||
        left.id.localeCompare(right.id)
      );
    });
    return {
      release: manifest.release,
      python: manifest.python,
      wheelGroups: groups,
      wheelCount: manifest.wheels.length,
      images: images,
      imageCount: images.length,
      chart: manifest.chart,
    };
  }

  function cardHeading(title, badge) {
    var heading = element("div", "ucm-install__card-heading");
    heading.appendChild(element("h3", "ucm-install__card-title", title));
    if (badge) heading.appendChild(element("span", "ucm-install__badge", badge));
    return heading;
  }

  function overviewEntries(inventory, messages) {
    return [
      {
        href: "whl/",
        title: messages.wheel,
        badge: inventory.wheelCount + " " + messages.wheels,
        description: messages.overviewWheel,
      },
      {
        href: "helm/",
        title: messages.helm,
        badge: inventory.chart.version,
        description: messages.overviewHelm,
      },
      {
        href: "image/",
        title: messages.image,
        badge: inventory.imageCount + " " + messages.images,
        description: messages.overviewImage,
      },
    ];
  }

  function renderOverview(root, inventory, messages) {
    var entries = overviewEntries(inventory, messages);
    var grid = element("div", "ucm-install__grid");
    entries.forEach(function (entry) {
      var card = element("article", "ucm-install__card ucm-download__overview-card");
      card.appendChild(cardHeading(entry.title, entry.badge));
      card.appendChild(element("p", "", entry.description));
      card.appendChild(directLink(entry.href, entry.title, "ucm-install__download"));
      grid.appendChild(card);
    });
    root.appendChild(grid);
  }

  function renderWheels(root, inventory, messages) {
    if (!inventory.wheelGroups.length) {
      root.appendChild(element("p", "ucm-install__empty", messages.empty));
      return;
    }
    var meta = element("article", "ucm-install__card ucm-download__card");
    meta.appendChild(cardHeading(messages.metaPackage, inventory.python.version));
    meta.appendChild(element("p", "", messages.freshEnvironment));
    var metaFacts = element("dl", "ucm-install__facts");
    metaFacts.appendChild(definition(messages.version, inventory.python.version));
    metaFacts.appendChild(
      definition(messages.platformTags, inventory.python.tags.join(", "))
    );
    if (inventory.python.pypi !== null) {
      metaFacts.appendChild(
        linkDefinition(
          messages.pypiProject,
          inventory.python.pypi.project_url,
          inventory.python.distribution
        )
      );
    }
    meta.appendChild(metaFacts);
    if (inventory.python.pypi === null) {
      meta.appendChild(
        element("p", "ucm-install__empty", messages.pypiUnavailable)
      );
    }
    meta.appendChild(
      directLink(
        inventory.python.url,
        inventory.python.filename,
        "ucm-install__download"
      )
    );
    root.appendChild(meta);
    inventory.wheelGroups.forEach(function (group) {
      var card = element("article", "ucm-install__card ucm-download__card");
      card.appendChild(cardHeading(group.extra, group.distribution));
      var packageFacts = element("dl", "ucm-install__facts");
      var installCommand = null;
      packageFacts.appendChild(definition(messages.extra, group.extra));
      packageFacts.appendChild(definition(messages.backend, group.distribution));
      if (inventory.python.pypi !== null) {
        packageFacts.appendChild(
          linkDefinition(
            messages.pypiProject,
            pypiProjectUrl(group.distribution, inventory.python.version),
            group.distribution
          )
        );
        installCommand =
          'pip install "' +
          inventory.python.distribution +
          "[" +
          group.extra +
          "]==" +
          inventory.python.version +
          '"';
      }
      card.appendChild(packageFacts);
      if (installCommand !== null) card.appendChild(command(installCommand));
      var list = element("div", "ucm-install__wheel-list");
      group.wheels.forEach(function (wheel) {
        var row = element("div", "ucm-install__wheel");
        var facts = element("dl", "ucm-install__facts");
        facts.appendChild(definition(messages.version, wheel.version));
        facts.appendChild(definition(messages.pythonAbi, wheel.python_abi));
        facts.appendChild(
          definition(messages.architecture, architectureLabel(wheel.architecture))
        );
        facts.appendChild(
          definition(messages.platformTags, wheel.platform_tags.join(", "))
        );
        row.appendChild(facts);
        row.appendChild(
          directLink(wheel.url, wheel.filename, "ucm-install__download")
        );
        list.appendChild(row);
      });
      card.appendChild(list);
      root.appendChild(card);
    });
  }

  function publicationRows(image) {
    return ["ghcr", "dockerhub"]
      .filter(function (channel) {
        return image.publications[channel] !== null;
      })
      .map(function (channel) {
        return { channel: channel, value: image.publications[channel] };
      });
  }

  function renderImages(root, inventory, messages) {
    if (!inventory.images.length) {
      root.appendChild(element("p", "ucm-install__empty", messages.empty));
      return;
    }
    inventory.images.forEach(function (image) {
      var card = element("article", "ucm-install__card ucm-download__card");
      card.appendChild(
        cardHeading(Manifest.productLabel(image.product), image.accelerator.variant)
      );
      var facts = element("dl", "ucm-install__facts ucm-install__facts--image");
      facts.appendChild(
        definition(
          messages.upstream,
          image.upstream.version + " / " + image.upstream.channel
        )
      );
      facts.appendChild(
        definition(messages.compute, Manifest.acceleratorLabel(image.accelerator))
      );
      facts.appendChild(definition(messages.platform, Manifest.osLabel(image.os)));
      card.appendChild(facts);
      publicationRows(image).forEach(function (published) {
        var block = element("section", "ucm-download__publication");
        block.appendChild(
          element("h4", "ucm-download__publication-title", published.channel)
        );
        block.appendChild(command("docker pull " + published.value.pull));
        var members = published.value.members
          .map(function (member) {
            return architectureLabel(member.architecture) + ": " + member.reference;
          })
          .join("\n");
        var memberFacts = element("dl", "ucm-install__facts");
        memberFacts.appendChild(definition(messages.members, members));
        block.appendChild(memberFacts);
        card.appendChild(block);
      });
      root.appendChild(card);
    });
  }

  function renderHelm(root, inventory, messages) {
    var chart = inventory.chart;
    var card = element("article", "ucm-install__card ucm-download__card");
    card.appendChild(cardHeading(chart.name, chart.version));
    var facts = element("dl", "ucm-install__facts");
    facts.appendChild(definition(messages.version, chart.version));
    if (chart.oci) facts.appendChild(definition(messages.chartOci, chart.oci));
    card.appendChild(facts);
    card.appendChild(command("helm install ucm " + chart.url));
    card.appendChild(
      directLink(chart.url, messages.download + " " + chart.filename, "ucm-install__download")
    );
    root.appendChild(card);
  }

  function render(root, kind, inventory, messages) {
    root.replaceChildren();
    if (kind === "wheel") renderWheels(root, inventory, messages);
    else if (kind === "image") renderImages(root, inventory, messages);
    else if (kind === "helm") renderHelm(root, inventory, messages);
    else renderOverview(root, inventory, messages);
  }

  function initialize() {
    var apps = Array.prototype.slice.call(
      document.querySelectorAll("[data-ucm-download]")
    );
    apps.forEach(function (app) {
      if (app.dataset.manifestLoading === "1" || app.dataset.manifestReady === "1") {
        return;
      }
      app.dataset.manifestLoading = "1";
      var locale = app.dataset.locale === "zh" ? "zh" : "en";
      var messages = TEXT[locale];
      var status = app.querySelector("[data-download-status]");
      var content = app.querySelector("[data-download-content]");
      status.textContent = messages.loading;
      status.className = "ucm-install__status";
      var manifestUrl = Manifest.defaultManifestUrl();
      Manifest.loadManifest(manifestUrl)
        .then(function (manifest) {
          var inventory = buildInventory(manifest);
          status.replaceChildren(
            document.createTextNode(messages.release + ": "),
            directLink(manifest.release.url, manifest.release.version)
          );
          status.className = "ucm-install__status ucm-install__status--ready";
          render(content, app.dataset.downloadKind || "overview", inventory, messages);
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
    });
  }

  return {
    TEXT: TEXT,
    buildInventory: buildInventory,
    overviewEntries: overviewEntries,
    publicationRows: publicationRows,
    initialize: initialize,
  };
});
