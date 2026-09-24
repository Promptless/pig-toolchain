"""Native compiler artifact contracts; real executables are covered by the CI matrix."""

from __future__ import annotations

import errno
import io
import json
import os
import shutil
import ssl
import stat
import subprocess
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX launchers require a Unix host")
def test_assembly_restores_launchers_after_windows_artifact_copy(tmp_path: Path) -> None:
    from scripts.build_native_runtime import add_launchers, assemble, write_manifest

    inputs = []
    for platform_id in native_bundle.PLATFORMS:
        root = tmp_path / platform_id
        root.mkdir()
        add_launchers(root)
        (root / "cacert.pem").write_text("CA fixture\n")
        executable = root / (
            "promptless-host-runtime.exe" if platform_id == "windows-x86_64" else f"native/{platform_id}/runtime"
        )
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        if platform_id == "windows-x86_64":
            # Windows ZIPs cannot preserve POSIX execute bits on these text files.
            for name in ("promptless-host-runtime", "cursor-hook.cmd"):
                (root / name).chmod(0o644)
        write_manifest(root, [platform_id])
        inputs.append(root)
    assert inputs[-1].name == "windows-x86_64"
    assembled = tmp_path / "assembled"
    assemble(inputs, assembled)
    archive = tmp_path / "native-runtime.zip"
    archive.write_bytes(archive_bytes(assembled))
    extracted = tmp_path / "extracted"
    native_runtime.extract_bundle(archive, extracted)
    assert native_bundle.validate_bundle(extracted)
    for root in (assembled, extracted):
        for name in ("promptless-host-runtime", "cursor-hook.cmd"):
            launcher = root / name
            assert stat.S_IMODE(launcher.stat().st_mode) == 0o755
            command = ["/bin/sh", str(launcher)] if name.endswith(".cmd") else [str(launcher)]
            subprocess.run(command, check=True, env={"PATH": ""})


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


def test_no_redirect_device_transport_preserves_frozen_roots_and_redirect_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import worker
    from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
        WorkerResponseError,
    )
    from tests.managed_bootstrap.test_device_enrollment import DeviceAPI

    certificates = Path(__file__).parents[1] / "fixtures/native-tls"
    shutil.copyfile(certificates / "localhost.pem", tmp_path / "cacert.pem")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates / "localhost.pem", certificates / "localhost.key")
    monkeypatch.setattr(native_bundle.sys, "frozen", True, raising=False)
    monkeypatch.setattr(native_bundle, "frozen_runtime_root", lambda: tmp_path)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    # Model a clean workstation with no usable OpenSSL system certificate paths.
    monkeypatch.setattr(native_bundle.ssl, "create_default_context", lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    api = DeviceAPI(tls_context=context)
    try:
        result = worker._post_json_response(
            api.base_url + "/device-sessions", None, {}, label="device", allow_redirects=False
        )
        assert result["device_code"] == api.proof
        api.status = 307
        api.redirect = api.base_url + "/untrusted-target"
        with pytest.raises(WorkerResponseError):
            worker._post_json_response(
                api.base_url + "/device-sessions", None, {}, label="device", allow_redirects=False
            )
        assert api.redirect_hits == 0
    finally:
        api.close()
