# Compact UCM release automation

The active release lane prioritizes the functional path from an event to
build artifacts and published tags. PR, `develop` daily, and formal `v*` runs
share one plan and the same wheel, Chart, and image build jobs. Only formal
`v*` runs execute publication.

## Workflow shape

```text
Sync builders -> Plan -> Wheel[] -> Image[] -> Publish release
                      \-> Chart -------------------/
```

`.github/workflows/release-ucm.yml` contains six top-level jobs:

1. `sync-builders` discovers the current vLLM and vLLM Ascend Builder pool.
2. `plan` selects compatible upstream tags and emits `release-plan.json`.
3. `build-wheels` runs one named matrix job per profile and architecture.
4. `package-chart` validates and packages the Helm Chart.
5. `build-images` runs one named matrix job per product, variant, and architecture.
6. `publish-release` publishes every enabled channel in one job.

Builder synchronization itself has two jobs: `prepare` and a dynamic
`build-missing` matrix. Ascend 310P definitions are filtered by
`builders.yaml`; newly discovered supported Builders are added automatically.

## Task and artifact names

Task IDs are stable coordinates rather than content hashes:

- wheel: `<derived-profile>-<arch>`, for example `cuda130-default-cp312-amd64`;
- image: `<product>-<variant>-<arch>`, for example
  `vllm-ascend-a3-arm64`;
- family: `<product>-<variant>`.

The Actions UI displays capability labels such as
`Wheel · CUDA 13.0 · amd64` and
`Image · vLLM Ascend · CANN 9.0 A2 · arm64`.

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
Chart OCI, and GitHub Release. `publish-release` performs the enabled operations
in this order:

1. create or reuse a GitHub Draft;
2. publish architecture image tags as `<target-tag>-amd64` and
   `<target-tag>-arm64`;
3. compose the final multi-architecture image tag;
4. upload PyPI wheels and the Chart OCI package when enabled;
5. upload wheel and Chart assets, then publish the GitHub prerelease.

The active lane uses mutable `repository:tag` references. It does not persist
or compare source, plan, task, artifact, or OCI digests. A failed publication
may leave partial remote objects; rerunning the same Tag overwrites or fills in
the same coordinates. Hashes required internally by wheel and OCI file formats
remain implementation details and are not release-plan fields.

## Local verification

```bash
python -m pytest -q .github/release/tests
ruff check .github/release
black --check .github/release
pre-commit run actionlint --all-files --hook-stage manual
git diff --check
```

The legacy `draft/v*` production controller and `.github/release/v2/` remain
separate and are not part of this compact main lane.
