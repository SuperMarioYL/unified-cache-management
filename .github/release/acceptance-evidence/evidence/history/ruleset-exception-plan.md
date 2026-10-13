# 四个历史 Release Tag 的临时规则例外计划

目前仅准备与只读核验；没有修改规则集或 Tag。待用户明确授权下述规则变更，并由协调者确认 default manual cleanup 独占窗口，才可执行写入。

## 实际阻断与权限

Fork `SuperMarioYL/unified-cache-management` 的规则集 [20626631](https://github.com/SuperMarioYL/unified-cache-management/rules/20626631) 名为 `Protect v* release tags`，`target=tag`、`enforcement=active`，匹配 `refs/tags/v*`，当前 `exclude=[]`、`bypass_actors=[]`。规则同时禁止删除和更新；当前账号 `current_user_can_bypass=never`。

P2 实际 cleanup run [38392917716](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/38392917716) 已完成 rc12 的 registry 阶段和 source-run 删除，随后在第三阶段删除 Tag 时收到 HTTP 422，原文为 `Repository rule violations found` 和 `Cannot delete this tag`。该次失败没有 registry 429；Release 387563734 与原 Tag 保留，Release 上的恢复快照已逐坐标校验为原计划的 169 项。原始响应见 [tag-422-response.json](deletions/v0.7.0rc12/tag-422-response.json)，恢复证据见 [recovery-snapshot-readback.json](deletions/v0.7.0rc12/recovery-snapshot-readback.json)。

只读 `GET /repos/SuperMarioYL/unified-cache-management` 返回账号 `admin=true`；规则 GET 返回 `bypass_actors`；另对要求 Administration 写权限的规则历史 GET 成功，返回 14 条记录。以上支持账号可编辑规则，实际 PUT 尚未尝试。GitHub 文档说明规则 GET 仅向具备规则写权限的请求者返回 `bypass_actors`，更新接口的 fine-grained token 需要 Administration 写权限：[Get ruleset](https://docs.github.com/en/rest/repos/rules#get-a-repository-ruleset)、[Update ruleset](https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset)。

原规则 `updated_at=2026-08-20T23:52:12.590+08:00`，ETag 为 `W/"e0e48b1b9420b9a11fbdaf99e48162891e4ae51b6730fe7084a0d9da37479a66"`。完整服务端原文、HTTP headers、权限及历史响应均独立保存于 [ruleset-exception](ruleset-exception/)；此处的时间和 ETag 只是这次 GET 的快照，写入前必须重新核验。

## 精确变更

完整 PUT 请求仅增加以下四项到 `/conditions/ref_name/exclude`：

```json
[
  "refs/tags/v0.7.0rc10",
  "refs/tags/v0.7.0rc11",
  "refs/tags/v0.7.0rc12",
  "refs/tags/v0.7.0rc14"
]
```

这四个精确 ref 会暂时不匹配整个规则集，因此删除和更新限制都会对它们暂停。例外覆盖这四个名字，无论请求者是谁；应只在受协调的短维护窗口开放。没有通配符例外，没有 bypass actor，没有改变 include、规则定义、enforcement、target 或 name。其余 Tag 继续匹配原规则；初始七个受保护验收 baseline 与四个 Tag 无重叠。`exclude` 的语义由 [GitHub Update ruleset 文档](https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset)定义。

完整写入体为 [ruleset-exception.put.json](ruleset-exception/ruleset-exception.put.json)，原样恢复的完整可写体为 [ruleset-restore.put.json](ruleset-exception/ruleset-restore.put.json)。[精确 diff](ruleset-exception/ruleset-exception.diff) 与 [机器核验结果](ruleset-exception/payload-validation.json) 确认唯一变化是上述路径。两份请求逐项保留 `name`、`target`、`enforcement`、`bypass_actors`、`conditions`、`rules`；服务端只读字段 `id`、`node_id`、`source`、`source_type`、链接和 timestamps 保存在 [完整原文](ruleset-exception/ruleset-original.full.json)，不作为 PUT 参数。更新或恢复后 `updated_at`/ETag 预计变化，恢复验收比较可写语义及规则身份，不声称服务端时间戳可以回退。

## 授权后的执行与恢复条件

1. 确认明确的四 Tag 规则例外授权、维护窗口与 P2 manual 独占锁。再次核验四个 Tag SHA、对应 Release/run 身份、完整 source-plan 与 assets 备份、169 项 Census 以及七个 baseline。rc12 已删 source run，不重造该 run 或库存；从保存的原记录与 Release 恢复快照继续。若 Tag 移动、库存身份变化、资源探测非 404 错误或 baseline 改变，停止写入。
2. 使用下面的 GET guard，比较完整可写体、ETag、updated_at。任何差异都停止，保留新响应交协调者审查；不得用旧全量 PUT 覆盖别人的变更。GET 与 PUT 之间仍存在竞态，这不是原子 compare-and-swap；GitHub 更新接口未在所用文档中保证 `If-Match`，不依赖未验证的条件写入。维护窗口中须避免其他规则编辑，并紧接着执行已审阅 PUT。
3. 经授权执行 exception PUT 后，立即 GET 确认全量可写体等于预定 exception，同时保存新的完整原文、ETag、updated_at。未明确回读到该状态，不删除任何 Tag；PUT 超时或响应不明确时，先 GET 确认实际状态，不盲目重复写入。
4. 按 rc12 → rc10 → rc11 → rc14 串行运行原 P2 default manual cleanup，每次唯一匹配新 Run ID。P2 为 `f66366538b35faa51507452ae1ad676b10994a4d`。使用当前已保留的完整 Census，不循环制造 registry 读取；遇到 401/403/429、GHCR skipped/partial、inventory blocked 或其它错误，不把它算 complete。rc12 从有效 recovery holder 重试，不直接假定只删 Release。每次用独立 Hcensus `history-readback` 精确回读原 Release、Tag、source Run、原资产 ID 与实际存在的 Chart version；所选资源全部 404 且 baseline 未变才算完整删除。
5. 四个 case 完成后立刻恢复。任何一步 failure、partial、blocked、取消、需要等待 quota、失去独占锁或出现未预期状态，也立即结束例外窗口并恢复，保留 recovery holder；不得把例外一直留到之后重试。即使剩余历史 case 未完成，也先恢复。后续再开窗口必须重新核验并获得协调者安排。
6. 恢复前 guard 必须与步骤 3 保存的 exception body、ETag、updated_at 完全匹配。若不匹配，停止旧恢复 PUT 并立即通知协调者：不能用原始全量体覆盖并发修改。不得擅自生成合并恢复体；如需在新状态下只移除本次四项，须重新审阅那份具体请求。若匹配，执行 restore PUT，随后 GET 核验原可写体完全恢复，`exclude=[]`、enforcement active、rules 与 bypass actors 不变、规则 ID/source 不变。七个 baseline 再次回读。

### 已准备的命令（未执行 PUT）

以下命令仅为审阅与授权后的 runbook。GET guard 本身只有读取副作用，已在当前规则上成功核验。

```sh
ruleset_evidence=/Users/yulei/workspace/ucm-release-acceptance/20261013/history/ruleset-exception
python3 "$ruleset_evidence/verify-ruleset-get.py" \
  --expected-body "$ruleset_evidence/ruleset-restore.put.json" \
  --expected-context "$ruleset_evidence/get-context.json" \
  --output-prefix "$ruleset_evidence/before-exception"

# 仅在上一步匹配、具体授权和维护窗口仍有效时执行。
gh api --method PUT \
  repos/SuperMarioYL/unified-cache-management/rulesets/20626631 \
  --input "$ruleset_evidence/ruleset-exception.put.json" \
  > "$ruleset_evidence/exception-put-response.json"
python3 "$ruleset_evidence/verify-ruleset-get.py" \
  --expected-body "$ruleset_evidence/ruleset-exception.put.json" \
  --output-prefix "$ruleset_evidence/exception-readback"

# 删除窗口结束或任一步需要中断：恢复前先核验例外状态及其版本。
python3 "$ruleset_evidence/verify-ruleset-get.py" \
  --expected-body "$ruleset_evidence/ruleset-exception.put.json" \
  --expected-context "$ruleset_evidence/exception-readback.context.json" \
  --output-prefix "$ruleset_evidence/before-restore"
gh api --method PUT \
  repos/SuperMarioYL/unified-cache-management/rulesets/20626631 \
  --input "$ruleset_evidence/ruleset-restore.put.json" \
  > "$ruleset_evidence/restore-put-response.json"
python3 "$ruleset_evidence/verify-ruleset-get.py" \
  --expected-body "$ruleset_evidence/ruleset-restore.put.json" \
  --output-prefix "$ruleset_evidence/restore-readback"
```

Shell 命令必须逐步检查退出码；不能在 guard 失败后继续执行下一条 PUT。实际 response 和 readback 的文件在对应步骤才会生成，目前不存在完成写入的证据。若 PUT 请求返回错误，仍用 GET 确认状态；只有回读可写体等于原体才能声明规则恢复。恢复未得到确认时，明确报告保护仍未确认，停止后续删除并保留完整证据。

## 保存的核验证据

- [protected cleanup 坐标与 rc14 Chart version 别名审计](protected-coordinate-audit/README.md)：六份原 Manifest 坐标与四个历史计划交集为零；runner 完整 version metadata 确认 rc14 version1240791935 仅有 `0.7.0-rc.14` 别名，未与受保护 Chart aliases 共用。rc13 无 Manifest，明确仍为 GitHub 指纹保护。
- [当前精确 holder 回读](ruleset-exception/holders-current/holders-current.json)：四个 Tag 均 200 且 SHA 与原备份一致，四个 Release 均 200 且 ID/Tag 一致。rc10、rc11、rc14 的 source run 仍 200，rc12 source run 为 404；Release assets 分别为 0、0、1、15。每个 GET 原始 HTTP 响应也保存在同目录。此回读只证明当前 holder 状态，不替代写入前再次核验或删除后的全资源 404 验收。
- [GET headers](ruleset-exception/ruleset-get.headers.txt)、[原始 JSON bytes](ruleset-exception/ruleset-get.raw.json)、[完整 JSON](ruleset-exception/ruleset-original.full.json)、[ETag/timestamps/context](ruleset-exception/get-context.json)。
- [编辑权限证据](ruleset-exception/edit-permission-evidence.json)、[只读规则历史 GET](ruleset-exception/ruleset-history-read.headers-and-body.txt)。
- [完整 exception PUT](ruleset-exception/ruleset-exception.put.json)、[完整 restore PUT](ruleset-exception/ruleset-restore.put.json)、[精确 diff](ruleset-exception/ruleset-exception.diff)、[payload 变更及 SHA256 核验](ruleset-exception/payload-validation.json)。
- [只读执行前与回读 guard](ruleset-exception/verify-ruleset-get.py)。

这项计划仅涉及 Fork 的单个规则集和四个历史名字，不涉及官方仓库、PyPI、共享 package 整体删除或任何新增发布资源。
