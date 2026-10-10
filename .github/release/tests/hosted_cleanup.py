"""Run disposable cleanup fixtures against the Fork's real GitHub and GHCR APIs.

This is a hosted test helper, not a release publisher. Run ``prepare`` from each
fixture Tag's release-tag.yml push, then ``closure`` from a separate workflow
once all three source runs have completed. Production modules come from the
explicit checkout root, even when this helper was downloaded from another ref.
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
DRAFT_TAGS = {f"draft/v0.8.0-{number}" for number in (9101, 9102, 9103)}
BASELINE_TAGS = (
    "nightly/v0.8.0-20260918-1",
    "nightly/v0.8.0-20260918-2",
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
    tag = arguments.tag
    require(
        tag in DRAFT_TAGS,
        "Only this run's three explicit Draft fixture Tags are allowed",
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
        "production_module_hashes": module_hashes(arguments.production_root),
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
        subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "--tag",
                reference,
                directory,
            ],
            check=True,
        )
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
        "Prepare changed an existing Nightly baseline",
    )
    print(json.dumps(ledger, sort_keys=True))


def fixture_paths(ledger):
    return {
        "release": f"/repos/{REPOSITORY}/releases/{ledger['release_id']}",
        "tag": f"/repos/{REPOSITORY}/git/ref/tags/{quote(ledger['tag'], safe='/')}",
        "source_run": f"/repos/{REPOSITORY}/actions/runs/{ledger['source_run_id']}",
        "ghcr_version": ledger["ghcr_version_path"],
    }


def readback(remote, ledger, absent):
    from ucm_release.cleanup_remote import RemoteError

    readings = {}
    for kind, path in fixture_paths(ledger).items():
        try:
            value = get_json(remote, path)
        except RemoteError as error:
            require(
                absent and error.is_missing,
                f"Unexpected readback error for {kind}: {error}",
            )
            readings[kind] = {"path": path, "status": 404}
        else:
            require(not absent, f"Deleted fixture still exists: {kind} {path}")
            readings[kind] = {"path": path, "status": 200}
            if kind in {"release", "source_run", "ghcr_version"}:
                expected_id = {
                    "release": ledger["release_id"],
                    "source_run": ledger["source_run_id"],
                    "ghcr_version": ledger["ghcr_version_id"],
                }[kind]
                require(
                    value.get("id") == expected_id,
                    f"Readback {kind} ID differs from its ledger",
                )
    return readings


def closure(arguments):
    from ucm_release.cleanup_inventory import collect_catalog

    remote = remote_client()
    paths = sorted(arguments.ledger_dir.rglob("fixture-*.json"))
    for path in paths:
        backup = arguments.output_dir / "source-ledgers" / path.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(path.read_bytes())
    ledgers = [json.loads(path.read_text()) for path in paths]
    proof = {"repository": REPOSITORY, "fixtures": ledgers, "status": "running"}
    proof_path = arguments.output_dir / "proof.json"
    write_json(proof_path, proof)
    expected_tags = set(json.loads(arguments.expected_tags))
    require(
        expected_tags == DRAFT_TAGS,
        "Closure requires exactly the three authorized Draft fixtures",
    )
    require(
        len(ledgers) == 3 and {ledger["tag"] for ledger in ledgers} == expected_tags,
        "The ledger must contain exactly one record for each authorized fixture",
    )
    hashes = module_hashes(arguments.production_root)
    for ledger in ledgers:
        require(
            ledger["kind"] == "ucm-hosted-cleanup-fixture"
            and ledger["repository"] == REPOSITORY
            and ledger["prepared"] is True,
            "Ledger is not a completed Fork fixture",
        )
        package = f"ucm-cleanup-fixture-{ledger['tag'].rsplit('-', 1)[-1]}-run-{ledger['source_run_id']}"
        require(
            ledger["registry_reference"].startswith(
                f"ghcr.io/supermarioyl/{package}:fixture-ucm-"
            ),
            "Ledger names a non-fixture registry resource",
        )
        require(
            ledger["ghcr_version_path"]
            == f"/users/SuperMarioYL/packages/container/{package}/versions/{ledger['ghcr_version_id']}",
            "GHCR path is not this fixture's exact package version",
        )
        require(
            ledger["production_module_hashes"] == hashes,
            "Source and closure must execute identical production modules",
        )
        run = remote.read_release_run(ledger["source_run_id"])
        require(
            run is not None
            and run["status"] == "completed"
            and run["head_branch"] == ledger["tag"]
            and run["head_sha"] == ledger["source_sha"],
            "Source workflow must be completed and exactly bound to the fixture",
        )
    proof["production_module_hashes"] = hashes
    write_json(proof_path, proof)
    original_baseline = ledgers[0]["baseline"]
    require(
        all(ledger["baseline"] == original_baseline for ledger in ledgers),
        "Source fixtures captured different Nightly baselines",
    )
    require(baseline(remote) == original_baseline, "Baseline changed before closure")
    catalog = collect_catalog(remote, release_type="draft")
    require(
        {record["tag"] for record in catalog.records} == expected_tags
        and not catalog.orphans,
        "Draft catalog contains non-fixture versions; refusing retention",
    )
    require(
        not catalog.active_tags and not catalog.blocked,
        f"Fixture catalog is not deletable: {catalog.blocked}",
    )
    ledger_by_tag = {ledger["tag"]: ledger for ledger in ledgers}
    for record in catalog.records:
        ledger = ledger_by_tag[record["tag"]]
        require(
            record["release_ids"] == [ledger["release_id"]]
            and {run["id"] for run in record["runs"]} == {ledger["source_run_id"]}
            and {resource["reference"] for resource in record["resources"]}
            == {ledger["registry_reference"]},
            "Resolved fixture includes resources not created by this test",
        )
    ledgers.sort(key=lambda ledger: ledger["release_id"])
    oldest, *kept = ledgers
    call_cleanup(
        [
            "tag",
            "--repository",
            REPOSITORY,
            "--tag",
            oldest["tag"],
            "--fail-resource",
            oldest["registry_reference"],
            "--report",
            str(arguments.output_dir / "injected-failure.json"),
        ],
        expected=1,
    )
    injected = json.loads((arguments.output_dir / "injected-failure.json").read_text())
    failures = [
        failure
        for result in injected["results"]
        for failure in result.get("failures", [])
    ]
    require(
        len(failures) == 1
        and failures[0]["reference"] == oldest["registry_reference"]
        and failures[0]["attempts"] == 3
        and "synthetic HTTP 503" in failures[0]["error"],
        "Failure proof did not exercise the intended three synthetic 503 attempts",
    )
    proof["injected_failure_readback"] = readback(remote, oldest, absent=False)
    write_json(proof_path, proof)
    call_cleanup(
        [
            "retention",
            "--repository",
            REPOSITORY,
            "--release-type",
            "draft",
            "--current-tag",
            ledgers[-1]["tag"],
            "--max-count",
            "2",
            "--pypi-enabled",
            "false",
            "--report",
            str(arguments.output_dir / "retention.json"),
        ]
    )
    proof["retention_deleted"] = {oldest["tag"]: readback(remote, oldest, absent=True)}
    proof["retention_kept"] = {
        ledger["tag"]: readback(remote, ledger, absent=False) for ledger in kept
    }
    write_json(proof_path, proof)
    for ledger in kept:
        call_cleanup(
            [
                "tag",
                "--repository",
                REPOSITORY,
                "--tag",
                ledger["tag"],
                "--report",
                str(arguments.output_dir / f"cleanup-{ledger['source_run_id']}.json"),
            ]
        )
    proof["final_readback"] = {
        ledger["tag"]: readback(remote, ledger, absent=True) for ledger in ledgers
    }
    # An exact package-version GET is primary proof; Crane independently checks the OCI references.
    proof["registry_readback"] = {}
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
    for ledger in ledgers:
        completed = subprocess.run(
            ["crane", "digest", ledger["registry_reference"]],
            text=True,
            capture_output=True,
            check=False,
        )
        require(
            completed.returncode != 0
            and remote._crane_error(completed.stderr or completed.stdout).is_missing,
            "OCI readback did not prove the exact reference absent",
        )
        proof["registry_readback"][ledger["registry_reference"]] = {
            "status": 404,
            "detail": (completed.stderr or completed.stdout).strip(),
        }
    proof["baseline_readback"] = baseline(remote)
    require(
        proof["baseline_readback"] == original_baseline,
        "Closure modified a preserved Nightly baseline",
    )
    proof["status"] = "complete"
    write_json(proof_path, proof)
    print(json.dumps(proof, sort_keys=True))


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
    closure_parser.add_argument("--expected-tags", required=True)
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
