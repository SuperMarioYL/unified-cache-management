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
  push, dispatch, or repository mutation.
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

Task 5 also produced two byte-identical current-input, no-cache OCI archives at
SHA256 `199b53854f9bee4a7d81a32d2a046d7de220356c35d900297d400fa65059731a`.
Its final local sequence recorded a clean Python 3.12 suite at 185 tests, a
9-case round-6 contract-order batch, and a clean full suite at 187 tests, with
pre-commit and actionlint green.
Task 6 did not change the Docker, wheel, base, or toolchain inputs, so that
historical local determinism evidence remains applicable. It is not evidence of
a Registry write or accelerator runtime.

Repository-wide Unit collection is not reported as passing. This host stops at
`test/conftest.py:14` because the external `pynvml` module is unavailable.

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
