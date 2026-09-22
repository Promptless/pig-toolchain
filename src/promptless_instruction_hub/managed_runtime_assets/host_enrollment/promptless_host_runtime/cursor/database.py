"""Read-only, allowlisted snapshots of Cursor desktop session storage."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..contracts import JsonValue
from .wire import MAX_BLOB, blob, fields, text_field, tool_result

ADAPTER_VERSION = "cursor-desktop-3.19-v1"
MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 4000
PAGE_SIZE = 128


@dataclass
class SessionPage:
    """A resumable page; offsets advance only after its journal is durable."""

    records: list[dict[str, JsonValue]]
    children: list[str]
    next_offset: int
    complete: bool = True


def database_path() -> Path:
    """Resolve the standard desktop database or an explicit operator override."""
    override = os.environ.get("PROMPTLESS_CURSOR_DATABASE")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "Cursor/User/globalStorage/state.vscdb"


def mapping(value: object) -> dict[str, JsonValue]:
    """Accept JSON objects at the native storage boundary."""
    return cast("dict[str, JsonValue]", value) if isinstance(value, dict) else {}


def string(value: object) -> str | None:
    """Accept nonempty native strings."""
    return value if isinstance(value, str) and value else None


def json_value(value: JsonValue) -> JsonValue:
    """Decode Cursor's JSON-in-string tool fields without broad row serialization."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class CursorSnapshot:
    """Exact-key reads under one short transaction with byte and time budgets."""

    def __init__(self, connection: sqlite3.Connection, deadline: float) -> None:
        self.connection = connection
        self.deadline = deadline
        self.bytes_read = 0

    def read(self, key: str) -> bytes | None:
        """Read one indexed key, checking size before SQLite materializes the value."""
        if time.monotonic() >= self.deadline or self.bytes_read >= MAX_SESSION_BYTES:
            raise ValueError("cursor_snapshot_budget")
        size = self.connection.execute("SELECT length(value) FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
        if size is None or size[0] is None:
            return None
        if size[0] > MAX_BLOB:
            raise ValueError("cursor_blob_size_limit")
        if self.bytes_read + size[0] > MAX_SESSION_BYTES:
            raise ValueError("cursor_snapshot_size_limit")
        row = self.connection.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        value = row[0].encode() if isinstance(row[0], str) else bytes(row[0])
        self.bytes_read += len(value)
        return value

    def object(self, key: str) -> dict[str, JsonValue]:
        """Decode one native JSON object."""
        value = self.read(key)
        return mapping(json.loads(value)) if value else {}

    def referenced_blob(self, reference: bytes) -> bytes:
        """Follow only bounded native blob references, never paths from a trace."""
        if not reference or len(reference) > 64:
            return b""
        value = self.read("agentKv:blob:" + reference.hex()) or b""
        if value == b"\x00":
            return b""
        # Older stores used a hex string instead of a BLOB.
        if value and len(value) % 2 == 0 and all(byte in b"0123456789abcdefABCDEF" for byte in value):
            return bytes.fromhex(value.decode("ascii"))
        return value


def _canonical_tools(
    snapshot: CursorSnapshot, composer: dict[str, JsonValue], wanted: set[str], result: dict[str, bytes]
) -> None:
    encoded = string(composer.get("conversationState"))
    if not encoded:
        return
    state = fields(base64.b64decode(encoded, validate=True))
    for reference in reversed(state.get(8, [])[-MAX_EVENTS:]):
        if not isinstance(reference, bytes):
            continue
        turn_bytes = snapshot.referenced_blob(reference)
        if not turn_bytes:
            continue
        turn = fields(blob(fields(turn_bytes), 1))
        for step_ref in turn.get(2, [])[:MAX_EVENTS]:
            if not isinstance(step_ref, bytes):
                continue
            step_bytes = snapshot.referenced_blob(step_ref)
            if not step_bytes:
                continue
            call = blob(fields(step_bytes), 2)
            if call:
                call_id = text_field(fields(call), 57)
                if call_id in wanted:
                    result[call_id] = call
            if len(result) == len(wanted):
                return


def read_session(session_id: str, *, deadline: float, offset: int = 0) -> SessionPage:
    """Snapshot saved events and child IDs without writing or waiting on Cursor."""
    uri = database_path().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0)) as connection:
        connection.execute("PRAGMA query_only = ON")
        # Rollback-journal readers can delay an editor commit. Refuse that mode
        # instead of taking a long-lived shared lock; WAL readers permit writers.
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            raise ValueError("cursor_database_requires_wal")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute("BEGIN")
        snapshot = CursorSnapshot(connection, deadline)
        composer = snapshot.object("composerData:" + session_id)
        if not composer:
            raise ValueError("cursor_session_not_saved")
        subagent = mapping(composer.get("subagentInfo"))
        metadata: dict[str, JsonValue] = {
            "parent_session_id": string(subagent.get("parentComposerId")),
            "agent_id": session_id if subagent else None,
            "model": string(mapping(composer.get("modelConfig")).get("modelName")),
        }
        headers = composer.get("fullConversationHeadersOnly")
        if not isinstance(headers, list):
            raise ValueError("cursor_unsupported_headers")
        records: list[dict[str, JsonValue]] = []
        bubbles: list[tuple[str, dict[str, JsonValue]]] = []
        next_offset = 0
        complete = True
        if isinstance(headers, list):
            start = offset if 0 <= offset < len(headers) else 0
            next_offset = start
            for header in headers[start : start + PAGE_SIZE]:
                bubble_id = string(mapping(header).get("bubbleId"))
                if not bubble_id:
                    next_offset += 1
                    continue
                try:
                    bubble = snapshot.object(f"bubbleId:{session_id}:{bubble_id}")
                except (ValueError, sqlite3.OperationalError) as exc:
                    if str(exc) == "cursor_blob_size_limit":
                        records.append(
                            {
                                "event_id": bubble_id + ":capture_gap",
                                **metadata,
                                "event": {
                                    "kind": "session_event",
                                    "name": "cursor_capture_gap",
                                    "data": {"reason": "size_limit"},
                                },
                                "capture": {"source": "database", "completeness": "size_limit"},
                            }
                        )
                        next_offset += 1
                        continue
                    # Keep completed reads and retry this header on the next pass.
                    complete = False
                    break
                if not bubble:
                    records.append(
                        {
                            "event_id": bubble_id + ":capture_gap",
                            **metadata,
                            "event": {
                                "kind": "session_event",
                                "name": "cursor_capture_gap",
                                "data": {"reason": "missing_bubble"},
                            },
                            "capture": {"source": "database", "completeness": "missing"},
                        }
                    )
                    complete = False
                bubbles.append((bubble_id, bubble))
                next_offset += 1
            if next_offset >= len(headers):
                next_offset = 0
        # UI toolCallBinary is a saved copy. Follow canonical references only
        # for calls whose UI copy is missing, reserving budget for event export.
        wanted = {
            call_id
            for _, bubble in bubbles
            if not mapping(bubble.get("toolFormerData")).get("toolCallBinary")
            if (call_id := string(mapping(bubble.get("toolFormerData")).get("toolCallId")))
        }
        canonical: dict[str, bytes] = {}
        if wanted:
            snapshot.deadline = min(deadline, time.monotonic() + 0.1)
            try:
                _canonical_tools(snapshot, composer, wanted, canonical)
            except (ValueError, sqlite3.OperationalError):
                # Per-event completeness records missing canonical results.
                pass
            finally:
                snapshot.deadline = deadline
        for bubble_id, bubble in bubbles:
            try:
                records.extend(_bubble_events(snapshot, bubble_id, bubble, canonical, metadata))
            except (ValueError, sqlite3.OperationalError):
                records.append(
                    {
                        "event_id": bubble_id + ":capture_gap",
                        **metadata,
                        "event": {
                            "kind": "session_event",
                            "name": "cursor_capture_gap",
                            "data": {"reason": "unsupported_or_budget_limited"},
                        },
                        "capture": {"source": "database", "completeness": "partial"},
                    }
                )
        children = composer.get("subagentComposerIds")
        return SessionPage(
            records,
            [child for child in children if isinstance(child, str)][:64] if isinstance(children, list) else [],
            next_offset,
            complete and next_offset == 0,
        )


def _bubble_events(
    snapshot: CursorSnapshot,
    bubble_id: str,
    bubble: dict[str, JsonValue],
    canonical: dict[str, bytes],
    metadata: dict[str, JsonValue],
) -> list[dict[str, JsonValue]]:
    records: list[dict[str, JsonValue]] = []
    generation = string(bubble.get("requestId"))
    timestamp = string(bubble.get("createdAt"))
    if timestamp:
        try:
            parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            timestamp = parsed.isoformat() if parsed.tzinfo is not None else None
        except ValueError:
            timestamp = None

    def add(identity: str, event: dict[str, JsonValue], completeness: str = "available") -> None:
        if timestamp:
            event["t"] = timestamp
        records.append(
            {
                "event_id": identity,
                "event": event,
                "generation_id": generation,
                "capture": {"source": "database", "completeness": completeness},
                **metadata,
            }
        )

    text = string(bubble.get("text"))
    if text:
        add(
            bubble_id + ":text",
            {"kind": "user_message" if bubble.get("type") == 1 else "assistant_message", "text": text},
        )
    thinking = string(bubble.get("thinking"))
    if thinking:
        add(bubble_id + ":thinking", {"kind": "reasoning", "text": thinking})
    tool = mapping(bubble.get("toolFormerData"))
    if not tool:
        return records
    name = string(tool.get("name")) or "unknown"
    call_id = string(tool.get("toolCallId")) or bubble_id
    add(
        call_id + ":call",
        {
            "kind": "tool_call",
            "tool": name,
            "call_id": call_id,
            "input": json_value(tool.get("params", tool.get("rawArgs"))),
        },
    )
    output: JsonValue = None
    completeness = "missing"
    binary = canonical.get(call_id)
    if binary is None and isinstance(tool.get("toolCallBinary"), str):
        binary = base64.b64decode(str(tool["toolCallBinary"]), validate=True)
    if binary:
        try:
            _, output, completeness = tool_result(binary)
        except ValueError:
            completeness = "unsupported"
    # The UI copy can retain a result pruned from the canonical blob. Export
    # tool result payloads, never additionalData, whole bubbles, or composer rows.
    result_error = isinstance(output, dict) and bool(
        output.get("error") or output.get("is_error") or output.get("exit_code")
    )
    ui_result = json_value(tool.get("result"))
    if isinstance(output, dict):
        for ref_key in ("output_blob_id", "content_blob_id", "data_blob_id"):
            reference = output.pop(ref_key, None)
            if isinstance(reference, str):
                try:
                    saved = snapshot.referenced_blob(bytes.fromhex(reference))
                except (ValueError, sqlite3.OperationalError):
                    saved = b""
                if saved and ref_key != "data_blob_id":
                    try:
                        output["content"] = saved.decode("utf-8")
                    except UnicodeDecodeError:
                        completeness = "unsupported"
                else:
                    completeness = "pruned"
        if any(output.get(key) for key in ("truncated", "exceeded_limit", "elided_chars", "output_location")):
            completeness = "pruned"
    if ui_result is not None and (not output or completeness in ("missing", "unsupported", "pruned", "partial")):
        # These are result values of an explicitly identified tool, not application metadata.
        output = {"saved_result": output, "ui_result": ui_result}
        completeness = "partial"
    status = string(tool.get("status")) or "unknown"
    if status == "cancelled":
        completeness = "interrupted"
    add(
        call_id + ":result",
        {
            "kind": "tool_result",
            "tool": name,
            "call_id": call_id,
            "is_error": result_error or status in ("error", "cancelled"),
            "output": output,
        },
        completeness,
    )
    return records
