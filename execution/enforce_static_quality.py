"""Enforce the checked-in static-quality ceilings."""

from __future__ import annotations

from quality.static_quality_gate import main

if __name__ == "__main__":
    raise SystemExit(main())
