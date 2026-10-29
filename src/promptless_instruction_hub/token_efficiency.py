"""Conservative local prose compaction and a standalone Claude hook (Python 3.9+).

This module is copied into plugins: keep it standard-library-only. Rewriting
natural language is not provably lossless; callers must explicitly opt in.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

# Deliberately omit stopwords, modal verbs, negations, quantities and qualifiers.
# Keep this small enough for reviewers to audit every transformation.
_REDUCTIONS = {
    "in order to": "to",
    "due to the fact that": "because",
    "at this point in time": "now",
    "in the event that": "if",
    "for the purpose of": "for",
    "with the exception of": "except",
    "on a daily basis": "daily",
}
# Fold ASCII phrase letters only, retaining Unicode-aware outer word boundaries.
_PHRASES = re.compile(r"\b(?ai:" + "|".join(re.escape(phrase) for phrase in _REDUCTIONS) + r")\b")
# A paragraph containing possible literals is left entirely intact. This also
# protects multiline quoted values, links, tables, HTML, paths and inline code.
_LITERAL_MARKERS = re.compile(r"[`\[\]{}<>|\\/\"'“”‘’]")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
MAX_INPUT_BYTES = 1024 * 1024

CONCISE_INSTRUCTION = (
    "Use concise prose: answer first; omit greetings, filler and repeated summaries. "
    "Preserve all requirements, reasoning needed to assess the answer, uncertainty, "
    "negations, conditions, numbers, units, and exact code, commands, paths and errors. "
    "Honor requested detail and formats. Never skip work, checks or necessary explanations to save tokens."
)


def _replace(match: re.Match[str]) -> str:
    phrase = match.group()
    replacement = _REDUCTIONS[phrase.lower()]
    if phrase.isupper():
        return replacement.upper()
    if phrase[0].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement


def compact_markdown(text: str) -> str:
    """Reduce reviewed phrases in plain paragraphs, preserving Markdown structure.

    Frontmatter, fenced/indented code and paragraphs containing possible literal
    syntax are opaque. Whitespace is never normalized. No model or network runs.
    """
    for literal in re.finditer(r""""(?:\\.|[^"\\])*"|(?<!\w)'[^']*'(?!\w)|“[^”]*”|‘[^’]*’""", text, re.DOTALL):
        if "\n" in literal.group():
            return text
    if text.count('"') % 2:
        return text
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    paragraph: list[str] = []
    fence_char = ""
    fence_length = 0
    frontmatter = bool(lines and lines[0].strip() == "---")

    def flush() -> None:
        content = "".join(paragraph)
        if not _LITERAL_MARKERS.search(content):
            content = _PHRASES.sub(_replace, content)
        output.append(content)
        paragraph.clear()

    for index, line in enumerate(lines):
        if frontmatter:
            output.append(line)
            if index > 0 and line.strip() in {"---", "..."}:
                frontmatter = False
            continue
        fence = _FENCE.match(line)
        if fence_char:
            output.append(line)
            if (
                fence
                and fence[1][0] == fence_char
                and len(fence[1]) >= fence_length
                and not line[fence.end() :].strip()
            ):
                fence_char = ""
            continue
        if fence:
            flush()
            fence_char, fence_length = fence[1][0], len(fence[1])
            output.append(line)
        elif not line.strip() or line.startswith(("    ", "\t", "#")):
            flush()
            output.append(line)
        else:
            paragraph.append(line)
    flush()
    return "".join(output)


def hook_response(payload: Any, *, concise: bool, compact: bool) -> dict[str, object] | None:
    """Use only host fields that change context or Agent/Task input, never permissions."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    if concise and event in ("SessionStart", "SubagentStart"):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": CONCISE_INSTRUCTION}}
    if not compact or event != "PreToolUse" or payload.get("tool_name") not in ("Agent", "Task"):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("prompt"), str):
        return None
    prompt = tool_input["prompt"]
    compressed = compact_markdown(prompt)
    if compressed == prompt:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**tool_input, "prompt": compressed},
        }
    }


def main() -> int:
    """Fail open without echoing prompt data; a disabled hook emits no context."""
    if os.environ.get("PIG_TOKEN_EFFICIENT_MODE", "").lower() == "off":
        return 0
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            print("PIG token efficiency: oversized hook input; leaving it unchanged", file=sys.stderr)
            return 0
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        print("PIG token efficiency: invalid hook input; leaving it unchanged", file=sys.stderr)
        return 0
    response = hook_response(payload, concise="--concise" in sys.argv, compact="--compact" in sys.argv)
    if response is not None:
        print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
