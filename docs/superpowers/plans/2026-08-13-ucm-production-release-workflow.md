# UCM Production Release Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and validate a portable, fail-closed production Tag workflow that publishes UCM Draft and RC artifacts to the current repository's GHCR, GitHub Packages, and GitHub Release, while fully implementing but not externally exercising Stable and Hotfix channels.

**Architecture:** A read-only Tag candidate workflow builds and seals six backend/architecture wheels, one Chart, and six image members. A separate `workflow_run` controller executes only default-branch control code, revalidates the candidate, reproducibly rebuilds the wheels, and enters `release-production` before rebuilding and publishing images by digest. Remote channels use pre-read/reconcile/write/readback state machines; candidate Tag code is never executed with a write token.

**Tech Stack:** Python 3.12 standard library, existing PyYAML/jsonschema/packaging release runtime, pytest, JSON Schema Draft 2020-12, GitHub Actions, actionlint, GitHub REST API, GHCR OCI Registry API, `crane`, Helm OCI, PyPI trusted publishing OIDC, Docker Hub token authentication.

## Global Constraints

- Work in `/Users/yulei/workspace/unified-cache-management/.orbit/worktrees/release-lifecycle-dry-run-v2` on `feature/release-lifecycle-dry-run-v2` until the hosted bootstrap task.
- The approved design is `docs/superpowers/specs/2026-08-13-ucm-production-release-workflow-design.md` at commit `f335d34968e86749481f8a1a656c9fda21645b70`, plus the committed clarification that `version.ini=0.6.0` is made on `0.6.0-release`, not on the default branch.
- Preserve the user's dirty `/Users/yulei/workspace/unified-cache-management` worktree and do not copy its uncommitted C++ or documentation changes into this branch.
- Keep the existing nine legacy production Workflow YAML files and eight v2 dry-run Workflow YAML files byte-identical. New production Workflows use new filenames.
- Existing `.github/release/ucm_release` behavior remains compatible. Shared-library edits are limited to pure build/config functions and must retain the complete legacy suite.
- Do not hard-code `SuperMarioYL`, `ModelEngine-Group`, repository IDs, or a default branch in new production Python or Workflow logic. The first hosted acceptance repository is data supplied by the GitHub event/API.
- Candidate jobs declare only `contents: read`; no Environment, Registry login, release write, OIDC, dispatch, comment, tag, or repository-setting operation is permitted.
- Trusted control code always comes from the double-read current default-branch SHA. Candidate Tag code is source/build data and never runs with publication credentials.
- Tag grammar is exact ASCII: `draft/vX.Y.Z-N`, `vX.Y.ZrcN`, or `vX.Y.Z`. Tags are annotated, immutable, and bound to their peeled 40-hex commit SHA.
- The first release line is `0.6`, base version `0.6.0`, release branch `0.6.0-release`, Draft Tag `draft/v0.6.0-1`, and RC Tag `v0.6.0rc1`.
- Product closure is exactly `uc-manager-cuda`, `uc-manager-cann-a2`, and `uc-manager-cann-a3`, each for amd64 and arm64; each provides `ucm`, and mixed/legacy installs fail closed.
- Draft uses private `<image>-private` GHCR repositories and a GitHub Draft Release. RC uses public GHCR repositories, Chart OCI in GitHub Packages, and a GitHub Pre-release. Stable/Hotfix additionally support opt-in PyPI and Docker Hub.
- Draft and first RC may carry `environment-test=waived-for-preview`; Stable and Hotfix require real `passed` evidence.
- Never overwrite a Tag, GHCR Tag, Chart version, Release asset, PyPI version, or Docker Hub Tag. `absent=create`, `identical=reuse`, `conflict=block`.
- Use `release-production` for every write job. Candidate/trusted preflight and deterministic rebuild jobs remain outside the Environment and have read-only permissions.
- Full OCI archives are not transferred from the candidate run. The publisher rebuilds from trusted control code and sealed wheel bytes, compares the full OCI closure, and then pushes.
- First-created RC packages may require owner visibility configuration. Preserve the Release as Draft with `visibility-configuration-required`, then rerun the same immutable Tag after the owner makes packages public.
- Local, Hosted Actions, Registry/Release readback, hardware, cluster, and public-delivery evidence are reported as separate layers.
- Use TDD for every task: observe the focused RED failure, implement the minimum behavior, observe GREEN, then run the stated regression gate and commit.

---

## File Structure

### New production control package

- `.github/release/production/production-release.json` — trusted release line, product/channel matrix, build locks, naming, Environment, and external-channel switches.
- `.github/release/production/ucm_release_production/common.py` — duplicate-key-safe JSON, canonical bytes, self-digests, strict scalar/path helpers, and controlled errors.
- `.github/release/production/ucm_release_production/config.py` — strict config load, product closure, repository-derived coordinate projection, and build-profile projection.
- `.github/release/production/ucm_release_production/tags.py` — annotated Tag parsing, PEP 440/SemVer mapping, release/hotfix branch derivation, and lineage rules.
- `.github/release/production/ucm_release_production/candidate.py` — candidate envelope creation/reopen, exact Artifact-member validation, wheel/Chart/image closure binding, and trusted-rebuild comparison.
- `.github/release/production/ucm_release_production/github_api.py` — bounded GitHub REST transport, double reads, run/workflow/ref/release/asset inventories, and typed responses.
- `.github/release/production/ucm_release_production/reconcile.py` — channel inventory normalization and absent/identical/conflict/partial planning.
- `.github/release/production/ucm_release_production/registry.py` — GHCR member/index/Chart operations, authenticated/anonymous readback, visibility classification, and OCI closure validation.
- `.github/release/production/ucm_release_production/github_release.py` — Draft/Pre-release/Release create/resume, asset upload, visibility transition, and download readback.
- `.github/release/production/ucm_release_production/external.py` — Stable/Hotfix-only PyPI OIDC and Docker Hub adapters with preflight/no-overwrite/readback contracts.
- `.github/release/production/ucm_release_production/evidence.py` — canonical final evidence assembly and safe Markdown summary rendering.
- `.github/release/production/ucm_release_production/cli.py` and `__main__.py` — file-oriented CLI; no caller-selected executable, URL, runner, shell, or arbitrary coordinate.

### New schemas and tests

- `.github/release/production/schemas/*.schema.json` — the seven approved production contracts.
- `.github/release/production/tests/` — focused unit, contract, transport-fixture, CLI, scenario, workflow, and security mutation tests.
- `.github/release/production/tests/fixtures/` — offline GitHub/GHCR/Release/PyPI/Docker Hub response fixtures only; no credentials or live tokens.

### New Workflows

- `.github/workflows/production-tag-candidate.yml` — read-only Tag router and candidate aggregate.
- `.github/workflows/_production-build-wheel.yml` — one read-only backend/architecture wheel build.
- `.github/workflows/_production-build-image.yml` — one read-only candidate OCI build and compact closure.
- `.github/workflows/production-release-controller.yml` — default-branch `workflow_run` trust gate.
- `.github/workflows/_production-release-controller.yml` — trusted rebuild, approval, channel orchestration, readback, and evidence.
- `.github/workflows/_production-publish-image-member.yml` — one approved rebuild/compare/push member job.

### Compatibility edits

- `setup.py` — accept a schema-v2 release authority containing an explicit approved distribution/version while preserving schema-v1 `uc-manager` builds.
- `.github/release/ucm_release/{core,wheel,image}.py` — only pure production build projections required by the new package; legacy defaults remain unchanged.
- `.github/release/tests/{test_core_release,test_image_build,test_workflows}.py` — compatibility tests and new-workflow allowlisting; legacy Workflow content assertions remain intact.
- `.github/release/v2/tests/test_workflows.py` — recognize the new production Workflow set without weakening v2 read-only policies.
- `.github/release/production/README.md` and `docs/ucm-production-release-workflow-implementation.md` — operator contract, repository bootstrap, evidence interpretation, and exact validation commands.

---

### Task 1: Freeze Baselines and Add the Trusted Production Configuration

**Files:**
- Create: `.github/release/production/production-release.json`
- Create: `.github/release/production/ucm_release_production/{__init__.py,__main__.py,cli.py,common.py,config.py}`
- Create: `.github/release/production/schemas/production-release-config.schema.json`
- Create: `.github/release/production/tests/{conftest.py,test_config.py,test_common.py}`
- Create: `.github/release/production/tests/fixtures/legacy-workflow-sha256.json`
- Test: `.github/release/tests/test_workflows.py`

**Interfaces:**
- Produces: `load_config(path: Path) -> dict[str, Any]`, `derive_repository(config, repository: str, repository_id: int, default_branch: str) -> dict[str, Any]`, `canonical_bytes(value: object) -> bytes`, `sha256_envelope(value: Mapping[str, Any]) -> dict[str, Any]`.
- Produces config keys used later: `release_line`, `base_version`, `release_branch`, `products`, `build_profiles`, `channels`, `environment`, `retention_days`, and `external_channels`.

- [ ] **Step 1: Write failing configuration and frozen-baseline tests**

```python
def test_config_derives_current_repository_without_owner_constants(tmp_path: Path) -> None:
    config = load_config(CONFIG)
    resolved = derive_repository(
        config,
        repository="octocat/unified-cache-management",
        repository_id=42,
        default_branch="develop",
    )
    assert resolved["ghcr_namespace"] == "octocat"
    assert resolved["release_branch"] == "0.6.0-release"
    assert "SuperMarioYL" not in json.dumps(resolved)

def test_legacy_workflow_fingerprints_are_frozen() -> None:
    assert current_legacy_workflow_hashes() == json.loads(FINGERPRINTS.read_text())
```

- [ ] **Step 2: Run the focused tests and observe RED**

Run: `python -m pytest -q .github/release/production/tests/test_common.py .github/release/production/tests/test_config.py`

Expected: collection fails because `ucm_release_production` and `production-release.json` do not exist.

- [ ] **Step 3: Implement canonical JSON and strict config loading**

Implement `common.py` with duplicate-key rejection, UTF-8-only reads, no NaN/Infinity, exact self-digest verification, safe POSIX paths, full lowercase SHA helpers, and `ProductionError` messages without traceback leakage. Implement `config.py` so the product order is exactly CUDA, CANN A2, CANN A3 and every build profile has exactly amd64/arm64.

The config contains basenames, never a repository owner:

```json
{
  "kind": "ucm-production-release-config",
  "schema_version": 1,
  "release_line": "0.6",
  "base_version": "0.6.0",
  "release_branch": "0.6.0-release",
  "environment": "release-production",
  "products": {
    "wheels": ["uc-manager-cuda", "uc-manager-cann-a2", "uc-manager-cann-a3"],
    "images": ["ucm-cuda", "ucm-cann-a2", "ucm-cann-a3"],
    "chart": "unified-cache-pd"
  },
  "external_channels": {"pypi": false, "docker_hub": false}
}
```

The complete file also copies the already reviewed immutable builder/upstream/tool digests from `.github/release/release.yaml`; no mutable tag becomes authority.

- [ ] **Step 4: Record and enforce the nine legacy Workflow SHA256 values**

Use a one-time read-only hash command to populate `legacy-workflow-sha256.json`, then make the test compare the current bytes. Do not modify the legacy Workflow files to satisfy the test.

- [ ] **Step 5: Run focused and legacy topology tests**

Run:

```bash
python -m pytest -q .github/release/production/tests/test_common.py .github/release/production/tests/test_config.py
python -m pytest -q .github/release/tests/test_workflows.py -k 'topology or workflow_set or fork_isolation'
git diff --check
```

Expected: all selected tests pass; the nine legacy hashes are unchanged.

- [ ] **Step 6: Commit Task 1**

```bash
git add .github/release/production .github/release/tests/test_workflows.py
git commit -m "feat(release): add trusted production configuration"
```

### Task 2: Implement Strict Tag Intent, Branch Binding, and Lineage

**Files:**
- Create: `.github/release/production/ucm_release_production/tags.py`
- Create: `.github/release/production/schemas/production-tag-intent.schema.json`
- Create: `.github/release/production/tests/test_tags.py`
- Modify: `.github/release/production/ucm_release_production/{cli.py,config.py}`

**Interfaces:**
- Consumes: `load_config`, `sha256_envelope`.
- Produces: `parse_tag(tag_name: str, config: Mapping[str, Any]) -> TagIntent`, `verify_ref_snapshot(intent: TagIntent, snapshot: Mapping[str, Any]) -> dict[str, Any]`.
- `TagIntent` is a frozen dataclass with `stage`, `tag_name`, `version`, `wheel_version`, `chart_version`, `image_tag`, `release_branch`, `draft_number`, and `rc_number`.

- [ ] **Step 1: Write RED tests for valid and adversarial Tag inputs**

```python
@pytest.mark.parametrize(
    ("tag", "stage", "wheel", "chart"),
    [
        ("draft/v0.6.0-1", "draft", "0.6.0.dev1", "0.6.0-draft.1"),
        ("v0.6.0rc1", "rc", "0.6.0rc1", "0.6.0-rc.1"),
        ("v0.6.0", "stable", "0.6.0", "0.6.0"),
        ("v0.6.1", "hotfix", "0.6.1", "0.6.1"),
    ],
)
def test_tag_projection(tag: str, stage: str, wheel: str, chart: str) -> None:
    intent = parse_tag(tag, load_config(CONFIG))
    assert (intent.stage, intent.wheel_version, intent.chart_version) == (
        stage, wheel, chart
    )
```

Add rejects for lightweight Tags, leading zeroes, Unicode digits/space, `latest`, local versions, missing annotations, mismatched release line, wrong branch head, tag/branch double-read drift, RC without same-SHA Draft evidence, Stable without same-SHA accepted RC, and Hotfix without previous-Stable ancestry.

- [ ] **Step 2: Run and observe RED**

Run: `python -m pytest -q .github/release/production/tests/test_tags.py`

Expected: import/attribute failures for `tags.py`.

- [ ] **Step 3: Implement the parser and source snapshot validator**

Use ASCII regular expressions with `re.ASCII`; distinguish Stable `v0.6.0` from Hotfix `v0.6.P` using `base_version`. Require the ref snapshot to contain two identical observations for the Tag object, peeled commit, release/hotfix branch, and default-branch control SHA.

- [ ] **Step 4: Expose file-oriented CLI commands**

Add:

```text
python -m ucm_release_production tag parse --config ... --tag ... --output ...
python -m ucm_release_production tag verify-refs --config ... --intent ... --snapshot ... --output ...
```

CLI exits `2`, writes no stdout payload, and prints one controlled line to stderr for invalid input.

- [ ] **Step 5: Run focused tests and schema validation**

Run:

```bash
python -m pytest -q .github/release/production/tests/test_tags.py
python -m pytest -q .github/release/production/tests/test_config.py
python -m json.tool .github/release/production/schemas/production-tag-intent.schema.json >/dev/null
```

Expected: all pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add .github/release/production
git commit -m "feat(release): validate production tag lineage"
```

### Task 3: Add Backend-Specific Production Wheel Authorities Without Breaking Legacy Builds

**Files:**
- Modify: `setup.py`
- Modify: `.github/release/ucm_release/{core.py,wheel.py}`
- Create: `.github/release/production/ucm_release_production/build.py`
- Create: `.github/release/production/tests/{test_build.py,test_setup_authority.py}`
- Modify: `.github/release/tests/{test_core_release.py,test_image_build.py}`

**Interfaces:**
- Consumes: `TagIntent`, trusted config build profiles.
- Produces: `project_build_task(config, intent, source: SourceIdentity, spec_id: str) -> dict[str, Any]`, `compare_wheel_candidates(candidate: Path, trusted: Path, task: Mapping[str, Any]) -> dict[str, Any]`.
- Schema-v2 authority adds exact keys `distribution`, `base_version`, and `stage`; schema-v1 authorities retain the old exact shape and behavior.

- [ ] **Step 1: Write legacy-preservation and new-authority RED tests**

```python
def test_schema_v1_release_authority_still_builds_uc_manager() -> None:
    result = run_setup_metadata(schema_v1_authority())
    assert result.name == "uc-manager"

@pytest.mark.parametrize(
    ("profile", "distribution"),
    [
        ("cuda130", "uc-manager-cuda"),
        ("cann900-a2", "uc-manager-cann-a2"),
        ("cann900-a3", "uc-manager-cann-a3"),
    ],
)
def test_schema_v2_authority_controls_exact_distribution(
    profile: str, distribution: str
) -> None:
    result = run_setup_metadata(schema_v2_authority(profile, distribution))
    assert result.name == distribution
    assert result.version == "0.6.0rc1"
```

Add mutation tests for arbitrary distribution, wrong profile mapping, local version on Stable, mismatched authority/task/source tree, schema-v1 extra fields, schema-v2 missing fields, mixed backend metadata, and legacy `uc-manager` co-install.

- [ ] **Step 2: Run focused RED tests**

Run: `python -m pytest -q .github/release/production/tests/test_setup_authority.py .github/release/production/tests/test_build.py`

Expected: schema-v2 cases fail because the current setup authority only accepts schema v1 and `uc-manager`.

- [ ] **Step 3: Implement dual authority parsing in `setup.py`**

Keep schema v1 exact. For schema v2, require the explicit distribution to equal the profile mapping and require `UCM_RELEASE_DISTRIBUTION`, `UCM_RELEASE_VERSION`, and the canonical authority bytes to agree. Set `setup(name=...)` from the validated release settings only when `UCM_RELEASE_BUILD=1`; ordinary builds continue using `uc-manager`.

- [ ] **Step 4: Project production build tasks using existing immutable builders**

Implement `build.py` by reusing legacy pure helpers for source archive, native target closure, wheel sealing, and image authority, but supply the trusted production config and Tag-derived version. Do not call `core.validate_config()` on the legacy hard-coded production file.

- [ ] **Step 5: Run focused, setup, and complete legacy tests**

Run:

```bash
python -m pytest -q .github/release/production/tests/test_setup_authority.py .github/release/production/tests/test_build.py
python -m pytest -q .github/release/tests/test_core_release.py .github/release/tests/test_image_build.py
git diff --check
```

Expected: all pass, including schema-v1 exactness.

- [ ] **Step 6: Commit Task 3**

```bash
git add setup.py .github/release/ucm_release .github/release/tests .github/release/production
git commit -m "feat(release): build backend-specific production wheels"
```

### Task 4: Seal Candidate Artifacts and Prove Trusted Rebuild Equality

**Files:**
- Create: `.github/release/production/ucm_release_production/candidate.py`
- Create: `.github/release/production/schemas/production-candidate-envelope.schema.json`
- Create: `.github/release/production/tests/test_candidate.py`
- Create: `.github/release/production/tests/fixtures/candidate/**`
- Modify: `.github/release/production/ucm_release_production/{build.py,cli.py}`

**Interfaces:**
- Produces: `seal_candidate(root: Path, intent: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]`, `reopen_candidate(zip_path: Path, expected: Mapping[str, Any]) -> CandidateBundle`, `compare_trusted_rebuild(bundle: CandidateBundle, trusted_root: Path) -> dict[str, Any]`.
- `CandidateBundle` exposes only validated file paths and canonical documents; callers cannot access unchecked zip members.

- [ ] **Step 1: Write RED candidate closure tests**

Require exactly six wheels, one Chart, six image member closures, three index identities, one source identity, one Tag intent, and one candidate run identity. Add negatives for zip-slip, absolute paths, symlinks, duplicate zip names, control characters, size/count limits, extra/missing products, re-signed semantic drift, source/config/run mismatch, cross-attempt Artifact reuse, and wheel byte drift.

```python
def test_candidate_bundle_has_exact_product_closure(candidate_root: Path) -> None:
    envelope = seal_candidate(candidate_root, INTENT, RUN)
    assert [item["spec_id"] for item in envelope["wheels"]] == EXPECTED_SIX
    assert len(envelope["image_members"]) == 6
    assert len(envelope["image_indexes"]) == 3
```

- [ ] **Step 2: Run and observe RED**

Run: `python -m pytest -q .github/release/production/tests/test_candidate.py`

Expected: `candidate.py` missing.

- [ ] **Step 3: Implement streaming-safe reopen and semantic validation**

Hash files in chunks, validate member names before extraction, extract only into a newly created temporary directory, and validate JSON bytes before exposing paths. Bind every child digest into the envelope self-digest.

- [ ] **Step 4: Implement trusted wheel equality and OCI closure comparison**

Wheel comparison is byte-for-byte plus reopened metadata/RECORD/ELF checks. OCI comparison includes manifest bytes, config bytes, ordered layer descriptor/diff-ID pairs, annotations, platform, source SHA, wheel SHA, recipe SHA, and index member set; diagnostic timestamps/logs are excluded from identity.

- [ ] **Step 5: Run focused and adversarial tests**

Run: `python -m pytest -q .github/release/production/tests/test_candidate.py -vv`

Expected: all pass with no network access.

- [ ] **Step 6: Commit Task 4**

```bash
git add .github/release/production
git commit -m "feat(release): seal production candidate evidence"
```

### Task 5: Implement Trusted GitHub Reads and Channel Reconciliation

**Files:**
- Create: `.github/release/production/ucm_release_production/{github_api.py,reconcile.py}`
- Create: `.github/release/production/schemas/{production-channel-inventory,production-publish-plan}.schema.json`
- Create: `.github/release/production/tests/{test_github_api.py,test_reconcile.py}`
- Create: `.github/release/production/tests/fixtures/github/**`
- Modify: `.github/release/production/ucm_release_production/cli.py`

**Interfaces:**
- Produces: `GitHubClient.request_json(method: Literal["GET"], path: str) -> object` for preflight; write methods are separate explicit functions used only after Task 7.
- Produces: `read_trusted_identity(client, repository: str, run_id: int) -> dict[str, Any]`, `build_inventory(...) -> dict[str, Any]`, `plan_publication(intent, candidate, inventory, config) -> dict[str, Any]`.

- [ ] **Step 1: Write RED transport/trust tests**

Use a local HTTP server fixture. Cover duplicate JSON keys, redirect, wrong host/repository ID, wrong workflow ID/path/event/conclusion, PR/fork/manual run, stale default branch, mismatched `referenced_workflows`, tag/ref double-read drift, pagination duplicates, expired candidate Artifact, and unbounded responses.

- [ ] **Step 2: Write RED reconcile tests**

For every Wheel asset, GHCR member/index, Chart, Release and external channel, test absent, identical, conflict, partial, and cross-stage coordinate occupancy. Confirm a conflict anywhere produces no write operations.

- [ ] **Step 3: Run RED tests**

Run: `python -m pytest -q .github/release/production/tests/test_github_api.py .github/release/production/tests/test_reconcile.py`

Expected: missing modules/functions.

- [ ] **Step 4: Implement bounded GET transport and exact trusted identity projection**

Allow only `https://api.github.com/repos/<current-repository>/...` and the Actions Artifact download URL returned for the same run. Disable redirects, cap response bytes, reject duplicate keys, and retry only explicit 429/5xx/secondary-rate-limit responses with bounded attempts.

- [ ] **Step 5: Implement the no-overwrite planner**

Each operation is typed as `create`, `reuse`, or `blocked`; write coordinates are generated by config/intent code, never accepted from fixtures or CLI strings. A plan with any `blocked` item has `publishable=false` and contains zero executable write requests.

- [ ] **Step 6: Run tests and commit**

```bash
python -m pytest -q .github/release/production/tests/test_github_api.py .github/release/production/tests/test_reconcile.py
git diff --check
git add .github/release/production
git commit -m "feat(release): reconcile trusted production inventory"
```

### Task 6: Implement GHCR Member, Index, and Chart Publication with Readback

**Files:**
- Create: `.github/release/production/ucm_release_production/registry.py`
- Create: `.github/release/production/schemas/production-channel-record.schema.json`
- Create: `.github/release/production/tests/test_registry.py`
- Create: `.github/release/production/tests/fixtures/registry/**`
- Modify: `.github/release/production/ucm_release_production/{cli.py,reconcile.py}`
- Reuse: `.github/release/ucm_release/registry.py`

**Interfaces:**
- Produces: `publish_member(request: MemberPublishRequest, transport: RegistryTransport) -> dict[str, Any]`, `publish_index(...)`, `publish_chart(...)`, `readback_reference(reference, visibility) -> dict[str, Any]`.
- `RegistryTransport` accepts only argv lists for pinned `crane`/`helm`; it never invokes a shell or caller-selected binary.

- [ ] **Step 1: Write RED Registry state-machine tests**

Cover private Draft, public RC, digest push, immutable Tag attach, exact member/index closure, Chart manifest/config/layer closure, prewrite/fresh-prewrite/postwrite reads, write-success/response-loss recovery, auth denial vs absence, public anonymous readback, private anonymous denial, redirect, wrong media type, and digest collision.

- [ ] **Step 2: Run and observe RED**

Run: `python -m pytest -q .github/release/production/tests/test_registry.py`

Expected: missing `registry.py`.

- [ ] **Step 3: Implement member and index operations by adapting proven legacy primitives**

Reuse the existing content-addressed `crane push`, child manifest readback, index creation, and operation-ledger logic, but derive repositories from current owner/config and require Task 5's publish plan. Before any mutation, reread the target Tag and reject conflict.

- [ ] **Step 4: Implement Chart OCI packaging and readback**

Package the already sealed deterministic Chart. For RC use `oci://ghcr.io/<owner>/charts/unified-cache-pd:X.Y.Z-rc.N`; Stable/Hotfix use final SemVer. Validate manifest/config/layer digest and anonymous public pull.

- [ ] **Step 5: Verify focused and legacy Registry tests**

Run:

```bash
python -m pytest -q .github/release/production/tests/test_registry.py
python -m pytest -q .github/release/tests/test_registry_reconcile.py
```

Expected: all pass.

- [ ] **Step 6: Commit Task 6**

```bash
git add .github/release/production .github/release/ucm_release/registry.py .github/release/tests/test_registry_reconcile.py
git commit -m "feat(release): publish immutable GHCR artifacts"
```

### Task 7: Implement GitHub Draft, Pre-release, Assets, and Idempotent Readback

**Files:**
- Create: `.github/release/production/ucm_release_production/github_release.py`
- Create: `.github/release/production/tests/test_github_release.py`
- Create: `.github/release/production/tests/fixtures/releases/**`
- Modify: `.github/release/production/ucm_release_production/{github_api.py,reconcile.py,cli.py}`
- Reuse: `.github/release/ucm_release/verify.py`

**Interfaces:**
- Produces: `prepare_release(plan, client)`, `upload_assets(plan, client)`, `finalize_release(plan, client)`, and `readback_release(plan, client)` returning `production-channel-record` documents.

- [ ] **Step 1: Write RED Release and asset tests**

Cover absent create, exact Draft resume, already-final identical reuse, wrong source marker, wrong release state, duplicate asset name/ID, checksum conflict, upload response loss, asset list drift, authenticated/API download, unauthenticated download, Draft non-public behavior, RC `make_latest=false`, partial-publication, and final visibility transition only after all mandatory channel records pass.

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest -q .github/release/production/tests/test_github_release.py`

Expected: missing module/functions.

- [ ] **Step 3: Implement bounded GitHub write transports**

Only the explicit `release create`, `asset upload`, and `release patch` operations accept POST/PATCH. Validate API/upload host, repository path, release ID, Content-Length, status, and response body. Never follow redirects during writes.

- [ ] **Step 4: Implement deterministic Release notes and asset manifest**

Asset set is six wheels, Chart tgz, checksums, production manifest, dependency/SBOM evidence, and environment status. Release body includes exact install/pull coordinates and clearly says `waived-for-preview` for the first Draft/RC.

- [ ] **Step 5: Verify focused and legacy Release tests**

Run:

```bash
python -m pytest -q .github/release/production/tests/test_github_release.py
python -m pytest -q .github/release/tests/test_workflows.py -k 'release_asset or final_release or idempotent'
```

Expected: all pass.

- [ ] **Step 6: Commit Task 7**

```bash
git add .github/release/production .github/release/ucm_release/verify.py .github/release/tests
git commit -m "feat(release): reconcile GitHub release assets"
```

### Task 8: Complete Stable/Hotfix External Channels and Final Evidence

**Files:**
- Create: `.github/release/production/ucm_release_production/{external.py,evidence.py}`
- Create: `.github/release/production/schemas/production-release-evidence.schema.json`
- Create: `.github/release/production/tests/{test_external.py,test_evidence.py}`
- Create: `.github/release/production/tests/fixtures/external/**`
- Modify: `.github/release/production/ucm_release_production/{cli.py,reconcile.py}`

**Interfaces:**
- Produces: `preflight_external_channels(intent, config, environment)`, `publish_pypi(...)`, `publish_docker_hub(...)`, `assemble_evidence(...)`, `render_summary(...)`.

- [ ] **Step 1: Write RED Stable/Hotfix and external-channel tests**

Require Stable same-SHA accepted RC, Hotfix previous-Stable lineage and different SHA, real environment `passed`, three backend distributions with no local versions, OIDC-only PyPI upload, Docker Hub secret presence, no-overwrite pre-read, and remote readback. Draft/RC must never schedule PyPI/Docker Hub operations.

- [ ] **Step 2: Run RED tests**

Run: `python -m pytest -q .github/release/production/tests/test_external.py .github/release/production/tests/test_evidence.py`

Expected: missing modules/functions.

- [ ] **Step 3: Implement explicit opt-in adapters**

If a Stable/Hotfix channel is enabled but required OIDC/secret identity is missing, return a blocker before any GHCR/Release write. PyPI coordinates are the three backend-specific distributions. Docker Hub repositories come from trusted config and cannot default to the GHCR owner.

- [ ] **Step 4: Assemble canonical evidence and injection-safe Markdown**

Evidence includes exact repo/Tag/control/candidate/deployment/channel identities, operations, authenticated and anonymous readbacks, environment state, blockers, and one of `complete`, `partial-publication`, or `blocked`. Markdown escapes all untrusted content and never renders raw HTML or code fences from external text.

- [ ] **Step 5: Run focused tests and commit**

```bash
python -m pytest -q .github/release/production/tests/test_external.py .github/release/production/tests/test_evidence.py
git diff --check
git add .github/release/production
git commit -m "feat(release): close stable and hotfix channels"
```

### Task 9: Add the Read-only Candidate and Trusted Production Workflows

**Files:**
- Create: `.github/workflows/{production-tag-candidate,_production-build-wheel,_production-build-image,production-release-controller,_production-release-controller,_production-publish-image-member}.yml`
- Create: `.github/release/production/tests/{test_workflows.py,test_security.py}`
- Modify: `.github/release/tests/test_workflows.py`
- Modify: `.github/release/v2/tests/test_workflows.py`
- Modify: `.github/release/production/ucm_release_production/cli.py`

**Interfaces:**
- Candidate outputs one aggregate Artifact name and candidate envelope digest.
- Controller trust job outputs `control_sha`, `source_sha`, `tag_name`, `tag_object_sha`, `candidate_run_id`, `candidate_run_attempt`, and `publish_plan_sha256`.
- Reusable publisher accepts only these validated identities and a closed `spec_id` enum.

- [ ] **Step 1: Write RED Workflow topology and permission tests**

Assert exact trigger, job graph, permission maps, Environment placement, concurrency, timeouts, action pins, Artifact names, outputs, and ordered step sequences. Candidate jobs must have only `contents: read`. Controller is triggered only by completed `UCM Production Tag Candidate` workflow runs.

- [ ] **Step 2: Write RED security mutation tests**

Mutate/delete/move/duplicate each repo/path/event/conclusion/ref/double-read/referenced-workflow check; move trust steps after checkout/CLI; add candidate write permission/login; execute candidate Python/shell/Action/Dockerfile in a write job; inject `$()`, process substitution, redirects, dynamic executable, curl pipe, arbitrary URL, unpinned Action, `workflow_dispatch`, PR/fork, external reusable Workflow, or candidate-selected runner. Every mutation must produce a finding.

- [ ] **Step 3: Run RED tests**

Run: `python -m pytest -q .github/release/production/tests/test_workflows.py .github/release/production/tests/test_security.py`

Expected: missing Workflows and security policy.

- [ ] **Step 4: Implement the candidate workflow**

`production-tag-candidate.yml` broadly routes `draft/v*` and `v*`; the first executable step strictly parses the Tag. It calls production-specific read-only wheel/image reusable Workflows, builds Chart, seals exact evidence, uploads compact artifacts, and has no Environment or write permission.

- [ ] **Step 5: Implement the trusted `workflow_run` controller**

The entry performs an embedded pre-checkout trust gate from the default branch, calls the local reusable controller, and verifies `referenced_workflows`. The reusable controller downloads only the triggering run's candidate Artifact, performs trusted wheel rebuilds, creates a no-write plan, and then uses separate Environment-bound jobs for GHCR/Chart and Release writes.

- [ ] **Step 6: Implement approved member publishers**

Each matrix job validates identities before checkout/login, uses default-branch control and Dockerfile, downloads only same-run trusted wheel Artifact, rebuilds one image, compares candidate closure, logs in with a temporary Docker config, rereads the target, pushes/reuses, reads back, logs out, deletes credentials, then uploads evidence.

- [ ] **Step 7: Update legacy topology allowlists without weakening frozen checks**

The tests may recognize the six new filenames and their explicit production policy. They must still hash all nine legacy Workflow bytes and continue rejecting any other arbitrary publish Workflow.

- [ ] **Step 8: Run Workflow/security gates**

Run:

```bash
python -m pytest -q .github/release/production/tests/test_workflows.py .github/release/production/tests/test_security.py
python -m pytest -q .github/release/v2/tests/test_workflows.py .github/release/v2/tests/test_security.py
python -m pytest -q .github/release/tests/test_workflows.py
pre-commit run actionlint --files .github/workflows/production-tag-candidate.yml .github/workflows/_production-build-wheel.yml .github/workflows/_production-build-image.yml .github/workflows/production-release-controller.yml .github/workflows/_production-release-controller.yml .github/workflows/_production-publish-image-member.yml
```

Expected: all pass and repository production security audit reports zero findings.

- [ ] **Step 9: Commit Task 9**

```bash
git add .github/workflows .github/release/production .github/release/tests .github/release/v2/tests
git commit -m "feat(release): add trusted production workflows"
```

### Task 10: Close Offline Scenarios, Documentation, and Full Local Verification

**Files:**
- Create: `.github/release/production/tests/test_scenarios.py`
- Create: `.github/release/production/README.md`
- Create: `docs/ucm-production-release-workflow-implementation.md`
- Modify: `docs/superpowers/specs/2026-08-13-ucm-production-release-workflow-design.md`
- Modify only files from Tasks 1-9 when integration tests expose a defect.

**Interfaces:**
- Produces documented operator commands and a verified final local tree; no hosted mutation yet.

- [ ] **Step 1: Add end-to-end offline scenarios**

Cover successful Draft, successful RC, first-public-package visibility hold, identical rerun, partial write recovery, target conflict, Tag/ref drift, candidate replacement, wheel rebuild drift, anonymous readback failure, Stable without real environment, Stable same-SHA RC success, Hotfix previous-Stable success, missing PyPI OIDC, and missing Docker Hub secrets.

- [ ] **Step 2: Run scenario tests from a clean temporary directory**

Run: `python -m pytest -q .github/release/production/tests/test_scenarios.py`

Expected: all scenarios pass without accessing the network.

- [ ] **Step 3: Document exact setup and evidence boundaries**

The README includes repository rules, Environment branch behavior for `workflow_run`, first-package visibility handshake, Draft/RC commands, rerun behavior, rollback/non-overwrite behavior, and secret/OIDC setup for future Stable/Hotfix. The implementation report records only observed local results; Hosted and Registry fields remain `not-run`.

- [ ] **Step 4: Run complete local gates on the fixed tree**

Run in separate pytest processes to avoid duplicate `test_workflows.py` module names:

```bash
python -m pytest -q .github/release/production/tests
python -m pytest -q .github/release/v2/tests
python -m pytest -q .github/release/tests
python -m compileall -q .github/release/production/ucm_release_production .github/release/ucm_release
ruff check .github/release/production .github/release/ucm_release setup.py
black --check .github/release/production .github/release/ucm_release setup.py
pre-commit run actionlint --all-files
git diff --check
```

Validate every production schema with `Draft202012Validator.check_schema`, every JSON/YAML file with duplicate-key rejection, every Action pin, and every frozen Workflow hash.

- [ ] **Step 5: Audit the final diff and index**

Confirm only planned files changed, the main dirty worktree is untouched, no credential-like string exists, the Git index is empty before staging, and the nine legacy Workflow fingerprints plus eight v2 dry-run Workflow fingerprints match baseline.

- [ ] **Step 6: Commit Task 10**

```bash
git add .github/release/production docs/ucm-production-release-workflow-implementation.md docs/superpowers/specs/2026-08-13-ucm-production-release-workflow-design.md
git commit -m "test(release): verify production lifecycle scenarios"
```

### Task 11: Push, Run Hosted Read-only Validation, and Merge Trusted Control

**Files:**
- Remote branch: `feature/release-lifecycle-dry-run-v2`
- Existing Draft PR: `SuperMarioYL/unified-cache-management#1`
- Repository settings: Actions default permissions, default-branch protection, production Environment, and tag rulesets.

**Interfaces:**
- Consumes the fixed local commit from Task 10.
- Produces a default-branch `control_sha` that contains the trusted controller and a successful Hosted read-only candidate/control validation before real Tags.

- [ ] **Step 1: Push the complete implementation branch**

Run:

```bash
git push origin feature/release-lifecycle-dry-run-v2
gh pr view 1 --repo SuperMarioYL/unified-cache-management --json headRefOid,isDraft,statusCheckRollup
```

Expected: remote head equals local HEAD; no production Tag or package has been created.

- [ ] **Step 2: Inspect all Hosted checks and artifacts**

Wait for Push Checks, legacy/v2/production tests, actionlint, and the real candidate build lane. Download and reopen relevant Artifacts; bind run IDs, attempts, source SHA, and digests in the implementation report. Fix on the feature branch and repeat Tasks 9-10 for any failure.

- [ ] **Step 3: Audit repository policy before mutation**

Use read-only `gh api` calls to snapshot repository default permissions, default branch, rulesets, `release-production`, required reviewer IDs, deployment branch policies, package access, and existing `main` Tag conflicts. Save only non-secret JSON evidence under the implementation report directory.

- [ ] **Step 4: Apply the minimum approved repository settings**

Set default Workflow permissions to read, keep PR approval disabled for Actions, protect the default branch and `0.6.0-release`, add immutable `draft/v*` and `v*` Tag rules, forbid a Tag matching the default branch, and configure `release-production` with required reviewer `SuperMarioYL`, no admin bypass, and selected deployment branch equal to the protected current default branch.

After every write, GET the setting again and compare the exact expected state. Do not create Tags in this step.

- [ ] **Step 5: Mark PR ready and merge only after required checks pass**

Use the repository's permitted merge strategy. Reopen the merged default-branch SHA, verify all six new Workflow files and production package fingerprints, and record it as `control_sha`.

- [ ] **Step 6: Commit any evidence-only documentation update and push**

If hosted run IDs are added to the report, make one documentation commit before merge or a follow-up PR; do not rewrite code evidence after the fixed-tree verdict.

### Task 12: Create the `0.6.0-release` Branch and Validate a Real Draft Tag

**Files:**
- Branch-only modify: `version.ini`
- Branch-only modify: `charts/ucm/Chart.yaml`
- Branch-only modify: any version fields explicitly enumerated by production config validation.
- Remote objects: branch `0.6.0-release`, annotated Tag `draft/v0.6.0-1`, private GHCR packages, GitHub Draft Release.

**Interfaces:**
- Produces real Draft production evidence bound to the release-branch commit and remote readback.

- [ ] **Step 1: Create the release branch from exact trusted default control SHA**

Create local branch `0.6.0-release` from `control_sha`; change only version-bearing source files required by the config validator from `0.5.0rc1` to `0.6.0`. Add/update tests proving the release-branch tree differs from control only in approved version files and release metadata.

- [ ] **Step 2: Commit, push, and protect the release branch**

Push the branch, verify the remote head twice, apply branch protection/ruleset, and GET it back. Candidate and controller Workflow/control files on the branch must match `control_sha` exactly.

- [ ] **Step 3: Create and push the annotated Draft Tag**

Create `draft/v0.6.0-1` with an explicit message containing the release line and source SHA. Verify locally that it is an annotated object and remotely that the Tag object peels to the exact release-branch head before allowing the candidate run to proceed.

- [ ] **Step 4: Verify the read-only candidate run**

Wait for `UCM Production Tag Candidate`; inspect all wheel/Chart/image jobs, download the aggregate, reopen its envelope, and verify source/config/product closure. If it fails, do not move/delete the Tag; fix the release branch and create `draft/v0.6.0-2` instead.

- [ ] **Step 5: Approve `release-production` and verify Draft publication**

Approve the waiting deployment only after the trusted plan shows `stage=draft`, the expected source SHA, six wheel assets, three private image repositories, one Chart asset, no external channels, and no conflicts.

Verify through fresh APIs:

- GHCR packages are private and authenticated readback returns the expected six members/three indexes.
- anonymous GHCR access is denied as private, not missing.
- GitHub Release is `draft=true`, `prerelease=false` and contains exactly the planned assets.
- downloaded asset SHA256 values equal the final manifest.
- evidence says `environment-test=waived-for-preview`, not `passed`.

- [ ] **Step 6: Record Draft evidence without calling it public delivery**

Update the implementation report with run IDs, deployment ID, Tag object/source SHA, Release ID, package coordinates/digests, asset SHA256, and remaining hardware/cluster gaps. Commit and push through the normal branch/PR path; do not edit the immutable Draft Tag.

### Task 13: Validate the Real RC Tag, First-Package Visibility, and Idempotent Rerun

**Files:**
- Remote objects: annotated Tag `v0.6.0rc1`, public GHCR packages, Chart OCI, GitHub Pre-release.
- Documentation: `docs/ucm-production-release-workflow-implementation.md`.

**Interfaces:**
- Produces complete preview publication evidence. Does not create Stable/Hotfix Tags or publish PyPI/Docker Hub.

- [ ] **Step 1: Confirm Draft/source lineage and create the annotated RC Tag**

Require the Draft candidate/production evidence to be complete and source SHA identical to current `0.6.0-release` head. Create annotated `v0.6.0rc1`, push it, and double-read the Tag object/peeled commit/branch head.

- [ ] **Step 2: Verify the RC candidate and approve the exact production plan**

The approval summary must show public GHCR coordinates, Chart `0.6.0-rc.1`, GitHub Pre-release assets, no PyPI/Docker Hub, and inherited preview waiver. Reject any different source, extra channel, conflict, or missing candidate member.

- [ ] **Step 3: Handle first-created package visibility without changing the Tag**

If the first approved run reports `visibility-configuration-required`, verify the pushed digests through authenticated readback and that the GitHub Release remains Draft. Change only the three formal image packages and Chart package to public in the GitHub UI/API, GET the new visibility, then rerun the same controller attempt family. Do not create `rc2` for a visibility-only handshake.

- [ ] **Step 4: Verify complete RC publication through fresh anonymous reads**

Verify three multi-platform GHCR indexes, all six member manifests/configs/layers, Chart OCI manifest/config/layer, GitHub `prerelease=true`/`draft=false`/`make_latest=false`, and every asset download checksum. Record the final production evidence digest.

- [ ] **Step 5: Rerun the same RC controller for idempotency**

Expected: every remote item is `identical/reused`; zero overwrite/delete/new-version operations; the final content identity matches the first complete run. A changed digest is a blocker, not a new revision.

- [ ] **Step 6: Exercise Stable/Hotfix negative gates only**

Using offline CLI fixtures and non-writing Workflow plan paths, prove Stable is blocked by preview-waived environment evidence, Hotfix is blocked without previous-Stable lineage, missing PyPI/Docker Hub authority blocks before writes, and a remote coordinate conflict blocks all operations. Do not push `v0.6.0` or `v0.6.1`.

- [ ] **Step 7: Publish the final evidence report through a normal PR**

The report separates local tests, Hosted candidate/controller, GHCR/Packages/Release readback, preview waiver, hardware, cluster, and public delivery. Only GHCR/Chart/Pre-release layers with fresh readback are marked complete.

## Final Completion Gate

The implementation is complete only when Tasks 1-10 are green on a fixed local tree, Task 11 establishes trusted control on the repository default branch, Task 12 produces a real private Draft channel with readback, and Task 13 produces a real public RC channel with anonymous readback plus an identical no-op rerun. The final state must preserve all legacy/v2 Workflow fingerprints, retain immutable Draft/RC Tags, create no Stable/Hotfix Tag, publish nothing to PyPI/Docker Hub, and explicitly leave GPU/NPU and Kubernetes acceptance unresolved.
