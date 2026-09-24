from __future__ import annotations

import base64
import json
import os
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.external_credentials import CREDENTIALS_ENV, git_environment
from promptless_instruction_hub.external_plugins import (
    _fetch_revision,
    resolve_external_plugins,
    verify_external_plugins,
)

from .external_helpers import external_definition, make_upstream, write_external
from .helpers import _git, _git_output

USERNAME = "test-git-user"
PASSWORD = "test-private-repository-password"
AUTHORIZATION = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()


@dataclass
class GitServer:
    origin: str
    sha: str
    requests: list[tuple[str, str | None]] = field(default_factory=list)
    redirects: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{self.origin}/allowed.git"


@pytest.fixture
def git_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[GitServer]:
    """Serve real smart Git over verified TLS, including its authentication challenge."""

    openssl = shutil.which("openssl")
    assert openssl is not None, "HTTPS Git tests require openssl"
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)
    monkeypatch.delenv("GIT_ASKPASS", raising=False)
    upstream = tmp_path / "upstream"
    with monkeypatch.context() as upstream_env:
        sha = make_upstream(upstream, upstream_env)
    repositories = tmp_path / "repositories"
    repositories.mkdir()
    _git(tmp_path, "clone", "--bare", str(upstream), str(repositories / "allowed.git"))

    cert, key, config = (tmp_path / name for name in ("server.pem", "server.key", "openssl.cnf"))
    config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=extensions\nprompt=no\n"
        "[dn]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n"
    )
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-config",
            str(config),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("GIT_SSL_CAINFO", str(cert))
    backend = Path(_git_output(tmp_path, "--exec-path").strip()) / "git-http-backend"
    backend_env = {**os.environ, "GIT_PROJECT_ROOT": str(repositories), "GIT_HTTP_EXPORT_ALL": "1"}
    state = GitServer("", sha)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.serve_git()

        def do_POST(self) -> None:
            self.serve_git()

        def log_message(self, format: str, *args: object) -> None:
            pass

        def serve_git(self) -> None:
            path = urlsplit(self.path)
            authorization = self.headers.get("Authorization")
            state.requests.append((path.path, authorization))
            if authorization != AUTHORIZATION:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="private git"')
                self.end_headers()
                return
            if path.path in state.redirects:
                self.send_response(302)
                self.send_header("Location", state.redirects[path.path])
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            result = subprocess.run(
                [str(backend)],
                env={
                    **backend_env,
                    "REQUEST_METHOD": self.command,
                    "PATH_INFO": path.path,
                    "QUERY_STRING": path.query,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                    "REMOTE_USER": USERNAME,
                    "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                },
                input=self.rfile.read(length),
                capture_output=True,
                check=True,
            )
            headers, body = result.stdout.split(b"\r\n\r\n", 1)
            fields = dict(line.decode().split(": ", 1) for line in headers.split(b"\r\n"))
            self.send_response(int(fields.pop("Status", "200 OK").split()[0]))
            for name, value in fields.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    state.origin = f"https://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def set_credentials(monkeypatch: pytest.MonkeyPatch, url: str, password: str = PASSWORD) -> None:
    monkeypatch.setenv(CREDENTIALS_ENV, json.dumps({url: {"username": USERNAME, "password": password}}))


def test_private_upstream_resolves_and_verifies_without_persisting_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_server: GitServer
) -> None:
    set_credentials(monkeypatch, git_server.url)
    hub = tmp_path / "hub"
    init_hub(hub)
    definition = external_definition("latest")
    definition["source"]["url"] = git_server.url
    write_external(hub, definition)

    assert {record["sha"] for record in resolve_external_plugins(hub)} == {git_server.sha}
    assert {record["sha"] for record in verify_external_plugins(hub)} == {git_server.sha}
    build_hub(hub)
    assert any(header == AUTHORIZATION for _, header in git_server.requests)
    for path in hub.rglob("*"):
        if path.is_file():
            assert PASSWORD.encode() not in path.read_bytes()
            assert USERNAME.encode() not in path.read_bytes()
    assert CREDENTIALS_ENV not in git_environment()


@pytest.mark.parametrize("path", ["other.git", "allowed.git/other.git", "allowed.git-other"])
def test_changed_repository_does_not_receive_configured_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_server: GitServer, path: str
) -> None:
    set_credentials(monkeypatch, git_server.url)
    with pytest.raises(InstructionHubError, match="cannot fetch"):
        _fetch_revision(tmp_path / "fetch", f"{git_server.origin}/{path}", git_server.sha)
    assert git_server.requests
    assert all(header is None for _, header in git_server.requests)


def test_url_rewrite_does_not_receive_configured_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_server: GitServer
) -> None:
    set_credentials(monkeypatch, git_server.url)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{git_server.url}/other.git.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", git_server.url)
    with pytest.raises(InstructionHubError, match="cannot fetch"):
        _fetch_revision(tmp_path / "fetch", git_server.url, git_server.sha)
    assert git_server.requests
    assert all(header is None for _, header in git_server.requests)


def test_authenticated_fetch_refuses_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_server: GitServer
) -> None:
    set_credentials(monkeypatch, git_server.url)
    monkeypatch.setenv(
        "GIT_CONFIG_PARAMETERS",
        f"'http.{git_server.url}.followRedirects=true' 'credential.{git_server.url}.useHttpPath=false'",
    )
    # Explicit fetch policy must override a developer's repository-specific setting.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"http.{git_server.url}.followRedirects")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    git_server.redirects["/allowed.git/info/refs"] = f"{git_server.origin}/other.git/info/refs"
    with pytest.raises(InstructionHubError, match="cannot fetch"):
        _fetch_revision(tmp_path / "fetch", git_server.url, git_server.sha)
    assert git_server.requests
    assert {path for path, _ in git_server.requests} == {"/allowed.git/info/refs"}
    assert any(header == AUTHORIZATION for _, header in git_server.requests)


def test_failed_authentication_does_not_report_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_server: GitServer, capsys: pytest.CaptureFixture[str]
) -> None:
    set_credentials(monkeypatch, git_server.url, "wrong-secret-password")
    hub = tmp_path / "hub"
    init_hub(hub)
    definition = external_definition(git_server.sha)
    definition["source"]["url"] = git_server.url
    write_external(hub, definition)
    assert main(["verify-external", "--hub", str(hub)]) == 1
    captured = capsys.readouterr()
    assert "cannot fetch" in captured.err
    assert "wrong-secret-password" not in captured.out + captured.err
    assert USERNAME not in captured.out + captured.err


@pytest.mark.parametrize("explicit_credentials", [False, True])
@pytest.mark.parametrize("scoped_helper", [False, True])
def test_existing_helpers_work_without_explicit_credentials_and_are_not_used_to_store_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_server: GitServer,
    explicit_credentials: bool,
    scoped_helper: bool,
) -> None:
    helper, log = tmp_path / "helper.sh", tmp_path / "helper.log"
    helper.write_text(f'#!/bin/sh\necho "$1" >> "{log}"\nprintf \'username={USERNAME}\\npassword={PASSWORD}\\n\\n\'\n')
    helper.chmod(0o700)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(helper))
    if scoped_helper:
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'credential.{git_server.url}.helper={helper}'")
    if explicit_credentials:
        set_credentials(monkeypatch, git_server.url)
    assert _fetch_revision(tmp_path / "fetch", git_server.url, git_server.sha).sha == git_server.sha
    assert log.exists() is not explicit_credentials
    config = (tmp_path / "fetch/config").read_text()
    assert "credential" not in config and PASSWORD not in config and USERNAME not in config


@pytest.mark.parametrize(
    "raw",
    [
        "not-json-secret",
        '["not-an-object-secret"]',
        '{"https://example.test/repo.git": "secret"}',
        '{"https://example.test/repo.git": {"username": "user", "password": "secret", "other": "secret"}}',
        '{"https://example.test/repo.git": {"username": "user", "password": "secret", "password": "secret2"}}',
        '{"https://example.test/repo.git": {}, "https://example.test/repo.git": {}}',
        json.dumps({"https://example.test/repo.git": {"username": "user", "password": "secret\nusername=oops"}}),
        json.dumps({"https://example.test/repo.git": {"username": "user:secret", "password": "secret"}}),
        json.dumps({"https://secret@example.test/repo.git": {"username": "user", "password": "secret"}}),
        json.dumps({"https://example.test/repo.git/../other.git": {"username": "user", "password": "secret"}}),
        json.dumps({"https://example.test/repo%2fgit": {"username": "user", "password": "secret"}}),
    ],
)
def test_invalid_credentials_fail_without_echoing_input(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(CREDENTIALS_ENV, raw)
    with pytest.raises(InstructionHubError) as error:
        git_environment("https://example.test/repo.git")
    assert CREDENTIALS_ENV in str(error.value)
    assert "secret" not in str(error.value)


def test_authenticated_fetch_disables_git_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.test/repo.git"
    set_credentials(monkeypatch, url)
    monkeypatch.setenv("GIT_TRACE_CURL", "/tmp/trace")
    monkeypatch.setenv("GIT_TRACE_REDACT", "0")
    monkeypatch.setenv("GIT_CURL_VERBOSE", "1")
    env = git_environment(url)
    assert "GIT_TRACE_CURL" not in env and "GIT_CURL_VERBOSE" not in env
    assert env["GIT_TRACE_REDACT"] == "1"


def test_invalid_credentials_report_actionable_cli_error_without_echoing_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    init_hub(tmp_path)
    write_external(tmp_path, external_definition())
    monkeypatch.setenv(CREDENTIALS_ENV, '{"secret-invalid-json')
    assert main(["verify-external", "--hub", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert CREDENTIALS_ENV in captured.err
    assert "secret-invalid-json" not in captured.out + captured.err
