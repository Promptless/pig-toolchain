"""Stable plugin-prefixed skill loading tools, independent of the MCP SDK."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.mcp_distribution.models import BundleAsset

MAX_TOOL_NAME_LENGTH = 64


@dataclass(frozen=True)
class SkillTool:
    """One plugin's discoverable alias for a skill or delegation skill."""

    name: str
    plugin: str
    asset: BundleAsset

    @property
    def description(self) -> str:
        """Preserve the authored trigger and explain the loading operation."""
        skill = self.asset.skill
        if skill is None:
            raise ValueError("skill tool requires a skill projection")
        return (
            f"{skill.frontmatter['description']} "
            "Loads the workflow instructions and supporting-file references. "
            "Follow the returned instructions using your available capabilities; this tool only retrieves content."
        )


def project_skill_tools(assets: Iterable[BundleAsset]) -> dict[str, SkillTool]:
    """Project every selected skill membership, rejecting ambiguous tool names."""
    result: dict[str, SkillTool] = {}
    for asset in sorted(assets, key=lambda item: item.ref):
        if asset.skill is None:
            continue
        for plugin in sorted(set(asset.plugins)):
            name = _tool_name(plugin, asset)
            if name in result:
                raise InstructionHubError(f"MCP skill tool name collision: {name}")
            result[name] = SkillTool(name=name, plugin=plugin, asset=asset)
    return dict(sorted(result.items()))


def _tool_name(plugin: str, asset: BundleAsset) -> str:
    """Keep the entire plugin prefix and shorten only long asset IDs with a digest."""
    prefix = f"{plugin}__load_{asset.kind}_"
    available = MAX_TOOL_NAME_LENGTH - len(prefix)
    if len(asset.id) <= available:
        return prefix + asset.id
    if available < 10:
        raise InstructionHubError(
            f"{asset.ref}: plugin id {plugin!r} is too long for a 64-character MCP skill tool name; "
            "shorten the plugin id"
        )
    digest = hashlib.sha256(asset.ref.encode()).hexdigest()[:8]
    return f"{prefix}{asset.id[: available - 9]}_{digest}"
