from __future__ import annotations

import datetime as dt
import json
import subprocess
import threading
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import (
    cli,
    device_enrollment,
    enrollment,
    metadata,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    BootstrapError,
    JsonValue,
)
from tests.config_helpers import enable_trace_ingestion

from .helpers import HOST_RUNTIME_BIN, _FakeWorkerServer, _clean_env, _host_state_path, _run_bootstrap


class DeviceAPI:
    """Independent hosted API and browser approval surface for client tests."""

    def __init__(self) -> None:
        self.creations: list[dict[str, JsonValue]] = []
        self.polls: list[str] = []
        self.approved = False
        self.overrides: dict[str, JsonValue] = {}
        self.status = 200
        self.redirect = ""
        self.poll_redirect = ""
        self.redirect_hits = 0
        self.proof = "plihenroll_devicecode"
        self.approval_token = "plihenroll_approval"
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def respond(self, payload: dict[str, JsonValue], status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == f"/instruction-hub/enroll?approval_token={api.approval_token}":
                    api.approved = True
                    self.respond({"status": "approved"})
                else:
                    api.redirect_hits += 1
                    self.respond({}, 400)

            def do_POST(self) -> None:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/device-sessions"):
                    assert self.headers.get("Authorization") is None
                    api.creations.append(payload)
                    if api.redirect:
                        self.send_response(api.status)
                        self.send_header("Location", api.redirect)
                        self.end_headers()
                        return
                    self.respond(
                        {
                            "session_id": str(uuid.uuid4()),
                            "device_code": api.proof,
                            "approval_url": f"{api.base_url}/instruction-hub/enroll?approval_token={api.approval_token}",
                            "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat(),
                            "poll_interval_seconds": 1,
                            **api.overrides,
                        },
                        api.status,
                    )
                elif self.path.endswith("/poll"):
                    assert payload == {"device_code": api.proof}
                    api.polls.append(self.path)
                    if api.poll_redirect:
                        self.send_response(307)
                        self.send_header("Location", api.poll_redirect)
                        self.end_headers()
                        return
                    self.respond(
                        {"status": "approved", "host_credential": "plihost_localcredential", "credential_id": "host-1"}
                        if api.approved
                        else {"status": "pending"}
                    )
                else:
                    api.redirect_hits += 1
                    self.respond({}, 400)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@dataclass
class DeviceHost:
    home: Path
    plugin: Path
    env: dict[str, str]
    api: DeviceAPI
    worker: _FakeWorkerServer


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DeviceHost]:
    hub = tmp_path / "hub"
    init_hub(hub, org="Acme")
    enable_trace_ingestion(hub)
    build_hub(hub)
    plugin = hub / "dist/codex/pig"
    home = tmp_path / "home"
    api = DeviceAPI()
    worker = _FakeWorkerServer()
    worker.start()
    env = _clean_env(
        HOME=str(home),
        CODEX_HOME=str(home / ".codex"),
        PLUGIN_ROOT=str(plugin),
        PROMPTLESS_WORKER_BASE_URL=worker.base_url,
        PROMPTLESS_DASHBOARD_BASE_URL=api.base_url,
        PROMPTLESS_HOSTED_API_BASE_URL=api.base_url,
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(enrollment, "ENROLLMENT_POLL_DEADLINE_SECONDS", 0)

    def no_browser(*args: object, **kwargs: object) -> None:
        raise AssertionError("device enrollment must not use a local browser or callback server")

    monkeypatch.setattr(enrollment, "_open_hosted_enrollment_url", no_browser)
    monkeypatch.setattr(enrollment, "_EnrollmentCallbackServer", no_browser)
    try:
        yield DeviceHost(home, plugin, env, api, worker)
    finally:
        api.close()
        worker.stop()


def _pending(host: DeviceHost) -> dict[str, JsonValue]:
    state = json.loads(_host_state_path(host.home).read_text())
    return next(iter(state["pending_enrollments"].values()))


def test_device_link_is_persisted_before_printing_and_works_from_another_device(host: DeviceHost) -> None:
    process = subprocess.Popen(
        [str(host.plugin / "runtime" / HOST_RUNTIME_BIN), "enroll", "--host", "codex", "--device"],
        env=host.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        first_line = process.stderr.readline()
        approval_url = process.stderr.readline().strip()
        assert "another device" in first_line or "a device with a browser" in first_line
        pending = _pending(host)
        assert pending["approval_url"] == approval_url
        assert pending["hosted_api_base_url"] == host.api.base_url
        with urllib.request.urlopen(approval_url, timeout=5) as response:
            assert response.status == 200
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stdout + stderr
        assert json.loads(stdout)["status"] == "enrolled"
        assert host.api.proof not in first_line + approval_url + stdout + stderr
        assert "plihost_localcredential" not in stdout + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert len(host.api.creations) == 1
    assert host.api.creations[0]["plugin_id"] == "pig"
    assert host.api.creations[0]["target"] == "codex"
    assert host.worker.session_requests == []
    payload, _ = _run_bootstrap(host.plugin, "codex", host.env)
    assert payload["status"] == "configured"
    assert len(host.worker.check_ins) == 1
    assert host.worker.policy_requests == ["/v0/host-enrollment/policy?target=codex"]


def test_pending_device_enrollment_resumes_without_recreation_or_expiry_extension(
    host: DeviceHost, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 0
    first_output = capsys.readouterr()
    assert json.loads(first_output.out) == {"status": "setup_pending", "reason": "approval_pending", "host": "codex"}
    before = _pending(host)
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 0
    output = capsys.readouterr()
    assert before == _pending(host)
    assert before["approval_url"] in output.err
    assert len(host.api.creations) == 1
    host.api.approved = True
    # An ordinary hook can resume this same session without opening a browser.
    assert cli.main(["enroll", "--host", "codex"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "enrolled"
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 0
    assert capsys.readouterr().err == ""
    assert len(host.api.creations) == 1
    assert json.loads(_host_state_path(host.home).read_text())["pending_enrollments"] == {}


def test_expired_device_session_is_replaced(host: DeviceHost, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["enroll", "--host", "codex", "--device"])
    capsys.readouterr()
    state_path = _host_state_path(host.home)
    state = json.loads(state_path.read_text())
    session = next(iter(state["pending_enrollments"].values()))
    old_id = session["session_id"]
    session["expires_at"] = "2020-01-01T00:00:00+00:00"
    state_path.write_text(json.dumps(state))
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 0
    assert len(host.api.creations) == 2
    assert _pending(host)["session_id"] != old_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "../../elsewhere"),
        ("device_code", ""),
        ("approval_url", "https://untrusted.example/instruction-hub/enroll"),
        ("approval_url", "http://localhost:1/instruction-hub/enroll"),
        ("expires_at", "2020-01-01T00:00:00Z"),
        ("expires_at", "2100-01-01T00:00:00Z"),
        ("poll_interval_seconds", 0),
        ("poll_interval_seconds", 31),
        ("poll_interval_seconds", True),
    ],
)
def test_invalid_device_response_is_not_persisted(host: DeviceHost, field: str, value: JsonValue) -> None:
    host.api.overrides[field] = value
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 1
    state = json.loads(_host_state_path(host.home).read_text())
    assert not state.get("pending_enrollments")
    assert host.api.polls == []


def test_device_session_accepts_clock_skew_without_renewing_expiry(host: DeviceHost) -> None:
    server_expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15, seconds=5)).isoformat()
    host.api.overrides["expires_at"] = server_expiry
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 0
    assert _pending(host)["expires_at"] == server_expiry
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 0
    assert len(host.api.creations) == 1
    assert _pending(host)["expires_at"] == server_expiry


@pytest.mark.parametrize("control", ["\n", "\t", "\x1b", "\x9b"])
def test_device_approval_link_rejects_terminal_control_characters(host: DeviceHost, control: str) -> None:
    host.api.overrides["approval_url"] = host.api.base_url + "/instruction-hub/enroll?approval_token=" + control
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 1
    assert not json.loads(_host_state_path(host.home).read_text()).get("pending_enrollments")


def test_malformed_saved_device_endpoints_do_not_downgrade_to_browser_polling(host: DeviceHost) -> None:
    cli.main(["enroll", "--host", "codex", "--device"])
    count = len(host.api.polls)
    state_path = _host_state_path(host.home)
    state = json.loads(state_path.read_text())
    pending = next(iter(state["pending_enrollments"].values()))
    pending["hosted_api_base_url"] = None
    pending["approval_url"] = None
    state_path.write_text(json.dumps(state))
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 1
    assert len(host.api.polls) == count


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_device_initiation_never_follows_redirects(host: DeviceHost, status: int) -> None:
    host.api.status = status
    host.api.redirect = host.api.base_url + "/steal"
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 1
    assert host.api.redirect_hits == 0


def test_device_poll_never_follows_redirects(host: DeviceHost) -> None:
    host.api.poll_redirect = host.api.base_url + "/steal"
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 1
    assert host.api.redirect_hits == 0


@pytest.mark.parametrize("status", [404, 405, 429])
def test_device_api_reports_actionable_errors(
    host: DeviceHost, status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    host.api.status = status
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 1
    message = json.loads(capsys.readouterr().out)["message"]
    assert ("wait before retrying" if status == 429 else "deploy the device-enrollment API") in message


def test_changed_api_origin_does_not_receive_pending_device_proof(
    host: DeviceHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.main(["enroll", "--host", "codex", "--device"])
    count = len(host.api.polls)
    monkeypatch.setenv("PROMPTLESS_HOSTED_API_BASE_URL", host.worker.base_url)
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 1
    assert len(host.api.polls) == count
    assert host.worker.poll_requests == []


def test_pending_poll_url_cannot_redirect_proof_to_another_path(host: DeviceHost) -> None:
    cli.main(["enroll", "--host", "codex", "--device"])
    state_path = _host_state_path(host.home)
    state = json.loads(state_path.read_text())
    next(iter(state["pending_enrollments"].values()))["poll_url"] = host.api.base_url + "/steal"
    state_path.write_text(json.dumps(state))
    assert cli.main(["enroll", "--host", "codex", "--device"]) == 1
    assert host.api.redirect_hits == 0


def test_concurrent_enrollment_defers_under_existing_leader_lock(host: DeviceHost) -> None:
    context = enrollment._enrollment_context(
        host.worker.base_url, host.api.base_url, metadata._load_runtime_metadata(host.plugin, "codex")
    )
    with enrollment._enrollment_leader_lock(context, _host_state_path(host.home)) as leader:
        assert leader
        result = device_enrollment._obtain_device_host_credential(context)
    assert result.reason == "enrollment_in_progress"
    assert host.api.creations == []


def test_hosted_api_configuration_precedence(host: DeviceHost, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = host.plugin / "hub.runtime-config.json"
    config_path.write_text('{"schema_version":1,"hosted_api_base_url":"https://customer.example/"}')
    assert metadata._hosted_api_base_url() == host.api.base_url
    monkeypatch.delenv("PROMPTLESS_HOSTED_API_BASE_URL")
    assert metadata._hosted_api_base_url() == "https://customer.example"
    config_path.write_text('{"schema_version":1}')
    assert metadata._hosted_api_base_url() == "https://api.gopromptless.ai"
    config_path.write_text('{"schema_version":1,"hosted_api_base_url":"http://unsafe.example"}')
    with pytest.raises(BootstrapError):
        metadata._hosted_api_base_url()
