"""Headless enrollment and bounded native capture for one cloud execution."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import BootstrapError, Host, HostCredential, JsonValue, LifecycleEvent, MAX_STDIN_BYTES, RUNTIME_VERSION
from .metadata import _load_runtime_metadata
from .storage import _load_state, _scoped_ledger_path, _state_file_lock, _write_state
from .traces import _hook_trace_context, _upload_source_paths
from .validation import _datetime_value, _decode_json_object, _requires_newer_bootstrap, _string_value
from .worker import _get_json, _post_json_response, _validate_signed_policy


@dataclass(frozen=True)
class CloudConfig:
    """Public integration configuration; secrets are supplied at execution time."""

    worker_url: str
    provider: str
    target: Host
    transport: str
    integration_id: str

    @classmethod
    def load(cls, path: Path) -> CloudConfig:
        """Read a strict, credential-free capture configuration."""
        value = _load_state(path)
        if set(value) != {"worker_url", "provider", "target", "transport", "integration_id"}:
            raise BootstrapError(
                "Cloud configuration requires worker_url, provider, target, transport, and integration_id"
            )
        url = _required_string(value, "worker_url").rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
        ):
            raise BootstrapError(
                "Cloud worker URL must be an HTTPS origin without credentials, path, query, or fragment"
            )
        provider = _required_string(value, "provider")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", provider) is None:
            raise BootstrapError("Invalid cloud provider")
        target = value.get("target")
        if target not in ("claude", "codex"):
            raise BootstrapError("Cloud capture supports claude and codex native JSONL")
        transport = _required_string(value, "transport")
        if transport not in ("proxy", "secret"):
            raise BootstrapError("Cloud transport must be proxy or secret")
        integration = _required_string(value, "integration_id")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", integration) is None:
            raise BootstrapError("Invalid cloud integration identifier")
        return cls(url, provider, target, transport, integration)


def _required_string(value: dict[str, JsonValue], name: str) -> str:
    result = _string_value(value.get(name))
    if not result:
        raise BootstrapError(f"Cloud state missing {name}")
    return result


def _grant_secret(config: CloudConfig) -> str:
    direct = os.environ.get("PROMPTLESS_CLOUD_GRANT")
    filename = os.environ.get("PROMPTLESS_CLOUD_GRANT_FILE")
    if config.transport == "proxy":
        if direct or filename:
            raise BootstrapError("Proxy transport requires the grant in the connection, not in the sandbox")
        return ""
    if bool(direct) == bool(filename):
        raise BootstrapError("Set exactly one of PROMPTLESS_CLOUD_GRANT or PROMPTLESS_CLOUD_GRANT_FILE")
    token = Path(filename).read_text().strip() if filename else direct
    if token is None or re.fullmatch(r"plicg_[A-Za-z0-9_-]{43}", token) is None:
        raise BootstrapError("Invalid cloud grant secret")
    return token


def _execution_scope(config: CloudConfig, context: dict[str, JsonValue]) -> tuple[Path, Path, str]:
    hook = _hook_trace_context(context)
    if hook.transcript_path is None or not hook.session_id or len(hook.session_id) > 160:
        raise BootstrapError("Cloud capture requires the hook's root session_id and transcript_path")
    main = hook.transcript_path.expanduser().resolve()
    if main.suffix != ".jsonl" or "subagents" in main.parts:
        raise BootstrapError("Cloud capture requires a root native JSONL transcript")
    identity = json.dumps([config.worker_url, config.integration_id, config.provider, config.target, hook.session_id])
    root = Path.home() / ".promptless" / "cloud" / hashlib.sha256(identity.encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root, main, hook.session_id


def _source_paths(main: Path, target: Host) -> tuple[Path, ...]:
    paths = [main] if main.is_file() else []
    if target == "claude":
        child_root = main.with_suffix("") / "subagents"
        # Refuse links outside this execution's native child directory.
        if child_root.resolve() == child_root:
            paths.extend(
                path
                for path in sorted(child_root.glob("agent-*.jsonl"))
                if path.is_file() and path.resolve().parent == child_root
            )
    return tuple(paths)


def _enroll(
    config: CloudConfig, root: Path, main: Path, execution_id: str, grant: str
) -> tuple[HostCredential, dict[str, JsonValue]]:
    state_path = root / "producer.json"
    with _state_file_lock(state_path):
        state = _load_state(state_path)
        if not state:
            state = {"producer_token": f"plicl_{secrets.token_urlsafe(32)}", "transcript_path": str(main)}
            # Save before the first network write so a lost response is retryable.
            _write_state(state_path, state)
        if state.get("transcript_path") != str(main):
            raise BootstrapError("Execution transcript changed; restore its original private state")
        producer = _required_string(state, "producer_token")
        if re.fullmatch(r"plicl_[A-Za-z0-9_-]{43}", producer) is None:
            raise BootstrapError("Invalid saved producer secret")
        lease = _post_json_response(
            f"{config.worker_url}/v0/cloud-enrollment/leases",
            grant,
            {
                "execution_id": execution_id,
                "credential_hash": hashlib.sha256(producer.encode()).hexdigest(),
                "target": config.target,
            },
            label="cloud lease",
            producer_token=producer,
        )
        if (
            lease.get("execution_id") != execution_id
            or lease.get("target") != config.target
            or lease.get("provider") != config.provider
        ):
            raise BootstrapError("Cloud lease scope mismatch")
        expiry = _datetime_value(lease.get("expires_at"), "cloud lease expiry")
        if expiry <= dt.datetime.now(dt.timezone.utc):
            raise BootstrapError("Cloud lease has expired")
        for field in ("id", "grant_id", "host_instance_id", "deployment_instance_id"):
            _required_string(lease, field)
            if field in state and state[field] != lease[field]:
                raise BootstrapError("Cloud identity changed; use a new integration identifier for a new grant")
            state[field] = lease[field]
        _write_state(state_path, state)
    return HostCredential(
        grant, _required_string(lease, "id"), _required_string(lease, "deployment_instance_id"), producer_token=producer
    ), lease


def collect_cloud(
    config_path: Path, context: dict[str, JsonValue], *, drain_seconds: float = 30
) -> dict[str, JsonValue]:
    """Enroll and drain complete native records for the hook execution.

    After enrollment and policy retrieval, watch for ``drain_seconds`` (default
    30, maximum 60), with ten seconds of drain grace and a bounded in-flight
    request. A drained status describes complete records during that window,
    not session completion. Only acknowledged ranges advance the shared ledger.
    """
    if not 0 <= drain_seconds <= 60:
        raise BootstrapError("Cloud drain must be between 0 and 60 seconds")
    config = CloudConfig.load(config_path)
    root, main, execution_id = _execution_scope(config, context)
    status_path = root / "status.json"
    status: dict[str, JsonValue] = {"status": "partial", "execution_id": execution_id, "uploaded_batches": 0}
    _write_state(status_path, status)
    try:
        credential, lease = _enroll(config, root, main, execution_id, _grant_secret(config))
        policy = _validate_signed_policy(
            _get_json(
                f"{config.worker_url}/v0/host-enrollment/policy?target={config.target}",
                credential.value,
                label="cloud policy",
                producer_token=credential.producer_token,
            ),
            config.target,
        )
        if _requires_newer_bootstrap(policy.required_bootstrap_version, RUNTIME_VERSION):
            status.update({"status": "blocked", "reason": "bootstrap_upgrade_required"})
            _write_state(status_path, status)
            return status
        ledger = _scoped_ledger_path(
            root / "ledger.json",
            worker_base_url=config.worker_url,
            deployment_instance_id=_required_string(lease, "deployment_instance_id"),
            host_instance_id=_required_string(lease, "host_instance_id"),
            host=config.target,
        )
        events: dict[str, LifecycleEvent] = {
            "Stop": "stop",
            "SessionEnd": "session_end",
            "SubagentStop": "subagent_stop",
        }
        lifecycle = events.get(_string_value(context.get("hook_event_name")) or "", "session_start")
        deadline = time.monotonic() + drain_seconds
        batches = 0
        unreadable = False
        while True:
            result = _upload_source_paths(
                _source_paths(main, config.target),
                upload_url=f"{config.worker_url}/v0/traces/batches?target={config.target}",
                credential=credential,
                host=config.target,
                metadata=_load_runtime_metadata(config_path.parent, config.target),
                policy=policy,
                lifecycle_event=lifecycle,
                hook_context=_hook_trace_context(context),
                ledger_path=ledger,
                deadline=deadline + 10,
            )
            batches += result[0]
            unreadable = unreadable or bool(result[3])
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0, min(2, deadline - time.monotonic())))
        status.update(
            {
                "status": "partial" if unreadable or not main.is_file() else "drained",
                "uploaded_batches": batches,
                "lease_id": lease["id"],
            }
        )
        _write_state(status_path, status)
        return status
    except (BootstrapError, OSError, ValueError, urllib.error.URLError) as exc:
        # Record only the category: remote responses and paths can contain secrets.
        status["error_type"] = type(exc).__name__
        _write_state(status_path, status)
        raise


def run_cloud_command(config_path: Path, *, detach: bool, event_file: Path | None = None) -> int:
    """Launch a detached hook collector or run one synchronously for diagnostics."""
    if event_file is None:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
        if len(raw) > MAX_STDIN_BYTES:
            raise BootstrapError("Cloud hook input is too large")
        context = _decode_json_object(raw or b"{}", "cloud hook input")
    else:
        context = _load_state(event_file)
    if detach:
        config = CloudConfig.load(config_path)
        root, _, _ = _execution_scope(config, context)
        pending = root / f"hook-{secrets.token_hex(12)}.json"
        _write_state(pending, context)
        executable = Path(__file__).parents[1] / "promptless-host-runtime"
        try:
            with open(os.devnull, "wb") as output:
                subprocess.Popen(
                    [
                        sys.executable,
                        str(executable),
                        "cloud-collect",
                        "--config",
                        str(config_path.resolve()),
                        "--event-file",
                        str(pending),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError:
            pending.unlink(missing_ok=True)
            raise
        return 0
    try:
        result = collect_cloud(config_path, context)
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        if event_file is not None:
            event_file.unlink(missing_ok=True)
