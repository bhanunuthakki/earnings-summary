"""KPI inflection sensor — full implementation (PR-N11).

``Cadence.DAILY`` sensor over ``kpi_facts`` for the KPIs the user has flagged
as load-bearing in ``user_kpi_registry``. For each registered KPI it loads the
recent quarterly series, runs the time-series layer's deterministic changepoint
detector (``timeseries.detect_inflection``), and — when the series has *just*
changed regime — emits a :class:`TriggerCandidate`.

CRITICAL contract (the inverse of ``earnings_tone``)
----------------------------------------------------
Earnings-tone's analysis *is* the LLM call: no LLM, no alert. KPI inflection is
the opposite. The inflection signal is **deterministic** — computed in code by
the time-series layer — so the alert MUST fire on a real inflection even when
the LLM is down. ``build_alert`` therefore composes a factual memo from the
evidence first; the optional 1-2 sentence "why it matters" line is a
best-effort enrichment that degrades silently to no-context on any failure
(LLM error, budget cap, parse miss). The alert fires regardless.

Path bridging
-------------
``scan`` is handed a ``sqlite3.Connection`` (the driver opens one per
(ticker, trigger) pair), but the registry CRUD and the time-series loaders take
a ``db_path`` rather than a connection. We resolve the file backing the
connection via ``PRAGMA database_list`` so both reads hit the *exact* DB the
driver opened — important under ``tmp_path`` test DBs where ``db.DB_PATH`` and
the handed-in connection could otherwise diverge. ``build_alert`` is not handed
a connection at all, so — like ``earnings_tone`` — it resolves ``db.DB_PATH``
for the artifact-store cache and the repo root for the thesis anchor.

Failure mode: ``scan`` is best-effort (missing registry / kpi tables, empty or
too-short series → ``[]``, never raise). ``build_alert`` never raises on LLM
failure — the deterministic memo is the contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from alerts.store import compute_signature_sha
from llm.anchors import load_thesis_anchor
from llm_artifact_store import (
    UpsertRequest,
    compute_input_sha256,
    read_current,
    upsert,
)
from llm_client import call_llm
from timeseries import (
    Observation,
    detect_inflection,
    load_kpi_series,
    rolling_zscore_anomalies,
)
from triggers.base import (
    AlertDraft,
    Cadence,
    QueuedActionDraft,
    ThesisAnchor,
    TriggerCandidate,
    UserStateContext,
)
from user_state.registry import UserKpiRegistryRow, list_kpis

log = logging.getLogger(__name__)

# Owner of the registry rows + alerts. Single-user today; mirrors the default
# baked into the user_state CRUD and the morning driver.
_USER_ID = "bhanu"

# Series window fed to the detector. detect_inflection needs >= 8 observations;
# we trim to the most recent 12 quarters so the changepoint search reflects the
# current regime rather than ancient history.
_LOOKBACK_QUARTERS = 12
_MIN_SERIES_LEN = 8

# A detected changepoint only counts as a *recent* inflection when its index
# falls within the last N quarters. detect_inflection places the changepoint at
# the first period of the post-regime and (with ruptures' min_size=3) can flag
# it no later than index n-3, so a window of 4 catches the most-recent-possible
# inflection regardless of which detector backend is installed.
_RECENT_INFLECTION_WINDOW = 4

# Floor on the detector's sd-normalized mean-shift magnitude — suppresses
# trivial regime "changes" that are really noise (detect_inflection already
# soft-suppresses < 0.5 when slopes agree; this is a hard guard on top).
_MIN_INFLECTION_MAGNITUDE = 0.5

# Trailing window for the latest-point z-score, and how many quarters of the
# tail to snapshot into evidence for the dashboard's evidence drawer.
_ZSCORE_WINDOW = 8
_SERIES_SNAPSHOT_LEN = 8

# |z| bar at which an *un-thresholded* KPI's inflection is significant enough
# to fire on its own. A mild inflection on a tracked-but-unthresholded KPI is
# noise and stays silent.
_SIGNIFICANT_ZSCORE = 2.0

# Registry threshold_direction vocabulary (migration 0060).
_THRESHOLD_BELOW = "below"
_THRESHOLD_ABOVE = "above"

_INFLECTION_UP = "up"
_INFLECTION_DOWN = "down"

# Units that carry no display suffix (the value is already a bare number).
_UNITLESS_UNITS: frozenset[str] = frozenset({"", "actual", "number", "count"})
# Units rendered as a trailing '%' with no separating space.
_PERCENT_UNITS: frozenset[str] = frozenset({"%", "percent", "pct", "percentage"})

# queued_actions.action_kind vocabulary (see triggers.base.QueuedActionDraft).
_ACTION_EARNINGS_PREP = "earnings_prep_append"
_ACTION_THESIS_UPDATE = "thesis_update"
_ACTION_SIZING_UPDATE = "sizing_update"

# Artifact-store purpose for the optional LLM context line. Must match the
# entry in llm_artifact_store.FACT_DEPENDENT_PURPOSES so a fact-side
# restatement marks the cached context dirty via the existing chain.
_ARTIFACT_PURPOSE = "kpi_inflection_context"
# Prompt-version key for the cache. Bump when the context prompt body changes
# materially.
_PROMPT_VERSION = "v1"


# --------------------------------------------------------------------------
# DB path / registry / unit helpers
# --------------------------------------------------------------------------


def _conn_db_path(conn: sqlite3.Connection) -> Path | None:
    """File path backing the 'main' schema of an open connection.

    scan() is handed a connection, but the registry CRUD and time-series
    loaders take a db_path; resolving it here lets both reach the *same* DB
    the driver opened. Returns None for an in-memory / unbacked connection
    (empty file string) or on any sqlite error.
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        # (seq, name, file) — 'main' is the primary attached DB.
        if len(row) >= 3 and row[1] == "main":
            file_path = row[2]
            if isinstance(file_path, str) and file_path:
                return Path(file_path)
    return None


def _load_registered_kpis(ticker: str, db_path: Path) -> list[UserKpiRegistryRow]:
    """Registry rows for (user, ticker). Best-effort: a missing DB /
    user_kpi_registry table yields ``[]`` rather than raising, so a fresh repo
    simply produces no candidates."""
    try:
        return list_kpis(user_id=_USER_ID, ticker=ticker, db_path=db_path)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        log.debug(
            {
                "event": "kpi_inflection_registry_unavailable",
                "ticker": ticker,
                "error": str(exc),
            }
        )
        return []


def _load_kpi_unit(
    conn: sqlite3.Connection, ticker: str, kpi_name: str
) -> str | None:
    """Display unit for the KPI's most-recent fact, or None.

    ``load_kpi_series`` intentionally returns only (period_end, value); the
    unit lives on ``kpi_facts``. We read it off the latest row via the same
    connection scan() already holds. Best-effort: any sqlite error → None.
    """
    try:
        row = conn.execute(
            "SELECT kf.unit FROM kpi_facts kf "
            "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
            "WHERE kf.ticker = ? AND kd.name = ? "
            "ORDER BY kf.period_end DESC LIMIT 1",
            (ticker.upper(), kpi_name),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


# --------------------------------------------------------------------------
# Pure formatting / math helpers
# --------------------------------------------------------------------------


def _format_value(value: float, unit: str | None) -> str:
    """Render a numeric value with its unit suffix. '%' attaches with no space;
    other word units attach with a leading space; unitless / sentinel units
    render bare. ``%g`` trims trailing zeros (16.40 → 16.4, 17.0 → 17)."""
    num = f"{value:g}"
    if unit is None:
        return num
    u = unit.strip()
    lowered = u.lower()
    if lowered in _UNITLESS_UNITS:
        return num
    if lowered in _PERCENT_UNITS:
        return f"{num}%"
    return f"{num} {u}"


def _crosses_threshold(direction: str, threshold: float, value: float) -> bool:
    """True when ``value`` breaches a registered threshold in the stated
    direction. Unknown direction strings never cross — a malformed registry
    row shouldn't fire an alert."""
    if direction == _THRESHOLD_BELOW:
        return value < threshold
    if direction == _THRESHOLD_ABOVE:
        return value > threshold
    return False


def _latest_zscore(series: list[Observation]) -> float | None:
    """Z-score of the most recent observation against its trailing window.

    Reuses ``rolling_zscore_anomalies`` (threshold=0 returns every computable
    point) and reads the entry matching the latest period. Returns None when
    the series is too short for a window or the trailing window has zero
    variance (a perfectly flat run — z-score undefined).
    """
    window = min(_ZSCORE_WINDOW, len(series) - 1)
    if window < 4:
        return None
    latest_period = series[-1].period_end.date().isoformat()
    for anomaly in rolling_zscore_anomalies(series, window=window, threshold=0.0):
        if anomaly.get("period_end") != latest_period:
            continue
        z = anomaly.get("zscore")
        if isinstance(z, (int, float)) and not isinstance(z, bool):
            return float(z)
    return None


def _registered_threshold(
    registered_kpis: list[dict[str, object]], kpi_name: str
) -> tuple[str | None, float | None]:
    """(threshold_direction, threshold_value) for the matching registry row,
    or (None, None) when the KPI isn't registered or carries no threshold.

    ``registered_kpis`` are plain dicts (the driver renders each
    ``UserKpiRegistryRow`` via ``dataclasses.asdict`` before composing
    ``UserStateContext``), so the keys are the dataclass field names.
    """
    for row in registered_kpis:
        if row.get("kpi_name") != kpi_name:
            continue
        direction = row.get("threshold_direction")
        value = row.get("threshold_value")
        if (
            isinstance(direction, str)
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            return direction, float(value)
        return None, None
    return None, None


def _factual_memo(ticker: str, evidence: dict[str, object]) -> str:
    """Deterministic one-line memo built entirely from numeric evidence.

    This is the contract: it is composed with no LLM involvement so the alert
    has a body even when the enrichment call fails.
    """
    kpi_name = str(evidence["kpi_name"])
    direction = str(evidence["inflection_direction"])
    unit = evidence.get("unit")
    unit_str = unit if isinstance(unit, str) else None
    prev_str = _format_value(_as_float(evidence["prev_value"]), unit_str)
    curr_str = _format_value(_as_float(evidence["curr_value"]), unit_str)

    memo = f"{ticker} {kpi_name} inflected {direction} from {prev_str} to {curr_str}"
    z = evidence.get("zscore")
    if isinstance(z, (int, float)) and not isinstance(z, bool):
        memo += f" (z-score {float(z):.1f})"
    memo += "."

    if evidence.get("threshold_crossed"):
        td = evidence.get("registered_threshold_direction")
        tv = evidence.get("registered_threshold_value")
        if isinstance(td, str) and isinstance(tv, (int, float)) and not isinstance(tv, bool):
            memo += f" Crossed your registered '{td} {float(tv):g}' threshold."
    return memo


def _build_context_prompt(factual_core: str, anchor_block: str) -> str:
    """Short prompt for the optional 'why it matters' enrichment line."""
    anchor = anchor_block.strip() or "(no thesis anchor on file)"
    return (
        f"A tracked KPI just inflected:\n{factual_core}\n\n"
        f"{anchor}\n\n"
        "In 1-2 sentences, explain why this KPI move matters for the thesis. "
        "Plain text, no preamble, no markdown."
    )


def _as_float(value: object) -> float:
    """Narrow a numeric evidence value to float. Fails loud on a non-numeric
    (rejecting bool, an int subclass) — scan() always stores prev/curr as
    floats, so a miss here is a contract bug, not user input to absorb."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise TypeError(f"expected numeric evidence value, got {type(value).__name__}")


def _sha256_text(text: str) -> str:
    """Deterministic sha256 over UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _context_cache_inputs(
    *,
    ticker: str,
    kpi_name: str,
    period_end: str,
    factual_core: str,
    anchor_text: str,
) -> list[bytes | str]:
    """Deterministic cache-input list for ``compute_input_sha256``. Order is
    part of the contract — changing it invalidates every cached context."""
    return [
        f"ticker={ticker}",
        f"kpi={kpi_name}",
        f"period_end={period_end}",
        "factual_sha=" + _sha256_text(factual_core),
        "anchor_sha=" + _sha256_text(anchor_text),
    ]


# --------------------------------------------------------------------------
# Path helpers — repo root / DB resolution (Protocol passes no connection to
# build_alert; mirrors earnings_tone).
# --------------------------------------------------------------------------


def _repo_root() -> Path:
    """``src/triggers/kpi_inflection.py`` → parents[2] = project root. Used to
    address ``micro_thesis/holdings/<TICKER>.json`` via load_thesis_anchor."""
    return Path(__file__).resolve().parents[2]


def _db_path() -> Path:
    """Resolve the portfolio DB for the artifact-store cache. Late import so a
    test's ``monkeypatch.setattr('db.DB_PATH', ...)`` is honored (binding at
    module import would freeze the pre-patch value)."""
    from db import DB_PATH

    return Path(DB_PATH)


# --------------------------------------------------------------------------
# Sensor implementation
# --------------------------------------------------------------------------


class KpiInflectionTrigger:
    """Daily sensor over ``kpi_facts`` for user-registered KPIs."""

    kind: ClassVar[str] = "kpi_inflection"
    cadence: ClassVar[Cadence] = Cadence.DAILY

    def scan(
        self, ticker: str, db: sqlite3.Connection
    ) -> list[TriggerCandidate]:
        """Emit one candidate per registered KPI whose series just inflected.

        Loads the user's registered KPIs, then for each loads the recent
        quarterly series and runs detect_inflection. A candidate is emitted
        only when the detected changepoint is *recent* (within the last
        ``_RECENT_INFLECTION_WINDOW`` quarters) and non-trivial. Returns ``[]``
        when there are no registered KPIs, the DB path can't be resolved, or
        none inflected — never raises on missing tables / empty series.
        """
        db_path = _conn_db_path(db)
        if db_path is None:
            log.debug({"event": "kpi_inflection_scan_no_db_path", "ticker": ticker})
            return []

        registered = _load_registered_kpis(ticker, db_path)
        if not registered:
            return []

        now = datetime.now(UTC).replace(tzinfo=None)
        candidates: list[TriggerCandidate] = []
        for reg in registered:
            series = load_kpi_series(
                ticker=ticker, kpi_name=reg.kpi_name, db_path=db_path
            )
            if len(series) > _LOOKBACK_QUARTERS:
                series = series[-_LOOKBACK_QUARTERS:]
            candidate = self._candidate_from_series(
                ticker=ticker, reg=reg, series=series, db=db, now=now
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _candidate_from_series(
        self,
        *,
        ticker: str,
        reg: UserKpiRegistryRow,
        series: list[Observation],
        db: sqlite3.Connection,
        now: datetime,
    ) -> TriggerCandidate | None:
        """Build a candidate iff the series shows a recent, non-trivial
        inflection. ``prev_value`` is the last reading of the prior regime
        (series[idx-1]); ``curr_value`` is the latest reading — so the memo
        reads "inflected from <pre-regime level> to <latest>"."""
        if len(series) < _MIN_SERIES_LEN:
            return None

        infl = detect_inflection(series)
        idx = infl.get("inflection_index")
        if not isinstance(idx, int):
            return None  # no changepoint (or insufficient_data)

        n = len(series)
        if not (1 <= idx < n) or idx < n - _RECENT_INFLECTION_WINDOW:
            return None  # changepoint too old to count as a recent inflection

        magnitude = infl.get("magnitude")
        if (
            not isinstance(magnitude, (int, float))
            or isinstance(magnitude, bool)
            or float(magnitude) < _MIN_INFLECTION_MAGNITUDE
        ):
            return None  # trivial regime shift — noise

        curr_value = float(series[-1].value)
        prev_value = float(series[idx - 1].value)
        period_end = series[-1].period_end.date().isoformat()
        direction = _INFLECTION_DOWN if curr_value < prev_value else _INFLECTION_UP
        zscore = _latest_zscore(series)
        unit = _load_kpi_unit(db, ticker, reg.kpi_name)

        threshold_dir = reg.threshold_direction
        threshold_val = reg.threshold_value
        threshold_crossed = (
            isinstance(threshold_dir, str)
            and threshold_val is not None
            and _crosses_threshold(threshold_dir, float(threshold_val), curr_value)
        )

        snapshot: list[dict[str, object]] = [
            {"period_end": o.period_end.date().isoformat(), "value": float(o.value)}
            for o in series[-_SERIES_SNAPSHOT_LEN:]
        ]

        inflection_period = infl.get("inflection_period")
        evidence: dict[str, object] = {
            "kpi_name": reg.kpi_name,
            "period_end": period_end,
            "prev_value": prev_value,
            "curr_value": curr_value,
            "zscore": zscore,
            "inflection_direction": direction,
            "unit": unit,
            "inflection_period": inflection_period if isinstance(inflection_period, str) else None,
            "inflection_magnitude": float(magnitude),
            "registered_threshold_direction": threshold_dir,
            "registered_threshold_value": threshold_val,
            "is_thesis_breaker": bool(reg.is_thesis_breaker),
            "threshold_crossed": bool(threshold_crossed),
            "series_snapshot": snapshot,
        }
        key = f"{ticker}:{reg.kpi_name}:{period_end}"
        return TriggerCandidate(
            ticker=ticker,
            kind=self.kind,
            key=key,
            evidence=evidence,
            computed_at=now,
        )

    def should_fire(
        self,
        candidate: TriggerCandidate,
        user_state: UserStateContext,
    ) -> bool:
        """Registry threshold policy.

        Fires when EITHER (a) the KPI has a registered threshold AND
        ``curr_value`` crosses it, OR (b) the KPI has no registered threshold
        but the inflection is statistically significant (``|zscore| >=
        _SIGNIFICANT_ZSCORE``). A registered-but-not-crossed threshold returns
        False — it does not fall through to the z-score path. Driver-side
        dedup (find_by_signature) owns the dismissed-alert suppression, so we
        don't re-check ``recent_dismissed_signatures`` here.
        """
        evidence = candidate.evidence
        kpi_name = evidence.get("kpi_name")
        curr_value = evidence.get("curr_value")
        if (
            not isinstance(kpi_name, str)
            or not isinstance(curr_value, (int, float))
            or isinstance(curr_value, bool)
        ):
            return False

        direction, threshold = _registered_threshold(
            user_state.registered_kpis, kpi_name
        )
        if direction is not None and threshold is not None:
            return _crosses_threshold(direction, threshold, float(curr_value))

        z = evidence.get("zscore")
        if isinstance(z, (int, float)) and not isinstance(z, bool):
            return abs(float(z)) >= _SIGNIFICANT_ZSCORE
        return False

    def signature_key_evidence(
        self, candidate: TriggerCandidate
    ) -> Mapping[str, object]:
        """Key the dedup hash on (kpi_name, period_end): a given quarter's
        inflection fires once, but next quarter's fires fresh. Must match the
        key_evidence ``build_alert`` feeds compute_signature_sha so the
        driver's pre-build dedup agrees with the persisted signature."""
        return {
            "kpi_name": candidate.evidence["kpi_name"],
            "period_end": candidate.evidence["period_end"],
        }

    def build_alert(
        self,
        candidate: TriggerCandidate,
        anchor: ThesisAnchor | None,
    ) -> AlertDraft:
        """Assemble the alert. The deterministic factual memo is built first
        and is the contract; the optional LLM context line is appended only
        when the best-effort enrichment succeeds. Never raises on LLM failure.
        """
        _ = anchor  # driver passes None; the markdown anchor is loaded from disk
        ticker = candidate.ticker
        evidence = candidate.evidence

        factual_memo = _factual_memo(ticker, evidence)
        llm_context = self._maybe_llm_context(
            ticker=ticker, evidence=evidence, factual_core=factual_memo
        )
        memo_text = factual_memo if llm_context is None else f"{factual_memo} {llm_context}"

        evidence_obj = dict(evidence)
        evidence_obj["llm_context_available"] = llm_context is not None
        evidence_json = json.dumps(
            evidence_obj, sort_keys=True, ensure_ascii=False, default=str
        )
        signature_sha = compute_signature_sha(
            self.kind, ticker, self.signature_key_evidence(candidate)
        )
        fired_at = datetime.now(UTC).replace(tzinfo=None)

        return AlertDraft(
            trigger_kind=self.kind,
            ticker=ticker,
            fired_at=fired_at,
            evidence_json=evidence_json,
            signature_sha=signature_sha,
            memo_text=memo_text,
        )

    def draft_actions(
        self,
        alert: AlertDraft,
        candidate: TriggerCandidate,
    ) -> list[QueuedActionDraft]:
        """Propose downstream mutations.

        Always an ``earnings_prep_append`` (revisit the KPI next quarter). When
        a registered threshold was crossed, add a ``thesis_update``; when the
        crossed KPI is also a thesis-breaker, add a ``sizing_update`` (an
        adverse move on a breaker warrants a sizing review). Reads the
        threshold / breaker flags carried in candidate.evidence by scan().
        """
        evidence = candidate.evidence
        kpi_name = evidence.get("kpi_name")
        direction = evidence.get("inflection_direction")
        if not isinstance(kpi_name, str) or not isinstance(direction, str):
            return []

        threshold_crossed = bool(evidence.get("threshold_crossed"))
        is_breaker = bool(evidence.get("is_thesis_breaker"))
        memo_text = alert.memo_text or ""

        actions: list[QueuedActionDraft] = [
            QueuedActionDraft(
                action_kind=_ACTION_EARNINGS_PREP,
                payload={
                    "body": (
                        f"Re-check {kpi_name} trend next quarter "
                        f"(inflected {direction} this period)."
                    ),
                    "kpi_name": kpi_name,
                },
            )
        ]

        if threshold_crossed:
            actions.append(
                QueuedActionDraft(
                    action_kind=_ACTION_THESIS_UPDATE,
                    payload={
                        "body": (
                            f"{kpi_name} crossed your "
                            f"{_threshold_label(evidence)} threshold: {memo_text}"
                        ),
                        "kpi_name": kpi_name,
                    },
                )
            )
            if is_breaker:
                actions.append(
                    QueuedActionDraft(
                        action_kind=_ACTION_SIZING_UPDATE,
                        payload={
                            "body": (
                                f"Adverse move on thesis-breaker {kpi_name}; "
                                "consider a sizing review."
                            ),
                            "kpi_name": kpi_name,
                            "intent_kind": "sizing_note",
                        },
                    )
                )
        return actions

    # ------------------------------------------------------------------
    # Internal — optional LLM enrichment (best-effort, cache-aware)
    # ------------------------------------------------------------------

    def _maybe_llm_context(
        self, *, ticker: str, evidence: dict[str, object], factual_core: str
    ) -> str | None:
        """Best-effort 1-2 sentence context line, or None.

        Degrades silently to None on ANY failure (LLM down, budget cap, empty
        response, store error) so build_alert always emits the deterministic
        memo. Cache-aware via the artifact store.
        """
        kpi_name = str(evidence["kpi_name"])
        period_end = str(evidence["period_end"])
        repo_root = _repo_root()
        db_path = _db_path()

        try:
            anchor_block = load_thesis_anchor(repo_root, ticker)
        except Exception as exc:  # anchor is optional; never block the memo
            log.debug(
                {
                    "event": "kpi_inflection_anchor_load_failed",
                    "ticker": ticker,
                    "error": str(exc),
                }
            )
            anchor_block = ""

        cache_inputs = _context_cache_inputs(
            ticker=ticker,
            kpi_name=kpi_name,
            period_end=period_end,
            factual_core=factual_core,
            anchor_text=anchor_block,
        )
        try:
            return self._read_cached_or_call_llm_context(
                ticker=ticker,
                kpi_name=kpi_name,
                factual_core=factual_core,
                anchor_block=anchor_block,
                cache_inputs=cache_inputs,
                db_path=db_path,
            )
        except Exception as exc:
            # The deterministic memo is the contract — an LLM/budget/parse
            # failure here must NOT sink the alert. Log and proceed without
            # the context line.
            log.warning(
                {
                    "event": "kpi_inflection_context_failed",
                    "ticker": ticker,
                    "kpi": kpi_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            return None

    def _read_cached_or_call_llm_context(
        self,
        *,
        ticker: str,
        kpi_name: str,
        factual_core: str,
        anchor_block: str,
        cache_inputs: list[bytes | str],
        db_path: Path,
    ) -> str | None:
        """Return the context line, hitting the artifact-store cache when
        possible. Per-KPI artifacts share the (ticker, purpose) slot, so the
        KPI name rides in ``fiscal_period`` to keep distinct KPIs from
        superseding each other; ``input_sha256`` still guards correctness."""
        existing = read_current(
            ticker=ticker,
            purpose=_ARTIFACT_PURPOSE,
            fiscal_period=kpi_name,
            db_path=db_path,
        )
        new_sha = compute_input_sha256(
            prompt_version=_PROMPT_VERSION, cache_inputs=cache_inputs
        )
        if (
            existing is not None
            and not existing.dirty
            and existing.input_sha256 == new_sha
            and isinstance(existing.content_md, str)
            and existing.content_md.strip()
        ):
            log.info(
                {
                    "event": "kpi_inflection_context_cache_hit",
                    "ticker": ticker,
                    "kpi": kpi_name,
                    "artifact_id": existing.id,
                }
            )
            return existing.content_md

        prompt = _build_context_prompt(factual_core, anchor_block)
        raw = call_llm(prompt, purpose=_ARTIFACT_PURPOSE, ticker=ticker)
        context = raw.strip()
        if not context:
            return None

        _ = upsert(
            UpsertRequest(
                ticker=ticker,
                purpose=_ARTIFACT_PURPOSE,
                fiscal_period=kpi_name,
                content_md=context,
                cache_inputs=cache_inputs,
                prompt_version=_PROMPT_VERSION,
            ),
            db_path=db_path,
        )
        return context


def _threshold_label(evidence: dict[str, object]) -> str:
    """'below 17' style label for the registered threshold, or 'registered'
    when the evidence lacks a usable threshold pair."""
    td = evidence.get("registered_threshold_direction")
    tv = evidence.get("registered_threshold_value")
    if isinstance(td, str) and isinstance(tv, (int, float)) and not isinstance(tv, bool):
        return f"{td} {float(tv):g}"
    return "registered"
