Draft 保留验收已闭环，生产固定为 P2 `f66366538b35faa51507452ae1ad676b10994a4d`。`case-result.json` 聚合全部真实 Run IDs、artifact URLs、原始生产报告、代码绑定与精确回读。

这是隔离生命周期 fixture，未构建 UCM 产品。生产实际目录计入 1 个失败 Tag-only 版本、6 个有真实 Release 的 fillers 和 1 个正常 Draft，共 8 个；原配置 max_count=7 未修改。独立 observer 38400199879（Hq2 `9462df4168fca541912755fc4fb745cd798f800c`）先上传完整源 metadata/logs/恢复快照到 artifact 11814699250，回读 ZIP CRC 后调用 P2 的 retention CLI。生产报告 7 kept、1 deleted，真实目录从 8 降为 7。

失败 victim 为 `draft/v0.8.0-202610139201` / source 38399999026。实际 target report 的 `release_ids=[]`，因此删除证据仅覆盖 Tag 和源 Run。Release-by-tag 在创建前及清理后都为 404，证明它从未分配，不能称为具体 Release ID 删除。真实 Draft Release-ID 删除由 ../real-failures/ 的 CMake 失败案例独立证明。

正常 Draft `draft/v0.8.2-1` / Release 411606028 / source 38390192146 在前置 actual catalog 中 active，生产报告 kept；它未被选中或传给 DELETE。后置原始源 metadata 证明它保留并最终正常完成（success）。原始 7 个基线加正常 Nightly 共 8 个指纹不变，四类正常发布的 Release ID 和 Tag commit 全部保留。

六个 fillers 用 Fork 默认 develop 的 P2 tag workflow 顺序清理，每例核对实际生产 SHA、四模块哈希、sole exact source/Release ID、零 registry，以及 Release/Tag/Run/原始两个 snapshot assets 的五个 HTTP404：

| Fixture suffix | Release ID | Source Run ID | Default P2 cleanup Run | Artifact ID |
| --- | --- | --- | --- | --- |
| 202610139202 | 411666525 | 38399689248 | 38401725967 | 11815996290 |
| 202610139203 | 411668647 | 38400006465 | 38401898952 | 11815556651 |
| 202610139204 | 411668707 | 38400015279 | 38402072204 | 11815402723 |
| 202610139205 | 411668911 | 38400021657 | 38402307918 | 11816485612 |
| 202610139206 | 411668817 | 38400028008 | 38402503689 | 11815542444 |
| 202610139207 | 411671779 | 38400442479 | 38402719657 | 11815622893 |

最终独立只读 Hq3 `668e5b621c5250c4b2587caa331d8ecaad1dd7ee` observer 38403852436 / artifact 11815579887 精确绑定原 Hq2 quota observer，33 个端点全部 HTTP404：六个 fillers × 五个资源=30，加失败 victim 的 Tag/Run=2，以及从未分配的 Release-by-tag=1。总计 32 个已分配资源的删除回读，加 1 个从未分配资源的缺失回读。当前实际 Draft 目录仅剩正常 Draft，共 1 个；隔离发布资源无残留，独立观察与备份 artifacts 保留作审计。

脚本自身错误另列，不算 P2 缺陷：Hq1 未初始化复用 helper CONFIG，6 源在资源初始化前失败，独立 archive observer 38399247127/artifact11813922393 归档日志 CRC/SHA 和 NoRelease/NoArtifacts 后，仅自己的六个控制 Tag/Run 被清掉；Hq2 首次只读回读误用 publication-source API 读取 cleanup observer，P2 正确拒绝。旧失败 observer38402985319/artifact11815882967保留，修正仅在 Hq3 只读分支，未修改生产接口或 P2。
