from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from promptless_instruction_hub.cli import main
from promptless_instruction_hub.compiler import build_hub, init_hub, verify_hub
from promptless_instruction_hub.config import EXTERNAL_LOCK_PATH
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.external_lock import ExternalPluginLock, apply_external_resolutions
from promptless_instruction_hub.external_plugins import resolve_external_plugins, verify_external_plugins
from promptless_instruction_hub.models import (
    ExternalPluginDefinition,
    ResolvedExternalPluginDefinition,
    ResolvedGitSource,
)
from promptless_instruction_hub.release.versions import read_release_manifest, resolve_publish_version
from promptless_instruction_hub.render.external import external_marketplace_entry, validate_external_marketplace_source
from promptless_instruction_hub.validate.hub import validate_hub

from .external_helpers import (
    PLUGIN_PATH,
    UPSTREAM_URL,
    commit_upstream,
    external_definition,
    make_upstream,
    write_external,
)
from .helpers import (
    _configure_split_plugin_hub,
    _git,
    _git_output,
    _init_action_repo,
    _run_action,
    _snapshot_tree,
    _write_release_manifest_with_fresh_identity,
)


def latest_definition(*, path: str = PLUGIN_PATH) -> dict[str, Any]:
    definition = external_definition(path=path)
    definition["source"] = {"type": "git", "url": UPSTREAM_URL, "ref": "latest"}
    return definition


def advance_upstream(upstream: Path, *, version: str = "1.2.4", path: str = PLUGIN_PATH) -> str:
    for target in ("claude", "codex", "cursor"):
        manifest = upstream / path / f".{target}-plugin/plugin.json"
        data = json.loads(manifest.read_text())
        data["version"] = version
        manifest.write_text(json.dumps(data))
    return commit_upstream(upstream)


@pytest.mark.parametrize("path", [".", PLUGIN_PATH])
def test_latest_resolves_once_and_builds_the_verified_pin_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    upstream, hub = tmp_path / "upstream", tmp_path / "hub"
    sha = make_upstream(upstream, monkeypatch, path=path)
    _git(upstream, "branch", "-m", "trunk")
    init_hub(hub, org="Promptless")
    definition = latest_definition(path=path)
    write_external(hub, definition)
    validate_hub(hub)
    for operation in (build_hub, verify_hub, verify_external_plugins, resolve_publish_version):
        with pytest.raises(InstructionHubError, match="pig resolve-external"):
            operation(hub)

    # All three targets share the same fetch and verified Git objects.
    run = subprocess.run
    fetches = []

    def record_fetch(*args, **kwargs):
        if "fetch" in args[0]:
            fetches.append(args[0])
        return run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record_fetch)
    assert main(["resolve-external", "--hub", str(hub)]) == 0
    records = json.loads(capsys.readouterr().out)["verified_external_plugins"]
    assert {record["sha"] for record in records} == {sha}
    assert {record["target"] for record in records} == {"claude", "codex", "cursor"}
    assert len(fetches) == 1 and fetches[0][-1] == "HEAD"
    assert yaml.safe_load((hub / "plugins/doc-detective.yaml").read_text()) == definition
    assert json.loads((hub / EXTERNAL_LOCK_PATH).read_text()) == {
        "schema_version": 1,
        "plugins": {"doc-detective": {"type": "git", "url": UPSTREAM_URL, "sha": sha}},
    }

    advance_upstream(upstream, path=path)
    assert {record["sha"] for record in verify_external_plugins(hub)} == {sha}
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("offline build fetched upstream"))
    result = build_hub(hub)
    before = _snapshot_tree(hub)
    assert verify_hub(hub).release_hash == result.release_hash
    assert build_hub(hub, check=True).checked
    assert _snapshot_tree(hub) == before
    for marketplace in (".claude-plugin", ".agents/plugins", ".cursor-plugin"):
        source = json.loads((hub / marketplace / "marketplace.json").read_text())["plugins"][1]["source"]
        assert source["sha"] == sha and "ref" not in source
    _, basis = read_release_manifest(hub / "hub.release.json")
    assert basis["plugins"][1]["source"] == {"type": "git", "url": UPSTREAM_URL, "sha": sha}


@pytest.mark.parametrize(
    "mutation", ["wrong-url", "missing-plugin", "bad-sha", "floating", "ref-only", "both-fields", "bad-schema"]
)
def test_offline_build_rejects_stale_or_invalid_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    make_upstream(tmp_path / "upstream", monkeypatch)
    hub = init_hub(tmp_path / "hub", org="Promptless")
    write_external(hub, latest_definition())
    resolve_external_plugins(hub)
    lock = json.loads((hub / EXTERNAL_LOCK_PATH).read_text())
    source = lock["plugins"]["doc-detective"]
    if mutation == "wrong-url":
        source["url"] = "https://different.example.test/repo.git"
    elif mutation == "missing-plugin":
        lock["plugins"] = {}
    elif mutation == "bad-sha":
        source["sha"] = "main"
    elif mutation == "floating":
        source["sha"] = "latest"
    elif mutation == "ref-only":
        source["ref"] = source.pop("sha")
    elif mutation == "both-fields":
        source["ref"] = source["sha"]
    else:
        lock["schema_version"] = 2
    (hub / EXTERNAL_LOCK_PATH).write_text(json.dumps(lock))
    with pytest.raises((InstructionHubError, ValueError)):
        build_hub(hub)
    assert not (hub / "hub.release.json").exists()


@pytest.mark.parametrize("ref", ["latest", "a" * 40])
def test_requested_sources_cannot_escape_into_marketplaces_or_release_provenance(tmp_path: Path, ref: str) -> None:
    definition = external_definition(ref)
    with pytest.raises(InstructionHubError, match="resolved before rendering"):
        external_marketplace_entry(ExternalPluginDefinition.model_validate(definition), "cursor")
    with pytest.raises(ValueError, match="pinned external"):
        validate_external_marketplace_source({"source": "url", "url": UPSTREAM_URL, "ref": ref})
    init_hub(tmp_path, org="Promptless")
    write_external(tmp_path, external_definition())
    build_hub(tmp_path)
    path = tmp_path / "hub.release.json"
    manifest = json.loads(path.read_text())
    manifest["version_basis"]["plugins"][1]["source"] = definition["source"]
    _write_release_manifest_with_fresh_identity(path, manifest)
    with pytest.raises(ValueError, match="invalid version_basis"):
        read_release_manifest(path)


@pytest.mark.parametrize("ref", ["latest", "a" * 40])
def test_resolution_preserves_catalog_requests_and_resolves_only_stable_plugins(tmp_path: Path, ref: str) -> None:
    init_hub(tmp_path, org="Promptless")
    definition = external_definition(ref)
    write_external(tmp_path, definition)
    # An unselected latest plugin does not need a lock or enter the build.
    unselected = {**latest_definition(), "id": "unselected"}
    (tmp_path / "plugins/unselected.yaml").write_text(yaml.safe_dump(unselected))
    validation = validate_hub(tmp_path)
    lock = ExternalPluginLock(plugins={"doc-detective": ResolvedGitSource(type="git", url=UPSTREAM_URL, sha="b" * 40)})
    resolved = apply_external_resolutions(validation, lock)

    requested = validation.stable_plugins[1].definition
    assert isinstance(requested, ExternalPluginDefinition)
    assert requested.source.ref == ref
    assert resolved.plugins == validation.plugins
    assert resolved.plugins["doc-detective"] is requested
    plugin = resolved.stable_plugins[1].definition
    assert isinstance(plugin, ResolvedExternalPluginDefinition)
    assert plugin.source.sha == ("b" * 40 if ref == "latest" else ref)
    assert [item.definition.id for item in resolved.stable_plugins] == ["pig", "doc-detective"]
    assert yaml.safe_load((tmp_path / "plugins/doc-detective.yaml").read_text()) == definition


def test_switching_latest_to_the_same_fixed_commit_preserves_release_identity(tmp_path: Path) -> None:
    init_hub(tmp_path, org="Promptless")
    write_external(tmp_path, latest_definition())
    lock = ExternalPluginLock(plugins={"doc-detective": ResolvedGitSource(type="git", url=UPSTREAM_URL, sha="a" * 40)})
    (tmp_path / EXTERNAL_LOCK_PATH).write_text(lock.model_dump_json())
    latest = build_hub(tmp_path)
    write_external(tmp_path, external_definition())
    (tmp_path / EXTERNAL_LOCK_PATH).unlink()
    assert build_hub(tmp_path).release_hash == latest.release_hash


@pytest.mark.parametrize("latest", [False, True])
def test_unrelated_upstream_changes_accept_a_new_commit_without_a_version_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, latest: bool
) -> None:
    upstream, hub = tmp_path / "upstream", init_hub(tmp_path / "hub", org="Promptless")
    sha = make_upstream(upstream, monkeypatch)
    write_external(hub, latest_definition() if latest else external_definition(sha))
    resolve_external_plugins(hub)
    build_hub(hub)
    previous = shutil.copytree(hub, tmp_path / "previous")
    (upstream / "README.md").write_text("Unrelated repository documentation\n")
    new_sha = commit_upstream(upstream)
    if not latest:
        write_external(hub, external_definition(new_sha))

    result = resolve_external_plugins(hub, previous_release_root=previous)

    assert result.retained_updates == []
    assert {record["sha"] for record in result.verified_plugins} == {new_sha}
    assert {record["sha"] for record in verify_external_plugins(hub, previous_release_root=previous)} == {new_sha}


@pytest.mark.parametrize("path", [".", PLUGIN_PATH])
@pytest.mark.parametrize("change", ["edit", "add", "delete", "executable"])
def test_changed_plugin_contents_retain_the_published_pin_for_all_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, change: str
) -> None:
    upstream, hub = tmp_path / "upstream", init_hub(tmp_path / "hub", org="Promptless")
    sha = make_upstream(upstream, monkeypatch, path=path)
    reference = upstream / path / "skills/example/reference.md"
    if change == "delete":
        reference.write_text("Reference to remove\n")
        sha = commit_upstream(upstream)
    write_external(hub, latest_definition(path=path))
    resolve_external_plugins(hub)
    build_hub(hub)
    previous = shutil.copytree(hub, tmp_path / "previous")
    skill = upstream / path / "skills/example/SKILL.md"
    if change == "edit":
        skill.write_text("# Updated skill\n")
    elif change == "add":
        (skill.parent / "reference.md").write_text("New reference\n")
    elif change == "delete":
        reference.unlink()
    else:
        _git(upstream, "update-index", "--chmod=+x", str(skill.relative_to(upstream)))
        # Commit the index directly so platforms with core.fileMode=false still exercise the mode change.
        _git(upstream, "commit", "-m", "make skill executable")
    new_sha = (
        _git_output(upstream, "rev-parse", "HEAD").strip() if change == "executable" else commit_upstream(upstream)
    )

    result = resolve_external_plugins(hub, previous_release_root=previous)

    assert len(result.retained_updates) == 1
    retained = result.retained_updates[0]
    assert retained.plugin_id == "doc-detective"
    assert retained.candidate.sha == new_sha
    assert retained.retained.sha == sha
    assert retained.upstream_version == "1.2.3"
    assert {record["sha"] for record in result.verified_plugins} == {sha}
    assert {record["target"] for record in result.verified_plugins} == {"claude", "codex", "cursor"}
    assert json.loads((hub / EXTERNAL_LOCK_PATH).read_text())["plugins"]["doc-detective"]["sha"] == sha
    assert {record["sha"] for record in verify_external_plugins(hub, previous_release_root=previous)} == {sha}

    # Verification checks the selected pin strictly; only resolution may retain a published revision.
    resolve_external_plugins(hub)
    with pytest.raises(InstructionHubError, match="retains upstream version"):
        verify_external_plugins(hub, previous_release_root=previous)


def test_cursor_only_directory_change_does_not_hold_back_an_unchanged_claude_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, hub = tmp_path / "upstream", init_hub(tmp_path / "hub", org="Promptless")
    make_upstream(upstream, monkeypatch)
    shutil.copytree(upstream / PLUGIN_PATH, upstream / "cursor-plugin")
    commit_upstream(upstream)
    definition = latest_definition()
    definition["targets"]["cursor"]["path"] = "cursor-plugin"
    write_external(hub, definition)
    resolve_external_plugins(hub)
    build_hub(hub)
    previous = shutil.copytree(hub, tmp_path / "previous")
    (upstream / "cursor-plugin/skills/example/SKILL.md").write_text("# Updated Cursor skill\n")
    new_sha = commit_upstream(upstream)

    result = resolve_external_plugins(hub, previous_release_root=previous)

    assert result.retained_updates == []
    assert {record["sha"] for record in result.verified_plugins} == {new_sha}


def test_retaining_one_plugin_does_not_hold_back_another_plugin_from_the_same_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, hub = tmp_path / "upstream", init_hub(tmp_path / "hub", org="Promptless")
    make_upstream(upstream, monkeypatch)
    shutil.copytree(upstream / PLUGIN_PATH, upstream / "other-plugin")
    for target in ("claude", "codex", "cursor"):
        manifest = upstream / f"other-plugin/.{target}-plugin/plugin.json"
        data = json.loads(manifest.read_text())
        data["name"] = "other-plugin"
        manifest.write_text(json.dumps(data))
    sha = commit_upstream(upstream)
    write_external(hub, latest_definition())
    definition = {**latest_definition(path="other-plugin"), "id": "other-plugin"}
    (hub / "plugins/other-plugin.yaml").write_text(yaml.safe_dump(definition))
    config = yaml.safe_load((hub / "hub.yaml").read_text())
    config["stable_plugins"].append("other-plugin")
    (hub / "hub.yaml").write_text(yaml.safe_dump(config))
    resolve_external_plugins(hub)
    build_hub(hub)
    previous = shutil.copytree(hub, tmp_path / "previous")
    (upstream / PLUGIN_PATH / "skills/example/SKILL.md").write_text("# Updated skill\n")
    new_sha = advance_upstream(upstream, path="other-plugin")

    result = resolve_external_plugins(hub, previous_release_root=previous)

    assert [update.plugin_id for update in result.retained_updates] == ["doc-detective"]
    pins = {record["id"]: record["sha"] for record in result.verified_plugins}
    assert pins == {"doc-detective": sha, "other-plugin": new_sha}
    lock = json.loads((hub / EXTERNAL_LOCK_PATH).read_text())
    assert {key: source["sha"] for key, source in lock["plugins"].items()} == pins


@pytest.mark.parametrize("change", ["path", "target", "enabled-target"])
def test_latest_retention_cannot_override_a_target_configuration_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    upstream, hub = tmp_path / "upstream", init_hub(tmp_path / "hub", org="Promptless")
    make_upstream(upstream, monkeypatch)
    write_external(hub, latest_definition())
    resolve_external_plugins(hub)
    build_hub(hub)
    previous = shutil.copytree(hub, tmp_path / "previous")
    lock = (hub / EXTERNAL_LOCK_PATH).read_bytes()
    (upstream / PLUGIN_PATH / "skills/example/SKILL.md").write_text("# Updated skill\n")
    definition = latest_definition()
    if change == "path":
        shutil.copytree(upstream / PLUGIN_PATH, upstream / "new-path")
        definition["targets"]["claude"]["path"] = "new-path"
    elif change == "target":
        del definition["targets"]["cursor"]
    else:
        config = yaml.safe_load((hub / "hub.yaml").read_text())
        config["targets"].remove("cursor")
        (hub / "hub.yaml").write_text(yaml.safe_dump(config))
    write_external(hub, definition)
    commit_upstream(upstream)

    with pytest.raises(InstructionHubError, match="retains upstream version"):
        resolve_external_plugins(hub, previous_release_root=previous)
    assert (hub / EXTERNAL_LOCK_PATH).read_bytes() == lock


def test_publish_continues_with_authored_changes_and_resumes_after_upstream_version_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = tmp_path / "upstream"
    sha = make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex", "cursor"))
    _configure_split_plugin_hub(repo, ("claude", "codex", "cursor"))
    write_external(repo, latest_definition())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "follow upstream alongside authored plugins")
    _git(repo, "push")
    initial = _run_action(repo, tmp_path / "output")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    (upstream / PLUGIN_PATH / "skills/example/SKILL.md").write_text("# Updated upstream skill\n")
    commit_upstream(upstream)
    authored_skill = "assets/skills/authoring-tools/SKILL.md"
    (repo / authored_skill).write_text("# Updated authored skill\n")
    _git(repo, "add", authored_skill)
    _git(repo, "commit", "-m", "update authored skill")
    _git(repo, "push")

    def publish(expected_sha: str, version: str) -> None:
        result = _run_action(repo, tmp_path / "output")
        assert result.returncode == 0, result.stdout + result.stderr
        _git(repo, "fetch", "origin")
        for branch in ("main", "release/stable"):
            for marketplace in (".claude-plugin", ".agents/plugins", ".cursor-plugin"):
                entries = json.loads(_git_output(repo, "show", f"origin/{branch}:{marketplace}/marketplace.json"))
                entry = next(plugin for plugin in entries["plugins"] if plugin["name"] == "doc-detective")
                assert entry["source"]["sha"] == expected_sha
            lock = json.loads(_git_output(repo, "show", f"origin/{branch}:{EXTERNAL_LOCK_PATH}"))
            assert lock["plugins"]["doc-detective"]["sha"] == expected_sha
        release = json.loads(_git_output(repo, "show", "origin/release/stable:hub.release.json"))
        assert release["version"] == version
        external = next(plugin for plugin in release["version_basis"]["plugins"] if plugin["id"] == "doc-detective")
        assert external["source"]["sha"] == expected_sha
        verification = json.loads(_git_output(repo, "show", "origin/release/stable:hub.external.json"))
        assert {record["sha"] for record in verification["verified_external_plugins"]} == {expected_sha}
        assert (
            _git_output(repo, "show", "origin/release/stable:dist/claude/dev/skills/authoring-tools/SKILL.md")
            == "# Updated authored skill\n"
        )
        assert _git_output(repo, "status", "--short") == ""

    publish(sha, "0.1.1")
    branches = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    publish(sha, "0.1.1")
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == branches
    new_sha = advance_upstream(upstream)
    publish(new_sha, "0.1.2")


@pytest.mark.parametrize("failure", ["missing-manifest", "unavailable"])
def test_failed_latest_refresh_preserves_lock_and_published_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    upstream = tmp_path / "upstream"
    make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex", "cursor"))
    write_external(repo, latest_definition())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "follow upstream")
    _git(repo, "push")
    initial = _run_action(repo, tmp_path / "output")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    before = _snapshot_tree(repo / ".cursor-plugin")
    lock = (repo / EXTERNAL_LOCK_PATH).read_bytes()
    branches = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    if failure == "unavailable":
        shutil.rmtree(upstream)
    else:
        (upstream / PLUGIN_PATH / ".cursor-plugin/plugin.json").unlink()
        commit_upstream(upstream)
    failed = _run_action(repo, tmp_path / "output")
    assert failed.returncode != 0
    expected = {
        "unavailable": "cannot fetch external plugin revision HEAD",
        "missing-manifest": "missing upstream manifest",
    }
    assert expected[failure] in failed.stderr
    assert (repo / EXTERNAL_LOCK_PATH).read_bytes() == lock
    assert _snapshot_tree(repo / ".cursor-plugin") == before
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == branches
    assert _git_output(repo, "status", "--short") == ""


@pytest.mark.parametrize("mode", ["build", "check"])
def test_ci_build_refreshes_latest_but_check_keeps_the_committed_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    upstream = tmp_path / "upstream"
    sha = make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex", "cursor"))
    write_external(repo, latest_definition())
    resolve_external_plugins(repo)
    build_hub(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "lock and build upstream")
    lock = (repo / EXTERNAL_LOCK_PATH).read_bytes()
    new_sha = advance_upstream(upstream)

    result = _run_action(repo, tmp_path / "output", extra_env={"INPUT_MODE": mode})
    assert result.returncode == 0, result.stdout + result.stderr
    expected_sha = new_sha if mode == "build" else sha
    assert f'"sha": "{expected_sha}"' in result.stdout
    assert (repo / EXTERNAL_LOCK_PATH).read_bytes() == lock
    assert _git_output(repo, "status", "--short") == ""


@pytest.mark.parametrize("hub_path", [".", "Customer Hub"])
def test_publish_refreshes_latest_without_catalog_edits_and_can_roll_back_to_a_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hub_path: str
) -> None:
    upstream = tmp_path / "upstream"
    sha = make_upstream(upstream, monkeypatch)
    repo = _init_action_repo(tmp_path / "publisher", targets=("claude", "codex", "cursor"), hub_root_name=hub_path)
    hub = repo / hub_path
    definition = latest_definition()
    write_external(hub, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "follow upstream")
    _git(repo, "push")
    prefix = "" if hub_path == "." else hub_path + "/"
    extra_env = {"GITHUB_SERVER_URL": "https://gitlab.example.test", "GITHUB_REPOSITORY": "acme/team/hub"}

    def publish(expected_sha: str, version: str, *, locked: bool = True) -> None:
        result = _run_action(repo, tmp_path / "output", hub_root=hub_path, extra_env=extra_env)
        assert result.returncode == 0, result.stdout + result.stderr
        _git(repo, "fetch", "origin")
        for branch in ("main", "release/stable"):
            for marketplace in (".claude-plugin", ".agents/plugins", ".cursor-plugin"):
                entries = json.loads(
                    _git_output(repo, "show", f"origin/{branch}:{prefix}{marketplace}/marketplace.json")
                )
                source = entries["plugins"][1]["source"]
                assert source["sha"] == expected_sha and "ref" not in source
            if locked:
                lock = json.loads(_git_output(repo, "show", f"origin/{branch}:{prefix}{EXTERNAL_LOCK_PATH}"))
                assert lock["plugins"]["doc-detective"]["sha"] == expected_sha
            else:
                assert EXTERNAL_LOCK_PATH.name not in _git_output(repo, "ls-tree", "-r", f"origin/{branch}")
        release = json.loads(_git_output(repo, "show", f"origin/release/stable:{prefix}hub.release.json"))
        assert release["version"] == version
        assert release["version_basis"]["plugins"][1]["source"]["sha"] == expected_sha
        assert yaml.safe_load((hub / "plugins/doc-detective.yaml").read_text()) == definition
        assert _git_output(repo, "status", "--short") == ""

    publish(sha, "0.1.0")
    branches = _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable")
    publish(sha, "0.1.0")
    assert _git_output(repo, "ls-remote", "origin", "refs/heads/main", "refs/heads/release/stable") == branches
    new_sha = advance_upstream(upstream)
    publish(new_sha, "0.1.1")

    definition["source"] = {"type": "git", "url": UPSTREAM_URL, "ref": sha}
    write_external(hub, definition)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "roll back and stop following latest")
    _git(repo, "push")
    publish(sha, "0.1.2", locked=False)
