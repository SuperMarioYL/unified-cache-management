"""Planner-driven GitHub Actions fan-out contracts."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, object]:
    value = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    # PyYAML follows YAML 1.1 and parses the Actions key ``on`` as ``True``.
    # Repair only that key instead of mutating SafeLoader's global boolean
    # resolvers, which would make every later true/false value a string.
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def test_wheel_consumer_selects_one_task_from_the_frozen_parent_plan() -> None:
    workflow = _load("_build-wheel.yml")
    inputs = workflow["on"]["workflow_call"]["inputs"]
    assert set(inputs) == {
        "source_sha",
        "task_id",
        "runner",
        "resolved_plan_artifact",
        "resolved_plan_sha256",
    }
    job = workflow["jobs"]["build"]
    assert job["runs-on"] == "${{ inputs.runner }}"
    text = (WORKFLOW_DIR / "_build-wheel.yml").read_text(encoding="utf-8")
    assert "catalog select" in text
    assert "--task-kind wheel" in text
    assert '--task-id "${TASK_ID}"' in text
    assert '--expected-plan-sha256 "${RESOLVED_PLAN_SHA256}"' in text
    assert "name: ${{ inputs.resolved_plan_artifact }}" in text
    assert "core hosted-matrix" not in text
    assert "endsWith(" not in text
    assert "spec_id:" not in text
    assert "cuda130" not in text
    assert "cann900" not in text


def test_image_consumer_uses_explicit_wheel_dependency_from_frozen_plan() -> None:
    workflow = _load("_build-image.yml")
    inputs = workflow["on"]["workflow_call"]["inputs"]
    assert set(inputs) == {
        "source_sha",
        "task_id",
        "runner",
        "resolved_plan_artifact",
        "resolved_plan_sha256",
        "deliver_full_oci",
    }
    job = workflow["jobs"]["build"]
    assert job["runs-on"] == "${{ inputs.runner }}"
    text = (WORKFLOW_DIR / "_build-image.yml").read_text(encoding="utf-8")
    assert "catalog select" in text
    assert "--task-kind image" in text
    assert '--task-id "${TASK_ID}"' in text
    assert '--expected-plan-sha256 "${RESOLVED_PLAN_SHA256}"' in text
    assert "wheel_task_id" in text
    assert "wheel_artifact_name" in text
    assert "image_task_sha256" in text
    assert "wheel_task_sha256" in text
    assert "core hosted-matrix" not in text
    assert "endsWith(" not in text
    assert "spec_id:" not in text
    assert "cuda130" not in text
    assert "cann900" not in text


def test_reusable_builds_bind_input_source_to_plan_before_checkout_and_setup() -> None:
    """A caller cannot checkout or build source B with a plan frozen at source A."""
    for filename in ("_build-wheel.yml", "_build-image.yml"):
        workflow = _load(filename)
        steps = workflow["jobs"]["build"]["steps"]
        source_gate_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Validate frozen plan source before checkout"
        )
        checkout_index = next(
            index
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        task_python_index = next(
            index for index, step in enumerate(steps) if step.get("id") == "task-python"
        )
        setup_python_index = next(
            index
            for index, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("actions/setup-python@")
        )
        source_gate = steps[source_gate_index]
        task_python = steps[task_python_index]

        assert source_gate_index < checkout_index
        assert checkout_index < task_python_index < setup_python_index
        assert source_gate["env"]["EXPECTED_SOURCE_SHA"] == "${{ inputs.source_sha }}"
        assert ".source.commit" in source_gate["run"]
        assert ".resolved_plan_sha256" in source_gate["run"]
        assert "del(.resolved_plan_sha256)" in source_gate["run"]
        assert "observed_plan_sha256" in source_gate["run"]
        assert task_python["env"]["EXPECTED_SOURCE_SHA"] == "${{ inputs.source_sha }}"
        assert '--expected-source-sha "${EXPECTED_SOURCE_SHA}"' in task_python["run"]


def test_build_consumers_bind_toolchain_and_image_runtime_to_selected_tasks() -> None:
    wheel = _load("_build-wheel.yml")
    wheel_steps = wheel["jobs"]["build"]["steps"]
    wheel_toolchain = next(
        step
        for step in wheel_steps
        if step.get("name") == "Install checksum-pinned Buildx"
    )
    wheel_command = str(wheel_toolchain["run"])
    assert "image task-toolchain-authority" in wheel_command
    assert "--resolved-plan input/plan/resolved-plan.json" in wheel_command
    assert "--task-kind wheel" in wheel_command
    assert '--task-id "${TASK_ID}"' in wheel_command
    assert '--expected-plan-sha256 "${RESOLVED_PLAN_SHA256}"' in wheel_command
    assert "image real-authorities" not in wheel_command
    assert 'matches=[x for x in a["members"]' not in wheel_command
    wheel_text = (WORKFLOW_DIR / "_build-wheel.yml").read_text(encoding="utf-8")
    assert (
        "cp out/task-toolchain-authority.json "
        "out/wheel-artifact/task-toolchain-authority.json"
    ) in wheel_text
    assert "out/image-toolchain-authority.json" not in wheel_text

    image = _load("_build-image.yml")
    image_steps = image["jobs"]["build"]["steps"]
    setup = next(
        step
        for step in image_steps
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    assert (
        setup["with"]["python-version"]
        == "${{ steps.task-python.outputs.python_version }}"
    )
    task_python_index = next(
        index
        for index, step in enumerate(image_steps)
        if step.get("id") == "task-python"
    )
    setup_index = image_steps.index(setup)
    assert task_python_index < setup_index

    select_step = next(step for step in image_steps if step.get("id") == "task")
    select_command = str(select_step["run"])
    assert "image real-authorities" in select_command
    assert "--resolved-plan input/plan/resolved-plan.json" in select_command
    assert '--task-id "${TASK_ID}"' in select_command
    assert '--expected-plan-sha256 "${RESOLVED_PLAN_SHA256}"' in select_command

    dependencies = next(
        step
        for step in image_steps
        if step.get("name")
        == "Download and hash-check locked runtime dependency wheels"
    )
    dependency_command = str(dependencies["run"])
    for field in ("python_version", "python_abi", "platform"):
        assert f".{field}" in dependency_command
    assert ".runtime_dependencies" in dependency_command
    for forbidden in (
        "--python-version 312",
        "--abi cp312",
        "wrapt==1.17.2",
        "manylinux2014_x86_64",
        "manylinux2014_aarch64",
        'test "${target_platform}" = "linux/${architecture}"',
        'test "${python_abi}" = "cp${python_minor}"',
    ):
        assert forbidden not in dependency_command
    assert ".runtime_dependencies[]" in dependency_command
    assert "(.runtime_dependencies) | length" in dependency_command

    context_step = next(
        step
        for step in image_steps
        if step.get("name") == "Prepare the allowlisted offline runtime-real context"
    )
    context_command = str(context_step["run"])
    assert 'runtime_wheel_args+=(--runtime-wheel "${runtime_wheel}")' in context_command
    for legacy_field in ("packaging_wheel", "wrapt_wheel", "wrapt==1.17.2"):
        assert legacy_field not in context_command

    verify_step = next(
        step
        for step in image_steps
        if step.get("name") == "Verify runtime-real OCI and emit compact evidence"
    )
    verify_command = str(verify_step["run"])
    assert "--resolved-plan input/plan/resolved-plan.json" in verify_command
    assert '--task-id "${TASK_ID}"' in verify_command
    assert '--expected-plan-sha256 "${RESOLVED_PLAN_SHA256}"' in verify_command


def test_top_level_resolves_once_and_fans_out_parent_plan_matrices() -> None:
    workflow = _load("release-ucm.yml")
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert "workflow_call" in workflow["on"]
    plan = workflow["jobs"]["plan"]
    assert set(plan["outputs"]) >= {
        "source_sha",
        "route",
        "resolved_plan_artifact",
        "resolved_plan_sha256",
        "wheel_matrix",
        "image_matrix",
        "family_matrix",
        "smoke_wheel_matrix",
        "smoke_image_matrix",
    }
    steps = plan["steps"]
    resolve_indexes = [
        index
        for index, step in enumerate(steps)
        if "catalog resolve" in str(step.get("run", ""))
    ]
    assert len(resolve_indexes) == 1
    crane_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Install checksum-pinned read-only crane"
    )
    assert crane_index < resolve_indexes[0]
    uploads = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    assert (
        uploads[0]["with"]["name"] == "${{ steps.plan.outputs.resolved_plan_artifact }}"
    )

    jobs = workflow["jobs"]
    wheel = jobs["build-wheels"]
    assert (
        wheel["strategy"]["matrix"]
        == "${{ fromJSON(needs.plan.outputs.build_wheel_matrix) }}"
    )
    assert wheel["with"]["task_id"] == "${{ matrix.task_id }}"
    assert wheel["with"]["runner"] == "${{ matrix.runner }}"
    assert (
        wheel["with"]["resolved_plan_artifact"]
        == "${{ needs.plan.outputs.resolved_plan_artifact }}"
    )
    assert (
        wheel["with"]["resolved_plan_sha256"]
        == "${{ needs.plan.outputs.resolved_plan_sha256 }}"
    )

    text = (WORKFLOW_DIR / "release-ucm.yml").read_text(encoding="utf-8")
    assert text.count("catalog resolve") == 1
    assert "core hosted-matrix" not in text
    assert "v0.5.0rc1" not in text
    assert "cuda130" not in text
    assert "cann900" not in text


def test_downstream_image_workflows_only_consume_parent_plan_and_matrices() -> None:
    feature = _load("release-vllm-images.yml")
    protected = _load("release-vllm-images-protected.yml")
    feature_inputs = feature["on"]["workflow_call"]["inputs"]
    assert set(feature_inputs) == {
        "source_sha",
        "wheel_matrix",
        "image_matrix",
        "resolved_plan_artifact",
        "resolved_plan_sha256",
        "deliver_full_oci",
    }
    protected_inputs = protected["on"]["workflow_call"]["inputs"]
    assert set(protected_inputs) == {
        "source_sha",
        "image_matrix",
        "family_matrix",
        "resolved_plan_artifact",
        "resolved_plan_sha256",
    }
    assert "plan" not in feature["jobs"]
    assert "plan" not in protected["jobs"]
    feature_build = feature["jobs"]["build-images-feature"]
    assert feature_build["strategy"]["matrix"] == "${{ fromJSON(inputs.image_matrix) }}"
    assert feature_build["with"]["task_id"] == "${{ matrix.task_id }}"
    protected_build = protected["jobs"]["build-images-protected"]
    assert (
        protected_build["strategy"]["matrix"] == "${{ fromJSON(inputs.image_matrix) }}"
    )
    assert protected_build["with"]["task_id"] == "${{ matrix.task_id }}"
    families = protected["jobs"]["publish-indexes"]
    assert families["strategy"]["matrix"] == "${{ fromJSON(inputs.family_matrix) }}"
    assert families["env"]["FAMILY_TASK_ID"] == "${{ matrix.family_task_id }}"
    assert families["env"]["CONTROL_TASK_ID"] == "${{ matrix.control_task_id }}"
    assert families["env"]["CONTROL_ARCH"] == "${{ matrix.control_arch }}"
    family_steps = families["steps"]
    select_family = next(
        step
        for step in family_steps
        if step.get("name") == "Select the exact plan-bound family control task"
    )
    install_tools = next(
        step
        for step in family_steps
        if step.get("name") == "Install checksum-pinned Registry tools"
    )
    assert family_steps.index(select_family) < family_steps.index(install_tools)
    assert "--task-kind family" in select_family["run"]
    assert ".control_arch" in select_family["run"]
    assert 'case "${CONTROL_ARCH}"' in install_tools["run"]
    assert "Linux_${crane_arch}" in install_tools["run"]
    assert "linux-${buildx_arch}" in install_tools["run"]
    assert "Linux_x86_64" not in install_tools["run"]
    assert "linux-amd64" not in install_tools["run"]
    for name in ("release-vllm-images.yml", "release-vllm-images-protected.yml"):
        text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
        assert "catalog resolve" not in text
        assert "core hosted-matrix" not in text
        assert "v0.5.0rc1" not in text
        assert "cuda130" not in text
        assert "cann900" not in text


def test_protected_member_publisher_reopens_one_frozen_task_before_auth() -> None:
    workflow = _load("_publish-image-member.yml")
    inputs = workflow["on"]["workflow_call"]["inputs"]
    assert set(inputs) == {
        "source_sha",
        "task_id",
        "runner",
        "resolved_plan_artifact",
        "resolved_plan_sha256",
    }
    build = workflow["jobs"]["build"]
    assert build["with"]["task_id"] == "${{ inputs.task_id }}"
    assert build["with"]["runner"] == "${{ inputs.runner }}"
    publisher = workflow["jobs"]["publish-member"]
    assert publisher["runs-on"] == "${{ inputs.runner }}"
    text = (WORKFLOW_DIR / "_publish-image-member.yml").read_text(encoding="utf-8")
    assert "catalog select" in text
    assert "catalog verify-drift" in text
    assert text.index("catalog verify-drift") < text.index(
        "Authenticate the pinned publisher"
    )
    assert '--expected-plan-sha256 "${RESOLVED_PLAN_SHA256}"' in text
    assert "v0.5.0rc1" not in text
    assert "cuda130" not in text
    assert "cann900" not in text
    assert "spec_id:" not in text


def test_pull_request_gate_invokes_smoke_projection_without_write_permissions() -> None:
    workflow = _load("pull-request.yml")
    smoke = workflow["jobs"]["release-catalog-smoke"]
    assert smoke["needs"] == ["pre-check"]
    assert smoke["uses"] == "./.github/workflows/release-ucm.yml"
    assert smoke["permissions"] == {"contents": "read"}
    assert smoke["with"] == {"deliver_full_oci": False}


def test_anonymous_closure_reopens_plan_and_uses_family_task_filenames() -> None:
    workflow = _load("release-ucm.yml")
    job = workflow["jobs"]["anonymous-registry-readback"]
    downloads = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert any(
        step["with"].get("name") == "${{ needs.plan.outputs.resolved_plan_artifact }}"
        and step["with"].get("path") == "input/plan"
        for step in downloads
    )
    text = (WORKFLOW_DIR / "release-ucm.yml").read_text(encoding="utf-8")
    anonymous = text[
        text.index("  anonymous-registry-readback:") : text.index("  publish-release:")
    ]
    assert "family_task_id:" in anonymous
    assert 'resolved_plan: "input/plan/resolved-plan.json"' in anonymous
    assert "resolved_plan_sha256:" in anonymous
    assert 'input/authenticated/provisionals/" + $family + ".json' in anonymous


def test_release_asset_publication_exports_and_threads_the_frozen_plan_hash() -> None:
    workflow = _load("release-ucm.yml")
    publish = workflow["jobs"]["publish-release"]
    step = next(item for item in publish["steps"] if item.get("id") == "publish")
    assert step["env"]["RESOLVED_PLAN_SHA256"] == (
        "${{ needs.plan.outputs.resolved_plan_sha256 }}"
    )
    run = step["run"]
    for action in (
        "assets-manifest",
        "plan-downloads",
        "plan-assets",
        "verify-upload-prefix",
        "record-upload-response",
        "verify-assets",
        "refresh-assets",
    ):
        assert f"release {action}" in run
    assert run.count("resolved_plan_sha256:") >= 9
    assert run.count('resolved_plan: "input/plan/resolved-plan.json"') >= 9


def test_release_draft_create_validates_downloaded_plan_before_write_request() -> None:
    """The draft job cannot form or POST a create request before plan validation."""
    workflow = _load("release-ucm.yml")
    draft = workflow["jobs"]["prepare-release-draft"]
    downloads = [
        step
        for step in draft["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert any(
        step["with"].get("name") == "${{ needs.plan.outputs.resolved_plan_artifact }}"
        and step["with"].get("path") == "input/plan"
        for step in downloads
    )
    create_step = next(step for step in draft["steps"] if step.get("id") == "draft")
    assert create_step["env"]["RESOLVED_PLAN_SHA256"] == (
        "${{ needs.plan.outputs.resolved_plan_sha256 }}"
    )
    run = str(create_step["run"])
    validation_index = run.index("release plan-state")
    first_gh_index = run.index("gh api")
    select_index = run.index("release select-pages")
    state_index = run.index("release plan-state", select_index)
    post_index = run.index("gh api --method POST")
    assert validation_index < first_gh_index < select_index < state_index < post_index
    before_post = run[:post_index]
    assert before_post.count('resolved_plan: "input/plan/resolved-plan.json"') >= 2
    assert before_post.count("resolved_plan_sha256:") >= 2


def test_protected_parent_uses_one_inventory_and_plan_bound_status_authority() -> None:
    text = (WORKFLOW_DIR / "release-vllm-images-protected.yml").read_text(
        encoding="utf-8"
    )
    parent = text[text.index("  aggregate-members:") : text.index("  publish-indexes:")]
    assert parent.count("registry inventory") == 0
    assert parent.count("registry plan-index") == 1
    assert ".member_record_sha256s" in parent
    assert 'x["task_id"]' not in parent
    assert ".inventory" in parent
    publisher = text[
        text.index("  publish-indexes:") : text.index("  authenticated-readback:")
    ]
    assert 'resolved_plan: "input/plan/resolved-plan.json"' in publisher
    assert "resolved_plan_sha256:" in publisher
