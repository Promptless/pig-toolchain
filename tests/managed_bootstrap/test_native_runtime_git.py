"""Generated native runtimes retain their verified bytes through Git publication."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from promptless_instruction_hub import native_runtime
from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import native_bundle
from scripts.build_native_runtime import add_launchers, write_manifest
from tests.config_helpers import enable_trace_ingestion
from tests.native_helpers import fixture_bundle

pytestmark = pytest.mark.native_runtime


def test_generated_runtime_survives_autocrlf_git_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Windows-style checkout must preserve launchers, CA data, and nested licenses."""
    bundle = fixture_bundle(tmp_path / "bundle")
    add_launchers(bundle)
    (bundle / "cacert.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n")
    license_path = bundle / "licenses" / "darwin-arm64" / "Python.txt"
    license_path.parent.mkdir(parents=True)
    license_path.write_bytes(b"License fixture\nSecond line\n")
    write_manifest(bundle, list(native_bundle.PLATFORMS))
    expected = native_bundle.validate_bundle(bundle)
    expected_bytes = {path.relative_to(bundle): path.read_bytes() for path in bundle.rglob("*") if path.is_file()}
    monkeypatch.setenv(native_runtime.LOCAL_BUNDLE_ENV, str(bundle))
    hub = tmp_path / "hub"
    init_hub(hub, org="Acme")
    enable_trace_ingestion(hub)
    build_hub(hub)

    repository = hub / "dist"
    (repository / ".gitattributes").write_bytes(b"* text=auto\n")
    (repository / "ordinary.txt").write_bytes(b"Outside the verified runtime\n")
    # Keep user Git attributes, hooks, and signing settings out of this local fixture.
    git_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*args: str, cwd: Path = repository) -> None:
        subprocess.run(["git", *args], cwd=cwd, env=git_env, check=True, capture_output=True)

    git("init")
    git("-c", "core.autocrlf=false", "add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Generated plugins")
    checkout = tmp_path / "checkout"
    git("-c", "core.autocrlf=true", "clone", str(repository), str(checkout))
    assert (checkout / "ordinary.txt").read_bytes() == b"Outside the verified runtime\r\n"
    for target in ("claude", "codex", "cursor"):
        runtime = checkout / target / "pig" / "runtime"
        assert native_bundle.validate_bundle(runtime) == expected
        assert {path.relative_to(runtime): path.read_bytes() for path in runtime.rglob("*") if path.is_file()} == (
            expected_bytes
        )
    assert not (checkout / "gemini" / "pig" / ".gitattributes").exists()
