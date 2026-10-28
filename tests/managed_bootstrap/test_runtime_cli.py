from __future__ import annotations

import json
import subprocess
from pathlib import Path


from tests.config_helpers import enable_trace_ingestion

from promptless_instruction_hub.compiler import build_hub, init_hub


from .helpers import (
    HOST_RUNTIME_BIN,
    _FakeWorkerServer,
    _clean_env,
    _host_state_path,
    _json_mapping,
    _json_string,
    _run_bootstrap,
    _run_runtime_json,
    _runtime_bundle_sha256,
)


def test_host_runtime_requires_subcommand_and_reports_version(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    enable_trace_ingestion(hub_root)
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
    runtime_path = plugin_root / "runtime" / HOST_RUNTIME_BIN
    home = tmp_path / "home"

    missing_command = subprocess.run(
        [str(runtime_path)],
        env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_command.returncode == 2
    assert "expected command:" in missing_command.stderr

    for abbreviated_flag in ("--det", "--super"):
        abbreviated_collect = subprocess.run(
            [str(runtime_path), "collect", "--host", "codex", abbreviated_flag],
            env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root)),
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
        assert abbreviated_collect.returncode == 2

    payload, _ = _run_runtime_json(
        plugin_root,
        ["version", "--json"],
        {"HOME": str(home), "PLUGIN_ROOT": str(plugin_root)},
    )
    assert payload["id"] == "host-runtime"
    assert payload["name"] == HOST_RUNTIME_BIN
    assert payload["version"] == "0.4.0"
    assert payload["channel"] == "stable"
    manifest = json.loads((plugin_root / "hub.managed-runtimes.json").read_text())
    bundle_sha256 = _runtime_bundle_sha256(plugin_root / "runtime")
    assert manifest["managed_runtimes"][0]["sha256"] == bundle_sha256
    native = json.loads((plugin_root / "runtime/native-manifest.json").read_text())
    assert payload["sha256"] in native["files"].values()

    isolated = subprocess.run(
        [str(runtime_path), "version", "--json"],
        cwd=tmp_path,
        env=_clean_env(HOME=str(home), PLUGIN_ROOT=str(plugin_root), PATH="", PYTHONPATH="/nonexistent"),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(isolated.stdout)["sha256"] == payload["sha256"]
    assert isolated.stderr == ""
    assert not list((plugin_root / "runtime").rglob("*.py"))


def test_host_runtime_enroll_status_and_reset_commands(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Promptless")
    enable_trace_ingestion(hub_root)
    build_hub(hub_root)
    plugin_root = hub_root / "dist/codex/pig"
    server = _FakeWorkerServer()
    server.start()
    try:
        home = tmp_path / "home"
        env = {
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PLUGIN_ROOT": str(plugin_root),
            "PROMPTLESS_WORKER_BASE_URL": server.base_url,
        }

        enroll_payload, _ = _run_runtime_json(plugin_root, ["enroll", "--host", "codex"], env)
        assert enroll_payload["status"] == "enrolled"
        assert enroll_payload["host"] == "codex"
        assert enroll_payload["credential_id"] == "22222222-2222-4222-8222-222222222222"
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert len(server.poll_requests) == 1
        assert server.policy_requests == []
        assert server.check_ins == []

        status_payload, _ = _run_runtime_json(
            plugin_root,
            ["status", "--host", "codex"],
            env,
        )
        assert status_payload["status"] == "ok"
        status_state = _json_mapping(status_payload["state"], "status.state")
        status_config = _json_mapping(status_payload["config"], "status.config")
        assert status_state["credential_count"] == 1
        assert status_state["pending_enrollment_count"] == 0
        assert status_config["managed_config_detected"] is False
        assert len(server.session_requests) == 1
        assert len(server.poll_requests) == 1
        assert server.policy_requests == []
        assert server.check_ins == []

        _run_bootstrap(plugin_root, "codex", env)
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert server.policy_requests == ["/v0/host-enrollment/policy?target=codex"]
        assert len(server.check_ins) == 1

        configured_status, _ = _run_runtime_json(
            plugin_root,
            ["status", "--host", "codex"],
            env,
        )
        configured_state = _json_mapping(configured_status["state"], "configured.state")
        configured_config = _json_mapping(configured_status["config"], "configured.config")
        host_instance_id = _json_string(configured_state["host_instance_id"], "host_instance_id")
        assert configured_state["credential_count"] == 1
        assert configured_state["last_seen_plugin_version"] == "0.1.0"
        assert configured_config["managed_config_detected"] is False
        assert len(server.session_requests) == 1
        assert len(server.check_ins) == 1

        reset_payload, _ = _run_runtime_json(
            plugin_root,
            ["reset", "--host", "codex", "--yes"],
            env,
        )
        assert reset_payload == {
            "credentials_removed": 1,
            "host": "codex",
            "pending_enrollments_removed": 0,
            "status": "reset",
        }
        state_after_reset = json.loads(_host_state_path(home).read_text())
        assert state_after_reset["host_instance_id"] == host_instance_id
        assert state_after_reset["last_seen_plugin_versions"] == {"codex": "0.1.0"}
        assert state_after_reset["credentials"] == {}
        assert state_after_reset["pending_enrollments"] == {}
        assert not (home / ".codex/config.toml").exists()
        assert len(server.session_requests) == 1
        assert len(server.check_ins) == 1

        reset_status, _ = _run_runtime_json(
            plugin_root,
            ["status", "--host", "codex"],
            env,
        )
        reset_state = _json_mapping(reset_status["state"], "reset.state")
        reset_config = _json_mapping(reset_status["config"], "reset.config")
        assert reset_state["credential_count"] == 0
        assert reset_state["last_seen_plugin_version"] == "0.1.0"
        assert reset_config["managed_config_detected"] is False
    finally:
        server.stop()
