"""Upload acknowledgements belong to one destination, host identity, and trace source."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.fs import JsonValue, validate_json_value
from tests.config_helpers import enable_trace_ingestion

from .helpers import (
    _FakeWorkerHandler,
    _FakeWorkerServer,
    _approved_poll_response,
    _host_state_path,
    _json_list,
    _json_mapping,
    _json_string,
    _policy_with,
    _run_collect,
    _run_runtime_json,
    _session_response,
)

FIRST = b'{"kind":"session_start","message":"first"}\n'
SECOND = b'{"kind":"note","message":"second"}\n'
THIRD = b'{"kind":"stop","message":"third"}\n'


@dataclass
class CollectionFixture:
    root: Path
    plugin: Path
    server: _FakeWorkerServer
    env: dict[str, str]
    transcript: Path

    @property
    def state_path(self) -> Path:
        return _host_state_path(Path(self.env["HOME"]))

    def enroll(self, host: str = "codex") -> None:
        _run_runtime_json(self.plugin, ["enroll", "--host", host], self.env)

    def collect(self, host: str = "codex") -> list[dict[str, JsonValue]]:
        self.server.trace_batches.clear()
        _run_collect(
            self.plugin,
            ["collect", "--host", host, "--lifecycle", "stop", "--quiet"],
            self.env,
            {"sessionId": "session", "transcriptPath": str(self.transcript)},
        )
        return list(self.server.trace_batches)


@pytest.fixture
def collection(tmp_path: Path) -> Iterator[CollectionFixture]:
    hub = tmp_path / "hub"
    init_hub(hub, org="Acme")
    enable_trace_ingestion(hub)
    build_hub(hub)
    server = _FakeWorkerServer(policy=_policy_with(enabled_hosts=["codex", "claude", "claude-desktop", "cursor"]))
    server.start()
    home = tmp_path / "home"
    plugin = hub / "dist/codex/pig"
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(FIRST)
    try:
        yield CollectionFixture(
            root=tmp_path,
            plugin=plugin,
            server=server,
            env={
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "PLUGIN_ROOT": str(plugin),
                "PROMPTLESS_WORKER_BASE_URL": server.base_url,
                "PROMPTLESS_DASHBOARD_BASE_URL": server.base_url,
                "PROMPTLESS_HOST_RUNTIME_LEDGER": str(tmp_path / "ledger.json"),
            },
            transcript=transcript,
        )
    finally:
        server.stop()


def assert_uploaded(batches: list[dict[str, JsonValue]], expected: bytes, *, start: int, source: str = "codex") -> None:
    assert len(batches) == 1
    assert batches[0]["source"] == source
    chunks = _json_list(batches[0]["chunks"], "chunks")
    assert len(chunks) == 1
    chunk = _json_mapping(chunks[0], "chunk")
    assert chunk["start_offset"] == start
    assert chunk["end_offset"] == start + len(expected)
    assert gzip.decompress(base64.b64decode(_json_string(chunk["content_base64"], "content"))) == expected


def test_switching_worker_destinations_resumes_each_acknowledged_range(collection: CollectionFixture) -> None:
    collection.enroll()
    assert_uploaded(collection.collect(), FIRST, start=0)
    collection.transcript.write_bytes(FIRST + SECOND)

    # Two routable worker URLs deliberately share the same deployment and host IDs.
    alternate_url = collection.server.base_url.replace("127.0.0.1", "localhost")
    collection.env["PROMPTLESS_WORKER_BASE_URL"] = alternate_url
    collection.enroll()
    assert_uploaded(collection.collect(), FIRST + SECOND, start=0)

    collection.transcript.write_bytes(FIRST + SECOND + THIRD)
    collection.env["PROMPTLESS_WORKER_BASE_URL"] = collection.server.base_url
    assert_uploaded(collection.collect(), SECOND + THIRD, start=len(FIRST))
    assert collection.collect() == []
    collection.env["PROMPTLESS_WORKER_BASE_URL"] = alternate_url
    assert_uploaded(collection.collect(), THIRD, start=len(FIRST + SECOND))
    assert collection.collect() == []
    assert not Path(collection.env["PROMPTLESS_HOST_RUNTIME_LEDGER"]).exists()


def test_normalized_worker_url_preserves_acknowledgements(collection: CollectionFixture) -> None:
    collection.enroll()
    assert_uploaded(collection.collect(), FIRST, start=0)
    collection.env["PROMPTLESS_WORKER_BASE_URL"] += "/"
    assert collection.collect() == []
    collection.transcript.write_bytes(FIRST + SECOND)
    assert_uploaded(collection.collect(), SECOND, start=len(FIRST))


def test_replaced_deployment_at_same_url_starts_its_own_acknowledgements(
    collection: CollectionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection.enroll()
    assert_uploaded(collection.collect(), FIRST, start=0)
    collection.transcript.write_bytes(FIRST + SECOND)

    monkeypatch.setattr(_FakeWorkerHandler, "deployment_instance_id", "replacement-deployment")
    session = _session_response()
    session["deployment_instance_id"] = "replacement-deployment"
    monkeypatch.setattr(_FakeWorkerHandler, "session_response", session)
    collection.enroll()
    assert_uploaded(collection.collect(), FIRST + SECOND, start=0)
    assert collection.collect() == []

    monkeypatch.setattr(_FakeWorkerHandler, "deployment_instance_id", "worker-local-1")
    assert_uploaded(collection.collect(), SECOND, start=len(FIRST))


def test_old_global_ledger_cannot_suppress_a_destination_upload(collection: CollectionFixture) -> None:
    base = Path(collection.env["PROMPTLESS_HOST_RUNTIME_LEDGER"])
    old_ledger = json.dumps(
        {
            "schema_version": 1,
            "sources": {
                hashlib.sha256(str(collection.transcript.resolve()).encode()).hexdigest(): {
                    "path": str(collection.transcript.resolve()),
                    "end_offset": len(FIRST),
                }
            },
        }
    ).encode()
    base.write_bytes(old_ledger)
    collection.enroll()
    assert_uploaded(collection.collect(), FIRST, start=0)
    assert collection.collect() == []
    assert base.read_bytes() == old_ledger


def test_credential_reset_renewal_and_plugin_upgrade_preserve_offsets(
    collection: CollectionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection.enroll()
    assert_uploaded(collection.collect(), FIRST, start=0)
    original_host = json.loads(collection.state_path.read_text())["host_instance_id"]
    _run_runtime_json(collection.plugin, ["reset", "--host", "codex", "--yes"], collection.env)
    assert json.loads(collection.state_path.read_text())["host_instance_id"] == original_host

    monkeypatch.setattr(_FakeWorkerHandler, "host_credential", "renewed-test-credential")
    monkeypatch.setattr(
        _FakeWorkerHandler,
        "poll_response",
        _approved_poll_response(host_credential="renewed-test-credential", credential_id="renewed-credential-id"),
    )
    collection.enroll()
    manifest_path = collection.plugin / "hub.managed-runtimes.json"
    manifest = _json_mapping(validate_json_value(json.loads(manifest_path.read_text()), "manifest"), "manifest")
    for value in _json_list(manifest["managed_runtimes"], "managed_runtimes"):
        _json_mapping(value, "runtime")["plugin_version"] = "9.9.9"
    manifest_path.write_text(json.dumps(manifest))
    assert collection.collect() == []
    collection.transcript.write_bytes(FIRST + SECOND)
    assert_uploaded(collection.collect(), SECOND, start=len(FIRST))


def test_new_host_identity_does_not_inherit_old_host_acknowledgements(collection: CollectionFixture) -> None:
    collection.enroll()
    assert_uploaded(collection.collect(), FIRST, start=0)
    previous_state = collection.state_path.read_bytes()
    previous_host = json.loads(previous_state)["host_instance_id"]
    collection.state_path.unlink()
    collection.enroll()
    assert json.loads(collection.state_path.read_text())["host_instance_id"] != previous_host
    assert_uploaded(collection.collect(), FIRST, start=0)
    assert collection.collect() == []
    collection.state_path.write_bytes(previous_state)
    assert collection.collect() == []


def test_claude_desktop_does_not_share_claude_upload_acknowledgements(collection: CollectionFixture) -> None:
    collection.plugin = collection.root / "hub/dist/claude/pig"
    collection.env["PLUGIN_ROOT"] = str(collection.plugin)
    collection.enroll("claude")
    assert_uploaded(collection.collect("claude"), FIRST, start=0, source="claude")
    # Desktop reuses Claude enrollment, but its upload source is independent.
    assert_uploaded(collection.collect("claude-desktop"), FIRST, start=0, source="claude-desktop")
    assert collection.collect("claude") == []
    assert collection.collect("claude-desktop") == []


def test_cursor_reuses_durable_capture_with_independent_destination_uploads(collection: CollectionFixture) -> None:
    collection.plugin = collection.root / "hub/dist/cursor/pig"
    collection.env["PLUGIN_ROOT"] = str(collection.plugin)
    native = collection.root / "state.vscdb"
    collection.env["PROMPTLESS_CURSOR_DATABASE"] = str(native)
    with sqlite3.connect(native) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        connection.executemany(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            [
                ("composerData:session", json.dumps({"fullConversationHeadersOnly": [{"bubbleId": "b"}]})),
                ("bubbleId:session:b", json.dumps({"type": 1, "text": "captured once"})),
            ],
        )
    collection.enroll("cursor")
    args = ["collect", "--host", "cursor", "--lifecycle", "stop", "--quiet"]
    context: dict[str, JsonValue] = {"conversation_id": "session", "generation_id": "generation"}
    _run_collect(collection.plugin, args, collection.env, context)
    journal_paths = list((collection.root / "cursor/journals").glob("*.jsonl"))
    assert len(journal_paths) == 1
    journal = journal_paths[0]
    captured = journal.read_bytes()
    assert b'"text":"captured once"' in captured
    assert_uploaded(collection.server.trace_batches, captured, start=0, source="cursor")

    collection.env["PROMPTLESS_WORKER_BASE_URL"] = collection.server.base_url.replace("127.0.0.1", "localhost")
    collection.enroll("cursor")
    collection.server.trace_batches.clear()
    _run_collect(collection.plugin, args, collection.env, context)
    assert_uploaded(collection.server.trace_batches, captured, start=0, source="cursor")
    assert journal.read_bytes() == captured
    assert list((collection.root / "cursor/journals").glob("*.jsonl")) == journal_paths

    collection.env["PROMPTLESS_WORKER_BASE_URL"] = collection.server.base_url
    collection.server.trace_batches.clear()
    _run_collect(collection.plugin, args, collection.env, context)
    assert collection.server.trace_batches == []
    assert journal.read_bytes() == captured
