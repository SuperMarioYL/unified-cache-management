from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
HEAD_SHA = "a" * 40
CURRENT_SHA = "b" * 40
BUILD_BODY = f"/release build {HEAD_SHA}"


def _run(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ | {"PYTHONPATH": str(V2_ROOT)}
    if env:
        command_env.update(env)
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=V2_ROOT,
        env=command_env,
        text=True,
        capture_output=True,
        check=False,
    )


def _parse(
    body: str,
    *,
    association: str = "MEMBER",
    observed: str | None = None,
    current: str | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [
        "command",
        "parse",
        "--body",
        body,
        "--actor",
        "octocat",
        "--author-association",
        association,
    ]
    if observed is not None:
        args.extend(["--observed-source-sha", observed])
    if current is not None:
        args.extend(["--current-source-sha", current])
    return _run(*args)


def _document(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert result.stdout.strip() == json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    digest = document["sha256"]
    unsigned = dict(document)
    unsigned.pop("sha256")
    assert (
        digest
        == hashlib.sha256(
            json.dumps(
                unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    assert document["kind"] == "release-command"
    assert document["schema_version"] == 2
    assert document["mode"] == "dry-run"
    assert document["actions_write"] is False
    assert document["operations"]
    assert all(operation["executed"] is False for operation in document["operations"])
    return document


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_build_preview_is_accepted_only_for_trusted_associations(
    association: str,
) -> None:
    """Catches removal of the trusted-association gate from a build request."""
    document = _document(
        _parse(
            BUILD_BODY,
            association=association,
            observed=HEAD_SHA,
            current=HEAD_SHA,
        )
    )

    assert document["command"] == "build"
    assert document["authorized"] is True
    assert document["accepted"] is True
    assert document["reason"] == "authorized-build-preview"
    assert document["requested_source_sha"] == HEAD_SHA
    assert document["operations"] == [{"action": "build-preview", "executed": False}]


@pytest.mark.parametrize(
    "body",
    [f" {BUILD_BODY} ", f"\t{BUILD_BODY}\t", f" \t{BUILD_BODY}\t "],
)
def test_only_ascii_space_and_tab_may_wrap_a_command(body: str) -> None:
    """Catches removal of the narrowly allowed ASCII command padding."""
    document = _document(_parse(body, observed=HEAD_SHA, current=HEAD_SHA))

    assert document["command"] == "build"
    assert document["accepted"] is True


@pytest.mark.parametrize(
    "association", ["CONTRIBUTOR", "FIRST_TIMER", "NONE", "member"]
)
def test_untrusted_build_is_a_no_write_denial_preview(association: str) -> None:
    """Catches an external commenter being authorized to request a build preview."""
    document = _document(
        _parse(
            BUILD_BODY,
            association=association,
            observed=HEAD_SHA,
            current=HEAD_SHA,
        )
    )

    assert document["command"] == "build"
    assert document["authorized"] is False
    assert document["accepted"] is False
    assert document["reason"] == "unauthorized-build"


@pytest.mark.parametrize(
    ("body", "command", "accepted", "reason", "action"),
    [
        ("/release status", "status", True, "read-only-status", "inspect-preview"),
        (
            "/release cancel",
            "cancel",
            False,
            "dry-run-no-actions-write",
            "cancel-preview",
        ),
    ],
)
def test_status_and_cancel_remain_read_only_for_untrusted_callers(
    body: str, command: str, accepted: bool, reason: str, action: str
) -> None:
    """Catches status or cancel gaining an Actions mutation path."""
    document = _document(_parse(body, association="NONE"))

    assert document["command"] == command
    assert document["authorized"] is True
    assert document["accepted"] is accepted
    assert document["reason"] == reason
    assert document["operations"] == [{"action": action, "executed": False}]


@pytest.mark.parametrize(
    "body",
    [
        "/release deploy",
        "/release build now",
        "/release build",
        f"/release build {'A' * 40}",
        f"/release build {'a' * 39}",
        f"/release build {HEAD_SHA} trailing",
        "/release build\n",
        "/release build\r",
        "/release build\r\n",
        "/release build\n/release status",
        "/release BUILD",
        "／release build",
        "/releas\N{CYRILLIC SMALL LETTER IE} build",
        "\N{EM SPACE}/release build\N{EM SPACE}",
        "\N{NO-BREAK SPACE}/release build\N{NO-BREAK SPACE}",
        "\N{IDEOGRAPHIC SPACE}/release build\N{IDEOGRAPHIC SPACE}",
        f"please {BUILD_BODY}",
        "",
    ],
)
def test_only_one_exact_ascii_release_command_is_supported(body: str) -> None:
    """Catches arguments, multiple commands, lookalikes, or prose being executed as commands."""
    result = _parse(body)
    document = _document(result)

    assert "Traceback" not in result.stderr
    assert document["command"] == "unsupported"
    assert document["authorized"] is False
    assert document["accepted"] is False
    assert document["reason"] == "unsupported-command"
    assert document["operations"] == [{"action": "none", "executed": False}]


def test_nul_body_from_file_is_an_unsupported_command_without_traceback(
    tmp_path: Path,
) -> None:
    """Catches NUL being accepted while exercising the real file-backed CLI boundary."""
    body_file = tmp_path / "comment.txt"
    body_file.write_text(BUILD_BODY + "\x00", encoding="utf-8")
    result = _run(
        "command",
        "parse",
        "--body-file",
        str(body_file),
        "--actor",
        "octocat",
        "--author-association",
        "MEMBER",
    )
    document = _document(result)

    assert "Traceback" not in result.stderr
    assert document["command"] == "unsupported"
    assert document["accepted"] is False


def test_stale_pr_head_sha_denies_an_otherwise_authorized_build() -> None:
    """Catches a command observed for one PR head being applied to a newer head."""
    document = _document(_parse(BUILD_BODY, observed=HEAD_SHA, current=CURRENT_SHA))

    assert document["observed_source_sha"] == HEAD_SHA
    assert document["current_source_sha"] == CURRENT_SHA
    assert document["authorized"] is True
    assert document["accepted"] is False
    assert document["reason"] == "stale-pr-sha"


def test_requested_comment_sha_must_match_both_comment_time_reads() -> None:
    """Catches a PR moving from requested A to observed/current B before the first GET."""
    document = _document(_parse(BUILD_BODY, observed=CURRENT_SHA, current=CURRENT_SHA))

    assert document["requested_source_sha"] == HEAD_SHA
    assert document["observed_source_sha"] == CURRENT_SHA
    assert document["current_source_sha"] == CURRENT_SHA
    assert document["accepted"] is False
    assert document["reason"] == "requested-pr-sha-mismatch"


@pytest.mark.parametrize("sha", ["abc", "A" * 40, "g" * 40, "a" * 39, "a" * 41])
def test_source_identity_requires_paired_exact_lowercase_shas(sha: str) -> None:
    """Catches ambiguous or non-immutable PR source identities."""
    malformed = _parse(BUILD_BODY, observed=sha, current=HEAD_SHA)
    unpaired = _parse(BUILD_BODY, observed=HEAD_SHA)

    assert malformed.returncode == 2
    assert "40 lowercase hexadecimal" in malformed.stderr
    assert unpaired.returncode == 2
    assert "provided together" in unpaired.stderr


def test_body_file_and_environment_inputs_do_not_require_shell_interpolation(
    tmp_path: Path,
) -> None:
    """Catches workflows being forced to interpolate an untrusted comment into a shell command."""
    body_file = tmp_path / "comment.txt"
    body_file.write_text("/release status", encoding="utf-8")
    common = ["--actor", "octocat", "--author-association", "NONE"]

    from_file = _run("command", "parse", "--body-file", str(body_file), *common)
    from_env = _run(
        "command",
        "parse",
        "--body-env",
        "RELEASE_COMMENT_BODY",
        *common,
        env={"RELEASE_COMMENT_BODY": "/release cancel"},
    )

    assert _document(from_file)["command"] == "status"
    assert _document(from_env)["command"] == "cancel"


def test_exactly_one_body_source_is_required(tmp_path: Path) -> None:
    """Catches duplicate comment sources silently overriding one another."""
    body_file = tmp_path / "comment.txt"
    body_file.write_text("/release status", encoding="utf-8")
    common = ["--actor", "octocat", "--author-association", "MEMBER"]

    duplicate = _run(
        "command",
        "parse",
        "--body",
        BUILD_BODY,
        "--body-file",
        str(body_file),
        *common,
    )
    missing = _run("command", "parse", *common)

    assert duplicate.returncode == 2
    assert missing.returncode == 2
