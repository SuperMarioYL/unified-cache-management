"""Read exact historical identities using a Fork job token; never delete resources."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote


def auth_state():
    config_dir = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
    config_path = config_dir / "config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    result = subprocess.run(
        ["crane", "auth", "get", "docker.io"], capture_output=True, check=False
    )
    credentials = json.loads(result.stdout) if result.returncode == 0 else {}
    return {
        "configured_username_matches": credentials.get("Username")
        == os.environ.get("DOCKERHUB_USERNAME"),
        "configured_secret_matches": credentials.get("Secret")
        == os.environ.get("DOCKERHUB_TOKEN"),
        "config_present": config_path.exists(),
        "docker_io_key_present": "docker.io" in config.get("auths", {}),
        "canonical_hub_key_present": "https://index.docker.io/v1/"
        in config.get("auths", {}),
        "credential_helper_present": bool(
            config.get("credsStore") or config.get("credHelpers")
        ),
        "crane_credentials_readable": result.returncode == 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("auth", "readback"))
    parser.add_argument("--production-root", type=Path, default=Path.cwd())
    parser.add_argument("--production-sha")
    parser.add_argument("--tag")
    args = parser.parse_args()
    output = Path("out/hosted")
    output.mkdir(parents=True, exist_ok=True)
    if args.mode == "auth":
        state = {"initial": auth_state()}
        user, token = os.environ.get("DOCKERHUB_USERNAME"), os.environ.get(
            "DOCKERHUB_TOKEN"
        )
        if not user or not token:
            raise RuntimeError(
                "Historical registry verification requires the configured credential pair"
            )
        docker_result = subprocess.run(
            ["docker", "login", "docker.io", "--username", user, "--password-stdin"],
            input=token.encode(),
            capture_output=True,
            check=False,
        )
        state["docker_login_succeeded"] = docker_result.returncode == 0
        state["after_docker_login"] = auth_state()
        result = subprocess.run(
            [
                "crane",
                "auth",
                "login",
                "docker.io",
                "--username",
                user,
                "--password-stdin",
            ],
            input=token.encode(),
            capture_output=True,
            check=False,
        )
        state["crane_login_succeeded"] = result.returncode == 0
        state["after_crane_login"] = auth_state()
        (output / "registry-auth.json").write_text(json.dumps(state, indent=2) + "\n")
        exchange = subprocess.run(
            ["crane", "auth", "token", "docker.io/lei6393/vllm-openai"],
            capture_output=True,
            check=False,
        )
        jwt_state = {
            "exchange_succeeded": exchange.returncode == 0,
            "parse_succeeded": False,
            "principal_present": False,
            "pull_scope_present": False,
        }
        if exchange.returncode == 0:
            try:
                response = json.loads(exchange.stdout)
                token_value = response.get("token") or response.get("access_token")
                segment = token_value.split(".")[1]
                payload = json.loads(
                    base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
                )
                jwt_state.update(
                    parse_succeeded=True,
                    principal_present=bool(payload.get("sub")),
                    pull_scope_present=any(
                        entry.get("name") == "lei6393/vllm-openai"
                        and "pull" in entry.get("actions", [])
                        for entry in payload.get("access", [])
                    ),
                )
            except (ValueError, KeyError, IndexError, AttributeError, TypeError):
                pass
        state["registry_bearer"] = jwt_state
        (output / "registry-auth.json").write_text(json.dumps(state, indent=2) + "\n")
        print(json.dumps(state))
        if (
            not state["crane_login_succeeded"]
            or not state["after_crane_login"]["crane_credentials_readable"]
        ):
            raise RuntimeError(
                "Crane cannot resolve the configured Docker Hub credentials"
            )
        return
    sys.path.insert(0, str(args.production_root.resolve() / ".github/release"))
    import hosted_cleanup as acceptance
    from ucm_release.cleanup_remote import RemoteError

    acceptance.CONFIG = json.loads(
        Path(__file__).with_name("acceptance-config.json").read_text()
    )
    targets = json.loads(
        Path(__file__).with_name("historical_targets.json").read_text()
    )
    if args.tag not in targets:
        raise RuntimeError("Tag is outside the exact historical readback allowlist")
    remote = acceptance.remote_client()
    production = acceptance.deployed_production(
        remote,
        SimpleNamespace(
            production_root=args.production_root.resolve(),
            production_sha=args.production_sha,
        ),
    )
    baseline = acceptance.baseline(remote)
    target = targets[args.tag]
    prefix = "/repos/" + acceptance.REPOSITORY
    paths = [
        ("release", f'{prefix}/releases/{target["release_id"]}'),
        ("tag", f'{prefix}/git/ref/tags/{quote(args.tag, safe="/")}'),
        ("source_run", f'{prefix}/actions/runs/{target["source_run_id"]}'),
    ]
    paths += [
        ("asset", f"{prefix}/releases/assets/{identifier}")
        for identifier in target["asset_ids"]
    ]
    paths += [("registry_version", path) for path in target["registry_version_paths"]]
    proof = {
        "tag": args.tag,
        "production": production,
        "resources": [],
        "status": "incomplete",
    }
    for kind, path in paths:
        try:
            value = remote.json_request("GET", path)
            entry = {
                "kind": kind,
                "path": path,
                "status": 200,
                "id": value.get("id") if isinstance(value, dict) else None,
            }
        except RemoteError as error:
            entry = {
                "kind": kind,
                "path": path,
                "status": 404 if error.is_missing else error.status,
                "error": None if error.is_missing else str(error),
            }
        proof["resources"].append(entry)
        (output / "historical-readback.json").write_text(
            json.dumps(proof, indent=2) + "\n"
        )
    proof["baseline"] = acceptance.baseline(remote, baseline)
    proof["status"] = (
        "complete"
        if all(item["status"] == 404 for item in proof["resources"])
        else "incomplete"
    )
    (output / "historical-readback.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(args.tag, proof["status"], len(proof["resources"]), "exact readbacks")
    if proof["status"] != "complete":
        raise RuntimeError(
            "Historical objects are still present or absence could not be proved"
        )


if __name__ == "__main__":
    main()
