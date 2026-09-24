#!/usr/bin/env python3
"""Freeze one platform, or assemble validated platform bundles for release/wheel use."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import distribution, version
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path

from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.native_bundle import (
    MANIFEST,
    PLATFORMS,
    bundle_files,
    tree_hash,
    validate_bundle,
)
from promptless_instruction_hub.native_runtime import ASSET_ROOT, runtime_source_sha256


def current_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    result = f"{system}-{machine}"
    if result not in PLATFORMS:
        raise ValueError(f"Unsupported native runtime build platform: {result}")
    return result


def write_manifest(root: Path, platforms: list[str]) -> None:
    files = bundle_files(root)
    (root / MANIFEST).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": runtime_source_sha256(),
                "platforms": sorted(platforms),
                "files": files,
                "bundle_sha256": tree_hash(files),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def add_launchers(root: Path) -> None:
    for source_name, name in (("native-launcher", "promptless-host-runtime"), ("cursor-hook.cmd", "cursor-hook.cmd")):
        destination = root / name
        shutil.copyfile(ASSET_ROOT / source_name, destination)
        destination.chmod(0o755)


def add_trust_and_licenses(root: Path, platform_id: str) -> None:
    if version("certifi") != "2026.7.22":
        raise ValueError("Native builds require certifi 2026.7.22 as a build-time CA data source")
    certifi = distribution("certifi")
    shutil.copyfile(certifi.locate_file("certifi/cacert.pem"), root / "cacert.pem")
    licenses = root / "licenses" / platform_id
    licenses.mkdir(parents=True)
    python_license = next(
        (
            path
            for path in (
                Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
                Path(sys.base_prefix) / "LICENSE.txt",
            )
            if path.is_file()
        ),
        None,
    )
    if python_license is None:
        raise ValueError("Python distribution is missing LICENSE.txt")
    shutil.copyfile(python_license, licenses / "Python.txt")
    for package in ("pyinstaller", "certifi"):
        metadata = distribution(package)
        for path in metadata.files or []:
            if path.name in ("COPYING.txt", "LICENSE") and ".dist-info/" in str(path):
                shutil.copyfile(metadata.locate_file(path), licenses / (package + ".txt"))
                break
        else:
            raise ValueError(f"{package} distribution is missing its license")


def freeze(output: Path) -> None:
    if version("pyinstaller") != "6.22.0" or sys.version_info[:2] != (3, 11):
        raise ValueError("Native builds require Python 3.11 and PyInstaller 6.22.0; see the release workflow")
    platform_id = current_platform()
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    with tempfile.TemporaryDirectory(prefix="pig-native-build-") as temporary:
        scratch = Path(temporary)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--onedir",
                "--name",
                "runtime",
                "--paths",
                str(ASSET_ROOT),
                "--distpath",
                str(scratch / "dist"),
                "--workpath",
                str(scratch / "build"),
                "--specpath",
                str(scratch),
                str(ASSET_ROOT / "native_entry.py"),
            ],
            check=True,
        )
        output.mkdir(parents=True)
        built = scratch / "dist" / "runtime"
        if platform_id == "windows-x86_64":
            shutil.copytree(built, output, dirs_exist_ok=True, symlinks=False)
            (output / "runtime.exe").rename(output / "promptless-host-runtime.exe")
        else:
            shutil.copytree(built, output / "native" / platform_id, symlinks=False)
    add_launchers(output)
    add_trust_and_licenses(output, platform_id)
    write_manifest(output, [platform_id])
    validate_bundle(output, source_sha256=runtime_source_sha256(), complete=False)


def assemble(inputs: list[Path], output: Path) -> None:
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    platforms: list[str] = []
    for root in inputs:
        manifest = validate_bundle(root, source_sha256=runtime_source_sha256(), complete=False)
        for platform_id in manifest["platforms"]:
            if platform_id in platforms:
                raise ValueError(f"Duplicate platform artifact: {platform_id}")
            platforms.append(platform_id)
    if set(platforms) != set(PLATFORMS):
        raise ValueError(f"Release requires exactly {PLATFORMS}; got {platforms}")
    output.mkdir(parents=True)
    for root in inputs:
        shutil.copytree(root, output, dirs_exist_ok=True)
    write_manifest(output, platforms)
    validate_bundle(output, source_sha256=runtime_source_sha256())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "assemble", "source-hash"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", action="append", type=Path, default=[])
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.mode == "source-hash":
        print(runtime_source_sha256())
        return
    if args.output is None:
        parser.error("--output is required")
    if args.mode == "freeze":
        freeze(args.output.resolve())
    else:
        assemble(args.input, args.output.resolve())
    if args.archive:
        with zipfile.ZipFile(args.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(args.output.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(args.output).as_posix())


if __name__ == "__main__":
    main()
