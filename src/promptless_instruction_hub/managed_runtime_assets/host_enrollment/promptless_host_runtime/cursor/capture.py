"""Capture Cursor observations into durable, append-only upload journals."""

from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path

from ..contracts import HookTraceContext, JsonValue, LifecycleEvent
from .database import ADAPTER_VERSION, mapping, read_session, string
from ..storage import _atomic_write_text, _ledger_path, _try_lock_state_file, _unlock_state_file

MAX_RECORD = 2 * 1024 * 1024
MAX_JOURNAL = 128 * 1024 * 1024
MAX_SPOOL = 1024 * 1024 * 1024
MAX_JOURNALS = 4096
SESSION_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,200}$")


@dataclass
class CursorExport:
    """Prepared upload context and whether background export needs another pass."""

    context: HookTraceContext
    complete: bool


def spool_root() -> Path:
    """Keep collector-owned data beside the host upload ledger."""
    return _ledger_path().parent / "cursor"


def transcript_glob() -> str:
    """Discover only Cursor's exported desktop Agent transcripts."""
    return str(Path.home() / ".cursor/projects/*/agent-transcripts/**/*.jsonl")


def _digest(value: JsonValue) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fallback(path: Path | None) -> list[dict[str, JsonValue]]:
    if path is None or not path.is_file():
        return []
    records: list[dict[str, JsonValue]] = []
    with path.open("rb") as handle:
        for index in range(4000):
            line = handle.readline(MAX_RECORD + 1)
            if not line:
                break
            if len(line) > MAX_RECORD or not line.endswith(b"\n"):
                break
            row = mapping(json.loads(line))
            content = mapping(row.get("message")).get("content")
            if not isinstance(content, list):
                continue
            for block_index, block_value in enumerate(content):
                block = mapping(block_value)
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    event: dict[str, JsonValue] = {
                        "kind": "user_message" if row.get("role") == "user" else "assistant_message",
                        "text": block["text"],
                    }
                elif block.get("type") == "tool_use":
                    event = {
                        "kind": "session_event",
                        "name": "cursor_transcript_tool_call",
                        "data": {"tool": block.get("name"), "input": block.get("input")},
                    }
                else:
                    continue
                records.append(
                    {
                        "event_id": f"transcript:{index}:{block_index}",
                        "event": event,
                        "capture": {"source": "transcript", "completeness": "partial"},
                    }
                )
    return records


def _semantic(event: JsonValue) -> str:
    return _digest({key: value for key, value in mapping(event).items() if key not in ("call_id", "t")})


def append_observations(session_id: str, observations: list[dict[str, JsonValue]]) -> Path:
    """Append changed native events, retaining identity and every accepted revision.

    The journal is the recovery source of truth. A torn last write is removed
    before appending; complete lines are never rewritten. Callers hold the
    collector lock, and the uploader acknowledges only complete line ranges.
    """
    if not SESSION_PATTERN.fullmatch(session_id):
        raise ValueError("cursor_invalid_session_id")
    path = spool_root() / "journals" / (session_id + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    journals = list(path.parent.glob("*.jsonl"))
    if len(journals) >= MAX_JOURNALS and not path.exists():
        raise ValueError("cursor_journal_count_limit")
    spool_bytes = sum(entry.stat().st_size for entry in journals)
    latest: dict[str, dict[str, JsonValue]] = {}
    aliases: dict[str, str] = {}
    fallback: dict[str, list[str]] = {}
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        handle.seek(0, os.SEEK_END)
        if handle.tell() > MAX_JOURNAL:
            raise ValueError("cursor_journal_size_limit")
        handle.seek(0)
        while True:
            offset = handle.tell()
            line = handle.readline(MAX_RECORD + 1)
            if not line:
                break
            if not line.endswith(b"\n"):
                handle.truncate(offset)
                break
            row = mapping(json.loads(line))
            identity = string(row.get("event_id"))
            if identity:
                latest[identity] = row
                native_id = string(row.get("native_event_id"))
                if native_id:
                    aliases[native_id] = identity
        for identity, row in latest.items():
            if mapping(row.get("capture")).get("source") == "transcript":
                fallback.setdefault(_semantic(row.get("event")), []).append(identity)
        has_database = any(mapping(row.get("capture")).get("source") == "database" for row in latest.values())
        handle.seek(0, os.SEEK_END)
        for observation in observations:
            if mapping(observation.get("capture")).get("source") == "transcript" and has_database:
                # Do not create a second identity space after database capture.
                # A later successful snapshot will recover new native events.
                continue
            native_id = string(observation.get("event_id"))
            if not native_id:
                continue
            identity = aliases.get(native_id, native_id)
            if identity not in latest and mapping(observation.get("capture")).get("source") == "database":
                matches = fallback.get(_semantic(observation.get("event")), [])
                if matches:
                    identity = matches.pop(0)
            previous = latest.get(identity, {})
            if mapping(previous.get("capture")).get("completeness") == "available" and mapping(
                observation.get("capture")
            ).get("completeness") in ("missing", "pruned", "partial", "unsupported", "size_limit"):
                # Cursor pruning must not replace a result we already retained.
                continue
            content = {**observation, "event_id": identity, "native_event_id": native_id}
            fingerprint = _digest(content)
            if previous.get("content_sha256") == fingerprint:
                continue
            record: dict[str, JsonValue] = {
                **content,
                "format": "promptless.cursor",
                "version": 1,
                "adapter": ADAPTER_VERSION,
                "session_id": session_id,
                "content_sha256": fingerprint,
                "revision": int(str(previous.get("revision", 0))) + 1,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            encoded = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
            if len(encoded) > MAX_RECORD:
                # Preserve event kind so tool-call counts remain stable on revision.
                event = dict(mapping(record["event"]))
                for key in ("input", "output", "text", "data"):
                    if key in event:
                        event[key] = "" if key == "text" else {} if key == "data" else None
                record["event"] = event
                record["capture"] = {**mapping(record.get("capture")), "completeness": "size_limit"}
                encoded = (json.dumps(record, separators=(",", ":")) + "\n").encode()
                if len(encoded) > MAX_RECORD:
                    raise ValueError("cursor_record_metadata_size_limit")
            if spool_bytes + len(encoded) > MAX_SPOOL:
                raise ValueError("cursor_spool_size_limit")
            if handle.tell() + len(encoded) > MAX_JOURNAL:
                raise ValueError("cursor_journal_size_limit")
            handle.write(encoded)
            spool_bytes += len(encoded)
            latest[identity] = record
            aliases[native_id] = identity
        handle.flush()
        os.fsync(handle.fileno())
    return path


def prepare_journals(context: HookTraceContext, lifecycle: LifecycleEvent) -> CursorExport:
    """Serialize journal writes, including explicit manual collect invocations."""
    root = spool_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "export.lock").open("a+b") as lock:
        if not _try_lock_state_file(lock):
            return CursorExport(replace(context, transcript_path=None, agent_transcript_path=None), False)
        try:
            return _prepare_journals(context, lifecycle)
        finally:
            _unlock_state_file(lock)


def _prepare_journals(context: HookTraceContext, lifecycle: LifecycleEvent) -> CursorExport:
    """Export the notified session first, then bounded transcript and subagent catch-up."""
    subject_path = context.agent_transcript_path if lifecycle == "subagent_stop" else context.transcript_path
    subject = (subject_path.stem if subject_path else None) or (
        context.agent_id if lifecycle == "subagent_stop" else context.session_id
    )
    if subject and not SESSION_PATTERN.fullmatch(subject):
        subject = None
    state_path = spool_root() / "scan-state.json"
    state = mapping(json.loads(state_path.read_text())) if state_path.exists() else {}
    offsets = mapping(state.get("offsets"))
    retry_offsets = mapping(state.get("retry_offsets"))
    saved_pending = state.get("pending")
    saved_completed = state.get("completed")
    resumed = [key for key in saved_pending if isinstance(key, str)] if isinstance(saved_pending, list) else []
    completed = {key for key in saved_completed if isinstance(key, str)} if isinstance(saved_completed, list) else set()
    after = string(state.get("after")) or ""
    discovered: dict[str, Path] = {}
    discovery_deadline = time.monotonic() + 1
    discovery_truncated = False
    for name in glob.iglob(transcript_glob(), recursive=True):
        path = Path(name)
        if SESSION_PATTERN.fullmatch(path.stem) and path.stem != subject:
            discovered[path.stem] = path
        if len(discovered) >= 10000 or time.monotonic() >= discovery_deadline:
            discovery_truncated = True
            break
    ordered = sorted(discovered)
    ordered = [key for key in ordered if key > after] + [key for key in ordered if key <= after]
    pending: list[tuple[str, Path | None]] = [(subject, subject_path)] if subject else []
    scheduled = {subject} if subject else set()
    for key in [*resumed, *ordered[:16]]:
        if key not in scheduled:
            pending.append((key, discovered.get(key)))
            scheduled.add(key)
    seen: set[str] = set()
    retry: list[str] = []
    deadline = time.monotonic() + 5
    current: Path | None = None
    errors: dict[str, int] = {}
    while pending and time.monotonic() < deadline and len(seen) < 32:
        session_id, transcript = pending.pop(0)
        if session_id in seen or not SESSION_PATTERN.fullmatch(session_id):
            continue
        seen.add(session_id)
        page_complete = False
        try:
            offset = offsets.get(session_id, 0)
            retry_offset = retry_offsets.get(session_id)
            page = read_session(
                session_id,
                deadline=min(deadline, time.monotonic() + 0.5),
                offset=offset if isinstance(offset, int) else 0,
                retry_offset=retry_offset if isinstance(retry_offset, int) else None,
            )
            observations = page.records
            for child in page.children:
                if child not in scheduled and child not in completed:
                    pending.append((child, discovered.get(child)))
                    scheduled.add(child)
            next_offset = page.next_offset
            page_complete = page.complete
        except (sqlite3.Error, OSError, ValueError) as exc:
            reason = "database_busy" if isinstance(exc, sqlite3.OperationalError) else "decode_or_budget"
            errors[reason] = errors.get(reason, 0) + 1
            observations = []
            next_offset = None
        if not observations:
            try:
                observations = _fallback(transcript)
            except (OSError, ValueError):
                errors["transcript_unreadable"] = errors.get("transcript_unreadable", 0) + 1
        if session_id == subject and lifecycle and next_offset == 0 and page_complete:
            observations.append(
                {
                    "event_id": f"lifecycle:{context.generation_id or 'unknown'}:{lifecycle}",
                    "generation_id": context.generation_id,
                    "parent_session_id": context.session_id
                    if lifecycle == "subagent_stop"
                    else context.parent_session_id,
                    "agent_id": subject if lifecycle == "subagent_stop" else context.agent_id,
                    "event": {"kind": "session_event", "name": lifecycle, "data": {}},
                    "capture": {"source": "hook", "completeness": "available"},
                }
            )
        if observations:
            path = append_observations(session_id, observations)
            if session_id == subject:
                current = path
        if next_offset is not None:
            offsets[session_id] = next_offset
            if page.retry_offset is None:
                retry_offsets.pop(session_id, None)
            else:
                retry_offsets[session_id] = page.retry_offset
        if session_id != subject:
            after = session_id
        if page_complete:
            completed.add(session_id)
        else:
            completed.discard(session_id)
            retry.append(session_id)
    remaining = [session_id for session_id, _ in pending] + retry
    # Save traversal progress only after journal fsyncs. Crash replay is idempotent.
    _atomic_write_text(
        state_path,
        json.dumps(
            {
                "offsets": offsets,
                "retry_offsets": retry_offsets,
                "after": after,
                "pending": remaining,
                "completed": sorted(completed) if pending else [],
            }
        ),
    )
    _atomic_write_text(
        spool_root() / "diagnostics.json",
        json.dumps(
            {
                "adapter": ADAPTER_VERSION,
                "sessions_observed": len(seen),
                "errors": errors,
                "pending_sessions": len(remaining),
                "discovery_truncated": discovery_truncated,
                "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        ),
    )
    return CursorExport(
        replace(context, transcript_path=current, agent_transcript_path=None, session_id=subject),
        not remaining,
    )
