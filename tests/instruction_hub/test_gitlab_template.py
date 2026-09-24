from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from .helpers import REPO_ROOT, _git, _git_output, _init_action_repo, _remote_branch_exists


def _render_template(inputs: dict[str, str] | None = None) -> str:
    text = (REPO_ROOT / "templates/gitlab/instruction-hub.yml").read_text()
    spec, _ = yaml.safe_load_all(text)
    for name, definition in spec["spec"]["inputs"].items():
        value = (inputs or {}).get(name, definition["default"])
        if "regex" in definition and re.fullmatch(definition["regex"], value) is None:
            raise ValueError(f"invalid template input: {name}")
        text = text.replace(f"$[[ inputs.{name} ]]", value)
    assert "$[[ inputs." not in text
    return text


def _run_template_job(
    repo: Path,
    job: str = "publish",
    *,
    hub_root: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    inputs = {"release-branch": "release/preview"}
    if hub_root is not None:
        inputs["hub-root"] = hub_root
    _, template = yaml.safe_load_all(_render_template(inputs))
    # Execute the real shared validation and job scripts, using this checkout
    # instead of downloading the toolchain in the remaining bootstrap steps.
    script = [template[".instruction-hub"]["before_script"][0], *template[f"instruction-hub-{job}"]["script"]]
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", "\n".join(script)],
        cwd=repo,
        env={
            **os.environ,
            **template[".instruction-hub"]["variables"],
            "GITHUB_ACTIONS": "false",
            "INPUT_GITHUB_TOKEN": "",
            "CI_PROJECT_DIR": str(repo),
            "CI_REPOSITORY_URL": str(repo.parent / "remote.git"),
            "CI_COMMIT_BRANCH": "main",
            "CI_DEFAULT_BRANCH": "main",
            "CI_COMMIT_TAG": "",
            "CI_SERVER_URL": "https://gitlab.example.com",
            "CI_SERVER_HOST": "gitlab.example.com",
            "CI_PROJECT_PATH": "group/subgroup/hub",
            "INSTRUCTION_HUB_TOOLCHAIN_DIR": str(REPO_ROOT),
            **(extra_env or {}),
        },
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("hub_root", [".", "docs/hub"])
def test_gitlab_check_builds_without_publishing(tmp_path: Path, hub_root: str) -> None:
    repo = _init_action_repo(tmp_path / "check", targets=("claude", "codex", "cursor"), hub_root_name=hub_root)
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach")

    result = _run_template_job(
        repo,
        "check",
        hub_root=None if hub_root == "." else hub_root,
        extra_env={"INSTRUCTION_HUB_ROOT": "must-not-override-the-input"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*").strip() == f"{initial}\trefs/heads/main"
    assert _git_output(repo, "status", "--short") == ""


@pytest.mark.parametrize(
    "extra_env",
    [
        {"CI_COMMIT_BRANCH": "feature"},
        {"CI_COMMIT_BRANCH": ""},
        {"CI_COMMIT_TAG": "v1.0.0"},
        {"CI_REPOSITORY_URL": "/nonexistent/instruction-hub.git"},
    ],
)
def test_gitlab_publish_rejects_unsafe_refs_and_fetch_failure(tmp_path: Path, extra_env: dict[str, str]) -> None:
    repo = _init_action_repo(tmp_path / "rejected", targets=("cursor",))
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach")

    result = _run_template_job(repo, extra_env=extra_env)

    assert result.returncode != 0
    assert _git_output(repo, "rev-parse", "HEAD").strip() == initial
    assert _git_output(repo.parent / "remote.git", "for-each-ref", "--format=%(refname)").strip() == "refs/heads/main"


@pytest.mark.parametrize(
    "changed_path",
    [".gitlab-ci.yml", ".gitignore", "hub.yaml", "hub.repo-context.json", "assets/new.md", "plugins/new.yaml"],
)
@pytest.mark.parametrize("hub_root", [".", "docs/hub"])
def test_gitlab_publish_skips_superseded_source(tmp_path: Path, changed_path: str, hub_root: str) -> None:
    repo = _init_action_repo(tmp_path / "superseded", targets=("cursor",), hub_root_name=hub_root)
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    if hub_root != "." and changed_path not in {".gitlab-ci.yml", ".gitignore"}:
        changed_path = f"{hub_root}/{changed_path}"
    with (repo / changed_path).open("a") as source:
        source.write("\n# Updated hub source\n")
    _git(repo, "add", changed_path)
    _git(repo, "commit", "-m", "newer hub source")
    _git(repo, "push", "origin", "main")
    latest = _git_output(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach", initial)

    result = _run_template_job(repo, hub_root=hub_root)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "a newer pipeline owns publication" in result.stdout
    assert _git_output(repo, "rev-parse", "HEAD").strip() == initial
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*").strip() == f"{latest}\trefs/heads/main"


@pytest.mark.parametrize("hub_root", [".", "docs/hub"])
def test_gitlab_publish_fast_forwards_pointer_commits_and_is_idempotent(tmp_path: Path, hub_root: str) -> None:
    repo = _init_action_repo(tmp_path / "publish", targets=("claude", "codex", "cursor"), hub_root_name=hub_root)
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach")

    first = _run_template_job(repo, hub_root=None if hub_root == "." else hub_root)

    assert first.returncode == 0, first.stdout + first.stderr
    assert _remote_branch_exists(repo, "release/preview")
    published_refs = _git_output(repo, "ls-remote", "origin", "refs/heads/*").strip()
    prefix = "" if hub_root == "." else f"{hub_root}/"
    for path in (
        ".claude-plugin/marketplace.json",
        ".agents/plugins/marketplace.json",
        ".cursor-plugin/marketplace.json",
    ):
        pointer = _git_output(repo, "show", f"origin/main:{prefix}{path}")
        assert "https://gitlab.example.com/group/subgroup/hub.git" in pointer
    cursor = json.loads(_git_output(repo, "show", f"origin/main:{prefix}.cursor-plugin/marketplace.json"))
    assert cursor["plugins"][0]["source"]["ref"] == "release/preview"
    assert cursor["plugins"][0]["source"]["path"] == f"{prefix}dist/cursor/pig"
    assert (
        _git_output(repo, "log", "-1", "--format=%an <%ae>", "origin/main").strip()
        == "GitLab CI <gitlab-ci@gitlab.example.com>"
    )

    # A second queued job still starts at the commit before the pointer update.
    _git(repo, "checkout", "--detach", initial)
    second = _run_template_job(repo, hub_root=None if hub_root == "." else hub_root)

    assert second.returncode == 0, second.stdout + second.stderr
    assert "a newer pipeline owns publication" not in second.stdout
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*").strip() == published_refs
    assert _git_output(repo, "rev-parse", "HEAD").strip() == _git_output(repo, "rev-parse", "origin/main").strip()


@pytest.mark.parametrize("changed_path", ["docs/hub/.gitignore", "docs/sibling/hub.yaml", "hub.yaml"])
def test_gitlab_nested_publish_freshness_scopes_source_paths(tmp_path: Path, changed_path: str) -> None:
    repo = _init_action_repo(tmp_path / "nested", targets=("cursor",), hub_root_name="docs/hub")
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    path = repo / changed_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# New source in a later commit\n")
    _git(repo, "add", changed_path)
    _git(repo, "commit", "-m", "newer source")
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "--detach", initial)

    result = _run_template_job(repo, hub_root="docs/hub")

    assert result.returncode == 0, result.stdout + result.stderr
    if changed_path == "docs/hub/.gitignore":
        assert "a newer pipeline owns publication" in result.stdout
        assert not _remote_branch_exists(repo, "release/preview")
        assert _git_output(repo, "rev-parse", "HEAD").strip() == initial
    else:
        assert "a newer pipeline owns publication" not in result.stdout
        assert _remote_branch_exists(repo, "release/preview")
        assert _git_output(repo, "show", f"origin/main:{changed_path}") == path.read_text()
        release = json.loads(_git_output(repo, "show", "origin/release/preview:docs/hub/hub.release.json"))
        assert release["marketplace"]["id"] == "acme-instruction-hub"


@pytest.mark.parametrize("hub_root", [".", "docs/hub", ".config/instruction-hub", "docs.v2/hub_1"])
def test_gitlab_change_filters_follow_rendered_hub_root(hub_root: str) -> None:
    spec, template = yaml.safe_load_all(_render_template({"hub-root": hub_root}))
    assert spec["spec"]["inputs"]["hub-root"]["default"] == "."
    root_paths = [".gitlab-ci.yml", ".gitignore", "hub.yaml", "hub.repo-context.json", "assets/**/*", "plugins/**/*"]
    nested_paths = [".gitlab-ci.yml", ".gitignore"] + [
        f"{hub_root}/{path}"
        for path in [".gitignore", "hub.yaml", "hub.repo-context.json", "assets/**/*", "plugins/**/*"]
    ]
    for job, condition in (
        ("check", '$CI_PIPELINE_SOURCE == "merge_request_event"'),
        ("publish", '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'),
    ):
        rules = template[f"instruction-hub-{job}"]["rules"]
        assert rules[0] == {"if": f'{condition} && "{hub_root}" == "."', "changes": root_paths}
        assert rules[1] == {"if": f'{condition} && "{hub_root}" != "."', "changes": nested_paths}
        active_paths = rules[0 if hub_root == "." else 1]["changes"]
        assert not any(path.startswith("./") or "//" in path for path in active_paths)


@pytest.mark.parametrize(
    "hub_root",
    [
        "",
        "/hub",
        "../hub",
        "docs/../hub",
        "./hub",
        "docs/./hub",
        "docs//hub",
        "docs/hub/",
        "docs\\hub",
        "docs/*",
        "docs/[hub]",
        "docs/$HUB",
        "docs/$(touch marker)",
        "docs/hub\n",
        "docs/hub with spaces",
    ],
)
def test_gitlab_hub_root_input_rejects_noncanonical_paths(hub_root: str) -> None:
    with pytest.raises(ValueError, match="invalid template input: hub-root"):
        _render_template({"hub-root": hub_root})


@pytest.mark.parametrize("job", ["check", "publish"])
@pytest.mark.parametrize("kind", ["missing", "file", "internal-symlink", "external-symlink"])
def test_gitlab_hub_root_must_be_a_real_checkout_directory(tmp_path: Path, job: str, kind: str) -> None:
    repo = _init_action_repo(tmp_path / "invalid-directory", targets=("cursor",))
    initial_refs = _git_output(repo, "ls-remote", "origin", "refs/heads/*")
    selected = repo / "selected"
    if kind == "file":
        selected.write_text("not a directory\n")
    elif kind in {"internal-symlink", "external-symlink"}:
        target = repo / "real-directory" if kind == "internal-symlink" else tmp_path / "outside"
        target.mkdir()
        selected.symlink_to(target, target_is_directory=True)

    result = _run_template_job(repo, job, hub_root="selected")

    assert result.returncode != 0
    assert "hub-root must be an existing directory inside the checkout without symlink components" in result.stderr
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*") == initial_refs
