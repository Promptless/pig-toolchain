"""Measure local SKILL.md savings with an explicitly named tokenizer.

Run: uv run --with tiktoken python scripts/measure_token_efficiency.py HUB
No model calls, source writes, or session/trace reads.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
from pathlib import Path

from promptless_instruction_hub.token_efficiency import CONCISE_INSTRUCTION, compact_markdown


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hub", type=Path)
    parser.add_argument("--encoding", default="o200k_base")
    parser.add_argument("--diff", action="store_true")
    args = parser.parse_args()
    try:
        tiktoken = importlib.import_module("tiktoken")
    except ModuleNotFoundError:
        parser.error("install the measurement-only dependency with uv run --with tiktoken")
    encoding = getattr(tiktoken, "get_encoding")(args.encoding)
    files = sorted((args.hub / "assets/skills").glob("*/SKILL.md"))
    if not files:
        parser.error("no assets/skills/*/SKILL.md files found")
    rows = []
    for path in files:
        original = path.read_bytes().decode("utf-8")
        compacted = compact_markdown(original)
        rows.append(
            {
                "file": path.relative_to(args.hub).as_posix(),
                "before": len(encoding.encode(original, disallowed_special=())),
                "after": len(encoding.encode(compacted, disallowed_special=())),
                "changed": original != compacted,
            }
        )
        if args.diff and original != compacted:
            print(
                "".join(
                    difflib.unified_diff(original.splitlines(True), compacted.splitlines(True), str(path), "compacted")
                )
            )
    print(
        json.dumps(
            {
                "encoding": args.encoding,
                "scope": "authored skill entrypoints, counted once each; not whole-session usage or provider billing",
                "before": sum(row["before"] for row in rows),
                "after": sum(row["after"] for row in rows),
                "concise_hook_context_tokens_per_activation": len(encoding.encode(CONCISE_INSTRUCTION)),
                "files": rows,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
