from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from conftest import PRODUCTION_ROOT
from ucm_release_production.cli import main
from ucm_release_production.common import ProductionError, load_json, verify_envelope
from ucm_release_production.config import load_config
from ucm_release_production.tags import (
    TagIntent,
    intent_document,
    parse_tag,
    verify_ref_snapshot,
)

CONFIG = PRODUCTION_ROOT / "production-release.json"
SOURCE = "1" * 40
CONTROL = "2" * 40
TAG_OBJECT = "3" * 40
EVIDENCE = "4" * 64
MESSAGE = "5" * 64


@pytest.mark.parametrize(
    ("tag", "stage", "wheel", "chart", "image", "branch", "draft", "rc"),
    [
        (
            "draft/v0.6.0-1",
            "draft",
            "0.6.0.dev1",
            "0.6.0-draft.1",
            "draft-v0.6.0-1",
            "0.6.0-release",
            1,
            None,
        ),
        (
            "v0.6.0rc1",
            "rc",
            "0.6.0rc1",
            "0.6.0-rc.1",
            "v0.6.0rc1",
            "0.6.0-release",
            None,
            1,
        ),
        (
            "v0.6.0",
            "stable",
            "0.6.0",
            "0.6.0",
            "v0.6.0",
            "0.6.0-release",
            None,
            None,
        ),
        (
            "v0.6.1",
            "hotfix",
            "0.6.1",
            "0.6.1",
            "v0.6.1",
            "hotfix/0.6.1",
            None,
            None,
        ),
    ],
)
def test_tag_projection(
    tag: str,
    stage: str,
    wheel: str,
    chart: str,
    image: str,
    branch: str,
    draft: int | None,
    rc: int | None,
) -> None:
    intent = parse_tag(tag, load_config(CONFIG))

    assert (
        intent.stage,
        intent.wheel_version,
        intent.chart_version,
        intent.image_tag,
        intent.release_branch,
        intent.draft_number,
        intent.rc_number,
    ) == (stage, wheel, chart, image, branch, draft, rc)
    document = intent_document(intent)
    assert (
        verify_envelope(
            document,
            kind="ucm-production-tag-intent",
            schema_version=1,
            exact_keys={
                "kind",
                "schema_version",
                "stage",
                "tag_name",
                "version",
                "wheel_version",
                "chart_version",
                "image_tag",
                "release_branch",
                "draft_number",
                "rc_number",
                "sha256",
            },
        )
        == document
    )


@pytest.mark.parametrize(
    "tag",
    [
        "draft/v0.6.0-0",
        "draft/v0.6.0-01",
        "draft/v00.6.0-1",
        "draft/v0.6.0-１",
        "draft/v0.6.0-1 ",
        "Draft/v0.6.0-1",
        "v0.6.0rc0",
        "v0.6.0rc01",
        "v0.6.0RC1",
        "v0.6.0+cuda",
        "v0.6.0-rc.1",
        "v0.5.9",
        "v0.7.0",
        "v0.6.0\nnext",
        "latest",
        "refs/tags/v0.6.0",
    ],
)
def test_tag_parser_rejects_noncanonical_or_wrong_line_tags(tag: str) -> None:
    with pytest.raises(ProductionError, match="tag"):
        parse_tag(tag, load_config(CONFIG))


def _lineage(intent: TagIntent) -> dict[str, object] | None:
    if intent.stage == "draft":
        return None
    if intent.stage == "rc":
        return {
            "accepted": True,
            "stage": "draft",
            "version": intent.version,
            "tag_name": "draft/v0.6.0-1",
            "source_commit_sha": SOURCE,
            "evidence_sha256": EVIDENCE,
        }
    if intent.stage == "stable":
        return {
            "accepted": True,
            "stage": "rc",
            "version": intent.version,
            "tag_name": "v0.6.0rc1",
            "source_commit_sha": SOURCE,
            "evidence_sha256": EVIDENCE,
        }
    return {
        "accepted": True,
        "stage": "stable",
        "version": "0.6.0",
        "tag_name": "v0.6.0",
        "source_commit_sha": "6" * 40,
        "evidence_sha256": EVIDENCE,
        "ancestry_verified": True,
    }


def _snapshot(intent: TagIntent) -> dict[str, object]:
    ref_read = {"object_type": "tag", "object_sha": TAG_OBJECT}
    object_read = {
        "tag_object_sha": TAG_OBJECT,
        "target_type": "commit",
        "peeled_commit_sha": SOURCE,
        "tagger": "release-operator",
        "tagged_at": "2026-08-13T08:00:00Z",
        "message_sha256": MESSAGE,
    }
    return {
        "kind": "ucm-production-ref-snapshot",
        "schema_version": 1,
        "repository": "OctoCat/unified-cache-management",
        "repository_id": 42,
        "tag": {
            "name": intent.tag_name,
            "ref_reads": [copy.deepcopy(ref_read), copy.deepcopy(ref_read)],
            "object_reads": [copy.deepcopy(object_read), copy.deepcopy(object_read)],
        },
        "source_branch": {
            "name": intent.release_branch,
            "head_reads": [SOURCE, SOURCE],
        },
        "control": {
            "default_branch": "develop",
            "head_reads": [CONTROL, CONTROL],
        },
        "lineage": _lineage(intent),
    }


@pytest.mark.parametrize("tag", ["draft/v0.6.0-1", "v0.6.0rc1", "v0.6.0", "v0.6.1"])
def test_annotated_tag_snapshot_closes_source_and_lineage(tag: str) -> None:
    intent = parse_tag(tag, load_config(CONFIG))

    result = verify_ref_snapshot(intent, _snapshot(intent))

    assert result["kind"] == "ucm-production-source-identity"
    assert result["stage"] == intent.stage
    assert result["tag_object_sha"] == TAG_OBJECT
    assert result["source_commit_sha"] == SOURCE
    assert result["control_sha"] == CONTROL
    assert result["lineage"] == _lineage(intent)
    verify_envelope(result, kind="ucm-production-source-identity", schema_version=1)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: [
                item.update(object_type="commit") for item in value["tag"]["ref_reads"]
            ],
            "annotated",
        ),
        (
            lambda value: value["tag"]["ref_reads"][1].update(object_sha="7" * 40),
            "double-read",
        ),
        (
            lambda value: value["tag"]["object_reads"][1].update(
                peeled_commit_sha="7" * 40
            ),
            "double-read",
        ),
        (
            lambda value: value["source_branch"]["head_reads"].__setitem__(1, "7" * 40),
            "double-read",
        ),
        (
            lambda value: value["control"]["head_reads"].__setitem__(1, "7" * 40),
            "double-read",
        ),
        (lambda value: value["source_branch"].update(name="develop"), "source branch"),
        (
            lambda value: value["source_branch"]["head_reads"].__setitem__(0, "7" * 40),
            "double-read",
        ),
        (lambda value: value["tag"].update(name="v0.6.0"), "tag name"),
    ],
)
def test_ref_snapshot_rejects_lightweight_drift_and_wrong_branch(
    mutation: object, message: str
) -> None:
    intent = parse_tag("draft/v0.6.0-1", load_config(CONFIG))
    snapshot = _snapshot(intent)
    mutation(snapshot)

    with pytest.raises(ProductionError, match=message):
        verify_ref_snapshot(intent, snapshot)


@pytest.mark.parametrize(
    ("tag", "mutation", "message"),
    [
        ("v0.6.0rc1", lambda value: value.update(lineage=None), "Draft"),
        (
            "v0.6.0rc1",
            lambda value: value["lineage"].update(accepted=False),
            "accepted",
        ),
        (
            "v0.6.0rc1",
            lambda value: value["lineage"].update(source_commit_sha="7" * 40),
            "same source",
        ),
        ("v0.6.0", lambda value: value["lineage"].update(stage="draft"), "RC"),
        (
            "v0.6.0",
            lambda value: value["lineage"].update(source_commit_sha="7" * 40),
            "same source",
        ),
        (
            "v0.6.1",
            lambda value: value["lineage"].update(ancestry_verified=False),
            "ancestry",
        ),
        (
            "v0.6.1",
            lambda value: value["lineage"].update(source_commit_sha=SOURCE),
            "different source",
        ),
        (
            "v0.6.1",
            lambda value: value["lineage"].update(version="0.5.9"),
            "previous Stable",
        ),
    ],
)
def test_stage_lineage_fails_closed(tag: str, mutation: object, message: str) -> None:
    intent = parse_tag(tag, load_config(CONFIG))
    snapshot = _snapshot(intent)
    mutation(snapshot)

    with pytest.raises(ProductionError, match=message):
        verify_ref_snapshot(intent, snapshot)


def test_tag_cli_is_file_oriented_and_reopens_intent(tmp_path: Path) -> None:
    intent_path = tmp_path / "intent.json"
    snapshot_path = tmp_path / "snapshot.json"
    identity_path = tmp_path / "identity.json"

    assert (
        main(
            [
                "tag",
                "parse",
                "--config",
                str(CONFIG),
                "--tag",
                "draft/v0.6.0-1",
                "--output",
                str(intent_path),
            ]
        )
        == 0
    )
    verify_envelope(
        load_json(intent_path, "tag intent"),
        kind="ucm-production-tag-intent",
        schema_version=1,
    )
    snapshot_path.write_text(
        json.dumps(_snapshot(parse_tag("draft/v0.6.0-1", load_config(CONFIG)))),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "tag",
                "verify-refs",
                "--config",
                str(CONFIG),
                "--intent",
                str(intent_path),
                "--snapshot",
                str(snapshot_path),
                "--output",
                str(identity_path),
            ]
        )
        == 0
    )
    assert load_json(identity_path, "source identity")["sha256"]


def test_tag_cli_rejects_invalid_tag_without_creating_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "should-not-exist.json"

    assert (
        main(
            [
                "tag",
                "parse",
                "--config",
                str(CONFIG),
                "--tag",
                "v0.6.0+mutable",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert not output.exists()
