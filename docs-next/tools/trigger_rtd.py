"""Notify Read the Docs only after a release manifest has been published and read back."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from release_manifest import ManifestError, load_manifest
from site_build import repository_name

API = "https://app.readthedocs.org/api/v3"


def request(path: str, *, method: str = "GET", data=None):
    token = os.environ.get("RTD_API_TOKEN")
    if not token:
        raise ValueError("RTD_API_TOKEN is required when RTD publication is configured")
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
    with urlopen(
        Request(API + path, data=body, headers=headers, method=method), timeout=30
    ) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def notify_release(
    repository: str, manifest_path: Path, projects: list[str]
) -> list[str]:
    manifest = load_manifest(manifest_path)
    release = manifest["release"]
    tag = release["tag"]
    expected_url = f"https://github.com/{repository}/releases/tag/{quote(tag, safe='')}"
    if release["url"].casefold() != expected_url.casefold():
        raise ManifestError(
            "RTD release notification must use this repository's manifest"
        )
    if release["type"] not in {"stable", "prerelease"}:
        raise ManifestError("RTD release notification requires a published release")
    triggered = []
    for project in projects:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
            raise ValueError("RTD project slug is missing or invalid")
        prefix = f"/projects/{project}"
        metadata = request(prefix + "/")
        if (
            repository_name(metadata["repository"]["url"]).casefold()
            != repository.casefold()
        ):
            raise ValueError(f"RTD project {project} is bound to another repository")
        request(prefix + "/sync-versions/", method="POST")
        version_path = prefix + f"/versions/{quote(tag, safe='')}/"
        # RTD synchronizes discovered refs asynchronously. Poll only this
        # documented transition, not failed builds or arbitrary API errors.
        for attempt in range(6):
            try:
                version = request(version_path)
                break
            except HTTPError as error:
                if error.code != 404 or attempt == 5:
                    raise
                time.sleep(5)
        if not version["active"]:
            request(
                version_path, method="PATCH", data={"active": True, "hidden": False}
            )
        else:
            request(version_path + "builds/", method="POST")
        triggered.append(f"{project}/{tag}")
        request(prefix + "/versions/latest/builds/", method="POST")
        triggered.append(f"{project}/latest")
        if release["type"] == "stable":
            stable = request(prefix + "/versions/stable/")
            if stable["active"] and stable.get("ref") == tag:
                request(prefix + "/versions/stable/builds/", method="POST")
                triggered.append(f"{project}/stable")
    return triggered


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--project", action="append", required=True)
    args = parser.parse_args()
    for build in notify_release(args.repository, args.manifest, args.project):
        print(f"[rtd] Requested {build}")


if __name__ == "__main__":
    main()
