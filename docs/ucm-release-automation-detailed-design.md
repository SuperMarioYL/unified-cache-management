# UCM release automation detailed design

## 1. Scope and invariants

The release system builds deterministic local evidence for UCM core artifacts,
the product Helm Chart, and UCM-installed vLLM image candidates. Repository
policy lives in `.github/release`; workflows only install fixed tools, move
artifacts, and call `python -m ucm_release`.

The checked-in release surface is fixed at:

- four release workflows;
- eight `ucm_release` Python modules;
- three JSON Schemas;
- four Docker files;
- two YAML configuration files;
- one product Chart at `charts/ucm`, including `SOURCE_PROVENANCE.json`.

The feature/fork lane is always fixture-only, read-only, and unpublished. The
production lane remains fail-closed while its external requirements are absent.

## 2. Repository layout

### 2.1 Four workflows

| Workflow | Trigger/interface | Responsibility |
| --- | --- | --- |
| `_build-wheel.yml` | Required `workflow_call` inputs `source_sha`, `profile_id`, `validation_lane` | First step validates the complete lowercase commit SHA, the one authorized fixture profile, and `fork-candidate`; then builds and uploads one deterministic fixture wheel and its canonical records |
| `_build-image.yml` | Required `workflow_call` inputs `source_sha`, `wheel_artifact`, `image_input_artifact`, `validation_lane` | First step validates all inputs; then authenticates Buildx, reads the fixed base descriptor chain, prepares the seven-file context, builds a local OCI archive, verifies it, removes the large archive, and uploads compact evidence |
| `release-vllm-images.yml` | Reusable call plus schedule, repository dispatch, and default-branch manual entry | Produces or accepts the fixture wheel, prepares one reconcile task, calls the image builder, records the verified image, and requires the second reconcile to return zero tasks |
| `release-ucm.yml` | Reusable call and pushes to `feature/**` or `v*` | Runs the read-only candidate lane for feature branches and fork tags, packages the Chart, invokes the image loop, and aggregates all evidence; an upstream `v*` tag reaches an explicit production failure until external capabilities exist |

All jobs explicitly use `contents: read`. Candidate routes do not inherit
secrets and do not use a protected environment, self-hosted runner, Registry
login/write, or GitHub mutation. `release-vllm-images.yml` can be awakened by a
`repository_dispatch` event, but the candidate never calls or initiates the
repository-dispatch API and has neither permission nor a write operation that
can send one.

### 2.2 Eight modules

| Module | Contract |
| --- | --- |
| `__init__.py` | Compact `python -m ucm_release` dispatch without a ninth forwarding file |
| `cli.py` | Single CLI surface and exit-2 fail-closed error handling |
| `core.py` | Strict YAML/JSON loading, schema subset validation, version agreement, 36-spec expansion, and manifest generation |
| `wheel.py` | Deterministic fixture wheel creation plus fixture/builder-candidate byte and metadata inspection |
| `chart.py` | Provenance verification, CUDA/A2/A3 Helm checks, and deterministic package normalization |
| `registry.py` | Exact upstream-tag parsing, read-only Registry snapshots, candidate identity, revision allocation, and reconciliation |
| `image.py` | Base/toolchain authority, seven-file context, local OCI scanning, image-result validation, and compact descriptor evidence |
| `verify.py` | Six-scenario loop, zero-write ledger audit, second reconcile, and final artifact recomputation |

### 2.3 Three schemas

| Schema | Instance |
| --- | --- |
| `config.schema.json` | `release.yaml` or `compatibility.yaml`, discriminated by `kind` |
| `release-manifest.schema.json` | Generated 36-wheel core manifest and GitHub Release asset declarations |
| `image-result.schema.json` | Fixture-only, unpublished image result and its required gates |

Unknown fields and duplicate JSON/YAML keys fail. The CLI additionally enforces
cross-document semantics that are more specific than structural schema checks.

### 2.4 Four Docker files

| File | Responsibility |
| --- | --- |
| `Dockerfile` | Digest-pinned frontend and install-only build sequence |
| `verify_base_image.py` | Reopen index, platform manifest, and config bytes; bind `FROM` to the exact platform digest |
| `install_ucm.py` | Verify the exact wheel, perform normal pip dependency resolution, run `pip check`, verify direct URL, versions, and imports |
| `inspect_runtime.py` | Verify Python ABI/package facts while retaining accelerator runtime and device checks as external-required |

## 3. Configuration and artifact contracts

### 3.1 Version authority

`version.ini` supplies `VLLM_UC_VERSION`. `core.validate_config` requires exact
agreement with:

- `release.yaml.ucm_version`;
- `compatibility.yaml.ucm_version`;
- Chart `appVersion`;
- `setup.py` version output.

For `0.5.0rc1`, the deterministic Helm SemVer is `0.5.0-rc.1`.

### 3.2 Wheel plan

Six declared profiles expand by NPU architecture, OS, CPU architecture, and ABI
to 36 specifications: 4 CUDA and 32 Ascend. CUDA requires immutable builder and
toolchain identities. Ascend additionally requires immutable ATB and torch-npu
package identities. Every specification requires an immutable runner identity.

All 36 checked-in specifications have unresolved identities and therefore 0 are
eligible. `core plan` succeeds to expose the complete blocked manifest;
`core plan --require-publishable` exits 2.

The feature/fork lane creates one deterministic fixture wheel bound to the full
source commit and exact fixture profile. That wheel is synthetic and remains
`fixture-only`, `published=false`, and `publication_eligible=false`.

Builder-candidate inspection, when the corresponding plan is resolved, requires
complete RECORD coverage, one ELF custom-op shared object, and an embedded build
binding for source commit, build-context digest, accelerator/runtime/device/OS/
CPU/ABI/profile. Inspection alone never marks an artifact published.

`wrapt==1.17.2` is ordinary wheel `Requires-Dist` metadata. The image install
uses pip dependency resolution with binary packages, then verifies `pip check`,
the installed direct URL, exact package versions, `import ucm`, and
`import wrapt`.

### 3.3 Chart contract

`release.yaml` binds `charts/ucm`, Chart name `unified-cache-pd`, the version
pair, and three cases:

- CUDA renders a digest-pinned synthetic image and `nvidia.com/gpu`;
- A2 renders a distinct digest-pinned synthetic image and
  `huawei.com/Ascend910`;
- A3 renders a third digest-pinned synthetic image and
  `huawei.com/Ascend910`.

Packaging verifies the immutable HTTPS source repository, source commit,
source-tree digest, every imported file digest, and the release-tree digest in
`SOURCE_PROVENANCE.json`. Helm lint and template run for all three cases. The
Helm-created archive is repacked with sorted members, fixed owner/group, mtime
zero, normalized modes, and deterministic gzip metadata, then linted again.

## 4. Image naming and Registry reconciliation

The two target repositories and public naming rules are:

```text
ghcr.io/modelengine-group/vllm-openai:<exact-upstream-tag>-ucm-<version>-rN
ghcr.io/modelengine-group/vllm-ascend:<exact-upstream-tag>-ucm-<version>-rN
```

Accepted vLLM OpenAI tags are canonical stable or RC tags. Accepted Ascend tags
use the same version with optional `-a3` and optional final `-openeuler`. No NPU
suffix means A2. An explicit A2 suffix, 310P, A5, nightly, dev, custom, `rc0`,
leading zeros, reordered suffixes, and extra architecture suffixes fail.

A3 and openEuler suffixes are retained only when present in the exact upstream
tag. CUDA, CANN, OS, Python, channel, and profile never become UCM-added
public-tag suffixes.

`tag_base` is `<exact-upstream-tag>-ucm-<version>`. `tag_family` is
`(target_repository, tag_base)`. The build key separately hashes the generated
manifest, exact wheel/spec, compatibility rule, upstream index and both platform
descriptor chains, Docker/base/toolchain implementation identity, and other
immutable build inputs.

Reconciliation consumes an inventory covering exactly both target repositories:

1. If one matching build key has equal observed and evidenced digests, return
   zero tasks.
2. If no matching build key exists, schedule the first unused revision.
3. If the matching tag has digest drift, preserve the prior revision and
   schedule the next unused revision, so `r1` remains and `r2` is created.
4. Duplicate/conflicting tags or multiple stable entries for one build key fail.

Every task carries the inventory SHA256, a tag-absence precondition, and the
tag-family concurrency key. The local candidate ledger permits read operations
and build planning only; any unknown or write-capable operation fails.

## 5. Install-only OCI contract

The candidate base authority fixes the repository, target platform, index,
platform manifest, and config digests. The image toolchain authority fixes
Buildx v0.19.2 binary hashes and digest-pinned BuildKit v0.18.2. Both authority
digests and all four Docker-file digests contribute to the implementation key.

The generated context contains exactly:

1. `Dockerfile`;
2. `verify_base_image.py`;
3. `install_ucm.py`;
4. `inspect_runtime.py`;
5. the exact wheel;
6. `image-recipe.json`;
7. `image-metadata.json`.

It contains no UCM source tree, setup/build script, CMake input, compiler input,
or source build command. Buildx produces a local OCI archive with provenance and
SBOM disabled and timestamp rewriting enabled. There is no Registry login or
push.

`image verify` reopens the OCI layout, index, manifest, config, every layer
descriptor, and ordered rootfs diff IDs. It extracts the embedded base, install,
runtime, recipe, metadata, and wheel evidence and requires these eight gates:

- base verified;
- exact wheel verified;
- install passed;
- `pip check` passed;
- direct URL passed;
- `ucm` import passed;
- `wrapt` import passed;
- Python ABI passed.

Runtime and device fields remain `external-required` with
`hardware_passed=false`.

The workflow uploads raw OCI layout/index/manifest/config documents and a
canonical descriptor/diff-ID closure, but omits the large archive and layer
blobs. Aggregation can bind that compact evidence to the recipe, metadata,
wheel, BuildKit descriptor, and image result. It cannot independently
decompress omitted layers; the full layer scan is proved only inside the same
image job before upload.

## 6. Loop Engineer protocol

Every implementation change follows this sequence:

1. Capture the narrow failing check or mutation.
2. Apply the smallest contract or implementation change.
3. Rerun the narrow check to GREEN.
4. Run the complete local matrix.
5. Record only observed successful results; keep external work blocked.

The deterministic loop must pass these six scenarios:

1. new input schedules one `r1` task;
2. identical stable input schedules zero tasks;
3. digest drift schedules `r2` without changing `r1`;
4. the upstream index contains complete amd64 and arm64 manifest/config chains;
5. missing arm64, duplicate/conflicting inventory, and unpublished production
   wheel paths block with their exact codes;
6. the fixture candidate goes from one task to a second full reconcile with
   zero tasks.

Ascend tag checks accept A2 and A3 and reject 310P and A5. Run metadata is kept
outside the deterministic payload identity. The final operation ledger must
derive `write_count=0` and publication `{status: blocked, attempted: false}`.

## 7. Verification commands

```bash
pytest -q .github/release/tests
pytest -q .github/release/tests/test_workflows.py
ruff check .github/release/ucm_release .github/release/tests .github/release/docker
ruff format --check .github/release/ucm_release .github/release/tests .github/release/docker
black --check .github/release/ucm_release .github/release/tests .github/release/docker
pre-commit run actionlint --all-files --hook-stage manual
PYTHONPATH=.github/release python -m ucm_release config validate
PYTHONPATH=.github/release python -m ucm_release core plan
```

For the publishability guard, `core plan --require-publishable` must exit 2 while
the checked-in identities remain unresolved. For Chart determinism, package to
two fresh output directories, compare archive bytes and member order, and lint
the resulting archive.

## 8. Current verification matrix

Fresh current-tree local evidence on 2026-08-08 is listed below. Hosted
fork-candidate attempt 1 also passed, but the final same-SHA attempt-2
comparison is still pending.

| Surface | Evidence | Status |
| --- | --- | --- |
| Complete compact tests | `190 passed in 55.84s` | Passed locally |
| Workflow policy | `84 passed` | Passed locally |
| Focused CLI/fixture/OCI/loop/tag checks | `24 passed in 1.11s` | Passed locally |
| Ruff lint/format and Black | 16 compact Python/test/helper files | Passed locally |
| Configuration and schemas | Both YAML files and all 3 schemas via CLI and independent `jsonschema` | Passed locally |
| Workflow documents | 7 parsed, 27 jobs, 48 external Action uses full-SHA pinned, 6 local calls without refs | Passed locally |
| actionlint | Pinned pre-commit hook | Passed locally |
| Chart | CUDA/A2/A3 lint/template/package twice; identical bytes and member tree | Passed locally |
| Chart package | SHA256 `4805117c69725d1ce093096ba6d5fcf46c4b2a7ff716544e993f5b87bedfefc6` | Passed locally |
| Chart release tree | SHA256 `6e0ea559cc946593ef162d8ea40497c05091466a543c8b997a2ecb0da22edb6f` | Passed locally |
| Repository Unit collection | `pynvml` missing at `test/conftest.py:14` | External dependency blocked |

Task 5 Round 5 produced two byte-identical current-code OCI archives with
SHA256 `199b53854f9bee4a7d81a32d2a046d7de220356c35d900297d400fa65059731a`, but
both builds used an already-running local builder. This proves repeatability
for those inputs, not execution through checksum-installed Linux Buildx plus
the authority-selected builder. Round 4 produced two byte-identical archives
with SHA256
`e25ce47385f701261f598453c0153e4813f912bf36a428db9d8f0a1c4044809e`
using authority-pinned BuildKit v0.18.2, but it has a pre-round-5 implementation
identity. The exact combined path of current final code, checksum-installed
Linux Buildx, the authority-created builder, and authority-pinned BuildKit
v0.18.2 then ran in the first hosted Task 7 candidate baseline in Section 9.4.
That run still produced only a local unpublished OCI archive.

The final local sequence now records 190 compact tests and 84 workflow-policy
tests, with pre-commit and actionlint green. Neither local OCI run proves a
Registry write, native production wheel, or accelerator runtime.

Hosted fork-candidate execution and artifact download have now run. A protected
production environment, all 36 native wheel builds, Registry write/readback,
cluster install, and CUDA/A2/A3 runtime and device acceptance remain unverified
and `external-required`. Production status remains blocked.

## 9. Task 7 hosted GitHub loop

The tracked [Task 7 GitHub Loop
plan](superpowers/plans/2026-08-08-ucm-release-slimming-loop.md) owns the hosted
execution limits and failure classifications. The [release operator
README](../.github/release/README.md#task-7-hosted-github-loop) is the only
canonical executable body; it begins with `set -euo pipefail` and accepts
`TASK7_DRY_RUN=1` to exercise all pre-push gates before deliberately refusing
the push.

### 9.1 Preconditions and tool authority

The script binds `feature/cicd` and its exact full `HEAD`, verifies that
`gh api user` returns `SuperMarioYL`, and accepts only the SSH or HTTPS URLs for
the `SuperMarioYL` origin and `ModelEngine-Group` upstream repositories. It
records the upstream `HEAD` before and after. The only push command is
`git push origin HEAD:refs/heads/feature/cicd`.

Crane is not a host prerequisite. The script downloads go-containerregistry
v0.20.3 into its task-specific directory and selects one hard-coded official
archive SHA256 for Darwin arm64, Darwin x86_64, Linux amd64, or Linux arm64. It
requires `sha256sum` or `shasum -a 256`, uses the resulting absolute crane path,
and verifies the exact `0.20.3` version before any anonymous Registry read.

### 9.2 Capability and readback boundary

Current probes of both owner package-list endpoints returned HTTP 403 because
the token lacks `read:packages`; anonymous crane reads of both known target GHCR
repositories returned `DENIED`. Each snapshot phase probes both package
endpoints again. Success is normalized by package ID; only explicit HTTP
403/`read:packages` errors become `UNAVAILABLE`, and other errors fail closed.
The loop does not expand token scope, does not turn unavailable package or
Registry reads into empty success, and does not block the authorized push solely
because those reads are explicitly unavailable.

The zero-write capability proof instead parses all four workflows and every
job permission as exactly `contents: read`, rejects `packages: write`, login,
Registry push, and dispatch-API commands, and later requires the runtime
operation ledger to report `write_count=0`. The before/after comparison covers
only fork PRs, tags, GitHub Releases, both owner package endpoint results,
upstream `HEAD`, and known-target GHCR tags/digests when those targets are
readable. A successful not-found response is canonical `ABSENT`;
authentication denial is canonical `UNAVAILABLE`.

### 9.3 Run discovery, rerun, and identity assertions

After the exact push, the script uses `gh run list --repo "$REPOSITORY"
--commit "$SOURCE_SHA" --event push --json
databaseId,workflowName,status,conclusion,headSha,url`. It applies local `jq`
selection to require exactly one `Push Commit Checks` and one
`Release UCM core artifacts` entry with the exact `headSha`. This avoids the
invalid assumption that a newly introduced feature-branch workflow is already
registered on the fork's default branch.

Both initial attempt-1 runs must be green before the first artifact download.
The same database IDs then receive `gh run rerun`; both must advance to attempt
2, finish green, and supply the second artifact tree. For each downloaded
envelope the script asserts `payload.source_sha`, `payload.repository`,
`payload.ref`, and `payload.workflow_refs`. It compares deterministic payload,
wheel, Chart, local OCI, and second-reconcile identities, requires the second
reconcile to be zero, and requires publication blocked plus `write_count=0`.
The local `oci_digest` is not Registry readback.

Finally, the readable-surface snapshots must compare byte-for-byte. Failures
are collected with `gh run view --log-failed`, job JSON, and artifacts within
the tracked plan limits. Hosted fixture success still leaves owner package
enumeration, GHCR readback, native wheels, Registry publication, cluster, and
accelerator evidence `external-required`; production remains blocked.

### 9.4 First hosted candidate baseline

The first full hosted candidate baseline ran on 2026-08-08 at source commit
`0ee113433b75868142d86c66ee0cda2af533cc89`.

| Evidence | Observed result |
| --- | --- |
| Push workflow | [Push Commit Checks run 31260552571](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31260552571), attempt 1, success |
| Release workflow | [Release UCM core artifacts run 31260552670](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31260552670), attempt 1, success |
| Evidence file SHA256 | `33a82f806d8ec43dbe55da887371b8651454baf8d1442324de0ab5a731ef7ec6` |
| Deterministic payload SHA256 | `02b5b2e174e6144f889a811f1571017897928546aaa3aec447e377687f04fe9f` |
| Wheel SHA256 | `08c4aa98ebb0cc5e0816619bda78d310fc5342da7fbec3611a70b2d5be76f19b` |
| Chart package SHA256 | `4805117c69725d1ce093096ba6d5fcf46c4b2a7ff716544e993f5b87bedfefc6` |
| Chart release-tree SHA256 | `6e0ea559cc946593ef162d8ea40497c05091466a543c8b997a2ecb0da22edb6f` |
| Local OCI manifest SHA256 | `7dfefdca3e4fb2f3e9fc8ca1efa55c3bbdf7ddd22a4584ec5b5a0af2afcd9a24` |
| OCI stable-closure SHA256 | `db3e37c1967ceb17c63af196f93d19f28a4dd5f85d0d57872604142dcf779c48` |
| Image-result SHA256 | `c7c00959d6bacd6bcdba2e284c3a2da16d2dc5bb10a4406149b2b61da53e556e` |
| Second-reconcile SHA256 | `29b1b9a6b6b3b62edc56f162d56cbfad0b9663ae20eb35aa5bb3bfcd05df5bdf` |

The Release run executed the fixture wheel, deterministic Chart, select-input,
first reconcile, install-only image build, final reconcile, and aggregate jobs.
Invalid, standalone, and production-only routes were skipped. The current
checksum-verified Buildx/toolchain authority created the builder used by this
run. The OCI result was local, unpublished, and `linux/amd64`; its eight
base/wheel/install/pip/direct-URL/import/ABI gates passed. Runtime and device
gates remained `external-required`.

All six Loop Engineer scenarios passed. The fixture descriptor scenario bound
two upstream platforms; that count must not be described as a multi-platform
OCI build. The final reconcile returned `already-present`, zero tasks, and an
empty task list. The ten-operation ledger contained only read and plan entries,
derived `write_count=0`, and kept publication blocked and unattempted.

The first hosted before/after audit was byte-identical for eight normalized
snapshot files. Fork PRs, tags, and Releases remained empty, and upstream
`HEAD` remained `e2b4c254801b77d4c05535a65bbc6c467b8c052b`. The two owner
package endpoints remained HTTP 403 and both target GHCR anonymous reads
remained `DENIED`; these are stable `UNAVAILABLE` states, not Registry
readback evidence.

This is an observed attempt-1 baseline. The final same-SHA rerun comparison is
deliberately deferred until the documentation commit containing this baseline
is pushed, so it is not claimed here as already complete.
