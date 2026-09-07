#!/usr/bin/env python3
"""Build, preview and translate UCM documentation."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
LANGS = ("en", "zh")


def _run(cmd: list[str]) -> int:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


def _mkdocs(*args: str) -> int:
    return _run([sys.executable, "-m", "mkdocs", *args])


def serve(args: argparse.Namespace) -> int:
    """Serve the site locally with live reload (both languages)."""
    return _mkdocs("serve", "--dev-addr", args.dev_addr)


def build(args: argparse.Namespace) -> int:
    from site_build import build_site

    build_site(
        language=args.lang,
        site_dir=args.site_dir or ROOT / "site" / args.lang,
        site_url=args.site_url
        or f"http://127.0.0.1:8000/{'zh-cn' if args.lang == 'zh' else 'en'}/latest/",
        strict=args.strict,
        repository=args.repository,
        ref=args.ref,
        manifest_path=args.manifest,
    )
    return 0


def validate(args: argparse.Namespace) -> int:
    from site_build import build_site

    for language, slug in (("en", "en"), ("zh", "zh-cn")):
        build_site(
            language=language,
            site_dir=ROOT / "site" / language,
            site_url=f"https://docs.example.invalid/{slug}/latest/",
            strict=True,
        )
    return 0


def rtd(args: argparse.Namespace) -> int:
    from site_build import build_readthedocs

    build_readthedocs()
    return 0


def _translation_root(checkout_root: Path | None) -> Path:
    if checkout_root is None:
        return ROOT
    candidate = checkout_root.resolve() / "docs-next"
    if not candidate.is_dir():
        raise OSError(f"docs-next root does not exist below {checkout_root}")
    return candidate


def translate(args: argparse.Namespace) -> int:
    """Prepare, finalize, check, or apply Chinese translation artifacts."""

    try:
        from translation.agent_pipeline import (
            TranslationProvenance,
            apply_translation_artifact,
            finalize_translation,
            load_agent_instructions,
            prepare_translation,
            resolve_git_sha,
        )
        from translation.core import (
            Glossary,
            TranslationError,
            TranslationIdentity,
            check_translations,
        )
        from translation.markdown import MarkdownTranslationError

        operation = args.translation_operation
        docs_root = _translation_root(getattr(args, "checkout_root", None))
        if operation == "prepare":
            mode = "changed" if args.changed else "missing"
            glossary = Glossary.load(docs_root / "translation-glossary.json")
            instructions = load_agent_instructions(args.instructions)
            identity = TranslationIdentity.from_runtime(
                provider=args.api_format,
                model=args.model,
                base_url=args.base_url,
                api_host=urlparse(args.base_url).hostname or "",
                agent_instructions=instructions,
                glossary=glossary,
            )
            provenance = TranslationProvenance(
                mode=mode,
                base_sha=resolve_git_sha(docs_root, args.base_ref),
                head_sha=args.head_sha,
                base_repository_id=args.base_repository_id,
                head_repository_id=args.head_repository_id,
                pr_number=args.pr_number,
            )
            task = prepare_translation(
                docs_root,
                mode=mode,
                output_dir=args.output_dir,
                provenance=provenance,
                identity=identity,
                base_ref=args.base_ref,
                glossary=glossary,
                agent_instructions_path=args.instructions,
                max_pages=args.max_pages,
            )
            print(
                "[translation] "
                + json.dumps(task.summary, ensure_ascii=True, sort_keys=True)
            )
            return 0

        if operation == "generate":
            from translation.api import generate_translation

            count = generate_translation(
                docs_root,
                task_path=args.task_dir / "translation-task.json",
                output_dir=args.output_dir,
                instructions_path=args.instructions,
            )
            print(f"[translation] generated_chunks={count}")
            return 0

        if operation == "finalize":
            artifact = finalize_translation(
                docs_root,
                task_path=args.task_dir / "translation-task.json",
                agent_output_dir=args.agent_output_dir,
                output_dir=args.output_dir,
                agent_instructions_path=args.instructions,
            )
            print(
                "[translation] "
                + json.dumps(artifact.summary, ensure_ascii=True, sort_keys=True)
            )
            return 0

        if operation == "check":
            check_result = check_translations(docs_root, base_ref=args.base_ref)
            for issue in check_result.issues:
                print(
                    f"[translation] {'error' if issue.blocking else 'warning'}: "
                    f"{issue.path}: {issue.code}: {issue.message}",
                    file=sys.stderr,
                )
            print(
                "[translation] "
                f"checked={check_result.checked_files} "
                f"issues={len(check_result.issues)}"
            )
            return 0 if check_result.ok else 1

        if operation == "apply":
            changed = apply_translation_artifact(
                docs_root,
                artifact_dir=args.artifact_dir,
            )
            print(
                "[translation] "
                + json.dumps(
                    {"applied": [str(path) for path in changed]},
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
            return 0
        raise TranslationError(f"unsupported translation operation: {operation}")
    except (MarkdownTranslationError, TranslationError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="site.py",
        description="UCM MkDocs documentation site entry point.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Serve the site locally.")
    p_serve.add_argument(
        "--dev-addr", default="127.0.0.1:8000", help="Dev server address."
    )
    p_serve.set_defaults(func=serve)

    p_build = sub.add_parser("build", help="Build the site.")
    p_build.add_argument(
        "--lang", default="en", choices=LANGS, help="Build only this language."
    )
    p_build.add_argument(
        "--strict", action="store_true", help="Treat warnings as errors."
    )
    p_build.add_argument(
        "--clean", action="store_true", help="Remove the site dir before building."
    )
    p_build.add_argument("--site-dir", type=Path)
    p_build.add_argument("--site-url")
    p_build.add_argument(
        "--repository", help="Read completed installation releases from OWNER/REPO."
    )
    p_build.add_argument("--ref", help="Git ref used for source/edit links.")
    p_build.add_argument("--manifest", type=Path, help="Use a local Schema 8 manifest.")
    p_build.set_defaults(func=build)

    p_rtd = sub.add_parser(
        "rtd", help="Build the language and version selected by Read the Docs."
    )
    p_rtd.set_defaults(func=rtd)

    p_validate = sub.add_parser("validate", help="Strict build across all languages.")
    p_validate.set_defaults(func=validate)

    p_translate = sub.add_parser(
        "translate", help="Prepare, validate, or apply Chinese translations."
    )
    translate_commands = p_translate.add_subparsers(
        dest="translation_operation", required=True
    )

    p_prepare = translate_commands.add_parser(
        "prepare", help="Prepare bounded provider-free Markdown chunks."
    )
    prepare_mode = p_prepare.add_mutually_exclusive_group(required=True)
    prepare_mode.add_argument("--changed", action="store_true")
    prepare_mode.add_argument("--missing", action="store_true")
    p_prepare.add_argument("--base-ref", required=True)
    p_prepare.add_argument("--head-sha", required=True)
    p_prepare.add_argument("--base-repository-id", required=True)
    p_prepare.add_argument("--head-repository-id", required=True)
    p_prepare.add_argument("--pr-number", type=int)
    p_prepare.add_argument(
        "--api-format",
        required=True,
        choices=(
            "openai-chat-completions",
            "openai-responses",
            "anthropic-messages",
            "gemini-generate-content",
        ),
    )
    p_prepare.add_argument("--model", required=True)
    p_prepare.add_argument("--base-url", required=True)
    p_prepare.add_argument(
        "--instructions",
        type=Path,
        default=ROOT / "tools" / "translation" / "instructions.md",
    )
    p_prepare.add_argument("--output-dir", type=Path, required=True)
    p_prepare.add_argument("--max-pages", type=int, choices=range(1, 11), default=10)
    p_prepare.add_argument("--checkout-root", type=Path)
    p_prepare.set_defaults(func=translate)

    p_generate = translate_commands.add_parser(
        "generate", help="Translate prepared chunks through the configured API."
    )
    p_generate.add_argument("--task-dir", type=Path, required=True)
    p_generate.add_argument("--output-dir", type=Path, required=True)
    p_generate.add_argument(
        "--instructions",
        type=Path,
        default=ROOT / "tools" / "translation" / "instructions.md",
    )
    p_generate.add_argument("--checkout-root", type=Path)
    p_generate.set_defaults(func=translate)

    p_finalize = translate_commands.add_parser(
        "finalize", help="Reconstruct and validate exact agent chunk output."
    )
    p_finalize.add_argument("--task-dir", type=Path, required=True)
    p_finalize.add_argument("--agent-output-dir", type=Path, required=True)
    p_finalize.add_argument("--output-dir", type=Path, required=True)
    p_finalize.add_argument(
        "--instructions",
        type=Path,
        default=ROOT / "tools" / "translation" / "instructions.md",
    )
    p_finalize.add_argument("--checkout-root", type=Path)
    p_finalize.set_defaults(func=translate)

    p_check = translate_commands.add_parser(
        "check", help="Check the current PR documentation without model access."
    )
    p_check.add_argument(
        "--base-ref", help="Compare with this base; omit for a full freshness check."
    )
    p_check.add_argument("--checkout-root", type=Path)
    p_check.set_defaults(func=translate)

    p_apply = translate_commands.add_parser(
        "apply", help="CAS-apply a validated translation artifact."
    )
    p_apply.add_argument("--artifact-dir", type=Path, required=True)
    p_apply.add_argument("--checkout-root", type=Path)
    p_apply.set_defaults(func=translate)

    args = parser.parse_args(argv)
    from release_manifest import ManifestError, ReleasePending

    try:
        return args.func(args)
    except ReleasePending as error:
        print(f"[docs] {error}; waiting for release completion", file=sys.stderr)
        return 183 if args.command == "rtd" else 2
    except (ManifestError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    raise SystemExit(main())
