# UCM Real Wheel and Image Release Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to execute this plan task by task. Every task is RED -> implementation -> local verification -> task review -> commit. Do not dispatch more than one implementation agent at a time.

**Goal:** Replace the fixture-only release lane with a compact, fail-closed implementation that builds six real native UCM wheels and six real install-only image members on GitHub-hosted native amd64/arm64 runners, while preserving a no-write feature lane and a protected Tag-only GHCR/Release lane.

**Architecture:** Keep exactly four release workflows and the existing `.github/release/` package. The top-level workflow builds the six wheels first. Image jobs then install the exact same-run wheel into immutable upstream vLLM member images. Feature runs keep Registry/Release writes disabled and retain wheel plus compact image evidence as Actions Artifacts. Protected Tag runs may push content-addressed members to one private staging package; only a six-of-six barrier can create three final dual-architecture `r1` indexes and a verified GitHub prerelease.

**Tech Stack:** GitHub Actions, Python 3.12, CMake, Buildx v0.19.2, BuildKit v0.18.2, crane v0.20.3, Docker/OCI, Helm 3.15.3, pytest, actionlint.

---

## Global constraints and proven baseline

- Work only in `/Users/yulei/workspace/unified-cache-management/.orbit/worktrees/cicd-real-release` on branch `feature/cicd-real-release` until final integration.
- Baseline is commit `4d6ad5762b5c440476517697240436413fff004d`; compact release suite is `190 passed in 59.00s`.
- The main worktree's three user C++ changes are out of scope. Their baseline combined diff SHA256 is `b66846b958d0e850e976a7d482c3cf0664037d4b839cc5b41da6cf763807e229`; no task may stage, copy, reformat, build from, or commit those bytes.
- Keep exactly four release workflows: `_build-wheel.yml`, `_build-image.yml`, `release-ucm.yml`, `release-vllm-images.yml`.
- Keep eight Python files under `.github/release/ucm_release`, three schemas, and four files under `.github/release/docker`; add no fifth workflow or ninth Python module.
- Do not recreate top-level `release/`, `scripts/release/`, or `docker/release/`; do not restore PR publishing, independent wrapt publishing, `/opt/ucm-release`, custom receipt/trust/state databases, or an image build that compiles UCM.
- `wrapt==1.17.2` remains an ordinary `Requires-Dist` dependency installed from an exact hashed wheel.
- Only `origin/feature/cicd` may be pushed during hosted candidate validation. Do not force-push, push upstream, create a PR, create a Git tag, create a GitHub Release, log in to GHCR, or write a package during feature validation.
- Production Tag jobs must remain unreachable unless all repository/Tag/default-branch/source-SHA/owner/Environment/ref-protection checks pass. No production Tag is created or run under this plan without a later explicit user instruction.
- Runtime and accelerator-device checks remain `external-required`; real wheel compilation, image installation, ABI inspection, Registry descriptor verification, and hosted runner evidence must not be described as CUDA/NPU device validation.
- Use `apply_patch` for repository edits. Use exact-path staging, never `git add -A`. Each task ends with a task-scoped commit after an independent task review.

## Exact first-release matrix

The production matrix contains exactly these six wheel records and three image families:

| profile | platform | wheel version | upstream image | target |
| --- | --- | --- | --- | --- |
| `cuda130` | `linux/amd64` | `0.5.0rc1+cuda130` | `docker.io/vllm/vllm-openai:v0.21.0` | `ghcr.io/supermarioyl/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1` |
| `cuda130` | `linux/arm64` | `0.5.0rc1+cuda130` | same index, arm64 member | same dual-arch index |
| `cann900-a2` | `linux/amd64` | `0.5.0rc1+cann900.a2` | `quay.io/ascend/vllm-ascend:v0.22.1rc1` | `ghcr.io/supermarioyl/vllm-ascend:v0.22.1rc1-ucm-0.5.0rc1-r1` |
| `cann900-a2` | `linux/arm64` | `0.5.0rc1+cann900.a2` | same index, arm64 member | same dual-arch index |
| `cann900-a3` | `linux/amd64` | `0.5.0rc1+cann900.a3` | `quay.io/ascend/vllm-ascend:v0.22.1rc1-a3` | `ghcr.io/supermarioyl/vllm-ascend:v0.22.1rc1-a3-ucm-0.5.0rc1-r1` |
| `cann900-a3` | `linux/arm64` | `0.5.0rc1+cann900.a3` | same index, arm64 member | same dual-arch index |

The implementation models three tag families but only two target GHCR packages.

## Exact immutable image authorities

### Final runtime images

| family | index | amd64 member/config | arm64 member/config |
| --- | --- | --- | --- |
| CUDA | `sha256:a230095847e93bd4df9888b33dab956fa9504537b828a23657d2b26fed57b5c9` | `sha256:4ac9b7c6dabc3ec762c0edef4e9245abe98373844da91cc53ee42e5c58280c5b` / `sha256:2497255b1272ba3ae9581acd51349f840038f228d0709cd9f6a142d39008d290` | `sha256:4f63b83537c4cbd82822403965f395877054dc3b69612e7044ecd649a9badb02` / `sha256:f023269abe06db3a1a7cd9e170a0f5bd2b333a19ef9cb99ed8df97a70345bc25` |
| A2 | `sha256:9008b47081282612abfe4d28069ce34436752c980fd06f7599343213205ce64d` | `sha256:a4176a62da7ff54e8eaf2e68e09578917d5c93dab6d2c9c7ebce551781e117b3` / `sha256:0539c8ff2dbe3f02d6e5de0d8a463e1d6142482d41e5139dc4b957a191951c8b` | `sha256:638fc04eaa3654fcf14688096ed4e9d88ea0d905fa8685eed4b36d5fffe8fd8d` / `sha256:1b8f114d14c4d0bea66ca32ebb5afe34bd1e10bfd0802b930ed678a116aaf078` |
| A3 | `sha256:e3d89f09a1c1d85f0ec6a1cc26e3c807b7bc8a7ec0f97a830dbef63ab50d8f81` | `sha256:ac166d960b3cb1b584f6ec413e5f4ef353b78049aa2b85e22b0e04eb8770eae4` / `sha256:9c5cc2811d8dd9f26e871389723bc432fc321ba7bf46279ec423cbdd4daf9853` | `sha256:28f44b9c94c7667a7cbcd6b7b91432f03e4dffe476784dfd9fd82f036bdb1e4d` / `sha256:c4d766b5f04fe6238a74731d67a215bb6331072ba242c7c5f24a25f99ce36c3b` |

### Wheel builder images

- CUDA builder root is `quay.io/pypa/manylinux_2_28_x86_64@sha256:f854c50adf7b7a325bc4794316f3758d387a41d61f9e2ebca0f26c7dc8f761d4` with config `sha256:3d2c02ecc50fe7a2333aac46e915d2018588246019e39c9b3cef19181ddbcab7`, or `quay.io/pypa/manylinux_2_28_aarch64@sha256:b9dd5b2d6885fae144119ac934978003bcc413087ea08f602a960257205ec246` with config `sha256:6ab38a8a874241b72374be51c305939ccbb0041f53a434fb59b34c9c9406f4f0`.
- CUDA toolkit source is `docker.io/nvidia/cuda:13.0.2-devel-ubuntu22.04` index `sha256:1c517d4fb96528c8999e14e6e6b16d7e2ff9ae8d194fd788d60fdc88e693e982`; amd64 member/config are `sha256:6b6617592b94e7dcc6ffbe6d00720eed27bc6e3b4f06b26b93b4070c31f57391` / `sha256:ec9a1a479ad110600f15a0959a7b4c5bc9ddaed8b34a2d020abb7aea8b5b1707`; arm64 member/config are `sha256:bf85cb304bfe9ee637bb1fed21d09afbbe284a5f313773ac80d352537422ea98` / `sha256:1a14f400cf953afc772b596f284218fd11cb32e35ba29ccceafb317007404a72`.
- CUDA builder copies only `/usr/local/cuda-13.0` plus `/usr/local/cuda` from that exact toolkit stage into the exact manylinux root. It installs no apt/rpm packages. The preflight requires cp312, matching SOABI, `gcc`, `g++`, `make`, `git`, `nvcc`, CUDA headers, and the configured CMake wheel.
- A2 builder roots are `quay.io/ascend/manylinux` index `sha256:d5dfc2dc4f4400ab35f05f7568503df9829d779f7b9f7c17be75adb65cadd965`; amd64 member/config `sha256:53bb61ac277cfcec2ca2e3f04043a91869a8cbf9a3099380133683f66bb92312` / `sha256:a4a55b63fcf1bd620263c989b9226e329e28d8823c26f1b39e014848762c3e01`; arm64 member/config `sha256:6cc01387d36c47d4a1bce1dbefa18cb5e8c3274f988795cfdb68760fb73daf75` / `sha256:1ef6c226c8d49d41d7c0ace251c8b6e79301113073c6da8dea940774958c0a1d`.
- A3 builder roots are `quay.io/ascend/manylinux` index `sha256:1070cb18ba4d158674871ccdc26ceeb67697b6af08137796702b78ea8e89f39b`; amd64 member/config `sha256:32180ed13a5183130466850ca8658c514b5ac91dc4d18fe81d1fc90eb586f283` / `sha256:0842ede965ed12f5be2d5940cb84f671831853eef4e148b3d4cd13efc4a88697`; arm64 member/config `sha256:7c729787b2a89816c350606155ae3705adbf1d251c7bae7ad11a3b5a4d0a3c50` / `sha256:27075961c39a224a8a162b6e640e7bb9fd80de978471d42c9b8c6238194dbe93`.
- A2/A3 builders copy the exact Mooncake headers and `/usr/local/lib` Mooncake artifacts from their exact final runtime member stage. They do not fetch or rebuild Mooncake independently.
- Python build lock is: `build==1.3.0` `7145f0b5061ba90a1500d60bd1b13ca0a8a4cebdd0cc16ed8adf1c0e739f43b4`; `pyproject-hooks==1.2.0` `9e5c6bfa8dcc30091c74b0cf803c81fdd29d94f01992a7707bc97babb1141913`; `packaging==24.2` `09abb1bccd265c01f4a3aa3f7a7db064b36514d2cba19a2f694fe6150451a759`; `setuptools==75.8.2` `558e47c15f1811c1fa7adbd0096669bf76c1d3f433f58324df69f3f5ecac4e8f`; `wheel==0.45.1` `708e7481cc80179af0e556bbf0cc00b8444c7321e2700b8d8580231d13017248`.
- CMake is `3.31.6`: x86_64 wheel `2297e9591307d9c61e557efe737bcf4d7c13a30f1f860732f684a204fee24dca`, aarch64 wheel `42d9883b8958da285d53d5f69d40d9650c2d1bcf922d82b3ebdceb2b3a7d4521`.
- All builder downloads are first saved into a wheelhouse and checked against these hashes; the builder installs with `--no-index --find-links --require-hashes --only-binary=:all:`.

## Exact native component policy

Common required native libraries are `ucmtrans`, `metrics`, `ucmmetrics`, `ucmlogger`, `ucmnfsstore`, `ucmpcstore`, `posixstore`, `compressor`, `cachestore`, `emptystore`, `fakestore`, and `ucmpipelinestore`. A2/A3 additionally require `mooncakestore`; CUDA forbids it. Every profile forbids `ds3fsstore`, `uc_hash_ext`, every sparse/custom-op library, and MindIE artifacts. The inspector validates the actual wheel member set and every ELF's architecture and DT_NEEDED closure; it does not require `ucm_custom_ops`.

---

### Task 1: Replace broad fixture declarations with the exact six-task production model

**Files:**
- Modify: `.github/release/release.yaml`
- Modify: `.github/release/compatibility.yaml`
- Modify: `.github/release/schemas/config.schema.json`
- Modify: `.github/release/ucm_release/core.py`
- Modify: `.github/release/ucm_release/cli.py`
- Modify: `.github/release/tests/test_config.py`
- Modify: `.github/release/tests/test_core_release.py`

- [ ] Write RED tests asserting exactly six immutable wheel specs, three image families/two target repositories, exact builder/runtime descriptor chains, runner mapping, local versions, dependency locks, target tags, and exact allowlists. Add mutations for a missing/extra profile, swapped architecture digest, mutable tag-only authority, basename-only evil repository, unresolved lock, duplicated public coordinate, and a caller-supplied raw runner label.
- [ ] Replace the 36 unresolved wheel declarations with the exact matrix and authorities above. Keep the configuration small: one base release object, three profiles, two architectures per profile, three image families, and exact runner maps.
- [ ] Make `validate_config()` rederive all canonical digests and require exact key sets. Cross-check release and compatibility records so a semantic mismatch cannot be hidden behind separately valid YAML.
- [ ] Add `core matrix --lane feature-candidate|protected-tag` and `core tag-preflight`. `matrix` emits the exact six task objects in stable order. `tag-preflight` validates repository `SuperMarioYL/unified-cache-management`, owner `SuperMarioYL`, `v0.5.0rc1`, `version.ini`, full source SHA, default branch `develop`, ref protection and `UCM_RELEASE_POLICY=owner-reviewed-v1`; the feature lane derives no write authority.
- [ ] Preserve Chart metadata/version checks and make CUDA/A2/A3 values accept only final `repository@sha256` coordinates.
- [ ] Run focused RED/GREEN, full compact tests, schema validation, Ruff/Black, and `git diff --check`.
- [ ] Commit exact Task 1 files with message `feat(release): define real six-task matrix`.

### Task 2: Build, seal, and inspect real native wheels

**Files:**
- Modify: `setup.py`
- Modify: `CMakeLists.txt`
- Modify: `ucm/shared/vendor/dep-fmt.cmake`
- Modify: `ucm/shared/vendor/dep-spdlog.cmake`
- Modify: `ucm/shared/vendor/dep-pybind11.cmake`
- Modify: `ucm/shared/vendor/dep-zlib.cmake`
- Modify: `ucm/store/ds3fs/CMakeLists.txt`
- Modify: `ucm/store/mooncakestore/CMakeLists.txt`
- Modify: `.github/release/docker/Dockerfile`
- Modify: `.github/release/ucm_release/wheel.py`
- Modify: `.github/release/ucm_release/cli.py`
- Modify: `.github/release/schemas/release-manifest.schema.json`
- Modify: `.github/release/tests/test_core_release.py`

- [ ] Write RED tests for controlled local versions, missing `PLATFORM`, wrong source/profile/architecture, incomplete required native set, forbidden target presence, missing Mooncake on Ascend, Mooncake on CUDA, corrupt/trailing ZIP bytes, noncanonical ZIP metadata/order/mode, forged `RECORD`, wrong ELF machine, unapproved DT_NEEDED, and source/path leakage.
- [ ] Add `UCM_RELEASE_PROFILE`, `UCM_RELEASE_SOURCE_SHA`, `UCM_RELEASE_VERSION`, `UCM_RELEASE_BUILD_KEY`, `SOURCE_DATE_EPOCH`, and required/forbidden target lists to `setup.py`/CMake. Production builds reject absent or inconsistent values rather than falling back to `simu`.
- [ ] Pin fmt/spdlog/pybind11/zlib to the four immutable commits in the design and remove `GIT_SHALLOW TRUE` for commit fetches.
- [ ] Make `mooncakestore` required for A2/A3 and forbidden for CUDA. Make `ds3fsstore`, MindIE, and sparse targets unconditionally absent in a release build. Emit a canonical installed-component manifest and fail CMake configuration if required/forbidden policy is not exact.
- [ ] Add deterministic compile/link flags: fixed `/workspace/ucm` source path, `-ffile-prefix-map`, `-fdebug-prefix-map`, UTC/source epoch, deterministic archive mode, and a pinned build-id policy.
- [ ] Add `wheel seal` and `wheel inspect --source-kind builder-candidate`. Sealing embeds exact `ucm-build.json`, rewrites `METADATA`, `WHEEL`, and `RECORD`, normalizes ZIP timestamp/order/mode, and renames to the exact cp312 platform filename. Inspection independently recomputes all metadata, native exact set, ELF machine/DT_NEEDED, binding, and digest.
- [ ] Extend the existing Dockerfile with `wheel-cuda` and `wheel-ascend` targets using the exact immutable builder stages and hashed Python tool wheelhouse; keep the install-only image target independent of repository source.
- [ ] Build and inspect at least the native arm64 CUDA wheel and both native arm64 Ascend wheels locally when Docker capacity permits. If a builder preflight is missing a declared file/tool, fail and fix the immutable builder composition; do not waive the gate or fall back to a fixture.
- [ ] Prove same-source double build byte equality for at least one real arm64 profile before committing.
- [ ] Run all release tests, formatter/lint, `python -m build` negative/positive smoke, and `git diff --check`.
- [ ] Commit exact Task 2 files with message `feat(release): build sealed native wheels`.

### Task 3: Build and verify six real install-only image members

**Files:**
- Modify: `.github/release/docker/Dockerfile`
- Modify: `.github/release/docker/install_ucm.py`
- Modify: `.github/release/docker/inspect_runtime.py`
- Modify: `.github/release/docker/verify_base_image.py`
- Modify: `.github/release/ucm_release/image.py`
- Modify: `.github/release/ucm_release/cli.py`
- Modify: `.github/release/schemas/image-result.schema.json`
- Modify: `.github/release/tests/test_image_build.py`

- [ ] Write RED tests for all six production authorities, exact same-run wheel binding, wrong base index/member/config, missing wrapt wheel or wrong hash, source/build-key mismatch, UCM source/CMake/compiler entering the final context, wrong ELF architecture, missing native member, failed import/pip/direct-url/ABI gate, fixture result relabeling, and mutable/noncanonical content identity.
- [ ] Generalize image authority, recipe, verification, and result schemas into distinct fixture-candidate and real-candidate variants. A real result is still `unpublished` until Registry/Release readback; it can never derive publication authority from caller flags.
- [ ] Prepare an exact install-only context containing Dockerfile/helpers/recipe/the one UCM wheel/the one architecture-specific wrapt wheel/hash lock. Reject any source tree, setup file, build tool, or extra wheel.
- [ ] Install with `pip --no-index --find-links --require-hashes --only-binary=:all: --no-cache-dir --disable-pip-version-check`, then run `pip check`, UCM/wrapt import, exact version, direct URL, native ELF, base descriptor-chain, runtime metadata and device metadata checks.
- [ ] Keep runtime/device as `external-required`; all other required gates must pass. Build keys bind source, wheel, profile, architecture, upstream index/member/config, Docker/helper digests, dependency lock, Buildx/BuildKit/frontend authority, source epoch, and deterministic flags.
- [ ] Feature mode emits local OCI, reopens index/manifest/config/layers/diff_ids/annotations, writes compact evidence, then removes the large archive. Production mode emits a canonical member digest record suitable for private staging readback.
- [ ] Locally build/reopen the three native arm64 members from the real Task 2 wheels when possible; never use fixture wheels for this positive path.
- [ ] Prove a repeat build has identical content identity; archive byte equality is required when the same pinned local builder path is used.
- [ ] Run focused/full tests, real OCI smoke, format/lint and diff checks.
- [ ] Commit exact Task 3 files with message `feat(release): verify real install-only images`.

### Task 4: Implement private staging, six-member barrier, and dual-arch index reconciliation

**Files:**
- Modify: `.github/release/ucm_release/registry.py`
- Modify: `.github/release/ucm_release/verify.py`
- Modify: `.github/release/ucm_release/cli.py`
- Modify: `.github/release/schemas/release-manifest.schema.json`
- Modify: `.github/release/tests/test_registry_reconcile.py`

- [ ] Write RED tests covering exact upstream repository allowlists, six wheel/member records, two architectures per family, three families/two target packages, private staging visibility, `staging-<64hex-build-key>` absence/same/different behavior, 6/6 barrier, failed/cancelled/skipped member, r1 create/reuse/conflict, deterministic member ordering, annotation/build-key closure, authenticated/anonymous readback, and zero writes in feature mode.
- [ ] Add canonical commands: `registry inventory`, `verify-member`, `plan-index`, `verify-index`, and `audit-operations`. Read operations and write operations have immutable typed ledgers; feature mode rejects any write-capable operation before execution.
- [ ] Model production writes only as: content-addressed push to `ghcr.io/supermarioyl/ucm-release-staging`, collision-safe GC tag, and post-barrier `imagetools create` for the three exact r1 tags. Do not save a parallel local release-state database.
- [ ] Reconcile uses Registry inventory plus OCI annotations. Same build key/digest is a no-op; any same-name content drift is a hard failure. amd64 and arm64 form one index r1 and must never be assigned separate revisions.
- [ ] Add loopback Registry contract tests using `docker.io/library/registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373`, random loopback port, temporary network/volume, crane v0.20.3, push-by-digest, complete readback, negative mutation, and trap cleanup. Assert no GHCR host is present in the operation ledger.
- [ ] Run focused/full tests, loopback publisher contract on the local architecture, formatter/lint and diff checks.
- [ ] Commit exact Task 4 files with message `feat(release): reconcile staged multiarch images`.

### Task 5: Rewrite the four workflows for real hosted builds and protected publication

**Files:**
- Modify: `.github/workflows/_build-wheel.yml`
- Modify: `.github/workflows/_build-image.yml`
- Modify: `.github/workflows/release-ucm.yml`
- Modify: `.github/workflows/release-vllm-images.yml`
- Modify: `.github/release/ucm_release/verify.py`
- Modify: `.github/release/ucm_release/cli.py`
- Modify: `.github/release/tests/test_workflows.py`

- [ ] Write workflow RED tests for the exact four-workflow topology, six-wheel/six-member matrices, native runner mapping, `fail-fast: false`, 180/210 minute timeouts, disk cleanup/preflight, full-SHA Action pins, feature zero-write permissions, protected Tag job allowlists, transitive skipped/failed/cancelled barriers, same-run artifact binding, staging-only pre-barrier writes, three index merge, Release readback order, and source-SHA workflow references.
- [ ] `_build-wheel.yml`: validate inputs before checkout, map architecture to `ubuntu-24.04`/`ubuntu-24.04-arm`, free disk, require >=60GiB, install pinned tools, build the exact Docker target, seal/inspect wheel, upload wheel/record/log with three-day retention.
- [ ] `_build-image.yml`: validate inputs before checkout, use the same architecture runner, download exact same-run wheel, probe the exact upstream descriptor chain, build/verify install-only context. Feature lane uses local OCI and no credentials; protected Tag lane uses only `contents: read, packages: write` in `release-production`, checks `ref_protected` and policy, then pushes to private staging by digest and reads it back.
- [ ] `release-vllm-images.yml`: consume all six wheel records, produce six image members with `fail-fast: false`, enforce explicit `!cancelled()` plus direct-need success checks, and permit no final tag before all six succeed. The merge job reopens all records and handles the three sorted families.
- [ ] `release-ucm.yml`: feature push runs real wheel -> Chart -> real image workflow and uploads only candidate artifacts/evidence. Protected `v0.5.0rc1` performs preflight, uses top-level `ucm-tag-${repository_id}-${ref_name}` concurrency, runs the same build path, then authenticated readback -> empty draft -> anonymous readback -> assets -> download/re-hash -> prerelease publish.
- [ ] Install crane/Buildx/Helm with exact checksums and BuildKit/frontend with exact digests. All third-party Actions use full commit SHAs. No caller controls `runs-on`, Environment, repository, tag, or artifact names.
- [ ] Update loop evidence so deterministic payload includes six wheel SHAs, six member digests/build keys, three index plans, Chart SHA, second reconcile zero, operation audit, and publication status; run/attempt/time are outside the deterministic payload.
- [ ] Run workflow focused tests, full release tests, exact-workflow actionlint, pre-commit, clean Python 3.12 environment, and path/permission/forbidden-command scans.
- [ ] Commit exact Task 5 files with message `feat(release): run real hosted release builds`.

### Task 6: Close local validation and update evidence-bound documentation

**Files:**
- Modify: `.github/release/README.md`
- Modify: `docs/ucm-release-automation-technical-review.md`
- Modify: `docs/ucm-release-automation-detailed-design.md`
- Modify: `docs/superpowers/specs/2026-08-09-ucm-tag-release-design.md`
- Modify only tests/code that a fresh validation proves defective; no speculative cleanup.

- [ ] Run the full release suite, schema validation, exact four-workflow actionlint, full pre-commit, Ruff, Black, `git diff --check`, and C++ scope guard.
- [ ] Build real native arm64 wheels/members locally to the extent supported by the local Docker engine. Record exact commands, wheel SHA, image/member digest, descriptor closure, native member list, install/import/ABI gates, and explicit runtime/device external status. Do not claim amd64 or hosted success from local arm64 evidence.
- [ ] Run the disposable loopback Registry publisher contract and verify inventory is empty after cleanup.
- [ ] Run Helm lint/template/package for CUDA/A2/A3 with final repository@digest fixtures; double-package and compare byte SHA.
- [ ] Update all three release documents and the design only with evidence from the current implementation. Clearly distinguish local arm64, hosted feature, protected Tag, GHCR, Release, and device evidence.
- [ ] Independently reader-test commands, links, names, matrix counts, permissions and external-required statements.
- [ ] Commit exact Task 6 files with message `docs(release): document real build boundary`.

### Task 7: Integrate, push the feature branch, and run the GitHub Loop Engineer

**Files:**
- Merge reviewed commits into main worktree branch `feature/cicd` without staging the three user C++ files.
- Modify the three release documents only after hosted evidence exists.

- [ ] Dispatch a most-capable whole-branch reviewer against `4d6ad576..HEAD`; fix one final round and re-review before integration.
- [ ] Verify every task commit, clean isolated index, exact diff, four/eight/three/four structure counts, absence of legacy paths, and unchanged C++ diff SHA.
- [ ] Fast-forward `feature/cicd` to the reviewed implementation while preserving the main worktree's unstaged C++ changes. Stage no C++ path.
- [ ] Run the canonical README pre-push safety audit; require exact origin `SuperMarioYL/unified-cache-management`, branch `feature/cicd`, actor, no staged contamination, read-only candidate permissions, and before snapshots.
- [ ] Push only `git push origin HEAD:refs/heads/feature/cicd`; no force, no upstream, no PR, no tag, no Release, no GHCR login/write.
- [ ] Wait for exact-SHA `Push Commit Checks` and `Release UCM core artifacts`. Confirm reusable jobs show six real wheel builds and six real image builds on native hosted architectures rather than fixture jobs.
- [ ] Download all six wheel artifacts and compact image evidence to a temporary directory. Recompute wheel bytes/records, confirm local versions and ELF architecture, verify six member identities, three family plans, Chart SHA, zero Registry writes and second reconcile zero.
- [ ] If hosted capacity or a real builder fails, classify the exact C0/C1/C2/C3 cause, add a RED regression when applicable, fix locally in the isolated branch, review, fast-forward, and push a new SHA. Never replace a real lane with a fixture to obtain green.
- [ ] Once one SHA is fully green, rerun that same release run once. Require identical six wheel SHAs, six member/build-key identities, three index plans, Chart SHA, deterministic payload SHA, and second reconcile zero.
- [ ] Run after snapshots. Readable PR/tag/Release/upstream state must be byte-identical; package/GHCR endpoints that remain 403/DENIED stay `UNAVAILABLE`, not false zero-write evidence. Static permissions plus operation ledger must show no write capability in the feature run.
- [ ] Update the three release documents with exact commit, run URLs, attempts, artifact digests and real hosted outcomes. Keep protected Tag/GHCR/Release/device capability marked unexecuted until separately authorized and observed.
- [ ] Commit documentation, push the final feature SHA, obtain a final green run and same-SHA rerun, then report the real wheel/image outputs and remaining production blockers.

## Completion gate

This plan is complete only when the fork's final `feature/cicd` SHA has produced six non-fixture native wheel artifacts and six non-fixture install-only image results, both native architectures have run, the same-SHA rerun is deterministic, full local/hosted checks are green, and the feature run has no Registry/Release/tag/upstream write capability. Implemented but unexecuted protected Tag publication must be reported separately, not counted as a produced public GHCR release.
