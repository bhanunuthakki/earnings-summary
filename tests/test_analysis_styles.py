import re
from pathlib import Path

from pipeline.analysis_styles import ANALYSIS_STYLE

ROOT = Path(__file__).parents[1]
CONSUMERS = (
    "src/pipeline/etf_workup.py",
    "src/pipeline/annual_letter_panel.py",
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

# Inline style ATTRIBUTES in markup literals (style="..." / style='...').
# Scoped to the attribute shapes on purpose: the M5 panel-shell contract
# (ui.panel.panel_section) carries this module's master stylesheet through its
# registered ``style=`` keyword (attribution_panel.py passes
# ``style=ANALYSIS_STYLE``), so a bare ``style=`` scan would flag the
# sanctioned seam. Consumer-owned <style> blocks stay banned by the assertion
# below, which still catches every literal CSS payload.
_INLINE_STYLE_ATTR = re.compile(r"style=[\"']")


def test_analysis_family_owns_css_in_one_token_clean_stylesheet() -> None:
    assert ANALYSIS_STYLE.startswith("<style>")
    assert ANALYSIS_STYLE.endswith("</style>")
    for selector in (".etfw", ".cc-open-loops", ".atr-card", ".wv-add", ".rt-brief"):
        assert selector in ANALYSIS_STYLE
    assert "#" not in ANALYSIS_STYLE
    assert not any("<style>" in (ROOT / path).read_text(encoding="utf-8") for path in CONSUMERS)
    assert not any(
        _INLINE_STYLE_ATTR.search((ROOT / path).read_text(encoding="utf-8")) for path in CONSUMERS
    )
