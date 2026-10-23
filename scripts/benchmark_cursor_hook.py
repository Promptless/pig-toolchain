#!/usr/bin/env python3
"""Measure the native Cursor foreground hook with isolated state and no trace reads."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import tempfile
import time

from promptless_instruction_hub.native_runtime import resolve_native_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=25)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("samples must be positive")
    bundle = resolve_native_bundle()
    binary = bundle / ("promptless-host-runtime.exe" if os.name == "nt" else "promptless-host-runtime")
    with tempfile.TemporaryDirectory(prefix="native-cursor-benchmark-") as directory:
        env = {
            **os.environ,
            "HOME": directory,
            "USERPROFILE": directory,
            "PATH": "",
            "PROMPTLESS_WORKER_BASE_URL": "http://127.0.0.1:1",
            "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES": "1",
            "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER": "0",
        }
        measurements = []
        for _ in range(args.samples):
            start = time.perf_counter()
            result = subprocess.run(
                [binary, "cursor-notify", "--detach", "--lifecycle", "stop"],
                input="{}",
                text=True,
                capture_output=True,
                timeout=3,
                env=env,
            )
            assert result.returncode == 0 and not result.stdout and not result.stderr
            measurements.append((time.perf_counter() - start) * 1000)
        ordered = sorted(measurements)
        print(
            json.dumps(
                {
                    "samples": len(ordered),
                    "median_ms": statistics.median(ordered),
                    "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
                    "max_ms": max(ordered),
                },
                indent=2,
            )
        )
        # Allow refused-connection collectors to finish before deleting their isolated state.
        time.sleep(1)


if __name__ == "__main__":
    main()
