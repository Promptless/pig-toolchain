"""Python 3.9+ execution boundary retained with each portable hook bundle.

Portable hooks provide context, not policy decisions. Isolate their failures
here so Python's missing-script exit code 2 cannot become a native hook veto.
Keep this module standard-library-only and preserve in-process signal delivery.
"""

from __future__ import annotations

import runpy
import sys


def main() -> None:
    if sys.version_info < (3, 9):
        print("Instruction Hub hooks require Python 3.9 or newer.", file=sys.stderr)
        return
    sys.dont_write_bytecode = True
    runner, *arguments = sys.argv[1:]
    sys.argv = [runner, *arguments]
    try:
        runpy.run_path(runner, run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(f"Instruction Hub context hook skipped: adapter exited {exc.code!s:.100}.", file=sys.stderr)
    except Exception as exc:
        # This is the boundary for arbitrary authored Python, including import
        # failures while an updater removes a bundle. Never swallow cancellation.
        detail = str(exc).replace("\n", " ")[:500]
        print(f"Instruction Hub context hook skipped: {type(exc).__name__}: {detail}", file=sys.stderr)


if __name__ == "__main__":
    main()
