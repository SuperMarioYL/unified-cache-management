from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "materialize_version.py"
SPEC = importlib.util.spec_from_file_location("materialize_version", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
materialize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(materialize)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.7.59rc5", "0.7.59rc5"),
        ("v0.7.59", "0.7.59"),
        ("draft/v0.6.0-13", "0.6.0.dev13"),
    ],
)
def test_version_from_tag(tag: str, expected: str) -> None:
    assert materialize.version_from_tag(tag) == expected


@pytest.mark.parametrize(
    "value",
    [
        "v0.7.59RC5",
        "v0.7",
        "v01.7.59",
        "draft/v0.6.0-0",
        "draft/v0.6.0-01",
        "draft/v0.6.0rc1-2",
        "0.7.59",
    ],
)
def test_invalid_or_noncanonical_tags_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        materialize.version_from_tag(value)


def test_cli_writes_one_line_and_prints_the_version(tmp_path: Path) -> None:
    output = tmp_path / "version.ini"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tag",
            "draft/v0.6.0-13",
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout == "0.6.0.dev13\n"
    assert output.read_text(encoding="utf-8") == "VLLM_UC_VERSION=0.6.0.dev13\n"


def test_version_option_requires_canonical_pep440(tmp_path: Path) -> None:
    output = tmp_path / "version.ini"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--version",
            "0.7.59RC5",
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "canonical PEP 440" in completed.stderr
    assert not output.exists()
