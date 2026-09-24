"""Promptless-owned runtime artifacts injected into generated plugins."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from promptless_instruction_hub.config import MANAGED_RUNTIME_MANIFEST_PATH
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, read_json_mapping, write_json
from promptless_instruction_hub.models import PIG_PLUGIN_ID, Harness, HubConfig, PluginDefinition
from promptless_instruction_hub.native_runtime import resolve_native_bundle
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.native_bundle import (
    NativeManifest,
    validate_bundle,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.runtime_config import (
    RUNTIME_CONFIG_NAME,
    RUNTIME_CONFIG_SCHEMA_VERSION,
)

RuntimeStatus = Literal["included"]

HOST_RUNTIME_ID = "host-runtime"
HOST_RUNTIME_ASSET_DIR = "host_enrollment"
HOST_RUNTIME_EXECUTABLE = "promptless-host-runtime"
HOST_RUNTIME_PACKAGE = "promptless_host_runtime"
# Directory (relative to a plugin root) that the runtime bundle is copied into.
# Intentionally not "bin": claude.ai-hosted plugins must not ship a top-level bin/.
HOST_RUNTIME_OUTPUT_DIR = "runtime"
# SessionStart only validates the local runtime, launches a detached enrollment and
# collection supervisor, and emits already-pending notices. Browser, network, trace
# discovery, and ledger work stay off the hook's critical path.
HOST_RUNTIME_SESSION_START_HOOK_TIMEOUT_SECONDS = 30
# Terminal hooks only launch detached collectors. Use a short launcher budget
# for every host/event, within Codex's 3s SessionEnd maximum.
# https://learn.chatgpt.com/docs/hooks#config-shape
HOST_RUNTIME_TERMINAL_HOOK_TIMEOUT_SECONDS = 3
HOST_RUNTIME_CHANNEL = "stable"
HOST_RUNTIME_VERSION = "0.3.0"
MANAGED_RUNTIME_MANIFEST = MANAGED_RUNTIME_MANIFEST_PATH
SUPPORTED_HOST_RUNTIME_TARGETS: tuple[Harness, ...] = ("claude", "codex", "cursor")


@dataclass(frozen=True)
class ManagedRuntimeRecord:
    """Exact managed runtime metadata written into generated plugin output."""

    id: str
    status: RuntimeStatus
    target: Harness
    package_id: str
    plugin_id: str
    plugin_name: str
    plugin_version: str
    toolchain_version: str
    channel: str | None = None
    version: str | None = None
    sha256: str | None = None
    executable: str | None = None
    path: str | None = None
    hook: str | None = None
    source_sha256: str | None = None
    platforms: tuple[str, ...] = ()

    def to_manifest(self) -> dict[str, JsonValue]:
        """Return a deterministic JSON record for manifests and check-in context."""

        data: dict[str, JsonValue] = {
            "id": self.id,
            # Enrollment still requires the v1 source-identity field.
            "package_id": self.package_id,
            "plugin_id": self.plugin_id,
            "plugin_name": self.plugin_name,
            "plugin_version": self.plugin_version,
            "status": self.status,
            "target": self.target,
            "toolchain_version": self.toolchain_version,
        }
        optional_fields: tuple[tuple[str, str | None], ...] = (
            ("channel", self.channel),
            ("version", self.version),
            ("sha256", self.sha256),
            ("executable", self.executable),
            ("path", self.path),
            ("hook", self.hook),
            ("source_sha256", self.source_sha256),
        )
        for key, value in optional_fields:
            if value is not None:
                data[key] = value
        if self.platforms:
            data["platforms"] = list(self.platforms)
        return data


def render_managed_runtimes(
    target_root: Path,
    target: Harness,
    config: HubConfig,
    plugin: PluginDefinition,
) -> tuple[ManagedRuntimeRecord, ...]:
    """Inject managed runtime artifacts when rendering the PIG plugin for a supported host."""

    if not config.trace_ingestion.enabled or plugin.id != PIG_PLUGIN_ID or target not in SUPPORTED_HOST_RUNTIME_TARGETS:
        return ()

    native_manifest = _copy_runtime_bundle(target_root)
    _write_runtime_config(target_root, config)
    _write_host_runtime_hooks(target_root, target)
    record = ManagedRuntimeRecord(
        id=HOST_RUNTIME_ID,
        status="included",
        target=target,
        package_id=plugin.id,
        plugin_id=plugin.id,
        plugin_name=plugin.name,
        plugin_version=config.version,
        toolchain_version=_toolchain_version(),
        channel=HOST_RUNTIME_CHANNEL,
        version=HOST_RUNTIME_VERSION,
        sha256=str(native_manifest["bundle_sha256"]),
        executable=HOST_RUNTIME_EXECUTABLE,
        path=f"{HOST_RUNTIME_OUTPUT_DIR}/{HOST_RUNTIME_EXECUTABLE}",
        hook="hooks/hooks.json",
        source_sha256=str(native_manifest["source_sha256"]),
        platforms=tuple(str(item) for item in native_manifest["platforms"]),
    )
    _write_plugin_manifest(target_root, (record,))
    return (record,)


def _write_runtime_config(target_root: Path, config: HubConfig) -> None:
    """Keep deployment configuration separate from enrollment metadata and code hashes."""

    runtime_config: dict[str, JsonValue] = {"schema_version": RUNTIME_CONFIG_SCHEMA_VERSION}
    for key, value in (
        ("worker_base_url", config.trace_ingestion.worker_base_url),
        ("dashboard_base_url", config.trace_ingestion.dashboard_base_url),
        ("hosted_api_base_url", config.trace_ingestion.hosted_api_base_url),
    ):
        if value is not None:
            runtime_config[key] = value
    write_json(target_root / RUNTIME_CONFIG_NAME, runtime_config)


def _copy_runtime_bundle(target_root: Path) -> NativeManifest:
    source = resolve_native_bundle()
    manifest = validate_bundle(source, complete=False)
    runtime_root = target_root / HOST_RUNTIME_OUTPUT_DIR
    shutil.copytree(source, runtime_root, dirs_exist_ok=True)
    # Keep Git from changing byte-hashed files on publication or checkout. The
    # rule belongs outside the validated runtime inventory and only covers it.
    attributes_path = target_root / ".gitattributes"
    existing = attributes_path.read_bytes() if attributes_path.exists() else b""
    separator = b"\n" if existing and not existing.endswith(b"\n") else b""
    attributes_path.write_bytes(existing + separator + f"/{HOST_RUNTIME_OUTPUT_DIR}/** -text\n".encode())
    return manifest


def _write_host_runtime_hooks(target_root: Path, target: Harness) -> None:
    hook_path = target_root / "hooks/hooks.json"
    hook_config = _existing_hook_config(hook_path)
    hooks = hook_config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        msg = f"{hook_path} field hooks must be a JSON object"
        raise InstructionHubError(msg)
    if target == "cursor":
        hook_config["version"] = 1
        for event, lifecycle in (
            ("sessionStart", "session_start"),
            ("stop", "stop"),
            ("sessionEnd", "session_end"),
            ("subagentStop", "subagent_stop"),
        ):
            entries = hooks.setdefault(event, [])
            if not isinstance(entries, list):
                raise InstructionHubError(f"{hook_path} field hooks.{event} must be an array")
            entries.append(
                {
                    "command": f'"${{CURSOR_PLUGIN_ROOT}}/runtime/cursor-hook.cmd" {lifecycle}',
                    # Frozen startup plus detached spawn; no network/SQLite work here.
                    "timeout": 30 if lifecycle == "session_start" else 3,
                }
            )
        write_json(hook_path, hook_config)
        return
    for event_name in _host_runtime_hook_events():
        event_hooks = hooks.setdefault(event_name, [])
        if not isinstance(event_hooks, list):
            msg = f"{hook_path} field hooks.{event_name} must be a JSON array"
            raise InstructionHubError(msg)
        event_hooks.append(_host_runtime_hook_entry(target, event_name))
    write_json(hook_path, hook_config)


def _existing_hook_config(hook_path: Path) -> dict[str, JsonValue]:
    if not hook_path.exists():
        return {}
    try:
        return read_json_mapping(hook_path)
    except OSError as exc:
        msg = f"failed to read existing hook config at {hook_path}: {exc}"
        raise InstructionHubError(msg) from exc
    except ValueError as exc:
        msg = f"existing hook config at {hook_path} is invalid: {exc}"
        raise InstructionHubError(msg) from exc


def _host_runtime_hook_events() -> tuple[str, ...]:
    return ("SessionStart", "Stop", "SessionEnd", "SubagentStop")


def _host_runtime_hook_entry(target: Harness, event_name: str) -> dict[str, JsonValue]:
    lifecycle = _host_runtime_lifecycle_arg(event_name)
    if event_name == "SessionStart":
        hook_command = _host_runtime_start_hook_command(target, lifecycle=lifecycle)
    else:
        hook_command = _host_runtime_terminal_hook_command(target, lifecycle=lifecycle)
    # Codex and Claude both load plugin-root hooks from hooks/hooks.json. Codex may require
    # the user to trust/review plugin hooks before running these commands.
    # https://developers.openai.com/codex/plugins/build
    # https://docs.anthropic.com/en/docs/claude-code/hooks
    # SessionStart launches detached enrollment, config/check-in reconciliation, and JSONL
    # collection. Terminal lifecycle hooks only launch detached native JSONL uploads.
    hook_entry: dict[str, JsonValue] = {
        "hooks": [
            {
                "type": "command",
                "timeout": _host_runtime_hook_timeout(event_name),
                "statusMessage": (
                    "Checking Promptless host runtime"
                    if event_name == "SessionStart"
                    else "Uploading Promptless traces"
                ),
                **hook_command,
            }
        ],
    }
    if event_name == "SessionStart":
        hook_entry["matcher"] = "startup|resume"
    return hook_entry


def _host_runtime_hook_timeout(event_name: str) -> int:
    if event_name == "SessionStart":
        return HOST_RUNTIME_SESSION_START_HOOK_TIMEOUT_SECONDS
    return HOST_RUNTIME_TERMINAL_HOOK_TIMEOUT_SECONDS


def _native_hook_command(target: Harness, lifecycle: str, *, start: bool) -> dict[str, JsonValue]:
    arguments = (
        ["session-start", "--host", target, "--detach"]
        if start
        else ["collect", "--host", target, "--lifecycle", lifecycle, "--detach", "--quiet"]
    )
    if target == "claude":
        # Claude exec form uses Node/Bun executable resolution on Windows, which
        # appends .exe for this extensionless path. The sibling Unix script has a shebang.
        return {"command": "${CLAUDE_PLUGIN_ROOT}/runtime/promptless-host-runtime", "args": arguments}
    command_args = " ".join(arguments)
    return {
        "command": f'"${{PLUGIN_ROOT}}/runtime/promptless-host-runtime" {command_args}',
        "commandWindows": f'& "${{PLUGIN_ROOT}}/runtime/promptless-host-runtime.exe" {command_args}',
    }


def _host_runtime_start_hook_command(target: Harness, *, lifecycle: str) -> dict[str, JsonValue]:
    return _native_hook_command(target, lifecycle, start=True)


def _host_runtime_terminal_hook_command(target: Harness, *, lifecycle: str) -> dict[str, JsonValue]:
    return _native_hook_command(target, lifecycle, start=False)


def _host_runtime_lifecycle_arg(event_name: str) -> str:
    match event_name:
        case "SessionStart":
            return "session_start"
        case "Stop":
            return "stop"
        case "SessionEnd":
            return "session_end"
        case "SubagentStop":
            return "subagent_stop"
        case _:
            msg = f"unsupported host runtime hook event: {event_name}"
            raise InstructionHubError(msg)


def _write_plugin_manifest(target_root: Path, records: tuple[ManagedRuntimeRecord, ...]) -> None:
    write_json(
        target_root / MANAGED_RUNTIME_MANIFEST,
        {
            "schema_version": 1,
            "managed_runtimes": [record.to_manifest() for record in records],
        },
    )


def _toolchain_version() -> str:
    try:
        return version("promptless-instruction-hub")
    except PackageNotFoundError:
        return "0.0.0+local"
