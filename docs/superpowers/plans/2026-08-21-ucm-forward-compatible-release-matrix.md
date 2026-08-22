# UCM 向前兼容发布矩阵实施计划

**Goal:** 用单文件模板化 `release.yaml` 替代固定 `wheel_profiles`，自动发现 CUDA、CANN、Variant、Mooncake、Python ABI 和 CPU 架构，并驱动 WHL、运行时镜像及正式发布全链路。

**Architecture:** 上游发现生成 Capability Catalog；产品规则生成 Candidate Plan；GitHub 构建结果与上一版 Release Manifest 共同生成 Admitted Release Plan。新增失败项隔离，既有正式产品失败阻断发布。所有入口共用同一计划。

**Tech Stack:** Python 3.12 控制器、PyYAML/JSON Schema、GitHub Actions、Docker Buildx、OCI/GHCR、PyPI、pytest、actionlint。

**Spec:** `docs/superpowers/specs/2026-08-21-ucm-forward-compatible-release-matrix-design.md`

## Global Constraints

- 使用 `feature/cicd-forward-compatible-matrix` 分支和 GitHub PR。
- 禁止在本地运行 pytest、actionlint、Docker build、Wheel build、镜像 build 或发布模拟。
- 本地只允许源码检查、编辑、`git diff`、commit 和 push。
- RED/GREEN 测试循环均通过 GitHub Actions 完成：先推送预期失败的测试，再推送实现并观察同一检查转绿。
- 每个验证结论必须记录 GitHub run ID、URL、head SHA、Job 结果和关键 Artifact。
- `release.yaml` 升级为 Schema v3，不保留 v2 兼容解析层。
- 不保留 `uc-manager-cuda`、`uc-manager-cann-a2/a3` 滚动别名，也不删除历史包。
- 只自动过滤 `310p`；其他 Variant 进入候选流程。
- Python ABI 自动探测，并按 `requires-python >=3.10` 过滤。
- Mooncake 版本来自匹配的 vLLM-Ascend 运行时。
- 新能力失败隔离；上一版已正式发布的能力失败则阻断发布。
- 发布阶段按 Admitted Release Plan 的独立单元全并行；不得在单一 Job 内串行循环发布产品或渠道。
- 并行发布保持动态增长：不得为 CUDA、A2、A3 或当前产品数量写死 Job。
- OCI member 先于所属 family index；最终 Manifest、GitHub Release 封板与 readback 是唯一允许等待全部发布 Result Artifact 的 barrier。

## Task 1: 建立 GitHub Hosted 测试基线

- 创建实施分支并建立 PR，保留所有现有 GitHub 检查。
- 修改 `push-check.yml` 和 `pull-request.yml`，确保 GitHub 执行：

  ```text
  pytest .github/release/tests -q
  pytest .github/release/production/tests -q
  actionlint
  git diff --check
  ```

- 增加 Hosted 测试结果汇总 Job，输出源码 SHA、测试数量和失败阶段。
- 推送仅包含测试入口的提交。
- 确认 Push Commit Checks 和 Pull Request Gate 在 GitHub 上通过，记录基线 run。
- 后续任务禁止用本地测试替代这些 Hosted checks。

## Task 2: 落地 Schema v3 和单一配置权威

- 将已确认的最终 YAML 写入 `.github/release/release.yaml`，同步更新配置 Schema。
- 新增 `capabilities.py` 和 `products.py`，分别拥有能力模型和产品展开规则。
- 删除 `wheel_profiles`、固定 PyPI Distribution 列表及 Profile Builder 注入逻辑。
- 实现：

  ```python
  compact_accelerator_runtime("cuda-13.0") == "130"
  compact_accelerator_runtime("cann-9.1.0") == "910"
  compact_mooncake_version("0.3.11.post1") == "0311post1"
  ```

- 先提交 Schema v3 RED 测试，推送后确认 GitHub 因尚未支持 v3 而失败。
- 再提交最小加载和规范化实现，推送后确认相同 GitHub Job 转绿。
- Hosted tests 必须覆盖：拒绝 v2、拒绝残留 `wheel_profiles`、非法模板、重复 Distribution 和未知模板变量。

## Task 3: 构建统一 Capability Catalog

- Catalog 条目包含：`accelerator`、`accelerator_runtime`、`variant`、`cpu_architecture`、`manylinux`、`python_version`、`python_abi`、`source_image`、`target_image`、`mooncake_version`。
- vLLM 从 Buildkite `BUILD_BASE_IMAGE` 发现 CUDA/架构，在 GitHub 原生 Runner 中探测全部 `/opt/python/cp*-cp*/bin/python`。
- vLLM-Ascend 扫描全部 `Dockerfile.buildwheel.*`，只排除 `310p`。
- Runtime Catalog 扫描 vLLM/vLLM-Ascend 镜像，并读取对应 Git Tag 源码。
- 从 vLLM-Ascend 运行时 Dockerfile 的 `MOONCAKE_TAG` 读取 Mooncake 版本，并在 GitHub Runner 中验证镜像实际安装版本。
- Ascend Builder 从匹配运行时镜像复制 Mooncake headers/libs，不再克隆固定 `0.3.9`。
- 先推送 discovery fixture RED 测试，确认 GitHub 拒绝旧的单 Python/固定 Mooncake Catalog。
- 推送实现后，fixture 覆盖 CUDA 12.9/13.0、CANN 9.0/9.1、多个 Python ABI、未来非 310P Variant 和 Mooncake 不一致隔离。
- 使用 PR 上的 `/ucm-build all` 或同等真实构建入口运行 Builder discovery，上传 Capability Catalog Artifact。
- 下载并检查 Artifact，确认内容来自真实上游发现，不是 fixture。

## Task 4: 动态生成 Candidate 和 Admitted Plan

### Task 4A1: Authority、选择和 dependency requests

- Task 3 validated Capability Catalog 是唯一发现输入；新增 CLI `ucm_release plan prepare-candidates`。
- Schema v3 按 `runtime_product` 绑定有序 exact selectors：vLLM 为 `v{version}`、`v{version}-cu{runtime.major_minor.compact}`，vLLM-Ascend 为 `v{version}`、`v{version}-{variant}`；禁止跨 policy 组合/重排。`variant` 只来自 Catalog-normalized Variant。
- 按 product/binding accelerator-runtime family/Variant/architecture 分组；组内按 runtime version 分桶降序，再执行 selector 声明顺序。同 selector 多匹配硬失败；无匹配形成 `runtime-flavor-unsupported` 后检查旧版本。baseline exact runtime 重开延后 Task 4B。
- `builders.py` 冻结并验证 `ucm-current-builder-authority`。CandidateSelection/Catalog/authority source SHA exact 相等；新 revision exact 匹配 current recipe/toolchain，零匹配 local exclusion，多匹配硬失败；products 不复制 authority validator。
- Schema v3 `dependencies` 只接受 canonical name 与不含 epoch/local 的 exact canonical PEP 440 pins；prerelease 必须显式 pin，禁止 constraint/range。每个 requirement 冻结 `{requirement_id,scope,name,version}`。
- `capabilities.compile_python_coordinate` 是 Python coordinate 唯一公式 owner；`validate_selected_capability_evidence` 是 capability/revision/runtime/binding identity/projection/compatibility 唯一 validator，且把每个 capability 的 revision IDs 精确投影为本次 selected revisions，拒绝 full historical/dangling ID；products 调用后再校验 discovered product/runtime 关系。
- `products.py` 输出封闭 `ucm-candidate-selection`，冻结 exact selected evidence、exclusions 和规范唯一 dependency requests。A1 CLI/API 不读 baseline；`baseline_manifest_sha256: null`、`baseline_selections: []`、`blockers: []`，不包含 build tasks 或网络访问。
- 先提交 authority/selector/Python coordinate/CandidateSelection RED；fixture 从独立 live-shaped raw Catalog 输入动态增长，不固定 runtime、binding 或 ABI 数量。

### Task 4A2: DependencyResolution 和纯 Candidate graph

- 新增 `dependencies.py` 与 CLI `ucm_release dependencies resolve --selection`。它显式消费规范化 config + raw persisted Selection，先调用唯一 `products.validate_candidate_selection`，再校验 config digest 和 `max_wheel_tasks` request fan-out，然后访问 fixed PEP 691 endpoint `https://pypi.org/simple/<canonical-name>/`（exact JSON Accept）；CLI 不接受任意 index override。依赖方向只允许 `dependencies -> products`。它按 request coordinate 构造 standards-compatible target tags；manylinux native candidate 的 PEP 600 glibc tuple 必须 `<=` target、architecture exact 相等，同层优先最高 compatible floor，并识别 `manylinux2014 -> manylinux_2_17` 等 legacy aliases 与 compressed platform tags。禁止读取 host glibc/`sys_tags`，禁止冻结 production Python minor/ABI/manylinux floor 枚举，也禁止 sdist、fallback 或数组顺序选择。
- PEP 691 只要求 v1.0 `meta/name/files` 与 file `filename/url/hashes`；`requires-python` 可选，unknown meta/project/file extension keys 忽略。本 resolver 不要求 PEP 700 `versions`、size 或 v1.1 字段。
- `ucm-dependency-resolution` 绑定 source/config/catalog/selection hashes 与固定 `index_url`；request status 仅 `success/failure`，resolved record 重复 exact `{requirement_id,scope,name,version}` 并冻结从 raw PEP 691 filename/absolute HTTPS URL/`hashes.sha256`/`requires-python` 派生的 canonical evidence。target Python 必须满足 `requires-python`。success 对每个 requirement 恰有一个 compatible resolved 且 failures 为空；failure 不含 partial resolved 且 failures 非空并规范完整列出所有失败 requirement。拒绝 missing/duplicate/unexpected/scope-name-version drift；同一最高 wheel-tag rank 多解硬失败。
- failure exact set 是 resolver 对本次 PEP 691 reads 的行为结论；纯 validator 不重读 index，只验证 nonempty closed/canonical unique failures、stable code 与 in-request repeated identity，不伪造对被删/新增 in-request failure 的可检测性。
- `ucm_release plan candidates --selection --dependency-resolution` 是显式消费规范化 config 的纯 planner；校验 config 摘要、输入摘要和 request closure，把 resolved records 冻结进 Candidate Plan，绝不重新选择或联网。新增 `selection_sha256` 与 `dependency_resolution_sha256`。
- 当前 A1 discovered-only path 的 unresolved dependency 只形成逐 discovered selection 的 `dependency-unavailable` exclusion 且不建 task；A2 不生成 baseline blocker，全部 `baseline_required` 为 false。`baseline-dependency-unavailable` 和 baseline graph 由 Task 4B 在 Manifest validator 后扩充 request closure 再生成。Candidate Plan 不能消费 partial failed data。
- WHL/image/family坐标按 Spec 计算；Task 4 `binding_id` 只由 exact revision/runtime pair 计算。Wheel 按完整 build coordinate 唯一且可被多个 image 共享。Family publication coordinate 包含 `product_id` 与 `python_abi`、聚合 architectures；family task ID hash exact member IDs/channel targets/build instance，stable admission key 排除 architecture/runtime patch/UCM version。
- Candidate Plan 冻结 Python coordinate、动态 tasks/matrices、`admission_requirements[]` 和 graph-derived `baseline_required`。`candidate_role` 只在 requirements 中。
- `ucm_release plan select` 只做 expected plan hash + exact task ID lookup，不承担 selection 或 dependency resolution。
- 先提交 DependencyResolution/Candidate graph/CLI RED，覆盖 request exact set、selector ordering、future ABI、共享 wheel、resource limits 和 anti-ordering；不固定六产品、三个 ABI 或任务数量。

### Task 4B: Result closure 和 admission

- 先定义 Section 9 Manifest closed projection/public validator，再按 exact revision/runtime ID 从 Catalog 重开 baseline；missing source 形成 `baseline-source-unavailable` blocker，不替换 current。A1 不预测或读取 Manifest。
- 新增 CLI：`ucm_release plan admit`；Candidate build Result 使用统一闭包。wheel 的 capability/revision 非空且 `binding_id: null`，image 三者非空，Chart 三者为 null。
- `plan admit` 在 Results 前消费 Candidate Plan blockers：formal 任一 blocker 都产生 blocked、`releasable: false`；evaluation 输出 `would-block`。planning blocker 不被成功 Result 或 quarantine 抵消。
- 从 `admission_requirements[]` 和 Result exact set 决定：baseline success → admitted；baseline failure/missing → block；new success → promote；new failure → quarantine。共享 wheel 只要被 baseline 反向依赖即为 baseline required。
- 首次正式 v3 RC 使用 `bootstrap: all-passing`；PR/daily 只输出不可发布 evaluation，不能生成正式 Admitted Plan。
- 分别验证 missing/duplicate/unexpected Result、新项失败隔离、baseline 阻断、显式 supersession 和隔离项后续晋级。
- Task 4 到 Candidate/Evaluation/Admitted Plan Artifact 为止；不实现 Task 6 的 trusted rebuild、publication DAG、Registry/Release 写入或 Manifest finalize。

## Task 5: 改造 WHL 和镜像构建

- `_build-wheel.yml` 只接收任务 ID 和冻结 Candidate Plan。
- Builder 只打开 Task 4 冻结的 `/opt/python/{python_tag}-{python_abi}/bin/python`，并核对 expected SOABI/wheel tag；不得重新推导或 fallback。
- 删除 `wheel.py` 中固定 Python/Profile 常量及固定 `cuda130/cann900/cp312` 判断。
- Wheel 文件名、METADATA、RECORD、ELF 和依赖闭包全部从动态任务生成。
- 每个任务无论成功失败均上传规范化 Result Artifact。
- `_build-image.yml` 只下载 Release Plan 指定的精确 WHL，并验证 Distribution、Python ABI、加速运行时及 Mooncake 一致。
- `libascend_hal.so` 保留为 `external-required/transitive/device-runtime`，不得复制进 WHL 或镜像。
- 推送 RED Docker/Workflow 合约测试，确认 GitHub 能定位固定 Profile 和 `cp312`。
- 推送实现后，通过 `/ucm-build wheel` 和 `/ucm-build image` 在 GitHub 原生 amd64/arm64 Runner 上构建真实 Artifact。
- GitHub 结果至少包含 CUDA、CANN A2、CANN A3、多 Python ABI 和两个 CPU 架构。

## Task 6: 收口所有 Workflow、生产控制器和全并行发布

- `release-ucm.yml`、`ucm-build-bot.yml`、`production-tag-candidate.yml` 和生产 Controller 全部读取动态 Plan。
- 删除所有固定 `spec_id` 数组、固定 cache 路径、固定六项循环和固定 `cp312` 下载参数。
- 生产 trusted rebuild 保留双构建/字节对比，但任务集合来自 Admitted Release Plan。
- 生产控制包删除 `_PROFILES`、`EXPECTED_IMAGE_SPECS`、固定 Distribution 映射和固定 Release 资产列表。
- 发布严格使用 Release Plan 精确资产清单，不使用文件 glob 推断产品。
- GitHub Release 自动生成 Distribution、Python ABI、架构、运行时、Mooncake、镜像地址和 quarantine 信息。
- 发布拓扑使用动态嵌套矩阵：顶层 `publish-image-families` 按 family 展开；每个 reusable family workflow 内部按 architecture/channel 并行发布 member，随后只构建该 family index。
- PyPI 按精确 WHL 任务矩阵并行；Chart OCI 独立发布；GitHub Release Draft 创建后按精确资产矩阵并行上传。它们与 image family 发布互不串行依赖。
- 每个发布任务无论成功失败都上传规范化 Publication Result Artifact；quarantine 项不得进入发布矩阵。
- `finalize-release` 只消费 Admitted Plan 和全部 Publication Result Artifact，校验精确闭包后生成 Release Manifest、完成 GitHub Release 封板并执行公共 readback。
- 先推送 Workflow RED 合约测试，确认 GitHub 检查能发现固定矩阵残留、单 Job 串行发布循环和不必要的跨渠道 `needs`。
- 推送实现后，Hosted 检查证明活动 Workflow 不再包含固定产品矩阵或串行发布循环。
- PR Gate、Push Commit Checks 和 `/ucm-build all` 在同一 head SHA 上全部通过。

## Task 7: GitHub 全链路验收

- 不运行任何本地测试或构建。
- 在 GitHub PR 上依次完成 Push Commit Checks、Pull Request Gate、PR release dry-run、`/ucm-build wheel`、`/ucm-build image`、`/ucm-build chart`、`/ucm-build all`。
- 每个 run 记录 run ID、URL、head SHA、触发事件、Job 结果、Artifact 名称和数量、quarantine 列表。
- PR/feature lane 禁止 PyPI、正式 GHCR 和 GitHub Release 写入。
- 合并后通过 `workflow_dispatch` 运行 daily candidate，确认真实 Catalog 和动态矩阵。
- 第一个 Schema v3 RC Tag 在受保护 Environment 中执行正式验证。
- RC 完成全部 baseline 构建、新增失败隔离、PyPI readback、GHCR member/index readback、GitHub Release asset readback、Release Manifest readback 和 `release-loop-success`。
- 发布 Jobs 的时间线必须证明各动态矩阵并发执行；不得仅凭 YAML 结构声称并行。
- 无设备 Hosted 构建不能声称 CUDA/CANN 硬件验收；硬件 E2E 使用独立 Workflow 和真实设备 Runner。

## Task 8: 迁移完成条件

- 旧 Schema 在途 run 完成或取消后再合并 v3。
- 首个 v3 RC 使用 `bootstrap: all-passing`，其 Manifest 成为后续 baseline。
- 旧无版本 Distribution 不删除、不 yank、不继续发布。
- 文档改为版本化安装命令。
- GitHub 上同时具备成功的 PR、daily 和正式 RC 三条闭环证据。
- 之后新增 CUDA、CANN、Mooncake、Python 或非 310P Variant，无需修改 `release.yaml`；GitHub 流水线自动发现、构建、验证、晋级或隔离。
