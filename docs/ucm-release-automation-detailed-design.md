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

Fresh Task 6 local evidence on 2026-08-08:

| Surface | Evidence | Status |
| --- | --- | --- |
| Complete compact tests | `187 passed in 57.62s` | Passed locally |
| Workflow policy | `81 passed in 45.61s` | Passed locally |
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
Linux Buildx, and authority-pinned BuildKit v0.18.2 remains
`external-required` pending an end-to-end Task 7 hosted GitHub run.

The final Task 5 local sequence separately recorded a clean Python 3.12 suite at
185 tests, a 9-case round-6 contract-order batch, and a clean full suite at 187
tests, with pre-commit and actionlint green. Neither OCI run proves a Registry
write, native production wheel, or accelerator runtime.

No GitHub release workflow has run for this candidate. Protected GitHub
execution, all 36 native wheel builds, Registry write/readback, cluster install,
and CUDA/A2/A3 runtime and device acceptance remain unverified and
`external-required`. Production status remains blocked.

## 9. Task 7 hosted GitHub loop

The tracked [Task 7 GitHub Loop
plan](superpowers/plans/2026-08-08-ucm-release-slimming-loop.md) owns the hosted
execution limits and failure classifications. The operator must use the
checksum-verified crane v0.20.3 binary declared by `_build-image.yml` and the
`snapshot_zero_write` function in the [release operator
README](../.github/release/README.md#task-7-hosted-github-loop).

The executable sequence is:

```bash
export REPOSITORY=SuperMarioYL/unified-cache-management
export UPSTREAM_REPOSITORY=https://github.com/ModelEngine-Group/unified-cache-management.git
export SOURCE_SHA="$(git rev-parse HEAD)"
export TASK7_ROOT="$(mktemp -d /tmp/ucm-task7.XXXXXX)"
test "$(git branch --show-current)" = feature/cicd
test "$(git rev-parse feature/cicd)" = "$SOURCE_SHA"
test "$(crane version)" = 0.20.3
snapshot_zero_write "$TASK7_ROOT/before"

git push origin HEAD:refs/heads/feature/cicd

push_run_id="$(gh run list --repo "$REPOSITORY" --commit "$SOURCE_SHA" \
  --workflow "Push Commit Checks" --limit 20 --json databaseId \
  --jq '.[0].databaseId')"
release_run_id="$(gh run list --repo "$REPOSITORY" --commit "$SOURCE_SHA" \
  --workflow "Release UCM core artifacts" --limit 20 --json databaseId \
  --jq '.[0].databaseId')"
test -n "$push_run_id"
test -n "$release_run_id"
gh run watch "$push_run_id" --repo "$REPOSITORY" --exit-status
gh run watch "$release_run_id" --repo "$REPOSITORY" --exit-status
gh run download "$release_run_id" --repo "$REPOSITORY" \
  --dir "$TASK7_ROOT/attempt-1"

gh run rerun "$push_run_id" --repo "$REPOSITORY"
gh run rerun "$release_run_id" --repo "$REPOSITORY"
gh run watch "$push_run_id" --repo "$REPOSITORY" --exit-status
gh run watch "$release_run_id" --repo "$REPOSITORY" --exit-status
gh run download "$release_run_id" --repo "$REPOSITORY" \
  --dir "$TASK7_ROOT/attempt-2"
```

Compare only deterministic fields. Attempt metadata is intentionally outside
the payload identity:

```bash
python - "$TASK7_ROOT" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])

def load(attempt):
    paths = list((root / attempt).rglob("release-loop-evidence.json"))
    assert len(paths) == 1, paths
    return json.loads(paths[0].read_text(encoding="utf-8"))

first = load("attempt-1")
second = load("attempt-2")
assert first["payload_sha256"] == second["payload_sha256"]
keys = ("wheel_sha256", "chart_sha256", "oci_digest", "second_reconcile_sha256")
assert all(
    first["payload"]["artifact_digests"][key]
    == second["payload"]["artifact_digests"][key]
    for key in keys
)
assert first["payload"]["must_green"]["second_reconcile_zero"] is True
assert second["payload"]["must_green"]["second_reconcile_zero"] is True
assert first["payload"]["write_audit"]["write_count"] == 0
assert second["payload"]["write_audit"]["write_count"] == 0
assert second["payload"]["publication"] == {"status": "blocked", "attempted": False}
PY

snapshot_zero_write "$TASK7_ROOT/after"
diff -ru "$TASK7_ROOT/before" "$TASK7_ROOT/after"
```

`Push Commit Checks` and `Release UCM core artifacts` must both be green for
the pushed SHA and the same-SHA `gh run rerun`. Downloaded nested artifacts
must prove matching payload, wheel, Chart, and OCI identities plus a second
zero-task reconcile. The snapshot diff must prove zero writes to fork PRs,
tags, GitHub Releases, GHCR packages, upstream Git refs, and the two pinned
upstream-image references. Production remains blocked: hosted fixture evidence
does not resolve native wheel, Registry publication/readback, cluster, or
accelerator requirements.
