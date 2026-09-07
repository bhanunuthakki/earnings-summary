"""CLI adapter for the deterministic 9+ score oracle (immutable subject/bundle design)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality.scoring import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
