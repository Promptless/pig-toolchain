from __future__ import annotations

from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, read_json_mapping, read_yaml_mapping, write_json, write_yaml
from promptless_instruction_hub.models import SUPPORTED_HARNESSES, Harness


def _mcp_hub(
    tmp_path: Path,
    server: dict[str, JsonValue],
    *,
    targets: tuple[Harness, ...] = SUPPORTED_HARNESSES,
) -> Path:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    config = read_yaml_mapping(hub_root / "hub.yaml")
    config["targets"] = list(targets)
    write_yaml(hub_root / "hub.yaml", config)
    write_json(hub_root / "assets/mcps/remote.json", {"mcpServers": {"remote": server}})
    write_yaml(hub_root / "plugins/pig.yaml", {"id": "pig", "name": "PIG", "includes": ["mcp:remote"]})
    return hub_root


def _rendered_servers(hub_root: Path, target: Harness) -> dict[str, JsonValue]:
    filename = {"gemini": "gemini-extension.json", "cursor": "mcp.json"}.get(target, ".mcp.json")
    rendered = read_json_mapping(hub_root / "dist" / target / "pig" / filename)
    servers = rendered["mcpServers"]
    assert isinstance(servers, dict)
    return servers


@pytest.mark.parametrize(
    "connection",
    [
        {"type": "http", "url": "https://example.invalid/mcp"},
        {"transport": "http", "url": "https://example.invalid/mcp"},
        {"httpUrl": "https://example.invalid/mcp"},
        {"url": "https://example.invalid/mcp"},
    ],
    ids=["explicit-http", "transport-http", "gemini-http", "default-http"],
)
def test_build_renders_http_transport_for_each_host(tmp_path: Path, connection: dict[str, JsonValue]) -> None:
    headers: dict[str, JsonValue] = {"X-Customer": "acme"}
    hub_root = _mcp_hub(tmp_path, {**connection, "headers": headers})

    build_hub(hub_root)

    for target in ("claude", "codex"):
        assert _rendered_servers(hub_root, target)["remote"] == {
            "type": "http",
            "url": "https://example.invalid/mcp",
            "headers": headers,
        }
    assert _rendered_servers(hub_root, "cursor")["remote"] == {
        "url": "https://example.invalid/mcp",
        "headers": headers,
    }
    assert _rendered_servers(hub_root, "gemini")["remote"] == {
        "httpUrl": "https://example.invalid/mcp",
        "headers": headers,
    }


@pytest.mark.parametrize("transport", [{}, {"type": "stdio"}, {"transport": "stdio"}])
def test_build_preserves_stdio_command_arguments_and_environment(
    tmp_path: Path, transport: dict[str, JsonValue]
) -> None:
    server: dict[str, JsonValue] = {
        "command": "npx",
        "args": ["-y", "@example/mcp-server"],
        "env": {"CUSTOMER_REGION": "us-east-1"},
    }
    hub_root = _mcp_hub(tmp_path, {**server, **transport})

    build_hub(hub_root)

    for target in SUPPORTED_HARNESSES:
        assert _rendered_servers(hub_root, target)["remote"] == server


def test_build_accepts_single_gemini_http_server_without_wrapper(tmp_path: Path) -> None:
    hub_root = _mcp_hub(tmp_path, {})
    write_json(hub_root / "assets/mcps/remote.json", {"httpUrl": "https://example.invalid/mcp"})

    build_hub(hub_root)

    assert _rendered_servers(hub_root, "claude")["remote"] == {
        "type": "http",
        "url": "https://example.invalid/mcp",
    }


def test_build_renders_sse_for_enabled_hosts_without_codex(tmp_path: Path) -> None:
    hub_root = _mcp_hub(
        tmp_path,
        {"type": "sse", "url": "https://example.invalid/events"},
        targets=("claude", "cursor", "gemini"),
    )

    build_hub(hub_root)

    assert _rendered_servers(hub_root, "claude")["remote"] == {
        "type": "sse",
        "url": "https://example.invalid/events",
    }
    for target in ("cursor", "gemini"):
        assert _rendered_servers(hub_root, target)["remote"] == {"url": "https://example.invalid/events"}


@pytest.mark.parametrize(
    "server",
    [
        {"command": "server", "url": "https://example.invalid/mcp"},
        {"command": "server", "httpUrl": "https://example.invalid/mcp"},
        {"url": "https://example.invalid/mcp", "httpUrl": "https://example.invalid/mcp"},
        {"type": "stdio", "url": "https://example.invalid/mcp"},
        {"type": "http", "command": "server"},
        {"type": "sse", "httpUrl": "https://example.invalid/mcp"},
        {"type": "http", "transport": "sse", "url": "https://example.invalid/mcp"},
        {"type": "unknown", "url": "https://example.invalid/mcp"},
        {"url": ""},
        {"command": 42},
    ],
    ids=[
        "command-and-url",
        "command-and-http-url",
        "url-and-http-url",
        "stdio-url",
        "http-command",
        "sse-http-url",
        "conflicting-transport-declarations",
        "unknown-transport",
        "empty-url",
        "non-string-command",
    ],
)
def test_verify_rejects_invalid_transport_with_server_context(tmp_path: Path, server: dict[str, JsonValue]) -> None:
    hub_root = _mcp_hub(tmp_path, server)

    with pytest.raises(InstructionHubError, match="MCP") as exc_info:
        verify_hub(hub_root)

    assert "mcp:remote" in str(exc_info.value)


@pytest.mark.parametrize(
    ("transport", "target"),
    [("sse", "codex"), ("ws", "codex"), ("ws", "cursor"), ("ws", "gemini")],
)
def test_verify_rejects_transport_unsupported_by_enabled_host(tmp_path: Path, transport: str, target: Harness) -> None:
    hub_root = _mcp_hub(tmp_path, {"type": transport, "url": "https://example.invalid/mcp"}, targets=(target,))

    with pytest.raises(InstructionHubError, match=f"{transport} transport is unsupported for {target}"):
        verify_hub(hub_root)


def test_build_accepts_claude_websocket_when_other_hosts_are_disabled(tmp_path: Path) -> None:
    server: dict[str, JsonValue] = {"type": "ws", "url": "wss://example.invalid/mcp"}
    hub_root = _mcp_hub(tmp_path, server, targets=("claude",))

    build_hub(hub_root)

    assert _rendered_servers(hub_root, "claude")["remote"] == server


def test_build_skips_transport_validation_for_unsupported_asset_target(tmp_path: Path) -> None:
    hub_root = _mcp_hub(tmp_path, {"type": "sse", "url": "https://example.invalid/events"})
    write_yaml(
        hub_root / "assets/mcps/remote.asset.yaml",
        {"support": {"codex": {"mode": "unsupported", "reason": "The service only provides SSE."}}},
    )

    build_hub(hub_root)

    assert not (hub_root / "dist/codex/pig/.mcp.json").exists()
    assert _rendered_servers(hub_root, "gemini")["remote"] == {"url": "https://example.invalid/events"}


def test_build_validates_transport_after_selecting_host_specific_override(tmp_path: Path) -> None:
    hub_root = _mcp_hub(tmp_path, {"type": "sse", "url": "https://example.invalid/events"})
    write_json(
        hub_root / "assets/mcps/codex-remote.json",
        {"mcpServers": {"remote": {"type": "http", "url": "https://example.invalid/mcp"}}},
    )
    write_yaml(
        hub_root / "assets/mcps/codex-remote.asset.yaml",
        {
            "support": {
                target: {"mode": "unsupported", "reason": "Only Codex needs the HTTP override."}
                for target in ("claude", "cursor", "gemini")
            }
        },
    )
    write_yaml(
        hub_root / "plugins/pig.yaml",
        {"id": "pig", "name": "PIG", "includes": ["mcp:remote", "mcp:codex-remote"]},
    )

    build_hub(hub_root)

    assert _rendered_servers(hub_root, "codex")["remote"] == {
        "type": "http",
        "url": "https://example.invalid/mcp",
    }
    assert _rendered_servers(hub_root, "claude")["remote"] == {
        "type": "sse",
        "url": "https://example.invalid/events",
    }
