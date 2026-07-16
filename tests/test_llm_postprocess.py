"""Red-team wave B (B8): ``llm.postprocess.strip_llm_preamble``.

Meta-commentary like "Having exhausted my web budget, I now have enough data
to write the memo. Here is the brief:" was served verbatim in product copy.
The stripper drops a SHORT first-person process-narration lead and nothing
else — content opening with a heading, a claim, or data is untouched.
"""

from __future__ import annotations

import pytest

from llm.postprocess import strip_llm_preamble

_OBSERVED_LEAK = (
    "Having exhausted my web budget, I now have enough data to write the memo. "
    "Here is the brief:\n"
    "\n"
    "## NU deposit franchise\n"
    "Deposit costs fell 40 bps QoQ while balances grew 12%.\n"
)


def test_observed_leak_is_stripped() -> None:
    out = strip_llm_preamble(_OBSERVED_LEAK)
    assert out.startswith("## NU deposit franchise")
    assert "web budget" not in out
    assert "Here is the brief" not in out
    assert "Deposit costs fell 40 bps" in out


@pytest.mark.parametrize(
    "lead",
    [
        "I now have enough evidence to answer this.",
        "I've reviewed the filings and the transcript.",
        "Here is the memo:",
        "Here's the brief you asked for:",
        "Writing from the evidence gathered above.",
        "I will now write the brief.",
        "Based on my research, here is the summary:",
    ],
)
def test_meta_lead_variants_are_stripped(lead: str) -> None:
    text = f"{lead}\n\nRevenue grew 23% YoY on stable take rates."
    assert strip_llm_preamble(text) == "Revenue grew 23% YoY on stable take rates."


def test_heading_first_content_is_untouched() -> None:
    text = "## Thesis check\nHaving a strong quarter matters here.\n"
    assert strip_llm_preamble(text) == text


def test_factual_lead_is_untouched() -> None:
    text = "Revenue grew 23% YoY.\n\nMargins expanded 120 bps."
    assert strip_llm_preamble(text) == text
    # A sentence merely CONTAINING a meta-ish phrase is not a meta lead.
    text2 = "Management said here is the plan: cut costs.\nMore detail follows."
    assert strip_llm_preamble(text2) == text2


def test_long_first_paragraph_is_never_stripped() -> None:
    # >2 lines before the first break = a real paragraph, however it opens.
    text = (
        "Having grown deposits 12% QoQ, NU now funds itself locally.\n"
        "That shift lowers beta to wholesale rates.\n"
        "It also widens NIM through the cycle.\n"
        "\n"
        "Second paragraph.\n"
    )
    assert strip_llm_preamble(text) == text


def test_never_strips_to_empty() -> None:
    only_meta = "Here is the brief:"
    assert strip_llm_preamble(only_meta) == only_meta
    assert strip_llm_preamble("") == ""
    assert strip_llm_preamble("   \n \n") == "   \n \n"


def test_persist_memo_strips_preamble_before_summary(tmp_path) -> None:
    """The advisor write path applies the stripper BEFORE deriving the
    note-sized summary line, so the feed card never shows narration."""
    import sqlite3
    from pathlib import Path

    from alembic.config import Config

    from advisor.memos import persist_memo
    from alembic import command
    from identity import DEFAULT_USER_ID

    project_root = Path(__file__).resolve().parents[1]
    db = tmp_path / "memos.db"
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, "0059_kpi_facts_restatement")
    command.upgrade(cfg, "head")

    result = persist_memo(
        db_path=db,
        user_id=DEFAULT_USER_ID,
        kind="next_dollar",
        ticker="NU",
        counter_ticker=None,
        title="Next dollar",
        body_md=_OBSERVED_LEAK,
        context={},
        write_ledger=False,
    )
    assert result.ok
    conn = sqlite3.connect(str(db))
    try:
        (body_md,) = conn.execute(
            "SELECT body_md FROM advisor_memos WHERE id = ?", (result.memo_id,)
        ).fetchone()
        (note_body,) = conn.execute(
            "SELECT body FROM analyst_notes WHERE source_ref = ?",
            (f"advisor_memo:{result.memo_id}",),
        ).fetchone()
    finally:
        conn.close()
    assert "web budget" not in body_md
    assert body_md.startswith("## NU deposit franchise")
    # The note summary line derives from the STRIPPED body.
    assert "web budget" not in note_body
    assert "Deposit costs fell 40 bps" in note_body
