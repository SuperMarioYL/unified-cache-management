"""User-visible GitHub Actions contract for the compact release lane."""

from __future__ import annotations

import ast
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


def _docker_run_commands(source: str) -> list[list[str]]:
    lines = _noncomment_shell(source).splitlines()
    commands: list[list[str]] = []
    for start, line in enumerate(lines):
        match = re.match(
            r"^\s*(?:(?:[A-Z_][A-Z0-9_]*)=\S+\s+)*docker\s+run\b",
            line,
        )
        if match is None:
            continue
        candidate: list[str] = []
        for continuation in lines[start:]:
            candidate.append(continuation)
            joined = "\n".join(candidate)
            if joined.rstrip().endswith("\\"):
                continue
            try:
                tokens = shlex.split(joined)
            except ValueError:
                continue
            docker_index = tokens.index("docker")
            if tokens[docker_index : docker_index + 2] == ["docker", "run"]:
                commands.append(tokens[docker_index:])
            break
    return commands


def _mooncake_shell_run_tokens(source: str) -> list[str] | None:
    matches: list[list[str]] = []
    image_tokens = {"$RUNTIME_IMAGE", "${RUNTIME_IMAGE}"}
    for tokens in _docker_run_commands(source):
        image_positions = [
            index for index, token in enumerate(tokens) if token in image_tokens
        ]
        if len(image_positions) != 1:
            continue
        image_position = image_positions[0]
        entrypoints: list[str] = []
        for index, token in enumerate(tokens[2:image_position], start=2):
            if token == "--entrypoint" and index + 1 < image_position:
                entrypoints.append(tokens[index + 1])
            elif token.startswith("--entrypoint="):
                entrypoints.append(token.split("=", 1)[1])
        if entrypoints != ["sh"]:
            continue
        if tokens[image_position + 1 : image_position + 2] != ["-c"]:
            continue
        matches.append(tokens)
    assert len(matches) <= 1, "ambiguous Mooncake runtime consumers"
    return matches[0] if matches else None


def _shell_command_argvs(source: str) -> list[list[str]]:
    active = _noncomment_shell(source).replace("\n", " ; ")
    lexer = shlex.shlex(active, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    commands: list[list[str]] = []
    command: list[str] = []
    for token in lexer:
        if token and set(token) <= set(";&|()"):
            if command:
                commands.append(command)
                command = []
            continue
        command.append(token)
    if command:
        commands.append(command)
    return commands


def _mooncake_probe_payload(command: list[str]) -> str:
    image_tokens = {"$RUNTIME_IMAGE", "${RUNTIME_IMAGE}"}
    image_position = next(
        index for index, token in enumerate(command) if token in image_tokens
    )
    assert command[image_position + 1] == "-c"
    return command[image_position + 2]


def _has_exact_mooncake_checkout_tag_readback(source: str) -> bool:
    checkout = "/vllm-workspace/Mooncake"
    commands = _shell_command_argvs(source)
    candidates = list(commands)
    for command in commands:
        if len(command) != 1:
            continue
        assignment = re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=\$\((?P<command>.*)\)",
            command[0],
        )
        if assignment is not None:
            candidates.append(shlex.split(assignment.group("command")))
    for index, command in enumerate(candidates):
        if not command or command[0] != "git":
            continue
        arguments = command[1:]
        checkout_bound = arguments[:2] == ["-C", checkout]
        if checkout_bound:
            arguments = arguments[2:]
        elif (
            index < len(commands)
            and index > 0
            and commands[index - 1]
            == [
                "cd",
                checkout,
            ]
        ):
            checkout_bound = True
        if not checkout_bound or not arguments or arguments[0] != "describe":
            continue
        options = set(arguments[1:])
        if {"--tags", "--exact-match"} <= options and "--always" not in options:
            return True
    return False


def _has_mooncake_tag_canonicalization(source: str) -> bool:
    parameter_trim = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*#v\}")
    for command in _shell_command_argvs(source):
        if (
            command
            and command[0] in {"echo", "printf"}
            and any(parameter_trim.fullmatch(token) for token in command[1:])
        ):
            return True
        if (
            command
            and command[0] == "sed"
            and any(re.search(r"s.\^v.", token) for token in command[1:])
        ):
            return True
    return False


def _has_mooncake_header_and_library_checks(source: str) -> bool:
    header = "/usr/local/include/transfer_engine.h"
    header_checked = False
    library_checked = False
    for command in _shell_command_argvs(source):
        if (
            len(command) > 2
            and command[0] == "test"
            and command[1] in {"-e", "-f", "-r"}
            and command[2] == header
        ):
            header_checked = True
        if (
            len(command) > 2
            and command[0] == "["
            and command[1] in {"-e", "-f", "-r"}
            and command[2] == header
        ):
            header_checked = True
        if command[:1] == ["stat"] and header in command[1:]:
            header_checked = True
        if command[:2] == ["find", "/usr/local/lib"]:
            library_checked = library_checked or (
                "-type" in command
                and "f" in command
                and any(
                    "mooncake" in token.lower() or "transfer_engine" in token.lower()
                    for token in command
                )
            )
        if (
            len(command) > 2
            and command[0] == "test"
            and command[1] in {"-e", "-f", "-r"}
            and command[2].startswith("/usr/local/lib/")
        ):
            library_checked = library_checked or any(
                name in command[2].lower() for name in ("mooncake", "transfer_engine")
            )
        if (
            len(command) > 2
            and command[0] == "["
            and command[1] in {"-e", "-f", "-r"}
            and command[2].startswith("/usr/local/lib/")
        ):
            library_checked = library_checked or any(
                name in command[2].lower() for name in ("mooncake", "transfer_engine")
            )
    return header_checked and library_checked


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
            if arm.group("pattern").strip() == "*":
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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "docker run --network none --entrypoint sh --rm "
            '"${RUNTIME_IMAGE}" -c true',
            True,
            id="separate-entrypoint-with-additional-flags",
        ),
        pytest.param(
            'docker run --rm --entrypoint=sh "$RUNTIME_IMAGE" -c true',
            True,
            id="entrypoint-equals",
        ),
        pytest.param(
            '# docker run --entrypoint sh "${RUNTIME_IMAGE}" -c true',
            False,
            id="comment-is-dead",
        ),
        pytest.param(
            'echo docker run --entrypoint sh "${RUNTIME_IMAGE}" -c true',
            False,
            id="echo-is-not-executable",
        ),
        pytest.param(
            'docker run --entrypoint bash "${RUNTIME_IMAGE}" -c true',
            False,
            id="wrong-entrypoint",
        ),
        pytest.param(
            'docker run --entrypoint sh "${RUNTIME_IMAGE}:latest" -c true',
            False,
            id="image-token-is-not-exact",
        ),
        pytest.param(
            'docker run "${RUNTIME_IMAGE}" --entrypoint sh -c true',
            False,
            id="entrypoint-after-image",
        ),
        pytest.param(
            'docker run --entrypoint sh "${RUNTIME_IMAGE}" true',
            False,
            id="missing-command-flag",
        ),
        pytest.param(
            'docker run --entrypoint sh "${RUNTIME_IMAGE}" -l true',
            False,
            id="login-shell-short-flag",
        ),
        pytest.param(
            'docker run --entrypoint sh "${RUNTIME_IMAGE}" -lc true',
            False,
            id="login-shell-combined-flag",
        ),
    ],
)
def test_mooncake_shell_run_parser_requires_executable_exact_tokens(
    source: str,
    expected: bool,
) -> None:
    assert (_mooncake_shell_run_tokens(source) is not None) is expected


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
    assert "builder_catalog_artifact" in outputs
    assert outputs["builder_catalog_artifact"]["value"] == (
        "${{ jobs.prepare.outputs.builder_catalog_artifact }}"
    )
    prepare = jobs["prepare"]
    prepare_outputs = set(prepare.get("outputs", {}))
    assert "builder_catalog_artifact" in prepare_outputs
    assert "builder_source_artifact" in prepare_outputs
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
    source_uploads = _artifact_steps(prepare, "upload")
    required_source_uploads = {
        _normalized_with(
            {
                "name": "${{ steps.source.outputs.builder_catalog_artifact }}",
                "path": "out/legacy-builder-catalog.json",
                "if-no-files-found": "error",
                "overwrite": False,
                "retention-days": 7,
            }
        ),
        _normalized_with(
            {
                "name": "${{ steps.source.outputs.builder_source_artifact }}",
                "path": "out/builder-catalog.json",
                "if-no-files-found": "error",
                "overwrite": False,
                "retention-days": 7,
            }
        ),
    }
    assert required_source_uploads <= {
        _normalized_with(step.get("with", {})) for step in source_uploads
    }
    assert (
        outputs["capability_catalog_artifact"]["value"]
        == "${{ jobs.assemble-capability-catalog.outputs.capability_catalog_artifact }}"
    )
    assembly_outputs = jobs["assemble-capability-catalog"].get("outputs", {})
    assert "capability_catalog_artifact" in assembly_outputs
    assert assembly_outputs["capability_catalog_artifact"] == (
        "${{ steps.catalog.outputs.capability_catalog_artifact }}"
    )


def test_live_discovery_jobs_use_typed_cli_without_inline_reconstruction() -> None:
    workflow = _load("sync-builders.yml")
    prepare = workflow["jobs"]["prepare"]
    source_steps = [step for step in prepare["steps"] if step.get("id") == "source"]
    assert len(source_steps) == 1
    source_run = _noncomment_shell(str(source_steps[0]["run"]))
    source_command = _cli_command(source_run, "builders", "discover-sources")
    _assert_cli_options(
        source_command,
        {
            "--legacy-output": "out/legacy-builder-catalog.json",
            "--output": "out/builder-catalog.json",
        },
    )

    runtime_job = workflow["jobs"]["discover-runtimes"]
    discover_steps = [
        step for step in runtime_job["steps"] if step.get("id") == "discover"
    ]
    assert len(discover_steps) == 1
    runtime_run = _noncomment_shell(str(discover_steps[0]["run"]))
    runtime_command = _cli_command(runtime_run, "catalog", "discover-runtimes")
    _assert_cli_options(
        runtime_command,
        {
            "--builder-catalog": "input/builder-source/builder-catalog.json",
            "--output": "out/runtime-discovery.json",
        },
    )
    assert "runtime_probe_matrix=" in runtime_run
    for forbidden in (
        "python - <<",
        "python3 - <<",
        "ascend_variants[0]",
        "vllm-project/vllm",
        "vllm-project/vllm-ascend",
        "Dockerfile.runtime.a2",
        "Dockerfile.runtime.a4",
    ):
        assert forbidden not in source_run
        assert forbidden not in runtime_run


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
    assert isinstance(probe.get("id"), str) and probe["id"]
    assert probe.get("env", {}).get("BUILDER_IMAGE") == "${{ matrix.builder_image }}"
    assert probe.get("env", {}).get("BUILDER_FACT_ID") == (
        "${{ matrix.builder_fact_id }}"
    )
    assert probe.get("env", {}).get("TARGET_BUILDER_DIGEST") == (
        "${{ matrix.target_builder_digest }}"
    )
    assert probe.get("env", {}).get("MANYLINUX") == "${{ matrix.manylinux }}"
    run = str(probe["run"])
    _assert_digest_guard_precedes(
        run,
        "BUILDER_IMAGE",
        r'(?m)^\s*docker\s+run\b[\s\S]*?"\$\{BUILDER_IMAGE\}"',
    )
    risky_source = _noncomment_shell(run)
    assert "packaging" not in risky_source
    assert re.search(r"\bjq\b", risky_source) is None
    assert re.search(
        r'["\']?\$(?:\{interpreter\}|interpreter)["\']?\s+'
        r"(?:-c|(?<!\S)-|\S+\.py)(?=\s)",
        risky_source,
    )
    for stdlib in ("import json", "import platform", "import sysconfig"):
        assert stdlib in risky_source
    assert "json.dumps" in risky_source or "json.dump" in risky_source
    for field in (
        "interpreter_path",
        "python_version",
        "python_abi",
        "soabi",
        "wheel_tag",
        "platform_tag",
    ):
        assert field in risky_source
    assert "/opt/python/cp*-cp*/bin/python" in risky_source
    assert "MANYLINUX" in risky_source
    assert "x86_64" in risky_source
    assert "aarch64" in risky_source
    assert "sysconfig.get_platform" in risky_source
    assert re.search(
        r"sysconfig\.get_config_var\(\s*[\"']SOABI[\"']\s*\)",
        risky_source,
    )
    abi_assignment = re.search(
        r"(?m)^\s*python_abi\s*=\s*(?P<value>[^\n]+)$",
        risky_source,
    )
    assert abi_assignment is not None
    assert any(
        authority in abi_assignment.group("value")
        for authority in ("path_abi", "soabi")
    )
    assert "sys.version_info" not in abi_assignment.group("value")
    assert 'f"{python_abi}-{python_abi}-' not in risky_source
    assert re.search(
        r"(?mi)^\s*platform_tag\s*=.*\bmanylinux\b",
        risky_source,
    )
    assert re.search(r"(?m)^\s*wheel_tag\s*=.*\bplatform_tag\b", risky_source)
    assert (
        re.search(
            r"(?m)^\s*(?:platform_tag|wheel_tag)\s*=.*sysconfig\.get_platform",
            risky_source,
        )
        is None
    )
    probe_source = yaml.safe_dump(probe, sort_keys=False)
    assert "builder_revision_id" not in probe_source
    assert "builder_source_image_digest" not in probe_source
    assert "cp312" not in run
    seal_steps = [
        step
        for step in job["steps"]
        if "out/python-probe/result.json" in str(step.get("run", ""))
    ]
    assert len(seal_steps) == 1
    seal = seal_steps[0]
    assert seal is not probe
    assert "always()" in str(seal.get("if", ""))
    assert seal.get("env", {}).get("PROBE_OUTCOME") == (
        f"${{{{ steps.{probe['id']}.outcome }}}}"
    )
    assert seal.get("env", {}).get("MANYLINUX") == "${{ matrix.manylinux }}"
    seal_run = _noncomment_shell(str(seal["run"]))
    assert "soabi" in seal_run
    producer = _structured_json_producer(seal_run, "out/python-probe/result.json")
    assert producer is not None
    for field in (
        "status",
        "probes",
        "failures",
        "python-probe-failed",
        "builder_fact_id",
        "builder_image",
        "target_builder_digest",
        "cpu_architecture",
        "manylinux",
        "runner",
        "evidence",
        "success",
        "failed",
    ):
        assert field in producer
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
    assert "matrix.builder_fact_id" not in uploads[0]["with"]["name"]


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
    assert "out/runtime-discovery.json" in run
    assert "runtime_probe_matrix=" in run
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
        if step.get("id") == "probe" and "docker run" in str(step.get("run", ""))
    ]
    assert len(probe_steps) == 1
    probe = probe_steps[0]
    assert isinstance(probe.get("id"), str) and probe["id"]
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
    assert probe.get("env", {}).get("MOONCAKE_VERSION") == (
        "${{ matrix.mooncake_version }}"
    )
    run = str(probe["run"])
    source = _noncomment_shell(run)
    assert all(
        not (
            command
            and command[0] == "sed"
            and any("MOONCAKE_TAG" in token for token in command)
        )
        for command in _shell_command_argvs(source)
    )
    _assert_digest_guard_precedes(
        run,
        "RUNTIME_IMAGE",
        rf"(?m)^\s*docker\s+run\b[\s\S]*?{_shell_variable('RUNTIME_IMAGE')}",
    )
    command = _mooncake_shell_run_tokens(run)
    assert command is not None
    payload = _mooncake_probe_payload(command)
    assert "/usr/local/include/mooncake" not in _noncomment_shell(payload)
    assert _has_mooncake_header_and_library_checks(payload)
    assert _has_exact_mooncake_checkout_tag_readback(payload)
    assert _has_mooncake_tag_canonicalization(payload)
    seal_steps = [
        step
        for step in job["steps"]
        if "out/mooncake-probe/result.json" in str(step.get("run", ""))
    ]
    assert len(seal_steps) == 1
    seal = seal_steps[0]
    assert seal is not probe
    assert "always()" in str(seal.get("if", ""))
    assert seal.get("env", {}).get("PROBE_OUTCOME") == (
        f"${{{{ steps.{probe['id']}.outcome }}}}"
    )
    assert seal.get("env", {}).get("MOONCAKE_VERSION") == (
        "${{ matrix.mooncake_version }}"
    )
    seal_run = _noncomment_shell(str(seal["run"]))
    assert re.search(
        r'(?m)^\s*declared_version="\$\{MOONCAKE_VERSION\}"\s*$',
        seal_run,
    )
    mismatch_body = _mismatch_result_branch(seal_run)
    assert mismatch_body is not None
    assert "mooncake-version-mismatch" in mismatch_body
    producer = _structured_json_producer(seal_run, "out/mooncake-probe/result.json")
    assert producer is not None
    for field in (
        "status",
        "probes",
        "failures",
        "mooncake-probe-failed",
        "mooncake-version-mismatch",
        "runtime_id",
        "runtime_image",
        "runtime_image_digest",
        "runtime_dockerfile",
        "cpu_architecture",
        "runner",
        "evidence",
        "success",
        "failed",
    ):
        assert field in producer
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
    assert "matrix.runtime_id" not in uploads[0]["with"]["name"]


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
                "name": "${{ needs.prepare.outputs.builder_source_artifact }}",
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
                "merge-multiple": False,
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
            "--runtime-discovery": ("input/runtime-discovery/runtime-discovery.json"),
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
    assert re.search(r"(?m)^\s*docker\s+buildx\s+imagetools\s+inspect\b", result_run)
    assert result.get("env", {}).get("BUILDER_PLAN_ID") == (
        "${{ matrix.builder_plan_id }}"
    )
    assert result.get("env", {}).get("BUILD_OUTCOME") == ("${{ steps.build.outcome }}")
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
    assert "matrix.builder_plan_id" not in uploads[0]["with"]["name"]


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
                "merge-multiple": False,
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
            "merge-multiple": False,
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
            "merge-multiple": False,
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
            "--runtime-discovery": ("input/runtime-discovery/runtime-discovery.json"),
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


def test_empty_probe_matrices_still_reach_downstream_consumers() -> None:
    workflow = _load("sync-builders.yml")
    jobs = workflow["jobs"]
    discover = jobs["discover-runtimes"]
    plan = jobs["plan-builder-sync"]
    collect = jobs["collect-builder-revisions"]
    mooncake = jobs["probe-mooncake"]
    build = jobs["build-missing"]
    python = jobs["probe-python"]
    assembly = jobs["assemble-capability-catalog"]

    assert discover["outputs"]["has_mooncake_probes"] == (
        "${{ steps.discover.outputs.has_mooncake_probes }}"
    )
    assert plan["outputs"]["has_builder_plans"] == (
        "${{ steps.plan.outputs.has_builder_plans }}"
    )
    assert collect["outputs"]["has_python_probes"] == (
        "${{ steps.collect.outputs.has_python_probes }}"
    )
    for job, producer_id, output in (
        (discover, "discover", "has_mooncake_probes="),
        (plan, "plan", "has_builder_plans="),
        (collect, "collect", "has_python_probes="),
    ):
        producers = [step for step in job["steps"] if step.get("id") == producer_id]
        assert len(producers) == 1
        producer_source = _noncomment_shell(str(producers[0].get("run", "")))
        assert re.search(
            rf"(?m)^\s*(?:echo|printf)\b[^\n]*{re.escape(output)}",
            producer_source,
        )
        assert re.search(_shell_variable("GITHUB_OUTPUT"), producer_source)

    assert "needs.discover-runtimes.outputs.has_mooncake_probes == 'true'" in str(
        mooncake.get("if", "")
    )
    assert "needs.plan-builder-sync.outputs.has_builder_plans == 'true'" in str(
        build.get("if", "")
    )
    assert "needs.collect-builder-revisions.outputs.has_python_probes == 'true'" in str(
        python.get("if", "")
    )
    assert "needs.probe-mooncake.result" not in str(plan.get("if", ""))
    assert "needs.build-missing.result" not in str(collect.get("if", ""))
    assert assembly.get("if") == "${{ always() }}"

    conditional_downloads = (
        (
            plan,
            "input/mooncake-probes",
            "needs.discover-runtimes.outputs.has_mooncake_probes == 'true'",
        ),
        (
            collect,
            "input/builder-results",
            "needs.plan-builder-sync.outputs.has_builder_plans == 'true'",
        ),
        (
            assembly,
            "input/python-probes",
            "needs.collect-builder-revisions.outputs.has_python_probes == 'true'",
        ),
        (
            assembly,
            "input/mooncake-probes",
            "needs.discover-runtimes.outputs.has_mooncake_probes == 'true'",
        ),
    )
    for consumer, path, condition in conditional_downloads:
        downloads = [
            step
            for step in _artifact_steps(consumer, "download")
            if step.get("with", {}).get("path") == path
        ]
        assert len(downloads) == 1
        assert condition in str(downloads[0].get("if", ""))
        assert downloads[0]["with"]["merge-multiple"] is False
        run_source = _noncomment_shell(
            "\n".join(str(step.get("run", "")) for step in consumer["steps"])
        )
        assert re.search(rf"(?m)^\s*mkdir\s+-p\s+[^\n]*{re.escape(path)}", run_source)


@pytest.mark.parametrize("filename", ["release-ucm.yml", "ucm-build-bot.yml"])
def test_task3_planners_keep_legacy_builder_catalog_until_task4(
    filename: str,
) -> None:
    workflow = _load(filename)
    plan = workflow["jobs"]["plan"]
    downloads = [
        step
        for step in _artifact_steps(plan, "download")
        if step.get("with", {}).get("path") == "input/builders"
    ]
    assert len(downloads) == 1
    assert downloads[0]["with"] == {
        "name": "${{ needs.sync-builders.outputs.builder_catalog_artifact }}",
        "path": "input/builders",
    }
    plan_steps = [
        step
        for step in plan["steps"]
        if "ucm_release compact plan" in str(step.get("run", ""))
    ]
    assert len(plan_steps) == 1
    run = str(plan_steps[0]["run"])
    assert "--builder-catalog input/builders/builder-catalog.json" in run
    assert "--capability-catalog" not in run


def test_task3_has_no_lossy_capability_to_legacy_projection() -> None:
    cli_path = ROOT / ".github" / "release" / "ucm_release" / "cli.py"
    tree = ast.parse(cli_path.read_text(encoding="utf-8"))
    assert all(
        not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        or node.name != "_builder_catalog_projection"
        for node in tree.body
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        if not (
            isinstance(node.value, ast.Subscript)
            and isinstance(node.value.slice, ast.Constant)
            and node.value.slice.value == "builder_revision_ids"
        ):
            continue
        assert not (
            isinstance(node.slice, ast.UnaryOp)
            and isinstance(node.slice.op, ast.USub)
            and isinstance(node.slice.operand, ast.Constant)
            and node.slice.operand.value == 1
        )


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
    assert (
        re.search(
            rf"^COPY\s+--from={re.escape(stage)}\s+/usr/local/lib/?\s+"
            r"/usr/local/lib/?$",
            instructions,
            re.MULTILINE,
        )
        is None
    )
    explicit_libraries = re.search(
        rf"^COPY\s+--from={re.escape(stage)}\s+[^\n]*"
        r"(?:lib[^/\s]*mooncake|mooncake[^/\s]*\.so)[^\n]*\s+"
        r"/usr/local/lib/?$",
        instructions,
        re.MULTILINE | re.IGNORECASE,
    )
    packed_libraries = re.search(
        rf"^COPY\s+--from={re.escape(stage)}\s+[^\n]*mooncake-libs/?\s+"
        r"/usr/local/lib/?$",
        instructions,
        re.MULTILINE | re.IGNORECASE,
    )
    assert explicit_libraries or packed_libraries


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


@pytest.mark.parametrize(
    ("job_id", "risky_id", "outcome_env", "result_path"),
    [
        pytest.param(
            "probe-mooncake",
            "probe",
            "PROBE_OUTCOME",
            "out/mooncake-probe/result.json",
            id="mooncake-local-quarantine",
        ),
        pytest.param(
            "build-missing",
            "build",
            "BUILD_OUTCOME",
            "out/builder-result/result.json",
            id="builder-local-quarantine",
        ),
        pytest.param(
            "probe-python",
            "probe",
            "PROBE_OUTCOME",
            "out/python-probe/result.json",
            id="python-local-quarantine",
        ),
    ],
)
def test_risky_matrix_steps_quarantine_failures_after_sealing_results(
    job_id: str,
    risky_id: str,
    outcome_env: str,
    result_path: str,
) -> None:
    job = _load("sync-builders.yml")["jobs"][job_id]
    risky_steps = [step for step in job["steps"] if step.get("id") == risky_id]
    assert len(risky_steps) == 1
    assert risky_steps[0].get("continue-on-error") is True

    expected_outcome = f"${{{{ steps.{risky_id}.outcome }}}}"
    seals = [
        step
        for step in job["steps"]
        if step.get("env", {}).get(outcome_env) == expected_outcome
    ]
    assert len(seals) == 1
    seal = seals[0]
    assert seal.get("if") == "${{ always() }}"
    producer = _structured_json_producer(
        _noncomment_shell(str(seal.get("run", ""))),
        result_path,
    )
    assert producer is not None

    uploads = _artifact_steps(job, "upload")
    assert len(uploads) == 1
    assert uploads[0].get("if") == "${{ always() }}"
    assert uploads[0].get("with", {}).get("path") == result_path


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
