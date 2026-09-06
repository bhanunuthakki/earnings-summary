"""CLI adapter for the tracked function lifecycle inventory."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality.function_lifecycle import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
