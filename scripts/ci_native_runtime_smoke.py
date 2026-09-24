#!/usr/bin/env python3
"""Exercise frozen plugin launchers with an empty PATH and a local fake worker."""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from promptless_instruction_hub import native_runtime
from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.fs import read_yaml_mapping, write_yaml
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.native_bundle import (
    validate_bundle,
)
from tests.managed_bootstrap.helpers import _FakeWorkerServer, _diagnostic_log_path, _signed_policy
from tests.managed_bootstrap.test_device_enrollment import DeviceAPI


def run(command: list[str], env: dict[str, str], body: str = "", timeout: int = 30) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    result = subprocess.run(
        command, env=env, input=body, text=True, encoding="utf-8", capture_output=True, timeout=timeout
    )
    if result.returncode != 0:
        raise AssertionError(f"Native command failed: {command}\n{result.stdout}\n{result.stderr}")
    print(f"{Path(command[0]).name}: {time.monotonic() - started:.3f}s")
    return result


def smoke(bundle: Path, *, complete: bool, embedded: bool = False) -> None:
    manifest = validate_bundle(bundle, complete=complete)
    if embedded:
        os.environ.pop("PIG_NATIVE_RUNTIME_DIR", None)
        assert native_runtime.EMBEDDED_ROOT.resolve() == bundle.resolve()
        assert native_runtime.resolve_native_bundle().resolve() == bundle.resolve()
    else:
        os.environ["PIG_NATIVE_RUNTIME_DIR"] = str(bundle.resolve())
    with tempfile.TemporaryDirectory(prefix="pig-native-smoke-") as temporary:
        workspace = Path(temporary)
        hub = workspace / "plugin space & apostrophe's café"
        init_hub(hub, org="Acme")
        config_path = hub / "hub.yaml"
        config = read_yaml_mapping(config_path)
        config["trace_ingestion"] = {"enabled": True}
        write_yaml(config_path, config)
        build_hub(hub)
        plugin = hub / "dist/codex/pig"
        runtime = plugin / "runtime/promptless-host-runtime"
        if os.name == "nt":
            runtime = runtime.with_suffix(".exe")
        home = workspace / "home"
        home.mkdir()
        # Keep Windows loader/OS variables, while removing executable discovery.
        env = {
            **os.environ,
            "PATH": "",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": "",
            "PYTHONHOME": "",
            "PLUGIN_ROOT": str(plugin),
            "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER": "0",
            "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "1",
        }
        version = json.loads(run([str(runtime), "version", "--json"], env).stdout)
        assert version["sha256"] == manifest["bundle_sha256"]
        server = _FakeWorkerServer(policy=_signed_policy(enabled_hosts=["codex", "claude", "cursor"]))
        server.start()
        try:
            env.update(PROMPTLESS_WORKER_BASE_URL=server.base_url, PROMPTLESS_DASHBOARD_BASE_URL=server.base_url)
            run([str(runtime), "enroll", "--host", "codex"], env)
            transcript = workspace / "trace café.jsonl"
            transcript.write_text('{"message":"héllo 世界"}\n', encoding="utf-8")
            body = json.dumps({"sessionId": "native-smoke", "transcriptPath": str(transcript)})
            hooks = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]
            terminal = hooks["Stop"][0]["hooks"][0]
            startup = hooks["SessionStart"][0]["hooks"][0]
            if os.name == "nt":
                powershells = [shutil.which(name) for name in ("powershell", "pwsh")]
                assert all(powershells), "Both Windows PowerShell 5.1 and PowerShell 7 must be qualified"
                command = terminal["commandWindows"].replace("${PLUGIN_ROOT}", str(plugin))
                for shell in powershells:
                    assert shell is not None
                    startup_command = startup["commandWindows"].replace("${PLUGIN_ROOT}", str(plugin))
                    run([shell, "-NoProfile", "-NonInteractive", "-Command", startup_command], env, body)
                    run([shell, "-NoProfile", "-NonInteractive", "-Command", command], env, body, timeout=3)
            else:
                for shell in ("/bin/sh", "/bin/bash", "/bin/zsh", "/bin/dash"):
                    if Path(shell).exists():
                        run([shell, "-c", startup["command"]], env, body)
                        run([shell, "-c", terminal["command"]], env, body, timeout=3)
            deadline = time.monotonic() + 20
            while not server.trace_batches and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.trace_batches, "Frozen detached self-exec did not upload"
            chunk = server.trace_batches[0]["chunks"][0]
            assert gzip.decompress(base64.b64decode(chunk["content_base64"])) == transcript.read_bytes()
            # Cursor 3.21.9 $executeHookDirect/f5a/E5a sends UTF-8 input via a
            # temporary file and adds the call operator to a quoted command.
            # Reproduce that host wrapper around the literal emitted command.
            cursor_plugin = hub / "dist/cursor/pig"
            cursor_hooks = json.loads((cursor_plugin / "hooks/hooks.json").read_text())["hooks"]
            cursor_command = cursor_hooks["sessionStart"][-1]["command"]
            cursor_body = json.dumps({"conversation_id": "native-cursor", "generation_id": "native-generation"})
            database = workspace / "state.vscdb"
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
                for key, value in (
                    ("composerData:native-cursor", {"fullConversationHeadersOnly": [{"bubbleId": "bubble"}]}),
                    ("bubbleId:native-cursor:bubble", {"type": 1, "text": "native Cursor héllo 世界"}),
                ):
                    connection.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, json.dumps(value)))
            connection.close()
            env["PROMPTLESS_CURSOR_DATABASE"] = str(database)
            cursor_runtime = cursor_plugin / "runtime" / runtime.name
            status_path = home / ".promptless/instruction-hub/cursor-launcher-status.json"
            pending_path = status_path.parent / "cursor/pending"

            def run_cursor(command: list[str], *, body: str = "", timeout: int = 30) -> None:
                # Await each collector before launching the next. A coalesced
                # non-owner's successful exit cannot conceal the owner's failure.
                status_path.unlink(missing_ok=True)
                result = run(command, {**env, "CURSOR_PLUGIN_ROOT": str(cursor_plugin)}, body, timeout)
                deadline = time.monotonic() + 30
                while not status_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                status = json.loads(status_path.read_text()) if status_path.exists() else {"status": "missing"}
                if status["status"] != "completed" or list(pending_path.glob("*.json")):
                    status_path.unlink(missing_ok=True)
                    input_probe = ""
                    if os.name == "nt":
                        # Diagnostic only: inspect the synthetic fixture bytes
                        # through the same host wrapper and batch file boundary.
                        probe_script = workspace / "capture-input.py"
                        probe_script.write_text("import sys; print(sys.stdin.buffer.read().hex())", encoding="utf-8")
                        probe_batch = workspace / "capture-input.cmd"
                        launcher = cursor_plugin / "runtime/cursor-hook.cmd"
                        probe_batch.write_text(
                            launcher.read_text().replace(
                                '"%~dp0promptless-host-runtime.exe" cursor-hook --lifecycle %1',
                                f'"{sys.executable}" "{probe_script}"',
                            ),
                            encoding="utf-8",
                        )
                        probe_command = [argument.replace(str(launcher), str(probe_batch)) for argument in command]
                        probe = subprocess.run(probe_command, env=env, capture_output=True, text=True, timeout=30)
                        input_probe = f"input probe={probe.returncode}: {probe.stdout} {probe.stderr}"
                    replay = subprocess.run(
                        [str(cursor_runtime), "cursor-hook", "--lifecycle", "stop"],
                        env=env,
                        input=cursor_body,
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        timeout=30,
                    )
                    replay_deadline = time.monotonic() + 30
                    while replay.returncode == 0 and not status_path.exists() and time.monotonic() < replay_deadline:
                        time.sleep(0.05)
                    diagnostic_path = _diagnostic_log_path(home)
                    diagnostics = (
                        diagnostic_path.read_text(encoding="utf-8") if diagnostic_path.exists() else "no diagnostics"
                    )
                    raise AssertionError(
                        f"{status}\nhost stdout={result.stdout}\nhost stderr={result.stderr}\n{input_probe}"
                        f"\ndirect cursor-hook={replay.returncode}: {replay.stdout}\n{replay.stderr}\n{diagnostics}"
                    )

            if os.name == "nt":
                payload_file = workspace / "cursor input's café.json"
                payload_file.write_text(cursor_body, encoding="utf-8")
                command = cursor_command.replace("${CURSOR_PLUGIN_ROOT}", str(cursor_plugin))
                if command.lstrip().startswith(("'", '"')):
                    command = "& " + command
                escaped_payload = str(payload_file).replace("'", "''")
                wrapper = (
                    "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                    f"Get-Content -LiteralPath '{escaped_payload}' -Raw | & {{ $input | {command} }}"
                )
                for name in ("powershell", "pwsh"):
                    shell = shutil.which(name)
                    assert shell is not None
                    run_cursor([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-c", wrapper])
                    terminal_wrapper = wrapper.replace(" session_start", " stop")
                    run_cursor(
                        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-c", terminal_wrapper],
                        timeout=3,
                    )
                node = shutil.which("node")
                assert node is not None, "CI requires Node to qualify Claude's exec-form resolution"
                script = "const r=require('child_process').spawnSync(process.argv[1],['version','--json'],{encoding:'utf8'}); if(r.error)throw r.error;process.stdout.write(r.stdout);process.exit(r.status);"
                result = run([node, "-e", script, str(plugin / "runtime/promptless-host-runtime")], env)
                assert json.loads(result.stdout)["sha256"] == manifest["bundle_sha256"]
            else:
                for shell in ("/bin/sh", "/bin/bash", "/bin/zsh", "/bin/dash"):
                    if Path(shell).exists():
                        run_cursor([shell, "-c", cursor_command], body="\ufeff" + cursor_body)
                        run_cursor([shell, "-c", cursor_hooks["stop"][-1]["command"]], body=cursor_body, timeout=3)
            cursor_records = [
                json.loads(line)
                for batch in server.trace_batches
                if batch["source"] == "cursor"
                for chunk in batch["chunks"]
                for line in gzip.decompress(base64.b64decode(chunk["content_base64"])).splitlines()
            ]
            assert any(row["event"].get("text") == "native Cursor héllo 世界" for row in cursor_records), (
                "Frozen Cursor hooks did not export and upload the seeded native SQLite event"
            )
        finally:
            server.stop()
        # A local TLS worker proves verification remains enabled and custom roots
        # are honored even with no machine Python/OpenSSL certificate installation.
        certificates = Path(__file__).resolve().parents[1] / "tests/fixtures/native-tls"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificates / "localhost.pem", certificates / "localhost.key")
        tls_server = _FakeWorkerServer(tls_context=context)
        tls_server.start()
        try:
            tls_env = {
                **env,
                "PROMPTLESS_WORKER_BASE_URL": tls_server.base_url,
                "PROMPTLESS_DASHBOARD_BASE_URL": tls_server.base_url,
            }
            tls_env.pop("SSL_CERT_FILE", None)
            tls_env.pop("SSL_CERT_DIR", None)
            rejected = subprocess.run(
                [str(runtime), "enroll", "--host", "codex"], env=tls_env, capture_output=True, text=True, timeout=30
            )
            assert "CERTIFICATE_VERIFY_FAILED" in rejected.stdout + rejected.stderr, rejected
            tls_env["SSL_CERT_FILE"] = str(certificates / "localhost.pem")
            run([str(runtime), "enroll", "--host", "codex"], tls_env)
            assert tls_server.session_requests, "Frozen runtime did not reach trusted HTTPS worker"
            api = DeviceAPI(tls_context=context)
            try:
                api.approved = True
                device_env = {
                    **tls_env,
                    "PROMPTLESS_HOSTED_API_BASE_URL": api.base_url,
                    "PROMPTLESS_DASHBOARD_BASE_URL": api.base_url,
                }
                run([str(runtime), "reset", "--yes"], device_env)
                enrolled = run([str(runtime), "enroll", "--host", "codex", "--device"], device_env)
                assert json.loads(enrolled.stdout)["status"] == "enrolled"
                assert api.creations and api.polls, "Frozen no-redirect device requests did not reach trusted HTTPS API"
            finally:
                api.close()
        finally:
            tls_server.stop()
        # Altering an external frozen library must invalidate the reported digest.
        library = next(path for path in (plugin / "runtime").rglob("base_library.zip"))
        with library.open("ab") as output:
            output.write(b"tampered")
        result = subprocess.run([str(runtime), "version", "--json"], env=env, capture_output=True)
        assert result.returncode != 0 and b"hash verification failed" in result.stderr


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--embedded", action="store_true")
    args = parser.parse_args()
    smoke(args.bundle, complete=args.complete, embedded=args.embedded)


if __name__ == "__main__":
    main()
