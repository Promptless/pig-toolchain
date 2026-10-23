"""Resolve source-matched native release artifacts for generated plugins."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import InstructionHubError
from .native_manifest import (
    safe_relative_path,
    validate_bundle,
)

ASSET_ROOT = Path(__file__).parent / "managed_runtime_assets" / "host_enrollment"
EMBEDDED_ROOT = Path(__file__).parent / "native_runtime_bundle"
LOCAL_BUNDLE_ENV = "PIG_NATIVE_RUNTIME_DIR"
RELEASE_BASE_ENV = "PIG_NATIVE_RUNTIME_RELEASE_BASE_URL"
# This is the publisher's immutable artifact location, never a customer worker URL.
RELEASE_BASE = "https://github.com/Promptless/pig-toolchain/releases/download"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1536 * 1024 * 1024
BUILD_RECIPE = "go1.27.3-cgo0-trimpath-buildvcsfalse-ldflags-s-w-v1"
SOURCE_ROOT = Path(__file__).parent / "host_runtime_source"
if not SOURCE_ROOT.is_dir():
    SOURCE_ROOT = Path(__file__).parents[2] / "host-runtime"


def runtime_source_sha256() -> str:
    digest = hashlib.sha256(BUILD_RECIPE.encode())
    for root in (SOURCE_ROOT, ASSET_ROOT):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if root == SOURCE_ROOT and not (path.suffix == ".go" or path.name in ("go.mod", "go.sum")):
                continue
            if root == SOURCE_ROOT and path.name.endswith("_test.go"):
                continue
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def release_base_url() -> str:
    value = os.environ.get(RELEASE_BASE_ENV, RELEASE_BASE).rstrip("/")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() or ord(character) < 32 for character in value)
            and "\\" not in value
            and all(part not in (".", "..") for part in unquote(parsed.path).split("/"))
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise InstructionHubError(
            f"{RELEASE_BASE_ENV} must be an HTTPS origin/path without credentials, query, fragment or traversal"
        )
    return value


def resolve_native_bundle() -> Path:
    source_hash = runtime_source_sha256()
    local = os.environ.get(LOCAL_BUNDLE_ENV)
    if local:
        return _validated(Path(local).expanduser().resolve(), source_hash, complete=False)
    if EMBEDDED_ROOT.exists():
        return _validated(EMBEDDED_ROOT, source_hash, complete=True)
    cache = Path.home() / ".cache" / "pig-toolchain" / "native" / source_hash
    if cache.exists():
        return _validated(cache, source_hash, complete=True)
    url = f"{release_base_url()}/native-{source_hash}/native-runtime.zip"
    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="native-download-", dir=cache.parent) as temporary:
            directory = Path(temporary)
            archive_path = directory / "runtime.zip"
            request = urllib.request.Request(url, headers={"User-Agent": "pig-toolchain-native-runtime"})
            with urllib.request.urlopen(request, timeout=30) as response, archive_path.open("wb") as destination:
                remaining = MAX_ARCHIVE_BYTES
                deadline = time.monotonic() + 120
                while block := response.read(min(1024 * 1024, remaining + 1)):
                    if time.monotonic() >= deadline:
                        raise ValueError("native runtime download exceeded time limit")
                    remaining -= len(block)
                    if remaining < 0:
                        raise ValueError("native runtime download exceeds size limit")
                    destination.write(block)
            extracted = directory / "bundle"
            extract_bundle(archive_path, extracted)
            _validated(extracted, source_hash, complete=True)
            try:
                extracted.rename(cache)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                _validated(cache, source_hash, complete=True)
    except (OSError, ValueError, zipfile.BadZipFile, urllib.error.URLError) as exc:
        raise InstructionHubError(
            f"Native runtime release native-{source_hash} is unavailable or invalid ({exc}). "
            "Use a released toolchain with matching native artifacts, or build local artifacts with "
            "scripts/build_native_runtime.py and set PIG_NATIVE_RUNTIME_DIR. "
            "The native-runtime release workflow must run before this source revision can publish telemetry plugins."
        ) from exc
    return cache


def _validated(root: Path, source_hash: str, *, complete: bool) -> Path:
    try:
        validate_bundle(root, source_sha256=source_hash, complete=complete)
    except (OSError, ValueError) as exc:
        raise InstructionHubError(f"Invalid native runtime bundle at {root}: {exc}") from exc
    return root


def extract_bundle(archive_path: Path, destination: Path) -> None:
    """Extract regular files only after checking the complete bounded archive inventory."""
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        seen: set[str] = set()
        size = 0
        for entry in entries:
            name = entry.filename.rstrip("/")
            mode = entry.external_attr >> 16
            if (
                not safe_relative_path(name)
                or name in seen
                or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
            ):
                raise ValueError(f"unsafe native runtime archive member: {entry.filename}")
            seen.add(name)
            size += entry.file_size
            if size > MAX_EXTRACTED_BYTES or len(seen) > 10000:
                raise ValueError("native runtime archive exceeds extraction limits")
        destination.mkdir(parents=True, exist_ok=False)
        for entry in entries:
            path = destination / entry.filename
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, path.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            path.chmod(0o755 if (entry.external_attr >> 16) & 0o111 else 0o644)
