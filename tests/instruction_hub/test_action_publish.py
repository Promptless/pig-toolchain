from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from promptless_instruction_hub.compiler import build_hub

from .helpers import (
    REPO_ROOT,
    _configure_split_plugin_hub,
    _git,
    _git_output,
    _init_action_repo,
    _release_branch_path_exists,
    _release_branch_plugin_versions,
    _remote_branch_exists,
    _run_action,
    _write_hub_config,
)


@pytest.mark.parametrize("generated_paths", ["/tmp/dist", "..", ".", "dist/../assets", "dist//codex"])
def test_action_script_rejects_generated_paths_outside_hub(generated_paths: str) -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "INPUT_MODE": "check",
            "INPUT_HUB_ROOT": ".",
            "INPUT_GENERATED_PATHS": generated_paths,
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "generated-path" in result.stderr
    assert "inside hub-root" in result.stderr


def test_action_script_rejects_invalid_release_branch() -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "INPUT_MODE": "check",
            "INPUT_HUB_ROOT": ".",
            "INPUT_RELEASE_BRANCH": "bad\nbranch",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Invalid release-branch" in result.stderr


@pytest.mark.parametrize(
    ("env_name", "label"),
    [
        ("INPUT_UPDATE_CLAUDE_POINTER", "update-claude-pointer"),
        ("INPUT_UPDATE_CODEX_POINTER", "update-codex-pointer"),
        ("INPUT_UPDATE_CURSOR_POINTER", "update-cursor-pointer"),
    ],
)
def test_action_script_rejects_invalid_update_marketplace_pointer(env_name: str, label: str) -> None:
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "INPUT_MODE": "check",
            "INPUT_HUB_ROOT": ".",
            env_name: "tru",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert f"Invalid {label}" in result.stderr


def test_action_check_rejects_hub_root_outside_checkout(tmp_path: Path) -> None:
    outside_hub = tmp_path / "outside-hub"
    outside_hub.mkdir()

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "INPUT_MODE": "check",
            "INPUT_HUB_ROOT": str(outside_hub),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "hub-root must be inside the git checkout" in result.stderr


def test_action_build_cleans_untracked_files_under_tracked_generated_paths(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "build-clean-tracked-generated-path", targets=("claude", "codex"))
    build_hub(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "track generated instruction hub output")
    stale_generated_file = repo / "dist/claude/pig/obsolete/dead.json"
    stale_generated_file.parent.mkdir(parents=True)
    stale_generated_file.write_text("{}\n")

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/run.sh")],
        cwd=repo,
        env={
            **os.environ,
            "GITHUB_ACTION_PATH": str(REPO_ROOT),
            "GITHUB_WORKSPACE": str(repo),
            "INPUT_MODE": "build",
            "INPUT_HUB_ROOT": ".",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not stale_generated_file.exists()
    assert _git_output(repo, "status", "--short") == ""


def test_action_publish_rejects_non_branch_ref(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-tag-ref", targets=("claude",))

    result = _run_action(
        repo,
        tmp_path / "github-output.txt",
        extra_env={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF_TYPE": "tag",
            "GITHUB_REF_NAME": "v1.0.0",
        },
    )

    assert result.returncode == 2
    assert "Publish mode must run from branch ref 'main'" in result.stderr


def test_action_publish_rejects_unexpected_source_branch(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-wrong-branch", targets=("claude",))

    result = _run_action(
        repo,
        tmp_path / "github-output.txt",
        extra_env={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF_TYPE": "branch",
            "GITHUB_REF_NAME": "feature/instructions",
        },
    )

    assert result.returncode == 2
    assert "Publish mode must run from source branch 'main'" in result.stderr


def test_action_publish_rejects_release_branch_equal_to_source_branch(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-same-branch", targets=("claude",))

    result = _run_action(repo, tmp_path / "github-output.txt", release_branch="main")

    assert result.returncode == 2
    assert "release-branch must differ from source-branch" in result.stderr


def test_action_publish_writes_release_branch_and_marketplace_pointers_for_stable_packages(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish", targets=("claude", "codex", "cursor"))
    output_path = tmp_path / "github-output.txt"
    _configure_split_plugin_hub(repo, ("claude", "codex", "cursor"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "split stable packages")
    _git(repo, "push", "origin", "main")

    result = _run_action(repo, output_path)

    assert result.returncode == 0, result.stdout + result.stderr
    _git(repo, "fetch", "origin", "release/stable")
    for target in ("claude", "codex", "cursor"):
        for package in ("dev", "ops", "pig"):
            manifest_name = {
                "claude": ".claude-plugin/plugin.json",
                "codex": ".codex-plugin/plugin.json",
                "cursor": ".cursor-plugin/plugin.json",
            }[target]
            assert json.loads(
                _git_output(repo, "show", f"origin/release/stable:dist/{target}/{package}/{manifest_name}")
            )

    claude_pointer = json.loads((repo / ".claude-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in claude_pointer["plugins"]] == [
        ("dev", "dist/claude/dev"),
        ("ops", "dist/claude/ops"),
        ("pig", "dist/claude/pig"),
    ]
    assert all(plugin["source"]["source"] == "git-subdir" for plugin in claude_pointer["plugins"])
    assert all(
        plugin["source"]["url"] == "https://github.com/Promptless/instruction-hub-test.git"
        for plugin in claude_pointer["plugins"]
    )
    assert all(plugin["source"]["ref"] == "release/stable" for plugin in claude_pointer["plugins"])
    assert all("version" not in plugin for plugin in claude_pointer["plugins"])

    codex_pointer = json.loads((repo / ".agents/plugins/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in codex_pointer["plugins"]] == [
        ("dev", "dist/codex/dev"),
        ("ops", "dist/codex/ops"),
        ("pig", "dist/codex/pig"),
    ]
    assert all(plugin["source"]["source"] == "git-subdir" for plugin in codex_pointer["plugins"])
    assert all(
        plugin["source"]["url"] == "https://github.com/Promptless/instruction-hub-test.git"
        for plugin in codex_pointer["plugins"]
    )
    assert all(plugin["source"]["ref"] == "release/stable" for plugin in codex_pointer["plugins"])
    assert all("version" not in plugin for plugin in codex_pointer["plugins"])

    cursor_pointer = json.loads((repo / ".cursor-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in cursor_pointer["plugins"]] == [
        ("dev", "dist/cursor/dev"),
        ("ops", "dist/cursor/ops"),
        ("pig", "dist/cursor/pig"),
    ]
    assert all(plugin["source"]["owner"] == "Promptless" for plugin in cursor_pointer["plugins"])
    assert all(plugin["source"]["repo"] == "instruction-hub-test" for plugin in cursor_pointer["plugins"])
    assert all(plugin["source"]["ref"] == "release/stable" for plugin in cursor_pointer["plugins"])
    assert all(plugin["source"]["type"] == "github" for plugin in cursor_pointer["plugins"])
    assert all("version" not in plugin for plugin in cursor_pointer["plugins"])
    assert output_path.read_text() == "release-branch=release/stable\n"


@pytest.mark.parametrize(
    ("server_url", "repository", "hub_root"),
    [
        ("https://gitlab.com", "promptless/instruction-hub", "."),
        ("https://gitlab.com", "group/subgroup/instruction-hub", "docs/hub"),
        ("https://git.example.com/gitlab/", "group/subgroup/instruction-hub", "docs/hub"),
    ],
)
def test_action_publish_cursor_pointer_preserves_non_github_repository_and_release_path(
    tmp_path: Path, server_url: str, repository: str, hub_root: str
) -> None:
    repo = _init_action_repo(tmp_path / "publish-gitlab", targets=("cursor",), hub_root_name=hub_root)
    release_branch = "releases/instructions"

    result = _run_action(
        repo,
        tmp_path / "github-output.txt",
        hub_root=hub_root,
        release_branch=release_branch,
        extra_env={"GITHUB_SERVER_URL": server_url, "GITHUB_REPOSITORY": repository},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    _git(repo, "fetch", "origin", "main", release_branch)
    prefix = "" if hub_root == "." else f"{hub_root}/"
    marketplace_path = f"{prefix}.cursor-plugin/marketplace.json"
    pointer = json.loads(_git_output(repo, "show", f"origin/main:{marketplace_path}"))
    assert pointer["plugins"][0]["source"] == {
        "source": "git-subdir",
        "url": f"{server_url.rstrip('/')}/{repository}.git",
        "path": f"{prefix}dist/cursor/pig",
        "ref": release_branch,
    }
    assert "version" not in pointer["plugins"][0]
    release_marketplace = json.loads(_git_output(repo, "show", f"origin/{release_branch}:{marketplace_path}"))
    assert release_marketplace["plugins"][0]["source"] == "dist/cursor/pig"
    plugin_path = pointer["plugins"][0]["source"]["path"]
    manifest = json.loads(
        _git_output(repo, "show", f"origin/{release_branch}:{plugin_path}/.cursor-plugin/plugin.json")
    )
    assert manifest["name"] == pointer["plugins"][0]["name"]


def test_action_publish_bumps_and_rewrites_outputs_when_package_id_changes(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-package-id-rename", targets=("claude", "codex", "cursor"))
    _configure_split_plugin_hub(repo, ("claude", "codex", "cursor"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "split stable packages")
    _git(repo, "push", "origin", "main")

    first = _run_action(repo, tmp_path / "github-output-first.txt")
    assert first.returncode == 0, first.stdout + first.stderr
    _git(repo, "fetch", "origin", "release/stable")
    assert _release_branch_plugin_versions(repo, packages=("dev", "ops"), targets=("claude", "codex", "cursor")) == {
        "0.1.0"
    }

    (repo / "hub.yaml").write_text((repo / "hub.yaml").read_text().replace("  - dev\n", "  - developer\n"))
    (repo / "plugins/dev.yaml").rename(repo / "plugins/developer.yaml")
    (repo / "plugins/developer.yaml").write_text(
        "id: developer\nname: Developer\nincludes:\n  - skill:authoring-tools\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "rename dev package id")
    _git(repo, "push", "origin", "main")

    second = _run_action(repo, tmp_path / "github-output-second.txt")
    assert second.returncode == 0, second.stdout + second.stderr
    _git(repo, "fetch", "origin", "release/stable")

    assert _release_branch_plugin_versions(
        repo,
        packages=("developer", "ops"),
        targets=("claude", "codex", "cursor"),
    ) == {"0.1.1"}
    assert _release_branch_path_exists(repo, "dist/claude/developer/.claude-plugin/plugin.json")
    assert not _release_branch_path_exists(repo, "dist/claude/dev/.claude-plugin/plugin.json")
    claude_pointer = json.loads((repo / ".claude-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in claude_pointer["plugins"]] == [
        ("developer", "dist/claude/developer"),
        ("ops", "dist/claude/ops"),
        ("pig", "dist/claude/pig"),
    ]
    codex_pointer = json.loads((repo / ".agents/plugins/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in codex_pointer["plugins"]] == [
        ("developer", "dist/codex/developer"),
        ("ops", "dist/codex/ops"),
        ("pig", "dist/codex/pig"),
    ]
    cursor_pointer = json.loads((repo / ".cursor-plugin/marketplace.json").read_text())
    assert [(plugin["name"], plugin["source"]["path"]) for plugin in cursor_pointer["plugins"]] == [
        ("developer", "dist/cursor/developer"),
        ("ops", "dist/cursor/ops"),
        ("pig", "dist/cursor/pig"),
    ]


@pytest.mark.parametrize(
    ("env_name", "disabled_pointer", "enabled_pointers"),
    [
        (
            "INPUT_UPDATE_CLAUDE_POINTER",
            Path(".claude-plugin/marketplace.json"),
            (Path(".agents/plugins/marketplace.json"), Path(".cursor-plugin/marketplace.json")),
        ),
        (
            "INPUT_UPDATE_CODEX_POINTER",
            Path(".agents/plugins/marketplace.json"),
            (Path(".claude-plugin/marketplace.json"), Path(".cursor-plugin/marketplace.json")),
        ),
        (
            "INPUT_UPDATE_CURSOR_POINTER",
            Path(".cursor-plugin/marketplace.json"),
            (Path(".claude-plugin/marketplace.json"), Path(".agents/plugins/marketplace.json")),
        ),
    ],
)
def test_action_publish_respects_disabled_marketplace_pointer(
    tmp_path: Path,
    env_name: str,
    disabled_pointer: Path,
    enabled_pointers: tuple[Path, Path],
) -> None:
    repo = _init_action_repo(tmp_path / f"publish-disable-{env_name.lower()}", targets=("claude", "codex", "cursor"))

    result = _run_action(repo, tmp_path / "github-output.txt", extra_env={env_name: "false"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (repo / disabled_pointer).exists()
    for pointer_path in enabled_pointers:
        assert (repo / pointer_path).exists()


def test_action_publish_fails_cursor_pointer_when_repository_is_not_owner_repo(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-bad-cursor-repository", targets=("cursor",))

    result = _run_action(repo, tmp_path / "github-output.txt", extra_env={"GITHUB_REPOSITORY": "bad"})

    assert result.returncode == 2
    assert "GITHUB_REPOSITORY must be owner/repo" in result.stderr
    assert not _remote_branch_exists(repo, "release/stable")
    assert not (repo / ".cursor-plugin/marketplace.json").exists()


def test_action_publish_uses_github_server_url_for_push_credentials(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-enterprise", targets=("claude",))
    fake_git_bin = tmp_path / "fake-git-bin"
    fake_git_bin.mkdir()
    fake_git_log = tmp_path / "fake-git.log"
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git = fake_git_bin / "git"
    fake_git.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
args=("$@")
command_index=0
while [[ "$command_index" -lt "${#args[@]}" ]]; do
  case "${args[$command_index]}" in
    -C | -c | --git-dir | --work-tree)
      command_index=$((command_index + 2))
      ;;
    --*)
      command_index=$((command_index + 1))
      ;;
    *)
      break
      ;;
  esac
done
command="${args[$command_index]:-}"
subcommand="${args[$((command_index + 1))]:-}"
if [[ "$command" == "remote" && "$subcommand" == "set-url" ]]; then
  printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
  exit 0
fi
if [[ "$command" == "push" ]]; then
  printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
  exit 0
fi
exec "$REAL_GIT" "$@"
"""
    )
    fake_git.chmod(0o755)

    result = _run_action(
        repo,
        tmp_path / "github-output.txt",
        extra_env={
            "FAKE_GIT_LOG": str(fake_git_log),
            "GITHUB_SERVER_URL": "https://github.enterprise.example",
            "INPUT_GITHUB_TOKEN": "enterprise-token",
            "PATH": f"{fake_git_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "REAL_GIT": real_git,
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected_remote = (
        "https://x-access-token:enterprise-token@github.enterprise.example/Promptless/instruction-hub-test.git"
    )
    log_text = fake_git_log.read_text()
    assert expected_remote in log_text
    assert "https://x-access-token:enterprise-token@github.com/" not in log_text
    pointer = json.loads((repo / ".claude-plugin/marketplace.json").read_text())
    assert (
        pointer["plugins"][0]["source"]["url"]
        == "https://github.enterprise.example/Promptless/instruction-hub-test.git"
    )


def test_action_publish_authenticates_before_release_branch_inspection(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-private-existing-branch", targets=("claude", "codex"))
    first = _run_action(repo, tmp_path / "github-output-first.txt")
    assert first.returncode == 0, first.stdout + first.stderr

    fake_git_bin = tmp_path / "private-fake-git-bin"
    fake_git_bin.mkdir()
    fake_git_log = tmp_path / "private-fake-git.log"
    fake_auth_marker = tmp_path / "private-fake-git-authenticated"
    real_git = shutil.which("git")
    assert real_git is not None
    fake_git = fake_git_bin / "git"
    fake_git.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
args=("$@")
command_index=0
while [[ "$command_index" -lt "${#args[@]}" ]]; do
  case "${args[$command_index]}" in
    -C | -c | --git-dir | --work-tree)
      command_index=$((command_index + 2))
      ;;
    --*)
      command_index=$((command_index + 1))
      ;;
    *)
      break
      ;;
  esac
done
command="${args[$command_index]:-}"
subcommand="${args[$((command_index + 1))]:-}"
if [[ "$command" == "remote" && "$subcommand" == "set-url" ]]; then
  remote_url="${args[$((command_index + 3))]:-}"
  printf 'set-url %s\\n' "$remote_url" >> "$FAKE_GIT_LOG"
  if [[ "$remote_url" == https://x-access-token:private-token@* ]]; then
    printf 'authenticated\\n' > "$FAKE_GIT_AUTH_MARKER"
  else
    rm -f "$FAKE_GIT_AUTH_MARKER"
  fi
  exit 0
fi
if [[ "$command" == "ls-remote" || "$command" == "fetch" ]]; then
  printf '%s %s\\n' "$command" "$*" >> "$FAKE_GIT_LOG"
  if [[ ! -f "$FAKE_GIT_AUTH_MARKER" ]]; then
    echo "authentication required" >&2
    exit 128
  fi
fi
exec "$REAL_GIT" "$@"
"""
    )
    fake_git.chmod(0o755)

    second = _run_action(
        repo,
        tmp_path / "github-output-second.txt",
        extra_env={
            "FAKE_GIT_AUTH_MARKER": str(fake_auth_marker),
            "FAKE_GIT_LOG": str(fake_git_log),
            "INPUT_GITHUB_TOKEN": "private-token",
            "PATH": f"{fake_git_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "REAL_GIT": real_git,
        },
    )

    assert second.returncode == 0, second.stdout + second.stderr
    log_lines = fake_git_log.read_text().splitlines()
    for remote_command in ("ls-remote", "fetch"):
        command_index = next(index for index, line in enumerate(log_lines) if line.startswith(remote_command))
        assert command_index > 0
        assert "x-access-token:private-token" in log_lines[command_index - 1]


@pytest.mark.parametrize("server_url", ["https://github.com", "https://gitlab.com"])
def test_action_publish_second_run_is_noop(tmp_path: Path, server_url: str) -> None:
    repo = _init_action_repo(tmp_path / "publish-noop", targets=("claude", "codex", "cursor"))
    env = {"GITHUB_SERVER_URL": server_url}

    first = _run_action(repo, tmp_path / "github-output-first.txt", extra_env=env)
    second = _run_action(repo, tmp_path / "github-output-second.txt", extra_env=env)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "No release branch changes to publish." in second.stdout
    assert "No source version or marketplace pointer changes to publish." in second.stdout


@pytest.mark.parametrize("server_url", ["https://github.com", "https://gitlab.com"])
def test_action_publish_bumps_generated_plugin_version_when_assets_change(tmp_path: Path, server_url: str) -> None:
    repo = _init_action_repo(tmp_path / "publish-version-bump", targets=("claude", "codex", "cursor", "gemini"))
    env = {"GITHUB_SERVER_URL": server_url}
    (repo / "plugins/pig.yaml").write_text("id: pig\nname: PIG\nincludes:\n  - skill:review-docs\n")
    skill_root = repo / "assets/skills/review-docs"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Review Docs\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add review docs")
    _git(repo, "push", "origin", "main")

    first = _run_action(repo, tmp_path / "github-output-first.txt", extra_env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    _git(repo, "fetch", "origin", "release/stable")
    assert _release_branch_plugin_versions(repo) == {"0.1.0"}
    first_source = _git_output(repo, "rev-parse", "HEAD").strip()

    (skill_root / "SKILL.md").write_text("# Review Docs\n\nPrefer concise summaries.\n")
    _git(repo, "add", "assets/skills/review-docs/SKILL.md")
    _git(repo, "commit", "-m", "update review docs")
    _git(repo, "push", "origin", "main")

    second = _run_action(repo, tmp_path / "github-output-second.txt", extra_env=env)
    assert second.returncode == 0, second.stdout + second.stderr
    _git(repo, "fetch", "origin", "release/stable")

    assert _release_branch_plugin_versions(repo) == {"0.1.1"}
    release_manifest = json.loads(_git_output(repo, "show", "origin/release/stable:hub.release.json"))
    stable_channel = json.loads(_git_output(repo, "show", "origin/release/stable:hub.stable.json"))
    assert release_manifest["version"] == "0.1.1"
    assert stable_channel["version"] == "0.1.1"
    assert "version: 0.1.1" in (repo / "hub.yaml").read_text()

    third = _run_action(repo, tmp_path / "github-output-third.txt", extra_env=env)
    assert third.returncode == 0, third.stdout + third.stderr
    assert "No release branch changes to publish." in third.stdout

    published_refs = _git_output(repo, "ls-remote", "origin", "refs/heads/*")
    _git(repo, "checkout", "--detach", first_source)
    stale = _run_action(repo, tmp_path / "github-output-stale.txt", extra_env={**env, "GITHUB_ACTIONS": "true"})

    assert stale.returncode == 0, stale.stdout + stale.stderr
    assert "a newer pipeline owns publication" in stale.stdout
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*") == published_refs
    assert _git_output(repo, "rev-parse", "HEAD").strip() == first_source


@pytest.mark.parametrize("ci_env", [{"GITHUB_ACTIONS": "true"}, {"GITLAB_CI": "true"}])
@pytest.mark.parametrize("reset_kind", ["ancestor", "diverged"])
def test_action_publish_skips_ci_commit_removed_from_source_branch(
    tmp_path: Path, ci_env: dict[str, str], reset_kind: str
) -> None:
    repo = _init_action_repo(tmp_path / "publish-reset", targets=("gemini",))
    first = _run_action(repo, tmp_path / "first.txt")
    assert first.returncode == 0, first.stdout + first.stderr
    retained = _git_output(repo, "rev-parse", "HEAD").strip()
    (repo / "hub.yaml").write_text((repo / "hub.yaml").read_text().replace("version: 0.1.0", "version: 0.2.0"))
    _git(repo, "add", "hub.yaml")
    _git(repo, "commit", "-m", "discarded hub change")
    discarded = _git_output(repo, "rev-parse", "HEAD").strip()
    _git(repo, "push", "origin", "main")
    if reset_kind == "diverged":
        retained = _git_output(repo, "commit-tree", f"{retained}^{{tree}}", "-p", retained, "-m", "replacement").strip()
    _git(repo, "push", "--force", "origin", f"{retained}:refs/heads/main")
    _git(repo, "checkout", "--detach", discarded)
    before = _git_output(repo, "ls-remote", "origin", "refs/heads/*")

    rerun = _run_action(repo, tmp_path / "rerun.txt", extra_env=ci_env)

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "no longer on source branch" in rerun.stdout
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/*") == before
    assert _git_output(repo, "rev-parse", "HEAD").strip() == discarded
    assert _git_output(repo, "status", "--short") == ""


def test_action_publish_removes_legacy_promptless_release_metadata(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-cleans-legacy-release-metadata", targets=("claude",))
    first = _run_action(repo, tmp_path / "first.txt")
    assert first.returncode == 0, first.stdout + first.stderr
    _git(repo, "switch", "-c", "release/stable", "origin/release/stable")
    (repo / ".promptless/releases").mkdir(parents=True)
    (repo / ".promptless/channels").mkdir(parents=True)
    (repo / ".promptless/releases/current.json").write_text("{}\n")
    (repo / ".promptless/channels/stable.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed legacy release metadata")
    _git(repo, "push", "-u", "origin", "release/stable")
    _git(repo, "switch", "main")

    result = _run_action(repo, tmp_path / "github-output.txt")

    assert result.returncode == 0, result.stdout + result.stderr
    _git(repo, "fetch", "origin", "release/stable")
    assert _release_branch_path_exists(repo, "hub.release.json")
    assert not _release_branch_path_exists(repo, ".promptless/releases/current.json")
    assert not _release_branch_path_exists(repo, ".promptless/channels/stable.json")


def test_action_publish_supports_subdirectory_hub_root_and_custom_release_branch(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-subdir", targets=("claude", "codex", "cursor"), hub_root_name="hub")

    result = _run_action(repo, tmp_path / "github-output.txt", hub_root="hub", release_branch="release/custom")

    assert result.returncode == 0, result.stdout + result.stderr
    _git(repo, "fetch", "origin", "release/custom")
    assert json.loads(_git_output(repo, "show", "origin/release/custom:hub/dist/claude/pig/.claude-plugin/plugin.json"))
    assert json.loads(_git_output(repo, "show", "origin/release/custom:hub/dist/codex/pig/.codex-plugin/plugin.json"))
    assert json.loads(_git_output(repo, "show", "origin/release/custom:hub/dist/cursor/pig/.cursor-plugin/plugin.json"))
    claude_pointer = json.loads((repo / "hub/.claude-plugin/marketplace.json").read_text())
    assert claude_pointer["plugins"][0]["source"]["path"] == "hub/dist/claude/pig"
    assert claude_pointer["plugins"][0]["source"]["ref"] == "release/custom"
    codex_pointer = json.loads((repo / "hub/.agents/plugins/marketplace.json").read_text())
    assert codex_pointer["plugins"][0]["source"]["path"] == "hub/dist/codex/pig"
    assert codex_pointer["plugins"][0]["source"]["ref"] == "release/custom"
    cursor_pointer = json.loads((repo / "hub/.cursor-plugin/marketplace.json").read_text())
    assert cursor_pointer["plugins"][0]["source"]["path"] == "hub/dist/cursor/pig"
    assert cursor_pointer["plugins"][0]["source"]["ref"] == "release/custom"


def test_action_publish_skips_claude_pointer_when_claude_target_is_absent(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-codex-only", targets=("codex",))

    result = _run_action(repo, tmp_path / "github-output.txt")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No Claude marketplace was generated" in result.stdout
    assert not (repo / ".claude-plugin/marketplace.json").exists()
    pointer = json.loads((repo / ".agents/plugins/marketplace.json").read_text())
    assert pointer["plugins"][0]["source"]["path"] == "dist/codex/pig"


def test_action_publish_removes_stale_pointer_when_target_is_removed(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "publish-removed-target", targets=("claude", "codex"))
    first = _run_action(repo, tmp_path / "github-output-first.txt")
    assert first.returncode == 0, first.stdout + first.stderr
    assert (repo / ".claude-plugin/marketplace.json").exists()

    _write_hub_config(repo, ("codex",))
    _git(repo, "add", "hub.yaml")
    _git(repo, "commit", "-m", "remove claude target")
    _git(repo, "push", "origin", "main")

    second = _run_action(repo, tmp_path / "github-output-second.txt")

    assert second.returncode == 0, second.stdout + second.stderr
    assert "removing stale source-branch Claude pointer" in second.stdout
    assert not (repo / ".claude-plugin/marketplace.json").exists()
    assert ".claude-plugin/marketplace.json" not in _git_output(repo, "ls-files").splitlines()
    _git(repo, "fetch", "origin", "release/stable")
    release_files = _git_output(repo, "ls-tree", "-r", "--name-only", "origin/release/stable").splitlines()
    assert ".claude-plugin/marketplace.json" not in release_files


@pytest.mark.parametrize("rejected_branch", ["main", "release/stable"])
@pytest.mark.parametrize("existing_release", [False, True])
def test_action_publish_ref_rejection_keeps_both_remote_branches(
    tmp_path: Path, rejected_branch: str, existing_release: bool
) -> None:
    repo = _init_action_repo(tmp_path / "atomic-rejection", targets=("gemini",))
    remote = repo.parent / "remote.git"
    if existing_release:
        first = _run_action(repo, tmp_path / "first.txt")
        assert first.returncode == 0, first.stdout + first.stderr
    (repo / "hub.yaml").write_text((repo / "hub.yaml").read_text().replace("version: 0.1.0", "version: 0.2.0"))
    _git(repo, "add", "hub.yaml")
    _git(repo, "commit", "-m", "request minor version")
    _git(repo, "push", "origin", "main")
    before = _git_output(remote, "show-ref", "--heads")
    local_before = _git_output(repo, "rev-parse", "HEAD")
    hook = remote / "hooks/update"
    hook.write_text(f'#!/bin/sh\n[ "$1" != "refs/heads/{rejected_branch}" ]\n')
    hook.chmod(0o755)

    result = _run_action(repo, tmp_path / "rejected.txt")

    assert result.returncode != 0
    assert "atomic" in result.stderr
    assert _git_output(remote, "show-ref", "--heads") == before
    assert _git_output(repo, "rev-parse", "HEAD") == local_before
    assert _git_output(repo, "status", "--short") == ""


@pytest.mark.parametrize("racing_branch", ["main", "release/stable"])
def test_action_publish_concurrent_update_preserves_winner_and_other_branch(tmp_path: Path, racing_branch: str) -> None:
    repo = _init_action_repo(tmp_path / "atomic-race", targets=("gemini",))
    remote = repo.parent / "remote.git"
    first = _run_action(repo, tmp_path / "first.txt")
    assert first.returncode == 0, first.stdout + first.stderr
    # The requested version is already on main: source still needs a recording
    # commit so Git cannot omit it (and its lease) as an up-to-date ref.
    (repo / "hub.yaml").write_text((repo / "hub.yaml").read_text().replace("version: 0.1.0", "version: 0.2.0"))
    _git(repo, "add", "hub.yaml")
    _git(repo, "commit", "-m", "request minor version")
    _git(repo, "push", "origin", "main")
    other_branch = "release/stable" if racing_branch == "main" else "main"
    other_before = _git_output(remote, "rev-parse", f"refs/heads/{other_branch}")
    raced_before = _git_output(remote, "rev-parse", f"refs/heads/{racing_branch}").strip()
    winner = _git_output(
        repo, "commit-tree", f"{raced_before}^{{tree}}", "-p", raced_before, "-m", "concurrent winner"
    ).strip()
    _git(repo, "push", "origin", f"{winner}:refs/heads/race-object")
    hook = repo / ".git/hooks/pre-push"
    hook.write_text(f'#!/bin/sh\ngit --git-dir="$2" update-ref refs/heads/{racing_branch} {winner} {raced_before}\n')
    hook.chmod(0o755)

    result = _run_action(repo, tmp_path / "race.txt")

    assert result.returncode != 0
    assert _git_output(remote, "rev-parse", f"refs/heads/{racing_branch}").strip() == winner
    assert _git_output(remote, "rev-parse", f"refs/heads/{other_branch}") == other_before


def test_action_publish_requires_atomic_push_support(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "no-atomic", targets=("gemini",))
    remote = repo.parent / "remote.git"
    _git(remote, "config", "receive.advertiseAtomic", "false")
    before = _git_output(remote, "show-ref", "--heads")

    result = _run_action(repo, tmp_path / "output.txt")

    assert result.returncode != 0
    assert "does not support --atomic push" in result.stderr
    assert _git_output(remote, "show-ref", "--heads") == before


@pytest.mark.parametrize("hub_root", [".", "docs/hub"])
def test_action_publish_repairs_source_version_and_then_is_noop(tmp_path: Path, hub_root: str) -> None:
    repo = _init_action_repo(tmp_path / "repair-version", targets=("gemini",), hub_root_name=hub_root)
    config_path = repo / hub_root / "hub.yaml"
    config_path.write_text(config_path.read_text().replace("version: 0.1.0", "version: 0.4.0"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "request version")
    first = _run_action(repo, tmp_path / "first.txt", hub_root=hub_root)
    assert first.returncode == 0, first.stdout + first.stderr
    config_path.write_text(config_path.read_text().replace("version: 0.4.0", "version: 0.1.0"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "stale source version")

    repair = _run_action(repo, tmp_path / "repair.txt", hub_root=hub_root)

    assert repair.returncode == 0, repair.stdout + repair.stderr
    assert "version: 0.4.0" in config_path.read_text()
    remote = repo.parent / "remote.git"
    repaired_refs = _git_output(remote, "show-ref", "--heads")
    again = _run_action(repo, tmp_path / "again.txt", hub_root=hub_root)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _git_output(remote, "show-ref", "--heads") == repaired_refs


def test_action_publish_rejects_untracked_source(tmp_path: Path) -> None:
    repo = _init_action_repo(tmp_path / "untracked-source", targets=("gemini",))
    (repo / "plugins/new.yaml").write_text("id: new\nname: New\nincludes: []\n")
    result = _run_action(repo, tmp_path / "output.txt")
    assert result.returncode != 0
    assert "all hub source files to be committed" in result.stderr
    assert not _remote_branch_exists(repo, "release/stable")
