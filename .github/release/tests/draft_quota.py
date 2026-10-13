"""Hosted Draft quota proof using seven exact disposable Fork source Tags.

The failed victim stops after production cleanup artifacts and before a Release
exists. Draft ordering uses Release IDs, so its genuine Tag-only record comes
before the protected live Draft. No inventory or identity is fabricated.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

CONFIG = json.loads(Path(__file__).with_name("draft-quota-config.json").read_text())
OUTPUT = Path("out/draft-quota")


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def load_acceptance(production_root):
    sys.path.insert(0, str(production_root / ".github/release"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hosted_cleanup as acceptance

    # The reused helper initializes CONFIG only in its own CLI entrypoint.
    # Importers must load its frozen configuration before calling its API.
    acceptance.CONFIG = json.loads(
        Path(__file__).with_name("acceptance-config.json").read_text()
    )
    require(
        acceptance.CONFIG["production_sha"] == CONFIG["production_sha"]
        and len(acceptance.CONFIG["protected_baseline"]) == 8,
        "Reused helper production binding or frozen baseline differs",
    )
    return acceptance


def write(name, value):
    path = OUTPUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def protected(remote, acceptance):
    baseline = acceptance.baseline(remote)
    normal = {}
    for tag, identity in CONFIG["normal_releases"].items():
        release = remote.json_request(
            "GET", f"/repos/{remote.repository}/releases/{identity['release_id']}"
        )
        ref = remote.json_request(
            "GET", f"/repos/{remote.repository}/git/ref/tags/{quote(tag, safe='/')}"
        )
        require(
            release["tag_name"] == tag and ref["object"] == identity["ref_object"],
            "A current normal publication identity changed",
        )
        normal[tag] = {
            "release_id": release["id"],
            "ref_object": ref["object"],
            "body_sha256": hashlib.sha256(
                (release.get("body") or "").encode()
            ).hexdigest(),
            "assets": [
                {k: a.get(k) for k in ("id", "name", "size", "digest")}
                for a in release["assets"]
            ],
        }
    return {"frozen_baseline": baseline, "normal_publications": normal}


def request_state(remote, path):
    from ucm_release.cleanup_remote import RemoteError

    try:
        document = remote.json_request("GET", path)
    except RemoteError as error:
        require(error.is_missing, f"Readback did not prove absence: {path}: {error}")
        return {"path": path, "status": 404}
    return {"path": path, "status": 200, "id": document.get("id"), "document": document}


def catalog_selection(remote):
    from ucm_release import cleanup_records, policy
    from ucm_release.cleanup_inventory import collect_catalog

    profile = policy.resolve(release_type="draft")
    require(
        profile["release_profile"]["max_count"] == CONFIG["max_count"] == 7,
        "Production Draft quota changed",
    )
    require(
        profile["publish"]["pypi"]["enabled"] is False,
        "PyPI-enabled retention is outside this proof",
    )
    catalog = collect_catalog(remote, release_type="draft")
    require(
        not catalog.orphans and not catalog.blocked,
        "Unowned orphan or blocked record would alter the retention scope",
    )
    selection = cleanup_records.select_retention(
        catalog.records,
        7,
        catalog.active_tags,
        release_type="draft",
        current_tag=CONFIG["current_tag"],
        pypi_enabled=False,
    )
    return catalog, selection, profile


def prepare(arguments, remote, acceptance, production):
    from ucm_release import version_config

    tag = os.environ["GITHUB_REF_NAME"]
    require(
        tag in CONFIG["fixture_tags"]
        and os.environ["GITHUB_REF"] == f"refs/tags/{tag}",
        "Source Tag is outside the exact quota allowlist",
    )
    acceptance.CONFIG["manual_draft_tag"] = tag
    ledger = acceptance.source_ledger(remote, arguments, tag, "manual-draft")
    victim = tag == CONFIG["victim_tag"]
    ledger.update(
        source_kind="draft-quota",
        role="tag-only-failed-victim" if victim else "filler",
        expected_conclusion="failure" if victim else "success",
        release_id=None,
        quota_max_count=7,
    )
    coordinates = version_config.classify_tag(tag)
    plan = {
        "kind": "ucm-release-plan",
        "route": "release",
        "repository": remote.repository,
        "git_tag": tag,
        "release_type": "draft",
        "version": coordinates["version"],
        "image_version": coordinates["image_version"],
        "families": [],
        "publish": {"chart_oci": {"enabled": False}},
    }
    if victim:
        require(
            request_state(
                remote,
                f"/repos/{remote.repository}/releases/tags/{quote(tag, safe='/')}",
            )["status"]
            == 404,
            "Tag-only victim already has a Release",
        )
        identity = [
            "record",
            "--repository",
            remote.repository,
            "--tag",
            tag,
            "--source-sha",
            ledger["source_sha"],
            "--run-id",
            str(ledger["source_run_id"]),
            "--run-attempt",
            str(ledger["source_attempt"]),
        ]
        acceptance.call_cleanup([*identity, "--output-dir", str(arguments.open_dir)])
        plan_path = arguments.ledger_dir.parent / "input/release-plan.json"
        acceptance.write_json(plan_path, plan)
        acceptance.call_cleanup(
            [
                *identity,
                "--plan",
                str(plan_path),
                "--output-dir",
                str(arguments.plan_dir),
            ]
        )
    else:
        acceptance.release_and_records(remote, arguments, ledger, plan)
    ledger["prepared"] = True
    acceptance.write_json(
        arguments.ledger_dir / f"fixture-{ledger['source_run_id']}.json", ledger
    )
    write(
        "source-code-binding.json",
        {
            "production": production,
            "source_run_id": ledger["source_run_id"],
            "source_sha": ledger["source_sha"],
            "tag": tag,
            "role": ledger["role"],
        },
    )
    protected(remote, acceptance)
    print(tag, ledger["role"], ledger["release_id"])


def capture(arguments, remote, acceptance, production):
    ids = [int(value) for value in arguments.source_run_ids.split(",")]
    require(
        len(ids) == len(set(ids)) and len(ids) in {6, 7},
        "Capture requires six or seven distinct source Run IDs",
    )
    if len(ids) == 6:
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            runs = remote.list_pages(
                f"/repos/{remote.repository}/actions/workflows/release-tag.yml/runs",
                key="workflow_runs",
            )
            matches = [
                run
                for run in runs
                if run["head_branch"] == CONFIG["current_tag"]
                and run["head_sha"] == arguments.helper_sha
                and run["event"] == "push"
            ]
            require(len(matches) <= 1, "Ambiguous seventh quota source")
            if matches:
                ids.append(matches[0]["id"])
                break
            time.sleep(5)
        require(
            len(ids) == 7 and len(set(ids)) == 7,
            "The seventh source did not arrive before the explicit lock deadline",
        )
    sources = {}
    for run_id in ids:
        run = remote.read_release_run(run_id)
        require(
            run is not None
            and run["head_branch"] in CONFIG["fixture_tags"]
            and run["head_sha"] == arguments.helper_sha
            and run["path"].split("@", 1)[0] == ".github/workflows/release-tag.yml"
            and run["event"] == "push",
            "Source does not bind the immutable quota Tag workflow",
        )
        tag = run["head_branch"]
        require(tag not in sources, "Duplicate quota source Tag")
        acceptance.CONFIG["manual_draft_tag"] = tag
        directory = OUTPUT / "sources" / str(run_id)
        source_args = SimpleNamespace(
            production_root=arguments.production_root,
            production_sha=arguments.production_sha,
            source_run_id=run_id,
            output_dir=directory,
        )
        acceptance.capture(source_args)
        ledger = json.loads((directory / "ledger.json").read_text())
        acceptance.wait_source(remote, source_args, ledger)
        logs = directory / "source-logs.zip"
        require(logs.exists(), "Source logs were not archived")
        with zipfile.ZipFile(logs) as archive:
            require(archive.testzip() is None, "Source logs failed CRC validation")
        selected = 0
        for artifact in remote.list_pages(
            f"/repos/{remote.repository}/actions/runs/{run_id}/artifacts",
            key="artifacts",
        ):
            if artifact["name"] in {
                f"ucm-release-cleanup-open-run-{run_id}-attempt-1",
                f"ucm-release-cleanup-plan-run-{run_id}-attempt-1",
            }:
                acceptance.archive_jsons(remote, artifact, directory / artifact["name"])
                selected += 1
        require(
            selected == 2, "Both real production recovery artifacts must be archived"
        )
        if ledger["release_id"] is not None:
            release = remote.json_request(
                "GET", f"/repos/{remote.repository}/releases/{ledger['release_id']}"
            )
            ledger["asset_ids"] = [asset["id"] for asset in release["assets"]]
        else:
            ledger["asset_ids"] = []
        ledger["source_logs_sha256"] = hashlib.sha256(logs.read_bytes()).hexdigest()
        sources[tag] = ledger
    require(
        set(sources) == set(CONFIG["fixture_tags"]),
        "Source batch differs from the reviewed quota allowlist",
    )
    catalog, selection, profile = catalog_selection(remote)
    expected_tags = set(CONFIG["fixture_tags"]) | {"draft/v0.8.2-1"}
    require(
        {record["tag"] for record in catalog.records} == expected_tags
        and len(catalog.records) == 8,
        "The actual Draft catalog does not have exactly eight expected versions",
    )
    require(
        len(selection.candidates) == 1
        and selection.candidates[0]["tag"] == CONFIG["victim_tag"]
        and selection.candidates[0]["release_ids"] == [],
        "Quota did not select the sole own Tag-only failed victim",
    )
    require(
        not selection.deferred and len(selection.kept) == 7,
        "Unexpected quota selection or active deletion candidate",
    )
    state = request_state(
        remote,
        f"/repos/{remote.repository}/releases/tags/{quote(CONFIG['victim_tag'], safe='/')}",
    )
    require(state["status"] == 404, "Victim unexpectedly has a Release")
    write(
        "quota-before.json",
        {
            "production": production,
            "profile": profile["release_profile"],
            "sources": sources,
            "records": catalog.records,
            "active_tags": sorted(catalog.active_tags),
            "candidate_tags": [record["tag"] for record in selection.candidates],
            "kept_tags": [record["tag"] for record in selection.kept],
            "victim_release_before": state,
            "protected": protected(remote, acceptance),
        },
    )
    print("Validated real catalog 8 -> sole own failed Tag-only candidate; seven kept")


def archived_before(remote, run_id, artifact_name):
    artifacts = remote.list_pages(
        f"/repos/{remote.repository}/actions/runs/{run_id}/artifacts", key="artifacts"
    )
    selected = [
        artifact
        for artifact in artifacts
        if artifact["name"] == artifact_name and not artifact["expired"]
    ]
    require(len(selected) == 1, "Independent evidence artifact is absent or ambiguous")
    raw = remote.request(
        "GET", f"/repos/{remote.repository}/actions/artifacts/{selected[0]['id']}/zip"
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(
            archive.testzip() is None,
            "Independent evidence artifact failed CRC validation",
        )
        value = json.loads(archive.read("quota-before.json"))
    return value, {
        "artifact_id": selected[0]["id"],
        "artifact_name": artifact_name,
        "zip_sha256": hashlib.sha256(raw).hexdigest(),
        "crc_valid": True,
    }


def archive_preparation(arguments, remote, acceptance, production):
    evidence = []
    prefix = f"/repos/{remote.repository}"
    for identifier, expected in CONFIG["failed_preparations"].items():
        run_id = int(identifier)
        run = remote.read_release_run(run_id)
        require(
            run
            and run["id"] == run_id
            and run["head_branch"] == expected["tag"]
            and run["head_sha"] == expected["source_sha"]
            and run["path"].split("@", 1)[0] == ".github/workflows/release-tag.yml"
            and run["event"] == "push"
            and run["status"] == "completed"
            and run["conclusion"] == "failure"
            and run["actor"]["login"] == "SuperMarioYL",
            "Preparation failure source binding differs",
        )
        release = request_state(
            remote, f"{prefix}/releases/tags/{quote(expected['tag'], safe='/')}"
        )
        tag = request_state(
            remote, f"{prefix}/git/ref/tags/{quote(expected['tag'], safe='/')}"
        )
        artifacts = remote.list_pages(
            f"{prefix}/actions/runs/{run_id}/artifacts", key="artifacts"
        )
        require(
            release["status"] == 404 and not artifacts,
            "Source has publication resources or recovery artifacts",
        )
        require(
            tag["status"] == 200
            and tag["document"]["object"]
            == {
                "type": "commit",
                "sha": expected["source_sha"],
                "url": f"https://api.github.com/repos/{remote.repository}/git/commits/{expected['source_sha']}",
            },
            "Preparation Tag moved",
        )
        raw = remote.request("GET", f"{prefix}/actions/runs/{run_id}/logs")
        directory = OUTPUT / "preparation-failures" / str(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source-logs.zip").write_bytes(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            require(archive.testzip() is None, "Preparation logs failed CRC validation")
            matches = [
                name
                for name in archive.namelist()
                if b"KeyError: 'production_sha'" in archive.read(name)
            ]
        require(
            matches, "Logs do not identify the observed harness initialization failure"
        )
        write(f"preparation-failures/{run_id}/source.json", run)
        write(
            f"preparation-failures/{run_id}/jobs.json",
            remote.json_request("GET", f"{prefix}/actions/runs/{run_id}/jobs"),
        )
        evidence.append(
            {
                "source_run_id": run_id,
                **expected,
                "release": release,
                "tag": tag,
                "artifact_count": 0,
                "logs_sha256": hashlib.sha256(raw).hexdigest(),
                "logs_crc_valid": True,
                "fault_log_entries": matches,
            }
        )
    write(
        "preparation-failures.json",
        {
            "status": "complete",
            "production": production,
            "cases": evidence,
            "protected": protected(remote, acceptance),
            "evidence_boundary": "Harness initialization failed before publication resources; only exact own control Tag/Run teardown is authorized. This is not P2 release-cleanup acceptance.",
        },
    )


def apply(arguments, remote, acceptance, production):
    from ucm_release import cleanup

    run_id = int(os.environ["GITHUB_RUN_ID"])
    before, archive = archived_before(
        remote, run_id, f"draft-quota-capture-run-{run_id}"
    )
    require(
        before == json.loads((OUTPUT / "quota-before.json").read_text())
        and before["production"] == production,
        "Uploaded capture differs from the bound mutation input",
    )
    catalog, selection, _ = catalog_selection(remote)
    require(
        len(catalog.records) == 8
        and [record["tag"] for record in selection.candidates] == [CONFIG["victim_tag"]]
        and not selection.deferred,
        "Quota changed after evidence upload; do not mutate",
    )
    write("capture-artifact-readback.json", archive)
    exit_code = cleanup.main(
        [
            "retention",
            "--repository",
            remote.repository,
            "--current-tag",
            CONFIG["current_tag"],
            "--release-type",
            "draft",
            "--max-count",
            "7",
            "--pypi-enabled",
            "false",
            "--report",
            str(OUTPUT / "production-retention-report.json"),
        ]
    )
    require(exit_code == 0, f"Production retention exited {exit_code}")
    report = json.loads((OUTPUT / "production-retention-report.json").read_text())
    deleted = [result for result in report["results"] if result["status"] == "deleted"]
    require(
        report["status"] == "complete"
        and len(deleted) == 1
        and deleted[0]["tag"] == CONFIG["victim_tag"]
        and deleted[0]["release_ids"] == [],
        "Production report did not prove the exact own Tag-only deletion",
    )
    victim = before["sources"][CONFIG["victim_tag"]]
    require(
        deleted[0]["run_ids"] == [victim["source_run_id"]]
        and deleted[0]["registry_references"] == 0,
        "Deleted source or registry scope differs from the fixture ledger",
    )
    prefix = f"/repos/{remote.repository}"
    states = [
        request_state(remote, path)
        for path in [
            f"{prefix}/git/ref/tags/{quote(victim['tag'], safe='/')}",
            f"{prefix}/actions/runs/{victim['source_run_id']}",
            f"{prefix}/releases/tags/{quote(victim['tag'], safe='/')}",
        ]
    ]
    require(
        all(state["status"] == 404 for state in states),
        "Exact quota victim readback did not prove absence",
    )
    after_catalog, _, _ = catalog_selection(remote)
    require(
        len(after_catalog.records) == 7
        and {record["tag"] for record in after_catalog.records}
        == set(before["kept_tags"]),
        "Actual Draft count did not become the exact seven kept versions",
    )
    write(
        "quota-after.json",
        {
            "status": "complete",
            "production": production,
            "entrypoint": "ucm_release.cleanup.main retention",
            "max_count": 7,
            "actual_count_before": 8,
            "actual_count_after": 7,
            "victim_source_conclusion": "failure",
            "victim_release_was_never_created": True,
            "exact_readbacks": states,
            "kept_tags": before["kept_tags"],
            "protected": protected(remote, acceptance),
        },
    )


def readback(arguments, remote, acceptance, production):
    run_id = arguments.quota_run_id
    source_run = remote.read_release_run(run_id)
    require(
        source_run
        and source_run["head_sha"] == arguments.helper_sha
        and source_run["conclusion"] == "success",
        "Quota observer binding or conclusion differs",
    )
    before, archive = archived_before(
        remote, run_id, f"draft-quota-run-{run_id}-attempt-1"
    )
    require(
        before["production"] == production,
        "Teardown readback names another production implementation",
    )
    prefix = f"/repos/{remote.repository}"
    result = []
    for tag, ledger in before["sources"].items():
        require(
            tag in CONFIG["fixture_tags"]
            and ledger["source_sha"] == arguments.helper_sha,
            "Teardown source is outside the frozen quota allowlist",
        )
        paths = [
            f"{prefix}/git/ref/tags/{quote(tag, safe='/')}",
            f"{prefix}/actions/runs/{ledger['source_run_id']}",
        ]
        paths += (
            [f"{prefix}/releases/{ledger['release_id']}"]
            if ledger["release_id"] is not None
            else [f"{prefix}/releases/tags/{quote(tag, safe='/')}"]
        )
        paths += [
            f"{prefix}/releases/assets/{identifier}"
            for identifier in ledger["asset_ids"]
        ]
        states = [request_state(remote, path) for path in paths]
        require(
            all(state["status"] == 404 for state in states),
            "An exact own fixture still exists or absence could not be proved",
        )
        result.append(
            {
                "tag": tag,
                "release_id": ledger["release_id"],
                "source_run_id": ledger["source_run_id"],
                "resources": states,
            }
        )
    catalog, _, _ = catalog_selection(remote)
    require(
        len(catalog.records) == 1 and catalog.records[0]["tag"] == "draft/v0.8.2-1",
        "Own Draft fixtures remain in the actual catalog",
    )
    write(
        "draft-quota-complete.json",
        {
            "status": "complete",
            "production": production,
            "quota_run_id": run_id,
            "archived_quota_evidence": archive,
            "fixture_readbacks": result,
            "actual_draft_count_after_teardown": 1,
            "protected": protected(remote, acceptance),
            "evidence_boundary": "Quota deleted a failed Tag-only version; no Release ID was allocated. Actual Draft Release-ID deletion is independently proved by the real CMake failure case.",
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "prepare",
            "binding",
            "capture",
            "apply",
            "readback",
            "archive-preparation",
        ),
    )
    parser.add_argument("--production-root", type=Path, default=Path.cwd())
    parser.add_argument("--production-sha", required=True)
    parser.add_argument("--helper-sha")
    parser.add_argument("--source-run-ids")
    parser.add_argument("--quota-run-id", type=int)
    arguments = parser.parse_args()
    arguments.production_root = arguments.production_root.resolve()
    arguments.open_dir = Path("out/open")
    arguments.plan_dir = Path("out/plan")
    arguments.ledger_dir = Path("out/ledger")
    acceptance = load_acceptance(arguments.production_root)

    require(
        arguments.production_sha == CONFIG["production_sha"],
        "Wrong fixed production SHA",
    )
    helper_sha = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parents[3]), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    require(
        helper_sha == os.environ["GITHUB_SHA"],
        "Calling workflow and helper code differ",
    )
    if arguments.operation != "prepare":
        require(
            arguments.helper_sha == helper_sha,
            "Observer must use the full immutable helper SHA",
        )
    remote = acceptance.remote_client()
    production = acceptance.deployed_production(remote, arguments)
    write(
        "code-binding.json",
        {
            "repository": remote.repository,
            "production": production,
            "helper_sha": helper_sha,
            "observer_run_id": int(os.environ["GITHUB_RUN_ID"]),
            "entrypoint": "ucm_release.cleanup.main",
        },
    )
    if arguments.operation == "binding":
        protected(remote, acceptance)
        print("DRAFT_RETENTION_LOCK_BOUND")
        return
    {
        "prepare": prepare,
        "capture": capture,
        "apply": apply,
        "readback": readback,
        "archive-preparation": archive_preparation,
    }[arguments.operation](arguments, remote, acceptance, production)


if __name__ == "__main__":
    main()
