"""Verified, in-memory catalog shared by every MCP retrieval path."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.fs import JsonValue
from promptless_instruction_hub.mcp_distribution.compiler import (
    MANIFEST_NAME,
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_MANIFEST_BYTES,
    MAX_SKILL_BYTES,
    MAX_SKILL_FILES,
    read_frontmatter,
)
from promptless_instruction_hub.mcp_distribution.models import (
    BundleAsset,
    BundleFile,
    BundleManifest,
    SkillEntry,
    SkillResource,
)
from promptless_instruction_hub.models import validate_identifier
from promptless_instruction_hub.release.hashing import stable_hash

T = TypeVar("T")


class Catalog:
    """One immutable release and one selected plugin view; no request-time file I/O."""

    def __init__(self, root: Path, plugins: set[str] | None = None) -> None:
        root = root.resolve()
        manifest_path = root / MANIFEST_NAME
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.stat().st_size > MAX_MANIFEST_BYTES
        ):
            raise InstructionHubError("MCP bundle manifest must be a regular file no larger than 16 MiB")
        self.manifest = BundleManifest.model_validate_json(manifest_path.read_bytes())
        expected = stable_hash(self.manifest.model_dump(mode="json", exclude={"bundle_id"}))
        if self.manifest.bundle_id != expected:
            raise InstructionHubError("MCP bundle identity does not match its manifest")
        plugin_ids = [plugin.id for plugin in self.manifest.plugins]
        if len(set(plugin_ids)) != len(plugin_ids):
            raise InstructionHubError("MCP bundle has duplicate plugin ids")
        for plugin_id in plugin_ids:
            validate_identifier(plugin_id, "bundle plugin id")
        self.plugins = set(plugin_ids) if plugins is None else set(plugins)
        if not self.plugins <= set(plugin_ids):
            raise InstructionHubError(
                f"unknown MCP plugin selection: {', '.join(sorted(self.plugins - set(plugin_ids)))}"
            )
        all_files = [file for asset in self.manifest.assets for file in asset.files]
        if len(all_files) > MAX_BUNDLE_FILES or sum(file.size for file in all_files) > MAX_BUNDLE_BYTES:
            raise InstructionHubError("MCP bundle exceeds 10,000 files or 256 MiB")
        paths = [file.path for file in all_files]
        if len({path.casefold() for path in paths}) != len(paths):
            raise InstructionHubError("MCP bundle has duplicate file paths")
        refs = [asset.ref for asset in self.manifest.assets]
        if len(set(refs)) != len(refs):
            raise InstructionHubError("MCP bundle has duplicate asset references")
        self.assets: dict[str, BundleAsset] = {}
        self.resources: dict[str, tuple[BundleAsset | None, BundleFile | None, bytes]] = {}
        self.skills: dict[str, SkillEntry] = {}
        self.prompts: dict[str, BundleAsset] = {}
        self.file_uris: dict[str, str] = {}
        for asset in self.manifest.assets:
            validate_identifier(asset.id, "bundle asset id")
            if (
                asset.ref != f"{asset.kind}:{asset.id}"
                or not asset.plugins
                or not set(asset.plugins) <= set(plugin_ids)
            ):
                raise InstructionHubError(f"invalid identity or plugin membership for {asset.ref}")
            contents = {file.path: _read_verified(root, file) for file in asset.files}
            _validate_projection(asset, contents)
            if not self.plugins.intersection(asset.plugins):
                continue
            selected = asset.model_copy(update={"plugins": sorted(self.plugins.intersection(asset.plugins))})
            self.assets[asset.ref] = selected
            for file in asset.files:
                uri = self._file_uri(asset, file)
                self.file_uris[file.path] = uri
                self.resources[uri] = (selected, file, contents[file.path])
            if asset.skill is not None:
                uri = self.file_uris[asset.skill.entrypoint]
                self.skills[uri] = SkillEntry(
                    uri=uri,
                    frontmatter=asset.skill.frontmatter,
                    resources=[
                        SkillResource(uri=self.file_uris[file.path], digest=f"sha256:{file.sha256}", size=file.size)
                        for file in asset.files
                        if file.path in asset.skill.files
                    ],
                )
            if asset.prompt is not None:
                if asset.prompt.name in self.prompts:
                    raise InstructionHubError("MCP bundle has duplicate prompt names")
                self.prompts[asset.prompt.name] = selected
        self.catalog_uri = f"hub://{self.manifest.marketplace.id}/{self.manifest.bundle_id}/catalog.json"
        catalog = self.manifest.model_dump(mode="json", exclude={"assets", "plugins"})
        catalog["plugins"] = [p.model_dump(mode="json") for p in self.manifest.plugins if p.id in self.plugins]
        catalog["assets"] = [self.describe(asset) for asset in self.assets.values()]
        self.resources[self.catalog_uri] = (None, None, (json.dumps(catalog, sort_keys=True, indent=2) + "\n").encode())

    def _file_uri(self, asset: BundleAsset, file: BundleFile) -> str:
        prefix = f"{self.manifest.marketplace.id}/{self.manifest.bundle_id}"
        if asset.skill is not None and file.path in asset.skill.files:
            relative = Path(file.path).relative_to(Path(asset.skill.entrypoint).parent).as_posix()
            return f"skill://{prefix}/{asset.kind}/{asset.id}/{asset.skill.frontmatter['name']}/{quote(relative, safe='/')}"
        return f"hub://{prefix}/{quote(file.path, safe='/')}"

    def describe(self, asset: BundleAsset) -> dict[str, JsonValue]:
        result = asset.model_dump(mode="json", exclude={"files", "skill", "prompt"})
        result["files"] = [dict(file.model_dump(), uri=self.file_uris[file.path]) for file in asset.files]
        result["skill_uri"] = self.file_uris[asset.skill.entrypoint] if asset.skill else None
        result["prompt_name"] = asset.prompt.name if asset.prompt else None
        return result

    def read(self, uri: str) -> dict[str, str]:
        """Exact catalog lookup prevents traversal and access to unselected assets."""
        try:
            _, file, data = self.resources[uri]
        except KeyError as exc:
            raise ValueError("resource not found in this bundle and plugin selection") from exc
        mime_type = file.mime_type if file else "application/json"
        result = {"uri": uri, "mimeType": mime_type}
        if mime_type.startswith("text/") or mime_type in {"application/json", "image/svg+xml"}:
            try:
                return dict(result, text=data.decode("utf-8"))
            except UnicodeDecodeError:
                pass  # The binary representation preserves bytes even for mislabeled text.
        return dict(result, blob=base64.b64encode(data).decode("ascii"))

    def page(self, items: list[T], cursor: str | None, key: str, limit: int = 50) -> tuple[list[T], str | None]:
        scope = stable_hash(
            {"bundle": self.manifest.bundle_id, "plugins": sorted(self.plugins), "key": key, "limit": limit}
        )
        offset = 0
        if cursor is not None:
            prefix, separator, position = cursor.partition(":")
            if (
                prefix != scope
                or not separator
                or not position.isascii()
                or not position.isdigit()
                or len(position) > 10
            ):
                raise ValueError("invalid cursor for this catalog query")
            offset = int(position)
            if offset >= len(items):
                raise ValueError("cursor is outside this catalog query")
        end = offset + limit
        return items[offset:end], f"{scope}:{end}" if end < len(items) else None


def _read_verified(root: Path, file: BundleFile) -> bytes:
    path = root / file.path
    if path.resolve() != path or not stat.S_ISREG(path.stat().st_mode):
        raise InstructionHubError(f"MCP bundle contains a symlink or non-regular file: {file.path}")
    if path.stat().st_size != file.size:
        raise InstructionHubError(f"MCP bundle size mismatch: {file.path}")
    data = path.read_bytes()
    if len(data) != file.size or hashlib.sha256(data).hexdigest() != file.sha256:
        raise InstructionHubError(f"MCP bundle digest mismatch: {file.path}")
    return data


def _validate_projection(asset: BundleAsset, contents: dict[str, bytes]) -> None:
    if asset.skill is not None:
        if asset.kind not in {"skill", "agent"}:
            raise InstructionHubError(f"{asset.ref}: only skills and agents can have a skill projection")
        skill = asset.skill
        paths = set(skill.files)
        if len(paths) != len(skill.files) or skill.entrypoint not in paths or not paths <= contents.keys():
            raise InstructionHubError(f"{asset.ref}: invalid skill file membership")
        root = Path(skill.entrypoint).parent
        if Path(skill.entrypoint).name != "SKILL.md" or any(not Path(path).is_relative_to(root) for path in paths):
            raise InstructionHubError(f"{asset.ref}: skill resources must stay in the skill directory")
        if paths != {path for path in contents if Path(path).is_relative_to(root)}:
            raise InstructionHubError(f"{asset.ref}: skill file manifest is incomplete")
        if len(paths) > MAX_SKILL_FILES or sum(len(contents[path]) for path in paths) > MAX_SKILL_BYTES:
            raise InstructionHubError(f"{asset.ref}: skill exceeds extension size bounds")
        if read_frontmatter(contents[skill.entrypoint]) != skill.frontmatter:
            raise InstructionHubError(f"{asset.ref}: skill frontmatter does not match served bytes")
    if asset.prompt is not None and (asset.kind != "command" or asset.prompt.name != f"command:{asset.id}"):
        raise InstructionHubError(f"{asset.ref}: invalid command prompt identity")
