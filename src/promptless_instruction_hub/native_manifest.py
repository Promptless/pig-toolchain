"""Validate the fixed inventory of a source-matched Go runtime release."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import TypedDict

PLATFORMS = ("darwin-amd64", "darwin-arm64", "linux-amd64", "linux-arm64", "windows-amd64", "windows-arm64")
MANIFEST = "native-manifest.json"
LAUNCHERS = ("promptless-host-runtime", "cursor-hook.cmd")


class NativeManifest(TypedDict):
    schema_version: int
    source_sha256: str
    bundle_sha256: str
    platforms: list[str]
    files: dict[str, str]


def safe_relative_path(value: str) -> bool:
    return (
        bool(value)
        and "\\" not in value
        and ":" not in value
        and not value.startswith("/")
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )


def binary_path(platform: str) -> str:
    # Windows executable resolution uses the root x64 binary (also supported by
    # Windows ARM64 emulation); the native ARM64 build is available explicitly.
    if platform == "windows-amd64":
        return "promptless-host-runtime.exe"
    return f"native/{platform}/promptless-host-runtime" + (".exe" if platform.startswith("windows-") else "")


def inventory_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_bundle(root: Path, *, source_sha256: str | None = None, complete: bool = True) -> NativeManifest:
    """Reject missing, modified, unexpected, or linked release files."""
    value = json.loads((root / MANIFEST).read_text())
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported native manifest")
    if source_sha256 is not None and value.get("source_sha256") != source_sha256:
        raise ValueError("native source hash does not match this toolchain")
    platforms = value.get("platforms")
    if not isinstance(platforms, list) or not platforms or any(p not in PLATFORMS for p in platforms):
        raise ValueError("invalid native platforms")
    if len(set(platforms)) != len(platforms) or (complete and set(platforms) != set(PLATFORMS)):
        raise ValueError("incomplete native platform inventory")
    files = value.get("files")
    expected = {*LAUNCHERS, *(binary_path(p) for p in platforms)}
    if not isinstance(files, dict) or set(files) != expected:
        raise ValueError("invalid native file inventory")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("native bundle must not contain symlinks")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != expected | {MANIFEST}:
        raise ValueError("native bundle contains unexpected or missing files")
    for name, digest in files.items():
        path = root.joinpath(*PurePosixPath(name).parts)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"native artifact digest mismatch: {name}")
    if inventory_digest(files) != value.get("bundle_sha256"):
        raise ValueError("native bundle digest mismatch")
    return value
