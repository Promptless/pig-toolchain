from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from tests.config_helpers import enable_trace_ingestion

from .helpers import _FakeWorkerServer, _policy_with, _run_bootstrap, _run_collect, _run_runtime_json


@pytest.mark.parametrize("override", ["~/Claude [work profile]", "", None])
def test_claude_settings_and_idle_discovery_share_config_root(tmp_path: Path, override: str | None) -> None:
    hub = tmp_path / "hub"
    init_hub(hub, org="Example")
    enable_trace_ingestion(hub)
    build_hub(hub)
    plugin = hub / "dist/claude/pig"
    home = tmp_path / "home"
    configured_root = home / ("Claude [work profile]" if override else ".claude")
    transcript = configured_root / "projects/project/session.jsonl"
    transcript.parent.mkdir(parents=True)
    expected = b'{"type":"user","message":{"role":"user","content":"configured root"}}\n'
    transcript.write_bytes(expected)
    settings = configured_root / "settings.json"
    settings.write_text(json.dumps({"env": {"PROMPTLESS_MANAGED_HOST_ENROLLMENT": "1"}, "theme": "dark"}))
    if override:
        decoy = home / ".claude/projects/old/session.jsonl"
        decoy.parent.mkdir(parents=True)
        decoy.write_text('{"type":"user","message":"wrong root"}\n')
        (home / ".claude/settings.json").write_text('{"theme":"light"}\n')
    server = _FakeWorkerServer(policy=_policy_with(enabled_hosts=["claude"]))
    server.start()
    try:
        env = {
            "HOME": str(home),
            "PLUGIN_ROOT": str(plugin),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(tmp_path / "ledger.json"),
        }
        if override is not None:
            env["CLAUDE_CONFIG_DIR"] = override
        _run_runtime_json(plugin, ["enroll", "--host", "claude"], env)
        _run_bootstrap(plugin, "claude", env, expected_status="needs_restart")
        assert json.loads(settings.read_text()) == {"theme": "dark"}
        status, _ = _run_runtime_json(plugin, ["status", "--host", "claude"], env)
        assert str(settings) in json.dumps(status)
        _run_collect(
            plugin,
            ["collect", "--host", "claude", "--lifecycle", "session_start", "--include-active", "--quiet"],
            env,
            {},
        )
        assert len(server.trace_batches) == 1
        chunks = server.trace_batches[0]["chunks"]
        assert isinstance(chunks, list) and len(chunks) == 1
        chunk = chunks[0]
        assert isinstance(chunk, dict)
        encoded = chunk["content_base64"]
        assert isinstance(encoded, str)
        assert gzip.decompress(base64.b64decode(encoded)) == expected
        if override:
            assert (home / ".claude/settings.json").read_text() == '{"theme":"light"}\n'
    finally:
        server.stop()
