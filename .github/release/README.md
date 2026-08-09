# Compact UCM release automation

This directory owns the repository release contracts and the unified
`python -m ucm_release` implementation. Real wheels and image candidates are
built by GitHub Actions; they are not prepared or uploaded from a developer
machine.

## Current feature-candidate result

The exact-six hosted path and its same-SHA determinism check completed at source
commit [`b9de1b3a29ae094e4c6d3895b0b642e92aa8ab42`](https://github.com/SuperMarioYL/unified-cache-management/commit/b9de1b3a29ae094e4c6d3895b0b642e92aa8ab42):

- [Push Commit Checks run 31329098122](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098122)
  succeeded.
- [Release UCM core artifacts run 31329098205, attempt 1](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/attempts/1)
  and [attempt 2](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/attempts/2)
  both succeeded. Each produced 15 Actions Artifacts: six real wheels, one
  Chart, six real compact image artifacts, one three-family image aggregate,
  and one final evidence aggregate.
- All six wheel jobs and all six image jobs passed on native GitHub-hosted
  `amd64` or `arm64` runners. The image aggregate contains three families, each
  with `linux/amd64` and `linux/arm64` members.
- Every image result is `real-verified-unpublished`. Publication is
  `{status: blocked, attempted: false}`.
- A frozen, downloaded comparison of all 15 artifacts passed across the two
  attempts. The six wheel bytes, Chart package/result, six image archive
  checksums and compact OCI identities, three family plans, candidate
  inventory, second reconcile, image aggregate payload, and final payload are
  exact. Only the expected `github.run_attempt` envelope field was excluded.

This is real hosted build evidence, not a release. The workflows have no GHCR
login or push, create no Git tag or GitHub Release, and do not write upstream.
Registry publication/readback, matching CUDA/A2/A3 device execution, cluster
acceptance, and protected Tag production remain open. Repository contents stay
read-only; the intended remote output of this feature lane is the temporary
Actions Artifact upload.

## Layout

| Path | Role |
| --- | --- |
| `release.yaml` | Version, exact six reviewed tasks across three profiles, immutable builders, upstream images, dependency locks, Chart, and runner mapping |
| `compatibility.yaml` | Accepted CUDA/CANN, A2/A3, OS, CPU, ABI, and upstream-channel rules |
| `ucm_release/` | Strict configuration, wheel, Chart, Registry, image, and aggregation implementation |
| `schemas/` | Configuration, release-manifest, and image-result contracts |
| `docker/` | Multi-stage wheel/image Dockerfile and three verification helpers |
| `tests/` | Structural, behavioral, mutation, workflow, and loop checks |
| `charts/ucm` | Product Helm Chart with source provenance |

The four release workflows are:

| Workflow | Current feature responsibility |
| --- | --- |
| `_build-wheel.yml` | Build, seal, reopen, inspect, and upload one reviewed native wheel |
| `_build-image.yml` | Reopen the same-run wheel, verify the pinned upstream base, install UCM and locked `wrapt`, build a local OCI archive, fully scan it, delete the large archive, and upload compact evidence |
| `release-vllm-images.yml` | Build all six image members, enforce a six-of-six barrier, form three dual-architecture candidate plans, and recompute the feature-only zero-task result |
| `release-ucm.yml` | Project the exact six tasks, build all wheels, package the Chart, invoke the image workflow, enforce the final barrier, and upload the final aggregate |

All feature jobs use `contents: read`. A `v*` or non-fork invocation enters the
explicit blocked job; the current implementation does not publish a Tag lane.

## Exact six matrix

| Family | Task IDs | Wheel tag | Hosted runner |
| --- | --- | --- | --- |
| CUDA 13.0 | `cuda130-amd64`, `cuda130-arm64` | `cp312-cp312-manylinux_2_28_x86_64` / `cp312-cp312-manylinux_2_28_aarch64` | `ubuntu-24.04` / `ubuntu-24.04-arm` |
| CANN 9.0.0 A2 | `cann900-a2-amd64`, `cann900-a2-arm64` | `cp312-cp312-linux_x86_64` / `cp312-cp312-linux_aarch64` | `ubuntu-24.04` / `ubuntu-24.04-arm` |
| CANN 9.0.0 A3 | `cann900-a3-amd64`, `cann900-a3-arm64` | `cp312-cp312-linux_x86_64` / `cp312-cp312-linux_aarch64` | `ubuntu-24.04` / `ubuntu-24.04-arm` |

The wheel versions are `0.5.0rc1+cuda130`,
`0.5.0rc1+cann900.a2`, and `0.5.0rc1+cann900.a3`. Each wheel artifact contains
the `.whl`, its canonical inspection and seal, source-context records, the
resolved task/toolchain authority, disk evidence, and the native build log.
CANN's `libascend_hal.so` record remains a structured
`kind=external-required` host-driver dependency; it is not bundled or treated
as an unresolved dependency.

The image context is install-only. It contains the Dockerfile, three helpers,
the exact UCM wheel, the architecture-specific `wrapt==1.17.2` wheel,
`requirements.lock`, `image-recipe.json`, and `image-authority.json`; it does
not contain the UCM source or compile UCM again. The full OCI archive is scanned
inside its build job and then removed. The uploaded compact artifact keeps the
index, manifest, config, descriptor/diff-ID closure, build records, and logs,
but not the large layer blobs.

## Artifact names and readback

The latest successful attempt exposes these artifact groups on the release run
page:

- `ucm-wheel-<spec-id>-<source-sha>`: six artifacts;
- `ucm-chart-<source-sha>`: one artifact;
- `ucm-image-<spec-id>-<source-sha>`: six artifacts;
- `ucm-real-images-<source-sha>`: one image-family aggregate;
- `release-loop-evidence-<source-sha>`: one final aggregate.

Direct links for the latest attempt are the [Chart](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042724281),
[image aggregate](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042832261),
and [final aggregate](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042839761).
Use the API below for the six wheel and six image links because every rerun
replaces the current artifact IDs.

Artifacts have the workflow's three-day retention. Artifact IDs are
attempt-specific, so use the current run API instead of copying an old ID from
a prior attempt:

```bash
set -euo pipefail

readonly REPOSITORY=SuperMarioYL/unified-cache-management
readonly RUN_ID=31329098205
readonly SOURCE_SHA=b9de1b3a29ae094e4c6d3895b0b642e92aa8ab42

gh api "repos/${REPOSITORY}/actions/runs/${RUN_ID}" \
  --jq '{head_sha,run_attempt,status,conclusion,html_url}'
gh api "repos/${REPOSITORY}/actions/runs/${RUN_ID}/artifacts" \
  --jq '.artifacts[] | [.name,.digest,.expired,
    "https://github.com/'"${REPOSITORY}"'/actions/runs/'"${RUN_ID}"'/artifacts/\(.id)"] | @tsv'

artifact_root="$(mktemp -d)"
gh run download "${RUN_ID}" --repo "${REPOSITORY}" \
  --name "release-loop-evidence-${SOURCE_SHA}" --dir "${artifact_root}/final"
gh run download "${RUN_ID}" --repo "${REPOSITORY}" \
  --name "ucm-real-images-${SOURCE_SHA}" --dir "${artifact_root}/images"

final_evidence="${artifact_root}/final/release-loop-evidence.json"
image_evidence="${artifact_root}/images/real-image-loop-evidence.json"

jq -e --arg sha "${SOURCE_SHA}" '
  .payload.source_sha == $sha and
  (.payload.wheels | length) == 6 and
  (.payload.images | length) == 6 and
  (.payload.families | length) == 3 and
  .payload.second_reconcile.task_count == 0 and
  .payload.publication == {status:"blocked",attempted:false}
' "${final_evidence}" >/dev/null

jq -e --arg sha "${SOURCE_SHA}" '
  .payload.source_sha == $sha and
  (.payload.wheels | length) == 6 and
  (.payload.images | length) == 6 and
  (.payload.families | length) == 3 and
  .payload.second_reconcile.task_count == 0 and
  .payload.publication == {status:"blocked",attempted:false}
' "${image_evidence}" >/dev/null
```

Both attempts have final payload SHA256
`sha256:88596b412798e34a037132320044d47283c1bfb9001eab20236f65ad44bcac1b`
and image aggregate payload SHA256
`sha256:dd2c17b710ddd01b7e836b1dbc25fac866e82a7512d43cbd3e734f083b8a7b37`.
The Chart package SHA256 is
`4805117c69725d1ce093096ba6d5fcf46c4b2a7ff716544e993f5b87bedfefc6`,
and its release-tree SHA256 is
`6e0ea559cc946593ef162d8ea40497c05091466a543c8b997a2ecb0da22edb6f`.

The frozen comparison also byte-matched all six wheels and the Chart archive.
For every image it matched the archive checksum, OCI manifest, config, layers,
diff IDs, compact closure, content identity, result, authority, and recipe. All
three family plans, the candidate inventory, and the zero-task second reconcile
matched as well.

## What `second_reconcile.task_count == 0` means

The image aggregate first reopens the exact six same-run wheel and image
artifacts and creates three unpublished family plans. It then places those
plans into a deterministic feature candidate inventory and recomputes the
strict expected task list. Zero means that this closed, exact-six inventory
contains no missing member or family.

It is not a target-GHCR query, publication digest readback, or proof that a
public tag already exists. Image jobs do read and verify the pinned upstream
base descriptors; no target-GHCR write or publication readback occurs.

The same-SHA claim comes from the separate strict attempt-1/attempt-2 artifact
comparison, not from the second-zero value alone.

## Historical determinism failure and fix

The earlier [run 31324468754](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31324468754)
at `166e0f474a3adab88917d65b7af61ea948f7492c` remains useful negative
evidence. Both attempts completed, and their wheels and Chart matched, but all
six image identities drifted. The shared cause was the generated
`/var/cache/ldconfig/aux-cache` in both runtime stages. Commit
[`ea931a95c231835a4bb4af353821084af9b998e6`](https://github.com/SuperMarioYL/unified-cache-management/commit/ea931a95c231835a4bb4af353821084af9b998e6)
removes that cache in the same layer that runs `ldconfig`; the new two-attempt
comparison is the hosted proof that the image identities now repeat.

The later bounded wheel-build retry improves transient transfer resilience,
but neither attempt of run 31329098205 retried. Retry recovery is covered by a
dynamic shell test, not by this hosted run.

## Unified CLI and verification

Run contract checks from the repository root:

```bash
export PYTHONPATH=.github/release
python -m ucm_release config validate
python -m ucm_release core tag-preflight --lane feature-candidate
python -m ucm_release core hosted-matrix --help
python -m ucm_release wheel context --help
python -m ucm_release wheel inspect --help
python -m ucm_release chart package --help
python -m ucm_release image real-authorities --help
python -m ucm_release image prepare-real --help
python -m ucm_release image verify --help
python -m ucm_release loop aggregate-real --help
```

```bash
pytest -q .github/release/tests
ruff check .github/release/ucm_release .github/release/tests .github/release/docker
black --check .github/release/ucm_release .github/release/tests .github/release/docker
pre-commit run actionlint --all-files --hook-stage manual
```

These commands validate contracts; they do not replace the hosted wheel/image
jobs. Historical fixture-only runs remain useful regression baselines and are
documented in the [technical review](../../docs/ucm-release-automation-technical-review.md#historical-fixture-baseline),
but they are not the current feature result.

## Remaining release work

The feature build and same-SHA artifact comparison are complete. Production
still requires a separately authorized protected Tag path, Registry write and
digest readback, a GitHub prerelease and asset readback, matching CUDA/A2/A3
runtime/device evidence, and cluster workload acceptance. Until those events
occur, keep all six images unpublished and do not describe this feature run as
a formal release.
