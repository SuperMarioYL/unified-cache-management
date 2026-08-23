"""User-visible Actions contracts for upstream planning and parallel publishing."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, object]:
    value = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def test_release_workflow_resolves_upstreams_before_builders_and_plan() -> None:
    jobs = _load("release-ucm.yml")["jobs"]
    assert list(jobs)[:3] == ["resolve-upstreams", "sync-builders", "plan"]
    assert jobs["sync-builders"]["needs"] == "resolve-upstreams"
    assert set(jobs["plan"]["needs"]) == {"resolve-upstreams", "sync-builders"}
    assert (
        jobs["sync-builders"]["with"]["upstream_selection_artifact"]
        == "${{ needs.resolve-upstreams.outputs.upstream_selection_artifact }}"
    )
    plan_text = yaml.safe_dump(jobs["plan"])
    assert "--upstream-selection" in plan_text
    assert "image_index_matrix" in plan_text
    assert "'{include:[.families[]" in plan_text


def test_release_workflow_has_six_parallel_publication_jobs() -> None:
    jobs = _load("release-ucm.yml")["jobs"]
    publish_jobs = [
        "prepare-release-draft",
        "publish-image-members",
        "publish-image-indexes",
        "publish-pypi",
        "publish-chart-oci",
        "finalize-release",
    ]
    assert all(name in jobs for name in publish_jobs)
    assert set(jobs["prepare-release-draft"]["needs"]) == {
        "plan",
        "build-wheels",
        "package-chart",
        "build-images",
    }
    assert set(jobs["publish-image-members"]["needs"]) == {
        "plan",
        "prepare-release-draft",
    }
    assert set(jobs["publish-image-indexes"]["needs"]) == {
        "plan",
        "publish-image-members",
    }
    assert set(jobs["publish-pypi"]["needs"]) == {
        "plan",
        "prepare-release-draft",
    }
    assert set(jobs["publish-chart-oci"]["needs"]) == {
        "plan",
        "prepare-release-draft",
    }
    assert set(jobs["finalize-release"]["needs"]) == {
        "plan",
        "prepare-release-draft",
        "publish-image-members",
        "publish-image-indexes",
        "publish-pypi",
        "publish-chart-oci",
    }
    for name in ("prepare-release-draft", "finalize-release"):
        assert jobs[name]["env"]["GH_REPO"] == "${{ github.repository }}"


def test_prepare_draft_executes_wheel_manifest_validation(tmp_path: Path) -> None:
    jobs = _load("release-ucm.yml")["jobs"]
    step = next(
        item
        for item in jobs["prepare-release-draft"]["steps"]
        if item.get("name") == "Validate publication artifacts"
    )
    match = re.search(
        r"jq -e --slurpfile results wheel-results\.json '(.*?)' \"\$\{plan\}\"",
        step["run"],
        re.DOTALL,
    )
    assert match is not None
    plan = {
        "wheels": [
            {
                "id": "cuda129-cp312-amd64",
                "dist_name": "uc-manager-cuda-cu129",
                "wheel_version": "0.7.59rc22",
                "python_abi": "cp312",
                "cpu_arch": "amd64",
            }
        ],
        "publish": {"pypi": {"distributions": ["uc-manager-cuda-cu129"]}},
    }
    results = [
        {
            "task_id": "cuda129-cp312-amd64",
            "distribution": "uc-manager-cuda-cu129",
            "version": "0.7.59rc22",
            "python_abi": "cp312",
            "cpu_arch": "amd64",
            "filename": "uc_manager_cuda_cu129-0.7.59rc22-cp312.whl",
        }
    ]
    plan_path = tmp_path / "release-plan.json"
    results_path = tmp_path / "wheel-results.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    results_path.write_text(json.dumps(results), encoding="utf-8")
    subprocess.run(
        [
            "jq",
            "-e",
            "--slurpfile",
            "results",
            str(results_path),
            match.group(1),
            str(plan_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_member_and_index_publication_are_unbounded_matrices() -> None:
    jobs = _load("release-ucm.yml")["jobs"]
    members = jobs["publish-image-members"]
    indexes = jobs["publish-image-indexes"]
    assert members["strategy"]["fail-fast"] is False
    assert indexes["strategy"]["fail-fast"] is False
    assert "max-parallel" not in members["strategy"]
    assert "max-parallel" not in indexes["strategy"]
    assert (
        members["strategy"]["matrix"]
        == "${{ fromJSON(needs.plan.outputs.image_matrix) }}"
    )
    assert (
        indexes["strategy"]["matrix"]
        == "${{ fromJSON(needs.plan.outputs.image_index_matrix) }}"
    )
    text = (WORKFLOWS / "release-ucm.yml").read_text(encoding="utf-8")
    assert "while IFS=$'\\t' read -r image_id" not in text


def test_finalize_is_the_only_job_that_publicizes_release() -> None:
    jobs = _load("release-ucm.yml")["jobs"]
    publicizers = []
    for name, job in jobs.items():
        if "draft=false" in yaml.safe_dump(job):
            publicizers.append(name)
    assert publicizers == ["finalize-release"]
    final_run = jobs["finalize-release"]["steps"][-1]["run"].rstrip()
    assert final_run.endswith(
        'gh release edit "${tag}" --draft=false --prerelease=true'
    )


def test_remote_writers_use_environment_and_minimum_permissions() -> None:
    jobs = _load("release-ucm.yml")["jobs"]
    for name in (
        "prepare-release-draft",
        "publish-image-members",
        "publish-image-indexes",
        "publish-pypi",
        "publish-chart-oci",
        "finalize-release",
    ):
        assert jobs[name]["environment"] == "release-production"
        assert jobs[name]["permissions"]["contents"] in {"read", "write"}
    assert "packages" not in jobs["publish-pypi"]["permissions"]
    assert jobs["publish-chart-oci"]["permissions"]["packages"] == "write"
    assert jobs["publish-image-members"]["permissions"]["packages"] == "write"


def test_builder_sync_consumes_selection_and_materializes_recipes() -> None:
    workflow = _load("sync-builders.yml")
    assert set(workflow["on"]) == {"workflow_call"}
    assert set(workflow["on"]["workflow_call"]["inputs"]) == {
        "upstream_selection_artifact",
    }
    text = (WORKFLOWS / "sync-builders.yml").read_text(encoding="utf-8")
    assert "--selection input/upstreams/upstream-selection.json" in text
    assert "matrix.source_repository" in text
    assert "matrix.source_ref" in text
    assert "matrix.build_mode != 'mirror'" in text
    assert "recipe-extend" in text
    assert "builders materialize-recipe" in text
    assert "strip_run_containing" in text
    assert 'imagetools inspect "${upstream_target}"' in text
    assert "Upstream recipe stage already exists" in text
    assert "REQUIRE_MOONCAKE" in text
    assert (
        workflow["jobs"]["build-missing"]["continue-on-error"]
        == "${{ matrix.checks.blocking != true }}"
    )
    assert "builders labels" in text
    assert "candidate-${GITHUB_RUN_ID}" in text
    assert 'imagetools create --tag "${target}" "${candidate}"' in text
    assert "Dockerfile.builder-mirror" in text
    assert workflow["jobs"]["build-missing"]["timeout-minutes"] == 180

    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.builder"
    ).read_text(encoding="utf-8")
    assert "gflags-config.cmake" in dockerfile
    assert "-DBUILD_UNIT_TESTS=OFF" in dockerfile
    assert "-DBUILD_EXAMPLES=OFF" in dockerfile
    assert "allocator_arg::template rebind<T>::other" in dockerfile
    assert "allocator_traits<allocator_arg>::template rebind_alloc<T>" in dockerfile
    assert "node_allocator::template rebind<U>::other" in dockerfile
    assert "allocator_traits<node_allocator>::template rebind_alloc<U>" in dockerfile
    assert "mooncake-transfer-engine/include/cuda_alike.h" in dockerfile

    gflags_config = (
        ROOT / ".github" / "release" / "docker" / "gflags-config.cmake"
    ).read_text(encoding="utf-8")
    assert "add_library(gflags::gflags UNKNOWN IMPORTED)" in gflags_config
    assert 'IMPORTED_LOCATION "${GFLAGS_LIBRARY}"' in gflags_config


def test_mooncake_installer_supports_old_upstream_curl() -> None:
    text = (
        ROOT / ".github" / "release" / "docker" / "mooncake_installer.sh"
    ).read_text(encoding="utf-8")
    assert "curl --help all" in text
    assert "curl_retry_all_errors=(--retry-all-errors)" in text
    assert '"${curl_retry_all_errors[@]}"' in text
    assert "--retry 8 --retry-all-errors" not in text
    assert "zstd-devel" in text
    assert "libzstd-devel" in text
    assert "xxhash-devel" in text
    assert "msgpack-devel" in text


def test_wheel_build_records_auditwheel_result_manifest() -> None:
    text = (WORKFLOWS / "_build-wheel.yml").read_text(encoding="utf-8")
    assert "auditwheel==" in text
    assert "python -m auditwheel show" in text
    assert "compact record-wheel-result" in text
    assert "out/wheel/wheel-result.json" in text


def test_reusable_builds_keep_functional_inputs() -> None:
    expected = {
        "_build-wheel.yml": {"wheel_id", "runner", "plan_artifact", "source_ref"},
        "_build-image.yml": {
            "image_id",
            "runner",
            "plan_artifact",
            "upload_oci",
            "source_ref",
        },
        "_build-chart.yml": {"plan_artifact", "source_ref"},
    }
    for filename, inputs in expected.items():
        workflow = _load(filename)
        assert set(workflow["on"]["workflow_call"]["inputs"]) == inputs


def test_compact_wheel_passes_dynamic_python_and_platform_to_build() -> None:
    workflow = (WORKFLOWS / "_build-wheel.yml").read_text(encoding="utf-8")
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.wheel"
    ).read_text(encoding="utf-8")
    assert "UCM_PYTHON_VERSION" in workflow
    assert "UCM_PYTHON_ABI" in workflow
    assert "UCM_PLATFORM" in workflow
    assert "ARG UCM_PYTHON_VERSION" in dockerfile
    assert "ARG UCM_PYTHON_ABI" in dockerfile
    assert "PIP_BREAK_SYSTEM_PACKAGES=1" in dockerfile
    assert 'sysconfig.get_path("scripts")' in dockerfile
    assert 'PATH="${python_scripts}:${PATH}"' in dockerfile


def test_release_build_keeps_gcc_fmt_false_positive_non_fatal() -> None:
    cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    assert 'CMAKE_BUILD_TYPE_LOWER STREQUAL "release"' in cmake
    assert 'CMAKE_CXX_COMPILER_ID STREQUAL "GNU"' in cmake
    assert "-Wno-error=stringop-overflow" in cmake


def test_chart_consumes_product_smoke_values_from_v4_policy() -> None:
    text = (WORKFLOWS / "_build-chart.yml").read_text(encoding="utf-8")
    assert ".chart.smoke_values[$product]" in text
    assert "huawei.com/Ascend910" in text
    assert "nvidia.com/gpu" in text
    assert "validation_cases" not in text


def test_ucm_build_bot_uses_probe_pipeline_without_hardcoded_capabilities() -> None:
    workflow = _load("ucm-build-bot.yml")
    assert set(workflow["on"]) == {"issue_comment", "workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {
        "pr_number",
        "subcommand",
        "image_refs",
        "profile",
    }
    jobs = workflow["jobs"]
    assert set(
        jobs["inspect-runtimes"]["needs"]
        if isinstance(jobs["inspect-runtimes"]["needs"], list)
        else [jobs["inspect-runtimes"]["needs"]]
    ) == {"permission-check"}
    assert set(jobs["probe-runtimes"]["needs"]) == {
        "permission-check",
        "inspect-runtimes",
    }
    assert set(jobs["resolve-pr-runtimes"]["needs"]) == {
        "permission-check",
        "inspect-runtimes",
        "probe-runtimes",
    }
    assert set(jobs["sync-pr-builders"]["needs"]) == {
        "permission-check",
        "resolve-pr-runtimes",
    }
    assert set(jobs["plan-image"]["needs"]) == {
        "permission-check",
        "resolve-pr-runtimes",
        "sync-pr-builders",
    }
    assert jobs["probe-runtimes"]["strategy"]["fail-fast"] is False
    text = (WORKFLOWS / "ucm-build-bot.yml").read_text(encoding="utf-8")
    hint = (WORKFLOWS / "ucm-build-hint.yml").read_text(encoding="utf-8")
    assert "runtime inspect" in text
    assert "runtime aggregate" in text
    assert "builders scan-registry" in text
    assert "runtime resolve" in text
    assert "opaque" in hint
    assert "pep440" not in hint.lower()
    assert "cann900" not in text + hint
    assert "--upstream-selection" in text
    assert "profile_id==$profile" in text


def test_pr_member_and_index_publication_are_separate_dynamic_matrices() -> None:
    jobs = _load("ucm-build-bot.yml")["jobs"]
    members = jobs["publish-pr-image-members"]
    indexes = jobs["publish-pr-image-indexes"]
    assert members["strategy"]["fail-fast"] is False
    assert indexes["strategy"]["fail-fast"] is False
    assert (
        members["strategy"]["matrix"]
        == "${{ fromJSON(needs.select-plan.outputs.image_matrix) }}"
    )
    assert (
        indexes["strategy"]["matrix"]
        == "${{ fromJSON(needs.select-plan.outputs.index_matrix) }}"
    )
    assert "has_indexes == 'true'" in indexes["if"]
    assert "all" in members["if"]
    assert "all" in indexes["if"]
    index_step = indexes["steps"][-1]["run"]
    assert "matrix.members" not in index_step
    assert "jq -er '.[]'" in index_step
    assert '"${members[@]}"' in index_step
    assert "-amd64" not in index_step
    assert "-arm64" not in index_step


def test_pr_receipt_is_always_posted_and_has_no_package_write_permission() -> None:
    receipt = _load("ucm-build-bot.yml")["jobs"]["post-build-receipt"]
    assert "always()" in receipt["if"]
    assert receipt["permissions"] == {
        "actions": "read",
        "contents": "read",
        "pull-requests": "write",
    }
    text = yaml.safe_dump(receipt)
    assert "runtime receipt" in text
    assert "pr-resolution.json" in text


def test_bot_control_plane_is_trusted_while_builds_use_pr_source() -> None:
    jobs = _load("ucm-build-bot.yml")["jobs"]
    for name in (
        "resolve-formal",
        "inspect-runtimes",
        "probe-runtimes",
        "resolve-pr-runtimes",
        "plan-formal",
        "plan-image",
        "post-build-receipt",
    ):
        checkouts = [
            step
            for step in jobs[name]["steps"]
            if step.get("uses") == "actions/checkout@v4.2.2"
        ]
        assert checkouts
        assert all("ref" not in step.get("with", {}) for step in checkouts)
    assert (
        jobs["build-wheels"]["with"]["source_ref"]
        == "${{ needs.select-plan.outputs.source_ref }}"
    )
    assert (
        jobs["build-images"]["with"]["source_ref"]
        == "${{ needs.select-plan.outputs.source_ref }}"
    )


def test_bot_all_retags_and_publishes_without_formal_tag_collision() -> None:
    jobs = _load("ucm-build-bot.yml")["jobs"]
    plan_text = yaml.safe_dump(jobs["plan-formal"])
    assert "compact retag-pr" in plan_text
    assert "index_matrix=${indexes}" in plan_text
    assert "publish-pr-image-members" in jobs
    assert "publish-pr-image-indexes" in jobs


def test_runtime_probe_frees_disk_and_uploads_failure_evidence() -> None:
    probe = _load("ucm-build-bot.yml")["jobs"]["probe-runtimes"]
    uses = [step.get("uses") for step in probe["steps"]]
    assert "jlumbroso/free-disk-space@v1.3.1" in uses
    upload = next(
        step
        for step in probe["steps"]
        if step.get("uses") == "actions/upload-artifact@v4.6.2"
    )
    assert "always()" in upload["if"]
    assert upload["with"]["path"] == "out/"
    image_build = (WORKFLOWS / "_build-image.yml").read_text(encoding="utf-8")
    assert ".runtime.image_reference" in image_build
    assert '.runtime.repository + ":" + .runtime.tag' not in image_build


def test_cross_job_artifact_names_survive_failed_job_reruns() -> None:
    names = (
        "release-ucm.yml",
        "sync-builders.yml",
        "_build-wheel.yml",
        "_build-image.yml",
        "_build-chart.yml",
        "ucm-build-bot.yml",
    )
    text = "\n".join((WORKFLOWS / name).read_text(encoding="utf-8") for name in names)
    assert "github.run_attempt" not in text
    assert "GITHUB_RUN_ATTEMPT" in text  # candidate Builder tags remain retry-scoped.


def test_a5_issue_is_isolated_to_formal_tag_workflow() -> None:
    issue = _load("release-capability-issue.yml")
    assert issue["on"] == {"push": {"tags": ["v*"]}}
    assert issue["permissions"] == {
        "actions": "read",
        "contents": "read",
        "issues": "write",
    }
    assert issue["concurrency"]["cancel-in-progress"] is True
    assert "issues" not in _load("release-ucm.yml")["permissions"]
    assert "issues" not in _load("ucm-build-bot.yml")["permissions"]
    text = (WORKFLOWS / "release-capability-issue.yml").read_text(encoding="utf-8")
    assert "problems render" in text
    assert "ucm-upstream-selection-run-${run_id}" in text
    assert "upstreams resolve" not in text
    assert "gh issue create" in text
    assert "gh issue close" in text
