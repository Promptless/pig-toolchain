"""Bounded protobuf reader for Cursor's saved tool results.

Field numbers are qualified against Cursor desktop 3.19.19. Unknown fields
are skipped, never exported. This module does not load application code.
"""

from __future__ import annotations

import math
import struct
from typing import Union

from .contracts import JsonValue

WireValue = Union[bytes, int]
MAX_BLOB = 8 * 1024 * 1024
MAX_FIELDS = 100_000


def fields(data: bytes) -> dict[int, list[WireValue]]:
    """Decode a bounded wire message, rejecting incomplete or malformed input."""
    if len(data) > MAX_BLOB:
        raise ValueError("cursor_blob_size_limit")
    result: dict[int, list[WireValue]] = {}
    pos = 0
    count = 0

    def varint() -> int:
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError("cursor_incomplete_varint")
            byte = data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("cursor_invalid_varint")

    while pos < len(data):
        count += 1
        if count > MAX_FIELDS:
            raise ValueError("cursor_field_limit")
        tag = varint()
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("cursor_invalid_field")
        if wire_type == 0:
            value: WireValue = varint()
        else:
            if wire_type == 2:
                length = varint()
            elif wire_type in (1, 5):
                length = 8 if wire_type == 1 else 4
            else:
                raise ValueError("cursor_unsupported_wire_type")
            if length > len(data) - pos:
                raise ValueError("cursor_incomplete_field")
            value = data[pos : pos + length]
            pos += length
        result.setdefault(number, []).append(value)
    return result


def blob(message: dict[int, list[WireValue]], number: int) -> bytes:
    """Return a length-delimited field, or empty bytes when absent."""
    values = message.get(number, [])
    return values[-1] if values and isinstance(values[-1], bytes) else b""


def text_field(message: dict[int, list[WireValue]], number: int) -> str | None:
    """Read a present UTF-8 field without treating absence as empty output."""
    return blob(message, number).decode("utf-8") if number in message else None


# Schema entries: number -> (exported name, scalar or nested schema).
# Repeated messages remain arrays, including map entries, preserving native order.
SCHEMAS: dict[str, dict[int, tuple[str, str]]] = {
    "error": {1: ("message", "text"), 2: ("reason", "text")},
    "shell": {
        1: ("command", "text"),
        2: ("working_directory", "text"),
        3: ("exit_code", "int"),
        4: ("signal", "text"),
        5: ("stdout", "text"),
        6: ("stderr", "text"),
        7: ("execution_time_ms", "int"),
        8: ("output_location", "text"),
        10: ("interleaved_output", "text"),
        15: ("output_head", "text"),
        16: ("output_tail", "text"),
        17: ("elided_chars", "int"),
    },
    "shell_failure": {
        1: ("command", "text"),
        2: ("working_directory", "text"),
        3: ("exit_code", "int"),
        5: ("stdout", "text"),
        6: ("stderr", "text"),
        9: ("interleaved_output", "text"),
        11: ("aborted", "bool"),
        13: ("output_head", "text"),
        14: ("output_tail", "text"),
        15: ("elided_chars", "int"),
    },
    "read_legacy": {
        1: ("path", "text"),
        2: ("content", "text"),
        3: ("total_lines", "int"),
        4: ("file_size", "int"),
        6: ("truncated", "bool"),
        7: ("output_blob_id", "ref"),
    },
    "read": {
        1: ("content", "text"),
        2: ("is_empty", "bool"),
        3: ("exceeded_limit", "bool"),
        4: ("total_lines", "int"),
        5: ("file_size", "int"),
        7: ("path", "text"),
        9: ("data_blob_id", "ref"),
        10: ("content_blob_id", "ref"),
    },
    "edit": {
        1: ("path", "text"),
        3: ("lines_added", "int"),
        4: ("lines_removed", "int"),
        5: ("diff", "text"),
        6: ("before", "text"),
        7: ("after", "text"),
        8: ("message", "text"),
    },
    "grep": {
        1: ("pattern", "text"),
        2: ("path", "text"),
        3: ("output_mode", "text"),
        4: ("workspaces", "[]grep_entry"),
        5: ("active_editor", "grep_union"),
    },
    "grep_entry": {1: ("workspace", "text"), 2: ("result", "grep_union")},
    "grep_union": {1: ("count", "grep_count"), 2: ("files", "grep_files"), 3: ("content", "grep_content")},
    "grep_count": {1: ("counts", "[]grep_file_count"), 2: ("total_files", "int"), 3: ("total_matches", "int")},
    "grep_file_count": {1: ("file", "text"), 2: ("count", "int")},
    "grep_files": {1: ("files", "[]text"), 2: ("total_files", "int")},
    "grep_content": {
        1: ("matches", "[]grep_file"),
        2: ("total_lines", "int"),
        3: ("total_matched_lines", "int"),
        4: ("client_truncated", "bool"),
        5: ("ripgrep_truncated", "bool"),
    },
    "grep_file": {1: ("file", "text"), 2: ("matches", "[]grep_match")},
    "grep_match": {
        1: ("line_number", "int"),
        2: ("content", "text"),
        3: ("content_truncated", "bool"),
        4: ("is_context_line", "bool"),
    },
    "mcp": {1: ("content", "[]mcp_content"), 2: ("is_error", "bool"), 3: ("structured_content", "struct")},
    "mcp_content": {1: ("text", "mcp_text"), 2: ("image", "omitted")},
    "mcp_text": {1: ("text", "text")},
    "web": {1: ("references", "[]web_reference")},
    "web_reference": {1: ("title", "text"), 2: ("url", "text"), 3: ("chunk", "text")},
    "task": {
        1: ("steps", "[]step"),
        2: ("agent_id", "text"),
        3: ("is_background", "bool"),
        4: ("duration_ms", "int"),
        5: ("result_suffix", "text"),
    },
    "assistant": {1: ("text", "text"), 2: ("started_at_ms", "int"), 3: ("completed_at_ms", "int")},
    "thinking": {1: ("text", "text"), 2: ("duration_ms", "int")},
    "delete": {1: ("path", "text"), 2: ("deleted_file", "text"), 3: ("file_size", "int"), 4: ("before", "text")},
    "web_fetch": {1: ("url", "text"), 2: ("markdown", "text"), 3: ("output_location", "text")},
}


def _structured(data: bytes, depth: int) -> dict[str, JsonValue]:
    """Decode google.protobuf.Struct used by saved MCP structured results."""
    if depth > 12:
        raise ValueError("cursor_depth_limit")
    result: dict[str, JsonValue] = {}
    for entry in fields(data).get(1, []):
        if isinstance(entry, bytes):
            pair = fields(entry)
            key = text_field(pair, 1)
            if key is not None:
                result[key] = _structured_value(blob(pair, 2), depth + 1)
    return result


def _structured_value(data: bytes, depth: int) -> JsonValue:
    """Decode the bounded JSON value union without loading Cursor application code."""
    if depth > 12:
        raise ValueError("cursor_depth_limit")
    message = fields(data)
    if 2 in message:
        number = blob(message, 2)
        if len(number) != 8:
            raise ValueError("cursor_invalid_number")
        value = struct.unpack("<d", number)[0]
        if not math.isfinite(value):
            raise ValueError("cursor_nonfinite_number")
        return value
    if 3 in message:
        return text_field(message, 3)
    if 4 in message:
        return bool(message[4][-1])
    if 5 in message:
        return _structured(blob(message, 5), depth + 1)
    if 6 in message:
        return [
            _structured_value(item, depth + 1)
            for item in fields(blob(message, 6)).get(1, [])
            if isinstance(item, bytes)
        ]
    return None


def _step(data: bytes, depth: int) -> dict[str, JsonValue]:
    """Retain saved subagent messages and nested results without recounting calls."""
    message = fields(data)
    if 1 in message:
        return {"assistant": decode(blob(message, 1), "assistant", depth + 1)}
    if 3 in message:
        return {"thinking": decode(blob(message, 3), "thinking", depth + 1)}
    if 2 in message:
        identity, result, completeness = tool_result(blob(message, 2), depth + 1)
        return {"call_id": identity, "result": result, "completeness": completeness}
    return {"completeness": "unsupported"}


def decode(data: bytes, schema: str, depth: int = 0) -> dict[str, JsonValue]:
    """Decode only schema-listed fields, with a recursion limit."""
    if depth > 12:
        raise ValueError("cursor_depth_limit")
    result: dict[str, JsonValue] = {}
    for number, values in fields(data).items():
        entry = SCHEMAS[schema].get(number)
        if entry is None:
            continue
        name, kind = entry
        repeated = kind.startswith("[]")
        kind = kind.removeprefix("[]")
        decoded: list[JsonValue] = []
        for value in values:
            if kind in ("int", "bool") and isinstance(value, int):
                decoded.append(bool(value) if kind == "bool" else value)
            elif isinstance(value, bytes):
                if kind == "text":
                    decoded.append(value.decode("utf-8"))
                elif kind == "ref":
                    decoded.append(value.hex())
                elif kind == "struct":
                    decoded.append(_structured(value, depth + 1))
                elif kind == "step":
                    decoded.append(_step(value, depth + 1))
                elif kind == "omitted":
                    decoded.append({"completeness": "unsupported", "reason": "binary_content"})
                elif kind in SCHEMAS:
                    decoded.append(decode(value, kind, depth + 1))
        if decoded:
            result[name] = decoded[0] if len(decoded) == 1 and not repeated else decoded
    return result


def tool_result(data: bytes, depth: int = 0) -> tuple[str | None, dict[str, JsonValue], str]:
    """Read ToolCall identity and allowlisted result fields; report unsupported shapes."""
    if depth > 12:
        raise ValueError("cursor_depth_limit")
    message = fields(data)
    call_id = text_field(message, 57)
    kinds = {
        1: "shell",
        3: "delete",
        5: "grep",
        8: "read",
        12: "edit",
        15: "mcp",
        18: "web",
        19: "task",
        37: "web_fetch",
    }
    for number, schema in kinds.items():
        if number not in message:
            continue
        call = fields(blob(message, number))
        result_bytes = blob(call, 2)
        if not result_bytes:
            return call_id, {}, "missing"
        variants = fields(result_bytes)
        if 1 in variants:
            success = fields(blob(variants, 1))
            if schema == "read" and isinstance(success.get(2, [None])[-1], bytes):
                schema = "read_legacy"
            result = decode(blob(variants, 1), schema, depth)
            # Presence of the success oneof distinguishes valid empty output.
            result["status"] = "success"
            completeness = "partial" if schema == "task" else "available"
            if schema == "mcp" and any(
                isinstance(item, dict) and "image" in item
                for item in result.get("content", [])
                if isinstance(result.get("content"), list)
            ):
                completeness = "partial"
            return call_id, result, completeness
        if 2 in variants:
            result = decode(blob(variants, 2), "shell_failure" if schema == "shell" else "error")
            return call_id, {"error": result}, "available"
        return call_id, {}, "unsupported"
    return call_id, {}, "unsupported"
