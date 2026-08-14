from __future__ import annotations

import hashlib
from pathlib import Path

from ucm_release_production.common import sha256_envelope
from ucm_release_production.config import load_config
from ucm_release_production.workflow_data import candidate_outputs, release_request

from conftest import PRODUCTION_ROOT


def _candidate(root: Path) -> dict[str, object]:
    wheels = []
    for profile, distribution, platform in (
        ("cuda130", "uc-manager-cuda", "manylinux_2_28"),
        ("cann900-a2", "uc-manager-cann-a2", "linux"),
        ("cann900-a3", "uc-manager-cann-a3", "linux"),
    ):
        for arch, wheel_arch in (("amd64", "x86_64"), ("arm64", "aarch64")):
            spec = f"{profile}-{arch}"
            name = f"{distribution.replace('-', '_')}-0.6.0rc1-cp312-cp312-{platform}_{wheel_arch}.whl"
            path = root / "wheels" / spec / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(spec.encode())
            wheels.append(
                {
                    "spec_id": spec,
                    "distribution": distribution,
                    "path": f"wheels/{spec}/{name}",
                    "file_sha256": "sha256:"
                    + hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    chart = root / "chart" / "unified-cache-pd-0.6.0-rc.1.tgz"
    chart.parent.mkdir(parents=True)
    chart.write_bytes(b"chart")
    source = {"control_default_branch": "develop"}
    run = {"source_date_epoch": 1786608000}
    return sha256_envelope(
        {
            "kind": "ucm-production-candidate-envelope",
            "schema_version": 1,
            "repository": "OctoCat/unified-cache-management",
            "repository_id": 42,
            "stage": "rc",
            "tag_name": "v0.6.0rc1",
            "source_sha": "1" * 40,
            "intent": {"version": "0.6.0"},
            "source_identity": source,
            "run": run,
            "wheels": wheels,
            "chart": {
                "path": "chart/unified-cache-pd-0.6.0-rc.1.tgz",
                "file_sha256": "sha256:"
                + hashlib.sha256(chart.read_bytes()).hexdigest(),
            },
        }
    )


def test_workflow_candidate_outputs_are_closed_scalars(tmp_path: Path) -> None:
    value = _candidate(tmp_path / "candidate")

    outputs = candidate_outputs(value)

    assert set(outputs) == {
        "stage",
        "candidate_sha256",
        "source_identity_b64",
        "source_date_epoch",
    }
    assert outputs["stage"] == "rc"


def test_release_request_materializes_only_seven_delivery_assets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate"
    candidate = _candidate(root)
    environment = sha256_envelope(
        {
            "kind": "ucm-production-environment-evidence",
            "schema_version": 1,
            "status": "waived-for-preview",
            "source_sha": "1" * 40,
            "environment": "release-production",
            "deployment_id": 99,
            "approval_actor": "OctoCat",
        }
    )

    result = release_request(
        candidate,
        root,
        environment,
        tmp_path / "channels",
        tmp_path / "assets",
        tmp_path / "release-request.json",
    )

    asset_names = [Path(path).name for path in result["assets"]]
    assert len(asset_names) == 7
    assert asset_names == [
        "uc_manager_cuda-0.6.0rc1-cp312-cp312-manylinux_2_28_x86_64.whl",
        "uc_manager_cuda-0.6.0rc1-cp312-cp312-manylinux_2_28_aarch64.whl",
        "uc_manager_cann_a2-0.6.0rc1-cp312-cp312-linux_x86_64.whl",
        "uc_manager_cann_a2-0.6.0rc1-cp312-cp312-linux_aarch64.whl",
        "uc_manager_cann_a3-0.6.0rc1-cp312-cp312-linux_x86_64.whl",
        "uc_manager_cann_a3-0.6.0rc1-cp312-cp312-linux_aarch64.whl",
        "unified-cache-pd-0.6.0-rc.1.tgz",
    ]
    assert sorted(path.name for path in (tmp_path / "assets").iterdir()) == sorted(
        asset_names
    )
    assert (
        load_config(PRODUCTION_ROOT / "production-release.json")["base_version"]
        == "0.6.0"
    )
