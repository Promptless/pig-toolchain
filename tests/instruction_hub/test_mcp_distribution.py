from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import anyio
import jsonschema
import pytest
import yaml
from mcp import Client, StdioServerParameters, types
from mcp.shared.exceptions import MCPError
from starlette.testclient import TestClient

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.errors import BuildCheckFailedError, InstructionHubError
from promptless_instruction_hub.mcp_distribution.catalog import Catalog
from promptless_instruction_hub.mcp_distribution.compiler import read_frontmatter
from promptless_instruction_hub.mcp_distribution.server import (
    GetSkillParams,
    GetSkillResult,
    ListSkillsResult,
    SKILLS_EXTENSION,
    create_http_app,
    create_server,
)
from promptless_instruction_hub.release.hashing import stable_hash
from promptless_instruction_hub.release.versions import read_release_manifest, resolve_publish_version

from .helpers import SCHEMAS, _snapshot_tree, _write_release_manifest_with_fresh_identity
from .external_helpers import external_definition, write_external


@pytest.fixture
def hub(tmp_path: Path) -> Path:
    root = tmp_path / "hub"
    init_hub(root, org="Acme")
    config = yaml.safe_load((root / "hub.yaml").read_text())
    config.update(mcp={"enabled": True}, stable_plugins=["dev", "ops", "pig"], targets=["claude"])
    (root / "hub.yaml").write_text(yaml.safe_dump(config))
    skill = root / "assets/skills/review"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(
        b"---\nname: review-code\ndescription: Review changes.\nmetadata:\n  owner: Acme\n---\nRead scripts/check.py.\n"
    )
    (skill / "scripts/check.py").write_text("raise RuntimeError('must never execute')\n")
    (skill / "sample.bin").write_bytes(b"\x00\xff\x80\r\n")
    (root / "assets/agents/reviewer.md").write_text(
        "---\ndescription: Delegate review.\ntools: Read\nmodel: opus\n---\nReview the patch.\n"
    )
    (root / "assets/commands/release.md").write_text(
        "---\ndescription: Release on explicit request.\n---\nReview then publish.\n"
    )
    (root / "assets/rules/style.md").write_text("---\nglobs: '*.py'\nalwaysApply: true\n---\nUse simple functions.\n")
    hook = root / "assets/hooks/audit"
    hook.mkdir()
    (hook / "audit.py").write_text("raise RuntimeError('must never execute')\n")
    (hook / "asset.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "audit",
                "type": "hook",
                "support": {"claude": {"mode": "native"}},
                "hook": {"entrypoint": "audit.py", "timeout": 5, "bindings": {"claude": [{"event": "SessionStart"}]}},
            }
        )
    )
    (root / "assets/mcps/repository.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "example": {
                        "type": "http",
                        "url": "https://mcp.example.com/mcp",
                        "headers": {"Authorization": "${MCP_TOKEN}"},
                    }
                }
            }
        )
    )
    (root / "plugins/dev.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "dev",
                "name": "Dev",
                "includes": [
                    "skill:review",
                    "agent:reviewer",
                    "command:release",
                    "rule:style",
                    "hook:audit",
                    "mcp:repository",
                ],
            }
        )
    )
    (root / "plugins/ops.yaml").write_text("id: ops\nname: Ops\nincludes: [skill:review]\n")
    build_hub(root)
    return root


def _rewrite_bundle(root: Path, manifest: dict) -> None:
    manifest.pop("bundle_id", None)
    manifest["bundle_id"] = stable_hash(manifest)
    (root / "bundle.json").write_text(json.dumps(manifest))


def test_bundle_covers_all_assets_and_preserves_bytes(hub: Path) -> None:
    catalog = Catalog(hub / "dist/mcp")
    assert {asset.kind for asset in catalog.assets.values()} == {"skill", "agent", "command", "rule", "hook", "mcp"}
    assert len(catalog.assets) == 6
    skill = catalog.assets["skill:review"]
    assert skill.plugins == ["dev", "ops"]
    assert skill.skill is not None
    uri = catalog.file_uris[skill.skill.entrypoint]
    assert "/review/review-code/SKILL.md" in uri
    assert catalog.skills[uri].frontmatter["metadata"] == {"owner": "Acme"}
    assert len(catalog.skills[uri].resources) == 3
    for resource in catalog.skills[uri].resources:
        content = catalog.read(resource.uri)
        data = content["text"].encode() if "text" in content else base64.b64decode(content["blob"])
        assert resource.size == len(data)
        assert resource.digest == "sha256:" + hashlib.sha256(data).hexdigest()
    assert catalog.read(uri)["text"].encode() == (hub / "assets/skills/review/SKILL.md").read_bytes()
    assert len(catalog.skills) == 2
    agent = catalog.assets["agent:reviewer"]
    assert agent.skill is not None
    assert "subagent" in catalog.read(catalog.file_uris[agent.skill.entrypoint])["text"]
    assert agent.compatibility.requirements == ["subagents", "workflow-dependencies"]
    hook = catalog.assets["hook:audit"]
    assert hook.hook is not None and hook.hook.bindings["claude"][0].event == "SessionStart"
    assert hook.compatibility.behavior == "unavailable"
    assert catalog.assets["rule:style"].compatibility.behavior == "advisory"
    assert catalog.assets["mcp:repository"].compatibility.requirements == ["upstream-mcp-connections", "upstream-auth"]


def test_release_determinism_verification_and_version_basis(hub: Path, tmp_path: Path) -> None:
    snapshot = _snapshot_tree(hub)
    verified = verify_hub(hub)
    assert _snapshot_tree(hub) == snapshot
    assert build_hub(hub, check=True).release_hash == verified.release_hash
    manifest = json.loads((hub / "hub.release.json").read_text())
    assert manifest["schema_version"] == 4
    assert manifest["mcp_bundle"] == manifest["version_basis"]["mcp_bundle"]
    jsonschema.validate(manifest, json.loads((SCHEMAS / "release-manifest.schema.json").read_text()))
    assert read_release_manifest(hub / "hub.release.json")[0] == "0.1.0"
    previous = tmp_path / "previous"
    shutil.copytree(hub, previous)
    assert resolve_publish_version(hub, previous_release_root=previous) == "0.1.0"
    # Metadata changes affect MCP even if no marketplace projection consumes them.
    (hub / "assets/skills/review/asset.yaml").write_text("id: review\ntype: skill\ntitle: New title\n")
    assert resolve_publish_version(hub, previous_release_root=previous) == "0.1.1"
    (hub / "dist/mcp/assets/skill/review/scripts/check.py").write_text("changed")
    with pytest.raises(BuildCheckFailedError):
        build_hub(hub, check=True)
    build_hub(hub, version="0.1.1")
    published = tmp_path / "published"
    shutil.copytree(hub, published)
    assert resolve_publish_version(hub, previous_release_root=published) == "0.1.1"


def test_opt_in_transition_and_disable_remove_bundle(tmp_path: Path) -> None:
    root = tmp_path / "hub"
    init_hub(root, org="Acme")
    build_hub(root)
    old = json.loads((root / "hub.release.json").read_text())
    assert old["schema_version"] == 2 and "mcp_bundle" not in old
    assert not (root / "dist/mcp").exists()
    previous = tmp_path / "previous"
    shutil.copytree(root, previous)
    config = yaml.safe_load((root / "hub.yaml").read_text())
    config["mcp"] = {"enabled": True}
    (root / "hub.yaml").write_text(yaml.safe_dump(config))
    assert resolve_publish_version(root, previous_release_root=previous) == "0.1.1"
    build_hub(root)
    assert Catalog(root / "dist/mcp").assets == {}
    new = json.loads((root / "hub.release.json").read_text())
    assert new["target_hashes"] == old["target_hashes"]
    config["mcp"] = {"enabled": False}
    (root / "hub.yaml").write_text(yaml.safe_dump(config))
    build_hub(root)
    assert not (root / "dist/mcp").exists()
    assert json.loads((root / "hub.release.json").read_text()) == old


@pytest.mark.parametrize("mutation", ["root-missing", "basis-missing", "mismatch", "path", "digest", "old-schema"])
def test_release_rejects_invalid_mcp_contract(hub: Path, mutation: str) -> None:
    path = hub / "hub.release.json"
    manifest = json.loads(path.read_text())
    if mutation == "root-missing":
        del manifest["mcp_bundle"]
    elif mutation == "basis-missing":
        del manifest["version_basis"]["mcp_bundle"]
    elif mutation == "mismatch":
        manifest["mcp_bundle"]["sha256"] = "1" * 64
    elif mutation in {"path", "digest"}:
        key = "path" if mutation == "path" else "sha256"
        manifest["mcp_bundle"][key] = manifest["version_basis"]["mcp_bundle"][key] = "invalid"
    else:
        manifest["schema_version"] = 2
    _write_release_manifest_with_fresh_identity(path, manifest)
    with pytest.raises(ValueError):
        read_release_manifest(path)


def test_unsupported_projections_are_explicit_resources(hub: Path) -> None:
    (hub / "assets/skills/review/SKILL.md").write_text("# Legacy skill without frontmatter\n")
    (hub / "assets/commands/release.md").write_text(
        "---\ndescription: Native command\nargument-hint: issue\n---\nRead $ARGUMENTS\n"
    )
    # Native marketplace commands may contain syntax the MCP prompt subset cannot preserve.
    (hub / "assets/commands/release.asset.yaml").write_text(
        "id: release\ntype: command\nsupport:\n  claude:\n    mode: verbatim\n"
    )
    build_hub(hub)
    catalog = Catalog(hub / "dist/mcp")
    assert catalog.assets["skill:review"].skill is None
    assert catalog.assets["command:release"].prompt is None
    for ref in ("skill:review", "command:release"):
        assert catalog.assets[ref].compatibility.behavior == "unavailable"
        assert catalog.assets[ref].files


def test_lowercase_entrypoint_is_normalized_with_identical_bytes(hub: Path) -> None:
    path = hub / "assets/skills/review/SKILL.md"
    data = path.read_bytes()
    path.rename(path.with_name("skill.md"))
    build_hub(hub)
    catalog = Catalog(hub / "dist/mcp")
    skill = catalog.assets["skill:review"].skill
    assert skill is not None and skill.entrypoint.endswith("/SKILL.md")
    assert catalog.read(catalog.file_uris[skill.entrypoint])["text"].encode() == data


def test_external_plugins_preserve_pins_without_fetching(hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_external(hub, external_definition())
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("offline build started a process"))
    build_hub(hub)
    assert read_release_manifest(hub / "hub.release.json")[0] == "0.1.0"
    manifest = json.loads((hub / "hub.release.json").read_text())
    assert manifest["schema_version"] == 4
    jsonschema.validate(manifest, json.loads((SCHEMAS / "release-manifest.schema.json").read_text()))
    catalog = Catalog(hub / "dist/mcp", {"doc-detective"})
    plugin = next(plugin for plugin in catalog.manifest.plugins if plugin.kind == "external")
    assert plugin.source is not None and plugin.source["sha"] == "a" * 40
    assert plugin.limitation and not catalog.assets and not catalog.skills
    assert len(catalog.resources) == 1  # The selected catalog still describes the external reference.
    assert build_hub(hub, check=True).checked


def test_nested_metadata_file_remains_a_skill_resource(hub: Path) -> None:
    path = hub / "assets/skills/review/scripts/asset.yaml"
    path.write_text("supporting: data\n")
    build_hub(hub)
    catalog = Catalog(hub / "dist/mcp")
    uri = catalog.file_uris["assets/skill/review/scripts/asset.yaml"]
    assert catalog.read(uri)["text"] == path.read_text()


def test_skill_size_limit_retains_source_without_advertising_skill(hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("promptless_instruction_hub.mcp_distribution.compiler.MAX_SKILL_BYTES", 1)
    build_hub(hub)
    catalog = Catalog(hub / "dist/mcp")
    asset = catalog.assets["skill:review"]
    assert asset.skill is None and asset.compatibility.behavior == "unavailable"
    assert "bound" in asset.compatibility.reason
    assert len(asset.files) == 3


def test_oversized_manifest_fails_without_replacing_output(hub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _snapshot_tree(hub)
    monkeypatch.setattr("promptless_instruction_hub.mcp_distribution.compiler.MAX_MANIFEST_BYTES", 1)
    with pytest.raises(InstructionHubError, match="manifest exceeds"):
        build_hub(hub)
    assert _snapshot_tree(hub) == before


@pytest.mark.parametrize(
    "mutation", ["identity", "bytes", "size", "traversal", "symlink", "frontmatter", "membership", "duplicate"]
)
def test_loader_rejects_tampered_artifacts(hub: Path, mutation: str, tmp_path: Path) -> None:
    root = hub / "dist/mcp"
    manifest = json.loads((root / "bundle.json").read_text())
    asset = next(asset for asset in manifest["assets"] if asset["kind"] == "skill")
    file = asset["files"][0]
    if mutation == "identity":
        manifest["org"] = "tampered"
        (root / "bundle.json").write_text(json.dumps(manifest))
    elif mutation == "bytes":
        data = (root / file["path"]).read_bytes()
        (root / file["path"]).write_bytes(b"X" + data[1:])
    elif mutation == "size":
        (root / file["path"]).write_bytes(b"short")
    elif mutation == "symlink":
        destination = tmp_path / "outside"
        destination.write_bytes((root / file["path"]).read_bytes())
        (root / file["path"]).unlink()
        (root / file["path"]).symlink_to(destination)
    else:
        if mutation == "traversal":
            file["path"] = "../../outside"
        elif mutation == "frontmatter":
            asset["skill"]["frontmatter"]["description"] = "different"
        elif mutation == "membership":
            asset["skill"]["files"].pop()
        else:
            asset["files"].append(file)
        _rewrite_bundle(root, manifest)
    with pytest.raises((InstructionHubError, ValueError)):
        Catalog(root)


def test_snapshot_and_scope_are_shared_by_every_lookup(hub: Path) -> None:
    root = hub / "dist/mcp"
    all_catalog = Catalog(root)
    catalog = Catalog(root, {"ops"})
    assert set(catalog.assets) == {"skill:review"}
    assert catalog.assets["skill:review"].plugins == ["ops"]
    catalog_text = catalog.read(catalog.catalog_uri)["text"]
    assert '"id": "dev"' not in catalog_text and '"kind": "command"' not in catalog_text
    for uri in all_catalog.resources.keys() - catalog.resources.keys():
        with pytest.raises(ValueError, match="not found"):
            catalog.read(uri)
    uri = next(iter(catalog.skills))
    before = catalog.read(uri)
    (root / "assets/skill/review/SKILL.md").write_text("changed after startup")
    assert catalog.read(uri) == before
    with pytest.raises(InstructionHubError, match="unknown MCP plugin"):
        Catalog(root, {"missing"})


def test_cursors_are_bound_to_bundle_scope_and_query(hub: Path) -> None:
    catalog = Catalog(hub / "dist/mcp")
    items = list(range(60))
    first, cursor = catalog.page(items, None, "query")
    assert first == list(range(50))
    assert catalog.page(items, cursor, "query") == (list(range(50, 60)), None)
    with pytest.raises(ValueError):
        catalog.page(items, cursor, "different")
    with pytest.raises(ValueError):
        Catalog(hub / "dist/mcp", {"ops"}).page(items, cursor, "query")


async def _skills(client: Client) -> ListSkillsResult:
    return await client.session.send_request(types.Request(method="skills/list", params={}), ListSkillsResult)


def test_protocol_roundtrip_and_scoped_errors(hub: Path) -> None:
    async def run() -> None:
        catalog = Catalog(hub / "dist/mcp")
        async with Client(create_server(catalog)) as client:
            assert SKILLS_EXTENSION in (client.server_capabilities.extensions or {})
            skills = await _skills(client)
            assert len(skills.skills) == 2 and skills.resultType == "complete"
            assert skills.cacheScope == "private" and skills.ttlMs == 0
            skill = skills.skills[0]
            got = await client.session.send_request(
                types.Request[GetSkillParams, str](method="skills/get", params=GetSkillParams(uri=skill.uri)),
                GetSkillResult,
            )
            assert got.skill == skill
            resources = await client.list_resources()
            assert len(resources.resources) == len(catalog.resources)
            for resource in resources.resources:
                result = await client.read_resource(str(resource.uri))
                assert len(result.contents) == 1
                assert result.ttl_ms == 0 and result.cache_scope == "private"
                content = result.contents[0].model_dump(mode="json", by_alias=True)
                assert all(content[key] == value for key, value in catalog.read(str(resource.uri)).items())
                if str(resource.uri) in catalog.skills:
                    assert resource.name == catalog.skills[str(resource.uri)].frontmatter["name"]
                    assert resource.description == catalog.skills[str(resource.uri)].frontmatter["description"]
            prompts = await client.list_prompts()
            assert [prompt.name for prompt in prompts.prompts] == ["command:release"]
            prompt = await client.get_prompt("command:release")
            assert prompt.messages[0].content == types.TextContent(type="text", text="Review then publish.\n")
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ["search_assets", "read_asset"]
            result = await client.call_tool("search_assets", {"query": "review changes", "kind": "skill"})
            assert not result.is_error
            assert result.structured_content["assets"][0]["ref"] == "skill:review"
            result = await client.call_tool("read_asset", {"ref": "skill:review"})
            assert "Read scripts/check.py" in result.structured_content["content"]["text"]
            for args in ({"limit": True}, {"limit": 0}, {"query": "", "extra": True}):
                assert (await client.call_tool("search_assets", args)).is_error
        async with Client(create_server(Catalog(hub / "dist/mcp", {"ops"}))) as client:
            assert len((await _skills(client)).skills) == 1
            assert not (await client.list_prompts()).prompts
            assert (await client.call_tool("read_asset", {"ref": "command:release"})).is_error
            assert (await client.call_tool("read_asset", {"ref": "skill:review", "path": "../../secret"})).is_error
            with pytest.raises(MCPError):
                await client.get_prompt("command:release")
            agent_uri = next(uri for uri in catalog.skills if "/agent/" in uri)
            with pytest.raises(MCPError) as error:
                await client.session.send_request(
                    types.Request[GetSkillParams, str](method="skills/get", params=GetSkillParams(uri=agent_uri)),
                    GetSkillResult,
                )
            assert isinstance(error.value, MCPError) and error.value.code == -32602
            with pytest.raises(MCPError) as error:
                await client.read_resource(agent_uri)
            assert isinstance(error.value, MCPError) and error.value.code == -32602

    anyio.run(run)


def test_stdio_cli_real_wire_roundtrip(hub: Path) -> None:
    async def run() -> None:
        process = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "promptless_instruction_hub.cli",
                "serve-mcp",
                "--bundle",
                str(hub / "dist/mcp"),
                "--plugin",
                "ops",
            ],
        )
        with anyio.fail_after(20):
            async with Client(process) as client:
                assert len((await _skills(client)).skills) == 1
                result = await client.call_tool("search_assets", {})
                assert [asset["ref"] for asset in result.structured_content["assets"]] == ["skill:review"]

    anyio.run(run)


def test_http_auth_host_origin_and_protocol(hub: Path) -> None:
    catalog = Catalog(hub / "dist/mcp", {"ops"})
    with pytest.raises(ValueError, match="non-loopback"):
        create_http_app(catalog, host="0.0.0.0")
    for token in ("", " ", "bad token"):
        with pytest.raises(ValueError, match="bearer token"):
            create_http_app(catalog, token=token)
    app = create_http_app(catalog, host="0.0.0.0", token="test-token", allowed_hosts=["hub.example"])
    with TestClient(app, base_url="http://hub.example") as client:
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        }
        discover = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}}
        assert client.post("/mcp", json=discover).status_code == 401
        assert client.get("/mcp").status_code == 401
        headers = {"Authorization": "Bearer test-token", "Accept": "application/json, text/event-stream"}
        assert client.post("/mcp", json=discover, headers={**headers, "Host": "evil.example"}).status_code == 421
        assert (
            client.post("/mcp", json=discover, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        )
        modern_headers = {**headers, "MCP-Protocol-Version": "2026-07-28"}
        response = client.post("/mcp", json=discover, headers={**modern_headers, "MCP-Method": "server/discover"})
        assert response.status_code == 200 and "result" in response.json(), response.text
        assert SKILLS_EXTENSION in response.json()["result"]["capabilities"]["extensions"]
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 4, "method": "skills/list", "params": {"_meta": meta}},
            headers={**modern_headers, "MCP-Method": "skills/list"},
        )
        assert response.status_code == 200 and "result" in response.json(), response.text
        assert len(response.json()["result"]["skills"]) == 1
        # Legacy clients still get retrieval tools without implementing the skills extension.
        initialize = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        response = client.post("/mcp", json=initialize, headers=headers)
        assert response.status_code == 200 and "tools" in response.json()["result"]["capabilities"]
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_assets", "arguments": {}},
            },
            headers={**headers, "MCP-Protocol-Version": "2025-11-25"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["structuredContent"]["assets"][0]["ref"] == "skill:review"


def test_cli_reports_invalid_bundle(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["serve-mcp", "--bundle", str(tmp_path / "missing")]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "error:" in captured.err


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: Bad Name\ndescription: x",
        "name: okay\nname: duplicate\ndescription: x",
        "name: okay\ndescription: ''",
        "name: okay\ndescription: x\nmetadata: {a: 1, a: 2}",
        "name: okay\ndescription: x\nmetadata: {1: value}",
        "name: okay\ndescription: x\nmetadata: &cycle [*cycle]",
    ],
)
def test_invalid_skill_frontmatter(frontmatter: str) -> None:
    with pytest.raises(ValueError):
        read_frontmatter(f"---\n{frontmatter}\n---\nbody".encode())
