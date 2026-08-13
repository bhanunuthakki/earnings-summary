"""Lazy, Shadow-DOM-isolated assets for the Work OS Full Brief reader."""

from __future__ import annotations

from pipeline.cc_action import CC_ACTION_CSS
from report.renderers.charts_v2 import CSS as CHARTS_V2_CSS
from report.renderers.workspace_comments import CSS as COMMENTS_CSS
from report.renderers.workspace_dcf import CSS as DCF_CSS
from report.renderers.workspace_styles import CSS as WORKSPACE_CSS

READER_OVERRIDE_CSS = """
:host {
  display: block;
  min-height: 100%;
  color: var(--fg);
  background: var(--bg);
}
.work-os-report-content.k-doc {
  inline-size: 100%;
  max-inline-size: var(--main-max-width);
  margin-inline: auto;
  padding: var(--sp-5);
}
.l1-root {
  height: auto !important;
  min-height: 100%;
  overflow: visible !important;
  padding-bottom: var(--sp-6);
  background: transparent !important;
}
.tab-group-pane,
.tab-pane,
.subtab-pane {
  display: block !important;
}
.tab-group-pane + .tab-group-pane {
  margin-top: var(--sp-6);
}
.work-os-report-content.k-doc .tab-group-pane,
.work-os-report-content.k-doc .tab-pane {
  max-inline-size: 100%;
}
.work-os-report-content.k-doc .tab-pane {
  padding-block: var(--sp-4);
  border-bottom: var(--bw-thin) solid var(--border);
}
.reader-group-title {
  margin: 0 0 var(--sp-4);
  padding-bottom: var(--sp-2);
  border-bottom: var(--bw-thin) solid var(--border);
  color: var(--fg);
  font-size: var(--fs-display);
}
.cmt-sidebar,
.chat-drawer,
.scrim {
  /* The transient Work OS reader owns comments/Ask in its outer toolbar. */
  display: none !important;
}
"""

READER_CSS = "\n".join(
    (
        WORKSPACE_CSS,
        CHARTS_V2_CSS,
        COMMENTS_CSS,
        DCF_CSS,
        CC_ACTION_CSS,
        READER_OVERRIDE_CSS,
    )
)

__all__ = ["READER_CSS", "READER_OVERRIDE_CSS"]
