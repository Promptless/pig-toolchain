from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.config_helpers import enable_trace_ingestion

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub, validate_hub
from promptless_instruction_hub.config import load_hub_config
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import write_yaml
from promptless_instruction_hub.release.versions import resolve_publish_version

from .helpers import FIXTURES


def test_cli_init_uses_explicit_marketplace_identity(tmp_path: Path) -> None:
    assert (
        main(
            [
                "init",
                "--hub",
                str(tmp_path),
                "--org",
                "Acme, Inc.",
                "--marketplace-id",
                "acme-tools-marketplace",
                "--marketplace-name",
                "Acme Tools",
            ]
        )
        == 0
    )

    config = load_hub_config(tmp_path)
    assert config.marketplace.id == "acme-tools-marketplace"
    assert config.marketplace.name == "Acme Tools"
    assert config.stable_plugins == ["pig"]
    assert (tmp_path / "plugins/pig.yaml").exists()
    assert not (tmp_path / "packages").exists()


def test_default_marketplace_identity_slugifies_org(tmp_path: Path) -> None:
    init_hub(tmp_path, org="Acme, Inc.")
    config = load_hub_config(tmp_path)
    assert config.marketplace.id == "acme-inc-instruction-hub"
    assert config.marketplace.name == "Acme, Inc. Instruction Hub"


@pytest.mark.parametrize("marketplace_id", ["acme-tools", "acme-tools-marketplace"])
def test_literal_ids_are_consistent_across_targets(tmp_path: Path, marketplace_id: str) -> None:
    init_hub(tmp_path, org="Acme", marketplace_id=marketplace_id, marketplace_name="Acme Tools")
    enable_trace_ingestion(tmp_path)
    config = load_hub_config(tmp_path)
    write_yaml(tmp_path / "hub.yaml", {**config.model_dump(), "stable_plugins": ["pig", "dev"]})
    write_yaml(tmp_path / "plugins/dev.yaml", {"id": "dev", "name": "Developer Tools", "includes": []})
    build_hub(tmp_path)

    for target, marketplace_path in {
        "claude": ".claude-plugin/marketplace.json",
        "codex": ".agents/plugins/marketplace.json",
        "cursor": ".cursor-plugin/marketplace.json",
    }.items():
        marketplace = json.loads((tmp_path / marketplace_path).read_text())
        assert marketplace["name"] == marketplace_id
        assert [plugin["name"] for plugin in marketplace["plugins"]] == ["pig", "dev"]
        if target == "codex":
            assert marketplace["interface"]["displayName"] == "Acme Tools"

    for target, manifest_path in {
        "claude": ".claude-plugin/plugin.json",
        "codex": ".codex-plugin/plugin.json",
        "cursor": ".cursor-plugin/plugin.json",
        "gemini": "gemini-extension.json",
    }.items():
        manifest = json.loads((tmp_path / "dist" / target / "dev" / manifest_path).read_text())
        assert manifest["name"] == "dev"
        if target == "codex":
            assert manifest["interface"]["displayName"] == "Developer Tools"
        elif target in {"claude", "cursor"}:
            assert manifest["displayName"] == "Developer Tools"

    for target in ("claude", "codex"):
        plugin_root = tmp_path / "dist" / target / "pig"
        update_skill = (plugin_root / "skills/update-instruction-hub/SKILL.md").read_text()
        assert f"generated marketplace name `{marketplace_id}`" in update_skill
        runtimes = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
        assert {runtime["plugin_id"] for runtime in runtimes["managed_runtimes"]} == {"pig"}


@pytest.mark.parametrize("marketplace", [{"id": "Bad ID", "name": "Tools"}, {"id": "acme", "name": ""}])
def test_invalid_marketplace_identity_is_rejected(tmp_path: Path, marketplace: dict[str, str]) -> None:
    init_hub(tmp_path)
    config = load_hub_config(tmp_path)
    write_yaml(tmp_path / "hub.yaml", {**config.model_dump(), "marketplace": marketplace})
    with pytest.raises(InstructionHubError, match="marketplace"):
        validate_hub(tmp_path)


@pytest.mark.parametrize("command", ["init", "validate", "verify", "build", "scan"])
def test_legacy_config_requires_migration_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    config_path = tmp_path / "hub.yaml"
    original = "org: Acme\nplugin_id: acme-instruction-hub\nplugin_name: Acme\nversion: 0.1.0\n"
    config_path.write_text(original)
    assert main([command, "--hub", str(tmp_path)]) == 1
    assert "legacy hub configuration" in capsys.readouterr().err
    assert config_path.read_text() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == ["hub.yaml"]


def test_legacy_directory_is_not_silently_ignored(tmp_path: Path) -> None:
    init_hub(tmp_path)
    (tmp_path / "plugins").rename(tmp_path / "packages")
    with pytest.raises(InstructionHubError, match="legacy plugin directory"):
        validate_hub(tmp_path)


@pytest.mark.parametrize(
    "extra_files",
    [
        {"packages/app/package.json": '{"name":"app"}\n', "packages/app/index.js": "export default 1;\n"},
        {"packages/workspace.yaml": "packages:\n  - app\n"},
        {"packages/unrelated.yaml": "[invalid: yaml"},
        {"packages/unrelated.yaml": "- a\n- b\n"},
        {"packages/app/metadata.yaml": "id: app\nname: Application\n"},
        {},
    ],
    ids=["monorepo", "workspace-yaml", "malformed-yaml", "yaml-list", "nested-metadata", "empty-directory"],
)
def test_customer_packages_directory_survives_init_and_verify(tmp_path: Path, extra_files: dict[str, str]) -> None:
    (tmp_path / "packages").mkdir()
    for name, content in extra_files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    assert main(["init", "--hub", str(tmp_path), "--org", "Acme"]) == 0
    assert main(["verify", "--hub", str(tmp_path)]) == 0

    for name, content in extra_files.items():
        assert (tmp_path / name).read_text() == content


@pytest.mark.parametrize("command", ["init", "validate", "verify", "build", "scan"])
@pytest.mark.parametrize("definition", ["id: old\nname: Old\n", "id: old\nname: Old\nincludes: []\n"])
def test_legacy_package_definitions_rejected_before_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, definition: str
) -> None:
    init_hub(tmp_path, org="Acme")
    (tmp_path / "packages").mkdir()
    legacy = tmp_path / "packages/old.yaml"
    legacy.write_text(definition)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    assert main([command, "--hub", str(tmp_path)]) == 1

    error = capsys.readouterr().err
    assert "legacy plugin directory" in error
    assert str(legacy) in error
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_duplicate_plugin_ids_are_rejected(tmp_path: Path) -> None:
    init_hub(tmp_path)
    (tmp_path / "plugins/duplicate.yaml").write_text("id: pig\nname: Another PIG\n")
    with pytest.raises(InstructionHubError, match="duplicate plugin id: pig"):
        validate_hub(tmp_path)


def test_publish_rejects_version_one_release(tmp_path: Path) -> None:
    init_hub(tmp_path, org="Acme", marketplace_id="acme-instruction-hub-marketplace")
    config = load_hub_config(tmp_path)
    write_yaml(tmp_path / "hub.yaml", {**config.model_dump(), "targets": ["cursor"]})
    with pytest.raises(ValueError, match="hub.release.json: version is missing"):
        resolve_publish_version(
            tmp_path,
            previous_release_root=FIXTURES / "legacy-release",
        )
