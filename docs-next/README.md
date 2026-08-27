# docs-next

UCM 文档站的重建工程,基于 **MkDocs Material**。与现网 Sphinx 站(`../docs/`)隔离建设,三项评审模块(切到 MkDocs、AI 中文生成、内容调整)全部通过前不切换正式入口。

> 状态:本地预览可用;严格构建通过(中文镜像页缺失时 fallback 英文,有 WARNING 但不阻塞)。

## 快速开始

```bash
cd docs-next
pip install -r requirements.txt  # 首次
mkdocs serve                    # 或: python tools/site.py serve
```

打开 http://127.0.0.1:8000 —— **英文在 `/`(默认语言无前缀),中文在 `/zh/`**。`Ctrl+C` 停。

> 改普通 `.md` 是热更新(live reload);改了 `overrides/main.html` 这类主题模板后必须**重启** `mkdocs serve`(livereload 不重载模板)。

## 更新文档

日常更新就两步:**加 md + 登记到 nav**。

### 1. 加 md 文件

放到 `docs/en/<路径>.md`(英文)。中文镜像放 `docs/zh/` 同路径(不建则 fallback 显示英文)。文件名建议 kebab-case。

### 2. 登记到导航

在 `mkdocs.yml` 的 `nav:` 加条目,否则页面能直接访问但不进左侧导航树(mkdocs 会提示 "not included in nav"):

```yaml
nav:
  - User Guide:
      - My Page: user-guide/my-page.md   # 路径相对 docs/en/
```

### 3. 静态资源(图片等)

- 图片放 `docs/assets/images/`,按当前 Markdown 文件位置使用相对路径引用，例如首页使用 `../assets/images/xxx.png`
- 计算器 JS/HTML 在 `docs/assets/` 根,iframe 同样使用相对路径，避免版本目录和 project Pages 指向站点根

### 写作注意点(踩过的坑)

- **表格前必须有空行**:GFM 表格前一行若是段落,表格不渲染(会变成段落文字)
- **HTML 容器带 `markdown` 属性**:`<div align="center" markdown>` 才渲染 div 里的 `![]()` 图片/徽章,没 `markdown` 属性则忽略
- **折叠块**用 admonition `??? note "标题"` 而非 `<details>`
- **图片宽度**用 attr_list `![](){ width=60% }` 而非 `<img width>`

## 构建与校验

| 命令 | 作用 |
| --- | --- |
| `mkdocs build` | 普通构建 |
| `mkdocs build --strict` | 严格构建(WARNING 升错误) |
| `python tools/site.py validate` | 全语言严格构建 |
| `python tools/site.py build --lang en --strict` | 单语言严格构建 |

严格构建通过(退出 0)。中文首页指向尚未创建的中文镜像页会产生若干 WARNING(i18n fallback 固有,不阻塞);AI 生成完整中文后 WARNING 消失。本地检查用 `mkdocs build` 即可。

`tools/site.py` 子命令:`serve` / `build --lang {en,zh} [--strict] [--clean]` / `validate` / `translate --changed`(CI,本地未接通) / `generate`(占位)。

## 多版本预览

版本选择器可用一次性本地分支预览，不得直接改 `gh-pages`:

```bash
mike deploy preview latest -u --branch docs-preview --ignore-remote-status
mike serve --branch docs-preview                    # 访问 /latest/
```

- `mkdocs serve`:dev 模式,有 live reload,**无版本选择器**
- `mike serve`:多版本静态预览,有版本选择器,**无 live reload**(改内容后需重新 `mike deploy`)
- `gh-pages` 的唯一写入口是 `tools/pages.py`;不要手工执行 `mike ... --push` 或直接提交该分支
- 一次性清理使用 `python tools/pages.py initialize --repository OWNER/REPO`
- latest 发布使用 `python tools/pages.py publish-latest --repository OWNER/REPO`
- Stable 发布使用 `python tools/pages.py publish-stable --repository OWNER/REPO --catalog PATH`
- 正式命令由 Pages CI 调用;脚本内部运行不带 `--push` 的 Mike，最后只普通 push 一次

## 项目结构

```
docs-next/
├── mkdocs.yml          站点配置:nav、Material 主题、i18n、markdown 扩展
├── docs/               docs_dir(站点内容根)
│   ├── en/             英文(默认语言,URL 无前缀)
│   ├── zh/             中文镜像(缺失页面回退英文)
│   └── assets/         共享静态资源(images、install.js/css、calculator、model-configs)
├── overrides/
│   └── main.html       主题覆盖:KaTeX CDN + header 白色 + 字体分层(Jost 侧栏 / Inter 正文)
├── tools/
│   ├── site.py         统一入口(serve/build/validate/translate/generate)
│   └── pages.py        gh-pages 唯一写入口(Mike、Catalog、Simple Index、单次 push)
├── tests/
│   └── test_pages.py   Pages/Catalog/Index/双语安装页 focused tests
├── requirements.txt    Python 依赖(MkDocs / Mike / packaging / pytest)
├── .venv/              本地虚拟环境(不提交)
└── site/               构建产物(不提交)
```

## 国际化(i18n)

- 插件 `mkdocs-static-i18n`,**folder 模式**(`docs/en/`、`docs/zh/`)
- 英文默认语言,**URL 无前缀**(在 `/`,不是 `/en/`);中文在 `/zh/`
- `fallback_to_default: true`:中文页缺失时回退英文内容
- 共享静态资源按 Markdown 源文件位置使用相对路径，由 MkDocs/Mike 保持在各版本目录内

## 内容规范

- **命令优先,非散文**:Model Tour / Engines / Deploy 尤其;先给可执行命令,再补说明
- **参数单一事实源**:任务页只内联相关子集并链 `reference/api-parameters.md`;全量参数表只在 Reference 维护
- **无 emoji**:表格状态用 `Yes` / `No` / `Untested`,评级用 `n/5`;架构图保留 ASCII 框线
- **占位页**:待补页面在 md 里写 "What to add" 规范(读者意图/必含/不要做/验收/负责人),由模块负责人填内容;已有内容页不改

## 数学公式

KaTeX 经 `overrides/main.html` 注入(CDN),不随 i18n 静态资产走。正文用 `$$...$$`(块)或 `$...$`(行内)。

## 相关文件

- 重构技术评审:`../docs/ucm-mkdocs-site-rearchitecture-technical-review.md`(§4.7.3 定义六模块顶层导航与负责人矩阵)
- 现网 Sphinx 站:`../docs/`——**切换前不动**
