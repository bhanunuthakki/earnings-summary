"""Workspace renderer visual ownership contracts."""

from __future__ import annotations

from inspect import signature
from pathlib import Path

from report.renderers import workspace_html
from report.renderers.workspace_charts import SparklineSize, sparkline, verdict_bar
from report.renderers.workspace_chat import CSS as CHAT_CSS
from report.renderers.workspace_comments import CSS as COMMENTS_CSS
from report.renderers.workspace_dcf import CSS as DCF_CSS
from report.renderers.workspace_script import JS as WORKSPACE_JS
from report.renderers.workspace_styles import (
    CHAT_CSS as MASTER_CHAT_CSS,
)
from report.renderers.workspace_styles import (
    COMMENTS_CSS as MASTER_COMMENTS_CSS,
)
from report.renderers.workspace_styles import (
    CSS as WORKSPACE_CSS,
)
from report.renderers.workspace_styles import (
    DCF_CSS as MASTER_DCF_CSS,
)

ROOT = Path(__file__).resolve().parents[1]


def test_workspace_component_css_has_one_master_owner() -> None:
    assert CHAT_CSS == MASTER_CHAT_CSS
    assert COMMENTS_CSS == MASTER_COMMENTS_CSS
    assert DCF_CSS == MASTER_DCF_CSS
    assert ".l1-root" in WORKSPACE_CSS
    assert ".chat-sidebar" in MASTER_CHAT_CSS
    assert ".cmt-sidebar" in MASTER_COMMENTS_CSS
    assert ".dcf-edit" in MASTER_DCF_CSS


def test_document_composes_each_master_slice_once() -> None:
    source = Path(workspace_html.__file__).read_text(encoding="utf-8")
    for name in ("CSS", "COMMENTS_CSS", "CHAT_CSS", "DCF_CSS"):
        assert source.count(f"<style>{{{name}}}</style>") == 1


def test_runtime_visibility_uses_hidden_state_not_inline_display() -> None:
    assert ".style.display" not in WORKSPACE_JS
    section_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src/report/renderers/workspace_sections").glob("*.py")
    )
    assert 'style="display' not in section_sources
    assert "style='display" not in section_sources


def test_workspace_chart_api_is_closed_and_presentation_comes_from_master() -> None:
    assert set(signature(sparkline).parameters) == {"values", "size", "dot"}
    rendered = sparkline([1.0, 2.0], size=SparklineSize.COMPACT)
    assert 'class="ws-spark ws-spark-compact"' in rendered
    assert " fill=" not in rendered
    assert " stroke=" not in rendered
    assert "style=" not in rendered
    verdict = verdict_bar(["EXCEEDED", "MET"])
    assert 'class="ws-verdict-bar"' in verdict
    assert "style=" not in verdict
