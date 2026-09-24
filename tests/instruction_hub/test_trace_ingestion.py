from __future__ import annotations

import json
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.config import load_hub_config
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, read_yaml_mapping, write_yaml
from promptless_instruction_hub.scan.hub import scan_hub
from tests.config_helpers import enable_trace_ingestion
from tests.managed_bootstrap.helpers import _write_native_hook_asset

from .helpers import FIXTURES, _git, _git_output, _init_action_repo, _run_action, _snapshot_tree


def _set_ingestion(hub: Path, value: JsonValue) -> None:
    config = read_yaml_mapping(hub / "hub.yaml")
    config["trace_ingestion"] = value
    write_yaml(hub / "hub.yaml", config)


def test_init_disables_ingestion_without_overwriting_existing_choice(tmp_path: Path) -> None:
    init_hub(tmp_path, org="Promptless")
    assert read_yaml_mapping(tmp_path / "hub.yaml")["trace_ingestion"] == {"enabled": False}
    enable_trace_ingestion(tmp_path)
    before = (tmp_path / "hub.yaml").read_bytes()
    init_hub(tmp_path, org="Promptless")
    assert (tmp_path / "hub.yaml").read_bytes() == before
    assert load_hub_config(tmp_path).trace_ingestion.enabled


@pytest.mark.parametrize("omit_section", [True, False])
def test_omitted_setting_disables_ingestion(tmp_path: Path, omit_section: bool) -> None:
    init_hub(tmp_path, org="Promptless")
    config = read_yaml_mapping(tmp_path / "hub.yaml")
    if omit_section:
        del config["trace_ingestion"]
    else:
        config["trace_ingestion"] = {}
    write_yaml(tmp_path / "hub.yaml", config)
    assert not load_hub_config(tmp_path).trace_ingestion.enabled
    build_hub(tmp_path)
    assert not (tmp_path / "dist/codex/pig/runtime").exists()
    assert not (tmp_path / "dist/claude/pig/hooks/hooks.json").exists()


@pytest.mark.parametrize(
    "setting", [None, False, "false", {"enabled": "false"}, {"enabled": 0}, {"enabled": None}, {"enable": False}]
)
def test_invalid_ingestion_setting_fails_validation(tmp_path: Path, setting: JsonValue) -> None:
    init_hub(tmp_path, org="Promptless")
    _set_ingestion(tmp_path, setting)
    with pytest.raises(InstructionHubError, match="trace_ingestion"):
        verify_hub(tmp_path)


def test_disabled_build_preserves_instructions_and_mcp_without_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_hub(tmp_path, org="Promptless")
    scan_hub(tmp_path, FIXTURES / "dogfood-source")

    def unexpected_runtime_copy(*args: object, **kwargs: object) -> None:
        pytest.fail("disabled ingestion must not copy a runtime")

    monkeypatch.setattr("promptless_instruction_hub.managed_runtime._copy_runtime_bundle", unexpected_runtime_copy)
    before = _snapshot_tree(tmp_path)
    verified = verify_hub(tmp_path)
    assert _snapshot_tree(tmp_path) == before
    built = build_hub(tmp_path)
    assert built.release_hash == verified.release_hash == build_hub(tmp_path, check=True).release_hash
    release = json.loads((tmp_path / "hub.release.json").read_text())
    assert release["managed_runtimes"] == release["version_basis"]["managed_runtimes"] == []
    for target in ("claude", "codex", "cursor", "gemini"):
        plugin = tmp_path / "dist" / target / "pig"
        assert (plugin / "skills/review-docs/SKILL.md").exists()
        assert not (plugin / "runtime").exists()
        assert not (plugin / "hooks").exists()
        assert not (plugin / "hub.managed-runtimes.json").exists()
        if target in ("claude", "codex"):
            assert (plugin / "skills/update-instruction-hub/SKILL.md").exists()
            mcp = json.loads((plugin / ".mcp.json").read_text())
            assert mcp["mcpServers"]["fixture-docs"]["url"] == "https://example.invalid/mcp"
    manifest = json.loads((tmp_path / "dist/codex/pig/.codex-plugin/plugin.json").read_text())
    assert "hooks" not in manifest
    assert "Hooks" not in manifest["interface"]["capabilities"]
    assert "lifecycle" not in manifest["description"]


@pytest.mark.parametrize("authored_hooks", [False, True])
def test_disabling_ingestion_removes_stale_runtime_and_preserves_authored_hooks(
    tmp_path: Path, authored_hooks: bool
) -> None:
    init_hub(tmp_path, org="Promptless")
    enable_trace_ingestion(tmp_path)
    hooks: dict[str, JsonValue] = {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo authored"}]}]}
    }
    if authored_hooks:
        _write_native_hook_asset(tmp_path, hooks)
    enabled = build_hub(tmp_path)
    assert (tmp_path / "dist/codex/pig/runtime").exists()
    _set_ingestion(tmp_path, {"enabled": False})
    disabled = build_hub(tmp_path)
    assert disabled.release_hash != enabled.release_hash
    assert build_hub(tmp_path, check=True).release_hash == disabled.release_hash
    for target in ("claude", "codex"):
        plugin = tmp_path / "dist" / target / "pig"
        assert not (plugin / "runtime").exists()
        assert not (plugin / "hub.managed-runtimes.json").exists()
        if authored_hooks:
            assert json.loads((plugin / "hooks/hooks.json").read_text()) == hooks
        else:
            assert not (plugin / "hooks/hooks.json").exists()
        assert (plugin / "skills/update-instruction-hub/SKILL.md").exists()


def test_publish_disabled_hub_without_worker_configuration(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish", targets=("claude", "codex", "cursor", "gemini"))
    _set_ingestion(repo, {"enabled": False})
    _git(repo, "add", "hub.yaml")
    _git(repo, "commit", "-m", "Disable ingestion")
    _git(repo, "push", "origin", "main")
    result = _run_action(repo, tmp_path / "output")
    assert result.returncode == 0, result.stdout + result.stderr
    _git(repo, "fetch", "origin", "release/stable")
    release = json.loads(_git_output(repo, "show", "origin/release/stable:hub.release.json"))
    assert release["managed_runtimes"] == []
    published_paths = _git_output(repo, "ls-tree", "-r", "--name-only", "origin/release/stable").splitlines()
    assert not any("/runtime/" in path or path.endswith("hub.managed-runtimes.json") for path in published_paths)
    assert "dist/codex/pig/skills/update-instruction-hub/SKILL.md" in published_paths
