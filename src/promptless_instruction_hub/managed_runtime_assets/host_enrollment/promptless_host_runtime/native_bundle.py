"""Integrity and process layout shared by the compiler and frozen runtime."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

MANIFEST = "native-runtime.json"
PLATFORMS = ("darwin-arm64", "darwin-x86_64", "linux-x86_64", "windows-x86_64")


class NativeManifest(TypedDict):
    schema_version: int
    source_sha256: str
    platforms: list[str]
    files: dict[str, str]
    bundle_sha256: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and "\\" not in value
        and ":" not in value
        and not path.is_absolute()
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )


def bundle_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"native runtime contains a symlink: {path}")
        if path.is_file() and path.relative_to(root).as_posix() != MANIFEST:
            files[path.relative_to(root).as_posix()] = file_sha256(path)
    return files


def validate_bundle(root: Path, *, source_sha256: str | None = None, complete: bool = True) -> NativeManifest:
    """Validate every frozen file before copying or reporting an installed digest."""
    manifest_path = root / MANIFEST
    if manifest_path.is_symlink():
        raise ValueError("native runtime manifest must not be a symlink")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported native runtime manifest")
    platforms = manifest.get("platforms")
    if (
        not isinstance(platforms, list)
        or not platforms
        or any(not isinstance(item, str) or item not in PLATFORMS for item in platforms)
        or len(set(platforms)) != len(platforms)
        or (complete and set(platforms) != set(PLATFORMS))
    ):
        raise ValueError("native runtime manifest has an incomplete or unsupported platform set")
    if source_sha256 is not None and manifest.get("source_sha256") != source_sha256:
        raise ValueError("native runtime source hash does not match this toolchain")
    files = manifest.get("files")
    if (
        not isinstance(files, dict)
        or not files
        or any(
            not isinstance(key, str) or not safe_relative_path(key) or not isinstance(value, str)
            for key, value in files.items()
        )
    ):
        raise ValueError("invalid native runtime file manifest")
    actual_files = bundle_files(root)
    if files != actual_files or manifest.get("bundle_sha256") != tree_hash(actual_files):
        raise ValueError("native runtime file hash verification failed")
    for platform in platforms:
        executable = "promptless-host-runtime.exe" if platform == "windows-x86_64" else f"native/{platform}/runtime"
        if executable not in files:
            raise ValueError(f"native runtime executable missing for {platform}")
    for launcher in ("promptless-host-runtime", "cursor-hook.cmd", "cacert.pem"):
        if launcher not in files:
            raise ValueError(f"native runtime launcher missing: {launcher}")
    return cast(NativeManifest, manifest)


def frozen_runtime_root() -> Path:
    executable = Path(sys.executable).resolve()
    return executable.parent if os.name == "nt" else executable.parents[2]


def self_command(arguments: list[str]) -> list[str]:
    """Frozen executables re-exec themselves; source tests retain the script argument."""
    prefix = [sys.executable]
    if not getattr(sys, "frozen", False):
        prefix.append(str(Path(sys.argv[0]).resolve()))
    return [*prefix, *arguments]


def build_runtime_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """Preserve custom handlers while supplying frozen public roots and explicit CA overrides."""
    context = ssl.create_default_context()
    if getattr(sys, "frozen", False) and not os.environ.get("SSL_CERT_FILE") and not os.environ.get("SSL_CERT_DIR"):
        context.load_verify_locations(cafile=frozen_runtime_root() / "cacert.pem")
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), *handlers)


def configure_frozen_https() -> None:
    """Supply public roots without depending on the build machine's OpenSSL paths."""
    if getattr(sys, "frozen", False):
        urllib.request.install_opener(build_runtime_opener())
