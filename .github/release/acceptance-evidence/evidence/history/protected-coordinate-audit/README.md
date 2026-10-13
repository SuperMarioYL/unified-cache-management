# 历史删除与受保护资源的坐标及别名检查

本次只读核验支持删除 rc14 的精确 Chart version：version `1240791935` 的完整 runner GET 响应中只有别名 `0.7.0-rc.14`，没有受保护成功版本的 Chart 别名。六个可得的原 Manifest 投影出的 cleanup 坐标也与四个历史计划无交集。缺少 Manifest 的 rc13 只具有 GitHub 指纹保护，不能把它计为已验证 registry 坐标。

## rc14 Chart version 的独立 runner 回读

隔离观察器基于不可变 Hcensus `26c9f17df7f28e6d169fdd41b39ed4c7ce5e6364`，仅在历史 `registry_version` GET 结果中增加完整 `metadata=value` 保存。新 helper commit 为 `776bfbb1cdad7fd30d6beb2a794ae9aedd87a708`，分支为 `feature/history-version-metadata-20261013`；生产和 default 仍为 P2 `f66366538b35faa51507452ae1ad676b10994a4d`。

只读 [observer run 38397900638](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/38397900638) 通过执行前后 API 新 Run ID 差集、helper SHA、branch、scenario 与 source-run ID 唯一绑定。它使用 runner 凭据 GET：

```text
/users/SuperMarioYL/packages/container/charts%2Funified-cache-chart/versions/1240791935
```

返回 HTTP 200，version id 为 `1240791935`，digest 为 `sha256:6f6be37f7eb83488c3578f383449e45b07d34fd7693bc8b67a63412173ed8cff`，`metadata.container.tags` 完整列表为 `["0.7.0-rc.14"]`。受保护 rc15、rc16、rc17、0.8rc2 的 Chart aliases 分别为 `0.7.0-rc.15`、`0.7.0-rc.16`、`0.7.0-rc.17`、`0.8.0-rc.2`；均不在这个 version 上。

完整 metadata 保存为 [chart-version-1240791935.full.json](../version-metadata-observer/chart-version-1240791935.full.json)，原始 always artifact ZIP 和完整 hosted proof 保存在 [version-metadata-observer](../version-metadata-observer/)，[validated-metadata.json](../version-metadata-observer/validated-metadata.json) 保存 source binding、digest、全部别名、文件 SHA256 与七个冻结 baseline 的一致性校验。

此 run 的 conclusion 为 failure，proof status 为 `incomplete`：19 个精确旧 holder、原 asset 和 Chart version 仍为 200，这是预期的删除前状态。没有 401/403 等探测错误，七个 protected baseline 与绑定时完全一致；它是别名审计证据，不是历史删除完成证据。此 observer 没有重新遍历四组 registry refs，也没有读取 DockerHub 镜像。

## 原 Manifest 的 cleanup 坐标交集

六份原 Manifest 通过绑定时的精确 asset ID 下载，并逐一验证 bytes SHA256 和 size 与初始 protected fingerprint 一致；没有由 Tag 名推测或重新生成坐标。使用 P2 的 `record_from_manifest` 投影，保留 Schema 9 Manifest 的原坐标，使用既有 Schema 6 读取约束。

| 初始 protected Release | 原 Manifest schema | registry cleanup 坐标数 | 与四个历史计划的交集 |
| --- | --- | ---: | ---: |
| nightly/v0.8.0-20260918-1 | 9 | 0 | 0 |
| nightly/v0.8.0-20260918-2 | 9 | 0 | 0 |
| v0.7.0rc15 | 9 | 169 | 0 |
| v0.7.0rc16 | 9 | 169 | 0 |
| v0.7.0rc17 | 6 | 169 | 0 |
| v0.8.0rc2 | 9 | 169 | 0 |
| v0.7.0rc13 | 无 Manifest | 未知 | 未声称验证 |

两个 Nightly 的原 Manifest 本身没有 registry 清理资源。其余四份共 676 个 registry 坐标；四个历史计划 rc10、rc11、rc12、rc14 各有 169 项。每个可得 Manifest 与每个历史计划分别比较 `(kind, reference)` 和不依赖 kind 的 `reference`，两种比较的 24 组交集都为空。这证明原坐标不重叠；Chart 可能同 digest 多别名的额外风险由上面的完整 version metadata 独立检查。

[manifest-download-ledger.json](manifest-download-ledger.json) 保存原 asset IDs、schema、bytes digest 验证和文件路径；每个子目录保留原 Manifest 及 P2 投影的完整 cleanup record。[coordinate-overlap.json](coordinate-overlap.json) 保存全部受保护坐标、24 组精确交集、24 组 reference-only 交集，以及原四个 Census 文件的 SHA256；原有 Census 与备份没有覆盖。

rc13 原 Release 没有 assets/Manifest。现有证据能保证它的 Release、Tag SHA、body 和 assets 指纹保持一致，不能推断其未知 registry 库存。本次检查没有执行规则例外或新的历史删除；具体规则授权仍待协调者确认。先前 rc12 的 P2 cleanup 已删除 source run，在 Tag 规则 422 后保留 Tag 与恢复 Release，不能算完整删除。后续实际删除仍须对原精确资源执行独立 404 回读，成功版本仍须通过 baseline 一致性核验。
