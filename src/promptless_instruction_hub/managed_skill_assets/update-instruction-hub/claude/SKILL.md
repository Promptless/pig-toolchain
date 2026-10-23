---
name: update-instruction-hub
description: Use when asked to update, refresh, or reload this Instruction Hub in Claude Code. Update installed plugins and report installation and current-session activation separately.
---

# Update Instruction Hub

## Entry Criteria

- Use the copy of this skill supplied by the PIG plugin for the hub the user wants to update, especially when more than one Instruction Hub is installed.
- Use the generated marketplace name `{{ instruction_hub_marketplace_name }}`; do not derive it from a repository name or location.

## Workflow

1. Run `claude plugin list --json`. Record every entry whose `id` ends exactly with `@{{ instruction_hub_marketplace_name }}`, including disabled entries, with its installed version, scope, and `projectPath`. These are installed versions, not proof of what the current session has loaded.
2. Refresh the marketplace:

   ```sh
   claude plugin marketplace update {{ instruction_hub_marketplace_name }}
   ```

   If the refresh fails, report the failure and stop before updating plugins.
3. Update each selected entry with `claude plugin update <id> --scope <scope>`. For `project` or `local` scope, run the command from that entry's `projectPath`. Do not update plugins from other marketplaces. Managed entries may be policy-controlled; report an update refusal without weakening the policy. Preserve per-entry failures and continue attempting the remaining selected entries.
4. Run `claude plugin list --json` again. Compare each selected id, scope, and project path against the installation snapshot, reporting actual installed versions and failures.
5. After every selected plugin has been attempted, reload the active session once with:

   ```text
   /reload-plugins
   ```

   This is a Claude Code slash command, not a shell command. Use the harness-native command channel when it is available. If the agent cannot submit slash commands, ask the user to enter `/reload-plugins`; do not start a second Claude process and do not claim the current session reloaded. Keep activation pending until the host or user confirms completion. A failed reload leaves activation failed or unknown even when installation succeeded.

Loaded hook commands can keep using their previous bundles until reload. The first rollout of the durable launcher also needs a reload; installation cannot rewrite commands already loaded in memory. Keep old cache versions and retained bundles intact. Do not redirect old paths to new code or remove caches to force activation. Other running sessions reload independently; plugin monitors may require a restart even after `/reload-plugins` succeeds.

## Output Specification

Report installation and activation separately:

- Updated plugin ids and scopes, versions before and after, enabled states, and per-entry failures.
- Current-session reload: confirmed, pending user action, failed, or unknown, with its evidence.
- Any remaining activation action, including a restart when required. Do not infer an active version from the installation listing or claim that other running sessions reloaded.

If installation succeeds but activation remains pending, say so explicitly instead of reporting a fully reloaded session.

## Scope

Update only `{{ instruction_hub_marketplace_name }}` and the installed plugins it supplies. Do not update other marketplaces or change global auto-update settings. A successful reload is activation evidence, not verification of every hook's or MCP server's live behavior.
