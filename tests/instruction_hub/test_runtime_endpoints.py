from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.config import load_hub_config
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, read_yaml_mapping, write_yaml
from promptless_instruction_hub.release.versions import resolve_publish_version

from .helpers import SCHEMAS, _snapshot_tree


def _configure_endpoints(hub: Path, **endpoints: JsonValue) -> None:
    config = read_yaml_mapping(hub / "hub.yaml")
    config["trace_ingestion"] = {"enabled": True, **endpoints}
    write_yaml(hub / "hub.yaml", config)


@pytest.mark.parametrize("field", ["worker_base_url", "dashboard_base_url"])
@pytest.mark.parametrize(
    "value",
    [
        "http://worker.example",
        "http://127.0.0.1:8080",
        "file:///tmp/worker",
        "//worker.example",
        "https://",
        "https://worker.example/api",
        "https://worker.example//",
        "https://worker.example?token=literal-secret",
        "https://worker.example#fragment",
        "https://user:literal-secret@worker.example",
        "https://user@worker.example",
        "https://worker.example:bad",
        "https://worker.example:65536",
        " https://worker.example",
        "https://worker.example ",
        "https://worker.ex\nample",
        "https://worker.example\\other",
        "",
        False,
        42,
        [],
        {},
    ],
)
def test_hub_rejects_invalid_packaged_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: JsonValue
) -> None:
    init_hub(tmp_path)
    _configure_endpoints(tmp_path, **{field: value})
    # The runtime's loopback test escape hatch must not loosen authored hub configuration.
    monkeypatch.setenv("PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES", "1")
    before = _snapshot_tree(tmp_path)
    with pytest.raises(InstructionHubError, match=field):
        verify_hub(tmp_path)
    assert _snapshot_tree(tmp_path) == before


@pytest.mark.parametrize("origin", ["https://worker.example", "https://worker.example:8443", "https://[::1]:8443"])
def test_hub_accepts_and_normalizes_https_origins(tmp_path: Path, origin: str) -> None:
    init_hub(tmp_path)
    _configure_endpoints(tmp_path, worker_base_url=origin + "/", dashboard_base_url=origin)
    ingestion = load_hub_config(tmp_path).trace_ingestion
    assert ingestion.worker_base_url == ingestion.dashboard_base_url == origin
    schema = json.loads((SCHEMAS / "instruction-hub.schema.json").read_text())
    Draft202012Validator(schema).validate(read_yaml_mapping(tmp_path / "hub.yaml"))


@pytest.mark.parametrize(
    "endpoints",
    [
        {},
        {"worker_base_url": None, "dashboard_base_url": None},
        {"worker_base_url": "https://worker.customer.example"},
        {"dashboard_base_url": "https://dashboard.customer.example"},
        {
            "worker_base_url": "https://worker.customer.example/",
            "dashboard_base_url": "https://dashboard.customer.example/",
        },
    ],
)
def test_build_packages_only_public_config_for_supported_runtimes(
    tmp_path: Path, endpoints: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    init_hub(tmp_path)
    _configure_endpoints(tmp_path, **endpoints)
    monkeypatch.setenv("PROMPTLESS_WORKER_BASE_URL", "https://do-not-package.example")
    monkeypatch.setenv("PROMPTLESS_DASHBOARD_BASE_URL", "https://also-do-not-package.example")
    monkeypatch.setenv("PROMPTLESS_HOST_CREDENTIAL", "plihost_do_not_package")
    built = build_hub(tmp_path)
    expected: dict[str, JsonValue] = {"schema_version": 1}
    expected.update({key: value.rstrip("/") for key, value in endpoints.items() if isinstance(value, str)})
    for target in ("claude", "codex", "cursor"):
        packaged = tmp_path / "dist" / target / "pig/hub.runtime-config.json"
        assert json.loads(packaged.read_text()) == expected
    assert not (tmp_path / "dist/gemini/pig/hub.runtime-config.json").exists()
    assert verify_hub(tmp_path).release_hash == built.release_hash
    assert build_hub(tmp_path, check=True).release_hash == built.release_hash
    for path in (tmp_path / "dist").rglob("*"):
        if path.is_file():
            contents = path.read_bytes()
            assert b"do-not-package.example" not in contents
            assert b"plihost_do_not_package" not in contents


@pytest.mark.parametrize("field", ["worker_base_url", "dashboard_base_url"])
def test_endpoint_change_changes_compiled_hash_and_publish_version(tmp_path: Path, field: str) -> None:
    hub = tmp_path / "hub"
    init_hub(hub)
    _configure_endpoints(hub, **{field: "https://old.customer.example"})
    before = build_hub(hub)
    previous = tmp_path / "previous"
    shutil.copytree(hub, previous)
    before_manifest = json.loads((hub / "hub.release.json").read_text())
    _configure_endpoints(hub, **{field: "https://new.customer.example"})
    assert resolve_publish_version(hub, previous_release_root=previous) == "0.1.1"
    after = build_hub(hub)
    after_manifest = json.loads((hub / "hub.release.json").read_text())
    assert after.release_hash != before.release_hash
    for target in ("claude", "codex", "cursor"):
        assert before_manifest["target_hashes"][target] != after_manifest["target_hashes"][target]
    assert before_manifest["target_hashes"]["gemini"] == after_manifest["target_hashes"]["gemini"]


def test_init_omits_unset_origins_and_preserves_existing_endpoints(tmp_path: Path) -> None:
    init_hub(tmp_path)
    assert read_yaml_mapping(tmp_path / "hub.yaml")["trace_ingestion"] == {"enabled": False}
    _configure_endpoints(tmp_path, worker_base_url="https://worker.customer.example")
    before = (tmp_path / "hub.yaml").read_bytes()
    init_hub(tmp_path)
    assert (tmp_path / "hub.yaml").read_bytes() == before


def test_disabling_ingestion_removes_packaged_endpoints(tmp_path: Path) -> None:
    init_hub(tmp_path)
    _configure_endpoints(tmp_path, worker_base_url="https://worker.customer.example")
    build_hub(tmp_path)
    config = read_yaml_mapping(tmp_path / "hub.yaml")
    config["trace_ingestion"] = {"enabled": False, "worker_base_url": "https://worker.customer.example"}
    write_yaml(tmp_path / "hub.yaml", config)
    build_hub(tmp_path)
    assert list((tmp_path / "dist").rglob("hub.runtime-config.json")) == []
