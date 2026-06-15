"""LLM-driven "key metrics" preselect — the generator behind the DIY picker's
preselect bubble row (directives/key_metrics_picker.md).

The Research → Explore "DIY" metric picker exposes every extracted fact for the
selected tickers as a flat ``<select multiple>`` of metric tokens. That is
exhaustive but undiscoverable: for a digital bank the load-bearing figures are
net interest margin, NPL, deposits, the efficiency ratio — but they sit in an
alphabetical list of hundreds of captured numbers with no signal of importance.

Two layers fix that:

* **Deterministic baseline (always, NO LLM)** — the tier-1/tier-2 KPI grading
  already in ``kpi_definitions.threshold_tier`` (the thesis breakers). Rendered
  straight from the DB by ``src/pipeline/key_metrics.py`` on every render.
* **LLM augmentation (this module, runs on ``--enable-llm``, NOT on render)** —
  ranks the metrics MOST important to the business over the FULL available
  catalog (so it can surface important CAPTURED metrics — ``definition_origin
  = 'capture'`` — that the tier grading hasn't reached yet), business-model
  aware. Cached to ``data/key_metrics/{TICKER}.json`` keyed on an input sha256;
  the renderer reads that cache directly and merges it onto the tier baseline.

Mirrors ``src/compute/peer_selection.py`` exactly (the platform's reference LLM
pattern): one ``key_metrics`` call → schema-validated structured output → cache
keyed on the inputs → degrade to the tier-graded baseline on ANY LLM/parse/
budget failure (the renderer never crashes, the build never aborts). See
``directives/peer_selection_llm.md`` for the governance the pattern satisfies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ValidationError, field_validator

from llm.cli import DEFAULT_MODEL, LLM_MODELS
from llm.structured import StructuredParseError, call_llm_structured

log = logging.getLogger(__name__)

PURPOSE = "key_metrics"

# How many catalog entries (per domain) to put in front of the LLM. The picker
# catalog can run to hundreds of captured numbers per ticker; cap the vocabulary
# so the prompt stays bounded while still covering the head of each domain
# (catalog rows arrive importance-agnostic but the LLM ranks within them).
_MAX_VOCAB_FIN = 120
_MAX_VOCAB_KPI = 200
_MAX_DESC_CHARS = 4000
# Default ceiling on the LLM's returned picks. The renderer caps the *combined*
# tier-graded + LLM bubble row separately; this only bounds the cached set.
_DEFAULT_MAX_METRICS = 12


class KeyMetricSuggestion(BaseModel):
    """One LLM-proposed important metric. ``token`` MUST be one of the catalog
    tokens handed to the model (``fin:<line_item>`` / ``kpi:<name>``); ``why``
    is the one-line rationale that becomes the bubble's tooltip."""

    token: str
    why: str

    @field_validator("token")
    @classmethod
    def _norm_token(cls, v: str) -> str:
        return v.strip()

    @field_validator("why")
    @classmethod
    def _norm_text(cls, v: str) -> str:
        return v.strip()


@dataclass
class KeyMetricsResult:
    ticker: str
    metrics: list[dict[str, str]]  # [{token, why}], LLM rank order preserved
    inputs_sha256: str | None
    model: str = DEFAULT_MODEL
    extracted_at: str | None = None
    skipped_reason: str | None = None


# ---------------------------------------------------------------------------
# the LLM call (also the eval's production entry point)
# ---------------------------------------------------------------------------


def _build_prompt(
    *,
    ticker: str,
    name: str | None,
    business_description: str,
    vocabulary: list[tuple[str, str]],
    max_metrics: int,
) -> str:
    label = f"{name} ({ticker})" if name else ticker
    vocab_lines = "\n".join(f"  {token}\t{vlabel}" for token, vlabel in vocabulary)
    return (
        f"You are choosing the KEY METRICS for {label} — the handful of figures a "
        "hedge-fund-style analyst would put on the dashboard for this company, the "
        "ones that actually load-bear the investment thesis and reveal whether the "
        "business is working.\n\n"
        "Choose by BUSINESS MODEL — how the company makes money and what would make "
        "an analyst worried or excited. A digital bank's key metrics are net interest "
        "margin, NPL / loan-loss, deposits, monthly active customers, efficiency "
        "ratio — not generic 'total assets'. A SaaS company's are net revenue "
        "retention, ARR / subscription revenue, gross margin, billings — not 'cost of "
        "goods sold'. Prefer the operating KPIs that explain the stock over the raw "
        "accounting lines when both are available.\n\n"
        f"Company: {label}\n"
        "Business description:\n"
        f"{business_description or '(none available — infer from the metric names below)'}\n\n"
        "Pick ONLY from this catalog of metrics that are actually available for this "
        "ticker (TOKEN<tab>label, one per line):\n"
        f"{vocab_lines}\n\n"
        f"Return ONLY a JSON array of {max_metrics} or fewer objects, each exactly:\n"
        '  {"token": "<one TOKEN copied verbatim from the catalog above>", '
        '"why": "<one concise clause: why this metric matters for this business>"}\n'
        "Rules:\n"
        "- `token` MUST be copied EXACTLY from the catalog above — never invent a "
        "token or a metric that is not listed.\n"
        "- Rank the most important / most thesis-relevant metric first.\n"
        "- Aim for the 6-12 metrics that genuinely matter; do not pad the list.\n"
        "- `why` states why THIS business is judged on THIS metric, not a generic "
        "definition.\n"
        "Return the JSON array and nothing else."
    )


def suggest_key_metrics(
    *,
    ticker: str,
    name: str | None,
    business_description: str,
    vocabulary: list[tuple[str, str]],
    model: str | None = None,
    backend: str | None = None,
    max_metrics: int = _DEFAULT_MAX_METRICS,
) -> list[KeyMetricSuggestion]:
    """One LLM call → validated important-metric picks over ``vocabulary``.

    ``vocabulary`` is the closed set of ``(token, label)`` the model may pick
    from — every returned token is validated against it, so a hallucinated or
    stale token is dropped (it would never map to a picker option). Order is the
    LLM's rank order.

    Raises ``StructuredParseError`` when the model returns unusable JSON on both
    attempts (the caller degrades to the tier-graded baseline — never crashes the
    build). Hard stops (budget cap / missing CLI) propagate per
    ``call_llm_structured``. Returns ``[]`` when the LLM legitimately proposes
    nothing or the vocabulary is empty.
    """
    ticker = ticker.upper()
    if not vocabulary:
        return []
    valid_tokens = {token for token, _label in vocabulary}
    prompt = _build_prompt(
        ticker=ticker,
        name=name,
        business_description=business_description[:_MAX_DESC_CHARS],
        vocabulary=vocabulary,
        max_metrics=max_metrics,
    )
    payload = call_llm_structured(
        prompt, purpose=PURPOSE, ticker=ticker, model=model, backend=backend, expect="array"
    )
    out: list[KeyMetricSuggestion] = []
    seen: set[str] = set()
    for entry in cast("list[object]", payload):
        if not isinstance(entry, dict):
            continue
        try:
            sug = KeyMetricSuggestion.model_validate(entry)
        except ValidationError:
            continue
        if sug.token not in valid_tokens or sug.token in seen:
            continue  # hallucinated / stale / duplicate token — would never map
        seen.add(sug.token)
        out.append(sug)
        if len(out) >= max_metrics:
            break
    return out


# ---------------------------------------------------------------------------
# extract → cache (the build step)
# ---------------------------------------------------------------------------


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    db_conn: sqlite3.Connection,
    *,
    refresh: bool = False,
    max_metrics: int = _DEFAULT_MAX_METRICS,
) -> KeyMetricsResult:
    """End-to-end gather → suggest → cache. Idempotent on the input sha256 unless
    ``refresh=True``. Never raises for LLM/parse failure — those are recorded in
    ``skipped_reason`` and the renderer falls back to the tier-graded baseline."""
    ticker = ticker.upper()
    db_path = repo_root / "data" / "portfolio.db"
    cache_path = _cache_path(repo_root, ticker)

    name = _company_name(db_conn, repo_root, ticker)
    business_description = _gather_business_description(repo_root, ticker)
    vocabulary = _vocabulary(db_path, ticker)
    inputs_sha = _inputs_sha(name, business_description, vocabulary)

    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("inputs_sha256") == inputs_sha:
            return KeyMetricsResult(**cast("dict[str, Any]", cached))

    start = datetime.now(UTC)
    if not vocabulary:
        result = KeyMetricsResult(
            ticker=ticker,
            metrics=[],
            inputs_sha256=inputs_sha,
            model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
            extracted_at=_stamp(start),
            skipped_reason="no extracted metrics in the catalog for this ticker",
        )
        _write_cache(cache_path, result)
        return result

    try:
        suggestions = suggest_key_metrics(
            ticker=ticker,
            name=name,
            business_description=business_description,
            vocabulary=vocabulary,
            max_metrics=max_metrics,
        )
    except StructuredParseError as exc:
        result = KeyMetricsResult(
            ticker=ticker,
            metrics=[],
            inputs_sha256=inputs_sha,
            model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
            extracted_at=_stamp(start),
            skipped_reason=f"llm parse failure: {exc}",
        )
        _write_cache(cache_path, result)
        return result

    metric_dicts = [{"token": s.token, "why": s.why} for s in suggestions]
    result = KeyMetricsResult(
        ticker=ticker,
        metrics=metric_dicts,
        inputs_sha256=inputs_sha,
        model=LLM_MODELS.get(PURPOSE, DEFAULT_MODEL),
        extracted_at=_stamp(start),
        skipped_reason=None if metric_dicts else "llm returned no metrics",
    )
    _write_cache(cache_path, result)
    return result


# ---------------------------------------------------------------------------
# input gathering
# ---------------------------------------------------------------------------


def _vocabulary(db_path: Path, ticker: str) -> list[tuple[str, str]]:
    """The (token, label) catalog the LLM may pick from: the ticker's financial
    line items + KPI names from the live picker catalog, capped per domain.

    Reuses ``viewspec.engine.metric_catalog`` (the exact vocabulary the DIY
    picker renders) so every suggested token maps to a real picker option.
    Segments are intentionally excluded — a key-metric bubble is a single
    headline figure, not a per-segment slice."""
    try:
        from viewspec.engine import metric_catalog
    except Exception:  # pragma: no cover - import guard
        return []
    try:
        catalog = metric_catalog(db_path, [ticker])
    except Exception:  # best-effort — a catalog failure just empties the vocab
        return []
    vocab: list[tuple[str, str]] = []
    for domain, cap in (("fin", _MAX_VOCAB_FIN), ("kpi", _MAX_VOCAB_KPI)):
        for entry in catalog.get(domain, [])[:cap]:
            token = entry.get("token")
            label = entry.get("label")
            if isinstance(token, str) and token and isinstance(label, str):
                vocab.append((token, label))
    return vocab


def _company_name(conn: sqlite3.Connection, repo_root: Path, ticker: str) -> str | None:
    """Display name for the prompt: tracked_companies → FMP profile → None."""
    try:
        row = conn.execute(
            "SELECT name FROM tracked_companies WHERE ticker = ? AND name <> '' LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None and row[0]:
        return str(row[0]).strip()
    rec = _first_record(repo_root / "data" / "historical" / "fmp" / f"{ticker}_profile.json")
    if rec is not None:
        return _str_or_none(rec.get("companyName"))
    return None


def _gather_business_description(repo_root: Path, ticker: str) -> str:
    """Best business description we have, in priority order: the
    company_description cache (analytical, grounded in the 10-K) → the owner's
    thesis → the FMP profile blurb. Mirrors peer_selection's gatherer."""
    parts: list[str] = []
    try:
        from compute.company_description import load_description

        desc = load_description(repo_root, ticker)
    except Exception:  # best-effort input gathering — never block on a bad cache
        desc = None
    if desc is not None and not desc.skipped_reason:
        for piece in (desc.elevator_pitch, desc.business_overview, desc.revenue_model):
            if isinstance(piece, str) and piece.strip():
                parts.append(piece.strip())
    if parts:
        return "\n\n".join(parts)[:_MAX_DESC_CHARS]

    thesis = _load_thesis(repo_root, ticker)
    if thesis:
        return thesis[:_MAX_DESC_CHARS]

    rec = _first_record(repo_root / "data" / "historical" / "fmp" / f"{ticker}_profile.json")
    if rec is not None:
        blurb = _str_or_none(rec.get("description"))
        if blurb:
            return blurb[:_MAX_DESC_CHARS]
    return ""


def _load_thesis(repo_root: Path, ticker: str) -> str:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    if not path.exists():
        return ""
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ""
    for key in ("thesis", "thesis_full", "thesis_one_liner"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _cache_path(repo_root: Path, ticker: str) -> Path:
    out_dir = repo_root / "data" / "key_metrics"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{ticker.upper()}.json"


def _inputs_sha(
    name: str | None,
    business_description: str,
    vocabulary: list[tuple[str, str]],
) -> str:
    blob = "\x00".join(
        [
            name or "",
            business_description,
            "|".join(f"{t}={lbl}" for t, lbl in vocabulary),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _first_record(path: Path) -> dict[str, object] | None:
    raw = _read_json(path)
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return cast("dict[str, object]", raw[0])
    if isinstance(raw, dict):
        return cast("dict[str, object]", raw)
    return None


def _str_or_none(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_cache(path: Path, result: KeyMetricsResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
