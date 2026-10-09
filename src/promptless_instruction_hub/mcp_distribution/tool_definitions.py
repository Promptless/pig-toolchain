"""MCP SDK definitions shared by local and hosted skill distribution servers."""

from mcp import types

from promptless_instruction_hub.mcp_distribution.catalog import Catalog


def skill_tool_definitions(catalog: Catalog) -> list[types.Tool]:
    """Advertise each plugin skill as a read-only tool accepting no arguments."""
    return [
        types.Tool(
            name=tool.name,
            description=tool.description,
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=types.ToolAnnotations(
                read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
            ),
        )
        for tool in catalog.skill_tools.values()
    ]
