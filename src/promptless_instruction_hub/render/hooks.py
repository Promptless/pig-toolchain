"""Assemble authored native hook configurations without translating host semantics."""

from __future__ import annotations

import shutil
from pathlib import Path

from promptless_instruction_hub import hook_launcher, hook_runtime
from promptless_instruction_hub.assets import METADATA_FILE
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, copy_tree, read_json_mapping, write_json
from promptless_instruction_hub.hook_bundles import LAUNCHER_NAME, RUNNER_NAME, write_bundle_manifest
from promptless_instruction_hub.hook_definitions import render_hook_definition
from promptless_instruction_hub.managed_runtime import copy_host_runtime_bundle
from promptless_instruction_hub.native_manifest import validate_bundle
from promptless_instruction_hub.models import Harness, LoadedAsset


def render_native_hooks(target_root: Path, target: Harness, assets: list[LoadedAsset]) -> list[str]:
    """Copy supported bundles and merge their selected configs at the host discovery path."""

    selected = sorted(
        (asset for asset in assets if asset.type == "hook" and asset.metadata.support[target].mode == "native"),
        key=lambda asset: asset.ref,
    )
    if not selected:
        return []
    merged: dict[str, JsonValue] = {}
    events: dict[str, JsonValue] = {}
    descriptions: list[str] = []
    hook_root = target_root / "hooks"
    has_config = False
    runtime = None
    if any(asset.metadata.hook is not None for asset in selected):
        copy_host_runtime_bundle(target_root)
        runtime = validate_bundle(target_root / "runtime", complete=False)
        # Manifests pin exact bytes, including Python source and text resources.
        with (target_root / ".gitattributes").open("a", encoding="utf-8") as attributes:
            attributes.write("/hooks/** -text\n")
    for asset in selected:
        if asset.path.is_file() and asset.path.suffix != ".json":
            # Legacy non-JSON native files retain their original paths and contents.
            hook_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asset.path, hook_root / asset.path.name)
            continue
        if asset.metadata.hook is not None:
            source = asset.path / METADATA_FILE
            hook_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(hook_runtime.__file__, hook_root / RUNNER_NAME)
            shutil.copy2(hook_launcher.__file__, hook_root / LAUNCHER_NAME)
            copy_tree(asset.path, hook_root / asset.id, skip_names={METADATA_FILE})
            assert runtime is not None
            digest = write_bundle_manifest(hook_root, asset, target, runtime)
            config = render_hook_definition(asset, target, digest)
        else:
            source = _config_path(asset, target)
            config = _read_config(source)
        has_config = True
        for key, value in config.items():
            if key == "hooks":
                assert isinstance(value, dict)
                for event, entries in value.items():
                    assert isinstance(entries, list)
                    combined = events.setdefault(event, [])
                    assert isinstance(combined, list)
                    combined.extend(entries)
            elif key == "description":
                assert isinstance(value, str)
                if value not in descriptions:
                    descriptions.append(value)
            elif key in merged and merged[key] != value:
                raise InstructionHubError(f"{source}: conflicting hook configuration field {key!r} for {target}")
            else:
                merged[key] = value
        if asset.metadata.hook is not None:
            continue  # Copied and hashed before rendering its command.
        if asset.path.is_dir():
            copy_tree(asset.path, hook_root / asset.id, skip_names={METADATA_FILE})
        else:
            hook_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, hook_root / source.name)
    if has_config:
        merged["hooks"] = events
        if descriptions:
            merged["description"] = "\n".join(descriptions)
        write_json(hook_root / "hooks.json", merged)
    return [asset.id for asset in selected]


def _config_path(asset: LoadedAsset, target: Harness) -> Path:
    if not asset.path.is_dir():
        return asset.path
    specific = asset.path / f"hooks.{target}.json"
    if specific.is_file():
        return specific
    shared = asset.path / "hooks.json"
    if shared.is_file():
        return shared
    raise InstructionHubError(f"{asset.ref}: native {target} bundle requires hooks.{target}.json or hooks.json")


def _read_config(path: Path) -> dict[str, JsonValue]:
    try:
        config = read_json_mapping(path)
    except (OSError, ValueError) as exc:
        raise InstructionHubError(f"{path}: invalid native hook configuration: {exc}") from exc
    events = config.get("hooks")
    if not isinstance(events, dict):
        raise InstructionHubError(f"{path} field hooks must be a JSON object")
    for event, entries in events.items():
        if not event or not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise InstructionHubError(f"{path}: hooks.{event} must be an array of handler objects")
    if "description" in config and not isinstance(config["description"], str):
        raise InstructionHubError(f"{path}: description must be a string")
    return config
