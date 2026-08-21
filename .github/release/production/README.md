# UCM production release controller

This directory is the trusted control package for the repository's production
release workflows. The first externally exercised release line is `0.6.0`.

## Legacy Draft candidate

This controller is retained only for `draft/v*` source and evidence previews.
Formal `v*` Tags are handled exclusively by
`.github/workflows/release-ucm.yml`; this controller has no manual or public-Tag
entrypoint.

```bash
# Run only after the release branch is pushed and protected.
git tag -a draft/v0.6.0-1 <release-branch-sha> \
  -m "UCM 0.6.0 Draft 1"
git push origin refs/tags/draft/v0.6.0-1

```

`draft/v0.6.0-1` publishes six wheel assets, one Chart asset, three private
multi-architecture GHCR images, and a GitHub Draft Release. Its
`production-release.json` channel settings are legacy Draft-controller inputs;
the formal loop reads publication switches only from `release.yaml`.

Every write job uses the `release-production` Environment. Configure that
Environment to require the repository owner as reviewer. A rerun of the same
immutable Draft Tag reuses byte-identical remote objects; a different object at
any coordinate is a hard failure. Never move or delete a Draft Tag to retry a
failed build: fix the release branch and use the next Draft number.

## Repository setup

- Keep Actions default `GITHUB_TOKEN` permission read-only.
- Protect the default branch and `0.6.0-release`.
- Protect `draft/v*` Tags from update and deletion. Formal `v*` protection is
  owned by the main release loop's repository ruleset.
- Configure `release-production` with a required reviewer and no bypass.
- Permit the default branch for Environment deployments; the `workflow_run`
  controller executes on that branch even though the source identity is a Tag.

## Evidence boundary

A successful workflow proves Hosted builds and the fresh Registry/Release
readbacks recorded in its production evidence Artifact. Draft explicitly
records `waived-for-preview` for environment testing. It does not prove GPU/NPU
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
