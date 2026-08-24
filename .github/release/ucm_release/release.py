"""Build staged GitHub Release manifests from validated build receipts."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")
    return value


def _list(value: object, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a JSON array")
    return value


def _one(paths: list[Path], context: str) -> Path:
    if len(paths) != 1:
        raise ValueError(f"{context} must resolve exactly once, found {len(paths)}")
    return paths[0]


def _validate_wheel_result_contract(result: dict[str, Any], result_path: Path) -> None:
    context = f"Wheel result {result_path}"
    if result.get("kind") != "ucm-wheel-result" or result.get("schema_version") != 2:
        raise ValueError(f"{context} must use ucm-wheel-result schema 2")

    audit_fields = {
        "platform_tags",
        "auditwheel_platform_tag",
        "glibc_versions",
        "glibc_floor",
        "external_libraries",
        "auditwheel_report",
    }
    missing_fields = sorted(audit_fields - result.keys())
    if missing_fields:
        raise ValueError(f"{context} is missing audit fields: {missing_fields}")

    platform_tags = result.get("platform_tags")
    if (
        not isinstance(platform_tags, list)
        or not platform_tags
        or any(not isinstance(value, str) or not value for value in platform_tags)
    ):
        raise ValueError(f"{context} has invalid platform_tags")
    auditwheel_platform = result.get("auditwheel_platform_tag")
    if not isinstance(auditwheel_platform, str) or not auditwheel_platform:
        raise ValueError(f"{context} has no auditwheel_platform_tag")

    glibc_versions = result.get("glibc_versions")
    if not isinstance(glibc_versions, list) or any(
        not isinstance(value, str) or not value for value in glibc_versions
    ):
        raise ValueError(f"{context} has invalid glibc_versions")
    glibc_floor = result.get("glibc_floor")
    if glibc_floor is not None and (
        not isinstance(glibc_floor, str) or glibc_floor not in glibc_versions
    ):
        raise ValueError(f"{context} has an invalid glibc_floor")
    external_libraries = result.get("external_libraries")
    if not isinstance(external_libraries, list) or any(
        not isinstance(value, str) or not value for value in external_libraries
    ):
        raise ValueError(f"{context} has invalid external_libraries")

    report = _mapping(result.get("auditwheel_report"), f"{context} auditwheel report")
    report_name = report.get("filename")
    if (
        not isinstance(report_name, str)
        or not report_name
        or report_name in {".", ".."}
        or Path(report_name).name != report_name
    ):
        raise ValueError(f"{context} has an invalid auditwheel report filename")
    report_path = result_path.parent / report_name
    if not report_path.is_file():
        raise ValueError(f"{context} has no matching auditwheel report file")
    report_digest = report.get("sha256")
    report_text = report.get("text")
    if not isinstance(report_digest, str) or _sha256(report_path) != report_digest:
        raise ValueError(f"{context} auditwheel report digest does not match")
    if (
        not isinstance(report_text, str)
        or report_path.read_text(encoding="utf-8") != report_text
    ):
        raise ValueError(f"{context} auditwheel report text does not match")


def _family_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    families = {
        str(item["id"]): _mapping(item, "release plan family")
        for item in _list(plan.get("families"), "release plan families")
    }
    if len(families) != len(plan["families"]):
        raise ValueError("release plan family IDs must be unique")
    return families


def _expected_targets(plan: dict[str, Any], reference: str) -> dict[str, str]:
    publish = _mapping(plan.get("publish"), "release plan publish")
    expected: dict[str, str] = {}
    if _mapping(publish.get("ghcr"), "release plan GHCR").get("enabled") is True:
        expected["ghcr"] = reference
    dockerhub = _mapping(publish.get("dockerhub"), "release plan Docker Hub")
    if (
        dockerhub.get("enabled") is True
        and plan.get("release_kind", "publish") != "draft"
    ):
        repository, separator, tag = reference.rpartition(":")
        if not separator or not repository or not tag:
            raise ValueError(f"planned image reference is invalid: {reference!r}")
        namespace = dockerhub.get("namespace")
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("enabled Docker Hub publication has no namespace")
        expected["dockerhub"] = f"{namespace}/{repository.rsplit('/', 1)[-1]}:{tag}"
    return expected


def build_artifacts_manifest(
    plan: dict[str, Any], wheels_root: Path, chart_root: Path
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Validate Wheel/Chart outputs and return the artifacts-ready manifest."""
    tasks = {
        str(item["id"]): _mapping(item, "release plan Wheel")
        for item in _list(plan.get("wheels"), "release plan Wheels")
    }
    if len(tasks) != len(plan["wheels"]):
        raise ValueError("release plan Wheel IDs must be unique")

    results: dict[str, dict[str, Any]] = {}
    wheel_files: dict[str, Path] = {}
    for result_path in sorted(wheels_root.rglob("wheel-result.json")):
        result = _mapping(_load_json(result_path), f"Wheel result {result_path}")
        _validate_wheel_result_contract(result, result_path)
        task_id = str(result.get("task_id", ""))
        if task_id not in tasks or task_id in results:
            raise ValueError(f"Wheel result {task_id!r} is unknown or duplicated")
        filename = str(result.get("filename", ""))
        wheel_path = result_path.parent / filename
        if not filename or not wheel_path.is_file():
            raise ValueError(f"Wheel result {task_id!r} has no matching file")
        task = tasks[task_id]
        expected = {
            "distribution": task["dist_name"],
            "version": task["wheel_version"],
            "python_abi": task["python_abi"],
            "cpu_arch": task["cpu_arch"],
        }
        if any(result.get(key) != value for key, value in expected.items()):
            raise ValueError(f"Wheel result {task_id!r} does not match its plan")
        results[task_id] = result
        wheel_files[task_id] = wheel_path

    if set(results) != set(tasks):
        missing = sorted(set(tasks) - set(results))
        raise ValueError(f"Wheel results do not cover the plan: {missing}")
    discovered_wheels = {path.resolve() for path in wheels_root.rglob("*.whl")}
    declared_wheels = {path.resolve() for path in wheel_files.values()}
    if discovered_wheels != declared_wheels:
        raise ValueError("Wheel files and result manifests must match exactly")
    filenames = [str(item["filename"]) for item in results.values()]
    if len(filenames) != len(set(filenames)):
        raise ValueError("Wheel result filenames must be unique")

    chart_path = _one(sorted(chart_root.rglob("*.tgz")), "Chart package")
    checksums: list[tuple[str, str]] = []
    wheels: list[dict[str, Any]] = []
    for task_id in sorted(results):
        result = copy.deepcopy(results[task_id])
        task = tasks[task_id]
        digest = _sha256(wheel_files[task_id])
        result["id"] = result.pop("task_id")
        result["sha256"] = digest
        result["backend"] = task["backend"]
        result["runtime_variant"] = task["runtime_variant"]
        result["manylinux_builder"] = task["manylinux"]
        builder = _mapping(task.get("builder"), f"Wheel task {task_id} Builder")
        if (
            not isinstance(builder.get("digest"), str)
            or _DIGEST.fullmatch(builder["digest"]) is None
        ):
            raise ValueError(f"Wheel task {task_id} has no immutable Builder digest")
        result["builder"] = copy.deepcopy(builder)
        wheels.append(result)
        checksums.append((digest, str(result["filename"])))

    chart_digest = _sha256(chart_path)
    checksums.append((chart_digest, chart_path.name))
    families = _family_map(plan)
    images = []
    for raw in _list(plan.get("images"), "release plan Images"):
        item = _mapping(raw, "release plan Image")
        family = families.get(str(item["family_id"]))
        if family is None:
            raise ValueError(f"Image {item.get('id')!r} has no release family")
        member = _one(
            [
                value
                for value in family["members"]
                if value.get("image_id") == item.get("id")
            ],
            f"Image {item.get('id')!r} family member",
        )
        images.append(
            {
                "id": item["id"],
                "family_id": item["family_id"],
                "wheel_id": item["wheel_id"],
                "cpu_arch": item["cpu_arch"],
                "runtime": copy.deepcopy(item["runtime"]),
                "planned_reference": member["reference"],
                "expected_targets": _expected_targets(plan, member["reference"]),
                "status": "building",
                "targets": [],
            }
        )

    manifest = {
        "kind": "ucm-release-manifest",
        "schema_version": 1,
        "release": {
            "git_tag": plan["git_tag"],
            "release_kind": plan.get("release_kind", "publish"),
            "is_prerelease": plan.get("is_prerelease", True),
            "version": plan["version"],
            "status": "artifacts-ready",
        },
        "chart": {
            "name": plan["chart"]["name"],
            "version": plan["chart"]["version"],
            "app_version": plan["chart"]["app_version"],
            "filename": chart_path.name,
            "sha256": chart_digest,
        },
        "wheels": wheels,
        "images": sorted(images, key=lambda item: str(item["id"])),
        "families": [
            {
                "id": family["id"],
                "runtime": copy.deepcopy(family["runtime"]),
                "planned_reference": family["published_reference"],
                "expected_targets": _expected_targets(
                    plan, family["published_reference"]
                ),
                "create_index": family["create_index"],
                "status": "building",
                "targets": [],
            }
            for family in sorted(families.values(), key=lambda item: str(item["id"]))
        ],
    }
    return manifest, sorted(checksums, key=lambda item: item[1])


def _receipt_map(receipts_root: Path, kind: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not receipts_root.exists():
        return result
    for path in sorted(receipts_root.rglob("*.json")):
        receipt = _mapping(_load_json(path), f"publication receipt {path}")
        if receipt.get("kind") != kind:
            continue
        if receipt.get("schema_version") != 1 or set(receipt) != {
            "kind",
            "schema_version",
            "id",
            "status",
            "targets",
        }:
            raise ValueError(f"publication receipt {path} has an invalid contract")
        identity = str(receipt.get("id", ""))
        if not identity or identity in result:
            raise ValueError(
                f"publication receipt {identity!r} is missing or duplicated"
            )
        result[identity] = receipt
    return result


def _validated_receipt_targets(
    receipt: dict[str, Any], expected: object, context: str
) -> list[dict[str, str]]:
    expected_targets = _mapping(expected, f"{context} expected targets")
    if receipt.get("status") not in {"published", "failed"}:
        raise ValueError(f"{context} has an invalid status")
    raw_targets = _list(receipt.get("targets"), f"{context} targets")
    targets: list[dict[str, str]] = []
    observed: dict[str, str] = {}
    for index, raw_target in enumerate(raw_targets):
        target = _mapping(raw_target, f"{context} targets[{index}]")
        if set(target) != {"channel", "reference", "digest"}:
            raise ValueError(f"{context} target fields must be exact")
        channel = target.get("channel")
        reference = target.get("reference")
        digest = target.get("digest")
        if (
            not isinstance(channel, str)
            or channel in observed
            or channel not in expected_targets
        ):
            raise ValueError(f"{context} has an unexpected or duplicate channel")
        if not isinstance(reference, str) or reference != expected_targets[channel]:
            raise ValueError(f"{context} target does not match its planned reference")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError(f"{context} target digest is invalid")
        observed[channel] = reference
        targets.append({"channel": channel, "reference": reference, "digest": digest})
    if receipt["status"] == "published" and set(observed) != set(expected_targets):
        raise ValueError(f"{context} does not cover its expected publication targets")
    return targets


def finalize_manifest(
    manifest: dict[str, Any],
    receipts_root: Path,
    *,
    build_outcome: str,
    member_outcome: str,
    index_outcome: str,
) -> dict[str, Any]:
    """Merge immutable publication receipts and close the image stage."""
    result = copy.deepcopy(manifest)
    members = _receipt_map(receipts_root, "ucm-image-member-receipt")
    indexes = _receipt_map(receipts_root, "ucm-image-index-receipt")
    expected_image_ids = {str(item["id"]) for item in result["images"]}
    expected_index_ids = {
        str(item["id"]) for item in result["families"] if item["create_index"]
    }
    if set(members) - expected_image_ids:
        raise ValueError("member receipts contain unknown Image IDs")
    if set(indexes) - expected_index_ids:
        raise ValueError("index receipts contain unknown family IDs")

    failed = build_outcome != "success" or member_outcome != "success"
    for image in result["images"]:
        receipt = members.get(str(image["id"]))
        targets = (
            _validated_receipt_targets(
                receipt, image.get("expected_targets"), f"Image {image['id']} receipt"
            )
            if receipt is not None
            else []
        )
        if receipt is not None and receipt.get("status") == "published":
            image["status"] = "published"
            image["targets"] = targets
        else:
            image["status"] = "failed"
            image["targets"] = targets
            failed = True

    if expected_index_ids and index_outcome != "success":
        failed = True
    for family in result["families"]:
        family_id = str(family["id"])
        if family["create_index"]:
            receipt = indexes.get(family_id)
            targets = (
                _validated_receipt_targets(
                    receipt,
                    family.get("expected_targets"),
                    f"family {family_id} receipt",
                )
                if receipt is not None
                else []
            )
            if receipt is not None and receipt.get("status") == "published":
                family["status"] = "published"
                family["targets"] = targets
            else:
                family["status"] = "failed"
                family["targets"] = targets
                failed = True
        else:
            members_for_family = [
                image
                for image in result["images"]
                if image["family_id"] == family["id"]
            ]
            if len(members_for_family) != 1:
                raise ValueError(f"single-arch family {family_id!r} is ambiguous")
            member = members_for_family[0]
            family["status"] = member["status"]
            family["targets"] = copy.deepcopy(member["targets"])
            failed = failed or member["status"] != "published"

    result["release"]["status"] = "images-failed" if failed else "complete"
    return result


def render_notes(manifest: dict[str, Any]) -> str:
    """Render concise Release notes from the manifest's current stage."""
    release = manifest["release"]
    lines = [
        f"# UCM {release['git_tag']}",
        "",
        f"Status: `{release['status']}`",
        "",
    ]
    if release["status"] != "release-open":
        lines.extend(
            [
                f"Wheels: {len(manifest.get('wheels', []))}",
                f"Chart: `{manifest.get('chart', {}).get('filename', 'building')}`",
                "",
            ]
        )
        wheel_names = {
            str(item["id"]): str(item["filename"])
            for item in manifest.get("wheels", [])
        }
        lines.extend(["Runtime / Wheel mapping:", ""])
        for family in manifest.get("families", []):
            runtime = family.get("runtime", {})
            runtime_ref = f"{runtime.get('repository')}:{runtime.get('tag')}"
            members = sorted(
                (
                    item
                    for item in manifest.get("images", [])
                    if item.get("family_id") == family.get("id")
                ),
                key=lambda item: str(item.get("cpu_arch")),
            )
            mapping = ", ".join(
                f"{item['cpu_arch']}={wheel_names.get(str(item['wheel_id']), 'missing')}"
                for item in members
            )
            lines.append(f"- `{runtime_ref}`: `{mapping}`")
        lines.append("")
    if release["status"] == "artifacts-ready":
        lines.append("Wheels and Chart are available. Images are still building.")
    elif release["status"] == "complete":
        lines.extend(["Published images:", ""])
        for family in manifest["families"]:
            targets = family.get("targets", [])
            rendered = ", ".join(
                f"`{item['reference']}@{item['digest']}`" for item in targets
            )
            lines.append(f"- {family['id']}: {rendered or 'no remote target enabled'}")
    elif release["status"] == "images-failed":
        lines.append(
            "Wheels and Chart remain available, but one or more images failed to publish."
        )
    else:
        lines.append("Wheels, Chart, and images are building.")
    lines.extend(
        [
            "",
            "See `release-manifest.json` for the Runtime member → Wheel → Image mapping.",
        ]
    )
    return "\n".join(lines) + "\n"


def _artifacts(arguments: argparse.Namespace) -> None:
    plan = _mapping(_load_json(arguments.plan), "release plan")
    manifest, checksums = build_artifacts_manifest(
        plan, arguments.wheels, arguments.chart
    )
    _write_json(arguments.output / "release-manifest.json", manifest)
    (arguments.output / "SHA256SUMS").write_text(
        "".join(f"{digest}  {filename}\n" for digest, filename in checksums),
        encoding="utf-8",
    )
    (arguments.output / "release-notes.md").write_text(
        render_notes(manifest), encoding="utf-8"
    )


def _finalize(arguments: argparse.Namespace) -> None:
    manifest = _mapping(_load_json(arguments.manifest), "release manifest")
    result = finalize_manifest(
        manifest,
        arguments.receipts,
        build_outcome=arguments.build_outcome,
        member_outcome=arguments.member_outcome,
        index_outcome=arguments.index_outcome,
    )
    _write_json(arguments.output / "release-manifest.json", result)
    (arguments.output / "release-notes.md").write_text(
        render_notes(result), encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    artifacts = commands.add_parser("artifacts")
    artifacts.add_argument("--plan", type=Path, required=True)
    artifacts.add_argument("--wheels", type=Path, required=True)
    artifacts.add_argument("--chart", type=Path, required=True)
    artifacts.add_argument("--output", type=Path, required=True)
    artifacts.set_defaults(func=_artifacts)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--manifest", type=Path, required=True)
    finalize.add_argument("--receipts", type=Path, required=True)
    finalize.add_argument("--build-outcome", required=True)
    finalize.add_argument("--member-outcome", required=True)
    finalize.add_argument("--index-outcome", required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.set_defaults(func=_finalize)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True)
    arguments.func(arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
