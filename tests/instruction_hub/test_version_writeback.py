from pathlib import Path

import pytest
from pydantic import ValidationError

from promptless_instruction_hub.compiler import init_hub
from promptless_instruction_hub.config import load_hub_config, write_hub_version
from promptless_instruction_hub.errors import InstructionHubError


@pytest.mark.parametrize("old_scalar", ["0.1.0", '"0.1.0"', "'0.1.0'"])
def test_writeback_preserves_other_source_and_comments(tmp_path: Path, old_scalar: str) -> None:
    init_hub(tmp_path, org="Promptless")
    config_path = tmp_path / "hub.yaml"
    source = "# Hub configuration\n" + config_path.read_text().replace(
        "version: 0.1.0", f"version: {old_scalar} # released"
    )
    config_path.write_text(source)

    write_hub_version(tmp_path, "0.4.0")

    assert config_path.read_text() == source.replace(f"version: {old_scalar}", "version: 0.4.0")
    assert load_hub_config(tmp_path).version == "0.4.0"
    write_hub_version(tmp_path, "0.4.0")
    assert config_path.read_text() == source.replace(f"version: {old_scalar}", "version: 0.4.0")


def test_writeback_rejects_invalid_version_without_changing_source(tmp_path: Path) -> None:
    init_hub(tmp_path, org="Promptless")
    before = (tmp_path / "hub.yaml").read_bytes()
    with pytest.raises(ValidationError, match="version must be SemVer"):
        write_hub_version(tmp_path, "invalid")
    assert (tmp_path / "hub.yaml").read_bytes() == before


@pytest.mark.parametrize("style", ["|-", ">-"])
def test_writeback_preserves_block_scalar_and_following_key(tmp_path: Path, style: str) -> None:
    init_hub(tmp_path, org="Promptless")
    config_path = tmp_path / "hub.yaml"
    source = config_path.read_text().replace("version: 0.1.0\n", f"version: {style} # released\n  0.1.0\n")
    config_path.write_text(source)

    write_hub_version(tmp_path, "0.4.0")

    assert config_path.read_text() == source.replace("  0.1.0\n", "  0.4.0\n")
    assert load_hub_config(tmp_path).version == "0.4.0"


def test_config_rejects_old_version_key(tmp_path: Path) -> None:
    init_hub(tmp_path, org="Promptless")
    config_path = tmp_path / "hub.yaml"
    config_path.write_text(config_path.read_text().replace("version:", "plugin_version:"))
    with pytest.raises(InstructionHubError, match="plugin_version"):
        load_hub_config(tmp_path)


def test_writeback_rejects_shared_yaml_alias_without_changing_source(tmp_path: Path) -> None:
    init_hub(tmp_path, org="Promptless")
    config_path = tmp_path / "hub.yaml"
    source = (
        config_path.read_text()
        .replace("org: Promptless", "org: &release 0.1.0")
        .replace("version: 0.1.0", "version: *release")
    )
    config_path.write_text(source)
    with pytest.raises(ValueError, match="independent YAML scalar"):
        write_hub_version(tmp_path, "0.4.0")
    assert config_path.read_text() == source
