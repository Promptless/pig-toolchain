"""Non-executable native bundle fixtures for compiler-only tests."""

import json
from pathlib import Path

from promptless_instruction_hub import native_runtime
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime import native_bundle


def fixture_bundle(root: Path, *, complete: bool = True) -> Path:
    """Fake binaries are only for compiler inventory tests, never execution tests."""
    root.mkdir(parents=True)
    platforms = list(native_bundle.PLATFORMS if complete else ("linux-x86_64",))
    names = ["promptless-host-runtime", "cursor-hook.cmd", "cacert.pem"]
    for platform in platforms:
        names.append("promptless-host-runtime.exe" if platform == "windows-x86_64" else f"native/{platform}/runtime")
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"compiler test fixture, not an executable")
        path.chmod(0o755)
    files = native_bundle.bundle_files(root)
    (root / native_bundle.MANIFEST).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": native_runtime.runtime_source_sha256(),
                "platforms": platforms,
                "files": files,
                "bundle_sha256": native_bundle.tree_hash(files),
            }
        )
    )
    return root
