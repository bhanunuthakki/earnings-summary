"""CLI adapter for the tracked function lifecycle inventory."""

from __future__ import annotations

import sys

try:
    from _lib import PROJECT_ROOT
except ImportError:
    from execution._lib import PROJECT_ROOT

from quality.function_lifecycle import main

if __name__ == "__main__":
    raise SystemExit(main(["--root", str(PROJECT_ROOT), *sys.argv[1:]]))
