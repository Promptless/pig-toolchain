from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.config_helpers import enable_trace_ingestion
from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.native_manifest import MANIFEST, validate_bundle
from .helpers import HOST_RUNTIME_BIN, _clean_env, _write_native_hook_asset


def test_build_injects_native_runtime(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    init_hub(hub, org="Promptless")
    enable_trace_ingestion(hub)
    build_hub(hub)
    for target in ("claude", "codex", "cursor"):
        plugin = hub / "dist" / target / "pig"
        runtime = plugin / "runtime"
        manifest = validate_bundle(runtime, complete=False)
        assert not list(runtime.rglob("*.py"))
        assert not list(runtime.rglob("*.cjs"))
        managed = json.loads((plugin / "hub.managed-runtimes.json").read_text())["managed_runtimes"][0]
        assert managed["sha256"] == manifest["bundle_sha256"]
        assert managed["source_sha256"] == manifest["source_sha256"]
        assert managed["platforms"] == manifest["platforms"]
        hooks = json.loads((plugin / "hooks/hooks.json").read_text())
        assert set(hooks["hooks"]) == (
            {"sessionStart", "stop", "sessionEnd", "subagentStop"}
            if target == "cursor"
            else {"SessionStart", "Stop", "SessionEnd", "SubagentStop"}
        )
        command = str(runtime / (HOST_RUNTIME_BIN + (".exe" if os.name == "nt" else "")))
        result = subprocess.run(
            [command, "version", "--json"],
            env=_clean_env(HOME=str(tmp_path / "home"), PATH=""),
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout)["version"] == "0.4.0"
        assert result.stderr == ""
        assert "/runtime/** -text" in (plugin / ".gitattributes").read_text()


def test_native_bundle_rejects_tampering_and_extra_files(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    init_hub(hub, org="Promptless")
    enable_trace_ingestion(hub)
    build_hub(hub)
    runtime = hub / "dist/codex/pig/runtime"
    manifest = json.loads((runtime / MANIFEST).read_text())
    artifact = runtime / next(name for name in manifest["files"] if name.startswith("native/"))
    original = artifact.read_bytes()
    artifact.write_bytes(original + b"mutation")
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_bundle(runtime, complete=False)
    artifact.write_bytes(original)
    (runtime / "unexpected.py").write_text("raise SystemExit(99)")
    with pytest.raises(ValueError, match="unexpected"):
        validate_bundle(runtime, complete=False)


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX fallback; Windows command is covered by platform smoke")
def test_codex_terminal_hook_skips_incomplete_sibling_and_updates_plugin_root(tmp_path: Path) -> None:
    from promptless_instruction_hub.managed_runtime import _posix_native_terminal_hook_command

    root = tmp_path / "plugin cache"
    valid = root / "1.0.1" / "runtime" / HOST_RUNTIME_BIN
    valid.parent.mkdir(parents=True)
    valid.write_text('#!/bin/sh\n[ "$1" = version ] && exit 0\nprintf "%s\\n" "$PLUGIN_ROOT" "$@" > "$CAPTURE"\n')
    valid.chmod(0o755)
    incomplete = root / "1.0.2" / "runtime" / HOST_RUNTIME_BIN
    incomplete.parent.mkdir(parents=True)
    incomplete.write_text("#!/bin/sh\nexit 127\n")
    incomplete.chmod(0o755)
    capture = tmp_path / "capture"
    env = _clean_env(PLUGIN_ROOT=str(root / "1.0.0"), CAPTURE=str(capture), PATH="")
    result = subprocess.run(
        ["/bin/sh", "-c", _posix_native_terminal_hook_command("stop")],
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0 and not result.stdout and not result.stderr
    assert capture.read_text().splitlines() == [
        str(valid.parent.parent),
        "collect",
        "--host",
        "codex",
        "--lifecycle",
        "stop",
        "--detach",
        "--quiet",
    ]
    valid.unlink()
    capture.unlink()
    result = subprocess.run(
        ["/bin/sh", "-c", _posix_native_terminal_hook_command("stop")],
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0 and not result.stdout and not result.stderr
    assert not capture.exists()
