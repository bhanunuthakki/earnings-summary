"""execution/import_owner_capacity.py — parsing + exclusion unit tests.

Hermetic (no dependency on the real wealthplan/CIO_CONTEXT machine files):
a synthetic CIO_CONTEXT.local.md fixture exercises the human-capital bucket
regex end-to-end (cap parsing, slash-combined tickers, parenthetical
stripping); ``stage_wealthplan_facts`` against a missing root proves the
"quiet no-op, never a crash" degrade. The real-file dry-run is exercised
manually (see the PR description), not in CI, since it depends on this
machine's sibling checkouts.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from import_owner_capacity import (  # noqa: E402
    _life_event_key,  # pyright: ignore[reportPrivateUsage]
    _parse_bucket_members,  # pyright: ignore[reportPrivateUsage]
    stage_cio_context_facts,
    stage_wealthplan_facts,
)

_CIO_FIXTURE = """# CIO Coaching Context

_Last reviewed: 2026-05-27_

---

## Human-capital correlation buckets

Positions whose primary revenue tailwind overlaps these buckets are
flagged for concentration risk.

Active buckets and their portfolio-wide aggregate caps:

- **`big_tech_ads`** (cap **15%**) — Meta employer overlap. Includes
  META, GOOG/GOOGL, AMZN (ads sliver), MSFT.
- **`ai_infra`** (cap **25%**) — AI infrastructure picks. Includes NVDA
  (100%), AVGO (70%), and similar.

---

## Coaching outputs

Unrelated section that must not be swept into the bucket parse.
"""


def test_parse_bucket_members_strips_parens_and_splits_slash() -> None:
    body = " Includes META, GOOG/GOOGL, AMZN (ads sliver), MSFT."
    assert _parse_bucket_members(body) == ["META", "GOOG", "GOOGL", "AMZN", "MSFT"]


def test_parse_bucket_members_drops_prose_tail() -> None:
    body = " Includes NVDA (100%), AVGO (70%), and similar."
    assert _parse_bucket_members(body) == ["NVDA", "AVGO"]


def test_parse_bucket_members_empty_without_includes() -> None:
    assert _parse_bucket_members(" no includes clause here.") == []


def test_stage_cio_context_facts_from_fixture(tmp_path: Path) -> None:
    path = tmp_path / "CIO_CONTEXT.local.md"
    path.write_text(_CIO_FIXTURE, encoding="utf-8")
    facts = stage_cio_context_facts(path)
    keys = {f.key for f in facts}
    assert keys == {"human_capital.big_tech_ads", "human_capital.ai_infra"}
    big_tech = next(f for f in facts if f.key == "human_capital.big_tech_ads")
    assert big_tech.value == {
        "cap_pct": 15.0,
        "members": ["META", "GOOG", "GOOGL", "AMZN", "MSFT"],
    }
    assert big_tech.provenance == "cio_context_import"
    assert (
        big_tech.source_detail == "portfolio-tracker/CIO_CONTEXT.local.md last_reviewed=2026-05-27"
    )
    ai_infra = next(f for f in facts if f.key == "human_capital.ai_infra")
    assert ai_infra.value == {"cap_pct": 25.0, "members": ["NVDA", "AVGO"]}
    # The "Coaching outputs" section text must never leak into a bucket body.
    assert not any("Coaching outputs" in f.narrative for f in facts)


def test_stage_cio_context_facts_missing_file_is_quiet(tmp_path: Path) -> None:
    assert stage_cio_context_facts(tmp_path / "nope.md") == []


def test_stage_cio_context_facts_missing_heading_is_quiet(tmp_path: Path) -> None:
    path = tmp_path / "CIO_CONTEXT.local.md"
    path.write_text("# No relevant section here\n", encoding="utf-8")
    assert stage_cio_context_facts(path) == []


def test_stage_wealthplan_facts_missing_root_is_quiet(tmp_path: Path) -> None:
    """A wealthplan checkout that doesn't exist (moved machine, fresh clone)
    degrades to zero staged facts — never a crash."""
    assert stage_wealthplan_facts(tmp_path / "no_such_wealthplan") == []


def test_life_event_key_disambiguates_by_person_and_date() -> None:
    import datetime

    d = datetime.date(2031, 1, 1)
    assert _life_event_key("baby", d, None) == "life_event.baby_2031_01_01"
    assert (
        _life_event_key("work_break", d, "person_a") == "life_event.work_break_person_a_2031_01_01"
    )
