# UCM release automation detailed design

## 1. Scope and invariants

The current implementation builds real UCM wheels and install-only vLLM image
candidates in GitHub Actions. A developer machine runs contract checks only; it
does not prebuild or upload release artifacts.

The checked-in release surface remains compact:

- four release workflows;
- eight `ucm_release` Python modules;
- three JSON Schemas;
- one multi-stage Dockerfile and three verification helpers;
- two YAML configuration files;
- one product Chart at `charts/ucm`.

The feature lane is real-build, read-only, and unpublished. It has exactly six
reviewed tasks and uses native GitHub-hosted x64/ARM64 runners. The current
`v*` route is explicitly blocked; Registry publication, GitHub Release
creation, hardware validation, and cluster acceptance are outside the completed
feature path.

All feature jobs use `contents: read`. Their only intended remote output is the
temporary Actions Artifact upload.

The words `candidate` and `eligible` mean that a task has complete build
authority. They do not mean `published`.

## 2. Workflow topology

### 2.1 `release-ucm.yml`

This is the feature push entry point.

1. Reject non-fork, non-feature, or Tag publication invocations through the
   explicit blocked job.
2. Check out the exact `github.sha`, run feature preflight, and project the
   canonical six-task matrix.
3. Call `_build-wheel.yml` once for each `spec_id`, with `fail-fast: false`.
4. Continue only after all six wheel calls succeed.
5. Lint, template, and deterministically package the Chart.
6. Call `release-vllm-images.yml` with the same full source SHA.
7. Require plan, six wheels, Chart, and all image work to be successful.
8. Download the six wheel artifacts, six image artifacts, and Chart; reopen the
   complete closure; upload the final aggregate.

The workflow does not publish a Registry package and does not create a GitHub
Release. The final Actions Artifact is named
`release-loop-evidence-<source-sha>`.

### 2.2 `_build-wheel.yml`

The reusable interface is the exact pair `source_sha` and `spec_id`. The first
step accepts only a full lowercase SHA and one of:

```text
cuda130-amd64
cuda130-arm64
cann900-a2-amd64
cann900-a2-arm64
cann900-a3-amd64
cann900-a3-arm64
```

The CPU suffix selects `ubuntu-24.04` or `ubuntu-24.04-arm`. Each job records
disk state, runs the pinned cleanup action, and requires at least 60 GiB free
before pulling the immutable builder.

The job then:

1. derives a canonical source context from the exact source commit;
2. reprojects the single reviewed hosted task and checks its task digest;
3. installs checksum-pinned Buildx v0.19.2 and digest-pinned BuildKit v0.18.2;
4. builds the `wheel-cuda` or `wheel-cann` target in the immutable builder,
   with at most three attempts on that same builder and partial-output cleanup
   between attempts;
5. requires one wheel and a matching `wheel-seal.json`;
6. reopens the wheel through `wheel inspect --source-kind builder-candidate`;
7. byte-compares the original and reopened inspection;
8. uploads the wheel plus source, task, toolchain, disk, and build-log evidence.

The artifact is `ucm-wheel-<spec-id>-<source-sha>`.

### 2.3 `_build-image.yml`

This reusable workflow has the same `source_sha` and `spec_id` interface and
uses the same native CPU runner selection. It:

1. downloads the exact same-run wheel artifact;
2. recomputes and compares its inspection;
3. installs the pinned Buildx/BuildKit and read-only crane tools;
4. fetches the configured upstream index, member manifest, and config by
   digest, then hashes the returned raw bytes;
5. downloads the exact architecture-specific `wrapt==1.17.2` wheel and checks
   its SHA256;
6. prepares the nine-file offline install context;
7. builds one local OCI member for the exact target platform;
8. streams and verifies the complete OCI archive, saves compact descriptor
   evidence, and removes the large archive;
9. uploads `ucm-image-<spec-id>-<source-sha>`.

No image job logs in to a Registry or pushes a blob, member, index, or tag.

### 2.4 `release-vllm-images.yml`

This workflow reprojects the same six tasks, calls `_build-image.yml` six
times, and places a mandatory matrix barrier after the calls. Only a full six
of six result reaches aggregation.

The aggregate job downloads all six wheels and all six compact images. It
reopens each source/task/wheel/image closure, forms three exact dual-architecture
plans, constructs their candidate inventory, emits the deterministic zero marker
after exact-six closure validation, and uploads `ucm-real-images-<source-sha>`.

## 3. Configuration and identity

### 3.1 Version authority

`version.ini` supplies `VLLM_UC_VERSION=0.5.0rc1`. Configuration validation
requires agreement with `release.yaml`, `compatibility.yaml`, `setup.py`, and
Chart `appVersion`. The Helm SemVer is `0.5.0-rc.1`.

The wheel profiles use controlled PEP 440 local versions:

| Profile | Wheel version | Wheel platform |
| --- | --- | --- |
| `cuda130` | `0.5.0rc1+cuda130` | `manylinux_2_28` |
| `cann900-a2` | `0.5.0rc1+cann900.a2` | `linux` |
| `cann900-a3` | `0.5.0rc1+cann900.a3` | `linux` |

### 3.2 Reviewed matrix

`core plan` currently reports six declared wheel specifications and six build
eligible specifications. Each entry resolves:

- native hosted runner identity;
- immutable builder index/member/config coordinate;
- Python 3.12/`cp312` build dependency lock;
- required and forbidden native members;
- allowed `DT_NEEDED` and external-required dependency policy;
- upstream image family and target identity inputs.

The six wheel filenames are:

```text
uc_manager-0.5.0rc1+cuda130-cp312-cp312-manylinux_2_28_x86_64.whl
uc_manager-0.5.0rc1+cuda130-cp312-cp312-manylinux_2_28_aarch64.whl
uc_manager-0.5.0rc1+cann900.a2-cp312-cp312-linux_x86_64.whl
uc_manager-0.5.0rc1+cann900.a2-cp312-cp312-linux_aarch64.whl
uc_manager-0.5.0rc1+cann900.a3-cp312-cp312-linux_x86_64.whl
uc_manager-0.5.0rc1+cann900.a3-cp312-cp312-linux_aarch64.whl
```

### 3.3 Source and wheel authority

The source context binds the commit, Git tree, deterministic archive SHA256,
source-context digest, and `SOURCE_DATE_EPOCH`. The build key additionally
binds the profile, CPU, builder, dependency lock, required/forbidden targets,
and tool wheels.

Wheel inspection requires complete RECORD coverage and verifies every native
member. It records ELF machine values, direct `DT_NEEDED`, and the resolved
dependency closure, and requires every unresolved-dependency list to be empty.
CUDA has no declared external-required dependency. CANN allows
`libascend_hal.so` only as a structured, resolved
`kind=external-required` transitive device-runtime dependency supplied by the
host Ascend driver.

The seal and reopened inspection bind the exact wheel bytes. A wheel that was
built but did not pass seal, reopen, and artifact upload is not counted among
the six results.

## 4. Chart contract

`release.yaml` binds Chart source `charts/ucm`, name `unified-cache-pd`, Chart
version `0.5.0-rc.1`, app version `0.5.0rc1`, and three render cases:

- CUDA with `nvidia.com/gpu`;
- A2 with `huawei.com/Ascend910`;
- A3 with `huawei.com/Ascend910`.

Packaging verifies `SOURCE_PROVENANCE.json`, source/release tree identities,
and imported file digests. Helm lint and template run for all three cases. The
archive is normalized for member order, ownership, mode, timestamp, and gzip
metadata, then linted again.

Both hosted attempts at the current source SHA produced the same:

- Chart package SHA256
  `4805117c69725d1ce093096ba6d5fcf46c4b2a7ff716544e993f5b87bedfefc6`;
- release-tree SHA256
  `6e0ea559cc946593ef162d8ea40497c05091466a543c8b997a2ecb0da22edb6f`.

The Chart is an Actions Artifact, not a GitHub Release asset.

## 5. Install-only image contract

### 5.1 Base and target identities

The feature candidate fixes the exact upstream index and architecture member
for each family:

| Family | Upstream tag | Target platforms |
| --- | --- | --- |
| `cuda130` | `docker.io/vllm/vllm-openai:v0.21.0` | `linux/amd64`, `linux/arm64` |
| `cann900-a2` | `quay.io/ascend/vllm-ascend:v0.22.1rc1` | `linux/amd64`, `linux/arm64` |
| `cann900-a3` | `quay.io/ascend/vllm-ascend:v0.22.1rc1-a3` | `linux/amd64`, `linux/arm64` |

The planned target repositories/tags are included in the build identity, but
they remain unpublished:

```text
ghcr.io/supermarioyl/vllm-openai:v0.21.0-ucm-0.5.0rc1-r1
ghcr.io/supermarioyl/vllm-ascend:v0.22.1rc1-ucm-0.5.0rc1-r1
ghcr.io/supermarioyl/vllm-ascend:v0.22.1rc1-a3-ucm-0.5.0rc1-r1
```

### 5.2 Context and install

The real image context contains exactly:

1. `Dockerfile`;
2. `verify_base_image.py`;
3. `install_ucm.py`;
4. `inspect_runtime.py`;
5. the exact UCM wheel;
6. the exact architecture-specific `wrapt` wheel;
7. `requirements.lock`;
8. `image-recipe.json`;
9. `image-authority.json`.

The context cannot contain the UCM source tree, `setup.py`, CMake input,
compiler input, or a UCM build command. The runtime stage only installs the
mounted, hash-locked UCM and `wrapt` wheels.

The install record checks the UCM/wrapt dependency scope rather than treating
unrelated packages already present in the upstream vLLM base as UCM release
dependencies. It still records the required `pip_check` gate for the locked
scope, exact package versions, direct URLs and hashes, and both imports.

### 5.3 Native and dependency closure

The installed native-member set, ELF machine, and `DT_NEEDED` map must equal the
wheel-builder inspection. Both the builder closure and runtime closure are
strictly validated before comparison.

For CANN, builder and runtime use the same immutable upstream member root, so
external library path and byte digest comparisons remain literal. CUDA uses a
manylinux builder root and a vLLM runtime root. In that cross-root case only the
absolute location and byte digest of ordinary `kind=external` system libraries
are normalized for the cross-root equality check. Dependency names, directness,
kind, all `wheel-member`, `virtual`, and `external-required` records,
`DT_NEEDED`, native members, and unresolved dependencies remain exact.

This normalization is compatibility between two independently validated
immutable roots; it does not turn unresolved dependencies into accepted ones.

### 5.4 OCI evidence

The build uses local OCI output, disables provenance and SBOM, and enables
timestamp rewriting. Each runtime stage removes the generated
`/var/cache/ldconfig/aux-cache` in the same layer that runs `ldconfig` so that
the cache does not make otherwise identical runtime layers drift. Verification
streams the archive and checks:

- OCI layout and index;
- manifest and config descriptor bytes;
- every layer digest and size;
- ordered rootfs diff IDs;
- annotations, labels, creation/history, recipe, and build key;
- embedded base, install, runtime, and native evidence.

The large OCI archive and layer blobs are omitted from Actions Artifact upload.
The compact artifact contains five canonical OCI files (`oci-layout.json`,
`index.json`, `manifest.json`, `config.json`, `closure.json`) plus the recipe,
authority, base records, BuildKit metadata, disk records, and logs. Aggregation
can reopen the complete compact descriptor closure but cannot decompress the
omitted layers a second time; the full layer scan occurred in the image job.

All feature image results retain:

```text
fixture_only=false
unpublished=true
publication_attempted=false
runtime_validation=external-required
device_validation=external-required
status=real-verified-unpublished
```

## 6. Family aggregation and deterministic zero marker

`aggregate-real` first requires exactly six task records and recomputes the
reviewed matrix from source SHA plus source epoch. For each task it reopens:

- hosted task record;
- wheel bytes, inspection, seal, and source context;
- image result and recipe;
- compact OCI descriptor closure;
- source, task, wheel, image, and manifest identities.

It then forms three family plans. Each plan must contain exactly
`linux/amd64` and `linux/arm64`, with each member bound to its task SHA,
manifest digest, build key, content identity, and image-result digest.

The candidate inventory is built from those three plans. After the exact-six
closure checks pass, `build_real_family_plans` directly emits this canonical
`second_reconcile` marker:

```json
{"decision":"already-present","task_count":0,"tasks":[]}
```

This is a deterministic sentinel, not a second task-set computation. The real
aggregate does not call Registry reconcile, query the target GHCR repositories,
or prove publication idempotence or public-tag readback. The earlier image jobs
separately read and verify pinned upstream base descriptors.

The image aggregate contains six wheels, six images, three families, the
candidate inventory, and the zero marker. The final aggregate adds the Chart
and independently reruns the same reopening logic.

## 7. Hosted evidence and artifact locations

The current feature result is bound to
`b9de1b3a29ae094e4c6d3895b0b642e92aa8ab42`.

| Surface | Hosted evidence |
| --- | --- |
| Push checks | [Run 31329098122](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098122), success |
| Release loop | [Attempt 1](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/attempts/1) and [attempt 2](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/attempts/2), both success |
| Wheels | Each attempt: 6/6 sealed, reopened, and uploaded |
| Images | Each attempt: 6/6 fully scanned and uploaded as compact evidence |
| Families | 3/3, each amd64 plus arm64 |
| Artifacts | Each attempt: 15 artifact groups; frozen download: 15 directories and 213 files |
| Image aggregate payload | Both attempts: `sha256:dd2c17b710ddd01b7e836b1dbc25fac866e82a7512d43cbd3e734f083b8a7b37` |
| Final aggregate payload | Both attempts: `sha256:88596b412798e34a037132320044d47283c1bfb9001eab20236f65ad44bcac1b` |
| Publication | blocked and unattempted |

The 15 artifact names follow five patterns:

```text
ucm-wheel-<six spec IDs>-<source SHA>
ucm-chart-<source SHA>
ucm-image-<six spec IDs>-<source SHA>
ucm-real-images-<source SHA>
release-loop-evidence-<source SHA>
```

They have three-day retention. Rerun artifact IDs are attempt-specific; use
the commands in the [operator README](../.github/release/README.md#artifact-names-and-readback)
to enumerate the current IDs and download the aggregate JSON.

Current aggregate links are the [Chart](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042724281),
[image aggregate](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042832261),
and [final aggregate](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31329098205/artifacts/9042839761).

The frozen same-SHA comparison passed for all canonical identities:

- all six wheel bytes, inspections, seals, task records, and source/toolchain
  records;
- Chart tgz and result JSON;
- all six image archive checksums, manifests, configs, layers, diff IDs,
  compact closures, content identities, results, authorities, and recipes;
- all three family plans, the candidate inventory, and the deterministic zero
  marker;
- the image aggregate payload and final aggregate payload.

Logs, disk telemetry, and BuildKit session metadata are diagnostics rather than
release identities. The only field excluded from the two canonical aggregate
envelopes was `github.run_attempt`. Both hosted attempts completed their wheel
builds without a retry; the bounded retry's recovery behavior is established by
the dynamic shell test, not by these runs.

## 8. Loop Engineer protocol

For changes to this release path:

1. capture the narrow failing contract or hosted log;
2. add or run the narrow regression check;
3. make the smallest functional change;
4. rerun the focused check and the complete release suite;
5. push only the authorized fork feature branch;
6. monitor the exact source SHA through six wheels, Chart, six images, barriers,
   and both aggregates;
7. reopen downloaded evidence before recording success;
8. rerun the same SHA and strictly compare the canonical identity set;
9. keep any unexecuted Registry, device, cluster, or publication claim pending.

Useful contract commands are:

```bash
pytest -q .github/release/tests
pytest -q .github/release/tests/test_workflows.py
ruff check .github/release/ucm_release .github/release/tests .github/release/docker
black --check .github/release/ucm_release .github/release/tests .github/release/docker
pre-commit run actionlint --all-files --hook-stage manual
PYTHONPATH=.github/release python -m ucm_release config validate
PYTHONPATH=.github/release python -m ucm_release core plan --require-publishable
PYTHONPATH=.github/release python -m ucm_release core tag-preflight --lane feature-candidate
PYTHONPATH=.github/release python -m ucm_release core hosted-matrix --help
```

These commands do not build release artifacts locally. The real artifact gate
is the hosted workflow.

## 9. Historical evidence

### 9.1 Determinism failure and repair

At source SHA `166e0f474a3adab88917d65b7af61ea948f7492c`, [run 31324468754](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31324468754)
completed successfully twice, but its same-SHA identity comparison failed. The
six wheel identities and Chart matched while all six image identities drifted,
which changed all three family plans and both aggregate payloads. Both
same-run zero markers were still present.

The common nondeterministic input was the generated
`/var/cache/ldconfig/aux-cache` in both runtime stages. Commit
[`ea931a95c231835a4bb4af353821084af9b998e6`](https://github.com/SuperMarioYL/unified-cache-management/commit/ea931a95c231835a4bb4af353821084af9b998e6)
removes it in the same layer as `ldconfig`. The current two-attempt result is
the hosted closure for that repair.

### 9.2 Fixture baseline

The earlier fixture path remains regression history. For example,
[Release run 31260552670](https://github.com/SuperMarioYL/unified-cache-management/actions/runs/31260552670)
at `0ee113433b75868142d86c66ee0cda2af533cc89` built one synthetic wheel
and one local amd64 OCI archive. Unlike the current real aggregate marker, that
legacy fixture path performed an actual second reconciliation; its reconcile
and zero-write ledger passed, but it did not build the six native wheels or six
real image members. It must not be used as the current feature result.

## 10. Open items

The following remain pending or `external-required`:

- protected production Environment and Tag authorization;
- GHCR member/index publication and authenticated plus anonymous digest
  readback;
- GitHub prerelease creation, asset upload, and download/hash readback;
- real CUDA, Ascend A2, and Ascend A3 runtime/device checks;
- cluster install and workload acceptance;
- formal Tag/Release publication and stable-release decision.

Current status: the hosted workflow has produced six same-SHA-repeatable real
wheels and six same-SHA-repeatable real install-only image candidates as
temporary Actions Artifacts. The full OCI tar files were verified and removed
inside their image jobs; only compact evidence was uploaded. Repository
contents were read-only. Registry/Tag/Release publication, upstream writes,
runtime/device checks, and cluster acceptance have not been completed.
