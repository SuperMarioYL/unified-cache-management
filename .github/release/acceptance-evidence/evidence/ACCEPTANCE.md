# UCM 发布工程 Fork 验收记录

仓库：`SuperMarioYL/unified-cache-management`。只修改和发布 Fork；官方仓库未修改。

最终生产提交 P2 为 `f66366538b35faa51507452ae1ad676b10994a4d`，部署在 `develop`。P1 为 `776110b2524bf43eb5e197dcdb96315f9c6c3d8c`。四模块清理边界保持；普通 Schema 9 使用 Manifest 精确历史坐标，新发布仍绑定 Tag。此次实际 hosted preview 发现 Docker Hub 404 probe 没有归一化为缺失，因此补入 P2，并保留 403 等真实错误。

## 本地与 Fork 检查

最终 release suite：610 passed、4 skipped；docs Python 契约：57 passed；Node 契约：16 passed。双语严格文档构建、actionlint、ShellCheck、Black、isort、Ruff 已通过。原始日志在 `local/`；最终 P2 默认分支与修复分支 Push/Docs 四个 CI 均成功，记录在 `runs/production-p2-ci.json`。无关 `.DS_Store` 与原空 `out/` 样例保留为未跟踪本地文件。

## 正常真实发布

| 类型 | Tag | source SHA | Run | 状态 |
| --- | --- | --- | --- | --- |
| Nightly | nightly/v0.8.0-20261013-1 | 776110b2524bf43eb5e197dcdb96315f9c6c3d8c | 38375348617 attempt 1 | complete |
| Prerelease | v0.8.2rc1 | 4858aef86456f712da6fd59ecb224235841ebdac | 38377675294 attempt 2 | complete |
| Stable | v0.8.2 | 3d6a11b01167c40b28ee6a08c7d7454d23940674 | 38383412316 attempt 2 | complete |
| Draft | draft/v0.8.2-1 | 3d6a11b01167c40b28ee6a08c7d7454d23940674 | 38390192146 attempt 2 | complete |

B=0.8.2 在发布前经 GitHub、TestPyPI 和目标 Registry 查重。Nightly/Prerelease 使用 P1；Stable/Draft 使用 P2，源码版本差异和实际 code binding 分开记录。正常产物保留，TestPyPI 版本不删除。Draft 的生产 workflow 未启用 hosted Chart delivery 检查，故另外执行独立拉包及本地 Helm delivery/render 核验；此项明确标为本地证据。Prerelease、Stable 和 Draft 各遇一个第三方 free-disk setup 超时；只重试失败 job，实际 attempt audit 为一个镜像重跑，原生构建及 PyPI 均未重复。正常发布逐渠道证据由 `publication/acceptance-summary.json` 和对应类型目录保存。

## 失败、恢复与保留验证

真实 CMake 故障 source SHA 为 `5e92e86a036b248cb2843ca0cc6061340d9b7369`，仅隔离分支加入确定性 CMake 错误，未部署默认分支。每类 12 个原生构建实际失败，24 处故障日志匹配，恢复 snapshot 已保存，未产生完成 Manifest。

| 类型 | Release ID | 源 Run | P2 清理 Run | 独立回读 Run | 状态 |
| --- | --- | --- | --- | --- | --- |
| Draft | 411498895 | 38375710915 | 38394213842 | 38394506414 | complete |
| Nightly | 411510905 | 38375711251 | 38394510840 | 38394857789 | complete |

两类 Release、Tag、源 Run、原 snapshot 附件共十个精确端点均返回 404。Draft 85 个计划 Registry 坐标在清理前全部不存在，Nightly 无启用 Registry 坐标；不能将它们算作实际发布后删除的 85 个镜像。12 个共享 Builder 原 digest 均保持 200 且一致。原日志、计划及回读在 `real-failures/real-failures-closure-summary.json` 与对应目录。

隔离 fixture 验证 active/deferred、取消 callback、三次 synthetic 503 失败后恢复 holder 保留、Tag 已删除时 rediscovery/retry、真实 GHCR last-tagged-version 400 的 partial 非阻断及后续完整闭环。Nightly max_count 保持 7，success/failure/cancel 三个超额目标经真实 callback 删除并独立 404。仅验收 helper 对证实独占的临时 GHCR package 整包删除，生产没有整包兜底。证据在 `fixtures/evidence/execution/case-results.json`。初版固定时间窗失效与旧 fixture SHA 的 ref 写入 403 均保留原证据，不计为通过。

Draft quota 的生产 retention 验证已通过，六个 fillers 清理及最后独立回读已完成，目录从八项到七项，再回到唯一正常 Draft。汇总记录在 `draft-quota-evidence/case-result.json`，独立 Run 38403852436、artifact 11815579887。三十三端点包含三十二个已分配资源的删除 404 和一个从未创建的 Release-by-tag 404。实际 Draft 按 Release ID 排序，新建旧 Tag 不会变成旧 Release；为保护先创建的正常 Draft，使用一项已保存恢复资料但尚未创建 Release 的失败 fixture 加六个真实 Release fillers，验证总八项、唯一 own 失败候选与清理后七项。此项不宣称 quota 删除一个已存在的 Release ID；真实 Draft Release ID 删除由前述 CMake 失败场景独立证明。

## 历史失败对象

| Tag | Release ID | 源 Run | 原结论 | 状态 |
| --- | --- | --- | --- | --- |
| v0.7.0rc12 | 387563734 | 34693636766 | failure | blocked - v* Tag protection |
| v0.7.0rc10 | 387548975 | 34691047256 | cancelled | blocked - v* Tag protection |
| v0.7.0rc11 | 387556353 | 34692329616 | failure | blocked - v* Tag protection |
| v0.7.0rc14 | 387579666 | 34696454025 | cancelled | blocked - v* Tag protection |

四项原 body、全部 attempts/jobs、计划与日志已独立备份；rc14 十五附件共 60,519,936 字节逐项验证 size/SHA256。每项 169 个计划坐标已在实际 runner 做精确 census：前三项均不存在，rc14 仅 Chart version 1240791935 存在。PyPI 已发布版本排除出清理范围。历史 Prerelease retention 无限制，本次仅手动精确 Tag 清理。

rc12 的 P2 清理 38392917716 已完成 Registry 阶段及源 Run 删除，Tag 删除因既有规则集 20626631 返回 422，恢复 Release 与完整计划 snapshot 保留。四个 Tag 精确临时例外及立即恢复原规则的请求已准备，尚未执行，等待明确授权；详见 `history/ruleset-exception-plan.md`。保护规则不会为了重试长期停用，也不会加入通配例外。

历史原始证据另独立归档在 Run 38400311597、artifact 11814193992（约 67 MB），包含四项原始计划、日志和 jobs，以及 rc14 全部十五附件的新下载/size/SHA 回读。

代码修复、四类真实发布、两例真实 CMake 失败清理、GHCR/503/active/cancel 与两类 quota 验收均已完成。整体状态为 **blocked**：仅四项历史 Tag 清理因原规则阻断，需要明确批准已准备的四 Tag 临时例外。rc12 已完成 Registry 阶段与源 Run 删除，但 Tag、恢复 Release 保留；其他三项尚未删除。当前记录不将 partial/blocked 或绿色 workflow 单独视作全部删除完成。

## 独立证据入口

- 正常四类渠道、安装及最终指纹：`publication/acceptance-summary.json`、`publication/acceptance-summary.md`。
- 真实 CMake 失败：`real-failures/real-failures-closure-summary.json`。
- GHCR、手动恢复及 Nightly quota：`fixtures/evidence/execution/case-results.json`。
- Draft quota 和 teardown：`draft-quota-evidence/case-result.json`、`draft-quota-evidence/README.md`。
- 历史完整原始证据独立 archive：Run 38400311597 / artifact 11814193992；本地再次下载验证 92 个文件、全部原附件 size/SHA 和 ZIP CRC，记录 `archive-independent-readback.json`。
- 历史保护配置变更的具体请求：`history/ruleset-exception-plan.md`；目前没有执行任何规则变更。

Draft Chart 的额外架构/render 检查使用 224 份真实 Registry 命令响应和相同生产 verify 函数，再执行十九个实际 Helm 命令，属于本地验证。Nightly/Prerelease 使用 P1，Stable/Draft 使用 P2；没有将本批所有运行归成同一个实现 SHA。三次第三方磁盘准备步骤超时由一次 failed-only retry 解决，五分钟配置保留，可另行评估准备步骤耗时。没有运行 GPU/NPU 推理或性能基准。
