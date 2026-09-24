"""Runtime environment discovery and installed-bundle metadata."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .contracts import (
    BootstrapError,
    DEFAULT_DASHBOARD_BASE_URL,
    DEFAULT_HOSTED_API_BASE_URL,
    DEFAULT_WORKER_BASE_URL,
    Host,
    MANAGED_RUNTIME_ID,
    MANAGED_RUNTIME_MANIFEST,
    RUNTIME_EXECUTABLE,
    RUNTIME_VERSION,
    RuntimeMetadata,
)
from .runtime_config import RUNTIME_CONFIG_NAME, RUNTIME_CONFIG_SCHEMA_VERSION, normalize_https_origin
from .validation import _normalize_base_url, _string_value


def _resolve_host(host_arg: str) -> Host:
    if host_arg in ("codex", "claude", "claude-desktop", "cursor"):
        return host_arg
    if os.environ.get("CURSOR_PLUGIN_ROOT"):
        return "cursor"
    if os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "claude"
    return "codex"


def _plugin_root() -> Path | None:
    raw_root = (
        os.environ.get("CURSOR_PLUGIN_ROOT") or os.environ.get("PLUGIN_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT")
    )
    if raw_root is None or raw_root.strip() == "":
        # Direct CLI invocations do not receive a host's plugin-root variable.
        # Only infer our own installed layout; never discover config from cwd.
        package_root = Path(__file__).resolve().parent
        bundle_root = package_root.parent
        candidate = bundle_root.parent
        markers = (candidate / RUNTIME_CONFIG_NAME, candidate / MANAGED_RUNTIME_MANIFEST)
        if (
            package_root.name == "promptless_host_runtime"
            and bundle_root.name == "runtime"
            and (bundle_root / RUNTIME_EXECUTABLE).is_file()
            and any(path.exists() or path.is_symlink() for path in markers)
        ):
            return candidate
        return None
    return Path(raw_root).expanduser()


def _worker_base_url() -> str:
    config = _load_runtime_config(_plugin_root())
    return _normalize_base_url(
        os.environ.get("PROMPTLESS_WORKER_BASE_URL") or config.get("worker_base_url") or DEFAULT_WORKER_BASE_URL,
        label="worker base URL",
    )


def _dashboard_base_url() -> str:
    config = _load_runtime_config(_plugin_root())
    return _normalize_base_url(
        os.environ.get("PROMPTLESS_DASHBOARD_BASE_URL")
        or config.get("dashboard_base_url")
        or DEFAULT_DASHBOARD_BASE_URL,
        label="dashboard base URL",
    )


def _hosted_api_base_url() -> str:
    config = _load_runtime_config(_plugin_root())
    return _normalize_base_url(
        os.environ.get("PROMPTLESS_HOSTED_API_BASE_URL")
        or config.get("hosted_api_base_url")
        or DEFAULT_HOSTED_API_BASE_URL,
        label="hosted API base URL",
    )


def _runtime_config_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    config: dict[str, object] = {}
    for key, value in pairs:
        if key in config:
            raise BootstrapError(f"{RUNTIME_CONFIG_NAME} must not contain duplicate keys")
        config[key] = value
    return config


def _load_runtime_config(plugin_root: Path | None) -> dict[str, str]:
    """Reject malformed installed configuration instead of using hosted defaults."""

    if plugin_root is None:
        return {}
    config_path = plugin_root / RUNTIME_CONFIG_NAME
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if config_path.is_symlink():
            raise BootstrapError(f"{RUNTIME_CONFIG_NAME} could not be read") from exc
        return {}
    except (OSError, UnicodeError) as exc:
        raise BootstrapError(f"{RUNTIME_CONFIG_NAME} could not be read") from exc
    try:
        config = json.loads(config_text, object_pairs_hook=_runtime_config_object)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{RUNTIME_CONFIG_NAME} must contain valid JSON") from exc
    if not isinstance(config, dict):
        raise BootstrapError(f"{RUNTIME_CONFIG_NAME} must contain a JSON object")
    allowed_keys = {"schema_version", "worker_base_url", "dashboard_base_url", "hosted_api_base_url"}
    if set(config) - allowed_keys:
        raise BootstrapError(f"{RUNTIME_CONFIG_NAME} contains unsupported fields")
    if type(config.get("schema_version")) is not int or config["schema_version"] != RUNTIME_CONFIG_SCHEMA_VERSION:
        raise BootstrapError(f"{RUNTIME_CONFIG_NAME} requires schema_version {RUNTIME_CONFIG_SCHEMA_VERSION}")
    endpoints: dict[str, str] = {}
    for key in ("worker_base_url", "dashboard_base_url", "hosted_api_base_url"):
        if key not in config:
            continue
        value = config[key]
        if not isinstance(value, str):
            raise BootstrapError(f"{RUNTIME_CONFIG_NAME} {key} must be a string")
        try:
            endpoints[key] = normalize_https_origin(value)
        except ValueError as exc:
            raise BootstrapError(f"{RUNTIME_CONFIG_NAME} {key} {exc}") from exc
    return endpoints


def _load_runtime_metadata(plugin_root: Path | None, host: Host) -> RuntimeMetadata:
    defaults = RuntimeMetadata(
        bootstrap_version=RUNTIME_VERSION,
        toolchain_version="unknown",
        plugin_id="unknown",
        plugin_name="unknown",
        plugin_version="unknown",
        package_id="unknown",
        target=host,
    )
    if plugin_root is None:
        return defaults
    manifest_path = plugin_root / MANAGED_RUNTIME_MANIFEST
    if not manifest_path.exists():
        return defaults
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(manifest, dict):
        return defaults
    runtimes = manifest.get("managed_runtimes")
    if not isinstance(runtimes, list):
        return defaults
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            continue
        if runtime.get("id") != MANAGED_RUNTIME_ID or runtime.get("status") != "included":
            continue
        return RuntimeMetadata(
            bootstrap_version=RUNTIME_VERSION,
            toolchain_version=_string_value(runtime.get("toolchain_version")) or "unknown",
            plugin_id=_string_value(runtime.get("plugin_id")) or "unknown",
            plugin_name=_string_value(runtime.get("plugin_name")) or "unknown",
            plugin_version=_string_value(runtime.get("plugin_version")) or "unknown",
            package_id=_string_value(runtime.get("package_id")) or "unknown",
            target=host,
        )
    return defaults


def _self_sha256() -> str:
    package_root = Path(__file__).resolve().parent
    bundle_root = package_root.parent
    files = [bundle_root / RUNTIME_EXECUTABLE, bundle_root / "cursor-hook.cjs"]
    files.extend(
        path
        for path in package_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(bundle_root).parts and path.suffix != ".pyc"
    )

    digest = hashlib.sha256()
    for path in sorted(files, key=lambda candidate: candidate.relative_to(bundle_root).as_posix()):
        relative_path = path.relative_to(bundle_root).as_posix()
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
