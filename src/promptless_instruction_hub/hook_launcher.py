"""Python 3.9+ launcher embedded in hook commands, independent of plugin files.

Portable hooks provide context, not policy decisions. Isolate their failures
here so Python's missing-script exit code 2 cannot become a native hook veto.
Keep this module standard-library-only and preserve in-process signal delivery.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import stat
import sys
import tempfile

RUNNER_NAME = "_instruction_hub_hook.py"


def data_root() -> Path:
    """Use a host-independent location that plugin cache cleanup does not own."""
    override = os.environ.get("PROMPTLESS_HOOK_DATA_HOME")
    if override:
        return Path(override).expanduser() / "v1"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "promptless/hook-runtime/v1"


def read_manifest(path: Path, digest: str) -> tuple[bytes, dict]:
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("hook manifest digest mismatch")
    manifest = json.loads(payload)
    if manifest["schema"] != 1 or manifest["host"] not in {"claude", "codex", "cursor"}:
        raise ValueError("unsupported hook bundle")
    files = manifest["files"]
    if RUNNER_NAME not in files or manifest["entrypoint"] not in files:
        raise ValueError("incomplete hook manifest")
    for name in files:
        path_name = PurePosixPath(name)
        if path_name.is_absolute() or ".." in path_name.parts or "\\" in name or name != path_name.as_posix():
            raise ValueError("invalid hook bundle path")
    return payload, manifest


def checked_file(root: Path, name: str, expected: dict) -> bytes:
    path = root / name
    # Source assets reject symlinks at build time. Check installed bundles too,
    # including parent directories, before copying or executing their contents.
    current = root
    for part in ("", *PurePosixPath(name).parts):
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink in hook bundle")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("non-regular file in hook bundle")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected["sha256"]:
        raise ValueError(f"hook file digest mismatch: {name}")
    return payload


def retained_bundle(source: Path, digest: str) -> tuple[Path, dict]:
    """Publish a complete immutable bundle; never resolve to a newer version."""
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid hook bundle digest")
    bundles = data_root() / "bundles"
    destination = bundles / digest
    if not destination.exists():
        payload, manifest = read_manifest(source / "_bundles" / f"{digest}.json", digest)
        bundles.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=bundles) as temporary:
            staging = Path(temporary) / "bundle"
            staging.mkdir(mode=0o700)
            for name, expected in manifest["files"].items():
                content = checked_file(source, name, expected)
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o700 if expected["executable"] else 0o600)
            (staging / "manifest.json").write_bytes(payload)
            try:
                staging.rename(destination)
            except OSError as exc:
                # Another hook may have published the same digest while we
                # staged. It is safe to use only after validating that winner.
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
    _, manifest = read_manifest(destination / "manifest.json", digest)
    expected_paths = set(manifest["files"]) | {"manifest.json"}
    for path in destination.rglob("*"):
        if path.is_symlink() or path.is_file() and path.relative_to(destination).as_posix() not in expected_paths:
            raise ValueError("unexpected file in retained hook bundle")
    for name, expected in manifest["files"].items():
        checked_file(destination, name, expected)
    return destination, manifest


def report_unavailable(digest: str, exc: Exception) -> None:
    """Warn once per unavailable bundle; do not persist hook input or output."""
    try:
        warnings = data_root() / "warnings"
        warnings.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Hash even malformed digests before using them as a filesystem key.
        key = hashlib.sha256(digest.encode()).hexdigest()
        with (warnings / key).open("x"):
            pass
    except FileExistsError:
        return
    except OSError:
        # An unwritable data directory must remain visible and nonblocking.
        pass
    detail = str(exc).replace("\n", " ")[:500]
    print(f"Instruction Hub context hook skipped: {type(exc).__name__}: {detail}", file=sys.stderr)


def main() -> None:
    sys.dont_write_bytecode = True
    source, digest, *options = sys.argv[1:]
    try:
        root, manifest = retained_bundle(Path(source), digest)
    except Exception as exc:
        # Cache removal, incomplete installs, and corrupt/unwritable storage
        # must never turn this context hook into a host policy decision.
        report_unavailable(digest, exc)
        return
    try:
        key = hashlib.sha256(digest.encode()).hexdigest()
        (data_root() / "warnings" / key).unlink(missing_ok=True)
    except OSError:
        pass  # Diagnostic bookkeeping cannot prevent an otherwise healthy hook.
    if options == ["--prepare"]:
        return
    runner = str(root / RUNNER_NAME)
    sys.argv = [runner, "--host", manifest["host"], "--entrypoint", str(root / manifest["entrypoint"])]
    try:
        runpy.run_path(runner, run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(f"Instruction Hub context hook skipped: adapter exited {exc.code!s:.100}.", file=sys.stderr)
    except Exception as exc:
        # This is the boundary for arbitrary authored Python, including import
        # failures while an updater removes a bundle. Never swallow cancellation.
        detail = str(exc).replace("\n", " ")[:500]
        print(f"Instruction Hub context hook skipped: {type(exc).__name__}: {detail}", file=sys.stderr)


if __name__ == "__main__":
    main()
