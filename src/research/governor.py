"""The nag governor — governed coach initiation (W2, 2026-07-02 grill decision #1).

The grill rescinded master_build's "pull-only / never initiates": the coach MAY
speak first, for a set of deterministic moment classes, under a governor
that operationalizes the owner's own kill bar ("kill it if it reads as
nagging"):

- ``falsifier_breach`` — a ``decision_condition`` alert fired on a
  ``decided_by='owner'`` decision: *your own falsifier tripped*. The single
  highest-value initiation the deep dive found (NU's 15-90d NPL >5% 2Q clause
  sits at the line).
- ``retro_annotation`` — an owner decision stub (pledge or retro-net) has sat
  >24h without its conviction/falsifier: the ledger is accruing unusable
  history until answered.
- ``intent_followup`` — a standing ``kind='intent'`` note is still open after
  a fortnight: "still live?" (re-asked at most once per fortnight via a
  bucketed key).
- ``capacity_breach`` (tenet-2 Phase 3) — an AFFIRMED human-capital bucket cap
  or cash-floor policy fact is currently violated. Collection + its own
  alerts-table episode keying live in ``research.capacity_moments``.
- ``life_event_checkpoint`` (tenet-2 Phase 3) — an affirmed dated life event
  enters its lookahead window; fires once, ever.
- ``profile_drift`` (tenet-2 Phase 3) — an affirmed fact is past its review
  horizon, or contradicted by observed state (e.g. stale tax-bucket totals
  vs. the tracker's live read); a re-affirmation ask, never an accusation.
- ``tenet_challenge`` (B5, 2026-07-19 program overhaul) — a standing Worldview
  Tenet's WEEKLY accountability pass (``synthesis.tenet_accountability``, run
  by ``execution/run_tenet_accountability.py`` off the Sat 09:00 PT cron) has
  a persisted verdict with at least one VIOLATED owner decision. This
  collector makes NO LLM call of its own — it only reads the verdict the
  weekly pass already wrote onto the Tenet's ``insight_notes.meta_json``
  (key ``"accountability"``) — and raises AT MOST ONE such moment per
  CURRENT iso-week: the Tenet with the highest ``est_cost_usd`` (ties: most
  violations), across every current Tenet carrying a violation. See
  ``_tenet_challenge_moments`` below.
- ``post_mortem`` (B6, 2026-07-19 program overhaul) — an exit post-mortem the
  18:00 sweep auto-drafted (``synthesis.exit_postmortem.run_postmortem_drafts``)
  has landed live on a closed ``position_entries`` row. This class makes NO
  LLM call of its own — it only reads whether the row's three grading fields
  are filled AND carry the ``llm_draft`` provenance marker
  (``synthesis.exit_postmortem.drafted_awaiting_glance``) — and, unlike
  ``tenet_challenge``, has no extra "once per week" cap of its own: the
  anti-nag UNIQUE ``class_``+``key`` ledger (``coach_pings``) IS the
  acknowledgment memory (see ``_post_mortem_moments`` below) — a moment is
  collected on every governor pass for every drafted-and-unpinged entry, but
  ``run_governor`` only ever inserts+sends the first time a given key is
  seen, exactly like every other class here.

Governor rules, all deterministic, ZERO LLM:

- **freshness gate** (the owner's staleness callout): a ping fires only after
  the underlying item is verified still-open — falsifier still present and
  ratified, ticker still held, stub still unannotated, intent still open.
  Can't verify → don't ping (recorded ``skipped_stale``). Extended for
  tenet-2 Phase 3: ANY ping grounded in an ``owner_profile_facts`` row
  (``life_event_checkpoint``, ``profile_drift``) also requires that row to
  still be ``status='affirmed'`` AND still the latest (not superseded) —
  ``_profile_fact_still_affirmed`` below. A ``capacity_breach`` ping requires
  its backing alert to still be pending AND the breach condition to still
  hold right now (``capacity_moments.capacity_breach_still_active``). Same
  rule either way: can't verify → don't ping.
- **frequency caps**: ≤ ``DAILY_CAP`` sends/day, ≤ ``WEEKLY_CAP``/week across
  all classes; overflow lands ``digest`` (surfaced quietly, never pushed).
  Competing moments contend in ``CLASS_PRIORITY`` order (money-at-risk first).
- **budget scope (B9 audit)**: the caps govern INITIATIONS only. Exempt by
  owner ruling (2026-07-19): capture replies (``capture.coach_reply``
  receipts), adoption announcements (``synthesis.adoption_notify``), and the
  one-off post-mortem backfill summary — all responses/receipts, not asks.
  The alert feed's gating (#873-876) is a SEPARATE budget entirely: alerts
  are market events, coach items are behavioral asks — never pooled.
- **auto-mute**: ``MUTE_AFTER`` consecutive dismissals of a class mutes it
  until explicitly cleared — dismissals train the coach; it never argues.
- one row per moment forever (UNIQUE class+key) — a moment is never re-pushed.
  Single exception: ``send_failed`` (an attempted push that died in flight)
  stays eligible, and the next run re-attempts delivery on the same row —
  an undelivered ping was never a nag.

The Telegram send is injected (``send_fn``) so the core is pure; the dismiss
button routes back through ``research_notify`` (``cp:dismiss:<id>``).
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from user_state._db import now_naive_utc, open_conn

if TYPE_CHECKING:
    from synthesis.insights import InsightRow

CLASSES: tuple[str, ...] = (
    "falsifier_breach",
    "retro_annotation",
    "intent_followup",
    "capacity_breach",
    "life_event_checkpoint",
    "profile_drift",
    # A conviction cohort graded below its bar (zero-LLM, receipts-first) —
    # research.calibration_moments; fires once per period per cohort.
    "calibration_finding",
    # B5 (2026-07-19 program overhaul) — a Worldview Tenet whose weekly
    # accountability pass found a violated owner decision; zero-LLM (reads a
    # persisted verdict). See _tenet_challenge_moments below.
    "tenet_challenge",
    # B6 (2026-07-19 program overhaul) — an LLM-drafted exit post-mortem
    # landed live on a closed position_entries row (the 18:00 sweep,
    # synthesis.exit_postmortem). Zero-LLM here too — reads the
    # drafted-and-unpinged set deterministically. See _post_mortem_moments.
    "post_mortem",
)
# B9 (2026-07-19 program overhaul, owner ruling: 1-2 durable items/day total):
# raised from 1/3 now that B1-B6 classes exist — one slot was starving every
# class behind falsifier_breach. Overflow still lands digest, never pushed.
DAILY_CAP = 2
WEEKLY_CAP = 8
MUTE_AFTER = 3
ANNOTATION_AGE_HOURS = 24
INTENT_FOLLOWUP_DAYS = 14

# B9: when moments COMPETE for the day's slots, money-at-risk speaks first and
# coaching speaks last — an explicit total order, not collection order.
# life_event_checkpoint sits with the capacity family (a changed circumstance
# can invalidate sizing today); the plan's published ordering omitted it.
# Unknown classes sort last (a future class must claim its rank explicitly).
CLASS_PRIORITY: tuple[str, ...] = (
    "falsifier_breach",
    "capacity_breach",
    "life_event_checkpoint",
    "post_mortem",
    "calibration_finding",
    "tenet_challenge",
    "retro_annotation",
    "intent_followup",
    "profile_drift",
)
_PRIORITY_RANK: dict[str, int] = {c: i for i, c in enumerate(CLASS_PRIORITY)}


@dataclass(frozen=True, slots=True)
class Moment:
    class_: str
    key: str
    ticker: str | None
    body: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class PingRow:
    """A ``coach_pings`` row, as read back for the ``cp:review`` callback (the
    Answer button on a falsifier_breach ping) — just enough to route the
    reply."""

    id: int
    class_: str
    ticker: str | None
    status: str


def _now(now: datetime | None) -> datetime:
    return (now or now_naive_utc()).replace(tzinfo=None)


def _tenet_accountability_meta(t: InsightRow) -> dict[str, object] | None:
    """The persisted ``"accountability"`` verdict on a Tenet's meta, or None."""
    acc_raw = t.meta.get("accountability")
    return cast("dict[str, object]", acc_raw) if isinstance(acc_raw, dict) else None


def _violated_ids(acc: dict[str, object]) -> list[int]:
    violated_raw = acc.get("violated")
    if not isinstance(violated_raw, list):
        return []
    return [v for v in cast("list[object]", violated_raw) if isinstance(v, int)]


def _tenet_challenge_moments(db_path: Path | str | None, *, now: datetime) -> list[Moment]:
    """Zero-LLM: read the ``"accountability"`` verdict
    ``synthesis.tenet_accountability`` persists onto each CURRENT Tenet's
    ``insight_notes.meta_json``, and emit AT MOST ONE ``tenet_challenge``
    moment for the current iso-week — the Tenet carrying a violation with the
    highest-MAGNITUDE ``est_cost_usd`` (a violation's cost is typically
    negative — a loss — so priority is by ``abs(cost)``, not the signed
    value; ties broken by most violations; a missing/None cost always loses
    to any real number)."""
    try:
        from synthesis.tenets import list_tenets
    except Exception:
        return []
    try:
        tenets = list_tenets(status="current", db_path=db_path)
    except Exception:
        return []

    iso = now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    # magnitude, n_violated, id, one_liner, as_of, raw_cost_or_None
    best: tuple[float, int, int, str, str, float | None] | None = None
    for t in tenets:
        acc = _tenet_accountability_meta(t)
        if acc is None:
            continue
        violated = _violated_ids(acc)
        if not violated:
            continue
        cost_raw = acc.get("est_cost_usd")
        cost = (
            float(cast("float | int", cost_raw))
            if isinstance(cost_raw, (int, float)) and not isinstance(cost_raw, bool)
            else None
        )
        magnitude = abs(cost) if cost is not None else -1.0
        one_liner = str(acc.get("one_liner") or "").strip()
        candidate = (magnitude, len(violated), int(t.id), one_liner, t.as_of, cost)
        if best is None or (candidate[0], candidate[1]) > (best[0], best[1]):
            best = candidate
    if best is None:
        return []

    _magnitude, n_violated, tenet_id, one_liner, as_of, cost = best
    cost_txt = f", est. -${abs(cost):,.0f}" if cost is not None else ""
    body = (
        f"{one_liner or 'A standing Tenet is under tension.'} — violated "
        f"{n_violated}x since {as_of[:10]}{cost_txt}."
    )
    return [
        Moment(
            class_="tenet_challenge",
            key=f"tenet_challenge:{tenet_id}:{week_key}",
            ticker=None,
            body=body,
            source_ref=f"tenet:{tenet_id}",
        )
    ]


def _post_mortem_moments(db_path: Path | str | None, *, now: datetime) -> list[Moment]:
    """Zero-LLM: read llm-drafted exit post-mortems that have landed live
    (``synthesis.exit_postmortem.drafted_awaiting_glance`` — closed
    ``position_entries`` rows whose three grading fields are all filled AND
    carry the ``llm_draft`` provenance note) and emit ONE ``post_mortem``
    moment per drafted entry, keyed ``post_mortem:<entry_id>``. ``now`` is
    unused (no weekly/period bucketing here, unlike ``tenet_challenge`` — a
    drafted post-mortem is a one-shot receipt, not a recurring verdict) but
    kept for signature symmetry with the other collectors in this module."""
    del now
    try:
        from synthesis.exit_postmortem import drafted_awaiting_glance
    except Exception:
        return []
    try:
        drafted = drafted_awaiting_glance(db_path)
    except Exception:
        return []
    moments: list[Moment] = []
    for entry in drafted:
        lessons_excerpt = (entry.lessons or "").strip()[:80]
        body = (
            f"Drafted post-mortem for {entry.ticker}: {entry.outcome_vs_thesis}, "
            f'"{lessons_excerpt}" — reply or revert.'
        )
        moments.append(
            Moment(
                class_="post_mortem",
                key=f"post_mortem:{entry.id}",
                ticker=entry.ticker,
                body=body,
                source_ref=f"position_entry:{entry.id}",
            )
        )
    return moments


def _tenet_still_has_violation(db_path: Path | str | None, tenet_id: int) -> bool:
    """Freshness re-verification for a ``tenet_challenge`` moment: does the
    REFERENCED Tenet still carry at least one violated decision in its
    persisted accountability meta, right now? Deliberately independent of
    the iso-week/ranking selection in :func:`_tenet_challenge_moments` (which
    depends on the collection-time ``now``) — freshness only needs to know
    the SPECIFIC referenced Tenet's condition still holds, exactly like every
    other class's freshness check (``falsifier_breach`` re-checks that one
    decision's falsifier, not the whole alert queue). Can't verify (missing
    table, tenet no longer current) → False."""
    try:
        from synthesis.tenets import list_tenets

        tenets = list_tenets(status="current", db_path=db_path)
    except Exception:
        return False
    for t in tenets:
        if t.id != tenet_id:
            continue
        acc = _tenet_accountability_meta(t)
        return acc is not None and bool(_violated_ids(acc))
    return False  # tenet not found (reverted / no longer current)


def collect_moments(
    db_path: Path | str | None = None,
    *,
    now: datetime | None = None,
    repo_root: Path | str | None = None,
    api_url: str | None = None,
) -> list[Moment]:
    """Deterministic scan for initiation-worthy moments. Read-only.

    ``repo_root`` is only needed by ``capacity_breach`` (the materialized
    weights cache lives at ``<repo_root>/data/portfolio_weights.json``); when
    omitted it is derived from ``db_path`` (``<repo_root>/data/portfolio.db``).
    ``api_url`` is passed straight through to the tracker reads the
    ``capacity_breach`` / ``profile_drift`` collectors make (``None`` uses
    their own env-var / localhost default)."""
    stamp = _now(now)
    moments: list[Moment] = []
    conn = open_conn(db_path)
    try:
        # falsifier_breach — an owner decision's own condition fired an alert
        try:
            rows = conn.execute(
                """
                SELECT a.id AS alert_id, d.id AS decision_id, d.ticker, d.falsifier
                FROM alerts a
                JOIN decisions d
                  ON d.id = CAST(json_extract(a.evidence_json, '$.decision_id') AS INTEGER)
                WHERE a.trigger_kind = 'decision_condition'
                  AND a.dismissed_at IS NULL
                  AND d.decided_by = 'owner'
                ORDER BY a.id
                """
            ).fetchall()
            for r in rows:
                falsifier = str(r[3] or "")[:200]
                moments.append(
                    Moment(
                        class_="falsifier_breach",
                        key=f"alert:{int(r[0])}",
                        ticker=str(r[2] or "") or None,
                        body=(
                            f'Your falsifier on {r[2]} tripped. You wrote: "{falsifier}". '
                            f"Keep the verdict with the framework, not the feeling — "
                            f"what's the call? (/review {r[2]})"
                        ),
                        source_ref=f"decision:{int(r[1])}",
                    )
                )
        except Exception:
            pass  # pre-0063/0086 schema — no alerts to scan

        # retro_annotation — pledge / retro-net stubs still missing their words
        cutoff = (stamp - timedelta(hours=ANNOTATION_AGE_HOURS)).isoformat()
        rows = conn.execute(
            """
            SELECT id, ticker, recommendation_kind, size_usd FROM decisions
            WHERE decided_by = 'owner'
              AND (conviction IS NULL OR falsifier IS NULL)
              AND (user_notes LIKE '%pledge:note:%' OR user_notes LIKE '%retro-net:%')
              AND created_at <= ?
            ORDER BY id
            """,
            (cutoff,),
        ).fetchall()
        for r in rows:
            size = f" (~${float(r[3]):,.0f})" if r[3] else ""
            moments.append(
                Moment(
                    class_="retro_annotation",
                    key=f"annot:{int(r[0])}",
                    ticker=str(r[1] or "") or None,
                    body=(
                        f"Decision #{int(r[0])} — {str(r[2]).upper()} {r[1]}{size} — is "
                        "still missing your conviction + falsifier. One line back "
                        "completes the record (it's write-once after that)."
                    ),
                    source_ref=f"decision:{int(r[0])}",
                )
            )

        # intent_followup — standing intents still open after a fortnight
        bucket = int(stamp.timestamp()) // (INTENT_FOLLOWUP_DAYS * 86400)
        opened_before = (stamp - timedelta(days=INTENT_FOLLOWUP_DAYS)).isoformat()
        rows = conn.execute(
            """
            SELECT id, ticker, body FROM analyst_notes
            WHERE kind = 'intent' AND status = 'open' AND created_at <= ?
            ORDER BY id
            """,
            (opened_before,),
        ).fetchall()
        for r in rows:
            moments.append(
                Moment(
                    class_="intent_followup",
                    key=f"intent:{int(r[0])}:{bucket}",
                    ticker=str(r[1] or "") or None,
                    body=(
                        f'Standing intent still open: "{str(r[2])[:140]}" — still live? '
                        "Reply here with a close reason, or resolve it from the Ledger tab."
                    ),
                    source_ref=f"note:{int(r[0])}",
                )
            )

        # tenet-2 Phase 3 — capacity_breach / life_event_checkpoint /
        # profile_drift. Each collector degrades independently (missing
        # owner_profile_facts table, offline tracker, unresolved repo_root) —
        # a failure here never breaks the three moment classes above.
        try:
            from research.capacity_moments import (
                collect_capacity_breach_moments,
                collect_life_event_moments,
                collect_profile_drift_moments,
                repo_root_from_db_path,
            )

            root = Path(repo_root) if repo_root is not None else repo_root_from_db_path(db_path)
            moments.extend(
                collect_capacity_breach_moments(
                    conn, repo_root=root, api_url=api_url, now=stamp, db_path=db_path
                )
            )
            moments.extend(collect_life_event_moments(conn, now=stamp))
            moments.extend(
                collect_profile_drift_moments(conn, repo_root=root, api_url=api_url, now=stamp)
            )
        except Exception:
            pass  # pre-0159 schema — no owner_profile_facts to scan

        # calibration_finding (2026-07-19 review) — a conviction cohort graded
        # below its bar, computed straight off the decisions ledger with no
        # LLM. Same independent-degrade contract as the tenet-2 collectors.
        try:
            from research.calibration_moments import collect_calibration_finding_moments

            if db_path is not None:
                moments.extend(
                    m
                    for m in collect_calibration_finding_moments(db_path, now=stamp)
                    if isinstance(m, Moment)
                )
        except Exception:
            pass  # missing decisions substrate — nothing to confront

        # tenet_challenge (B5) — same independent-degrade contract as the
        # collectors above; a missing/malformed accountability meta never
        # breaks the other moment classes.
        with contextlib.suppress(Exception):
            moments.extend(_tenet_challenge_moments(db_path, now=stamp))

        # post_mortem (B6) — same independent-degrade contract; a missing or
        # malformed synthesis.exit_postmortem module never breaks the
        # collectors above.
        with contextlib.suppress(Exception):
            moments.extend(_post_mortem_moments(db_path, now=stamp))
    finally:
        conn.close()
    return moments


def _profile_fact_still_affirmed(conn: sqlite3.Connection, fact_id: int) -> bool:
    """tenet-2 Phase 3 freshness-gate extension: ANY ping grounded in an
    ``owner_profile_facts`` row must re-verify that row is still
    ``status='affirmed'`` and still the latest (not superseded) — the owner
    may have re-affirmed, updated (superseding it), or dropped it since the
    moment was collected. Can't verify (missing table, missing row) → False."""
    try:
        from owner_profile.store import get_fact

        row = get_fact(conn, fact_id)
    except Exception:
        return False
    return row is not None and row.is_latest and row.status == "affirmed"


def freshness_ok(
    moment: Moment,
    db_path: Path | str | None = None,
    *,
    repo_root: Path | str | None = None,
    api_url: str | None = None,
) -> bool:
    """The staleness callout as a gate: verify the moment's subject is still
    live RIGHT NOW. Can't verify → False (don't ping). ``repo_root`` /
    ``api_url`` only matter for ``capacity_breach`` (see module docstring)."""
    conn = open_conn(db_path)
    try:
        kind, _, ref_id = moment.source_ref.partition(":")
        if moment.class_ == "falsifier_breach":
            row = conn.execute(
                """
                SELECT d.falsifier FROM decisions d
                WHERE d.id = ? AND d.decided_by = 'owner' AND d.falsifier IS NOT NULL
                  AND d.falsifier NOT LIKE '%(inferred)%'
                  AND EXISTS (SELECT 1 FROM tracked_companies t
                              WHERE t.ticker = d.ticker AND t.list_type = 'portfolio')
                """,
                (int(ref_id),),
            ).fetchone()
            return row is not None
        if moment.class_ == "retro_annotation":
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE id = ? AND decided_by = 'owner' "
                "AND (conviction IS NULL OR falsifier IS NULL)",
                (int(ref_id),),
            ).fetchone()
            return row is not None
        if moment.class_ == "intent_followup":
            row = conn.execute(
                "SELECT 1 FROM analyst_notes WHERE id = ? AND kind = 'intent' AND status = 'open'",
                (int(ref_id),),
            ).fetchone()
            return row is not None
        if moment.class_ in ("life_event_checkpoint", "profile_drift"):
            if kind != "fact":
                return False
            return _profile_fact_still_affirmed(conn, int(ref_id))
        if moment.class_ == "capacity_breach":
            if kind != "alert":
                return False
            from research.capacity_moments import (
                capacity_breach_still_active,
                repo_root_from_db_path,
            )

            root = Path(repo_root) if repo_root is not None else repo_root_from_db_path(db_path)
            return capacity_breach_still_active(
                conn, int(ref_id), repo_root=root, api_url=api_url, db_path=db_path
            )
        if moment.class_ == "calibration_finding":
            # Still-true check = recompute the deterministic finding and require
            # the SAME key to re-emerge — grades landing since collection can
            # heal the cohort, and a healed cohort must not be confronted.
            if db_path is None:
                return False
            from research.calibration_moments import collect_calibration_finding_moments

            fresh = collect_calibration_finding_moments(db_path, now=now_naive_utc())
            return any(isinstance(m, Moment) and m.key == moment.key for m in fresh)
        if moment.class_ == "tenet_challenge":
            if kind != "tenet":
                return False
            return _tenet_still_has_violation(db_path, int(ref_id))
        if moment.class_ == "post_mortem":
            if kind != "position_entry":
                return False
            from synthesis.exit_postmortem import is_awaiting_glance

            return is_awaiting_glance(int(ref_id), db_path=db_path)
        return False
    except Exception:
        return False
    finally:
        conn.close()


def _sent_counts(conn: sqlite3.Connection, stamp: datetime) -> tuple[int, int]:
    day_cut = (stamp - timedelta(days=1)).isoformat()
    week_cut = (stamp - timedelta(days=7)).isoformat()
    day = conn.execute(
        "SELECT COUNT(*) FROM coach_pings WHERE status = 'sent' AND created_at >= ?",
        (day_cut,),
    ).fetchone()[0]
    week = conn.execute(
        "SELECT COUNT(*) FROM coach_pings WHERE status = 'sent' AND created_at >= ?",
        (week_cut,),
    ).fetchone()[0]
    return int(day), int(week)


def run_governor(
    db_path: Path | str | None = None,
    *,
    send_fn: Callable[[int, Moment], bool] | None = None,
    now: datetime | None = None,
    repo_root: Path | str | None = None,
    api_url: str | None = None,
) -> dict[str, int]:
    """One governed pass: collect → freshness-gate → cap → send/digest.

    ``send_fn(ping_id, moment) -> bool`` performs the push (Telegram). No
    ``send_fn`` at all (dry-run / no bot configured) downgrades the ping to
    the quiet digest — that IS the delivery surface then. A ``send_fn`` that
    returns False (an ATTEMPTED push failed — Telegram down, bad token) marks
    the row ``send_failed``: the one status the anti-nag once-forever rule
    exempts, so the next governor run re-attempts delivery instead of
    silently dropping a falsifier_breach the owner never saw.
    ``repo_root`` / ``api_url`` (tenet-2 Phase 3) thread through to
    ``collect_moments`` / ``freshness_ok`` for the ``capacity_breach`` class
    only; every other class ignores them. Deterministic, zero LLM."""
    stamp = _now(now)
    tally = {
        "sent": 0,
        "digest": 0,
        "skipped_muted": 0,
        "skipped_stale": 0,
        "send_failed": 0,
        "seen": 0,
    }
    moments = collect_moments(db_path, now=stamp, repo_root=repo_root, api_url=api_url)
    # B9: competing moments contend for DAILY_CAP slots in priority order
    # (money-at-risk first, coaching last) — stable within a class, so each
    # collector's own ordering survives.
    moments.sort(key=lambda m: _PRIORITY_RANK.get(m.class_, len(CLASS_PRIORITY)))
    conn = open_conn(db_path)
    try:
        muted = {str(r[0]) for r in conn.execute("SELECT class_ FROM coach_mutes").fetchall()}
        for moment in moments:
            prior = conn.execute(
                "SELECT id, status FROM coach_pings WHERE class_ = ? AND key = ?",
                (moment.class_, moment.key),
            ).fetchone()
            if prior is not None and str(prior[1]) != "send_failed":
                continue  # anti-nag: a moment is considered exactly once, forever
            tally["seen"] += 1
            iso = stamp.isoformat()
            if moment.class_ in muted:
                status = "skipped_muted"
            elif not freshness_ok(moment, db_path, repo_root=repo_root, api_url=api_url):
                status = "skipped_stale"
            else:
                day, week = _sent_counts(conn, stamp)
                status = "digest" if (day >= DAILY_CAP or week >= WEEKLY_CAP) else "sent"
            if prior is None:
                cur = conn.execute(
                    """
                    INSERT INTO coach_pings
                        (class_, key, ticker, body, status, source_ref, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        moment.class_,
                        moment.key,
                        moment.ticker,
                        moment.body,
                        status,
                        moment.source_ref,
                        iso,
                        iso,
                    ),
                )
                ping_id = int(cur.lastrowid or 0)
            else:
                # A prior run's push died mid-flight — re-run the gates on the
                # SAME row (UNIQUE class+key), then retry the delivery below.
                ping_id = int(prior[0])
                conn.execute(
                    "UPDATE coach_pings SET status = ?, updated_at = ? WHERE id = ?",
                    (status, iso, ping_id),
                )
            conn.commit()
            if status == "sent":
                if send_fn is None:
                    # No push channel — not a send failure; park in the digest.
                    delivered, fallback = False, "digest"
                else:
                    delivered, fallback = bool(send_fn(ping_id, moment)), "send_failed"
                if not delivered:
                    conn.execute(
                        "UPDATE coach_pings SET status = ?, updated_at = ? WHERE id = ?",
                        (fallback, iso, ping_id),
                    )
                    conn.commit()
                    status = fallback
            tally[status] += 1
    finally:
        conn.close()
    return tally


def record_dismissal(ping_id: int, *, db_path: Path | str | None = None) -> tuple[bool, str | None]:
    """Owner dismissed a ping. Returns (recorded, muted_class|None): MUTE_AFTER
    consecutive dismissals of a class silence it until cleared."""
    stamp = now_naive_utc().isoformat()
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT class_ FROM coach_pings WHERE id = ? AND status IN ('sent','digest')",
            (ping_id,),
        ).fetchone()
        if row is None:
            return False, None
        class_ = str(row[0])
        conn.execute(
            "UPDATE coach_pings SET status = 'dismissed', updated_at = ? WHERE id = ?",
            (stamp, ping_id),
        )
        recent = conn.execute(
            """
            SELECT status FROM coach_pings
            WHERE class_ = ? AND status IN ('dismissed', 'acted')
            ORDER BY updated_at DESC, id DESC LIMIT ?
            """,
            (class_, MUTE_AFTER),
        ).fetchall()
        muted: str | None = None
        if len(recent) >= MUTE_AFTER and all(str(r[0]) == "dismissed" for r in recent):
            conn.execute(
                "INSERT OR IGNORE INTO coach_mutes (class_, muted_at, reason) VALUES (?, ?, ?)",
                (class_, stamp, f"{MUTE_AFTER} consecutive dismissals"),
            )
            muted = class_
        conn.commit()
        return True, muted
    finally:
        conn.close()


def unmute(class_: str, *, db_path: Path | str | None = None) -> bool:
    conn = open_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM coach_mutes WHERE class_ = ?", (class_,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def record_ping_message_id(
    ping_id: int, message_id: int, *, db_path: Path | str | None = None
) -> bool:
    """Best-effort: remember which Telegram message carried this ping (B3), so a
    later free-text REPLY can route back to the finding (``capture.coach_reply``)
    the same way a note-card reply routes via
    ``context_json['telegram_message_id']`` (``poller._stash_card_mid``) —
    ``coach_pings`` has no JSON column, hence a real ``telegram_message_id``
    column (migration 0188). Swallows ``sqlite3.OperationalError`` so an
    un-migrated DB (column missing) never breaks a send; returns whether the
    write happened."""
    stamp = now_naive_utc().isoformat()
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE coach_pings SET telegram_message_id = ?, updated_at = ? WHERE id = ?",
            (message_id, stamp, ping_id),
        )
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False  # pre-0188 schema — no telegram_message_id column yet
    finally:
        conn.close()


def mark_ping_acted(ping_id: int, *, db_path: Path | str | None = None) -> bool:
    """Owner's reply resolved this ping (acknowledge / annotate_decision outcome
    in ``capture.coach_reply``) — flip it to ``acted`` from ``sent``/``digest``
    only, mirroring ``record_dismissal``'s status-guard shape. Returns whether a
    row was updated."""
    stamp = now_naive_utc().isoformat()
    conn = open_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE coach_pings SET status = 'acted', updated_at = ? "
            "WHERE id = ? AND status IN ('sent', 'digest')",
            (stamp, ping_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_ping(ping_id: int, db_path: Path | str | None = None) -> PingRow | None:
    """One ``coach_pings`` row by id, or None. Backs the ``cp:review`` callback
    (the Answer button on a falsifier_breach ping) — it needs the ticker to
    route the reply, without pulling in the full ``Moment`` collection path."""
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, class_, ticker, status FROM coach_pings WHERE id = ?",
            (ping_id,),
        ).fetchone()
        if row is None:
            return None
        return PingRow(id=int(row[0]), class_=str(row[1]), ticker=row[2], status=str(row[3]))
    finally:
        conn.close()


def digest_pings(
    db_path: Path | str | None = None, *, limit: int = 10
) -> list[tuple[int, str, str]]:
    """(id, class, body) rows waiting in the quiet digest — the overflow lane."""
    conn = open_conn(db_path)
    try:
        return [
            (int(r[0]), str(r[1]), str(r[2]))
            for r in conn.execute(
                "SELECT id, class_, body FROM coach_pings WHERE status = 'digest' "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        ]
    finally:
        conn.close()
