"""No-overwrite GitHub Draft/Pre-release publication and byte readback."""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .common import (
    ProductionError,
    canonical_bytes,
    decode_json,
    require_lower_commit_sha,
    require_lower_sha256,
    require_string,
    sha256_envelope,
    verify_envelope,
)

_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}", re.ASCII
)
_TAG = re.compile(
    r"(?:draft/v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)-[1-9][0-9]*|v(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:rc[1-9][0-9]*)?)",
    re.ASCII,
)
_ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,255}", re.ASCII)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_STAGES = {"draft", "rc", "stable", "hotfix"}
_MAX_ASSET_BYTES = 4 * 1024 * 1024 * 1024
_SUPPORT_ASSETS = {
    "SHA256SUMS",
    "ucm-production-manifest.json",
    "ucm-production-sbom.json",
    "ucm-production-environment.json",
}


def _release_versions(stage: str, tag_name: str, version: str) -> tuple[str, str]:
    escaped = re.escape(version)
    if stage == "draft":
        match = re.fullmatch(rf"draft/v{escaped}-([1-9][0-9]*)", tag_name, re.ASCII)
        if match is None:
            raise ProductionError("Draft Release Tag differs from version")
        number = match.group(1)
        return f"{version}.dev{number}", f"{version}-draft.{number}"
    if stage == "rc":
        match = re.fullmatch(rf"v{escaped}rc([1-9][0-9]*)", tag_name, re.ASCII)
        if match is None:
            raise ProductionError("RC Release Tag differs from version")
        number = match.group(1)
        return f"{version}rc{number}", f"{version}-rc.{number}"
    if tag_name != f"v{version}":
        raise ProductionError("final Release Tag differs from version")
    return version, version


def _expected_asset_names(plan: GitHubReleasePlan) -> tuple[str, ...]:
    wheel_version, chart_version = _release_versions(
        plan.stage, plan.tag_name, plan.version
    )
    profiles = (
        ("uc_manager_cuda", "manylinux_2_28"),
        ("uc_manager_cann_a2", "linux"),
        ("uc_manager_cann_a3", "linux"),
    )
    wheels = tuple(
        f"{distribution}-{wheel_version}-cp312-cp312-{platform}_{architecture}.whl"
        for distribution, platform in profiles
        for architecture in ("x86_64", "aarch64")
    )
    return (
        *wheels,
        f"unified-cache-pd-{chart_version}.tgz",
        "SHA256SUMS",
        "ucm-production-manifest.json",
        "ucm-production-sbom.json",
        "ucm-production-environment.json",
    )


class GitHubNotFound(ProductionError):
    """The requested public Release object is intentionally not visible."""


class GitHubResponseLost(ProductionError):
    """A GitHub write may have completed but its response was not observed."""


class ReleaseClient(Protocol):
    repository: str

    def find_releases(
        self, tag_name: str, *, anonymous: bool = False
    ) -> list[dict[str, Any]]: ...

    def create_release(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def patch_release(
        self, release_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    def list_release_assets(
        self, release_id: int, *, anonymous: bool = False
    ) -> list[dict[str, Any]]: ...

    def upload_release_asset(
        self, release_id: int, name: str, media_type: str, path: Path
    ) -> dict[str, Any]: ...

    def download_release_asset(
        self, asset: dict[str, Any], *, anonymous: bool = False
    ) -> bytes: ...


_HTTPTransport = Callable[
    [str, str, dict[str, str], bytes | None],
    tuple[int, dict[str, str], bytes],
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class GitHubReleaseClient:
    """Bounded current-repository GitHub Release REST adapter."""

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        transport: _HTTPTransport | None = None,
        max_json_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        if _REPOSITORY.fullmatch(repository) is None:
            raise ProductionError("GitHub Release client repository is invalid")
        if token is not None and (
            not token or any(ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            raise ProductionError("GitHub Release token is malformed")
        if type(max_json_bytes) is not int or max_json_bytes < 1:
            raise ProductionError("GitHub Release JSON size limit is invalid")
        self.repository = repository
        self.token = token
        self.transport = transport or self._urllib_transport
        self.max_json_bytes = max_json_bytes

    def _urllib_transport(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, method=method, headers=headers, data=body)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=60) as response:
                limit = (
                    _MAX_ASSET_BYTES
                    if headers.get("accept") == "application/octet-stream"
                    else self.max_json_bytes
                )
                raw = response.read(limit + 1)
                return response.status, dict(response.headers.items()), raw
        except urllib.error.HTTPError as error:
            limit = self.max_json_bytes
            return error.code, dict(error.headers.items()), error.read(limit + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise error

    def _headers(
        self, *, content_type: str | None = None, content_length: int = 0
    ) -> dict[str, str]:
        headers = {
            "accept": "application/vnd.github+json",
            "content-length": str(content_length),
            "user-agent": "ucm-production-release-controller/1",
            "x-github-api-version": "2022-11-28",
        }
        if content_type is not None:
            headers["content-type"] = content_type
        if self.token is not None:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        expect: set[int],
        response_loss_possible: bool = False,
        anonymous: bool = False,
        binary: bool = False,
    ) -> object:
        parsed = urllib.parse.urlsplit(url)
        api_prefix = f"/repos/{self.repository}/"
        if (
            parsed.scheme != "https"
            or parsed.netloc not in {"api.github.com", "uploads.github.com"}
            or not parsed.path.startswith(api_prefix)
            or parsed.fragment
            or "\\" in parsed.path
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            raise ProductionError(
                "GitHub Release URL is outside the current repository"
            )
        if parsed.netloc == "uploads.github.com":
            expected_prefix = api_prefix + "releases/"
            if (
                method != "POST"
                or not parsed.path.startswith(expected_prefix)
                or not parsed.path.endswith("/assets")
            ):
                raise ProductionError("GitHub Release upload route is invalid")
        elif method not in {"GET", "POST", "PATCH"}:
            raise ProductionError("GitHub Release HTTP method is not approved")
        headers = self._headers(
            content_type=content_type,
            content_length=len(body) if body is not None else 0,
        )
        if anonymous:
            headers.pop("authorization", None)
        if binary:
            headers["accept"] = "application/octet-stream"
        try:
            status, response_headers, raw = self.transport(method, url, headers, body)
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            if response_loss_possible:
                raise GitHubResponseLost(str(error)) from None
            raise ProductionError(f"GitHub Release transport failed: {error}") from None
        normalized = {
            str(key).lower(): str(value) for key, value in response_headers.items()
        }
        if status == 302 and binary and method == "GET":
            location = normalized.get("location", "")
            redirected = urllib.parse.urlsplit(location)
            if (
                redirected.scheme != "https"
                or not redirected.netloc.endswith(".githubusercontent.com")
                or redirected.username is not None
                or redirected.password is not None
                or not redirected.path.startswith("/")
                or redirected.fragment
            ):
                raise ProductionError("GitHub Release asset redirect is unapproved")
            redirect_headers = dict(headers)
            redirect_headers.pop("authorization", None)
            try:
                status, response_headers, raw = self.transport(
                    "GET", location, redirect_headers, None
                )
            except (OSError, TimeoutError, urllib.error.URLError) as error:
                raise ProductionError(
                    f"GitHub Release asset redirect transport failed: {error}"
                ) from None
            normalized = {
                str(key).lower(): str(value) for key, value in response_headers.items()
            }
        if 300 <= status <= 399:
            raise ProductionError("GitHub Release redirect is forbidden")
        limit = _MAX_ASSET_BYTES if binary else self.max_json_bytes
        length = normalized.get("content-length")
        if length is not None:
            if not length.isdecimal() or int(length) > limit:
                raise ProductionError("GitHub Release response size is invalid")
        if not isinstance(raw, bytes) or len(raw) > limit:
            raise ProductionError("GitHub Release response size exceeds limit")
        if status not in expect:
            if status == 404:
                raise GitHubNotFound("GitHub Release object is not found")
            if response_loss_possible and status >= 500:
                raise GitHubResponseLost(f"GitHub Release returned HTTP {status}")
            raise ProductionError(f"GitHub Release returned HTTP {status}")
        if binary:
            return raw
        media = normalized.get("content-type", "").split(";", 1)[0].strip().lower()
        if media not in {
            "application/json",
            "application/vnd.github+json",
        } and not media.endswith("+json"):
            raise ProductionError("GitHub Release response is not JSON")
        return decode_json(raw, "GitHub Release response")

    def _api_url(self, path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or "?" in path
            or "#" in path
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/")[1:])
        ):
            raise ProductionError("GitHub Release path is malformed")
        return f"https://api.github.com/repos/{self.repository}{path}"

    def find_releases(
        self, tag_name: str, *, anonymous: bool = False
    ) -> list[dict[str, Any]]:
        if _TAG.fullmatch(tag_name) is None:
            raise ProductionError("GitHub Release lookup Tag is invalid")
        # The list endpoint is required for Draft visibility and duplicate detection.
        result: list[dict[str, Any]] = []
        for page in range(1, 11):
            url = self._api_url("/releases") + f"?per_page=100&page={page}"
            value = self._request("GET", url, expect={200}, anonymous=anonymous)
            if not isinstance(value, list) or any(
                not isinstance(item, dict) for item in value
            ):
                raise ProductionError("GitHub Release listing is malformed")
            result.extend(item for item in value if item.get("tag_name") == tag_name)
            if len(value) < 100:
                return result
        raise ProductionError("GitHub Release listing exceeds ten pages")

    def create_release(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = canonical_bytes(payload)
        value = self._request(
            "POST",
            self._api_url("/releases"),
            body=raw,
            content_type="application/json",
            expect={201},
            response_loss_possible=True,
        )
        if not isinstance(value, dict):
            raise ProductionError("GitHub Release create response is malformed")
        return value

    def patch_release(self, release_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        release_id = _positive(release_id, "GitHub Release id")
        raw = canonical_bytes(payload)
        value = self._request(
            "PATCH",
            self._api_url(f"/releases/{release_id}"),
            body=raw,
            content_type="application/json",
            expect={200},
            response_loss_possible=True,
        )
        if not isinstance(value, dict):
            raise ProductionError("GitHub Release patch response is malformed")
        return value

    def list_release_assets(
        self, release_id: int, *, anonymous: bool = False
    ) -> list[dict[str, Any]]:
        release_id = _positive(release_id, "GitHub Release id")
        result: list[dict[str, Any]] = []
        for page in range(1, 11):
            url = (
                self._api_url(f"/releases/{release_id}/assets")
                + f"?per_page=100&page={page}"
            )
            value = self._request("GET", url, expect={200}, anonymous=anonymous)
            if not isinstance(value, list) or any(
                not isinstance(item, dict) for item in value
            ):
                raise ProductionError("GitHub Release asset listing is malformed")
            result.extend(value)
            if len(value) < 100:
                return result
        raise ProductionError("GitHub Release asset listing exceeds ten pages")

    def upload_release_asset(
        self, release_id: int, name: str, media_type: str, path: Path
    ) -> dict[str, Any]:
        release_id = _positive(release_id, "GitHub Release id")
        if _ASSET_NAME.fullmatch(name) is None or media_type != _media_type(name):
            raise ProductionError("GitHub Release upload asset identity is invalid")
        candidate = Path(path)
        if not candidate.is_file() or candidate.is_symlink() or candidate.name != name:
            raise ProductionError("GitHub Release upload asset path is invalid")
        raw = candidate.read_bytes()
        if not 1 <= len(raw) <= _MAX_ASSET_BYTES:
            raise ProductionError("GitHub Release upload asset size is invalid")
        query = urllib.parse.urlencode({"name": name}, quote_via=urllib.parse.quote)
        value = self._request(
            "POST",
            f"https://uploads.github.com/repos/{self.repository}/releases/{release_id}/assets?{query}",
            body=raw,
            content_type=media_type,
            expect={201},
            response_loss_possible=True,
        )
        if not isinstance(value, dict):
            raise ProductionError("GitHub Release upload response is malformed")
        return value

    def download_release_asset(
        self, asset: dict[str, Any], *, anonymous: bool = False
    ) -> bytes:
        if not isinstance(asset, dict):
            raise ProductionError("GitHub Release download asset is malformed")
        asset_id = _positive(asset.get("id"), "GitHub Release asset id")
        expected = self._api_url(f"/releases/assets/{asset_id}")
        if asset.get("url") != expected:
            raise ProductionError("GitHub Release download asset URL differs")
        value = self._request(
            "GET",
            expected,
            expect={200},
            anonymous=anonymous,
            binary=True,
        )
        if not isinstance(value, bytes):
            raise ProductionError("GitHub Release asset download is malformed")
        return value


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise ProductionError(
            f"cannot hash Release asset {path.name}: {error}"
        ) from None
    if not 1 <= size <= _MAX_ASSET_BYTES:
        raise ProductionError("Release asset size is outside the approved bounds")
    return "sha256:" + digest.hexdigest(), size


def _media_type(name: str) -> str:
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".tgz"):
        return "application/gzip"
    if name == "SHA256SUMS":
        return "text/plain"
    return "application/octet-stream"


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    path: Path
    sha256: str
    size: int
    media_type: str

    @classmethod
    def from_path(cls, path: Path) -> ReleaseAsset:
        candidate = Path(path)
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or _ASSET_NAME.fullmatch(candidate.name) is None
        ):
            raise ProductionError("Release asset must be one canonical regular file")
        digest, size = _file_digest(candidate)
        return cls(candidate.name, candidate, digest, size, _media_type(candidate.name))

    def verify(self) -> None:
        if (
            _ASSET_NAME.fullmatch(self.name) is None
            or Path(self.path).name != self.name
            or self.media_type != _media_type(self.name)
            or _DIGEST.fullmatch(self.sha256) is None
            or type(self.size) is not int
            or not 1 <= self.size <= _MAX_ASSET_BYTES
        ):
            raise ProductionError("Release asset record is malformed")
        digest, size = _file_digest(Path(self.path))
        if digest != self.sha256 or size != self.size:
            raise ProductionError(f"Release asset bytes changed: {self.name}")


@dataclass(frozen=True)
class GitHubReleasePlan:
    stage: str
    repository: str
    repository_id: int
    tag_name: str
    source_sha: str
    version: str
    candidate_sha256: str
    environment_status: str
    assets: tuple[ReleaseAsset, ...]
    channel_records: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        if self.stage not in _STAGES:
            raise ProductionError("GitHub Release stage is invalid")
        if _REPOSITORY.fullmatch(self.repository) is None:
            raise ProductionError("GitHub Release repository is invalid")
        if type(self.repository_id) is not int or self.repository_id < 1:
            raise ProductionError("GitHub Release repository id is invalid")
        if _TAG.fullmatch(self.tag_name) is None:
            raise ProductionError("GitHub Release Tag is invalid")
        if (self.stage == "draft") != self.tag_name.startswith("draft/"):
            raise ProductionError("GitHub Release stage and Tag differ")
        require_lower_commit_sha(self.source_sha, "GitHub Release source SHA")
        require_lower_sha256(self.candidate_sha256, "candidate SHA256")
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version, re.ASCII) is None:
            raise ProductionError("GitHub Release version is invalid")
        allowed_environment = (
            {"passed", "waived-for-preview"}
            if self.stage in {"draft", "rc"}
            else {"passed"}
        )
        if self.environment_status not in allowed_environment:
            raise ProductionError("GitHub Release environment status is invalid")
        if not isinstance(self.assets, tuple) or len(self.assets) != 11:
            raise ProductionError("GitHub Release requires exactly eleven assets")
        for asset in self.assets:
            if not isinstance(asset, ReleaseAsset):
                raise ProductionError("GitHub Release assets must be sealed records")
            asset.verify()
        names = tuple(asset.name for asset in self.assets)
        if names != _expected_asset_names(self) or not _SUPPORT_ASSETS <= set(names):
            raise ProductionError("GitHub Release asset set is not canonical")
        if not isinstance(self.channel_records, tuple):
            raise ProductionError("GitHub Release channel records must be a tuple")

    @property
    def reference(self) -> str:
        return f"github-release://{self.repository}/{self.tag_name}"


def _authority(plan: GitHubReleasePlan) -> dict[str, Any]:
    owner = plan.repository.split("/", 1)[0].lower()
    image_tag = plan.tag_name.replace("/", "-")
    suffix = "-private" if plan.stage == "draft" else ""
    lines = [
        f"UCM production {plan.stage} release for {plan.tag_name}.",
        "",
        f"Source commit: {plan.source_sha}",
        f"Candidate SHA256: {plan.candidate_sha256}",
        f"Environment test: {plan.environment_status}",
        "",
        "Wheel assets: six backend and architecture specific distributions.",
        f"CUDA image: ghcr.io/{owner}/ucm-cuda{suffix}:{image_tag}",
        f"CANN A2 image: ghcr.io/{owner}/ucm-cann-a2{suffix}:{image_tag}",
        f"CANN A3 image: ghcr.io/{owner}/ucm-cann-a3{suffix}:{image_tag}",
    ]
    if plan.stage != "draft":
        _, chart_version = _release_versions(plan.stage, plan.tag_name, plan.version)
        lines.append(
            f"Chart: oci://ghcr.io/{owner}/charts/unified-cache-pd:{chart_version}"
        )
    lines.extend(
        [
            "",
            "Hardware and Kubernetes cluster acceptance are not claimed by this release.",
        ]
    )
    return {
        "tag_name": plan.tag_name,
        "target_commitish": plan.source_sha,
        "name": f"UCM {plan.tag_name}",
        "body": "\n".join(lines),
        "draft": True,
        "prerelease": False,
        "make_latest": "false",
    }


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ProductionError(f"{label} must be a positive integer")
    return value


def _release_url(plan: GitHubReleasePlan, release_id: int) -> str:
    return f"https://api.github.com/repos/{plan.repository}/releases/{release_id}"


def _validate_release(
    plan: GitHubReleasePlan, value: object, *, allow_final: bool = True
) -> tuple[dict[str, Any], str]:
    if not isinstance(value, dict):
        raise ProductionError("GitHub Release response must be an object")
    release = dict(value)
    release_id = _positive(release.get("id"), "GitHub Release id")
    authority = _authority(plan)
    for key in ("tag_name", "target_commitish", "name", "body"):
        if release.get(key) != authority[key]:
            label = "source" if key == "target_commitish" else key
            raise ProductionError(f"GitHub Release {label} differs from authority")
    if release.get("draft") is True and release.get("prerelease") is False:
        state = "draft"
    elif (
        plan.stage == "rc"
        and release.get("draft") is False
        and release.get("prerelease") is True
    ):
        state = "prerelease"
    elif (
        plan.stage in {"stable", "hotfix"}
        and release.get("draft") is False
        and release.get("prerelease") is False
    ):
        state = "release"
    else:
        raise ProductionError("GitHub Release state differs from authority")
    if not allow_final and state != "draft":
        raise ProductionError("GitHub Release state must remain Draft")
    api_url = _release_url(plan, release_id)
    if (
        release.get("url") != api_url
        or release.get("assets_url") != api_url + "/assets"
        or release.get("upload_url")
        != (
            f"https://uploads.github.com/repos/{plan.repository}/releases/"
            f"{release_id}/assets{{?name,label}}"
        )
    ):
        raise ProductionError("GitHub Release transport identity differs")
    parsed = urllib.parse.urlsplit(str(release.get("html_url", "")))
    prefix = f"/{plan.repository}/releases/tag/"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not parsed.path.startswith(prefix)
        or not parsed.path.removeprefix(prefix)
        or parsed.query
        or parsed.fragment
    ):
        raise ProductionError("GitHub Release HTML identity differs")
    author = release.get("author")
    if (
        not isinstance(author, dict)
        or author.get("login") != "github-actions[bot]"
        or author.get("type") != "Bot"
    ):
        raise ProductionError("GitHub Release author identity differs")
    assets = release.get("assets")
    if not isinstance(assets, list) or any(
        not isinstance(item, dict) for item in assets
    ):
        raise ProductionError("GitHub Release embedded assets are malformed")
    return release, state


def _find_one(
    plan: GitHubReleasePlan,
    client: ReleaseClient,
    *,
    anonymous: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    values = client.find_releases(plan.tag_name, anonymous=anonymous)
    if not isinstance(values, list) or any(
        not isinstance(item, dict) for item in values
    ):
        raise ProductionError("GitHub Release listing is malformed")
    if len(values) > 1:
        raise ProductionError("duplicate GitHub Release Tag occupancy")
    if not values:
        return None, None
    return _validate_release(plan, values[0])


def prepare_release(plan: GitHubReleasePlan, client: ReleaseClient) -> dict[str, Any]:
    """Create an empty Draft or reopen one exact existing release."""

    if client.repository != plan.repository:
        raise ProductionError("GitHub Release client repository differs")
    release, state = _find_one(plan, client)
    operations: list[dict[str, Any]] = []
    if release is None:
        authority = _authority(plan)
        try:
            response = client.create_release(authority)
        except GitHubResponseLost:
            release, state = _find_one(plan, client)
            if release is None or state != "draft" or release["assets"]:
                raise ProductionError(
                    "GitHub Release create response was lost without exact readback"
                ) from None
            decision = "create-response-loss-recovered"
            outcome = "response-loss-recovered"
        else:
            release, state = _validate_release(plan, response, allow_final=False)
            if release["assets"]:
                raise ProductionError("new GitHub Release must be an empty Draft")
            reread, reread_state = _find_one(plan, client)
            if reread is None or reread_state != state or reread["id"] != release["id"]:
                raise ProductionError("new GitHub Release readback differs")
            release = reread
            decision = "create"
            outcome = "completed"
        operations.append(
            {
                "action": "create-release",
                "release_id": release["id"],
                "outcome": outcome,
            }
        )
    else:
        decision = "resume-draft" if state == "draft" else "reuse-final"
    return sha256_envelope(
        {
            "kind": "ucm-production-github-release-prepare",
            "schema_version": 1,
            "stage": plan.stage,
            "reference": plan.reference,
            "decision": decision,
            "release_state": state,
            "release": release,
            "operations": operations,
        }
    )


def _remote_asset(
    plan: GitHubReleasePlan,
    release_id: int,
    value: object,
    expected: ReleaseAsset,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProductionError("GitHub Release asset response must be an object")
    item = dict(value)
    asset_id = _positive(item.get("id"), "GitHub Release asset id")
    if (
        item.get("name") != expected.name
        or item.get("state") != "uploaded"
        or type(item.get("size")) is not int
        or item.get("size") != expected.size
        or item.get("digest") != expected.sha256
    ):
        raise ProductionError(f"GitHub Release asset conflict: {expected.name}")
    if item.get("url") != (
        f"https://api.github.com/repos/{plan.repository}/releases/assets/{asset_id}"
    ):
        raise ProductionError("GitHub Release asset API identity differs")
    parsed = urllib.parse.urlsplit(str(item.get("browser_download_url", "")))
    prefix = f"/{plan.repository}/releases/download/"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not parsed.path.startswith(prefix)
        or urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) != expected.name
        or parsed.query
        or parsed.fragment
    ):
        raise ProductionError("GitHub Release asset download identity differs")
    uploader = item.get("uploader")
    if (
        not isinstance(uploader, dict)
        or uploader.get("login") != "github-actions[bot]"
        or uploader.get("type") != "Bot"
    ):
        raise ProductionError("GitHub Release asset uploader differs")
    return item


def _asset_snapshot(
    plan: GitHubReleasePlan,
    client: ReleaseClient,
    release: dict[str, Any],
    *,
    anonymous: bool = False,
) -> dict[str, dict[str, Any]]:
    release_id = release["id"]
    values = client.list_release_assets(release_id, anonymous=anonymous)
    if not isinstance(values, list) or any(
        not isinstance(item, dict) for item in values
    ):
        raise ProductionError("GitHub Release asset list is malformed")
    expected = {asset.name: asset for asset in plan.assets}
    result: dict[str, dict[str, Any]] = {}
    ids: set[int] = set()
    raw_ids = [
        _positive(value.get("id"), "GitHub Release asset id") for value in values
    ]
    if len(raw_ids) != len(set(raw_ids)):
        raise ProductionError("duplicate GitHub Release asset id")
    for value in values:
        name = value.get("name")
        if name not in expected:
            raise ProductionError(f"foreign GitHub Release asset: {name}")
        if name in result:
            raise ProductionError(f"duplicate GitHub Release asset name: {name}")
        item = _remote_asset(plan, release_id, value, expected[name])
        ids.add(item["id"])
        result[name] = item
    return result


def _download(
    expected: ReleaseAsset,
    remote: dict[str, Any],
    client: ReleaseClient,
    *,
    anonymous: bool,
) -> dict[str, Any]:
    raw = client.download_release_asset(remote, anonymous=anonymous)
    if not isinstance(raw, bytes) or len(raw) > _MAX_ASSET_BYTES:
        raise ProductionError("GitHub Release asset download is malformed")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if digest != expected.sha256 or len(raw) != expected.size:
        raise ProductionError(
            f"GitHub Release asset download conflict: {expected.name}"
        )
    return {
        "id": remote["id"],
        "name": expected.name,
        "size": expected.size,
        "digest": expected.sha256,
        "api_url": remote["url"],
        "browser_download_url": remote["browser_download_url"],
        ("anonymous_sha256" if anonymous else "authenticated_sha256"): digest,
    }


def upload_assets(plan: GitHubReleasePlan, client: ReleaseClient) -> dict[str, Any]:
    """Upload only absent assets, recovering response loss by exact readback."""

    prepared = prepare_release(plan, client)
    release, state = _validate_release(plan, prepared["release"])
    observed = _asset_snapshot(plan, client, release)
    if state != "draft" and len(observed) != len(plan.assets):
        raise ProductionError("final GitHub Release asset set is incomplete")
    operations = list(prepared["operations"])
    recovered = prepared["decision"] == "create-response-loss-recovered"
    uploaded = False
    for asset in plan.assets:
        asset.verify()
        if asset.name in observed:
            continue
        if state != "draft":
            raise ProductionError("cannot add assets to a final GitHub Release")
        fresh_release, fresh_state = _find_one(plan, client)
        if (
            fresh_release is None
            or fresh_state != "draft"
            or fresh_release["id"] != release["id"]
        ):
            raise ProductionError("GitHub Release changed before asset upload")
        fresh = _asset_snapshot(plan, client, fresh_release)
        if set(fresh) != set(observed):
            raise ProductionError("GitHub Release asset list drifted before upload")
        try:
            response = client.upload_release_asset(
                release["id"], asset.name, asset.media_type, asset.path
            )
        except GitHubResponseLost:
            fresh = _asset_snapshot(plan, client, fresh_release)
            if asset.name not in fresh:
                raise ProductionError(
                    "GitHub Release upload response was lost without exact readback"
                ) from None
            item = fresh[asset.name]
            recovered = True
            outcome = "response-loss-recovered"
        else:
            item = _remote_asset(plan, release["id"], response, asset)
            fresh = _asset_snapshot(plan, client, fresh_release)
            if fresh.get(asset.name, {}).get("id") != item["id"]:
                raise ProductionError("GitHub Release upload readback differs")
            outcome = "completed"
        _download(asset, item, client, anonymous=False)
        observed = fresh
        uploaded = True
        operations.append(
            {
                "action": "upload-asset",
                "release_id": release["id"],
                "asset_id": item["id"],
                "name": asset.name,
                "outcome": outcome,
            }
        )
    final_release, final_state = _find_one(plan, client)
    if (
        final_release is None
        or final_release["id"] != release["id"]
        or final_state != state
    ):
        raise ProductionError("GitHub Release changed after asset uploads")
    observed = _asset_snapshot(plan, client, final_release)
    if list(observed) != [asset.name for asset in plan.assets]:
        # API order is significant evidence: a partial rerun must preserve prefix order.
        if set(observed) != {asset.name for asset in plan.assets}:
            raise ProductionError("GitHub Release asset set is incomplete")
        observed = {asset.name: observed[asset.name] for asset in plan.assets}
    authenticated = [
        _download(asset, observed[asset.name], client, anonymous=False)
        for asset in plan.assets
    ]
    if not uploaded and not prepared["operations"]:
        decision = "reuse-assets"
    elif recovered:
        decision = "create-assets-response-loss-recovered"
    else:
        decision = "create-assets"
    return sha256_envelope(
        {
            "kind": "ucm-production-github-release-assets",
            "schema_version": 1,
            "stage": plan.stage,
            "reference": plan.reference,
            "decision": decision,
            "release_id": release["id"],
            "release_state": state,
            "assets": authenticated,
            "operations": operations,
        }
    )


def _mandatory_channels(plan: GitHubReleasePlan) -> None:
    observed: set[str] = set()
    for record_value in plan.channel_records:
        record = verify_envelope(
            record_value,
            kind="ucm-production-channel-record",
            schema_version=1,
        )
        if record.get("stage") != plan.stage or record.get("status") != "complete":
            raise ProductionError("mandatory channel is not complete")
        channel = record.get("channel")
        if channel == "ghcr-member":
            identity = "member:" + require_string(record.get("spec_id"), "member spec")
        elif channel == "ghcr-index":
            identity = "index:" + require_string(
                record.get("profile_id"), "index profile"
            )
        elif channel == "chart-oci":
            identity = "chart:" + require_string(record.get("name"), "Chart name")
        else:
            raise ProductionError("mandatory channel record type is invalid")
        if identity in observed:
            raise ProductionError("mandatory channel record is duplicated")
        observed.add(identity)
    expected = {
        *(
            f"member:{profile}-{arch}"
            for profile in ("cuda130", "cann900-a2", "cann900-a3")
            for arch in ("amd64", "arm64")
        ),
        *(f"index:{profile}" for profile in ("cuda130", "cann900-a2", "cann900-a3")),
    }
    if plan.stage != "draft":
        expected.add("chart:unified-cache-pd")
    if observed != expected:
        raise ProductionError("mandatory channel record closure differs")


def readback_release(plan: GitHubReleasePlan, client: ReleaseClient) -> dict[str, Any]:
    """Reopen Release state and every asset byte with the expected visibility."""

    release, state = _find_one(plan, client)
    if release is None:
        raise ProductionError("GitHub Release is absent during readback")
    observed = _asset_snapshot(plan, client, release)
    if set(observed) != {asset.name for asset in plan.assets}:
        raise ProductionError("GitHub Release asset set differs during readback")
    authenticated = [
        _download(asset, observed[asset.name], client, anonymous=False)
        for asset in plan.assets
    ]
    if state == "draft":
        anonymous_release, _ = _find_one(plan, client, anonymous=True)
        if anonymous_release is not None:
            raise ProductionError("Draft GitHub Release is anonymously visible")
        visibility = "private"
        anonymous: object = {"status": "not-found"}
    else:
        anonymous_release, anonymous_state = _find_one(plan, client, anonymous=True)
        if (
            anonymous_release is None
            or anonymous_state != state
            or anonymous_release["id"] != release["id"]
        ):
            raise ProductionError("public GitHub Release anonymous state differs")
        anonymous_observed = _asset_snapshot(
            plan, client, anonymous_release, anonymous=True
        )
        if set(anonymous_observed) != set(observed):
            raise ProductionError("anonymous GitHub Release asset set differs")
        anonymous_assets = [
            _download(
                asset,
                anonymous_observed[asset.name],
                client,
                anonymous=True,
            )
            for asset in plan.assets
        ]
        anonymous = {
            "release": {
                "id": anonymous_release["id"],
                "tag_name": anonymous_release["tag_name"],
                "target_commitish": anonymous_release["target_commitish"],
                "draft": anonymous_release["draft"],
                "prerelease": anonymous_release["prerelease"],
            },
            "assets": anonymous_assets,
        }
        visibility = "public"
    return {
        "release_id": release["id"],
        "release_state": state,
        "visibility": visibility,
        "authenticated_readback": {
            "release": {
                "id": release["id"],
                "tag_name": release["tag_name"],
                "target_commitish": release["target_commitish"],
                "draft": release["draft"],
                "prerelease": release["prerelease"],
            },
            "assets": authenticated,
        },
        "anonymous_readback": anonymous,
        "assets": anonymous["assets"] if state != "draft" else authenticated,
    }


def finalize_release(plan: GitHubReleasePlan, client: ReleaseClient) -> dict[str, Any]:
    """Keep Draft private or transition only a complete preview/final Release."""

    _mandatory_channels(plan)
    uploaded = upload_assets(plan, client)
    release, state = _find_one(plan, client)
    if release is None or release["id"] != uploaded["release_id"]:
        raise ProductionError("GitHub Release changed before finalization")
    operations = list(uploaded["operations"])
    wrote = bool(operations)
    if plan.stage != "draft" and state == "draft":
        payload = {
            "draft": False,
            "prerelease": plan.stage == "rc",
            "make_latest": "false",
        }
        try:
            response = client.patch_release(release["id"], payload)
        except GitHubResponseLost:
            fresh, fresh_state = _find_one(plan, client)
            expected_state = "prerelease" if plan.stage == "rc" else "release"
            if (
                fresh is None
                or fresh["id"] != release["id"]
                or fresh_state != expected_state
            ):
                raise ProductionError(
                    "GitHub Release finalization response was lost without exact readback"
                ) from None
            release, state = fresh, fresh_state
            outcome = "response-loss-recovered"
        else:
            release, state = _validate_release(plan, response)
            expected_state = "prerelease" if plan.stage == "rc" else "release"
            if state != expected_state:
                raise ProductionError("GitHub Release final state differs")
            fresh, fresh_state = _find_one(plan, client)
            if fresh is None or fresh["id"] != release["id"] or fresh_state != state:
                raise ProductionError("GitHub Release final readback differs")
            release = fresh
            outcome = "completed"
        operations.append(
            {
                "action": "finalize-release",
                "release_id": release["id"],
                "outcome": outcome,
            }
        )
        wrote = True
    readback = readback_release(plan, client)
    if plan.stage == "draft" and readback["release_state"] != "draft":
        raise ProductionError("Draft GitHub Release was finalized unexpectedly")
    if plan.stage == "rc" and readback["release_state"] != "prerelease":
        raise ProductionError("RC GitHub Release is not a Pre-release")
    if plan.stage in {"stable", "hotfix"} and readback["release_state"] != "release":
        raise ProductionError("final GitHub Release is not published")
    decision = "reuse" if not wrote else "create"
    return sha256_envelope(
        {
            "kind": "ucm-production-channel-record",
            "schema_version": 1,
            "channel": "github-release",
            "status": "complete",
            "stage": plan.stage,
            "repository": plan.repository,
            "reference": plan.reference,
            "visibility": readback["visibility"],
            "decision": decision,
            "release_id": readback["release_id"],
            "release_state": readback["release_state"],
            "tag_name": plan.tag_name,
            "source_sha": plan.source_sha,
            "candidate_sha256": plan.candidate_sha256,
            "environment_status": plan.environment_status,
            "asset_count": len(plan.assets),
            "assets": readback["assets"],
            "authenticated_readback": readback["authenticated_readback"],
            "anonymous_readback": readback["anonymous_readback"],
            "operations": operations,
        }
    )
