from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from promptless_instruction_hub.cli import main as pig_main
from promptless_instruction_hub.cloud_bundle import CLOUD_PLUGIN_VERSION, build_cloud_bundle
from promptless_instruction_hub.managed_runtime import _runtime_bundle_sha256
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.cloud import (
    CloudConfig,
    _execution_scope,
    _grant_secret,
    _source_paths,
    collect_cloud,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    BootstrapAuthError,
    BootstrapError,
    JsonValue,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.metadata import (
    _load_runtime_metadata,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.validation import (
    _decode_json_object,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.worker import (
    _get_json,
    _post_json_response,
)

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
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._handle({})

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self._handle(_decode_json_object(self.rfile.read(length), "test request"))

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
                    self._reply(_signed_policy())
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
    return _execution_scope(CloudConfig.load(config_path), context)[0]


def _upload_requests(worker: _CloudWorker) -> list[_Request]:
    return [request for request in worker.requests if request.path.startswith("/v0/traces/batches")]


def test_scoped_discovery_does_not_sweep_neighbors_or_symlinked_children(tmp_path: Path) -> None:
    main = (tmp_path / "session.jsonl").resolve()
    main.write_text("{}\n")
    neighbor = main.with_name("another-session.jsonl")
    neighbor.write_text("{}\n")
    children = main.with_suffix("") / "subagents"
    children.mkdir(parents=True)
    child = children / "agent-child.jsonl"
    child.write_text("{}\n")
    (children / "agent-link.jsonl").symlink_to(neighbor)
    (children / "unrelated.jsonl").write_text("{}\n")
    nested = children / "nested"
    nested.mkdir()
    (nested / "agent-hidden.jsonl").write_text("{}\n")

    assert _source_paths(main, "claude") == (main, child)
    assert _source_paths(main, "codex") == (main,)
    other_main = main.with_name("other.jsonl")
    other_main.write_text("{}\n")
    other_main.with_suffix("").mkdir()
    (other_main.with_suffix("") / "subagents").symlink_to(children, target_is_directory=True)
    assert _source_paths(other_main, "claude") == (other_main,)


def test_lost_enrollment_response_reuses_private_producer(
    execution: tuple[Path, dict[str, JsonValue]], cloud_worker: _CloudWorker
) -> None:
    cloud_worker.drop_lease_response = True
    with pytest.raises(OSError):
        collect_cloud(*execution, drain_seconds=0)
    root = _state_root(execution)
    before = json.loads((root / "producer.json").read_text())
    assert set(before) == {"producer_token", "transcript_path"}
    assert (root / "producer.json").stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    assert collect_cloud(*execution, drain_seconds=0)["status"] == "drained"
    requests = [request for request in cloud_worker.requests if request.path.endswith("/leases")]
    assert len(cloud_worker.leases) == 1
    assert requests[0].producer == requests[1].producer == before["producer_token"]
    assert requests[0].payload == requests[1].payload
    assert _GRANT not in (root / "producer.json").read_text()
    assert before["producer_token"] not in (root / "status.json").read_text()


def test_renewal_preserves_acknowledged_ranges_and_only_uploads_new_records(
    execution: tuple[Path, dict[str, JsonValue]], cloud_worker: _CloudWorker
) -> None:
    first = collect_cloud(*execution, drain_seconds=0)
    root = _state_root(execution)
    state_before = (root / "producer.json").read_bytes()
    ledger_paths = list(root.glob("ledger.*.json"))
    assert len(ledger_paths) == 1
    assert first["uploaded_batches"] == 1
    assert collect_cloud(*execution, drain_seconds=0)["uploaded_batches"] == 0
    transcript = Path(str(execution[1]["transcript_path"]))
    original_size = transcript.stat().st_size
    with transcript.open("ab") as handle:
        handle.write(b'{"type":"assistant","sessionId":"root-session","message":{"content":"done"}}\n')
    assert collect_cloud(*execution, drain_seconds=0)["uploaded_batches"] == 1
    assert (root / "producer.json").read_bytes() == state_before
    assert list(root.glob("ledger.*.json")) == ledger_paths
    requests = _upload_requests(cloud_worker)
    assert len(requests) == 2
    second_chunks = requests[1].payload["chunks"]
    assert isinstance(second_chunks, list) and isinstance(second_chunks[0], dict)
    assert second_chunks[0]["start_offset"] == original_size
    assert len({request.producer for request in cloud_worker.requests}) == 1


@pytest.mark.parametrize("failure", ["lost_response", "invalid_ack"])
def test_failed_ack_does_not_advance_ledger(
    execution: tuple[Path, dict[str, JsonValue]], cloud_worker: _CloudWorker, failure: str
) -> None:
    cloud_worker.drop_upload_response = failure == "lost_response"
    cloud_worker.reject_ack = failure == "invalid_ack"
    with pytest.raises((OSError, BootstrapError)):
        collect_cloud(*execution, drain_seconds=0)
    assert not list(_state_root(execution).glob("ledger.*.json"))
    cloud_worker.reject_ack = False
    assert collect_cloud(*execution, drain_seconds=0)["uploaded_batches"] == 1
    requests = _upload_requests(cloud_worker)
    assert len(requests) == 2
    assert requests[0].payload["batch_id"] == requests[1].payload["batch_id"]
    assert requests[0].payload["chunks"] == requests[1].payload["chunks"]
    assert len(cloud_worker.batches) == 1
    assert collect_cloud(*execution, drain_seconds=0)["uploaded_batches"] == 0


def test_concurrent_hooks_upload_each_range_once(
    execution: tuple[Path, dict[str, JsonValue]], cloud_worker: _CloudWorker
) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(collect_cloud, *execution, drain_seconds=0) for _ in range(2)]
        outcomes = [future.result(timeout=15) for future in futures]
    assert sorted(outcome["uploaded_batches"] for outcome in outcomes) == [0, 1]
    assert len(_upload_requests(cloud_worker)) == 1
    assert len(cloud_worker.leases) == 1


@pytest.mark.parametrize("path", ["/v0/cloud-enrollment/leases", "/v0/host-enrollment/policy", "/v0/traces/batches"])
def test_revoked_auth_stops_collection_without_advancing_ledger(
    execution: tuple[Path, dict[str, JsonValue]], cloud_worker: _CloudWorker, path: str
) -> None:
    cloud_worker.revoked_path = path
    with pytest.raises(BootstrapAuthError):
        collect_cloud(*execution, drain_seconds=0)
    root = _state_root(execution)
    assert not list(root.glob("ledger.*.json"))
    status = json.loads((root / "status.json").read_text())
    assert status["status"] == "partial"
    assert status["error_type"] == "BootstrapAuthError"
    assert _GRANT not in json.dumps(status)


@pytest.mark.parametrize("transport", ["proxy", "secret"])
def test_transport_sends_independent_producer_header(
    execution: tuple[Path, dict[str, JsonValue]],
    cloud_worker: _CloudWorker,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    _write_config(execution[0], cloud_worker.url, transport=transport)
    if transport == "proxy":
        monkeypatch.delenv("PROMPTLESS_CLOUD_GRANT")
    collect_cloud(*execution, drain_seconds=0)
    assert len(cloud_worker.requests) == 3
    for request in cloud_worker.requests:
        assert request.authorization == (f"Bearer {_GRANT}" if transport == "secret" else None)
        assert request.producer is not None and request.producer.startswith("plicl_")


def test_proxy_rejects_sandbox_grants_and_secret_mode_requires_exactly_one(
    execution: tuple[Path, dict[str, JsonValue]],
    cloud_worker: _CloudWorker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_config(execution[0], cloud_worker.url, transport="proxy")
    with pytest.raises(BootstrapError, match="not in the sandbox"):
        _grant_secret(CloudConfig.load(execution[0]))
    _write_config(execution[0], cloud_worker.url)
    secret_file = tmp_path / "grant-secret"
    secret_file.write_text(_GRANT + "\n")
    monkeypatch.setenv("PROMPTLESS_CLOUD_GRANT_FILE", str(secret_file))
    with pytest.raises(BootstrapError, match="exactly one"):
        _grant_secret(CloudConfig.load(execution[0]))
    monkeypatch.delenv("PROMPTLESS_CLOUD_GRANT")
    assert _grant_secret(CloudConfig.load(execution[0])) == _GRANT
    monkeypatch.delenv("PROMPTLESS_CLOUD_GRANT_FILE")
    with pytest.raises(BootstrapError, match="exactly one"):
        _grant_secret(CloudConfig.load(execution[0]))


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_https_redirect_never_forwards_cloud_credentials(cloud_worker: _CloudWorker, code: int, method: str) -> None:
    cloud_worker.redirect_code = code
    cloud_worker.redirect_url = cloud_worker.url.replace("127.0.0.1", "localhost") + "/credential-sink"
    # A changed hostname is a different origin, even with the same local TLS endpoint.
    with pytest.raises(BootstrapError, match="does not follow HTTP redirects"):
        if method == "GET":
            _get_json(cloud_worker.url + "/redirect", _GRANT, label="redirect", producer_token="producer")
        else:
            _post_json_response(cloud_worker.url + "/redirect", _GRANT, {}, label="redirect", producer_token="producer")
    assert len(cloud_worker.requests) == 1
    assert cloud_worker.requests[0].path == "/redirect"


def test_cloud_transport_rejects_cleartext_before_request() -> None:
    with pytest.raises(BootstrapError, match="requires HTTPS"):
        _post_json_response("http://127.0.0.1:1/", _GRANT, {}, label="unsafe", producer_token="producer")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://user:password@example.test",
        "https://example.test/path",
        "https://example.test?token=secret",
        "https://example.test#fragment",
    ],
)
def test_cloud_config_rejects_unsafe_origin(tmp_path: Path, url: str) -> None:
    path = tmp_path / "config.json"
    _write_config(path, url)
    with pytest.raises(BootstrapError, match="HTTPS origin"):
        CloudConfig.load(path)


@pytest.mark.parametrize(
    "field,value",
    [("execution_id", "other"), ("target", "codex"), ("provider", "other"), ("expires_at", "2000-01-01T00:00:00Z")],
)
def test_rejects_wrong_or_expired_lease_scope(
    execution: tuple[Path, dict[str, JsonValue]], cloud_worker: _CloudWorker, field: str, value: str
) -> None:
    cloud_worker.lease_overrides[field] = value
    with pytest.raises(BootstrapError):
        collect_cloud(*execution, drain_seconds=0)
    assert len(cloud_worker.requests) == 1


def test_rejects_changed_identity_on_renewal(
    execution: tuple[Path, dict[str, JsonValue]], cloud_worker: _CloudWorker
) -> None:
    collect_cloud(*execution, drain_seconds=0)
    before = (_state_root(execution) / "producer.json").read_bytes()
    cloud_worker.lease_overrides["host_instance_id"] = "different-host"
    with pytest.raises(BootstrapError, match="identity changed"):
        collect_cloud(*execution, drain_seconds=0)
    assert (_state_root(execution) / "producer.json").read_bytes() == before
    assert len(_upload_requests(cloud_worker)) == 1


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
    metadata = _load_runtime_metadata(plugin_root, "claude")
    assert metadata.plugin_id == "pig-cloud-capture"
    assert metadata.plugin_version == CLOUD_PLUGIN_VERSION
    assert metadata.toolchain_version != "unknown"
    manifest = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())["managed_runtimes"][0]
    assert manifest["sha256"] == _runtime_bundle_sha256(plugin_root / "runtime")
    hooks = json.loads((plugin_root / "hooks/hooks.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop", "SubagentStop", "SessionEnd"}
    assert all(hook[0]["hooks"][0]["timeout"] == 3 for hook in hooks.values())
    # -S removes installed packages; only the copied stdlib runtime can execute.
    event = tmp_path / "event.json"
    event.write_text(json.dumps(execution[1]))
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
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
        if status.get("error_type") == "BootstrapAuthError" and not tuple(state_root.glob("hook-*.json")):
            break
        time.sleep(0.02)
    assert status["status"] == "partial"
    assert status["error_type"] == "BootstrapAuthError"
    assert not tuple(state_root.glob("hook-*.json"))
    assert len(cloud_worker.requests) == 1
    assert cloud_worker.requests[0].authorization == f"Bearer {_GRANT}"
    assert cloud_worker.requests[0].producer is not None
