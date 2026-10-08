"""Build a credential-free Claude plugin for cloud native trace capture."""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

from promptless_instruction_hub.managed_runtime import (
    HOST_RUNTIME_EXECUTABLE,
    HOST_RUNTIME_ID,
    HOST_RUNTIME_OUTPUT_DIR,
    HOST_RUNTIME_VERSION,
    ManagedRuntimeRecord,
    _runtime_bundle_sha256,
    _toolchain_version,
    _write_plugin_manifest,
    copy_host_runtime_bundle,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.cloud import CloudConfig

CLOUD_PLUGIN_ID = "pig-cloud-capture"
CLOUD_PLUGIN_VERSION = "0.1.0"


def build_cloud_bundle(output: Path, *, worker_url: str, provider: str, integration_id: str, transport: str) -> Path:
    """Package headless hooks and a stdlib-only collector without credentials.

    ``transport`` selects a connection-injected grant (``proxy``) or a sandbox
    environment/file grant (``secret``). Invalid public configuration and existing
    output files are rejected before the archive is written.
    """
    if output.suffix != ".zip":
        raise ValueError("Cloud bundle output must end in .zip")
    if output.exists():
        raise ValueError("Cloud bundle output already exists")
    with tempfile.TemporaryDirectory(prefix="pig-cloud-bundle-") as temporary:
        root = Path(temporary)
        config_path = root / "cloud-capture.json"
        config_path.write_text(
            json.dumps(
                {
                    "worker_url": worker_url,
                    "provider": provider,
                    "target": "claude",
                    "transport": transport,
                    "integration_id": integration_id,
                },
                indent=2,
            )
            + "\n"
        )
        CloudConfig.load(config_path)
        copy_host_runtime_bundle(root)
        _write_plugin_manifest(
            root,
            (
                ManagedRuntimeRecord(
                    id=HOST_RUNTIME_ID,
                    status="included",
                    target="claude",
                    package_id=CLOUD_PLUGIN_ID,
                    plugin_id=CLOUD_PLUGIN_ID,
                    plugin_name="PIG Cloud Capture",
                    plugin_version=CLOUD_PLUGIN_VERSION,
                    toolchain_version=_toolchain_version(),
                    version=HOST_RUNTIME_VERSION,
                    sha256=_runtime_bundle_sha256(root / HOST_RUNTIME_OUTPUT_DIR),
                    executable=HOST_RUNTIME_EXECUTABLE,
                    path=f"{HOST_RUNTIME_OUTPUT_DIR}/{HOST_RUNTIME_EXECUTABLE}",
                    hook="hooks/hooks.json",
                ),
            ),
        )
        (root / ".claude-plugin").mkdir()
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": CLOUD_PLUGIN_ID,
                    "version": CLOUD_PLUGIN_VERSION,
                    "description": "Export this cloud execution's native traces to your PIG worker.",
                },
                indent=2,
            )
            + "\n"
        )
        command = 'python3 "${CLAUDE_PLUGIN_ROOT}/runtime/promptless-host-runtime" cloud-collect --config "${CLAUDE_PLUGIN_ROOT}/cloud-capture.json" --detach'
        hooks = {
            event: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
            for event in ("SessionStart", "UserPromptSubmit", "Stop", "SubagentStop", "SessionEnd")
        }
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}, indent=2) + "\n")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root))
    return output
