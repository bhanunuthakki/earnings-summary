"""CLI adapter for the tracked-file static-quality inventory."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality.static_quality import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
