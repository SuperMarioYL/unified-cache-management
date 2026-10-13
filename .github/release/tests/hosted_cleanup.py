"""Fork-only lifecycle acceptance; imports the fixed deployed production modules.

The source ledger survives in an independent closure run before cleanup starts.
Whole-package deletion is confined to a proven exclusive disposable GHCR fixture.
This helper and its workflows must stay on the isolated acceptance branch.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

REPOSITORY = "SuperMarioYL/unified-cache-management"
OWNER = "SuperMarioYL"
MODULES = (
    "cleanup.py",
    "cleanup_records.py",
    "cleanup_inventory.py",
    "cleanup_remote.py",
)
CONFIG = {}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def module_hashes(root):
    return {
        name: hashlib.sha256(
            (root / ".github/release/ucm_release" / name).read_bytes()
        ).hexdigest()
        for name in MODULES
    }


def remote_client():
    from ucm_release.cleanup_remote import ProductionRemote

    require(os.environ.get("GITHUB_ACTIONS") == "true", "Run only in GitHub Actions")
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY, "Wrong Fork")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    require(bool(token), "Missing job GITHUB_TOKEN")
    return ProductionRemote(REPOSITORY, token)


def post_json(remote, path, payload):
    raw = remote.request("POST", path, data=json.dumps(payload).encode())
    return json.loads(raw) if raw else None


def deployed_production(remote, arguments):
    repository = remote.json_request("GET", f"/repos/{REPOSITORY}")
    require(
        repository["default_branch"] == "develop", "Default branch must remain develop"
    )
    live = remote.json_request("GET", f"/repos/{REPOSITORY}/git/ref/heads/develop")[
        "object"
    ]["sha"]
    actual = subprocess.run(
        ["git", "-C", str(arguments.production_root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    require(
        re.fullmatch(r"[0-9a-f]{40}", arguments.production_sha),
        "Use the full approved production SHA",
    )
    require(
        arguments.production_sha == CONFIG["production_sha"],
        "Fixture configuration names another production commit",
    )
    require(
        actual == live == arguments.production_sha,
        "Production/default branch changed during acceptance",
    )
    return {"sha": actual, "module_hashes": module_hashes(arguments.production_root)}


def fixture_tag(tag):
    if tag in {CONFIG["draft_tag"], CONFIG["manual_draft_tag"]}:
        return True
    dates = "|".join(re.escape(value) for value in CONFIG["nightly_dates"].values())
    return (
        re.fullmatch(
            rf"nightly/v{re.escape(CONFIG['version'])}-(?:{dates})-[1-9][0-9]{{9,}}",
            tag,
        )
        is not None
    )


def baseline(remote, previous=None):
    releases = remote.list_releases()
    expected = CONFIG["protected_baseline"] if previous is None else previous
    selected = [r for r in releases if r["tag_name"] in expected]
    result = {}
    for release in selected:
        tag = release["tag_name"]
        ref = remote.json_request(
            "GET", f"/repos/{REPOSITORY}/git/ref/tags/{quote(tag, safe='/')}"
        )
        result[tag] = {
            "release_id": release["id"],
            "tag_name": tag,
            "draft": release["draft"],
            "prerelease": release["prerelease"],
            "name": release["name"],
            "body_sha256": hashlib.sha256(
                (release.get("body") or "").encode()
            ).hexdigest(),
            "assets": sorted(
                [
                    {key: asset.get(key) for key in ("id", "name", "size", "digest")}
                    for asset in release["assets"]
                ],
                key=lambda asset: asset["id"],
            ),
            "ref_object": ref["object"],
        }
    require(
        result == expected, "A protected baseline Release, asset, body or Tag changed"
    )
    return result


def call_cleanup(arguments, expected=0):
    from ucm_release import cleanup

    status = cleanup.main(arguments)
    require(status == expected, f"cleanup exit {status}, expected {expected}")


def source_ledger(remote, arguments, tag, source_kind):
    production = deployed_production(remote, arguments)
    run_id = int(os.environ["GITHUB_RUN_ID"])
    run = remote.read_release_run(run_id)
    source_sha = os.environ["GITHUB_SHA"]
    expected_path = (
        ".github/workflows/release-tag.yml"
        if source_kind in {"ghcr", "manual-draft"}
        else ".github/workflows/release-nightly.yml"
    )
    expected_event = (
        "push" if source_kind in {"ghcr", "manual-draft"} else "workflow_dispatch"
    )
    require(
        run
        and run["path"].split("@", 1)[0] == expected_path
        and run["event"] == expected_event
        and run["head_sha"] == source_sha,
        "Source workflow, event or SHA differs",
    )
    require(fixture_tag(tag), "Tag is outside this batch's fixture allowlist")
    require(
        not any(
            r["tag_name"] == tag or (r.get("draft") and r.get("name") == tag)
            for r in remote.list_releases()
        ),
        "Refusing to replace an existing Release",
    )
    return {
        "kind": "ucm-hosted-cleanup-fixture",
        "batch": CONFIG["batch"],
        "repository": REPOSITORY,
        "tag": tag,
        "source_kind": source_kind,
        "source_run_id": run_id,
        "source_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "source_sha": source_sha,
        "source_workflow": expected_path,
        "production_sha": production["sha"],
        "production_module_hashes": production["module_hashes"],
        "baseline": baseline(remote),
        "prepared": False,
    }


def release_and_records(remote, arguments, ledger, plan):
    tag = ledger["tag"]
    release = post_json(
        remote,
        f"/repos/{REPOSITORY}/releases",
        {
            "tag_name": tag,
            "name": tag,
            "draft": True,
            "prerelease": True,
            "make_latest": "false",
            "body": "Status: `release-open`\n\nDisposable Fork lifecycle fixture; no UCM product build.",
        },
    )
    ledger["release_id"] = release["id"]
    write_json(arguments.ledger_dir / f"fixture-{ledger['source_run_id']}.json", ledger)
    identity = [
        "record-targets",
        "--repository",
        REPOSITORY,
        "--tag",
        tag,
        "--release-type",
        "nightly" if ledger["source_kind"] == "nightly" else "draft",
        "--run-id",
        str(ledger["source_run_id"]),
        "--run-attempt",
        str(ledger["source_attempt"]),
        "--source-sha",
        ledger["source_sha"],
        "--release-id",
        str(ledger["release_id"]),
    ]
    call_cleanup([*identity, "--output-dir", str(arguments.open_dir)])
    plan_path = arguments.ledger_dir.parent / "input/release-plan.json"
    write_json(plan_path, plan)
    call_cleanup(
        [*identity, "--plan", str(plan_path), "--output-dir", str(arguments.plan_dir)]
    )


def prepare_nightly(arguments):
    from ucm_release import version_config
    from ucm_release.cleanup_inventory import collect_catalog

    remote = remote_client()
    run_id = int(os.environ["GITHUB_RUN_ID"])
    role = arguments.role
    tag = f"nightly/v{CONFIG['version']}-{CONFIG['nightly_dates'][role]}-{run_id}"
    ledger = source_ledger(remote, arguments, tag, "nightly")
    ledger.update(role=role, expected_conclusion=arguments.outcome)
    max_count = int(CONFIG["nightly_max_count"])
    current = collect_catalog(remote, release_type="nightly").records
    if role == "filler":
        require(
            len(current) < max_count,
            "Quota is already full; do not create another filler",
        )
    if role == "victim":
        require(
            len(current) == max_count,
            "Fill the exact quota before creating a deletion victim",
        )
    # POST fails on collisions. A job token-created Tag does not start a second Tag workflow.
    post_json(
        remote,
        f"/repos/{REPOSITORY}/git/refs",
        {
            "ref": f"refs/tags/{tag}",
            "sha": ledger["source_sha"],
        },
    )
    identity = version_config.classify_tag(tag)
    plan = {
        "kind": "ucm-release-plan",
        "route": "release",
        "repository": REPOSITORY,
        "git_tag": tag,
        "release_type": "nightly",
        "version": identity["version"],
        "image_version": identity["image_version"],
        "families": [],
        "publish": {"chart_oci": {"enabled": False}},
    }
    release_and_records(remote, arguments, ledger, plan)
    ledger["prepared"] = True
    write_json(arguments.ledger_dir / f"fixture-{run_id}.json", ledger)
    baseline(remote, ledger["baseline"])
    print(
        f"FIXTURE_READY tag={tag} run_id={run_id} release_id={ledger['release_id']}",
        flush=True,
    )


def prepare_draft(arguments):
    from ucm_release import version_config

    remote = remote_client()
    tag = os.environ["GITHUB_REF_NAME"]
    require(
        tag == CONFIG["manual_draft_tag"]
        and os.environ["GITHUB_REF"] == f"refs/tags/{tag}",
        "Wrong manual Draft fixture Tag push",
    )
    ledger = source_ledger(remote, arguments, tag, "manual-draft")
    ledger["expected_conclusion"] = "failure"
    identity = version_config.classify_tag(tag)
    plan = {
        "kind": "ucm-release-plan",
        "route": "release",
        "repository": REPOSITORY,
        "git_tag": tag,
        "release_type": "draft",
        "version": identity["version"],
        "image_version": identity["image_version"],
        "families": [],
        "publish": {"chart_oci": {"enabled": False}},
    }
    release_and_records(remote, arguments, ledger, plan)
    ledger["prepared"] = True
    write_json(arguments.ledger_dir / f"fixture-{ledger['source_run_id']}.json", ledger)
    baseline(remote, ledger["baseline"])
    print(
        f"FIXTURE_READY tag={tag} run_id={ledger['source_run_id']} release_id={ledger['release_id']}",
        flush=True,
    )


def labels(ledger):
    return {
        "org.opencontainers.image.source": f"https://github.com/{REPOSITORY}",
        "ucm.hosted.cleanup.source_run": str(ledger["source_run_id"]),
        "ucm.hosted.cleanup.source_sha": ledger["source_sha"],
        "ucm.hosted.cleanup.tag": ledger["tag"],
        "ucm.hosted.cleanup.batch": ledger["batch"],
    }


def docker_login(remote):
    subprocess.run(
        ["docker", "login", "ghcr.io", "--username", OWNER, "--password-stdin"],
        input=remote.token + "\n",
        text=True,
        check=True,
    )


def prepare_ghcr(arguments):
    from ucm_release import cleanup_records as records
    from ucm_release import runtime, version_config

    remote = remote_client()
    tag = os.environ["GITHUB_REF_NAME"]
    require(
        tag == CONFIG["draft_tag"] and os.environ["GITHUB_REF"] == f"refs/tags/{tag}",
        "Wrong fixture Tag push",
    )
    ledger = source_ledger(remote, arguments, tag, "ghcr")
    ledger["expected_conclusion"] = "failure"
    version = str(version_config.classify_tag(tag)["version"])
    package = f"ucm-cleanup-fixture-{CONFIG['batch']}-run-{ledger['source_run_id']}"
    reference = f"ghcr.io/{OWNER.lower()}/{package}:fixture-ucm-{runtime.oci_tag_version(version)}"
    ledger.update(
        registry_reference=reference,
        package=package,
        ghcr_version_path=None,
        ghcr_version_id=None,
    )
    plan = {
        "kind": "ucm-release-plan",
        "route": "release",
        "repository": REPOSITORY,
        "git_tag": tag,
        "release_type": "draft",
        "version": version,
        "publish": {
            "ghcr": {"enabled": True},
            "dockerhub": {"enabled": False},
            "chart_oci": {"enabled": False},
        },
        "families": [
            {
                "create_index": False,
                "published_reference": reference,
                "members": [{"reference": reference, "cpu_arch": "amd64"}],
            }
        ],
    }
    release_and_records(remote, arguments, ledger, plan)
    docker_login(remote)
    with tempfile.TemporaryDirectory(prefix="ucm-hosted-cleanup-") as directory:
        context = Path(directory)
        (context / "marker").write_text(
            f"{tag}\n{ledger['source_run_id']}\n{ledger['source_sha']}\n"
        )
        (context / "Dockerfile").write_text(
            "FROM scratch\nCOPY marker /fixture-marker\n"
        )
        command = ["docker", "build", "--platform", "linux/amd64", "--tag", reference]
        for key, value in labels(ledger).items():
            command.extend(["--label", f"{key}={value}"])
        subprocess.run([*command, directory], check=True)
        subprocess.run(["docker", "push", reference], check=True)
    resource = records.registry_resources_from_references([("ghcr-member", reference)])[
        0
    ]
    state = None
    for delay in (0, 2, 5):
        time.sleep(delay)
        state = remote.probe(resource)
        if state is not None:
            break
    require(isinstance(state, str), "New GHCR package version is not readable")
    ledger.update(
        ghcr_version_path=state,
        ghcr_version_id=int(state.rsplit("/", 1)[-1]),
        prepared=True,
    )
    write_json(arguments.ledger_dir / f"fixture-{ledger['source_run_id']}.json", ledger)
    baseline(remote, ledger["baseline"])
    print(
        f"FIXTURE_READY tag={tag} run_id={ledger['source_run_id']} package={package}",
        flush=True,
    )


def archive_jsons(remote, artifact, destination):
    raw = remote.request(
        "GET", f"/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"
    )
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "artifact.zip").write_bytes(raw)
    result = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            if not entry.filename.endswith(".json"):
                continue
            require(
                entry.file_size < 8 * 1024 * 1024, "Unexpectedly large evidence JSON"
            )
            name = Path(entry.filename).name
            require(name not in result, "Ambiguous duplicate evidence basename")
            value = json.loads(archive.read(entry))
            result[name] = value
            write_json(destination / name, value)
    return result


def capture(arguments):
    remote = remote_client()
    production = deployed_production(remote, arguments)
    run_id = arguments.source_run_id
    run = remote.read_release_run(run_id)
    require(run is not None, "Capture must start before source Run deletion")
    write_json(arguments.output_dir / "source-at-capture.json", run)
    artifact_name = f"fixture-ledger-run-{run_id}"
    deadline = time.monotonic() + 180
    artifacts = []
    while time.monotonic() < deadline:
        artifacts = [
            a
            for a in remote.list_pages(
                f"/repos/{REPOSITORY}/actions/runs/{run_id}/artifacts", key="artifacts"
            )
            if a["name"] == artifact_name and not a["expired"]
        ]
        if artifacts:
            break
        time.sleep(5)
    require(len(artifacts) == 1, "Source ledger is absent or ambiguous")
    values = archive_jsons(remote, artifacts[0], arguments.output_dir / "source-ledger")
    require(
        list(values) == [f"fixture-{run_id}.json"],
        "Source ledger archive has unexpected shape",
    )
    ledger = next(iter(values.values()))
    require(
        ledger["repository"] == REPOSITORY
        and ledger["batch"] == CONFIG["batch"]
        and ledger["source_run_id"] == run_id
        and ledger["source_sha"] == run["head_sha"]
        and ledger["prepared"] is True
        and fixture_tag(ledger["tag"]),
        "Invalid source ledger binding",
    )
    require(
        ledger["production_sha"] == production["sha"]
        and ledger["production_module_hashes"] == production["module_hashes"],
        "Source used different production modules",
    )
    baseline(remote, ledger["baseline"])
    write_json(arguments.output_dir / "ledger.json", ledger)
    write_json(arguments.output_dir / "production.json", production)
    print(f"LEDGER_CAPTURED source_run_id={run_id} tag={ledger['tag']}", flush=True)


def paths(ledger):
    value = {
        "release": f"/repos/{REPOSITORY}/releases/{ledger['release_id']}",
        "tag": f"/repos/{REPOSITORY}/git/ref/tags/{quote(ledger['tag'], safe='/')}",
        "source_run": f"/repos/{REPOSITORY}/actions/runs/{ledger['source_run_id']}",
    }
    if ledger.get("ghcr_version_path"):
        value["ghcr_version"] = ledger["ghcr_version_path"]
    return value


def readback(remote, ledger, expected):
    from ucm_release.cleanup_remote import RemoteError

    result = {}
    for kind, path in paths(ledger).items():
        try:
            document = remote.json_request("GET", path)
        except RemoteError as error:
            require(
                error.is_missing and expected[kind] == 404,
                f"Unexpected {kind} readback error: {error}",
            )
            status = 404
        else:
            require(expected[kind] == 200, f"{kind} unexpectedly still exists")
            if kind != "tag":
                key = {
                    "release": "release_id",
                    "source_run": "source_run_id",
                    "ghcr_version": "ghcr_version_id",
                }[kind]
                require(document["id"] == ledger[key], f"{kind} ID differs from ledger")
            status = 200
        result[kind] = {"path": path, "status": status}
    return result


def wait_source(remote, arguments, ledger):
    from ucm_release.cleanup_remote import RemoteError

    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        run = remote.read_release_run(ledger["source_run_id"])
        require(
            run is not None, "Source vanished before its terminal evidence was captured"
        )
        if run["status"] == "completed":
            require(
                run["conclusion"] == ledger["expected_conclusion"],
                "Unexpected source conclusion",
            )
            write_json(arguments.output_dir / "source-terminal.json", run)
            try:
                (arguments.output_dir / "source-logs.zip").write_bytes(
                    remote.request(
                        "GET", f"/repos/{REPOSITORY}/actions/runs/{run['id']}/logs"
                    )
                )
            except RemoteError as error:
                write_json(
                    arguments.output_dir / "source-log-read-error.json",
                    {"error": str(error)},
                )
            return run
        time.sleep(2)
    raise RuntimeError("Source did not finish within acceptance timeout")


def exclusive_package(remote, ledger):
    package = f"ucm-cleanup-fixture-{CONFIG['batch']}-run-{ledger['source_run_id']}"
    require(
        ledger.get("package") == package,
        "Package is not this source's disposable fixture",
    )
    path = f"/users/{OWNER}/packages/container/{package}"
    version = ledger["ghcr_version_id"]
    tag = ledger["registry_reference"].rsplit(":", 1)[-1]
    require(
        ledger["ghcr_version_path"] == f"{path}/versions/{version}",
        "Wrong GHCR version identity",
    )
    versions = remote.list_pages(path + "/versions")
    require(
        len(versions) == 1
        and versions[0]["id"] == version
        and set(versions[0]["metadata"]["container"]["tags"]) == {tag},
        "Package is not exclusive",
    )
    config = json.loads(
        subprocess.run(
            ["crane", "config", ledger["registry_reference"]],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    )
    require(
        all(
            config.get("config", {}).get("Labels", {}).get(k) == v
            for k, v in labels(ledger).items()
        ),
        "Image labels do not prove exact fixture ownership",
    )
    return {"path": path, "version_id": version, "tags": [tag]}


def cleanup_report(arguments, ledger, name, partial=False):
    report_path = arguments.output_dir / f"{name}.json"
    call_cleanup(
        [
            "tag",
            "--repository",
            REPOSITORY,
            "--tag",
            ledger["tag"],
            "--report",
            str(report_path),
        ]
    )
    report = json.loads(report_path.read_text())
    require(
        report["status"] == ("partial" if partial else "complete"),
        "Unexpected overall cleanup status",
    )
    require(
        len(report["results"]) == 1 and report["results"][0]["tag"] == ledger["tag"],
        "Unexpected cleanup targets",
    )
    result = report["results"][0]
    require(not result.get("failures"), "GHCR skip became a blocking failure")
    if partial:
        skipped = result.get("skipped", [])
        require(
            result["status"] == "partial"
            and len(skipped) == 1
            and skipped[0]["reference"] == ledger["registry_reference"]
            and "HTTP 400" in skipped[0]["error"],
            "Real last-tagged-version HTTP 400 was not observed",
        )
    else:
        require(
            result["status"] == "deleted" and not result.get("skipped"),
            "Final retry did not close recovery",
        )
    return report


def ghcr_closure(remote, arguments, ledger):
    from ucm_release.cleanup_inventory import ReleaseInventory, collect_catalog
    from ucm_release.cleanup_remote import RemoteError

    require(ledger["source_kind"] == "ghcr", "Wrong GHCR ledger")
    catalog = collect_catalog(remote, release_type="draft")
    matches = [r for r in catalog.records if r["tag"] == ledger["tag"]]
    require(
        len(matches) == 1
        and ledger["tag"] not in catalog.blocked
        and ledger["tag"] not in catalog.active_tags,
        "Fixture is not uniquely discoverable and idle",
    )
    target = ReleaseInventory(remote).resolve_record(matches[0]).target
    require(
        len(target.releases) == 1
        and target.releases[0].identifier == ledger["release_id"]
        and target.releases[0].holds_recovery_data
        and len(target.actions) == 1
        and target.actions[0].identifier == ledger["source_run_id"]
        and not target.actions[0].holds_recovery_data
        and {r.reference for r in target.registry} == {ledger["registry_reference"]},
        "Unexpected resolved resource set",
    )
    docker_login(remote)
    proof = {
        "exclusive_package": exclusive_package(remote, ledger),
        "before": readback(remote, ledger, {kind: 200 for kind in paths(ledger)}),
    }
    write_json(arguments.output_dir / "proof.json", proof)
    partial_state = {"release": 200, "tag": 404, "source_run": 404, "ghcr_version": 200}
    for name in ("first-partial", "rediscovery-partial"):
        deployed_production(remote, arguments)
        proof[name] = cleanup_report(arguments, ledger, name, partial=True)
        proof[name + "-readback"] = readback(remote, ledger, partial_state)
        baseline(remote, ledger["baseline"])
        write_json(arguments.output_dir / "proof.json", proof)
    package = exclusive_package(remote, ledger)
    remote.json_request("DELETE", package["path"])
    try:
        remote.json_request("GET", package["path"])
    except RemoteError as error:
        require(error.is_missing, "Fixture package teardown readback failed")
    else:
        raise RuntimeError("Disposable package still exists")
    proof["package_teardown"] = {**package, "status": 404}
    proof["final_cleanup"] = cleanup_report(arguments, ledger, "final-complete")
    proof["final_readback"] = readback(
        remote, ledger, {kind: 404 for kind in paths(ledger)}
    )
    proof["baseline"] = baseline(remote, ledger["baseline"])
    proof["status"] = "complete"
    write_json(arguments.output_dir / "proof.json", proof)


def manual_run(remote, arguments, ledger, failure):
    workflow = "cleanup-ucm-release.yml"
    endpoint = f"/repos/{REPOSITORY}/actions/workflows/{workflow}/runs"
    recent = endpoint + "?event=workflow_dispatch&per_page=30"
    before = {r["id"] for r in remote.json_request("GET", recent)["workflow_runs"]}
    inputs = {"tag": ledger["tag"]}
    if failure:
        inputs["fail_resource"] = f"{ledger['tag']}#{ledger['release_id']}"
    post_json(
        remote,
        f"/repos/{REPOSITORY}/actions/workflows/{workflow}/dispatches",
        {"ref": "develop", "inputs": inputs},
    )
    deadline = time.monotonic() + 600
    run = None
    while time.monotonic() < deadline:
        candidates = [
            r
            for r in remote.json_request("GET", recent)["workflow_runs"]
            if r["id"] not in before and r["head_sha"] == arguments.production_sha
        ]
        require(
            len(candidates) <= 1, "Concurrent manual cleanup dispatches are ambiguous"
        )
        if candidates:
            run = candidates[0]
            if run["status"] == "completed":
                break
        time.sleep(3)
    require(run and run["status"] == "completed", "Manual cleanup did not finish")
    require(
        run["conclusion"] == ("failure" if failure else "success"),
        "Unexpected manual workflow conclusion",
    )
    name = "manual-first-failure" if failure else "manual-retry-success"
    write_json(arguments.output_dir / f"{name}-run.json", run)
    (arguments.output_dir / f"{name}-logs.zip").write_bytes(
        remote.request("GET", f"/repos/{REPOSITORY}/actions/runs/{run['id']}/logs")
    )
    jobs = remote.list_pages(
        f"/repos/{REPOSITORY}/actions/runs/{run['id']}/jobs", key="jobs"
    )
    require(
        any(j["name"] == f"Cleanup · {ledger['tag']}" for j in jobs),
        "Manual workflow targeted another Tag",
    )
    write_json(arguments.output_dir / f"{name}-jobs.json", jobs)
    artifact_name = f"ucm-tag-cleanup-run-{run['id']}-attempt-{run['run_attempt']}"
    artifacts = remote.list_pages(
        f"/repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts", key="artifacts"
    )
    selected = [a for a in artifacts if a["name"] == artifact_name and not a["expired"]]
    require(
        len(selected) == 1,
        "Actual manual cleanup report artifact is absent or ambiguous",
    )
    reports = archive_jsons(remote, selected[0], arguments.output_dir / name)
    require(
        reports.get("report.json", {}).get("status")
        == ("failed" if failure else "complete"),
        "Actual manual workflow report has the wrong outcome",
    )
    deployed_production(remote, arguments)
    return run


def manual_closure(remote, arguments, ledger):
    require(
        ledger["source_kind"] in {"nightly", "manual-draft"},
        "Manual retry fixture must avoid registry targets",
    )
    proof = {"before": readback(remote, ledger, {kind: 200 for kind in paths(ledger)})}
    proof["first_run"] = manual_run(remote, arguments, ledger, True)
    proof["first_readback"] = readback(
        remote, ledger, {"release": 200, "tag": 404, "source_run": 404}
    )
    baseline(remote, ledger["baseline"])
    write_json(arguments.output_dir / "proof.json", proof)
    proof["retry_run"] = manual_run(remote, arguments, ledger, False)
    proof["final_readback"] = readback(
        remote, ledger, {kind: 404 for kind in paths(ledger)}
    )
    proof["baseline"] = baseline(remote, ledger["baseline"])
    proof["status"] = "complete"
    write_json(arguments.output_dir / "proof.json", proof)


def callback_proof(remote, arguments, ledger):
    deadline = time.monotonic() + 600
    checked = set()
    while time.monotonic() < deadline:
        endpoint = f"/repos/{REPOSITORY}/actions/workflows/nightly-cleanup.yml/runs?event=workflow_run&per_page=30"
        runs = remote.json_request("GET", endpoint)["workflow_runs"]
        for run in runs:
            if (
                run["id"] in checked
                or run["head_sha"] != arguments.production_sha
                or run["status"] != "completed"
            ):
                continue
            artifacts = remote.list_pages(
                f"/repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts",
                key="artifacts",
            )
            expected = (
                f"ucm-nightly-retention-run-{run['id']}-attempt-{run['run_attempt']}"
            )
            matches = [
                a for a in artifacts if a["name"] == expected and not a["expired"]
            ]
            checked.add(run["id"])
            if len(matches) != 1:
                continue
            values = archive_jsons(
                remote, matches[0], arguments.output_dir / f"callback-{run['id']}"
            )
            binding = values.get("code-binding.json", {})
            source = binding.get("source_run", {})
            source_id = source.get(
                "id", binding.get("source_run_id", binding.get("trigger_run_id"))
            )
            if source_id != ledger["source_run_id"]:
                continue
            require(
                source.get("run_attempt") == ledger["source_attempt"]
                and source.get("head_sha") == ledger["source_sha"]
                and source.get("path", "").split("@", 1)[0] == ledger["source_workflow"]
                and source.get("conclusion") == ledger["expected_conclusion"],
                "Callback source attempt, SHA, path or conclusion differs from ledger",
            )
            sha = binding.get(
                "implementation_sha",
                binding.get("production_sha", binding.get("checkout_sha")),
            )
            require(
                sha == arguments.production_sha
                and binding.get("module_hashes") == ledger["production_module_hashes"],
                "Callback actual checkout/modules differ from deployed production",
            )
            require(run["conclusion"] == "success", "Matched callback failed")
            write_json(arguments.output_dir / "callback-run.json", run)
            return values, binding
        time.sleep(5)
    raise RuntimeError("No exact source-bound deployed callback evidence was found")


def retention_closure(remote, arguments, ledger):
    require(ledger["source_kind"] == "nightly", "Wrong retention ledger")
    values, binding = callback_proof(remote, arguments, ledger)
    report = values.get("report.json")
    require(isinstance(report, dict), "Callback has no retention report")
    matches = [r for r in report.get("results", []) if r["tag"] == ledger["tag"]]
    expected = "kept" if arguments.scenario == "kept" else "deleted"
    require(
        len(matches) == 1 and matches[0]["status"] == expected,
        f"Fixture was not {expected} by actual retention",
    )
    state = {kind: (200 if expected == "kept" else 404) for kind in paths(ledger)}
    proof = {
        "callback_binding": binding,
        "retention": report,
        "readback": readback(remote, ledger, state),
        "baseline": baseline(remote, ledger["baseline"]),
        "status": "complete",
    }
    write_json(arguments.output_dir / "proof.json", proof)


def observe(arguments):
    remote = remote_client()
    ledger = json.loads((arguments.output_dir / "ledger.json").read_text())
    production = deployed_production(remote, arguments)
    require(
        production["module_hashes"] == ledger["production_module_hashes"],
        "Production modules changed",
    )
    wait_source(remote, arguments, ledger)
    baseline(remote, ledger["baseline"])
    if arguments.scenario == "ghcr":
        ghcr_closure(remote, arguments, ledger)
    elif arguments.scenario == "manual":
        manual_closure(remote, arguments, ledger)
    else:
        retention_closure(remote, arguments, ledger)


def history_preview(arguments):
    from ucm_release import version_config
    from ucm_release.cleanup_inventory import ReleaseInventory, collect_catalog
    from ucm_release.cleanup_records import CleanupError, Resource

    require(
        arguments.tag in CONFIG["history_preview_tags"],
        "History preview permits only the four reviewed historical Tags",
    )
    remote = remote_client()
    production = deployed_production(remote, arguments)
    protected = baseline(remote)
    write_json(arguments.output_dir / "production.json", production)
    write_json(arguments.output_dir / "baseline-before.json", protected)
    try:
        release_type = version_config.classify_tag(arguments.tag)["release_type"]
        catalog = collect_catalog(remote, release_type=release_type)
        matches = [
            record
            for record in (*catalog.records, *catalog.orphans)
            if record["tag"] == arguments.tag
        ]
        require(
            len(matches) == 1
            and arguments.tag not in catalog.active_tags
            and arguments.tag not in catalog.blocked,
            "Historical target is not uniquely proven and idle",
        )
        target = ReleaseInventory(remote).resolve_record(matches[0]).target
        probes = []
        for resource in (
            *target.registry,
            *target.actions,
            *target.releases,
            Resource("git-tag", arguments.tag, arguments.tag),
        ):
            row = {
                "kind": resource.kind,
                "reference": resource.reference,
                "identifier": resource.identifier,
            }
            try:
                state = remote.probe(resource)
            except (CleanupError, OSError) as error:
                row.update(presence="error", state=None, error=str(error))
            else:
                row.update(
                    presence="absent" if state is None else "present", state=state
                )
                if state is not None and resource.kind in {
                    "ghcr-member",
                    "ghcr-index",
                    "chart-oci",
                }:
                    row["ghcr_version_id"] = int(state.rsplit("/", 1)[-1])
                if state is not None and resource.kind.startswith("dockerhub-"):
                    row["dockerhub_digest"] = state
            probes.append(row)
            write_json(
                arguments.output_dir / "history-resource-probes.json",
                {
                    "repository": REPOSITORY,
                    "tag": arguments.tag,
                    "production_sha": production["sha"],
                    "read_only": True,
                    "registry_planned": len(target.registry),
                    "registry_presence": {
                        status: sum(
                            row["presence"] == status
                            for row in probes
                            if row["kind"]
                            in {
                                "ghcr-member",
                                "ghcr-index",
                                "chart-oci",
                                "dockerhub-member",
                                "dockerhub-index",
                            }
                        )
                        for status in ("present", "absent", "error")
                    },
                    "resources": probes,
                },
            )
        call_cleanup(
            [
                "tag",
                "--repository",
                REPOSITORY,
                "--tag",
                arguments.tag,
                "--dry-run",
                "--report",
                str(arguments.output_dir / "history-preview.json"),
            ]
        )
    finally:
        write_json(
            arguments.output_dir / "baseline-after.json", baseline(remote, protected)
        )
        deployed_production(remote, arguments)


def main():
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "prepare-nightly",
        "prepare-ghcr",
        "prepare-draft",
        "capture",
        "observe",
        "history-preview",
    ):
        command = commands.add_parser(name)
        command.add_argument("--production-root", type=Path, default=Path.cwd())
        command.add_argument("--production-sha", required=True)
        command.add_argument(
            "--config",
            type=Path,
            default=Path(__file__).with_name("acceptance-config.json"),
        )
        command.add_argument("--output-dir", type=Path, default=Path("out/hosted"))
        if name.startswith("prepare"):
            command.add_argument("--ledger-dir", type=Path, default=Path("out/ledger"))
            command.add_argument("--open-dir", type=Path, default=Path("out/open"))
            command.add_argument("--plan-dir", type=Path, default=Path("out/plan"))
        if name == "prepare-nightly":
            command.add_argument(
                "--role", choices=("filler", "victim", "manual"), required=True
            )
            command.add_argument(
                "--outcome", choices=("success", "failure", "cancelled"), required=True
            )
        if name == "capture":
            command.add_argument("--source-run-id", type=int, required=True)
        if name == "observe":
            command.add_argument(
                "--scenario",
                choices=("ghcr", "manual", "retention", "kept"),
                required=True,
            )
        if name == "history-preview":
            command.add_argument("--tag", required=True)
    arguments = parser.parse_args()
    global CONFIG
    CONFIG = json.loads(arguments.config.read_text())
    require(CONFIG["repository"] == REPOSITORY, "Wrong acceptance configuration")
    arguments.production_root = arguments.production_root.resolve()
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(arguments.production_root / ".github/release"))
    operations = {
        "prepare-nightly": prepare_nightly,
        "prepare-ghcr": prepare_ghcr,
        "prepare-draft": prepare_draft,
        "capture": capture,
        "observe": observe,
        "history-preview": history_preview,
    }
    try:
        operations[arguments.command](arguments)
    except Exception as error:
        write_json(
            arguments.output_dir / "acceptance-error.json",
            {"command": arguments.command, "error": str(error)},
        )
        raise


if __name__ == "__main__":
    main()
