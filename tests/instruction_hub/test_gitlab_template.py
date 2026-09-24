from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from .helpers import REPO_ROOT, _git, _git_output, _init_action_repo, _remote_branch_exists


def _run_template_job(
    repo: Path, job: str = "publish", *, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    _, template = yaml.safe_load_all((REPO_ROOT / "templates/gitlab/instruction-hub.yml").read_text())
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", "\n".join(template[f"instruction-hub-{job}"]["script"])],
        cwd=repo,
        env={
            **os.environ,
            "GITHUB_ACTIONS": "false",
            "GITLAB_CI": "true",
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
            "INSTRUCTION_HUB_RELEASE_BRANCH": "release/preview",
            **(extra_env or {}),
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_gitlab_check_builds_without_publishing(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "check", targets=("claude", "codex", "cursor"))
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach")

    result = _run_template_job(repo, "check")

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
    [
        ".github/workflows/publish.yml",
        ".gitlab-ci.yml",
        ".gitignore",
        "hub.yaml",
        "hub.repo-context.json",
        "assets/new.md",
        "plugins/new.yaml",
    ],
)
def test_gitlab_publish_skips_superseded_source(tmp_path: Path, changed_path: str) -> None:
    repo = _init_action_repo(tmp_path / "superseded", targets=("cursor",))
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    (repo / changed_path).parent.mkdir(parents=True, exist_ok=True)
    with (repo / changed_path).open("a") as source:
        source.write("\n# Updated hub source\n")
    _git(repo, "add", changed_path)
    _git(repo, "commit", "-m", "newer hub source")
    _git(repo, "push", "origin", "main")
    latest = _git_output(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach", initial)

    result = _run_template_job(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "a newer pipeline owns publication" in result.stdout
    assert _git_output(repo, "rev-parse", "HEAD").strip() == initial
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*").strip() == f"{latest}\trefs/heads/main"


def test_gitlab_publish_fast_forwards_pointer_commits_and_is_idempotent(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish", targets=("claude", "codex", "cursor"))
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach")

    first = _run_template_job(repo)

    assert first.returncode == 0, first.stdout + first.stderr
    assert _remote_branch_exists(repo, "release/preview")
    published_refs = _git_output(repo, "ls-remote", "origin", "refs/heads/*").strip()
    for path in (
        ".claude-plugin/marketplace.json",
        ".agents/plugins/marketplace.json",
        ".cursor-plugin/marketplace.json",
    ):
        pointer = _git_output(repo, "show", f"origin/main:{path}")
        assert "https://gitlab.example.com/group/subgroup/hub.git" in pointer
    cursor = json.loads(_git_output(repo, "show", "origin/main:.cursor-plugin/marketplace.json"))
    assert cursor["plugins"][0]["source"]["ref"] == "release/preview"
    assert (
        _git_output(repo, "log", "-1", "--format=%an <%ae>", "origin/main").strip()
        == "GitLab CI <gitlab-ci@gitlab.example.com>"
    )

    # A second queued job still starts at the commit before the pointer update.
    _git(repo, "checkout", "--detach", initial)
    second = _run_template_job(repo)

    assert second.returncode == 0, second.stdout + second.stderr
    assert "a newer pipeline owns publication" not in second.stdout
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*").strip() == published_refs
    assert _git_output(repo, "rev-parse", "HEAD").strip() == _git_output(repo, "rev-parse", "origin/main").strip()
