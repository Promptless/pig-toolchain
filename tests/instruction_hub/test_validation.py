from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub, validate_hub
from promptless_instruction_hub.errors import InstructionHubError


def test_validate_rejects_empty_stable_plugins(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "marketplace:",
                "  id: acme-instruction-hub",
                "  name: Acme Instruction Hub",
                "version: 0.1.0",
                "stable_plugins: []",
                "",
            ]
        )
    )

    with pytest.raises(InstructionHubError, match="stable_plugins"):
        validate_hub(hub_root)


def test_validate_requires_pig_stable_package(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "plugins/customer.yaml").write_text("id: customer\nname: Customer\nincludes: []\n")
    (hub_root / "hub.yaml").write_text((hub_root / "hub.yaml").read_text().replace("- pig\n", "- customer\n"))

    with pytest.raises(InstructionHubError, match="required 'pig' plugin"):
        validate_hub(hub_root)


def test_validate_rejects_empty_package_name(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "plugins/pig.yaml").write_text("id: pig\nname: ''\nincludes: []\n")

    with pytest.raises(InstructionHubError, match="name"):
        validate_hub(hub_root)


def test_validate_rejects_unknown_package_refs(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes:\n  - skill:missing\n")

    with pytest.raises(InstructionHubError, match="unknown asset refs"):
        validate_hub(hub_root)


@pytest.mark.parametrize("skill_id", ["update-instruction-hub", "add-external-plugin"])
def test_validate_rejects_authored_managed_skill_in_pig(tmp_path: Path, skill_id: str) -> None:
    hub_root = tmp_path / "hub"
    skill_root = hub_root / "assets/skills" / skill_id
    init_hub(hub_root)
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Customer updater\n")
    (hub_root / "plugins/pig.yaml").write_text(f"id: pig\nname: PIG\nincludes:\n  - skill:{skill_id}\n")

    with pytest.raises(InstructionHubError, match=f"reserved managed asset 'skill:{skill_id}'"):
        validate_hub(hub_root)


def test_validate_merges_sparse_target_support_with_defaults(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes:\n  - rule:partial\n")
    (hub_root / "assets/rules/partial.md").write_text("# Partial\n")
    (hub_root / "assets/rules/partial.asset.yaml").write_text(
        "\n".join(
            [
                "title: Partial",
                "support:",
                "  codex:",
                "    mode: projected",
                "",
            ]
        )
    )

    validation = validate_hub(hub_root)

    asset = validation.assets["rule:partial"]
    assert asset.metadata.support["codex"].mode == "projected"
    assert asset.metadata.support["cursor"].mode == "unsupported"


def test_validate_rejects_unsafe_asset_ids(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    skill_root = hub_root / "assets/skills/bad"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Bad\n")
    (skill_root / "asset.yaml").write_text("id: ../bad\ntype: skill\n")

    with pytest.raises(ValueError, match="asset id"):
        validate_hub(hub_root)


@pytest.mark.parametrize(
    "config_text",
    [
        "org: ''\nmarketplace:\n  id: acme-instruction-hub\n  name: Acme Instruction Hub\nversion: 0.1.0\n",
        "org: Acme\nmarketplace:\n  id: acme-instruction-hub\n  name: ''\nversion: 0.1.0\n",
    ],
)
def test_validate_rejects_empty_required_config_strings(tmp_path: Path, config_text: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "hub.yaml").write_text(config_text)

    with pytest.raises(InstructionHubError, match="String should have at least 1 character"):
        validate_hub(hub_root)


def test_validate_rejects_empty_target_list(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "hub.yaml").write_text(
        "\n".join(
            [
                "org: Acme",
                "marketplace:",
                "  id: acme-instruction-hub",
                "  name: Acme Instruction Hub",
                "version: 0.1.0",
                "targets: []",
                "",
            ]
        )
    )

    with pytest.raises(InstructionHubError, match="at least 1 item"):
        validate_hub(hub_root)


def test_validate_rejects_metadata_type_mismatch(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/rules/team-style.md").write_text("# Team Style\n")
    (hub_root / "assets/rules/team-style.asset.yaml").write_text("id: team-style\ntype: skill\n")

    with pytest.raises(InstructionHubError, match="declares type"):
        validate_hub(hub_root)


def test_validate_rejects_malformed_asset_candidates(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/skills/broken").mkdir(parents=True)

    with pytest.raises(InstructionHubError, match="must contain SKILL.md"):
        validate_hub(hub_root)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable on this platform")
def test_validate_rejects_symlinked_skill_files(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    secret_path = tmp_path / "outside-secret.md"
    skill_root = hub_root / "assets/skills/leak"
    init_hub(hub_root)
    skill_root.mkdir(parents=True)
    secret_path.write_text("# Leaked\n\nexternal content\n")
    os.symlink(secret_path, skill_root / "SKILL.md")

    with pytest.raises(InstructionHubError, match="symlink"):
        validate_hub(hub_root)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable on this platform")
def test_validate_rejects_symlinked_mcp_files(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    outside_asset = tmp_path / "outside.json"
    init_hub(hub_root)
    outside_asset.write_text("{}\n")
    os.symlink(outside_asset, hub_root / "assets/mcps/leak.json")

    with pytest.raises(InstructionHubError, match="symlink"):
        validate_hub(hub_root)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable on this platform")
def test_validate_rejects_symlinked_assets_root(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    outside_assets = tmp_path / "outside-assets"
    init_hub(hub_root)
    shutil.rmtree(hub_root / "assets")
    outside_assets.mkdir()
    os.symlink(outside_assets, hub_root / "assets")

    with pytest.raises(InstructionHubError, match="assets.*symlink"):
        validate_hub(hub_root)


def test_validate_allows_json_array_files_inside_skill_assets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    skill_root = hub_root / "assets/skills/json-fixture"
    init_hub(hub_root)
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# JSON Fixture\n")
    (skill_root / "examples").mkdir()
    (skill_root / "examples/data.json").write_text(json.dumps([{"name": "safe fixture"}]))

    validate_hub(hub_root)


def test_validate_rejects_literal_mcp_secrets(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    mcp_path = hub_root / "assets/mcps/bad.yaml"
    mcp_path.write_text("api_token: sk-live-secret\n")

    with pytest.raises(InstructionHubError, match="literal secret"):
        validate_hub(hub_root)


def test_validate_rejects_literal_mcp_authorization_headers(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bad": {
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer literal-secret"},
                    }
                }
            }
        )
    )

    with pytest.raises(InstructionHubError, match="literal secret"):
        validate_hub(hub_root)


def test_validate_rejects_literal_secret_mcp_arg_values(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bad": {
                        "command": "bad",
                        "args": ["--token", "literal-secret"],
                    }
                }
            }
        )
    )

    with pytest.raises(InstructionHubError, match=r"args\.1"):
        validate_hub(hub_root)


def test_validate_rejects_literal_secret_mcp_inline_arg_values(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bad": {
                        "command": "bad",
                        "args": ["--api-key=literal-secret"],
                    }
                }
            }
        )
    )

    with pytest.raises(InstructionHubError, match=r"args\.0"):
        validate_hub(hub_root)


def test_validate_accepts_env_placeholder_mcp_arg_values(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/good.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "good": {
                        "command": "good",
                        "args": ["--token", "${MCP_TOKEN}"],
                    }
                }
            }
        )
    )

    validate_hub(hub_root)


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization"])
@pytest.mark.parametrize(
    "value",
    ["${MCP_TOKEN}", "Bearer ${MCP_TOKEN}", "basic ${MCP_TOKEN}", "Bearer ${env:MCP_TOKEN}", "${env:api-key}"],
)
def test_validate_accepts_mcp_authorization_env_references(tmp_path: Path, header: str, value: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    (hub_root / "assets/mcps/good.json").write_text(
        json.dumps({"mcpServers": {"docs": {"url": "https://example.invalid/mcp", "headers": {header: value}}}})
    )

    validate_hub(hub_root)


@pytest.mark.parametrize(
    "server_name", ["tokenizer", "secretary", "token-service", "api_keys", "private_keys", "apikeys"]
)
def test_validate_does_not_treat_mcp_server_names_as_credentials(tmp_path: Path, server_name: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    (hub_root / "assets/mcps/good.json").write_text(
        json.dumps({"mcpServers": {server_name: {"command": "npx", "args": ["-y", "tokenizer-mcp"]}}})
    )

    validate_hub(hub_root)


@pytest.mark.parametrize(
    "credential",
    [
        "${}",
        "${TOKEN:-literal-secret}",
        "${TOKEN}literal-secret",
        "${A}${B}",
        "${TOKEN}\n",
        "env:TOKEN literal-secret",
        "Bearer literal-secret",
        "Bearer ${TOKEN}literal-secret",
        "Bearer ${TOKEN:-literal-secret}",
    ],
)
def test_validate_rejects_literal_or_malformed_authorization_values(tmp_path: Path, credential: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    (hub_root / "assets/mcps/bad.json").write_text(
        json.dumps(
            {"mcpServers": {"docs": {"url": "https://example.invalid", "headers": {"Authorization": credential}}}}
        )
    )

    with pytest.raises(InstructionHubError, match="literal secret"):
        validate_hub(hub_root)


@pytest.mark.parametrize(
    "field",
    [
        "apiKey",
        "APIKey",
        "clientSecret",
        "refreshToken",
        "privateKey",
        "MCP_API_KEY",
        "AWSSecretAccessKey",
        "PGPASSWORD",
        "SYSTEM_ACCESSTOKEN",
        "CLIENTSECRET",
        "REFRESHTOKEN",
        "PRIVATEKEY",
        "AWS_SECRETACCESSKEY",
    ],
)
def test_validate_still_rejects_literal_credential_fields(tmp_path: Path, field: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    (hub_root / "assets/mcps/bad.json").write_text(
        json.dumps({"mcpServers": {"tokenizer": {"command": "npx", "env": {field: "literal-secret"}}}})
    )

    with pytest.raises(InstructionHubError, match="literal secret"):
        validate_hub(hub_root)


@pytest.mark.parametrize("value", ["${DATABASE_PASSWORD}", "${env:DATABASE_PASSWORD}"])
@pytest.mark.parametrize(
    "field", ["PGPASSWORD", "SYSTEM_ACCESSTOKEN", "CLIENTSECRET", "REFRESHTOKEN", "PRIVATEKEY", "AWS_SECRETACCESSKEY"]
)
def test_validate_accepts_credential_environment_references(tmp_path: Path, value: str, field: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    (hub_root / "assets/mcps/postgres.json").write_text(
        json.dumps({"mcpServers": {"postgres": {"command": "postgres-mcp", "env": {field: value}}}})
    )

    validate_hub(hub_root)


@pytest.mark.parametrize("payload", [{"tokens": ["literal-secret"]}, {"api_key": {"value": "literal-secret"}}])
def test_validate_still_rejects_wrapped_literal_credentials(tmp_path: Path, payload: object) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    skill_root = hub_root / "assets/skills/example"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text("# Example\n")
    (skill_root / "config.json").write_text(json.dumps(payload))

    with pytest.raises(InstructionHubError, match="literal secret"):
        validate_hub(hub_root)


@pytest.mark.parametrize(
    "field", ["api_keys", "private_keys", "apikeys", "apiKeys", "privateKeys", "API_KEYS", "PRIVATEKEYS"]
)
@pytest.mark.parametrize("shape", ["list", "value", "default", "wrapped-items"])
@pytest.mark.parametrize("credential", ["literal-secret", "${MCP_SECRET}", "${env:MCP_SECRET}"])
def test_validate_checks_plural_credential_collections(tmp_path: Path, field: str, shape: str, credential: str) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    skill_root = hub_root / "assets/skills/example"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text("# Example\n")
    value: object = [[credential]]
    if shape in {"value", "default"}:
        value = {shape: value}
    elif shape == "wrapped-items":
        value = [{"value": [credential]}, {"default": [credential]}]
    (skill_root / "config.json").write_text(json.dumps({field: value}))

    if credential == "literal-secret":
        with pytest.raises(InstructionHubError, match="literal secret"):
            validate_hub(hub_root)
    else:
        validate_hub(hub_root)


@pytest.mark.parametrize(
    "payload",
    [
        {"mcpServers": []},
        {"servers": "bad"},
        {"bad-server": "bad"},
    ],
)
def test_validate_rejects_malformed_mcp_server_shapes(tmp_path: Path, payload: object) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.json").write_text(json.dumps(payload))

    with pytest.raises(InstructionHubError, match="MCP server"):
        validate_hub(hub_root)


def test_build_rejects_same_priority_duplicate_mcp_servers(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "plugins/pig.yaml").write_text(
        "\n".join(
            [
                "id: pig",
                "name: PIG",
                "includes:",
                "  - mcp:first",
                "  - mcp:second",
                "",
            ]
        )
    )
    (hub_root / "assets/mcps/first.json").write_text(json.dumps({"shared": {"command": "first"}}))
    (hub_root / "assets/mcps/second.json").write_text(json.dumps({"shared": {"command": "second"}}))

    with pytest.raises(InstructionHubError, match="duplicate MCP server 'shared'"):
        build_hub(hub_root)


def test_validate_wraps_malformed_yaml_with_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    config_path = hub_root / "hub.yaml"
    config_path.write_text("org: [\n")

    with pytest.raises(InstructionHubError, match=re.escape(str(config_path))):
        validate_hub(hub_root)


def test_validate_rejects_unimplemented_target_support_source(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/rules/source-mode.md").write_text("# Source Mode\n")
    (hub_root / "assets/rules/source-mode.asset.yaml").write_text(
        "\n".join(
            [
                "support:",
                "  codex:",
                "    mode: projected",
                "    source: native",
                "",
            ]
        )
    )

    with pytest.raises(ValueError, match="source"):
        validate_hub(hub_root)


def test_validate_rejects_mcp_support_modes_that_cannot_render(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/trace.json").write_text(json.dumps({"trace": {"command": "trace-agent"}}))
    (hub_root / "assets/mcps/trace.asset.yaml").write_text(
        "\n".join(
            [
                "support:",
                "  codex:",
                "    mode: projected",
                "",
            ]
        )
    )

    with pytest.raises(InstructionHubError, match="mcp:trace declares unsupported mode"):
        validate_hub(hub_root)


def test_validate_rejects_yaml_values_outside_json_manifest_contract(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    (hub_root / "assets/mcps/bad.yaml").write_text("1: one\n")

    with pytest.raises(ValueError, match="non-string mapping key"):
        validate_hub(hub_root)
