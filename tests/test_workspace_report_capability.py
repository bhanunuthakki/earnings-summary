from __future__ import annotations

from datetime import date
from io import StringIO
from pathlib import Path

import pytest

from report.models import ReportSpec
from report.renderers.workspace_chat import JS as CHAT_JS
from report.renderers.workspace_comments import JS as COMMENTS_JS
from report.renderers.workspace_dcf import JS as DCF_JS
from report.renderers.workspace_decision_card import JS as DECISION_CARD_JS
from report.renderers.workspace_sections.boot import _comment_boot_data


def test_workspace_boot_embeds_static_report_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMENTS_SERVER_REPORT_CAPABILITY", "capability-for-test")
    spec = ReportSpec.model_construct(
        ticker="NU",
        generation_date=date(2026, 7, 26),
        repo_root=str(tmp_path),
    )
    body = StringIO()
    _comment_boot_data(body, spec)
    assert '"report_capability": "capability-for-test"' in body.getvalue()


def test_http_reports_use_the_serving_tailscale_origin() -> None:
    for script in (COMMENTS_JS, CHAT_JS, DCF_JS, DECISION_CARD_JS):
        assert "window.location.origin" in script
        assert "/^https?:$/.test(window.location.protocol)" in script


def test_server_navigation_links_follow_the_serving_origin() -> None:
    assert 'a[href^="/"]:not([href^="//"])' in COMMENTS_JS
    assert "link.href = SERVER_URL + path" in COMMENTS_JS
