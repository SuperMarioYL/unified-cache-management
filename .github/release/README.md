# Compact UCM release automation

This directory contains the repository-owned release contracts and the unified
`python -m ucm_release` implementation. The checked-in topology is deliberately
small: four release workflows, eight Python modules, three JSON Schemas, four
Docker files, two YAML configuration files, and the product Chart at
`charts/ucm`.

## Layout

| Path | Role |
| --- | --- |
| `release.yaml` | UCM version, ordinary Python dependency, Chart, and wheel-profile declarations |
| `compatibility.yaml` | Accepted CUDA/Ascend runtime, device, OS, CPU, ABI, and upstream-channel rules |
| `ucm_release/` | Configuration, wheel, Chart, Registry, image, and loop implementation |
| `schemas/` | Configuration, generated manifest, and image-result contracts |
| `docker/` | One install-only Dockerfile and three verification helpers |
| `tests/` | Structural, behavioral, mutation, workflow, and loop checks |

The four release workflows are:

- `_build-wheel.yml`: validates its call before checkout and builds one
  deterministic fixture wheel.
- `_build-image.yml`: validates its call before checkout, creates the exact
  seven-file install context, builds a local OCI archive, and uploads compact
  evidence.
- `release-vllm-images.yml`: runs Registry reconciliation, the image build, and
  the required second zero-task reconciliation.
- `release-ucm.yml`: packages the Chart and aggregates the wheel, Chart, image,
  and reconciliation evidence.

## Unified CLI

Run commands from the repository root with `PYTHONPATH=.github/release`:

```bash
python -m ucm_release config validate
python -m ucm_release core plan
python -m ucm_release wheel fixture-build --help
python -m ucm_release wheel inspect --help
python -m ucm_release chart package --help
python -m ucm_release registry scan --help
python -m ucm_release reconcile --help
python -m ucm_release image prepare --help
python -m ucm_release image verify --help
python -m ucm_release loop prepare --help
python -m ucm_release loop complete --help
python -m ucm_release loop aggregate --help
```

`release.yaml` declares 36 production wheel specifications, but their builder,
toolchain, package, and runner identities are unresolved, so 0 are eligible.
The feature/fork path instead builds one deterministic fixture wheel. Fixture
success is local, unpublished evidence; it is not a native production wheel or
publication authority.

`wrapt==1.17.2` remains an ordinary `Requires-Dist` dependency. The image helper
runs pip with dependency resolution enabled, then checks `pip check`,
`import ucm`, and `import wrapt`.

## Image names and revisions

```text
ghcr.io/modelengine-group/vllm-openai:<exact-upstream-tag>-ucm-<version>-rN
ghcr.io/modelengine-group/vllm-ascend:<exact-upstream-tag>-ucm-<version>-rN
```

A3 and openEuler suffixes are retained only when they are part of the exact
upstream tag. CUDA, CANN, OS, Python, channel, and profile values never become
UCM-added public-tag suffixes. `tag_family` is
`(target_repository, tag_base)`: an identical build key with the same observed
and evidenced digest schedules nothing; digest drift keeps `r1` and schedules
`r2`.

## Fixture and production boundary

The fixture lane has `contents: read`, uses hosted runners, emits a zero-write
operation ledger, and never logs in to or writes an OCI Registry. Its local OCI
archive verifies the base descriptor chain, exact wheel bytes, normal pip
install, direct URL, imports, ABI, manifest layers, and rootfs diff IDs.

The uploaded compact OCI artifact intentionally omits the archive and layer
blobs. The final aggregate can reopen the descriptor/config closure emitted by
the same image job, but cannot independently decompress omitted layers.

The following remain `external-required`: protected GitHub execution, resolved
production wheel builders and runners, native custom-op wheels, Registry
credentials/write/readback, CUDA and Ascend runtime/device checks, cluster
installation, and formal publication.

## Loop Engineer verification

```bash
pytest -q .github/release/tests
ruff check .github/release/ucm_release .github/release/tests .github/release/docker
ruff format --check .github/release/ucm_release .github/release/tests .github/release/docker
black --check .github/release/ucm_release .github/release/tests .github/release/docker
pre-commit run actionlint --all-files --hook-stage manual
PYTHONPATH=.github/release python -m ucm_release config validate
PYTHONPATH=.github/release python -m ucm_release core plan
```

For every change, run the narrow failing check first, apply the smallest fix,
rerun it, then run the complete commands above. Record only fresh successful
results. Keep external-required work blocked until its real GitHub, Registry,
wheel, cluster, or accelerator evidence exists.
