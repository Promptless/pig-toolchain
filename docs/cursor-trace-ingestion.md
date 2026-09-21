# Cursor desktop trace ingestion

Cursor desktop Agent sessions and subagents use four lifecycle hooks:
`sessionStart`, `stop`, `sessionEnd`, and `subagentStop`. Each launches a detached
collector. There are no per-tool hooks.

## Enable collection

1. Deploy Runtime with Cursor source support and its capability migration.
2. Upgrade the Instruction Hub worker and verify that its check-in advertises
   `cursor` in `supported_trace_sources`. Runtime rejects a Cursor policy until
   the worker reports support.
3. Set `trace_ingestion.enabled: true` in `hub.yaml`, build and publish the hub,
   then refresh the installed Cursor `pig` plugin.
4. Add `cursor` to the installation's enabled hosts. Ensure Node and Python
   3.9+ are available on Cursor's PATH, then complete host enrollment.

The generated plugin includes managed runtime 0.3.0. An unenrolled collector can
open the enrollment page in the background. Worker or enrollment failure leaves
Cursor usable and pending collection available for a subsequent lifecycle hook.

## What runs on the user's machine

The foreground Node launcher reads bounded hook metadata and spawns a detached
process with closed output pipes. Its Cursor timeout is 250 ms and its stalled
stdin timer is 180 ms. Python discovery, enrollment, database access, journal
writes, and network calls happen in the background.

The background collector reads saved conversation data from Cursor's SQLite
database using read-only, query-only connections, WAL mode, and a zero busy
timeout. Non-WAL databases are skipped to avoid delaying an editor write. It
does not change database settings, read current project files to reconstruct
output, or replay tools.

| Platform | Default database |
| --- | --- |
| macOS | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/Cursor/User/globalStorage/state.vscdb` |
| Windows | `%APPDATA%/Cursor/User/globalStorage/state.vscdb` |

`PROMPTLESS_CURSOR_DATABASE` overrides the database path. Discovery uses desktop
transcripts under `~/.cursor/projects/*/agent-transcripts/**/*.jsonl`, with the
hook's current conversation first. Saved subagent references add child sessions.

The adapter exports selected messages, tool arguments, saved results, and
session identity. It excludes whole application rows and encryption metadata.
Missing, pruned, unsupported, interrupted, and oversized content gets a capture
marker. Saved empty output remains distinguishable from missing output.
Transcript text is a degraded fallback. Binary images, CLI, Cloud Agents, and
Tab completion are outside this adapter's scope.

## Recovery and diagnostics

The collector saves append-only journals under `cursor/journals/` beside the
host runtime ledger. Uploads use the existing byte-range and acknowledgement
protocol. Later saved results append revisions; acknowledged bytes never change.
The worker uses the latest revision for analysis without counting another call.

The same `cursor/` directory contains pending notifications, scan offsets, and
`diagnostics.json`. Runtime failures use the existing host runtime diagnostics;
missing Python is reported in
`~/.promptless/instruction-hub/cursor-launcher-status.json`. These files contain
status metadata; journals contain sensitive trace content.

To retry explicitly, invoke the installed runtime from its plugin directory:

```sh
CURSOR_PLUGIN_ROOT="$PWD" python3 runtime/promptless-host-runtime collect --host cursor --include-active
```

On Windows PowerShell, run:

```powershell
$env:CURSOR_PLUGIN_ROOT = (Get-Location).Path
py -3 runtime/promptless-host-runtime collect --host cursor --include-active
```

This command scans saved sessions
and uploads pending journal bytes. It needs an enrolled host and a policy that
enables Cursor. A pending lifecycle notification is retried by a subsequent
hook; manual collection alone does not synthesize its terminal event.

Collection stops at 128 MiB per journal, 1 GiB total journals, 4,096 journals, or
4,096 pending notifications. There is no automatic journal deletion. Before
removing local data, verify the worker acknowledged it and archive the journal
with its ledger and scan state. Disabling ingestion and refreshing the plugin
removes managed hooks from the installed release.

## Qualification

The adapter's saved field numbers were checked against Cursor desktop 3.19.19.
Synthetic tests exercise the collector and worker contracts. Actual desktop
dogfooding on macOS, Linux, and Windows remains a release requirement.

Run `python3 scripts/benchmark_cursor_hook.py` to measure launcher overhead
without accessing user traces or a worker. A 100-launch macOS arm64 sample with
Node 26.5.0 measured p95 45.17 ms and p99 56.83 ms, within the proposed 50/100 ms
targets. That benchmark excludes Cursor's own hook scheduling. Background work
is bounded and runs at reduced priority where supported; it still consumes
some CPU, disk, and network resources.
