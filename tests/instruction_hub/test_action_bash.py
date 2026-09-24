from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .helpers import REPO_ROOT, _git_output, _init_action_repo


def _available_bash_interpreters() -> tuple[str, ...]:
    candidates = ("/bin/bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash", shutil.which("bash"))
    return tuple(dict.fromkeys(str(Path(path).resolve()) for path in candidates if path and Path(path).is_file()))


@pytest.fixture(params=_available_bash_interpreters())
def bash(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _publisher_env(repo: Path, output: Path, temporary: Path) -> dict[str, str]:
    temporary.mkdir()
    return {
        **os.environ,
        "GITHUB_ACTION_PATH": str(REPO_ROOT),
        "GITHUB_WORKSPACE": str(repo),
        "GITHUB_REPOSITORY": "Example/instruction-hub",
        "GITHUB_REF_NAME": "main",
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_OUTPUT": str(output),
        "INPUT_MODE": "publish",
        "INPUT_HUB_ROOT": ".",
        "INPUT_SOURCE_BRANCH": "main",
        "INPUT_RELEASE_BRANCH": "release/stable",
        "INPUT_UPDATE_CLAUDE_POINTER": "true",
        "INPUT_UPDATE_CODEX_POINTER": "true",
        "INPUT_UPDATE_CURSOR_POINTER": "true",
        "TMPDIR": str(temporary),
    }


def _run_script(bash: str, repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash, str(REPO_ROOT / "scripts/run.sh")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _assert_temporary_paths_cleaned(temporary: Path) -> None:
    # uv owns its process lock; all temporary publisher files must be removed.
    assert [path for path in temporary.iterdir() if not (path.name.startswith("uv-") and path.suffix == ".lock")] == []


@pytest.mark.parametrize("disable_pointers", [False, True])
def test_publish_without_marketplace_pointers_updates_both_remote_branches(
    tmp_path: Path, bash: str, disable_pointers: bool
) -> None:
    targets = ("claude", "codex", "cursor") if disable_pointers else ("gemini",)
    repo = _init_action_repo(tmp_path / "publication", targets=targets)
    remote = repo.parent / "remote.git"
    source_before = _git_output(remote, "rev-parse", "refs/heads/main")
    output = tmp_path / "action-output"
    temporary = tmp_path / "action temporary paths"
    env = _publisher_env(repo, output, temporary)
    if disable_pointers:
        for host in ("CLAUDE", "CODEX", "CURSOR"):
            env[f"INPUT_UPDATE_{host}_POINTER"] = "false"

    result = _run_script(bash, repo, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "unbound variable" not in result.stderr
    assert output.read_text() == "release-branch=release/stable\n"
    source_after = _git_output(remote, "rev-parse", "refs/heads/main")
    assert source_after != source_before
    assert _git_output(repo, "rev-parse", "HEAD") == source_after
    manifest = json.loads(_git_output(remote, "show", "refs/heads/release/stable:hub.release.json"))
    assert manifest["version"] == "0.1.0"
    release_files = _git_output(remote, "ls-tree", "-r", "--name-only", "refs/heads/release/stable").splitlines()
    for target in targets:
        assert any(path.startswith(f"dist/{target}/pig/") for path in release_files)
    source_files = _git_output(remote, "ls-tree", "-r", "--name-only", "refs/heads/main").splitlines()
    assert not any(path.endswith("marketplace.json") for path in source_files)
    assert _git_output(repo, "status", "--short") == ""
    _assert_temporary_paths_cleaned(temporary)
    assert _git_output(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_whitespace_generated_paths_builds_without_empty_array_abort(tmp_path: Path, bash: str) -> None:
    repo = _init_action_repo(tmp_path / "publication", targets=("gemini",))
    output = tmp_path / "action-output"
    temporary = tmp_path / "action temporary paths"
    env = _publisher_env(repo, output, temporary)
    env.update({"INPUT_MODE": "build", "INPUT_GENERATED_PATHS": " \t\n\n  "})

    result = _run_script(bash, repo, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "unbound variable" not in result.stderr
    assert (repo / "dist/gemini/pig/gemini-extension.json").is_file()
    assert output.read_text() == "release-branch=release/stable\n"
    _assert_temporary_paths_cleaned(temporary)


@pytest.mark.parametrize(
    ("fault", "expected_status"),
    [
        ("uv() { return 23; }", 23),
        ("uv() { exit 0; }", 1),
        ('unset PIG_TEST_UNSET; uv() { : "$PIG_TEST_UNSET"; }', 1),
        ('uv() { if [[ "${5:-}" == "publish-version" ]]; then return 37; fi; command uv "$@"; }', 37),
    ],
    ids=["command-failure", "incomplete-zero-exit", "nounset-abort", "command-substitution-failure"],
)
def test_incomplete_publisher_fails_and_cleans_up(tmp_path: Path, bash: str, fault: str, expected_status: int) -> None:
    repo = _init_action_repo(tmp_path / "publication", targets=("gemini",))
    remote = repo.parent / "remote.git"
    before = _git_output(remote, "show-ref", "--heads")
    output = tmp_path / "action-output"
    temporary = tmp_path / "action temporary paths"
    env = _publisher_env(repo, output, temporary)
    # Bash loads BASH_ENV before running the script. Substitute a failing tool
    # at its normal boundary after the publisher has allocated temporary paths.
    shell_environment = tmp_path / "shell-environment"
    shell_environment.write_text(fault + "\n")
    env["BASH_ENV"] = str(shell_environment)

    result = _run_script(bash, repo, env)

    assert result.returncode == expected_status, result.stdout + result.stderr
    if "PIG_TEST_UNSET" in fault:
        assert "PIG_TEST_UNSET: unbound variable" in result.stderr
    assert not output.exists()
    assert _git_output(remote, "show-ref", "--heads") == before
    _assert_temporary_paths_cleaned(temporary)
    assert _git_output(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_failed_release_commit_removes_registered_worktree(tmp_path: Path, bash: str) -> None:
    repo = _init_action_repo(tmp_path / "publication", targets=("gemini",))
    remote = repo.parent / "remote.git"
    before = _git_output(remote, "show-ref", "--heads")
    output = tmp_path / "action-output"
    temporary = tmp_path / "action temporary paths"
    env = _publisher_env(repo, output, temporary)
    shell_environment = tmp_path / "shell-environment"
    shell_environment.write_text(
        'git() {\n  if [[ "${1:-}" == "-C" && "${3:-}" == "commit" ]]; then return 31; fi\n  command git "$@"\n}\n'
    )
    env["BASH_ENV"] = str(shell_environment)

    result = _run_script(bash, repo, env)

    assert result.returncode == 31, result.stdout + result.stderr
    assert not output.exists()
    assert _git_output(remote, "show-ref", "--heads") == before
    _assert_temporary_paths_cleaned(temporary)
    assert _git_output(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_output_write_failure_is_not_reported_as_success(tmp_path: Path, bash: str) -> None:
    repo = _init_action_repo(tmp_path / "publication", targets=("gemini",))
    output = tmp_path / "missing-directory/action-output"
    env = _publisher_env(repo, output, tmp_path / "action temporary paths")
    env["INPUT_MODE"] = "build"

    result = _run_script(bash, repo, env)

    assert result.returncode != 0
    assert str(output) in result.stderr
    assert not output.exists()
