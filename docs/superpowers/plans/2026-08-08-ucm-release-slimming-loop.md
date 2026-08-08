# UCM Release Automation Slimming and Loop Engineer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Every task uses RED -> implementation -> focused verification -> review.

**Goal:** Replace the oversized release subsystem with four workflows and one small package under `.github/release`, then push only `origin/feature/cicd` and prove the fork dry-run loop on GitHub Actions.

**Architecture:** Workflows orchestrate; `.github/release/ucm_release` owns deterministic planning and verification. Fork branch runs are read-only, fixture-only, and unpublished. Production paths remain fail-closed until protected infrastructure exists.

**Tech Stack:** Python, GitHub Actions, Docker Buildx, Helm 3, crane, pytest, actionlint.

## Global Constraints

- Push only `origin HEAD:refs/heads/feature/cicd`; never push `upstream`, force-push, create a PR, Git tag, GitHub Release, or GHCR package.
- Precisely stage release files. Never stage the existing C++ changes in `ucm/store/compress/cc/` and never use `git add -A`.
- Fork validation permissions are exactly `contents: read`; no write scopes, secrets, environments, self-hosted runners, login, dispatch, or image push.
- Pin every third-party action to a full commit SHA.
- Keep `charts/ucm` as the product Chart. Remove root `release/`, `scripts/release/`, and `docker/release/`.
- Keep `wrapt==1.17.2` as the ordinary Python runtime dependency; remove every standalone wrapt release artifact/config/bundle.
- Keep unverified production capabilities `blocked` or `external-required`; never call fixture evidence production evidence.
- Public image repository basenames remain `vllm-openai` and `vllm-ascend`; immutable tag is `<exact-upstream-tag>-ucm-<ucm-version>-rN`.
- Ascend accepts A2 and A3 only; reject 310P, A5, nightly, dev, custom, and explicit `-a2`.

---

### Task 1: Baseline, Safety Guard, and RED Contract

**Files:**
- Create: `docs/superpowers/plans/2026-08-08-ucm-release-slimming-loop.md`
- Create: `.github/release/tests/test_config.py`
- Create: `.github/release/tests/test_workflows.py`

**Deliverable:** A failing structural contract that demands four release workflows, at most eight package files, three schemas, four Docker files, no legacy release roots, no `/opt/ucm-release`, no standalone wrapt bundle, and read-only fork jobs.

**Verification:** Run the two tests and record the expected RED failures against the current tree. Record Git/GitHub pre-write snapshots and the exact do-not-stage paths.

### Task 2: Compact Configuration and Core Artifact Package

**Files:**
- Create: `.github/release/release.yaml`
- Create: `.github/release/compatibility.yaml`
- Create: `.github/release/schemas/config.schema.json`
- Create: `.github/release/schemas/release-manifest.schema.json`
- Create: `.github/release/schemas/image-result.schema.json`
- Create: `.github/release/ucm_release/{__init__,cli,core,wheel,chart}.py`
- Create: `.github/release/tests/test_core_release.py`
- Modify: `setup.py`

**Interfaces:**
- `python -m ucm_release config validate`
- `python -m ucm_release core plan`
- `python -m ucm_release wheel inspect`
- `python -m ucm_release chart package`

**Deliverable:** Strict two-file configuration, exact version/wheel/Chart binding, deterministic Chart archive, candidate release manifest, and explicit production blockers. GitHub Release remains the only planned Chart publication target.

**Verification:** Tests cover unknown fields, unresolved locks, wheel SHA/metadata, version agreement, CUDA/A2/A3 Helm lint/template/package, and deterministic repeat packaging.

### Task 3: Registry Reconciliation and Loop Evidence

**Files:**
- Create: `.github/release/ucm_release/registry.py`
- Create: `.github/release/ucm_release/verify.py`
- Create: `.github/release/tests/test_registry_reconcile.py`

**Interfaces:**
- `python -m ucm_release registry scan`
- `python -m ucm_release reconcile`
- `python -m ucm_release loop verify`

**Deliverable:** crane-backed read-only discovery plus deterministic fixture inventory. Build keys bind release manifest, wheel, upstream index/platform digest, compatibility rule, and implementation digest. Registry inventory is the production source of truth; no custom state database remains.

**Verification:** New input -> one task; identical input -> zero; tag digest drift -> r2 without changing r1; complete digest chain; required failure blocks; successful fixture candidate followed by full zero reconcile. Strict A2/A3 inclusion and 310P/A5 exclusion.

### Task 4: Install-Only Image Builder

**Files:**
- Create: `.github/release/ucm_release/image.py`
- Create: `.github/release/docker/Dockerfile`
- Create: `.github/release/docker/install_ucm.py`
- Create: `.github/release/docker/inspect_runtime.py`
- Create: `.github/release/docker/verify_base_image.py`
- Create: `.github/release/tests/test_image_build.py`

**Interfaces:**
- `python -m ucm_release image verify`

**Deliverable:** One CUDA/Ascend-neutral Dockerfile which receives a digest-pinned base and one exact UCM wheel, resolves ordinary Python dependencies, never receives UCM source, and outputs local OCI metadata only.

**Verification:** Exact wheel/base digest checks; base without wrapt installs `wrapt==1.17.2` through ordinary wheel metadata; `pip check` and imports; no UCM compilation; wrong base/wheel/gate blocks; local fixture OCI output is deterministic.

### Task 5: Four Workflow Orchestration and Fork Candidate Lane

**Files:**
- Replace: `.github/workflows/_build-wheel.yml`
- Replace: `.github/workflows/_build-image.yml`
- Replace: `.github/workflows/release-ucm.yml`
- Replace: `.github/workflows/release-vllm-images.yml`
- Modify: `.github/workflows/lint-and-test.yml`
- Delete: the other six release workflows

**Deliverable:** A `feature/**` branch push starts `release-ucm` in fork-candidate mode, which invokes all four release workflow files at the same SHA, builds a fixture wheel, packages the Chart, builds an OCI candidate, reconciles twice, and uploads three-day diagnostic artifacts. It never publishes. Production jobs require the upstream repository, release tag, and protected environment.

**Verification:** actionlint all workflows; static permission/action pinning checks; fork job path proves all reusable workflows ran, no write scopes/secret/environment/login/push, and evidence source SHA equals the commit SHA.

### Task 6: Legacy Deletion, Documentation, and Full Local Closure

**Files:**
- Delete: root `release/`, `scripts/release/`, `docker/release/`
- Delete: `pr-build-artifacts.yml` and obsolete release images/fixtures/tests
- Modify: `docs/ucm-release-automation-technical-review.md`
- Modify: `docs/ucm-release-automation-detailed-design.md`
- Modify: `.gitignore`

**Deliverable:** No stale paths or ignored-report links; docs describe only the slim implementation and evidenced candidate scope. Local generated `.DS_Store`, stale macOS wrapt wheel, `stage/`, caches, and obsolete ignored evidence are removed only after references are gone.

**Verification:** Focused release tests, repository lint/unit tests, actionlint, strict schema/config checks, Helm checks, Buildx smoke, six reconciliation scenarios, stale reference scans, `git diff --check`, and exact structural budgets.

### Task 7: GitHub Loop Engineer Push, Fix, Rerun, and Evidence

**Files:**
- Modify after first green run: the two official release documents with real fork run evidence

**Deliverable:** Exact release files are committed without user C++ changes and pushed only with `git push origin HEAD:refs/heads/feature/cicd`. `Push Commit Checks` and `Release UCM core artifacts` complete on the pushed SHA; nested evidence proves the other three release workflows executed.

**Loop:** For each failed pushed SHA, collect `gh run view --log-failed`, job JSON, and artifacts; classify C0-C5; reproduce deterministic failures locally; add a regression; commit and push a new SHA. Allow one rerun only for transient external failures. Maximum five repair pushes, eight runs, 45 minutes per run, six hours total, and stop after the same root cause repeats three times.

**Success:** The final SHA push run and one same-SHA rerun are green; deterministic payload digests match; second full reconcile has zero tasks; fork PR/tag/Release/package and upstream SHA snapshots are unchanged; production remains blocked for exact allowlisted external conditions.
