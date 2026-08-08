# UCM release automation technical review

## Review result

The repository now has one compact, fail-closed release candidate loop. Four
release workflows orchestrate eight Python modules under `.github/release`,
three JSON Schemas, four install-image files, and the product Chart at
`charts/ucm`. The feature/fork path is fixture-only, read-only, and unpublished.
It has now run successfully on GitHub Actions at source commit
`0ee113433b75868142d86c66ee0cda2af533cc89`. The production path remains
intentionally blocked until external infrastructure and real artifacts exist.
This hosted result is attempt 1 only; the final same-SHA attempt-2 comparison
is still pending.

This review accepts the local and hosted fork-candidate architecture. It does
not approve a production release or Registry publication.

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
| Complete compact release suite | `190 passed in 55.84s` |
| Workflow policy suite | `84 passed` |
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
Buildx, the authority-created builder, and authority-pinned BuildKit v0.18.2
then ran in the first hosted Task 7 candidate baseline described below. That
hosted run is stronger evidence than the two local history items, but still
produced only a local unpublished OCI archive.

The final local sequence now records 190 compact tests and 84 workflow-policy
tests, with pre-commit and actionlint green. Neither local OCI history item is
evidence of a Registry write or accelerator runtime.

Repository-wide Unit collection is not reported as passing. This host stops at
`test/conftest.py:14` because the external `pynvml` module is unavailable.

## Task 7 hosted GitHub loop

The executable continuation is governed by the tracked [Task 7 GitHub Loop
plan](superpowers/plans/2026-08-08-ucm-release-slimming-loop.md). Execute the
single fail-closed [canonical operator
script](../.github/release/README.md#task-7-hosted-github-loop); this review does
not copy it because a second command body would create drift.

The current token's owner package-list probes returned HTTP 403 without
`read:packages`, and anonymous reads of both known target GHCR repositories
returned `DENIED`. Each before/after phase reprobes these surfaces. Successful
package reads are normalized; only explicit HTTP 403/`read:packages` failures
become `UNAVAILABLE`, while any other API failure stops the script. The
procedure does not request broader authentication, and an explicitly
unavailable package or target Registry read does not block the authorized
branch push.

The exact execution and acceptance steps are:

1. Run the canonical script with `TASK7_DRY_RUN=1`. It must validate the exact
   branch/HEAD, `SuperMarioYL` login, allowlisted origin push URL, exact upstream
   URL and `HEAD`, download crane v0.20.3 with the platform's hard-coded SHA256,
   prove every release job has only `contents: read`, and stop immediately
   before the push.
2. Run the same script without dry-run. Its only push is
   `git push origin HEAD:refs/heads/feature/cicd`.
3. Discover runs with `gh run list --repo "$REPOSITORY" --commit "$SOURCE_SHA"
   --event push --json
   databaseId,workflowName,status,conclusion,headSha,url`. Local `jq` selection
   must find exactly one `Push Commit Checks` and one
   `Release UCM core artifacts` entry, both with the exact `headSha`; it never
   depends on the workflow being registered on the fork's default branch.
4. Require both initial attempt-1 runs to succeed, download the release
   evidence, invoke same-ID `gh run rerun`, require attempt 2, and download the
   second evidence tree.
5. For both evidence files, assert `payload.source_sha`, `payload.repository`,
   `payload.ref`, and `payload.workflow_refs`, then compare payload, wheel,
   Chart, local OCI, and second-zero-reconcile identities. Both operation
   ledgers must have `write_count=0` and publication blocked.
6. Diff only the surfaces actually read: fork PRs, tags, GitHub Releases, both
   owner package endpoints' normalized list/state, upstream `HEAD`, and
   known-target GHCR tags/digests when readable. An `UNAVAILABLE` package or
   Registry state is an external gap, not readback evidence.

The no-GHCR-write conclusion is instead supported by the static absence of
`packages: write`, login, and Registry push commands plus the runtime
zero-operation ledgers. Failures require `gh run view --log-failed`, run/job
JSON, and artifacts under the bounded plan. Production remains blocked.

### First hosted candidate baseline

The first complete hosted candidate baseline ran on 2026-08-08 at source SHA
`0ee113433b75868142d86c66ee0cda2af533cc89`:

- [Push Commit Checks run 31260552571](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31260552571)
  completed successfully with Python/release tests, C++ tests, and both linters
  green.
- [Release UCM core artifacts run 31260552670](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31260552670)
  completed successfully. The fixture wheel, Chart package, first reconcile,
  install-only image build, final reconcile, and aggregate jobs ran; invalid,
  standalone, and production-only routes were skipped by policy.

The downloaded attempt-1 evidence file has SHA256
`33a82f806d8ec43dbe55da887371b8651454baf8d1442324de0ab5a731ef7ec6`.
Its deterministic payload SHA256 is
`02b5b2e174e6144f889a811f1571017897928546aaa3aec447e377687f04fe9f`.
The evidence binds the exact source, fork repository, feature ref, and all four
release workflow names. Its key artifact identities are:

| Artifact or closure | SHA256 |
| --- | --- |
| Fixture wheel | `08c4aa98ebb0cc5e0816619bda78d310fc5342da7fbec3611a70b2d5be76f19b` |
| Deterministic Chart package | `4805117c69725d1ce093096ba6d5fcf46c4b2a7ff716544e993f5b87bedfefc6` |
| Chart release tree | `6e0ea559cc946593ef162d8ea40497c05091466a543c8b997a2ecb0da22edb6f` |
| Local OCI manifest | `7dfefdca3e4fb2f3e9fc8ca1efa55c3bbdf7ddd22a4584ec5b5a0af2afcd9a24` |
| OCI stable closure | `db3e37c1967ceb17c63af196f93d19f28a4dd5f85d0d57872604142dcf779c48` |
| Image result | `c7c00959d6bacd6bcdba2e284c3a2da16d2dc5bb10a4406149b2b61da53e556e` |
| Second reconcile | `29b1b9a6b6b3b62edc56f162d56cbfad0b9663ae20eb35aa5bb3bfcd05df5bdf` |

The four required checks—fixture wheel, CUDA/A2/A3 Helm validation,
install-only image, and second-reconcile zero—passed, as did all six
deterministic scenarios. The second reconcile returned
`decision=already-present`, `task_count=0`, and an empty task list. All eight
install/ABI/import gates passed; runtime and device validation remained
`external-required`. The image was a local unpublished `linux/amd64` OCI
result, while the two-platform count in the scenario record is fixture
contract evidence rather than a multi-platform image build.

The operation ledger contained ten read/plan operations, no write-capable
operation, `write_count=0`, and publication `{status: blocked, attempted:
false}`. A fresh after-snapshot was byte-identical to the before-snapshot for
fork PRs, tags, Releases, and upstream `HEAD`; package endpoints remained HTTP
403 and both target GHCR anonymous reads remained `DENIED`, so those four
surfaces stay explicitly `UNAVAILABLE` rather than being treated as empty.

This baseline does not yet satisfy the final same-SHA rerun acceptance step;
that comparison is intentionally performed only after this evidence is
recorded in the documents and the resulting documentation commit is pushed.

## External blockers

Hosted fork-candidate execution and artifact download have now run. The
following production capabilities remain unverified and `external-required`:

- protected production environment and publication authorization;
- resolved builders, toolchains, package locks, and production runners for all
  36 wheel specifications;
- native ELF/custom-op wheel inspection from those builders;
- OCI Registry credentials, write, and digest readback;
- CUDA and CANN runtime checks on matching CUDA, A2, and A3 devices;
- cluster installation and workload acceptance;
- formal publication.

The accurate current status is a hosted fork-candidate attempt 1 with a local,
unpublished, fixture-only OCI result; final same-SHA rerun acceptance is still
pending, and production remains blocked.
