# UCM documentation

文档使用 MkDocs Material，目标托管平台为 Read the Docs（RTD）。英文是内容来源，中文位于同路径的 `docs/zh/`。切换采用 Fork 预览 → 官方 `ucm` 的顺序；旧 Sphinx 历史版本和 GitHub Pages 历史下载入口继续保留。

## 本地开发与验证

使用 Python 3.12：

```bash
cd docs-next
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt -r requirements-translation.txt
python tools/site.py serve
```

开发服务的英文在 `/`，中文在 `/zh/`。默认不联网获取 Release，也不调用翻译模型。

```bash
# 与 RTD 相同的独立语言构建；输出 site/en 和 site/zh
python tools/site.py validate
python tools/site.py build --lang en --strict
python tools/site.py build --lang zh --strict

# 从指定仓库选择真实安装清单，或者传入本地已验证的 Schema 8 文件
python tools/site.py build --lang en --strict --repository OWNER/REPO
python tools/site.py build --lang zh --strict --manifest /path/to/release-manifest.json

python -m pytest -q tests
node --test tests/install-ui.test.cjs
python tools/site.py translate check
```

`requirements.txt` 固定站点依赖；`requirements-dev.txt` 添加测试工具；`requirements-translation.txt` 添加 Co-op 的 Markdown 分块与重组 API。Co-op 的 Azure 传递依赖包含预发布版本，若使用 uv 安装这一组依赖，需要传入 `--prerelease=allow`。

`build --lang` 真正选择一种语言。中文构建在临时目录中补入缺译英文页，并显示“英文原文”提示及同版本英文链接；它不会复制文件到仓库中文目录，也不会将回退内容登记为译文。严格构建负责页面与资源，中文覆盖由独立翻译检查负责。

## 内容维护

- 英文放在 `docs/en/`，中文使用 `docs/zh/` 下相同路径。新增公开页面登记到 `mkdocs.yml`；图片及脚本使用相对链接。
- 安装包、镜像及 Chart 坐标只来自当前构建的发布清单；Quickstart 负责配置、运行与验证，链接 Installation，不另写裸 `pip install uc-manager` 或镜像 `latest`。
- 参数参考以实际配置读取位置为依据。任务页引用参考，避免重复维护默认值。
- 新页面必须有对读者有效的内容。内部待办留在任务或 Issue 中；无配方的模型页只提供官方教程及通用集成入口。
- 能力指南按当前源码重新撰写，说明适用场景、配置入口、验证方法和实现边界，不搬运旧站正文或历史部署脚本。旧链接通过跳转保留。
- 性能页提供可执行工具与实验设计；测量结果必须有对应环境、版本和原始证据。服务健康、外部缓存读写和性能收益分别验证。
- `translation-required.txt` 是关键中文路径的唯一清单，共 28 页。关键页缺译或过期阻塞合入；其他页可明确回退英文，继续报告翻译状态。
- 删除或迁移公开页面时更新 `redirects.json`。映射相对当前语言/版本根，生成跳转页，保留 query 和 fragment；不能覆盖实际正文、跳到不同版本或复制导航规则到 RTD 后台。

## RTD 构建与安装清单

根 `.readthedocs.yaml` 使用 Python 3.12，调用 `python docs-next/tools/site.py rtd`。构建读取 RTD 的语言、Git identifier、commit hash、canonical URL 和输出目录，生成一个语言/版本根。英文使用 `/en/<version>/`，中文 RTD 语言为 `zh-cn`，对应源码目录 `zh`。

RTD Addons 提供版本、语言和线上搜索入口，本地保留 MkDocs 搜索。源码链接绑定实际仓库和 Git ref；PR 使用 commit hash，不使用 PR 编号作为分支名。

安装器仍读取本版本根目录的 `release-manifest.json`：

| 构建类型 | 清单来源 |
| --- | --- |
| Tag / Stable | 同仓库、对应 Git 标签的完整 Schema 8 Release；支持正式版和 RC |
| Latest / PR | 同仓库最高版本、已完成且具有有效 Schema 8 清单的 Stable Release |
| 没有合格 Release | 页面明确显示安装数据不可用，提供源码构建入口 |

旧 Schema 6/7 不能驱动安装选择器，不做推测转换。已有清单损坏、标签/仓库不匹配、文件集合或下载 URL 与 Release 不一致时构建失败。Latest 页面显示实际安装制品的版本，避免把开发文档版本当作发布版本。

RTD 可能在 Tag 推送时先于产物完成启动构建；此时返回 RTD 的取消码 `183`，不发布不完整页面。Release 流水线完成清单上传、回读及保留策略后，再触发中英文项目的对应 Tag 和 Latest；仅在 RTD 当前 active Stable 对应该 Tag 时重建 Stable，重建旧标签不会回退别名。

## 自定义 AI 翻译

翻译通过一个 GitHub workflow 直接调用所选 HTTP API。供应商和模型不设默认值，失败不切换服务。

| 配置 | 保存位置 | 作用 |
| --- | --- | --- |
| `DOCS_TRANSLATION_API_FORMAT` | Repository Variable | 选择下表协议；留空时生成 job 跳过 |
| `DOCS_TRANSLATION_BASE_URL` | Repository Variable | 包含版本前缀的 API root |
| `DOCS_TRANSLATION_MODEL` | Repository Variable | 模型或部署名 |
| `DOCS_TRANSLATION_API_KEY` | `docs-translation` Environment Secret | 仅模型调用步骤使用的密钥 |
| `DOCS_TRANSLATION_APP_ID` | Repository Variable | 同仓 PR 写回的 GitHub App ID |
| `DOCS_TRANSLATION_APP_PRIVATE_KEY` | Repository Secret | 仅交付 job 使用的 App 私钥 |

| API 格式 | API root 示例 | 追加路径 |
| --- | --- | --- |
| `openai-chat-completions` | `https://api.openai.com/v1` | `/chat/completions` |
| `openai-responses` | `https://api.openai.com/v1` | `/responses` |
| `anthropic-messages` | `https://api.anthropic.com/v1` | `/messages` |
| `gemini-generate-content` | `https://generativelanguage.googleapis.com/v1beta` | `/models/<model>:generateContent` |

自建服务只需实现选定协议；API root 可以使用自定义主机及前缀。请求采用同步文本响应、120 秒超时和 16384 输出 token 上限。空结果、截断、HTTP 错误或无法解析的响应均导致本次任务失败，不重试或提交部分结果。

执行链路为：可信代码选页并准备分块 → 配置的 API 翻译 → 确定性重组和校验 → 隔离交付。术语表和占位符规则进入系统指令，源文作为数据传入。模型不能操作仓库。没有分块时不会读取模型配置或发送请求；纯删除和状态整理仍能完成。

```bash
# 在真实 PR 上使用 base/head 和仓库 ID，不要用示例 ID 上线
python tools/site.py translate prepare --changed \
  --base-ref "$BASE_SHA" --head-sha "$HEAD_SHA" \
  --base-repository-id "$BASE_REPOSITORY_ID" \
  --head-repository-id "$HEAD_REPOSITORY_ID" --pr-number "$PR_NUMBER" \
  --api-format "$DOCS_TRANSLATION_API_FORMAT" \
  --model "$DOCS_TRANSLATION_MODEL" --base-url "$DOCS_TRANSLATION_BASE_URL" \
  --output-dir /tmp/ucm-translation/task
python tools/site.py translate generate \
  --task-dir /tmp/ucm-translation/task --output-dir /tmp/ucm-translation/chunks
python tools/site.py translate finalize \
  --task-dir /tmp/ucm-translation/task --agent-output-dir /tmp/ucm-translation/chunks \
  --output-dir /tmp/ucm-translation/validated
python tools/site.py translate apply --artifact-dir /tmp/ucm-translation/validated
```

`prepare`、`generate`、`finalize` 可用同一个 `--instructions` 文件；默认指令位于 `tools/translation/instructions.md`。工作流使用仓库默认指令。

- 作者在同一 PR 更新对应中文时尊重人工版本；机器人所有权与英文新鲜度分别判断，避免历史 stale 漏检。
- 同仓 PR 由最小权限 App 写回，触发新 HEAD 的检查；Fork PR 获得绑定当前 HEAD 的 patch/产物。按机器人评论从基仓库已验证的 `BASE_SHA` 提取可信 apply 工具，再校验 HEAD 和目标文件原始哈希后应用；不执行 PR 中的脚本。
- required gate 无模型密钥和 App 凭据；RTD 也不持有翻译密钥，只构建已经审阅的内容。
- 手动补译触发 `docs-translation-generate.yml`，每批最多 5 页；常规 PR 单次最多 10 页，超过上限须拆分或先手工同步，不会默默处理一半。
- GitHub App 仅安装到目标仓库，权限为 Contents 与 Pull requests 的读写。初次将可信工具合入基线、完成同仓/Fork canary 后，才把 `Docs translation synchronized` 设为必需检查。

## Fork 验收及官方切换

1. 在 RTD 创建英文父项目及中文 Translation 项目，均绑定 Fork；预览阶段默认分支设为 `feature/docs-rtd`，使用本分支的根 RTD 配置，启用 PR Preview。
2. 语言分别设置为 English 和 Simplified Chinese (`zh-cn`)，版本模式使用带翻译的多版本模式。启用 Addons 的版本/语言、搜索及 Preview 提示。
3. 先验证中英文 Latest、真实 PR Preview、实际安装选择器、旧 URL、favicon 和计算器。记录 RTD build ID、源码 SHA 与公开 URL；取消构建不等于已部署。
4. 配置翻译 API 和 GitHub App，验证一次真实调用、一次零请求重跑、同仓写回及 Fork patch。未配置凭据时只能报告契约测试通过。
5. Fork 验收后，通过官方开发分支集成切换 `ucm` 项目；英文父项目仍使用现有 `ucm`，关联中文项目。新内容只维护 `docs-next`。
6. GitHub Repository Variables 设置 `RTD_PROJECT_EN`、`RTD_PROJECT_ZH`，Repository Secret 设置 `RTD_API_TOKEN`。项目仓库必须与当前 Release 仓库一致。未配置项目时 Release 跳过 RTD 通知；配置不完整会明确失败。
7. 官方先切换 Latest。首个包含新配置、完整 Schema 8 Release 且 RTD Tag 构建通过后启用 Stable。旧 Git 标签仍按原配置构建，不改写历史标签。
8. 验收通过后停止新 Pages 发布，保留原 `gh-pages` 内容及自定义域名，尤其历史下载索引；本轮不修改 DNS。若正式切换失败，恢复上一版 RTD 配置即可继续旧站构建。

RTD 管理、API Token、模型密钥及 App 私钥通过对应后台配置，不能写入源码、翻译状态或日志。
