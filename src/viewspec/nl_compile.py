"""Natural-language → ViewSpec compiler (master build P5.2).

The Explore panel's query box: a question like "NU vs MELI revenue growth,
last 8 quarters" goes to a fast model (Haiku — see ``viewspec_compile`` in
``llm.cli.LLM_MODELS``) that emits a ViewSpec JSON object, which is then
validated by the SAME ``ViewSpec.from_dict`` the builder UI and the REST
routes use. The model never writes SQL and its output never executes
unvalidated — a spec that fails validation gets ONE repair attempt (the
validation message is fed back) and then the panel degrades to the
structured builder. That tri-state is the module's whole contract:

  ok             — a validated ViewSpec, ready for execute_view
  budget_skipped — the ``viewspec_compile`` purpose is on_exceed='skip'
                   and at/over its monthly cap (llm_budgets, 0080): no
                   call is made, the NL box reports itself disabled
  error          — unparseable/invalid output after the repair attempt,
                   or the LLM call itself failed; the builder UI keeps
                   working either way

Grounding: the prompt carries the metric vocabulary (the engine's
``metric_catalog``) for the panel's current tickers plus any tracked
tickers named in the query, so the model copies real tokens instead of
inventing metric names. Tokens it still invents fail validation or render
as no-data warnings — never an exception, never a query.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from llm.cli import call_llm
from llm_budget import should_skip_for_budget
from viewspec.engine import metric_catalog
from viewspec.spec import (
    MAX_METRICS,
    MAX_PERIODS,
    MAX_TICKERS,
    ViewSpec,
    ViewSpecError,
)

log = logging.getLogger(__name__)

PURPOSE = "viewspec_compile"

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")
_TICKERISH_RE = re.compile(r"\b[A-Z][A-Z0-9.\-]{0,5}\b")

# Vocabulary caps bound the grounding prompt so an interactive compile stays
# fast. Raised well above the old 60/120/80 so the "capture every reported
# number" long tail (one-off / company-specific facts) lands in the prompt
# instead of being truncated out — a few hundred short token lines is still a
# small prompt for the fast compile model. The hard ceiling only guards a
# pathological catalog; the picker's own (much larger) cap lives in
# engine._CATALOG_LIMIT_PER_DOMAIN.
_MAX_VOCAB_FIN = 250
_MAX_VOCAB_KPI = 500
_MAX_VOCAB_SEG = 300


@dataclass(slots=True)
class NLCompileResult:
    """What one compile attempt produced — see the module docstring.

    ``prompt_text`` / ``raw_response`` carry the LAST prompt sent (including
    any repair feedback) and the model's raw output — transcript capture for
    the eval harness (src/evals/) and for debugging failed compiles. ``None``
    when no LLM call was made (empty query, budget skip).
    """

    status: str  # "ok" | "budget_skipped" | "error"
    spec: ViewSpec | None = None
    message: str | None = None
    attempts: int = 0
    prompt_text: str | None = None
    raw_response: str | None = None


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text).strip()
    return text


def _tracked_tickers(db_path: Path) -> set[str]:
    """Every tracked symbol (any list type) — membership test for resolving
    ticker-ish tokens in the query. Best-effort: empty set on a missing DB."""
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return set()
    try:
        rows = conn.execute("SELECT DISTINCT ticker FROM tracked_companies").fetchall()
        return {str(r[0]).upper() for r in rows}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _tickers_in_query(query: str, tracked: set[str]) -> list[str]:
    """Tracked tickers literally named (uppercase) in the query, in order."""
    out: dict[str, None] = {}
    for m in _TICKERISH_RE.finditer(query):
        sym = m.group(0)
        if sym in tracked:
            out.setdefault(sym, None)
    return list(out)


def _vocab_block(db_path: Path, tickers: list[str]) -> str:
    """The grounding vocabulary: one token per line, per domain, capped."""
    catalog = metric_catalog(db_path, tickers)
    lines: list[str] = []
    for domain, cap in (("fin", _MAX_VOCAB_FIN), ("kpi", _MAX_VOCAB_KPI), ("seg", _MAX_VOCAB_SEG)):
        entries = catalog.get(domain, [])[:cap]
        if not entries:
            continue
        lines.append(f"# {domain} tokens")
        lines.extend(str(e["token"]) for e in entries)
    return "\n".join(lines) if lines else "# (no metric vocabulary available)"


def _build_prompt(
    query: str,
    vocab: str,
    default_tickers: list[str],
    context_spec: dict[str, object] | None = None,
) -> str:
    defaults = ", ".join(default_tickers) if default_tickers else "(none configured)"
    context_block = ""
    if context_spec:
        context_block = f"""
Current view (the user may be REFINING it — "now annual", "add MELI",
"same but as margins"): {json.dumps(context_spec)}
- When the question refines the current view, return the FULL updated spec,
  carrying over everything the question doesn't change. When the question is
  a fresh one, ignore the current view entirely.
"""
    return f"""\
You translate an analyst's question into a ViewSpec JSON object for a
financial pivot engine. Output ONLY the JSON object — no prose, no
markdown fences.

Schema:
{{"tickers": [1-{MAX_TICKERS} uppercase symbols],
 "metrics": [1-{MAX_METRICS} metric tokens copied EXACTLY from the vocabulary below],
 "transform": "level" | "yoy" | "cagr" | "margin",
 "cadence": "quarterly" | "annual",
 "periods": <int 1-{MAX_PERIODS}, how many period columns>,
 "cagr_years": <int 1-10, only meaningful when transform="cagr">}}

Rules:
- metrics MUST be tokens copied verbatim from the vocabulary ("fin:..." financial line items,
  "kpi:..." company KPIs, "seg:<dim_type>:<dim_name>:<metric>" segment slices). Never invent one.
  If the user names something with no matching token, use the closest token that exists.
- Resolve colloquial metric names to the MOST SPECIFIC matching token, never a broader
  rollup that merely contains it: "R&D spend" -> fin:research_and_development (NOT
  fin:operating_expenses), "capex" -> fin:capital_expenditure.
- tickers: use the symbols the user names; when none are named, default to: {defaults}.
- transform: "yoy" for growth/change questions, "cagr" for multi-year compounding (set
  cagr_years from the question, default 3), "margin" for "as % of revenue" questions,
  otherwise "level".
- The transform is computed ON TOP of the chosen metric, so growth/margin questions take
  the BASE measure and let the transform do the math: "revenue growth" -> fin:revenue +
  transform "yoy", NOT a pre-computed kpi like kpi:Revenue YoY Growth (USD).
- kpi tokens whose NAME already encodes a growth rate / YoY change / margin
  ("...YoY Growth...", "...Margin...", "...YoY change...") are ALREADY transformed: when
  one IS the right metric, transform must be "level" — never stack "yoy"/"margin" on top
  (that computes growth-of-a-growth-rate / margin-of-a-margin, which is always wrong).
- cadence "annual" only when the question is in years/annual/FY terms; periods defaults
  to 12 for quarterly and 8 for annual when the question doesn't say.
{context_block}
Metric vocabulary:
{vocab}

Question: {query}
JSON:"""


def _parse_and_validate(raw: str) -> ViewSpec:
    """Fenced/raw model output → validated ViewSpec. Raises ViewSpecError
    with a readable message on any failure (fed back to the repair pass)."""
    text = _strip_fences(raw)
    try:
        parsed: object = json.loads(text)
    except ValueError as exc:
        raise ViewSpecError(f"not valid JSON ({exc})") from exc
    return ViewSpec.from_dict(parsed)


def compile_nl_to_viewspec(
    query: str,
    *,
    db_path: Path,
    context_tickers: list[str] | None = None,
    context_spec: dict[str, object] | None = None,
    run_id: str | None = None,
) -> NLCompileResult:
    """Compile one natural-language question. Never raises — every failure
    mode returns a status the panel can render (see module docstring).

    ``context_tickers`` is the panel's current ticker input — it scopes the
    kpi/segment vocabulary and serves as the default universe when the
    question names no tickers. ``context_spec`` is the PREVIOUS turn's spec
    (the Ask thread, PR5): the prompt instructs the model to treat the query
    as a refinement of it when that reading fits ("now annual" carries the
    rest of the view over).
    """
    q = query.strip()
    if not q:
        return NLCompileResult(status="error", message="Ask a question first.")

    skip = should_skip_for_budget(PURPOSE, db_path=db_path)
    if skip is not None:
        return NLCompileResult(
            status="budget_skipped",
            message=(
                f"NL compile is over its ${float(skip.cap):g} monthly budget "
                f"(${float(skip.current_spend):.2f} spent) — use the builder, or raise "
                f"the {PURPOSE} cap under Settings."
            ),
        )

    defaults = [t.strip().upper() for t in (context_tickers or []) if t.strip()]
    tracked = _tracked_tickers(db_path)
    named = _tickers_in_query(q, tracked)
    context_spec_tickers: list[str] = []
    if context_spec:
        raw_tk = context_spec.get("tickers")
        if isinstance(raw_tk, list):
            context_spec_tickers = [
                t.upper() for t in cast("list[object]", raw_tk) if isinstance(t, str) and t.strip()
            ]
    vocab_tickers = (
        list(dict.fromkeys([*defaults, *named, *context_spec_tickers])) or sorted(tracked)[:8]
    )
    vocab = _vocab_block(db_path, vocab_tickers)
    prompt = _build_prompt(q, vocab, defaults or named, context_spec=context_spec)

    attempts = 0
    last_error = ""
    raw = ""
    ask = prompt
    while attempts < 2:
        attempts += 1
        ask = prompt
        if last_error:
            ask = (
                f"{prompt}\n\nYour previous output was rejected: {last_error}\n"
                f"Previous output (for reference):\n{raw[:500]}\n"
                "Return ONLY the corrected JSON object."
            )
        try:
            raw = call_llm(ask, purpose=PURPOSE, scope="explore", run_id=run_id)
        except Exception as exc:
            # Interactive surface: a budget hard-stop or CLI failure must
            # degrade to the builder, never 500 the panel.
            log.warning(
                {
                    "event": "viewspec_nl_compile_call_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "attempt": attempts,
                }
            )
            return NLCompileResult(
                status="error",
                message="The compile call failed — use the builder.",
                attempts=attempts,
                prompt_text=ask,
            )
        try:
            spec = _parse_and_validate(raw)
        except ViewSpecError as exc:
            last_error = str(exc)
            log.info(
                {
                    "event": "viewspec_nl_compile_invalid",
                    "attempt": attempts,
                    "error": last_error,
                }
            )
            continue
        return NLCompileResult(
            status="ok", spec=spec, attempts=attempts, prompt_text=ask, raw_response=raw
        )

    return NLCompileResult(
        status="error",
        message=f"Could not compile that question ({last_error}) — use the builder.",
        attempts=attempts,
        prompt_text=ask,
        raw_response=raw,
    )


__all__ = ["PURPOSE", "NLCompileResult", "compile_nl_to_viewspec"]
