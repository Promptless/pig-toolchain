"""Read-only MCP distribution over stdio or authenticated Streamable HTTP."""

from __future__ import annotations

import hmac
import ipaddress
import json
from pathlib import Path
from typing import Literal, TypeVar

import anyio
from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from promptless_instruction_hub.mcp_distribution.catalog import Catalog
from promptless_instruction_hub.mcp_distribution.models import SkillEntry
from promptless_instruction_hub.models import AssetKind

SKILLS_EXTENSION = "io.modelcontextprotocol/skills"
T = TypeVar("T")


class GetSkillParams(types.RequestParams):
    uri: str = Field(min_length=1, max_length=4096)


class ListSkillsResult(types.Result):
    skills: list[SkillEntry]
    nextCursor: str | None = None
    resultType: Literal["complete"] = "complete"
    ttlMs: int = 0
    cacheScope: Literal["private"] = "private"


class GetSkillResult(types.Result):
    skill: SkillEntry
    resultType: Literal["complete"] = "complete"
    ttlMs: int = 0
    cacheScope: Literal["private"] = "private"


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(default="", max_length=1000)
    kind: AssetKind | None = None
    cursor: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=20, ge=1, le=50)


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ref: str = Field(min_length=1, max_length=256)
    path: str | None = Field(default=None, max_length=4096)


def create_server(catalog: Catalog) -> Server:
    """Every method sees the same immutable, selected catalog."""

    def page(items: list[T], params: types.PaginatedRequestParams | None, key: str) -> tuple[list[T], str | None]:
        try:
            return catalog.page(items, params.cursor if params else None, key)
        except ValueError as exc:
            raise MCPError(types.INVALID_PARAMS, str(exc)) from exc

    async def list_skills(ctx: ServerRequestContext, params: types.PaginatedRequestParams) -> ListSkillsResult:
        skills, cursor = page(list(catalog.skills.values()), params, "skills")
        return ListSkillsResult(skills=skills, nextCursor=cursor)

    async def get_skill(ctx: ServerRequestContext, params: GetSkillParams) -> GetSkillResult:
        skill = catalog.skills.get(params.uri)
        if skill is None:
            raise MCPError(types.INVALID_PARAMS, "skill not found in this bundle and plugin selection")
        return GetSkillResult(skill=skill)

    async def list_resources(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        resources, cursor = page(list(catalog.resources.items()), params, "resources")
        entries: list[types.Resource] = []
        for uri, (asset, file, data) in resources:
            skill = catalog.skills.get(uri)
            if skill is not None:
                name = str(skill.frontmatter["name"])
                description = str(skill.frontmatter["description"])
            else:
                name = file.path if file else "catalog.json"
                description = f"{asset.ref}: {asset.compatibility.reason}" if asset else "Selected hub catalog"
            entries.append(
                types.Resource(
                    uri=uri,
                    name=name,
                    description=description,
                    mime_type=file.mime_type if file else "application/json",
                    size=len(data),
                )
            )
        return types.ListResourcesResult(resources=entries, next_cursor=cursor)

    async def read_resource(
        ctx: ServerRequestContext, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        try:
            content = catalog.read(str(params.uri))
        except ValueError as exc:
            raise MCPError(types.INVALID_PARAMS, str(exc)) from exc
        return types.ReadResourceResult.model_validate({"contents": [content]})

    async def list_prompts(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        assets, cursor = page(list(catalog.prompts.values()), params, "prompts")
        return types.ListPromptsResult(
            prompts=[
                types.Prompt(name=asset.prompt.name, description=asset.prompt.description)
                for asset in assets
                if asset.prompt is not None
            ],
            next_cursor=cursor,
        )

    async def get_prompt(ctx: ServerRequestContext, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        asset = catalog.prompts.get(params.name)
        if asset is None or asset.prompt is None:
            raise MCPError(types.INVALID_PARAMS, "prompt not found in this bundle and plugin selection")
        if params.arguments:
            raise MCPError(types.INVALID_PARAMS, "this command prompt takes no arguments")
        return types.GetPromptResult(
            description=asset.prompt.description,
            messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=asset.prompt.body))],
        )

    annotations = types.ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    tools = [
        types.Tool(
            name="search_assets",
            description=(
                "Discover Instruction Hub skills, commands, agents, rules, hooks, and MCP configurations. "
                "Returns plugin memberships, compatibility requirements, and file paths. An empty query lists assets. "
                "Discovery does not activate instructions or grant execution permission."
            ),
            input_schema=SearchArguments.model_json_schema(),
            annotations=annotations,
        ),
        types.Tool(
            name="read_asset",
            description=(
                "Retrieve an Instruction Hub asset and its compatibility requirements. Supply a ref from search_assets. "
                "Omit path to read its skill entrypoint or first source file; use an exact returned path for other files. "
                "This only reads content. Commands require explicit user invocation; hooks are not registered; "
                "scripts and delegated agents require host capabilities and authorization."
            ),
            input_schema=ReadArguments.model_json_schema(),
            annotations=annotations,
        ),
    ]

    async def list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        selected, cursor = page(tools, params, "tools")
        return types.ListToolsResult(tools=selected, next_cursor=cursor)

    async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            if params.name == "search_assets":
                args = SearchArguments.model_validate(params.arguments or {})
                words = args.query.casefold().split()
                matches = [
                    asset
                    for asset in catalog.assets.values()
                    if (args.kind is None or asset.kind == args.kind)
                    and all(
                        word
                        in " ".join(
                            (
                                asset.ref,
                                asset.title,
                                str(asset.skill.frontmatter.get("description", "")) if asset.skill else "",
                                asset.prompt.description if asset.prompt else "",
                                " ".join(asset.plugins),
                            )
                        ).casefold()
                        for word in words
                    )
                ]
                found, cursor = catalog.page(matches, args.cursor, f"search:{args.kind}:{args.query}", args.limit)
                result = {
                    "bundle_id": catalog.manifest.bundle_id,
                    "plugins": [
                        plugin.model_dump(mode="json")
                        for plugin in catalog.manifest.plugins
                        if plugin.id in catalog.plugins
                    ],
                    "limitations": catalog.manifest.limitations,
                    "assets": [catalog.describe(asset) for asset in found],
                    "next_cursor": cursor,
                }
            elif params.name == "read_asset":
                read_args = ReadArguments.model_validate(params.arguments or {})
                asset = catalog.assets.get(read_args.ref)
                if asset is None:
                    raise ValueError("asset not found in this bundle and plugin selection")
                path = read_args.path or (asset.skill.entrypoint if asset.skill else asset.files[0].path)
                if path not in {file.path for file in asset.files}:
                    raise ValueError("file not found in this asset")
                result = {"asset": catalog.describe(asset), "content": catalog.read(catalog.file_uris[path])}
            else:
                raise MCPError(types.INVALID_PARAMS, "unknown tool")
        except ValueError as exc:
            return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc))], is_error=True)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, sort_keys=True))], structured_content=result
        )

    server = Server(
        "instruction-hub",
        version=catalog.manifest.version,
        instructions=(
            f"Instruction Hub: {catalog.manifest.marketplace.name}. "
            "Use skills/list and resources/read, or search_assets and read_asset, to retrieve selected assets. "
            "Read each asset's compatibility requirements before applying it. Retrieval grants no permission "
            "to execute scripts, activate commands, spawn agents, register hooks, or connect upstream MCPs. "
            f"Full selected catalog: {catalog.catalog_uri}"
        ),
        on_list_resources=list_resources,
        on_read_resource=read_resource,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    server.extensions[SKILLS_EXTENSION] = {}
    server.add_request_handler("skills/list", types.PaginatedRequestParams, list_skills)
    server.add_request_handler("skills/get", GetSkillParams, get_skill)
    return server


class BearerTokenMiddleware:
    """A single deployment credential, not delegated OAuth or per-user authorization."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            values = Headers(scope=scope).getlist("authorization")
            if len(values) != 1 or not hmac.compare_digest(values[0].encode("utf-8"), self.expected):
                response = Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_app(
    catalog: Catalog,
    *,
    host: str = "127.0.0.1",
    token: str | None = None,
    allowed_hosts: list[str] | None = None,
) -> Starlette:
    """Serve a fixed plugin view. External binds require a deployment credential."""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if token is not None and (not token.strip() or any(char.isspace() for char in token)):
        raise ValueError("MCP bearer token must be nonempty and contain no whitespace")
    if not loopback and not token:
        raise ValueError("non-loopback MCP HTTP requires a bearer token environment variable")
    hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
    hosts.extend(allowed_hosts or [])
    app = create_server(catalog).streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=host,
        max_request_body_size=64 * 1024,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[]),
    )
    if token is not None:
        app.add_middleware(BearerTokenMiddleware, token=token)
    return app


def serve(
    bundle: Path,
    *,
    plugins: set[str] | None = None,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    token: str | None = None,
    allowed_hosts: list[str] | None = None,
) -> None:
    catalog = Catalog(bundle, plugins)
    if transport == "streamable-http":
        import uvicorn

        uvicorn.run(create_http_app(catalog, host=host, token=token, allowed_hosts=allowed_hosts), host=host, port=port)
    elif transport == "stdio":
        server = create_server(catalog)

        async def run() -> None:
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())

        anyio.run(run)
    else:
        raise ValueError(f"unsupported MCP transport: {transport}")
