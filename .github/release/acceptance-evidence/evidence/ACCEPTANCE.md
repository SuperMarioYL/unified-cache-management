# UCM 发布修复与 Fork 验收结果

当前状态：**complete**。目标仓库为 `SuperMarioYL/unified-cache-management`，最终生产代码 P2 已部署到默认分支 `develop`。官方仓库没有修改。本批正常发布产物、TestPyPI 版本和共享 package 保留；隔离测试产物及四个已确认失败或取消的历史对象均完成精确清理。

## 代码与检查

修复分支 `feature/release-cleanup-closure-20261013`，P1=`776110b2524bf43eb5e197dcdb96315f9c6c3d8c`，最终 P2=`f66366538b35faa51507452ae1ad676b10994a4d`。补齐共用 Crane 安装与精确 Release 查询，保留 GHCR skipped/partial 恢复语义及普通 Schema 9 历史 Manifest 坐标恢复；Schema 6/Nightly 约束及新发布 Tag 绑定保留。四模块清理边界没有改写。P2 根据实际 hosted preview 修复 Docker Hub probe 的 404 缺失归一化；403 等真实错误继续阻断。清理与 callback 输出日志、JSON report、实际实现 SHA/四模块 hash，独立 artifact 保留证据。

本地 release suite **610 passed、4 skipped**，docs Python **57 passed**，Node **16 passed**；双语严格文档构建、actionlint、ShellCheck、Black/isort/Ruff 通过。P2 默认分支及修复分支的四个 Push/Docs CI 通过，原始记录在 `runs/production-p2-ci.json`、`local/`。原无关 `.DS_Store` 与空 `out/` 样例仍为未跟踪本地文件。

## 四类真实正常发布

| 类型 | Tag | Run / attempt | 实际 source / code |
| --- | --- | --- | --- |
| Nightly | nightly/v0.8.0-20261013-1 | 38375348617 / 1 | P1 / P1 |
| Prerelease | v0.8.2rc1 | 38377675294 / 2 | V1 / P1 |
| Stable | v0.8.2 | 38383412316 / 2 | V2 / P2 |
| Draft | draft/v0.8.2-1 | 38390192146 / 2 | V2 / P2 |

V1=`4858aef86456f712da6fd59ecb224235841ebdac`，V2=`3d6a11b01167c40b28ee6a08c7d7454d23940674`，相对对应 code 的文件差异仅为 `version.ini`。B=0.8.2 经各目标渠道查重后选择。P1/P2 两代运行分别保留实际绑定，不将本批全部归为同一 SHA。

四类均完成十二个 backend wheel 的实际 Linux build/runtime install，以及 Toolkit Python 3.9/3.12 安装。RC/Stable 各核验 TestPyPI 八 projects、十四 files、十三个 public-index 安装及全部168个 OCI 引用（84 GHCR、84 Docker Hub）；Draft 全84 GHCR引用通过。三类 Chart 的 OCI/GitHub 包字节 SHA 一致。RC/Stable hosted Chart 检查覆盖84个引用；Draft 原 profile 跳过该 hosted job，额外保存224份真实 Registry 响应，交相同 verify 函数及十九个实际 Helm 命令完成本地架构/render 核验。

RC/Stable RTD 英/中 versioned/latest 回执与公开 manifest 已核验；Stable 完成后 latest 按预期转向 Stable，RC versioned 仍为 RC。匿名响应 cookie 已脱敏，body SHA 保留。三类首轮各有一个第三方磁盘准备步骤五分钟超时，先保存失败证据，再仅重试失败及依赖 job；各实际新增一个 image 执行，native/PyPI重跑均零。五分钟配置保留，后续可单独评估该准备步骤耗时。

完整渠道、安装、源码差异、最终 body/assets/ref/manifest 指纹见 `publication/acceptance-summary.json`、`publication/acceptance-summary.md`。正常产物均 kept。没有运行 GPU/NPU 推理或性能基准。

## 新失败、恢复及配额

确定性 CMake 故障仅位于隔离 source F=`5e92e86a036b248cb2843ca0cc6061340d9b7369`。Draft/Nightly 各十二个原生 job 实际失败、二十四处故障日志匹配，恢复 snapshot 已保存，未产生完成 Manifest。两类 Release、Tag、源 Run 与原 snapshot assets 共十个端点独立404；十二个共享 Builder 原 digest 保持200且一致。Draft 的85计划 Registry坐标清理前全部不存在，Nightly无启用 Registry，不能将它们算作85个已发布镜像删除。证据在 `real-failures/real-failures-closure-summary.json`。

隔离 fixture 验证 active/deferred、真实取消 callback、三次 synthetic503失败后 holder 保留、Tag 已删除时重新发现与重试，以及真实 GHCR last-tagged-version400的 partial 非阻断与最终闭环。只有验收 helper 在确认整包独占后删除临时 package；生产没有整包删除兜底。Nightly max_count=7保持，success/failure/cancel超额目标经实际callback删除。源日志和回读由独立 observer先保存，再删除源 Run，见 `fixtures/evidence/execution/case-results.json`。

Draft 按 Release ID排序。为保护先创建的正常 Draft，最小配额构造使用一个已保存恢复资料、尚未创建 Release 的失败版本及六个真实 Release fillers。生产入口实测8→7，唯一失败候选属于本批allowlist，正常Draft当时active且kept；随后六个fillers精确清理，目录回到唯一正常Draft。三十三端点中，三十二个已分配资源404，另一项是从未创建的 Release-by-tag404。此项不宣称quota删除已存在的Release ID；实际失败Draft Release删除由前述CMake案例独立证明。见 `draft-quota-evidence/case-result.json`、`README.md`。

固定时间窗、旧workflow commit ref权限、quota配置初始化、观察器契约误用及汇总打包遗漏等 harness 失败均保留原始记录，修正后重新验真，不算作P2缺陷或通过证据。

## 历史四项与保护恢复

| Tag | 原 Release ID | 原 Source Run | P2 cleanup Run | 独立 observer Run | 状态 |
| --- | --- | --- | --- | --- | --- |
| v0.7.0rc12 | 387563734 | 34693636766 | 38412788662 | 38413855557 | complete |
| v0.7.0rc10 | 387548975 | 34691047256 | 38414533508 | 38415134558 | complete |
| v0.7.0rc11 | 387556353 | 34692329616 | 38415489855 | 38415967255 | complete |
| v0.7.0rc14 | 387579666 | 34696454025 | 38416268574 | 38416905738 | complete |

四项均先核验实际失败或取消 Run、精确 Tag/SHA、归属、原始备份和全量 census。四次原169计划坐标中，675项原已不存在，只有rc14 Chart version1240791935原存在，现独立404。原body、计划、全部attempts/jobs、日志和rc14十五原附件均完整备份；恢复snapshot先在原正确Release ID上传、回读169原坐标，再删源Run。四项原Release/Tag/Run及原/恢复assets、Chart共三十二个精确已分配资源独立404。Stable/Prerelease不限自动保留，本次证明的是手动精确Tag清理。PyPI已发布记录保持。

初次rc12因规则集20626631的v*删除保护返回422，holder安全保留。用户明确批准四精确Tag临时例外；执行前/恢复前逐次完整body、ETag、updated_at核验。第一窗口在观察器GitHub504后立即恢复原规则，只读重试取到精确404后才重开第二窗口。最后全部写入完成立即恢复；独立Admin GET确认原可写body完全一致、exclude=[]、active、bypass=[]。原七个对象及本批四类正常产物的二十二次final GET指纹全部一致。服务端时间戳按实际更新记录，不声称回退。见 `history/authorized-history-summary.json`、`history/ruleset-exception/windows/`。

历史原始独立archive：Run38400311597 / artifact11814193992，约67MB。再次下载核验92文件SHA/size、ZIP CRC及十五附件60,519,936字节，见 `archive-independent-readback.json`；唯一证据没有留在已删除源Run内。

最后聚合观察器独立回读完成后，其Run/artifact链接与原始JSON将记录到 `final-evidence-delivery.json`。
