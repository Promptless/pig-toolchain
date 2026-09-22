"""Cursor isolation, saved-result decoding, and append-only recovery contracts."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Iterator

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import cursor, cursor_db
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    CollectionResult,
    HookTraceContext,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.cursor_wire import (
    fields,
    tool_result,
)
from tests.config_helpers import enable_trace_ingestion


def varint(value: int) -> bytes:
    """Encode fixture protobuf varints."""
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def wire(number: int, value: bytes | str | int) -> bytes:
    """Encode a fixture field without a generated Cursor dependency."""
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    data = value.encode() if isinstance(value, str) else value
    return varint((number << 3) | 2) + varint(len(data)) + data


def call(kind: int, result: bytes, identity: str = "call-1") -> bytes:
    """Create a ToolCall with a saved success result."""
    return wire(kind, wire(2, wire(1, result))) + wire(57, identity)


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    """Use only a synthetic database and private collector spool."""
    path = tmp_path / "state.vscdb"
    monkeypatch.setenv("PROMPTLESS_CURSOR_DATABASE", str(path))
    monkeypatch.setattr(cursor, "spool_root", lambda: tmp_path / "spool")
    monkeypatch.setattr(cursor, "transcript_glob", lambda: str(tmp_path / "transcripts/*.jsonl"))
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    yield connection
    connection.close()


def put(connection: sqlite3.Connection, key: str, value: object) -> None:
    """Write a synthetic native value."""
    connection.execute(
        "INSERT OR REPLACE INTO cursorDiskKV VALUES (?, ?)",
        (key, value if isinstance(value, bytes) else json.dumps(value)),
    )
    connection.commit()


def test_oversized_fallback_data_is_capped_without_losing_origin(
    database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cursor, "MAX_RECORD", 1024)
    observation = {
        "event_id": "transcript:0:0",
        "event": {"kind": "session_event", "name": "cursor_transcript_tool_call", "data": {"input": "x" * 4000}},
        "capture": {"source": "transcript", "completeness": "partial"},
    }
    path = cursor.append_observations("session", [observation])
    saved = json.loads(path.read_bytes())
    assert path.stat().st_size <= 1024
    assert saved["event"]["data"] == {}
    assert saved["capture"] == {"source": "transcript", "completeness": "size_limit"}
    cursor.append_observations("session", [observation])
    assert len(path.read_text().splitlines()) == 1


def test_saved_results_are_allowlisted_and_empty_success_is_distinct() -> None:
    identity, result, completeness = tool_result(call(1, wire(5, "saved stdout") + wire(99, "SECRET")))
    assert identity == "call-1"
    assert result == {"stdout": "saved stdout", "status": "success"}
    assert completeness == "available"
    assert tool_result(call(1, b""))[1:] == ({"status": "success"}, "available")
    assert tool_result(wire(1, b"") + wire(57, "call-1"))[2] == "missing"
    assert tool_result(wire(100, b""))[2] == "unsupported"
    with pytest.raises(ValueError):
        fields(b"\x0a\xff")


@pytest.mark.parametrize(
    ("kind", "saved", "expected"),
    [
        (
            8,
            wire(1, "file at execution time") + wire(7, "/never-read"),
            {"content": "file at execution time", "path": "/never-read"},
        ),
        (8, wire(1, "/legacy") + wire(2, "legacy saved"), {"path": "/legacy", "content": "legacy saved"}),
        (12, wire(6, "before") + wire(7, "after"), {"before": "before", "after": "after"}),
        (15, wire(1, wire(1, wire(1, "MCP text"))), {"content": [{"text": {"text": "MCP text"}}]}),
        (
            19,
            wire(1, wire(1, wire(1, "child response"))) + wire(2, "child"),
            {"steps": [{"assistant": {"text": "child response"}}], "agent_id": "child"},
        ),
    ],
)
def test_result_families(kind: int, saved: bytes, expected: dict[str, object]) -> None:
    """Capture saved file, edit, MCP, and subagent data."""
    assert tool_result(call(kind, saved))[1] == {**expected, "status": "success"}


def test_canonical_reference_graph_and_read_only_wal(database: sqlite3.Connection) -> None:
    put(
        database,
        "composerData:session",
        {
            "fullConversationHeadersOnly": [{"bubbleId": "bubble"}],
            "conversationState": base64.b64encode(wire(8, b"turn")).decode(),
            "modelConfig": {"modelName": "model"},
            "encryptionKey": "SECRET",
        },
    )
    put(
        database,
        "bubbleId:session:bubble",
        {
            "toolFormerData": {
                "name": "run_terminal_cmd",
                "toolCallId": "call-1",
                "params": '{"command":"echo saved"}',
            },
            "additionalData": "SECRET",
        },
    )
    put(database, "agentKv:blob:" + b"turn".hex(), wire(1, wire(2, b"step")))
    put(database, "agentKv:blob:" + b"step".hex(), wire(2, call(1, wire(5, "saved stdout"))))
    database.execute("BEGIN IMMEDIATE")
    database.execute("INSERT INTO cursorDiskKV VALUES ('uncommitted', 'value')")
    page = cursor_db.read_session("session", deadline=time.monotonic() + 1)
    assert page.complete
    assert page.records[-1]["event"]["output"]["stdout"] == "saved stdout"
    assert "SECRET" not in json.dumps(page.records)
    database.commit()  # The collector did not block the editor's writer.
    assert database.execute("SELECT count(*) FROM cursorDiskKV").fetchone()[0] == 5


def test_exclusive_database_lock_returns_without_waiting(database: sqlite3.Connection) -> None:
    database.execute("PRAGMA journal_mode=DELETE")
    database.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        cursor_db.read_session("session", deadline=started + 0.5)
    assert time.monotonic() - started < 0.1
    database.rollback()


def test_rollback_journal_is_skipped_to_avoid_delaying_editor_writes(database: sqlite3.Connection) -> None:
    database.execute("PRAGMA journal_mode=DELETE")
    with pytest.raises(ValueError, match="cursor_database_requires_wal"):
        cursor_db.read_session("session", deadline=time.monotonic() + 0.5)
    database.execute("BEGIN EXCLUSIVE")
    database.rollback()


def test_missing_bubble_is_reported_and_retried(database: sqlite3.Connection) -> None:
    put(database, "composerData:session", {"fullConversationHeadersOnly": [{"bubbleId": "pending"}]})
    page = cursor_db.read_session("session", deadline=time.monotonic() + 0.5)
    assert not page.complete
    assert page.records[0]["capture"]["completeness"] == "missing"
    put(database, "bubbleId:session:pending", {"type": 1, "text": "saved"})
    assert cursor_db.read_session("session", deadline=time.monotonic() + 0.5).complete


def test_pending_notification_survives_failed_upload(
    database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import cli

    monkeypatch.setattr(cli, "_run_ensure", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "_run_collect", lambda *args, **kwargs: CollectionResult.INCOMPLETE)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.os, "nice", lambda _: None, raising=False)
    cli._run_cursor_notify({"conversation_id": "session", "generation_id": "g", "secret": "never persist"}, "stop")
    pending = list((cursor.spool_root() / "pending").glob("*.json"))
    assert len(pending) == 1
    assert "never persist" not in pending[0].read_text()
    monkeypatch.setattr(cli, "_run_collect", lambda *args, **kwargs: CollectionResult.COMPLETE)
    cli._run_cursor_notify({"conversation_id": "session", "generation_id": "g"}, "stop")
    assert not list((cursor.spool_root() / "pending").glob("*.json"))


def test_busy_collector_preserves_notification_until_next_wakeup(
    database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A competing process queues work without waiting or starting another collector."""
    from unittest.mock import Mock

    from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import (
        cli,
        storage,
    )

    root = cursor.spool_root()
    root.mkdir()
    program = (
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime "
        "import cli, cursor\n"
        "cursor.spool_root = lambda: Path(sys.argv[1])\n"
        "def unexpected(*args, **kwargs):\n"
        "    raise AssertionError('A busy collector must not enroll or collect')\n"
        "cli._run_ensure = cli._run_collect = unexpected\n"
        "started = time.monotonic()\n"
        "result = cli._run_cursor_notify({'conversation_id': 'session', 'generation_id': 'g'}, 'stop')\n"
        "print(json.dumps({'result': result, 'elapsed': time.monotonic() - started}))\n"
    )
    with (root / "collector.lock").open("a+b") as lock:
        storage._lock_state_file(lock)
        try:
            result = subprocess.run(
                [sys.executable, "-c", program, str(root)],
                capture_output=True,
                text=True,
                check=True,
                timeout=3,
            )
            report = json.loads(result.stdout)
            assert report["result"] == 0
            assert report["elapsed"] < 0.5
            pending = list((root / "pending").glob("*.json"))
            assert len(pending) == 1
            assert json.loads(pending[0].read_text())["conversation_id"] == "session"
        finally:
            storage._unlock_state_file(lock)

    ensure = Mock(return_value=0)
    collect = Mock(return_value=CollectionResult.COMPLETE)
    monkeypatch.setattr(cli, "_run_ensure", ensure)
    monkeypatch.setattr(cli, "_run_collect", collect)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli.os, "nice", lambda _: None, raising=False)
    assert cli._run_cursor_notify({"conversation_id": "next-session"}, "session_start") == 0
    ensure.assert_called_once()
    assert {call.kwargs["hook_context"].session_id for call in collect.call_args_list} == {"session", "next-session"}
    assert not list((root / "pending").glob("*.json"))


def test_spool_limit_preserves_existing_journal(database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    first = {"event_id": "a", "event": {"kind": "user_message", "text": "saved"}}
    path = cursor.append_observations("session", [first])
    prefix = path.read_bytes()
    monkeypatch.setattr(cursor, "MAX_SPOOL", len(prefix))
    with pytest.raises(ValueError, match="cursor_spool_size_limit"):
        cursor.append_observations("session", [{**first, "event_id": "b"}])
    assert path.read_bytes() == prefix


def test_journal_revisions_preserve_acknowledged_bytes_and_native_identity(database: sqlite3.Connection) -> None:
    first = {
        "event_id": "call:result",
        "event": {"kind": "tool_result", "tool": "shell", "call_id": "call", "output": None},
        "capture": {"source": "database", "completeness": "missing"},
    }
    path = cursor.append_observations("session", [first])
    prefix = path.read_bytes()
    cursor.append_observations("session", [first])
    assert path.read_bytes() == prefix
    with path.open("ab") as handle:
        handle.write(b'{"torn')
    second = {
        **first,
        "event": {**first["event"], "output": "saved"},
        "capture": {"source": "database", "completeness": "available"},
    }
    cursor.append_observations("session", [second])
    assert path.read_bytes().startswith(prefix)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["revision"] for row in records] == [1, 2]
    cursor.append_observations("session", [first])
    assert len(path.read_text().splitlines()) == 2
    with pytest.raises(ValueError):
        cursor.append_observations("../escape", [first])


def test_fallback_to_database_amends_matching_messages(database: sqlite3.Connection) -> None:
    message = {"kind": "user_message", "text": "same prompt"}
    path = cursor.append_observations(
        "session",
        [
            {
                "event_id": "transcript:0:0",
                "event": message,
                "capture": {"source": "transcript", "completeness": "partial"},
            }
        ],
    )
    native = {
        "event_id": "bubble:text",
        "event": message,
        "capture": {"source": "database", "completeness": "available"},
    }
    cursor.append_observations("session", [native, native])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["revision"] for row in rows] == [1, 2]
    assert {row["event_id"] for row in rows} == {"transcript:0:0"}


def test_pages_resume_before_terminal_and_child_hook_never_closes_parent(
    database: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cursor_db, "PAGE_SIZE", 1)
    put(database, "composerData:child", {"fullConversationHeadersOnly": [{"bubbleId": "a"}, {"bubbleId": "b"}]})
    for identity in ("a", "b"):
        put(database, "bubbleId:child:" + identity, {"type": 1, "text": identity})
    context = HookTraceContext(
        session_id="parent",
        agent_id="child",
        generation_id="g",
        transcript_path=None,
        agent_transcript_path=None,
        parent_session_id=None,
        agent_type=None,
    )
    first = cursor.prepare_journals(context, "subagent_stop")
    assert not first.complete
    assert first.context.transcript_path is not None
    assert "subagent_stop" not in first.context.transcript_path.read_text()
    second = cursor.prepare_journals(context, "subagent_stop")
    assert second.complete
    assert second.context.transcript_path is not None
    rows = [json.loads(line) for line in second.context.transcript_path.read_text().splitlines()]
    assert {row["session_id"] for row in rows} == {"child"}
    assert rows[-1]["parent_session_id"] == "parent"
    assert rows[-1]["event"]["name"] == "subagent_stop"


def test_cursor_hooks_detach_slow_work_and_close_output_pipes(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Cursor hooks")
    hub = tmp_path / "hub"
    init_hub(hub)
    enable_trace_ingestion(hub)
    build_hub(hub)
    plugin = hub / "dist/cursor/pig"
    config = json.loads((plugin / "hooks/hooks.json").read_text())
    assert set(config["hooks"]) == {"sessionStart", "stop", "sessionEnd", "subagentStop"}
    assert all(entries[-1]["timeout"] == 0.25 for entries in config["hooks"].values())
    marker = tmp_path / "finished.json"
    runtime = plugin / "runtime/promptless-host-runtime"
    runtime.write_text(
        "import json, pathlib, sys, time\nbody=json.load(sys.stdin)\ntime.sleep(0.8)\npathlib.Path("
        + repr(str(marker))
        + ").write_text(json.dumps(body))\n"
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": str(Path(shutil.which("python3")).parent) + os.pathsep + os.environ["PATH"],
    }
    start = time.monotonic()
    result = subprocess.run(
        [node, str(plugin / "runtime/cursor-hook.cjs"), "stop"],
        input=json.dumps({"conversation_id": "session", "generation_id": "g", "prompt": "SECRET"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=0.5,
        check=False,
    )
    assert time.monotonic() - start < 0.5
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    assert not marker.exists()
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert json.loads(marker.read_text()) == {"conversation_id": "session", "generation_id": "g"}


def test_unsaved_session_remains_pending(database: sqlite3.Connection) -> None:
    """A hook can run before Cursor commits its first composer row."""
    context = HookTraceContext(
        session_id="new",
        transcript_path=None,
        agent_transcript_path=None,
        parent_session_id=None,
        agent_id=None,
        agent_type=None,
    )
    exported = cursor.prepare_journals(context, "session_end")
    assert not exported.complete
    assert not list((cursor.spool_root() / "journals").glob("*.jsonl"))


def test_cursor_collect_uploads_only_acknowledged_journal_ranges(tmp_path: Path) -> None:
    """Exercise generated bundle, enrollment, native export, gzip upload and ledger."""
    import gzip
    from .helpers import _FakeWorkerServer, _policy_with, _run_collect, _run_runtime_json

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


def test_journal_retains_complete_result_after_pruned_ui_fallback(database: sqlite3.Connection) -> None:
    """A pruned database snapshot cannot supersede an already retained result."""
    put(database, "composerData:session", {"fullConversationHeadersOnly": [{"bubbleId": "tool"}]})
    tool = {"name": "run_terminal_cmd", "toolCallId": "call-1", "status": "completed"}
    put(
        database,
        "bubbleId:session:tool",
        {
            "toolFormerData": {
                **tool,
                "toolCallBinary": base64.b64encode(call(1, wire(5, "complete stdout"))).decode(),
            }
        },
    )
    page = cursor_db.read_session("session", deadline=time.monotonic() + 1)
    path = cursor.append_observations("session", page.records)
    before = path.read_bytes()
    put(database, "bubbleId:session:tool", {"toolFormerData": {**tool, "result": "truncated UI result"}})
    page = cursor_db.read_session("session", deadline=time.monotonic() + 1)
    assert page.records[-1]["capture"]["completeness"] == "partial"
    cursor.append_observations("session", page.records)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("program", "status"),
    [
        ("import sys; sys.stdin.read()", "completed"),
        ("raise RuntimeError('SECRET')", "collector_failed"),
        ("import time; time.sleep(30)", "collector_timeout"),
    ],
)
def test_detached_launcher_records_collector_outcome(tmp_path: Path, program: str, status: str) -> None:
    """Crashes and watchdog kills produce only bounded metadata after detachment."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Cursor hooks")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    source = Path(cursor.__file__).parents[1] / "cursor-hook.cjs"
    hook = runtime / source.name
    shutil.copyfile(source, hook)
    (runtime / "promptless-host-runtime").write_text(program)
    # Exercise the actual watchdog without waiting two minutes in the test.
    preload = tmp_path / "timers.cjs"
    preload.write_text(
        "const original = global.setTimeout;\n"
        "global.setTimeout = (fn, ms, ...args) => original(fn, ms === 120000 ? 200 : ms, ...args);\n"
    )
    result = subprocess.run(
        [node, "--require", str(preload), str(hook), "stop", "--background", "e30="],
        env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert not result.stdout and not result.stderr
    diagnostic = json.loads((tmp_path / ".promptless/instruction-hub/cursor-launcher-status.json").read_text())
    assert diagnostic["status"] == status
    assert set(diagnostic) == {"status", "observed_at"}
