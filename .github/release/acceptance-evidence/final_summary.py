"""Read back the frozen Fork acceptance targets; never mutate remote resources."""

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).parent
REPOSITORY = "SuperMarioYL/unified-cache-management"
OBSERVED = {"checks": [], "errors": []}


def read_api(path):
    result = subprocess.run(
        ["gh", "api", "--include", path],
        capture_output=True,
        text=True,
        check=False,
    )
    status = None
    body = result.stdout
    if body.startswith("HTTP/"):
        header, body = body.split("\n\n", 1)
        status = int(header.splitlines()[0].split()[1])
    value = None
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        pass
    return {"path": path, "status": status, "value": value}


def check(condition, message, observation):
    OBSERVED["checks"].append({"check": message, "observed": observation})
    if not condition:
        OBSERVED["errors"].append(message)


def fingerprint(release, ref):
    return {
        "release_id": release["id"],
        "tag_name": release["tag_name"],
        "draft": release["draft"],
        "prerelease": release["prerelease"],
        "name": release["name"],
        "body_sha256": hashlib.sha256((release.get("body") or "").encode()).hexdigest(),
        "assets": sorted(
            [
                {key: asset.get(key) for key in ("id", "name", "size", "digest")}
                for asset in release["assets"]
            ],
            key=lambda asset: asset["id"],
        ),
        "ref_object": ref["object"],
    }


def main():
    expected = json.loads((ROOT / "expected.json").read_text())
    for item in expected["files"]:
        path = ROOT / item["path"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        check(actual == item["sha256"], f"evidence checksum {item['path']}", actual)

    sys.path.insert(0, str(ROOT.parent))
    from ucm_release.cleanup_remote import ProductionRemote

    archive = expected["original_history_archive"]
    remote = ProductionRemote(REPOSITORY, os.environ["GH_TOKEN"])
    raw = remote.request(
        "GET", f"/repos/{REPOSITORY}/actions/artifacts/{archive['id']}/zip"
    )
    digest = hashlib.sha256(raw).hexdigest()
    check(digest == archive["sha256"], "independent original archive digest", digest)
    if digest != archive["sha256"]:
        raise SystemExit("Independent archive digest differs from frozen metadata")
    with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
        if zipped.testzip() is not None:
            raise SystemExit("Independent original archive CRC failed")
    OBSERVED["original_history_archive"] = {
        **archive,
        "bytes": len(raw),
        "digest_verified": True,
        "crc_verified": True,
    }

    branch = read_api(f"/repos/{REPOSITORY}/git/ref/heads/develop")
    check(
        branch["status"] == 200
        and (branch["value"] or {}).get("object", {}).get("sha")
        == expected["production_sha"],
        "default branch is final production P2",
        branch,
    )
    for tag, frozen in expected["preserved"].items():
        release = read_api(f"/repos/{REPOSITORY}/releases/{frozen['release_id']}")
        ref = read_api(f"/repos/{REPOSITORY}/git/ref/tags/{quote(tag, safe='/')}")
        actual = None
        if release["status"] == ref["status"] == 200:
            actual = fingerprint(release["value"], ref["value"])
        check(actual == frozen, f"preserve Release/assets/body/ref {tag}", actual)

    for item in expected["source_runs"]:
        run = read_api(f"/repos/{REPOSITORY}/actions/runs/{item['id']}")
        value = run["value"] or {}
        check(
            run["status"] == 200
            and value.get("head_sha") == item["sha"]
            and value.get("run_attempt") == item["attempt"]
            and value.get("conclusion") == "success",
            f"successful publication source run {item['id']}",
            run,
        )
    for item in expected["readbacks"]:
        actual = read_api(item["path"])
        check(
            actual["status"] == item["status"],
            f"exact {item['status']} {item['path']}",
            actual,
        )

    ruleset = read_api(f"/repos/{REPOSITORY}/rulesets/20626631")
    value = ruleset["value"] or {}
    # A read-only job token cannot see bypass actors; the admin restoration
    # readback is preserved separately in the historical closure evidence.
    actual = {key: value.get(key) for key in expected["ruleset_readable"]}
    check(
        ruleset["status"] == 200 and actual == expected["ruleset_readable"],
        "original observable v* conditions and rules",
        ruleset,
    )
    OBSERVED["acceptance_status"] = expected["acceptance_status"]
    OBSERVED["status"] = "failed" if OBSERVED["errors"] else "verified"
    if OBSERVED["errors"]:
        raise SystemExit("; ".join(OBSERVED["errors"]))


if __name__ == "__main__":
    try:
        main()
    finally:
        if "status" not in OBSERVED:
            OBSERVED["status"] = "failed before all readbacks completed"
        (ROOT / "final-independent-readback.json").write_text(
            json.dumps(OBSERVED, indent=2) + "\n"
        )
