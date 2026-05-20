"""Backward-compatible re-export from `sources.price`.

The live-price logic moved to `src/sources/price.py` when the multi-source
preference stack (yfinance -> FMP cache) was added. This module re-exports
the public API so existing imports (`from dcf import live_price`) keep
working without churn.

Prefer importing `sources.price` directly in new code.
"""

from __future__ import annotations

from sources.price import LivePrice, read_live_price

__all__ = ["LivePrice", "read_live_price"]
