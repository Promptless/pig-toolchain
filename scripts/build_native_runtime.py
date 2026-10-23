#!/usr/bin/env python3
"""Cross-compile source-matched Go binaries for plugin and cloud distribution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as host_platform
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from promptless_instruction_hub.native_manifest import (  # noqa: E402
    LAUNCHERS,
    MANIFEST,
    PLATFORMS,
    binary_path,
    inventory_digest,
    validate_bundle,
)
from promptless_instruction_hub.native_runtime import ASSET_ROOT, SOURCE_ROOT, runtime_source_sha256  # noqa: E402


def build(output: Path, platforms: list[str]) -> None:
    source_hash = runtime_source_sha256()
    output.mkdir(parents=True, exist_ok=True)
    for name in LAUNCHERS:
        shutil.copyfile(ASSET_ROOT / name, output / name)
        (output / name).chmod(0o755)
    for platform in platforms:
        goos, goarch = platform.split("-")
        destination = (output / binary_path(platform)).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "go",
                "build",
                "-trimpath",
                "-buildvcs=false",
                "-ldflags=-s -w",
                "-o",
                str(destination),
                "./cmd/promptless-host-runtime",
            ],
            cwd=SOURCE_ROOT,
            env={**os.environ, "CGO_ENABLED": "0", "GOOS": goos, "GOARCH": goarch, "GOTOOLCHAIN": "go1.27.3"},
            check=True,
        )
    names = [*LAUNCHERS, *(binary_path(platform) for platform in platforms)]
    files = {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in sorted(names)}
    if runtime_source_sha256() != source_hash:
        raise RuntimeError("Runtime sources changed during compilation; rebuild from a stable checkout")
    (output / MANIFEST).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": source_hash,
                "bundle_sha256": inventory_digest(files),
                "platforms": sorted(platforms),
                "files": files,
            },
            indent=2,
        )
        + "\n"
    )
    validate_bundle(output, complete=set(platforms) == set(PLATFORMS))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--platform", action="append", choices=PLATFORMS)
    group.add_argument("--host", action="store_true", help="Build only the current host for local tests")
    args = parser.parse_args()
    machine = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        host_platform.machine().lower()
    )
    host = f"{host_platform.system().lower()}-{machine}"
    if args.host and host not in PLATFORMS:
        parser.error(f"unsupported host platform: {host}")
    build(args.output, [host] if args.host else args.platform or list(PLATFORMS))


if __name__ == "__main__":
    main()
