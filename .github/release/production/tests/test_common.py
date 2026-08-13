from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ucm_release_production.common import (
    ProductionError,
    canonical_bytes,
    load_json,
    sha256_envelope,
    verify_envelope,
)


def test_canonical_bytes_are_utf8_sorted_and_compact() -> None:
    value = {"z": "UCM", "a": [3, 2, 1], "unicode": "发布"}

    assert canonical_bytes(value) == (
        b'{"a":[3,2,1],"unicode":"\xe5\x8f\x91\xe5\xb8\x83","z":"UCM"}'
    )


def test_load_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"kind":"first","kind":"second"}\n', encoding="utf-8")

    with pytest.raises(ProductionError, match="duplicate key: kind"):
        load_json(path, "test document")


@pytest.mark.parametrize("raw", [b'{"value":NaN}\n', b'{"value":Infinity}\n'])
def test_load_json_rejects_nonfinite_numbers(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "nonfinite.json"
    path.write_bytes(raw)

    with pytest.raises(ProductionError, match="non-finite"):
        load_json(path, "test document")


def test_sha256_envelope_is_self_independent_and_reopenable() -> None:
    document = sha256_envelope({"kind": "example", "schema_version": 1})
    expected = hashlib.sha256(
        canonical_bytes({"kind": "example", "schema_version": 1})
    ).hexdigest()

    assert document["sha256"] == expected
    assert verify_envelope(document, kind="example", schema_version=1) == document


def test_verify_envelope_rejects_resigned_unknown_keys() -> None:
    document = sha256_envelope(
        {"kind": "example", "schema_version": 1, "unexpected": True}
    )

    with pytest.raises(ProductionError, match="exactly"):
        verify_envelope(
            document,
            kind="example",
            schema_version=1,
            exact_keys={"kind", "schema_version", "sha256"},
        )
