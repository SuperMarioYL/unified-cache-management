from __future__ import annotations

from pathlib import Path

from ucm_release_production import chart

from conftest import REPO_ROOT


def test_package_chart_validates_with_a_complete_repository_values_file(
    monkeypatch: object, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> None:
        calls.append(argv)

    def repack(source: Path, destination: Path) -> None:
        destination.write_bytes(b"deterministic-chart")

    monkeypatch.setattr(chart, "_run", run)
    monkeypatch.setattr(chart, "_repack", repack)

    chart.package_chart(
        REPO_ROOT / "charts" / "ucm",
        tmp_path,
        chart_version="0.6.0-draft.1",
        app_version="0.6.0.dev1",
        source_sha="1" * 40,
    )

    values = str(
        REPO_ROOT / "charts" / "ucm" / "models" / "cuda" / "values-qwen3-0p6b-1e1.yaml"
    )
    assert calls[0] == [
        "helm",
        "lint",
        str(REPO_ROOT / "charts" / "ucm"),
        "--values",
        values,
    ]
    assert calls[1] == [
        "helm",
        "template",
        "ucm-production",
        str(REPO_ROOT / "charts" / "ucm"),
        "--values",
        values,
    ]
    assert calls[-1] == [
        "helm",
        "lint",
        str(tmp_path / "unified-cache-pd-0.6.0-draft.1.tgz"),
        "--values",
        values,
    ]
