"""Lazy, Shadow-DOM-isolated assets for the Work OS Full Brief reader."""

from __future__ import annotations

import hashlib

from pipeline.cc_action import CC_ACTION_CSS
from report.renderers.charts_v2 import CSS as CHARTS_V2_CSS
from report.renderers.workspace_styles import CSS as WORKSPACE_CSS
from report.renderers.workspace_styles import READER_OVERRIDE_CSS

READER_CSS = "\n".join(
    (
        WORKSPACE_CSS,
        CHARTS_V2_CSS,
        CC_ACTION_CSS,
        READER_OVERRIDE_CSS,
    )
)

READER_STYLE_URL = (
    "/api/work-os/report-reader.css?v="
    + hashlib.sha256(READER_CSS.encode("utf-8")).hexdigest()[:16]
)

__all__ = ["READER_CSS", "READER_OVERRIDE_CSS", "READER_STYLE_URL"]
