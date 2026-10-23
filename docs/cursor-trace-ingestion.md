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
4. Add `cursor` to the installation's enabled hosts. Complete host enrollment; the managed
   runtime has no Python, Node, or Go installation prerequisite.

The generated plugin includes managed runtime 0.4.0. An unenrolled collector can
open the enrollment page in the background. Worker or enrollment failure leaves
Cursor usable and pending collection available for a subsequent lifecycle hook.

## What runs on the user's machine

The shell/batch launcher invokes a native Go executable. The foreground reads
bounded identity/path metadata and spawns a detached process with closed output
pipes. Stalled stdin is discarded after 200 ms. Startup hooks have a 30-second
host timeout and terminal hooks 3 seconds. Enrollment, database access, journal
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
`diagnostics.json`. Runtime failures use
`~/.promptless/instruction-hub/last-bootstrap-status.json` and the bounded
`host-runtime-diagnostics.jsonl` log. These files contain
status metadata; journals contain sensitive trace content.

To retry explicitly, invoke the installed runtime from its plugin directory:

```sh
CURSOR_PLUGIN_ROOT="$PWD" runtime/promptless-host-runtime collect --host cursor --include-active
```

On Windows PowerShell, run:

```powershell
$env:CURSOR_PLUGIN_ROOT = (Get-Location).Path
./runtime/promptless-host-runtime.exe collect --host cursor --include-active
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

After building local binaries, run
`PIG_NATIVE_RUNTIME_DIR="$PWD/.native-runtime" uv run python scripts/benchmark_cursor_hook.py`
to measure the native foreground hook using isolated state and a refused loopback
connection. It does not access user traces or a production worker. The benchmark
excludes Cursor's own hook scheduling. Background collection is bounded by its
scan, upload, and pending-notification budgets; it still consumes CPU, disk, and
network resources.
