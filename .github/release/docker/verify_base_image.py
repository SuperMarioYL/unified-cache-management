#!/usr/bin/env python3
"""Verify that Docker's FROM input is the exact authorized platform subject."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

DIGEST = r"sha256:[0-9a-f]{64}"
SUBJECT_RE = re.compile(
    rf"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?"
    rf"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@{DIGEST}"
)
INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
CONFIG_MEDIA_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}


def _load(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r} in {path}")
            value[key] = item
        return value

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _raw_json(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValueError(f"{label} raw bytes must be a string")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r} in {label}")
            value[key] = item
        return value

    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _blob(
    value: object, label: str, allowed_media_types: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {
        "media_type",
        "digest",
        "size",
        "raw",
    }:
        raise ValueError(f"{label} blob fields are noncanonical")
    if value["media_type"] not in allowed_media_types:
        raise ValueError(f"{label} media type is invalid")
    if not isinstance(value["size"], int) or isinstance(value["size"], bool):
        raise ValueError(f"{label} size is invalid")
    raw = value["raw"]
    if not isinstance(raw, str):
        raise ValueError(f"{label} raw bytes must be a string")
    raw_bytes = raw.encode("utf-8")
    digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    if value["size"] != len(raw_bytes) or value["digest"] != digest:
        raise ValueError(f"{label} raw bytes do not match digest/size")
    return value, _raw_json(raw, label)


def _validated_base(
    base: object, target_platform: str, candidate_kind: str
) -> tuple[str, str]:
    required = {
        "schema_version",
        "kind",
        "fixture_only",
        "repository",
        "index",
        "manifest",
        "config",
        "platform",
        "subject",
    }
    if not isinstance(base, dict) or set(base) != required:
        raise ValueError("recipe base record has noncanonical fields")
    identities = {
        "fixture-candidate": ("fixture-base-image-record", True),
        "real-candidate": ("ucm-real-base-image-record", False),
    }
    if candidate_kind not in identities:
        raise ValueError("recipe candidate kind is invalid")
    expected_kind, fixture_only = identities[candidate_kind]
    if (
        base["schema_version"] != 1
        or base["kind"] != expected_kind
        or base["fixture_only"] is not fixture_only
    ):
        raise ValueError("recipe base record candidate identity is invalid")
    try:
        target_os, target_architecture = target_platform.split("/", 1)
    except ValueError as error:
        raise ValueError("Docker target platform must be os/architecture") from error
    index_blob, index = _blob(base["index"], "base index", INDEX_MEDIA_TYPES)
    manifest_blob, manifest = _blob(
        base["manifest"], "base manifest", MANIFEST_MEDIA_TYPES
    )
    config_blob, config = _blob(base["config"], "base config", CONFIG_MEDIA_TYPES)
    if (
        index.get("schemaVersion") != 2
        or index.get("mediaType") != index_blob["media_type"]
        or not isinstance(index.get("manifests"), list)
    ):
        raise ValueError("base index structure is invalid")
    descriptors = []
    for descriptor in index["manifests"]:
        if not isinstance(descriptor, dict):
            raise ValueError("base index descriptor must be an object")
        platform = descriptor.get("platform")
        if isinstance(platform, dict) and (
            platform.get("os"),
            platform.get("architecture"),
        ) == (target_os, target_architecture):
            descriptors.append(descriptor)
    if len(descriptors) != 1:
        raise ValueError("base index must contain one exact target descriptor")
    descriptor = descriptors[0]
    if (
        descriptor.get("mediaType") != manifest_blob["media_type"]
        or descriptor.get("digest") != manifest_blob["digest"]
        or descriptor.get("size") != manifest_blob["size"]
    ):
        raise ValueError("base index descriptor does not bind manifest bytes")
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") != manifest_blob["media_type"]
        or not isinstance(manifest.get("config"), dict)
        or not isinstance(manifest.get("layers"), list)
    ):
        raise ValueError("base manifest structure is invalid")
    config_descriptor = manifest["config"]
    if (
        config_descriptor.get("mediaType") != config_blob["media_type"]
        or config_descriptor.get("digest") != config_blob["digest"]
        or config_descriptor.get("size") != config_blob["size"]
    ):
        raise ValueError("base manifest descriptor does not bind config bytes")
    if (config.get("os"), config.get("architecture")) != (
        target_os,
        target_architecture,
    ):
        raise ValueError("base config platform does not match target")
    descriptor_platform = descriptor["platform"]
    if descriptor_platform.get("variant") != config.get("variant"):
        raise ValueError("base index/config platform variants do not match")
    expected_platform = {
        "os": target_os,
        "architecture": target_architecture,
        "variant": config.get("variant"),
        "manifest_media_type": manifest_blob["media_type"],
        "manifest_digest": manifest_blob["digest"],
        "manifest_size": manifest_blob["size"],
        "config_media_type": config_blob["media_type"],
        "config_digest": config_blob["digest"],
        "config_size": config_blob["size"],
    }
    if base["platform"] != expected_platform:
        raise ValueError(
            "derived base platform summary does not match descriptor bytes"
        )
    subject = f"{base['repository']}@{manifest_blob['digest']}"
    if SUBJECT_RE.fullmatch(subject) is None or base["subject"] != subject:
        raise ValueError(
            "authorized base subject is not an immutable repository digest"
        )
    return subject, f"{target_os}/{target_architecture}"


def verify(recipe_path: Path, base_image: str, target_platform: str) -> dict[str, Any]:
    recipe = _load(recipe_path)
    if set(recipe) != {"payload", "payload_sha256"}:
        raise ValueError(
            "recipe envelope must contain exactly payload and payload_sha256"
        )
    actual_recipe_sha256 = (
        "sha256:" + hashlib.sha256(_canonical(recipe["payload"])).hexdigest()
    )
    if recipe["payload_sha256"] != actual_recipe_sha256:
        raise ValueError("recipe payload digest mismatch")
    payload = recipe["payload"]
    base = payload.get("base") if isinstance(payload, dict) else None
    candidate_kind = payload.get("candidate_kind", "fixture-candidate")
    subject, expected_platform = _validated_base(base, target_platform, candidate_kind)
    if (
        target_platform != payload.get("target_platform")
        or target_platform != expected_platform
    ):
        raise ValueError("Docker target platform does not match the authorized recipe")
    if SUBJECT_RE.fullmatch(base_image) is None:
        raise ValueError("BASE_IMAGE must be an immutable repository@sha256 subject")
    if base_image != subject:
        raise ValueError("BASE_IMAGE does not match the authorized recipe subject")
    return {
        "schema_version": 1,
        "kind": "ucm-base-verification",
        "base_subject": subject,
        "target_platform": target_platform,
        "status": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--target-platform", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.recipe, args.base_image, args.target_platform)
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
