from pathlib import Path

from pipeline.analysis_styles import ANALYSIS_STYLE

ROOT = Path(__file__).parents[1]
CONSUMERS = (
    "src/pipeline/etf_workup.py",
    "src/pipeline/annual_letter_panel.py",
    "src/pipeline/senior_partner_brief_panel.py",
    "src/pipeline/worldview_panel.py",
    "src/pipeline/redteam_pnl_panel.py",
    "src/pipeline/attribution_panel.py",
    "src/pipeline/key_metrics.py",
    "src/pipeline/since_last.py",
    "src/pipeline/you_said.py",
    "src/pipeline/three_regime_renderer.py",
    "src/pipeline/open_loops.py",
    "src/redteam/brief.py",
)


def test_analysis_family_owns_css_in_one_token_clean_stylesheet() -> None:
    assert ANALYSIS_STYLE.startswith("<style>")
    assert ANALYSIS_STYLE.endswith("</style>")
    for selector in (".etfw", ".cc-open-loops", ".atr-card", ".wv-add", ".rt-brief"):
        assert selector in ANALYSIS_STYLE
    assert "#" not in ANALYSIS_STYLE
    assert not any("<style>" in (ROOT / path).read_text(encoding="utf-8") for path in CONSUMERS)
    assert not any("style=" in (ROOT / path).read_text(encoding="utf-8") for path in CONSUMERS)
