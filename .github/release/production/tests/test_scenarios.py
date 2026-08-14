from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest

from ucm_release_production.candidate import (
    compare_trusted_rebuild,
    reopen_candidate,
)
from ucm_release_production.common import ProductionError
from ucm_release_production.external import (
    ExternalCredentials,
    preflight_external_channels,
)
from ucm_release_production.github_release import (
    finalize_release,
    readback_release,
    upload_assets,
)
from ucm_release_production.reconcile import build_inventory, plan_publication
from ucm_release_production.registry import (
    VisibilityConfigurationRequired,
    publish_member,
)
from ucm_release_production.tags import parse_tag, verify_ref_snapshot

from test_candidate import _archive as candidate_archive
from test_candidate import _expected as candidate_expected
from test_external import _enabled as external_enabled_config
from test_external import _environment as external_environment
from test_github_release import FakeReleaseClient
from test_github_release import _plan as github_release_plan
from test_reconcile import _candidate as publication_candidate
from test_registry import FakeRegistry
from test_registry import _member_request as member_request
from test_tags import _snapshot as ref_snapshot

from conftest import PRODUCTION_ROOT

CONFIG = PRODUCTION_ROOT / "production-release.json"
REPOSITORY = "OctoCat/unified-cache-management"
REPOSITORY_ID = 42


def _config() -> dict[str, object]:
    from ucm_release_production.config import load_config

    return load_config(CONFIG)


def _publication_plan(
    tag: str, objects: list[dict[str, object]] | None = None
) -> dict[str, object]:
    config = _config()
    return plan_publication(
        parse_tag(tag, config),
        publication_candidate(tag),
        build_inventory(REPOSITORY, REPOSITORY_ID, objects or []),
        config,
    )


def test_draft_scenario_is_private_preview_without_external_writes() -> None:
    plan = _publication_plan("draft/v0.6.0-1")
    external = preflight_external_channels(
        parse_tag("draft/v0.6.0-1", external_enabled_config()),
        external_enabled_config(),
        external_environment("waived-for-preview"),
        ExternalCredentials(
            pypi_oidc=False,
            docker_username=None,
            docker_token_present=False,
        ),
    )

    assert plan["publishable"] is True
    assert all(item["decision"] == "create" for item in plan["items"])
    assert all(
        "-private:" in item["coordinate"]
        for item in plan["items"]
        if item["kind"] == "image-index"
    )
    assert not any(item["kind"] == "chart-oci" for item in plan["items"])
    assert external["operations"] == []


def test_rc_scenario_closes_public_ghcr_chart_and_prerelease() -> None:
    plan = _publication_plan("v0.6.0rc1")
    kinds = [item["kind"] for item in plan["items"]]

    assert plan["publishable"] is True
    assert kinds.count("image-member") == 6
    assert kinds.count("image-index") == 3
    assert kinds.count("chart-oci") == 1
    assert kinds.count("github-release") == 1
    assert not any("-private:" in item["coordinate"] for item in plan["items"])


def test_first_public_package_visibility_hold_preserves_remote_bytes(
    tmp_path: Path,
) -> None:
    transport = FakeRegistry()
    request = member_request(tmp_path, visibility="public")
    transport.private_repositories.add(request.repository)

    with pytest.raises(VisibilityConfigurationRequired) as raised:
        publish_member(request, transport)

    record = raised.value.record
    assert record["status"] == "visibility-configuration-required"
    assert record["authenticated_readback"]["digest"] == record["manifest_digest"]
    assert any(operation[0] == "push" for operation in transport.operations)


def test_identical_rc_rerun_has_zero_planned_writes() -> None:
    first = _publication_plan("v0.6.0rc1")
    inventory = [
        {
            "coordinate": item["coordinate"],
            "stage": "rc",
            "state": "complete",
            "identity": item["desired_identity"],
        }
        for item in first["items"]
    ]

    repeated = _publication_plan("v0.6.0rc1", inventory)

    assert repeated["publishable"] is True
    assert repeated["operations"] == []
    assert {item["decision"] for item in repeated["items"]} == {"reuse"}


def test_partial_release_upload_recovers_without_overwrite(tmp_path: Path) -> None:
    plan = github_release_plan(tmp_path)
    client = FakeReleaseClient()
    client.fail_upload_name = plan.assets[4].name

    with pytest.raises(ProductionError, match="simulated"):
        upload_assets(plan, client)
    uploaded_before = len(client.releases[0]["assets"])
    client.fail_upload_name = None

    record = finalize_release(plan, client)

    assert uploaded_before == 4
    assert record["status"] == "complete"
    assert record["release_state"] == "prerelease"
    assert len(client.releases[0]["assets"]) == 7
    assert len({asset["name"] for asset in client.releases[0]["assets"]}) == 7


def test_remote_identity_conflict_blocks_every_planned_write() -> None:
    initial = _publication_plan("v0.6.0rc1")
    selected = initial["items"][0]
    conflict = {
        "coordinate": selected["coordinate"],
        "stage": "rc",
        "state": "complete",
        "identity": "sha256:" + "9" * 64,
    }

    blocked = _publication_plan("v0.6.0rc1", [conflict])

    assert blocked["publishable"] is False
    assert blocked["operations"] == []
    assert blocked["blockers"] == [
        {"coordinate": conflict["coordinate"], "reason": "identity-conflict"}
    ]


def test_tag_ref_double_read_drift_blocks_before_publication() -> None:
    intent = parse_tag("v0.6.0rc1", _config())
    snapshot = ref_snapshot(intent)
    snapshot["tag"]["ref_reads"][1]["object_sha"] = "9" * 40

    with pytest.raises(ProductionError, match="double-read"):
        verify_ref_snapshot(intent, snapshot)


def test_candidate_replacement_is_rejected_on_reopen(tmp_path: Path) -> None:
    archive, _ = candidate_archive(tmp_path)
    expected = candidate_expected()
    expected["run_attempt"] = 2

    with pytest.raises(ProductionError, match="run_attempt"):
        reopen_candidate(archive, expected)


def test_trusted_wheel_byte_drift_blocks_candidate(tmp_path: Path) -> None:
    archive, _ = candidate_archive(tmp_path)
    with reopen_candidate(archive, candidate_expected()) as bundle:
        trusted = tmp_path / "trusted"
        for spec_id, wheel in bundle.wheel_paths.items():
            destination = trusted / spec_id / wheel.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(wheel, destination)
        selected = next(trusted.rglob("*.whl"))
        selected.write_bytes(selected.read_bytes() + b"drift")

        with pytest.raises(ProductionError, match="wheel|byte|ZIP"):
            compare_trusted_rebuild(bundle, trusted)


def test_anonymous_release_readback_failure_blocks_completion(tmp_path: Path) -> None:
    plan = github_release_plan(tmp_path)
    client = FakeReleaseClient()
    complete = finalize_release(plan, client)
    assert complete["status"] == "complete"
    asset_id = client.releases[0]["assets"][0]["id"]
    client.contents[asset_id] = b"anonymous-readback-drift"

    with pytest.raises(ProductionError, match="download"):
        readback_release(plan, client)


def test_stable_preview_waiver_is_not_production_evidence() -> None:
    config = external_enabled_config()
    intent = parse_tag("v0.6.0", config)

    with pytest.raises(ProductionError, match="environment"):
        preflight_external_channels(
            intent,
            config,
            external_environment("waived-for-preview"),
            ExternalCredentials(
                pypi_oidc=True,
                docker_username="operator",
                docker_token_present=True,
            ),
        )


def test_stable_same_sha_rc_lineage_and_real_environment_are_accepted() -> None:
    config = _config()
    intent = parse_tag("v0.6.0", config)
    source = verify_ref_snapshot(intent, ref_snapshot(intent))
    external = preflight_external_channels(
        intent,
        config,
        external_environment("passed"),
        ExternalCredentials(
            pypi_oidc=False,
            docker_username=None,
            docker_token_present=False,
        ),
    )

    assert source["lineage"]["stage"] == "rc"
    assert source["lineage"]["source_commit_sha"] == source["source_commit_sha"]
    assert external["operations"] == []
    assert external["channels"] == {"pypi": "disabled", "docker_hub": "disabled"}


def test_hotfix_requires_and_accepts_previous_stable_ancestry() -> None:
    intent = parse_tag("v0.6.1", _config())
    accepted = verify_ref_snapshot(intent, ref_snapshot(intent))
    rejected = ref_snapshot(intent)
    rejected["lineage"] = copy.deepcopy(rejected["lineage"])
    rejected["lineage"]["ancestry_verified"] = False

    assert accepted["lineage"]["stage"] == "stable"
    with pytest.raises(ProductionError, match="ancestry"):
        verify_ref_snapshot(intent, rejected)


@pytest.mark.parametrize(
    "credentials",
    [
        ExternalCredentials(
            pypi_oidc=False,
            docker_username="operator",
            docker_token_present=True,
        ),
        ExternalCredentials(
            pypi_oidc=True,
            docker_username=None,
            docker_token_present=False,
        ),
    ],
)
def test_stable_external_credentials_are_fail_closed(
    credentials: ExternalCredentials,
) -> None:
    config = external_enabled_config()
    with pytest.raises(ProductionError, match="PyPI|Docker Hub"):
        preflight_external_channels(
            parse_tag("v0.6.0", config),
            config,
            external_environment("passed"),
            credentials,
        )
