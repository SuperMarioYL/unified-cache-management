# UCM production release controller

This directory is the trusted control package for the repository's production
release workflows. The first externally exercised release line is `0.6.0`.

## Preview channels

The production path is Tag driven; it has no manual publish entrypoint.

```bash
# Run only after the release branch is pushed and protected.
git tag -a draft/v0.6.0-1 <release-branch-sha> \
  -m "UCM 0.6.0 Draft 1"
git push origin refs/tags/draft/v0.6.0-1

# Create only after the Draft evidence is complete at the same source SHA.
git tag -a v0.6.0rc1 <release-branch-sha> \
  -m "UCM 0.6.0 RC 1"
git push origin refs/tags/v0.6.0rc1
```

`draft/v0.6.0-1` publishes six wheel assets, one Chart asset, three private
multi-architecture GHCR images, and a GitHub Draft Release. `v0.6.0rc1`
publishes the public GHCR image set, the Chart OCI package, the same Release
asset closure, and a GitHub Pre-release. PyPI and Docker Hub remain disabled in
`production-release.json` for these preview Tags.

Every write job uses the `release-production` Environment. Configure that
Environment to require the repository owner as reviewer. A rerun of the same
immutable Tag reuses byte-identical remote objects; a different object at any
coordinate is a hard failure. Never move or delete the Draft or RC Tag to retry
a failed build: fix the release branch and use the next Draft/RC number.

The first public RC run can stop with
`visibility-configuration-required`. In that state the exact GHCR bytes have
already been authenticated, but the new packages are still private. Change
only the three release image packages and the Chart package to public, then
rerun the same controller. Do not replace the Tag.

## Repository setup

- Keep Actions default `GITHUB_TOKEN` permission read-only.
- Protect the default branch and `0.6.0-release`.
- Protect `draft/v*` and `v*` Tags from update and deletion.
- Configure `release-production` with a required reviewer and no bypass.
- Permit the default branch for Environment deployments; the `workflow_run`
  controller executes on that branch even though the source identity is a Tag.

## Evidence boundary

A successful workflow proves Hosted builds and the fresh Registry/Release
readbacks recorded in its production evidence Artifact. Draft and RC explicitly
record `waived-for-preview` for environment testing. They do not prove GPU/NPU
hardware execution, Kubernetes acceptance, or a Stable public release.

## Build input boundary

The production wheel projection writes three canonical records before Docker:
`build-authority.json`, `build-projection.json`, and `wheel-build.json`.
`Dockerfile.wheel` consumes the selected builder plus `wheel-build.json`, uses
the trusted-control `ucm_release` implementation for source preparation, and
leaves final sealing to the host workflow. `Dockerfile.image` retains the
`production-runtime` target and installs the UCM wheel together with every
runtime wheel named by the closed image context (`packaging` and `wrapt`).

## Local verification

Run the three suites separately because legacy and v2 contain test modules with
the same basename.

```bash
PYTHONPATH=.github/release/production python -m pytest -q .github/release/production/tests
python -m pytest -q .github/release/v2/tests
python -m pytest -q .github/release/tests
python -m compileall -q .github/release/production/ucm_release_production .github/release/ucm_release
ruff check .github/release/production .github/release/ucm_release setup.py
black --check .github/release/production .github/release/ucm_release setup.py
pre-commit run actionlint --all-files
git diff --check
```
