"""Regression checks for runtime presentation state ownership."""

from __future__ import annotations

from pathlib import Path

from ui.conformance_scan import css_text, geometry_debt_fingerprints, scan_surface_evidence

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_state_uses_master_css_and_closed_class_transitions() -> None:
    mobile = (ROOT / "src/pipeline/mobile_inbox_panel.py").read_text(encoding="utf-8")
    operations = (ROOT / "src/pipeline/operations_panel.py").read_text(encoding="utf-8")
    styles = (ROOT / "src/pipeline/operations_styles.py").read_text(encoding="utf-8")
    card = (ROOT / "src/dashboard/_card.py").read_text(encoding="utf-8")
    alerts = (ROOT / "execution/comments_server_alert_routes.py").read_text(encoding="utf-8")

    assert ".style.opacity" not in mobile
    assert "mi-deferred" in mobile and ".mi-card.mi-deferred" in styles
    assert "className = 'k-pill k-pill-' + body.tone" not in operations
    assert all(token in operations for token in ("k-pill-ok", "k-pill-warn", "k-pill-bad"))
    assert "<h4>Queued actions</h4>" not in card
    assert '<main class="k-well"><h2 class="k-card-title">Confirm action</h2>' in alerts


def test_owned_surfaces_have_no_direct_conformance_findings_or_geometry_debt() -> None:
    for rel in (
        "pipeline/mobile_inbox_panel.py",
        "pipeline/operations_panel.py",
        "pipeline/operations_styles.py",
        "dashboard/_card.py",
        "execution/comments_server_alert_routes.py",
    ):
        path = ROOT / "src" / rel if not rel.startswith("execution/") else ROOT / rel
        source = css_text(path)
        evidence = scan_surface_evidence(rel, source)
        assert not evidence.findings, (rel, evidence.findings)
        assert not geometry_debt_fingerprints(rel, source), rel
