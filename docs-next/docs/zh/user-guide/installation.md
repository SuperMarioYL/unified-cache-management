# 安装

从本页标明的完整发布中选择制品。标签版本文档使用对应 Release；latest 和 PR 预览使用同一仓库中符合条件的最新稳定 Release。选择器显示实际安装版本。更改选项后，选择器会选择第一个有效的已发布组合，并生成对应的完整安装命令。可通过站点版本菜单切换 UCM 文档版本。

安装 Wheel 时，使用独立的 Python 环境，并且只安装一个 backend extra。不同后端共用 `ucm` 导入命名空间。

<div id="ucm-install-app" class="ucm-install" data-locale="zh">
  <p class="ucm-install__status" data-install-status aria-live="polite">
    正在加载当前发布清单……
  </p>
  <div class="ucm-selector" data-install-selector></div>
  <section class="ucm-install__output" data-install-output aria-live="polite"></section>
</div>

<noscript>
  请启用 JavaScript，以加载发布清单并生成安装命令。
</noscript>

## 后续步骤

- [vLLM（CUDA）](quick_start/quickstart_vllm.md)
- [vLLM-Ascend（NPU）](quick_start/quickstart_vllm_ascend.md)
- [Kubernetes 部署](frameworks/kubernetes.md)
- [从源码构建](../developer-guide/build_from_source.md)
