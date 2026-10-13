"""Archive one reviewed resource-free fixture failure before deleting its run."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace


def main():
    root = Path.cwd()
    sys.path.insert(0, str(root / ".github/release"))
    import hosted_cleanup as acceptance
    from ucm_release.cleanup_remote import RemoteError

    acceptance.CONFIG = json.loads(
        Path(__file__).with_name("acceptance-config.json").read_text()
    )
    remote = acceptance.remote_client()
    production = acceptance.deployed_production(
        remote,
        SimpleNamespace(
            production_root=root, production_sha=os.environ["PRODUCTION_SHA"]
        ),
    )
    run_id = 38381692109
    run = remote.json_request(
        "GET", f"/repos/{acceptance.REPOSITORY}/actions/runs/{run_id}"
    )
    acceptance.require(
        run["head_sha"] == "48151729a32147384ea1721aaeda59b05efd80ac"
        and run["head_branch"] == "feature/release-cleanup-fixtures-20261013"
        and run["event"] == "workflow_dispatch"
        and run["path"].split("@")[0] == ".github/workflows/release-nightly.yml"
        and run["conclusion"] == "failure"
        and run["actor"]["login"] == "SuperMarioYL",
        "Reviewed orphan source binding differs",
    )
    tag = "nightly/v0.8.0-20260917-" + str(run_id)
    try:
        remote.json_request("GET", f"/repos/{acceptance.REPOSITORY}/git/ref/tags/{tag}")
    except RemoteError as error:
        acceptance.require(error.is_missing, "Orphan Tag absence is not proven")
    else:
        raise RuntimeError("Orphan source unexpectedly created its Tag")
    acceptance.require(
        not any(
            r["tag_name"] == tag or r.get("name") == tag for r in remote.list_releases()
        ),
        "Orphan source has an associated Release",
    )
    artifacts = remote.list_pages(
        f"/repos/{acceptance.REPOSITORY}/actions/runs/{run_id}/artifacts",
        key="artifacts",
    )
    acceptance.require(
        not any(
            a["name"].startswith(("fixture-ledger-", "ucm-release-cleanup-"))
            for a in artifacts
        ),
        "Orphan source has ownership evidence requiring ordinary cleanup",
    )
    raw = remote.request(
        "GET", f"/repos/{acceptance.REPOSITORY}/actions/runs/{run_id}/logs"
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        acceptance.require(
            archive.testzip() is None, "Orphan log archive is incomplete"
        )
    out = Path("out/hosted")
    out.mkdir(parents=True, exist_ok=True)
    (out / "orphan-source-logs.zip").write_bytes(raw)
    (out / "orphan-source.json").write_text(
        json.dumps(
            {
                "production": production,
                "source_run": run,
                "tag_absent": True,
                "release_absent": True,
                "ownership_artifacts_absent": True,
                "logs_sha256": hashlib.sha256(raw).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    print("Reviewed orphan source archived:", run_id)


if __name__ == "__main__":
    main()
