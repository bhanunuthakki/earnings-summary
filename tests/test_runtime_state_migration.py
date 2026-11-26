"""Regression checks for runtime presentation state ownership."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_state_uses_master_css_and_closed_class_transitions() -> None:
    operations = (ROOT / "src/pipeline/operations_panel.py").read_text(encoding="utf-8")
    card = (ROOT / "src/dashboard/_card.py").read_text(encoding="utf-8")
    alerts = (ROOT / "src/cockpit/alert_routes.py").read_text(encoding="utf-8")

    assert "className = 'k-pill k-pill-' + body.tone" not in operations
    assert all(token in operations for token in ("k-pill-ok", "k-pill-warn", "k-pill-bad"))
    assert "<h4>Queued actions</h4>" not in card
    assert '<main class="k-well"><h2 class="k-card-title">Confirm action</h2>' in alerts
