"""Non-mutating, exact diff preview for the same compactor used by the hooks."""

from __future__ import annotations

import difflib
from pathlib import Path

from promptless_instruction_hub.token_efficiency import compact_markdown


def preview_file(path: Path) -> str:
    original = path.read_bytes().decode("utf-8")
    compacted = compact_markdown(original)
    before, after = len(original.encode("utf-8")), len(compacted.encode("utf-8"))
    summary = f"{path}: {before} -> {after} UTF-8 bytes ({before - after} saved; not a token estimate)\n"
    return summary + "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            compacted.splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} (compacted)",
        )
    )
