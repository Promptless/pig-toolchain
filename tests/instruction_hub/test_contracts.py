from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub

from .external_helpers import external_definition, write_external
from .helpers import (
    FIXTURES,
    SCHEMAS,
    WORKFLOWS,
)


@pytest.mark.parametrize("workflow_name", ["pr-check.yml", "publish.yml"])
def test_reusable_workflows_run_caller_pinned_toolchain_ref(workflow_name: str) -> None:
    workflow_text = (WORKFLOWS / workflow_name).read_text()

    assert "Promptless/pig-toolchain@v0" not in workflow_text
    assert f"EXPECTED_WORKFLOW_PATH: .github/workflows/{workflow_name}" in workflow_text
    assert "JOB_WORKFLOW_REF: ${{ job.workflow_ref }}" in workflow_text
    assert "repository: ${{ steps.toolchain-ref.outputs.repository }}" in workflow_text
    assert "ref: ${{ steps.toolchain-ref.outputs.ref }}" in workflow_text
    assert "path: .promptless-pig-toolchain" in workflow_text
    assert "uses: ./.promptless-pig-toolchain" in workflow_text


def test_cli_init_scan_verify_build_validate_and_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hub_root = tmp_path / "hub"

    assert main(["init", "--hub", str(hub_root), "--org", "Acme"]) == 0
    assert main(["scan", "--hub", str(hub_root), "--source", str(FIXTURES / "dogfood-source")]) == 0
    assert main(["validate", "--hub", str(hub_root)]) == 0
    assert main(["verify", "--hub", str(hub_root)]) == 0
    assert main(["build", "--hub", str(hub_root)]) == 0
    assert main(["build", "--hub", str(hub_root), "--check"]) == 0
    assert main(["status", "--manifest", str(hub_root / "hub.release.json")]) == 0

    output = capsys.readouterr().out
    assert "valid Instruction Hub" in output
    assert "verified release" in output
    assert "release_hash" in output


def test_empty_hub_fixture_bootstraps(tmp_path: Path) -> None:
    hub_root = tmp_path / "empty-hub"
    shutil.copytree(FIXTURES / "empty-hub", hub_root)

    init_hub(hub_root)
    result = build_hub(hub_root)

    assert result.asset_count == 0
    assert (hub_root / "hub.release.json").exists()


def test_release_manifest_schema_matches_generated_contract() -> None:
    schema = json.loads((SCHEMAS / "release-manifest.schema.json").read_text())

    assert schema["additionalProperties"] is False
    assert "target_hashes" in schema["required"]
    assert "version_basis" in schema["required"]
    assert "managed_runtimes" in schema["required"]
    assert "git_commit" not in schema["properties"]
    assert schema["properties"]["stable_plugins"]["minItems"] == 1
    assert schema["properties"]["targets"]["minItems"] == 1
    assert schema["properties"]["target_hashes"]["minProperties"] == 1
    assert "default" not in schema["properties"]["managed_runtimes"]
    version_basis_schema = schema["properties"]["version_basis"]
    assert version_basis_schema["required"] == [
        "org",
        "version",
        "marketplace",
        "stable_plugins",
        "targets",
        "plugins",
        "target_hashes",
        "managed_runtimes",
    ]
    assert version_basis_schema["properties"]["stable_plugins"]["minItems"] == 1
    assert version_basis_schema["properties"]["targets"]["minItems"] == 1
    assert version_basis_schema["properties"]["plugins"]["minItems"] == 1
    assert version_basis_schema["properties"]["target_hashes"] == {"$ref": "#/properties/target_hashes"}
    assert version_basis_schema["properties"]["managed_runtimes"] == {"$ref": "#/properties/managed_runtimes"}
    managed_runtime_schema = schema["properties"]["managed_runtimes"]["items"]
    assert managed_runtime_schema["required"] == [
        "id",
        "channel",
        "executable",
        "hook",
        "package_id",
        "path",
        "plugin_id",
        "plugin_name",
        "plugin_version",
        "sha256",
        "status",
        "target",
        "toolchain_version",
        "version",
    ]
    assert managed_runtime_schema["properties"]["id"] == {"const": "host-runtime"}
    assert managed_runtime_schema["properties"]["status"] == {"const": "included"}
    assert managed_runtime_schema["properties"]["target"] == {"enum": ["claude", "codex"]}
    assert managed_runtime_schema["properties"]["plugin_name"]["maxLength"] == 200
    assert "oneOf" not in managed_runtime_schema
    asset_schema = schema["properties"]["assets"]["items"]
    assert asset_schema["required"] == ["ref", "id", "type", "title", "source_path", "content_hash", "support"]
    assert "pattern" in schema["properties"]["version"]
    target_support_schema = schema["$defs"]["target_support"]
    assert "source" not in target_support_schema["properties"]
    assert target_support_schema["properties"]["reason"] == {"type": "string", "minLength": 1}
    assert target_support_schema["allOf"][0]["then"]["required"] == ["reason"]


def test_instruction_hub_schema_requires_non_empty_lists() -> None:
    schema = json.loads((SCHEMAS / "instruction-hub.schema.json").read_text())

    assert schema["properties"]["stable_plugins"]["minItems"] == 1
    assert schema["properties"]["stable_plugins"]["contains"] == {"const": "pig"}
    assert schema["properties"]["stable_plugins"]["default"] == ["pig"]
    assert schema["properties"]["targets"]["minItems"] == 1


def test_optional_hub_tests_keep_command_and_platform_in_caller_configuration() -> None:
    import os
    import subprocess
    import yaml

    workflow = yaml.safe_load((WORKFLOWS / "pr-check.yml").read_text())
    job = workflow["jobs"]["hub-tests"]
    assert job["if"] == "${{ inputs.test-command != '' }}"
    assert job["runs-on"] == "${{ inputs.test-runs-on }}"
    assert job["steps"][0]["with"]["persist-credentials"] is False
    assert job["steps"][1]["with"]["python-version"] == "${{ inputs.test-python-version }}"
    step = job["steps"][2]
    assert step["working-directory"] == "${{ inputs.hub-root }}"
    assert step["env"]["HUB_TEST_COMMAND"] == "${{ inputs.test-command }}"
    # Run the actual workflow shell step; a failing hub suite must fail the CI job.
    result = subprocess.run(["bash", "-c", step["run"]], env={**os.environ, "HUB_TEST_COMMAND": "exit 23"})
    assert result.returncode == 23


@pytest.mark.parametrize("plugin_path", [None, ".", "plugins/doc-detective", "plugins/Doc Detective"])
def test_generated_release_validates_against_shipped_schema(tmp_path: Path, plugin_path: str | None) -> None:
    init_hub(tmp_path)
    if plugin_path is not None:
        write_external(tmp_path, external_definition(path=plugin_path))
    build_hub(tmp_path)
    schema = json.loads((SCHEMAS / "release-manifest.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    release = json.loads((tmp_path / "hub.release.json").read_text())
    Draft202012Validator(schema).validate(release)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../plugin",
        "/plugin",
        "x/../plugin",
        "x//plugin",
        "./plugin",
        "C:/plugin",
        "x\\y",
        "plugins/review\tdocs",
        "plugins/review\ndocs",
        "plugins/review\x00docs",
        "plugins/review\u00a0docs",
        "plugins/review docs\n",
    ],
)
def test_external_target_schema_rejects_unsafe_paths(path: str) -> None:
    schema = json.loads((SCHEMAS / "release-manifest.schema.json").read_text())
    assert not Draft202012Validator(schema["$defs"]["external_target"]).is_valid({"path": path})
