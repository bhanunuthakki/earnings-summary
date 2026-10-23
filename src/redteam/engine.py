"""Red Team engine orchestration (monthly_red_team.md Phase 2, PR5).

Assembles the per-name pass (held names weight > 0.5%, excluding cash-likes
and index ETFs, one rotating-lens attack each) and the three cross-book
passes, with per-item degrade: a transient LLM failure (parse failure or a
transient call error) defers *that* item and tallies it — the loop continues
so one flaky call never denies every other item that month's brief. A hard
stop (``llm.cli.is_hard_stop`` — budget cap / missing CLI) propagates and
halts the whole run, per ``directives/llm_quota_scheduling.md`` rule 3 and
the ``attach_conditions`` reference implementation
(``src/decision_conditions.py``).

Idempotency key: ``red_team_{YYYY_MM}`` — a run with existing items for that
key is a no-op unless ``force=True``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from bear_lint import BearLintFinding, build_bear_lint
from clock import now_naive_utc
from llm.cli import is_hard_stop
from llm.structured import StructuredParseError, call_llm_structured
from portfolio_correlation import build_holdings_correlation_from_disk
from portfolio_montecarlo import CASH_LIKE_TICKERS
from portfolio_style_factors import build_style_rollup_from_disk
from redteam import lenses, store
from redteam.lenses import NameEvidencePack, RedTeamLensError, load_holdings_json
from redteam.models import RedTeamLLMItem

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

# Index ETFs excluded from the per-name pass alongside cash-likes — no repo-wide
# registry for "index ETF" exists (research confirmed), so this is a small,
# documented, local constant (mirrors CASH_LIKE_TICKERS' shape).
INDEX_ETF_TICKERS: frozenset[str] = frozenset({"VTI", "SPY", "VOO"})

# Held-name weight floor for the per-name pass (task spec: "weight >0.5%").
_MIN_WEIGHT_PCT = 0.005


# ---------------------------------------------------------------------------
# The three cross-book adversarial passes (folded in from the former
# ``redteam.cross_book``; monthly_red_team.md Phase 2): factor-block
# detection, style drift vs. stated strategy, and a human-capital overlay.
# Each is ONE structured LLM call over deterministic, code-assembled inputs —
# the pass never asks the LLM to compute a correlation or a factor loading,
# only to judge what a human-readable summary of already-computed numbers
# implies. Evidence assembly reuses the existing disk-read substrates
# (``portfolio_correlation``, ``portfolio_style_factors``) rather than
# recomputing anything — pure Layer-3 composition.
# ---------------------------------------------------------------------------

_CROSS_BOOK_PURPOSE = "red_team_cross_book"

# The owner's income is Meta/tech/AI-correlated (task directive, human-capital
# overlay spec). This is a small STATIC config, not LLM-inferred — the pass
# only asks the LLM to judge whether the book's OWN AI-linked bets compound
# that already-known factor, and propose a concrete diversification rule.
OWNER_INCOME_CORRELATED_SECTOR = "Big Tech / AI (compensation tied to a large-cap AI/tech employer)"
AI_LINKED_TICKERS: frozenset[str] = frozenset(
    {"META", "GOOGL", "GOOG", "MSFT", "AMZN", "NVDA", "NOW", "VEEV", "RBRK"}
)


def _business_one_liner(repo_root: Path, ticker: str) -> str:
    """A short, code-assembled business-description line for ``ticker`` —
    ``key_driver`` from the holdings JSON, falling back to a bare ticker
    when no holdings file exists (the correlation/style rollups can include
    names with no written thesis)."""
    payload = load_holdings_json(repo_root, ticker)
    if payload is None:
        return ticker
    key_driver = payload.get("key_driver")
    if isinstance(key_driver, str) and key_driver.strip():
        return f"{ticker} ({key_driver.strip()})"
    return ticker


_CROSS_BOOK_SCHEMA_INSTRUCTIONS = (
    "Return ONLY a JSON object with this exact shape:\n"
    '{"attack_md": "<2-4 sentences: the falsifiable claim, anchored to the '
    'numbers given>", '
    '"question_md": "<ONE direct, falsifiable question the analyst must '
    'answer>", '
    '"proposed_change_md": "<ONE concrete rule or scenario change>", '
    '"severity": "<one of high, med, low>"}\n\n'
    "Output the JSON object and nothing else — no markdown fences, no "
    "commentary, no prefatory prose."
)


class RedTeamCrossBookError(RuntimeError):
    """Raised when a cross-book pass's call returns unusable JSON on both
    attempts or fails schema validation. See ``lenses.RedTeamLensError`` for
    the same contract on the per-name side."""


def _cross_book_call(prompt: str, *, lens: str, run_key: str) -> RedTeamLLMItem:
    try:
        payload = call_llm_structured(
            prompt,
            purpose=_CROSS_BOOK_PURPOSE,
            scope=f"red_team:{lens}",
            run_id=run_key,
            expect="object",
            required_keys=("attack_md", "question_md", "proposed_change_md", "severity"),
            schema=TypeAdapter(RedTeamLLMItem),
        )
    except StructuredParseError as exc:
        raise RedTeamCrossBookError(f"{lens}: unusable JSON: {exc}") from exc
    try:
        return RedTeamLLMItem.model_validate(payload)
    except ValidationError as exc:
        raise RedTeamCrossBookError(f"{lens}: schema validation failed: {exc}") from exc


def factor_block_prompt(
    repo_root: Path,
    *,
    clusters: Sequence[tuple[tuple[str, ...], float, float]],
    weights: Mapping[str, float],
) -> str | None:
    """``clusters`` is ``(tickers, combined_weight_pct, avg_corr)`` tuples
    (from ``portfolio_correlation.CrowdingCluster``). ``None`` when there are
    no clusters — nothing to attack, the caller skips the pass entirely
    (zero LLM spend on an empty book-shape)."""
    if not clusters:
        return None
    lines: list[str] = []
    for tickers, combined_weight_pct, avg_corr in clusters:
        names = ", ".join(_business_one_liner(repo_root, t) for t in tickers)
        lines.append(
            f"- {names}: combined {combined_weight_pct:.1f}% of book, "
            f"avg pairwise correlation {avg_corr:.2f}"
        )
    other = ", ".join(f"{t} ({w:.1%})" for t, w in sorted(weights.items()))
    return (
        "You are the monthly adversarial Red Team reviewing a long-only "
        "equity book for hidden CONCENTRATION masquerading as diversification.\n\n"
        "ATTACK ANGLE: shared-factor / crowding block detection. The "
        "following clusters of held names move together (correlation >= 0.70 "
        "on trailing daily returns):\n" + "\n".join(lines) + "\n\n"
        f"Full book weights for context: {other}\n\n"
        "Identify the SINGLE most dangerous cluster — name the shared factor "
        "explicitly (a macro driver, a geography, a supply chain, a funding "
        "regime) — and argue why the owner is running a bigger single bet "
        "than the position sizes individually suggest.\n\n" + _CROSS_BOOK_SCHEMA_INSTRUCTIONS
    )


def style_drift_prompt(
    *,
    legs: Sequence[tuple[str, str, float | None]],
    strategy_summary: str,
) -> str | None:
    """``legs`` is ``(label, spread_label, book_beta)`` tuples (from
    ``portfolio_style_factors.StyleFactorLeg``). ``None`` when no leg has a
    usable beta."""
    usable = [(label, spread, beta) for label, spread, beta in legs if beta is not None]
    if not usable:
        return None
    lines = [f"- {label} ({spread}): book beta {beta:+.2f}" for label, spread, beta in usable]
    return (
        "You are the monthly adversarial Red Team checking a long-only "
        "equity book for STYLE DRIFT against its own stated strategy.\n\n"
        f"Stated strategy: {strategy_summary}\n\n"
        "Current style-factor loadings (book beta to each factor spread, "
        "renormalized over priced names):\n" + "\n".join(lines) + "\n\n"
        "ATTACK ANGLE: identify the loading that is MOST inconsistent with "
        "the stated strategy, and argue what unintended bet the book is "
        "actually running as a result (e.g. a value tilt hiding as GARP, "
        "momentum-chasing beyond the stated sleeve).\n\n" + _CROSS_BOOK_SCHEMA_INSTRUCTIONS
    )


def human_capital_prompt(*, weights: Mapping[str, float]) -> str | None:
    """Static config (``AI_LINKED_TICKERS``) + the book's actual weights —
    ``None`` when no held name is AI-linked (nothing to compound)."""
    linked = {t: w for t, w in weights.items() if t.upper() in AI_LINKED_TICKERS}
    if not linked:
        return None
    combined_pct = 100.0 * sum(linked.values())
    lines = ", ".join(f"{t} ({w:.1%})" for t, w in sorted(linked.items(), key=lambda kv: -kv[1]))
    return (
        "You are the monthly adversarial Red Team checking whether a "
        "long-only equity book compounds the owner's OWN human-capital risk.\n\n"
        f"Owner's income is correlated with: {OWNER_INCOME_CORRELATED_SECTOR}.\n\n"
        f"Held names also exposed to that same factor (AI/Big-Tech demand or "
        f"disruption): {lines} — combined {combined_pct:.1f}% of book.\n\n"
        "ATTACK ANGLE: the owner's paycheck AND a meaningful slice of the "
        "portfolio would draw down together in a Big-Tech/AI-capex "
        "slowdown. Argue how large that compounding bet actually is once "
        "human capital is treated as an (illiquid, un-diversifiable) asset "
        "in the total portfolio, and propose a concrete trim or hedge "
        "rule.\n\n" + _CROSS_BOOK_SCHEMA_INSTRUCTIONS
    )


def generate_factor_block_item(
    repo_root: Path,
    *,
    clusters: Sequence[tuple[tuple[str, ...], float, float]],
    weights: Mapping[str, float],
    run_key: str,
) -> RedTeamLLMItem | None:
    prompt = factor_block_prompt(repo_root, clusters=clusters, weights=weights)
    if prompt is None:
        return None
    return _cross_book_call(prompt, lens="factor_block", run_key=run_key)


def generate_style_drift_item(
    *,
    legs: Sequence[tuple[str, str, float | None]],
    strategy_summary: str,
    run_key: str,
) -> RedTeamLLMItem | None:
    prompt = style_drift_prompt(legs=legs, strategy_summary=strategy_summary)
    if prompt is None:
        return None
    return _cross_book_call(prompt, lens="style_drift", run_key=run_key)


def generate_human_capital_item(
    *,
    weights: Mapping[str, float],
    run_key: str,
) -> RedTeamLLMItem | None:
    prompt = human_capital_prompt(weights=weights)
    if prompt is None:
        return None
    return _cross_book_call(prompt, lens="human_capital", run_key=run_key)


# The owner's stated strategy (directives/monthly_red_team.md Phase 2: "style
# drift vs stated strategy (value/size/momentum loadings vs the owner's
# GARP+index+momentum-sleeve statement)"). Short, code-owned summary — not
# re-derived from prose each run.
STRATEGY_SUMMARY = (
    "GARP (growth-at-a-reasonable-price) core of concentrated, high-conviction "
    "single-name bets, sized well above an index weight; a passive-index sleeve "
    "(VTI/SPY/VOO) for diversification; a smaller momentum/thematic sleeve. "
    "Deliberately NOT broadly diversified — concentration is the strategy, not a bug."
)


@dataclass(slots=True)
class RedTeamRunResult:
    """Summary of one ``run_red_team`` call — what the CLI prints and the
    tally structured events log."""

    run_key: str
    already_done: bool = False
    dry_run: bool = False
    held_tickers: list[str] = field(default_factory=list[str])
    tally: dict[str, int] = field(default_factory=dict[str, int])
    item_ids: list[int] = field(default_factory=list[int])
    first_prompt: str | None = None
    first_prompt_label: str | None = None


def _run_key(month: str) -> str:
    year_s, month_s = month.split("-", 1)
    return f"red_team_{int(year_s):04d}_{int(month_s):02d}"


def _current_month() -> str:
    now = now_naive_utc()
    return f"{now.year:04d}-{now.month:02d}"


def held_tickers(repo_root: Path) -> dict[str, float]:
    """Weight > 0.5%, cash-likes and index ETFs excluded, from the
    materialized-weights disk cache (the render path's only weight source —
    never a live tracker call, per ``portfolio_weights``' own contract)."""
    from portfolio_weights import read_materialized_weights

    raw = read_materialized_weights(repo_root)
    excluded = CASH_LIKE_TICKERS | INDEX_ETF_TICKERS
    return {t: w for t, w in raw.items() if w > _MIN_WEIGHT_PCT and t.upper() not in excluded}


def _other_holdings_line(ticker: str, weights: Mapping[str, float]) -> str:
    others = sorted((t, w) for t, w in weights.items() if t != ticker)
    return ", ".join(f"{t} ({w:.1%})" for t, w in others)


def _bump(tally: dict[str, int], key: str) -> None:
    tally[key] = tally.get(key, 0) + 1


def run_red_team(
    *,
    repo_root: Path,
    db_path: Path,
    month: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
) -> RedTeamRunResult:
    """Run one monthly red-team pass. See module docstring for the degrade
    policy. ``dry_run`` assembles every evidence pack + prompt (deterministic,
    zero LLM calls) so the CLI can preview item counts before spending."""
    month_resolved = month or _current_month()
    run_key = _run_key(month_resolved)
    result = RedTeamRunResult(run_key=run_key, dry_run=dry_run)

    if not force and not dry_run and store.run_key_exists(db_path=db_path, run_key=run_key):
        log.info({"event": "red_team_already_done", "run_key": run_key})
        result.already_done = True
        return result

    weights = held_tickers(repo_root)
    tickers = sorted(weights)
    if limit is not None:
        tickers = tickers[:limit]
    result.held_tickers = tickers
    month_index = lenses.month_index_for(month_resolved)

    import sqlite3

    conn: sqlite3.Connection | None = None
    if not dry_run:
        from user_state._db import open_conn

        try:
            conn = open_conn(db_path)
        except (FileNotFoundError, RuntimeError) as exc:
            log.warning({"event": "red_team_db_open_failed", "error": str(exc)})
            conn = None

    try:
        _run_per_name(
            repo_root,
            conn,
            weights=weights,
            tickers=tickers,
            month_index=month_index,
            run_key=run_key,
            db_path=db_path,
            dry_run=dry_run,
            result=result,
        )
        _run_cross_book(
            repo_root,
            weights=weights,
            tickers=tickers,
            run_key=run_key,
            db_path=db_path,
            dry_run=dry_run,
            result=result,
        )
    finally:
        if conn is not None:
            conn.close()

    log.info({"event": "red_team_run_done", "run_key": run_key, **result.tally})
    return result


def _run_per_name(
    repo_root: Path,
    conn: object,
    *,
    weights: Mapping[str, float],
    tickers: list[str],
    month_index: int,
    run_key: str,
    db_path: Path,
    dry_run: bool,
    result: RedTeamRunResult,
) -> None:
    # Bear-realism lint (Phase 1 PR1, src/bear_lint.py): one book-wide pass,
    # not one per name — a pure disk/DB read, safe even in --dry-run. A
    # failure here (e.g. no dcf_runs table) degrades to no bear evidence for
    # every name rather than blocking the per-name pass.
    try:
        bear_by_ticker: dict[str, BearLintFinding] = {
            f.ticker: f for f in build_bear_lint(db_path, repo_root=repo_root).findings
        }
    except Exception as exc:  # best-effort context, never blocks the pass
        log.debug({"event": "red_team_bear_lint_failed", "error": str(exc)})
        bear_by_ticker = {}

    # profile_drift lens evidence (tenet-2 Phase 4): book-wide, computed ONCE
    # per run (like bear_by_ticker above), not one read per name — every
    # ticker that lands on this lens this month shares the same evidence.
    profile_drift_evidence = lenses.build_profile_drift_evidence(conn)

    for ticker in tickers:
        lens = lenses.lens_for(ticker, month_index)
        pack: NameEvidencePack | None = lenses.build_name_evidence_pack(
            repo_root,
            conn,
            ticker=ticker,
            weight_pct=weights[ticker],
            bear_finding=bear_by_ticker.get(ticker),
            profile_drift_evidence=profile_drift_evidence,
        )
        if pack is None:
            _bump(result.tally, "no_thesis")
            continue

        other_line = _other_holdings_line(ticker, weights)

        if dry_run:
            prompt = lenses.build_prompt(pack, lens, other_holdings_line=other_line)
            _bump(result.tally, f"lens:{lens}")
            _bump(result.tally, "would_generate")
            if result.first_prompt is None:
                result.first_prompt = prompt
                result.first_prompt_label = f"per_name:{ticker}:{lens}"
            continue

        try:
            llm_item = lenses.generate_per_name_item(
                pack, lens, run_key=run_key, other_holdings_line=other_line
            )
        except RedTeamLensError as exc:
            log.warning(
                {
                    "event": "red_team_per_name_parse_failed",
                    "ticker": ticker,
                    "lens": lens,
                    "error": str(exc),
                }
            )
            _bump(result.tally, "parse_failed")
            continue
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            log.warning(
                {
                    "event": "red_team_per_name_transient_failure",
                    "ticker": ticker,
                    "lens": lens,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            )
            _bump(result.tally, "deferred_transient")
            continue

        item_id = store.insert_item(
            db_path=db_path,
            run_key=run_key,
            ticker=ticker,
            lens=lens,
            kind="per_name",
            item=llm_item,
        )
        result.item_ids.append(item_id)
        _bump(result.tally, f"lens:{lens}")
        _bump(result.tally, "generated")


def _run_cross_book(
    repo_root: Path,
    *,
    weights: Mapping[str, float],
    tickers: list[str],
    run_key: str,
    db_path: Path,
    dry_run: bool,
    result: RedTeamRunResult,
) -> None:
    clusters: list[tuple[tuple[str, ...], float, float]] = []
    correlation = build_holdings_correlation_from_disk(repo_root, tickers, weights)
    if correlation is not None:
        clusters = [
            (tuple(c.tickers), c.combined_weight_pct, c.avg_corr) for c in correlation.clusters
        ]

    legs: list[tuple[str, str, float | None]] = []
    rollup = build_style_rollup_from_disk(repo_root, tickers, weights)
    if rollup is not None:
        legs = [(leg.label, leg.spread_label, leg.book_beta) for leg in rollup.legs]

    # (lens, preview-prompt-or-None, live generator thunk) — the preview prompt
    # is only used for the None-skip check and the --dry-run preview; the live
    # path calls the public generate_* wrapper (which re-derives the same
    # prompt deterministically from the same inputs — cheap, no LLM cost).
    passes: list[tuple[str, str | None, Callable[[], RedTeamLLMItem | None]]] = [
        (
            "factor_block",
            factor_block_prompt(repo_root, clusters=clusters, weights=weights),
            lambda: generate_factor_block_item(
                repo_root, clusters=clusters, weights=weights, run_key=run_key
            ),
        ),
        (
            "style_drift",
            style_drift_prompt(legs=legs, strategy_summary=STRATEGY_SUMMARY),
            lambda: generate_style_drift_item(
                legs=legs, strategy_summary=STRATEGY_SUMMARY, run_key=run_key
            ),
        ),
        (
            "human_capital",
            human_capital_prompt(weights=weights),
            lambda: generate_human_capital_item(weights=weights, run_key=run_key),
        ),
    ]

    for lens, preview_prompt, generate in passes:
        if preview_prompt is None:
            _bump(result.tally, f"cross_book_skipped:{lens}")
            continue
        if dry_run:
            _bump(result.tally, f"lens:{lens}")
            _bump(result.tally, "would_generate")
            if result.first_prompt is None:
                result.first_prompt = preview_prompt
                result.first_prompt_label = f"cross_book:{lens}"
            continue

        try:
            llm_item = generate()
        except RedTeamCrossBookError as exc:
            log.warning(
                {"event": "red_team_cross_book_parse_failed", "lens": lens, "error": str(exc)}
            )
            _bump(result.tally, "parse_failed")
            continue
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            log.warning(
                {
                    "event": "red_team_cross_book_transient_failure",
                    "lens": lens,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            )
            _bump(result.tally, "deferred_transient")
            continue

        if llm_item is None:
            # Prompt inputs went empty between the preview check and the live
            # call (e.g. clusters recomputed empty) — treat like the upfront skip.
            _bump(result.tally, f"cross_book_skipped:{lens}")
            continue

        item_id = store.insert_item(
            db_path=db_path,
            run_key=run_key,
            ticker=None,
            lens=lens,
            kind="cross_book",
            item=llm_item,
        )
        result.item_ids.append(item_id)
        _bump(result.tally, f"lens:{lens}")
        _bump(result.tally, "generated")
