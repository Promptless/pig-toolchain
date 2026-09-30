"""Scanner for importing reusable agent assets and inventorying repo context."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from promptless_instruction_hub.assets import unsupported_support
from promptless_instruction_hub.config import PLUGIN_DIR, REPO_CONTEXT_PATH, load_hub_config, load_plugins
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import (
    JsonValue,
    file_hash,
    read_yaml_mapping,
    write_json,
    write_yaml,
)
from promptless_instruction_hub.mcp_config import read_mcp_servers
from promptless_instruction_hub.models import (
    PIG_PLUGIN_ID,
    PIG_PLUGIN_NAME,
    Harness,
    PluginDefinition,
    TargetSupport,
)

SKILL_SOURCE_DIRS = (Path(".agents/skills"), Path(".claude/skills"), Path(".cursor/skills"))
ROOT_MCP_CONFIG_CANDIDATES = (Path(".mcp.json"), Path("mcp.json"), Path("mcp.yaml"), Path("mcp.yml"))
CURSOR_MCP_CONFIG = Path(".cursor/mcp.json")
REPO_CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")


@dataclass(frozen=True)
class ScanResult:
    """Summary of imported reusable assets and inventoried repo context."""

    imported_skills: tuple[str, ...]
    imported_mcps: tuple[str, ...]
    inventoried_context_files: tuple[str, ...]


@dataclass(frozen=True)
class _SkillImport:
    source: Path
    skill_file: Path
    paths: tuple[Path, ...]


def scan_hub(hub_root: Path, source_root: Path) -> ScanResult:
    """Import reusable assets and inventory repo context from a source repo."""

    root = hub_root.resolve()
    source = source_root.resolve()
    load_hub_config(root)
    load_plugins(root)
    imported_skills = _import_skills(root, source)
    imported_mcps = _import_mcp_configs(root, source)
    imported_asset_refs = [f"skill:{skill_id}" for skill_id in imported_skills]
    imported_asset_refs.extend(f"mcp:{mcp_id}" for mcp_id in imported_mcps)
    _update_pig_plugin(root, imported_asset_refs)
    context_files = _inventory_repo_context(root, source)
    return ScanResult(
        imported_skills=tuple(imported_skills),
        imported_mcps=tuple(imported_mcps),
        inventoried_context_files=tuple(context_files),
    )


def _import_skills(hub_root: Path, source_root: Path) -> list[str]:
    imports: dict[str, _SkillImport] = {}
    for relative_root in SKILL_SOURCE_DIRS:
        skills_root = source_root / relative_root
        if not skills_root.exists() and not skills_root.is_symlink():
            continue
        _ensure_source_path_importable(skills_root, source_root, "source skills directory")
        if not skills_root.is_dir():
            msg = f"source skills directory must be a directory: {skills_root}"
            raise InstructionHubError(msg)
        for source_skill in sorted(skills_root.iterdir()):
            if source_skill.is_symlink():
                msg = f"source skill directory cannot be a symlink: {source_skill}"
                raise InstructionHubError(msg)
            if not source_skill.is_dir():
                continue
            _ensure_source_path_importable(source_skill, source_root, "source skill directory")
            skill_file = _find_skill_file(source_skill)
            if skill_file is None:
                msg = f"source skill directory must contain SKILL.md: {source_skill}"
                raise InstructionHubError(msg)
            asset_id = _slugify(source_skill.name)
            previous = imports.get(asset_id)
            if previous is not None:
                msg = f"source skill directories {previous.source} and {source_skill} both map to asset id {asset_id!r}"
                raise InstructionHubError(msg)
            paths = _skill_tree_paths(source_skill, source_root)
            imports[asset_id] = _SkillImport(source_skill, skill_file, paths)

    # Discover collisions and unsafe source paths before replacing any assets.
    for asset_id, skill in imports.items():
        destination = hub_root / "assets/skills" / asset_id
        _copy_skill_tree(skill, destination)
    return list(imports)


def _import_mcp_configs(hub_root: Path, source_root: Path) -> list[str]:
    imported: list[str] = []
    root_mcp_path = _first_existing_source_path(source_root, ROOT_MCP_CONFIG_CANDIDATES)
    root_servers: dict[str, JsonValue] = {}
    if root_mcp_path is not None:
        root_servers = _read_mcp_servers(root_mcp_path)
        _copy_mcp_config(
            hub_root=hub_root,
            source_root=source_root,
            source_path=root_mcp_path,
            asset_id="repo-mcp",
        )
        imported.append("repo-mcp")

    cursor_mcp_path = source_root / CURSOR_MCP_CONFIG
    if cursor_mcp_path.is_symlink():
        msg = f"source MCP config cannot be a symlink: {cursor_mcp_path}"
        raise InstructionHubError(msg)
    if not cursor_mcp_path.is_file():
        return imported
    _ensure_source_path_importable(cursor_mcp_path, source_root, "source MCP config")

    cursor_servers = _read_mcp_servers(cursor_mcp_path)
    if root_servers and _mcp_servers_subset(cursor_servers, root_servers):
        return imported

    _copy_mcp_config(
        hub_root=hub_root,
        source_root=source_root,
        source_path=cursor_mcp_path,
        asset_id="cursor-mcp",
        title="Cursor MCP Servers",
        support=_cursor_only_mcp_support(),
    )
    imported.append("cursor-mcp")
    return imported


def _update_pig_plugin(hub_root: Path, imported_asset_refs: list[str]) -> None:
    plugin_path = hub_root / PLUGIN_DIR / f"{PIG_PLUGIN_ID}.yaml"
    raw_plugin = (
        read_yaml_mapping(plugin_path) if plugin_path.exists() else {"id": PIG_PLUGIN_ID, "name": PIG_PLUGIN_NAME}
    )
    plugin = PluginDefinition.model_validate(raw_plugin)
    includes = set(plugin.includes)
    includes.update(imported_asset_refs)
    write_yaml(
        plugin_path,
        {
            "id": plugin.id,
            "name": plugin.name,
            "owners": plugin.owners,
            "includes": sorted(includes),
        },
    )


def _inventory_repo_context(hub_root: Path, source_root: Path) -> list[str]:
    files: list[dict[str, JsonValue]] = []
    for file_name in REPO_CONTEXT_FILES:
        source_path = source_root / file_name
        if not source_path.exists():
            continue
        files.append(
            {
                "path": file_name,
                "sha256": file_hash(source_path),
                "bytes": source_path.stat().st_size,
                "imported": False,
                "reason": "repo-specific context is inventoried but not converted into org-wide assets",
            }
        )
    write_json(
        hub_root / REPO_CONTEXT_PATH,
        {
            "schema_version": 1,
            "files": files,
        },
    )
    return [str(file["path"]) for file in files]


def _skill_tree_paths(source_skill: Path, source_root: Path) -> tuple[Path, ...]:
    skip_names = {".pytest_cache", ".ruff_cache", "__pycache__"}
    paths: list[Path] = []
    for source_path in sorted(source_skill.rglob("*")):
        relative_path = source_path.relative_to(source_skill)
        if any(part in skip_names for part in relative_path.parts):
            continue
        if source_path.is_symlink():
            msg = f"source skill contains a symlink that cannot be imported: {source_path}"
            raise InstructionHubError(msg)
        _ensure_source_path_importable(source_path, source_root, "source skill file")
        paths.append(source_path)
    return tuple(paths)


def _copy_skill_tree(skill: _SkillImport, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for source_path in skill.paths:
        relative_path = source_path.relative_to(skill.source)
        target_relative_path = Path("SKILL.md") if source_path == skill.skill_file else relative_path
        target_path = destination / target_relative_path
        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)


def _copy_mcp_config(
    *,
    hub_root: Path,
    source_root: Path,
    source_path: Path,
    asset_id: str,
    title: str | None = None,
    support: dict[Harness, TargetSupport] | None = None,
) -> None:
    _ensure_source_path_importable(source_path, source_root, "source MCP config")
    destination = hub_root / "assets/mcps" / f"{asset_id}{source_path.suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination)
    metadata_path = destination.with_suffix(".asset.yaml")
    if title is None and support is None:
        metadata_path.unlink(missing_ok=True)
        return
    metadata: dict[str, JsonValue] = {
        "source_path": str(source_path.relative_to(source_root)),
    }
    if title is not None:
        metadata["title"] = title
    if support is not None:
        metadata["support"] = {target: details.model_dump(exclude_none=True) for target, details in support.items()}
    write_yaml(
        metadata_path,
        metadata,
    )


def _find_skill_file(skill_dir: Path) -> Path | None:
    for child in sorted(skill_dir.iterdir()):
        if child.is_symlink():
            msg = f"source skill contains a symlink that cannot be imported: {child}"
            raise InstructionHubError(msg)
        if child.is_file() and child.name.lower() == "skill.md":
            return child
    return None


def _first_existing_source_path(source_root: Path, candidates: tuple[Path, ...]) -> Path | None:
    for relative_path in candidates:
        source_path = source_root / relative_path
        if source_path.is_symlink():
            msg = f"source MCP config cannot be a symlink: {source_path}"
            raise InstructionHubError(msg)
        if source_path.is_file():
            _ensure_source_path_importable(source_path, source_root, "source MCP config")
            return source_path
    return None


def _ensure_source_path_importable(source_path: Path, source_root: Path, label: str) -> None:
    if source_path.is_symlink():
        msg = f"{label} cannot be a symlink: {source_path}"
        raise InstructionHubError(msg)
    try:
        resolved_path = source_path.resolve(strict=True)
    except FileNotFoundError as exc:
        msg = f"{label} does not exist: {source_path}"
        raise InstructionHubError(msg) from exc
    if not resolved_path.is_relative_to(source_root):
        msg = f"{label} must stay inside source root: {source_path}"
        raise InstructionHubError(msg)


def _read_mcp_servers(path: Path) -> dict[str, JsonValue]:
    return read_mcp_servers(path, default_server_name=path.stem.removeprefix("."))


def _mcp_servers_subset(candidate_servers: dict[str, JsonValue], source_servers: dict[str, JsonValue]) -> bool:
    return all(source_servers.get(name) == server_config for name, server_config in candidate_servers.items())


def _cursor_only_mcp_support() -> dict[Harness, TargetSupport]:
    support = unsupported_support("Cursor-specific MCP config is only distributed to Cursor.")
    support["cursor"] = TargetSupport(mode="native")
    return support


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "skill"
