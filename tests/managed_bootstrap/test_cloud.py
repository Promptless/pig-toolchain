from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import ssl
import subprocess
import threading
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from promptless_instruction_hub.cli import main as pig_main
from promptless_instruction_hub.cloud_bundle import CLOUD_PLUGIN_VERSION, build_cloud_bundle
from promptless_instruction_hub.managed_runtime import _runtime_bundle_sha256
from promptless_instruction_hub.fs import JsonValue, validate_json_value

from .helpers import _signed_policy

_GRANT = "plicg_" + "a" * 43


@dataclass(frozen=True)
class _Request:
    method: str
    path: str
    authorization: str | None
    producer: str | None
    payload: dict[str, JsonValue]


class _CloudWorker:
    """Serve the cloud HTTP contract over real TLS with controllable failures."""

    def __init__(self, certificate: Path, key: Path) -> None:
        self.requests: list[_Request] = []
        self.leases: dict[str, str] = {}
        self.batches: dict[str, dict[str, JsonValue]] = {}
        self.drop_lease_response = False
        self.drop_upload_response = False
        self.reject_ack = False
        self.revoked_path: str | None = None
        self.redirect_code: int | None = None
        self.redirect_url = ""
        self.lease_overrides: dict[str, JsonValue] = {}
        self.policy = _signed_policy()
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._handle({})

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                payload = validate_json_value(json.loads(self.rfile.read(length)), "test request")
                assert isinstance(payload, dict)
                self._handle(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

            def _handle(self, payload: dict[str, JsonValue]) -> None:
                request = _Request(
                    self.command,
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("X-Promptless-Producer-Token"),
                    payload,
                )
                worker.requests.append(request)
                parsed = urlsplit(self.path)
                if worker.redirect_code is not None and parsed.path == "/redirect":
                    self.send_response(worker.redirect_code)
                    self.send_header("Location", worker.redirect_url)
                    self.end_headers()
                    return
                if parsed.path == worker.revoked_path:
                    self._reply({"detail": "revoked"}, 403)
                    return
                if parsed.path == "/v0/cloud-enrollment/leases":
                    execution = payload["execution_id"]
                    credential_hash = payload["credential_hash"]
                    assert isinstance(execution, str)
                    assert isinstance(credential_hash, str)
                    assert request.producer is not None
                    assert credential_hash == hashlib.sha256(request.producer.encode()).hexdigest()
                    if execution in worker.leases:
                        assert worker.leases[execution] == credential_hash
                    worker.leases[execution] = credential_hash
                    if worker.drop_lease_response:
                        worker.drop_lease_response = False
                        self.close_connection = True
                        return
                    self._reply(
                        {
                            "id": "lease-1",
                            "grant_id": "grant-1",
                            "host_instance_id": "host-1",
                            "deployment_instance_id": "deployment-1",
                            "execution_id": execution,
                            "target": payload["target"],
                            "provider": "claude_tag",
                            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat(),
                            **worker.lease_overrides,
                        }
                    )
                    return
                if parsed.path == "/v0/host-enrollment/policy":
                    assert parse_qs(parsed.query)["target"] in (["claude"], ["codex"])
                    self._reply(worker.policy)
                    return
                if parsed.path == "/v0/traces/batches":
                    assert parse_qs(parsed.query)["target"] == [payload["host"]]
                    batch_id = payload["batch_id"]
                    assert isinstance(batch_id, str)
                    worker.batches[batch_id] = payload
                    if worker.drop_upload_response:
                        worker.drop_upload_response = False
                        self.close_connection = True
                        return
                    chunks = payload["chunks"]
                    assert isinstance(chunks, list)
                    ranges: list[JsonValue] = []
                    for chunk in chunks:
                        assert isinstance(chunk, dict)
                        ranges.append(
                            {
                                key: chunk[key]
                                for key in ("kind", "source_path_hash", "start_offset", "end_offset", "content_sha256")
                            }
                        )
                    self._reply(
                        {
                            "accepted": True,
                            "batch_id": batch_id,
                            "policy_version": payload["policy_version"],
                            "raw_artifact_count": len(chunks),
                            "skipped_record_count": 0,
                            "acknowledged_ranges": [] if worker.reject_ack else ranges,
                            "unparsed_record_count": 0,
                        }
                    )
                    return
                self._reply({"detail": "unexpected route"}, 404)

            def _reply(self, payload: dict[str, JsonValue], status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        """Stop the local test endpoint and its thread."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("cloud-tls")
    certificate, key = root / "certificate.pem", root / "key.pem"
    config = root / "openssl.conf"
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
        "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=critical,CA:TRUE\n"
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-config",
            str(config),
            "-keyout",
            str(key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    return certificate, key


@pytest.fixture
def cloud_worker(tls_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[_CloudWorker]:
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_files[0]))
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    worker = _CloudWorker(*tls_files)
    try:
        yield worker
    finally:
        worker.close()


@pytest.fixture
def execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cloud_worker: _CloudWorker
) -> tuple[Path, dict[str, JsonValue]]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PROMPTLESS_CLOUD_GRANT", _GRANT)
    monkeypatch.delenv("PROMPTLESS_CLOUD_GRANT_FILE", raising=False)
    config_path = tmp_path / "cloud-capture.json"
    _write_config(config_path, cloud_worker.url)
    transcript = tmp_path / "root-session.jsonl"
    transcript.write_bytes(b'{"type":"user","sessionId":"root-session","message":{"content":"hello"}}\n')
    return config_path, {"session_id": "root-session", "transcript_path": str(transcript), "hook_event_name": "Stop"}


def _write_config(path: Path, url: str, *, transport: str = "secret", target: str = "claude") -> None:
    path.write_text(
        json.dumps(
            {
                "worker_url": url,
                "provider": "claude_tag",
                "target": target,
                "transport": transport,
                "integration_id": "test-integration",
            }
        )
    )


def _state_root(execution: tuple[Path, dict[str, JsonValue]]) -> Path:
    config_path, context = execution
    config = json.loads(config_path.read_text())
    scope = [
        config["worker_url"].rstrip("/"),
        config["integration_id"],
        config["provider"],
        config["target"],
        context["session_id"],
    ]
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    return Path.home() / ".promptless/cloud" / digest


def _upload_requests(worker: _CloudWorker) -> list[_Request]:
    return [request for request in worker.requests if request.path.startswith("/v0/traces/batches")]


def test_generated_bundle_runs_without_installed_toolchain(
    execution: tuple[Path, dict[str, JsonValue]], cloud_worker: _CloudWorker, tmp_path: Path
) -> None:
    output = tmp_path / "capture.zip"
    assert (
        pig_main(
            [
                "cloud-bundle",
                "--worker-url",
                cloud_worker.url,
                "--provider",
                "claude_tag",
                "--integration-id",
                "test-integration",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    plugin_root = tmp_path / "unpacked plugin"
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert not any(name.startswith("bin/") or "producer.json" in name or "__pycache__" in name for name in names)
        assert all(_GRANT.encode() not in archive.read(name) for name in names)
        archive.extractall(plugin_root)
        for info in archive.infolist():
            if info.external_attr >> 16 & 0o111:
                (plugin_root / info.filename).chmod(0o755)
    manifest = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())["managed_runtimes"][0]
    assert manifest["sha256"] == _runtime_bundle_sha256(plugin_root / "runtime")
    assert manifest["plugin_id"] == "pig-cloud-capture"
    assert manifest["plugin_version"] == CLOUD_PLUGIN_VERSION
    assert manifest["toolchain_version"] != "unknown"
    hooks = json.loads((plugin_root / "hooks/hooks.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop", "SubagentStop", "SessionEnd"}
    assert all(hook[0]["hooks"][0]["timeout"] == 3 for hook in hooks.values())
    # The extracted Go runtime executes without an installed toolchain.
    event = tmp_path / "event.json"
    event.write_text(json.dumps(execution[1]))
    completed = subprocess.run(
        [
            str(plugin_root / "runtime/promptless-host-runtime"),
            "cloud-collect",
            "--config",
            str(plugin_root / "cloud-capture.json"),
            "--event-file",
            str(event),
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "drained"
    assert not event.exists()
    batch = _upload_requests(cloud_worker)[0].payload
    assert batch["plugin_version"] == CLOUD_PLUGIN_VERSION
    chunks = batch["chunks"]
    assert isinstance(chunks, list) and isinstance(chunks[0], dict)
    content = chunks[0]["content_base64"]
    assert isinstance(content, str)
    assert gzip.decompress(base64.b64decode(content)) == Path(str(execution[1]["transcript_path"])).read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        build_cloud_bundle(
            output, worker_url=cloud_worker.url, provider="claude_tag", integration_id="test", transport="secret"
        )


@pytest.mark.skipif(os.name != "posix", reason="Claude cloud command hooks run in POSIX sandboxes")
def test_generated_hook_detaches_and_records_revoked_auth(
    execution: tuple[Path, dict[str, JsonValue]], cloud_worker: _CloudWorker, tmp_path: Path
) -> None:
    output = build_cloud_bundle(
        tmp_path / "capture.zip",
        worker_url=cloud_worker.url,
        provider="claude_tag",
        integration_id="test-integration",
        transport="secret",
    )
    plugin_root = tmp_path / "plugin with spaces"
    with zipfile.ZipFile(output) as archive:
        archive.extractall(plugin_root)
        for info in archive.infolist():
            if info.external_attr >> 16 & 0o111:
                (plugin_root / info.filename).chmod(0o755)
    hook = json.loads((plugin_root / "hooks/hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]
    cloud_worker.revoked_path = "/v0/cloud-enrollment/leases"
    completed = subprocess.run(
        ["sh", "-c", hook["command"]],
        input=json.dumps(execution[1]),
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_root)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=hook["timeout"],
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == completed.stderr == ""
    state_root = _state_root((plugin_root / "cloud-capture.json", execution[1]))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status_path = state_root / "status.json"
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        if status.get("error_type") == "*runtime.HTTPError" and not tuple(state_root.glob("hook-*.json")):
            break
        time.sleep(0.02)
    assert status["status"] == "partial"
    assert status["error_type"] == "*runtime.HTTPError"
    assert not tuple(state_root.glob("hook-*.json"))
    assert len(cloud_worker.requests) == 1
    assert cloud_worker.requests[0].authorization == f"Bearer {_GRANT}"
    assert cloud_worker.requests[0].producer is not None
