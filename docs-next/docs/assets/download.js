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
      overviewWheel: "Browse every Wheel, ABI, architecture, Simple Index, and direct link.",
      overviewImage: "Browse every published image family and registry publication.",
      overviewHelm: "Download the Release Chart and use its OCI reference when published.",
      channel: "Channel",
      index: "Simple Index",
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
      overviewWheel: "查看全部 Wheel、ABI、架构、Simple Index 和直接下载链接。",
      overviewImage: "查看全部已发布镜像 Family 及其 Registry 发布结果。",
      overviewHelm: "下载 Release Chart；如已发布，也可以使用 OCI 引用。",
      channel: "Channel",
      index: "Simple Index",
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

  function command(command) {
    var wrapper = element("div", "ucm-install__command");
    var pre = element("pre", "ucm-install__pre ucm-install__pre--inventory");
    pre.appendChild(element("code", "", command));
    wrapper.appendChild(pre);
    return wrapper;
  }

  function buildInventory(manifest, manifestUrl) {
    Manifest.validateManifest(manifest);
    var target = new URL(String(manifestUrl || Manifest.defaultManifestUrl()));
    var wheelGroups = {};
    manifest.wheels.forEach(function (wheel) {
      if (!wheelGroups[wheel.channel]) {
        wheelGroups[wheel.channel] = {
          channel: wheel.channel,
          indexUrl: new URL(
            "../whl/" + encodeURIComponent(wheel.channel) + "/",
            target
          ).href,
          wheels: [],
        };
      }
      wheelGroups[wheel.channel].wheels.push(wheel);
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
    inventory.wheelGroups.forEach(function (group) {
      var card = element("article", "ucm-install__card ucm-download__card");
      card.appendChild(
        cardHeading(group.channel, group.wheels.length + " " + messages.wheels)
      );
      var indexFacts = element("dl", "ucm-install__facts");
      var indexFact = element("div", "ucm-install__fact");
      indexFact.appendChild(element("dt", "ucm-install__term", messages.index));
      var value = element("dd", "ucm-install__value");
      value.appendChild(directLink(group.indexUrl, group.indexUrl));
      indexFact.appendChild(value);
      indexFacts.appendChild(indexFact);
      card.appendChild(indexFacts);
      var list = element("div", "ucm-install__wheel-list");
      group.wheels.forEach(function (wheel) {
        var row = element("div", "ucm-install__wheel");
        var facts = element("dl", "ucm-install__facts");
        facts.appendChild(definition(messages.version, wheel.version));
        facts.appendChild(definition(messages.pythonAbi, wheel.python_abi));
        facts.appendChild(
          definition(messages.architecture, architectureLabel(wheel.architecture))
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
          var inventory = buildInventory(manifest, manifestUrl);
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
