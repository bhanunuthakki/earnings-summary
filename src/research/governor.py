"""Collect governed research findings into the dashboard digest.

Freshness, class priority, mutes, and linked thesis attention govern admission.
Each class/key is retained once. Historical failed deliveries can enter the
quiet digest on the same row after all gates pass. Expired historical sending
leases become retryable; active leases and terminal owner actions stay intact.
Collectors also maintain the existing capacity alerts. No Telegram or LLM calls
are made here. The caller holds the notification-delivery job lane.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from decision_calibration import build_calibration
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
MUTE_AFTER = 3
# Preserve the recovery boundary for historical delivery claims.
SEND_LEASE = timedelta(minutes=15)
ANNOTATION_AGE_HOURS = 24
INTENT_FOLLOWUP_DAYS = 14

# Keep money-at-risk findings first, with a stable order within each class.
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
    """One stored finding and its current state, including historical pings."""

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
            float(cost_raw)
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
    if db_path is None:
        return []
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


# ---------------------------------------------------------------------------
# calibration_finding (2026-07-19 program review) — folded in from the former
# ``research.calibration_moments``. The owner's own graded ledger contained a
# textbook finding — high-conviction calls at 5 correct / 8 wrong (33% hit-rate
# vs the ~75% the label implies) — that no surface ever delivered: the July
# scorecard's LLM leg failed transiently in a quota-dead window and persisted
# "no biases met the grounding bar", while the deterministic substrate that
# could have said it needed no LLM at all.
#
# This collector reads ``decision_calibration.build_calibration`` directly and
# emits AT MOST ONE moment per period when a conviction cohort crosses a
# deterministic bar: hit-rate below ``_HIT_RATE_BAR`` with at least
# ``_MIN_GRADED`` graded calls (the Wilson floor discipline — a thin cohort
# can't carry an accusation). The moment key is
# ``calibration_finding:<YYYY-MM>:<cohort>``, so the durable coach_pings ledger
# (UNIQUE class+key) makes it fire once per period per cohort — a persistent
# pattern re-surfaces monthly until it heals, never daily.
#
# Receipts-first body (the owner's action-UX bar): counts, rate, and what the
# label implies — never a vague "consider reviewing your process".
# ---------------------------------------------------------------------------

# A high/medium-conviction cohort graded below this rate is miscalibrated
# enough to confront; 'low'/'unstated' cohorts are excluded (a low-conviction
# miss is not a calibration failure). Bars are deliberately coarse — this is a
# confrontation trigger, not a statistics lesson.
_HIT_RATE_BAR = 0.50
_MIN_GRADED = 10
_CONFRONTABLE = ("high", "medium")

# What the label is supposed to mean, for the receipt line.
_IMPLIED = {"high": 0.75, "medium": 0.60}


def collect_calibration_finding_moments(
    db_path: Path | str,
    *,
    now: datetime,
) -> list[object]:
    """≤1 Moment when a confrontable conviction cohort is graded below the bar.

    Returns ``object``-typed Moments (mirrors the other collectors' honest
    seams); callers filter with ``isinstance(m, Moment)``. Degrades to [] on
    any substrate problem; the governor's other classes must never be broken
    by this one.
    """
    try:
        stats = build_calibration(db_path=db_path)
    except Exception:
        return []
    if stats is None:
        return []

    period = now.strftime("%Y-%m")
    worst: tuple[float, object] | None = None
    for bucket in stats.by_conviction:
        if bucket.conviction not in _CONFRONTABLE:
            continue
        if bucket.graded < _MIN_GRADED or bucket.hit_rate is None:
            continue
        if bucket.hit_rate >= _HIT_RATE_BAR:
            continue
        if worst is None or bucket.hit_rate < worst[0]:
            worst = (bucket.hit_rate, bucket)
    if worst is None:
        return []

    b = worst[1]
    implied = _IMPLIED.get(str(b.conviction), _HIT_RATE_BAR)
    band = (
        f" (95% CI {b.wilson_low:.0%}-{b.wilson_high:.0%})"
        if b.wilson_low is not None and b.wilson_high is not None
        else ""
    )
    body = (
        f"Calibration check: your {b.conviction}-conviction calls are graded "
        f"{b.correct} correct / {b.wrong} wrong / {b.mixed} mixed — a {b.hit_rate:.0%} "
        f"hit-rate{band} against the ~{implied:.0%} the label implies. "
        f"Worth sizing the next {b.conviction}-conviction add as if it were "
        f"one notch lower until the rate recovers."
    )
    return [
        Moment(
            class_="calibration_finding",
            key=f"calibration_finding:{period}:{b.conviction}",
            ticker=None,
            body=body,
            source_ref=f"calibration:{period}",
        )
    ]


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
            fresh = collect_calibration_finding_moments(db_path, now=now_naive_utc())
            return any(isinstance(m, Moment) and m.key == moment.key for m in fresh)
        if moment.class_ == "tenet_challenge":
            if kind != "tenet":
                return False
            return _tenet_still_has_violation(db_path, int(ref_id))
        if moment.class_ == "post_mortem":
            if kind != "position_entry":
                return False
            if db_path is None:
                return False
            from synthesis.exit_postmortem import is_awaiting_glance

            return is_awaiting_glance(int(ref_id), db_path=db_path)
        return False
    except Exception:
        return False
    finally:
        conn.close()


def run_governor(
    db_path: Path | str | None = None,
    *,
    now: datetime | None = None,
    repo_root: Path | str | None = None,
    api_url: str | None = None,
) -> dict[str, int]:
    """Collect, gate, and retain findings in the quiet digest.

    Callers hold the notification-delivery JobLock for the complete pass.
    Historical sending leases retain their recovery semantics. Retry writes
    compare the observed status and timestamp, so owner actions win races.
    Legacy delivery tally keys remain zero for result-shape compatibility.
    """
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
    # Preserve class priority and each collector's stable ordering.
    moments.sort(key=lambda m: _PRIORITY_RANK.get(m.class_, len(CLASS_PRIORITY)))
    conn = open_conn(db_path)
    try:
        muted = {str(r[0]) for r in conn.execute("SELECT class_ FROM coach_mutes").fetchall()}
        coach_ping_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(coach_pings)").fetchall()
        }
        has_episode_link = "thesis_evaluation_episode_id" in coach_ping_columns
        attention_columns: set[str] = set()
        if has_episode_link:
            attention_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(thesis_evaluation_episodes)").fetchall()
            }
        required_attention_columns = {
            "episode_id",
            "ticker",
            "attention_state",
            "acknowledged_at",
            "acknowledgement_note",
            "next_review_at",
            "acted_on_decision_id",
            "superseded_by_episode_id",
            "attention_updated_at",
        }
        has_attention_schema = required_attention_columns.issubset(attention_columns)
        # An expired claim is an unconfirmed attempt whether or not its moment
        # is still collected: make it retryable instead of stuck in flight.
        conn.execute(
            "UPDATE coach_pings SET status = 'send_failed', updated_at = ? "
            "WHERE status = 'sending' AND updated_at <= ?",
            (stamp.isoformat(), (stamp - SEND_LEASE).isoformat()),
        )
        conn.commit()
        for moment in moments:
            if has_episode_link:
                prior = conn.execute(
                    "SELECT id,status,updated_at,thesis_evaluation_episode_id FROM coach_pings "
                    "WHERE class_ = ? AND key = ?",
                    (moment.class_, moment.key),
                ).fetchone()
            else:
                prior = conn.execute(
                    "SELECT id,status,updated_at FROM coach_pings WHERE class_ = ? AND key = ?",
                    (moment.class_, moment.key),
                ).fetchone()
            prior_id = None if prior is None else int(prior[0])
            linked_episode_id = None if prior is None or not has_episode_link else prior[3]
            if linked_episode_id is not None and prior_id is not None:
                if not has_attention_schema:
                    missing = sorted(required_attention_columns - attention_columns)
                    raise RuntimeError(
                        "incomplete thesis episode attention schema for linked coach ping; "
                        f"missing columns: {', '.join(missing)}"
                    )
                from compute.thesis_episode_attention import get_attention

                episode_attention = get_attention(
                    conn,
                    str(linked_episode_id),
                    now=stamp.replace(tzinfo=UTC),
                )
                if not episode_attention.actionable:
                    conn.execute(
                        "UPDATE coach_pings SET status=?,updated_at=? "
                        "WHERE id=? AND status='send_failed'",
                        ("acted", stamp.isoformat(), prior_id),
                    )
                    conn.commit()
                    continue
            if prior is not None and str(prior[1]) != "send_failed":
                continue  # anti-nag: a moment is considered exactly once, forever
            tally["seen"] += 1
            iso = stamp.isoformat()
            if moment.class_ in muted:
                status = "skipped_muted"
            elif not freshness_ok(moment, db_path, repo_root=repo_root, api_url=api_url):
                status = "skipped_stale"
            else:
                status = "digest"
            if prior is None:
                conn.execute(
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
            else:
                # Recheck historical failed deliveries on the same row. An
                # owner action or sibling run that changed the observed row wins.
                ping_id = int(prior[0])
                claimed = conn.execute(
                    "UPDATE coach_pings SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status = ? AND updated_at = ?",
                    (status, iso, ping_id, str(prior[1]), str(prior[2])),
                )
                if claimed.rowcount != 1:
                    conn.commit()
                    continue
            conn.commit()
            if status in tally:
                tally[status] += 1
    finally:
        conn.close()
    return tally


def record_dismissal(ping_id: int, *, db_path: Path | str | None = None) -> tuple[bool, str | None]:
    """Owner dismissed a ping. Returns (recorded, muted_class|None): MUTE_AFTER
    consecutive dismissals of a class silence it until cleared.

    Legacy ``routed_to_brief`` rows remain dismissable so removing the retired
    weekly-brief product does not strand historical owner controls.
    """

    stamp = now_naive_utc().isoformat()
    conn = open_conn(db_path)
    try:
        try:
            linked = conn.execute(
                "SELECT thesis_evaluation_episode_id FROM coach_pings WHERE id=?",
                (ping_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            linked = None
        if linked is not None and linked[0] is not None:
            from compute.thesis_episode_attention import acknowledge_episode

            acknowledge_episode(
                conn,
                str(linked[0]),
                acknowledged_at=datetime.fromisoformat(stamp).replace(tzinfo=UTC),
                note="Dismissed from the coach prompt.",
            )
            conn.commit()
            return True, None
        row = conn.execute(
            "SELECT class_ FROM coach_pings WHERE id = ? "
            "AND status IN ('sending','sent','digest','routed_to_brief','acted')",
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


def mark_ping_acted(ping_id: int, *, db_path: Path | str | None = None) -> bool:
    """A shared attention action resolved this finding — flip it to ``acted`` from
    ``sending``/``sent``/``digest``. The status guard mirrors
    ``record_dismissal``. Returns whether a row was updated."""
    stamp = now_naive_utc().isoformat()
    conn = open_conn(db_path)
    try:
        try:
            linked = conn.execute(
                "SELECT thesis_evaluation_episode_id FROM coach_pings WHERE id=?",
                (ping_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            linked = None
        if linked is not None and linked[0] is not None:
            from compute.thesis_episode_attention import acknowledge_episode

            acknowledge_episode(
                conn,
                str(linked[0]),
                acknowledged_at=datetime.fromisoformat(stamp).replace(tzinfo=UTC),
                note="Acknowledged from the coach prompt.",
            )
            conn.commit()
            return True
        cur = conn.execute(
            "UPDATE coach_pings SET status = 'acted', updated_at = ? "
            "WHERE id = ? AND status IN ('sending', 'sent', 'digest')",
            (stamp, ping_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_ping(ping_id: int, db_path: Path | str | None = None) -> PingRow | None:
    """One stored finding by ID, or None, without collecting new findings."""
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
