from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

V2_ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def _intent(stage: str, version: str, source_sha: str = SHA) -> dict[str, str]:
    return {"stage": stage, "version": version, "source_sha": source_sha}


def _resign(value: dict[str, object]) -> dict[str, object]:
    unsigned = json.loads(json.dumps(value))
    unsigned.pop("sha256", None)
    unsigned["sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return unsigned


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"PYTHONPATH": str(V2_ROOT)}
    return subprocess.run(
        ["python3", "-m", "ucm_release_v2", *args],
        cwd=V2_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _plan_args(
    *,
    stage: str,
    trigger: str,
    ref: str,
    role: str,
    intent: dict[str, str] | None = None,
) -> list[str]:
    args = [
        "lifecycle",
        "plan",
        "--stage",
        stage,
        "--trigger",
        trigger,
        "--ref",
        ref,
        "--source-sha",
        SHA,
        "--repository-role",
        role,
        "--pr-number",
        "42",
        "--run-number",
        "1",
        "--date",
        "2026-08-12",
        "--config",
        "release.yaml",
    ]
    if intent is not None:
        args.extend(["--intent-json", json.dumps(intent, sort_keys=True)])
    return args


@pytest.mark.parametrize(
    (
        "stage",
        "trigger",
        "ref",
        "role",
        "intent",
        "expected_channel",
        "expected_version",
    ),
    [
        (
            "pr",
            "pull_request",
            "refs/pull/42/head",
            "validation",
            None,
            "pr",
            "0.6.0.dev0+pr.42.g.aaaaaaaaaaaa",
        ),
        (
            "develop",
            "push",
            "refs/heads/develop",
            "validation",
            None,
            "develop",
            "0.6.0.dev1+develop.g.aaaaaaaaaaaa",
        ),
        (
            "nightly",
            "schedule",
            "refs/heads/develop",
            "validation",
            None,
            "nightly",
            "0.6.0.dev20260812+nightly.g.aaaaaaaaaaaa",
        ),
        (
            "draft",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("draft", "0.6.0rc1"),
            "draft",
            "0.6.0rc1.dev0+draft.g.aaaaaaaaaaaa",
        ),
        (
            "rc",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("rc", "0.6.0rc1"),
            "rc",
            "0.6.0rc1",
        ),
        (
            "stable",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("stable", "0.6.0"),
            "stable",
            "0.6.0",
        ),
        (
            "hotfix",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("hotfix", "0.6.1"),
            "hotfix",
            "0.6.1",
        ),
    ],
)
def test_all_stages_plan_a_read_only_versioned_release(
    stage: str,
    trigger: str,
    ref: str,
    role: str,
    intent: dict[str, str] | None,
    expected_channel: str,
    expected_version: str,
) -> None:
    """Catches stage routing that plans the wrong release channel or version."""
    result = _run(
        *_plan_args(stage=stage, trigger=trigger, ref=ref, role=role, intent=intent)
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["stage"] == stage
    assert plan["channel"] == expected_channel
    assert plan["version"] == expected_version
    assert plan["source_sha"] == SHA
    assert plan["repository"] == (
        "ModelEngine-Group/unified-cache-management"
        if role == "production"
        else "SuperMarioYL/unified-cache-management"
    )
    assert plan["operations"] and all(
        item["executed"] is False for item in plan["operations"]
    )


def test_plan_rejects_non_exact_source_sha() -> None:
    """Catches a plan that can drift from an immutable Git object."""
    args = _plan_args(
        stage="develop", trigger="push", ref="refs/heads/develop", role="validation"
    )
    args[args.index(SHA)] = "abc"
    result = _run(*args)

    assert result.returncode != 0
    assert "exactly 40 lowercase hexadecimal" in result.stderr


@pytest.mark.parametrize(
    ("stage", "trigger", "ref", "role"),
    [
        ("pr", "push", "refs/heads/develop", "validation"),
        ("develop", "push", "refs/heads/main", "validation"),
        ("rc", "workflow_dispatch", "refs/heads/main", "validation"),
        ("stable", "workflow_dispatch", "refs/heads/develop", "production"),
    ],
)
def test_plan_rejects_contradictory_trigger_ref_or_repository_role(
    stage: str, trigger: str, ref: str, role: str
) -> None:
    """Catches release plans routed through an unauthorized event, ref, or repository."""
    result = _run(
        *_plan_args(
            stage=stage,
            trigger=trigger,
            ref=ref,
            role=role,
            intent=_intent(stage, "0.6.0rc1") if stage == "rc" else None,
        )
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("stage", "intent"),
    [
        ("draft", None),
        ("rc", _intent("rc", "0.6.0rc1", "b" * 40)),
        ("stable", _intent("stable", "0.6.0rc1")),
        ("hotfix", _intent("stable", "0.6.1")),
    ],
)
def test_protected_stages_require_a_matching_intent(
    stage: str, intent: dict[str, str] | None
) -> None:
    """Catches protected channels accepting missing, stale, or stage-invalid intent."""
    result = _run(
        *_plan_args(
            stage=stage,
            trigger="workflow_dispatch",
            ref="refs/heads/main",
            role="production",
            intent=intent,
        )
    )

    assert result.returncode != 0


def test_canonical_plan_envelope_is_stable_and_self_independent() -> None:
    """Catches nondeterministic JSON or a digest that includes itself."""
    args = _plan_args(
        stage="nightly", trigger="schedule", ref="refs/heads/develop", role="validation"
    )
    first_result = _run(*args)
    second_result = _run(*args)
    assert first_result.returncode == second_result.returncode == 0
    first = json.loads(first_result.stdout)
    second = json.loads(second_result.stdout)
    unsigned = dict(first)
    digest = unsigned.pop("sha256")

    canonical = json.dumps(first, sort_keys=True, separators=(",", ":"))
    assert canonical == json.dumps(second, sort_keys=True, separators=(",", ":"))
    assert (
        digest
        == hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    assert json.loads(canonical)["sha256"] == digest


@pytest.mark.parametrize("source", ["inline", "file"])
def test_protected_intent_rejects_duplicate_json_keys(
    tmp_path: Path, source: str
) -> None:
    """Catches a duplicate intent field silently overriding the reviewed identity."""
    duplicate_intent = (
        '{"stage":"rc","source_sha":"'
        + SHA
        + '","source_sha":"'
        + ("b" * 40)
        + '","version":"0.6.0rc1"}'
    )
    args = _plan_args(
        stage="rc",
        trigger="workflow_dispatch",
        ref="refs/heads/main",
        role="production",
    )
    if source == "inline":
        args.extend(["--intent-json", duplicate_intent])
    else:
        path = tmp_path / "intent.json"
        path.write_text(duplicate_intent, encoding="utf-8")
        args.extend(["--intent", str(path)])

    result = _run(*args)

    assert result.returncode != 0
    assert "duplicate key" in result.stderr


def test_plan_schema_declares_stage_dependent_intent_and_channel_contracts() -> None:
    """Catches a permissive schema that accepts protected/unprotected intent drift."""
    schema = json.loads(
        (V2_ROOT / "schemas" / "lifecycle-plan.schema.json").read_text(encoding="utf-8")
    )
    contracts = schema["allOf"]

    assert any(
        item.get("if", {}).get("properties", {}).get("stage", {}).get("enum")
        == ["pr", "develop", "nightly"]
        and item.get("then", {}).get("not") == {"required": ["release_intent"]}
        for item in contracts
    )
    for stage, version_pattern in (
        ("draft", "rc"),
        ("rc", "rc"),
        ("stable", "[0-9]"),
        ("hotfix", "[0-9]"),
    ):
        matching = [
            item
            for item in contracts
            if item.get("if", {}).get("properties", {}).get("stage", {}).get("const")
            == stage
        ]
        assert matching
        assert any(
            "release_intent" in item.get("then", {}).get("required", [])
            for item in matching
        )
        assert any(
            item.get("then", {}).get("properties", {}).get("channel", {}).get("const")
            == stage
            for item in matching
        )
        assert any(
            version_pattern
            in item.get("then", {})
            .get("properties", {})
            .get("version", {})
            .get("pattern", "")
            for item in matching
        )


def test_plan_schema_binds_retention_to_each_lifecycle_stage() -> None:
    """Catches another allowed retention value being accepted for the wrong stage."""
    schema = json.loads(
        (V2_ROOT / "schemas/lifecycle-plan.schema.json").read_text(encoding="utf-8")
    )
    contracts = schema["allOf"]
    expected = {
        "pr": 7,
        "develop": 14,
        "nightly": 14,
        "draft": 30,
        "rc": None,
        "stable": None,
        "hotfix": None,
    }
    for stage, retention in expected.items():
        matching = [
            item
            for item in contracts
            if item.get("if", {}).get("properties", {}).get("stage", {}).get("const")
            == stage
        ]
        assert len(matching) == 1
        assert matching[0]["then"]["properties"]["retention_days"] == {
            "const": retention
        }


def test_plan_schema_validator_rejects_allowed_retention_from_another_stage(
    tmp_path: Path,
) -> None:
    """Catches stage retention constraints that look present but do not validate documents."""
    validator = shutil.which("jsonschema")
    if validator is None:
        pytest.skip("jsonschema CLI is unavailable")
    schema = V2_ROOT / "schemas/lifecycle-plan.schema.json"
    cases = [
        ("pr", "pull_request", "refs/pull/42/head", "validation", None, 14),
        ("develop", "push", "refs/heads/develop", "validation", None, 7),
        ("nightly", "schedule", "refs/heads/develop", "validation", None, 30),
        (
            "draft",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("draft", "0.6.0rc1"),
            14,
        ),
        (
            "rc",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("rc", "0.6.0rc1"),
            30,
        ),
        (
            "stable",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("stable", "0.6.0"),
            7,
        ),
        (
            "hotfix",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("hotfix", "0.6.1"),
            14,
        ),
    ]
    valid_paths: list[Path] = []
    invalid_paths: list[Path] = []
    for stage, trigger, ref, role, intent, wrong_retention in cases:
        completed = _run(
            *_plan_args(
                stage=stage,
                trigger=trigger,
                ref=ref,
                role=role,
                intent=intent,
            )
        )
        assert completed.returncode == 0, completed.stderr
        document = json.loads(completed.stdout)
        valid_path = tmp_path / f"{stage}-valid.json"
        valid_path.write_text(json.dumps(document), encoding="utf-8")
        valid_paths.append(valid_path)
        document["retention_days"] = wrong_retention
        invalid_path = tmp_path / f"{stage}-wrong-retention.json"
        invalid_path.write_text(json.dumps(document), encoding="utf-8")
        invalid_paths.append(invalid_path)

    valid = subprocess.run(
        [
            validator,
            str(schema),
            *[argument for path in valid_paths for argument in ("-i", str(path))],
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    invalid = subprocess.run(
        [
            validator,
            str(schema),
            *[argument for path in invalid_paths for argument in ("-i", str(path))],
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr
    assert invalid.returncode != 0


def test_plan_schema_draft202012_closes_structural_stage_local_contracts(
    tmp_path: Path,
) -> None:
    """The standalone contract must reject semantic drift, not only extra properties."""
    validator = shutil.which("jsonschema")
    if validator is None:
        pytest.skip("jsonschema CLI is unavailable")
    schema = V2_ROOT / "schemas/lifecycle-plan.schema.json"
    cases = [
        ("pr", "pull_request", "refs/pull/42/head", "validation", None),
        ("develop", "push", "refs/heads/develop", "validation", None),
        ("nightly", "schedule", "refs/heads/develop", "validation", None),
        (
            "draft",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("draft", "0.6.0rc1"),
        ),
        (
            "rc",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("rc", "0.6.0rc1"),
        ),
        (
            "stable",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("stable", "0.6.0"),
        ),
        (
            "hotfix",
            "workflow_dispatch",
            "refs/heads/main",
            "production",
            _intent("hotfix", "0.6.1"),
        ),
    ]
    documents: list[dict[str, object]] = []
    for stage, trigger, ref, role, intent in cases:
        completed = _run(
            *_plan_args(stage=stage, trigger=trigger, ref=ref, role=role, intent=intent)
        )
        assert completed.returncode == 0, completed.stderr
        document = json.loads(completed.stdout)
        path = tmp_path / f"semantic-{stage}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        valid = subprocess.run(
            [validator, str(schema), "-i", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert valid.returncode == 0, valid.stderr
        documents.append(document)

    base = documents[2]
    mutations: list[dict[str, object]] = []
    for change in (
        lambda item: item.__setitem__("gates", []),
        lambda item: item.pop("products"),
        lambda item: item["operations"][0].__setitem__("action", "delete-release"),
        lambda item: item.__setitem__("trigger", "workflow_dispatch"),
        lambda item: item.__setitem__("ref", "refs/heads/main"),
        lambda item: item.__setitem__("repository_role", "production"),
        lambda item: item["products"].append(item["products"][0]),
        lambda item: item["products"][0].__setitem__("kind", "wheel"),
        lambda item: item["products"][0].__setitem__("name", "wrong-name"),
        lambda item: item["products"][0].__setitem__("coordinate", "not-a-coordinate"),
        lambda item: item.__setitem__("stage", "stable"),
    ):
        mutated = json.loads(json.dumps(base))
        change(mutated)
        mutations.append(mutated)

    for index, mutation in enumerate(mutations):
        path = tmp_path / f"semantic-invalid-{index}.json"
        path.write_text(json.dumps(mutation), encoding="utf-8")
        invalid = subprocess.run(
            [validator, str(schema), "-i", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert invalid.returncode != 0, f"mutation {index} unexpectedly validated"


def test_plan_schema_declares_runtime_cross_field_validation_boundary() -> None:
    """Consumers must not mistake structural JSON Schema checks for semantic equality."""
    schema = json.loads(
        (V2_ROOT / "schemas/lifecycle-plan.schema.json").read_text(encoding="utf-8")
    )
    assert schema.get("x-ucm-validation") == {
        "cross_field_invariants": [
            "canonical-self-digest",
            "configured-repository-and-product-closure",
            "source-sha-version-binding",
            "release-intent-source-and-version-binding",
        ],
        "required_command": "python3 -m ucm_release_v2 lifecycle validate",
        "schema_scope": "strict-structural-and-stage-local",
    }


def test_nightly_validator_rejects_resigned_impossible_calendar_date(
    tmp_path: Path,
) -> None:
    """Catches a re-signed 20260230 Nightly version passing syntax-only validation."""
    generated = _run(
        *_plan_args(
            stage="nightly",
            trigger="schedule",
            ref="refs/heads/develop",
            role="validation",
        )
    )
    assert generated.returncode == 0, generated.stderr
    plan = json.loads(generated.stdout)
    old_version = plan["version"]
    new_version = str(old_version).replace("20260812", "20260230")
    plan["version"] = new_version
    for product in plan["products"]:
        product["coordinate"] = product["coordinate"].replace(old_version, new_version)
    path = tmp_path / "impossible-nightly.json"
    path.write_text(json.dumps(_resign(plan)), encoding="utf-8")

    result = _run(
        "lifecycle", "validate", "--plan", str(path), "--config", "release.yaml"
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "calendar date" in result.stderr
    assert "Traceback" not in result.stderr


def test_runtime_validator_rejects_resigned_schema_valid_cross_field_drift(
    tmp_path: Path,
) -> None:
    """Format-valid cross-field drift must fail the mandatory semantic validator."""
    validator = shutil.which("jsonschema")
    if validator is None:
        pytest.skip("jsonschema CLI is unavailable")
    schema = V2_ROOT / "schemas/lifecycle-plan.schema.json"

    def plan(stage: str) -> dict[str, object]:
        intent = None
        if stage == "rc":
            intent = _intent("rc", "0.6.0rc1")
        completed = _run(
            *_plan_args(
                stage=stage,
                trigger={
                    "pr": "pull_request",
                    "nightly": "schedule",
                    "rc": "workflow_dispatch",
                }[stage],
                ref={
                    "pr": "refs/pull/42/head",
                    "nightly": "refs/heads/develop",
                    "rc": "refs/heads/main",
                }[stage],
                role="production" if stage == "rc" else "validation",
                intent=intent,
            )
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    pr = plan("pr")
    nightly = plan("nightly")
    rc = plan("rc")
    mutations: list[tuple[str, dict[str, object]]] = []

    different_product_version = json.loads(json.dumps(pr))
    different_product_version["products"][0][
        "coordinate"
    ] = "unified-cache-pd@0.6.0.dev0+pr.43.g.aaaaaaaaaaaa"
    mutations.append(("product-version", _resign(different_product_version)))

    different_owner = json.loads(json.dumps(nightly))
    image = next(
        item for item in different_owner["products"] if item["kind"] == "image"
    )
    image["coordinate"] = image["coordinate"].replace(
        "ghcr.io/SuperMarioYL/", "ghcr.io/attacker/"
    )
    mutations.append(("image-owner", _resign(different_owner)))

    source_suffix = json.loads(json.dumps(nightly))
    source_suffix["source_sha"] = "b" * 40
    mutations.append(("source-version", _resign(source_suffix)))

    intent_source = json.loads(json.dumps(rc))
    intent_source["release_intent"]["source_sha"] = "b" * 40
    mutations.append(("intent-source", _resign(intent_source)))

    intent_version = json.loads(json.dumps(rc))
    intent_version["release_intent"]["version"] = "0.6.0rc2"
    mutations.append(("intent-version", _resign(intent_version)))

    for label, mutation in mutations:
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps(mutation), encoding="utf-8")
        structural = subprocess.run(
            [validator, str(schema), "-i", str(path)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert structural.returncode == 0, structural.stderr

        semantic = _run(
            "lifecycle",
            "validate",
            "--plan",
            str(path),
            "--config",
            "release.yaml",
        )
        assert semantic.returncode == 2
        assert semantic.stdout == ""
        assert "Traceback" not in semantic.stderr
