# Compact UCM release automation

The active release lane prioritizes the functional path from an event to
build artifacts and published tags. The root `version.ini` is the UCM version
source for wheels, the Chart, image suffixes, and the GitHub Release tag. PR,
`develop` daily, and formal `v*` runs share one plan and the same wheel, Chart,
and image build jobs. PR, daily, and manual branch builds use the checked-in
file unchanged. A formal `v*` run materializes the Tag version into each
independent wheel and Chart checkout before planning or building. Only formal
`v*` runs execute publication.

## Workflow shape

```text
Resolve upstream Tags -> Sync builders -> Plan -> Wheel[] -> Image[]
                                         \-> Chart -----------/
All builds -> Draft -> Members[] -> Indexes[] -----\
                    \-> PyPI -----------------------+-> Finalize prerelease
                    \-> Chart OCI -----------------/
```

`resolve-upstreams` chooses each runtime version once and parses its exact Git
Tag. `sync-builders` and `plan` consume the same `upstream-selection.json`, so
Builder discovery cannot drift from the runtime matrix. `plan` emits the
stable `wheels`, `images`, `families`, `wheel_matrix`, and `image_matrix`
contract. The image-index matrix is derived from `families` only as an Actions
output.

Builder synchronization itself has two jobs: `prepare` and a dynamic
`build-missing` matrix. Ascend 310P definitions are filtered by
`builders.yaml`; newly discovered default-OS variants enter automatically and
must have a UCM backend contract before build matrices start. Registry
publication is append-only: old Builder tags remain but are no longer selected.

## Task and artifact names

Task IDs are stable coordinates rather than content hashes:

- wheel: `<group>-<python-abi>-<arch>`, for example
  `cann900-a2-cp310-arm64`;
- image: `<group>-<arch>`, for example `cuda129-amd64`;
- family: `cuda129`, `cuda130`, `cann900-a2`, or `cann900-a3`.

The Actions UI displays capability labels such as
`Wheel · CUDA 13.0 · cp312 · amd64` and
`Image · vLLM Ascend · CANN 9.0 A2 · arm64`.

Each Wheel artifact contains the Wheel plus `wheel-result.json`. The result
records the actual filename and platform tags checked by `auditwheel`; release
publication never guesses the filename from configuration.

Artifacts are scoped by run and attempt:

- `ucm-release-plan-run-<run>-attempt-<attempt>`;
- `ucm-wheel-<wheel-id>-run-<run>-attempt-<attempt>`;
- `ucm-chart-run-<run>-attempt-<attempt>`;
- `ucm-image-<image-id>-run-<run>-attempt-<attempt>` when OCI upload is enabled.

PR and daily image builds validate the install-only image locally and normally
discard the OCI archive. Formal Tags and explicit `/ucm-build image` requests
upload the archive for publication.

## Publication

`release.yaml` remains the single switchboard for PyPI, GHCR, Docker Hub,
Chart OCI, and GitHub Release. After every build succeeds, publication runs as:

1. create or reuse a GitHub Draft;
2. publish architecture image members in an unbounded matrix;
3. publish all family indexes after every member succeeds;
4. publish PyPI and Chart OCI independently in parallel with image publication;
5. upload Wheel and Chart assets, then publish the GitHub prerelease from the
   sole `finalize-release` job.

The active lane uses mutable `repository:tag` references. It does not persist
or compare source, plan, task, artifact, or OCI digests. A failed publication
may leave partial Registry, Chart, or PyPI objects, but the GitHub Draft remains
private until every publication branch succeeds. Hashes required internally by
wheel and OCI file formats remain implementation details and are not
release-plan fields.

GitHub's automatically generated Tag source archives retain the checked-in
`version.ini` from the tagged commit. Published wheels, images, and the Chart
come from the same Tag checkout after the runner has materialized `version.ini`
from the Tag. The GitHub Release notes state this distinction explicitly.

## Local verification

```bash
python -m pytest -q .github/release/tests
ruff check .github/release
black --check .github/release
pre-commit run actionlint --all-files --hook-stage manual
git diff --check
```

The compact main lane is the repository's only release implementation.
