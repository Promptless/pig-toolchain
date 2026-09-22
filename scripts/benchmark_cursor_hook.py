"""Measure the real foreground launcher with an inert background runtime.

Run with Python 3.9+ and Node on PATH. No enrollment, native database access,
network, or user trace collection occurs. Desktop hook scheduling is excluded.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time


def main() -> None:
    node = shutil.which("node")
    if node is None:
        raise SystemExit("Node must be on PATH")
    source = (
        Path(__file__).resolve().parents[1]
        / "src/promptless_instruction_hub/managed_runtime_assets/host_enrollment/cursor-hook.cjs"
    )
    with tempfile.TemporaryDirectory(prefix="cursor-hook-benchmark-") as name:
        root = Path(name)
        runtime = root / "runtime"
        runtime.mkdir()
        hook = runtime / "cursor-hook.cjs"
        shutil.copyfile(source, hook)
        (runtime / "promptless-host-runtime").write_text("import sys\nsys.stdin.read()\n")
        env = {**os.environ, "HOME": str(root), "USERPROFILE": str(root)}
        samples = []
        for _ in range(100):
            start = time.perf_counter()
            result = subprocess.run(
                [node, str(hook), "stop"],
                input=b'{"conversation_id":"benchmark"}',
                capture_output=True,
                env=env,
                timeout=1,
                check=True,
            )
            samples.append((time.perf_counter() - start) * 1000)
            assert not result.stdout and not result.stderr
            time.sleep(0.05)
        samples.sort()
        blocked = subprocess.Popen(
            [node, str(hook), "stop"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        start = time.perf_counter()
        blocked.wait(timeout=1)
        blocked_ms = (time.perf_counter() - start) * 1000
        blocked.communicate()
        print(
            json.dumps(
                {
                    "platform": platform.platform(),
                    "node": subprocess.check_output([node, "--version"], text=True).strip(),
                    "samples": len(samples),
                    "milliseconds": {
                        **{f"p{p}": round(samples[math.ceil(len(samples) * p / 100) - 1], 2) for p in (50, 95, 99)},
                        "max": round(max(samples), 2),
                        "stdin_never_closed": round(blocked_ms, 2),
                    },
                },
                indent=2,
            )
        )
        time.sleep(0.5)


if __name__ == "__main__":
    main()
