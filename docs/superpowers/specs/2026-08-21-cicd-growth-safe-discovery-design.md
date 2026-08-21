# Growth-Safe CICD Discovery and Admission Design

## Context

The release pipeline currently treats several snapshots of today's repository as
permanent invariants. Adding a new `docker/Dockerfile.ucm-*`, Builder fixture,
compatible upstream tag, wheel profile, or release asset can therefore fail the
entire catalog before the new item has been classified. This couples discovery
growth to formal-release closure and turns normal upstream evolution into a
pipeline outage.

## Goals

- Discover new Dockerfiles, Builder bases, and upstream tags without globally
  failing the release catalog.
- Select one latest fully admissible tag for each configured product and variant.
- Record unsupported or superseded targets as canonical exclusions.
- Skip an unsupported target while allowing other compatible targets to proceed.
- Preserve hard failures for ambiguous rules, missing selected Builders, malformed
  configuration, resource-limit overflow, and incomplete formal publication.
- Derive formal task and asset expectations from the catalog rather than fixed
  `6/6/3/7` literals.
- Keep the active release catalog as the sole authority for the main pipeline.
- Remove byte-level workflow fingerprints and reviewed-digest seals that require
  manual resealing after legitimate workflow evolution.

## Non-goals

- Automatically applying a version-specific patch to an unverified runtime.
- Building every historical compatible tag in the formal or daily lane.
- Removing scan or matrix resource limits.
- Weakening same-run artifact, registry, or public readback closure.
- Reworking legacy production or v2 release behavior beyond deleting their static
  byte-level seals.

## Discovery and Recipe Ownership

`docker-recipes.yaml` is a declaration of recipes assigned to a pipeline lane; it
is not an exhaustive inventory of every repository Dockerfile. Validation remains
strict for declared recipes: each path must exist, consume catalog-owned base-image
arguments, and satisfy runner, lane, and output-safety contracts. An undeclared
`docker/Dockerfile.ucm-*` is unmanaged/manual-only and does not invalidate the
catalog.

Builder discovery remains expansive. The implementation continues to scan vLLM
and vLLM-Ascend sources directly, explicitly filters `310p`, deduplicates by
capability, and admits future non-excluded variants. Tests assert those behaviors,
not today's exact Builder count or tag list.

Structural tests may prohibit a concrete obsolete path or unsafe construct, but
must not cap the number of Python modules, schemas, Dockerfiles, or discovered
Builders.

## Candidate Selection

Registry discovery keeps every parsed tag as a candidate or exclusion. Candidates
are grouped by `(product_id, variant)` and evaluated newest-first using PEP 440.
The first candidate that passes all admission checks is selected:

1. product version range and channel;
2. variant declaration and exclusion patterns;
3. exactly one compatibility/profile rule for every required CPU architecture;
4. exactly one runtime-patch strategy, including an explicit `strategy: none`;
5. required multi-architecture registry members;
6. a unique selected Builder for every generated wheel profile.

After a candidate is selected, older admissible candidates in the same group are
recorded with reason `superseded-compatible-version`. A candidate with no runtime
patch rule is recorded as `runtime-patch-unsupported`; one with no compatibility
profile is `compatibility-unsupported`; one missing required architectures is
`required-architecture-missing`. These are target-level exclusions, not global
errors. Evaluation continues with the next candidate in that group.

Malformed manifests, overlapping patch or compatibility rules, duplicate target
coordinates, unresolved selected Builders, and registry transport failures remain
hard failures. They indicate ambiguity or unavailable evidence rather than an
unsupported target.

PR/manual pinning preserves the exact requested tag. The pinned candidate uses the
same admission logic; an unsupported pin produces a canonical exclusion and no
task instead of silently selecting another tag.

## Runtime Patch Boundary

The runtime manifest remains schema version 1 with exactly-one matching strategy.
Zero matches are probeable during planning and cause target exclusion. Multiple
matches remain invalid. `strategy: none` is an explicit supported no-op and must
not be confused with an unsupported target.

The installed runtime dispatcher remains strict: bypassing the release planner and
starting an unknown runtime with `ENABLE_UCM_PATCH=1` still raises on zero matches.
The pipeline never builds or publishes an unsupported runtime, so no fake skipped
image result is created.

Forward-safe behavior may use an intentionally broad PEP 440 range. Risky patches
such as CPU binding, sparse replacements, SFA, and whole-method overrides remain
limited to explicitly verified ranges.

## Catalog-Derived Closure

The catalog derives a topology contract from its declarations:

- wheel coordinates from every `(wheel_profile, cpu_arch)`;
- family coordinates from every configured `(product, variant)`;
- image coordinates from each family and its required CPU architectures;
- GitHub Release assets from selected wheel filenames plus the Chart package.

Formal full-loop validation compares the resolved plan with these exact sets. It
does not compare against numeric literals. Publication adapters validate the exact
files and member records named by the resolved plan. Workflow asset-count checks
read the expected set from the plan.

This preserves fail-closed publication: a missing selected target still blocks a
formal release, while merely discovering additional historical or unsupported tags
does not expand or block the matrix.

## Static Seal Removal

Byte-level workflow fingerprints are not release authority. The legacy production
fingerprint fixture, reviewed-workflow digest table, and tests that require exact
workflow bytes are removed. The v2 auditor drops root-context, job-context,
step-sequence, and embedded-body SHA tables and their equality checks.

Other closed, audit-only chains are removed with their unused fields: the static
Chart `SOURCE_PROVENANCE.json` release-tree seal, hard-coded image authority hashes
that have no source-byte comparison, the unconsumed member-audit artifact, and the
v2 repository-policy report envelope hash. Their live package, implementation,
member-record, and policy findings remain.

Semantic checks remain where they express behavior directly: workflow permissions,
event restrictions, pinned actions, trusted checkout boundaries, forbidden shell
constructs, and required publication/readback topology. A test must assert the
semantic property it protects instead of comparing an opaque digest.

Functional hashes remain unchanged. Source commits, resolved-plan hashes, artifact
SHA256 values, Docker implementation digests, OCI manifest/config digests, and
same-run evidence hashes continue to bind build and publication identities.

## Limits and Failure Semantics

`scan_limits` and `matrix_limits` remain configurable hard limits and are evaluated
after unsupported and superseded candidates have been excluded. The pipeline never
silently truncates an admitted set.

Feature-candidate and discovery paths may produce a partial or zero-task plan with
structured exclusions. Protected publication still requires the complete
catalog-derived topology before Draft creation or any write.

## Test Strategy

- Add an undeclared future Dockerfile and prove catalog validation still succeeds.
- Prove declared missing or malformed recipes still fail.
- Add future Builder fixtures and prove dynamic discovery, deduplication, and
  `310p` filtering without fixed counts.
- Add multiple compatible tags per product/variant and prove newest admissible
  selection plus canonical superseded exclusions.
- Prove the newest unsupported tag falls back to the next supported tag.
- Prove zero runtime-patch matches exclude only that target; overlap still fails.
- Prove pinning an unsupported tag yields an exclusion and no task.
- Mutate catalog topology and prove full-loop/publication expectations derive from
  the catalog and plan rather than `6/6/3/7` constants.
- Delete legacy workflow fingerprint fixtures and prove workflow changes are checked
  by semantic contracts rather than reviewed-byte hashes.
- Run main release, production, v2, actionlint, pre-commit, Ruff, compile, and
  diff checks.

## Hosted Verification

Push the feature branch to the user's fork. Verify push checks and the branch-safe
release dry-run workflow on the exact branch SHA. Do not create a release, publish
packages, or push a tag. Report hosted CI separately from local verification.
