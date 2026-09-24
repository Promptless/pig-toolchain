"""Explicit source-runtime injection for the prebuilt-artifact-independent test suite.

Compiler tests use an explicit non-executable local artifact inventory, including
subprocess action builds. Existing runtime integration tests inject the source
modules so they exercise actual enrollment and collection without a release. Native
artifact contract tests opt out; CI separately freezes and exercises actual binaries
with no Python or Node available on PATH. This fixture is never
packaged and cannot enable an interpreter fallback in generated customer plugins.
"""

from pathlib import Path
import shutil

import pytest

from promptless_instruction_hub import managed_runtime
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.native_bundle import (
    NativeManifest,
)
from promptless_instruction_hub.native_runtime import ASSET_ROOT, runtime_source_sha256
from tests.managed_bootstrap.helpers import _runtime_bundle_sha256
from tests.native_helpers import fixture_bundle


@pytest.fixture(scope="session")
def compiler_native_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture_bundle(tmp_path_factory.mktemp("compiler-native") / "bundle")


@pytest.fixture(autouse=True)
def source_runtime_for_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, compiler_native_fixture: Path
) -> None:
    if request.node.get_closest_marker("native_runtime") is not None:
        return
    monkeypatch.setenv("PIG_NATIVE_RUNTIME_DIR", str(compiler_native_fixture))
    if "managed_bootstrap" not in Path(str(request.node.path)).parts:
        return

    def copy_source_runtime(target_root: Path) -> NativeManifest:
        runtime = target_root / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("promptless-host-runtime",):
            shutil.copyfile(ASSET_ROOT / name, runtime / name)
        (runtime / "promptless-host-runtime").chmod(0o755)
        shutil.copytree(
            ASSET_ROOT / "promptless_host_runtime",
            runtime / "promptless_host_runtime",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            dirs_exist_ok=True,
        )
        return {
            "schema_version": 1,
            "source_sha256": runtime_source_sha256(),
            "bundle_sha256": _runtime_bundle_sha256(runtime),
            "platforms": [],
            "files": {},
        }

    monkeypatch.setattr(managed_runtime, "_copy_runtime_bundle", copy_source_runtime)
