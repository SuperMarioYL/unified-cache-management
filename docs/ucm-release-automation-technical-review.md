# UCM release automation technical review

## Review result

The feature branch now has a real, exact-six hosted build loop. At source SHA
[`b9de1b3a29ae094e4c6d3895b0b642e92aa8ab42`](https://github.com/SuperMarioYL/unified-cache-management/commit/b9de1b3a29ae094e4c6d3895b0b642e92aa8ab42),
[Push Commit Checks run 31329098122](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098122)
and both [Release run 31329098205 attempt 1](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/attempts/1)
and [attempt 2](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/attempts/2)
completed successfully.

Each release attempt produced 15 Actions Artifacts:

- six real native wheel artifacts;
- one deterministic Helm Chart artifact;
- six real install-only image compact artifacts;
- one image aggregate containing three dual-architecture family plans;
- one final six-wheel, six-image, and Chart aggregate.

This review accepts that hosted feature functionality. It does not approve a
production release. All image results are `real-verified-unpublished`, and the
aggregate records publication as `{status: blocked, attempted: false}`. The
downloaded identity comparison passed across all 15 artifact groups. It matched
all six wheel bytes, the Chart, all six compact image identities, three family
plans, both aggregates, and the feature second-zero result. The only excluded
field in the aggregate envelopes was the expected `github.run_attempt`.

## Reviewed architecture

| Surface | Reviewed role |
| --- | --- |
| `_build-wheel.yml` | Validate `source_sha` and one of six `spec_id` values before checkout; build on the matching native CPU runner; seal, reopen, inspect, and upload one native wheel |
| `_build-image.yml` | Reopen the exact same-run wheel; verify pinned upstream index/member/config bytes; prepare the allowlisted offline context; build and fully scan a local OCI member; upload compact evidence |
| `release-vllm-images.yml` | Run the six-member matrix, enforce the six-of-six barrier, reopen every wheel/image pair, form three dual-architecture plans, and require the feature inventory to return zero new tasks |
| `release-ucm.yml` | Project the reviewed matrix, build six wheels, package the Chart, call the image loop, enforce the final barrier, and aggregate all evidence |
| `.github/release/ucm_release` | Own strict configuration, source, wheel, ELF/dependency, image, Registry-plan, and aggregate identities |
| `charts/ucm` | Produce the deterministic product Chart and bind `SOURCE_PROVENANCE.json` |

The four workflows remain orchestration. The implementation is concentrated in
eight Python modules, three JSON Schemas, four Docker files, and two YAML
configuration files under `.github/release`. Feature jobs use `contents: read`;
their only intended remote output is Actions Artifact upload.

## Exact-six matrix and wheel result

The feature path does not use the old 36-item unresolved manifest or a fixture
wheel. `core hosted-matrix` projects exactly these six reviewed tasks:

| Profile | Platform | Wheel filename |
| --- | --- | --- |
| `cuda130` | `linux/amd64` | `uc_manager-0.5.0rc1+cuda130-cp312-cp312-manylinux_2_28_x86_64.whl` |
| `cuda130` | `linux/arm64` | `uc_manager-0.5.0rc1+cuda130-cp312-cp312-manylinux_2_28_aarch64.whl` |
| `cann900-a2` | `linux/amd64` | `uc_manager-0.5.0rc1+cann900.a2-cp312-cp312-linux_x86_64.whl` |
| `cann900-a2` | `linux/arm64` | `uc_manager-0.5.0rc1+cann900.a2-cp312-cp312-linux_aarch64.whl` |
| `cann900-a3` | `linux/amd64` | `uc_manager-0.5.0rc1+cann900.a3-cp312-cp312-linux_x86_64.whl` |
| `cann900-a3` | `linux/arm64` | `uc_manager-0.5.0rc1+cann900.a3-cp312-cp312-linux_aarch64.whl` |

The CUDA builders emit `manylinux_2_28`; the CANN builders emit `linux`.
`ubuntu-24.04` handles amd64 and `ubuntu-24.04-arm` handles arm64. Each job
requires at least 60 GiB after runner cleanup, creates the checksum-pinned
Buildx/BuildKit builder, and builds from the canonical source context. The
wheel build has a maximum of three attempts on the same builder and removes
partial output before another attempt. It uploads the real wheel only after all
of these records agree:

- source commit, tree, archive, and context digest;
- reviewed task and immutable builder coordinate;
- wheel seal, reopened inspection, and byte SHA256;
- required/forbidden native members, ELF machine, `DT_NEEDED`, dependency
  closure, and unresolved dependency policy;
- exact `Requires-Dist: wrapt==1.17.2`.

CANN's `libascend_hal.so` is recorded as a resolved
`kind=external-required` transitive host-driver dependency, not as a bundled or
policy-accepted unresolved library.

Both hosted attempts passed all six wheel jobs on their first build attempt and
uploaded all six wheel artifacts. The retry branch is dynamically shell-tested,
but this hosted run is not retry-recovery evidence. The wheel results are real
native artifact evidence, but remain unpublished and are not GitHub Release or
PyPI assets.

## Image result and family aggregation

Each image job downloads its same-run wheel artifact and independently reopens
it. The job then reopens the fixed upstream index, architecture-specific
manifest, and config by digest. The three base families are:

| Family | Upstream base | Platforms | Planned unpublished target |
| --- | --- | --- | --- |
| CUDA 13.0 | `docker.io/vllm/vllm-openai:v0.21.0` | amd64, arm64 | `ghcr.io/supermarioyl/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1` |
| CANN 9.0.0 A2 | `quay.io/ascend/vllm-ascend:v0.22.1rc1` | amd64, arm64 | `ghcr.io/supermarioyl/vllm-ascend:v0.22.1rc1-ucm-0.5.0rc1-r1` |
| CANN 9.0.0 A3 | `quay.io/ascend/vllm-ascend:v0.22.1rc1-a3` | amd64, arm64 | `ghcr.io/supermarioyl/vllm-ascend:v0.22.1rc1-a3-ucm-0.5.0rc1-r1` |

The planned target names are identity inputs only. Nothing was pushed to those
repositories.

The real context contains nine allowlisted files: the Dockerfile, three
helpers, the UCM wheel, the architecture-specific locked `wrapt` wheel,
`requirements.lock`, `image-recipe.json`, and `image-authority.json`. It does
not contain UCM source or build UCM again. The image installs from the offline
wheelhouse and requires these gates to pass:

- base descriptor chain;
- exact wheel and native-member evidence;
- install and dependency check;
- direct URL and exact installed versions;
- `import ucm` and `import wrapt`;
- Python ABI, ELF, and dependency closure.

Buildx writes a local OCI archive with provenance and SBOM disabled and
timestamp rewriting enabled. The verifier streams every descriptor and layer,
checks the manifest/config/diff-ID closure, writes compact descriptor evidence,
and removes the large archive before artifact upload. Each accepted result has
`fixture_only=false`, `unpublished=true`, `publication_attempted=false`, and
`status=real-verified-unpublished`.

All six image jobs passed. The image aggregate then required exactly two
members per family, ordered as `linux/amd64` and `linux/arm64`, and emitted
three unpublished index plans.

## Hosted evidence

### Current two-attempt result

| Evidence | Observed value |
| --- | --- |
| Source SHA | `b9de1b3a29ae094e4c6d3895b0b642e92aa8ab42` |
| Push workflow | [Run 31329098122](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098122), success |
| Release workflow | [Attempt 1](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/attempts/1) and [attempt 2](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/attempts/2), both success |
| Artifact count | Each attempt: 15 = 6 wheels + 1 Chart + 6 compact images + 1 image aggregate + 1 final aggregate |
| Wheel/image matrix | 6/6 wheels and 6/6 images passed |
| Image families | 3/3, each with amd64 and arm64 |
| Downloaded freeze | Each attempt: 15 artifact directories and 213 artifact files |
| Final payload SHA256 | Both attempts: `sha256:88596b412798e34a037132320044d47283c1bfb9001eab20236f65ad44bcac1b` |
| Image aggregate payload SHA256 | Both attempts: `sha256:dd2c17b710ddd01b7e836b1dbc25fac866e82a7512d43cbd3e734f083b8a7b37` |
| Chart package SHA256 | `4805117c69725d1ce093096ba6d5fcf46c4b2a7ff716544e993f5b87bedfefc6` |
| Chart release-tree SHA256 | `6e0ea559cc946593ef162d8ea40497c05091466a543c8b997a2ecb0da22edb6f` |
| Publication | `{status: blocked, attempted: false}` |

Artifacts are retained for three days and rerunning the same run replaces the
current API artifact listing. Therefore artifact IDs from the first attempt are
not treated as durable links. Use the release [operator readback](../.github/release/README.md#artifact-names-and-readback)
to enumerate and download the latest attempt's artifacts.

### Same-SHA identity comparison

The frozen attempt-1 and attempt-2 artifact sets produced this result:

| Surface | Same-SHA comparison |
| --- | --- |
| Six wheel bytes, inspections, seals, tasks, and source/toolchain identities | Exact match |
| Chart tgz and `chart-result.json` | Exact match |
| Six image archive checksums | Exact match |
| Six OCI manifests, configs, layers, diff IDs, and compact closures | Exact match |
| Six image content identities, results, authorities, and recipes | Exact match |
| Three family plans, candidate inventory, and `second_reconcile` | Exact match |
| Image aggregate payload | Exact match: `sha256:dd2c17b710ddd01b7e836b1dbc25fac866e82a7512d43cbd3e734f083b8a7b37` |
| Final aggregate payload | Exact match: `sha256:88596b412798e34a037132320044d47283c1bfb9001eab20236f65ad44bcac1b` |

The comparator treats logs, disk telemetry, and BuildKit session metadata as
diagnostics rather than release identities. Within the canonical identity set,
every comparison passed; only `github.run_attempt` was excluded from the two
aggregate envelopes.

### Latest attempt artifact links

The current run API lists the attempt-2 artifacts below. These links are
temporary because the workflow retains artifacts for three days and a later
rerun replaces their IDs.

| Task | Wheel artifact | Image artifact |
| --- | --- | --- |
| `cuda130-amd64` | [wheel](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042701932) | [image](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042815911) |
| `cuda130-arm64` | [wheel](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042681509) | [image](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042797559) |
| `cann900-a2-amd64` | [wheel](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042710295) | [image](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042808225) |
| `cann900-a2-arm64` | [wheel](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042682185) | [image](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042797618) |
| `cann900-a3-amd64` | [wheel](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042721466) | [image](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042828168) |
| `cann900-a3-arm64` | [wheel](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042686915) | [image](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042788092) |

The remaining artifacts are the [Chart](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042724281),
[image aggregate](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042832261),
and [final aggregate](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042839761).

## Meaning of the second zero-task result

`second_reconcile.task_count == 0` is a strict feature-internal recomputation:

1. reopen all six wheel artifacts and six compact image artifacts;
2. require their tasks and source SHA to equal the reviewed matrix;
3. construct three dual-architecture unpublished family plans;
4. place those exact plans in a deterministic candidate inventory;
5. recompute the expected task set and require it to be empty.

This proves closure of the same-run feature artifact list. It is not a scan of
the target GHCR repositories, a publication digest readback, an existing public
OCI index, or idempotence of a publication path. Image jobs do read pinned
upstream descriptors, but perform no target-GHCR login or push.

The result was structurally identical and zero in both current attempts. That
same-run closure is necessary, but the independent cross-attempt comparison is
what proves repeatable artifact identities. The historical failure below shows
why the two claims must remain separate.

## Historical determinism failure and repair

At source SHA `166e0f474a3adab88917d65b7af61ea948f7492c`, both attempts of
[run 31324468754](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31324468754)
completed successfully. Six wheel identities and the Chart matched, but all six
image manifest/content identities changed, which changed all three family
plans and both aggregate payloads. The same-run second reconcile was still zero
in both attempts; it did not detect that cross-attempt drift.

The common source was the generated `/var/cache/ldconfig/aux-cache` in both
runtime stages. Commit
[`ea931a95c231835a4bb4af353821084af9b998e6`](https://github.com/SuperMarioYL/unified-cache-management/commit/ea931a95c231835a4bb4af353821084af9b998e6)
removes the cache in the same layer as `ldconfig`. The later bounded wheel-build
retry is a transfer-resilience change: neither attempt in the current hosted
run retried, so recovery evidence remains the dynamic shell test rather than a
hosted retry event.

## Historical fixture baseline

The earlier [Release run 31260552670](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31260552670)
at source SHA `0ee113433b75868142d86c66ee0cda2af533cc89` remains historical
fixture evidence. It built one synthetic wheel and one local amd64 OCI archive,
and its fixture reconciliation returned zero tasks. It was useful for testing
the contract shape, but it is not the current exact-six hosted result and does
not supply native wheel, dual-architecture image, Registry, or device evidence.

## Remaining external work

The following are still pending or `external-required`:

- a protected production Tag path and explicit publication authorization;
- GHCR login, member/index push, and authenticated plus anonymous digest
  readback;
- GitHub prerelease creation and Release asset download verification;
- CUDA, Ascend A2, and Ascend A3 runtime/device validation on matching hardware;
- cluster installation and workload acceptance;
- formal tag/release publication and stable-release policy.

The current accurate status is: real hosted wheels and real hosted install-only
image candidates exist as temporary Actions Artifacts; nothing has been
published.
