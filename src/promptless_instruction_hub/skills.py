"""Validate authored skill metadata and normalize portable skill entrypoints."""

from __future__ import annotations

import json
from dataclasses import dataclass

import yaml

from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.models import IDENTIFIER_RE, LoadedAsset


@dataclass(frozen=True)
class Skill:
    """An authored invocation name and its validated, publishable Markdown."""

    name: str
    contents: str


def read_skill(asset: LoadedAsset) -> Skill:
    """Keep authored host fields, requiring usable name and description metadata."""

    path = asset.path
    if path.is_dir():
        candidates = [p for p in sorted(path.iterdir()) if p.is_file() and p.name.lower() == "skill.md"]
        if len(candidates) != 1:
            raise InstructionHubError(f"{asset.ref}: expected exactly one SKILL.md entrypoint")
        path = candidates[0]
    contents = path.read_text(encoding="utf-8")
    lines = contents.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        closing = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
        if closing is None:
            raise InstructionHubError(f"{asset.ref}: skill YAML frontmatter must end with ---")
        frontmatter = "".join(lines[1:closing])
        try:
            node = yaml.compose(frontmatter, Loader=yaml.SafeLoader)
            if not isinstance(node, yaml.MappingNode):
                raise InstructionHubError(f"{asset.ref}: skill frontmatter must be a YAML mapping")
            keys: set[str] = set()
            for key, _ in node.value:
                if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                    raise InstructionHubError(
                        f"{asset.ref}: skill frontmatter keys must be strings; merge keys are unsupported"
                    )
                if key.value in keys:
                    raise InstructionHubError(f"{asset.ref}: duplicate skill frontmatter field {key.value!r}")
                keys.add(key.value)
            metadata = yaml.safe_load(frontmatter)
        except yaml.YAMLError as exc:
            raise InstructionHubError(f"{asset.ref}: malformed skill YAML frontmatter") from exc
        name = metadata.get("name")
        description = metadata.get("description")
    else:
        name = asset.id
        description = asset.metadata.title or asset.id
        frontmatter = f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(description)}\n---\n"
        contents = frontmatter + (f"\n{contents.rstrip()}\n" if contents.strip() else "")
    if not isinstance(name, str) or len(name) > 64 or IDENTIFIER_RE.fullmatch(name) is None or "--" in name:
        raise InstructionHubError(
            f"{asset.ref}: skill name must be a kebab-case identifier of 1 to 64 characters without consecutive hyphens"
        )
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise InstructionHubError(
            f"{asset.ref}: skill description must be a nonempty string of at most 1024 characters"
        )
    return Skill(name=name, contents=contents if contents.endswith("\n") else contents + "\n")
