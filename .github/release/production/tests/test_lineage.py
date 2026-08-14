from __future__ import annotations

from typing import Any

from ucm_release_production.lineage import resolve_release_lineage
from ucm_release_production.tags import TagIntent

SOURCE = "1" * 40
CANDIDATE = "2" * 64


class SevenAssetReleaseClient:
    repository = "OctoCat/unified-cache-management"

    def __init__(self) -> None:
        self.release = {
            "id": 41,
            "tag_name": "draft/v0.6.0-12",
            "target_commitish": SOURCE,
            "draft": True,
            "prerelease": False,
            "body": (
                "UCM production draft release.\n\n"
                '<!-- ucm-production-lineage-v1 {"candidate_sha256":"'
                + CANDIDATE
                + '","environment_status":"waived-for-preview","source_sha":"'
                + SOURCE
                + '"} -->'
            ),
        }
        names = [
            "uc_manager_cuda-0.6.0.dev12-cp312-cp312-manylinux_2_28_x86_64.whl",
            "uc_manager_cuda-0.6.0.dev12-cp312-cp312-manylinux_2_28_aarch64.whl",
            "uc_manager_cann_a2-0.6.0.dev12-cp312-cp312-linux_x86_64.whl",
            "uc_manager_cann_a2-0.6.0.dev12-cp312-cp312-linux_aarch64.whl",
            "uc_manager_cann_a3-0.6.0.dev12-cp312-cp312-linux_x86_64.whl",
            "uc_manager_cann_a3-0.6.0.dev12-cp312-cp312-linux_aarch64.whl",
            "unified-cache-pd-0.6.0-draft.12.tgz",
        ]
        self.assets = [
            {
                "id": position,
                "name": name,
                "size": position,
                "digest": "sha256:" + f"{position:x}" * 64,
            }
            for position, name in enumerate(names, start=1)
        ]

    def list_releases(self) -> list[dict[str, Any]]:
        return [dict(self.release)]

    def list_release_assets(self, release_id: int) -> list[dict[str, Any]]:
        assert release_id == 41
        return [dict(item) for item in self.assets]

    def download_release_asset(self, asset: dict[str, Any]) -> bytes:
        raise AssertionError(
            f"lineage must not download internal asset {asset['name']}"
        )


def test_rc_lineage_uses_seven_delivery_assets_and_release_marker() -> None:
    intent = TagIntent(
        stage="rc",
        tag_name="v0.6.0rc1",
        version="0.6.0",
        wheel_version="0.6.0rc1",
        chart_version="0.6.0-rc.1",
        image_tag="v0.6.0rc1",
        release_branch="0.6.0-release",
        draft_number=None,
        rc_number=1,
    )

    result = resolve_release_lineage(SevenAssetReleaseClient(), intent, SOURCE)

    assert result is not None
    assert result["accepted"] is True
    assert result["stage"] == "draft"
    assert result["tag_name"] == "draft/v0.6.0-12"
    assert result["source_commit_sha"] == SOURCE
    assert len(result["evidence_sha256"]) == 64
