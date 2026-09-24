#!/usr/bin/env python3
"""Qualify cross-process runtime state locking on each native build platform.

Frozen launchers and detached uploads are covered by ci_native_runtime_smoke.
This source-only helper keeps the OS file-lock regression independently exercised.
"""

from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ASSET_ROOT = (
    Path(__file__).resolve().parents[1] / "src/promptless_instruction_hub/managed_runtime_assets/host_enrollment"
)

RUNTIME_PACKAGE = "promptless_host_runtime"


def _wait_for_signal(path: Path, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"lock child exited before creating {path.name} ({process.returncode})\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for lock child to create {path}")
        time.sleep(0.02)


def _lock_child(bin_root: Path, state_path: Path, signal_a: Path, signal_b: Path, *, holder: bool) -> None:
    sys.path.insert(0, str(bin_root))
    storage = importlib.import_module(f"{RUNTIME_PACKAGE}.storage")
    state_file_lock = getattr(storage, "_state_file_lock")
    if holder:
        with state_file_lock(state_path):
            signal_a.write_text("ready\n")
            deadline = time.monotonic() + 15
            while not signal_b.exists():
                if time.monotonic() >= deadline:
                    raise AssertionError("lock holder timed out waiting for release")
                time.sleep(0.02)
        return

    started_at = time.monotonic()
    signal_a.write_text("attempting\n")
    with state_file_lock(state_path):
        elapsed = time.monotonic() - started_at
        signal_b.write_text(f"{elapsed:.6f}\n")


def _two_process_lock_smoke(bin_root: Path, runtime_python: Path, workspace: Path) -> None:
    state_path = workspace / "lock-state.json"
    holder_ready = workspace / "holder-ready"
    release_holder = workspace / "release-holder"
    waiter_attempting = workspace / "waiter-attempting"
    waiter_acquired = workspace / "waiter-acquired"
    script = Path(__file__).resolve()
    holder = subprocess.Popen(
        [
            str(runtime_python),
            "-S",
            str(script),
            "_lock-holder",
            str(bin_root),
            str(state_path),
            str(holder_ready),
            str(release_holder),
        ],
        cwd=workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    waiter: subprocess.Popen[str] | None = None
    try:
        _wait_for_signal(holder_ready, holder, 10)
        waiter = subprocess.Popen(
            [
                str(runtime_python),
                "-S",
                str(script),
                "_lock-waiter",
                str(bin_root),
                str(state_path),
                str(waiter_attempting),
                str(waiter_acquired),
            ],
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for_signal(waiter_attempting, waiter, 10)
        time.sleep(0.5)
        if waiter_acquired.exists() or waiter.poll() is not None:
            raise AssertionError("second process acquired the state lock while the first process still held it")
        release_holder.write_text("release\n")

        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        waiter_stdout, waiter_stderr = waiter.communicate(timeout=10)
        if holder.returncode != 0:
            raise AssertionError(
                f"lock holder failed ({holder.returncode})\nstdout:\n{holder_stdout}\nstderr:\n{holder_stderr}"
            )
        if waiter.returncode != 0:
            raise AssertionError(
                f"lock waiter failed ({waiter.returncode})\nstdout:\n{waiter_stdout}\nstderr:\n{waiter_stderr}"
            )
        if not waiter_acquired.exists():
            raise AssertionError("second process did not acquire the state lock after release")
        blocked_seconds = float(waiter_acquired.read_text().strip())
        if blocked_seconds < 0.35:
            raise AssertionError(
                f"second process was not observably blocked by the state lock ({blocked_seconds:.3f}s)"
            )
    finally:
        release_holder.touch()
        for process in (waiter, holder):
            if process is not None and process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("_lock-holder", "_lock-waiter"))
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    if args.command:
        _lock_child(*args.paths, holder=args.command == "_lock-holder")
    else:
        with tempfile.TemporaryDirectory(prefix="pig-state-lock-") as temporary:
            _two_process_lock_smoke(ASSET_ROOT, Path(sys.executable), Path(temporary))
        print("validated exclusive cross-process state locking")


if __name__ == "__main__":
    main()
