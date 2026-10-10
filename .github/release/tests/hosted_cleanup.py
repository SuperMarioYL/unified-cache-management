"""Exercise nonfatal GHCR deletion against one disposable Fork fixture.

Both preparation and cleanup import the deployed develop production modules.
The helper alone tears down its exclusive test package after proving production
cleanup returned partial, retained recovery evidence, and deleted the Tag/run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

REPOSITORY = "SuperMarioYL/unified-cache-management"
DRAFT_TAGS = {"draft/v0.8.0-9101"}
BASELINE_TAGS = (
    "v0.8.0rc2",
    "nightly/v0.8.0-20260918-1",
    "nightly/v0.8.0-20260918-2",
    "v0.7.0rc17",
    "v0.7.0rc16",
    "v0.7.0rc15",
    "v0.7.0rc14",
    "v0.7.0rc13",
    "v0.7.0rc12",
    "v0.7.0rc11",
    "v0.7.0rc10",
)

MODULES = (
    "cleanup.py",
    "cleanup_records.py",
    "cleanup_inventory.py",
    "cleanup_remote.py",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_json(path, value):
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

    require(
        os.environ.get("GITHUB_ACTIONS") == "true",
        "This helper only runs in GitHub Actions",
    )
    require(
        os.environ.get("GITHUB_REPOSITORY") == REPOSITORY,
        "Only the explicitly authorized Fork is allowed",
    )
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    require(bool(token), "A job GITHUB_TOKEN is required")
    return ProductionRemote(REPOSITORY, token)


def get_json(remote, path):
    return remote.json_request("GET", path)


def baseline(remote):
    result = {}
    for tag in BASELINE_TAGS:
        release = get_json(
            remote, f"/repos/{REPOSITORY}/releases/tags/{quote(tag, safe='')}"
        )
        ref = get_json(
            remote, f"/repos/{REPOSITORY}/git/ref/tags/{quote(tag, safe='/')}"
        )
        result[tag] = {
            "release_id": release["id"],
            "tag_name": release["tag_name"],
            "assets": sorted(
                [
                    {"id": asset["id"], "name": asset["name"]}
                    for asset in release["assets"]
                ],
                key=lambda asset: asset["id"],
            ),
            "ref_object": ref["object"],
        }
    return result


def call_cleanup(arguments, expected=0):
    from ucm_release import cleanup

    status = cleanup.main(arguments)
    require(
        status == expected,
        f"cleanup returned {status}, expected {expected}: {arguments}",
    )


def prepare(arguments):
    from ucm_release import cleanup_records as records
    from ucm_release import runtime, version_config

    remote = remote_client()
    production = deployed_production(remote, arguments.production_root)
    tag = arguments.tag
    require(
        tag in DRAFT_TAGS,
        "Only the authorized disposable Draft fixture Tag is allowed",
    )
    require(
        os.environ.get("GITHUB_EVENT_NAME") == "push",
        "Prepare requires the genuine Tag push event",
    )
    require(
        os.environ.get("GITHUB_REF") == f"refs/tags/{tag}",
        "Fixture Tag differs from the current event",
    )
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    source_sha = os.environ["GITHUB_SHA"]
    run = remote.read_release_run(run_id)
    require(run is not None, "Fixture source run is missing")
    require(
        run["path"].split("@", 1)[0] == ".github/workflows/release-tag.yml"
        and run.get("event") == "push"
        and run.get("head_branch") == tag
        and run.get("head_sha") == source_sha,
        "Fixture must be produced by this Fork's exact release-tag.yml push and SHA",
    )
    require(
        not any(release["tag_name"] == tag for release in remote.list_releases()),
        "Fixture Release already exists; refusing to overwrite it",
    )
    original_baseline = baseline(remote)
    version = str(version_config.classify_tag(tag)["version"])
    package = f"ucm-cleanup-fixture-{tag.rsplit('-', 1)[-1]}-run-{run_id}"
    image_tag = f"fixture-ucm-{runtime.oci_tag_version(version)}"
    reference = f"ghcr.io/supermarioyl/{package}:{image_tag}"
    payload = {
        "tag_name": tag,
        "name": tag,
        "draft": True,
        "prerelease": True,
        "body": f"Hosted cleanup fixture for Actions run {run_id}; contains no distributable UCM build.",
    }
    release = json.loads(
        remote.request(
            "POST", f"/repos/{REPOSITORY}/releases", data=json.dumps(payload).encode()
        )
    )
    release_id = release["id"]
    ledger = {
        "kind": "ucm-hosted-cleanup-fixture",
        "repository": REPOSITORY,
        "tag": tag,
        "release_id": release_id,
        "source_run_id": run_id,
        "source_attempt": attempt,
        "source_sha": source_sha,
        "registry_reference": reference,
        "ghcr_version_path": None,
        "ghcr_version_id": None,
        "production_module_hashes": production["module_hashes"],
        "production_sha": production["sha"],
        "baseline": original_baseline,
        "prepared": False,
    }
    ledger_path = arguments.ledger_dir / f"fixture-{run_id}.json"
    write_json(ledger_path, ledger)
    identity = [
        "record-targets",
        "--repository",
        REPOSITORY,
        "--tag",
        tag,
        "--release-type",
        "draft",
        "--run-id",
        str(run_id),
        "--run-attempt",
        str(attempt),
        "--source-sha",
        source_sha,
        "--release-id",
        str(release_id),
    ]
    call_cleanup([*identity, "--output-dir", str(arguments.open_dir)])
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
    plan_path = arguments.ledger_dir.parent / "input" / "release-plan.json"
    write_json(plan_path, plan)
    call_cleanup(
        [*identity, "--plan", str(plan_path), "--output-dir", str(arguments.plan_dir)]
    )
    subprocess.run(
        [
            "docker",
            "login",
            "ghcr.io",
            "--username",
            "SuperMarioYL",
            "--password-stdin",
        ],
        input=remote.token + "\n",
        text=True,
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="ucm-hosted-cleanup-") as directory:
        context = Path(directory)
        (context / "marker").write_text(f"{tag}\n{run_id}\n{source_sha}\n")
        (context / "Dockerfile").write_text(
            f'FROM scratch\nLABEL org.opencontainers.image.source="https://github.com/{REPOSITORY}"\nCOPY marker /fixture-marker\n'
        )
        command = ["docker", "build", "--platform", "linux/amd64", "--tag", reference]
        for key, value in fixture_labels(ledger).items():
            command.extend(["--label", f"{key}={value}"])
        subprocess.run([*command, directory], check=True)
        subprocess.run(["docker", "push", reference], check=True)
    resource = records.registry_resources_from_references([("ghcr-member", reference)])[
        0
    ]
    state = None
    # GHCR publication is complete only once its new package version is readable.
    for delay in (0, 2, 5):
        if delay:
            time.sleep(delay)
        state = remote_client().probe(resource)
        if state is not None:
            break
    require(
        isinstance(state, str), "Published fixture has no readable GHCR package version"
    )
    ledger["ghcr_version_path"] = state
    ledger["ghcr_version_id"] = int(state.rsplit("/", 1)[-1])
    ledger["prepared"] = True
    write_json(ledger_path, ledger)
    require(
        baseline(remote) == original_baseline,
        "Prepare changed a protected baseline",
    )
    print(
        json.dumps(
            {
                "tag": tag,
                "source_run_id": run_id,
                "release_id": release_id,
                "ghcr_version_id": ledger["ghcr_version_id"],
                "baseline_count": len(original_baseline),
                "production_sha": production["sha"],
            },
            sort_keys=True,
        )
    )


def fixture_paths(ledger):
    return {
        "release": f"/repos/{REPOSITORY}/releases/{ledger['release_id']}",
        "tag": f"/repos/{REPOSITORY}/git/ref/tags/{quote(ledger['tag'], safe='/')}",
        "source_run": f"/repos/{REPOSITORY}/actions/runs/{ledger['source_run_id']}",
        "ghcr_version": ledger["ghcr_version_path"],
    }


def readback(remote, ledger, expected):
    from ucm_release.cleanup_remote import RemoteError

    readings = {}
    for kind, path in fixture_paths(ledger).items():
        try:
            value = get_json(remote, path)
        except RemoteError as error:
            require(
                expected[kind] == 404 and error.is_missing,
                f"Unexpected readback error for {kind}: {error}",
            )
            readings[kind] = {"path": path, "status": 404}
        else:
            require(
                expected[kind] == 200, f"Fixture unexpectedly remains: {kind} {path}"
            )
            if kind in {"release", "source_run", "ghcr_version"}:
                expected_id = {
                    "release": ledger["release_id"],
                    "source_run": ledger["source_run_id"],
                    "ghcr_version": ledger["ghcr_version_id"],
                }[kind]
                require(
                    value.get("id") == expected_id,
                    f"Readback {kind} ID differs from ledger",
                )
            readings[kind] = {"path": path, "status": 200}
    return readings


def deployed_production(remote, root):
    repository = get_json(remote, f"/repos/{REPOSITORY}")
    require(
        repository["default_branch"] == "develop",
        "Production must be the Fork's default develop branch",
    )
    expected = get_json(remote, f"/repos/{REPOSITORY}/git/ref/heads/develop")["object"][
        "sha"
    ]
    actual = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    require(
        actual == expected,
        "Production checkout is not the currently deployed develop commit",
    )
    return {"branch": "develop", "sha": actual, "module_hashes": module_hashes(root)}


def fixture_labels(ledger):
    return {
        "org.opencontainers.image.source": f"https://github.com/{REPOSITORY}",
        "ucm.hosted.cleanup.source_run": str(ledger["source_run_id"]),
        "ucm.hosted.cleanup.source_sha": ledger["source_sha"],
        "ucm.hosted.cleanup.tag": ledger["tag"],
    }


def exclusive_package(remote, ledger):
    """Prove the package consists solely of the source-bound temporary image."""
    package = f"ucm-cleanup-fixture-9101-run-{ledger['source_run_id']}"
    path = f"/users/SuperMarioYL/packages/container/{package}"
    version = ledger["ghcr_version_id"]
    tag = ledger["registry_reference"].rsplit(":", 1)[-1]
    require(
        ledger["ghcr_version_path"] == f"{path}/versions/{version}",
        "Ledger does not identify this fixture's exact package version",
    )
    versions = remote.list_pages(path + "/versions")
    require(
        len(versions) == 1
        and versions[0]["id"] == version
        and set(versions[0]["metadata"]["container"]["tags"]) == {tag},
        "Fixture requires a package with exactly one source-owned version and Tag; no guard is added",
    )
    config = json.loads(
        subprocess.run(
            ["crane", "config", ledger["registry_reference"]],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    )
    labels = config.get("config", {}).get("Labels", {})
    require(
        all(labels.get(key) == value for key, value in fixture_labels(ledger).items()),
        "Package image labels do not match this exact source fixture",
    )
    return {"path": path, "version_id": version, "tags": [tag]}


def remove_fixture_package(remote, ledger):
    """Fixture-only teardown; production cleanup never falls back to package deletion."""
    from ucm_release.cleanup_remote import RemoteError

    package = exclusive_package(remote, ledger)
    remote.json_request("DELETE", package["path"])
    try:
        get_json(remote, package["path"])
    except RemoteError as error:
        require(error.is_missing, f"Package teardown readback failed: {error}")
    else:
        raise RuntimeError("Fixture package still exists after explicit teardown")
    package["status"] = 404
    return package


def cleanup_report(arguments, ledger, name, *, partial):
    report_path = arguments.output_dir / (name + ".json")
    call_cleanup(
        [
            "tag",
            "--repository",
            REPOSITORY,
            "--tag",
            ledger["tag"],
            "--report",
            str(report_path),
        ],
        expected=0,
    )
    report = json.loads(report_path.read_text())
    require(
        report["status"] == ("partial" if partial else "complete"),
        f"Cleanup has unexpected overall status: {report}",
    )
    require(
        len(report["results"]) == 1 and report["results"][0]["tag"] == ledger["tag"],
        "Exact-Tag cleanup reported another version",
    )
    result = report["results"][0]
    require(
        not result.get("failures"), "Nonfatal GHCR cleanup reported blocking failures"
    )
    if partial:
        skipped = result.get("skipped", [])
        require(
            result["status"] == "partial"
            and len(skipped) == 1
            and skipped[0]["reference"] == ledger["registry_reference"]
            and "HTTP 400" in skipped[0]["error"],
            f"Cleanup did not prove the real GHCR version DELETE HTTP 400: {result}",
        )
    else:
        require(
            result["status"] == "deleted" and not result.get("skipped"),
            "Final retry did not close the recovery Release",
        )
    return report


def closure(arguments):
    from ucm_release.cleanup_inventory import ReleaseInventory, collect_catalog

    remote = remote_client()
    require(
        arguments.tag in DRAFT_TAGS, "Only the exact authorized fixture Tag is allowed"
    )
    paths = sorted(arguments.ledger_dir.rglob("fixture-*.json"))
    require(len(paths) == 1, "GHCR skip acceptance requires exactly one source ledger")
    ledger = json.loads(paths[0].read_text())
    production = deployed_production(remote, arguments.production_root)
    proof = {
        "repository": REPOSITORY,
        "scenario": "ghcr-last-tagged-version-nonfatal",
        "fixture": ledger,
        "production": production,
        "status": "running",
    }
    proof_path = arguments.output_dir / "proof.json"
    write_json(proof_path, proof)
    require(
        ledger["kind"] == "ucm-hosted-cleanup-fixture"
        and ledger["repository"] == REPOSITORY
        and ledger["tag"] == arguments.tag
        and ledger["prepared"] is True,
        "Ledger does not describe the exact completed Fork fixture",
    )
    require(
        ledger["production_module_hashes"] == production["module_hashes"]
        and ledger["production_sha"] == production["sha"],
        "Source and closure must execute identical deployed production modules",
    )
    package = f"ucm-cleanup-fixture-9101-run-{ledger['source_run_id']}"
    from ucm_release import runtime, version_config

    version = str(version_config.classify_tag(arguments.tag)["version"])
    require(
        ledger["registry_reference"]
        == f"ghcr.io/supermarioyl/{package}:fixture-ucm-{runtime.oci_tag_version(version)}",
        "Ledger names a non-fixture registry target",
    )
    run = remote.read_release_run(ledger["source_run_id"])
    require(
        run is not None
        and run["status"] == "completed"
        and run["head_branch"] == ledger["tag"]
        and run["head_sha"] == ledger["source_sha"],
        "Source run is not completed and exactly bound to this Tag/SHA",
    )
    original_baseline = ledger["baseline"]
    require(
        set(original_baseline) == set(BASELINE_TAGS) and len(original_baseline) == 11,
        "Ledger must capture all eleven protected baseline Releases",
    )
    require(
        baseline(remote) == original_baseline,
        "Protected baseline changed before closure",
    )
    catalog = collect_catalog(remote, release_type="draft")
    matches = [record for record in catalog.records if record["tag"] == ledger["tag"]]
    require(
        len(matches) == 1
        and ledger["tag"] not in catalog.blocked
        and ledger["tag"] not in catalog.active_tags,
        "Fixture is not exactly discoverable and idle",
    )
    target = ReleaseInventory(remote).resolve_record(matches[0]).target
    require(
        len(target.releases) == 1
        and target.releases[0].identifier == ledger["release_id"]
        and target.releases[0].holds_recovery_data
        and len(target.actions) == 1
        and target.actions[0].identifier == ledger["source_run_id"]
        and not target.actions[0].holds_recovery_data
        and {resource.reference for resource in target.registry}
        == {ledger["registry_reference"]},
        "Resolved fixture includes unexpected resources or lacks a full Release recovery holder",
    )
    subprocess.run(
        [
            "docker",
            "login",
            "ghcr.io",
            "--username",
            "SuperMarioYL",
            "--password-stdin",
        ],
        input=remote.token + "\n",
        text=True,
        check=True,
    )
    proof["exclusive_package"] = exclusive_package(remote, ledger)
    proof["before"] = readback(
        remote, ledger, {kind: 200 for kind in fixture_paths(ledger)}
    )
    write_json(proof_path, proof)
    partial_state = {"release": 200, "tag": 404, "source_run": 404, "ghcr_version": 200}
    proof["first_cleanup"] = cleanup_report(
        arguments, ledger, "first-partial", partial=True
    )
    proof["first_readback"] = readback(remote, ledger, partial_state)
    require(
        baseline(remote) == original_baseline,
        "First cleanup modified a protected baseline",
    )
    write_json(proof_path, proof)
    proof["rediscovery_cleanup"] = cleanup_report(
        arguments, ledger, "rediscovery-partial", partial=True
    )
    proof["rediscovery_readback"] = readback(remote, ledger, partial_state)
    require(
        baseline(remote) == original_baseline, "Retry modified a protected baseline"
    )
    write_json(proof_path, proof)
    # Package deletion is authorized only for this proven exclusive temporary test
    # image. It is separate from the production CLI and occurs after both proofs.
    proof["fixture_package_teardown"] = remove_fixture_package(remote, ledger)
    proof["package_teardown_readback"] = readback(
        remote,
        ledger,
        {
            "release": 200,
            "tag": 404,
            "source_run": 404,
            "ghcr_version": 404,
        },
    )
    require(
        baseline(remote) == original_baseline,
        "Fixture package teardown modified a protected baseline",
    )
    write_json(proof_path, proof)
    proof["final_cleanup"] = cleanup_report(
        arguments, ledger, "final-complete", partial=False
    )
    proof["final_readback"] = readback(
        remote, ledger, {kind: 404 for kind in fixture_paths(ledger)}
    )
    proof["baseline_readback"] = baseline(remote)
    require(
        proof["baseline_readback"] == original_baseline,
        "Final cleanup modified a protected baseline",
    )
    proof["status"] = "complete"
    write_json(proof_path, proof)
    print(
        json.dumps(
            {
                "status": proof["status"],
                "tag": ledger["tag"],
                "baseline_count": 11,
                "production_sha": production["sha"],
                "proof": str(proof_path),
            },
            sort_keys=True,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--tag", required=True)
    prepare_parser.add_argument("--ledger-dir", type=Path, default=Path("out/ledger"))
    prepare_parser.add_argument("--open-dir", type=Path, default=Path("out/open"))
    prepare_parser.add_argument("--plan-dir", type=Path, default=Path("out/plan"))
    closure_parser = commands.add_parser("closure")
    closure_parser.add_argument("--ledger-dir", type=Path, required=True)
    closure_parser.add_argument("--tag", required=True)
    closure_parser.add_argument("--output-dir", type=Path, default=Path("out/hosted"))
    for command in (prepare_parser, closure_parser):
        command.add_argument("--production-root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()
    arguments.production_root = arguments.production_root.resolve()
    sys.path.insert(0, str(arguments.production_root / ".github/release"))
    try:
        prepare(arguments) if arguments.command == "prepare" else closure(arguments)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        if arguments.command == "closure":
            proof_path = arguments.output_dir / "proof.json"
            proof = (
                json.loads(proof_path.read_text())
                if proof_path.exists()
                else {"repository": REPOSITORY}
            )
            proof.update(status="failed", error=str(error))
            write_json(proof_path, proof)
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
