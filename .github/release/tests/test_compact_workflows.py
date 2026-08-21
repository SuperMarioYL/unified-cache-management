"""User-visible GitHub Actions contract for the compact release lane."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    value = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    if True in value and "on" not in value:
        value["on"] = value.pop(True)
    return value


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    return {value} if isinstance(value, str) else set(value)


def _artifact_steps(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [
        step
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith(f"actions/{action}-artifact@")
    ]


def _step_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job.get("steps", []) if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one {name!r} step"
    return matches[0]


def _noncomment_dockerfile(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _noncomment_shell(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        single_quoted = False
        double_quoted = False
        escaped = False
        for index, character in enumerate(line):
            if escaped:
                escaped = False
                continue
            if character == "\\" and not single_quoted:
                escaped = True
                continue
            if character == "'" and not double_quoted:
                single_quoted = not single_quoted
                continue
            if character == '"' and not single_quoted:
                double_quoted = not double_quoted
                continue
            if (
                character == "#"
                and not single_quoted
                and not double_quoted
                and (index == 0 or line[index - 1].isspace())
            ):
                line = line[:index].rstrip()
                break
        if line:
            lines.append(line)
    return "\n".join(lines)


def _shell_variable(variable: str) -> str:
    name = re.escape(variable)
    reference = rf"\$(?:\{{{name}\}}|{name})"
    return rf'(?:{reference}|"{reference}")'


def _has_failure_command(body: str) -> bool:
    return (
        re.search(
            r"(?m)(?:^|[;&)])[ \t]*"
            r"(?:exit(?:\s+[1-9][0-9]*)?|return(?:\s+[1-9][0-9]*)?|false)\b",
            body,
        )
        is not None
    )


def _digest_guard_positions(
    source: str, variable: str, consumer_position: int
) -> list[int]:
    variable_ref = _shell_variable(variable)
    digest_glob = r"\*\s*[\"']?@sha256:[\"']?\s*\*"
    bash_presence = re.compile(
        rf"^\s*{variable_ref}\s*(?:==|=)\s*{digest_glob}\s*$",
        re.DOTALL,
    )
    bash_absence = re.compile(
        rf"^\s*{variable_ref}\s*!=\s*{digest_glob}\s*$",
        re.DOTALL,
    )
    name = re.escape(variable)
    original = rf'"\$(?:\{{{name}\}}|{name})"'
    trimmed = rf'"\$\{{{name}(?:#{{1,2}}\*@sha256:|%{{1,2}}@sha256:\*)\}}"'
    posix_presence = re.compile(
        rf"^\s*(?:{trimmed}\s+!=\s+{original}|" rf"{original}\s+!=\s+{trimmed})\s*$",
        re.DOTALL,
    )
    posix_absence = re.compile(
        rf"^\s*(?:{trimmed}\s+=\s+{original}|" rf"{original}\s+=\s+{trimmed})\s*$",
        re.DOTALL,
    )
    positions: list[int] = []

    for guard in re.finditer(
        r"(?ms)^\s*\[\[(?P<condition>.*?)\]\](?P<suffix>[^\n]*)$",
        source,
    ):
        suffix = guard.group("suffix").strip()
        suffix_fails = re.fullmatch(
            r"\|\|\s*" r"(?:exit(?:\s+[1-9][0-9]*)?|return(?:\s+[1-9][0-9]*)?|false)",
            suffix,
        )
        errexit_precedes = re.search(
            r"(?m)^\s*set\s+(?:-[A-Za-z]*e[A-Za-z]*|-o\s+errexit)\b",
            source[: guard.start()],
        )
        if bash_presence.fullmatch(guard.group("condition")) and (
            suffix_fails or (not suffix and errexit_precedes)
        ):
            positions.append(guard.end())

    for guard in re.finditer(
        r"(?ms)^\s*if\s+(?:\[\[(?P<double>.*?)\]\]|"
        r"\[(?P<single>.*?)\]|test\s+(?P<test>.*?))\s*;?\s*then"
        r"(?P<body>.*?)^\s*fi(?:\s*;)?\s*$",
        source,
    ):
        condition = (
            guard.group("double") or guard.group("single") or guard.group("test") or ""
        )
        body = guard.group("body")
        else_clause = re.search(r"(?m)^\s*else\s*$", body)
        then_body = body if else_clause is None else body[: else_clause.start()]
        else_body = None if else_clause is None else body[else_clause.end() :]
        then_start = guard.start("body")
        then_end = then_start + len(then_body)
        else_start = (
            None if else_clause is None else then_end + len(else_clause.group(0))
        )

        presence = bash_presence if guard.group("double") else posix_presence
        absence = bash_absence if guard.group("double") else posix_absence
        if presence.fullmatch(condition):
            if then_start <= consumer_position < then_end:
                positions.append(guard.start("body"))
            if else_body is not None and _has_failure_command(else_body):
                positions.append(guard.end())
        elif absence.fullmatch(condition) and _has_failure_command(then_body):
            positions.append(guard.end())
            if else_start is not None and else_start <= consumer_position < guard.end():
                positions.append(guard.start("body"))

    for guard in re.finditer(
        r"(?ms)^\s*(?:\[(?P<single>.*?)\]|test\s+(?P<test>.*?))"
        r"\s*\|\|\s*"
        r"(?:exit(?:\s+[1-9][0-9]*)?|return(?:\s+[1-9][0-9]*)?|false)"
        r"\s*$",
        source,
    ):
        condition = guard.group("single") or guard.group("test") or ""
        if posix_presence.fullmatch(condition):
            positions.append(guard.end())

    for guard in re.finditer(
        rf"(?ms)^\s*case\s+{variable_ref}\s+in(?P<body>.*?)" r"^\s*esac(?:\s*;)?\s*$",
        source,
    ):
        arms = list(
            re.finditer(
                r"(?ms)^\s*(?P<pattern>[^\n)]+)\)" r"(?P<body>.*?)(?:;;|;&|;;&)",
                guard.group("body"),
            )
        )
        accepts_digest = any(
            arm.group("pattern").strip() == "*@sha256:*" for arm in arms
        )
        rejects_other = any(
            arm.group("pattern").strip() == "*"
            and _has_failure_command(arm.group("body"))
            for arm in arms
        )
        if accepts_digest and rejects_other:
            positions.append(guard.end())

    return positions


def _assert_digest_guard_precedes(
    run: str, variable: str, consumer_pattern: str
) -> None:
    source = _noncomment_shell(run)
    consumer = re.search(consumer_pattern, source, re.MULTILINE | re.DOTALL)
    assert consumer, f"{variable} must be consumed by the expected command"
    guards = _digest_guard_positions(source, variable, consumer.start())
    assert guards, (
        f"{variable} must have an executable [[...]], [...], test, or case "
        "digest validation"
    )
    assert any(
        position < consumer.start() for position in guards
    ), f"{variable} must be validated before it is consumed"


def _mismatch_result_branch(source: str) -> str | None:
    declared = re.compile(_shell_variable("declared_version"))
    installed = re.compile(_shell_variable("installed_version"))

    for branch in re.finditer(
        r"(?ms)^\s*if\s+(?:\[\[(?P<double>.*?)\]\]|"
        r"\[(?P<single>.*?)\]|test\s+(?P<test>.*?))"
        r"\s*;?\s*then(?P<body>.*?)"
        r"^\s*fi(?:\s*;)?\s*$",
        source,
    ):
        condition = (
            branch.group("double")
            or branch.group("single")
            or branch.group("test")
            or ""
        )
        if not declared.search(condition) or not installed.search(condition):
            continue
        bodies = re.split(r"(?m)^\s*else\s*$", branch.group("body"), maxsplit=1)
        if "!=" in condition:
            mismatch_body = bodies[0]
        elif re.search(r"(?:==|(?<![!<>=])=(?!=))", condition) and len(bodies) == 2:
            mismatch_body = bodies[1]
        else:
            continue
        if "out/mooncake-probe/result.json" in mismatch_body:
            return mismatch_body

    for branch in re.finditer(
        r"(?ms)^\s*case\s+(?P<expression>.*?)\s+in(?P<body>.*?)"
        r"^\s*esac(?:\s*;)?\s*$",
        source,
    ):
        arms = list(
            re.finditer(
                r"(?ms)^\s*(?P<pattern>[^\n)]+)\)" r"(?P<body>.*?)(?:;;|;&|;;&)",
                branch.group("body"),
            )
        )
        equality_source = (
            branch.group("expression")
            + "\n"
            + "\n".join(
                arm.group("pattern")
                for arm in arms
                if arm.group("pattern").strip() != "*"
            )
        )
        if not declared.search(equality_source) or not installed.search(
            equality_source
        ):
            continue
        for arm in arms:
            if arm.group(
                "pattern"
            ).strip() == "*" and "out/mooncake-probe/result.json" in arm.group("body"):
                return arm.group("body")
    return None


def _structured_json_producer(branch: str, path: str) -> str | None:
    output = rf'["\']?{re.escape(path)}["\']?'
    logical_lines = re.sub(r"\\\s*\n", " ", branch).splitlines()
    for command in logical_lines:
        if not re.match(r"^\s*jq\b", command):
            continue
        has_null_input = re.search(r"(?:^|\s)-[A-Za-z]*n[A-Za-z]*(?:\s|$)", command)
        if has_null_input and re.search(rf">\s*{output}", command):
            return command

    python_commands = [
        line for line in logical_lines if re.match(r"^\s*python(?:3)?\b", line)
    ]
    for command in python_commands:
        if re.search(r"\bjson\.dumps?\s*\(", command) and (
            re.search(rf">\s*{output}", command)
            or re.search(rf"(?:open|Path)\s*\(\s*{output}", command)
        ):
            return command

    for command in re.finditer(
        r"(?ms)^\s*python(?:3)?\b(?P<header>[^\n]*?)"
        r"<<-?\s*[\"']?(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)[\"']?"
        r"(?P<tail>[^\n]*)\n(?P<body>.*?)"
        r"^\s*(?P=delimiter)\s*$",
        branch,
    ):
        producer = command.group(0)
        if re.search(r"\bjson\.dumps?\s*\(", producer) and (
            re.search(rf">\s*{output}", command.group("tail"))
            or re.search(rf"(?:open|Path)\s*\(\s*{output}", producer)
        ):
            return producer
    return None


def _cli_command(run: str, group: str, action: str) -> list[str]:
    source = re.sub(r"\\\s*\n", " ", _noncomment_shell(run))
    pattern = (
        rf"(?m)^\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)*"
        rf"python(?:3)?\s+-m\s+ucm_release\s+{re.escape(group)}\s+"
        rf"{re.escape(action)}\b[^\n]*$"
    )
    matches = list(re.finditer(pattern, source))
    assert len(matches) == 1
    command = shlex.split(matches[0].group(0))
    python_index = next(
        index for index, token in enumerate(command) if token in {"python", "python3"}
    )
    assert command[python_index : python_index + 5] == [
        command[python_index],
        "-m",
        "ucm_release",
        group,
        action,
    ]
    return command[python_index:]


def _assert_cli_options(command: list[str], expected: dict[str, str]) -> None:
    for option, value in expected.items():
        assert command.count(option) == 1
        index = command.index(option)
        assert command[index + 1] == value


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    return []


def _assert_no_fixed_mooncake_sources(sources: list[str]) -> None:
    normalized = [re.sub(r"\\\s*\n", " ", source) for source in sources]
    active_source = "\n;\n".join(normalized)

    assert (
        re.search(r"mooncake[\s_-]*installer\.sh", active_source, re.IGNORECASE) is None
    )
    assert (
        re.search(
            r"(?im)(?:--build-arg(?:\s+|=)|^[ \t-]*)[\"']?\s*" r"MOONCAKE_TAG\s*=",
            active_source,
        )
        is None
    )
    assert (
        re.search(
            r"(?is)\bgit\s+clone\b(?:(?!&&|;).){0,512}"
            r"(?:github\.com[/:](?:kvcache-ai/)?mooncake(?:\.git)?|\bmooncake\b)",
            active_source,
        )
        is None
    )
    assert (
        re.search(
            r"(?im)^(?=[^;\n]*\bmooncake(?:_tag|_version)?\b)"
            r"(?=[^;\n]*\btarget_tag\b)[^;\n]*$",
            active_source,
        )
        is None
    )
    assert (
        re.search(
            r"(?is)\bmooncake(?:_tag|_version)?\b\s*=\s*" r"[^;&]{0,512}\btarget_tag\b",
            active_source,
        )
        is None
    )
    assert re.search(r"(?<![0-9])0\.3\.9(?![0-9])", active_source) is None


def _normalized_with(value: dict[str, Any]) -> tuple[tuple[str, object], ...]:
    return tuple(sorted(value.items()))


def test_release_workflow_has_six_visible_stages_and_flat_build_matrices() -> None:
    jobs = _load("release-ucm.yml")["jobs"]

    assert list(jobs) == [
        "sync-builders",
        "plan",
        "build-wheels",
        "package-chart",
        "build-images",
        "publish-release",
    ]
    assert jobs["build-wheels"]["name"] == "Wheel · ${{ matrix.label }}"
    assert jobs["build-images"]["name"] == "Image · ${{ matrix.label }}"
    assert jobs["build-images"]["uses"] == "./.github/workflows/_build-image.yml"
    assert set(jobs["build-images"]["needs"]) == {"plan", "build-wheels"}
    assert set(jobs["publish-release"]["needs"]) == {
        "plan",
        "build-wheels",
        "package-chart",
        "build-images",
    }
    assert jobs["build-images"]["with"]["upload_oci"] == (
        "${{ needs.plan.outputs.route == 'release' || "
        "inputs.deliver_full_oci == true }}"
    )


def test_builder_sync_exports_run_scoped_capability_catalog_from_assembly() -> None:
    workflow = _load("sync-builders.yml")
    jobs = workflow["jobs"]
    outputs = workflow["on"]["workflow_call"]["outputs"]

    assert {
        "prepare",
        "discover-runtimes",
        "probe-mooncake",
        "plan-builder-sync",
        "build-missing",
        "collect-builder-revisions",
        "probe-python",
        "assemble-capability-catalog",
    } <= set(jobs)
    assert jobs["build-missing"]["name"] == "Builder · ${{ matrix.label }}"
    assert "capability_catalog_artifact" in outputs
    prepare = jobs["prepare"]
    prepare_outputs = set(prepare.get("outputs", {}))
    assert "builder_catalog_artifact" in prepare_outputs
    assert {
        "matrix",
        "has_missing",
        "python_probe_matrix",
        "builder_fact_id",
        "builder_revision_id",
    }.isdisjoint(prepare_outputs)
    prepare_source = yaml.safe_dump(prepare, sort_keys=False)
    for predicted in (
        "python_probe_matrix",
        "builder_fact_id",
        "builder_revision_id",
        "target_builder_digest",
    ):
        assert predicted not in prepare_source
    assert (
        outputs["capability_catalog_artifact"]["value"]
        == "${{ jobs.assemble-capability-catalog.outputs.capability_catalog_artifact }}"
    )
    assembly_outputs = jobs["assemble-capability-catalog"].get("outputs", {})
    assert "capability_catalog_artifact" in assembly_outputs
    assert assembly_outputs["capability_catalog_artifact"] == (
        "${{ steps.catalog.outputs.capability_catalog_artifact }}"
    )


def test_python_probe_matrix_enumerates_all_abis_on_native_builder_runners() -> None:
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"].get("probe-python")

    assert isinstance(job, dict), "missing stable probe-python job"
    assert _needs(job) == {"collect-builder-revisions"}
    condition = str(job.get("if", ""))
    assert "always()" in condition
    assert "needs.collect-builder-revisions.result" in condition
    assert "success" in condition
    assert job["strategy"].get("fail-fast") is False
    assert job["strategy"].get("matrix") == (
        "${{ fromJSON(needs.collect-builder-revisions.outputs.python_probe_matrix) }}"
    )
    assert job["runs-on"] == "${{ matrix.runner }}"
    probe_steps = [
        step
        for step in job["steps"]
        if "/opt/python/cp*-cp*/bin/python" in str(step.get("run", ""))
    ]
    assert len(probe_steps) == 1
    probe = probe_steps[0]
    assert probe.get("env", {}).get("BUILDER_IMAGE") == "${{ matrix.builder_image }}"
    assert probe.get("env", {}).get("BUILDER_FACT_ID") == (
        "${{ matrix.builder_fact_id }}"
    )
    assert probe.get("env", {}).get("TARGET_BUILDER_DIGEST") == (
        "${{ matrix.target_builder_digest }}"
    )
    run = str(probe["run"])
    _assert_digest_guard_precedes(
        run,
        "BUILDER_IMAGE",
        r'(?m)^\s*docker\s+run\b[\s\S]*?"\$\{BUILDER_IMAGE\}"',
    )
    assert "out/python-probe/result.json" in run
    assert "builder_fact_id" in run
    assert "builder_image" in run
    for field in (
        "target_builder_digest",
        "interpreter_path",
        "python_version",
        "python_abi",
        "wheel_tag",
        "cpu_architecture",
    ):
        assert field in run
    probe_source = yaml.safe_dump(probe, sort_keys=False)
    assert "builder_revision_id" not in probe_source
    assert "builder_source_image_digest" not in probe_source
    assert "cp312" not in run
    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ always() }}"
    assert uploads[0]["with"] == {
        "name": (
            "ucm-python-probe-${{ matrix.id }}-run-${{ github.run_id }}-"
            "attempt-${{ github.run_attempt }}"
        ),
        "path": "out/python-probe/result.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


def test_runtime_discovery_records_immutable_image_and_git_source_facts() -> None:
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"].get("discover-runtimes")

    assert isinstance(job, dict), "missing stable discover-runtimes job"
    assert _needs(job) == {"prepare"}
    assert {
        "runtime_discovery_artifact",
        "runtime_probe_matrix",
    } <= set(job.get("outputs", {}))
    discover_steps = [
        step
        for step in job["steps"]
        if "out/runtime-discovery.json" in str(step.get("run", ""))
    ]
    assert len(discover_steps) == 1
    run = str(discover_steps[0]["run"])
    for project in ("vllm", "vllm-ascend"):
        assert project in run
    for field in (
        "runtime_image",
        "runtime_image_digest",
        "runtime_dockerfile",
        "git_tag",
        "git_commit",
        "variant",
        "cpu_architecture",
        "runner",
    ):
        assert field in run
    assert (
        "ucm-runtime-discovery-run-${GITHUB_RUN_ID}-attempt-"
        "${GITHUB_RUN_ATTEMPT}" in run
    )
    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ always() }}"
    assert uploads[0]["with"] == {
        "name": job["outputs"]["runtime_discovery_artifact"],
        "path": "out/runtime-discovery.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


def test_mooncake_probe_compares_runtime_dockerfile_tag_with_installed_version() -> (
    None
):
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"].get("probe-mooncake")

    assert isinstance(job, dict), "missing stable probe-mooncake job"
    assert _needs(job) == {"discover-runtimes"}
    condition = str(job.get("if", ""))
    assert "always()" in condition
    assert "needs.discover-runtimes.result" in condition and "success" in condition
    assert job["strategy"].get("fail-fast") is False
    assert job["strategy"].get("matrix") == (
        "${{ fromJSON(needs.discover-runtimes.outputs.runtime_probe_matrix) }}"
    )
    assert job["runs-on"] == "${{ matrix.runner }}"
    probe_steps = [
        step
        for step in job["steps"]
        if "out/mooncake-probe/result.json" in str(step.get("run", ""))
    ]
    assert len(probe_steps) == 1
    probe = probe_steps[0]
    assert probe.get("env", {}).get("RUNTIME_ID") == "${{ matrix.runtime_id }}"
    assert probe.get("env", {}).get("RUNTIME_IMAGE") == "${{ matrix.runtime_image }}"
    assert probe.get("env", {}).get("RUNTIME_IMAGE_DIGEST") == (
        "${{ matrix.runtime_image_digest }}"
    )
    assert probe.get("env", {}).get("RUNTIME_DOCKERFILE") == (
        "${{ matrix.runtime_dockerfile }}"
    )
    assert probe.get("env", {}).get("CPU_ARCHITECTURE") == (
        "${{ matrix.cpu_architecture }}"
    )
    run = str(probe["run"])
    source = _noncomment_shell(run)
    assert "MOONCAKE_TAG" in source
    assert "${RUNTIME_DOCKERFILE}" in source
    _assert_digest_guard_precedes(
        run,
        "RUNTIME_IMAGE",
        r'(?m)^\s*docker\s+run\b[\s\S]*?"\$\{RUNTIME_IMAGE\}"',
    )
    mismatch_body = _mismatch_result_branch(source)
    assert (
        mismatch_body is not None
    ), "missing executable declared/installed mismatch Result branch"
    result_path = "out/mooncake-probe/result.json"
    producer = _structured_json_producer(mismatch_body, result_path)
    assert (
        producer is not None
    ), "mismatch Result must be written by jq -n or a Python JSON writer"
    assert "reason_code" in producer
    assert "mooncake-version-mismatch" in producer
    for field in (
        "runtime_id",
        "runtime_image",
        "runtime_image_digest",
        "runtime_dockerfile",
        "cpu_architecture",
    ):
        assert field in producer
    assert (
        re.search(
            rf"(?m)^\s*(?:echo|printf)\b[^\n]*>\s*"
            rf'["\']?{re.escape(result_path)}["\']?',
            mismatch_body,
        )
        is None
    )
    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ always() }}"
    assert uploads[0]["with"] == {
        "name": (
            "ucm-mooncake-probe-${{ matrix.id }}-run-${{ github.run_id }}-"
            "attempt-${{ github.run_attempt }}"
        ),
        "path": "out/mooncake-probe/result.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


def test_builder_sync_plan_waits_for_runtime_and_mooncake_results() -> None:
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"].get("plan-builder-sync")

    assert isinstance(job, dict), "missing stable plan-builder-sync job"
    assert {"prepare", "discover-runtimes", "probe-mooncake"} <= _needs(job)
    condition = str(job.get("if", ""))
    assert "always()" in condition
    for dependency in ("prepare", "discover-runtimes"):
        assert re.search(
            rf"needs\.{re.escape(dependency)}\.result\s*==\s*['\"]success['\"]",
            condition,
        )
    assert set(re.findall(r"needs\.([a-z0-9-]+)\.result", condition)) == {
        "prepare",
        "discover-runtimes",
    }
    assert "needs.probe-mooncake.result" not in condition
    assert {"builder_plan_artifact", "matrix"} <= set(job.get("outputs", {}))

    required_downloads = {
        _normalized_with(
            {
                "name": "${{ needs.prepare.outputs.builder_catalog_artifact }}",
                "path": "input/builder-source",
            }
        ),
        _normalized_with(
            {
                "name": (
                    "${{ needs.discover-runtimes.outputs."
                    "runtime_discovery_artifact }}"
                ),
                "path": "input/runtime-discovery",
            }
        ),
        _normalized_with(
            {
                "pattern": (
                    "ucm-mooncake-probe-*-run-${{ github.run_id }}-"
                    "attempt-${{ github.run_attempt }}"
                ),
                "path": "input/mooncake-probes",
                "merge-multiple": True,
            }
        ),
    }
    downloads = {
        _normalized_with(step.get("with", {}))
        for step in _artifact_steps(job, "download")
    }
    assert required_downloads <= downloads
    plan_steps = [
        step
        for step in job["steps"]
        if "out/builder-sync-plan.json" in str(step.get("run", ""))
    ]
    assert len(plan_steps) == 1
    plan = plan_steps[0]
    assert plan.get("id") == "plan"
    run = _noncomment_shell(str(plan["run"]))
    command = _cli_command(run, "builders", "plan-facts")
    _assert_cli_options(
        command,
        {
            "--builder-catalog": "input/builder-source/builder-catalog.json",
            "--runtime-discovery": (
                "input/runtime-discovery/runtime-discovery.json"
            ),
            "--mooncake-probes": "input/mooncake-probes",
            "--output": "out/builder-sync-plan.json",
        },
    )
    assert "matrix=" in run
    assert "builder_plan_artifact=" in run
    assert "${GITHUB_OUTPUT}" in run
    assert job["outputs"]["builder_plan_artifact"] == (
        "${{ steps.plan.outputs.builder_plan_artifact }}"
    )
    assert job["outputs"]["matrix"] == "${{ steps.plan.outputs.matrix }}"
    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0].get("if") == "${{ always() }}"
    assert uploads[0]["with"] == {
        "name": "${{ steps.plan.outputs.builder_plan_artifact }}",
        "path": "out/builder-sync-plan.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


def test_builder_fanout_always_emits_existing_built_or_failed_result() -> None:
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"].get("build-missing")

    assert isinstance(job, dict), "missing stable build-missing job"
    assert _needs(job) == {"plan-builder-sync"}
    condition = str(job.get("if", ""))
    assert "always()" in condition
    assert "needs.plan-builder-sync.result" in condition and "success" in condition
    assert job["strategy"].get("fail-fast") is False
    assert job["strategy"].get("matrix") == (
        "${{ fromJSON(needs.plan-builder-sync.outputs.matrix) }}"
    )
    assert job["runs-on"] == "${{ matrix.runner }}"
    job_source = yaml.safe_dump(job, sort_keys=False)
    assert "matrix.builder_fact_id" not in job_source
    assert "matrix.builder_revision_id" not in job_source
    assert "matrix.target_builder_digest" not in job_source

    build = _step_named(job, "Build missing Builder")
    assert build.get("id") == "build"
    build_run = _noncomment_shell(str(build.get("run", "")))
    assert "docker buildx imagetools inspect" in build_run
    assert "docker buildx build" in build_run or "imagetools create" in build_run
    result_steps = [
        step
        for step in job["steps"]
        if "out/builder-result/result.json" in str(step.get("run", ""))
    ]
    assert len(result_steps) == 1
    result = result_steps[0]
    assert "always()" in str(result.get("if", ""))
    result_run = _noncomment_shell(str(result["run"]))
    assert "out/builder-result/result.json" in result_run
    assert re.search(
        r"(?m)^\s*docker\s+buildx\s+imagetools\s+inspect\b", result_run
    )
    assert result.get("env", {}).get("BUILDER_PLAN_ID") == (
        "${{ matrix.builder_plan_id }}"
    )
    assert result.get("env", {}).get("BUILD_OUTCOME") == (
        "${{ steps.build.outcome }}"
    )
    assert result.get("env", {}).get("TARGET_REPOSITORY") == (
        "${{ matrix.target_repository }}"
    )
    assert result.get("env", {}).get("TARGET_TAG") == "${{ matrix.target_tag }}"
    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0].get("if") == "${{ always() }}"
    assert uploads[0]["with"] == {
        "name": (
            "ucm-builder-result-${{ matrix.id }}-run-${{ github.run_id }}-"
            "attempt-${{ github.run_attempt }}"
        ),
        "path": "out/builder-result/result.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


def test_builder_collector_links_every_result_before_python_probe_matrix() -> None:
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"].get("collect-builder-revisions")

    assert isinstance(job, dict), "missing stable collect-builder-revisions job"
    assert {"plan-builder-sync", "build-missing"} <= _needs(job)
    condition = str(job.get("if", ""))
    assert "always()" in condition
    assert "needs.plan-builder-sync.result" in condition and "success" in condition
    assert {"builder_facts_artifact", "python_probe_matrix"} <= set(
        job.get("outputs", {})
    )
    assert job["outputs"]["builder_facts_artifact"] == (
        "${{ steps.collect.outputs.builder_facts_artifact }}"
    )
    assert job["outputs"]["python_probe_matrix"] == (
        "${{ steps.collect.outputs.python_probe_matrix }}"
    )
    required_downloads = {
        _normalized_with(
            {
                "name": "${{ needs.plan-builder-sync.outputs.builder_plan_artifact }}",
                "path": "input/builder-plan",
            }
        ),
        _normalized_with(
            {
                "pattern": (
                    "ucm-builder-result-*-run-${{ github.run_id }}-"
                    "attempt-${{ github.run_attempt }}"
                ),
                "path": "input/builder-results",
                "merge-multiple": True,
            }
        ),
    }
    downloads = {
        _normalized_with(step.get("with", {}))
        for step in _artifact_steps(job, "download")
    }
    assert required_downloads <= downloads
    collect_steps = [step for step in job["steps"] if step.get("id") == "collect"]
    assert len(collect_steps) == 1
    run = _noncomment_shell(str(collect_steps[0]["run"]))
    command = _cli_command(run, "builders", "collect-facts")
    _assert_cli_options(
        command,
        {
            "--plan": "input/builder-plan/builder-sync-plan.json",
            "--results": "input/builder-results",
            "--output": "out/builder-facts.json",
        },
    )
    assert "${GITHUB_OUTPUT}" in run
    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0].get("if") == "${{ always() }}"
    assert uploads[0]["with"] == {
        "name": "${{ steps.collect.outputs.builder_facts_artifact }}",
        "path": "out/builder-facts.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


def test_catalog_assembly_waits_for_all_results_and_calls_stable_cli() -> None:
    workflow = _load("sync-builders.yml")
    assembly = workflow["jobs"].get("assemble-capability-catalog")

    assert isinstance(assembly, dict), "missing Capability Catalog assembly job"
    assert {
        "collect-builder-revisions",
        "probe-python",
        "discover-runtimes",
        "probe-mooncake",
    } <= _needs(assembly)
    assert assembly.get("if") == "${{ always() }}"
    downloads = _artifact_steps(assembly, "download")
    required_download_values = [
        {
            "name": (
                "${{ needs.collect-builder-revisions.outputs."
                "builder_facts_artifact }}"
            ),
            "path": "input/builders",
        },
        {
            "pattern": (
                "ucm-python-probe-*-run-${{ github.run_id }}-"
                "attempt-${{ github.run_attempt }}"
            ),
            "path": "input/python-probes",
            "merge-multiple": True,
        },
        {
            "name": "${{ needs.discover-runtimes.outputs.runtime_discovery_artifact }}",
            "path": "input/runtime-discovery",
        },
        {
            "pattern": (
                "ucm-mooncake-probe-*-run-${{ github.run_id }}-"
                "attempt-${{ github.run_attempt }}"
            ),
            "path": "input/mooncake-probes",
            "merge-multiple": True,
        },
    ]
    required_downloads = {_normalized_with(value) for value in required_download_values}
    actual_downloads = {_normalized_with(step.get("with", {})) for step in downloads}
    assert required_downloads <= actual_downloads
    catalog_steps = [step for step in assembly["steps"] if step.get("id") == "catalog"]
    assert len(catalog_steps) == 1
    run = _noncomment_shell(str(catalog_steps[0]["run"]))
    command = _cli_command(run, "catalog", "assemble-capabilities")
    _assert_cli_options(
        command,
        {
            "--builder-facts": "input/builders/builder-facts.json",
            "--python-probes": "input/python-probes",
            "--runtime-discovery": (
                "input/runtime-discovery/runtime-discovery.json"
            ),
            "--mooncake-probes": "input/mooncake-probes",
            "--output": "out/capability-catalog.json",
        },
    )
    assert (
        "ucm-capability-catalog-run-${GITHUB_RUN_ID}-attempt-"
        "${GITHUB_RUN_ATTEMPT}" in run
    )
    assert "capability_catalog_artifact=${artifact}" in run
    assert "${GITHUB_OUTPUT}" in run
    uploads = _artifact_steps(assembly, "upload")
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ always() }}"
    output_value = "${{ steps.catalog.outputs.capability_catalog_artifact }}"
    assert assembly["outputs"]["capability_catalog_artifact"] == output_value
    assert uploads[0]["with"] == {
        "name": output_value,
        "path": "out/capability-catalog.json",
        "if-no-files-found": "error",
        "overwrite": False,
        "retention-days": 7,
    }


@pytest.mark.parametrize("filename", ["release-ucm.yml", "ucm-build-bot.yml"])
def test_planners_consume_capability_catalog_instead_of_flat_builder_catalog(
    filename: str,
) -> None:
    workflow = _load(filename)
    plan = workflow["jobs"]["plan"]
    downloads = [
        step
        for step in _artifact_steps(plan, "download")
        if step.get("with", {}).get("path") == "input/capabilities"
    ]
    assert len(downloads) == 1
    assert downloads[0]["with"] == {
        "name": "${{ needs.sync-builders.outputs.capability_catalog_artifact }}",
        "path": "input/capabilities",
    }
    plan_steps = [
        step
        for step in plan["steps"]
        if "ucm_release compact plan" in str(step.get("run", ""))
    ]
    assert len(plan_steps) == 1
    run = str(plan_steps[0]["run"])
    assert "--capability-catalog input/capabilities/capability-catalog.json" in run
    assert "--builder-catalog" not in run


def test_ascend_builder_copies_mooncake_from_matching_immutable_runtime() -> None:
    workflow = _load("sync-builders.yml")
    job = workflow["jobs"]["build-missing"]
    build = _step_named(job, "Build missing Builder")
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.builder"
    ).read_text(encoding="utf-8")

    assert build.get("env", {}).get("RUNTIME_IMAGE") == (
        "${{ matrix.mooncake_source_runtime_image }}"
    )
    assert build.get("env", {}).get("RUNTIME_ID") == (
        "${{ matrix.mooncake_source_runtime_id }}"
    )
    assert build.get("env", {}).get("MOONCAKE_VERSION") == (
        "${{ matrix.mooncake_version }}"
    )
    run = str(build["run"])
    _assert_digest_guard_precedes(
        run,
        "RUNTIME_IMAGE",
        r"(?m)^\s*docker\s+buildx\s+build\b[\s\S]*?"
        r'--build-arg\s+"MOONCAKE_RUNTIME_IMAGE=\$\{RUNTIME_IMAGE\}"',
    )
    instructions = _noncomment_dockerfile(dockerfile)
    runtime_stage = re.search(
        r"^ARG\s+(?P<arg>[A-Z_]*RUNTIME_IMAGE)\s*$\n"
        r"FROM\s+\$\{(?P=arg)\}\s+AS\s+(?P<stage>[-a-z0-9]+)$",
        instructions,
        re.MULTILINE,
    )
    assert runtime_stage
    assert runtime_stage.group("arg") == "MOONCAKE_RUNTIME_IMAGE"
    stage = runtime_stage.group("stage")
    assert re.search(
        rf"^COPY\s+--from={re.escape(stage)}\s+.*include",
        instructions,
        re.MULTILINE,
    )
    assert re.search(
        rf"^COPY\s+--from={re.escape(stage)}\s+.*lib",
        instructions,
        re.MULTILINE,
    )


def test_ascend_builder_has_no_tag_inference_or_fixed_mooncake_clone() -> None:
    workflow = _load("sync-builders.yml")
    build = _step_named(workflow["jobs"]["build-missing"], "Build missing Builder")
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.builder"
    ).read_text(encoding="utf-8")
    instructions = _noncomment_dockerfile(dockerfile)
    parsed_build_sources = [
        _noncomment_shell(str(build.get("run", ""))),
        *_string_values(build.get("with", {})),
    ]
    _assert_no_fixed_mooncake_sources([*parsed_build_sources, instructions])


def test_probe_matrices_isolate_failures_and_always_upload_results() -> None:
    workflow = _load("sync-builders.yml")
    for job_id in ("build-missing", "probe-python", "probe-mooncake"):
        job = workflow["jobs"].get(job_id)
        assert isinstance(job, dict), f"missing stable {job_id} job"
        assert job["strategy"].get("fail-fast") is False
        uploads = _artifact_steps(job, "upload")
        assert len(uploads) == 1
        assert uploads[0].get("if") == "${{ always() }}"


def test_reusable_builds_expose_only_functional_inputs() -> None:
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
        text = (WORKFLOWS / filename).read_text(encoding="utf-8").lower()
        assert "resolved_plan_sha256" not in text
        assert "task_sha256" not in text
        assert "source_sha" not in text


def test_compact_wheel_passes_cpu_architecture_to_the_native_build() -> None:
    workflow = _load("_build-wheel.yml")
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.wheel"
    ).read_text(encoding="utf-8")
    text = yaml.safe_dump(workflow)

    assert "UCM_CPU_ARCH=$(jq -r '.cpu_arch' out/wheel-task.json)" in text
    assert "ARG UCM_CPU_ARCH" in dockerfile
    assert 'UCM_BUILD_CPU_ARCH="${UCM_CPU_ARCH}"' in dockerfile


def test_runtime_image_checks_ucm_without_auditing_the_base_environment() -> None:
    dockerfile = (
        ROOT / ".github" / "release" / "docker" / "Dockerfile.runtime"
    ).read_text(encoding="utf-8")

    assert "python3 -c 'import ucm'" in dockerfile
    assert "pip check" not in dockerfile


def test_removed_wrapper_workflows_are_absent() -> None:
    for name in (
        "release-vllm-images.yml",
        "release-vllm-images-protected.yml",
        "_publish-image-member.yml",
    ):
        assert not (WORKFLOWS / name).exists()


def test_single_publish_job_consumes_all_channel_switches_and_finishes_release_last() -> (
    None
):
    job = _load("release-ucm.yml")["jobs"]["publish-release"]
    steps = job["steps"]
    text = yaml.safe_dump(job)

    for channel in ("pypi", "ghcr", "dockerhub", "chart_oci", "github_release"):
        assert f".publish.{channel}.enabled" in text
    assert "${target_tag}-${arch}" in text
    assert "docker buildx imagetools create" in text
    assert steps[-1]["name"] == "Upload assets and publish GitHub Release"
    assert "gh release edit" in steps[-1]["run"]


def test_ucm_build_bot_uses_compact_plan_and_functional_build_inputs() -> None:
    workflow = _load("ucm-build-bot.yml")
    text = (WORKFLOWS / "ucm-build-bot.yml").read_text(encoding="utf-8")

    assert "ucm_release compact plan" in text
    assert "resolved_plan_sha256" not in text
    assert "task_sha256" not in text
    assert set(workflow["jobs"]["build-wheels"]["with"]) == {
        "source_ref",
        "wheel_id",
        "runner",
        "plan_artifact",
    }
    assert set(workflow["jobs"]["build-images"]["with"]) == {
        "source_ref",
        "image_id",
        "runner",
        "plan_artifact",
        "upload_oci",
    }
