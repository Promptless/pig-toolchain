from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.native_manifest import (
    LAUNCHERS,
    MANIFEST,
    PLATFORMS,
    binary_path,
    inventory_digest,
    validate_bundle,
)
from promptless_instruction_hub.native_runtime import (
    extract_bundle,
    release_base_url,
    resolve_native_bundle,
    runtime_source_sha256,
)


def bundle(root: Path) -> Path:
    root.mkdir()
    files = {}
    for name in (*LAUNCHERS, *(binary_path(p) for p in PLATFORMS)):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / MANIFEST).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": runtime_source_sha256(),
                "bundle_sha256": inventory_digest(files),
                "platforms": list(PLATFORMS),
                "files": files,
            }
        )
    )
    return root


def test_source_matched_local_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = bundle(tmp_path / "bundle")
    monkeypatch.setenv("PIG_NATIVE_RUNTIME_DIR", str(root))
    assert resolve_native_bundle() == root
    manifest = json.loads((root / MANIFEST).read_text())
    manifest["source_sha256"] = "0" * 64
    (root / MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(InstructionHubError, match="source hash"):
        resolve_native_bundle()


def test_release_requires_all_platforms(tmp_path: Path) -> None:
    root = bundle(tmp_path / "bundle")
    manifest = json.loads((root / MANIFEST).read_text())
    platform = manifest["platforms"].pop()
    (root / binary_path(platform)).unlink()
    del manifest["files"][binary_path(platform)]
    manifest["bundle_sha256"] = inventory_digest(manifest["files"])
    (root / MANIFEST).write_text(json.dumps(manifest))
    validate_bundle(root, complete=False)
    with pytest.raises(ValueError, match="incomplete"):
        validate_bundle(root)


@pytest.mark.parametrize("name", ["../escape", "/escape", "C:/escape", "a/../escape", "a\\escape", "a//b"])
def test_reject_unsafe_zip_members_before_extracting(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("safe", b"data")
        output.writestr(name, b"data")
    destination = tmp_path / "extracted"
    with pytest.raises(ValueError, match="unsafe"):
        extract_bundle(archive, destination)
    assert not destination.exists()


def test_reject_zip_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    link = zipfile.ZipInfo("link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(link, b"../escape")
    with pytest.raises(ValueError, match="unsafe"):
        extract_bundle(archive, tmp_path / "extracted")


def test_bundle_archive_round_trip(tmp_path: Path) -> None:
    root = bundle(tmp_path / "bundle")
    (root / LAUNCHERS[0]).chmod(0o755)
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for path in root.rglob("*"):
            if path.is_file():
                output.write(path, path.relative_to(root).as_posix())
    destination = tmp_path / "extracted"
    extract_bundle(archive, destination)
    assert validate_bundle(destination) == validate_bundle(root)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:secret@example.com",
        "https://example.com/a/../b",
        "https://example.com/a?secret=1",
    ],
)
def test_release_override_is_https_without_secrets_or_traversal(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("PIG_NATIVE_RUNTIME_RELEASE_BASE_URL", url)
    with pytest.raises(InstructionHubError, match="HTTPS"):
        release_base_url()
