"""Offline MCP bundle compilation, including explicit behavioral limitations."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from promptless_instruction_hub.agent_skills import read_agent_skill, render_agent_skill
from promptless_instruction_hub.assets import METADATA_FILE
from promptless_instruction_hub.commands import read_command
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue, validate_json_value, write_json
from promptless_instruction_hub.mcp_distribution.models import (
    BundleAsset,
    BundleFile,
    BundleManifest,
    BundlePlugin,
    BundlePrompt,
    BundleSkill,
    Compatibility,
)
from promptless_instruction_hub.models import LoadedAsset, ResolvedExternalPluginDefinition, ResolvedHubPluginDefinition
from promptless_instruction_hub.release.hashing import stable_hash
from promptless_instruction_hub.validate.hub import ValidationResult

BUNDLE_PATH = Path("dist/mcp")
MANIFEST_NAME = "bundle.json"
MAX_SKILL_FILES = 512
MAX_SKILL_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_FILES = 10_000
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024

# Explicit mappings keep release hashes independent of OS MIME databases.
MIME_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".js": "text/javascript",
    ".ts": "text/plain",
    ".json": "application/json",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".toml": "text/plain",
    ".csv": "text/csv",
    ".html": "text/html",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
}


def render_mcp_bundle(output_root: Path, validation: ValidationResult[ResolvedHubPluginDefinition]) -> None:
    """Render once from resolved local assets; never fetch or execute source content."""
    if not validation.config.mcp.enabled:
        return
    root = output_root / BUNDLE_PATH
    memberships: dict[str, list[str]] = {}
    plugins: list[BundlePlugin] = []
    for plugin in validation.stable_plugins:
        definition = plugin.definition
        external = isinstance(definition, ResolvedExternalPluginDefinition)
        plugins.append(
            BundlePlugin(
                id=definition.id,
                name=definition.name,
                kind="external" if external else "authored",
                source=definition.source.model_dump() if external else None,
                limitation="Upstream marketplace reference only; its assets require a separately resolved export."
                if external
                else None,
            )
        )
        for asset in plugin.assets:
            memberships.setdefault(asset.ref, []).append(definition.id)
    assets = [_compile_asset(root, asset, sorted(memberships[asset.ref])) for asset in validation.stable_assets]
    files = [file for asset in assets for file in asset.files]
    if len(files) > MAX_BUNDLE_FILES or sum(file.size for file in files) > MAX_BUNDLE_BYTES:
        raise InstructionHubError("MCP bundle exceeds 10,000 files or 256 MiB")
    if len({file.path.casefold() for file in files}) != len(files):
        raise InstructionHubError("MCP bundle has duplicate file paths")
    manifest = BundleManifest(
        bundle_id="0" * 64,
        org=validation.config.org,
        marketplace=validation.config.marketplace,
        version=validation.config.version,
        plugins=plugins,
        assets=assets,
        limitations=[
            "Client behavior, including Dots skill discovery and execution, is unverified.",
            "Execution stays with the client: this service does not run scripts, agents, hooks, or upstream MCPs.",
            "Managed plugin installation/update skills and trace-ingestion runtimes are not distributed over MCP.",
            "Resource reads establish delivery, not instruction compliance or trace capture.",
        ],
    )
    manifest.bundle_id = stable_hash(manifest.model_dump(mode="json", exclude={"bundle_id"}))
    write_json(root / MANIFEST_NAME, manifest.model_dump(mode="json"))
    if (root / MANIFEST_NAME).stat().st_size > MAX_MANIFEST_BYTES:
        raise InstructionHubError("MCP bundle manifest exceeds 16 MiB")


def _compile_asset(root: Path, asset: LoadedAsset, plugins: list[str]) -> BundleAsset:
    relative_root = Path("assets") / asset.type / asset.id
    sources = sorted(asset.path.rglob("*")) if asset.path.is_dir() else [asset.path]
    files: list[BundleFile] = []
    for source in sources:
        if source.is_symlink():
            raise InstructionHubError(f"{asset.ref}: symlinks cannot be distributed")
        if source.is_dir() or source == asset.path / METADATA_FILE:
            continue
        if not source.is_file():
            raise InstructionHubError(f"{asset.ref}: only regular files can be distributed")
        relative = source.relative_to(asset.path) if asset.path.is_dir() else Path(source.name)
        files.append(_write_file(root, relative_root / relative, source.read_bytes()))
    result = BundleAsset(
        ref=asset.ref,
        id=asset.id,
        kind=asset.type,
        title=asset.metadata.title or asset.id,
        plugins=plugins,
        source_hash=asset.content_hash,
        source_support=asset.metadata.support,
        files=files,
        hook=asset.metadata.hook,
        compatibility=_compatibility(asset.type),
    )
    files = result.files
    try:
        if asset.type == "skill":
            entrypoints = [
                file
                for file in files
                if Path(file.path).parent == relative_root and Path(file.path).name.lower() == "skill.md"
            ]
            if len(entrypoints) != 1:
                raise ValueError("a skill must have exactly one root SKILL.md")
            original = entrypoints[0]
            content = (root / original.path).read_bytes()
            frontmatter = read_frontmatter(content)
            # Normalize filename casing without changing the authored bytes.
            entrypoint = relative_root / "SKILL.md"
            if original.path != entrypoint.as_posix():
                (root / original.path).unlink()
                files.remove(original)
                files.append(_write_file(root, entrypoint, content))
            result.skill = BundleSkill(
                frontmatter=frontmatter, entrypoint=entrypoint.as_posix(), files=sorted(file.path for file in files)
            )
        elif asset.type == "agent":
            content = render_agent_skill(read_agent_skill(asset)).encode("utf-8")
            entrypoint = Path("projections/agent") / asset.id / "SKILL.md"
            result.files.append(_write_file(root, entrypoint, content))
            result.skill = BundleSkill(
                frontmatter=read_frontmatter(content), entrypoint=entrypoint.as_posix(), files=[entrypoint.as_posix()]
            )
        elif asset.type == "command":
            # The Codex conversion validates the host-independent, explicit-invocation subset.
            command = read_command(asset, "codex")
            result.prompt = BundlePrompt(name=f"command:{asset.id}", description=command.description, body=command.body)
        if result.skill is not None:
            skill_files = [file for file in result.files if file.path in result.skill.files]
            if len(skill_files) > MAX_SKILL_FILES or sum(file.size for file in skill_files) > MAX_SKILL_BYTES:
                raise ValueError("skill exceeds the extension's 512-file or 16 MiB bound")
    except (InstructionHubError, ValueError) as exc:
        result.skill = None
        result.prompt = None
        result.compatibility = Compatibility(
            representation="resources",
            behavior="unavailable",
            requirements=[],
            reason=f"Source is available as resources; behavioral projection is unavailable: {exc}",
        )
    result.files.sort(key=lambda file: file.path)
    return result


def _write_file(root: Path, relative: Path, data: bytes) -> BundleFile:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return BundleFile(
        path=relative.as_posix(),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        mime_type=MIME_TYPES.get(path.suffix.lower(), "application/octet-stream"),
    )


def read_frontmatter(content: bytes) -> dict[str, JsonValue]:
    """Read exactly the frontmatter advertised by the extension, without rewriting it."""
    lines = content.decode("utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md must begin with YAML frontmatter")
    end = next((i for i, line in enumerate(lines[1:], 1) if line == "---"), None)
    if end is None:
        raise ValueError("SKILL.md frontmatter is unterminated")
    try:
        yaml_text = "\n".join(lines[1:end])
        node = yaml.compose(yaml_text)
        if not isinstance(node, yaml.MappingNode):
            raise ValueError("skill frontmatter must be an object")
        _validate_yaml_nodes(node)
        data = validate_json_value(yaml.safe_load(yaml_text), "SKILL.md")
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError("SKILL.md has malformed YAML frontmatter") from exc
    if not isinstance(data, dict):
        raise ValueError("skill frontmatter must be an object")
    name, description = data.get("name"), data.get("description")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None:
        raise ValueError("skill name must be 1-64 lowercase letters, digits, or single separating hyphens")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("skill description must contain 1-1024 characters")
    return data


def _validate_yaml_nodes(node: yaml.Node) -> None:
    """Reject lossy mappings and cyclic or excessively expanded YAML aliases."""
    active: set[int] = set()
    remaining = 10_000

    def visit(current: yaml.Node) -> None:
        nonlocal remaining
        remaining -= 1
        if id(current) in active or len(active) >= 100 or remaining < 0:
            raise ValueError("skill frontmatter contains cyclic or excessive YAML nesting")
        active.add(id(current))
        if isinstance(current, yaml.MappingNode):
            keys = [
                key.value
                for key, _ in current.value
                if isinstance(key, yaml.ScalarNode) and key.tag == "tag:yaml.org,2002:str"
            ]
            if len(keys) != len(current.value) or len(keys) != len(set(keys)):
                raise ValueError("skill frontmatter keys must be unique strings")
            for _, child in current.value:
                visit(child)
        elif isinstance(current, yaml.SequenceNode):
            for child in current.value:
                visit(child)
        active.remove(id(current))

    visit(node)


def _compatibility(kind: str) -> Compatibility:
    values = {
        "skill": (
            "skills+resources",
            "requires-host",
            ["skill-loading", "workflow-dependencies"],
            "Instructions and supporting bytes are preserved; the client must supply execution and dependencies.",
        ),
        "command": (
            "prompts+resources",
            "requires-host",
            ["explicit-prompt-invocation", "workflow-dependencies"],
            "A prompt preserves explicit invocation; the client supplies execution. No implicit activation.",
        ),
        "agent": (
            "delegation-skill+resources",
            "requires-host",
            ["subagents", "workflow-dependencies"],
            "Delegation is required. Tool restrictions are advisory; model overrides are not propagated.",
        ),
        "rule": (
            "resources",
            "advisory",
            ["rule-activation"],
            "The client must apply scope and precedence; delivery provides no always-on enforcement.",
        ),
        "hook": (
            "resources+hook-declaration",
            "unavailable",
            ["host-lifecycle-events", "host-execution"],
            "Source and declared bindings are exposed; the service does not receive host lifecycle events.",
        ),
        "mcp": (
            "resources+connection-definition",
            "requires-host",
            ["upstream-mcp-connections", "upstream-auth"],
            "The client must connect and authorize upstream MCPs; configurations do not register or proxy tools.",
        ),
    }
    representation, behavior, requirements, reason = values[kind]
    return Compatibility.model_validate(
        dict(representation=representation, behavior=behavior, requirements=requirements, reason=reason)
    )
