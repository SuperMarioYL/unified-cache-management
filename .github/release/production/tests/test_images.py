from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from ucm_release_production.build import project_build_task
from ucm_release_production.common import ProductionError, sha256_envelope
from ucm_release_production.config import load_config
from ucm_release_production.images import image_recipe, prepare_image_context
from ucm_release_production.tags import intent_document, parse_tag

from conftest import PRODUCTION_ROOT


CONFIG = PRODUCTION_ROOT / "production-release.json"
SOURCE = "1" * 40


def _source() -> dict[str, object]:
    return sha256_envelope(
        {
            "kind": "ucm-production-source-identity",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "stage": "rc",
            "tag_name": "v0.6.0rc1",
            "tag_object_sha": "2" * 40,
            "source_commit_sha": SOURCE,
            "source_branch": "0.6.0-release",
            "tagger": "Octo Cat <octo@example.invalid>",
            "tagged_at": "2026-08-13T00:00:00Z",
            "tag_message_sha256": "3" * 64,
            "control_default_branch": "develop",
            "control_sha": "4" * 40,
            "lineage": {
                "accepted": True,
                "stage": "draft",
                "version": "0.6.0",
                "tag_name": "draft/v0.6.0-1",
                "source_commit_sha": SOURCE,
                "evidence_sha256": "5" * 64,
            },
        }
    )


def test_image_recipe_is_complete_and_context_reopens_pinned_wheels(
    tmp_path: Path,
) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(config, intent, _source(), "cuda130-amd64")
    wheel = tmp_path / "uc_manager_cuda-0.6.0rc1-cp312-cp312-manylinux_2_28_x86_64.whl"
    wheel.write_bytes(b"sealed-production-wheel")
    wheel_record = sha256_envelope(
        {
            "kind": "ucm-production-wheel-record",
            "schema_version": 1,
            "spec_id": task["spec_id"],
            "distribution": task["distribution"],
            "version": task["wheel_version"],
            "filename": wheel.name,
            "file_sha256": "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "task_sha256": "sha256:" + task["sha256"],
            "source_sha": SOURCE,
        }
    )
    wrapt = config["toolchain"]["wrapt"]["amd64"]
    wrapt_path = tmp_path / wrapt["filename"]
    wrapt_path.write_bytes(b"pinned-wrapt-wheel")
    mutable = copy.deepcopy(config)
    mutable["toolchain"]["wrapt"]["amd64"]["sha256"] = hashlib.sha256(
        wrapt_path.read_bytes()
    ).hexdigest()
    dockerfile = tmp_path / "Dockerfile.image"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    recipe = image_recipe(task, intent.image_tag)
    result = prepare_image_context(
        mutable,
        task,
        intent_document(intent),
        wheel_record,
        wheel,
        dockerfile,
        tmp_path / "context",
    )

    assert recipe["base"] == task["runtime"]
    assert result["recipe"] == recipe
    assert (tmp_path / "context" / wheel.name).read_bytes() == wheel.read_bytes()
    assert "--hash=sha256:" in (tmp_path / "context" / "requirements.lock").read_text()


def test_image_context_rejects_wheel_record_drift(tmp_path: Path) -> None:
    config = load_config(CONFIG)
    intent = parse_tag("v0.6.0rc1", config)
    task = project_build_task(config, intent, _source(), "cuda130-amd64")
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"bytes")
    record = sha256_envelope(
        {
            "kind": "ucm-production-wheel-record",
            "schema_version": 1,
            "spec_id": task["spec_id"],
            "distribution": task["distribution"],
            "version": task["wheel_version"],
            "filename": wheel.name,
            "file_sha256": "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "task_sha256": "sha256:" + "9" * 64,
            "source_sha": SOURCE,
        }
    )

    with pytest.raises(ProductionError, match="differs"):
        prepare_image_context(
            config,
            task,
            intent_document(intent),
            record,
            wheel,
            tmp_path / "Dockerfile",
            tmp_path / "context",
        )
