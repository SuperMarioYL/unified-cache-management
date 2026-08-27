# 安装

本页读取当前 Stable UCM Release 发布的安装目录。下方 Wheel、镜像和 Chart
均直接来自该目录；没有实际发布的组合不会显示。

<div id="ucm-install-app" class="ucm-install" data-locale="zh">
  <p class="ucm-install__status" data-install-status aria-live="polite">
    正在加载当前 Stable 版本目录……
  </p>

  <section class="ucm-install__section" aria-labelledby="ucm-wheels-heading">
    <h2 id="ucm-wheels-heading">Python Wheel</h2>
    <p>选择能力 Channel，并使用对应的独立 pip Simple Index。</p>
    <div class="ucm-install__grid" data-install-wheels></div>
  </section>

  <section class="ucm-install__section" aria-labelledby="ucm-images-heading">
    <h2 id="ucm-images-heading">运行时镜像</h2>
    <p>拉取与实际上游运行时和平台对应的已发布镜像。</p>
    <div class="ucm-install__grid" data-install-images></div>
  </section>

  <section class="ucm-install__section" aria-labelledby="ucm-chart-heading">
    <h2 id="ucm-chart-heading">Helm Chart</h2>
    <p>安装同一 Stable Release 附带的 Chart 资产。</p>
    <div class="ucm-install__grid" data-install-chart></div>
  </section>

  <section class="ucm-install__section" aria-labelledby="ucm-source-heading">
    <h2 id="ucm-source-heading">从源码构建</h2>
    <p>检出安装目录对应的不可变源码 Tag。</p>
    <div class="ucm-install__grid" data-install-source></div>
  </section>
</div>

<noscript>
  请启用 JavaScript 以加载安装目录并生成命令。Release 资产仍可从项目
  Releases 页面访问。
</noscript>

## 后续步骤

- [在 CUDA 上使用 vLLM](quick_start/quickstart_vllm.md)
- [在 NPU 上使用 vLLM-Ascend](quick_start/quickstart_vllm_ascend.md)
- [Kubernetes 部署](frameworks/kubernetes.md)
