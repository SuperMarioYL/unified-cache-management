# CICD Growth-Safe Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the active release pipeline tolerate newly discovered Dockerfiles, Builders, and upstream versions while selecting a bounded latest-compatible matrix and preserving catalog-derived publication closure.

**Architecture:** Discovery is expansive and admission is target-local. Registry candidates are grouped by product and variant, evaluated newest-first, and either selected or recorded as canonical exclusions; formal task and asset sets are derived from the catalog. Opaque workflow byte seals are deleted while functional source, artifact, plan, and OCI hashes remain.

**Tech Stack:** Python 3.12, PyYAML, `packaging`, pytest, GitHub Actions YAML, OCI registry fixtures, JSON Schema.

**Spec:** `docs/superpowers/specs/2026-08-21-cicd-growth-safe-discovery-design.md`

## Global Constraints

- Unknown runtime versions create target-level exclusions and no build or publication tasks.
- Runtime-patch and compatibility overlap remains a hard failure.
- Explicit `strategy: none` remains a supported runtime strategy.
- Registry transport errors, selected Builder ambiguity, and resource-limit overflow remain hard failures.
- Formal publication validates the exact catalog-derived task and asset sets.
- Do not restore workflow byte fingerprints, reviewed-digest tables, or static audit seals.
- Preserve functional source, artifact, resolved-plan, Docker implementation, and OCI digest checks.
- Do not create a tag, GitHub Release, or public package during hosted verification.

---

### Task 1: Freeze the Upstream-Integrated CICD Baseline

**Files:**
- Commit: all existing CICD/Builder changes already present in the working tree
- Include: `docs/superpowers/specs/2026-08-21-cicd-growth-safe-discovery-design.md`
- Include: `docs/superpowers/plans/2026-08-21-cicd-growth-safe-discovery.md`

**Interfaces:**
- Consumes: `upstream/develop@e354ce52da3a18c4551ffc77c077e6a3eb9cd7ef`
- Produces: a reviewable baseline commit on `feature/cicd-growth-safe-discovery`

- [ ] **Step 1: Verify the branch and index boundary**

Run:

```bash
git branch --show-current
git rev-parse HEAD
git diff --cached --quiet
git diff --name-only --diff-filter=U
```

Expected: branch `feature/cicd-growth-safe-discovery`, HEAD `e354ce52da3a18c4551ffc77c077e6a3eb9cd7ef`, empty index, and no unmerged paths.

- [ ] **Step 2: Run the baseline release checks**

Run:

```bash
python3 -m pytest -q .github/release/tests
python3 -m pytest -q .github/release/v2/tests
actionlint
git diff --check
```

Expected: main `292 passed, 1 skipped`, v2 `565 passed`, actionlint clean, and diff check clean. Production may retain only its inherited workflow-fingerprint failure, which Task 6 deletes.

- [ ] **Step 3: Commit the integrated baseline**

```bash
git add -A
git commit -m "feat(release): integrate cicd pipeline on upstream develop"
```

Expected: one baseline commit and a clean worktree.

---

### Task 2: Remove Exhaustive Repository and Snapshot Budgets

**Files:**
- Modify: `.github/release/ucm_release/core.py:1028-1171`
- Modify: `.github/release/tests/test_repository_recipes.py:1-35`
- Modify: `.github/release/tests/test_config.py:80-140`
- Modify: `.github/release/tests/test_builders.py:30-65`
- Modify: `.github/docker-recipes.yaml`

**Interfaces:**
- Consumes: declared `docker_recipes` entries
- Produces: `validate_repository_recipe_inventory(catalog: dict[str, Any], *, repository_root: Path) -> None` that validates declared recipes without requiring exhaustive registration

- [ ] **Step 1: Write a failing undeclared-Dockerfile test**

Replace the exact-inventory assertion with a behavioral test:

```python
def test_unregistered_future_dockerfile_does_not_block_declared_recipes(
    tmp_path: Path,
) -> None:
    catalog = core.load_catalog()
    docker_root = tmp_path / "docker"
    docker_root.mkdir()
    for recipe in catalog["docker_recipes"]:
        source = ROOT / recipe["path"]
        target = tmp_path / recipe["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    (docker_root / "Dockerfile.ucm-vllm-ascend.a2-v0.99.0").write_text(
        "ARG IMAGE_SOURCE=\"quay.io/ascend\"\n"
        "ARG IMAGE_NAME_VERSION=\"vllm-ascend:v0.99.0\"\n"
        "FROM ${IMAGE_SOURCE}/${IMAGE_NAME_VERSION}\n",
        encoding="utf-8",
    )

    core.validate_repository_recipe_inventory(catalog, repository_root=tmp_path)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m pytest -q .github/release/tests/test_repository_recipes.py::test_unregistered_future_dockerfile_does_not_block_declared_recipes
```

Expected: FAIL containing `unregistered=['docker/Dockerfile.ucm-vllm-ascend.a2-v0.99.0']`.

- [ ] **Step 3: Remove only the exhaustive inventory comparison**

Delete the `discovered`, `missing`, and `discovered - registered_paths` check at the end of `validate_repository_recipe_inventory`. Keep the declared-path existence, symlink, base-image, runner, and lane checks. Remove the two manually added stable 0.23 recipes from `.github/docker-recipes.yaml`; they must no longer be required for catalog validity.

- [ ] **Step 4: Remove structural snapshot budgets**

In `test_release_tree_obeys_the_slim_structural_budget`, delete Python-file-count, exact-three-schema, and exact-Dockerfile-layout assertions. Retain concrete forbidden legacy roots, `/opt/ucm-release` references, and standalone wrapt checks. Change the Builder fixture test to assert:

```python
assert upstream
assert all(item["variant"] != "310p" for item in upstream)
assert {item["architecture"] for item in upstream} == {"amd64", "arm64"}
```

Keep future non-excluded variant and capability-deduplication tests; remove `len(upstream) == 8` and exact current tag snapshots.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
python3 -m pytest -q \
  .github/release/tests/test_repository_recipes.py \
  .github/release/tests/test_config.py \
  .github/release/tests/test_builders.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .github/docker-recipes.yaml .github/release/ucm_release/core.py \
  .github/release/tests/test_repository_recipes.py \
  .github/release/tests/test_config.py .github/release/tests/test_builders.py
git commit -m "refactor(release): remove exhaustive discovery budgets"
```

---

### Task 3: Add Probeable Candidate Admission

**Files:**
- Modify: `.github/release/ucm_release/core.py:604-630,1268-1330`
- Modify: `.github/release/tests/test_catalog_model.py`
- Modify: `.github/release/tests/test_catalog_resolution.py`

**Interfaces:**
- Produces: `find_runtime_patch_rule(manifest, snapshot, variant, *, relaxed=False) -> dict[str, Any] | None`
- Produces: `candidate_exclusion_reason(catalog, product, candidate, patch_manifest, *, relaxed=False) -> str | None`
- Preserves: `_matching_runtime_patch_rule(manifest, snapshot, variant, *, relaxed=False) -> dict[str, Any]` as the strict selected-target wrapper

- [ ] **Step 1: Write zero-match, explicit-none, and overlap tests**

Add tests that assert:

```python
assert core.find_runtime_patch_rule(manifest, unknown, "default") is None
assert core.find_runtime_patch_rule(manifest, explicit_none, "default")["strategy"] == "none"
with pytest.raises(ValueError, match="overlapping"):
    core.find_runtime_patch_rule(overlapping_manifest, known, "default")
```

Add a candidate test where every required architecture has no compatibility profile and assert `candidate_exclusion_reason(catalog, product, candidate, manifest) == "compatibility-unsupported"`.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m pytest -q \
  .github/release/tests/test_catalog_model.py \
  .github/release/tests/test_catalog_resolution.py -k 'runtime_patch or candidate_exclusion'
```

Expected: FAIL because the probe interfaces do not exist and zero matches currently raise.

- [ ] **Step 3: Implement probe and strict-wrapper separation**

Implement `find_runtime_patch_rule` with these exact outcomes:

```python
if not matches:
    return None
if len(matches) > 1:
    identifiers = ", ".join(rule["id"] for rule in matches)
    raise ValueError(
        f"resolved upstream matches overlapping runtime patch strategies: {identifiers}"
    )
return matches[0]
```

Make `_matching_runtime_patch_rule` call the probe and retain the existing zero-match exception for targets that registry already admitted. Refactor profile matching similarly so zero matches are probeable and overlaps still raise. `candidate_exclusion_reason` returns `runtime-patch-unsupported`, `compatibility-unsupported`, or `None` without swallowing malformed/overlap errors.

- [ ] **Step 4: Verify GREEN and strict runtime behavior**

Run:

```bash
python3 -m pytest -q \
  .github/release/tests/test_catalog_model.py \
  .github/release/tests/test_catalog_resolution.py \
  .github/release/tests/test_runtime_patch.py
```

Expected: PASS, including the installed dispatcher test that still raises on an unknown runtime.

- [ ] **Step 5: Commit**

```bash
git add .github/release/ucm_release/core.py \
  .github/release/tests/test_catalog_model.py \
  .github/release/tests/test_catalog_resolution.py
git commit -m "refactor(release): make candidate support probeable"
```

---

### Task 4: Select the Latest Admissible Target Per Product and Variant

**Files:**
- Modify: `.github/release/ucm_release/registry.py:470-545,630-810`
- Modify: `.github/release/tests/test_catalog_resolution.py`
- Modify: `.github/release/tests/fixtures/catalog-registry.json`

**Interfaces:**
- Consumes: `core.candidate_exclusion_reason(catalog, product, candidate, patch_manifest, *, relaxed=False)`
- Produces: one selected resolved upstream per configured `(product_id, variant)` when an admissible tag exists
- Produces exclusion reasons: `runtime-patch-unsupported`, `compatibility-unsupported`, `required-architecture-missing`, `superseded-compatible-version`

- [ ] **Step 1: Add failing latest-selection fixtures**

Add multiple compatible versions for each product/variant. Assert the highest admissible PEP 440 version is selected and each older candidate appears once in exclusions with `superseded-compatible-version`.

Add a newer tag with no runtime-patch rule and assert the next supported tag is selected while the newer tag is excluded with `runtime-patch-unsupported`.

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -m pytest -q .github/release/tests/test_catalog_resolution.py \
  -k 'latest or superseded or unsupported'
```

Expected: FAIL because every in-range tag is currently selected or aborts planning.

- [ ] **Step 3: Implement grouped newest-first admission**

Keep tag parsing and initial exclusions in `select_catalog_tags`, then group eligible candidates by `(product_id, variant)` and sort each group by `Version(version)` descending. For each group:

1. call `candidate_exclusion_reason` before registry reads;
2. record unsupported candidates and continue;
3. resolve the first statically admissible candidate;
4. catch only `RegistryBlocker` codes prefixed `missing-linux-`, record `required-architecture-missing`, and continue;
5. re-check the inspected variant;
6. select the first fully admissible snapshot;
7. record all remaining candidates as `superseded-compatible-version` without resolving them.

Do not catch transport/tool errors or overlap exceptions. Apply `max_selected_upstreams` after this partition. Canonically sort the combined exclusions before hashing the resolved plan.

- [ ] **Step 4: Apply identical semantics to pinning**

An unsupported pinned tag yields one exclusion and no task. It must never fall back to another tag. Keep PR pin matrices empty as today.

- [ ] **Step 5: Permit explained partial feature plans**

Update `_pr_smoke_projection` so a selector absent because its product/variant has an explicit exclusion does not abort feature-candidate planning. Unexplained selector absence remains an error. Protected full-loop validation remains separate and strict.

- [ ] **Step 6: Verify GREEN**

Run:

```bash
python3 -m pytest -q .github/release/tests/test_catalog_resolution.py
```

Expected: PASS with canonical selected/excluded counts and matrices.

- [ ] **Step 7: Commit**

```bash
git add .github/release/ucm_release/registry.py \
  .github/release/tests/test_catalog_resolution.py \
  .github/release/tests/fixtures/catalog-registry.json
git commit -m "feat(release): select latest admissible upstream targets"
```

---

### Task 5: Derive Formal Closure and Publication Sets from the Catalog

**Files:**
- Modify: `.github/release/ucm_release/core.py`
- Modify: `.github/release/ucm_release/registry.py:990-1045`
- Modify: `.github/release/ucm_release/publish.py:196-210,270-295,375-405`
- Modify: `.github/workflows/release-ucm.yml:860-885`
- Modify: `.github/release/tests/test_catalog_resolution.py`
- Modify: `.github/release/tests/test_publish.py`
- Modify: `.github/release/tests/test_workflows.py`

**Interfaces:**
- Produces: `release_topology(catalog) -> dict[str, list[dict[str, str]]]`
- Consumes: resolved-plan task lists as the exact publication authority

- [ ] **Step 1: Write failing dynamic-topology tests**

Create a catalog mutation with an additional profile/architecture coordinate and assert `release_topology` returns the expanded wheel set. Build plan fixtures whose family/image counts are not `3/6` and assert validation compares coordinates rather than literals.

Add publication tests with a smaller valid plan and assert wheel filenames, member-result count, and GitHub asset names derive from the plan.

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -m pytest -q \
  .github/release/tests/test_catalog_resolution.py \
  .github/release/tests/test_publish.py \
  .github/release/tests/test_workflows.py -k 'topology or matrix or artifact or asset'
```

Expected: FAIL on hard-coded `6`, `3`, or `7` checks.

- [ ] **Step 3: Implement catalog topology derivation**

Return canonically sorted coordinates:

```python
{
    "wheels": [
        {"profile_id": profile["id"], "cpu_arch": architecture}
        for profile in catalog["wheel_profiles"]
        for architecture in profile["cpu_arch"]
    ],
    "families": [
        {"product_id": product["id"], "variant": variant["id"]}
        for product in catalog["upstream_products"]
        for variant in product["variants"]
    ],
    "images": [
        {
            "product_id": product["id"],
            "variant": variant["id"],
            "cpu_arch": architecture,
        }
        for product in catalog["upstream_products"]
        for variant in product["variants"]
        for architecture in product["required_cpu_architectures"]
    ],
}
```

`validate_main_full_loop_plan` compares the resolved task coordinate sets with these lists and verifies every family links exactly its declared architectures. Return computed counts rather than fixed values.

- [ ] **Step 4: Remove publication count literals**

`_expected_wheels` requires uniqueness and exact equality with plan wheel tasks, not length six. `_require_release_image_matrix` validates family/image linkage from each family task. `_load_member_results` expects `len(plan["image_tasks"])` JSON files and the same number of unique task hashes.

In `release-ucm.yml`, derive expected wheel and asset counts from `resolved-plan.json`:

```bash
expected_wheels="$(jq '.wheel_tasks | length' input/plan/resolved-plan.json)"
expected_assets="$((expected_wheels + 1))"
test "${#wheels[@]}" = "${expected_wheels}"
test "$(find out/assets -maxdepth 1 -type f | wc -l | tr -d ' ')" = "${expected_assets}"
```

- [ ] **Step 5: Verify GREEN**

Run:

```bash
python3 -m pytest -q \
  .github/release/tests/test_catalog_resolution.py \
  .github/release/tests/test_publish.py \
  .github/release/tests/test_workflows.py
actionlint
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add .github/release/ucm_release/core.py \
  .github/release/ucm_release/registry.py \
  .github/release/ucm_release/publish.py \
  .github/workflows/release-ucm.yml \
  .github/release/tests/test_catalog_resolution.py \
  .github/release/tests/test_publish.py .github/release/tests/test_workflows.py
git commit -m "refactor(release): derive publication closure from catalog"
```

---

### Task 6: Delete Legacy Workflow SHA and Audit Seals

**Files:**
- Delete: `.github/release/production/tests/fixtures/legacy-workflow-sha256.json`
- Delete: `.github/release/production/ucm_release_production/security.py`
- Delete: `.github/release/production/tests/test_security.py`
- Modify: `.github/release/production/tests/test_config.py`
- Modify: `.github/release/v2/ucm_release_v2/security.py`
- Modify: `.github/release/v2/tests/test_security.py`
- Modify: `.github/release/v2/tests/test_residual_trust.py`
- Modify: `.github/release/v2/tests/test_scenarios.py`

**Interfaces:**
- Removes: legacy exact-workflow fingerprint and reviewed-byte APIs
- Preserves: semantic workflow checks and functional build/publication hashes

- [ ] **Step 1: Write/adjust tests to express semantic contracts**

Delete the production exact-byte audit tests. In v2 tests, retain mutations that violate permissions, trigger restrictions, pinned-action policy, trusted checkout ordering, or shell/Python policy. Remove tests whose only oracle is a root/job/step/body SHA mismatch.

Add a test that changes harmless workflow display text and asserts the semantic auditor remains clean.

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -m pytest -q \
  .github/release/production/tests/test_config.py \
  .github/release/v2/tests/test_security.py \
  .github/release/v2/tests/test_residual_trust.py
```

Expected: the harmless edit currently fails because byte/context hashes change.

- [ ] **Step 3: Delete production byte seals**

Remove the fingerprint fixture, `_workflow_hashes`, `test_legacy_workflow_fingerprints_are_frozen`, and the production byte-audit module/tests. Do not remove production build, candidate, registry, publication, or readback tests.

- [ ] **Step 4: Remove v2 digest tables and comparisons**

Delete `_WORKFLOW_ROOT_CONTEXT_SHA256`, `_WORKFLOW_JOB_CONTEXT_SHA256`, `_WORKFLOW_JOB_STEP_SEQUENCE_SHA256`, and `_TRUST_CRITICAL_RUN_BODY_SHA256`. Remove digest-equality findings and exact job/step coverage derived only from those tables. Continue iterating over observed jobs and steps and applying semantic permission, runner, action, shell, embedded-Python, and reusable-workflow policies.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
python3 -m pytest -q .github/release/production/tests
python3 -m pytest -q .github/release/v2/tests
```

Expected: both suites pass without resealing workflow bytes.

- [ ] **Step 6: Commit**

```bash
git add -A .github/release/production .github/release/v2
git commit -m "refactor(release): remove static workflow audit seals"
```

---

### Task 7: Full Local and Fork-Hosted Verification

**Files:**
- Verify: all changed files
- No release/tag/publication mutations

**Interfaces:**
- Produces: local verification evidence and GitHub Actions run URLs for the exact pushed SHA

- [ ] **Step 1: Run full local verification**

```bash
python3 -m pytest -q .github/release/tests
python3 -m pytest -q .github/release/production/tests
python3 -m pytest -q .github/release/v2/tests
pre-commit run --all-files
actionlint
ruff check .github/release ucm/integration/vllm/patch/apply_patch.py
python3 -m compileall -q .github/release ucm/integration/vllm/patch
git diff --check
```

Expected: all commands pass. If broad Ruff reports untouched upstream findings, rerun on changed Python files and report the baseline findings separately.

- [ ] **Step 2: Commit verification-only fixes**

```bash
git status --short
git add -A
git commit -m "test(release): close growth-safe discovery regressions"
```

Skip the commit if the worktree is already clean.

- [ ] **Step 3: Push the feature branch to the fork**

```bash
git push -u origin feature/cicd-growth-safe-discovery
```

Record the pushed SHA and do not force-push.

- [ ] **Step 4: Verify hosted push checks**

Use `gh run list --repo SuperMarioYL/unified-cache-management --branch feature/cicd-growth-safe-discovery` and wait for runs bound to the pushed SHA. Inspect failed jobs and logs; fix, commit, and push normally if needed.

- [ ] **Step 5: Run a branch-safe release dry run**

Dispatch the existing feature/dry-run workflow only if it exposes `workflow_dispatch` and performs no publication. Bind the dispatch to `feature/cicd-growth-safe-discovery`, wait for completion, and inspect its artifacts. Do not dispatch `release-ucm.yml`, push a `v*` tag, create a GitHub Release, or publish an image/package.

- [ ] **Step 6: Report evidence boundaries**

Report separately:

- local unit/static validation;
- hosted workflow run IDs and exact commit SHA;
- generated Actions artifacts;
- anything not proven, including GPU/NPU runtime, Kubernetes acceptance, or public Registry/Release readback.
