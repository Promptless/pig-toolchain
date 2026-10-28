"""Generated Cursor hook and native capture integration tests."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from promptless_instruction_hub.compiler import build_hub, init_hub
from tests.config_helpers import enable_trace_ingestion


def put(connection: sqlite3.Connection, key: str, value: object) -> None:
    """Write a synthetic native value."""
    connection.execute(
        "INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
        (key, value if isinstance(value, bytes) else json.dumps(value)),
    )
    connection.commit()


def test_cursor_hooks_detach_and_close_output_pipes(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    init_hub(hub, org="Promptless")
    enable_trace_ingestion(hub)
    build_hub(hub)
    plugin = hub / "dist/cursor/pig"
    config = json.loads((plugin / "hooks/hooks.json").read_text())
    assert set(config["hooks"]) == {"sessionStart", "stop", "sessionEnd", "subagentStop"}
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PATH": "",
        "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "1",
        "PROMPTLESS_WORKER_BASE_URL": "http://127.0.0.1:9",
    }
    result = subprocess.run(
        [str(plugin / "runtime/promptless-host-runtime"), "cursor-notify", "--detach", "--lifecycle", "stop"],
        input=json.dumps({"conversation_id": "session", "generation_id": "g", "prompt": "SECRET"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=2,
        check=True,
    )
    assert result.stdout == result.stderr == ""
    queue = tmp_path / ".promptless/instruction-hub/cursor/pending"
    deadline = time.monotonic() + 5
    while not list(queue.glob("*.json")) and time.monotonic() < deadline:
        time.sleep(0.02)
    pending = list(queue.glob("*.json"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text()) == {
        "lifecycle": "stop",
        "conversation_id": "session",
        "generation_id": "g",
    }


def test_cursor_collect_uploads_only_acknowledged_journal_ranges(tmp_path: Path) -> None:
    """Exercise generated bundle, enrollment, native export, gzip upload and ledger."""
    import gzip
    from .helpers import _FakeWorkerServer, _policy_with, _run_collect, _run_runtime_json, _scoped_ledger_path_for_test

    hub = tmp_path / "hub"
    init_hub(hub, org="Promptless")
    enable_trace_ingestion(hub)
    build_hub(hub)
    plugin = hub / "dist/cursor/pig"
    native = tmp_path / "state.vscdb"
    connection = sqlite3.connect(native)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    put(connection, "composerData:session", {"fullConversationHeadersOnly": [{"bubbleId": "b"}]})
    put(connection, "bubbleId:session:b", {"type": 1, "text": "first"})
    server = _FakeWorkerServer(policy=_policy_with(enabled_hosts=["cursor"]), enforce_trace_watermarks=True)
    server.start()
    try:
        ledger = tmp_path / "ledger.json"
        env = {
            "HOME": str(tmp_path / "home"),
            "CURSOR_PLUGIN_ROOT": str(plugin),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
            "PROMPTLESS_HOST_RUNTIME_LEDGER": str(ledger),
            "PROMPTLESS_CURSOR_DATABASE": str(native),
        }
        _run_runtime_json(plugin, ["enroll", "--host", "cursor"], env)
        ledger = _scoped_ledger_path_for_test(
            ledger, home=tmp_path / "home", worker_base_url=server.base_url, host="cursor"
        )
        args = ["collect", "--host", "cursor", "--lifecycle", "stop", "--quiet"]
        context = {"conversation_id": "session", "generation_id": "generation"}
        _run_collect(plugin, args, env, context)
        assert len(server.trace_batches) == 1
        chunk = server.trace_batches[0]["chunks"][0]
        assert server.trace_batches[0]["source"] == "cursor" and chunk["start_offset"] == 0
        prefix = gzip.decompress(base64.b64decode(chunk["content_base64"]))
        assert b'"text":"first"' in prefix
        assert next(iter(json.loads(ledger.read_text())["sources"].values()))["end_offset"] == len(prefix)
        _run_collect(plugin, args, env, context)
        assert len(server.trace_batches) == 1
        put(connection, "bubbleId:session:b", {"type": 1, "text": "updated"})
        _run_collect(plugin, args, env, context)
        chunk = server.trace_batches[-1]["chunks"][0]
        assert chunk["start_offset"] == len(prefix)
        revision = json.loads(gzip.decompress(base64.b64decode(chunk["content_base64"])))
        assert revision["revision"] == 2 and revision["event"]["text"] == "updated"
    finally:
        server.stop()
        connection.close()
