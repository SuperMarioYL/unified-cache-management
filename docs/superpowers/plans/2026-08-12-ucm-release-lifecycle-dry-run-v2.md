# UCM Release Lifecycle Dry-run v2 Implementation Plan

> **For Codex:** Execute this plan with `superpowers:subagent-driven-development`, `superpowers:test-driven-development`, and `superpowers:verification-before-completion`. Use one implementation agent at a time, review every task independently, and do not commit, stage, push, publish, or mutate repository settings.

**Goal:** Add a fail-closed, read-only release lifecycle control plane that models PR, develop, Nightly, Draft, RC, Stable, and Hotfix delivery; produces reviewable plans and evidence contracts; and exercises failure, permission, conflict, retention, and idempotency scenarios without writing to GitHub Releases, registries, package indexes, pull requests, issues, tags, Actions, or repository settings.

**Architecture:** Keep the existing `.github/release/ucm_release` implementation, existing workflows, and the published `v0.5.0rc1` path frozen. Add an isolated v2 package under `.github/release/v2/`, strict JSON Schemas, repository-owned YAML configuration, deterministic CLIs, and eight new dry-run workflows with only `contents: read`. Four manual entry workflows are data-only callers of the exact validation-repository reusable controller at `@main`; executable manual logic is centralized in that called workflow. The v2 layer is a planning, validation, reconciliation-preview, cleanup-preview, and policy-audit control plane: every operation is explicitly `executed: false`, all identities bind to an exact 40-character source SHA, and simulated environment evidence can never satisfy a production gate.

**Tech stack:** Python 3.12-compatible standard library, PyYAML, jsonschema, packaging, pytest, GitHub Actions, actionlint, JSON Schema Draft 2020-12, Markdown Job Summaries, Actions Artifacts.

---

## Global constraints and baseline

- Work only in `/Users/yulei/workspace/unified-cache-management/.orbit/worktrees/release-lifecycle-dry-run-v2` on branch `feature/release-lifecycle-dry-run-v2`.
- Starting commit is `9a8e236f9412cfa1f8d3ebf57ba067b77ab8040b`.
- Verified baseline is `544 passed, 1 skipped` from `python3 -m pytest -q .github/release/tests`.
- Preserve the original dirty `feature/cicd` worktree and all user-owned files in it.
- Do not edit existing release workflows, `.github/release/ucm_release`, existing schemas, or the current `v0.5.0rc1` Release path unless a test proves an isolated compatibility defect and the user authorizes that expansion.
- Do not commit, stage, push, create a PR, create a tag, publish an artifact, log in to a registry, change a repository rule, or call a write-capable GitHub API.
- New workflows must declare only `permissions: contents: read`. Actions Artifact upload and `$GITHUB_STEP_SUMMARY` are allowed; GitHub state changes are not.
- Python code must not hard-code the repository owner. Repository coordinates, branches, products, retention windows, and environment policy live in `.github/release/v2/release.yaml`.
- The validation repository is the user's fork; the production coordinate is configured separately. Both are data, never implied publication authority.
- Existing `uc-manager` packaging remains unmodified and independently usable, but the v2 environment guard must never report legacy `uc-manager` as compatible. The v2 model accepts exactly one of three mutually exclusive distributions: `uc-manager-cuda`, `uc-manager-cann-a2`, and `uc-manager-cann-a3`, all importing `ucm`; any other provider of the `ucm` top-level import is unsafe.
- Each manual `workflow_dispatch` file must contain only one data-only reusable job calling exactly `SuperMarioYL/unified-cache-management/.github/workflows/release-control-dry-run.yml@main`; wrappers may not contain `runs-on`, `steps`, or Actions. Before checkout or v2 CLI execution, the called controller must perform two exact non-redirecting GETs of `main`, validate duplicate-free commit JSON and identical 40-hex SHAs equal to the required `job.workflow_sha` identity projection, bind required `job.workflow_repository`/`job.workflow_file_path`/`job.workflow_ref` fields to the exact validation-repository controller at `refs/heads/main`, ignore unrelated GitHub-generated job fields, enforce the caller-repository allowlist, and checkout only that verified SHA. The security scanner must require an exact ordered step-name/type sequence for every executable job, with the expected and observed job mapping equal, and bind both trust-critical embedded validators to exact workflow/job/index/name/body identities. Hosted eligibility must retain the called-workflow identity. Because GitHub gives a same-named tag precedence over a branch for reusable `@main`, rulesets must prevent a tag named `main` and protect wrapper/controller changes, or a follow-up commit must pin wrappers to the immutable controller commit SHA; code alone cannot close a malicious tag-shadow controller or selected branch that removes its own gate.
- All positive and negative tests must be deterministic and offline. Live GitHub, Registry, Release, hardware, accelerator, and cluster evidence remain explicitly unexecuted or external-required.

## Canonical v2 contracts

- `lifecycle-plan.json`: stage, trigger, repository role, exact source SHA, deterministic product coordinates, gates, blockers, and read-only operations.
- `artifact-manifest.json`: complete product inventory, file SHA256 or OCI digest/platform identity, plan binding, and validation results.
- `environment-test-request.json`: blue/yellow environment request bound to a lifecycle plan and artifact manifest.
- `environment-test-result.json`: environment identity, artifact closure, checks, evidence level, and verdict. Dry-run only permits `evidence_level: simulated`; production gates stay blocked.
- `cleanup-plan.json`: retain/delete candidates, shared-reference protection, reasons, failures, and `executed: false` operations.
- `release-preview.md`: user-facing Wheel/Image/Chart install paths, compatibility, evidence level, known issues, and blockers.

---

### Task 1: Establish the v2 configuration, schemas, and lifecycle planner

**Files:**
- Add: `.github/release/v2/release.yaml`
- Add: `.github/release/v2/ucm_release_v2/{__init__.py,__main__.py,cli.py,common.py,config.py,lifecycle.py}`
- Add: `.github/release/v2/schemas/{lifecycle-plan,release-intent}.schema.json`
- Add: `.github/release/v2/tests/{conftest.py,test_config.py,test_lifecycle.py}`
- Add: `.github/release/v2/README.md`

- [ ] Write RED tests for all seven stages, exact source-SHA enforcement, trigger/ref routing, repository-role validation, version formats, deterministic output, retention lookup, owner-free Python, and `mode=dry-run` immutability.
- [ ] Add strict YAML configuration for production/validation repositories, `develop`/`main`, product matrices, three Wheel distributions, image families, Chart, environments, and 7/14/30-day retention.
- [ ] Implement `lifecycle plan` with stage-specific versions and channels: PR, develop, Nightly, Draft, RC, Stable, and Hotfix.
- [ ] Require release intent for Draft/RC/Stable/Hotfix, bind it to the exact source SHA, and reject ambiguous or contradictory stage/ref/version combinations.
- [ ] Emit canonical JSON with stable ordering and a self-independent SHA256 envelope.
- [ ] Verify focused tests, all v2 tests, baseline release tests, and `git diff --check`.

### Task 2: Model the three backend Wheel distributions and reject mixed installs

**Files:**
- Add: `.github/release/v2/ucm_release_v2/wheels.py`
- Add: `.github/release/v2/packaging/{cuda,cann-a2,cann-a3}/distribution.json`
- Add: `.github/release/v2/packaging/backend_guard.py`
- Add: `.github/release/v2/tests/test_wheels.py`
- Modify: `.github/release/v2/release.yaml`
- Modify: `.github/release/v2/ucm_release_v2/{cli.py,lifecycle.py}`

- [ ] Write RED tests for distribution/import mapping, equal formal versions, mutually exclusive backend markers, legacy-package conflict, two-or-three-backend mixtures, malformed metadata, and deterministic Wheel coordinates.
- [ ] Implement `wheel plan` and `wheel check-environment` using installed-distribution metadata supplied as a fixture or collected read-only from the current Python environment.
- [ ] Add an install-time/runtime guard that fails with actionable uninstall/install commands when more than one UCM distribution is present.
- [ ] Keep backend selection explicit; never infer CUDA/CANN from host hardware in the planner.
- [ ] Verify focused tests, isolated synthetic metadata scenarios, all v2 tests, baseline release tests, and `git diff --check`.

### Task 3: Collect and validate content-addressed artifact manifests

**Files:**
- Add: `.github/release/v2/ucm_release_v2/artifacts.py`
- Add: `.github/release/v2/schemas/artifact-manifest.schema.json`
- Add: `.github/release/v2/tests/test_artifacts.py`
- Modify: `.github/release/v2/ucm_release_v2/cli.py`

- [ ] Write RED tests for missing/extra products, wrong plan digest, wrong file checksum, malformed OCI digest, duplicate coordinates with conflicting content, platform gaps, stage/version drift, and stable-order reproducibility.
- [ ] Implement `artifacts collect` for local files and declarative OCI records without network access.
- [ ] Implement `artifacts validate` with exact product-set closure against the lifecycle plan.
- [ ] Record each validation gate independently; do not collapse local checksum evidence into Registry or runtime evidence.
- [ ] Verify focused mutation tests, all v2 tests, baseline release tests, and `git diff --check`.

### Task 4: Add PR, develop, and Nightly read-only workflows

**Files:**
- Add: `.github/workflows/pr-release-dry-run.yml`
- Add: `.github/workflows/develop-release-dry-run.yml`
- Add: `.github/workflows/nightly-release-dry-run.yml`
- Add: `.github/workflows/release-control-dry-run.yml`
- Add: `.github/release/v2/ucm_release_v2/commands.py`
- Add: `.github/release/v2/tests/{test_commands.py,test_workflows.py}`
- Modify: `.github/release/v2/ucm_release_v2/cli.py`

- [ ] Write RED tests for fork PR head/base SHA separation, stale PR SHA, unsupported comment commands, unauthorized command previews, cancel/status no-write behavior, branch routing, Nightly date/run identity, concurrency keys, and permissions.
- [ ] Implement a pure `command parse` CLI with exact ASCII grammar `/release build <40-lowercase-hex-sha>`, `/release status`, and `/release cancel`; build must bind the requested SHA to two current PR observations, and all results are plans or explanations that never mutate Actions or PR state.
- [ ] Load the develop controller from the default branch via `workflow_run` for the exact successful same-repository `Push Commit Checks` `push` run at `.github/workflows/push-check.yml@develop`; treat its head SHA only as data and never checkout/import/execute develop-controlled code.
- [ ] Add workflows that generate lifecycle plans, validate them, upload preview artifacts, and write Job Summaries with `contents: read` only.
- [ ] Pin every Action to a full commit SHA; ban `pull_request_target`, write permissions, registry login, publication commands, and interpolated shell execution.
- [ ] Verify YAML parsing, workflow policy tests, actionlint when available, all v2 tests, baseline release tests, and `git diff --check`.

### Task 5: Add Draft and blue/yellow environment request/result contracts

**Files:**
- Add: `.github/release/v2/ucm_release_v2/environment.py`
- Add: `.github/release/v2/schemas/{environment-test-request,environment-test-result}.schema.json`
- Add: `.github/release/v2/tests/test_environment.py`
- Add: `.github/workflows/draft-environment-dry-run.yml`
- Modify: `.github/release/v2/ucm_release_v2/cli.py`

- [ ] Write RED tests for plan/manifest/environment identity mismatch, replayed results, missing required products, duplicate checks, forged production evidence, unsupported environment names, rejected verdicts, and simulated-pass gate semantics.
- [ ] Implement `environment export` for blue/yellow requests and `environment verify` for results.
- [ ] Require nonce/request SHA, exact source SHA, plan SHA, manifest SHA, environment identity, and complete required check set.
- [ ] A valid simulated pass is recorded as useful dry-run evidence but must always emit `production_gate: blocked`.
- [ ] Reconciliation may classify environment evidence as `draft-passed`/`draft-failed` only when the original Draft lifecycle plan and artifact manifest are reopened and reproduce the request exactly; request/result-only evidence remains explicitly unanchored.
- [ ] Add a data-only manual wrapper that passes Draft inputs to the exact trusted reusable controller at `@main`; the controller creates request/result fixtures and summaries without contacting a cluster.
- [ ] Verify focused positive/negative tests, workflow policy tests, all v2 tests, baseline release tests, and `git diff --check`.

### Task 6: Preview RC, Stable, and Hotfix reconciliation and Releases

**Files:**
- Add: `.github/release/v2/ucm_release_v2/{reconcile.py,render.py}`
- Add: `.github/release/v2/schemas/reconcile-plan.schema.json`
- Add: `.github/release/v2/tests/{test_reconcile.py,test_render.py}`
- Add: `.github/workflows/release-lifecycle-dry-run.yml`
- Modify: `.github/release/v2/ucm_release_v2/cli.py`

- [ ] Write RED tests for absent target/create preview, identical target/no-op, conflicting target/blocker, partial family conflict, rerun determinism, Stable-from-unaccepted-RC, Hotfix base mismatch, and attempted write execution.
- [ ] Implement `reconcile plan` against an offline inventory snapshot. Every operation has `executed: false`; conflicts fail closed.
- [ ] Bind Stable/Hotfix promotion evidence to a reopened source lifecycle plan and artifact manifest. Stable requires accepted same-source RC lineage; Hotfix requires the immediately previous Stable lineage and may use a new target SHA. Unanchored promotion declarations remain blocked.
- [ ] Implement `release render` with install commands for all three Wheels, exact image coordinates/digests/platforms, Chart install command, compatibility table, evidence boundary, known issues, and blockers.
- [ ] Add a data-only manual RC/Stable/Hotfix wrapper to the trusted reusable controller, with only read permissions and no publication code path.
- [ ] Verify focused tests, markdown golden tests, workflow policy tests, all v2 tests, baseline release tests, and `git diff --check`.

### Task 7: Plan retention cleanup and audit repository policy read-only

**Files:**
- Add: `.github/release/v2/ucm_release_v2/{cleanup.py,policy.py}`
- Add: `.github/release/v2/schemas/{cleanup-plan,repository-policy-report}.schema.json`
- Add: `.github/release/v2/tests/{test_cleanup.py,test_policy.py}`
- Add: `.github/workflows/{release-cleanup-dry-run.yml,repository-policy-audit-dry-run.yml}`
- Modify: `.github/release/v2/ucm_release_v2/cli.py`

- [ ] Write RED tests for 7/14/30-day boundaries, timezone normalization, shared digests, live release references, malformed inventory, missing timestamps, duplicated records, delete failures, exact retry determinism, and forbidden protected-channel deletion.
- [ ] Implement `cleanup plan` from an offline inventory snapshot. Preserve shared/live content and emit only non-executing delete proposals.
- [ ] Write RED policy tests for missing `main`, unprotected `develop`, missing exact tag rules, Environment-policy drift, insufficient workflow permissions, and owner/repository mismatches.
- [ ] Implement `repo-policy audit` against a snapshot; the workflow may gather state through read-only `gh api` calls but may not call any mutating endpoint.
- [ ] Keep cleanup and policy manual files as data-only wrappers to the exact trusted `@main` reusable controller; executable steps remain centralized and read-only.
- [ ] Verify focused boundary/mutation tests, workflow policy tests, all v2 tests, baseline release tests, and `git diff --check`.

### Task 8: Close the scenario matrix, security audit, and documentation

**Files:**
- Add: `.github/release/v2/tests/test_scenarios.py`
- Add: `.github/release/v2/tests/fixtures/**`
- Modify: `.github/release/v2/README.md`
- Add: `docs/ucm-release-lifecycle-dry-run-v2-implementation.md`
- Read only: `docs/ucm-development-test-release-process.md` in the protected main worktree
- Modify only v2 code/workflows where integration tests expose a defect.

- [ ] Add end-to-end offline scenarios for all lifecycle stages and intentional failures: stale SHA, mixed backend install, checksum drift, environment mismatch, forged evidence, same-content rerun, target conflict, retention boundary, shared digest, and repository-policy gaps.
- [ ] Add static scans proving no v2 workflow or Python module can publish, delete, comment, dispatch, approve, tag, change settings, or acquire write permissions.
- [ ] Run every CLI from a clean temporary directory and validate every emitted JSON document against its schema.
- [ ] Run full v2 tests, the unchanged legacy release suite, actionlint, pre-commit checks relevant to changed files, YAML/JSON validation, Python compile checks, and `git diff --check`.
- [ ] Update documentation with exact commands, artifact relationships, tested outcomes, evidence layers, and explicit external blockers. Do not describe local/simulated results as hosted, Registry, hardware, or cluster evidence.
- [ ] Request a whole-diff independent review, resolve findings, rerun the complete verification set, and report uncommitted changed files plus remaining external-only validation.

## Completion gate

The plan is complete when every v2 contract is schema-valid and deterministic, all seven lifecycle stages have positive and adversarial tests, three backend Wheel distributions reject mixed installs, all workflows are statically proven read-only, simulated environment results cannot satisfy production gates, cleanup protects shared/live content, reconciliation is idempotent and conflict-safe, legacy release tests remain green, and the final diff contains no edits to the existing production release path. Hosted Actions, Registry/Release publication, accelerator/runtime checks, and live cluster acceptance remain separately unexecuted until the user explicitly authorizes them.
