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

- 新增 CLI：`ucm_release catalog discover`、`ucm_release plan candidates`、`ucm_release plan admit`、`ucm_release plan select`。
- WHL 唯一坐标为 `distribution + ucm_version + python_abi + cpu_architecture`。
- Runtime Image 绑定键为 `accelerator + accelerator_runtime + variant + mooncake_version + python_abi + cpu_architecture`。
- CUDA Distribution 模板为 `uc-manager-cuda{runtime.compact}`。
- CANN Distribution 模板为 `uc-manager-cann{runtime.compact}-{variant}-mc{mooncake.compact}`。
- 依赖配置只保存版本；GitHub 计划 Job 为每个 Python ABI/架构解析二进制 wheel 并冻结文件名和摘要。
- 从最近的 Schema v3 Release Manifest 读取 baseline；首次使用 `bootstrap: all-passing`。
- 准入状态机：baseline success → admitted；baseline failure → block release；new success → promote；new failure → quarantine。
- 先推送矩阵展开 RED 测试，在 GitHub 验证旧逻辑只能生成固定六项。
- 推送实现后，fixture 为 6 个工具链产品 × 3 个 Python ABI × 2 个架构生成 36 个唯一 WHL。
- GitHub 分别验证新项失败隔离、baseline 失败阻断、隔离项下次成功后晋级。

## Task 5: 改造 WHL 和镜像构建

- `_build-wheel.yml` 只接收任务 ID 和冻结 Candidate Plan。
- Builder 按任务依次选择 `/opt/python/{python_abi}-{python_abi}/bin/python`、`python{python_version}`、`python3`。
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
