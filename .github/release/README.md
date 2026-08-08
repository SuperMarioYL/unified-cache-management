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
The current `SuperMarioYL` token receives HTTP 403 from both owner package-list
endpoints because it lacks `read:packages`. Each before/after phase probes both
endpoints again: success is normalized as the actual package list, only an
explicit HTTP 403/`read:packages` failure becomes canonical `UNAVAILABLE`, and
any other failure stops the script. The procedure neither refreshes nor expands
authentication. Anonymous reads of the two known target GHCR repositories
currently return `DENIED`; the same explicit classification rule prevents an
unavailable Registry from becoming an empty successful snapshot.

The before/after diff covers the fork pull-request list, fork tags, fork GitHub
Releases, the two package endpoints' normalized list or state, and the validated
upstream repository `HEAD`. It also covers every tag/digest in either known
target GHCR repository only when anonymous readback succeeds, or a canonical
`ABSENT`/`UNAVAILABLE` state otherwise. When package or GHCR reads are
unavailable, the no-write conclusion comes from the four workflows' exact
`contents: read` permissions, absence of `packages: write`, Registry login, or
push commands, and both downloaded operation ledgers having `write_count=0`.

Run this one canonical script from the repository root. Set
`TASK7_DRY_RUN=1` for the required clean-shell rehearsal; it performs every
pre-push check and snapshot, then deliberately exits immediately before the
only permitted push.

```bash
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1
readonly REPOSITORY=SuperMarioYL/unified-cache-management
readonly EXPECTED_LOGIN=SuperMarioYL
readonly EXPECTED_BRANCH=feature/cicd
readonly EXPECTED_REF=refs/heads/feature/cicd
readonly CRANE_VERSION=v0.20.3
readonly TASK7_DRY_RUN="${TASK7_DRY_RUN:-0}"
[[ "$TASK7_DRY_RUN" == 0 || "$TASK7_DRY_RUN" == 1 ]]

for command in curl gh git jq python tar; do
  command -v "$command" >/dev/null
done

REPO_ROOT="$(git rev-parse --show-toplevel)"
readonly REPO_ROOT
cd "$REPO_ROOT"
test "$(git branch --show-current)" = "$EXPECTED_BRANCH"
SOURCE_SHA="$(git rev-parse --verify HEAD)"
readonly SOURCE_SHA
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$(git rev-parse --verify "$EXPECTED_REF")" = "$SOURCE_SHA"
git cat-file -e "${SOURCE_SHA}^{commit}"
git diff --cached --quiet
test "$(gh api user --jq .login)" = "$EXPECTED_LOGIN"

ORIGIN_FETCH_URL="$(git remote get-url origin)"
ORIGIN_PUSH_URL="$(git remote get-url --push origin)"
readonly ORIGIN_FETCH_URL ORIGIN_PUSH_URL
for origin_url in "$ORIGIN_FETCH_URL" "$ORIGIN_PUSH_URL"; do
  case "$origin_url" in
    git@github.com:SuperMarioYL/unified-cache-management.git | \
      https://github.com/SuperMarioYL/unified-cache-management.git) ;;
    *) printf 'unexpected origin URL: %s\n' "$origin_url" >&2; exit 2 ;;
  esac
done

UPSTREAM_URL="$(git remote get-url upstream)"
readonly UPSTREAM_URL
case "$UPSTREAM_URL" in
  git@github.com:ModelEngine-Group/unified-cache-management.git | \
    https://github.com/ModelEngine-Group/unified-cache-management.git) ;;
  *) printf 'unexpected upstream URL: %s\n' "$UPSTREAM_URL" >&2; exit 2 ;;
esac

TASK7_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ucm-task7.XXXXXX")"
readonly TASK7_ROOT
mkdir -p "$TASK7_ROOT/bin" "$TASK7_ROOT/diagnostics" \
  "$TASK7_ROOT/anonymous-docker"
export DOCKER_CONFIG="$TASK7_ROOT/anonymous-docker"
CRANE=''

install_crane() {
  local os arch asset expected_sha archive actual_sha
  os="$(uname -s)"
  arch="$(uname -m)"
  case "${os}:${arch}" in
    Darwin:arm64)
      asset=go-containerregistry_Darwin_arm64.tar.gz
      expected_sha=7a46898cf7ba8b995ae8eed3a6c29d7038058b409d92ead456ff12b47a9dde37
      ;;
    Darwin:x86_64)
      asset=go-containerregistry_Darwin_x86_64.tar.gz
      expected_sha=03e520639a1898ceee815f88a07e41f2bd810e16d4f70506d7c399d925476bb6
      ;;
    Linux:x86_64 | Linux:amd64)
      asset=go-containerregistry_Linux_x86_64.tar.gz
      expected_sha=36c67a932f489b3f2724b64af90b599a8ef2aa7b004872597373c0ad694dc059
      ;;
    Linux:arm64 | Linux:aarch64)
      asset=go-containerregistry_Linux_arm64.tar.gz
      expected_sha=d2235f7779cd39c6e40f43701d2512c997409f629fb53e621ede0d57d3f995e2
      ;;
    *) printf 'unsupported crane platform: %s/%s\n' "$os" "$arch" >&2; return 2 ;;
  esac
  archive="$TASK7_ROOT/$asset"
  curl --fail --location --retry 3 --show-error --silent \
    --output "$archive" \
    "https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/${asset}"
  if command -v sha256sum >/dev/null; then
    actual_sha="$(sha256sum "$archive" | awk '{print $1}')"
  elif command -v shasum >/dev/null; then
    actual_sha="$(shasum -a 256 "$archive" | awk '{print $1}')"
  else
    printf 'sha256sum or shasum is required\n' >&2
    return 2
  fi
  test "$actual_sha" = "$expected_sha"
  tar -xzf "$archive" -C "$TASK7_ROOT/bin" crane
  CRANE="$TASK7_ROOT/bin/crane"
  [[ "$CRANE" = /* ]]
  test -x "$CRANE"
  test "$("$CRANE" version)" = "${CRANE_VERSION#v}"
}

verify_no_write_capability() {
  python - "$TASK7_ROOT/static-no-write-capability.json" <<'PY'
import json
import pathlib
import re
import sys

import yaml

paths = [
    pathlib.Path(".github/workflows/_build-wheel.yml"),
    pathlib.Path(".github/workflows/_build-image.yml"),
    pathlib.Path(".github/workflows/release-vllm-images.yml"),
    pathlib.Path(".github/workflows/release-ucm.yml"),
]
for path in paths:
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert workflow["permissions"] == {"contents": "read"}, path
    assert workflow["jobs"], path
    for job_name, job in workflow["jobs"].items():
        assert job.get("permissions") == {"contents": "read"}, (path, job_name)

text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
for pattern in (
    r"(?m)^\s*packages:\s*write\s*$",
    r"(?m)^\s*contents:\s*write\s*$",
    r"\b(?:docker|crane)\s+(?:login|push|copy|tag)\b",
    r"(?<![\w-])--push(?:\s|$)",
    r"/dispatches\b",
):
    assert re.search(pattern, text, re.IGNORECASE) is None, pattern

result = {
    "forbidden_commands": [],
    "job_permission": {"contents": "read"},
    "packages_write": False,
    "workflow_count": len(paths),
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
}

snapshot_known_ghcr() {
  local destination phase repository name tags_raw error_file status digest tag
  destination="$1"
  phase="$2"
  repository="$3"
  name="${repository##*/}"
  error_file="$TASK7_ROOT/diagnostics/${phase}-${name}.stderr"
  if tags_raw="$("$CRANE" ls "$repository" 2>"$error_file")"; then
    printf '%s\n' "$tags_raw" | awk 'NF' | LC_ALL=C sort -u \
      >"$destination/ghcr-${name}.tags"
    printf '{"repository":"%s","state":"PRESENT"}\n' "$repository" \
      >"$destination/ghcr-${name}.state.json"
    : >"$destination/ghcr-${name}.digests"
    while IFS= read -r tag; do
      if ! digest="$("$CRANE" digest "$repository:$tag" 2>>"$error_file")"; then
        cat "$error_file" >&2
        return 2
      fi
      [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
      printf '%s %s\n' "$tag" "$digest" \
        >>"$destination/ghcr-${name}.digests"
    done <"$destination/ghcr-${name}.tags"
    return 0
  else
    status=$?
  fi
  if grep -Eqi 'NAME_UNKNOWN|MANIFEST_UNKNOWN|repository.*not found|404 Not Found' \
    "$error_file"; then
    printf '{"repository":"%s","state":"ABSENT"}\n' "$repository" \
      >"$destination/ghcr-${name}.state.json"
  elif grep -Eqi 'DENIED|UNAUTHORIZED|denied:|unauthorized:|401|403' \
    "$error_file"; then
    printf '%s\n' \
      "{\"external_required\":true,\"reason\":\"anonymous-read-denied\",\"repository\":\"${repository}\",\"state\":\"UNAVAILABLE\"}" \
      >"$destination/ghcr-${name}.state.json"
  else
    cat "$error_file" >&2
    return "$status"
  fi
}

snapshot_owner_packages() {
  local destination phase endpoint name raw_file error_file status
  destination="$1"
  phase="$2"
  endpoint="$3"
  name="$4"
  raw_file="$TASK7_ROOT/diagnostics/${phase}-${name}.stdout"
  error_file="$TASK7_ROOT/diagnostics/${phase}-${name}.stderr"
  if gh api --paginate --slurp "$endpoint" >"$raw_file" 2>"$error_file"; then
    jq -S 'flatten | sort_by(.id)' "$raw_file" \
      >"$destination/${name}.json"
    printf '{"endpoint":"%s","state":"PRESENT"}\n' "$endpoint" \
      >"$destination/${name}.state.json"
    return 0
  else
    status=$?
  fi
  if grep -Eqi 'HTTP 403|read:packages' "$error_file"; then
    printf '%s\n' \
      "{\"endpoint\":\"${endpoint}\",\"external_required\":true,\"reason\":\"read:packages-http-403\",\"state\":\"UNAVAILABLE\"}" \
      >"$destination/${name}.state.json"
    return 0
  fi
  cat "$error_file" >&2
  return "$status"
}

snapshot_zero_write() {
  local destination phase
  destination="$1"
  phase="$2"
  mkdir -p "$destination"
  gh api --paginate --slurp \
    "repos/${REPOSITORY}/pulls?state=all&per_page=100" \
    | jq -S 'flatten | sort_by(.number)' >"$destination/pulls.json"
  git ls-remote --tags "$ORIGIN_PUSH_URL" | LC_ALL=C sort \
    >"$destination/tags.txt"
  gh api --paginate --slurp \
    "repos/${REPOSITORY}/releases?per_page=100" \
    | jq -S 'flatten | sort_by(.id)' >"$destination/releases.json"
  git ls-remote "$UPSTREAM_URL" HEAD | LC_ALL=C sort \
    >"$destination/upstream-head.txt"
  snapshot_owner_packages "$destination" "$phase" \
    'users/SuperMarioYL/packages?package_type=container&per_page=100' \
    fork-owner-packages
  snapshot_owner_packages "$destination" "$phase" \
    'user/packages?package_type=container&per_page=100' \
    authenticated-owner-packages
  snapshot_known_ghcr "$destination" "$phase" \
    ghcr.io/modelengine-group/vllm-openai
  snapshot_known_ghcr "$destination" "$phase" \
    ghcr.io/modelengine-group/vllm-ascend
}

discover_push_runs() {
  local poll push_count release_count
  for poll in {1..30}; do
    gh run list --repo "$REPOSITORY" --commit "$SOURCE_SHA" --event push \
      --json databaseId,workflowName,status,conclusion,headSha,url \
      >"$TASK7_ROOT/push-runs.json"
    push_count="$(jq --arg sha "$SOURCE_SHA" \
      '[.[] | select(.workflowName == "Push Commit Checks" and .headSha == $sha)] | length' \
      "$TASK7_ROOT/push-runs.json")"
    release_count="$(jq --arg sha "$SOURCE_SHA" \
      '[.[] | select(.workflowName == "Release UCM core artifacts" and .headSha == $sha)] | length' \
      "$TASK7_ROOT/push-runs.json")"
    if ((push_count > 1 || release_count > 1)); then
      printf 'duplicate same-SHA push runs: push=%s release=%s\n' \
        "$push_count" "$release_count" >&2
      return 2
    fi
    if ((push_count == 1 && release_count == 1)); then
      return 0
    fi
    sleep 10
  done
  printf 'required same-SHA push runs were not discovered\n' >&2
  return 2
}

assert_green_run() {
  local run_id workflow_name expected_attempt output
  run_id="$1"
  workflow_name="$2"
  expected_attempt="$3"
  output="$TASK7_ROOT/run-${run_id}-attempt-${expected_attempt}.json"
  gh run view "$run_id" --repo "$REPOSITORY" \
    --json attempt,databaseId,workflowName,status,conclusion,headSha,url \
    >"$output"
  jq -e --arg workflow "$workflow_name" --arg sha "$SOURCE_SHA" \
    --argjson run_id "$run_id" --argjson attempt "$expected_attempt" '
      .databaseId == $run_id and
      .workflowName == $workflow and
      .headSha == $sha and
      .attempt == $attempt and
      .status == "completed" and
      .conclusion == "success" and
      (.url | startswith("https://github.com/"))
    ' "$output" >/dev/null
}

wait_for_attempt() {
  local run_id expected_attempt poll actual_attempt
  run_id="$1"
  expected_attempt="$2"
  for poll in {1..60}; do
    actual_attempt="$(gh run view "$run_id" --repo "$REPOSITORY" \
      --json attempt --jq .attempt)"
    if ((actual_attempt == expected_attempt)); then
      return 0
    fi
    if ((actual_attempt > expected_attempt)); then
      printf 'run %s advanced past expected attempt %s\n' \
        "$run_id" "$expected_attempt" >&2
      return 2
    fi
    sleep 5
  done
  printf 'run %s did not reach attempt %s\n' "$run_id" "$expected_attempt" >&2
  return 2
}

install_crane
readonly CRANE
verify_no_write_capability
snapshot_zero_write "$TASK7_ROOT/before" before
if [[ "$TASK7_DRY_RUN" == 1 ]]; then
  printf 'TASK7_DRY_RUN=1: pre-push gates complete; refusing push; evidence=%s\n' \
    "$TASK7_ROOT"
  exit 0
fi

git push origin HEAD:refs/heads/feature/cicd

discover_push_runs
PUSH_RUN_ID="$(jq -er --arg sha "$SOURCE_SHA" \
  '[.[] | select(.workflowName == "Push Commit Checks" and .headSha == $sha)][0].databaseId' \
  "$TASK7_ROOT/push-runs.json")"
RELEASE_RUN_ID="$(jq -er --arg sha "$SOURCE_SHA" \
  '[.[] | select(.workflowName == "Release UCM core artifacts" and .headSha == $sha)][0].databaseId' \
  "$TASK7_ROOT/push-runs.json")"
readonly PUSH_RUN_ID RELEASE_RUN_ID
[[ "$PUSH_RUN_ID" =~ ^[0-9]+$ ]]
[[ "$RELEASE_RUN_ID" =~ ^[0-9]+$ ]]

gh run watch "$PUSH_RUN_ID" --repo "$REPOSITORY" --exit-status
gh run watch "$RELEASE_RUN_ID" --repo "$REPOSITORY" --exit-status
assert_green_run "$PUSH_RUN_ID" "Push Commit Checks" 1
assert_green_run "$RELEASE_RUN_ID" "Release UCM core artifacts" 1
gh run download "$RELEASE_RUN_ID" --repo "$REPOSITORY" \
  --dir "$TASK7_ROOT/attempt-1"

gh run rerun "$PUSH_RUN_ID" --repo "$REPOSITORY"
gh run rerun "$RELEASE_RUN_ID" --repo "$REPOSITORY"
wait_for_attempt "$PUSH_RUN_ID" 2
wait_for_attempt "$RELEASE_RUN_ID" 2
gh run watch "$PUSH_RUN_ID" --repo "$REPOSITORY" --exit-status
gh run watch "$RELEASE_RUN_ID" --repo "$REPOSITORY" --exit-status
assert_green_run "$PUSH_RUN_ID" "Push Commit Checks" 2
assert_green_run "$RELEASE_RUN_ID" "Release UCM core artifacts" 2
gh run download "$RELEASE_RUN_ID" --repo "$REPOSITORY" \
  --dir "$TASK7_ROOT/attempt-2"

python - "$TASK7_ROOT" "$SOURCE_SHA" "$REPOSITORY" "$EXPECTED_REF" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
source_sha, repository, ref = sys.argv[2:]
workflow_refs = [
    "release-ucm.yml",
    "_build-wheel.yml",
    "release-vllm-images.yml",
    "_build-image.yml",
]

def evidence(attempt):
    paths = list((root / attempt).rglob("release-loop-evidence.json"))
    assert len(paths) == 1, paths
    return json.loads(paths[0].read_text(encoding="utf-8"))

first = evidence("attempt-1")
second = evidence("attempt-2")
for item in (first, second):
    payload = item["payload"]
    assert payload["source_sha"] == source_sha
    assert payload["repository"] == repository
    assert payload["ref"] == ref
    assert payload["workflow_refs"] == workflow_refs
    assert payload["must_green"]["second_reconcile_zero"] is True
    assert payload["write_audit"]["write_count"] == 0
    assert payload["publication"] == {"status": "blocked", "attempted": False}

assert first["payload_sha256"] == second["payload_sha256"]
keys = ("wheel_sha256", "chart_sha256", "oci_digest", "second_reconcile_sha256")
assert all(
    first["payload"]["artifact_digests"][key]
    == second["payload"]["artifact_digests"][key]
    for key in keys
)
PY

snapshot_zero_write "$TASK7_ROOT/after" after
diff -ru "$TASK7_ROOT/before" "$TASK7_ROOT/after"
printf 'Task 7 fixture loop complete; production remains blocked; evidence=%s\n' \
  "$TASK7_ROOT"
```

If a run fails, collect `gh run view --log-failed`, the run/job JSON, and all
available artifacts under the plan's bounded repair loop. The `oci_digest`
above is the verified local OCI layout identity, not GHCR readback. Production
remains blocked. Both evidence envelopes must bind the exact
`payload.source_sha`, `payload.repository`, `payload.ref`, and
`payload.workflow_refs`. Owner package enumeration and GHCR readback remain
`external-required` whenever their canonical state is `UNAVAILABLE`; native
wheels, Registry publication, cluster installation, and accelerator evidence
also remain `external-required`.

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
