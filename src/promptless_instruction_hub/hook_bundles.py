"""Describe the complete portable hook bundle pinned by a generated command."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from promptless_instruction_hub.fs import JsonValue
from promptless_instruction_hub.models import Harness, LoadedAsset
from promptless_instruction_hub.native_manifest import NativeManifest, binary_path

RUNNER_NAME = "_instruction_hub_hook.py"
LAUNCHER_NAME = "_instruction_hub_hook_launcher.py"


def write_bundle_manifest(hook_root: Path, asset: LoadedAsset, target: Harness, runtime: NativeManifest) -> str:
    spec = asset.metadata.hook
    assert spec is not None
    paths = [
        hook_root / RUNNER_NAME,
        hook_root / LAUNCHER_NAME,
        *(path for path in (hook_root / asset.id).rglob("*") if path.is_file()),
    ]
    files: dict[str, JsonValue] = {
        path.relative_to(hook_root).as_posix(): {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "executable": bool(path.stat().st_mode & 0o111),
        }
        for path in sorted(paths)
    }
    manifest = {
        "schema": 1,
        "host": target,
        "hook": asset.id,
        "entrypoint": f"{asset.id}/{spec.entrypoint}",
        "config": spec.model_dump(mode="json"),
        "files": files,
        "runtimes": {platform: runtime["files"][binary_path(platform)] for platform in runtime["platforms"]},
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    path = hook_root / "_bundles" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return digest
