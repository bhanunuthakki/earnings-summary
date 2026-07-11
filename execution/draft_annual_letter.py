"""Draft the annual letter-to-self (monthly_red_team.md Phase 3, PR7).

Assembles a fully deterministic evidence pack for one calendar year — the
year's thesis-ledger entries, position_entries opened/closed, the Red Team
response log, the calibration/Brier trajectory, and the Decision P&L rows —
and makes ONE LLM call (purpose ``annual_letter``, registered in
``llm.cli.LLM_MODELS`` + ``llm.prompt_versions`` only; no eval registry touched
per the directive) to draft a letter-to-self in the owner's voice: what was
believed, what was wrong, what the scored record says, what to do differently.

Output is a DELIVERABLE (never ``.tmp/``): ``data/annual_letters/<year>.md``.
The owner edits and signs the file directly — this script never re-writes an
existing letter without ``--force`` (idempotency key ``annual_letter_{year}``).

Cadence: January, once a year, covering the PRIOR calendar year (default
``--year`` = this year - 1). No new scheduled task is registered — there is no
existing monthly/quarterly hook this naturally attaches to the way
``refresh_scenario_priors`` attaches to the 1st-of-month cron (see
``directives/llm_quota_scheduling.md``); a once-a-year job doesn't earn its own
cron entry. Invoke by hand each January:

    python execution/draft_annual_letter.py                 # drafts last year
    python execution/draft_annual_letter.py --year 2026
    python execution/draft_annual_letter.py --dry-run        # evidence pack only, zero LLM calls
    python execution/draft_annual_letter.py --force          # re-draft an existing year
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from clock import now_naive_utc  # noqa: E402
from decision_calibration import build_calibration  # noqa: E402
from llm.cli import is_hard_stop  # noqa: E402
from llm.structured import StructuredParseError, call_llm_structured  # noqa: E402
from position_lifecycle import list_entries as list_position_entries  # noqa: E402
from redteam.decision_pnl import build_decision_pnl  # noqa: E402
from redteam.store import list_responded_items  # noqa: E402
from user_state.ledger import list_recent_entries  # noqa: E402

_OUT_DIR_NAME = "annual_letters"
_LEDGER_FETCH_LIMIT = 2000  # enough headroom for a year's worth across all tickers
_POSITION_FETCH_LIMIT = 500


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}, default=str), file=sys.stderr)


@dataclass(frozen=True, slots=True)
class AnnualLetterEvidence:
    """The fully deterministic evidence pack for one year. Every field is a
    plain read off existing stores — no synthesis happens before the single
    LLM call that drafts the letter."""

    year: int
    ledger_lines: list[str] = field(default_factory=list[str])
    position_lines: list[str] = field(default_factory=list[str])
    redteam_lines: list[str] = field(default_factory=list[str])
    calibration_lines: list[str] = field(default_factory=list[str])
    decision_pnl_lines: list[str] = field(default_factory=list[str])


def _year_str(y: int) -> str:
    return f"{y:04d}"


def _in_year(stamp: str | None, year: int) -> bool:
    return bool(stamp) and str(stamp)[:4] == _year_str(year)


def _ledger_lines(db_path: Path, year: int) -> list[str]:
    entries = list_recent_entries(db_path=db_path, limit=_LEDGER_FETCH_LIMIT)
    out: list[str] = []
    for e in entries:
        if e.created_at.year != year:
            continue
        body = " ".join(e.body.split())[:300]
        out.append(f"- {e.created_at.date().isoformat()} {e.ticker} [{e.entry_kind}] {body}")
    return list(reversed(out))  # chronological within the year


def _position_lines(db_path: Path, year: int) -> list[str]:
    entries = list_position_entries(db_path=db_path, limit=_POSITION_FETCH_LIMIT)
    out: list[str] = []
    for e in entries:
        if _in_year(e.entry_date, year):
            out.append(
                f"- OPENED {e.ticker} {e.entry_date} "
                f"(conviction {e.entry_conviction or 'unstated'})"
            )
        if _in_year(e.exit_date, year):
            outcome = e.outcome_vs_thesis or "ungraded"
            reason = (e.exit_reason or "").strip()[:200]
            lesson = (e.lessons or "").strip()[:300]
            line = f"- CLOSED {e.ticker} {e.exit_date} outcome={outcome}"
            if reason:
                line += f" reason: {reason}"
            if lesson:
                line += f" | lesson: {lesson}"
            out.append(line)
    return sorted(out)


def _redteam_lines(db_path: Path, year: int) -> list[str]:
    items = list_responded_items(db_path=db_path)
    out: list[str] = []
    for i in items:
        if i.responded_at is None or i.responded_at.year != year:
            continue
        subject = i.ticker or "cross-book"
        response = " ".join((i.response_md or "").split())[:200]
        out.append(
            f"- {i.responded_at.date().isoformat()} {subject} [{i.lens}] {i.status.upper()}"
            + (f": {response}" if response else "")
        )
    return out


def _calibration_lines(db_path: Path, year: int) -> list[str]:
    cal = build_calibration(db_path=db_path)
    if cal is None:
        return ["(no decisions ledger on file)"]
    out = [
        f"Overall: {cal.graded} graded calls of {cal.total} logged, "
        f"{f'{cal.overall_hit_rate * 100:.0f}%' if cal.overall_hit_rate is not None else 'n/a'} correct."
    ]
    year_cohorts = [c for c in cal.cohorts if c.sort_key.startswith(_year_str(year))]
    for c in year_cohorts:
        rate = f"{c.hit_rate * 100:.0f}%" if c.hit_rate is not None else "n/a"
        out.append(f"  {c.period}: {c.graded} graded, {rate} correct.")
    if cal.conviction_calibration is not None and cal.conviction_calibration.n:
        cc = cal.conviction_calibration
        out.append(
            f"Brier (conviction-implied probability vs outcome): {cc.brier:.3f} "
            f"(baseline {cc.baseline_brier:.3f}, n={cc.n})."
        )
    else:
        out.append("Brier: no data yet (needs graded correct/wrong calls with stated conviction).")
    return out


def _decision_pnl_lines(db_path: Path, repo_root: Path, year: int) -> list[str]:
    report = build_decision_pnl(db_path=db_path, repo_root=repo_root)
    out: list[str] = []
    for r in report.rows:
        if not r.responded_at or r.responded_at[:4] != _year_str(year):
            continue
        subject = r.ticker or "cross-book"
        scored = f"{r.scored_pct:+.1%}" if r.scored_pct is not None else "not scorable"
        out.append(f"- {subject} [{r.lens}] {r.status.upper()} -> {scored} ({r.note})")
    return out


def build_evidence_pack(*, db_path: Path, repo_root: Path, year: int) -> AnnualLetterEvidence:
    """Pure read assembly — every line traces to an existing store, nothing
    synthesized. Safe to call repeatedly (e.g. from --dry-run)."""
    return AnnualLetterEvidence(
        year=year,
        ledger_lines=_ledger_lines(db_path, year),
        position_lines=_position_lines(db_path, year),
        redteam_lines=_redteam_lines(db_path, year),
        calibration_lines=_calibration_lines(db_path, year),
        decision_pnl_lines=_decision_pnl_lines(db_path, repo_root, year),
    )


def render_evidence_md(ev: AnnualLetterEvidence) -> str:
    def _section(title: str, lines: list[str]) -> str:
        body = "\n".join(lines) if lines else "(none on file)"
        return f"### {title}\n{body}\n"

    return "\n".join(
        [
            f"## {ev.year} evidence pack",
            _section("Thesis ledger entries", ev.ledger_lines),
            _section("Positions opened/closed", ev.position_lines),
            _section("Red Team responses", ev.redteam_lines),
            _section("Calibration / Brier trajectory", ev.calibration_lines),
            _section("Decision P&L (Red Team)", ev.decision_pnl_lines),
        ]
    )


_LETTER_PROMPT = """You are drafting a letter-to-self for a solo equity-research investor,
covering calendar year {year}. Write in the FIRST PERSON, as the owner
writing to himself — not as an advisor addressing a client. The letter must
be grounded ONLY in the evidence below; do not invent beliefs, trades, or
numbers not present in it.

EVIDENCE PACK:
{evidence_md}

Structure the letter around exactly these four questions, in this order:
1. What did I believe, going into and through {year}?
2. What was I wrong about, and how do I know (cite the scored record)?
3. What does the scored record actually say (calibration/Brier, Decision P&L)
   — not vibes, the numbers above?
4. What will I do differently next year — concrete, falsifiable if possible?

Rules:
- Cite specific tickers/dates/numbers from the evidence pack wherever you
  make a claim; a section with nothing to cite should say so plainly rather
  than generalize.
- If a section of the evidence pack is empty ("(none on file)"), say so
  honestly in the letter rather than skip the question.
- Plain, direct, first-person prose. No headers beyond the four numbered
  questions. Markdown body text only.

Return ONLY a JSON object, no fences, no prose outside it:
{{"letter_md": "<the full letter as markdown>"}}"""


def draft_letter(*, evidence: AnnualLetterEvidence, db_path: Path) -> str:
    """The ONE LLM call. Raises StructuredParseError on unusable output (loud,
    per the repo's structured-output discipline); hard stops propagate."""
    prompt = _LETTER_PROMPT.format(year=evidence.year, evidence_md=render_evidence_md(evidence))
    payload = call_llm_structured(
        prompt,
        purpose="annual_letter",
        scope="portfolio",
        expect="object",
        required_keys=("letter_md",),
        db_path=db_path,
    )
    letter = (
        cast("dict[str, object]", payload).get("letter_md") if isinstance(payload, dict) else None
    )
    if not isinstance(letter, str) or not letter.strip():
        raise StructuredParseError("annual_letter: empty letter_md in response")
    return letter.strip()


def _output_path(repo_root: Path, year: int) -> Path:
    return repo_root / "data" / _OUT_DIR_NAME / f"{year}.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--year", type=int, default=None, help="Calendar year to draft (default: last year)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Assemble + print the evidence pack and prompt. ZERO LLM calls, no file written.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-draft even if this year's letter already exists."
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "portfolio.db")
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = args.db.resolve()
    year = args.year or (now_naive_utc().year - 1)
    idempotency_key = f"annual_letter_{year}"
    out_path = _output_path(repo_root, year)

    evidence = build_evidence_pack(db_path=db_path, repo_root=repo_root, year=year)

    if args.dry_run:
        _log("annual_letter_dry_run", idempotency_key=idempotency_key, year=year)
        print(render_evidence_md(evidence))
        print(_LETTER_PROMPT.format(year=year, evidence_md=render_evidence_md(evidence)))
        return 0

    if out_path.exists() and not args.force:
        _log("annual_letter_already_done", idempotency_key=idempotency_key, path=str(out_path))
        print(json.dumps({"idempotency_key": idempotency_key, "already_done": True}))
        return 0

    try:
        letter_md = draft_letter(evidence=evidence, db_path=db_path)
    except StructuredParseError as exc:
        _log("annual_letter_parse_failed", year=year, error=str(exc))
        print(json.dumps({"error": f"parse failure: {exc}"}), file=sys.stderr)
        return 1
    except Exception as exc:  # hard stop (budget/setup) — propagate loudly
        if is_hard_stop(exc):
            _log("annual_letter_hard_stop", year=year, error=f"{type(exc).__name__}: {exc}")
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
            return 1
        _log("annual_letter_transient_failure", year=year, error=f"{type(exc).__name__}: {exc}")
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = f"# Letter to self — {year}\n\n_Drafted {now_naive_utc().isoformat()} by draft_annual_letter.py; edit and sign below before treating this as final._\n\n"
    out_path.write_text(header + letter_md + "\n", encoding="utf-8", newline="\n")

    _log("annual_letter_written", year=year, path=str(out_path))
    print(json.dumps({"idempotency_key": idempotency_key, "path": str(out_path)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
