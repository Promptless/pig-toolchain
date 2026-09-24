"""Static entrypoint analyzed by PyInstaller; no dynamic imports or client Python."""

import sys

from promptless_host_runtime.cli import main  # ty: ignore[unresolved-import]
from promptless_host_runtime.native_bundle import configure_frozen_https  # ty: ignore[unresolved-import]

sys.dont_write_bytecode = True

if __name__ == "__main__":
    configure_frozen_https()
    raise SystemExit(main())
