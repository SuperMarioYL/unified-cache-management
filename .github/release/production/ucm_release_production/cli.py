"""File-oriented CLI for the trusted production release controller."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from .build import (
    docker_build_projection,
    prepare_source_context,
    project_build_task,
    seal_built_wheel,
    wheel_build_config_from_task,
)
from .candidate import (
    candidate_run_document,
    compare_trusted_rebuild,
    pack_candidate,
    reopen_candidate,
    seal_candidate,
)
from .chart import package_chart
from .common import ProductionError, load_json, verify_envelope, write_json
from .config import load_config
from .environment import environment_evidence
from .evidence import assemble_evidence, render_summary
from .github_api import GitHubClient, read_trusted_identity
from .github_release import (
    GitHubReleaseClient,
    GitHubReleasePlan,
    ReleaseAsset,
    finalize_release,
)
from .images import (
    extract_oci_archive,
    image_recipe,
    inspect_oci_layout,
    prepare_image_context,
)
from .lineage import resolve_release_lineage
from .reconcile import build_inventory, plan_publication
from .registry import (
    ChartPublishRequest,
    CommandRegistryTransport,
    IndexPublishRequest,
    MemberPublishRequest,
    VisibilityConfigurationRequired,
    publish_chart,
    publish_index,
    publish_member,
)
from .tags import (
    inspect_local_ref_snapshot,
    intent_document,
    parse_tag,
    reopen_intent,
    verify_ref_snapshot,
)
from .workflow_data import (
    candidate_outputs,
    channel_requests,
    channels_list,
    member_request,
    release_request,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ucm-release-production")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate = config_commands.add_parser("validate")
    validate.add_argument("--config", required=True, type=Path)
    tag = commands.add_parser("tag")
    tag_commands = tag.add_subparsers(dest="tag_command", required=True)
    parse = tag_commands.add_parser("parse")
    parse.add_argument("--config", required=True, type=Path)
    parse.add_argument("--tag", required=True)
    parse.add_argument("--output", required=True, type=Path)
    refs = tag_commands.add_parser("verify-refs")
    refs.add_argument("--config", required=True, type=Path)
    refs.add_argument("--intent", required=True, type=Path)
    refs.add_argument("--snapshot", required=True, type=Path)
    refs.add_argument("--output", required=True, type=Path)
    snapshot = tag_commands.add_parser("snapshot-local")
    snapshot.add_argument("--config", required=True, type=Path)
    snapshot.add_argument("--intent", required=True, type=Path)
    snapshot.add_argument("--repository-root", required=True, type=Path)
    snapshot.add_argument("--repository", required=True)
    snapshot.add_argument("--repository-id", required=True, type=int)
    snapshot.add_argument("--default-branch", required=True)
    snapshot.add_argument("--lineage", type=Path)
    snapshot.add_argument("--output", required=True, type=Path)
    lineage = tag_commands.add_parser("resolve-lineage")
    lineage.add_argument("--config", required=True, type=Path)
    lineage.add_argument("--intent", required=True, type=Path)
    lineage.add_argument("--repository", required=True)
    lineage.add_argument("--source-sha", required=True)
    lineage.add_argument("--repository-root", type=Path)
    lineage.add_argument("--output", required=True, type=Path)

    build = commands.add_parser("build")
    build_commands = build.add_subparsers(dest="build_command", required=True)
    task = build_commands.add_parser("task")
    task.add_argument("--config", required=True, type=Path)
    task.add_argument("--intent", required=True, type=Path)
    task.add_argument("--source", required=True, type=Path)
    task.add_argument("--spec-id", required=True)
    task.add_argument("--output", required=True, type=Path)
    context = build_commands.add_parser("source-context")
    context.add_argument("--repository-root", required=True, type=Path)
    context.add_argument("--source-sha", required=True)
    context.add_argument("--output-dir", required=True, type=Path)
    projection = build_commands.add_parser("projection")
    projection.add_argument("--config", required=True, type=Path)
    projection.add_argument("--task", required=True, type=Path)
    projection.add_argument("--source-context", required=True, type=Path)
    projection.add_argument("--source-date-epoch", required=True, type=int)
    projection.add_argument("--output-dir", required=True, type=Path)
    wheel = build_commands.add_parser("seal-wheel")
    wheel.add_argument("--task", required=True, type=Path)
    wheel.add_argument("--authority", required=True, type=Path)
    wheel.add_argument("--raw-wheel", required=True, type=Path)
    wheel.add_argument("--output-dir", required=True, type=Path)

    image = commands.add_parser("image")
    image_commands = image.add_subparsers(dest="image_command", required=True)
    recipe = image_commands.add_parser("recipe")
    recipe.add_argument("--task", required=True, type=Path)
    recipe.add_argument("--intent", required=True, type=Path)
    recipe.add_argument("--config", required=True, type=Path)
    recipe.add_argument("--output", required=True, type=Path)
    extract = image_commands.add_parser("extract")
    extract.add_argument("--archive", required=True, type=Path)
    extract.add_argument("--output-dir", required=True, type=Path)
    inspect = image_commands.add_parser("inspect")
    inspect.add_argument("--layout", required=True, type=Path)
    inspect.add_argument("--recipe", required=True, type=Path)
    inspect.add_argument("--wheel-sha256", required=True)
    inspect.add_argument("--output", required=True, type=Path)
    image_context = image_commands.add_parser("context")
    image_context.add_argument("--config", required=True, type=Path)
    image_context.add_argument("--task", required=True, type=Path)
    image_context.add_argument("--intent", required=True, type=Path)
    image_context.add_argument("--wheel-record", required=True, type=Path)
    image_context.add_argument("--wheel", required=True, type=Path)
    image_context.add_argument("--dockerfile", required=True, type=Path)
    image_context.add_argument("--output-dir", required=True, type=Path)
    image_context.add_argument("--recipe-output", required=True, type=Path)

    chart = commands.add_parser("chart")
    chart_commands = chart.add_subparsers(dest="chart_command", required=True)
    package = chart_commands.add_parser("package")
    package.add_argument("--config", required=True, type=Path)
    package.add_argument("--intent", required=True, type=Path)
    package.add_argument("--chart-dir", required=True, type=Path)
    package.add_argument("--source-sha", required=True)
    package.add_argument("--output-dir", required=True, type=Path)

    candidate = commands.add_parser("candidate")
    candidate_commands = candidate.add_subparsers(
        dest="candidate_command", required=True
    )
    run = candidate_commands.add_parser("run")
    run.add_argument("--source", required=True, type=Path)
    run.add_argument("--workflow-id", required=True, type=int)
    run.add_argument("--run-id", required=True, type=int)
    run.add_argument("--run-attempt", required=True, type=int)
    run.add_argument("--source-date-epoch", required=True, type=int)
    run.add_argument("--output", required=True, type=Path)
    seal = candidate_commands.add_parser("seal")
    seal.add_argument("--root", required=True, type=Path)
    seal.add_argument("--intent", required=True, type=Path)
    seal.add_argument("--run", required=True, type=Path)
    seal.add_argument("--output", required=True, type=Path)
    pack = candidate_commands.add_parser("pack")
    pack.add_argument("--root", required=True, type=Path)
    pack.add_argument("--envelope", required=True, type=Path)
    pack.add_argument("--output", required=True, type=Path)

    trusted = commands.add_parser("trusted")
    trusted_commands = trusted.add_subparsers(dest="trusted_command", required=True)
    identity = trusted_commands.add_parser("identity")
    identity.add_argument("--repository", required=True)
    identity.add_argument("--run-id", required=True, type=int)
    identity.add_argument("--output", required=True, type=Path)
    reopen = trusted_commands.add_parser("reopen-candidate")
    reopen.add_argument("--identity", required=True, type=Path)
    reopen.add_argument("--artifact-dir", required=True, type=Path)
    reopen.add_argument("--output-dir", required=True, type=Path)
    compare = trusted_commands.add_parser("compare-wheels")
    compare.add_argument("--candidate-zip", required=True, type=Path)
    compare.add_argument("--identity", required=True, type=Path)
    compare.add_argument("--trusted-root", required=True, type=Path)
    compare.add_argument("--output", required=True, type=Path)

    workflow = commands.add_parser("workflow-data")
    workflow_commands = workflow.add_subparsers(
        dest="workflow_data_command", required=True
    )
    workflow_candidate = workflow_commands.add_parser("candidate-outputs")
    workflow_candidate.add_argument("--candidate", required=True, type=Path)
    workflow_candidate.add_argument("--github-output", required=True, type=Path)
    workflow_member = workflow_commands.add_parser("member-request")
    workflow_member.add_argument("--config", required=True, type=Path)
    workflow_member.add_argument("--candidate", required=True, type=Path)
    workflow_member.add_argument("--candidate-root", required=True, type=Path)
    workflow_member.add_argument("--spec-id", required=True)
    workflow_member.add_argument("--layout", required=True, type=Path)
    workflow_member.add_argument("--output", required=True, type=Path)
    workflow_channels = workflow_commands.add_parser("channel-requests")
    workflow_channels.add_argument("--config", required=True, type=Path)
    workflow_channels.add_argument("--candidate", required=True, type=Path)
    workflow_channels.add_argument("--candidate-root", required=True, type=Path)
    workflow_channels.add_argument("--member-root", required=True, type=Path)
    workflow_channels.add_argument("--output-dir", required=True, type=Path)
    workflow_release = workflow_commands.add_parser("release-request")
    workflow_release.add_argument("--candidate", required=True, type=Path)
    workflow_release.add_argument("--candidate-root", required=True, type=Path)
    workflow_release.add_argument("--environment", required=True, type=Path)
    workflow_release.add_argument("--channel-root", required=True, type=Path)
    workflow_release.add_argument("--assets-dir", required=True, type=Path)
    workflow_release.add_argument("--output", required=True, type=Path)
    workflow_list = workflow_commands.add_parser("channels-list")
    workflow_list.add_argument("--channel-root", required=True, type=Path)
    workflow_list.add_argument("--release-record", required=True, type=Path)
    workflow_list.add_argument("--output", required=True, type=Path)

    plan = commands.add_parser("plan")
    plan.add_argument("--config", required=True, type=Path)
    plan.add_argument("--intent", required=True, type=Path)
    plan.add_argument("--candidate", required=True, type=Path)
    plan.add_argument("--inventory", type=Path)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--repository-id", required=True, type=int)
    plan.add_argument("--output", required=True, type=Path)

    publish = commands.add_parser("publish")
    publish_commands = publish.add_subparsers(dest="publish_command", required=True)
    member = publish_commands.add_parser("member")
    member.add_argument("--request", required=True, type=Path)
    member.add_argument("--output", required=True, type=Path)
    index = publish_commands.add_parser("index")
    index.add_argument("--request", required=True, type=Path)
    index.add_argument("--output", required=True, type=Path)
    chart_publish = publish_commands.add_parser("chart")
    chart_publish.add_argument("--request", required=True, type=Path)
    chart_publish.add_argument("--output", required=True, type=Path)
    release = publish_commands.add_parser("release")
    release.add_argument("--request", required=True, type=Path)
    release.add_argument("--output", required=True, type=Path)

    evidence = commands.add_parser("evidence")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)
    assemble = evidence_commands.add_parser("assemble")
    assemble.add_argument("--identity", required=True, type=Path)
    assemble.add_argument("--candidate", required=True, type=Path)
    assemble.add_argument("--environment", required=True, type=Path)
    assemble.add_argument("--channels", required=True, type=Path)
    assemble.add_argument("--output", required=True, type=Path)
    summary = evidence_commands.add_parser("summary")
    summary.add_argument("--evidence", required=True, type=Path)
    summary.add_argument("--output", required=True, type=Path)
    environment = evidence_commands.add_parser("environment")
    environment.add_argument("--repository", required=True)
    environment.add_argument("--source-sha", required=True)
    environment.add_argument("--control-sha", required=True)
    environment.add_argument("--control-ref", required=True)
    environment.add_argument("--tag-name", required=True)
    environment.add_argument("--environment", required=True)
    environment.add_argument("--stage", required=True)
    environment.add_argument("--minimum-deployment-id", required=True, type=int)
    environment.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "config" and args.config_command == "validate":
            config = load_config(args.config)
            print(
                json.dumps(
                    {
                        "kind": config["kind"],
                        "schema_version": config["schema_version"],
                        "release_line": config["release_line"],
                        "valid": True,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "tag" and args.tag_command == "parse":
            config = load_config(args.config)
            write_json(
                args.output,
                intent_document(parse_tag(args.tag, config)),
                "tag intent output",
            )
            return 0
        if args.command == "tag" and args.tag_command == "verify-refs":
            config = load_config(args.config)
            intent = reopen_intent(load_json(args.intent, "tag intent"), config)
            snapshot = load_json(args.snapshot, "ref snapshot")
            write_json(
                args.output,
                verify_ref_snapshot(intent, snapshot),
                "source identity output",
            )
            return 0
        if args.command == "tag" and args.tag_command == "snapshot-local":
            config = load_config(args.config)
            intent = reopen_intent(load_json(args.intent, "tag intent"), config)
            lineage = (
                load_json(args.lineage, "release lineage") if args.lineage else None
            )
            snapshot = inspect_local_ref_snapshot(
                args.repository_root,
                intent,
                repository=args.repository,
                repository_id=args.repository_id,
                default_branch=args.default_branch,
                lineage=lineage,
            )
            write_json(args.output, snapshot, "local ref snapshot")
            return 0
        if args.command == "tag" and args.tag_command == "resolve-lineage":
            config = load_config(args.config)
            intent = reopen_intent(load_json(args.intent, "tag intent"), config)
            client = GitHubReleaseClient(
                args.repository, token=os.environ.get("GITHUB_TOKEN")
            )
            value = resolve_release_lineage(
                client,
                intent,
                args.source_sha,
                repository_root=args.repository_root,
            )
            if value is None:
                raise ProductionError("Draft does not require a lineage output")
            write_json(args.output, value, "release lineage")
            return 0
        if args.command == "build" and args.build_command == "task":
            config = load_config(args.config)
            intent = reopen_intent(load_json(args.intent, "tag intent"), config)
            task = project_build_task(
                config,
                intent,
                load_json(args.source, "source identity"),
                args.spec_id,
            )
            write_json(args.output, task, "production build task")
            return 0
        if args.command == "build" and args.build_command == "source-context":
            manifest = prepare_source_context(
                args.repository_root, args.source_sha, args.output_dir
            )
            print(json.dumps(manifest, sort_keys=True))
            return 0
        if args.command == "build" and args.build_command == "projection":
            config = load_config(args.config)
            task_value = load_json(args.task, "production build task")
            context_value = load_json(args.source_context, "source context")
            projected = docker_build_projection(
                config, task_value, context_value, args.source_date_epoch
            )
            args.output_dir.mkdir(parents=True, exist_ok=False)
            write_json(
                args.output_dir / "build-authority.json",
                projected["authority"],
                "production build authority",
            )
            write_json(
                args.output_dir / "build-projection.json",
                {key: value for key, value in projected.items() if key != "authority"},
                "production Docker projection",
            )
            write_json(
                args.output_dir / "wheel-build.json",
                wheel_build_config_from_task(task_value, projected["authority"]),
                "production wheel build config",
            )
            return 0
        if args.command == "build" and args.build_command == "seal-wheel":
            record = seal_built_wheel(
                args.raw_wheel,
                args.output_dir,
                load_json(args.task, "production build task"),
                load_json(args.authority, "production build authority"),
            )
            print(json.dumps(record, sort_keys=True))
            return 0
        if args.command == "image" and args.image_command == "recipe":
            config = load_config(args.config)
            intent = reopen_intent(load_json(args.intent, "tag intent"), config)
            value = image_recipe(
                load_json(args.task, "production build task"), intent.image_tag
            )
            write_json(args.output, value, "production image recipe")
            return 0
        if args.command == "image" and args.image_command == "extract":
            extract_oci_archive(args.archive, args.output_dir)
            return 0
        if args.command == "image" and args.image_command == "inspect":
            value = inspect_oci_layout(
                args.layout,
                load_json(args.recipe, "production image recipe"),
                wheel_sha256=args.wheel_sha256,
            )
            write_json(args.output, value, "production image closure")
            return 0
        if args.command == "image" and args.image_command == "context":
            value = prepare_image_context(
                load_json(args.config, "production release config"),
                load_json(args.task, "production build task"),
                load_json(args.intent, "production tag intent"),
                load_json(args.wheel_record, "production wheel record"),
                args.wheel,
                args.dockerfile,
                args.output_dir,
            )
            write_json(
                args.recipe_output,
                value["recipe"],
                "production image recipe",
            )
            print(json.dumps(value, sort_keys=True))
            return 0
        if args.command == "chart" and args.chart_command == "package":
            config = load_config(args.config)
            intent = reopen_intent(load_json(args.intent, "tag intent"), config)
            value = package_chart(
                args.chart_dir,
                args.output_dir,
                chart_version=intent.chart_version,
                app_version=intent.wheel_version,
                source_sha=args.source_sha,
            )
            print(json.dumps(value, sort_keys=True))
            return 0
        if args.command == "candidate" and args.candidate_command == "run":
            source = load_json(args.source, "source identity")
            value = candidate_run_document(
                repository=source["repository"],
                repository_id=source["repository_id"],
                workflow_id=args.workflow_id,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                source_sha=source["source_commit_sha"],
                tag_name=source["tag_name"],
                tag_object_sha=source["tag_object_sha"],
                source_date_epoch=args.source_date_epoch,
            )
            write_json(args.output, value, "candidate run")
            return 0
        if args.command == "candidate" and args.candidate_command == "seal":
            value = seal_candidate(
                args.root,
                load_json(args.intent, "tag intent"),
                load_json(args.run, "candidate run"),
            )
            write_json(args.output, value, "candidate envelope")
            return 0
        if args.command == "candidate" and args.candidate_command == "pack":
            pack_candidate(
                args.root,
                load_json(args.envelope, "candidate envelope"),
                args.output,
            )
            return 0
        if args.command == "trusted" and args.trusted_command == "identity":
            client = GitHubClient(args.repository, token=os.environ.get("GITHUB_TOKEN"))
            write_json(
                args.output,
                read_trusted_identity(client, args.repository, args.run_id),
                "trusted candidate identity",
            )
            return 0
        if args.command == "trusted" and args.trusted_command == "reopen-candidate":
            identity = load_json(args.identity, "trusted candidate identity")
            if args.output_dir.exists():
                raise ProductionError("candidate reopen output already exists")
            args.output_dir.mkdir(parents=True)
            artifact_dir = Path(args.artifact_dir)
            if not artifact_dir.is_dir() or artifact_dir.is_symlink():
                raise ProductionError("Actions Artifact directory is invalid")
            files = {
                path.name: path
                for path in artifact_dir.iterdir()
                if path.is_file() and not path.is_symlink()
            }
            if set(files) != {"candidate-envelope.json", "candidate.zip"}:
                raise ProductionError("Actions Artifact exact member set differs")
            for name, source in files.items():
                if source.stat().st_size > 6 * 1024 * 1024 * 1024:
                    raise ProductionError("Actions Artifact member is invalid")
                shutil.copyfile(source, args.output_dir / name)
            expected = {
                "repository": identity["repository"],
                "repository_id": identity["repository_id"],
                "tag_name": identity["tag_name"],
                "tag_object_sha": identity["tag_object_sha"],
                "source_sha": identity["source_sha"],
                "run_id": identity["run_id"],
                "run_attempt": identity["run_attempt"],
                "artifact_name": identity["candidate_artifact"]["name"],
            }
            with reopen_candidate(
                args.output_dir / "candidate.zip", expected
            ) as bundle:
                if (
                    load_json(
                        args.output_dir / "candidate-envelope.json",
                        "outer candidate envelope",
                    )
                    != bundle.envelope
                ):
                    raise ProductionError("outer candidate envelope bytes differ")
                if bundle.envelope.get("control_sha") != identity["control_sha"]:
                    raise ProductionError(
                        "candidate control snapshot differs from trusted default branch"
                    )
                write_json(
                    args.output_dir / "verified-envelope.json",
                    bundle.envelope,
                    "verified candidate envelope",
                )
                shutil.copytree(bundle.root, args.output_dir / "candidate")
            return 0
        if args.command == "trusted" and args.trusted_command == "compare-wheels":
            identity = load_json(args.identity, "trusted candidate identity")
            expected = {
                "repository": identity["repository"],
                "repository_id": identity["repository_id"],
                "tag_name": identity["tag_name"],
                "tag_object_sha": identity["tag_object_sha"],
                "source_sha": identity["source_sha"],
                "run_id": identity["run_id"],
                "run_attempt": identity["run_attempt"],
                "artifact_name": identity["candidate_artifact"]["name"],
            }
            with reopen_candidate(args.candidate_zip, expected) as bundle:
                if bundle.envelope.get("control_sha") != identity["control_sha"]:
                    raise ProductionError(
                        "candidate control snapshot differs from trusted default branch"
                    )
                write_json(
                    args.output,
                    compare_trusted_rebuild(bundle, args.trusted_root),
                    "trusted wheel comparison",
                )
            return 0
        if (
            args.command == "workflow-data"
            and args.workflow_data_command == "candidate-outputs"
        ):
            outputs = candidate_outputs(load_json(args.candidate, "candidate envelope"))
            with args.github_output.open("a", encoding="utf-8") as stream:
                for key, value in outputs.items():
                    stream.write(f"{key}={value}\n")
            return 0
        if (
            args.command == "workflow-data"
            and args.workflow_data_command == "member-request"
        ):
            write_json(
                args.output,
                member_request(
                    load_json(args.config, "production release config"),
                    load_json(args.candidate, "candidate envelope"),
                    spec_id=args.spec_id,
                    layout=args.layout,
                    candidate_root=args.candidate_root,
                ),
                "member publish request",
            )
            return 0
        if (
            args.command == "workflow-data"
            and args.workflow_data_command == "channel-requests"
        ):
            print(
                json.dumps(
                    channel_requests(
                        load_json(args.config, "production release config"),
                        load_json(args.candidate, "candidate envelope"),
                        args.candidate_root,
                        args.member_root,
                        args.output_dir,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if (
            args.command == "workflow-data"
            and args.workflow_data_command == "release-request"
        ):
            release_request(
                load_json(args.candidate, "candidate envelope"),
                args.candidate_root,
                load_json(args.environment, "production Environment evidence"),
                args.channel_root,
                args.assets_dir,
                args.output,
            )
            return 0
        if (
            args.command == "workflow-data"
            and args.workflow_data_command == "channels-list"
        ):
            channels_list(args.channel_root, args.release_record, args.output)
            return 0
        if args.command == "plan":
            config = load_config(args.config)
            intent = reopen_intent(load_json(args.intent, "tag intent"), config)
            candidate_value = load_json(args.candidate, "candidate envelope")
            inventory = (
                load_json(args.inventory, "channel inventory")
                if args.inventory
                else build_inventory(args.repository, args.repository_id, [])
            )
            write_json(
                args.output,
                plan_publication(intent, candidate_value, inventory, config),
                "production publish plan",
            )
            return 0
        if args.command == "publish" and args.publish_command == "member":
            request = load_json(args.request, "member publish request")
            closure_value = request["closure"]
            if isinstance(closure_value, str):
                closure_value = load_json(
                    Path(closure_value), "candidate member closure"
                )
            if (
                isinstance(closure_value, dict)
                and closure_value.get("kind") == "ucm-production-image-member-closure"
            ):
                verified_closure = verify_envelope(
                    closure_value,
                    kind="ucm-production-image-member-closure",
                    schema_version=1,
                )
                closure_value = {
                    key: value
                    for key, value in verified_closure.items()
                    if key
                    not in {
                        "kind",
                        "schema_version",
                        "task_sha256",
                        "wheel_sha256",
                        "recipe_sha256",
                        "sha256",
                    }
                }
            value = publish_member(
                MemberPublishRequest(
                    stage=request["stage"],
                    spec_id=request["spec_id"],
                    repository=request["repository"],
                    tag=request["tag"],
                    layout=Path(request["layout"]),
                    closure=closure_value,
                    visibility=request["visibility"],
                ),
                CommandRegistryTransport(),
            )
            write_json(args.output, value, "member channel record")
            return 0
        if args.command == "publish" and args.publish_command == "index":
            request = load_json(args.request, "index publish request")
            records = tuple(
                load_json(Path(path), "member channel record")
                for path in request["member_records"]
            )
            if len(records) != 2:
                raise ProductionError("index requires exactly two member records")
            value = publish_index(
                IndexPublishRequest(
                    stage=request["stage"],
                    profile_id=request["profile_id"],
                    repository=request["repository"],
                    tag=request["tag"],
                    source_sha=request["source_sha"],
                    members=(records[0], records[1]),
                    visibility=request["visibility"],
                ),
                CommandRegistryTransport(),
            )
            write_json(args.output, value, "index channel record")
            return 0
        if args.command == "publish" and args.publish_command == "chart":
            request = load_json(args.request, "Chart publish request")
            value = publish_chart(
                ChartPublishRequest(
                    stage=request["stage"],
                    name=request["name"],
                    version=request["version"],
                    chart=Path(request["chart"]),
                    helm_repository=request["helm_repository"],
                    reference=request["reference"],
                    file_sha256=request["file_sha256"],
                    visibility=request["visibility"],
                ),
                CommandRegistryTransport(),
            )
            write_json(args.output, value, "Chart channel record")
            return 0
        if args.command == "publish" and args.publish_command == "release":
            request = load_json(args.request, "GitHub Release publish request")
            assets = tuple(
                ReleaseAsset.from_path(Path(path)) for path in request["assets"]
            )
            channels = tuple(
                load_json(Path(path), "channel record")
                for path in request["channel_records"]
            )
            release_plan = GitHubReleasePlan(
                stage=request["stage"],
                repository=request["repository"],
                repository_id=request["repository_id"],
                tag_name=request["tag_name"],
                source_sha=request["source_sha"],
                version=request["version"],
                candidate_sha256=request["candidate_sha256"],
                environment_status=request["environment_status"],
                assets=assets,
                channel_records=channels,
            )
            client = GitHubReleaseClient(
                request["repository"], token=os.environ.get("GITHUB_TOKEN")
            )
            write_json(
                args.output,
                finalize_release(release_plan, client),
                "GitHub Release channel record",
            )
            return 0
        if args.command == "evidence" and args.evidence_command == "assemble":
            channels_value = load_json(args.channels, "channel record list")
            if not isinstance(channels_value, list):
                raise ProductionError("channel record list must be an array")
            write_json(
                args.output,
                assemble_evidence(
                    load_json(args.identity, "trusted identity"),
                    load_json(args.candidate, "candidate envelope"),
                    load_json(args.environment, "environment evidence"),
                    channels_value,
                ),
                "production release evidence",
            )
            return 0
        if args.command == "evidence" and args.evidence_command == "summary":
            args.output.write_text(
                render_summary(load_json(args.evidence, "production evidence")),
                encoding="utf-8",
            )
            return 0
        if args.command == "evidence" and args.evidence_command == "environment":
            client = GitHubClient(args.repository, token=os.environ.get("GITHUB_TOKEN"))
            write_json(
                args.output,
                environment_evidence(
                    client,
                    repository=args.repository,
                    source_sha=args.source_sha,
                    control_sha=args.control_sha,
                    control_ref=args.control_ref,
                    tag_name=args.tag_name,
                    environment=args.environment,
                    stage=args.stage,
                    minimum_deployment_id=args.minimum_deployment_id,
                ),
                "production environment evidence",
            )
            return 0
        raise ProductionError("unsupported command")
    except VisibilityConfigurationRequired as error:
        output = getattr(locals().get("args"), "output", None)
        if isinstance(output, Path):
            write_json(output, error.record, "visibility hold channel record")
        print(f"production release validation failed: {error}", file=sys.stderr)
        return 3
    except (ProductionError, OSError, KeyError, TypeError) as error:
        print(f"production release validation failed: {error}", file=sys.stderr)
        return 2
