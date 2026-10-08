"""Versioned bundle contract. This module does not depend on the MCP SDK."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from promptless_instruction_hub.models import AssetKind, Harness, HookDefinition, MarketplaceDefinition, TargetSupport

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleFile(BundleModel):
    path: str
    sha256: Sha256
    size: int = Field(ge=0)
    mime_type: str

    @field_validator("path")
    @classmethod
    def contained_path(cls, value: str) -> str:
        if not value or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("bundle files require canonical relative POSIX paths")
        if any(ord(char) < 32 for char in value) or ":" in value:
            raise ValueError("bundle file path contains an unsafe character")
        return value


class Compatibility(BundleModel):
    representation: str
    behavior: Literal["requires-host", "advisory", "unavailable"]
    requirements: list[str]
    reason: str
    client_verification: Literal["unverified"] = "unverified"


class BundleSkill(BundleModel):
    frontmatter: dict[str, JsonValue]
    entrypoint: str
    files: list[str]


class SkillResource(BundleModel):
    uri: str
    digest: str
    size: int


class SkillEntry(BundleModel):
    uri: str
    frontmatter: dict[str, JsonValue]
    resources: list[SkillResource]


class BundlePrompt(BundleModel):
    name: str
    description: str
    body: str


class BundleAsset(BundleModel):
    ref: str
    id: str
    kind: AssetKind
    title: str
    plugins: list[str]
    source_hash: Sha256
    source_support: dict[Harness, TargetSupport]
    files: list[BundleFile] = Field(min_length=1)
    compatibility: Compatibility
    hook: HookDefinition | None = None
    skill: BundleSkill | None = None
    prompt: BundlePrompt | None = None


class BundlePlugin(BundleModel):
    id: str
    name: str
    kind: Literal["authored", "external"]
    source: dict[str, JsonValue] | None = None
    limitation: str | None = None


class BundleManifest(BundleModel):
    schema_version: Literal[1] = 1
    bundle_id: Sha256
    org: str
    marketplace: MarketplaceDefinition
    version: str
    plugins: list[BundlePlugin]
    assets: list[BundleAsset]
    limitations: list[str]
