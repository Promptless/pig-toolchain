---
name: update-instruction-hub
description: Use when asked to update, refresh, or reload this Instruction Hub in Codex. Update installed plugins and report installation and current-session activation separately.
---

# Update Instruction Hub

## Entry Criteria

- Use the copy of this skill supplied by the PIG plugin for the hub the user wants to update, especially when more than one Instruction Hub is installed.
- Use the generated marketplace name `{{ instruction_hub_marketplace_name }}`; do not derive it from a repository name or location.

## Workflow

1. Record the installed plugin ids, versions, and enabled states with `codex plugin list --marketplace {{ instruction_hub_marketplace_name }} --json`. This is an installation snapshot, not evidence of what the running session has loaded. Record any session-specific activation evidence separately; otherwise its active version is unknown.
2. Use Codex's current-session `marketplace/upgrade` action with `marketplaceName` set to `{{ instruction_hub_marketplace_name }}`. The action refreshes the marketplace and force-refreshes all installed plugins sourced from it.
3. If the host action is unavailable, report that the Instruction Hub could not be updated and stop. Do not substitute `codex plugin marketplace upgrade`: that CLI command refreshes only the configured Git marketplace snapshot, not installed plugins. If the action reports partial failures, retain them in the result and continue verification of the installed state.
4. After the host action finishes, call `skills/list` once with `forceReload: true` to invalidate and reload skill discovery. Record whether it succeeds. This refresh establishes skill discovery only; it does not prove hook or MCP activation, including in other running sessions.
5. Repeat `codex plugin list --marketplace {{ instruction_hub_marketplace_name }} --json` and compare with the installation snapshot. Report each plugin's actual installed version and failures; do not infer success solely from the upgrade action returning.
6. Activate the new hook registrations only through a supported current-session host reload, if one is exposed, and record its explicit result. A discovery listing such as `hooks/list` is not evidence that the session's hook engine reloaded. If no supported reload is available, report hook activation as pending restart and tell the user to restart Codex and resume the conversation at a safe boundary. Do not launch another Codex process and call that a reload of this session.

Existing portable hook commands can remain pinned to their retained bundles while new versions are installed. Installation does not switch those registrations. The first rollout of the durable launcher also needs a reload: old registered commands cannot gain the new launcher merely because files changed on disk. Do not delete caches or redirect old version paths to new code to force activation.

## Output Specification

Report installation and activation separately:

- Installed plugin ids, versions before and after, enabled states, and per-plugin failures.
- Skill discovery refresh result.
- Current-session hook activation: confirmed by a supported reload, pending restart, failed, or unknown. Include the evidence; do not manufacture an active version from the installed version.
- Any remaining activation action. Other running sessions have independent activation state and are not covered by this session's reload.

If installation succeeds but activation remains pending, say so explicitly instead of reporting a fully reloaded session.

## Scope

Update only `{{ instruction_hub_marketplace_name }}` and the installed plugins it supplies. Do not update other marketplaces or change global auto-update settings. This workflow does not verify live behavior of every hook or MCP server.
