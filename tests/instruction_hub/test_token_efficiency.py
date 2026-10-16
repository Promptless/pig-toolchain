from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from promptless_instruction_hub import token_efficiency
from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.models import TokenEfficiencyConfig
from promptless_instruction_hub.token_efficiency import compact_markdown, hook_response
from tests.config_helpers import enable_trace_ingestion


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("In order to deploy, verify approval first.\n", "To deploy, verify approval first.\n"),
        ("IN THE EVENT THAT tests fail, never deploy.", "IF tests fail, never deploy."),
        (
            "Do not delete data in order to save 12.5 GiB unless approved.",
            "Do not delete data to save 12.5 GiB unless approved.",
        ),
        (
            "Only retry in the event that status is 429; at most 3 times.",
            "Only retry if status is 429; at most 3 times.",
        ),
        ("Skip all jobs with the exception of staging.", "Skip all jobs except staging."),
        ("Keep plain prose concise.", "Keep plain prose concise."),
        ("in order today", "in order today"),
        ("İn order to ship", "İn order to ship"),
        ("for the purpoſe of testing", "for the purpoſe of testing"),
        ("éin order to ship", "éin order to ship"),
    ],
)
def test_plain_prose_and_idempotence(original: str, expected: str) -> None:
    assert compact_markdown(original) == expected
    assert compact_markdown(expected) == expected


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: x\ndescription: in order to route\n---\n",
        "---\ndescription: in order to route\n",
        "```python\nin order to\n```\n",
        "~~~~text\nin order to\n~~~\nin order to\n~~~~\n",
        "```text\nin order to\n",
        "    in order to\n\tin order to\n",
        "> in order to\n",
        "# In order to\n",
        "Use `in order to` in order to match the exact string.\n",
        'Return "in order to" exactly.\n',
        "Return 'in order to' exactly.\n",
        "Use “in order to” literally.\n",
        "[in order to](https://example.com/in-order-to)\n",
        "Open /tmp/in order to/file.\n",
        "in order to | due to the fact that\n",
        "> quoted text\nin order to continue its lazy blockquote\n",
        'Return "multiline\n\nin order to\n\nvalue" exactly.\n',
        "Return 'multiline\n\nin order to\n\nvalue' exactly.\n",
        "Return “multiline\n\nin order to\n\nvalue” exactly.\n",
        'Return "unterminated\n\nin order to\n',
        "<rule>in order to</rule>\n",
        '{"value": "in order to"}\n',
    ],
)
def test_literals_and_structure_are_opaque(text: str) -> None:
    assert compact_markdown(text) == text


def test_fences_and_frontmatter_do_not_disable_following_prose() -> None:
    original = "---\nname: x\n---\n\n```text\nin order to\n```\n\nIn order to test, run checks.\r\n"
    assert compact_markdown(original) == original.replace("In order to test", "To test")


def test_hook_preserves_every_other_argument_and_permissions() -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "prompt": "Never publish in order to bypass approval.",
            "model": "sonnet",
            "isolation": "worktree",
            "run_in_background": True,
            "unknown": {"future": [1, 2]},
        },
    }
    response = hook_response(payload, concise=False, compact=True)
    assert response == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**payload["tool_input"], "prompt": "Never publish to bypass approval."},
        }
    }
    assert "in order to" in json.dumps(payload)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"hook_event_name": []},
        {"hook_event_name": "PreToolUse", "tool_name": []},
        [],
        {},
        {"hook_event_name": "UserPromptSubmit", "prompt": "in order to"},
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"prompt": "in order to"}},
        {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": {"prompt": 42}},
        {"hook_event_name": "PreToolUse", "tool_name": "Task", "tool_input": {"prompt": "already concise"}},
    ],
)
def test_unknown_and_unchanged_inputs_emit_nothing(payload: object) -> None:
    assert hook_response(payload, concise=True, compact=True) is None


def test_switches_are_independent() -> None:
    assert hook_response({"hook_event_name": "SessionStart"}, concise=False, compact=True) is None
    assert hook_response({"hook_event_name": "SubagentStart"}, concise=True, compact=False) == {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": token_efficiency.CONCISE_INSTRUCTION,
        }
    }
    assert (
        hook_response(
            {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": {"prompt": "in order to"}},
            concise=True,
            compact=False,
        )
        is None
    )


def test_standalone_hook_invalid_input_and_emergency_disable() -> None:
    runner = Path(token_efficiency.__file__)
    for data in [b"not JSON", b"\xff", b"[" * 10000, b"a" * (token_efficiency.MAX_INPUT_BYTES + 1)]:
        result = subprocess.run([sys.executable, str(runner), "--compact"], input=data, capture_output=True, check=True)
        assert result.stdout == b""
        assert b"leaving it unchanged" in result.stderr
        assert b"not JSON" not in result.stderr
    result = subprocess.run(
        [sys.executable, str(runner), "--concise"],
        input=b'{"hook_event_name":"SessionStart"}',
        capture_output=True,
        check=True,
        env={**os.environ, "PIG_TOKEN_EFFICIENT_MODE": "off"},
    )
    assert not result.stdout and not result.stderr


def _configure(root: Path, *, concise: bool, compact: bool) -> None:
    path = root / "hub.yaml"
    config = yaml.safe_load(path.read_text())
    config["token_efficiency"] = {"concise_output": concise, "compact_instructions": compact}
    config["mcp"] = {"enabled": True}
    path.write_text(yaml.safe_dump(config))


def _skill(root: Path) -> str:
    path = root / "assets/skills/sample"
    path.mkdir(parents=True)
    content = "---\nname: sample\ndescription: Use in order to test.\n---\n\nIn order to ship, never skip checks.\n"
    (path / "SKILL.md").write_text(content)
    (path / "reference.md").write_text("in order to preserve resource bytes\n")
    (root / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [skill:sample]\n")
    (root / "plugins/team.yaml").write_text("id: team\nname: Team\nincludes: [skill:sample]\n")
    config_path = root / "hub.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["stable_plugins"].append("team")
    config_path.write_text(yaml.safe_dump(config))
    return content


def test_opt_in_build_mcp_integrity_hook_merge_and_disable(tmp_path: Path) -> None:
    init_hub(tmp_path, org="Promptless")
    original = _skill(tmp_path)
    build_hub(tmp_path)
    assert (tmp_path / "dist/claude/pig/skills/sample/SKILL.md").read_text() == original
    assert not (tmp_path / "dist/claude/pig/hooks/hooks.json").exists()
    enable_trace_ingestion(tmp_path)
    _configure(tmp_path, concise=True, compact=True)
    before = (tmp_path / "assets/skills/sample/SKILL.md").read_bytes()
    build_hub(tmp_path)
    build_hub(tmp_path, check=True)
    for target in ("claude", "codex", "gemini", "cursor"):
        for plugin in ("pig", "team"):
            root = tmp_path / "dist" / target / plugin
            assert (root / "skills/sample/SKILL.md").read_text() == original.replace("In order to ship", "To ship")
            assert (root / "skills/sample/reference.md").read_text() == "in order to preserve resource bytes\n"
            assert (root / "hooks/pig-token-efficiency.py").exists() == (target == "claude" and plugin == "pig")
    root = tmp_path / "dist/claude/pig"
    hooks = json.loads((root / "hooks/hooks.json").read_text())["hooks"]
    assert len(hooks["SessionStart"]) == 2  # managed trace hook is preserved
    assert hooks["PreToolUse"][0]["matcher"] == "Agent|Task"
    assert "UserPromptSubmit" not in hooks
    result = subprocess.run(
        hooks["PreToolUse"][0]["hooks"][0]["command"],
        shell=True,
        input=json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": "Task", "tool_input": {"prompt": "in order to test"}}
        ),
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(root)},
    )
    assert json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["prompt"] == "to test"
    manifest = json.loads((tmp_path / "dist/mcp/bundle.json").read_text())
    for asset in manifest["assets"]:
        for file in asset["files"]:
            data = (tmp_path / "dist/mcp" / file["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == file["sha256"]
            assert len(data) == file["size"]
    mcp_content = (tmp_path / "dist/mcp/assets/skill/sample/SKILL.md").read_text()
    assert mcp_content == original.replace("In order to ship", "To ship")
    assert (tmp_path / "assets/skills/sample/SKILL.md").read_bytes() == before
    _configure(tmp_path, concise=False, compact=False)
    build_hub(tmp_path)
    assert not (root / "hooks/pig-token-efficiency.py").exists()
    assert (root / "skills/sample/SKILL.md").read_text() == original
    assert "PreToolUse" not in json.loads((root / "hooks/hooks.json").read_text())["hooks"]


def test_verify_and_preview_never_modify_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    init_hub(tmp_path, org="Promptless")
    _skill(tmp_path)
    _configure(tmp_path, concise=True, compact=True)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    verify_hub(tmp_path)
    assert main(["compact-preview", str(tmp_path / "assets/skills/sample/SKILL.md")]) == 0
    output = capsys.readouterr().out
    assert "not a token estimate" in output
    assert "-In order to ship" in output and "+To ship" in output
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize(("concise", "compact"), [(True, False), (False, True), (False, False)])
def test_build_switches_are_independent(tmp_path: Path, concise: bool, compact: bool) -> None:
    init_hub(tmp_path, org="Promptless")
    original = _skill(tmp_path)
    _configure(tmp_path, concise=concise, compact=compact)
    build_hub(tmp_path)
    root = tmp_path / "dist/claude/pig"
    path = root / "hooks/hooks.json"
    events = json.loads(path.read_text())["hooks"] if path.exists() else {}
    assert ("SessionStart" in events) == concise
    assert ("SubagentStart" in events) == concise
    assert ("PreToolUse" in events) == compact
    expected = original.replace("In order to ship", "To ship") if compact else original
    assert (root / "skills/sample/SKILL.md").read_text() == expected


def test_agent_and_command_projections_preserve_sources_and_native_output(tmp_path: Path) -> None:
    init_hub(tmp_path, org="Promptless")
    _configure(tmp_path, concise=False, compact=True)
    original = "---\ndescription: Use when shipping.\n---\n\nIn order to ship, require passing checks.\n"
    agent = tmp_path / "assets/agents/reviewer.md"
    agent.write_text(original)
    agent.with_suffix(".asset.yaml").write_text(
        "support:\n  claude:\n    mode: native\n  codex:\n    mode: agent-skill\n"
    )
    command = tmp_path / "assets/commands/ship.md"
    command.write_text(original)
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [agent:reviewer, command:ship]\n")
    build_hub(tmp_path)
    for path in (
        "dist/codex/pig/skills/reviewer/SKILL.md",
        "dist/claude/pig/skills/ship/SKILL.md",
        "dist/mcp/projections/agent/reviewer/SKILL.md",
    ):
        assert (tmp_path / path).read_text().endswith("To ship, require passing checks.\n")
    for path in (
        "dist/claude/pig/agents/reviewer.md",
        "dist/mcp/assets/agent/reviewer/reviewer.md",
        "dist/mcp/assets/command/ship/ship.md",
    ):
        assert (tmp_path / path).read_text() == original
    manifest = json.loads((tmp_path / "dist/mcp/bundle.json").read_text())
    prompt = next(asset["prompt"] for asset in manifest["assets"] if asset["ref"] == "command:ship")
    assert prompt["body"].endswith("To ship, require passing checks.\n")
    assert agent.read_text() == command.read_text() == original


@pytest.mark.parametrize("value", [{"concise_output": "true"}, {"compact_instructions": 1}, {"unknown": True}])
def test_config_rejects_ambiguous_switches(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TokenEfficiencyConfig.model_validate(value)
