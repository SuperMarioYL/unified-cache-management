# UCM release automation technical review

## Review result

The repository now has one compact, fail-closed release candidate loop. Four
release workflows orchestrate eight Python modules under `.github/release`,
three JSON Schemas, four install-image files, and the product Chart at
`charts/ucm`. The feature/fork path is fixture-only, read-only, and unpublished.
The production path is intentionally blocked until external infrastructure and
real artifacts exist.

This review accepts the local candidate architecture. It does not approve a
production release or Registry publication.

## Reviewed architecture

| Surface | Reviewed role |
| --- | --- |
| `_build-wheel.yml` | Gate callable inputs before checkout; build and inspect one source-bound fixture wheel |
| `_build-image.yml` | Gate callable inputs before checkout; authenticate Buildx, resolve the fixed base descriptor chain, build a local install-only OCI archive, and emit compact evidence |
| `release-vllm-images.yml` | Prepare the Registry plan, build the image, and prove the second reconciliation returns zero tasks |
| `release-ucm.yml` | Run the fixture wheel, deterministic Chart package, image loop, and final evidence aggregation |
| `.github/release/ucm_release` | Own all validation, identity, reconciliation, OCI, and evidence policy |
| `charts/ucm` | Provide the product Helm package with immutable `SOURCE_PROVENANCE.json` |

The repository contains exactly those four release workflows plus
`lint-and-test.yml`, `pull-request.yml`, and `push-check.yml`.

## Contract findings

`version.ini` is the UCM version authority. The current value `0.5.0rc1` agrees
with `setup.py`, both release YAML files, Chart `appVersion`, and the derived
Chart version `0.5.0-rc.1`.

`release.yaml` declares six profiles that expand to 36 production wheel
specifications: 4 CUDA and 32 Ascend. All builder, toolchain, package, and runner
identities are unresolved, so 0 of 36 are eligible. The one fixture wheel used
by the local loop is synthetic, source-bound, unpublished, and not counted as an
eligible production wheel.

The Python package keeps `wrapt==1.17.2` as ordinary dependency metadata. The
image install uses normal pip dependency resolution, followed by `pip check`,
direct-URL verification, and imports of both `ucm` and `wrapt`.

The public image rule is exactly:

```text
ghcr.io/modelengine-group/vllm-openai:<exact-upstream-tag>-ucm-<version>-rN
ghcr.io/modelengine-group/vllm-ascend:<exact-upstream-tag>-ucm-<version>-rN
```

A3 and openEuler suffixes are retained only when present in the exact upstream
tag. CUDA, CANN, OS, Python, channel, and profile values never become UCM-added
public-tag suffixes. `tag_family` is `(target_repository, tag_base)`. The same
build key and matching observed/evidenced digest returns zero tasks. Digest
drift does not overwrite `r1`; it allocates `r2`.

## Security and failure boundaries

- Every release job declares `permissions: contents: read`. Candidate work uses
  hosted runners and no protected environment, secret inheritance, login,
  push, or repository mutation. `release-vllm-images.yml` can be awakened by a
  `repository_dispatch` event, but the candidate never calls or initiates the
  repository-dispatch API and has neither permission nor a write operation that
  can send one.
- Reusable build contracts reject malformed SHA, lane, profile, or artifact
  input before checkout, Python setup, downloads, or repository code.
- All third-party Actions use full 40-character commit SHAs. Buildx v0.19.2
  binaries are checked against architecture-specific SHA256 values; BuildKit
  v0.18.2 and the Dockerfile frontend are digest pinned.
- YAML/JSON loaders reject duplicate keys. Schemas reject extra fields, and
  canonical JSON hashes bind the configuration, wheel, Chart, Registry read
  set, image recipe, and loop payload.
- Registry discovery is read-only. Reconciliation consumes an immutable
  inventory digest and emits a tag-absence precondition plus a zero-write
  operation ledger.
- The Docker context has exactly seven files: one Dockerfile, three helpers,
  one wheel, one recipe, and one metadata document. It contains no UCM source,
  build system, compiler, or custom-op build step.
- The OCI verifier reopens manifest/config/layer descriptors and checks ordered
  rootfs diff IDs before accepting embedded install evidence.

The compact OCI artifact uploaded by the image workflow does not contain the
large archive or its layer blobs. Final aggregation reopens the compact
descriptor closure produced after the same job scanned the complete archive,
but cannot independently decompress the omitted layers. This is an explicit
local-evidence limitation.

## Fresh local evidence on 2026-08-08

| Check | Result |
| --- | --- |
| Complete compact release suite | `187 passed in 57.62s` |
| Workflow policy suite | `81 passed in 45.61s` |
| Focused CLI, fixture wheel, OCI, six-scenario, and tag-boundary checks | `24 passed in 1.11s` |
| Ruff lint/format and Black | Passed over all 16 compact Python/test/helper files |
| Both YAML configurations and all three JSON Schemas | CLI and independent `jsonschema` validation passed |
| Seven workflow documents | Parsed; 27 jobs; 48 external Action uses full-SHA pinned; 6 local reusable calls had no ref suffix |
| actionlint | Passed through the pinned pre-commit hook |
| Helm CUDA/A2/A3 lint, template, and repeated package | Passed; both archives and member trees matched |
| Helm package SHA256 | `4805117c69725d1ce093096ba6d5fcf46c4b2a7ff716544e993f5b87bedfefc6` |
| Chart release-tree SHA256 | `6e0ea559cc946593ef162d8ea40497c05091466a543c8b997a2ecb0da22edb6f` |

Task 5 Round 5 produced two byte-identical current-code OCI archives at SHA256
`199b53854f9bee4a7d81a32d2a046d7de220356c35d900297d400fa65059731a`, but
both builds used an already-running local builder. This proves repeatability
for those inputs, not execution through checksum-installed Linux Buildx plus
the authority-selected builder. Round 4 produced two byte-identical archives
at SHA256
`e25ce47385f701261f598453c0153e4813f912bf36a428db9d8f0a1c4044809e`
using authority-pinned BuildKit v0.18.2, but it has a pre-round-5 implementation
identity. The exact combination of current final code, checksum-installed Linux
Buildx, and authority-pinned BuildKit v0.18.2 has not run end to end. That
combination is `external-required` pending the Task 7 hosted GitHub run.

The final Task 5 local sequence separately recorded a clean Python 3.12 suite at
185 tests, a 9-case round-6 contract-order batch, and a clean full suite at 187
tests, with pre-commit and actionlint green. Neither OCI history item is
evidence of a Registry write or accelerator runtime.

Repository-wide Unit collection is not reported as passing. This host stops at
`test/conftest.py:14` because the external `pynvml` module is unavailable.

## Task 7 hosted GitHub loop

The executable continuation is governed by the tracked [Task 7 GitHub Loop
plan](superpowers/plans/2026-08-08-ucm-release-slimming-loop.md). Use the
`snapshot_zero_write` function and evidence-comparison block in the [release
operator README](../.github/release/README.md#task-7-hosted-github-loop). They
capture PRs, tags, GitHub Releases, every GHCR tag/digest, and the exact
upstream Git refs plus Docker Hub/Quay upstream-image digests before and after
the run.

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

The command shown is the only permitted push. A failed run is handled with
`gh run view --log-failed`, job JSON, and downloaded artifacts under the plan's
bounded repair loop. A final green push attempt and same-SHA `gh run rerun` are
still fixture evidence: the second reconcile must be zero, the before/after
audit must show no PR, tag, GitHub Release, GHCR, or upstream change, and
production remains blocked.

## External blockers

No GitHub release workflow has run for this candidate. The following remain
unverified and `external-required`:

- protected GitHub execution and artifact download/readback;
- resolved builders, toolchains, package locks, and production runners for all
  36 wheel specifications;
- native ELF/custom-op wheel inspection from those builders;
- OCI Registry credentials, write, and digest readback;
- CUDA and CANN runtime checks on matching CUDA, A2, and A3 devices;
- cluster installation and workload acceptance;
- formal publication.

Until those checks run on their real systems, the only accurate status is
`fixture-verified-unpublished` locally and production blocked.
