"""Terminal hooks remain callable after a plugin update removes a session's root."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from tests.config_helpers import enable_trace_ingestion


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook behavior; Windows is covered by the native smoke")
@pytest.mark.parametrize(
    "event,lifecycle", [("Stop", "stop"), ("SessionEnd", "session_end"), ("SubagentStop", "subagent_stop")]
)
@pytest.mark.parametrize("state", ["current", "deleted", "no-runtime", "unset"])
def test_codex_terminal_hook_recovers_sibling_without_interpreters(
    tmp_path: Path, event: str, lifecycle: str, state: str
) -> None:
    hub = tmp_path / "hub"
    init_hub(hub, org="Acme")
    enable_trace_ingestion(hub)
    build_hub(hub)
    hook = json.loads((hub / "dist/codex/pig/hooks/hooks.json").read_text())["hooks"][event][0]["hooks"][0]
    cache = tmp_path / "plugin space & apostrophe's café $value `literal` [brackets]" / "pig"
    current = cache / "0.3.0"
    sibling = cache / "0.3.1"
    for root in (current, sibling):
        runtime = root / "runtime/promptless-host-runtime"
        runtime.parent.mkdir(parents=True)
        runtime.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0" "$@" > "$CALL_LOG"\n'
            'while IFS= read -r line || [ -n "$line" ]; do printf "%s\\n" "$line"; done > "$INPUT_LOG"\n'
            'exit "${RUNTIME_EXIT:-0}"\n'
        )
        runtime.chmod(0o755)
    if state in ("deleted", "no-runtime"):
        shutil.rmtree(current)
    if state == "no-runtime":
        shutil.rmtree(sibling)
    log = tmp_path / "calls"
    input_log = tmp_path / "input"
    env = {"PATH": "", "CALL_LOG": str(log), "INPUT_LOG": str(input_log)}
    if state != "unset":
        env["PLUGIN_ROOT"] = str(current)
    body = '{"message":"héllo 世界", "sessionId":"test"}\n'
    result = subprocess.run(
        ["/bin/sh", "-c", hook["command"]], input=body, text=True, env=env, capture_output=True, timeout=3
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    if state in ("no-runtime", "unset"):
        assert not log.exists()
    else:
        selected = current if state == "current" else sibling
        assert log.read_text().splitlines() == [
            str(selected / "runtime/promptless-host-runtime"),
            "collect",
            "--host",
            "codex",
            "--lifecycle",
            lifecycle,
            "--detach",
            "--quiet",
        ]
        assert input_log.read_text() == body
        failed = subprocess.run(
            ["/bin/sh", "-c", hook["command"]],
            input=body,
            text=True,
            env={**env, "RUNTIME_EXIT": "17"},
            capture_output=True,
            timeout=3,
        )
        assert failed.returncode == 17
