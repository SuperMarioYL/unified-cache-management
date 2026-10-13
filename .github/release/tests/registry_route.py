"""Compare authenticated manifest requests without saving credentials or tokens."""

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
    from ucm_release.cleanup_records import Resource
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
    out = Path("out/hosted")
    out.mkdir(parents=True, exist_ok=True)
    config_dir = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
    config = json.loads((config_dir / "config.json").read_text())
    headers = config.get("HttpHeaders", {})
    auth_state = json.loads((out / "registry-auth.json").read_text())
    proof = {
        "production": production,
        "auth": auth_state,
        "docker_authorization_httpheader_present": any(
            key.casefold() == "authorization" for key in headers
        ),
        "docker_useragent_httpheader_present": any(
            key.casefold() == "user-agent" for key in headers
        ),
        "requests": [],
    }
    exchange = subprocess.run(
        ["crane", "auth", "token", "docker.io/lei6393/vllm-openai"],
        capture_output=True,
        check=True,
    )
    response = json.loads(exchange.stdout)
    token = response.get("token") or response.get("access_token")
    segment = token.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    proof["bearer"] = {
        "principal_present": bool(payload.get("sub")),
        "pull_scope_present": any(
            entry.get("name") == "lei6393/vllm-openai"
            and "pull" in entry.get("actions", [])
            for entry in payload.get("access", [])
        ),
    }
    tags = [
        "supermarioyl-v0.26.0-ucm-0.7.0rc12-amd64",
        "supermarioyl-v0.26.0-ucm-0.7.0rc12",
    ]
    for tag in tags:
        reference = "docker.io/lei6393/vllm-openai:" + tag
        row = {"reference": reference, "direct": []}
        try:
            state = remote.probe(Resource("dockerhub-member", reference))
            row["crane"] = {"presence": "absent" if state is None else "present"}
        except RemoteError as error:
            row["crane"] = {"status": error.status, "presence": "error"}
        for method in ["HEAD", "GET"]:
            request = urllib.request.Request(
                "https://index.docker.io/v2/lei6393/vllm-openai/manifests/"
                + quote(tag, safe=""),
                method=method,
                headers={
                    "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
                },
            )
            request.add_unredirected_header("Authorization", "Bearer " + token)
            try:
                with urllib.request.urlopen(request, timeout=60) as result:
                    status, result_headers, body = (
                        result.status,
                        result.headers,
                        result.read(),
                    )
            except urllib.error.HTTPError as error:
                status, result_headers, body = error.code, error.headers, error.read()
            value = {
                "method": method,
                "status": status,
                "rate_limit": result_headers.get("ratelimit-limit"),
                "rate_remaining": result_headers.get("ratelimit-remaining"),
            }
            if method == "GET":
                try:
                    value["error_codes"] = [
                        entry.get("code")
                        for entry in json.loads(body).get("errors", [])
                    ]
                except (ValueError, AttributeError):
                    value["error_codes"] = []
            row["direct"].append(value)
        proof["requests"].append(row)
        (out / "registry-route.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
