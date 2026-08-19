# UCM 文档网站 MkDocs 改造实施计划

## 目标
按评审文档方案三，在 `docs-next/` 并行建设 MkDocs Material 站点，按新导航 `Home / User Guide / Reference / Benchmark / Developer Guide / Toolkit` 重组并迁移现有英文内容，配置双语能力，最后本地 `mkdocs serve` 启动预览。**不动** `docs/`（现 Sphinx）与 `.readthedocs.yaml`，现网继续服务。

## 1. 目录结构（新建 docs-next/，完全隔离）
```
docs-next/
├── mkdocs.yml                 # 主配置：Material 主题 + i18n + nav
├── requirements.txt           # mkdocs-material / mkdocs-static-i18n / pymdown-extensions / mkdocs-macros
├── overrides/                 # 主题覆盖（首页 partial、安装选择器样式）
├── docs/
│   ├── assets/                # 共享静态资源（从 docs/source/_static 迁移）
│   │   ├── images/            # 28 张图 + logos
│   │   ├── css/               # logo.css、计算器 styles.css
│   │   ├── js/                # model-configs.js、calculator.js、install-matrix.js（新）
│   │   └── kv_cache_calculator.html
│   ├── en/                    # 英文内容（主，完整迁移）
│   │   ├── index.md           # Home 首页（重写为 Material 风格）
│   │   ├── about.md
│   │   ├── user-guide/
│   │   │   ├── installation.md          # 安装选择器页（新，交互版）
│   │   │   ├── engines/
│   │   │   │   ├── vllm.md               # ← quickstart_vllm
│   │   │   │   ├── vllm-ascend.md        # ← quickstart_vllm_ascend
│   │   │   │   ├── sglang.md             # ← quickstart_sglang
│   │   │   │   └── mindie.md             # ← quickstart_mindie_llm（原孤儿页，纳入导航）
│   │   │   ├── deploy/
│   │   │   │   └── glm-pd-best-practice.md  # ← best-practices/GLM-5.1...
│   │   │   ├── capabilities/
│   │   │   │   ├── prefix-cache/
│   │   │   │   │   ├── index.md          # ← prefix-cache/index
│   │   │   │   │   ├── pipeline-store.md # ← pipeline_store
│   │   │   │   │   ├── nfs-store.md      # ← nfs_store
│   │   │   │   │   ├── ds3fs-store.md     # ← ds3fs_store
│   │   │   │   │   ├── mooncake-store.md  # ← mooncake_store
│   │   │   │   │   └── compress-store.md # ← compress_store
│   │   │   │   ├── sparse-attention/
│   │   │   │   │   ├── index.md          # ← sparse-attention/index
│   │   │   │   │   ├── gsa.md            # ← gsa
│   │   │   │   │   └── cacheblend.md     # ← cacheblend
│   │   │   │   ├── pd-disaggregation/
│   │   │   │   │   ├── index.md
│   │   │   │   │   ├── centralized-pd.md
│   │   │   │   │   ├── distributed-pd.md
│   │   │   │   │   └── large-scale-ep.md
│   │   │   │   └── rerope.md              # ← rerope（启用 KaTeX 数学）
│   │   │   ├── metrics.md                # ← metrics
│   │   │   └── troubleshooting.md        # ← troubleshooting
│   │   ├── reference/
│   │   │   ├── compatibility.md          # ← support_matrix
│   │   │   └── api-parameters.md         # 占位（待补 OpenAI API、引擎参数）
│   │   ├── benchmark/
│   │   │   └── index.md                  # 占位
│   │   ├── developer-guide/
│   │   │   ├── contribute.md             # ← contribute（更新构建命令为 mkdocs）
│   │   │   ├── architecture.md           # ← deepdive_ucm
│   │   │   ├── add-metrics.md            # ← add_metrics
│   │   │   └── extending-store.md        # ← extending_store
│   │   └── toolkit/
│   │       └── kv-cache-calculator.md   # ← kv_cache_calculator（iframe 路径调整）
│   └── zh-cn/                            # 中文镜像（样例，其余 i18n fallback 英文）
│       ├── index.md                      # 首页中文样例
│       └── about.md                     # about 中文样例
└── tools/
    └── site.py                          # 薄封装：serve/build/validate/translate
```

## 2. 内容迁移映射表（30 个现有文件全部安置）
- 现有 MyST `:::{toctree}` → 全部重写进 `mkdocs.yml` 的 `nav:`。
- `:::{figure}`（仅 index.md logo）→ 标准 `![]()` 或 Material 卡片。
- `:::{raw} html`（GitHub 按钮）→ Material `repo` 配置 + 首页自定义 HTML。
- 图片路径 `../../_static/images/x` → `../../assets/images/x`（批量改写，约 11 处本地图 + iframe）。
- `<details>`/`<figure>`/`<br>`/shield.io 徽章/HTML div → Material 原生支持，保留。
- `rerope.md` 的 `$$...$$` → 启用 `pymdownx.arithmatex` + KaTeX。
- `contribute.md` 的 `make html` 说明 → 更新为 `mkdocs build/serve`。
- 超宽表（compress_store 14 列）→ 保留，靠 Material 横向滚动。

## 3. mkdocs.yml 关键配置
- `theme: material`，特性：`navigation.tabs`、`navigation.sections`、`navigation.expand`、`toc.integrate`、`search.suggest`、`content.code.copy`、`content.code.annotate`、`content.tabs.link`。
- `repo_url` / `edit_uri` 指向 GitHub（对应原 `use_edit_page_button`）。
- `markdown_extensions`: `pymdownx.superfences`、`pymdownx.tabbed`、`pymdownx.arithmatex`、`admonition`、`attr_list`、`md_in_html`、`toc`、`tables`。
- `plugins`: `i18n`（mkdocs-static-i18n，folder 模式，`default_language: en`，`languages: {en: English, zh: 简体中文}`，`fallback_to_default: true`）、`search`。
- `nav:` 按新六模块嵌套。
- `extra`: 版本入口、社交链接。

## 4. 双语策略
- 用 mkdocs-static-i18n 的 folder 模式（`docs/en/`、`docs/zh-cn/`）。
- 英文完整迁移；中文先放 `index.md` + `about.md` 样例，其余页面缺失时 `fallback_to_default` 显示英文。
- AI 自动生成中文属 CI 能力（评审模块二），本地预览不做；site.py 留 `translate` 占位入口。
- 语言选择器由 i18n 插件自动注入 Material 顶栏。
- 兼容性备选：若 i18n 插件与 Material 版本冲突，退化为纯英文站点优先跑起来，双语后续再加。

## 5. 安装选择器（Home/User Guide 亮点）
- 参考 PyTorch：单页 + 下拉选择器（UCM 版本 / 引擎 / 设备 / OS / 架构 / 安装方式）+ 静态安装矩阵数据 + 前端 JS 动态渲染对应命令。
- 复用 `model-configs.js` 的"静态数据 + 前端脚本"模式：新建 `assets/js/install-matrix.js` 承载安装矩阵，渲染 Docker/Helm/pip/源码命令。
- 做可工作的基础交互版（维度 + 预置几条命令样例），完整矩阵数据后续补齐。

## 6. Home 首页重写
- Material 风格：项目定位 + 核心能力卡片 + Quickstart 入口 + 支持概览 + 论文列表。
- GitHub Star/Watch/Fork → Material `repo` 按钮 + 首页徽章。
- 去掉 `{figure}`/`{raw}`，用标准 Markdown + 少量 HTML。

## 7. tools/site.py
- 薄封装，对应评审 5.3：`serve`（默认英文）/ `build [--lang en|zh --strict]` / `validate`（mkdocs build --strict）/ `translate --changed`（占位） / `generate`（占位，标注 docker-recipes.generated.md 当前缺失待重建）。
- 本地预览直接 `mkdocs serve` 亦可。

## 8. 本地启动
```bash
cd docs-next
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdocs serve   # 或 python tools/site.py serve
# 访问 http://127.0.0.1:8000，顶栏可切换 EN / 中文
```

## 9. 不做 / 留待后续
- 不改 `docs/` 与 `.readthedocs.yaml`，不切正式入口（评审要求三项验收后才切）。
- 不实现 AI Robot 中文自动生成（需 CI secret + GitHub App，属后续）。
- 不从源码生成完整 API 文档（评审 3.2 明确当前不依赖）。
- `docker-recipes.generated.md` 当前仓库缺失，generate 步骤做占位。
- Model Tour 各模型系列（GLM/Qwen3/DeepSeek 等）部署教程、Benchmark、API 参数完整内容属后续补齐，本轮只建占位页。
- 不动 UCM 运行时、Helm Chart、推理引擎代码。

## 10. 交付物
- 可本地 `mkdocs serve` 启动的 Material 站点。
- 新六模块导航 + 现有 30 篇英文内容全部迁移到位。
- 双语切换可用（英文完整 + 中文首页样例，fallback 英文）。
- 可交互的安装选择器基础版。
- site.py 统一入口（薄封装）。
