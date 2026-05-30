"""Semi-automated KPI registry seeder for the Personal CIO substrate.

Two-step flow (no interactive prompting — the user edits the YAML at their
own pace between the two runs):

    python scratch/seed_kpi_registry.py --propose --ticker NU
    # edit scratch/proposals/NU.yaml, mark rows status: accepted
    python scratch/seed_kpi_registry.py --write --in scratch/proposals/NU.yaml

`--propose` assembles the ticker's thesis anchor, recent kpi_facts rows,
and the latest 10-K narrative, then asks the LLM for N load-bearing KPI
proposals. The response lands as a YAML draft for the user to review.

`--write` reads the YAML, filters to rows the user marked
``status: accepted``, and upserts each one via
``user_state.registry.upsert_kpi``.

`--auto` is the no-human-review bootstrap (Opus, confidence-gated). It reuses
the same input assembly but asks the OPUS-tier model for polarity + grounded
thresholds, then a deterministic gate writes the high-confidence, name-aligned,
correctly-directed rows straight to the registry (scaffold_source ``llm_auto``)
and routes everything ambiguous to a review YAML for the unchanged ``--write``
flow. See ``scratch/plans/auto_kpi_seeding_plan.md``.

Lives under ``scratch/`` per the project convention for one-shot tools.
The dev DB / FMP cache live under the MAIN repo's ``data/`` directory
(gitignored, never propagated into a worktree); this script discovers
both by walking up from its own location.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml

# Path setup: scratch/ is at <repo_root>/scratch/ (whether <repo_root> is
# the main repo or a .claude/worktrees/<name>/ checkout). Add <repo_root>/src
# to sys.path so the canonical llm_client + user_state.registry imports work.
_SCRATCH_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRATCH_DIR.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from llm.anchors import load_thesis_anchor  # noqa: E402
from llm_client import JSON_FENCE_RE, call_llm  # noqa: E402
from user_state import registry  # noqa: E402
from user_state.kpi_catalog import allowed_kpi_names  # noqa: E402
from user_state.kpi_polarity import (  # noqa: E402
    Polarity,
    adverse_direction,
    infer_polarity_from_table,
)

log = logging.getLogger("seed_kpi_registry")

# Artifact-purpose key passed to call_llm for ledger attribution. Intentionally
# NOT registered in LLM_MODELS — proposals are one-shot and the default Sonnet
# is the right pick. The cli logs a one-line warning when the purpose is
# unknown; that's acceptable for a scratch tool.
_ARTIFACT_PURPOSE = "kpi_registry_proposal"

_DEFAULT_N_PROPOSALS = 5
_RECENT_KPI_QUARTERS = 4
_TENK_TEXT_CAP_CHARS = 12_000
_PROPOSAL_RATIONALE_NOTES_CAP = 500
_DEFAULT_USER_ID = "bhanu"
_SCAFFOLD_SOURCE = "llm_proposal"

# --auto mode (Opus, confidence-gated; no per-row human review).
# Purpose key is registered in llm.cli.LLM_MODELS -> claude-opus-4-7, so the
# auto proposer runs on Opus while the manual --propose path stays Sonnet.
_AUTO_ARTIFACT_PURPOSE = "kpi_registry_auto_proposal"
# Distinguishes machine-written rows from manually-curated ones ('manual',
# 'llm_proposal'). Only 'llm_auto' rows are safe for --auto to overwrite on a
# re-run; anything else is preserved (see auto_seed_ticker).
_AUTO_SCAFFOLD_SOURCE = "llm_auto"
_DEFAULT_MIN_CONFIDENCE = 0.75
_DEFAULT_MAX_AUTO_PER_TICKER = 6

# Hints for filtering 10-K sections to the narrative-bearing ones. The FMP
# 10-K JSONs are keyed by section title (one key per H1 in the original
# filing); these substrings — risk, business, mda, etc. — flag the prose
# sections we want to feed the model. Tables/statements are filtered out
# by the prefix/suffix exclusions below.
_NARRATIVE_KEY_HINTS: tuple[str, ...] = (
    "risk",
    "discussion",
    "management",
    "operations",
    "business",
    "narrative",
    "outlook",
    "md&a",
    "mda",
    "item 1",
    "item 7",
)

# Excluded even if a hint matches — these section keys are structured
# statements/tables, not narrative.
_NON_NARRATIVE_KEY_PREFIXES: tuple[str, ...] = (
    "consolidated ",
    "cover",
    "audit",
    "statement of",
    "schedule ii",
    "basis of",
)
_NON_NARRATIVE_KEY_SUFFIXES: tuple[str, ...] = (
    "(tables)",
    "(details)",
)

# Retry hint prepended to the user prompt on a second attempt when the
# first LLM response failed to parse as JSON. Mirrors the earnings-tone
# trigger's retry strategy.
_JSON_RETRY_PREAMBLE = (
    "IMPORTANT: Your previous response was not valid JSON. "
    "Return ONLY the JSON array specified at the end of this prompt — "
    "no markdown fences, no commentary, no prefatory prose.\n\n"
)

_VALID_DIRECTIONS: frozenset[str] = frozenset({"above", "below"})
_ACCEPTED_STATUS = "accepted"
_VALID_POLARITY_VALUES: frozenset[str] = frozenset(p.value for p in Polarity)


class SeederError(RuntimeError):
    """Raised on a structural problem with the YAML or runtime inputs."""


class SeederLLMError(RuntimeError):
    """Raised when the LLM proposal pass cannot be completed.

    Surfaces in two cases: (a) the LLM returned non-JSON on both the
    first attempt AND the retry; (b) the parsed JSON is structurally
    wrong (missing required keys / wrong element shape).
    """


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Proposal:
    """One LLM-proposed KPI row, pre-YAML.

    Mirrors the YAML row shape minus ``status`` / ``notes`` which default
    to ``proposed`` / "" on initial render. The user fills those in
    while reviewing.
    """

    kpi_name: str
    threshold_direction: str | None
    threshold_value: float | None
    is_thesis_breaker: bool
    rationale: str


@dataclass(slots=True)
class WriteSummary:
    """Counts emitted by ``--write`` so the CLI can print a single-line tally."""

    accepted: int
    written: int
    skipped: int
    errors: int


# ---------------------------------------------------------------------------
# Default path resolution
# ---------------------------------------------------------------------------


def _default_repo_root() -> Path:
    """Resolve the repo root containing ``data/`` and ``micro_thesis/``.

    From a worktree under ``.claude/worktrees/<name>/``, walk up past the
    ``.claude`` dir to the main repo. Outside a worktree (running from a
    plain checkout), the scratch dir's parent IS the repo root.
    """
    here = _SCRATCH_DIR.parent
    if (here / "data").exists():
        return here
    # worktree layout: <repo>/.claude/worktrees/<name>/scratch/...
    if len(here.parents) >= 3 and here.parents[1].name == ".claude":
        candidate = here.parents[2]
        if (candidate / "data").exists():
            return candidate
    return here


def _default_db_path(repo_root: Path) -> Path:
    return repo_root / "data" / "portfolio.db"


def _default_fmp_dir(repo_root: Path) -> Path:
    return repo_root / "data" / "historical" / "fmp"


# ---------------------------------------------------------------------------
# Inputs assembly
# ---------------------------------------------------------------------------


def _portfolio_tickers(db_path: Path) -> list[str]:
    """List ``portfolio`` tickers from ``tracked_companies``. Empty when the
    DB or table is missing — caller decides whether to halt or proceed."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies WHERE list_type = 'portfolio' ORDER BY ticker"
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "portfolio_query_failed", "error": str(exc)})
        return []
    finally:
        conn.close()
    return [str(r[0]) for r in rows if r[0] is not None]


def _recent_kpi_observations(
    db_path: Path,
    ticker: str,
    *,
    n_quarters: int = _RECENT_KPI_QUARTERS,
) -> list[dict[str, object]]:
    """Return up to ``n_quarters`` most-recent observations per distinct KPI
    name for ``ticker`` from ``kpi_facts JOIN kpi_definitions``.

    Returns ``[]`` when the DB or tables are missing (so the LLM still
    gets the rest of the inputs — the trigger framework's pattern of
    degrading rather than halting).
    """
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT kd.name AS kpi_name,
                   kf.period_end,
                   kf.fiscal_period_type,
                   kf.value,
                   kf.unit
            FROM kpi_facts kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker = ?
              AND kf.fiscal_period_type IN ('Q1','Q2','Q3','Q4')
            ORDER BY kf.period_end DESC, kd.name ASC
            """,
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "kpi_facts_query_failed", "ticker": ticker, "error": str(exc)})
        return []
    finally:
        conn.close()

    grouped: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        name = str(r["kpi_name"])
        bucket = grouped.setdefault(name, [])
        if len(bucket) >= n_quarters:
            continue
        bucket.append(
            {
                "kpi_name": name,
                "period_end": str(r["period_end"])[:10],
                "fiscal_period_type": str(r["fiscal_period_type"]),
                "value": float(r["value"]),
                "unit": str(r["unit"]) if r["unit"] is not None else "",
            }
        )

    out: list[dict[str, object]] = []
    for name in sorted(grouped):
        out.extend(grouped[name])
    return out


def _format_kpi_observations_table(obs: list[dict[str, object]]) -> str:
    """Render KPI observations as a pipe-table fragment for prompt injection."""
    if not obs:
        return ""
    header = (
        "| kpi_name | period_end | fiscal_period_type | value | unit |\n"
        "| :--- | :--- | :--- | ---: | :--- |"
    )
    body_lines: list[str] = []
    for o in obs:
        name = cast("str", o["kpi_name"])
        pe = cast("str", o["period_end"])
        fpt = cast("str", o["fiscal_period_type"])
        v = cast("float", o["value"])
        unit = cast("str", o["unit"])
        body_lines.append(f"| {name} | {pe} | {fpt} | {v:.4f} | {unit} |")
    return header + "\n" + "\n".join(body_lines)


def _latest_10k_path(ticker: str, *, fmp_dir: Path) -> Path | None:
    """Return the path to the most-recent ``<TICKER>_form_10k_<YEAR>.json``.

    Sorted by filename — relies on the ``YYYY`` suffix being chronological,
    which holds for every file produced by ``execution/save_fmp_data.py``.
    Returns None when nothing matches.
    """
    if not fmp_dir.exists():
        return None
    matches = sorted(fmp_dir.glob(f"{ticker.upper()}_form_10k_*.json"))
    return matches[-1] if matches else None


def _is_narrative_section_key(key: str) -> bool:
    """Decide whether a section key in the FMP 10-K JSON is prose-narrative.

    The FMP JSON parses every H1 in the filing as a top-level key. Most are
    financial statements / tables (excluded); the narrative-bearing ones
    (Risk Factors, MD&A, Operations) carry the substrings in
    ``_NARRATIVE_KEY_HINTS``.
    """
    k = key.lower().strip()
    for prefix in _NON_NARRATIVE_KEY_PREFIXES:
        if k.startswith(prefix):
            return False
    for suffix in _NON_NARRATIVE_KEY_SUFFIXES:
        if k.endswith(suffix):
            return False
    return any(hint in k for hint in _NARRATIVE_KEY_HINTS)


def _walk_strings(value: object, out: list[str]) -> None:
    """Recursively collect string leaves out of a JSON-shaped value.

    The FMP 10-K JSONs nest section content as
    ``list[dict[str, list[str]]]`` for most schemas, but some sections
    use a bare string or a deeper tree. Walking the tree handles every
    observed shape without per-shape branching.
    """
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for item in cast("list[object]", value):
            _walk_strings(item, out)
    elif isinstance(value, dict):
        for v in cast("dict[str, object]", value).values():
            _walk_strings(v, out)


def _extract_section_text(value: object) -> str:
    strings: list[str] = []
    _walk_strings(value, strings)
    return "\n".join(s for s in strings if s.strip())


def _extract_10k_narrative(path: Path, *, cap: int = _TENK_TEXT_CAP_CHARS) -> str:
    """Read the 10-K JSON, return concatenated narrative text from
    narrative-keyed sections. Tail-preserving truncation when over ``cap``.

    Returns "" when the file is unreadable or the JSON shape is
    unexpected — the caller decides whether to halt (no 10-K available)
    or proceed with an empty narrative.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning({"event": "tenk_read_failed", "path": str(path), "error": str(exc)})
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning({"event": "tenk_json_invalid", "path": str(path), "error": str(exc)})
        return ""
    if not isinstance(payload, dict):
        return ""

    parts: list[str] = []
    for key, value in cast("dict[str, object]", payload).items():
        if not isinstance(key, str):
            continue
        if not _is_narrative_section_key(key):
            continue
        text = _extract_section_text(value)
        if not text.strip():
            continue
        parts.append(f"## {key}\n{text}")

    body = "\n\n".join(parts).strip()
    if len(body) <= cap:
        return body
    # tail-preserving truncation so opening + closing context survive
    head_budget = cap - 250
    head = body[:head_budget].rstrip()
    tail = body[-200:].lstrip()
    return (
        f"{head}\n\n[...10-K narrative truncated for prompt-size budget — "
        f"{len(body) - cap} chars elided here...]\n\n{tail}"
    )


# ---------------------------------------------------------------------------
# LLM call + response parsing
# ---------------------------------------------------------------------------


def _build_proposal_prompt(
    *,
    ticker: str,
    thesis_anchor: str,
    recent_kpi_table: str,
    tenk_narrative: str,
    n_proposals: int,
) -> str:
    """Compose the proposal prompt body."""
    return f"""You propose load-bearing KPIs for an investor's thesis registry.

A "load-bearing KPI" is a metric whose movement materially confirms or refutes the bull case.
A "thesis-breaker" KPI is one whose movement past a specific threshold negates the bull case entirely.

Ticker: {ticker}

## Thesis anchor
{thesis_anchor.strip() or "(no thesis anchor on file)"}

## Recent KPI observations (last {_RECENT_KPI_QUARTERS} quarters)
{recent_kpi_table.strip() or "(no recent KPI observations on file)"}

## 10-K narrative excerpts (Risk Factors / MD&A / Operations / Business sections)
{tenk_narrative.strip() or "(no 10-K narrative available)"}

---

Task: Propose {n_proposals} load-bearing KPIs for {ticker}.

For each, return one JSON object with these keys:
  - kpi_name:            string. Prefer names already present in the Recent KPI observations table — use the same kpi_name string verbatim.
  - threshold_direction: "above" | "below" | null
  - threshold_value:     number | null
  - is_thesis_breaker:   true | false
  - rationale:           string. 2-3 sentences. Ground each threshold in the 10-K text or thesis anchor — cite the section by name.

Rules:
  1. Prefer KPIs already present in the Recent KPI observations table. Re-use the same kpi_name string verbatim so future kpi_facts JOINs work without aliasing.
  2. Set threshold_direction non-null ONLY when the rationale points to a specific level grounded in the 10-K text or thesis anchor.
  3. Set is_thesis_breaker=true ONLY when the 10-K Risk Factors or thesis anchor explicitly states a level past which the business model breaks. If you cannot point to a specific clause, set false.
  4. If you cannot ground a threshold in source text, set BOTH threshold_direction and threshold_value to null rather than inventing.

Return ONLY a JSON array of exactly {n_proposals} proposal objects. No commentary. No markdown fences. No prefatory prose.
"""


def _strip_json_fence(raw: str) -> str:
    """Strip optional ```json ... ``` fences the LLM occasionally adds."""
    stripped = raw.strip()
    if stripped.startswith("```"):
        return JSON_FENCE_RE.sub("", stripped).strip()
    return stripped


def _validate_direction(raw: object, *, where: str) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s == "":
            return None
        if s in _VALID_DIRECTIONS:
            return s
        if s in ("null", "none"):
            return None
    raise ValueError(f"{where}: threshold_direction must be 'above', 'below', or null; got {raw!r}")


def _validate_threshold_value(raw: object, *, where: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"{where}: threshold_value must be number or null; got bool {raw!r}")
    if isinstance(raw, (int, float)):
        return float(raw)
    raise ValueError(f"{where}: threshold_value must be number or null; got {type(raw).__name__}")


def _parse_proposal_response(raw: str) -> list[Proposal]:
    """Parse + structurally validate the LLM JSON-array response.

    Raises ``ValueError`` on any parse / shape failure — the caller's
    retry policy decides whether to re-prompt or surface.
    """
    payload = json.loads(_strip_json_fence(raw))
    if not isinstance(payload, list):
        raise ValueError(
            f"kpi_registry_proposal response is not a JSON array: got {type(payload).__name__}"
        )
    out: list[Proposal] = []
    for idx, entry in enumerate(cast("list[object]", payload)):
        where = f"proposal[{idx}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: not a JSON object; got {type(entry).__name__}")
        obj = cast("dict[str, object]", entry)
        kpi_name = obj.get("kpi_name")
        if not isinstance(kpi_name, str) or not kpi_name.strip():
            raise ValueError(f"{where}: missing 'kpi_name' string")
        rationale = obj.get("rationale")
        if not isinstance(rationale, str):
            raise ValueError(f"{where}: missing 'rationale' string")
        is_breaker_raw = obj.get("is_thesis_breaker")
        if not isinstance(is_breaker_raw, bool):
            raise ValueError(f"{where}: missing 'is_thesis_breaker' bool")
        out.append(
            Proposal(
                kpi_name=kpi_name.strip(),
                threshold_direction=_validate_direction(
                    obj.get("threshold_direction"), where=where
                ),
                threshold_value=_validate_threshold_value(obj.get("threshold_value"), where=where),
                is_thesis_breaker=is_breaker_raw,
                rationale=rationale.strip(),
            )
        )
    return out


def _call_llm_with_retry(prompt: str, *, ticker: str, model: str | None) -> list[Proposal]:
    """Call ``call_llm`` and parse. Retries once on a parse failure.

    Wraps the underlying call_llm exception as ``SeederLLMError`` so the
    CLI surface is a single failure type.
    """
    try:
        raw_first = call_llm(prompt, purpose=_ARTIFACT_PURPOSE, ticker=ticker, model=model)
    except Exception as exc:
        raise SeederLLMError(
            f"kpi_registry_proposal LLM call failed (initial attempt): {exc}"
        ) from exc
    try:
        return _parse_proposal_response(raw_first)
    except ValueError as first_exc:
        log.warning(
            {
                "event": "kpi_registry_proposal_parse_failed_retrying",
                "ticker": ticker,
                "error": str(first_exc),
            }
        )

    retry_prompt = _JSON_RETRY_PREAMBLE + prompt
    try:
        raw_retry = call_llm(retry_prompt, purpose=_ARTIFACT_PURPOSE, ticker=ticker, model=model)
    except Exception as exc:
        raise SeederLLMError(
            f"kpi_registry_proposal LLM call failed (retry attempt): {exc}"
        ) from exc
    try:
        return _parse_proposal_response(raw_retry)
    except ValueError as retry_exc:
        raise SeederLLMError(
            f"kpi_registry_proposal LLM returned malformed JSON twice: {retry_exc}"
        ) from retry_exc


# ---------------------------------------------------------------------------
# YAML render + read
# ---------------------------------------------------------------------------


class _ProposalDumper(yaml.SafeDumper):
    """SafeDumper subclass with literal-block formatting for multi-line strings.

    Subclassing is required so the custom string representer registered
    below doesn't bleed into the global SafeDumper's representers (which
    other yaml.safe_dump callers in the process would inherit).
    """


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_ProposalDumper.add_representer(str, _str_representer)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_yaml(
    *,
    ticker: str,
    model: str,
    proposals: Iterable[Proposal],
    generated_at: str | None = None,
) -> str:
    """Render the YAML draft. ``generated_at`` defaults to current UTC ISO.

    Public so the test suite can verify literal-block formatting on
    multi-line rationales — the property that makes the YAML editable in
    a real editor rather than a one-line jumble.
    """
    payload: dict[str, object] = {
        "ticker": ticker.upper(),
        "generated_at": generated_at or _now_iso(),
        "model": model,
        "proposals": [
            {
                "kpi_name": p.kpi_name,
                "threshold_direction": p.threshold_direction,
                "threshold_value": p.threshold_value,
                "is_thesis_breaker": p.is_thesis_breaker,
                "rationale": p.rationale,
                "status": "proposed",
                "notes": "",
            }
            for p in proposals
        ],
    }
    return yaml.dump(
        payload,
        Dumper=_ProposalDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        # Suppress implicit line-folding on long single-line strings — a folded
        # rationale looks like a YAML list to a casual reader, which makes the
        # file confusing to edit. Multi-line strings still use literal block (|).
        width=1_000_000,
    )


def _read_yaml_proposals(path: Path) -> dict[str, object]:
    """Read + lightly-validate a YAML proposal file. Returns the parsed dict.

    Raises ``SeederError`` when the root isn't a mapping or the required
    top-level keys are missing — the caller's job is to surface that to
    the user, not silently no-op.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SeederError(f"cannot read YAML at {path}: {exc}") from exc
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise SeederError(f"YAML root is not a mapping in {path}: got {type(payload).__name__}")
    obj = cast("dict[str, object]", payload)
    if not isinstance(obj.get("ticker"), str) or not cast("str", obj["ticker"]).strip():
        raise SeederError(f"YAML 'ticker' field missing or empty in {path}")
    if not isinstance(obj.get("proposals"), list):
        raise SeederError(f"YAML 'proposals' field missing or not a list in {path}")
    return obj


# ---------------------------------------------------------------------------
# --propose orchestration
# ---------------------------------------------------------------------------


def propose_for_ticker(
    *,
    ticker: str,
    db_path: Path,
    fmp_dir: Path,
    repo_root: Path,
    n_proposals: int = _DEFAULT_N_PROPOSALS,
    model: str | None = None,
) -> tuple[str, list[Proposal]] | None:
    """Generate LLM proposals for one ticker.

    Returns ``(model_used, proposals)`` on success; ``None`` when a
    precondition is missing (no thesis anchor, no 10-K). The caller
    decides whether to surface that as a hard error (single-ticker
    mode) or skip (--all mode).
    """
    ticker_u = ticker.upper()
    thesis_anchor = load_thesis_anchor(repo_root, ticker_u)
    if not thesis_anchor.strip():
        log.info({"event": "propose_skip_no_thesis_anchor", "ticker": ticker_u})
        return None

    tenk_path = _latest_10k_path(ticker_u, fmp_dir=fmp_dir)
    if tenk_path is None:
        log.info({"event": "propose_skip_no_10k", "ticker": ticker_u})
        return None
    tenk_narrative = _extract_10k_narrative(tenk_path)

    obs = _recent_kpi_observations(db_path, ticker_u)
    recent_kpi_table = _format_kpi_observations_table(obs)

    prompt = _build_proposal_prompt(
        ticker=ticker_u,
        thesis_anchor=thesis_anchor,
        recent_kpi_table=recent_kpi_table,
        tenk_narrative=tenk_narrative,
        n_proposals=n_proposals,
    )
    proposals = _call_llm_with_retry(prompt, ticker=ticker_u, model=model)
    used_model = model or "claude-sonnet-4-6"
    return used_model, proposals


def propose_and_write_yaml(
    *,
    ticker: str,
    out: Path,
    db_path: Path,
    fmp_dir: Path,
    repo_root: Path,
    n_proposals: int = _DEFAULT_N_PROPOSALS,
    model: str | None = None,
) -> bool:
    """Run propose for one ticker; write the YAML draft. Returns True when
    the YAML lands; False on skip (no thesis anchor / no 10-K)."""
    result = propose_for_ticker(
        ticker=ticker,
        db_path=db_path,
        fmp_dir=fmp_dir,
        repo_root=repo_root,
        n_proposals=n_proposals,
        model=model,
    )
    if result is None:
        return False
    used_model, proposals = result
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_yaml(ticker=ticker, model=used_model, proposals=proposals),
        encoding="utf-8",
    )
    log.info(
        {
            "event": "propose_yaml_written",
            "ticker": ticker.upper(),
            "path": str(out),
            "n_proposals": len(proposals),
        }
    )
    return True


# ---------------------------------------------------------------------------
# --write orchestration
# ---------------------------------------------------------------------------


def write_from_yaml(
    yaml_path: Path,
    *,
    db_path: Path | None = None,
    dry_run: bool = False,
) -> WriteSummary:
    """Read the YAML, upsert each accepted row via ``registry.upsert_kpi``.

    Rows whose ``status`` is anything other than ``accepted`` are skipped
    (this is how the user signals "not ready" / "rejected").
    """
    payload = _read_yaml_proposals(yaml_path)
    ticker = cast("str", payload["ticker"]).strip().upper()
    proposals_raw = cast("list[object]", payload["proposals"])

    accepted = 0
    written = 0
    skipped = 0
    errors = 0
    for idx, entry in enumerate(proposals_raw):
        where = f"{yaml_path}::proposals[{idx}]"
        if not isinstance(entry, dict):
            log.warning({"event": "write_skip_non_mapping", "where": where})
            errors += 1
            continue
        e = cast("dict[str, object]", entry)
        status_raw = e.get("status")
        status = (
            status_raw.strip().lower()
            if isinstance(status_raw, str) and status_raw.strip()
            else "proposed"
        )
        if status != _ACCEPTED_STATUS:
            skipped += 1
            log.info({"event": "write_skip_status", "where": where, "status": status})
            continue
        accepted += 1

        kpi_name_raw = e.get("kpi_name")
        if not isinstance(kpi_name_raw, str) or not kpi_name_raw.strip():
            log.warning({"event": "write_skip_no_kpi_name", "where": where})
            errors += 1
            continue
        kpi_name = kpi_name_raw.strip()

        try:
            direction = _validate_direction(e.get("threshold_direction"), where=where)
            value = _validate_threshold_value(e.get("threshold_value"), where=where)
        except ValueError as exc:
            log.warning({"event": "write_skip_malformed_threshold", "error": str(exc)})
            errors += 1
            continue

        is_breaker_raw = e.get("is_thesis_breaker")
        is_breaker = bool(is_breaker_raw) if isinstance(is_breaker_raw, bool) else False

        rationale_raw = e.get("rationale")
        rationale = rationale_raw if isinstance(rationale_raw, str) else ""
        notes_payload = rationale.strip()[:_PROPOSAL_RATIONALE_NOTES_CAP] or None

        if dry_run:
            log.info(
                {
                    "event": "write_dry_run",
                    "ticker": ticker,
                    "kpi_name": kpi_name,
                    "threshold_direction": direction,
                    "threshold_value": value,
                    "is_thesis_breaker": is_breaker,
                }
            )
            continue

        try:
            registry.upsert_kpi(
                user_id=_DEFAULT_USER_ID,
                ticker=ticker,
                kpi_name=kpi_name,
                threshold_direction=direction,
                threshold_value=value,
                is_thesis_breaker=is_breaker,
                scaffold_source=_SCAFFOLD_SOURCE,
                notes=notes_payload,
                db_path=db_path,
            )
        except Exception as exc:
            log.warning(
                {
                    "event": "write_upsert_failed",
                    "where": where,
                    "ticker": ticker,
                    "kpi_name": kpi_name,
                    "error": str(exc),
                }
            )
            errors += 1
            continue
        written += 1
        log.info(
            {
                "event": "write_row_persisted",
                "ticker": ticker,
                "kpi_name": kpi_name,
                "is_thesis_breaker": is_breaker,
            }
        )

    return WriteSummary(accepted=accepted, written=written, skipped=skipped, errors=errors)


# ---------------------------------------------------------------------------
# --auto orchestration (Opus, confidence-gated; no per-row human review)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AutoProposal:
    """One Opus-proposed KPI row for the ``--auto`` path.

    Differs from :class:`Proposal` (the ``--propose``/YAML shape): the model
    reports ``polarity`` (which direction is good for the thesis) and the code
    derives the *adverse* ``threshold_direction`` from it (correctness
    mechanism #2; see plan §5) — the model never sets the alert direction
    directly. Carries the grounding + confidence fields the gate reads.
    """

    kpi_name: str
    polarity: Polarity
    is_thesis_breaker: bool
    threshold_value: float | None
    threshold_grounded: bool
    grounding_citation: str
    confidence: float
    rationale: str


@dataclass(slots=True)
class AutoSeedSummary:
    """Per-run tally for the ``--auto`` CLI single-line output."""

    written: int = 0
    overwritten: int = 0
    preserved: int = 0
    skipped_only_new: int = 0
    reviewed: int = 0
    dropped: int = 0
    tickers: int = 0
    skipped_tickers: int = 0

    def merge(self, other: AutoSeedSummary) -> None:
        self.written += other.written
        self.overwritten += other.overwritten
        self.preserved += other.preserved
        self.skipped_only_new += other.skipped_only_new
        self.reviewed += other.reviewed
        self.dropped += other.dropped
        self.tickers += other.tickers
        self.skipped_tickers += other.skipped_tickers


def _validate_polarity(raw: object, *, where: str) -> Polarity:
    """Parse the LLM ``polarity`` field into the :class:`Polarity` enum.

    Raises ``ValueError`` on a missing/unknown value so the caller's retry
    re-prompts — polarity is load-bearing for the adverse-direction
    derivation, so a malformed value must not silently default.
    """
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _VALID_POLARITY_VALUES:
            return Polarity(s)
    raise ValueError(
        f"{where}: polarity must be one of {sorted(_VALID_POLARITY_VALUES)}; got {raw!r}"
    )


def _validate_confidence(raw: object, *, where: str) -> float:
    """Parse + clamp the LLM ``confidence`` to ``[0, 1]``.

    Rejects bool (an int subclass) and non-numbers (raise -> retry). An
    out-of-range number is clamped rather than rejected: confidence only gates
    auto-write, and the load-bearing checks (name, polarity, grounding) run
    regardless, so clamping cannot bypass them.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{where}: confidence must be a number in [0,1]; got {raw!r}")
    value = float(raw)
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _build_auto_proposal_prompt(
    *,
    ticker: str,
    thesis_anchor: str,
    recent_kpi_table: str,
    tenk_narrative: str,
    allowed_names: list[str],
    n_proposals: int,
) -> str:
    """Compose the Opus auto-proposal prompt.

    The candidate ``kpi_name`` set is CLOSED: the model must choose verbatim
    from ``allowed_names`` (correctness mechanism #1; plan §4) — names not in
    that set are dropped post-parse. The model reports ``polarity`` (not a
    direction); the code derives the adverse comparator.
    """
    names_block = "\n".join(f"  - {n}" for n in allowed_names)
    return f"""You propose load-bearing KPIs for an investor's thesis registry. These rows are seeded automatically with no human review, so be precise and conservative.

A "load-bearing KPI" is a metric whose movement materially confirms or refutes the bull case.
A "thesis-breaker" KPI is one whose movement past a specific threshold negates the bull case entirely.

Ticker: {ticker}

## Thesis anchor
{thesis_anchor.strip() or "(no thesis anchor on file)"}

## Recent KPI observations (last {_RECENT_KPI_QUARTERS} quarters)
{recent_kpi_table.strip() or "(no recent KPI observations on file)"}

## 10-K narrative excerpts (Risk Factors / MD&A / Operations / Business sections)
{tenk_narrative.strip() or "(no 10-K narrative available)"}

## Candidate KPI names (CLOSED SET — choose kpi_name ONLY from this list, copied character-for-character)
{names_block or "  (none — return an empty array)"}

---

Task: From the candidate list above, choose up to {n_proposals} load-bearing KPIs for {ticker}.

For each, return one JSON object with these keys:
  - kpi_name:           string. MUST be copied verbatim from the Candidate KPI names list. Do NOT invent, rephrase, abbreviate, or re-case. If no candidate is thesis-critical, return fewer (or an empty array).
  - polarity:           "higher_is_better" | "lower_is_better". Whether a HIGHER or LOWER value is good for the thesis. Do NOT specify any alert direction — the system derives it from polarity.
  - is_thesis_breaker:  true | false. true ONLY when source text names a level past which the business model breaks AND you can ground a threshold_value below.
  - threshold_value:    number | null. The level at which the KPI's deterioration matters. Set non-null ONLY when grounded in the 10-K or thesis anchor.
  - threshold_grounded: true | false. true ONLY when threshold_value is cited to specific source text.
  - grounding_citation: string. The 10-K section / thesis clause grounding the threshold or breaker status (e.g. "Item 1A Risk Factors: '...'"). Empty string if ungrounded.
  - confidence:         number in [0,1]. Joint certainty in (kpi choice + polarity + threshold). Reserve > 0.75 for grounded, unambiguous cases.
  - rationale:          string. 2-3 sentences grounding the proposal in the source text.

Rules:
  1. kpi_name MUST be from the candidate list, verbatim. A non-matching name is discarded.
  2. Report polarity, never a direction. The system fires breakers in the ADVERSE direction by construction.
  3. is_thesis_breaker=true REQUIRES a grounded, non-null threshold_value. If you cannot ground one, set is_thesis_breaker=false.
  4. If you cannot ground a threshold, set threshold_value=null and threshold_grounded=false rather than inventing a level.

Return ONLY a JSON array of proposal objects (at most {n_proposals}). No commentary. No markdown fences. No prefatory prose.
"""


def _parse_auto_proposal_response(raw: str) -> list[AutoProposal]:
    """Parse + structurally validate the Opus JSON-array response.

    Raises ``ValueError`` on any parse/shape failure (the caller retries once,
    then surfaces ``SeederLLMError``). ``threshold_grounded`` /
    ``grounding_citation`` fail closed (missing -> False / "") because an
    ungrounded breaker routes to review, the safe direction; ``kpi_name``,
    ``polarity``, ``confidence``, ``is_thesis_breaker`` and ``rationale`` are
    required.
    """
    payload = json.loads(_strip_json_fence(raw))
    if not isinstance(payload, list):
        raise ValueError(
            f"kpi_registry_auto_proposal response is not a JSON array: got {type(payload).__name__}"
        )
    out: list[AutoProposal] = []
    for idx, entry in enumerate(cast("list[object]", payload)):
        where = f"auto_proposal[{idx}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: not a JSON object; got {type(entry).__name__}")
        obj = cast("dict[str, object]", entry)
        kpi_name = obj.get("kpi_name")
        if not isinstance(kpi_name, str) or not kpi_name.strip():
            raise ValueError(f"{where}: missing 'kpi_name' string")
        rationale = obj.get("rationale")
        if not isinstance(rationale, str):
            raise ValueError(f"{where}: missing 'rationale' string")
        is_breaker_raw = obj.get("is_thesis_breaker")
        if not isinstance(is_breaker_raw, bool):
            raise ValueError(f"{where}: missing 'is_thesis_breaker' bool")
        grounded_raw = obj.get("threshold_grounded")
        grounded = grounded_raw if isinstance(grounded_raw, bool) else False
        citation_raw = obj.get("grounding_citation")
        citation = citation_raw.strip() if isinstance(citation_raw, str) else ""
        out.append(
            AutoProposal(
                kpi_name=kpi_name.strip(),
                polarity=_validate_polarity(obj.get("polarity"), where=where),
                is_thesis_breaker=is_breaker_raw,
                threshold_value=_validate_threshold_value(obj.get("threshold_value"), where=where),
                threshold_grounded=grounded,
                grounding_citation=citation,
                confidence=_validate_confidence(obj.get("confidence"), where=where),
                rationale=rationale.strip(),
            )
        )
    return out


def _call_auto_llm_with_retry(prompt: str, *, ticker: str, model: str | None) -> list[AutoProposal]:
    """Opus call + parse, retrying once on a parse failure. Mirrors
    ``_call_llm_with_retry`` but uses the auto purpose (-> Opus via LLM_MODELS)
    and the auto parser. Wraps failures as ``SeederLLMError``."""
    try:
        raw_first = call_llm(prompt, purpose=_AUTO_ARTIFACT_PURPOSE, ticker=ticker, model=model)
    except Exception as exc:
        raise SeederLLMError(
            f"kpi_registry_auto_proposal LLM call failed (initial attempt): {exc}"
        ) from exc
    try:
        return _parse_auto_proposal_response(raw_first)
    except ValueError as first_exc:
        log.warning(
            {
                "event": "kpi_registry_auto_proposal_parse_failed_retrying",
                "ticker": ticker,
                "error": str(first_exc),
            }
        )

    retry_prompt = _JSON_RETRY_PREAMBLE + prompt
    try:
        raw_retry = call_llm(
            retry_prompt, purpose=_AUTO_ARTIFACT_PURPOSE, ticker=ticker, model=model
        )
    except Exception as exc:
        raise SeederLLMError(
            f"kpi_registry_auto_proposal LLM call failed (retry attempt): {exc}"
        ) from exc
    try:
        return _parse_auto_proposal_response(raw_retry)
    except ValueError as retry_exc:
        raise SeederLLMError(
            f"kpi_registry_auto_proposal LLM returned malformed JSON twice: {retry_exc}"
        ) from retry_exc


# Gate decisions.
_DECISION_WRITE = "write"
_DECISION_REVIEW = "review"
_DECISION_DROP = "drop"


@dataclass(slots=True)
class GateOutcome:
    """Result of running the confidence gate over one :class:`AutoProposal`."""

    decision: str
    reason: str
    threshold_direction: str | None
    resolved_polarity: Polarity


def _gate_proposal(
    proposal: AutoProposal,
    *,
    allowed_names: set[str],
    min_confidence: float,
) -> GateOutcome:
    """Decide write / review / drop for one proposal (plan §3-§5, §7).

    Order: name validation (drop) -> polarity resolution (the known-KPI table
    is authoritative; a table-vs-LLM conflict -> review) -> confidence gate ->
    breaker grounding. The derived ``threshold_direction`` is the ADVERSE
    comparator for the resolved polarity (None when there is no threshold).
    """
    table_polarity = infer_polarity_from_table(proposal.kpi_name)
    # The deterministic table wins over the model when it has an opinion.
    resolved = table_polarity if table_polarity is not None else proposal.polarity
    direction = adverse_direction(resolved) if proposal.threshold_value is not None else None

    if proposal.kpi_name not in allowed_names:
        return GateOutcome(
            _DECISION_DROP,
            "kpi_name not in fireable catalog (no >=8-quarter series)",
            direction,
            resolved,
        )
    if table_polarity is not None and table_polarity is not proposal.polarity:
        return GateOutcome(
            _DECISION_REVIEW,
            f"polarity conflict: table={table_polarity.value} llm={proposal.polarity.value}",
            direction,
            resolved,
        )
    if proposal.confidence < min_confidence:
        return GateOutcome(
            _DECISION_REVIEW,
            f"confidence {proposal.confidence:.2f} < min {min_confidence:.2f}",
            direction,
            resolved,
        )
    if proposal.is_thesis_breaker and (
        not proposal.threshold_grounded or proposal.threshold_value is None
    ):
        return GateOutcome(
            _DECISION_REVIEW,
            "thesis-breaker without a grounded threshold",
            direction,
            resolved,
        )
    return GateOutcome(_DECISION_WRITE, "auto-write", direction, resolved)


def _auto_to_review_proposal(proposal: AutoProposal, outcome: GateOutcome) -> Proposal:
    """Map a gated-out :class:`AutoProposal` onto the ``--propose``/YAML
    :class:`Proposal` shape so the residual lands in the existing review file.

    The gate reason + confidence + citation are prepended to the rationale so
    the reviewer sees why it was routed; ``threshold_direction`` is pre-derived
    (adverse, table-authoritative) so the human only supplies a level (if any)
    and flips ``status`` to ``accepted``.
    """
    citation = f"; cite: {proposal.grounding_citation}" if proposal.grounding_citation else ""
    prefix = (
        f"[auto-seed -> review: {outcome.reason}; "
        f"confidence={proposal.confidence:.2f}; "
        f"grounded={proposal.threshold_grounded}{citation}] "
    )
    return Proposal(
        kpi_name=proposal.kpi_name,
        threshold_direction=outcome.threshold_direction,
        threshold_value=proposal.threshold_value,
        is_thesis_breaker=proposal.is_thesis_breaker,
        rationale=prefix + proposal.rationale,
    )


def auto_seed_ticker(
    *,
    ticker: str,
    db_path: Path,
    fmp_dir: Path,
    repo_root: Path,
    review_out: Path,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    max_auto_per_ticker: int = _DEFAULT_MAX_AUTO_PER_TICKER,
    only_new: bool = False,
    dry_run: bool = False,
    model: str | None = None,
) -> AutoSeedSummary:
    """Auto-seed one ticker: Opus proposes, the gate decides, confident rows
    upsert as ``llm_auto`` and the rest land in a review YAML.

    Skips (no LLM call) when there is no fireable catalog, no thesis anchor, or
    no 10-K — those tickers cannot be auto-seeded into fireable rows. Returns
    the per-ticker tally.
    """
    ticker_u = ticker.upper()
    summary = AutoSeedSummary()

    allowed = allowed_kpi_names(ticker_u, db_path)
    if not allowed:
        log.info({"event": "auto_seed_skip_no_fireable_kpis", "ticker": ticker_u})
        summary.skipped_tickers = 1
        return summary

    thesis_anchor = load_thesis_anchor(repo_root, ticker_u)
    if not thesis_anchor.strip():
        log.info({"event": "auto_seed_skip_no_thesis_anchor", "ticker": ticker_u})
        summary.skipped_tickers = 1
        return summary

    tenk_path = _latest_10k_path(ticker_u, fmp_dir=fmp_dir)
    if tenk_path is None:
        log.info({"event": "auto_seed_skip_no_10k", "ticker": ticker_u})
        summary.skipped_tickers = 1
        return summary
    tenk_narrative = _extract_10k_narrative(tenk_path)

    obs = _recent_kpi_observations(db_path, ticker_u)
    recent_kpi_table = _format_kpi_observations_table(obs)

    prompt = _build_auto_proposal_prompt(
        ticker=ticker_u,
        thesis_anchor=thesis_anchor,
        recent_kpi_table=recent_kpi_table,
        tenk_narrative=tenk_narrative,
        allowed_names=sorted(allowed),
        n_proposals=max_auto_per_ticker,
    )
    proposals = _call_auto_llm_with_retry(prompt, ticker=ticker_u, model=model)
    summary.tickers = 1

    existing_by_name = {
        row.kpi_name: row
        for row in registry.list_kpis(user_id=_DEFAULT_USER_ID, ticker=ticker_u, db_path=db_path)
    }

    review_rows: list[Proposal] = []
    write_candidates: list[tuple[AutoProposal, GateOutcome]] = []
    for proposal in proposals:
        outcome = _gate_proposal(proposal, allowed_names=allowed, min_confidence=min_confidence)
        if outcome.decision == _DECISION_DROP:
            summary.dropped += 1
            log.info(
                {
                    "event": "auto_seed_dropped",
                    "ticker": ticker_u,
                    "kpi_name": proposal.kpi_name,
                    "reason": outcome.reason,
                }
            )
            continue
        if outcome.decision == _DECISION_REVIEW:
            review_rows.append(_auto_to_review_proposal(proposal, outcome))
            continue
        write_candidates.append((proposal, outcome))

    # Enforce the per-ticker write cap; overflow (highest-confidence kept) is
    # routed to review rather than silently dropped (no silent caps).
    if len(write_candidates) > max_auto_per_ticker:
        write_candidates.sort(key=lambda pc: pc[0].confidence, reverse=True)
        overflow = write_candidates[max_auto_per_ticker:]
        write_candidates = write_candidates[:max_auto_per_ticker]
        for proposal, outcome in overflow:
            review_rows.append(
                _auto_to_review_proposal(
                    proposal,
                    GateOutcome(
                        _DECISION_REVIEW,
                        f"exceeds --max-auto-per-ticker={max_auto_per_ticker}",
                        outcome.threshold_direction,
                        outcome.resolved_polarity,
                    ),
                )
            )

    for proposal, outcome in write_candidates:
        existing = existing_by_name.get(proposal.kpi_name)
        if existing is not None:
            if existing.scaffold_source != _AUTO_SCAFFOLD_SOURCE:
                # Preserve manual / llm_proposal (and any non-auto) curation.
                summary.preserved += 1
                log.info(
                    {
                        "event": "auto_seed_preserved_curated",
                        "ticker": ticker_u,
                        "kpi_name": proposal.kpi_name,
                        "scaffold_source": existing.scaffold_source,
                    }
                )
                continue
            if only_new:
                summary.skipped_only_new += 1
                log.info(
                    {
                        "event": "auto_seed_skip_only_new",
                        "ticker": ticker_u,
                        "kpi_name": proposal.kpi_name,
                    }
                )
                continue

        notes_payload = proposal.rationale.strip()[:_PROPOSAL_RATIONALE_NOTES_CAP] or None
        if dry_run:
            log.info(
                {
                    "event": "auto_seed_dry_run_write",
                    "ticker": ticker_u,
                    "kpi_name": proposal.kpi_name,
                    "threshold_direction": outcome.threshold_direction,
                    "threshold_value": proposal.threshold_value,
                    "is_thesis_breaker": proposal.is_thesis_breaker,
                    "overwrite": existing is not None,
                }
            )
        else:
            _ = registry.upsert_kpi(
                user_id=_DEFAULT_USER_ID,
                ticker=ticker_u,
                kpi_name=proposal.kpi_name,
                threshold_direction=outcome.threshold_direction,
                threshold_value=proposal.threshold_value,
                is_thesis_breaker=proposal.is_thesis_breaker,
                scaffold_source=_AUTO_SCAFFOLD_SOURCE,
                notes=notes_payload,
                db_path=db_path,
            )
        if existing is not None:
            summary.overwritten += 1
        else:
            summary.written += 1

    if review_rows:
        summary.reviewed = len(review_rows)
        if dry_run:
            log.info(
                {
                    "event": "auto_seed_dry_run_review",
                    "ticker": ticker_u,
                    "n_review": len(review_rows),
                }
            )
        else:
            review_out.parent.mkdir(parents=True, exist_ok=True)
            review_out.write_text(
                render_yaml(
                    ticker=ticker_u,
                    model=model or "claude-opus-4-7",
                    proposals=review_rows,
                ),
                encoding="utf-8",
            )
            log.info(
                {
                    "event": "auto_seed_review_written",
                    "ticker": ticker_u,
                    "path": str(review_out),
                    "n_review": len(review_rows),
                }
            )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--propose",
        action="store_true",
        help="Generate LLM proposals; write YAML draft for human review.",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="Read YAML; upsert each row with status=accepted.",
    )
    mode.add_argument(
        "--auto",
        action="store_true",
        help="Opus auto-seed: gate proposals, write confident rows directly "
        "(scaffold_source=llm_auto), route the rest to a review YAML.",
    )

    parser.add_argument(
        "--ticker",
        help="Ticker for --propose (single-ticker mode). Mutually exclusive with --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_tickers",
        help="Iterate the portfolio tickers (--propose mode).",
    )
    parser.add_argument(
        "--out",
        help="Output YAML path for --propose --ticker. Default: scratch/proposals/<TICKER>.yaml",
    )
    parser.add_argument(
        "--out-dir",
        help="Output directory for --propose --all. Default: scratch/proposals/",
    )
    parser.add_argument(
        "--n-proposals",
        type=int,
        default=_DEFAULT_N_PROPOSALS,
        help=f"How many KPI proposals to request (default: {_DEFAULT_N_PROPOSALS}).",
    )
    parser.add_argument(
        "--in",
        dest="in_path",
        help="Input YAML path for --write.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="--write mode: log what WOULD be written, but don't call upsert_kpi.",
    )
    parser.add_argument(
        "--db-path",
        help="Override portfolio DB path (defaults to <repo_root>/data/portfolio.db).",
    )
    parser.add_argument(
        "--fmp-dir",
        help="Override FMP cache dir (defaults to <repo_root>/data/historical/fmp/).",
    )
    parser.add_argument(
        "--repo-root",
        help="Override the repo root used to find holdings JSON for the thesis anchor.",
    )
    parser.add_argument(
        "--model",
        help="LLM model id override (default: claude-sonnet-4-6 via call_llm; "
        "--auto defaults to claude-opus-4-7 via the registered purpose).",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=_DEFAULT_MIN_CONFIDENCE,
        help=f"--auto: minimum LLM confidence to auto-write a row "
        f"(default: {_DEFAULT_MIN_CONFIDENCE}). Below this -> review YAML.",
    )
    parser.add_argument(
        "--max-auto-per-ticker",
        type=int,
        default=_DEFAULT_MAX_AUTO_PER_TICKER,
        help=f"--auto: cap auto-written rows per ticker "
        f"(default: {_DEFAULT_MAX_AUTO_PER_TICKER}); overflow -> review YAML.",
    )
    parser.add_argument(
        "--only-new",
        action="store_true",
        help="--auto: never overwrite an existing llm_auto row (purely additive "
        "re-run). Curated rows are always preserved regardless of this flag.",
    )
    parser.add_argument(
        "--review-out",
        help="--auto: directory for the review YAMLs of gated-out rows "
        "(default: scratch/proposals/review/).",
    )
    return parser


def _print_summary(summary: WriteSummary) -> None:
    """Single-line tally so the CLI is greppable."""
    print(
        f"{{accepted: {summary.accepted}, written: {summary.written}, "
        f"skipped: {summary.skipped}, errors: {summary.errors}}}"
    )


def _resolve_repo_root(arg: str | None) -> Path:
    return Path(arg) if arg else _default_repo_root()


def _run_propose(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(cast("str | None", args.repo_root))
    db_path = Path(cast("str", args.db_path)) if args.db_path else _default_db_path(repo_root)
    fmp_dir = Path(cast("str", args.fmp_dir)) if args.fmp_dir else _default_fmp_dir(repo_root)

    n_proposals = int(args.n_proposals)
    model = cast("str | None", args.model)

    if args.all_tickers and args.ticker:
        print("error: --propose accepts either --ticker or --all, not both", file=sys.stderr)
        return 2
    if args.all_tickers:
        out_dir = Path(cast("str", args.out_dir)) if args.out_dir else _SCRATCH_DIR / "proposals"
        tickers = _portfolio_tickers(db_path)
        if not tickers:
            log.warning({"event": "propose_all_no_tickers", "db_path": str(db_path)})
            return 0
        for t in tickers:
            try:
                _ = propose_and_write_yaml(
                    ticker=t,
                    out=out_dir / f"{t.upper()}.yaml",
                    db_path=db_path,
                    fmp_dir=fmp_dir,
                    repo_root=repo_root,
                    n_proposals=n_proposals,
                    model=model,
                )
            except SeederLLMError as exc:
                log.error({"event": "propose_failed", "ticker": t, "error": str(exc)})
        return 0
    if not args.ticker:
        print(
            "error: --propose requires --ticker or --all",
            file=sys.stderr,
        )
        return 2
    ticker = cast("str", args.ticker)
    out = (
        Path(cast("str", args.out))
        if args.out
        else _SCRATCH_DIR / "proposals" / f"{ticker.upper()}.yaml"
    )
    try:
        wrote = propose_and_write_yaml(
            ticker=ticker,
            out=out,
            db_path=db_path,
            fmp_dir=fmp_dir,
            repo_root=repo_root,
            n_proposals=n_proposals,
            model=model,
        )
    except SeederLLMError as exc:
        log.error({"event": "propose_failed", "ticker": ticker, "error": str(exc)})
        return 1
    if not wrote:
        print(
            f"skipped {ticker.upper()} — see log for reason (missing thesis anchor or 10-K)",
            file=sys.stderr,
        )
        return 0
    print(f"wrote {out}")
    return 0


def _run_write(args: argparse.Namespace) -> int:
    if not args.in_path:
        print("error: --write requires --in <yaml>", file=sys.stderr)
        return 2
    yaml_path = Path(cast("str", args.in_path))
    db_path = Path(cast("str", args.db_path)) if args.db_path else None
    try:
        summary = write_from_yaml(
            yaml_path,
            db_path=db_path,
            dry_run=bool(args.dry_run),
        )
    except SeederError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_summary(summary)
    return 0 if summary.errors == 0 else 1


def _print_auto_summary(summary: AutoSeedSummary) -> None:
    """Single-line tally so the --auto CLI is greppable."""
    print(
        f"{{tickers: {summary.tickers}, written: {summary.written}, "
        f"overwritten: {summary.overwritten}, preserved: {summary.preserved}, "
        f"skipped_only_new: {summary.skipped_only_new}, reviewed: {summary.reviewed}, "
        f"dropped: {summary.dropped}, skipped_tickers: {summary.skipped_tickers}}}"
    )


def _run_auto(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(cast("str | None", args.repo_root))
    db_path = Path(cast("str", args.db_path)) if args.db_path else _default_db_path(repo_root)
    fmp_dir = Path(cast("str", args.fmp_dir)) if args.fmp_dir else _default_fmp_dir(repo_root)
    review_dir = (
        Path(cast("str", args.review_out))
        if args.review_out
        else _SCRATCH_DIR / "proposals" / "review"
    )
    min_confidence = float(args.min_confidence)
    max_auto = int(args.max_auto_per_ticker)
    only_new = bool(args.only_new)
    dry_run = bool(args.dry_run)
    model = cast("str | None", args.model)

    if args.all_tickers and args.ticker:
        print(
            "error: --auto accepts either --ticker or --all, not both",
            file=sys.stderr,
        )
        return 2
    if args.all_tickers:
        tickers = _portfolio_tickers(db_path)
        if not tickers:
            log.warning({"event": "auto_seed_all_no_tickers", "db_path": str(db_path)})
            return 0
    elif args.ticker:
        tickers = [cast("str", args.ticker)]
    else:
        print("error: --auto requires --ticker or --all", file=sys.stderr)
        return 2

    total = AutoSeedSummary()
    had_error = False
    for t in tickers:
        try:
            per = auto_seed_ticker(
                ticker=t,
                db_path=db_path,
                fmp_dir=fmp_dir,
                repo_root=repo_root,
                review_out=review_dir / f"{t.upper()}.yaml",
                min_confidence=min_confidence,
                max_auto_per_ticker=max_auto,
                only_new=only_new,
                dry_run=dry_run,
                model=model,
            )
        except SeederLLMError as exc:
            had_error = True
            log.error({"event": "auto_seed_failed", "ticker": t, "error": str(exc)})
            continue
        total.merge(per)
    _print_auto_summary(total)
    return 1 if had_error else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_parser().parse_args(argv)
    if args.propose:
        return _run_propose(args)
    if args.auto:
        return _run_auto(args)
    return _run_write(args)


if __name__ == "__main__":
    raise SystemExit(main())
