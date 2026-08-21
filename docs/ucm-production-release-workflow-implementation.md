# UCM 生产发布 Workflow 实施与验收记录

## 结论

当前分支已实现仓库自身的完整预览发布主链：只读 Tag candidate、默认分支可信
controller、六个 backend/architecture wheel、六个镜像 member、三个多架构
GHCR index、Chart OCI、GitHub Draft/Pre-release、远端读回与幂等恢复。

本轮在 `SuperMarioYL/unified-cache-management` 验证以下真实通道：

- Draft：私有 GHCR + GitHub Draft Release；
- RC：公开 GHCR + Chart OCI + GitHub Pre-release；
- 同一 RC Tag 的第二次执行：只允许 identical/reuse，不允许覆盖。

PyPI 与 Docker Hub 已保留 Stable/Hotfix 适配器，但当前可信配置明确关闭，且本轮不对这
两个外部系统写入。GPU/NPU 运行和 Kubernetes 集群验收也不由发布 Workflow 代替。

## 发布拓扑

1. `production-tag-candidate.yml` 由附注 Tag 触发，以只读权限构建并封存候选制品。
2. `production-release-controller.yml` 只接受成功的 candidate `workflow_run`，并从当前
   默认分支取得可信控制代码。
3. `_production-release-controller.yml` 重建 wheel 并比较字节，生成无写入计划，再经
   `release-production` Environment 审批。
4. 发布 Job 以 absent/create、identical/reuse、conflict/block 状态机写入并读回 GHCR、
   Chart OCI 与 GitHub Release。
5. 最终 evidence Artifact 独立记录 run、source、Tag、Environment、channel 与 operation。

## 第一次实际预览

固定版本和对象：

```text
release branch: 0.6.0-release
Draft Tag:      draft/v0.6.0-1
RC Tag:         v0.6.0rc1
```

发布分支只修改受信配置允许的版本文件。Tag 必须为附注 Tag，并指向发布分支当前 head。
Tag 一旦推送就保留；源码修复使用新的 Draft/RC 编号。

Draft 审批前检查计划中只包含当前仓库坐标、三个 `-private` 镜像仓库、六个 wheel 与一个
Chart Release asset，且 PyPI/Docker Hub operation 为零。RC 审批前确认 source SHA 与
已接受 Draft 一致、三个正式 GHCR 仓库和 Chart OCI 坐标正确。

若新建的正式 GHCR/Chart package 仍为 private，第一次 RC 会保留 GitHub Release 为
Draft，并返回 `visibility-configuration-required`。由 owner 只修改这四个 package 的
visibility 后，重新运行原 controller；不得移动 Tag 或删除远端对象。

## 证据层

| 证据层 | 当前状态 | 完成条件 |
| --- | --- | --- |
| 本地实现与测试 | production 219/219；v2 565/565；legacy 543 passed、1 skipped、1 Docker daemon 离线 | Hosted CI 需补齐真实 Buildx 结果 |
| GitHub Hosted | 尚未运行 | PR checks 和真实 candidate/controller run 通过 |
| GHCR/Release/Chart 读回 | 尚未运行 | API/匿名或认证读回摘要一致 |
| GPU/NPU 硬件 | 未验证 | 独立硬件测试证据 |
| Kubernetes 集群 | 未验证 | 独立集群验收证据 |
| Stable/PyPI/Docker Hub | 未发布 | 后续 Stable/Hotfix 流程另行批准 |

本地静态门禁结果：Ruff、Black、compileall、7 个 Draft 2020-12 Schema 和 6 个生产
Workflow 的 actionlint 全部通过。legacy 唯一失败来自本机 Docker CLI 存在但 Docker
daemon 未运行，错误为无法连接 `~/.docker/run/docker.sock`；它不被记录为测试通过，交由
GitHub Hosted runner 的真实 Buildx 检查补齐。

## 回滚与重试

生产 Workflow 不覆盖、不删除。失败后保留已经完成的对象和诊断 evidence；同一不可变
Tag 重新运行时只补齐缺失对象。任何同名不同摘要对象都阻断整次发布，交由人工调查。
发布分支或源码有变化时必须创建新 Tag 编号。
