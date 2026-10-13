"""Preserve original historical assets in an independent acceptance artifact."""

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT.parent))
from ucm_release.cleanup_remote import ProductionRemote


def main():
    frozen = json.loads(
        (ROOT / "evidence/history/v0.7.0rc14/asset-backup-ledger.json").read_text()
    )
    remote = ProductionRemote(
        "SuperMarioYL/unified-cache-management", os.environ["GH_TOKEN"]
    )
    release = remote.json_request(
        "GET", "/repos/SuperMarioYL/unified-cache-management/releases/387579666"
    )
    expected_ids = {asset["id"] for asset in frozen["assets"]}
    if (
        release["tag_name"] != "v0.7.0rc14"
        or {asset["id"] for asset in release["assets"]} != expected_ids
    ):
        raise SystemExit("Historical Release asset identity changed")

    destination = ROOT / "original-history-assets"
    destination.mkdir(exist_ok=True)
    observations = []
    for asset in frozen["assets"]:
        raw = remote.request(
            "GET",
            f"/repos/SuperMarioYL/unified-cache-management/releases/assets/{asset['id']}",
            accept="application/octet-stream",
        )
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != asset["expected_size"] or digest != asset["sha256"]:
            raise SystemExit(f"Original asset {asset['id']} size/digest changed")
        (destination / asset["name"]).write_bytes(raw)
        observations.append(
            {
                "id": asset["id"],
                "name": asset["name"],
                "bytes": len(raw),
                "sha256": digest,
            }
        )
        (ROOT / "original-history-assets-readback.json").write_text(
            json.dumps(observations, indent=2) + "\n"
        )
    print(f"Archived {len(observations)} exact original assets; all digests verified")


if __name__ == "__main__":
    main()
