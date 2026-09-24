"""Diagnostics retain cross-process locking and rotation with native launchers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.contracts import (
    MAX_DIAGNOSTIC_LOG_BYTES,
)
from promptless_instruction_hub.managed_runtime_assets.host_enrollment.promptless_host_runtime.storage import (
    _diagnostic_log_path,
    _last_status_path,
    _state_file_lock,
)
from promptless_instruction_hub.native_runtime import ASSET_ROOT


def test_concurrent_collector_failures_wait_for_lock_and_rotate_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    log = _diagnostic_log_path()
    log.parent.mkdir(parents=True)
    full_log = "x" * MAX_DIAGNOSTIC_LOG_BYTES
    log.write_text(full_log)
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from promptless_host_runtime.output import _record_collector_failure\n"
        "Path(sys.argv[1]).touch()\n"
        "_record_collector_failure('codex', exit_code=7, error_code=None)\n"
    )
    processes: list[subprocess.Popen[bytes]] = []
    try:
        with _state_file_lock(log):
            ready_files = [tmp_path / f"ready-{index}" for index in range(2)]
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(ready)],
                    env={**os.environ, "PYTHONPATH": str(ASSET_ROOT)},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for ready in ready_files
            ]
            deadline = time.monotonic() + 5
            while not all(ready.exists() for ready in ready_files) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert all(ready.exists() for ready in ready_files)
            for process in processes:
                with pytest.raises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.1)
            assert log.read_text() == full_log
            assert not _last_status_path().exists()
        for process in processes:
            assert process.wait(timeout=5) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    assert log.with_name(f"{log.name}.1").read_text() == full_log
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) == 2
    for record in records:
        assert record["reason"] == "collector_process_failed"
        assert record["exit_code"] == 7
        assert record["host"] == "codex"
        assert record["emitted_at"]
    status = json.loads(_last_status_path().read_text())
    assert status["reason"] == "collector_process_failed"
    assert status["host"] == "codex"
    assert status["exit_code"] == 7
    assert log.stat().st_mode & 0o777 == 0o600
    assert _last_status_path().stat().st_mode & 0o777 == 0o600
