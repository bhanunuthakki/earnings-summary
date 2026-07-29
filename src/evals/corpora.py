"""Audit-mode corpora: real production outputs for the rubric judge (mode B).

Mode A (golden sets) generates fresh outputs through the production call path;
mode B audits what production has ALREADY written. Each loader returns the
artifacts of one purpose as ``AuditItem`` rows, newest first, so
``--limit N`` grades the freshest N and ``--since-days D`` scopes a weekly
cron run to the artifacts that changed since the last one
(directives/llm_evals_plan.md §2.1, PR 2).

Loaders are read-only and tolerant of missing sources: a repo without
``data/bear_case/`` or without the ``advisor_memos`` table simply has nothing
to audit (empty corpus), which the runner reports as "nothing to grade" —
distinct from a grading failure.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

# Defensive cap on the text handed to the judge. The real artifacts sit far
# below it (bear cases ~8-15K chars, summaries ~3-6K, memos ~4K); the cap only
# bites pathological files, and the truncation is marked so the judge (and the
# persisted transcript) can see it happened.
MAX_CONTENT_CHARS = 30_000

# Mirrors _SUMMARY_RX in src/report/sections/earnings.py (the §5 reader of the
# same files). Replicated rather than imported: that module pulls the pydantic
# report stack, which the eval harness doesn't need.
_SUMMARY_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?:investor_update_)?summary\.txt$"
)


@dataclass(frozen=True, slots=True)
class AuditItem:
    """One production artifact queued for rubric judgment.

    ``item_id`` maps onto eval_case_results.case_id; ``label`` onto its
    question column (it names the source, so the failed-case drill-down reads
    well); ``produced_at`` is naive-UTC per repo convention and drives the
    ``--since-days`` freshness filter (None = age unknown ⇒ excluded by any
    since filter, kept in unfiltered runs).
    """

    item_id: str
    label: str
    ticker: str | None
    content: str
    produced_at: datetime | None = None


CorpusLoader = Callable[[Path], list[AuditItem]]


def _clip(text: str) -> str:
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    return text[:MAX_CONTENT_CHARS] + f"\n...[truncated from {len(text)} chars]"


def _mtime_naive_utc(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).replace(tzinfo=None)
    except OSError:
        return None


def _parse_naive_utc(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp to naive-UTC (the AuditItem.produced_at
    contract). Older ``advisor_memos``/``ask_turns`` rows persisted an offset
    (``…+00:00``) while newer ones are naive; a bare ``fromisoformat`` therefore
    yields a mix of aware and naive datetimes that then crashes the newest-first
    sort and ``filter_since`` ("can't compare offset-naive and offset-aware").
    Coercing any aware stamp to naive-UTC here keeps the whole pipeline naive."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def load_bear_case_corpus(repo_root: Path) -> list[AuditItem]:
    """Every ``data/bear_case/<TICKER>.json`` sidecar, newest mtime first.

    The sidecar is the parsed JSON the §7 section cached (the same content
    the llm_artifacts row carries); unreadable/unparseable files are skipped
    with a log line — a corrupt cache file is the section's problem, not a
    quality score.
    """
    out: list[AuditItem] = []
    base = repo_root / "data" / "bear_case"
    if not base.is_dir():
        return out
    for path in sorted(base.glob("*.json")):
        ticker = path.stem.upper()
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning(
                {
                    "event": "eval_corpus_skip_unreadable",
                    "purpose": "bear_case",
                    "path": str(path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        out.append(
            AuditItem(
                item_id=ticker,
                label=f"bear_case/{ticker} (data/bear_case/{path.name})",
                ticker=ticker,
                content=_clip(json.dumps(payload, indent=2, ensure_ascii=False)),
                produced_at=_mtime_naive_utc(path),
            )
        )
    out.sort(key=lambda i: i.produced_at or datetime.min, reverse=True)
    return out


def load_transcript_summary_corpus(repo_root: Path) -> list[AuditItem]:
    """Every per-quarter summary in ``.tmp/``, newest fiscal quarter first.

    Matches the same filename grammar §5 reads (``_SUMMARY_RX``), so the
    corpus is exactly the set of notes the report surfaces.
    """
    keyed: list[tuple[int, int, AuditItem]] = []
    tmp_dir = repo_root / ".tmp"
    if not tmp_dir.is_dir():
        return []
    for path in sorted(tmp_dir.iterdir()):
        m = _SUMMARY_RX.match(path.name)
        if not m:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning(
                {
                    "event": "eval_corpus_skip_unreadable",
                    "purpose": "transcript_summary",
                    "path": str(path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if not text.strip():
            continue
        ticker = m.group("ticker")
        q, y = int(m.group("q")), int(m.group("y"))
        keyed.append(
            (
                y,
                q,
                AuditItem(
                    item_id=f"{ticker}_Q{q}_{y}",
                    label=f"transcript_summary/{ticker} Q{q}'{y % 100:02d} (.tmp/{path.name})",
                    ticker=ticker,
                    content=_clip(text),
                    produced_at=_mtime_naive_utc(path),
                ),
            )
        )
    # Fiscal recency beats file mtime here: a re-rendered old quarter should
    # not outrank the newest print under --limit.
    keyed.sort(key=lambda t: (t[0], t[1], t[2].item_id), reverse=True)
    return [item for _, _, item in keyed]


def load_advisor_next_dollar_corpus(repo_root: Path) -> list[AuditItem]:
    """Every ``advisor_memos`` row with kind='next_dollar', newest first.

    Missing DB / missing table ⇒ empty corpus (nothing has been generated to
    audit), logged at info — the weekly cron must not fail on a repo that
    hasn't run the advisor yet.
    """
    out: list[AuditItem] = []
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return out
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_db_open_failed", "error": str(exc)})
        return out
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='advisor_memos'"
        ).fetchone()
        if present is None:
            log.info({"event": "eval_corpus_no_advisor_memos_table"})
            return out
        rows = conn.execute(
            """
            SELECT id, ticker, title, body_md, created_at
            FROM advisor_memos
            WHERE kind = 'next_dollar' AND body_md IS NOT NULL AND body_md != ''
            ORDER BY id DESC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_advisor_read_failed", "error": str(exc)})
        return out
    finally:
        conn.close()
    for memo_id, ticker, title, body_md, created_at in rows:
        out.append(
            AuditItem(
                item_id=f"memo:{memo_id}",
                label=f"advisor_next_dollar/memo:{memo_id} — {str(title)[:80]}",
                ticker=str(ticker) if ticker else None,
                content=_clip(str(body_md)),
                produced_at=_parse_naive_utc(created_at),
            )
        )
    return out


# A portfolio-scope data turn is recorded as an assistant turn whose body is the
# view's one-line confirmation (src/ask/engine.py::_data_events) — it is NOT an
# advisory prose answer, so the answer-quality rubric must not grade it.
_DATA_VIEW_MARKER = "(rendered as a live data view)"
# Floor on a gradeable advisory answer: shorter assistant turns are command
# acks / error lines / one-word replies, not advice worth a 4-facet audit.
_MIN_ASK_ANSWER_CHARS = 80
# How many preceding turns to show the judge as conversation context. Enough to
# anchor "is the answer responsive / does it advance the open thread" without
# ballooning the prompt (each is clipped by the engine's history cap on write).
_ASK_CONTEXT_TURNS = 4


def _is_data_view_turn(text: str) -> bool:
    return text.rstrip().endswith(_DATA_VIEW_MARKER)


def _format_ask_citations(citations_json: object) -> str:
    """Render the answer's attached citations (ask_turns.citations_json — a list
    of chip payloads, see ask.grounding.chip_payload) as the numbered ground
    truth the grounding facet checks against. Tolerant of any/empty/garbage
    payload: returns "(no sources cited)" rather than raising."""
    if not isinstance(citations_json, str) or not citations_json.strip():
        return "(no sources cited)"
    try:
        parsed: object = json.loads(citations_json)
    except (ValueError, TypeError):
        return "(no sources cited)"
    if not isinstance(parsed, list) or not parsed:
        return "(no sources cited)"
    lines: list[str] = []
    for raw in cast("list[object]", parsed):
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, object]", raw)
        n = item.get("n")
        label = str(item.get("label") or item.get("kind") or "source")
        conf = item.get("confidence")
        marker = f"[{n}]" if isinstance(n, int) else "-"
        conf_str = (
            f" (conf {round(float(conf) * 100)}%)"
            if isinstance(conf, (int, float)) and not isinstance(conf, bool)
            else ""
        )
        lines.append(f"{marker} {label}{conf_str}")
    return "\n".join(lines) if lines else "(no sources cited)"


def _format_ask_item_content(
    prior: list[sqlite3.Row], answer: sqlite3.Row, scope: str | None
) -> str:
    """Assemble the audited content: conversation context, the answer under
    audit, and the cited evidence — each delimited so the judge grades the
    answer alone (the rubric pins this contract)."""
    context_lines = [
        f"[{str(t['role']).upper()}] {str(t['text']).strip()}"
        for t in prior
        if str(t["text"]).strip()
    ]
    context_block = "\n\n".join(context_lines) if context_lines else "(no prior turns)"
    cites_block = _format_ask_citations(answer["citations_json"])
    scope_line = f"Conversation scope: {scope}\n\n" if scope else ""
    return (
        f"{scope_line}"
        "=== CONVERSATION SO FAR (context only — do NOT grade) ===\n"
        f"{context_block}\n\n"
        "=== ANSWER UNDER AUDIT (grade THIS) ===\n"
        f"{str(answer['text']).strip()}\n\n"
        "=== EVIDENCE THE ANSWER CITED (the only ground truth available) ===\n"
        f"{cites_block}"
    )


def load_ask_advisory_answer_corpus(repo_root: Path) -> list[AuditItem]:
    """Every real production advisory answer in ``ask_turns``, newest first.

    The corpus is the assistant turns of the conversational ask path
    (``src/ask/engine.py`` portfolio + ticker narrative scopes), each paired
    with the preceding turns for context and the evidence it cited. Data-view
    confirmation turns and trivially-short acks are excluded — only genuine
    advisory prose is graded.

    Missing DB / missing ``ask_turns`` table ⇒ empty corpus (the ask path
    hasn't run yet), logged at info — the weekly cron must not fail on a repo
    with no conversation history. Newest answer first so ``--limit N`` grades
    the freshest and ``--since-days`` scopes the weekly fresh-only run.
    """
    out: list[AuditItem] = []
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return out
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_db_open_failed", "error": str(exc)})
        return out
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ask_turns'"
        ).fetchone()
        if present is None:
            log.info({"event": "eval_corpus_no_ask_turns_table"})
            return out
        rows = conn.execute(
            "SELECT id, session_id, role, text, citations_json, created_at"
            " FROM ask_turns ORDER BY session_id, id ASC"
        ).fetchall()
        scopes = {
            str(r["id"]): r["scope"]
            for r in conn.execute("SELECT id, scope FROM ask_sessions").fetchall()
        }
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_ask_turns_read_failed", "error": str(exc)})
        return out
    finally:
        conn.close()

    by_session: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_session.setdefault(str(r["session_id"]), []).append(r)

    for sid, turns in by_session.items():
        for idx, turn in enumerate(turns):
            if str(turn["role"]) != "assistant":
                continue
            text = str(turn["text"]).strip()
            if len(text) < _MIN_ASK_ANSWER_CHARS or _is_data_view_turn(text):
                continue
            prior = turns[max(0, idx - _ASK_CONTEXT_TURNS) : idx]
            scope = scopes.get(sid)
            out.append(
                AuditItem(
                    item_id=f"ask_turn:{turn['id']}",
                    label=f"ask_advisory_answer/turn:{turn['id']}"
                    + (f" ({scope})" if scope else ""),
                    ticker=None,
                    content=_clip(_format_ask_item_content(prior, turn, scope)),
                    produced_at=_parse_naive_utc(turn["created_at"]),
                )
            )
    out.sort(key=lambda i: i.produced_at or datetime.min, reverse=True)
    return out


def load_calibration_coach_corpus(repo_root: Path) -> list[AuditItem]:
    """Every persisted monthly calibration scorecard that carries synthesised
    coach prose, newest period first (close_the_loops L8).

    Reads ``data/calibration_scorecard/<period>.json`` written by
    ``calibration_coach.save_scorecard``. The graded text is the denormalised
    ``prose`` field (named biases + the period's behavioural experiment with
    their deterministic grounding). Scorecards too thin to coach (no biases, no
    experiment) carry nothing to judge and are skipped — distinct from a grading
    failure. Missing directory ⇒ empty corpus (no scorecard generated yet)."""
    out: list[AuditItem] = []
    base = Path(repo_root) / "data" / "calibration_scorecard"
    if not base.is_dir():
        return out
    for path in sorted(base.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        card = cast("dict[str, object]", raw)
        biases = card.get("biases")
        experiment = card.get("experiment")
        has_coach = (isinstance(biases, list) and biases) or isinstance(experiment, dict)
        prose = card.get("prose")
        if not has_coach or not isinstance(prose, str) or not prose.strip():
            continue
        period = str(card.get("period") or path.stem)
        out.append(
            AuditItem(
                item_id=f"calibration_coach:{period}",
                label=f"calibration_coach/{period}",
                ticker=None,
                content=_clip(prose),
                produced_at=_mtime_naive_utc(path),
            )
        )
    out.sort(key=lambda i: i.produced_at or datetime.min, reverse=True)
    return out


def load_peer_selection_corpus(repo_root: Path) -> list[AuditItem]:
    """Every cached peer selection artifact, newest first.

    Reads from ``data/peer_selection/<TICKER>.json`` files written by
    ``compute.peer_selection.extract_for_ticker`` on the --enable-llm build.
    The rubric judge scores each list for business-model match, why-string
    specificity, cross-boundary coverage, and peer count.

    Missing directory or no files → empty corpus (nothing generated yet — not
    a grading failure).
    """
    out: list[AuditItem] = []
    peer_dir = Path(repo_root) / "data" / "peer_selection"
    if not peer_dir.exists():
        return out
    for path in sorted(peer_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        ticker = path.stem.upper()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        raw_dict = cast("dict[str, object]", raw)
        suggestions = raw_dict.get("suggestions")
        if not isinstance(suggestions, list):
            continue
        content = json.dumps(suggestions, indent=2, ensure_ascii=False)
        out.append(
            AuditItem(
                item_id=f"peer_selection:{ticker}",
                label=f"peer_selection/{ticker}",
                ticker=ticker,
                content=_clip(content),
                produced_at=_parse_naive_utc(raw_dict.get("extracted_at")),
            )
        )
    return out


def load_earnings_themes_corpus(repo_root: Path) -> list[AuditItem]:
    """Every cached earnings-themes artifact, newest mtime first.

    Reads from ``data/earnings_themes/<TICKER>.json`` files written by
    ``report.sections.earnings._write_themes_cache`` on the --enable-llm
    build. The rubric judge scores the prepared-vs-Q&A theme split for
    distinctiveness, cross-quarter grounding, evidence specificity, and
    correct lane assignment.

    Missing directory or no files → empty corpus (nothing generated yet).
    """
    out: list[AuditItem] = []
    themes_dir = Path(repo_root) / "data" / "earnings_themes"
    if not themes_dir.exists():
        return out
    for path in sorted(themes_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        ticker = path.stem.upper()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        raw_dict = cast("dict[str, object]", raw)
        payload = raw_dict.get("payload")
        if not isinstance(payload, dict):
            continue
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        out.append(
            AuditItem(
                item_id=f"earnings_themes:{ticker}",
                label=f"earnings_themes_split/{ticker}",
                ticker=ticker,
                content=_clip(content),
                produced_at=_mtime_naive_utc(path),
            )
        )
    return out


def load_qa_topics_corpus(repo_root: Path) -> list[AuditItem]:
    """Every cached Q&A-topic-label set, newest mtime first.

    Reads from ``data/qa_topics/<TICKER>.json`` files written by
    ``report.sections.qa_roster._save_topics_cache`` on the --enable-llm
    build. The file contains a ``by_key`` dict mapping cache-key hashes to
    topic arrays; each entry becomes one AuditItem scored independently so
    per-quarter label quality is visible.

    Missing directory or no files → empty corpus (nothing generated yet).
    """
    out: list[AuditItem] = []
    topics_dir = Path(repo_root) / "data" / "qa_topics"
    if not topics_dir.exists():
        return out
    for path in sorted(topics_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        ticker = path.stem.upper()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        raw_dict = cast("dict[str, object]", raw)
        by_key = raw_dict.get("by_key")
        if not isinstance(by_key, dict):
            continue
        for i, (_cache_key, topics) in enumerate(cast("dict[str, object]", by_key).items()):
            if not isinstance(topics, list):
                continue
            content = json.dumps(topics, indent=2, ensure_ascii=False)
            out.append(
                AuditItem(
                    item_id=f"qa_topics:{ticker}:{i}",
                    label=f"qa_topics/{ticker} entry {i}",
                    ticker=ticker,
                    content=_clip(content),
                    produced_at=_mtime_naive_utc(path),
                )
            )
    return out


def load_position_review_corpus(repo_root: Path) -> list[AuditItem]:
    """Every ``advisor_memos`` row with kind='position_review', newest first.

    The graded text is the memo body the /review service wrote (verdict + reason
    + behavioral-check + grounded facts). Missing DB / missing table ⇒ empty
    corpus (nothing generated to audit yet), so the weekly cron never fails on a
    repo that hasn't produced a review — mirrors load_advisor_next_dollar_corpus.
    """
    out: list[AuditItem] = []
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return out
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_db_open_failed", "error": str(exc)})
        return out
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='advisor_memos'"
        ).fetchone()
        if present is None:
            log.info({"event": "eval_corpus_no_advisor_memos_table"})
            return out
        rows = conn.execute(
            """
            SELECT id, ticker, title, body_md, created_at
            FROM advisor_memos
            WHERE kind = 'position_review' AND body_md IS NOT NULL AND body_md != ''
            ORDER BY id DESC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_position_review_read_failed", "error": str(exc)})
        return out
    finally:
        conn.close()
    for memo_id, ticker, title, body_md, created_at in rows:
        out.append(
            AuditItem(
                item_id=f"memo:{memo_id}",
                label=f"position_review/memo:{memo_id} — {str(title)[:80]}",
                ticker=str(ticker) if ticker else None,
                content=_clip(str(body_md)),
                produced_at=_parse_naive_utc(created_at),
            )
        )
    return out


def load_behavior_distill_corpus(repo_root: Path) -> list[AuditItem]:
    """Every staged behavioral rule (``owner_profile_facts`` rows with
    ``category='behavioral'`` and ``provenance='derived'``, latest-row-only),
    newest id first.

    Distinct from ``load_position_review_corpus``'s live-memo shape: a
    behavioral rule's graded citations ARE its evidence, so the judged content
    is the rule's narrative (which already carries the wrong/total tally
    computed from validated citations) -- the rubric grades whether the rule
    reads as a real, falsifiable, second-person pattern grounded in that
    evidence, not a vague truism. Missing DB / missing table -> empty corpus
    (nothing distilled yet), mirroring every other loader here.
    """
    out: list[AuditItem] = []
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return out
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_db_open_failed", "error": str(exc)})
        return out
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='owner_profile_facts'"
        ).fetchone()
        if present is None:
            log.info({"event": "eval_corpus_no_owner_profile_facts_table"})
            return out
        rows = conn.execute(
            """
            SELECT id, key, narrative, created_at
            FROM owner_profile_facts
            WHERE category = 'behavioral' AND provenance = 'derived' AND is_latest = 1
            ORDER BY id DESC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_behavior_distill_read_failed", "error": str(exc)})
        return out
    finally:
        conn.close()
    for fact_id, key, narrative, created_at in rows:
        if not narrative:
            continue
        out.append(
            AuditItem(
                item_id=f"owner_profile_fact:{fact_id}",
                label=f"behavior_distill/{key} (fact:{fact_id})",
                ticker=None,
                content=_clip(str(narrative)),
                produced_at=_parse_naive_utc(created_at),
            )
        )
    return out


def load_incremental_dollar_recommendation_corpus(repo_root: Path) -> list[AuditItem]:
    """Every current (non-superseded) ``incremental_dollar_recommendation``
    artifact, newest first (P0.4a, personal_investment_partner_prd.md
    §7.4/§10.5). Graded content is the artifact's ``content_md`` (the humility
    + evidence prose the rubric actually judges), scope='portfolio' so
    ``ticker`` is always None. Missing DB/table ⇒ empty corpus (nothing
    generated yet), mirroring the other llm_artifacts-backed loaders."""
    out: list[AuditItem] = []
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return out
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_db_open_failed", "error": str(exc)})
        return out
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
        ).fetchone()
        if present is None:
            log.info({"event": "eval_corpus_no_llm_artifacts_table"})
            return out
        rows = conn.execute(
            """
            SELECT id, content_md, generated_at
            FROM llm_artifacts
            WHERE purpose = 'incremental_dollar_recommendation'
              AND scope = 'portfolio'
              AND superseded_by_id IS NULL
              AND content_md IS NOT NULL AND content_md != ''
            ORDER BY id DESC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_incremental_dollar_read_failed", "error": str(exc)})
        return out
    finally:
        conn.close()
    for artifact_id, content_md, generated_at in rows:
        out.append(
            AuditItem(
                item_id=f"artifact:{artifact_id}",
                label=f"incremental_dollar_recommendation/artifact:{artifact_id}",
                ticker=None,
                content=_clip(str(content_md)),
                produced_at=_parse_naive_utc(generated_at),
            )
        )
    return out


def load_investment_decision_card_corpus(repo_root: Path) -> list[AuditItem]:
    """Every current (non-superseded) ``investment_decision_card`` artifact,
    newest first (P1.1, personal_investment_partner_prd.md §8.1/§10.5).
    Graded content is the artifact's ``content_md`` — scope='ticker', so
    ``ticker`` is populated (unlike the portfolio-scope Incremental Dollar
    Recommendation). Missing DB/table ⇒ empty corpus, mirroring the other
    llm_artifacts-backed loaders."""
    out: list[AuditItem] = []
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return out
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_db_open_failed", "error": str(exc)})
        return out
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
        ).fetchone()
        if present is None:
            log.info({"event": "eval_corpus_no_llm_artifacts_table"})
            return out
        rows = conn.execute(
            """
            SELECT id, ticker, content_md, generated_at
            FROM llm_artifacts
            WHERE purpose = 'investment_decision_card'
              AND scope = 'ticker'
              AND superseded_by_id IS NULL
              AND content_md IS NOT NULL AND content_md != ''
            ORDER BY id DESC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning(
            {"event": "eval_corpus_investment_decision_card_read_failed", "error": str(exc)}
        )
        return out
    finally:
        conn.close()
    for artifact_id, ticker, content_md, generated_at in rows:
        out.append(
            AuditItem(
                item_id=f"artifact:{artifact_id}",
                label=f"investment_decision_card/{ticker}/artifact:{artifact_id}",
                ticker=str(ticker) if ticker else None,
                content=_clip(str(content_md)),
                produced_at=_parse_naive_utc(generated_at),
            )
        )
    return out


def load_senior_partner_brief_corpus(repo_root: Path) -> list[AuditItem]:
    """Every current (non-superseded) ``senior_partner_brief`` artifact,
    newest first (P2.2, personal_investment_partner_prd.md §9.1/§10.5).
    Graded content is the artifact's ``content_md`` — scope='portfolio', so
    ``ticker`` is always None (mirrors the Incremental Dollar Recommendation
    loader). Missing DB/table -> empty corpus."""
    out: list[AuditItem] = []
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return out
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_db_open_failed", "error": str(exc)})
        return out
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
        ).fetchone()
        if present is None:
            log.info({"event": "eval_corpus_no_llm_artifacts_table"})
            return out
        rows = conn.execute(
            """
            SELECT id, content_md, generated_at
            FROM llm_artifacts
            WHERE purpose = 'senior_partner_brief'
              AND scope = 'portfolio'
              AND superseded_by_id IS NULL
              AND content_md IS NOT NULL AND content_md != ''
            ORDER BY id DESC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "eval_corpus_senior_partner_brief_read_failed", "error": str(exc)})
        return out
    finally:
        conn.close()
    for artifact_id, content_md, generated_at in rows:
        out.append(
            AuditItem(
                item_id=f"artifact:{artifact_id}",
                label=f"senior_partner_brief/artifact:{artifact_id}",
                ticker=None,
                content=_clip(str(content_md)),
                produced_at=_parse_naive_utc(generated_at),
            )
        )
    return out


# purpose -> loader. The rubric runner resolves its corpus here; adding an
# audit purpose = one rubric file + one loader + one entry (+ registry/model
# wiring asserted by tests).
CORPUS_LOADERS: dict[str, CorpusLoader] = {
    "bear_case": load_bear_case_corpus,
    "transcript_summary": load_transcript_summary_corpus,
    "advisor_next_dollar": load_advisor_next_dollar_corpus,
    "incremental_dollar_recommendation": load_incremental_dollar_recommendation_corpus,
    "investment_decision_card": load_investment_decision_card_corpus,
    "senior_partner_brief": load_senior_partner_brief_corpus,
    "ask_advisory_answer": load_ask_advisory_answer_corpus,
    "calibration_coach": load_calibration_coach_corpus,
    "peer_selection": load_peer_selection_corpus,
    "earnings_themes_split": load_earnings_themes_corpus,
    "qa_topics": load_qa_topics_corpus,
    "position_review": load_position_review_corpus,
    "behavior_distill": load_behavior_distill_corpus,
}


def filter_since(items: list[AuditItem], since_days: int | None) -> list[AuditItem]:
    """Keep items produced within the window. ``None`` = no filter. Items
    with unknown age are excluded by an active filter (a weekly cron judging
    "fresh artifacts" must not re-grade undatable ones every week)."""
    if since_days is None:
        return items
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=since_days)
    return [i for i in items if i.produced_at is not None and i.produced_at >= cutoff]
