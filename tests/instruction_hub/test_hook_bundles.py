from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import pytest

from promptless_instruction_hub import hook_launcher
from promptless_instruction_hub.compiler import build_hub
from tests.instruction_hub.test_hook_declarations import declaration, event

ROOT_VARS = {"codex": "PLUGIN_ROOT", "claude": "CLAUDE_PLUGIN_ROOT", "cursor": "CURSOR_PLUGIN_ROOT"}


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMPTLESS_HOOK_DATA_HOME", str(tmp_path / "data"))


def snapshot(root: Path, host: str, version: str) -> tuple[Path, str, str]:
    hub = root / f"hub-{version}"
    metadata = declaration(hub)
    (metadata.parent / "helper.py").write_text(f"def echo(event): return {version!r}\n")
    (metadata.parent / "settings.json").write_text(json.dumps({"version": version}))
    (metadata.parent / "handler's $example.py").write_text(
        "import json\nfrom pathlib import Path\nfrom helper import echo\n"
        "settings = json.loads(Path(__file__).with_name('settings.json').read_text())\n"
        "assert settings['version'] == echo(None)\n"
        "print(json.dumps({'context': echo(None)}))\n"
    )
    build_hub(hub)
    plugin = root / "cache with $spaces" / version
    shutil.copytree(hub / "dist" / host / "pig", plugin)
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]

    def command(name: str) -> str:
        entry = hooks[name][0]
        return (entry if host == "cursor" else entry["hooks"][0])["command"]

    return (
        plugin,
        command(event(host)["hook_event_name"]),
        command("sessionStart" if host == "cursor" else "SessionStart"),
    )


def invoke(plugin: Path, host: str, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps({**event(host), "session_id": "test-session"}),
        env={**os.environ, ROOT_VARS[host]: str(plugin)},
        capture_output=True,
        text=True,
        timeout=10,
    )


def context(result: subprocess.CompletedProcess[str], host: str) -> str:
    assert result.returncode == 0 and not result.stderr
    response = json.loads(result.stdout)
    return response["additional_context"] if host == "cursor" else response["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("host", ROOT_VARS)
def test_loaded_hook_keeps_complete_bundle_across_update_and_resume(tmp_path: Path, host: str) -> None:
    old, old_command, prepare_old = snapshot(tmp_path, host, "0.1.0")
    prepared = invoke(old, host, prepare_old)
    assert prepared.returncode == 0 and not prepared.stdout and not prepared.stderr
    new, new_command, prepare_new = snapshot(tmp_path, host, "0.2.0")
    assert invoke(new, host, prepare_new).returncode == 0
    shutil.rmtree(old)
    # The first tool event can arrive after the update removed the old cache.
    assert context(invoke(old, host, old_command), host) == "0.1.0"
    assert context(invoke(new, host, new_command), host) == "0.2.0"
    shutil.rmtree(new)
    # Later invocations/resumes and a rollback still resolve the exact digest.
    assert context(invoke(old, host, old_command), host) == "0.1.0"
    assert context(invoke(new, host, new_command), host) == "0.2.0"
    assert len(list((hook_launcher.data_root() / "bundles").iterdir())) == 2


def test_concurrent_first_invocations_publish_one_complete_bundle(tmp_path: Path) -> None:
    plugin, command, _ = snapshot(tmp_path, "codex", "0.1.0")
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: invoke(plugin, "codex", command), range(8)))
    assert all(context(result, "codex") == "0.1.0" for result in results)
    assert len(list((hook_launcher.data_root() / "bundles").iterdir())) == 1


@pytest.mark.parametrize("damage", ["missing", "changed", "extra", "symlink"])
def test_corrupt_retained_bundle_is_never_executed(tmp_path: Path, damage: str) -> None:
    plugin, command, prepare = snapshot(tmp_path, "codex", "0.1.0")
    assert invoke(plugin, "codex", prepare).returncode == 0
    retained = next((hook_launcher.data_root() / "bundles").iterdir())
    helper = retained / "example/helper.py"
    if damage == "missing":
        helper.unlink()
    elif damage == "changed":
        helper.write_text("def echo(event): return 'wrong version'")
    elif damage == "extra":
        (retained / "example/untracked.py").write_text("raise RuntimeError('untracked')")
    else:
        helper.unlink()
        helper.symlink_to(plugin / "hooks/example/helper.py")
    first = invoke(plugin, "codex", command)
    second = invoke(plugin, "codex", command)
    assert first.returncode == second.returncode == 0
    assert not first.stdout and not second.stdout
    assert "context hook skipped" in first.stderr
    assert not second.stderr  # Missing/corrupt bundle diagnostics do not flood every tool call.


def test_cache_disappearing_during_staging_does_not_publish_partial_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _, _ = snapshot(tmp_path, "codex", "0.1.0")
    digest = next((plugin / "hooks/_bundles").iterdir()).stem
    original = hook_launcher.checked_file

    def remove_source(root: Path, name: str, expected: dict) -> bytes:
        payload = original(root, name, expected)
        shutil.rmtree(plugin)
        return payload

    monkeypatch.setattr(hook_launcher, "checked_file", remove_source)
    with pytest.raises(FileNotFoundError):
        hook_launcher.retained_bundle(plugin / "hooks", digest)
    assert not list((hook_launcher.data_root() / "bundles").iterdir())


def test_same_version_replacement_cannot_change_a_loaded_command(tmp_path: Path) -> None:
    plugin, command, prepare = snapshot(tmp_path, "codex", "0.1.0")
    assert invoke(plugin, "codex", prepare).returncode == 0
    replacement, replacement_command, _ = snapshot(tmp_path, "codex", "replacement")
    shutil.rmtree(plugin)
    replacement.rename(plugin)
    assert context(invoke(plugin, "codex", command), "codex") == "0.1.0"
    assert context(invoke(plugin, "codex", replacement_command), "codex") == "replacement"


def test_unwritable_storage_is_nonblocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin, command, _ = snapshot(tmp_path, "codex", "0.1.0")
    occupied = tmp_path / "not-a-directory"
    occupied.write_text("occupied")
    monkeypatch.setenv("PROMPTLESS_HOOK_DATA_HOME", str(occupied))
    result = invoke(plugin, "codex", command)
    assert result.returncode == 0 and not result.stdout
    assert "context hook skipped" in result.stderr


def test_generated_launcher_python_floor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    interpreter = os.environ.get("HOOK_TEST_PYTHON") or shutil.which("python3.9")
    if not interpreter:
        pytest.skip("Python 3.9 is exercised by the release-artifacts job")
    plugin, command, prepare = snapshot(tmp_path, "codex", "0.1.0")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(interpreter)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    assert invoke(plugin, "codex", prepare).returncode == 0
    shutil.rmtree(plugin)
    assert context(invoke(plugin, "codex", command), "codex") == "0.1.0"


def test_generated_launcher_preserves_signal_delivery_after_cache_deletion(tmp_path: Path) -> None:
    hub = tmp_path / "hub"
    metadata = declaration(hub)
    ready = tmp_path / "ready"
    (metadata.parent / "handler's $example.py").write_text(
        "import json, os, signal, time\nfrom pathlib import Path\n"
        "def cancel(*args):\n    print(json.dumps({'context': 'cancelled'}))\n    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, cancel)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.05)\n"
    )
    build_hub(hub)
    plugin = hub / "dist/codex/pig"
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]
    command = hooks["PostToolUse"][0]["hooks"][0]["command"]
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(event("codex")))
    with payload.open() as stdin:
        process = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PLUGIN_ROOT": str(plugin)},
        )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert int(ready.read_text()) == process.pid
        shutil.rmtree(plugin)
        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=5)
        assert process.returncode == 0 and not error
        assert json.loads(output)["hookSpecificOutput"]["additionalContext"] == "cancelled"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
