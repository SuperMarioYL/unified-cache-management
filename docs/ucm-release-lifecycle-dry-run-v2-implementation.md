# UCM Release Lifecycle Dry-run v2 实施说明

## 1. 结论与边界

Dry-run v2 已在隔离 worktree 中形成一套只读发布控制面，用于生成和校验
PR、`develop`、Nightly、Draft、RC、Stable、Hotfix 七个阶段的计划、产物清单、
环境测试请求与结果、发布协调预览、清理预览和仓库策略审计报告。

这套实现不会发布任何内容。预览中的 `pip install`、`docker pull` 和
`helm upgrade --install` 命令只是计划坐标，当前不可作为可安装、可拉取或可部署
的交付路径。所有操作均为 `executed: false`，没有 commit、stage、push、PR、Tag、
Release、Registry、PyPI、GitHub Packages、Docker Hub 或仓库设置变更。

现有生产发布路径和已发布的 `v0.5.0rc1` 保持冻结；本次没有修改现有生产 workflow、
`.github/release/ucm_release` 或现有 release package。

## 2. 为什么新增本文档

原始流程规范 `docs/ucm-development-test-release-process.md` 只存在于主工作树，
是用户未跟踪的 712 行文件，不属于本隔离基线。为保护用户文件，本次仅将它作为只读
规范输入，没有复制、修改或覆盖；实施结果写入当前新文件，并由
`.github/release/v2/README.md` 链接。

## 3. 三种 Wheel、镜像和 Chart 的关系

三个 Wheel 都提供 `ucm` import，但后端互斥：

| 后端 | 只能选择一个的 Wheel | 对应镜像族 |
| --- | --- | --- |
| CUDA | `uc-manager-cuda` | `ucm-cuda` |
| Ascend A2 | `uc-manager-cann-a2` | `ucm-cann-a2` |
| Ascend A3 | `uc-manager-cann-a3` | `ucm-cann-a3` |

同一 Python 环境必须三选一。v2 检查器把旧 `uc-manager` 单独存在也判为不兼容，并会
拒绝旧包与任一 v2 Wheel 混装、任意两种 v2 Wheel 混装、三种 v2 Wheel 同时存在、
同一 distribution 重复、元数据异常，以及任何其他合法 distribution 对 `ucm`
top-level import 的声明。只有恰好一个批准的 v2 distribution 才是 `compatible`；空环境
单独报告为 `absent`。现有旧包本身没有被修改，仍可脱离 v2 guard 独立使用，但不能被
v2 环境检查称为兼容。每个生命周期计划同时声明三个候选 Wheel、三个镜像族和一个
`unified-cache-pd` Chart；实际用户环境只选择与后端相符的一组 Wheel 与镜像，
Chart 负责部署该组运行对象。

产物身份分层如下：

- Wheel 和本地 Chart 文件：相对安全 POSIX 路径、文件大小、裸 64 位小写 SHA256；
- 镜像：计划坐标、`sha256:<64hex>` OCI index digest、`linux/amd64` 与
  `linux/arm64` member digest；
- `artifact-manifest.json`：绑定 lifecycle plan SHA、完整 source SHA、阶段、版本和
  七个产物；
- JSON envelope：从不包含自身 `sha256` 字段的 canonical JSON 重算摘要，属于
  content-addressed/self-digested 合同，不是数字签名。

## 4. 七阶段命令与输入输出

以下命令从仓库根目录执行：

```bash
export UCM_V2_ROOT="$PWD/.github/release/v2"
export PYTHONPATH="$UCM_V2_ROOT"
export UCM_V2_CONFIG="$UCM_V2_ROOT/release.yaml"
export UCM_SOURCE_SHA=0123456789abcdef0123456789abcdef01234567
```

| 阶段 | 关键输入 | `lifecycle plan` 关键参数 | 主要输出 | 保留期 |
| --- | --- | --- | --- | --- |
| PR | PR 编号、`refs/pull/42/head`、精确 head SHA | `--stage pr --trigger pull_request --repository-role validation --pr-number 42` | PR 只读构建预览 | 7 天 |
| `develop` | default-branch `workflow_run` controller、成功的同仓库 `Push Commit Checks`、`refs/heads/develop` 精确 SHA | `--stage develop --trigger push --repository-role validation --run-number 17` | 合入检查预览 | 14 天 |
| Nightly | main 上的可信 control revision、两次只读 GET 一致的 `refs/heads/develop` commit SHA、真实 UTC 日历日期 | `--stage nightly --trigger schedule --date 2026-08-12` | Nightly 全矩阵预览 | 14 天 |
| Draft | main 控制 ref、Draft intent、精确 SHA | `--stage draft --trigger workflow_dispatch --repository-role production --intent intent.json` | Draft 产物与模拟环境请求 | 30 天 |
| RC | main 控制 ref、RC intent、精确 SHA | `--stage rc --trigger workflow_dispatch --repository-role production --intent intent.json` | RC reconcile/Release Markdown 预览 | 保护 |
| Stable | main 控制 ref、Stable intent、精确 SHA | `--stage stable --trigger workflow_dispatch --repository-role production --intent intent.json` | Stable reconcile/Release Markdown 预览 | 保护 |
| Hotfix | main 控制 ref、补丁 intent、精确 SHA | `--stage hotfix --trigger workflow_dispatch --repository-role production --intent intent.json` | Hotfix reconcile/Release Markdown 预览 | 保护 |

示例：

```bash
python3 -m ucm_release_v2 lifecycle plan \
  --stage nightly --trigger schedule --ref refs/heads/develop \
  --source-sha "$UCM_SOURCE_SHA" --repository-role validation \
  --date 2026-08-12 --config "$UCM_V2_CONFIG" \
  --output /tmp/lifecycle-plan.json

python3 -m ucm_release_v2 lifecycle validate \
  --plan /tmp/lifecycle-plan.json --config "$UCM_V2_CONFIG"

python3 -m ucm_release_v2 wheel plan \
  --lifecycle-plan /tmp/lifecycle-plan.json --config "$UCM_V2_CONFIG"

python3 -m ucm_release_v2 artifacts collect \
  --lifecycle-plan /tmp/lifecycle-plan.json \
  --records-json /tmp/artifact-records.json --base-dir /tmp/artifacts \
  --config "$UCM_V2_CONFIG" --output /tmp/artifact-manifest.json

python3 -m ucm_release_v2 artifacts validate \
  --lifecycle-plan /tmp/lifecycle-plan.json \
  --manifest /tmp/artifact-manifest.json --base-dir /tmp/artifacts \
  --config "$UCM_V2_CONFIG"
```

Draft 环境链路继续生成 `environment-test-request.json`、模拟
`environment-test-result.json`，再执行 `environment verify`。即使模拟检查全部通过，
输出仍固定为 `production_gate: blocked`。RC、Stable、Hotfix 使用离线 inventory 执行
`reconcile plan`；相同输入字节一致，已应用 inventory 全部 `skip-identical`，逻辑目标
或反向坐标冲突则阻断。`release render` 最后生成 Markdown，而不是 JSON。

环境 request/result 自摘要只证明内部一致性。reconcile 只有同时收到
`--environment-lifecycle-plan`、`--environment-manifest`、`--environment-request`、
`--environment-result` 四件套，并从原 Draft plan/manifest 精确重建 request 后，才会
给出 `draft-passed` 或 `draft-failed`；缺少 origin anchors 的内部一致 pair 只会得到
`unanchored-simulation` 与 `draft-environment-unanchored` blocker。

Stable/Hotfix promotion 同样要求 evidence、source lifecycle plan 和 source manifest
三件套。evidence 中的 source plan/manifest digest、stage、version、source SHA 必须与
重开的输入一致。Stable 还要求接受的 RC 与目标 plan 使用同一个 source SHA 和 release
line；Hotfix 必须锚定紧邻的上一 Stable，允许目标 Hotfix 使用新 SHA。未提供 source
anchors 的声明保留为 `promotion-unanchored`，不能成为 eligible。所有情况仍保持
`production_ready: false` 和外部环境证据 blocker。

## 5. 清理、安全与合同

临时对象按创建时间采用严格边界：PR 7 天、`develop`/Nightly 14 天、Draft 30 天；
恰好到达边界仍保留，只有严格早于边界才进入 `delete-preview`。RC、Stable、Hotfix、
protected state、active Release/Draft 引用以及被未过期对象共享的 digest 始终保留。
清理只产生预览，不执行删除；输入对象、引用和失败数组重排不会改变 inventory SHA 或
plan SHA。cleanup inventory 中本地 artifact 和 Chart 使用裸 64 位文件 SHA256，image
使用 `sha256:<64hex>` OCI digest；三种 kind 即使 coordinate 相同也属于不同命名空间。
最早 UTC 年份导致 retention 减法下溢时统一返回 CLI exit 2，无 traceback。

独立的 `ucm_release_v2.security` 审计模块对当前 shipped v2 Python（包括
`packaging/backend_guard.py`）使用按文件关闭的 import、API、callable 与 definition
能力白名单，并对八个 workflow 使用按 step 关闭的 Action、shell executable、argv 与
heredoc Python 语法白名单。任何白名单外的新能力或命令先失败、再等待人工审查；这项
静态合同审计不声称能够证明任意 Python 程序的安全性。对当前已交付 surface，审计确认：
权限仅 `contents: read`，Actions 只能来自 checkout/setup-python/upload-artifact 的精确 pin，
checkout 均 `persist-credentials: false`，不存在 `pull_request_target`、head-controlled
checkout、Registry login/push、twine/Helm/Release 发布、Tag/评论/审批/dispatch/设置
变更、写 HTTP 方法、动态 import/eval/exec/compile/getattr 网络绕过或 Python
发布/执行/删除实现。shell 审计在 argv 解析前拒绝 command substitution、反引号、
process substitution、here-string、未批准重定向、pipeline、分号与后台操作；仅允许
经过逐行白名单审查的固定本地 JSON stdout 输出和数组构造。PR workflow 仅保留两次显式 GET：
`https://api.github.com/repos/${REPOSITORY}/pulls/${PR_NUMBER}`，用于比较观察时与当前
head SHA；`/release build` 命令必须携带评论时请求的精确 40 位小写 SHA，并要求两次
readback 都等于该 SHA。`develop` controller 改由 default branch 上的 `workflow_run`
加载，除 workflow 名称、同仓库 `develop` 和 `success` 外，还精确要求 event 为 `push`、
run path 为 `.github/workflows/push-check.yml@develop`；event head SHA 仅作为数据。它只
checkout 两次 main GET 与 `github.workflow_sha` 共同验证的 control SHA。Nightly 从
`github.workflow_sha` checkout main 上的可信控制代码，不 checkout
或执行 develop 代码；它仅对精确的
`https://api.github.com/repos/${REPOSITORY}/git/ref/heads/${DEVELOP_BRANCH}` 做两次显式
GET，严格校验 `refs/heads/develop`、commit 类型和 40 位 SHA，并要求两次 SHA 一致后
才把该 SHA 作为 lifecycle source data。

四个 `workflow_dispatch` 文件只保留单个 data-only reusable job，没有 `runs-on`、
`steps` 或 Action；它们精确调用
`SuperMarioYL/unified-cache-management/.github/workflows/release-control-dry-run.yml@main`，
选中的 branch/ref 只能提供声明过的输入数据。所有可执行 manual control 逻辑集中在该
reusable controller。它在任何 checkout/CLI 前对
`https://api.github.com/repos/${REPOSITORY}/git/ref/heads/${CONFIGURED_MAIN}` 做两次
`--max-redirs 0` 的只读 GET，严格拒绝重复 JSON key、非 main ref、非 commit、非法或
变化的 SHA；从完整 `toJSON(job)` 中要求 `job.workflow_repository`、`job.workflow_file_path`、
`job.workflow_ref` 精确标识 validation repo 中 `refs/heads/main` 的 controller，
`job.workflow_sha` 四项 identity projection 必须存在且为字符串，其中 SHA 必须等于
两次 main SHA；GitHub 生成的 status/container/services 等其他 job 字段不参与授权。
caller repository 也显式仅允许
`SuperMarioYL/unified-cache-management`；生产坐标只是配置数据，不是执行授权，任意 fork 不会
通过。除 PR、Nightly、develop/reusable control 这些精确 GET 外不允许网络命令。
Actions Artifact 上传和 Job Summary 是允许的运行输出。

Hosted 结果只有在保留并核对 called-workflow 的 `job.workflow_ref`、repository、path 与
SHA 后，才可作为 controller identity 证据。GitHub 在 branch 与 tag 同名时会让 reusable
`@main` 优先解析为 tag；当前诚实 controller 会因 `job.workflow_ref` 不是
`@refs/heads/main` 而拒绝，但恶意 tag-shadow controller 可以在执行前删掉自己的检查。
因此 hosted 启用必须通过 ruleset 禁止名为 `main` 的 tag，并保护 wrapper/controller
变更；或者在 controller 合入 `main` 后，再以单独提交把四个 wrapper 固定到 controller
的不可变 commit SHA。恶意 selected branch 也仍可删除或替换 wrapper call。这里的代码
不能单独关闭上述外部 bootstrap/ruleset 边界，本地静态测试只证明当前交付 tree。
仓库 policy 的 JSON Artifact 保留原始 evidence 供离线审阅；Job Summary 的 Gaps 明确
省略 free-form evidence，header 只包含已验证的 identity、status 和 digest，并且 gaps
只渲染受控的 check `id`/`status`，从而不把 snapshot-derived Markdown、HTML、反引号或
fenced-code 内容拼接进 summary。

所有 16 份 Draft 2020-12 Schema 都是本地文件，object 合同关闭
`additionalProperties`，并对真实 CLI 输出执行 standalone `jsonschema` 验证。
`environment-test-result.schema.json` 自包含 artifact `$defs`，不依赖 sibling/网络 ref；
`lifecycle-plan.schema.json` 对七阶段 trigger/ref/role/channel/retention/intent 做条件约束，
并固定两个 gate、两个 `executed: false` operation 及 3 Wheel + 3 image + 1 Chart 产品闭包；
reconcile identity 按 `target.kind` 约束，镜像使用 OCI digest，Wheel/Chart 使用裸 SHA256；
promotion evidence 严格包含 source plan/manifest digest，reconcile 输出保留四个 nullable
origin digest，并用 `unanchored-simulation` 显式区分未锚定的环境 pair。
这里的 lifecycle JSON Schema 保证严格结构与阶段局部约束，不声称标准 Draft 2020-12
能够表达任意 sibling 字段相等关系。所有 workflow 必须紧接 `lifecycle plan` 执行
`lifecycle validate`；Wheel、artifact、environment、reconcile 与 render 等下游 CLI
consumer 在读取计划时也调用同一 `validate_plan` 运行时验证器。该验证器重算
self-digest，并把配置仓库/产品闭包、source SHA 与版本后缀、release intent 的
source/version 绑定纳入显式 semantic gates。

动态 Python loader 只有一个精确例外：`wheels.py` 的 `_V2_ROOT` 与
`_PACKAGING_ROOT` 顶层绑定必须由规范化 AST 唯一证明来自当前模块位置，全树只允许这两个
批准 target 的两次 Store，且不允许 Delete；完整 `_guard_module` 函数的规范化 AST 也必须
与批准模板逐节点一致。identity、相对路径表达式、spec/module 目标、loader 调用和 return
都固定；替换路径、动态命名空间修改、增加第二 loader、重赋、重排或别名调用都会失败。
Workflow 审计还关闭 root/job/step 的执行上下文字段和值；runner 固定为
`ubuntu-24.04`，不允许自定义 shell、workflow/job defaults、container、services 或
strategy/matrix。唯一允许的 reusable job 是四个 wrapper 到精确 `@main` controller 的
data-only mapping；target、input expression、runner、step 或 Action 的任何漂移都会失败。
八个 workflow 中每个 executable job 都绑定完整有序 step name/type 序列，expected 与
observed executable-job mapping 必须完全相等且不能有 orphan。两个 trust-critical
embedded validator 还绑定精确 workflow、owner job、零基 step index、名称及解析后的完整
run-body digest；跨 job 搬移/重复、把 checkout 或业务 CLI 提到 validator 前、正文改动、
删除整步或改名，都会在常规 capability audit 之外额外 fail closed。

## 6. 本地验证结果

2026-08-12 在隔离 worktree 完成以下本地验证：

| 验证项 | 结果 |
| --- | --- |
| Task 8 场景与独立安全测试 | collection `135 tests` |
| Residual trust focused tests | `223 passed` |
| 完整 v2 测试 | 单进程 `524 passed` |
| 未变更 legacy release suite | `544 passed, 1 skipped` |
| 16 份 Schema `check_schema` 与真实实例 | 全部通过，使用当前 `/opt/anaconda3/bin/jsonschema` |
| 8 个 dry-run workflow actionlint/YAML | 全部通过 |
| Ruff、Black、compileall | 全部通过 |
| relevant pre-commit：codespell、Black、isort、actionlint | 全部通过 |
| `git diff --check` 与 untracked per-file 检查 | 全部通过 |

v2 完整测试先由 `pytest --collect-only` 确认 524 项，再在同一 pytest 进程全部运行通过。
Legacy suite 也使用同一 pytest 进程运行，保持原命令语义并得到 544/1 的完整汇总。

这些结果只证明本地代码、离线 fixture、Schema 和静态 workflow 合同，不代表 hosted
Actions 或外部发布结果。

## 7. 证据分层与明确阻断项

| 证据层 | 当前状态 | 能证明什么 |
| --- | --- | --- |
| 本地代码与 CLI | 已执行 | 输入校验、确定性、fail-closed、无执行路径 |
| 本地文件字节 | 已执行 | fixture 文件大小与 SHA256 可重算 |
| 本地 JSON Schema | 已执行 | 当前 `jsonschema` CLI 接受真实生成实例 |
| workflow 静态合同 | 已执行 | YAML 中的权限、pin、checkout、命令边界 |
| hosted GitHub Actions | 未执行，明确 blocker | 尚无 hosted runner 日志或 Actions Artifact 回读 |
| Registry/GitHub Release/PyPI/GitHub Packages/Docker Hub | 未执行，明确 blocker | 尚无发布、匿名拉取、digest 或 Release readback |
| runtime/硬件 | 未执行，明确 blocker | 尚无 import、服务、推理、CUDA/CANN 设备结果 |
| Kubernetes/集群验收 | 未执行，明确 blocker | 尚无 Helm 安装、就绪、升级、回退或集群测试 |
| 仓库 policy | 仅离线 snapshot | 合规与 fork-like gaps 是 fixture 结果，不是当前远端设置回读 |

因此，本文中的安装、拉取、Chart 命令和产品坐标都必须理解为 planned/unavailable。
只有 hosted workflow、正式渠道发布、独立 readback、目标 runtime/硬件以及集群验收分别
完成后，才能升级对应证据层的结论。
