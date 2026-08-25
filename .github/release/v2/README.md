# UCM Release Lifecycle Dry-run v2

This directory is an isolated, read-only control plane for PR, `develop`,
Nightly, Draft, RC, Stable, and Hotfix planning. It does not publish a package,
image, Chart, Git tag, GitHub Release, comment, workflow dispatch, or repository
setting. Every operation is `executed: false`; every source identity is an exact
40-character lowercase Git SHA.

The full user-facing contract, scenario matrix, evidence boundary, retention
policy, and local validation record are in
[`docs/ucm-release-lifecycle-dry-run-v2-implementation.md`](../../../docs/ucm-release-lifecycle-dry-run-v2-implementation.md).

Run from the repository root with explicit paths so the command is independent
of the current directory:

```bash
export UCM_V2_ROOT="$PWD/.github/release/v2"
export PYTHONPATH="$UCM_V2_ROOT"
export UCM_V2_CONFIG="$UCM_V2_ROOT/release.yaml"

python3 -m ucm_release_v2 config validate --config "$UCM_V2_CONFIG"
python3 -m ucm_release_v2 config retention nightly --config "$UCM_V2_CONFIG"
python3 -m ucm_release_v2 lifecycle plan \
  --stage nightly --trigger schedule --ref refs/heads/develop \
  --source-sha 0123456789abcdef0123456789abcdef01234567 \
  --repository-role validation --date 2026-08-12 \
  --config "$UCM_V2_CONFIG" --output /tmp/ucm-lifecycle-plan.json
python3 -m ucm_release_v2 lifecycle validate \
  --plan /tmp/ucm-lifecycle-plan.json --config "$UCM_V2_CONFIG"
```

The lifecycle-plan JSON Schema is intentionally a strict structural and
stage-local contract. Standard Draft 2020-12 validation does not enforce every
sibling-field equality. Every workflow runs `lifecycle validate` immediately
after plan generation, and every downstream CLI consumer calls the same
`validate_plan` semantic validator before using a plan. It checks the canonical
self-digest, configured route and product closure, source/version binding, and
release-intent source/version binding.

Draft, RC, Stable, and Hotfix require a release-intent JSON object whose
`stage`, `version`, and `source_sha` exactly match the request. JSON outputs use
canonical, self-digested SHA256 envelopes; they are content-addressed and make
no cryptographic-authentication claim.

The issue-comment grammar is exact ASCII. A build preview is
`/release build <40-lowercase-hex-sha>`; the requested SHA must equal both
read-only PR observations. `/release status` and `/release cancel` remain
SHA-free, non-writing previews. CR/LF, Unicode lookalikes, abbreviated SHA, and
comment-time/current-head races fail closed.

The three Wheel distributions are mutually exclusive:
`uc-manager-cuda`, `uc-manager-cann-a2`, and `uc-manager-cann-a3`. Choose exactly
one for an environment. `wheel check-environment` rejects legacy `uc-manager`,
two-way mixtures, all-three mixtures, duplicates, malformed metadata, and any
other valid distribution that declares the `ucm` top-level import. An empty
environment is reported as absent; compatibility means exactly one approved v2
distribution. The pre-existing legacy package remains independently usable,
but is never reported as compatible by the v2 guard.

The install, pull, and Helm commands produced by `release render` are previews.
They are not usable delivery evidence until hosted publication and independent
Registry/Release readback have succeeded.

The `develop` controller is loaded from the default branch through
`workflow_run` after the existing `Push Commit Checks` workflow completes for
`develop`. It accepts only the exact `push` event and exact run path
`.github/workflows/push-check.yml@develop`, in addition to the workflow name,
successful conclusion, same-repository `develop` branch, and lowercase 40-hex
head SHA. Control is bound to two identical `main` ref reads and
`github.workflow_sha`; the develop head remains source data and is never checked
out, imported, or executed.

Scheduled Nightly runs likewise keep control code pinned to
`github.workflow_sha` on the configured `main` branch. They read the configured
`develop` ref twice through the exact GitHub read-only endpoint, validate an
unchanged commit SHA, and use that SHA only as lifecycle source data; develop
code is never checked out or executed by the Nightly control job. Nightly
versions also undergo real UTC calendar validation, so an internally consistent
but impossible date such as `20260230` is rejected.

The four manual entry workflows (Draft environment, protected lifecycle,
cleanup, and policy audit) are data-only wrappers. Each has one job with no
`runs-on`, `steps`, or Action and calls exactly
`SuperMarioYL/unified-cache-management/.github/workflows/release-control-dry-run.yml@main`;
selected-ref bytes can supply only declared input data. All executable manual
logic lives in that reusable controller. Before any checkout or v2 CLI, its
minimal gate performs two non-redirecting GETs of the exact `main` ref and
requires duplicate-free commit responses with the same lowercase 40-hex SHA.
The observed SHA must equal `job.workflow_sha`, while `job.workflow_repository`,
`job.workflow_file_path`, and `job.workflow_ref` required identity projection must identify the exact
validation-repository controller path at `refs/heads/main`. Caller repository
identity is separately restricted to `SuperMarioYL/unified-cache-management`; the
configured production coordinate and arbitrary forks are data, not execution
authorities. Other GitHub-generated `job` fields such as status, container, or
services do not participate in authority. Every control checkout uses only the
verified SHA.

A hosted result is eligible as controller evidence only when its called-workflow
identity, including `job.workflow_ref`, repository, file path, and SHA, is
captured and matches this boundary. GitHub resolves a reusable `@main` reference
to a tag when a tag and branch have the same name. The honest controller rejects
that resolution because its `job.workflow_ref` must end in `@refs/heads/main`,
but a malicious tag-shadow controller could remove its own check before it runs.
Hosted enablement therefore requires repository rules that prevent a tag named
`main` and prevent unreviewed wrapper/controller changes, or a follow-up commit
that pins all four wrappers to the immutable controller commit SHA after that
controller exists on `main`. A malicious selected branch can likewise delete or
replace its wrapper call. Code in this delivery proves only the shipped tree; it
does not close those external bootstrap and repository-governance boundaries.

Promotion evidence is eligible only when accompanied by its reopened source
lifecycle plan and artifact manifest. Stable binds to an accepted RC with the
same target source SHA and release line; Hotfix binds to the immediately
previous Stable plan/manifest and may use a different new target SHA. Evidence
without those anchors remains visible but carries `promotion-unanchored`.
Likewise, a Draft request/result pair can become `draft-passed` or
`draft-failed` only when the original Draft plan and manifest are reopened and
reconstruct the request exactly. A self-consistent pair without origin anchors
is `unanchored-simulation`. None of these states opens the production gate.

The reusable security audit is a default-deny review of the current closed
Python capability surface and the exact executable/argv grammar of these eight
workflows. It is not a general proof of arbitrary Python program safety; adding
a new import, callable, Action, step form, executable, or network argument fails
until its precise capability is reviewed and allowlisted.
The only dynamic loader exception combines exact normalized module provenance
for `_V2_ROOT`/`_PACKAGING_ROOT` with the complete normalized AST of the
repository-owned `_guard_module` function. Any path replacement, extra Store or
Delete, dynamic namespace mutation, reassignment, reordering, second loader,
different identity/path/module argument, or aliased loader call fails.
Workflow root, job, runner, and step execution contexts are also closed: custom
shells, defaults, containers, services, and matrices are outside the reviewed
surface. The sole reusable-job form is the exact data-only wrapper mapping to
the trusted `@main` controller; any other reusable target, input expression,
runner, step, or Action fails closed. Every executable job in the eight-workflow
set has an exact ordered step-name/type sequence, and the expected executable-job
mapping must equal the observed mapping without an orphan. The two trust-critical
embedded validators additionally bind exact workflow, owning job, zero-based
step index, name, and parsed run-body digest. Cross-job moves or duplication,
checkout/CLI reordering, body edits, removal, or rename fail before the ordinary
capability audit continues. Policy Job Summary gaps omit free-form evidence; the
header contains only validated identity, status, and digest fields.
