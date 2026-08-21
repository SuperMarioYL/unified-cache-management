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
    Previous["上一版 Release Manifest"] --> Candidate
    Candidate --> Build["Build matrices"]
    Build --> BuildResults["Build Result Artifacts"]
    Previous --> Admission["Admission"]
    Candidate --> Admission
    BuildResults --> Admission
    Admission --> Admitted["Admitted Release Plan"]
    Admitted --> Trusted["Trusted rebuild and byte compare"]
    Trusted --> TrustedResults["Trusted Build Result Artifacts"]
    Admitted --> PublishGate["Publication input gate"]
    TrustedResults --> PublishGate
    PublishGate --> Publication["并行 Publication DAG"]
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
| `ucm_release/results.py` | Build/Trusted Build/Publication Result 的统一写入、校验与闭包收集 | 产品选择和 baseline 决策 |
| `ucm_release/admission.py` | baseline 读取、状态机、依赖传播、Admitted Release Plan | 构建与发布动作 |
| `ucm_release/publication.py` | 发布矩阵、渠道坐标、Release Manifest、最终闭包和 readback 判定 | 上游能力发现 |
| Workflow | 触发、权限、矩阵 fan-out、Artifact 搬运和失败传播 | 业务映射、固定产品列表、文件 glob 推断 |

`capabilities.py` 和 `products.py` 是新的核心深模块。其余支持模块可以在实现时合并，
但以上知识只能有一个权威所有者。例如 Workflow 不得重新实现 Distribution 模板，
生产 Controller 不得保留 `_PROFILES` 或 `EXPECTED_IMAGE_SPECS`。

所有 v3 JSON 对象采用封闭 Schema：顶层包含 `kind`、`schema_version: 3`，禁止未知
字段；数组按规范坐标排序；重复键、重复坐标、非规范字符串或摘要不匹配均失败。
每个持久对象包含其规范 JSON 的 `sha256`，后继对象记录所消费对象的摘要。自摘要使用
明确的 canonical projection：复制完整对象，排除且只排除对象自身的摘要字段（例如
`catalog_sha256` 或 `result_sha256`），保留所有嵌套对象摘要，按键排序并用 UTF-8、无
额外空白的规范 JSON 编码后计算 SHA256。作为集合的数据数组必须先按其规范坐标排序，
具有显式顺序语义的数组保留原顺序。摘要字段本身不参与自身计算，避免递归；校验器以
相同 projection 重算并拒绝缺失或不匹配。

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

Catalog 不把 Builder 和 runtime 塞进一个会覆盖多版本的唯一键。它包含三个规范数组：

- `builder_capabilities[]`：某个 Builder 可以构建什么；
- `runtime_candidates[]`：每一个发现到的 runtime Tag/digest 是什么；
- `bindings[]`：一个 Builder capability 与一个 runtime candidate 是否组成可构建产品。

Builder capability 至少包含 `builder_id`、`accelerator`、`accelerator_runtime`、
`variant`、`cpu_architecture`、`manylinux`、`python_version`、`python_abi`、
`source_image`、`target_image` 和 `mooncake_version`。Ascend 的 Mooncake 是已从匹配
runtime 复制并验证的版本；CUDA 为显式 `null`。Builder identity 为：

```text
accelerator + accelerator_runtime + variant + python_abi
+ cpu_architecture + manylinux + mooncake_version
```

同一 Builder identity 的 Python version、source image 或 target image 不一致时是来源
冲突。`source_image` 使用 digest，`target_image` 使用本次 Builder 同步得到的不可变
坐标；不能因某个 runtime 更新而覆盖另一个 Builder capability。

Runtime candidate 至少包含 `runtime_id`、`product_id`、`runtime_version`、`channel`、
`variant`、`cpu_architecture`、`accelerator`、`accelerator_runtime`、
`mooncake_version`、`runtime_image`、`git_tag` 和 `git_commit`。其 identity 为：

```text
product_id + runtime repository + runtime tag + variant + cpu_architecture
```

相同 identity 必须解析到唯一 image digest、Git commit、accelerator runtime 和
Mooncake；不一致是硬失败。不同 Tag/version 各自保留为独立 runtime candidate，不能
由“最新版本”覆盖或被 Builder identity 去重。因此 growth-safe 选择器可以从完整多版本
集合中依次评估最新候选、回退候选和 baseline 保留候选。

每个 `bindings[]` 条目以 `builder_id + runtime_id` 为唯一键，并投影出计划所需的完整
字段：`accelerator`、`accelerator_runtime`、`variant`、`cpu_architecture`、
`manylinux`、`python_version`、`python_abi`、`source_image`、`target_image`、
`mooncake_version`。绑定只在 architecture、accelerator runtime、Variant 和 Mooncake
兼容时产生；同一 runtime 可以绑定多个 Python ABI Builder，同一 Builder 也可以绑定
多个兼容 runtime candidate。

`exclusions[]` 指向 `builder_id`、`runtime_id` 或来源坐标，并包含规范 reason code 和
证据，不伪造 binding。`310p` 使用 `variant-filtered-310p`；声明/实装 Mooncake 不同
使用 `mooncake-version-mismatch`。

Catalog 还记录 `source_sha`、上游读取集合、Builder sync 结果和 `catalog_sha256`，不把
运行墙钟时间写进内容对象；时间只属于外层 run evidence。重跑相同来源应得到相同能力、
候选、绑定和摘要。

## 5. Candidate Plan 与精确任务坐标

`ucm_release plan candidates` 消费 Schema v3 配置、一个已验证 Catalog，以及可选的上一版
正式 v3 Manifest。它先按现有 growth-safe 规则从完整 `runtime_candidates[]` 为每个
产品/Variant 选择最新可支持 runtime，再让 `products.py` 展开产品。某个目标不支持形成
exclusion，并继续检查同组下一版本；规则重叠、模板冲突或任务坐标重复仍然全局失败。

选择“最新”只决定新的 discovered selection，不能删除 baseline。Candidate task set 是
以下集合的规范并集：

1. 上一版 Manifest 中状态为 `active` 的每个 admission key 对应的
   `baseline_carry_forward`；
2. 本次 discovery 选中的每个新候选；
3. 配置中尚未生效但显式请求评估的 retirement/supersession 目标。

baseline carry-forward 优先在 Catalog bindings 中按 Manifest 的 immutable runtime/builder
来源重建；若当前 registry 扫描已不列出旧 Tag，可使用 Manifest 冻结的 digest/commit
坐标和 append-only Builder capability 重建。无法精确重建时保留一个失败任务并产生
`baseline-source-unavailable` Result，不能直接从计划中消失。这样旧 baseline 和新候选
在同一 run 共存并分别产生 Build Result。

每个任务除 `admission_key` 外还有 `lineage_key`。Lineage 保留 accelerator runtime、
Variant、ABI 和 architecture，但不包含 UCM version、上游 runtime version 或 Mooncake
patch version。不同 accelerator runtime 是可并存的新产品线；同一 lineage 内更高的
runtime/Mooncake 候选可以由产品规则产生显式 `supersedes` 关系。默认是共存，不允许仅因
“不是最新”而隐式删除 baseline。`retired` 只来自 Schema v3 中包含旧 key、原因、替代项
和生效版本的显式 retirement 声明。

### 5.1 坐标

产品坐标和构建实例坐标都是结构化字段的规范 JSON，不以数组下标、当前 Profile 名或
文件名代替：

```text
Wheel publication coordinate
      = distribution + ucm_version + python_abi + cpu_architecture

Runtime Image binding key
      = accelerator + accelerator_runtime + variant
      + mooncake_version + python_abi + cpu_architecture

Image build instance
      = Runtime Image binding key + ucm_version + runtime_id

Image Family = accelerator + accelerator_runtime + variant
             + mooncake_version + runtime_version
```

CUDA Runtime Image 的 `mooncake_version` 使用显式 `null`，不能省略。计划要求的 Runtime
Image 绑定键保持上述六字段精确闭包；`runtime_id` 只用于区分同一绑定键下的 baseline
runtime revision 与新 upstream revision。`task_id` 是 build instance 规范 JSON 的
`wheel-<sha256>`、`image-<sha256>` 或 `family-<sha256>`；Actions UI 另用动态 label 显示
Distribution、runtime、Variant、ABI 和 architecture。Artifact 名称使用
`task_id + run_id + run_attempt`，不会因产品数量增长冲突。

每个任务还携带不含本次 UCM version 的稳定 `admission_key`。Wheel 的 admission key
为 `distribution + python_abi + cpu_architecture`；image 的 admission key 就是上面的
Runtime Image 绑定键；family 的 admission key 不含上游 runtime patch version。这样
正常的 UCM 版本递增仍能匹配上一版正式能力，而新的 accelerator runtime、Mooncake、
Variant、ABI 或 architecture 会被识别成新能力。`admission_key` 只用于 baseline 状态机，
不能代替带 UCM version 的精确构建和发布坐标。

同一个 admission key 在 Candidate Plan 中允许至多一个 `candidate_role: baseline` 和
一个由最新选择器产生的 `candidate_role: successor`；二者以不同 `runtime_id/task_id`
存在，并由显式 `supersedes` 边连接。若 selection 与 baseline 的 runtime identity 完全
相同，则去重为一个 `candidate_role: baseline-current` 实例。没有 role/edge 的重复
admission key 是计划歧义并失败。

Wheel 坐标要求完全唯一。一个 wheel 可以供多个 runtime image 使用，但 image 必须
通过其 `wheel_task_id` 精确绑定，不能按 `*.whl` 搜索。Runtime Image 键包含
Mooncake、ABI 和 architecture，防止 CANN runtime 与错误 Mooncake 或 Python wheel
拼接。Family 明确列出其 member task IDs 和 publication channels，不假设只有
`amd64/arm64`。

### 5.2 Candidate Plan 契约

`ucm-candidate-plan` 至少包含：

- `route`、`source_sha`、`ucm_version`、`release_tag`、`config_sha256`、
  `catalog_sha256`、可选 `baseline_manifest_sha256`；
- `capabilities[]`：实际被产品展开消费的 capability IDs；
- `wheel_tasks[]`：精确坐标、动态 Distribution、Builder digest、manylinux、Python、
  native/ELF 规则、冻结依赖、预期 wheel 文件名和 Artifact 名；
- `image_tasks[]`：精确坐标、`wheel_task_id`、runtime digest、Mooncake、目标 repository
  与 member tag、`runtime_id`、`candidate_role`、预期 OCI 输出和 Artifact 名；
- `family_tasks[]`：动态 family 坐标、member task IDs、每个启用 channel 的目标 index；
- `baseline_carry_forward[]`、`discovered_selections[]`、显式 `supersessions[]` 和
  `retirements[]`；
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

正式 `ucm_release plan admit` 消费 Candidate Plan、所有期望 Build Result，以及
Candidate Plan 已绑定的最近一个公开且成功回读的 Schema v3 Release Manifest（存在时）。
`baseline_manifest_sha256: null` 只允许首个 v3 RC 的 `bootstrap: all-passing`。baseline
只能来自配置所指 GitHub Release 渠道中的 Manifest asset；按正式版本顺序选择，验证其
Schema、Repository、渠道、Manifest 摘要和公共 readback 状态。Admission 必须重开同一
Manifest 并与 Candidate Plan 中的 `baseline_manifest_sha256` 相等，Actions Artifact、
Draft 或本地文件不能充当正式 baseline。

状态机逐项保留任务精确坐标，并用稳定 `admission_key` 加 `candidate_role` 与 baseline
active revision 建立关系；`successor` 即使复用同一 admission key 仍按新候选评估：

| 候选角色 | 本次构建 | 决策 | 发布影响 |
| --- | --- | --- | --- |
| baseline active revision | success | `admitted` | 保留正式产品 |
| baseline active revision | failure 或 Result 缺失 | `blocked-baseline-failure` | 整个发布在任何写入前失败 |
| successor 或全新 capability | success | `promoted` | 本次进入 admitted 发布闭包 |
| successor 或全新 capability | failure 或 Result 缺失 | `quarantined` | 仅隔离新项，其他项继续 |

表中的 baseline 关系通过稳定 `admission_key`、Manifest active runtime identity 和
`candidate_role` 建立。上一版 Manifest 中 `active` 的 key
必须出现在 `baseline_carry_forward[]` 并产生 Result；缺失产生
`baseline-capability-missing` blocker，不能把消失解释成自动退役。

同一 lineage 的 baseline 与 successor 按以下顺序决策：

1. 先独立评估 baseline；其 failure/missing 仍是 blocker，不能被新候选成功掩盖；
2. 再评估 successor；success 为 `promoted`，failure 为 `quarantined`；
3. 只有 baseline 和 successor 本次都 success，且 Candidate Plan 存在显式
   `supersedes: old_task_id -> new_task_id`，Admitted Plan 才把旧 revision 的 lifecycle
   标为 `superseded`、新 revision 标为 `active`；两者的 admission decision 仍分别是 `admitted`
   和 `promoted`。否则两者 lifecycle 都保持 `active` 共存；
4. `retired` 只接受配置中已绑定原因、生效版本和可选替代 key 的显式 retirement，写入
   Manifest 后从下一版 baseline active set 移除。没有声明的缺失永远不是 retirement。

被 `superseded` 或 `retired` 的历史远端包不删除、不 yank，也不进入本次 publication
matrix；状态和 successor/原因写入 Manifest。若新候选失败，它只 quarantine，旧 baseline
继续 active 并发布当前 UCM 版本，因此自动发现升级不会让旧产品线静默消失。

依赖失败沿依赖边传播。例如新 wheel 失败时，所有绑定它的新 image/member/family 都
quarantine；baseline image 依赖的新生成 wheel 失败时属于 baseline failure，不能降级
为 quarantine。Chart 和计划/结果收集器没有“新能力”语义，任一失败始终阻断发布。
quarantine 项保留坐标、失败 Result 和 reason，绝不进入任何发布矩阵。

首个 v3 RC 没有 v3 baseline，必须显式使用 `bootstrap: all-passing`：Candidate Plan
中的所有期望构建任务均成功才产生可发布的 Admitted Plan；任一失败都阻断首次 v3
发布，不能用 quarantine 缩小首版 baseline。该 RC 成功后的 Release Manifest 成为
后续唯一 baseline。非 RC 正式运行不能自行启用 bootstrap。

PR 和 daily 在首个 v3 RC 前没有正式 baseline 时，不生成
`ucm-admitted-release-plan`。它们调用 `plan admit --mode evaluation`，输出单独的
`ucm-admission-evaluation`：`formal: false`、`baseline_state: unavailable-pre-v3`、
`publishable: false`，并把任务标为 `observed-success` 或 `observed-failure`，同时给出
`bootstrap_all_passing_would_pass`。若已有正式 baseline，PR/daily 仍只输出
`would-admit`、`would-promote`、`would-quarantine`、`would-block` 的 evaluation，不得
输出可发布矩阵、取得 Environment 或把预测状态写成正式 admitted/promoted。只有 RC/
formal route 能生成 Admitted Release Plan。

### 7.1 Admitted Release Plan

`ucm-admitted-release-plan` 包含 Candidate Plan 身份、baseline Manifest 身份、每个任务
的 `decision`（`admitted/promoted/quarantined/blocked`）和 `lifecycle_state`
（`active/superseded/retired`，仅成功或显式退役项可用）、准入 Build Result 摘要，以及：

- 精确 `wheel_publication_tasks[]`；
- 精确 `image_family_publication_tasks[]`，每个 family 内含 member/channel matrix；
- 唯一 `chart_publication_task`；
- 精确 `github_asset_tasks[]`；
- 精确 `expected_trusted_build_results[]`；
- `expected_publication_results[]`；
- `releasable`、`blockers[]`、`quarantine[]` 和 `admitted_plan_sha256`。

正式入口只有 `releasable: true` 才启动只读 trusted rebuild；只有后续
`publication-input-gate.ready: true` 的 publication Jobs 才能取得发布 Environment。
任何 blocker、Schema 错误、Result 重复、未计划 Result、trusted compare 失败或精确闭包
不等都在创建 Draft 或登录 Registry 前失败。

### 7.2 Trusted rebuild 与 publication input gate

Trusted rebuild 位于 Admission 之后、任何 publication 之前。它只消费不可变 Admitted
Plan、Candidate Build Result 中的成功 wheel bytes，以及默认分支可信控制代码；不会
回写 Candidate/Admitted Plan，因此数据流无环：

```text
Candidate Plan -> candidate build Results -> Admitted Plan
Admitted Plan -> trusted rebuild/compare Results -> publication input gate
Admitted Plan + trusted closure -> publication matrices
```

`trusted-rebuild` 按 Admitted Plan 中 lifecycle 为 `active` 且 decision 为
`admitted/promoted` 的 wheel task 动态展开。每个任务用默认
分支可信配方独立构建 A、B 两份 wheel，逐字节校验 `A == B == candidate wheel`，并重新
核对 Distribution、version、ABI、architecture、METADATA、RECORD、ELF 与依赖闭包。
它只读且不取得发布 Environment。

每个任务无论比较成功或失败都上传 `ucm-trusted-build-result`，复用 Result envelope 并
增加 `admitted_plan_sha256`、`candidate_build_result_sha256`、`candidate_wheel_sha256`、
`rebuild_a_sha256`、`rebuild_b_sha256`、三个 byte-equality 判定和验证结论。失败、取消或
Result 缺失均不能成为 publication 输入。

`collect-trusted-build-results` 以 `if: always()` 运行，按 Admitted Plan 的
`expected_trusted_build_results[]` 拒绝 missing/duplicate/unexpected/failure，生成
`ucm-trusted-build-closure`。随后的 `publication-input-gate` 只校验 Admitted Plan 与该
closure 的摘要和精确任务集，输出 `ready: true|false`；不重新做准入，也不产生第二份
Admitted Plan。所有 publication 分支同时依赖此 gate，只有 `ready: true` 才能取得写
权限。Image member publisher 只使用 closure 中已比较的 wheel，并继续按 Candidate image
Result 的 OCI closure 验证装配结果。

## 8. 全并行 Publication DAG

正式发布不能把产品或渠道放在单个 shell `for/while` 中串行处理，也不能声明
`publish-cuda`、`publish-a2`、`publish-a3` 等固定 Job。唯一拓扑如下：

```mermaid
flowchart TD
    Admit["Admitted Release Plan"] --> Trusted["trusted-rebuild matrix"]
    Trusted --> Compare["collect trusted Results"]
    Admit --> Gate["publication-input-gate"]
    Compare --> Gate
    Gate --> Families["publish-image-families matrix"]
    Gate --> PyPI["publish-pypi-wheels matrix"]
    Gate --> Chart["publish-chart"]
    Gate --> Draft["create-github-draft"]
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

`publication-input-gate` 成功后，`publish-image-families` 使用 Admitted Plan 产生的
顶层动态 family matrix，并调用 reusable `_publish-image-family.yml`。reusable workflow
内部再以该 family 的
`architecture × enabled channel` member matrix 并行发布；其 index Job 只 `needs`
该 family 的 member matrix，声明 `if: always()`，并从 Result Artifacts 对计划中实际
member 坐标做精确闭包。全部 member success 时才创建 index；任一 member failure、
cancelled、skipped 或 Result 缺失时，index 不写 Registry，但仍上传状态为 failure、
code 为 `member-closure-incomplete` 的 index Publication Result，然后让 Job 失败。它不
等待其他 family、PyPI、Chart 或 GitHub assets，也不假设 architecture 是固定两项。

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
未计划 Result。GitHub job 必须声明 `if: always()`，不能因任一 `needs` 为 failure、
cancelled 或 skipped 而被默认跳过；它通过 Actions Artifact API/本 run artifact pattern
收集 Result，不依赖失败 Job 的 outputs。Draft 创建失败时，asset matrix 也以
`if: always()` 运行，为每个计划资产生成 `draft-unavailable` failure Result。通过精确
闭包后才按顺序：

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
- 每个 lifecycle 为 active 且 decision 为 admitted/promoted 的 wheel 精确坐标、文件名
  和摘要；
- 每个 runtime member/family 的精确坐标、channel、digest 和绑定 wheel；
- 每个 lineage 的 active、superseded、retired 状态及 successor/retirement reason；
- Chart 坐标和摘要；
- quarantine 项及其失败 Result 摘要；
- 全部成功 Trusted Build Result 和 trusted closure 摘要；
- 全部成功 Publication Result 的精确集合；
- 预期公共渠道坐标和 `manifest_sha256`。

Manifest 不把 Hosted 构建写成硬件或集群通过，也不把 Draft 存在写成发布成功。
Manifest 只在所有 publication Result 成功后生成；它自身与 GitHub Release 一起经过
公共 readback。后续 admission 只读取完成这条闭环的 Manifest。

## 10. 触发、路由与写边界

所有入口共享 `catalog discover -> plan candidates -> build`。PR/daily 随后执行
`plan admit --mode evaluation`；只有 RC/formal 执行正式 `plan admit`，再进入
`trusted rebuild -> publication input gate -> publication`。route 同时决定选择范围、
保留时长和写权限，evaluation 不能通过字段或 Workflow output 冒充正式 Admitted Plan。

| route | 触发与 source | 允许写入 | 禁止与完成条件 |
| --- | --- | --- | --- |
| PR | `pull_request` 或通过权限检查的 `/ucm-build`，绑定 PR head SHA | Actions Artifact；受信同仓库 `/ucm-build` 可更新共享 Builder pool 和显式 PR 临时 GHCR tag | 只生成 Admission Evaluation，不写 PyPI、正式 GHCR、Chart OCI 或 GitHub Release；完成只表示 Hosted candidate evidence |
| daily | `schedule` 或默认分支 `workflow_dispatch`，必须是 `develop` 的不可变 SHA | Actions Artifact；共享 Builder pool；配置明确启用的 staging/candidate 临时对象 | 只生成 Admission Evaluation，不创建正式 Tag/Release，不写 PyPI 或正式产品坐标；完成表示 daily candidate loop |
| RC/formal | 受保护的 `refs/tags/v*`，Tag/source/默认分支可信控制身份继续沿用现有生产边界 | 准入和 trusted rebuild 只读；`releasable: true` 且 publication input gate ready 后，由 `release-production` Environment 授予 PyPI、正式 Registry、Chart 和 GitHub Release 写权限 | baseline/trusted failure 或首次 bootstrap 非全绿时写入前终止；完成必须有 Manifest 和全部公共 readback |

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
- trusted rebuild/compare 的 Result 缺失、失败或字节不等在 publication input gate 阻断，
  不回退到 Candidate bytes 直接发布；
- publication 单元失败阻断 final barrier，但不把已写远端对象称为 Release，也不自动
  删除历史或部分对象；
- final 精确集合存在 missing、duplicate、unexpected 或 digest mismatch 时失败；
- Registry/GitHub Release readback、真实硬件、集群接受是不同证据域，不能相互代替。

任务失败不能通过 `fail-fast` 隐藏同矩阵其他结果；构建、trusted rebuild 和发布矩阵
使用 `fail-fast: false`，让所有独立单元产生 Result。所有 Result 上传步骤、trusted
collector、family index 和 finalize 使用 `if: always()`；其中 collector/index/finalize
在上传规范失败 Result 或 barrier 报告后再令 Job 失败。只有本 family index 对本
family member 存在必要依赖，finalize 对全部 publication 分支存在必要依赖；其他
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
- Capability Catalog 分离 Builder capability、Runtime candidate 和 binding，对 CUDA/CANN、
  未来非 310P Variant、多 runtime、多 ABI 和 architecture 动态增长，Mooncake 来源与实装一致；
- Candidate 与 Admitted Plan 使用本文精确坐标、baseline carry-forward、显式 lifecycle
  状态和 baseline 状态机；
- 正式 publication 只消费通过双 trusted rebuild 和逐字节比较的 trusted closure；
- wheel/image/production 控制代码没有固定 `cp312`、固定 Profile、固定六项 cache 或资产；
- 发布 Workflow 使用动态 family/reusable member 两级矩阵，PyPI、Chart、GitHub assets
  并行；family index 与 finalize 即使上游失败也产生闭包 Result，只有 finalize 是全局 barrier；
- PR、daily、RC 三条 Hosted loop 分别有不可变 run evidence；
- 首个 v3 RC 以 `bootstrap: all-passing` 成功并公开回读 Manifest；
- 后续新增上游能力无需修改 `release.yaml`，可自动晋级或 quarantine；
- Hosted build、公共发布、硬件和集群结论在报告中继续分开。
