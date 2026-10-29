"""Read-only verification of the Git revisions referenced by a Hub marketplace."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import cast

from promptless_instruction_hub.config import EXTERNAL_LOCK_PATH, RELEASE_MANIFEST_PATH
from promptless_instruction_hub.errors import InstructionHubError
from promptless_instruction_hub.external_lock import (
    ExternalPluginLock,
    apply_external_resolutions,
    load_external_resolutions,
)
from promptless_instruction_hub.fs import JsonValue, validate_json_value, write_json
from promptless_instruction_hub.models import (
    RequestedGitSource,
    ResolvedGitSource,
    ResolvedExternalPluginDefinition,
    ResolvedHubPluginDefinition,
    ExternalPluginDefinition,
    ExternalPluginHarness,
    ExternalPluginTarget,
    PluginDefinition,
    SEMVER_RE,
)
from promptless_instruction_hub.release.external import read_external_versions
from promptless_instruction_hub.release.versions import read_release_manifest, resolve_release_version
from promptless_instruction_hub.validate.hub import ValidationResult, validate_hub

MANIFEST_PATHS = {
    "claude": ".claude-plugin/plugin.json",
    "codex": ".codex-plugin/plugin.json",
    "cursor": ".cursor-plugin/plugin.json",
}


@dataclass(frozen=True)
class GitRevision:
    """Fetched Git objects; upstream files are never checked out or executed."""

    root: Path
    sha: str
    files: dict[str, str]

    def tree_id(self, path: str) -> str:
        """Identify a plugin directory's names, file contents, and executable modes."""

        tree = f"{self.sha}^{{tree}}" if path == "." else f"{self.sha}:{path}"
        return _git(self.root, "rev-parse", tree).strip()

    def manifest(self, plugin: ResolvedExternalPluginDefinition, target: ExternalPluginHarness) -> dict[str, JsonValue]:
        prefix = plugin.targets[target].path
        prefix = "" if prefix == "." else prefix + "/"
        files = {name.removeprefix(prefix): mode for name, mode in self.files.items() if name.startswith(prefix)}
        for name, mode in files.items():
            if mode not in {"100644", "100755"}:
                raise InstructionHubError(
                    f"{plugin.id} ({target}): upstream plugin contains a symlink or submodule: {name}"
                )
        manifest_path = MANIFEST_PATHS[target]
        if manifest_path not in files:
            raise InstructionHubError(f"{plugin.id} ({target}): missing upstream manifest {prefix}{manifest_path}")
        try:
            manifest = validate_json_value(
                json.loads(_git(self.root, "show", f"{self.sha}:{prefix}{manifest_path}")), manifest_path
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise InstructionHubError(f"{plugin.id} ({target}): invalid upstream manifest {manifest_path}") from exc
        if not isinstance(manifest, dict) or manifest.get("name") != plugin.id:
            raise InstructionHubError(f"{plugin.id} ({target}): upstream manifest name must match the Hub plugin id")
        if "version" in manifest:
            version = manifest["version"]
            if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
                raise InstructionHubError(f"{plugin.id} ({target}): upstream manifest version must be SemVer")
        _validate_component_paths(plugin.id, target, manifest, files)
        return manifest


@dataclass(frozen=True)
class RetainedExternalUpdate:
    """An automatic update deferred because Claude would reuse its cached version."""

    plugin_id: str
    candidate: ResolvedGitSource
    retained: ResolvedGitSource
    upstream_version: str


@dataclass
class ExternalPluginResolution:
    """Verified publication sources and automatic updates held at the published pin."""

    verified_plugins: list[dict[str, JsonValue]] = dataclass_field(default_factory=list)
    retained_updates: list[RetainedExternalUpdate] = dataclass_field(default_factory=list)


def verify_external_plugins(
    hub_root: Path, *, previous_release_root: Path | None = None, hub_relative_path: str = ""
) -> list[dict[str, JsonValue]]:
    """Verify exact pins (including locked latest sources) without changing the Hub."""

    validation = load_external_resolutions(hub_root, validate_hub(hub_root))
    with _git_revisions() as revision:
        return _verify_external_plugins(validation, revision, previous_release_root, hub_relative_path).verified_plugins


def resolve_external_plugins(
    hub_root: Path, *, previous_release_root: Path | None = None, hub_relative_path: str = ""
) -> ExternalPluginResolution:
    """Resolve latest sources, retaining published pins for updates without a version change.

    Retention requires a previous release and an unchanged repository and target
    configuration. Explicit pins and invalid upstream manifests still fail. Save
    the lock only after all selected sources pass verification.
    """

    validation = validate_hub(hub_root)
    lock = ExternalPluginLock()
    with _git_revisions() as revision:
        for plugin in validation.stable_plugins:
            definition = plugin.definition
            if isinstance(definition, ExternalPluginDefinition) and definition.source.ref == "latest":
                lock.plugins[definition.id] = ResolvedGitSource(
                    type="git", url=definition.source.url, sha=revision(definition.source).sha
                )
        resolved = apply_external_resolutions(validation, lock)
        result = _verify_external_plugins(
            resolved, revision, previous_release_root, hub_relative_path, retain_latest=True
        )
        for update in result.retained_updates:
            lock.plugins[update.plugin_id] = update.retained
    path = hub_root / EXTERNAL_LOCK_PATH
    if lock.plugins:
        # Never leave a partial lock behind if the process stops during the write.
        with tempfile.TemporaryDirectory(prefix=".pig-external-", dir=hub_root) as temp_dir:
            staged = Path(temp_dir) / EXTERNAL_LOCK_PATH
            write_json(staged, lock.model_dump())
            staged.replace(path)
    else:
        path.unlink(missing_ok=True)
    return result


RevisionReader = Callable[[RequestedGitSource | ResolvedGitSource], GitRevision]


@contextmanager
def _git_revisions() -> Iterator[RevisionReader]:
    with tempfile.TemporaryDirectory(prefix="pig-external-") as temp_dir:
        cache: dict[tuple[str, str], GitRevision] = {}

        def revision(source: RequestedGitSource | ResolvedGitSource) -> GitRevision:
            requested = source.sha if isinstance(source, ResolvedGitSource) else source.ref
            if requested == "latest":
                requested = "HEAD"
            key = (source.url, requested)
            if key not in cache:
                fetched = _fetch_revision(Path(temp_dir) / str(len(cache)), source.url, requested)
                cache[key] = fetched
                cache[(source.url, fetched.sha)] = fetched
            return cache[key]

        yield revision


def _verify_external_plugins(
    validation: ValidationResult[ResolvedHubPluginDefinition],
    revision: RevisionReader,
    previous_release_root: Path | None,
    hub_relative_path: str,
    *,
    retain_latest: bool = False,
) -> ExternalPluginResolution:
    result = ExternalPluginResolution()
    plugins = [
        plugin.definition
        for plugin in validation.stable_plugins
        if isinstance(plugin.definition, ResolvedExternalPluginDefinition)
    ]
    if not plugins and previous_release_root is None:
        return result
    previous: dict[str, ResolvedExternalPluginDefinition] = {}
    previous_authored_ids: set[str] = set()
    previous_version: str | None = None
    previous_targets: list[str] = []
    previous_external_versions: dict[tuple[str, str], str | None] | None = None
    if previous_release_root is not None:
        relative_path = Path(hub_relative_path)
        manifest_path = (previous_release_root / relative_path / RELEASE_MANIFEST_PATH).resolve()
        if relative_path.is_absolute() or not manifest_path.is_relative_to(previous_release_root.resolve()):
            raise InstructionHubError("Hub path must be relative and stay inside the previous release root")
        previous_version, basis = read_release_manifest(manifest_path)
        previous_external_versions = read_external_versions(manifest_path, basis)
        # The authoritative reader validates these nested fields before returning.
        previous_targets = cast(list[str], basis["targets"])
        for item in cast(list[dict[str, JsonValue]], basis["plugins"]):
            if item.get("kind") == "external":
                definition = ResolvedExternalPluginDefinition.model_validate(item)
                previous[definition.id] = definition
            else:
                previous_authored_ids.add(cast(str, item["id"]))

    authored_ids = {
        plugin.definition.id for plugin in validation.stable_plugins if isinstance(plugin.definition, PluginDefinition)
    }
    authored_replacements = [
        plugin
        for plugin in previous.values()
        if plugin.id in authored_ids
        and "claude" in plugin.targets
        and "claude" in previous_targets
        and "claude" in validation.config.targets
    ]
    if not plugins and not authored_replacements:
        return result

    def previous_claude_version(plugin: ResolvedExternalPluginDefinition) -> JsonValue:
        if previous_external_versions is not None:
            return previous_external_versions[(plugin.id, "claude")]
        return revision(plugin.source).manifest(plugin, "claude").get("version")

    if authored_replacements:
        publish_version = resolve_release_version(
            validation, previous_release_root=previous_release_root, hub_relative_path=hub_relative_path
        )
        for old in authored_replacements:
            old_version = previous_claude_version(old)
            if old_version == publish_version:
                raise InstructionHubError(
                    f"{old.id} (claude): authored replacement retains upstream version {old_version}; "
                    "Claude may keep the cached plugin. Set a different Hub version before publishing."
                )

    for plugin in plugins:
        manifests = {
            target: revision(plugin.source).manifest(plugin, target)
            for target in sorted(plugin.targets)
            if target in validation.config.targets
        }
        old = previous.get(plugin.id)
        old_version = None
        claude = manifests.get("claude")
        if claude is not None and "claude" in previous_targets:
            if (
                old is not None
                and "claude" in old.targets
                and (old.source != plugin.source or old.targets["claude"] != plugin.targets["claude"])
            ):
                old_version = previous_claude_version(old)
            elif plugin.id in previous_authored_ids:
                old_version = previous_version
        if claude is not None and old_version is not None and claude.get("version") == old_version:
            unchanged_content = (
                old is not None
                and old.source.url == plugin.source.url
                and old.targets["claude"] == plugin.targets["claude"]
                and revision(old.source).tree_id(old.targets["claude"].path)
                == revision(plugin.source).tree_id(plugin.targets["claude"].path)
            )
            requested = validation.plugins[plugin.id]
            can_retain = (
                retain_latest
                and isinstance(requested, ExternalPluginDefinition)
                and requested.source.ref == "latest"
                and old is not None
                and old.source.url == plugin.source.url
                and old.targets == plugin.targets
                and set(manifests) == set(old.targets).intersection(previous_targets)
            )
            if not unchanged_content:
                if not can_retain or old is None:
                    raise InstructionHubError(
                        f"{plugin.id} (claude): changed source retains upstream version {old_version}; "
                        "Claude may keep the cached plugin. Select an upstream release with a different version."
                    )
                result.retained_updates.append(
                    RetainedExternalUpdate(plugin.id, plugin.source, old.source, str(old_version))
                )
                plugin = plugin.model_copy(update={"source": old.source})
                # Every target shares one source pin. Validate the retained revision
                # for every selected target before recording or persisting it.
                manifests = {target: revision(plugin.source).manifest(plugin, target) for target in manifests}
        for target, manifest in manifests.items():
            result.verified_plugins.append(
                {
                    "id": plugin.id,
                    "target": target,
                    "url": plugin.source.url,
                    "sha": revision(plugin.source).sha,
                    "path": plugin.targets[target].path,
                    "upstream_version": manifest.get("version"),
                }
            )
    return result


def _fetch_revision(root: Path, url: str, requested: str) -> GitRevision:
    root.mkdir()
    _git(root, "init", "--bare", "--quiet")
    try:
        _git(root, "fetch", "--quiet", "--no-tags", "--depth=1", "--recurse-submodules=no", "--", url, requested)
    except InstructionHubError as exc:
        raise InstructionHubError(
            f"cannot fetch external plugin revision {requested} from {url}; check the revision and repository access"
        ) from exc
    sha = _git(root, "rev-parse", "FETCH_HEAD^{commit}").strip()
    if requested != "HEAD" and sha != requested:
        raise InstructionHubError(f"external source did not resolve to the requested commit {requested}")
    files: dict[str, str] = {}
    for entry in _git(root, "ls-tree", "-r", "-z", sha).split("\0"):
        if entry:
            metadata, name = entry.split("\t", 1)
            files[name] = metadata.split(" ", 1)[0]
    return GitRevision(root, sha, files)


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(root), *arguments],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstructionHubError(f"external plugin git {arguments[0]} could not complete") from exc
    if result.returncode:
        # Credential helpers can include credentials in stderr. Report the operation only.
        raise InstructionHubError(f"external plugin git {arguments[0]} failed (exit {result.returncode})")
    return result.stdout


def _validate_component_paths(
    plugin_id: str, target: str, manifest: dict[str, JsonValue], files: dict[str, str]
) -> None:
    fields = ("skills", "commands", "agents", "hooks", "mcpServers", "lspServers")
    file_fields = {"hooks", "mcpServers", "lspServers"}
    if target == "cursor":
        fields += ("rules",)
    for field in fields:
        if field not in manifest:
            continue
        value = manifest[field]
        if isinstance(value, dict) and field in file_fields:
            continue
        paths = value if isinstance(value, list) else [value]
        for path in paths:
            if not isinstance(path, str) or not path.startswith("./"):
                raise InstructionHubError(f"{plugin_id} ({target}): {field} paths must start with './'")
            relative = path.removeprefix("./").removesuffix("/") or "."
            try:
                ExternalPluginTarget(path=relative)
            except ValueError as exc:
                raise InstructionHubError(f"{plugin_id} ({target}): {field} path must stay inside the plugin") from exc
            if field in file_fields:
                if path.endswith("/") or files.get(relative) not in {"100644", "100755"}:
                    raise InstructionHubError(
                        f"{plugin_id} ({target}): {field} path must reference an upstream file: {path}"
                    )
            elif (
                relative != "." and relative not in files and not any(name.startswith(relative + "/") for name in files)
            ):
                raise InstructionHubError(f"{plugin_id} ({target}): missing upstream {field} path {path}")
