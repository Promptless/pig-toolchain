# Native host runtime

This module implements managed runtime 0.4.0 in Go: enrollment, policy and host
configuration, detached hook supervision, trace uploads, Cursor SQLite capture,
and cloud capture. The Python compiler embeds or downloads a source-matched
native bundle; installed managed hooks never discover an interpreter.

## Development

From the repository root:

```sh
uv sync --frozen
uv run python scripts/build_native_runtime.py --output .native-runtime --host
export PIG_NATIVE_RUNTIME_DIR="$PWD/.native-runtime"
uv run pytest tests -q -n auto --dist worksteal
uv run python scripts/ci_managed_runtime_smoke.py platform
(cd host-runtime && go test -race ./... && go vet ./...)
```

Go 1.27.3 is pinned in the build recipe and module toolchain. The build uses
`CGO_ENABLED=0`, `-trimpath`, `-buildvcs=false`, and stripped symbols. SQLite uses
`modernc.org/sqlite`, so cross-compilation does not require a C toolchain. Omit
`--host` to build all six platforms. Use a fresh output directory when changing
the platform inventory; validation rejects unexpected leftover binaries.
`PIG_NATIVE_RUNTIME_DIR` permits a partial inventory only for local development.

The native release workflow cross-compiles all platforms and publishes
`native-<source-sha256>/native-runtime.zip`. The source hash covers production Go
sources, go.mod/go.sum, launcher assets, and the pinned recipe. Tests and this
guide are excluded. Release inventories contain SHA-256 hashes of every binary
and launcher; the compiler checks them and rejects extra files, symlinks, source
mismatches, and unsafe ZIP paths. Existing source-addressed releases are verified
on rerun and never overwritten. Changing build flags or Go versions requires a
new recipe string in `native_runtime.py`.

macOS/Linux launchers select amd64 or arm64. Windows launchers invoke the
root amd64 `.exe`, including on ARM64 through Windows emulation. Claude hooks
use explicit Bash shell form, requiring Git Bash on Windows; Codex uses its
PowerShell override and Cursor uses the batch launcher. An explicit
`native/windows-arm64/promptless-host-runtime.exe` is also shipped. CI executes
native tests and generated plugins on Linux, macOS, and Windows; cross-compiling
ARM64 is not a substitute for execution on ARM64 Windows or Linux.

Claude and Codex `session-start --detach` install the executing binary under
`~/.promptless/instruction-hub/runtime/<binary-sha256>/`, save plugin metadata
keyed by its original path, and atomically update the private `bin/` launchers.
Terminal hooks use these short, stable paths even after all plugin cache versions
are removed. Installation uses a bounded cross-process lock and keeps existing
immutable copies; a later session may select a newer binary without replacing an
executable that Windows still has open. Cursor retains its bundled short launcher.
CI executes startup, a second installation, and terminal hooks after deleting both
plugin directories, with an empty `PATH` and spaces in plugin and home paths.

## Maintenance boundaries

`cmd/promptless-host-runtime` is the CLI entry point. `internal/runtime` keeps
shared contracts private to the runtime, divided into enrollment/transport,
persistence, host configuration, uploads, Cursor capture, cloud capture, and
hook lifecycle files. Protocol and persisted documents preserve unknown fields;
internal enrollment, policy, source-event, and CLI state use explicit Go types.

Migration compatibility includes existing host IDs and credentials, Python's
canonical JSON encoding for persisted keys and batch IDs, scoped upload ledgers,
OS-backed state locks, private atomic state replacement, append-only Cursor
journals, and exact upload acknowledgment/range proofs. Acknowledged offsets
advance only after a worker response proves the captured bytes. Desktop hooks
fail open; background failures leave bounded, redacted diagnostics.

Go tests directly cover these low-level contracts. Python integration tests run
the generated native executable against mock workers, including enrollment,
collection, Cursor journals, and cloud capture. `tests/reference_runtime` holds
the frozen Python implementation for migration-reference unit tests only; those
tests do not count as native runtime coverage. Real-host desktop dogfooding and
worker deployment acceptance remain separate from local and CI checks.
