# Fork 四类正常发布验收

四类原始发布流程已完整成功，正常产物保留。范围是 `SuperMarioYL/unified-cache-management` 的 Fork preview；没有发布到官方仓库或正式 PyPI。GitHub 指纹在 Finalize 和适用的 retention/callback 终态后独立重取，包含 Tag、source Run、Release flags、完整 assets 和 Schema 9 manifest。

| 类型 | 实际 Tag | 实际 Run | 最终 attempt | GH assets | 实际启用渠道 |
|---|---|---|---:|---:|---|
| Nightly | `nightly/v0.8.0-20261013-1` | [38375348617](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/38375348617) | 1 | 16 | github_release |
| RC | `v0.8.2rc1` | [38377675294](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/38377675294) | 2 | 18 | chart_oci, dockerhub, ghcr, github_release, pypi |
| Stable | `v0.8.2` | [38383412316](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/38383412316) | 2 | 18 | chart_oci, dockerhub, ghcr, github_release, pypi |
| Draft | `draft/v0.8.2-1` | [38390192146](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/38390192146) | 2 | 18 | chart_oci, ghcr, github_release |

Nightly 使用 P1；RC 的 V1 相对 P1 的文件差异仅是 `version.ini`；Stable/Draft 的 V2 相对 P2 的文件差异也仅是该版本配置。P2 是本轮中途部署的 DockerHub absence probe 修复。每类保存 exact commit diff 与 source/code 对照，不将两代代码归成一个 SHA。

| 类型 | 实际 source SHA | 对应 production code SHA |
|---|---|---|
| Nightly | `776110b2524bf43eb5e197dcdb96315f9c6c3d8c` | `776110b2524bf43eb5e197dcdb96315f9c6c3d8c` |
| RC | `4858aef86456f712da6fd59ecb224235841ebdac` | `776110b2524bf43eb5e197dcdb96315f9c6c3d8c` |
| Stable | `3d6a11b01167c40b28ee6a08c7d7454d23940674` | `f66366538b35faa51507452ae1ad676b10994a4d` |
| Draft | `3d6a11b01167c40b28ee6a08c7d7454d23940674` | `f66366538b35faa51507452ae1ad676b10994a4d` |

每类完成 12 个 backend wheels 的实际 Linux build/runtime install，以及 Toolkit Python 3.9、3.12 安装检查。GH 每类提供 12 backend + 1 Toolkit wheel；所有 13 个 GH asset API digest 均匹配生成回执。Meta wheel属于 PyPI 交付契约，不计入 GH wheel assets。

RC/Stable 各验证 TestPyPI 的 8 projects、14 files，文件名全集和 SHA256 都匹配发布回执；各有 13 个实际 public-index install job 成功。RC/Stable 分别全量读取 168 个 OCI 引用（GHCR84 + DockerHub84），Draft 全量读取84个 GHCR引用；每个 index/member digest匹配原发布回执。三类 Chart 都独立从 OCI pull 和 GH asset download，包字节 SHA256 与完成态的生成回执相同。RC/Stable 的 hosted Helm 交付检查分别覆盖84个首选 registry引用；Draft按原profile跳过hosted该job，本轮额外并行保存真实Registry响应原文/SHA/时间，再用相同verify函数和实际Helm CLI验证其84个GHCR引用的架构与render，证据边界单独记录。

RC、Stable 的 RTD 英/中 versioned/latest 构建回执与公开 manifest 比较已完成，并保存公开响应 body/headers（匿名Set-Cookie值已脱敏，body及SHA不变）。RC versioned站仍为 RC；`latest` 在 Stable 完成后按预期指向 Stable。回执中 versioned commit 分别是 V1/V2，而 latest commit是 P2，JSON保留这一差别。

RC、Stable、Draft 的初次失败均来自单个 image 的 `free-disk-space` 五分钟 step timeout，发生于实际 Docker build/verify 之前；Finalize 随后因 image publication不完整失败。每类先归档失败状态/日志/receipt，再一次只重试 failed/dependent jobs。以实际 job started_at、runner_id、状态审计，三类均只有1个新 image执行，native和PyPI重跑数都为0；保留了第一次的真实失败证据。

Nightly外置 callback成功且当前产物 `kept`；RC/Stable运行内retention成功，策略为 unlimited；Draft运行内有限retention成功，当前tag明确 `kept`。完整 fingerprint与各渠道原始对照入口见 [acceptance-summary.json](acceptance-summary.json) 的每类 `proofs`，所有原始文件与 manifest保存在对应目录。

这些结果证明真实发布、安装和渠道闭环；没有运行 GPU/NPU 推理或吞吐基准。历史清理与注入 fixture 的验收由父任务独立汇总。
