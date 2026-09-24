from __future__ import annotations

import json
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from tests.config_helpers import enable_trace_ingestion
from .helpers import HOST_RUNTIME_BIN, HOST_RUNTIME_PACKAGE, _run_runtime_json, _write_native_hook_asset


def test_host_runtime_bundle_digest_tracks_runtime_files_only(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    enable_trace_ingestion(hub_root)
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
    bin_root = plugin_root / "runtime"
    runtime_path = bin_root / HOST_RUNTIME_BIN
    manifest = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
    manifest_sha256 = manifest["managed_runtimes"][0]["sha256"]
    env = {"HOME": str(tmp_path / "home"), "PLUGIN_ROOT": str(plugin_root)}

    original_launcher = runtime_path.read_bytes()
    runtime_path.write_bytes(original_launcher + b"\n# digest mutation\n")
    launcher_payload, _ = _run_runtime_json(plugin_root, ["version", "--json"], env)
    assert launcher_payload["sha256"] != manifest_sha256
    runtime_path.write_bytes(original_launcher)

    module_path = bin_root / HOST_RUNTIME_PACKAGE / "contracts.py"
    original_module = module_path.read_bytes()
    module_path.write_bytes(original_module + b"\n# digest mutation\n")
    module_payload, _ = _run_runtime_json(plugin_root, ["version", "--json"], env)
    assert module_payload["sha256"] != manifest_sha256
    module_path.write_bytes(original_module)

    cache_root = bin_root / HOST_RUNTIME_PACKAGE / "__pycache__"
    cache_root.mkdir()
    (cache_root / "contracts.cpython-311.pyc").write_bytes(b"ignored bytecode")
    cache_payload, _ = _run_runtime_json(plugin_root, ["version", "--json"], env)
    assert cache_payload["sha256"] == manifest_sha256


def test_build_appends_bootstrap_hook_to_existing_hook_asset(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    enable_trace_ingestion(hub_root)
    _write_native_hook_asset(
        hub_root,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [{"type": "command", "command": "existing-hook"}],
                    }
                ]
            }
        },
    )

    build_hub(hub_root)

    hooks = json.loads((hub_root / "dist/codex/pig/hooks/hooks.json").read_text())
    session_start = hooks["hooks"]["SessionStart"]
    assert session_start[0]["hooks"][0]["command"] == "existing-hook"
    assert f"runtime/{HOST_RUNTIME_BIN}" in session_start[1]["hooks"][0]["command"]


def test_build_leaves_customer_package_hook_unmanaged(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    enable_trace_ingestion(hub_root)
    _write_native_hook_asset(
        hub_root,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [{"type": "command", "command": "customer-hook"}],
                    }
                ]
            }
        },
        package_id="customer",
    )

    build_hub(hub_root)

    hooks = json.loads((hub_root / "dist/codex/customer/hooks/hooks.json").read_text())
    assert hooks["hooks"]["SessionStart"] == [
        {
            "matcher": "startup",
            "hooks": [{"type": "command", "command": "customer-hook"}],
        }
    ]
    assert not (hub_root / "dist/codex/customer/runtime").exists()
    assert not (hub_root / "dist/codex/customer/hub.managed-runtimes.json").exists()


def test_build_rejects_malformed_existing_hook_asset(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    enable_trace_ingestion(hub_root)
    _write_native_hook_asset(hub_root, {"hooks": []})

    with pytest.raises(InstructionHubError, match="field hooks must be a JSON object"):
        build_hub(hub_root)
