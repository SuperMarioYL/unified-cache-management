# 安装

这里只提供当前文档版本实际发布的制品组合。切换选项时，页面会自动选择
第一个有效组合，并只生成一条精确安装命令。跨 UCM 版本请使用站点版本菜单。

使用 Wheel 安装时，请在全新的 Python 环境中只安装一个 Backend Extra；
不同 Extra 共享同一个 `ucm` 导入命名空间。

<div id="ucm-install-app" class="ucm-install" data-locale="zh">
  <p class="ucm-install__status" data-install-status aria-live="polite">
    正在加载当前版本的 Release Manifest……
  </p>
  <div class="ucm-selector" data-install-selector></div>
  <section class="ucm-install__output" data-install-output aria-live="polite"></section>
</div>

<noscript>
  请启用 JavaScript 以加载 Release Manifest 并生成安装命令。所有已发布制品
  仍可在 <a href="../../download/">下载</a> 页面中查看。
</noscript>

如需查看全部 ABI、架构、直接链接和 Registry 发布结果，请前往
[下载](../../download/index.md)。

## 后续步骤

- [在 CUDA 上使用 vLLM](quick_start/quickstart_vllm.md)
- [在 NPU 上使用 vLLM-Ascend](quick_start/quickstart_vllm_ascend.md)
- [Kubernetes 部署](frameworks/kubernetes.md)
- [从源码构建](../../developer-guide/build_from_source.md)
