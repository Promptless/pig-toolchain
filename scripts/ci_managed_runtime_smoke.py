#!/usr/bin/env python3
"""Exercise installed compiler artifacts and interpreter-free native runtimes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Sequence


def run(
    command: Sequence[os.PathLike[str] | str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise AssertionError(f"command failed: {command}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def executable(environment: Path, name: str) -> Path:
    return environment / ("Scripts" if os.name == "nt" else "bin") / (name + (".exe" if os.name == "nt" else ""))


def build_hub(toolchain: Sequence[os.PathLike[str] | str], workspace: Path) -> Path:
    root = workspace / "hub with spaces"
    run([*toolchain, "init", "--hub", root, "--org", "Artifact Smoke"], cwd=workspace)
    config = root / "hub.yaml"
    config.write_text(config.read_text().replace("enabled: false", "enabled: true"))
    run([*toolchain, "build", "--hub", root], cwd=workspace)
    return root


def verify_runtime(plugin: Path, workspace: Path) -> None:
    runtime = plugin / "runtime"
    inventory = json.loads((runtime / "native-manifest.json").read_text())
    files = inventory["files"]
    assert {p.relative_to(runtime).as_posix() for p in runtime.rglob("*") if p.is_file()} == set(files) | {
        "native-manifest.json"
    }
    for name, digest in files.items():
        assert hashlib.sha256((runtime / name).read_bytes()).hexdigest() == digest, name
    assert (
        inventory["bundle_sha256"]
        == hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    record = json.loads((plugin / "hub.managed-runtimes.json").read_text())["managed_runtimes"][0]
    assert record["sha256"] == inventory["bundle_sha256"]
    assert record["source_sha256"] == inventory["source_sha256"]
    assert not list(runtime.rglob("*.py")) and not list(runtime.rglob("*.cjs"))
    home = workspace / ("home with spaces-" + plugin.parent.name)
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PLUGIN_ROOT": str(plugin),
        "PATH": "",
        "PROMPTLESS_WORKER_BASE_URL": "http://127.0.0.1:1",
        "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "1",
        "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER": "0",
    }
    binary = runtime / ("promptless-host-runtime.exe" if os.name == "nt" else "promptless-host-runtime")

    def invoke(*args: str) -> dict[str, Any]:
        return json.loads(run([binary, *args], cwd=workspace, env=env).stdout)

    version = invoke("version", "--json")
    assert version["sha256"] in files.values()
    assert version["version"] == record["version"] == "0.4.0"
    assert version["channel"] == record["channel"]
    status = invoke("status", "--host", "codex")
    assert status["status"] == "ok" and status["runtime"] == version
    assert status["state"]["exists"] is False
    assert invoke("reset", "--host", "codex", "--yes")["status"] == "reset"
    assert invoke("status", "--host", "codex")["state"]["exists"] is True

    # Exercise real native detachment and pipe closure, with no interpreters on PATH.
    # Connection refusal makes the child leave a durable diagnostic and terminate.
    result = subprocess.run(
        [binary, "cursor-notify", "--detach", "--lifecycle", "stop"],
        input='{"conversation_id":"smoke","prompt":"DO_NOT_PERSIST"}',
        capture_output=True,
        text=True,
        timeout=3,
        env=env,
        cwd=workspace,
    )
    assert result.returncode == 0 and result.stdout == "" and result.stderr == "", result
    state = home / ".promptless" / "instruction-hub"
    deadline = time.monotonic() + 15
    while not (state / "last-bootstrap-status.json").is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (state / "last-bootstrap-status.json").is_file(), "detached native process did not report completion"
    for path in state.rglob("*.json"):
        assert "DO_NOT_PERSIST" not in path.read_text(), path

    # Execute the actual generated command forms. Startup installs the launcher;
    # terminal hooks must still work after an update removes the cached plugin.
    target = plugin.parent.name
    env.update(CLAUDE_PLUGIN_ROOT=str(plugin), CURSOR_PLUGIN_ROOT=str(plugin))
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]
    status_path = state / "last-bootstrap-status.json"

    def execute_hook(event: str) -> None:
        entry = hooks[event][0] if target == "cursor" else hooks[event][0]["hooks"][0]
        command = hook_command(entry, target, env)
        status_path.unlink(missing_ok=True)
        result = subprocess.run(
            command,
            input='{"conversation_id":"wrapper-smoke"}',
            capture_output=True,
            text=True,
            timeout=3,
            env=env,
            cwd=workspace,
        )
        assert result.returncode == 0 and not result.stdout and not result.stderr, result
        deadline = time.monotonic() + 15
        while not status_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert status_path.is_file(), f"{target} {event} hook did not run its detached child"

    if target == "cursor":
        execute_hook("stop")
    else:
        execute_hook("SessionStart")
        stable = state / "bin" / "promptless-host-runtime"
        assert stable.is_file()
        # Install an updated plugin using the same home, then delete both cache
        # entries. No same-plugin sibling can rescue these terminal invocations.
        updated = plugin.with_name("pig updated")
        shutil.copytree(plugin, updated)
        original_env = env.copy()
        env.update(PLUGIN_ROOT=str(updated), CLAUDE_PLUGIN_ROOT=str(updated), CURSOR_PLUGIN_ROOT=str(updated))
        execute_hook("SessionStart")
        env.update(original_env)
        shutil.rmtree(plugin)
        shutil.rmtree(updated)
        for event in ("Stop", "SessionEnd", "SubagentStop"):
            execute_hook(event)


def hook_command(entry: dict[str, Any], target: str, env: dict[str, str]) -> list[str] | str:
    if os.name != "nt":
        return ["/bin/sh", "-c", entry["command"]]
    if target == "claude":
        assert entry["shell"] == "bash" and "args" not in entry
        git = shutil.which("git")
        assert git, "Git Bash is required by the generated Claude hook on Windows"
        shell = Path(git).resolve().parent.parent / "bin/bash.exe"
        assert shell.is_file(), shell
        return [str(shell), "--noprofile", "--norc", "-c", entry["command"]]
    if target == "cursor":
        # cmd.exe /s /c strips this outer pair of quotes.
        hook = entry["command"].replace("${CURSOR_PLUGIN_ROOT}", env["CURSOR_PLUGIN_ROOT"])
        return f'"{os.environ["COMSPEC"]}" /d /s /c "{hook}"'
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "PowerShell is needed to exercise Codex's Windows command form"
    return [shell, "-NoProfile", "-NonInteractive", "-Command", entry["commandWindows"]]


def verify_hub(hub: Path, workspace: Path) -> None:
    for target in ("claude", "codex", "cursor"):
        plugin = hub / "dist" / target / "pig"
        assert (plugin / "skills" / "add-external-plugin" / "SKILL.md").is_file()
        verify_runtime(plugin, workspace)


def verify_archive(artifact: Path) -> None:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()
    else:
        with tarfile.open(artifact, "r:gz") as archive:
            names = archive.getnames()
    assert any(name.endswith("/cmd/promptless-host-runtime/main.go") for name in names), "Go source missing"
    assert any(name.endswith("/go.sum") for name in names), "Go dependency lock missing"
    assert any(name.endswith("/host_enrollment/cursor-hook.cmd") for name in names), "native launcher missing"
    assert not any("/host_enrollment/promptless_host_runtime/" in name for name in names)
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def artifacts(dist: Path, uv: Path) -> None:
    wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "expected exactly one wheel and sdist"
    for artifact in (*wheels, *sdists):
        verify_archive(artifact)
        with tempfile.TemporaryDirectory(prefix="native-artifact-smoke-") as directory:
            workspace = Path(directory)
            environment = workspace / "venv"
            run([uv, "venv", "--python", sys.executable, environment], cwd=workspace)
            run(
                [uv, "pip", "install", "--python", executable(environment, "python"), artifact.resolve()], cwd=workspace
            )
            verify_hub(build_hub([executable(environment, "pig")], workspace), workspace)
        print(f"validated release artifact: {artifact.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    artifact = commands.add_parser("artifacts")
    artifact.add_argument("--dist", type=Path, required=True)
    artifact.add_argument("--uv", type=Path, required=True)
    commands.add_parser("platform")
    args = parser.parse_args()
    if args.command == "artifacts":
        artifacts(args.dist.resolve(), args.uv.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="native-platform-smoke-") as directory:
            workspace = Path(directory)
            verify_hub(build_hub([sys.executable, "-m", "promptless_instruction_hub.cli"], workspace), workspace)
        print(f"validated native runtime on {sys.platform}")


if __name__ == "__main__":
    main()
