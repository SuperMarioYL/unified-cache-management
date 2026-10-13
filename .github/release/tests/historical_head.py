"""Read-only exact manifest census using HEAD, avoiding quota-consuming GET fallback."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote


def main():
    root = Path.cwd()
    sys.path.insert(0, str(root / ".github/release"))
    import hosted_cleanup as acceptance
    from ucm_release import version_config
    from ucm_release.cleanup_inventory import ReleaseInventory, collect_catalog
    from ucm_release.cleanup_records import OCI_REFERENCE
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
    tag = os.environ["PREVIEW_TAG"]
    expected = json.loads(
        Path(__file__).with_name("historical_targets.json").read_text()
    )[tag]
    baseline = acceptance.baseline(remote)
    catalog = collect_catalog(
        remote, release_type=version_config.classify_tag(tag)["release_type"]
    )
    records = [
        item for item in (*catalog.records, *catalog.orphans) if item["tag"] == tag
    ]
    acceptance.require(
        len(records) == 1
        and tag not in catalog.blocked
        and tag not in catalog.active_tags,
        "Historical target is not proven and idle",
    )
    target = ReleaseInventory(remote).resolve_record(records[0]).target
    acceptance.require(
        [r.identifier for r in target.releases] == [expected["release_id"]]
        and list(target.run_ids) == [expected["source_run_id"]],
        "Historical exact IDs differ",
    )
    expected_count = expected["registry_count"]
    acceptance.require(
        len(target.registry) == expected_count,
        "Resolved registry targets differ from the frozen planned count",
    )
    release = remote.json_request(
        "GET", f"/repos/{acceptance.REPOSITORY}/releases/{expected['release_id']}"
    )
    acceptance.require(
        sorted(asset["id"] for asset in release["assets"])
        == sorted(expected["asset_ids"]),
        "Release assets differ from the frozen backup",
    )
    source = remote.read_release_run(expected["source_run_id"])
    acceptance.require(
        source is not None and source["status"] == "completed",
        "Source is not completed",
    )
    if "source_sha" in expected:
        acceptance.require(
            source["head_sha"] == expected["source_sha"]
            and source["conclusion"] == "failure"
            and source["event"] == "push"
            and source["head_branch"] == tag
            and source["path"].split("@", 1)[0] == ".github/workflows/release-tag.yml",
            "Real CMake failure source identity differs from the frozen backup",
        )
        acceptance.require(
            "Status: `artifacts-failed`" in (release.get("body") or "")
            and "Wheel outcome: failure" in release["body"]
            and not any(
                asset["name"] == "release-manifest.json" for asset in release["assets"]
            ),
            "Real failed Release lost its detailed outcome or gained a public manifest",
        )
        jobs = remote.list_pages(
            f"/repos/{acceptance.REPOSITORY}/actions/runs/{source['id']}/jobs",
            key="jobs",
        )
        by_name = {job["name"]: job for job in jobs}
        acceptance.require(
            all(
                by_name[name]["conclusion"] == "failure"
                for name in expected["native_job_names"]
            )
            and len(expected["native_job_names"]) == expected["native_failed_jobs"],
            "Actual native build job outcomes differ from the frozen backup",
        )
        acceptance.require(
            all(
                by_name[name]["conclusion"] == "skipped"
                for name in expected["publisher_job_names"]
            ),
            "A side-effect publication stage ran after the real CMake failure",
        )
    out = Path("out/hosted")
    out.mkdir(parents=True, exist_ok=True)
    tokens = {}
    rows = []
    proof = {
        "tag": tag,
        "production": production,
        "release_ids": [r.identifier for r in target.releases],
        "run_ids": list(target.run_ids),
        "registry_planned": len(target.registry),
        "registry_expected": expected_count,
        "source": {
            key: source[key]
            for key in (
                "id",
                "head_sha",
                "event",
                "status",
                "conclusion",
                "run_attempt",
            )
        },
        "asset_ids": sorted(asset["id"] for asset in release["assets"]),
        "source_evidence": expected.get("evidence", "historical-release"),
        "resources": rows,
    }
    for resource in target.registry:
        row = {"kind": resource.kind, "reference": resource.reference}
        try:
            if resource.kind.startswith("dockerhub-"):
                match = OCI_REFERENCE.fullmatch(resource.reference)
                repository = match.group("repository")
                if repository not in tokens:
                    exchange = subprocess.run(
                        ["crane", "auth", "token", repository],
                        capture_output=True,
                        check=True,
                    )
                    data = json.loads(exchange.stdout)
                    value = data.get("access_token") or data.get("token")
                    segment = value.split(".")[1]
                    claims = json.loads(
                        base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
                    )
                    path = repository.removeprefix("docker.io/")
                    acceptance.require(
                        bool(claims.get("sub"))
                        and any(
                            item.get("name") == path
                            and "pull" in item.get("actions", [])
                            for item in claims.get("access", [])
                        ),
                        "Historical HEAD token lacks verified principal or pull scope",
                    )
                    tokens[repository] = value
                request = urllib.request.Request(
                    "https://index.docker.io/v2/"
                    + repository.removeprefix("docker.io/")
                    + "/manifests/"
                    + quote(match.group("tag"), safe=""),
                    method="HEAD",
                    headers={
                        "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json"
                    },
                )
                request.add_unredirected_header(
                    "Authorization", "Bearer " + tokens[repository]
                )
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        state = response.headers.get("Docker-Content-Digest")
                        acceptance.require(
                            state and state.startswith("sha256:"),
                            "Manifest HEAD omitted its immutable digest",
                        )
                        row.update(
                            presence="present", state=state, status=response.status
                        )
                except urllib.error.HTTPError as error:
                    row.update(
                        presence="absent" if error.code == 404 else "error",
                        state=None,
                        status=error.code,
                    )
            else:
                state = remote.probe(resource)
                row.update(
                    presence="absent" if state is None else "present",
                    state=state,
                    status=404 if state is None else 200,
                )
        except RemoteError as error:
            row.update(
                presence="absent" if error.is_missing else "error",
                state=None,
                status=error.status,
                error=None if error.is_missing else str(error),
            )
        rows.append(row)
        (out / "historical-head-census.json").write_text(
            json.dumps(proof, indent=2) + "\n"
        )
    proof["baseline"] = acceptance.baseline(remote, baseline)
    proof["status"] = (
        "complete"
        if len(rows) == expected_count
        and all(item["presence"] != "error" for item in rows)
        and (
            expected.get("evidence") != "real-cmake-build-failure"
            or all(item["presence"] == "absent" for item in rows)
        )
        else "blocked"
    )
    (out / "historical-head-census.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(tag, proof["status"], len(rows), "exact registry HEAD/API probes")
    acceptance.require(
        proof["status"] == "complete",
        "Historical census still has unresolved read errors",
    )


if __name__ == "__main__":
    main()
