"""CLI adapter for the conservative operational reachability oracle."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality.reachability import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
