"""Opt-in generated skill compaction and Claude hooks in the canonical PIG plugin."""

from __future__ import annotations

import shutil
from pathlib import Path

from promptless_instruction_hub import token_efficiency
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, read_json_mapping, write_json
from promptless_instruction_hub.models import PIG_PLUGIN_ID, Harness, HubConfig, PluginDefinition


def render_token_efficiency(target_root: Path, target: Harness, config: HubConfig, plugin: PluginDefinition) -> None:
    mode = config.token_efficiency
    if mode.compact_instructions:
        # Only skill entrypoints. Resources, scripts, native rules and metadata
        # remain authored bytes, and external plugin directories never reach here.
        for directory in sorted((target_root / "skills").glob("*")):
            if directory.is_dir():
                for path in sorted(directory.iterdir()):
                    if path.is_file() and path.name.lower() == "skill.md":
                        content = path.read_bytes().decode("utf-8")
                        path.write_bytes(token_efficiency.compact_markdown(content).encode("utf-8"))
    if target != "claude" or plugin.id != PIG_PLUGIN_ID or not (mode.concise_output or mode.compact_instructions):
        return
    hook_root = target_root / "hooks"
    hook_root.mkdir(parents=True, exist_ok=True)
    runner = hook_root / "pig-token-efficiency.py"
    if runner.exists():
        raise InstructionHubError(f"token efficiency hook conflicts with authored file: {runner}")
    shutil.copy2(token_efficiency.__file__, runner)
    path = hook_root / "hooks.json"
    content = read_json_mapping(path) if path.exists() else {}
    events = content.setdefault("hooks", {})
    if not isinstance(events, dict):
        raise InstructionHubError(f"{path}: hooks must be an object")
    command = 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/pig-token-efficiency.py"'
    bindings = []
    if mode.concise_output:
        bindings.extend([("SessionStart", None, "--concise"), ("SubagentStart", None, "--concise")])
    if mode.compact_instructions:
        bindings.append(("PreToolUse", "Agent|Task", "--compact"))
    for event, matcher, flag in bindings:
        handlers = events.setdefault(event, [])
        if not isinstance(handlers, list):
            raise InstructionHubError(f"{path}: {event} must be an array")
        entry: dict[str, JsonValue] = {"hooks": [{"type": "command", "command": f"{command} {flag}", "timeout": 5}]}
        if matcher:
            entry["matcher"] = matcher
        handlers.append(entry)
    write_json(path, content)
