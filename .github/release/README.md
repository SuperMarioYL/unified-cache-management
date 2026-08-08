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

Run commands from the repository root:

```bash
export PYTHONPATH=.github/release
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

Round 5 produced two byte-identical current-code archives at SHA256
`199b53854f9bee4a7d81a32d2a046d7de220356c35d900297d400fa65059731a`,
but both builds used an already-running local builder. This is repeatability
evidence for those inputs; it is not evidence that the current code ran through
checksum-installed Linux Buildx and the authority-selected builder. Round 4
produced two byte-identical archives at SHA256
`e25ce47385f701261f598453c0153e4813f912bf36a428db9d8f0a1c4044809e`
with authority-pinned BuildKit v0.18.2. That run has a pre-round-5 implementation
identity. The exact combination of current final code, checksum-installed Linux
Buildx, and authority-pinned BuildKit v0.18.2 is therefore `external-required`
until the Task 7 hosted GitHub run executes it end to end.

`release-vllm-images.yml` can be awakened by a `repository_dispatch` event. The
candidate itself never calls or initiates the repository-dispatch API and has
neither permission nor an operation that can send such an event.

The following remain `external-required`: protected GitHub execution, resolved
production wheel builders and runners, native custom-op wheels, Registry
credentials/write/readback, CUDA and Ascend runtime/device checks, cluster
installation, and formal publication.

## Task 7 hosted GitHub loop

Follow the tracked [Task 7 GitHub Loop
plan](../../docs/superpowers/plans/2026-08-08-ucm-release-slimming-loop.md).
Use the checksum-verified crane v0.20.3 binary declared by `_build-image.yml`.
Capture the read-only surfaces before the push:

```bash
export REPOSITORY=SuperMarioYL/unified-cache-management
export UPSTREAM_REPOSITORY=https://github.com/ModelEngine-Group/unified-cache-management.git
export SOURCE_SHA="$(git rev-parse HEAD)"
export TASK7_ROOT="$(mktemp -d /tmp/ucm-task7.XXXXXX)"
test "$(git branch --show-current)" = feature/cicd
test "$(git rev-parse feature/cicd)" = "$SOURCE_SHA"
test "$(crane version)" = 0.20.3

snapshot_zero_write() {
  destination="$1"
  mkdir -p "$destination"
  gh api --paginate --slurp \
    "repos/${REPOSITORY}/pulls?state=all&per_page=100" >"$destination/pulls.json"
  git ls-remote --tags origin | LC_ALL=C sort >"$destination/tags.txt"
  gh api --paginate --slurp \
    "repos/${REPOSITORY}/releases?per_page=100" >"$destination/releases.json"
  git ls-remote "$UPSTREAM_REPOSITORY" | LC_ALL=C sort \
    >"$destination/upstream-git-refs.txt"
  for repository in \
    ghcr.io/modelengine-group/vllm-openai \
    ghcr.io/modelengine-group/vllm-ascend; do
    name="${repository##*/}"
    crane ls "$repository" | LC_ALL=C sort >"$destination/ghcr-${name}.tags"
    while IFS= read -r tag; do
      printf '%s %s\n' "$tag" "$(crane digest "$repository:$tag")"
    done <"$destination/ghcr-${name}.tags" \
      >"$destination/ghcr-${name}.digests"
  done
  {
    crane digest docker.io/vllm/vllm-openai:v0.10.2
    crane digest quay.io/ascend/vllm-ascend:v0.9.1
  } >"$destination/upstream-digests.txt"
}

snapshot_zero_write "$TASK7_ROOT/before"
git push origin HEAD:refs/heads/feature/cicd
```

The command above is the only permitted push. Locate both caller runs by the
same full commit SHA, require the initial attempts to finish, and download the
release evidence before the one same-SHA rerun:

```bash
push_run_id="$(gh run list --repo "$REPOSITORY" --commit "$SOURCE_SHA" \
  --workflow "Push Commit Checks" --limit 20 --json databaseId \
  --jq '.[0].databaseId')"
release_run_id="$(gh run list --repo "$REPOSITORY" --commit "$SOURCE_SHA" \
  --workflow "Release UCM core artifacts" --limit 20 --json databaseId \
  --jq '.[0].databaseId')"
test -n "$push_run_id"
test -n "$release_run_id"
gh run watch "$push_run_id" --repo "$REPOSITORY" --exit-status
gh run watch "$release_run_id" --repo "$REPOSITORY" --exit-status
gh run download "$release_run_id" --repo "$REPOSITORY" \
  --dir "$TASK7_ROOT/attempt-1"

gh run rerun "$push_run_id" --repo "$REPOSITORY"
gh run rerun "$release_run_id" --repo "$REPOSITORY"
gh run watch "$push_run_id" --repo "$REPOSITORY" --exit-status
gh run watch "$release_run_id" --repo "$REPOSITORY" --exit-status
gh run download "$release_run_id" --repo "$REPOSITORY" \
  --dir "$TASK7_ROOT/attempt-2"
```

Compare the deterministic payload plus wheel, Chart, OCI, and second-reconcile
identities, then prove that the rerun made zero PR, tag, GitHub Release, GHCR,
or upstream-digest writes:

```bash
python - "$TASK7_ROOT" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])

def evidence(attempt):
    paths = list((root / attempt).rglob("release-loop-evidence.json"))
    assert len(paths) == 1, paths
    return json.loads(paths[0].read_text(encoding="utf-8"))

first = evidence("attempt-1")
second = evidence("attempt-2")
assert first["payload_sha256"] == second["payload_sha256"]
keys = ("wheel_sha256", "chart_sha256", "oci_digest", "second_reconcile_sha256")
assert all(
    first["payload"]["artifact_digests"][key]
    == second["payload"]["artifact_digests"][key]
    for key in keys
)
assert first["payload"]["must_green"]["second_reconcile_zero"] is True
assert second["payload"]["must_green"]["second_reconcile_zero"] is True
assert first["payload"]["write_audit"]["write_count"] == 0
assert second["payload"]["write_audit"]["write_count"] == 0
assert second["payload"]["publication"] == {"status": "blocked", "attempted": False}
PY

snapshot_zero_write "$TASK7_ROOT/after"
diff -ru "$TASK7_ROOT/before" "$TASK7_ROOT/after"
```

Any failed run must be investigated with `gh run view --log-failed`, job JSON,
and downloaded artifacts according to the tracked plan. Production remains
blocked. This hosted fixture loop may not create or mutate any PR, tag, GitHub
Release, GHCR image, upstream Git ref, or pinned upstream-image digest.

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
