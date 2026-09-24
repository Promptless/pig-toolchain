from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from .helpers import _git, _git_output, _init_action_repo, _run_action


@pytest.mark.parametrize("hub_root", [".", "docs/hub"])
@pytest.mark.parametrize(
    "changed_path",
    [
        ".github/workflows/publish.yml",
        ".gitlab-ci.yml",
        ".gitignore",
        "{hub}/.gitignore",
        "{hub}/hub.yaml",
        "{hub}/hub.repo-context.json",
        "{hub}/assets/new.md",
        "{hub}/plugins/new.yaml",
    ],
)
def test_publish_skips_superseded_source(tmp_path: Path, hub_root: str, changed_path: str) -> None:
    repo = _init_action_repo(tmp_path / "superseded", targets=("cursor",), hub_root_name=hub_root)
    _git(repo, "branch", "-m", "trunk")
    _git(repo, "push", "origin", "trunk")
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    source = repo / changed_path.format(hub=hub_root)
    source.parent.mkdir(parents=True, exist_ok=True)
    with source.open("a") as stream:
        stream.write("\n# Updated hub source\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "newer hub source")
    _git(repo, "push", "origin", "trunk")
    published_refs = _git_output(repo, "ls-remote", "origin", "refs/heads/*")
    _git(repo, "checkout", "--detach", initial)

    result = _run_action(
        repo,
        tmp_path / "output.txt",
        hub_root=hub_root,
        source_branch="trunk",
        extra_env={"GITHUB_ACTIONS": "true"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "a newer pipeline owns publication" in result.stdout
    assert _git_output(repo, "rev-parse", "HEAD").strip() == initial
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*") == published_refs


@pytest.mark.parametrize("hub_root", [".", "docs/hub"])
def test_publish_fast_forwards_pointer_and_unrelated_commits(tmp_path: Path, hub_root: str) -> None:
    repo = _init_action_repo(tmp_path / "pointers", targets=("claude", "codex", "cursor"), hub_root_name=hub_root)
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    first = _run_action(repo, tmp_path / "first.txt", hub_root=hub_root)
    assert first.returncode == 0, first.stdout + first.stderr
    (repo / "README.md").write_text("Unrelated documentation.\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "update documentation")
    _git(repo, "push", "origin", "main")
    published_refs = _git_output(repo, "ls-remote", "origin", "refs/heads/*")
    _git(repo, "checkout", "--detach", initial)

    result = _run_action(repo, tmp_path / "second.txt", hub_root=hub_root, extra_env={"GITHUB_ACTIONS": "true"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No release branch changes to publish." in result.stdout
    assert "No source version or marketplace pointer changes to publish." in result.stdout
    assert _git_output(repo, "rev-parse", "HEAD") == _git_output(repo, "rev-parse", "origin/main")
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*") == published_refs


def test_publish_rejects_diverged_history_even_when_hub_source_matches(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "diverged", targets=("claude",))
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    (repo / "README.md").write_text("Remote documentation.\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "remote documentation")
    _git(repo, "push", "origin", "main")
    published_refs = _git_output(repo, "ls-remote", "origin", "refs/heads/*")
    _git(repo, "checkout", "--detach", initial)
    (repo / "NOTES.md").write_text("Local documentation.\n")
    _git(repo, "add", "NOTES.md")
    _git(repo, "commit", "-m", "local documentation")
    local_head = _git_output(repo, "rev-parse", "HEAD")

    result = _run_action(repo, tmp_path / "output.txt")

    assert result.returncode != 0
    assert _git_output(repo, "rev-parse", "HEAD") == local_head
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*") == published_refs


@pytest.mark.parametrize("failed_command", ["fetch", "diff", "merge-base"])
def test_publish_fails_closed_when_source_check_fails(tmp_path: Path, failed_command: str) -> None:
    repo = _init_action_repo(tmp_path / "failed-check", targets=("claude",))
    published_refs = _git_output(repo, "ls-remote", "origin", "refs/heads/*")
    real_git = shutil.which("git")
    assert real_git is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "-C" && "$3" == "$FAILED_COMMAND" ]]; then\n'
        '  echo "injected source check failure" >&2\n'
        "  exit 128\n"
        "fi\n"
        'exec "$REAL_GIT" "$@"\n'
    )
    fake_git.chmod(0o755)

    result = _run_action(
        repo,
        tmp_path / "output.txt",
        extra_env={
            "GITHUB_ACTIONS": "true",
            "FAILED_COMMAND": failed_command,
            "REAL_GIT": real_git,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
    )

    assert result.returncode != 0
    assert "injected source check failure" in result.stderr
    assert "a newer pipeline owns publication" not in result.stdout
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*") == published_refs


def test_publish_atomically_rejects_source_that_advances_during_compilation(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "during-build", targets=("claude",))
    initial = _git_output(repo, "rev-parse", "HEAD").strip()
    with (repo / "hub.yaml").open("a") as stream:
        stream.write("\n# Newer hub source\n")
    _git(repo, "add", "hub.yaml")
    _git(repo, "commit", "-m", "newer source")
    newer = _git_output(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "--detach", initial)
    real_uv = shutil.which("uv")
    assert real_uv is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        '"$REAL_UV" "$@"\n'
        'if [[ " $* " == *" promptless-instruction-hub build "* ]]; then\n'
        '  git push origin "$NEWER_SOURCE:refs/heads/main"\n'
        "fi\n"
    )
    fake_uv.chmod(0o755)

    result = _run_action(
        repo,
        tmp_path / "output.txt",
        extra_env={
            "NEWER_SOURCE": newer,
            "REAL_UV": real_uv,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
    )

    assert result.returncode != 0
    assert "atomic" in result.stderr
    assert _git_output(repo, "rev-parse", "HEAD").strip() == initial
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*").strip() == f"{newer}\trefs/heads/main"
