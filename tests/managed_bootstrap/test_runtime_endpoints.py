from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.fs import read_yaml_mapping, write_yaml

from .helpers import HOST_RUNTIME_BIN, _FakeWorkerHandler, _FakeWorkerServer, _clean_env, _host_state_path


def _build_plugin(hub: Path, *, worker: str | None = None, dashboard: str | None = None, host: str = "codex") -> Path:
    init_hub(hub, org="Acme")
    config = read_yaml_mapping(hub / "hub.yaml")
    config["trace_ingestion"] = {"enabled": True, "worker_base_url": worker, "dashboard_base_url": dashboard}
    write_yaml(hub / "hub.yaml", config)
    build_hub(hub)
    return hub / "dist" / host / "pig"


def _bundled_endpoints(plugin: Path, *, cwd: Path, overrides: dict[str, str]) -> dict[str, str]:
    # Import the generated stdlib-only bundle in an isolated interpreter, independent of the installed toolchain.
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "from promptless_host_runtime.metadata import _worker_base_url, _dashboard_base_url, _plugin_root; "
        "print(json.dumps({'worker': _worker_base_url(), 'dashboard': _dashboard_base_url(), "
        "'plugin_root': str(_plugin_root())}))"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", script, str(plugin / "runtime")],
        cwd=cwd,
        env=_clean_env(**overrides),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("worker_env", "dashboard_env", "worker_expected", "dashboard_expected"),
    [
        (None, None, "https://worker.customer.example", "https://dashboard.customer.example"),
        (
            "https://worker.override.example/",
            None,
            "https://worker.override.example",
            "https://dashboard.customer.example",
        ),
        (
            None,
            "https://dashboard.override.example/",
            "https://worker.customer.example",
            "https://dashboard.override.example",
        ),
        (
            "https://worker.override.example",
            "https://dashboard.override.example",
            "https://worker.override.example",
            "https://dashboard.override.example",
        ),
    ],
)
def test_each_environment_origin_overrides_only_its_packaged_value(
    tmp_path: Path,
    worker_env: str | None,
    dashboard_env: str | None,
    worker_expected: str,
    dashboard_expected: str,
) -> None:
    plugin = _build_plugin(
        tmp_path / "hub", worker="https://worker.customer.example", dashboard="https://dashboard.customer.example"
    )
    overrides = {"HOME": str(tmp_path / "home")}
    # Avoid _clean_env's test convenience that couples a worker override to the dashboard.
    overrides["PROMPTLESS_DASHBOARD_BASE_URL"] = dashboard_env or ""
    if worker_env is not None:
        overrides["PROMPTLESS_WORKER_BASE_URL"] = worker_env
    endpoints = _bundled_endpoints(plugin, cwd=tmp_path, overrides=overrides)
    assert endpoints == {"worker": worker_expected, "dashboard": dashboard_expected, "plugin_root": str(plugin)}


@pytest.mark.parametrize(
    ("worker", "dashboard", "remove_config"),
    [
        (None, None, False),
        ("https://customer.example", None, False),
        (None, "https://customer.example", False),
        (None, None, True),
    ],
)
def test_missing_packaged_origin_keeps_existing_default(
    tmp_path: Path, worker: str | None, dashboard: str | None, remove_config: bool
) -> None:
    plugin = _build_plugin(tmp_path / "hub", worker=worker, dashboard=dashboard)
    if remove_config:
        (plugin / "hub.runtime-config.json").unlink()
    endpoints = _bundled_endpoints(plugin, cwd=tmp_path, overrides={"HOME": str(tmp_path / "home")})
    assert endpoints["worker"] == (worker or "https://pig.promptless.ai")
    assert endpoints["dashboard"] == (dashboard or "https://app.gopromptless.ai")
    assert endpoints["plugin_root"] == str(plugin)


@pytest.mark.parametrize(
    "contents",
    [
        "{broken-json",
        "[]",
        "null",
        "{}",
        '{"schema_version": 2}',
        '{"schema_version": true}',
        '{"schema_version": 1, "unknown": "literal-secret"}',
        '{"schema_version": 1, "worker_base_url": null}',
        '{"schema_version": 1, "dashboard_base_url": 42}',
        '{"schema_version": 1, "worker_base_url": "http://127.0.0.1:8080"}',
        '{"schema_version": 1, "worker_base_url": "https://user:literal-secret@worker.example"}',
        '{"schema_version": 1, "dashboard_base_url": "https://worker.example?token=literal-secret"}',
        '{"schema_version": 1, "schema_version": 1}',
    ],
)
def test_invalid_present_config_fails_cli_even_with_environment_overrides(tmp_path: Path, contents: str) -> None:
    plugin = _build_plugin(tmp_path / "hub")
    (plugin / "hub.runtime-config.json").write_text(contents)
    _assert_enrollment_config_error(plugin, tmp_path)


@pytest.mark.parametrize("invalid_file", ["directory", "invalid_utf8", "dangling_symlink"])
def test_unreadable_present_config_does_not_fall_back_to_defaults(tmp_path: Path, invalid_file: str) -> None:
    plugin = _build_plugin(tmp_path / "hub")
    config = plugin / "hub.runtime-config.json"
    config.unlink()
    if invalid_file == "directory":
        config.mkdir()
    elif invalid_file == "invalid_utf8":
        config.write_bytes(b"\xff\xfeinvalid")
    else:
        config.symlink_to(tmp_path / "missing-config.json")
    _assert_enrollment_config_error(plugin, tmp_path)


def _assert_enrollment_config_error(plugin: Path, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-S", str(plugin / "runtime" / HOST_RUNTIME_BIN), "enroll", "--host", "codex"],
        cwd=tmp_path,
        env=_clean_env(
            HOME=str(tmp_path / "home"),
            PROMPTLESS_WORKER_BASE_URL="http://127.0.0.1:1",
            PROMPTLESS_DASHBOARD_BASE_URL="http://127.0.0.1:1",
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "hub.runtime-config.json" in payload["message"]
    assert "literal-secret" not in result.stdout
    assert not _host_state_path(tmp_path / "home").exists()


@pytest.mark.parametrize("host", ["claude", "codex", "cursor"])
def test_direct_installed_cli_enrolls_against_packaged_https_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("the HTTPS enrollment fixture needs openssl to generate a local certificate")
    certificate = tmp_path / "certificate.pem"
    private_key = tmp_path / "private-key.pem"
    cert_config = tmp_path / "certificate.cnf"
    cert_config.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=extensions\nprompt=no\n"
        "[dn]\nCN=localhost\n"
        "[extensions]\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n"
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
            "-config",
            str(cert_config),
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    worker = _FakeWorkerServer()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    worker._server.socket = tls.wrap_socket(worker._server.socket, server_side=True)
    worker.base_url = worker.base_url.replace("http://", "https://")
    monkeypatch.setattr(_FakeWorkerHandler, "_base_url", lambda self: worker.base_url)
    plugin = _build_plugin(tmp_path / "hub", worker=worker.base_url, dashboard=worker.base_url, host=host)
    installed = tmp_path / "installed-plugin"
    shutil.copytree(plugin, installed)
    shutil.rmtree(tmp_path / "hub")
    unrelated = tmp_path / "unrelated-project"
    unrelated.mkdir()
    (unrelated / "hub.runtime-config.json").write_text(
        '{"schema_version":1,"worker_base_url":"https://wrong-project.invalid"}'
    )
    home = tmp_path / "home"
    worker.start()
    try:
        result = subprocess.run(
            [sys.executable, "-S", str(installed / "runtime" / HOST_RUNTIME_BIN), "enroll", "--host", host],
            cwd=unrelated,
            env=_clean_env(HOME=str(home), SSL_CERT_FILE=str(certificate), PYTHONPATH=""),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        assert json.loads(result.stdout)["status"] == "enrolled"
        assert len(worker.session_requests) == len(worker.poll_requests) == 1
        request = worker.session_requests[0]
        assert request["target"] == host
        assert request["plugin_id"] == request["package_id"] == "pig"
        assert request["plugin_version"] == "0.1.0"
        state = json.loads(_host_state_path(home).read_text())
        credential = next(iter(state["credentials"].values()))
        assert credential["worker_base_url"] == worker.base_url
        assert "plihost_localcredential" not in result.stdout + result.stderr
        assert "plihenroll_devicecode" not in result.stdout + result.stderr
        packaged = json.loads((installed / "hub.runtime-config.json").read_text())
        assert packaged == {
            "schema_version": 1,
            "worker_base_url": worker.base_url,
            "dashboard_base_url": worker.base_url,
        }
    finally:
        worker.stop()
