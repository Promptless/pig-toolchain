from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from promptless_instruction_hub.compiler import build_hub, init_hub
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.validate.hub import validate_hub


def _hub(tmp_path: Path, contents: str, *, filename: str = "SKILL.md") -> Path:
    init_hub(tmp_path, org="Example")
    skill = tmp_path / "assets/skills/review-docs"
    skill.mkdir(parents=True)
    (skill / filename).write_text(contents)
    (skill / "references").mkdir()
    (skill / "references/checklist.md").write_text("Keep this supporting file.\n")
    (tmp_path / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes: [skill:review-docs]\n")
    return skill


@pytest.mark.parametrize("filename", ["SKILL.md", "skill.md", "Skill.md"])
@pytest.mark.parametrize("mode", ["agent-skill", "native"])
def test_skill_entrypoint_and_plain_markdown_metadata_are_portable(tmp_path: Path, filename: str, mode: str) -> None:
    skill = _hub(tmp_path, "# Review Docs\n\nCheck the document.\n", filename=filename)
    if mode == "native":
        (skill / "asset.yaml").write_text(
            yaml.safe_dump({"support": {target: {"mode": mode} for target in ("claude", "codex", "gemini", "cursor")}})
        )
    build_hub(tmp_path)
    for target in ("claude", "codex", "gemini", "cursor"):
        if mode == "native" and target == "cursor":
            assert (tmp_path / f"dist/{target}/pig/rules/review-docs.mdc").exists()
            continue
        output = tmp_path / f"dist/{target}/pig/skills/review-docs"
        entrypoints = [p.name for p in output.iterdir() if p.name.lower() == "skill.md"]
        assert entrypoints == ["SKILL.md"]
        text = (output / "SKILL.md").read_text()
        metadata = yaml.safe_load(text.split("---", 2)[1])
        assert metadata == {"name": "review-docs", "description": "Review Docs"}
        assert text.endswith("# Review Docs\n\nCheck the document.\n")
        assert (output / "references/checklist.md").read_text() == "Keep this supporting file.\n"
        assert not (output / "asset.yaml").exists()
    assert (skill / filename).read_text() == "# Review Docs\n\nCheck the document.\n"


def test_valid_skill_metadata_and_host_extensions_are_preserved(tmp_path: Path) -> None:
    contents = '---\nname: customer-alias\ndescription: "Use when reviewing a document."\ndisable-model-invocation: true\nallowed-tools: Read\nmetadata:\n  owner: example\n---\n\nKeep exact body.'
    _hub(tmp_path, contents, filename="skill.md")
    build_hub(tmp_path)
    for target in ("claude", "codex", "gemini", "cursor"):
        assert (tmp_path / f"dist/{target}/pig/skills/review-docs/SKILL.md").read_text() == contents + "\n"


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: review-docs",
        "description: Good",
        "name: review-docs\ndescription: ''",
        "name: review-docs\ndescription: '  '",
        "name: review-docs\ndescription: [bad]",
        "name: 42\ndescription: Good",
        "name: BAD\ndescription: Good",
        "name: a--b\ndescription: Good",
        "name: ' padded '\ndescription: Good",
        "name: " + "a" * 65 + "\ndescription: Good",
        "name: review-docs\ndescription: " + "a" * 1025,
        "name: review-docs\ndescription: first\ndescription: second",
        "name: review-docs\nname: alias\ndescription: Good",
        "[name, description]",
        "name: [broken",
        "? [complex, key]\n: value",
        "<<: {name: review-docs, description: merged}",
        "",
    ],
)
def test_invalid_authored_skill_metadata_fails_before_generated_output(tmp_path: Path, frontmatter: str) -> None:
    _hub(tmp_path, f"---\n{frontmatter}\n---\nBody.\n")
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(InstructionHubError, match="skill"):
        build_hub(tmp_path)
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_unclosed_frontmatter_does_not_get_replaced_by_synthetic_metadata(tmp_path: Path) -> None:
    _hub(tmp_path, "---\nname: review-docs\ndescription: Good\nBody.\n")
    with pytest.raises(InstructionHubError, match="must end"):
        validate_hub(tmp_path)


def test_skill_with_duplicate_case_entrypoints_is_rejected(tmp_path: Path) -> None:
    skill = _hub(tmp_path, "# Review Docs\n")
    (skill / "skill.md").write_text("# Different entrypoint\n")
    if len([p for p in skill.iterdir() if p.name.lower() == "skill.md"]) != 2:
        pytest.skip("filesystem is case insensitive")
    with pytest.raises(InstructionHubError, match="exactly one"):
        build_hub(tmp_path)


def test_skill_validation_applies_without_codex_enabled(tmp_path: Path) -> None:
    _hub(tmp_path, "---\nname: review-docs\n---\nBody.\n")
    config_path = tmp_path / "hub.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["targets"] = ["claude", "cursor", "gemini"]
    config_path.write_text(yaml.safe_dump(config))
    with pytest.raises(InstructionHubError, match="description"):
        build_hub(tmp_path)


def test_repeated_skill_build_is_deterministic(tmp_path: Path) -> None:
    _hub(tmp_path, "# Review Docs\n", filename="skill.md")
    build_hub(tmp_path)
    before = json.loads((tmp_path / "hub.release.json").read_text())
    build_hub(tmp_path)
    assert json.loads((tmp_path / "hub.release.json").read_text()) == before
