from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tests.config_helpers import enable_trace_ingestion

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.release.versions import read_release_manifest, resolve_publish_version

from .helpers import (
    SCHEMAS,
    _configure_split_plugin_hub,
    _write_release_manifest_with_fresh_identity,
)


def test_publish_version_bumps_when_package_name_changes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    _configure_split_plugin_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)

    (hub_root / "plugins/dev.yaml").write_text("id: dev\nname: Developer Tools\nincludes:\n  - skill:authoring-tools\n")

    assert resolve_publish_version(hub_root, previous_release_root=previous_release_root) == "0.1.1"


def test_publish_version_bumps_when_package_membership_changes(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    _configure_split_plugin_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)

    (hub_root / "plugins/dev.yaml").write_text("id: dev\nname: Dev\nincludes:\n  - skill:runbooks\n")
    (hub_root / "plugins/ops.yaml").write_text("id: ops\nname: Ops\nincludes:\n  - skill:authoring-tools\n")

    assert resolve_publish_version(hub_root, previous_release_root=previous_release_root) == "0.1.1"


def test_publish_version_prefers_manual_semver_promotion(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, version="1.0.0-alpha.1")
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    (hub_root / "hub.yaml").write_text(
        (hub_root / "hub.yaml").read_text().replace("version: 1.0.0-alpha.1", "version: 1.0.0")
    )

    assert resolve_publish_version(hub_root, previous_release_root=previous_release_root) == "1.0.0"


def test_publish_version_prefers_higher_configured_version_floor(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, version="0.1.1")
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    (hub_root / "hub.yaml").write_text((hub_root / "hub.yaml").read_text().replace("version: 0.1.1", "version: 0.2.0"))

    assert resolve_publish_version(hub_root, previous_release_root=previous_release_root) == "0.2.0"


def test_publish_version_rejects_legacy_managed_runtime_id_in_previous_release(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    enable_trace_ingestion(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    for runtime in manifest["managed_runtimes"]:
        runtime["id"] = "host-enrollment-bootstrap"
    for runtime in manifest["version_basis"]["managed_runtimes"]:
        runtime["id"] = "host-enrollment-bootstrap"
    _write_release_manifest_with_fresh_identity(manifest_path, manifest)

    with pytest.raises(ValueError, match="id must be host-runtime"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_invalid_authoritative_release_manifest(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text(
        json.dumps({"version": "not-semver", "marketplace": {"id": "acme", "name": "Acme"}, "version_basis": {}})
    )

    with pytest.raises(ValueError, match=r"hub\.release\.json: version must be SemVer"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_malformed_authoritative_version_basis(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text(
        json.dumps({"version": "0.1.0", "marketplace": {"id": "acme", "name": "Acme"}, "version_basis": {}})
    )

    with pytest.raises(ValueError, match=r"hub\.release\.json: version_basis must contain exactly"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


@pytest.mark.parametrize("field", ["stable_plugins", "targets"])
def test_publish_version_rejects_empty_authoritative_version_basis_required_lists(
    tmp_path: Path,
    field: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version_basis"][field] = []
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=rf"hub\.release\.json: version_basis\.{field} must not be empty"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_reports_nested_authoritative_version_basis_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version_basis"]["marketplace"]["id"] = None
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=r"hub\.release\.json: version_basis\.marketplace\.id must be a string"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("schema_version", r"hub\.release\.json: schema_version must be 2, 3, or 4"),
        ("assets_object", r"hub\.release\.json: assets must be a list"),
        ("assets_empty", r"hub\.release\.json: assets refs must match version_basis plugin assets"),
        (
            "target_hash_missing",
            r"hub\.release\.json: version_basis\.target_hashes keys must match version_basis\.targets",
        ),
        (
            "managed_runtime_bad_sha",
            r"hub\.release\.json: version_basis\.managed_runtimes\[0\]\.sha256 must be a sha256 hex digest",
        ),
        ("release_id_empty", r"hub\.release\.json: release_id must not be empty"),
        ("release_hash_mismatch", r"hub\.release\.json: release_hash must match manifest content"),
    ],
)
def test_publish_version_rejects_authoritative_release_manifest_tampering(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    _configure_split_plugin_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "schema_version":
        manifest["schema_version"] = 999
    elif mutation == "assets_object":
        manifest["assets"] = {}
    elif mutation == "assets_empty":
        manifest["assets"] = []
    elif mutation == "target_hash_missing":
        del manifest["version_basis"]["target_hashes"]["claude"]
    elif mutation == "managed_runtime_bad_sha":
        manifest["version_basis"]["managed_runtimes"][0]["sha256"] = "bad"
    elif mutation == "release_id_empty":
        manifest["release_id"] = ""
    elif mutation == "release_hash_mismatch":
        manifest["release_hash"] = "0" * 64
    else:
        raise AssertionError(f"unhandled mutation: {mutation}")
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=message):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_authoritative_release_manifest_unexpected_root_key(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    manifest_path = previous_release_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["unexpected"] = True
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match=r"hub\.release\.json: release manifest must contain exactly"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_previous_release_without_version_metadata(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, version="0.2.0")
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "README.md").write_text("# Previous release\n")

    with pytest.raises(ValueError, match="previous release is missing its release manifest"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_ignores_repo_context_inventory(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    _configure_split_plugin_hub(hub_root, targets=("claude", "codex"))
    build_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_release_root)
    (hub_root / "hub.repo-context.json").write_text(
        json.dumps({"schema_version": 1, "files": [{"path": "AGENTS.md", "imported": False}]})
    )

    assert resolve_publish_version(hub_root, previous_release_root=previous_release_root) == "0.1.0"


def test_publish_version_rejects_release_manifest_without_version_basis(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text(
        json.dumps({"version": "0.1.0", "marketplace": {"id": "acme", "name": "Acme"}})
    )

    with pytest.raises(ValueError, match=r"hub\.release\.json: version_basis is missing"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_reports_malformed_previous_release_json_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_text("{")

    with pytest.raises(ValueError, match=r"hub\.release\.json contains malformed JSON"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_reports_malformed_previous_release_json_encoding_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "hub.release.json").write_bytes(b"\xff")

    with pytest.raises(ValueError, match=r"hub\.release\.json contains malformed JSON"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


def test_publish_version_rejects_missing_previous_hub_path(tmp_path: Path) -> None:
    hub_root = tmp_path / "repo/hub"
    init_hub(hub_root)
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()

    with pytest.raises(ValueError, match="previous release is missing hub path: hub"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root, hub_relative_path="hub")


def test_publish_version_rejects_previous_release_without_manifest(tmp_path: Path) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, version="0.3.0")
    previous_release_root = tmp_path / "previous-release"
    previous_release_root.mkdir()
    (previous_release_root / "dist").mkdir()

    with pytest.raises(ValueError, match="previous release is missing its release manifest"):
        resolve_publish_version(hub_root, previous_release_root=previous_release_root)


@pytest.mark.parametrize("schema_version", [2, 3])
def test_publish_migrates_immutable_runtime_metadata(tmp_path: Path, schema_version: int) -> None:
    hub_root = tmp_path / "hub"
    init_hub(hub_root, org="Acme")
    enable_trace_ingestion(hub_root)
    build_hub(hub_root)
    previous_root = tmp_path / "previous-release"
    shutil.copytree(hub_root, previous_root)
    manifest_path = previous_root / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = schema_version
    for runtimes in (manifest["managed_runtimes"], manifest["version_basis"]["managed_runtimes"]):
        for runtime in runtimes:
            runtime["package_id"] = runtime["plugin_id"]
    _write_release_manifest_with_fresh_identity(manifest_path, manifest)
    previous_bytes = manifest_path.read_bytes()

    schema = json.loads((SCHEMAS / "release-manifest.schema.json").read_text())
    Draft202012Validator(schema).validate(json.loads((hub_root / "hub.release.json").read_text()))
    Draft202012Validator(schema).validate(json.loads(previous_bytes))
    assert read_release_manifest(manifest_path)[0] == "0.1.0"
    assert resolve_publish_version(hub_root, previous_release_root=previous_root) == "0.1.1"
    assert manifest_path.read_bytes() == previous_bytes


@pytest.mark.parametrize("schema_version", [2, 3, 4])
def test_release_reader_rejects_runtime_metadata_for_another_schema(tmp_path: Path, schema_version: int) -> None:
    init_hub(tmp_path)
    enable_trace_ingestion(tmp_path)
    build_hub(tmp_path)
    manifest_path = tmp_path / "hub.release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = schema_version
    if schema_version == 4:
        for runtimes in (manifest["managed_runtimes"], manifest["version_basis"]["managed_runtimes"]):
            for runtime in runtimes:
                runtime["package_id"] = runtime["plugin_id"]
    _write_release_manifest_with_fresh_identity(manifest_path, manifest)

    schema = json.loads((SCHEMAS / "release-manifest.schema.json").read_text())
    assert not Draft202012Validator(schema).is_valid(manifest)
    with pytest.raises(ValueError, match="must contain exactly these keys"):
        read_release_manifest(manifest_path)
