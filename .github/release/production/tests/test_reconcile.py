from __future__ import annotations

import copy

import pytest

from ucm_release_production.common import ProductionError, sha256_envelope
from ucm_release_production.config import load_config
from ucm_release_production.reconcile import build_inventory, plan_publication
from ucm_release_production.tags import parse_tag

from conftest import PRODUCTION_ROOT

CONFIG = PRODUCTION_ROOT / "production-release.json"
SOURCE = "1" * 40


def _candidate(tag: str = "v0.6.0rc1") -> dict[str, object]:
    config = load_config(CONFIG)
    intent = parse_tag(tag, config)
    wheels = []
    members = []
    indexes = []
    distributions = {
        "cuda130": "uc-manager-cuda",
        "cann900-a2": "uc-manager-cann-a2",
        "cann900-a3": "uc-manager-cann-a3",
    }
    for profile in distributions:
        index_members = []
        for arch in ("amd64", "arm64"):
            spec_id = f"{profile}-{arch}"
            wheel_digest = (
                "sha256:" + (hash(spec_id) % (16**64)).to_bytes(32, "big").hex()
            )
            manifest = "sha256:" + hashlib_sha(spec_id)
            wheels.append(
                {
                    "spec_id": spec_id,
                    "distribution": distributions[profile],
                    "version": intent.wheel_version,
                    "path": f"wheels/{spec_id}/{distributions[profile]}-{intent.wheel_version}-{arch}.whl",
                    "file_sha256": wheel_digest,
                    "record_path": f"wheels/{spec_id}/record.json",
                    "record_sha256": "sha256:" + hashlib_sha("record:" + spec_id),
                    "task_sha256": "sha256:" + hashlib_sha("task:" + spec_id),
                }
            )
            members.append(
                {
                    "spec_id": spec_id,
                    "profile_id": profile,
                    "platform": f"linux/{arch}",
                    "manifest_digest": manifest,
                    "config_digest": "sha256:" + hashlib_sha("config:" + spec_id),
                    "layers": [],
                    "recipe_sha256": "sha256:" + hashlib_sha("recipe:" + spec_id),
                    "path": f"images/{spec_id}/closure.json",
                    "record_sha256": "sha256:" + hashlib_sha("closure:" + spec_id),
                }
            )
            index_members.append(
                {
                    "spec_id": spec_id,
                    "platform": f"linux/{arch}",
                    "manifest_digest": manifest,
                }
            )
        indexes.append(
            {
                "profile_id": profile,
                "image_tag": intent.image_tag,
                "members": index_members,
                "path": f"indexes/{profile}/index.json",
                "record_sha256": "sha256:" + hashlib_sha("index:" + profile),
            }
        )
    return sha256_envelope(
        {
            "kind": "ucm-production-candidate-envelope",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "stage": intent.stage,
            "tag_name": intent.tag_name,
            "tag_object_sha": "2" * 40,
            "source_sha": SOURCE,
            "control_sha": "3" * 40,
            "run_id": 100,
            "run_attempt": 1,
            "artifact_name": "candidate",
            "intent": {},
            "source_identity": {},
            "run": {},
            "files": [],
            "wheels": wheels,
            "chart": {
                "name": "unified-cache-pd",
                "version": intent.chart_version,
                "path": f"chart/unified-cache-pd-{intent.chart_version}.tgz",
                "file_sha256": "sha256:" + hashlib_sha("chart"),
                "content_tree_sha256": "sha256:" + hashlib_sha("chart-tree"),
                "record_path": "chart/record.json",
                "record_sha256": "sha256:" + hashlib_sha("chart-record"),
            },
            "image_members": members,
            "image_indexes": indexes,
        }
    )


def hashlib_sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _plan(
    tag: str = "v0.6.0rc1", objects: list[dict[str, object]] | None = None
) -> dict[str, object]:
    config = load_config(CONFIG)
    intent = parse_tag(tag, config)
    candidate = _candidate(tag)
    inventory = build_inventory(
        repository="OctoCat/unified-cache-management",
        repository_id=42,
        objects=objects or [],
    )
    return plan_publication(intent, candidate, inventory, config)


def test_absent_inventory_plans_exact_rc_product_closure() -> None:
    plan = _plan()

    assert plan["publishable"] is True
    assert all(item["decision"] == "create" for item in plan["items"])
    assert len(plan["items"]) == 6 + 6 + 3 + 2 + 1
    assert len(plan["operations"]) == len(plan["items"])
    coordinates = {item["coordinate"] for item in plan["items"]}
    assert "github-release://OctoCat/unified-cache-management/v0.6.0rc1" in coordinates
    assert "ghcr.io/octocat/ucm-cuda:v0.6.0rc1" in coordinates
    assert "oci://ghcr.io/octocat/charts/unified-cache-pd:0.6.0-rc.1" in coordinates
    assert not any(
        "pypi" in coordinate or "docker.io" in coordinate for coordinate in coordinates
    )


def test_draft_uses_private_images_and_no_chart_oci() -> None:
    plan = _plan("draft/v0.6.0-1")
    coordinates = {item["coordinate"] for item in plan["items"]}

    assert "ghcr.io/octocat/ucm-cuda-private:draft-v0.6.0-1" in coordinates
    assert not any(coordinate.startswith("oci://") for coordinate in coordinates)
    assert len(plan["items"]) == 6 + 6 + 3 + 1 + 1


def test_identical_inventory_reuses_every_remote_object() -> None:
    initial = _plan()
    objects = [
        {
            "coordinate": item["coordinate"],
            "stage": "rc",
            "state": "complete",
            "identity": item["desired_identity"],
        }
        for item in initial["items"]
    ]

    repeated = _plan(objects=objects)

    assert repeated["publishable"] is True
    assert all(item["decision"] == "reuse" for item in repeated["items"])
    assert repeated["operations"] == []


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda item: item.update(identity="sha256:" + "9" * 64), "identity-conflict"),
        (lambda item: item.update(stage="draft"), "cross-stage-occupancy"),
        (
            lambda item: item.update(state="partial", identity=None),
            "partial-publication",
        ),
    ],
)
def test_any_conflict_blocks_the_entire_plan_and_zeroes_write_operations(
    mutation: object, reason: str
) -> None:
    initial = _plan()
    selected = initial["items"][0]
    remote = {
        "coordinate": selected["coordinate"],
        "stage": "rc",
        "state": "complete",
        "identity": selected["desired_identity"],
    }
    mutation(remote)

    blocked = _plan(objects=[remote])

    assert blocked["publishable"] is False
    assert blocked["operations"] == []
    matching = next(
        item for item in blocked["items"] if item["coordinate"] == remote["coordinate"]
    )
    assert matching["decision"] == "blocked"
    assert matching["reason"] == reason


def test_inventory_rejects_duplicates_unknown_state_and_cross_repo() -> None:
    item = {
        "coordinate": "ghcr.io/octocat/ucm-cuda:v0.6.0rc1",
        "stage": "rc",
        "state": "complete",
        "identity": "sha256:" + "1" * 64,
    }
    with pytest.raises(ProductionError, match="duplicate"):
        build_inventory(
            REPOSITORY := "OctoCat/unified-cache-management", 42, [item, item]
        )
    with pytest.raises(ProductionError, match="state"):
        build_inventory(REPOSITORY, 42, [{**item, "state": "unknown"}])
    with pytest.raises(ProductionError, match="repository"):
        plan_publication(
            parse_tag("v0.6.0rc1", load_config(CONFIG)),
            _candidate(),
            build_inventory("evil/fork", 99, []),
            load_config(CONFIG),
        )


def test_candidate_digest_tamper_is_rejected_before_planning() -> None:
    config = load_config(CONFIG)
    candidate = copy.deepcopy(_candidate())
    candidate["sha256"] = "9" * 64

    with pytest.raises(ProductionError, match="sha256"):
        plan_publication(
            parse_tag("v0.6.0rc1", config),
            candidate,
            build_inventory("OctoCat/unified-cache-management", 42, []),
            config,
        )
