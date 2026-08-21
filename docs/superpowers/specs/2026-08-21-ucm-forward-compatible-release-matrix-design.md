# UCM 向前兼容发布矩阵设计

- 状态：已批准实施架构
- 日期：2026-08-21
- 对应计划：`docs/superpowers/plans/2026-08-21-ucm-forward-compatible-release-matrix.md`
- 配置版本：Schema v3

## 1. 目标、边界与迁移原则

本设计把当前由 `wheel_profiles`、固定 Distribution、固定 Python ABI、固定
Mooncake 和固定六项矩阵驱动的发布链，改成一个由上游事实驱动的闭环：发现能力，
展开候选，构建并记录结果，按上一版正式 Manifest 准入，最后只发布准入闭包。
新增 CUDA、CANN、Variant、Mooncake、Python ABI 或 CPU 架构时，只要上游暴露了
可验证能力且现有模板能够表示，流水线不需要再修改 `release.yaml`。

本设计只规定新的 v3 能力、产品、准入、构建和发布架构。PR #26 至 #28 已合入的
growth-safe 行为继续成立：

- vLLM 和 vLLM-Ascend Builder 仍从上游源码直接发现，Builder 同步保持只新增；
- 只直接过滤 `310p`，未来其他 Variant 进入候选流程；
- 未声明的仓库 Dockerfile 不让整个 Catalog 失败，已声明配方仍严格校验；
- 不支持、已被更新版本替代或缺少匹配规则的上游目标形成结构化 exclusion，
  不升级为全局失败；
- 规则重叠、坐标重复、选中 Builder 缺失、上游证据损坏、扫描或矩阵超过硬上限
  仍然失败，且绝不静默截断；
- 正式发布的任务和资产闭包继续从计划派生，不使用 `6/6/3/7` 一类数量常量；
- Workflow 以行为契约验证，不恢复工作流字节指纹；源码、任务、制品和 OCI 摘要等
  功能性绑定继续保留。

以下不在范围内：重新设计生产信任边界、增加新的安全或审计系统、删除或 yank
历史包、为旧 Schema 增加兼容解析器、自动声明硬件或 Kubernetes 集群验收。

Schema v3 是一次切换，不双读、不降级、不保留 v2 适配层。旧 v2 在途 run 完成或
取消后才合入 v3。历史无版本 Distribution 和历史镜像保留为只读对象，但不再写入；
`uc-manager-cuda`、`uc-manager-cann-a2`、`uc-manager-cann-a3` 也不作为滚动别名继续发布。

## 2. 总体数据流与所有权

```mermaid
flowchart LR
    Config["release.yaml Schema v3"] --> Catalog["Capability Catalog"]
    Upstream["上游源码、镜像与 Builder"] --> Catalog
    Catalog --> Candidate["Candidate Plan"]
    Config --> Candidate
    Candidate --> Build["Build matrices"]
    Build --> BuildResults["Build Result Artifacts"]
    Previous["上一版 Release Manifest"] --> Admission["Admission"]
    Candidate --> Admission
    BuildResults --> Admission
    Admission --> Admitted["Admitted Release Plan"]
    Admitted --> Publication["并行 Publication DAG"]
    Publication --> PublicationResults["Publication Result Artifacts"]
    Admitted --> Finalize["Finalize barrier"]
    PublicationResults --> Finalize
    Finalize --> Manifest["Release Manifest"]
    Finalize --> Public["GitHub Release 与公共回读"]
```

权威所有权如下。CLI 和 Workflow 只编排这些模块，不复制规则。

| 所有者 | 权威职责 | 不拥有的知识 |
| --- | --- | --- |
| `.github/release/release.yaml` + `schemas/config.schema.json` | 静态产品规则、模板、版本约束、渠道开关、扫描与矩阵上限、首次 v3 bootstrap 策略 | 已发现版本、ABI、架构和构建结果 |
| `ucm_release/capabilities.py` | 上游读取、规范化、去重、exclusion、Capability Catalog | Distribution 命名和发布准入 |
| `ucm_release/products.py` | 模板编译、产品展开、精确任务坐标、Candidate Plan、依赖解析冻结 | 构建是否成功 |
| `ucm_release/results.py` | Build/Publication Result 的统一写入、校验与闭包收集 | 产品选择和 baseline 决策 |
| `ucm_release/admission.py` | baseline 读取、状态机、依赖传播、Admitted Release Plan | 构建与发布动作 |
| `ucm_release/publication.py` | 发布矩阵、渠道坐标、Release Manifest、最终闭包和 readback 判定 | 上游能力发现 |
| Workflow | 触发、权限、矩阵 fan-out、Artifact 搬运和失败传播 | 业务映射、固定产品列表、文件 glob 推断 |

`capabilities.py` 和 `products.py` 是新的核心深模块。其余支持模块可以在实现时合并，
但以上知识只能有一个权威所有者。例如 Workflow 不得重新实现 Distribution 模板，
生产 Controller 不得保留 `_PROFILES` 或 `EXPECTED_IMAGE_SPECS`。

所有 v3 JSON 对象采用封闭 Schema：顶层包含 `kind`、`schema_version: 3`，禁止未知
字段；数组按规范坐标排序；重复键、重复坐标、非规范字符串或摘要不匹配均失败。
每个持久对象包含其规范 JSON 的 `sha256`，后继对象记录所消费对象的摘要。

## 3. Schema v3 配置权威

### 3.1 单文件配置形态

`release.yaml` 只保存策略和模板，不保存发现结果。至少包含以下概念：

```yaml
kind: release-config
schema_version: 3

source:
  default_branch: develop
  protected_environment: release-production

discovery:
  exclude_variants: [310p]
  python_requires: ">=3.10"
  bootstrap: all-passing
  scan_limits: {}
  matrix_limits: {}

products:
  cuda:
    accelerator: cuda
    distribution: "uc-manager-cuda{runtime.compact}"
  cann:
    accelerator: ascend
    distribution: "uc-manager-cann{runtime.compact}-{variant}-mc{mooncake.compact}"

dependencies:
  build:
    packaging: "24.2"
    pyyaml: "6.0.2"
  runtime: {}

publish:
  pypi: {enabled: false, index: "https://upload.pypi.org/legacy/"}
  ghcr: {enabled: true, namespace: "ghcr.io/{owner}"}
  dockerhub: {enabled: false, namespace: "docker.io/{owner}"}
  chart_oci: {enabled: true, namespace: "ghcr.io/{owner}/charts"}
  github_release: {enabled: true}
```

示例只表达字段职责，最终配置还保留现有上游版本范围、渠道、运行时 patch、Chart、
原生依赖和 runner 映射。以下 v2 字段在 v3 中非法：`wheel_profiles`、PyPI `dists`、
Profile Builder 注入、固定 `python_abi`、固定 `dist_name` 以及固定产品资产列表。
配置加载器看到 `schema_version` 不是整数 `3` 或出现这些残留字段时直接失败，不能把
它们忽略为未知扩展。

依赖配置只保存版本或版本约束。Candidate Plan 的依赖解析阶段必须针对每个
`python_abi + cpu_architecture` 在 GitHub Hosted Runner 上解析二进制 wheel，冻结
Distribution、版本、文件名、来源 URL 和 SHA256；构建 Job 只消费冻结结果，不重新
选择依赖，也不从宽泛 glob 猜测文件。

### 3.2 模板编译与规范化

模板在读取配置时编译一次。允许变量按字段建立 allowlist；Distribution 模板只允许：

- CUDA：`{runtime.compact}`；
- CANN：`{runtime.compact}`、`{variant}`、`{mooncake.compact}`。

未知变量、未闭合占位符、属性链越界、空展开、非 PEP 503 Distribution 名称、模板
缺失必需变量，以及不同能力展开到同一 Distribution 都是配置错误。模板只在 Python
中以结构化上下文展开，不交给 shell、`envsubst` 或 GitHub expression 二次解析。

规范化函数是配置和所有计划的单一权威：

```text
compact_accelerator_runtime("cuda-13.0")       = "130"
compact_accelerator_runtime("cann-9.1.0")      = "910"
compact_mooncake_version("0.3.11.post1")       = "0311post1"
```

因此典型 Distribution 为：

```text
uc-manager-cuda130
uc-manager-cann910-a2-mc0311post1
uc-manager-cann910-a3-mc0311post1
```

compact 结果必须是小写字母数字，版本必须先通过对应版本解析器；不得通过简单删除任意
标点接受非法版本。Variant 规范化为小写 OCI/PEP 503 安全 token，原始值同时保留在
Catalog 中用于来源追踪。

## 4. Capability Catalog

### 4.1 发现来源

`ucm_release catalog discover` 生成唯一的 `ucm-capability-catalog` Artifact：

- vLLM Builder：读取 Buildkite release pipeline 中每个 `BUILD_BASE_IMAGE`，由任务和
  image 同时确定 CUDA runtime 与 CPU architecture；不使用手写 CUDA 列表；
- vLLM-Ascend Builder：扫描全部 `Dockerfile.buildwheel.*`，文件名产生 Variant，
  只跳过精确 Variant `310p`；`a2`、`a3` 和任何未来非 `310p` Variant 都进入发现；
- Python：在每个真实 Builder 镜像的对应原生 GitHub Runner 中列举全部
  `/opt/python/cp*-cp*/bin/python`，运行解释器读取 version/ABI，并用项目
  `requires-python >=3.10` 过滤；目录名、解释器报告和 wheel tag 必须一致；
- Runtime：扫描 vLLM 和 vLLM-Ascend runtime image，并读取与镜像版本对应的 Git Tag
  源码。registry tag、镜像 digest 和源码 Tag commit 都进入来源记录；
- Mooncake：从匹配的 vLLM-Ascend runtime Dockerfile 读取 `MOONCAKE_TAG`，然后在
  每个 runtime architecture 的 Hosted Runner 中检查镜像实际安装版本。声明和实装
  不一致只排除该能力；无法唯一读取来源则是来源证据错误；
- Ascend Builder：从匹配 runtime image 复制 Mooncake headers 和 libraries，验证
  版本后形成 Builder capability；不再 clone 固定 `0.3.9`。

发现阶段先收集完整事实，再应用唯一的产品过滤 `variant == 310p`。不能把当前配置的
版本范围、当前 A2/A3 数量或当前 Python 版本提前变成扫描过滤器；这些属于候选展开。

### 4.2 Catalog 条目契约

每个 `entries[]` 至少包含：

```json
{
  "capability_id": "capability-<sha256>",
  "accelerator": "cuda|ascend",
  "accelerator_runtime": "cuda-13.0|cann-9.1.0",
  "variant": "default|a2|a3|<future>",
  "cpu_architecture": "amd64|arm64|<future>",
  "manylinux": "manylinux_2_28",
  "python_version": "3.12",
  "python_abi": "cp312",
  "source_image": "registry/repository@sha256:<digest>",
  "target_image": "ghcr.io/<owner>/<builder>:<immutable-tag>",
  "mooncake_version": null,
  "sources": {
    "project": "vllm-project/vllm",
    "git_tag": "vX.Y.Z",
    "git_commit": "<40-hex>",
    "runtime_image": "repository@sha256:<digest>",
    "builder_declaration": "path#entry"
  }
}
```

Ascend 条目的 `mooncake_version` 必须为验证过的 PEP 440 版本；CUDA 必须为 `null`。
`source_image` 和 runtime image 使用 digest，`target_image` 是本次 Builder 同步得到的
不可变坐标。Catalog 的唯一键为：

```text
accelerator + accelerator_runtime + variant + python_abi + cpu_architecture
```

`manylinux`、Python version、Mooncake 或来源在相同键下冲突时不能任选其一，发现失败。
`exclusions[]` 包含来源坐标、规范 reason code 和证据，不伪造可构建条目。`310p` 使用
`variant-filtered-310p`；声明/实装 Mooncake 不同使用
`mooncake-version-mismatch`。

Catalog 还记录 `source_sha`、上游读取集合、Builder sync 结果、发现时间和
`catalog_sha256`。时间不参与能力 ID，重跑相同来源应得到相同条目和摘要。

## 5. Candidate Plan 与精确任务坐标

`ucm_release plan candidates` 只消费 Schema v3 配置和一个已验证 Catalog。它先按现有
growth-safe 规则选择每个产品/Variant 的最新可支持 runtime，再让 `products.py`
展开产品。某个目标不支持形成 exclusion，并继续检查同组下一版本；规则重叠、模板
冲突或任务坐标重复仍然全局失败。

### 5.1 坐标

坐标是结构化字段的规范 JSON，不以数组下标、当前 Profile 名或文件名代替：

```text
Wheel = distribution + ucm_version + python_abi + cpu_architecture

Runtime Image = accelerator + accelerator_runtime + variant
              + mooncake_version + python_abi + cpu_architecture

Image Family = accelerator + accelerator_runtime + variant
             + mooncake_version + runtime_version
```

CUDA Runtime Image 的 `mooncake_version` 使用显式 `null`，不能省略。`task_id` 是上述
坐标规范 JSON 的 `wheel-<sha256>`、`image-<sha256>` 或 `family-<sha256>`；Actions UI
另用动态 label 显示 Distribution、runtime、Variant、ABI 和 architecture。Artifact
名称使用 `task_id + run_id + run_attempt`，不会因产品数量增长冲突。

每个任务还携带不含本次 UCM version 的稳定 `admission_key`。Wheel 的 admission key
为 `distribution + python_abi + cpu_architecture`；image 的 admission key 就是上面的
Runtime Image 绑定键；family 的 admission key 不含上游 runtime patch version。这样
正常的 UCM 版本递增仍能匹配上一版正式能力，而新的 accelerator runtime、Mooncake、
Variant、ABI 或 architecture 会被识别成新能力。`admission_key` 只用于 baseline 状态机，
不能代替带 UCM version 的精确构建和发布坐标。

Wheel 坐标要求完全唯一。一个 wheel 可以供多个 runtime image 使用，但 image 必须
通过其 `wheel_task_id` 精确绑定，不能按 `*.whl` 搜索。Runtime Image 键包含
Mooncake、ABI 和 architecture，防止 CANN runtime 与错误 Mooncake 或 Python wheel
拼接。Family 明确列出其 member task IDs 和 publication channels，不假设只有
`amd64/arm64`。

### 5.2 Candidate Plan 契约

`ucm-candidate-plan` 至少包含：

- `route`、`source_sha`、`ucm_version`、`release_tag`、`config_sha256`、
  `catalog_sha256`；
- `capabilities[]`：实际被产品展开消费的 capability IDs；
- `wheel_tasks[]`：精确坐标、动态 Distribution、Builder digest、manylinux、Python、
  native/ELF 规则、冻结依赖、预期 wheel 文件名和 Artifact 名；
- `image_tasks[]`：精确坐标、`wheel_task_id`、runtime digest、Mooncake、目标 repository
  与 member tag、预期 OCI 输出和 Artifact 名；
- `family_tasks[]`：动态 family 坐标、member task IDs、每个启用 channel 的目标 index；
- `chart_task`：唯一的 Chart 输入、版本、文件名和目标 OCI 坐标；
- `expected_build_results[]`、`exclusions[]`、动态 Actions matrices 和资源计数；
- `candidate_plan_sha256`。

计划中的 wheel 文件名由 Distribution、UCM version、ABI 和 architecture 冻结；
依赖 wheel 也逐项冻结文件名和摘要。`plan select` 只能按 `task_id` 返回恰好一个任务，
且校验整个计划摘要；`_build-wheel.yml` 和 `_build-image.yml` 只接收任务 ID 和冻结计划。

## 6. Build Result Artifacts

每个 wheel、image 和 Chart 任务无论成功或失败都尝试上传一个规范化
`ucm-build-result` JSON。构建步骤把退出状态交给结果写入步骤；结果上传使用
`if: always()`，上传后 Job 再按结果失败。Runner 丢失、取消或 Artifact 缺失无法伪造
结果，收集器把“缺少期望 Result”当作该任务失败。

统一契约包含：

```text
kind, schema_version, result_type, status
route, source_sha, run_id, run_attempt
candidate_plan_sha256, task_id, task_coordinate
started_at, completed_at, outputs, failure, result_sha256
```

`status` 只有 `success` 或 `failure`。成功时 `failure` 为 `null`，`outputs` 必须闭合；
失败时 `outputs` 只保留已经验证的诊断输出，`failure` 包含稳定 `code`、阶段和简短摘要，
不得把 token 或完整环境写入 Result。

- wheel 输出：唯一文件名、Distribution、UCM version、Python ABI、architecture、
  wheel tag、SHA256、METADATA/RECORD/ELF/依赖闭包结论；
- image 输出：OCI archive/manifest/config/layer 摘要、runtime digest、绑定 wheel 摘要、
  accelerator runtime、Variant、Mooncake、ABI 和 architecture；
- Chart 输出：唯一 tgz 文件名、Chart/app version、SHA256 和全部动态 family 渲染结论。

`libascend_hal.so` 始终记录为
`external-required / transitive / device-runtime`，不能进入 wheel 或 image 文件闭包。
Image 构建在安装前核对精确 wheel 文件名、Distribution、ABI、accelerator runtime 与
Mooncake；任何一项不同都写失败 Result，不尝试“最接近”匹配。

Builder 内 Python 选择顺序固定为：

```text
/opt/python/{python_abi}-{python_abi}/bin/python
python{python_version}
python3
```

后两个只是在解释器存在时的兼容查找，最终报告的 version、SOABI 和 ABI 必须与任务
一致，否则失败。不能恢复 `cp312` 特判或 Profile 常量。

## 7. Baseline 与准入状态机

`ucm_release plan admit` 消费 Candidate Plan、所有期望 Build Result，以及最近一个公开
且成功回读的 Schema v3 Release Manifest。baseline 只能来自配置所指 GitHub Release
渠道中的 Manifest asset；按正式版本顺序选择，验证其 Schema、Repository、渠道、
Manifest 摘要和公开 readback 状态。Actions Artifact、Draft 或本地文件不能充当正式
baseline。

状态机逐项保留任务精确坐标，并用任务的稳定 `admission_key` 与 baseline 建立关系：

| baseline 关系 | 本次构建 | 决策 | 发布影响 |
| --- | --- | --- | --- |
| baseline 已正式发布 | success | `admitted` | 保留正式产品 |
| baseline 已正式发布 | failure 或 Result 缺失 | `blocked-baseline-failure` | 整个发布在任何写入前失败 |
| baseline 中不存在 | success | `promoted` | 本次进入 admitted 发布闭包 |
| baseline 中不存在 | failure 或 Result 缺失 | `quarantined` | 仅隔离新项，其他项继续 |

表中的 baseline 关系通过稳定 `admission_key` 建立。上一版 Manifest 中的 admitted key
在本次 Candidate Plan 完全消失时，产生 `baseline-capability-missing` blocker；不能把
消失解释成自动退役。依赖失败沿依赖边传播。例如新 wheel 失败时，所有绑定它的新 image/member/family 都
quarantine；baseline image 依赖的新生成 wheel 失败时属于 baseline failure，不能降级
为 quarantine。Chart 和计划/结果收集器没有“新能力”语义，任一失败始终阻断发布。
quarantine 项保留坐标、失败 Result 和 reason，绝不进入任何发布矩阵。

首个 v3 RC 没有 v3 baseline，必须显式使用 `bootstrap: all-passing`：Candidate Plan
中的所有期望构建任务均成功才产生可发布的 Admitted Plan；任一失败都阻断首次 v3
发布，不能用 quarantine 缩小首版 baseline。该 RC 成功后的 Release Manifest 成为
后续唯一 baseline。非 RC 正式运行不能自行启用 bootstrap。

### 7.1 Admitted Release Plan

`ucm-admitted-release-plan` 包含 Candidate Plan 身份、baseline Manifest 身份、每个任务
的 `admitted/promoted/quarantined/blocked` 决策、准入 Build Result 摘要，以及：

- 精确 `wheel_publication_tasks[]`；
- 精确 `image_family_publication_tasks[]`，每个 family 内含 member/channel matrix；
- 唯一 `chart_publication_task`；
- 精确 `github_asset_tasks[]`；
- `expected_publication_results[]`；
- `releasable`、`blockers[]`、`quarantine[]` 和 `admitted_plan_sha256`。

正式入口只有 `releasable: true` 才能取得发布 Environment。任何 blocker、Schema 错误、
Result 重复、未计划 Result 或精确闭包不等都在创建 Draft 或登录 Registry 前失败。

## 8. 全并行 Publication DAG

正式发布不能把产品或渠道放在单个 shell `for/while` 中串行处理，也不能声明
`publish-cuda`、`publish-a2`、`publish-a3` 等固定 Job。唯一拓扑如下：

```mermaid
flowchart TD
    Admit["Admitted Release Plan"] --> Families["publish-image-families matrix"]
    Admit --> PyPI["publish-pypi-wheels matrix"]
    Admit --> Chart["publish-chart"]
    Admit --> Draft["create-github-draft"]
    Draft --> Assets["publish-github-assets matrix"]

    subgraph family["每个 reusable image-family workflow"]
        Members["architecture x channel member matrix"] --> Index["该 family 的 channel indexes"]
    end

    Families -. "每个 matrix 项调用" .-> Members
    Families --> Final["finalize-release"]
    PyPI --> Final
    Chart --> Final
    Assets --> Final
    Draft --> Final
    Final --> Manifest["Manifest + publish Release + readback"]
```

`publish-image-families` 使用 Admitted Plan 产生的顶层动态 family matrix，并调用一个
reusable `_publish-image-family.yml`。reusable workflow 内部再以该 family 的
`architecture × enabled channel` member matrix 并行发布；其 index Job 只 `needs`
该 family 的 member matrix，并对计划中实际 member 坐标组成 index。它不等待其他
family、PyPI、Chart 或 GitHub assets，也不假设 architecture 是固定两项。

其他分支互不串行依赖：

- `publish-pypi-wheels` 对 Admitted Plan 中每个精确 wheel task 建 matrix；
- `publish-chart` 是独立单 Job；
- `create-github-draft` 只创建或复用精确 Tag 的 Draft；
- `publish-github-assets` 只依赖 Draft 和 Admitted Plan，对精确资产清单建 matrix；
  它不等待 image、PyPI 或 Chart 渠道发布完成。

每个 member、index、PyPI wheel、Chart OCI、Draft 和 GitHub asset 单元都上传一个
`ucm-publication-result`，字段与 Build Result 的共同 envelope 一致，另包含
`channel`、远端规范坐标、发布后 digest/version/asset ID 和渠道 readback 摘要。
即使命令失败，Result 仍记录 `failure`；Result 缺失同样是失败。

`finalize-release` 是唯一允许 `needs` 全部发布分支的 barrier。它从 Admitted Plan 的
`expected_publication_results` 计算精确集合，拒绝缺失、重复、失败、quarantine 或
未计划 Result。通过后按顺序：

1. 生成 Release Manifest 并上传到现有 Draft；
2. 校验 Draft 的精确 asset 集合后把 GitHub Release 从 Draft 封板为目标 prerelease
   或 stable 状态；
3. 对 PyPI、每个 OCI member/index、Chart OCI、GitHub Release assets 和 Manifest
   执行公共 readback；
4. 所有 readback 闭合后才输出 `release-loop-success`。

发布中途失败可能留下 Draft 或远端渠道的部分对象；实现不做推测性回滚、删除或 yank。
相同 Tag 重跑必须按 Admitted Plan 的精确坐标幂等核对已有对象。只要最终 barrier 未
完成，就没有新的有效 Release Manifest，也不能声称该 run 完成正式发布。

## 9. Release Manifest

`ucm-release-manifest` 是一次正式发布的持久 baseline，而不是运行日志。它至少包含：

- Repository、Tag、UCM version、stage、source SHA、run ID/attempt；
- config、Catalog、Candidate Plan、Admitted Plan 的摘要；
- bootstrap 或 previous Manifest 的来源；
- 每个 admitted/promoted wheel 的精确坐标、文件名和摘要；
- 每个 runtime member/family 的精确坐标、channel、digest 和绑定 wheel；
- Chart 坐标和摘要；
- quarantine 项及其失败 Result 摘要；
- 全部成功 Publication Result 的精确集合；
- 预期公共渠道坐标和 `manifest_sha256`。

Manifest 不把 Hosted 构建写成硬件或集群通过，也不把 Draft 存在写成发布成功。
Manifest 只在所有 publication Result 成功后生成；它自身与 GitHub Release 一起经过
公共 readback。后续 admission 只读取完成这条闭环的 Manifest。

## 10. 触发、路由与写边界

所有入口调用同一套 `catalog discover -> plan candidates -> build -> plan admit`，只由
route 决定选择范围、保留时长和写权限。

| route | 触发与 source | 允许写入 | 禁止与完成条件 |
| --- | --- | --- | --- |
| PR | `pull_request` 或通过权限检查的 `/ucm-build`，绑定 PR head SHA | Actions Artifact；受信同仓库 `/ucm-build` 可更新共享 Builder pool 和显式 PR 临时 GHCR tag | 不写 PyPI、正式 GHCR、Chart OCI 或 GitHub Release；完成只表示 Hosted candidate evidence |
| daily | `schedule` 或默认分支 `workflow_dispatch`，必须是 `develop` 的不可变 SHA | Actions Artifact；共享 Builder pool；配置明确启用的 staging/candidate 临时对象 | 不创建正式 Tag/Release，不写 PyPI 或正式产品坐标；完成表示 daily candidate loop |
| RC/formal | 受保护的 `refs/tags/v*`，Tag/source/默认分支可信控制身份继续沿用现有生产边界 | 准入前只读；`releasable: true` 后由 `release-production` Environment 授予 PyPI、正式 Registry、Chart 和 GitHub Release 写权限 | baseline failure 或首次 bootstrap 非全绿时写入前终止；完成必须有 Manifest 和全部公共 readback |

普通 PR Gate 不依赖 fork secret。GitHub 对 reusable workflow 的静态最大权限要求，不得
被解释成 PR Job 实际拥有发布权；实现必须用事件、同仓库身份、授权评论者和 job-level
条件把 Builder/临时 PR 写入限制在受信入口。PR 临时镜像使用 PR/run 坐标并由现有清理
流程处理，不等于正式 GHCR 发布。

RC 仍保留现有“候选 Tag 只读、默认分支可信 Controller、受保护 Environment 发布”
责任边界，但其固定 `spec_id`、固定 cache key、固定 Distribution、固定资产列表和
生产 `_PROFILES` 全部由动态 Admitted Plan 替代。trusted rebuild 的双构建和 wheel
逐字节比较继续保留，任务集合来自 Admitted Plan。

## 11. 失败语义

失败按拥有层处理，不增加跨层 fallback：

- 配置/模板错误、重复坐标、规则歧义、Catalog 来源损坏、资源上限、baseline 身份错误
  是全局失败；
- 可明确归因到单一新 capability 的不支持、Mooncake 不一致或构建失败形成 quarantine；
- 任何 baseline 任务失败、Result 缺失或依赖闭包破坏都阻断正式发布；
- publication 单元失败阻断 final barrier，但不把已写远端对象称为 Release，也不自动
  删除历史或部分对象；
- final 精确集合存在 missing、duplicate、unexpected 或 digest mismatch 时失败；
- Registry/GitHub Release readback、真实硬件、集群接受是不同证据域，不能相互代替。

任务失败不能通过 `fail-fast` 隐藏同矩阵其他结果；构建和发布矩阵使用
`fail-fast: false`，让所有独立单元产生 Result。只有本 family index 对本 family
member 存在必要依赖，finalize 对全部 publication Result 存在必要依赖；其他
`needs` 必须能由数据依赖解释。

## 12. 证据边界

证据结论必须使用以下词义：

| 证据 | 能证明 | 不能证明 |
| --- | --- | --- |
| Source check | Schema、Controller、Workflow 和测试契约在源码中存在 | GitHub 实际执行或远端产物存在 |
| Hosted build | 指定 head SHA 的 GitHub Runner 构建、校验和 Artifact | Registry/Release 已发布、GPU/NPU 可运行 |
| Registry/Release readback | 精确公共包、digest、asset 和 Manifest 可回读 | 真实设备推理或集群验收 |
| Hardware E2E | 指定 GPU/NPU Runner 上的设备行为 | Kubernetes 部署验收 |
| Cluster acceptance | 指定集群、存储和工作负载的端到端结果 | 其他硬件或公共渠道状态 |

无设备 GitHub Hosted Runner 只能产出 Hosted build evidence。`hardware-e2e.yml` 使用真实
设备 Runner，集群验收使用独立入口；二者不进入本设计的 release publication barrier，
也不能被空跑或静态 YAML 检查替代。

## 13. Hosted-only RED/GREEN 验证

本迁移禁止在本地运行 pytest、actionlint、Docker、wheel/image build 或发布模拟。
本地只允许源码检查和 `git diff --check`；所有行为性 RED/GREEN 在 GitHub Hosted
Actions 上完成。

每个实现任务按两次不可变提交验证：

1. RED 提交只增加预期契约/fixture，Hosted Job 必须因尚未实现的特定行为失败；
2. GREEN 提交增加最小实现，同一个检查和同一测试选择在新 head SHA 上通过；
3. 记录两次 run，而不是用最终绿灯覆盖 RED 证据；
4. 真实 Builder/Catalog、wheel、image、Chart 和 publication 验证必须检查 Actions
   Artifact 或远端 readback，fixture 只证明控制逻辑。

每个 run 在 `docs` 下维护结构化 evidence record，至少包含：

```yaml
phase: schema-v3|catalog|candidate-admission|build|publication|acceptance
expectation: red|green|real-build|formal-readback
run_id: 123456789
run_url: https://github.com/<owner>/<repo>/actions/runs/123456789
head_sha: <40-hex>
event: pull_request|push|workflow_dispatch|schedule|issue_comment
workflow: <name and path>
jobs:
  - name: <job>
    conclusion: success|failure|cancelled|skipped
    expected_failure: <test and reason or null>
artifacts:
  - name: <exact artifact name>
    count: 1
    sha256: <downloaded payload digest when applicable>
quarantine: []
publication:
  attempted: false
  channels: []
```

基线 Hosted 检查必须在 `push-check.yml` 和 `pull-request.yml` 执行主 release tests、
production tests、actionlint 和 `git diff --check`，并汇总 source SHA、测试数量与失败
阶段。最终验收要求 PR Gate、Push Commit Checks 和 `/ucm-build all` 位于同一 head SHA；
daily 与首个 v3 RC 另有各自 run evidence。只有 RC 的 Publication Result、Release
Manifest 和公共 readback 全部闭合，才能记录 `release-loop-success`。

## 14. 实施完成条件

实现完成必须同时满足：

- active `release.yaml` 仅接受 Schema v3，且不存在 `wheel_profiles` 或固定 Distribution
  列表；
- Capability Catalog 对 CUDA/CANN、未来非 310P Variant、多 ABI 和 architecture 动态
  增长，Mooncake 来源与实装一致；
- Candidate 与 Admitted Plan 使用本文精确坐标和 baseline 状态机；
- wheel/image/production 控制代码没有固定 `cp312`、固定 Profile、固定六项 cache 或资产；
- 发布 Workflow 使用动态 family/reusable member 两级矩阵，PyPI、Chart、GitHub assets
  并行，只有 finalize 是全局 barrier；
- PR、daily、RC 三条 Hosted loop 分别有不可变 run evidence；
- 首个 v3 RC 以 `bootstrap: all-passing` 成功并公开回读 Manifest；
- 后续新增上游能力无需修改 `release.yaml`，可自动晋级或 quarantine；
- Hosted build、公共发布、硬件和集群结论在报告中继续分开。
