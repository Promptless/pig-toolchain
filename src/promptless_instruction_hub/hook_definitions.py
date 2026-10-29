"""Validate portable hook declarations and render explicit native bindings."""

from __future__ import annotations

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue
from promptless_instruction_hub.hook_runtime import EVENTS
from promptless_instruction_hub.models import Harness, LoadedAsset

PLUGIN_ROOTS = {"claude": "CLAUDE_PLUGIN_ROOT", "codex": "PLUGIN_ROOT", "cursor": "CURSOR_PLUGIN_ROOT"}


def validate_hook_definition(asset: LoadedAsset) -> None:
    spec = asset.metadata.hook
    if spec is None:
        return
    if not asset.path.is_dir():
        raise InstructionHubError(f"{asset.ref}: hook declarations require a directory bundle")
    if not (asset.path / spec.entrypoint).is_file():
        raise InstructionHubError(f"{asset.ref}: hook entrypoint does not exist: {spec.entrypoint}")
    if any(
        (asset.path / name).exists()
        for name in ("hooks.json", *(f"hooks.{target}.json" for target in asset.metadata.support))
    ):
        raise InstructionHubError(f"{asset.ref}: choose a hook declaration or native JSON configuration, not both")
    supported = {target for target, support in asset.metadata.support.items() if support.mode == "native"}
    if set(spec.bindings) != supported:
        raise InstructionHubError(f"{asset.ref}: hook bindings must exactly match targets with native support")
    for target, bindings in spec.bindings.items():
        seen = set()
        for binding in bindings:
            if binding.event not in EVENTS.get(target, {}):
                raise InstructionHubError(
                    f"{asset.ref}: no portable adapter for {target} event {binding.event!r}; use native JSON"
                )
            key = (binding.event, binding.matcher)
            if key in seen:
                raise InstructionHubError(f"{asset.ref}: duplicate {target} hook binding: {binding.event}")
            seen.add(key)


def render_hook_definition(asset: LoadedAsset, target: Harness, digest: str) -> dict[str, JsonValue]:
    spec = asset.metadata.hook
    assert spec is not None
    events: dict[str, JsonValue] = {}
    for binding in spec.bindings[target]:
        action: dict[str, JsonValue] = {**_hook_command(target, digest, "run"), "timeout": spec.timeout}
        if target == "cursor":
            entry = action
        else:
            action["type"] = "command"
            if spec.status_message:
                action["statusMessage"] = spec.status_message
            entry = {"hooks": [action]}
        if binding.matcher:
            entry["matcher"] = binding.matcher
        handlers = events.setdefault(binding.event, [])
        assert isinstance(handlers, list)
        handlers.append(entry)
    # Prepare dependencies before the first authored event, without executing it.
    startup = "sessionStart" if target == "cursor" else "SessionStart"
    prepare: dict[str, JsonValue] = {**_hook_command(target, digest, "prepare"), "timeout": 10}
    if target != "cursor":
        prepare = {"hooks": [{"type": "command", **prepare}]}
    handlers = events.setdefault(startup, [])
    assert isinstance(handlers, list)
    handlers.insert(0, prepare)
    return {"version": 1, "hooks": events} if target == "cursor" else {"hooks": events}


def _hook_command(target: Harness, digest: str, operation: str) -> dict[str, JsonValue]:
    """Bootstrap a pinned executable; all bundle management lives in Go."""
    root = PLUGIN_ROOTS[target]
    data = "${PROMPTLESS_HOOK_DATA_HOME:-${XDG_DATA_HOME:-${USERPROFILE:-$HOME}/.local/share}/promptless/hook-runtime}"
    # The first invocation uses the plugin executable. Subsequent calls use the
    # exact retained executable even if the source cache has disappeared.
    command = (
        f'bundle={digest}; runtime="{data}/v1/bin/$bundle/promptless-host-runtime"; '
        f'[ -x "$runtime" ] || runtime="${{{root}}}/runtime/promptless-host-runtime"; '
        f'if [ -x "$runtime" ]; then exec "$runtime" hook {operation} '
        f'--bundle "$bundle" --source "${{{root}}}/hooks"; '
        "else printf '%s\\n' 'Instruction Hub context hook skipped: runtime unavailable.' >&2; fi"
    )
    result: dict[str, JsonValue] = {"command": command}
    if target == "claude":
        result["shell"] = "bash"
    if target == "codex":
        result["commandWindows"] = (
            f"$bundle='{digest}'; "
            "$data=$env:PROMPTLESS_HOOK_DATA_HOME; if (!$data) { "
            "$base=$env:XDG_DATA_HOME; if (!$base) { $base=Join-Path $env:USERPROFILE '.local/share' }; "
            "$data=Join-Path $base 'promptless/hook-runtime' }; "
            '$runtime=Join-Path $data "v1/bin/$bundle/promptless-host-runtime.exe"; '
            "if (!(Test-Path -LiteralPath $runtime -PathType Leaf)) { "
            '$runtime=Join-Path $env:PLUGIN_ROOT "runtime/promptless-host-runtime.exe" }; '
            "if (Test-Path -LiteralPath $runtime -PathType Leaf) { "
            f'& $runtime hook {operation} --bundle $bundle --source "$env:PLUGIN_ROOT/hooks"; exit $LASTEXITCODE '
            '} else { [Console]::Error.WriteLine("Instruction Hub context hook skipped: runtime unavailable.") }'
        )
    return result
