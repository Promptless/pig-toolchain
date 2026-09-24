"""Native compiler artifact contracts; real executables are covered by the CI matrix."""

from __future__ import annotations

import errno
import io
import json
import shutil
import stat
import urllib.error
import zipfile
from pathlib import Path

import pytest

from promptless_instruction_hub import native_runtime
from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import native_bundle
from tests.config_helpers import enable_trace_ingestion
from tests.native_helpers import fixture_bundle

pytestmark = pytest.mark.native_runtime


def archive_bytes(root: Path) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path in root.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())
    return output.getvalue()


def test_build_copies_validated_native_files_and_emits_host_launchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = fixture_bundle(tmp_path / "bundle")
    monkeypatch.setenv(native_runtime.LOCAL_BUNDLE_ENV, str(bundle))
    hub = tmp_path / "hub"
    init_hub(hub, org="Acme")
    enable_trace_ingestion(hub)
    build_hub(hub)
    expected = native_bundle.validate_bundle(bundle)
    for target in ("claude", "codex", "cursor"):
        plugin = hub / "dist" / target / "pig"
        assert native_bundle.validate_bundle(plugin / "runtime") == expected
        record = json.loads((plugin / "hub.managed-runtimes.json").read_text())["managed_runtimes"][0]
        assert record["sha256"] == expected["bundle_sha256"]
        assert record["source_sha256"] == expected["source_sha256"]
        assert record["platforms"] == expected["platforms"]
        assert not (plugin / "runtime/promptless_host_runtime").exists()
        hooks = json.loads((plugin / "hooks/hooks.json").read_text())["hooks"]
        if target == "cursor":
            for event, lifecycle in (
                ("sessionStart", "session_start"),
                ("stop", "stop"),
                ("sessionEnd", "session_end"),
                ("subagentStop", "subagent_stop"),
            ):
                assert hooks[event][-1] == {
                    "command": f'"${{CURSOR_PLUGIN_ROOT}}/runtime/cursor-hook.cmd" {lifecycle}',
                    "timeout": 30 if lifecycle == "session_start" else 3,
                }
        else:
            for event, lifecycle in (
                ("SessionStart", "session_start"),
                ("Stop", "stop"),
                ("SessionEnd", "session_end"),
                ("SubagentStop", "subagent_stop"),
            ):
                hook = hooks[event][-1]["hooks"][0]
                assert hook["timeout"] == (30 if event == "SessionStart" else 3)
                args = (
                    ["session-start", "--host", target, "--detach"]
                    if event == "SessionStart"
                    else ["collect", "--host", target, "--lifecycle", lifecycle, "--detach", "--quiet"]
                )
                if target == "claude":
                    assert hook["command"] == "${CLAUDE_PLUGIN_ROOT}/runtime/promptless-host-runtime"
                    assert hook["args"] == args
                else:
                    assert hook["command"] == '"${PLUGIN_ROOT}/runtime/promptless-host-runtime" ' + " ".join(args)
                    assert hook[
                        "commandWindows"
                    ] == '& "${PLUGIN_ROOT}/runtime/promptless-host-runtime.exe" ' + " ".join(args)
    assert not (hub / "dist/gemini/pig/runtime").exists()


@pytest.mark.parametrize("mutation", ("changed", "missing", "extra"))
def test_manifest_rejects_changed_frozen_files(tmp_path: Path, mutation: str) -> None:
    bundle = fixture_bundle(tmp_path / "bundle")
    path = bundle / "native/linux-x86_64/runtime"
    if mutation == "changed":
        path.write_bytes(b"changed")
    elif mutation == "missing":
        path.unlink()
    else:
        (bundle / "unexpected.dll").write_bytes(b"extra")
    with pytest.raises(ValueError, match="hash verification failed"):
        native_bundle.validate_bundle(bundle)


def test_local_development_artifacts_are_explicit_and_source_matched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = fixture_bundle(tmp_path / "bundle", complete=False)
    monkeypatch.setenv(native_runtime.LOCAL_BUNDLE_ENV, str(bundle))
    assert native_runtime.resolve_native_bundle() == bundle
    manifest_path = bundle / native_bundle.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["source_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(InstructionHubError, match="source hash does not match"):
        native_runtime.resolve_native_bundle()


def test_embedded_release_requires_all_platforms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = fixture_bundle(tmp_path / "bundle", complete=False)
    monkeypatch.delenv(native_runtime.LOCAL_BUNDLE_ENV, raising=False)
    monkeypatch.setattr(native_runtime, "EMBEDDED_ROOT", bundle)
    with pytest.raises(InstructionHubError, match="incomplete or unsupported platform set"):
        native_runtime.resolve_native_bundle()


@pytest.mark.parametrize(
    "mirror",
    (
        "http://mirror.example/releases",
        "https://user:secret@mirror.example/releases",
        "https://mirror.example/?x=1",
        "https://mirror.example/#fragment",
        "https://mirror.example/../outside",
        "https://mirror.example/%2e%2e/outside",
        "https://mirror.example:bad",
        "https://mirror.example/white space",
    ),
)
def test_rejects_unsafe_mirror_configuration(monkeypatch: pytest.MonkeyPatch, mirror: str) -> None:
    monkeypatch.setenv(native_runtime.RELEASE_BASE_ENV, mirror)
    with pytest.raises(InstructionHubError, match="must be an HTTPS origin/path") as error:
        native_runtime.release_base_url()
    assert "secret" not in str(error.value)


def test_downloads_validated_complete_mirror_bundle_then_uses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = archive_bytes(fixture_bundle(tmp_path / "fixture"))
    monkeypatch.delenv(native_runtime.LOCAL_BUNDLE_ENV, raising=False)
    monkeypatch.setenv(native_runtime.RELEASE_BASE_ENV, "https://mirror.example/artifacts/")
    monkeypatch.setattr(native_runtime, "EMBEDDED_ROOT", tmp_path / "not-embedded")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    seen = []

    def download(request, *, timeout):
        seen.append(request.full_url)
        assert timeout == 30
        return io.BytesIO(data)

    monkeypatch.setattr(native_runtime.urllib.request, "urlopen", download)
    result = native_runtime.resolve_native_bundle()
    assert native_bundle.validate_bundle(result)
    assert native_runtime.resolve_native_bundle() == result
    assert seen == [
        f"https://mirror.example/artifacts/native-{native_runtime.runtime_source_sha256()}/native-runtime.zip"
    ]


def test_missing_release_fails_with_actionable_instructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(native_runtime.LOCAL_BUNDLE_ENV, raising=False)
    monkeypatch.setattr(native_runtime, "EMBEDDED_ROOT", tmp_path / "not-embedded")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    def unavailable(request, *, timeout):
        raise urllib.error.URLError("fixture: not published")

    monkeypatch.setattr(native_runtime.urllib.request, "urlopen", unavailable)
    with pytest.raises(InstructionHubError, match="release workflow must run") as error:
        native_runtime.resolve_native_bundle()
    assert "PIG_NATIVE_RUNTIME_DIR" in str(error.value)


@pytest.mark.parametrize("name", ("../escape", "/absolute", "C:/absolute", "dir\\escape", "dir/../escape"))
def test_archive_rejects_traversal_before_writing(tmp_path: Path, name: str) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("safe-first", b"safe")
        archive.writestr(name, b"unsafe")
    destination = tmp_path / "extracted"
    with pytest.raises(ValueError, match="unsafe native runtime archive member"):
        native_runtime.extract_bundle(archive_path, destination)
    assert not destination.exists()


def test_archive_rejects_symlinks_and_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        entry = zipfile.ZipInfo("link")
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, "../target")
    with pytest.raises(ValueError, match="unsafe"):
        native_runtime.extract_bundle(archive_path, tmp_path / "link")
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("regular", b"too large")
    monkeypatch.setattr(native_runtime, "MAX_EXTRACTED_BYTES", 1)
    with pytest.raises(ValueError, match="extraction limits"):
        native_runtime.extract_bundle(archive_path, tmp_path / "large")


def test_frozen_self_exec_does_not_pass_python_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_bundle.sys, "frozen", True, raising=False)
    monkeypatch.setattr(native_bundle.sys, "executable", "/plugin/native/linux-x86_64/runtime")
    assert native_bundle.self_command(["collect", "--supervised"]) == [
        "/plugin/native/linux-x86_64/runtime",
        "collect",
        "--supervised",
    ]


@pytest.mark.parametrize("race_errno", (errno.EEXIST, errno.ENOTEMPTY))
def test_concurrent_first_download_uses_valid_winner_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race_errno: int
) -> None:
    bundle = fixture_bundle(tmp_path / "fixture")
    data = archive_bytes(bundle)
    monkeypatch.delenv(native_runtime.LOCAL_BUNDLE_ENV, raising=False)
    monkeypatch.setattr(native_runtime, "EMBEDDED_ROOT", tmp_path / "not-embedded")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(native_runtime.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))

    def winner_renamed_first(source: Path, target: Path) -> None:
        shutil.copytree(bundle, target)
        raise OSError(race_errno, "another build populated the destination")

    monkeypatch.setattr(Path, "rename", winner_renamed_first)
    result = native_runtime.resolve_native_bundle()
    assert native_bundle.validate_bundle(result) == native_bundle.validate_bundle(bundle)
