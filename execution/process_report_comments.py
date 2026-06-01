"""execution/process_report_comments.py
----------------------------------------
Loop through every OPEN comment on a (ticker, report_date) report and
take action based on the comment's `intent`.

  drop_kpi         → remove the named KPI from micro_thesis/holdings/<T>.json
  edit_thesis      → ask Opus to revise the thesis NARRATIVE paragraph;
                     when multiple edit_thesis comments are queued, they are
                     BATCHED into a single LLM call so the revised thesis is
                     coherent (not a serial accretion of clauses)
  edit_structured  → mutate STRUCTURED fields (break_rules, tier_1/2/3_kpis,
                     chart_priorities, competitive_watchlist) via a JSON-patch
                     LLM call. Applied per-comment.
  ask_question     → ask Opus the question (with ReportSpec context); append
                     the answer to follow_up_thread as an `assistant` turn
  fix_data         → log a TODO in directives/data_fixes.md (manual ticket)
  rewrite_section  → ask Opus to rewrite the targeted section (company
                     overview / valuation rationale / bear failure mode);
                     write the rewrite back to its cache
  platform_change  → cross-workspace bug or feature. Logged to
                     directives/platform_backlog.md (NOT data_fixes.md, which
                     is for single-ticker data corrections) so it doesn't get
                     confused with brief-level edits. Skipped by auto-rebuild
                     (no holdings JSON mutation).

Comments with `intent = null` get auto-classified via a Haiku bucketer first.

Five-pass apply flow:
  PASS 1 (always): classify intent + dry-run draft each comment. No writes.
  PASS 2 (apply only): a sequencer LLM call orders edit_thesis +
                   edit_structured items — narrative reframes before
                   refinements before structured edits.
  PASS 3 (apply only): execute the routes:
                   - All edit_thesis comments are BATCHED into ONE LLM call
                     that takes them all as input and produces ONE coherent
                     revised thesis (no more serial layering).
                   - edit_structured runs per-comment in sequenced order
                     against the (now-revised) holdings JSON.
                   - Other intents run individually.
  PASS 3.5 (apply only, conditional): if any edit_structured succeeded, a
                   synthesis LLM call rewrites the thesis prose so it
                   explicitly grounds in the (just-mutated) structured
                   fields — naming the actual KPI titles, break-rule
                   conditions, and chart priorities. Skipped when only
                   edit_thesis ran (the batched call already produced
                   coherent prose).
  PASS 4 (apply only, auto): if any holdings JSON mutation landed,
                   invalidate the bear_case cache, then chain through:
                     seed_kpi_definitions   sync kpi_definitions with the
                                            current tier_*_kpis (idempotent)
                     extract_kpis_from_summaries  LLM-extract values for
                                            newly-added KPIs from transcript
                                            summaries (idempotent skip on
                                            existing facts)
                     run_thesis_evaluator   refresh break_rule_evaluations
                                            with the latest kpi_facts
                     build_artifacts        regenerate workspace HTML.

Default mode is `--dry-run` (print the plan, don't touch files). Pass
`--apply` to execute. `--clear` drops all addressed + dismissed comments
after the run. `--no-sequence` bypasses the sequencing layer. `--no-rebuild`
skips the auto-rebuild (debug only — leaves the report stale vs JSON).

Usage:
  python execution/process_report_comments.py --ticker NU
  python execution/process_report_comments.py --ticker NU --apply
  python execution/process_report_comments.py --ticker NU --apply --clear
  python execution/process_report_comments.py --ticker NU --apply --no-sequence
  python execution/process_report_comments.py --ticker NU --apply --no-rebuild
  python execution/process_report_comments.py --all
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import comments  # noqa: E402
from comments import Comment, ThreadEntry  # noqa: E402
from llm_client import (  # noqa: E402
    JSON_FENCE_RE,
    call_llm,
    load_bear_anchor,
    load_ir_anchor,
    load_thesis_anchor,
)

# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def process_comments_for_ticker(
    repo_root: Path,
    ticker: str,
    report_date: date,
    apply: bool,
    clear: bool,
    sequence: bool = True,
    auto_rebuild: bool = True,
) -> dict[str, object]:
    """Four-pass processing: draft → sequence → apply (batched thesis) → rebuild.

    Pass 1 always runs (drafts every comment).
    Pass 2 + 3 + 4 only run when `apply=True`.

    Pass 3 batches ALL edit_thesis comments into one LLM call so the revised
    thesis is coherent, not a serial accretion. edit_structured comments run
    individually in sequenced order against the (newly-coherent) holdings JSON.

    Pass 4 invalidates downstream caches that depend on the thesis/structured
    fields (bear_case.json) and spawns build_artifacts to refresh the
    workspace HTML. Skipped via auto_rebuild=False.
    """
    open_comments = comments.list_comments(repo_root, ticker, report_date, status="open")

    # -------------------------------------------------------------------
    # PASS 1: classify intents + draft each comment's resolution (dry-run)
    # -------------------------------------------------------------------
    plan: list[dict[str, object]] = []
    for c in open_comments:
        intent = c.intent or _classify_intent(c)
        try:
            dry = _route(repo_root, ticker, report_date, c, intent, apply=False)
            plan.append(
                {
                    "comment": c,
                    "intent": intent,
                    "dry": dry,
                    "_error": None,
                }
            )
        except Exception as e:
            plan.append(
                {
                    "comment": c,
                    "intent": intent,
                    "dry": None,
                    "_error": f"{type(e).__name__}: {e}",
                }
            )

    if not apply:
        # Dry-run mode: emit the drafts as the legacy output shape.
        results = [_as_legacy_result(item) for item in plan]
        return {
            "ticker": ticker,
            "report_date": report_date.isoformat(),
            "processed": len(results),
            "applied": False,
            "cleared": 0,
            "sequence_used": False,
            "results": results,
        }

    # -------------------------------------------------------------------
    # PASS 2: sequence the plan (LLM-driven for edit_thesis)
    # -------------------------------------------------------------------
    sequence_decision: dict[str, object] = {"reordered": False, "rationale": None}
    if sequence:
        plan, sequence_decision = sequence_resolutions(plan, ticker)

    # -------------------------------------------------------------------
    # PASS 3: execute. edit_thesis items are BATCHED; others run in sequence.
    # -------------------------------------------------------------------
    results: list[dict[str, object]] = []

    # First, write back any reclassified intents to the on-disk comment
    # store so subsequent runs see them — this is independent of execution.
    for item in plan:
        c, intent = item["comment"], item["intent"]
        if intent != c.intent:
            comments.update_comment(repo_root, ticker, report_date, c.id, intent=intent)

    # Surface pass-1 errors first so they don't get lost.
    for item in plan:
        if item.get("_error") and item.get("dry") is None:
            c: Comment = item["comment"]
            results.append(
                {
                    "id": c.id,
                    "intent": item["intent"],
                    "status": "error",
                    "error": item["_error"],
                    "stage": "draft",
                }
            )

    # Batch all edit_thesis comments into ONE LLM call for a coherent rewrite.
    edit_thesis_items = [
        item
        for item in plan
        if item["intent"] == "edit_thesis" and not (item.get("_error") and item.get("dry") is None)
    ]
    if edit_thesis_items:
        batch_comments_list = [item["comment"] for item in edit_thesis_items]
        try:
            batch_resolution = _route_edit_thesis_batch(
                repo_root, ticker, batch_comments_list, apply=True
            )
            for c in batch_comments_list:
                comments.update_comment(
                    repo_root,
                    ticker,
                    report_date,
                    c.id,
                    status="addressed",
                    resolution_note=batch_resolution.get("summary", "applied"),
                )
                results.append(
                    {
                        "id": c.id,
                        "intent": "edit_thesis",
                        "status": "ok",
                        "resolution": batch_resolution,
                        "batched": True,
                    }
                )
        except Exception as e:
            for c in batch_comments_list:
                results.append(
                    {
                        "id": c.id,
                        "intent": "edit_thesis",
                        "status": "error",
                        "error": f"{type(e).__name__}: {e}",
                        "stage": "apply_batch",
                    }
                )

    # Now run the remaining (non-edit_thesis, non-error) items in plan order.
    # This preserves the sequencer's relative ordering of edit_structured vs
    # ask_question vs drop_kpi etc.
    for item in plan:
        c = item["comment"]
        intent = item["intent"]
        if item.get("_error") and item.get("dry") is None:
            continue  # already surfaced above
        if intent == "edit_thesis":
            continue  # already batched
        try:
            resolution = _route(repo_root, ticker, report_date, c, intent, apply=True)
            if intent == "ask_question" and resolution.get("answer"):
                comments.update_comment(
                    repo_root,
                    ticker,
                    report_date,
                    c.id,
                    append_thread=ThreadEntry(role="assistant", text=resolution["answer"]),
                    status="addressed",
                    resolution_note=resolution.get("summary", ""),
                )
            else:
                comments.update_comment(
                    repo_root,
                    ticker,
                    report_date,
                    c.id,
                    status="addressed",
                    resolution_note=resolution.get("summary", "applied"),
                )
            results.append(
                {
                    "id": c.id,
                    "intent": intent,
                    "status": "ok",
                    "resolution": resolution,
                }
            )
        except Exception as e:
            results.append(
                {
                    "id": c.id,
                    "intent": intent,
                    "status": "error",
                    "error": f"{type(e).__name__}: {e}",
                    "stage": "apply",
                }
            )

    cleared = 0
    if clear:
        cleared = comments.clear_addressed(repo_root, ticker, report_date)

    # -------------------------------------------------------------------
    # PASS 3.5: synthesis pass — align thesis prose with the structured fields
    # touched by edit_structured comments. Skipped when only edit_thesis ran
    # (the batched edit_thesis already produced a coherent prose).
    # -------------------------------------------------------------------
    synthesis_info: dict[str, object] | None = None
    applied_intents = {r.get("intent") for r in results if r.get("status") == "ok"}
    if "edit_structured" in applied_intents:
        synthesis_info = _synthesize_holdings_coherence(repo_root, ticker)

    # -------------------------------------------------------------------
    # PASS 4: auto-rebuild downstream artifacts if any holdings JSON mutation landed
    # -------------------------------------------------------------------
    rebuild_info: dict[str, object] | None = None
    if auto_rebuild:
        rebuild_info = maybe_auto_rebuild(repo_root, ticker, results)

    return {
        "ticker": ticker,
        "report_date": report_date.isoformat(),
        "processed": len(results),
        "applied": True,
        "cleared": cleared,
        "sequence_used": sequence,
        "sequence_decision": sequence_decision,
        "synthesis": synthesis_info,
        "auto_rebuild": rebuild_info,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Pass 3.5 — synthesis pass: align thesis prose with structured fields
# ---------------------------------------------------------------------------


# Fields the synthesis pass reads + may consider when grounding the prose.
# Listed in roughly priority order so the LLM has a clear hierarchy.
_SYNTHESIS_STRUCTURED_FIELDS: tuple[str, ...] = (
    "tier_1_kpis",
    "break_rules",
    "break_rules_soft",
    "chart_priorities",
    "tier_2_kpis",
    "tier_3_kpis",
    "thesis_breakers_qualitative",
)


def _synthesize_holdings_coherence(repo_root: Path, ticker: str) -> dict[str, object]:
    """Final pass: rewrite thesis prose to ground in the (updated) structured fields.

    Triggered when `edit_structured` has succeeded — the structured fields
    just changed, so the existing thesis prose may no longer reference the
    actual monitorables / break rules / KPI tier ordering. One LLM call
    reads the current holdings JSON, identifies the structured monitorables
    that should be visible in the prose, and rewrites the thesis to
    explicitly thread them through.

    The structured fields are NOT touched by this pass — they're the
    authoritative source of truth that the prose must align with.

    Returns a record with status + diff summary. On parse / LLM failure,
    leaves the thesis unchanged and reports `ran=False`.
    """
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return {"ran": False, "reason": "holdings JSON not found"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    current_thesis = (payload.get("thesis") or "").strip()
    if not current_thesis:
        return {"ran": False, "reason": "no thesis on file"}

    structured_view = {k: payload[k] for k in _SYNTHESIS_STRUCTURED_FIELDS if payload.get(k)}
    if not structured_view:
        return {"ran": False, "reason": "no structured fields to ground in"}

    prompt = f"""You are doing a coherence pass on an analyst's investment thesis for {ticker}.

CURRENT THESIS PROSE:
\"\"\"
{current_thesis}
\"\"\"

CURRENT STRUCTURED FIELDS (authoritative — do NOT change):
{json.dumps(structured_view, indent=2)}

TASK: Rewrite the thesis prose so it explicitly grounds in the structured fields above. The prose should:
  - Reference the actual KPI names from tier_1_kpis (e.g. \"ROE\", \"risk-adjusted NIM\", \"product penetration by country\") rather than vague proxies.
  - Name the actual break-rule conditions in plain language (e.g. \"if ROE sustains below 25%\", \"if revenue growth decelerates by more than 80% YoY\") in the thesis-killer / break-condition part.
  - Reflect the priority order of chart_priorities (the highest-priority monitorables should be featured first or most prominently).
  - Carry the analytical \"what's the bet\" framing — DON'T just enumerate fields. Keep the prose intentional.

REQUIREMENTS:
  - Same density and analytical voice as the original. Similar length, not longer.
  - Don't reference KPIs / rules that aren't in the structured fields.
  - Don't list every field exhaustively — pick the ones that carry the most analytical weight.
  - If the existing prose ALREADY grounds well in the structured fields, return it essentially unchanged with diff_summary describing it as a no-op.

Return ONLY a JSON object:
{{
  "revised_thesis": "<coherent thesis grounded in structured fields>",
  "diff_summary": "<one sentence: what shifted vs the prior thesis, or 'no material change'>"
}}

No markdown fence, no prose outside the JSON.
"""

    try:
        raw = call_llm(prompt, purpose="company_description").strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        parsed = json.loads(raw)
    except Exception as e:
        return {"ran": False, "reason": f"LLM/parse error: {type(e).__name__}: {e}"}

    revised = parsed.get("revised_thesis") if isinstance(parsed, dict) else None
    diff = (
        (parsed.get("diff_summary") or "(no diff summary)")
        if isinstance(parsed, dict)
        else "(no diff summary)"
    )
    if not isinstance(revised, str) or not revised.strip():
        return {"ran": False, "reason": "LLM did not return a usable revised_thesis"}

    # Only write if the revised thesis is materially different — avoids needless churn.
    if revised.strip() == current_thesis:
        return {
            "ran": True,
            "wrote": False,
            "diff_summary": "no change after synthesis",
            "structured_fields_considered": list(structured_view.keys()),
        }

    payload["thesis"] = revised
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {
        "ran": True,
        "wrote": True,
        "diff_summary": diff,
        "structured_fields_considered": list(structured_view.keys()),
        "new_thesis_length": len(revised),
    }


# ---------------------------------------------------------------------------
# Pass 4 — auto-rebuild downstream artifacts after holdings JSON changes
# ---------------------------------------------------------------------------

# Intents that mutate the holdings JSON. When any of these applied, the
# workspace HTML / sections.json / bear_case cache are stale and need refresh.
_HOLDINGS_MUTATING_INTENTS: frozenset[str] = frozenset(
    # All of these change DB or holdings-JSON state that the brief reads;
    # auto-rebuild fires whenever any of them succeed. `extract_kpi` writes
    # to kpi_facts (not holdings JSON), but still warrants a refresh so the
    # thesis tab's KPI ledger reflects the new value.
    {"edit_thesis", "edit_structured", "drop_kpi", "extract_kpi"}
)


def maybe_auto_rebuild(
    repo_root: Path, ticker: str, results: list[dict[str, object]]
) -> dict[str, object]:
    """If any holdings JSON mutation applied, refresh the full downstream chain.

    Two-step chain (matches `daily_fetch_and_brief`'s synthesize→publish slice):

      1. run_thesis_evaluator — re-evaluates `break_rules` against the latest
         kpi_facts time-series and writes a new `thesis_evaluations` row.
         Required because build_artifacts reads break_rule_evaluations from
         the DB, not from the holdings JSON; without this step, the brief's
         "Universal break rules" panel still shows the OLD evaluations even
         though the JSON has new rules.
      2. build_artifacts (workspace + enable-llm) — regenerates the HTML,
         sections JSON, DCF xlsx, and (with the bear_case cache invalidated)
         a fresh bear_case payload reflecting the new thesis.

    Returns a record describing each step's outcome. Best-effort: a failure
    in one step does not raise; results are already persisted regardless.
    """
    mutated = [
        r
        for r in results
        if r.get("status") == "ok" and r.get("intent") in _HOLDINGS_MUTATING_INTENTS
    ]
    if not mutated:
        return {"rebuilt": False, "reason": "no holdings-mutating comments applied"}

    # When edit_structured fired, the KPI roster in tier_*_kpis likely changed.
    # New KPI names need a fresh extraction attempt across ALL quarters — the
    # default per-period idempotency check would skip quarters where the first
    # auto-rebuild's LLM pass returned nothing for the new KPI (Haiku is
    # noisy on the first ask for unfamiliar names). --refresh is one wasted
    # re-extraction of stable KPIs in exchange for materially better
    # population of the new ones the analyst just added.
    structured_changed = any(
        r.get("intent") == "edit_structured" and r.get("status") == "ok" for r in results
    )
    refresh_kpi_facts = structured_changed

    # Invalidate the bear case cache so the next build regenerates from the new thesis.
    invalidated: list[str] = []
    bear_case_path = repo_root / "data" / "bear_case" / f"{ticker.upper()}.json"
    if bear_case_path.exists():
        try:
            bear_case_path.unlink()
            invalidated.append(str(bear_case_path.relative_to(repo_root)))
        except OSError:
            pass

    steps: list[dict[str, object]] = []

    # Step 1 — keep kpi_definitions in sync with the (possibly mutated)
    # tier_*_kpis arrays. Comment processor edits tier_1_kpis in the
    # holdings JSON but kpi_definitions is what the brief's KPI history
    # join uses. Without this, new tier-1 KPIs show empty history forever.
    # Idempotent: INSERT OR IGNORE on (ticker, name).
    steps.append(
        _spawn_step(
            repo_root,
            name="seed_kpi_definitions",
            cmd=[
                sys.executable,
                str(repo_root / "execution" / "seed_kpi_definitions.py"),
                "--ticker",
                ticker,
                "--repo-root",
                str(repo_root),
            ],
            timeout=60,
        )
    )

    # Step 2a — LLM-extract values from per-quarter EARNINGS-CALL summaries.
    # Idempotent on (ticker, period, KPI name) — values already in kpi_facts
    # are skipped, so adding new KPI names only re-prompts for the new ones.
    # When edit_structured fired this round, we --refresh so newly-added KPI
    # names get a clean re-attempt across all quarters.
    earnings_cmd = [
        sys.executable,
        str(repo_root / "execution" / "extract_kpis_from_summaries.py"),
        "--ticker",
        ticker,
        "--source",
        "earnings",
        "--repo-root",
        str(repo_root),
    ]
    if refresh_kpi_facts:
        earnings_cmd.append("--refresh")
    steps.append(
        _spawn_step(
            repo_root,
            name="extract_kpis_from_summaries(earnings)"
            + (" [refresh]" if refresh_kpi_facts else ""),
            cmd=earnings_cmd,
            timeout=900,
        )
    )

    # Step 2b — LLM-extract values from IR PRESS-RELEASE / PRESENTATION briefs.
    # These are typically richer with explicit numeric KPIs (NPL ratios,
    # capital adequacy, segment margins) than the qualitative earnings-call
    # summaries, so this step fills gaps that 2a couldn't.
    ir_cmd = [
        sys.executable,
        str(repo_root / "execution" / "extract_kpis_from_summaries.py"),
        "--ticker",
        ticker,
        "--source",
        "ir",
        "--repo-root",
        str(repo_root),
    ]
    if refresh_kpi_facts:
        ir_cmd.append("--refresh")
    steps.append(
        _spawn_step(
            repo_root,
            name="extract_kpis_from_summaries(ir)" + (" [refresh]" if refresh_kpi_facts else ""),
            cmd=ir_cmd,
            timeout=900,
        )
    )

    # Step 3 — re-evaluate break_rules against the (now populated) kpi_facts
    # so the brief's Universal break rules panel reflects the new rules
    # with their actual current values.
    steps.append(
        _spawn_step(
            repo_root,
            name="run_thesis_evaluator",
            cmd=[
                sys.executable,
                str(repo_root / "execution" / "run_thesis_evaluator.py"),
                "--ticker",
                ticker,
            ],
            timeout=300,
        )
    )

    # Step 4 — regenerate the workspace HTML from the updated DB + holdings.
    steps.append(
        _spawn_step(
            repo_root,
            name="build_artifacts",
            cmd=[
                sys.executable,
                str(repo_root / "execution" / "build_artifacts.py"),
                "--ticker",
                ticker,
                "--renderer",
                "workspace",
                "--enable-llm",
                "--repo-root",
                str(repo_root),
            ],
            timeout=900,
        )
    )

    overall_ok = all(s.get("exit_code") == 0 for s in steps)
    return {
        "rebuilt": overall_ok,
        "caches_invalidated": invalidated,
        "mutating_intents_applied": len(mutated),
        "steps": steps,
    }


def _spawn_step(repo_root: Path, *, name: str, cmd: list[str], timeout: int) -> dict[str, object]:
    """Run one subprocess step; return a structured record. Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"name": name, "exit_code": -1, "error": f"timeout after {timeout}s"}
    except OSError as e:
        return {"name": name, "exit_code": -1, "error": f"spawn failed: {e}"}
    return {
        "name": name,
        "exit_code": proc.returncode,
        "stderr_tail": (proc.stderr or "")[-400:] if proc.returncode != 0 else "",
    }


def _as_legacy_result(item: dict[str, object]) -> dict[str, object]:
    """Shape pass-1 plan items as the dry-run output the CLI used to print."""
    c: Comment = item["comment"]
    if item.get("_error") and item.get("dry") is None:
        return {
            "id": c.id,
            "intent": item["intent"],
            "status": "error",
            "error": item["_error"],
            "stage": "draft",
        }
    return {
        "id": c.id,
        "intent": item["intent"],
        "status": "ok",
        "resolution": item["dry"],
    }


# ---------------------------------------------------------------------------
# Sequencer (pass 2) — LLM-driven ordering for edit_thesis items
# ---------------------------------------------------------------------------


_SEQUENCEABLE_INTENTS: frozenset[str] = frozenset({"edit_thesis", "edit_structured"})


def sequence_resolutions(
    plan: list[dict[str, object]], ticker: str
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Reorder edit_thesis + edit_structured items by impact scope.

    Calls a single LLM to rank the editable subset. Falls back to the
    original order on any failure or invalid permutation. Returns
    (reordered_plan, decision_record) for telemetry. Non-editable items
    (ask_question, fix_data, drop_kpi, rewrite_section) keep their original
    positions relative to each other.

    Ordering rule the LLM is asked to apply:
      1. Foundational reframings of the thesis NARRATIVE first (edit_thesis,
         broad scope) — they reset context for later prose edits
      2. Narrative refinements (edit_thesis, narrower) — build on the reframe
      3. Structured-field edits (edit_structured) — last, since they mutate
         break_rules / KPI tiers and don't depend on narrative wording
    """
    edit_positions = [
        i
        for i, item in enumerate(plan)
        if item["intent"] in _SEQUENCEABLE_INTENTS and item.get("dry") is not None
    ]
    if len(edit_positions) <= 1:
        return plan, {"reordered": False, "rationale": "≤1 sequenceable edit, nothing to sequence"}

    # Build the prompt body
    lines: list[str] = []
    for idx, pos in enumerate(edit_positions):
        item = plan[pos]
        c: Comment = item["comment"]
        dry = item["dry"] or {}
        lines.append(
            f"[{idx}] comment={c.id} intent={item['intent']} anchor={c.anchor.key!r}\n"
            f"     user_said: {c.comment[:280]}\n"
            f"     proposed_change: {dry.get('diff_summary', '?')}"
        )

    prompt = f"""You are ordering {len(edit_positions)} proposed edits for {ticker} so they apply in the right sequence.

CRITICAL: each edit reads the CURRENT state and overwrites a piece of the holdings JSON. Later edits build on earlier ones. Order matters.

Two intent types:
  - edit_thesis: rewrites the thesis NARRATIVE paragraph
  - edit_structured: mutates structured fields (break_rules, tier_1/2/3_kpis, chart_priorities)

Apply in this priority:
  1. edit_thesis foundational reframings first (broad narrative resets)
  2. edit_thesis narrative refinements (adjust emphasis / framing within the new narrative)
  3. edit_structured (mutate the JSON arrays — they don't depend on narrative wording)

Within each tier, prefer edits that are more sweeping over edits that are narrower.

PROPOSED EDITS:
{chr(10).join(lines)}

Return ONLY a JSON object of the form: {{"order": [int, int, ...], "rationale": "one-sentence why this order"}}
The order array must be a permutation of {list(range(len(edit_positions)))} (0-indexed positions of the EDITS above), giving the apply order.
"""

    try:
        raw = call_llm(prompt, purpose="intake_classifier").strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        parsed = json.loads(raw)
        new_order = parsed.get("order")
        rationale = str(parsed.get("rationale", ""))
    except Exception as e:
        return plan, {
            "reordered": False,
            "rationale": f"sequencer fallback (error): {type(e).__name__}: {e}",
        }

    if not isinstance(new_order, list) or sorted(new_order) != list(range(len(edit_positions))):
        return plan, {
            "reordered": False,
            "rationale": "sequencer fallback (invalid permutation from LLM)",
        }

    # Rebuild plan: keep non-edit_thesis items where they are, slot edit_thesis
    # items at their original positions but in the LLM-determined order.
    reordered_edits = [plan[edit_positions[i]] for i in new_order]
    new_plan = list(plan)
    for pos, item in zip(edit_positions, reordered_edits, strict=True):
        new_plan[pos] = item

    # Detect if the order actually changed (LLM could legitimately return identity)
    changed = any(new_plan[pos] is not plan[pos] for pos in edit_positions)
    return new_plan, {
        "reordered": changed,
        "rationale": rationale or "(LLM returned no rationale)",
        "edit_count": len(edit_positions),
        "order": new_order,
    }


def _route(
    repo_root: Path,
    ticker: str,
    report_date: date,
    c: Comment,
    intent: str | None,
    apply: bool,
) -> dict[str, object]:
    if intent == "drop_kpi":
        return _route_drop_kpi(repo_root, ticker, c, apply=apply)
    if intent == "edit_thesis":
        return _route_edit_thesis(repo_root, ticker, c, apply=apply)
    if intent == "edit_structured":
        return _route_edit_structured(repo_root, ticker, c, apply=apply)
    if intent == "extract_kpi":
        return _route_extract_kpi(repo_root, ticker, c, apply=apply)
    if intent == "ask_question":
        return _route_ask_question(repo_root, ticker, report_date, c)
    if intent == "fix_data":
        return _route_fix_data(repo_root, ticker, c, apply=apply)
    if intent == "rewrite_section":
        return _route_rewrite_section(repo_root, ticker, c, apply=apply)
    if intent == "platform_change":
        return _route_platform_change(repo_root, ticker, c, apply=apply)
    return {"summary": f"no router for intent={intent!r} — left as open"}


# Whitelist of fields the structured-edit route is allowed to mutate. Any
# field outside this set returned by the LLM is silently dropped on the merge
# so the LLM can't accidentally clobber thesis, ticker, name, wacc, etc.
_STRUCTURED_EDITABLE_FIELDS: tuple[str, ...] = (
    "tier_1_kpis",
    "tier_2_kpis",
    "tier_3_kpis",
    "break_rules",
    "break_rules_soft",
    "chart_priorities",
    "competitive_watchlist",
    "thesis_breakers_qualitative",
)


# ---------------------------------------------------------------------------
# Intent routers
# ---------------------------------------------------------------------------


def _route_drop_kpi(repo_root: Path, ticker: str, c: Comment, apply: bool) -> dict[str, object]:
    """Remove the named KPI from micro_thesis/holdings/<T>.json."""
    if c.anchor.type != "kpi_ledger_row":
        return {"summary": f"drop_kpi: anchor.type={c.anchor.type} is not a KPI row"}
    kpi_name = c.anchor.key
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return {"summary": "drop_kpi: holdings JSON not found"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    removed_from: list[str] = []
    for tier_key in ("tier_1_kpis", "tier_2_kpis", "tier_3_kpis"):
        rows = payload.get(tier_key)
        if not isinstance(rows, list):
            continue
        kept = [r for r in rows if not (isinstance(r, dict) and r.get("name") == kpi_name)]
        if len(kept) != len(rows):
            removed_from.append(tier_key)
            payload[tier_key] = kept
    if not removed_from:
        return {"summary": f"drop_kpi: KPI {kpi_name!r} not found in any tier"}
    if apply:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {
        "summary": f"drop_kpi: removed {kpi_name!r} from {','.join(removed_from)}",
        "kpi": kpi_name,
        "tiers_touched": removed_from,
        "dry_run": not apply,
    }


def _route_edit_thesis(repo_root: Path, ticker: str, c: Comment, apply: bool) -> dict[str, object]:
    """Ask Opus to revise the thesis text using the comment as guidance."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return {"summary": "edit_thesis: holdings JSON not found"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    current_thesis = payload.get("thesis") or payload.get("thesis_full") or ""
    if not current_thesis:
        return {"summary": "edit_thesis: no thesis field on file"}
    prompt = f"""You are revising an analyst's investment thesis for {ticker}.

CURRENT THESIS:
\"\"\"
{current_thesis}
\"\"\"

ANALYST COMMENT (the revision they want):
\"\"\"
{c.comment}
\"\"\"

Return a JSON object with EXACTLY these fields:
{{
  "revised_thesis": "<the revised thesis paragraph; same density and analytical voice as the original; incorporate the analyst's feedback>",
  "diff_summary": "<one-sentence note on what changed>"
}}

Return ONLY the JSON object. No markdown fence, no prose.
"""
    raw = call_llm(prompt, purpose="company_description").strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    parsed = json.loads(raw)
    revised = parsed.get("revised_thesis")
    diff = parsed.get("diff_summary") or "(no diff summary)"
    if not isinstance(revised, str):
        return {"summary": "edit_thesis: LLM did not return a revised_thesis string"}
    if apply:
        payload["thesis"] = revised
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {
        "summary": f"edit_thesis: {diff}",
        "diff_summary": diff,
        "revised_preview": revised[:200] + ("..." if len(revised) > 200 else ""),
        "revised_full": revised,  # full revised thesis for the dashboard diff preview
        "dry_run": not apply,
    }


def _route_edit_thesis_batch(
    repo_root: Path, ticker: str, comments_list: list[Comment], apply: bool
) -> dict[str, object]:
    """One LLM call that revises the thesis incorporating ALL pending edit_thesis comments.

    Replaces the previous serial-rewrite behavior where each edit_thesis
    comment triggered an independent LLM call that took the prior call's
    output as input — the result accreted clauses without holistic rewrite,
    producing run-on, redundant theses. Batching lets the LLM see every
    requested change at once and produce ONE coherent thesis.

    Single-comment case delegates to `_route_edit_thesis` for simplicity.
    """
    if len(comments_list) == 1:
        return _route_edit_thesis(repo_root, ticker, comments_list[0], apply=apply)

    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return {"summary": "edit_thesis_batch: holdings JSON not found"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    current_thesis = payload.get("thesis") or payload.get("thesis_full") or ""
    if not current_thesis:
        return {"summary": "edit_thesis_batch: no thesis field on file"}

    edits_text = "\n\n".join(
        f"### Comment {i + 1} (id={c.id}, anchor={c.anchor.key!r})\n{c.comment}"
        for i, c in enumerate(comments_list)
    )

    prompt = f"""You are revising an analyst's investment thesis for {ticker}, incorporating {len(comments_list)} analyst comments at once.

CURRENT THESIS:
\"\"\"
{current_thesis}
\"\"\"

ANALYST COMMENTS (incorporate ALL of these into ONE coherent revised thesis):
{edits_text}

REQUIREMENTS:
- Produce ONE coherent revised thesis — same density and analytical voice as the original. Aim for similar length, not longer.
- Every comment's intent should be visibly addressed, but the result must read as a UNIFIED thesis, NOT a serial list of edits.
- TIGHTEN, CONSOLIDATE, and RESTRUCTURE as needed. Don't just append clauses or paste each edit verbatim. Cut redundancy aggressively — if two comments overlap (e.g. both reference ROE), express the merged point once.
- The structured fields (KPI tiers, break_rules, chart_priorities) will be handled separately. Don't enumerate quantitative thresholds in the prose unless they're truly thesis-defining — keep the thesis at the "what's the bet and what could break it" altitude.
- If two comments conflict, defer to the more specific / quantitative one and flag the conflict in diff_summary.

Return a JSON object with EXACTLY these fields:
{{
  "revised_thesis": "<coherent, dense, unified thesis>",
  "diff_summary": "<one-sentence summary of what overall changed vs the original>"
}}

Return ONLY the JSON object. No markdown fence, no prose.
"""
    raw = call_llm(prompt, purpose="company_description").strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    parsed = json.loads(raw)
    revised = parsed.get("revised_thesis")
    diff = parsed.get("diff_summary") or "(no diff summary)"
    if not isinstance(revised, str):
        return {"summary": "edit_thesis_batch: LLM did not return a revised_thesis string"}

    if apply:
        payload["thesis"] = revised
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "summary": f"edit_thesis_batch ({len(comments_list)} comments): {diff}",
        "diff_summary": diff,
        "revised_preview": revised[:300] + ("..." if len(revised) > 300 else ""),
        "revised_full": revised,  # full revised thesis for the dashboard diff preview
        "batched_count": len(comments_list),
        "batched_ids": [c.id for c in comments_list],
        "dry_run": not apply,
    }


def _route_edit_structured(
    repo_root: Path, ticker: str, c: Comment, apply: bool
) -> dict[str, object]:
    """Edit structured fields of the holdings JSON (break_rules / KPI tiers / etc.).

    Distinct from `edit_thesis` (which mutates only the thesis narrative).
    The LLM is given the current structured payload + the comment, and asked
    to return a JSON patch — a partial dict containing only the fields it
    wants to update with their full new values. Returned keys outside the
    whitelist are silently dropped on the merge.
    """
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return {"summary": "edit_structured: holdings JSON not found"}
    payload = json.loads(path.read_text(encoding="utf-8"))

    # Slice the payload down to the editable fields for the prompt.
    editable_snapshot = {k: payload[k] for k in _STRUCTURED_EDITABLE_FIELDS if k in payload}
    thesis_excerpt = (payload.get("thesis") or "")[:600]

    prompt = f"""You are editing the STRUCTURED fields of an investment thesis JSON for {ticker}.

The thesis NARRATIVE (already finalized — do not change) reads:
\"\"\"
{thesis_excerpt}{"..." if len(payload.get("thesis") or "") > 600 else ""}
\"\"\"

CURRENT STRUCTURED FIELDS (you may edit any subset):
{json.dumps(editable_snapshot, indent=2)}

ANALYST COMMENT (the change they want):
\"\"\"
{c.comment}
\"\"\"

Anchor type: {c.anchor.type}
Anchor key: {c.anchor.key}

Editable fields (return any subset; each field is REPLACED in full by what you return):
- tier_1_kpis / tier_2_kpis / tier_3_kpis: list of KPI dicts. Each: {{"name": str, "current": str|null, "prior": str|null, "yoy": str|null, "status": str, "break_condition": str, "source": str}}
- break_rules: list of structured rule dicts. **PREFER THIS** for analyst-authored tripwires with a definite numeric threshold + comparator (e.g. "ROE < 25% for 2Q", "revenue YoY < -80%"). These get auto-evaluated against kpi_facts every cycle and rendered in the workspace "Universal break rules" panel. Each: {{"rule_id": str, "kpi_name": str, "comparator": str ("lt"|"gt"|"le"|"ge"|"eq"), "threshold": number, "unit": str ("percent"|"usd"|"ratio"|"count"), "consecutive_periods": int, "narrative": str (HARD LIMIT 500 chars — keep terse, one analytical sentence)}}
- break_rules_soft: same shape as break_rules. **Use ONLY for fuzzy/qualitative tripwires** that can't yet be expressed numerically. The soft-rule evaluator is not yet wired up, so these are dormant — don't put numeric analyst tripwires here even if the language was "soft" (e.g. "should be monitored", "if dipping below"); those still belong in break_rules with a definite threshold.
- chart_priorities: list of KPI name strings (order matters)
- competitive_watchlist: list of competitor name strings
- thesis_breakers_qualitative: list of structured-qualitative breaker dicts (free shape)

DO NOT touch: ticker, name, thesis, verdict, verdict_color, key_driver, wacc, mos_bar, dcf_defaults, last_updated. They are not in the snapshot above — don't introduce them.

Return ONLY a JSON object containing the fields you want to UPDATE (each with its FULL new value) plus a "diff_summary" string. Example:
{{
  "break_rules": [...new full array...],
  "tier_1_kpis": [...new full array...],
  "diff_summary": "Added ROE <25% sustained break rule and promoted ROE to tier_1_kpis position 1."
}}

If the comment is ambiguous or doesn't map to structured changes, return {{"diff_summary": "no structured changes warranted"}} with no field updates.

Return ONLY the JSON object. No markdown fence, no prose.
"""
    raw = call_llm(prompt, purpose="company_description").strip()
    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return {"summary": "edit_structured: LLM did not return a JSON object"}

    diff = str(parsed.get("diff_summary") or "(no diff summary)")

    # Filter to the whitelist + merge.
    touched: list[str] = []
    for field in _STRUCTURED_EDITABLE_FIELDS:
        if field in parsed:
            value = parsed[field]
            # Defensive: BreakRule's `narrative` is capped at 500 chars by the
            # Pydantic model — truncate over-long LLM-generated entries so
            # the thesis_evaluator load doesn't crash. Same for break_rules_soft.
            if field in ("break_rules", "break_rules_soft") and isinstance(value, list):
                for rule in value:
                    if isinstance(rule, dict):
                        narr = rule.get("narrative")
                        if isinstance(narr, str) and len(narr) > 500:
                            rule["narrative"] = narr[:480].rstrip() + "..."
            payload[field] = value
            touched.append(field)

    if not touched:
        return {
            "summary": f"edit_structured: {diff} (no whitelisted fields returned)",
            "diff_summary": diff,
            "fields_touched": [],
            "dry_run": not apply,
        }

    if apply:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "summary": f"edit_structured: {diff}",
        "diff_summary": diff,
        "fields_touched": touched,
        "updated_fields": {f: payload[f] for f in touched},  # new values for the diff preview
        "dry_run": not apply,
    }


def preview_thesis_edits(
    repo_root: Path,
    ticker: str,
    report_date: date,
    *,
    comment_ids: list[str] | None = None,
) -> dict[str, object]:
    """Dry-run preview of the open edit_thesis / edit_structured comments.

    Returns the before/after thesis (+ a unified diff) and the per-comment
    structured-field changes WITHOUT writing the holdings JSON — it calls the
    same routers the apply path uses (apply=False), so what you preview is what
    Apply produces (modulo LLM nondeterminism; the prompt is identical).

    Raises whatever the routers raise (e.g. LLMBudgetExceeded on a hard cap) —
    the caller (the dashboard endpoint) decides propagate-vs-degrade via
    `is_hard_stop`.
    """
    t = ticker.upper()
    path = repo_root / "micro_thesis" / "holdings" / f"{t}.json"
    before_thesis = ""
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            before_thesis = str(payload.get("thesis") or payload.get("thesis_full") or "")
        except (OSError, ValueError, AttributeError):
            before_thesis = ""

    open_comments = comments.list_comments(repo_root, t, report_date, status="open")
    relevant = [c for c in open_comments if (c.intent or "") in ("edit_thesis", "edit_structured")]
    if comment_ids is not None:
        wanted = set(comment_ids)
        relevant = [c for c in relevant if c.id in wanted]

    thesis_comments = [c for c in relevant if c.intent == "edit_thesis"]
    structured_comments = [c for c in relevant if c.intent == "edit_structured"]

    result: dict[str, object] = {
        "ticker": t,
        "report_date": report_date.isoformat(),
        "comment_ids": [c.id for c in relevant],
        "intents_covered": sorted({c.intent for c in relevant if c.intent}),
        "before_thesis": before_thesis,
        "after_thesis": None,
        "thesis_diff": None,
        "diff_summary": None,
        "structured": [],
    }
    if not relevant:
        result["note"] = "no open edit_thesis / edit_structured comments"
        return result

    if thesis_comments:
        r = _route_edit_thesis_batch(repo_root, t, thesis_comments, apply=False)
        result["diff_summary"] = r.get("diff_summary")
        revised = r.get("revised_full")
        if isinstance(revised, str):
            result["after_thesis"] = revised
            result["thesis_diff"] = "".join(
                difflib.unified_diff(
                    before_thesis.splitlines(keepends=True),
                    revised.splitlines(keepends=True),
                    fromfile="thesis (before)",
                    tofile="thesis (after)",
                )
            )
        else:
            result["thesis_note"] = str(r.get("summary") or "no thesis revision produced")

    structured_out: list[dict[str, object]] = []
    for c in structured_comments:
        r = _route_edit_structured(repo_root, t, c, apply=False)
        structured_out.append(
            {
                "comment_id": c.id,
                "diff_summary": r.get("diff_summary"),
                "fields_touched": r.get("fields_touched", []),
                "updated_fields": r.get("updated_fields", {}),
            }
        )
    result["structured"] = structured_out
    return result


def _route_extract_kpi(repo_root: Path, ticker: str, c: Comment, apply: bool) -> dict[str, object]:
    """Comment-driven KPI extraction.

    The user comments on a `kpi_ledger_row` anchor (or any tier_*_kpi name)
    and supplies either a verbatim quote from a transcript / IR doc / filing
    OR a direct value statement. The LLM parses the comment into
    (value, period_end, source_excerpt) and persists into kpi_facts.

    Persistence path:
      1. Resolve anchor.key → kpi_definitions row (must already exist; the
         auto-rebuild's seed_kpi_definitions step keeps this in sync with
         tier_*_kpis).
      2. INSERT a synthetic `documents` row of type ANALYST_COMMENT / source
         ANALYST_COMMENT, file_path pointing at the comment's id, so the
         NOT-NULL source_doc_id FK on kpi_facts is satisfied.
      3. UPSERT kpi_facts row keyed on (ticker, period_end, fiscal_period_type,
         kpi_definition_id) with the LLM-parsed value, unit, and
         source_excerpt = the verbatim quote / user statement.

    On any LLM/parse/DB failure, returns a `summary` describing what went
    wrong without raising.
    """
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return {"summary": "extract_kpi: portfolio.db not found"}

    # Resolve the targeted KPI by name. Anchor key is the most reliable signal
    # (the comment was attached to a specific row); fall back to scanning the
    # comment body for a KPI name match.
    kpi_name = (c.anchor.key or "").strip() if c.anchor else ""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        kdef_row = conn.execute(
            "SELECT id, name, unit FROM kpi_definitions WHERE ticker = ? AND name = ?",
            (ticker.upper(), kpi_name),
        ).fetchone()
        if kdef_row is None:
            return {
                "summary": (
                    f"extract_kpi: no kpi_definitions row for ticker={ticker!r} "
                    f"name={kpi_name!r}. Run seed_kpi_definitions first or fix the anchor."
                ),
            }

        # LLM parses the comment for (value, period_end, source_excerpt).
        prompt = f"""You are extracting a KPI value the analyst supplied in a workspace comment.

TICKER: {ticker.upper()}
KPI: {kpi_name} (unit: {kdef_row["unit"]})

ANALYST COMMENT (this is the source — it may contain a verbatim quote from a
transcript / IR doc / filing, OR a direct value statement, OR both):
\"\"\"
{c.comment}
\"\"\"

Extract:
  - value: the numeric value the analyst is asserting for this KPI
  - period_end: the fiscal-period end date (YYYY-MM-DD). Infer from context
    (e.g. "Q3 2025" → 2025-09-30, "Q4 2024" → 2024-12-31). If the comment
    references "latest" or "current" without naming a quarter, return null.
  - source_excerpt: the verbatim quote the value came from (keep ≤ 500 chars).
    If the analyst stated the value directly without a quote, use the
    analyst's exact words.
  - fiscal_period_type: "quarter" by default, "annual" only when the comment
    is clearly about a fiscal year.

Return ONLY a JSON object:
{{
  "value": <number or null>,
  "period_end": "<YYYY-MM-DD or null>",
  "fiscal_period_type": "quarter" | "annual",
  "source_excerpt": "<verbatim quote or analyst statement, ≤500 chars>"
}}

If the comment doesn't contain enough information to extract a value, return
{{"value": null, "source_excerpt": "<reason>"}} with no other fields. No
markdown fence, no prose.
"""
        raw = call_llm(prompt, purpose="company_description").strip()
        if raw.startswith("```"):
            raw = JSON_FENCE_RE.sub("", raw).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {"summary": "extract_kpi: LLM did not return a JSON object"}

        value = parsed.get("value")
        period_end_str = parsed.get("period_end")
        excerpt = str(parsed.get("source_excerpt") or "")[:1024]
        fiscal_period_type = str(parsed.get("fiscal_period_type") or "quarter").lower()
        if fiscal_period_type not in ("quarter", "annual"):
            fiscal_period_type = "quarter"

        if value is None:
            return {
                "summary": f"extract_kpi: LLM couldn't extract a value: {excerpt or '(no reason given)'}",
                "extracted_value": None,
            }
        if not isinstance(value, (int, float)):
            return {
                "summary": f"extract_kpi: LLM returned non-numeric value: {value!r}",
            }
        if not period_end_str or not isinstance(period_end_str, str):
            return {
                "summary": (
                    "extract_kpi: LLM couldn't infer the fiscal period. "
                    "Re-comment with explicit quarter context (e.g. 'Q3 2025')."
                ),
            }
        try:
            period_end_dt = datetime.strptime(period_end_str[:10], "%Y-%m-%d")
        except ValueError:
            return {
                "summary": f"extract_kpi: LLM returned malformed period_end {period_end_str!r}",
            }

        if not apply:
            return {
                "summary": (
                    f"extract_kpi: would upsert {kpi_name}={value} for "
                    f"{ticker} {period_end_str} from comment quote"
                ),
                "extracted_value": value,
                "period_end": period_end_str,
                "fiscal_period_type": fiscal_period_type,
                "source_excerpt": excerpt[:200] + ("..." if len(excerpt) > 200 else ""),
                "dry_run": True,
            }

        # Insert the synthetic ANALYST_COMMENT document row so kpi_facts has
        # a valid source_doc_id FK. file_path encodes the comment id so we
        # can trace back; sha256 is a stable hash of the comment text + id.
        sha = hashlib.sha256(f"{c.id}::{c.comment}".encode()).hexdigest()
        doc_cur = conn.execute(
            "INSERT OR IGNORE INTO documents "
            "(ticker, source_type, doc_type, period_end, file_path, sha256, "
            " fetched_at, fetch_status, raw_bytes_size, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                ticker.upper(),
                # source_type stays in the SourceType enum (MANUAL_ENTRY covers
                # any analyst-entered value); doc_type is the distinct
                # ANALYST_COMMENT value so we can filter for comment-sourced
                # facts in audits without colliding with the SourceType keys.
                "manual_entry",
                "analyst_comment",
                period_end_dt,
                f"comments://{ticker.upper()}/{c.id}",
                sha,
                datetime.now(UTC),
                "ok",
                len(c.comment.encode("utf-8")),
            ),
        )
        if doc_cur.rowcount > 0:
            doc_id = doc_cur.lastrowid
        else:
            # Already-inserted with the same sha → look it up.
            existing = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha,)).fetchone()
            doc_id = int(existing["id"]) if existing else None
        if doc_id is None:
            return {"summary": "extract_kpi: failed to resolve synthetic document_id"}

        # UPSERT the kpi_facts row. Mirrors persist_one_kpi_fact's logical-key
        # contract, but allows source_excerpt and treats analyst-supplied
        # values as authoritative (highest doc_id always wins on conflict).
        conn.execute(
            "INSERT INTO kpi_facts "
            "(ticker, period_end, fiscal_period_type, kpi_definition_id, "
            " value, unit, source_doc_id, source_excerpt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticker, period_end, fiscal_period_type, kpi_definition_id) "
            "DO UPDATE SET "
            "    value = excluded.value, "
            "    unit = excluded.unit, "
            "    source_doc_id = excluded.source_doc_id, "
            "    source_excerpt = excluded.source_excerpt",
            (
                ticker.upper(),
                period_end_dt,
                fiscal_period_type,
                int(kdef_row["id"]),
                str(value),
                str(kdef_row["unit"]),
                doc_id,
                excerpt or None,
            ),
        )
        conn.commit()

        return {
            "summary": (
                f"extract_kpi: upserted {kpi_name}={value} for "
                f"{ticker} {period_end_str} (kpi_definition_id={kdef_row['id']}, "
                f"source_doc_id={doc_id})"
            ),
            "extracted_value": value,
            "period_end": period_end_str,
            "fiscal_period_type": fiscal_period_type,
            "source_excerpt": excerpt[:200] + ("..." if len(excerpt) > 200 else ""),
            "kpi_definition_id": int(kdef_row["id"]),
            "source_doc_id": int(doc_id),
            "dry_run": False,
        }
    except Exception as e:
        return {"summary": f"extract_kpi: error: {type(e).__name__}: {e}"}
    finally:
        conn.close()


def _route_ask_question(
    repo_root: Path, ticker: str, report_date: date, c: Comment
) -> dict[str, object]:
    """Answer the analyst's question using thesis + bear + IR anchors as context."""
    anchor = load_thesis_anchor(repo_root, ticker)
    bear = load_bear_anchor(repo_root, ticker)
    ir = load_ir_anchor(repo_root, ticker)
    context = "\n\n---\n\n".join([b for b in (anchor, bear, ir) if b]) or "(no thesis on file)"
    prompt = f"""You are an analyst assistant for {ticker} answering a
specific question the analyst left as a comment on the workspace report.

CONTEXT (analyst's own framing of this name):
{context}

The comment is attached to: **{c.anchor.type}** — `{c.anchor.key}`
(report dated {report_date.isoformat()})

ANALYST QUESTION:
\"\"\"
{c.comment}
\"\"\"

Answer the question in 2-5 sentences. Cite the specific KPI / failure
mode / business mechanic where relevant. If the question can't be
answered from the context, say so and name what data would resolve it.
"""
    answer = call_llm(prompt, purpose="company_description").strip()
    return {
        "summary": "ask_question: answered (see follow_up_thread)",
        "answer": answer,
    }


def _route_fix_data(repo_root: Path, ticker: str, c: Comment, apply: bool) -> dict[str, object]:
    """Log the comment to directives/data_fixes.md for manual intervention."""
    out = repo_root / "directives" / "data_fixes.md"
    line = (
        f"- [ ] **{ticker}** ({c.anchor.type} · `{c.anchor.key}`) — "
        f"reported {datetime.now(UTC).date().isoformat()}: {c.comment}\n"
    )
    if apply:
        with out.open("a", encoding="utf-8") as f:
            f.write(line)
    return {
        "summary": f"fix_data: logged to {out.relative_to(repo_root)}",
        "line": line.strip(),
        "dry_run": not apply,
    }


# Phrase → backlog area heuristic. The LLM classifier sets the intent; this
# secondary pass extracts a coarse "where in the codebase does this likely
# land" tag so the backlog is grep-able by feature area. Keep it small —
# better to under-classify (and tag `general`) than to invent buckets.
_PLATFORM_AREA_HEURISTICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("renderer", ("hover", "tooltip", "click", "panel", "column", "render", "display", "ui")),
    ("saydo", ("saydo", "say-do", "say do", "attribution", "execution vs")),
    ("pipeline", ("fetch", "pull", "transcript", "ingest", "backfill", "refresh")),
    ("kpi", ("kpi", "break rule", "status", "tier")),
    ("chart", ("yoy", "chart", "matrix", "geography", "segment")),
    ("dcf", ("dcf", "valuation", "discounted cash flow")),
)


def _classify_platform_area(comment_text: str) -> str:
    text = comment_text.lower()
    for area, keywords in _PLATFORM_AREA_HEURISTICS:
        if any(k in text for k in keywords):
            return area
    return "general"


def _route_platform_change(
    repo_root: Path, ticker: str, c: Comment, apply: bool
) -> dict[str, object]:
    """Log a cross-workspace bug/feature to directives/platform_backlog.md.

    Distinct from `fix_data` (single-ticker data correction): the entry is
    NOT tagged primarily by ticker, but by feature area + the originating
    workspace. The intent is that the next engineering pass groups by area
    and ships the renderer/pipeline change once for every workspace.

    Best-effort area tagging via simple phrase heuristic — gives the
    backlog a grep-able structure without needing another LLM call.
    """
    area = _classify_platform_area(c.comment)
    out = repo_root / "directives" / "platform_backlog.md"
    today = datetime.now(UTC).date().isoformat()
    line = (
        f"- [ ] **[{area}]** ({c.anchor.type} · `{c.anchor.key}` · tab={c.anchor.tab or '-'}) "
        f"— reported {today} from {ticker}: {c.comment}\n"
    )
    if apply:
        # Create the file with a header on first write so it doesn't look like
        # an accidentally-empty markdown stub.
        if not out.exists():
            out.write_text(
                "# Platform backlog\n\n"
                "Cross-workspace bugs and features captured via the comment "
                "processor (intent=`platform_change`). Entries are tagged by "
                "feature area and the originating workspace. Group by area "
                "when planning engineering work — every entry should land as "
                "ONE renderer/pipeline change applied across all workspaces, "
                "not as a per-ticker brief edit.\n\n",
                encoding="utf-8",
            )
        with out.open("a", encoding="utf-8") as f:
            f.write(line)
    return {
        "summary": f"platform_change: logged to {out.relative_to(repo_root)} as [{area}]",
        "area": area,
        "line": line.strip(),
        "dry_run": not apply,
    }


def _route_rewrite_section(
    repo_root: Path, ticker: str, c: Comment, apply: bool
) -> dict[str, object]:
    """Stub for now — surface the target + ask the user to re-run the build."""
    target_map = {
        "company_overview": "data/company_description/{T}.json (regenerate via build_artifacts --enable-llm --refresh)",
        "valuation_rationale": "data/valuation_basis/{T}.json (delete + re-run --enable-llm)",
        "failure_mode": "data/bear_case/{T}.json (delete + re-run --enable-llm)",
        "thesis_lede": "micro_thesis/holdings/{T}.json (use edit_thesis intent for narrative edits)",
    }
    hint = target_map.get(c.anchor.type, "unknown target")
    return {
        "summary": f"rewrite_section: target={hint.format(T=ticker.upper())}",
        "next_step": (
            "Delete the cache file then rebuild with --enable-llm --refresh. "
            "Or use intent=edit_thesis for narrative-only edits."
        ),
    }


# ---------------------------------------------------------------------------
# Intent classification (Haiku bucketer for unlabelled comments)
# ---------------------------------------------------------------------------


def _classify_intent(c: Comment) -> str:
    """Fast Haiku call to bucket an unlabelled comment into one of the routers."""
    prompt = f"""Classify the following analyst comment into ONE of these
buckets:

- platform_change: user is reporting a BUG or requesting a FEATURE that
  applies BEYOND the current workspace. Strong signals: explicit phrases
  like "across reports", "across workspaces", "across briefs", "every
  workspace", "all tickers", "investigate ... bug", "should also be a
  change across workspaces", "make sure this is not a bug across
  workspaces". Also implicit signals where the symptom is structural
  rather than name-specific: a renderer/UI bug (tooltips not showing,
  panels empty for everyone, columns missing), a pipeline/ingestion change
  (pull more transcripts, fetch additional history), a feature request
  that names "the report" / "the workspace" generically rather than a
  specific KPI or thesis claim. CRITICAL: pick this OVER fix_data or
  edit_structured when the issue is plainly not single-ticker.
- drop_kpi: user wants to remove this KPI from the thesis (single ticker)
- edit_thesis: user wants the thesis NARRATIVE paragraph rewritten (framing,
  emphasis, story arc, prose-level changes). Picks this when the comment
  is about HOW the thesis is told.
- edit_structured: user wants STRUCTURED fields changed — add/modify/remove
  a break_rule, change a KPI's break_condition, add a KPI to a tier,
  consolidate rules, reorder chart_priorities, etc. Picks this when the
  comment names a specific monitorable / break rule / threshold / KPI tier
  movement FOR THIS ticker. If the comment touches BOTH narrative and
  structured changes, prefer edit_structured (structured fields are the
  more specific signal). If the comment is asking for the FEATURE that
  produces a column / panel / rule to exist across all workspaces, prefer
  platform_change.
- extract_kpi: user is SUPPLYING a KPI value with provenance — a verbatim
  quote from a transcript / IR doc / filing, OR a direct value statement
  like "ROE Q3 2025 was 32.0%". The comment names a specific period + value
  for a SPECIFIC KPI (typically anchored to a kpi_ledger_row). Picks this
  when the comment is asserting a number, not asking for the thesis or
  structured fields to change.
- ask_question: user is asking a question or wanting more info about THIS
  ticker's numbers / thesis / data
- fix_data: user is reporting a data error / inaccuracy SPECIFIC to this
  ticker (a wrong YoY value, a stale figure, a transcription error). NOT
  the same as platform_change — fix_data is "this one cell on this one
  workspace is wrong"; platform_change is "the renderer is buggy for
  everyone".
- rewrite_section: user wants a specific section (company overview,
  valuation rationale, failure mode) rewritten for THIS ticker

Anchor type: {c.anchor.type}
Anchor key: {c.anchor.key}
Comment: \"\"\"{c.comment}\"\"\"

Reply with just one of: platform_change, drop_kpi, edit_thesis, edit_structured, extract_kpi, ask_question, fix_data, rewrite_section
"""
    raw = call_llm(prompt, purpose="intake_classifier").strip().lower()
    valid = {
        "platform_change",
        "drop_kpi",
        "edit_thesis",
        "edit_structured",
        "extract_kpi",
        "ask_question",
        "fix_data",
        "rewrite_section",
    }
    for tok in raw.split():
        if tok in valid:
            return tok
    return "ask_question"  # safest default


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_latest_report_date(repo_root: Path, ticker: str) -> date | None:
    out = repo_root / "output" / "research" / ticker.upper()
    if not out.exists():
        return None
    workspaces = sorted(out.glob("*_workspace.html"), reverse=True)
    if not workspaces:
        return None
    name = workspaces[0].name  # e.g. 2026-05-18_workspace.html
    try:
        return date.fromisoformat(name[:10])
    except ValueError:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker")
    g.add_argument("--all", action="store_true")
    p.add_argument(
        "--report-date",
        type=date.fromisoformat,
        help="ISO date of the report to process (default: latest)",
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    p.add_argument("--apply", action="store_true", help="actually mutate files; default is dry-run")
    p.add_argument(
        "--clear", action="store_true", help="drop addressed+dismissed comments after processing"
    )
    p.add_argument(
        "--no-sequence",
        action="store_true",
        help="skip the sequencer pass (apply in classify order). Debug only — without sequencing, edit_thesis items can compound in surprising ways.",
    )
    p.add_argument(
        "--no-rebuild",
        action="store_true",
        help="skip the auto-rebuild after apply (debug only — leaves the workspace HTML stale vs the updated holdings JSON).",
    )
    args = p.parse_args()
    repo_root = args.repo_root.resolve()

    tickers: list[str]
    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        base = repo_root / "data" / "report_comments"
        tickers = sorted([p.name for p in base.iterdir() if p.is_dir()]) if base.exists() else []

    summary: list[dict[str, object]] = []
    for t in tickers:
        rd = args.report_date or _resolve_latest_report_date(repo_root, t)
        if rd is None:
            print(f"[{t}] no report on disk, skipping", file=sys.stderr)
            continue
        result = process_comments_for_ticker(
            repo_root,
            t,
            rd,
            apply=args.apply,
            clear=args.clear,
            sequence=not args.no_sequence,
            auto_rebuild=not args.no_rebuild,
        )
        summary.append(result)
        print(
            f"[{t} {rd}] processed={result['processed']} "
            f"applied={result['applied']} cleared={result['cleared']}",
            file=sys.stderr,
        )

    print(json.dumps({"summary": summary}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
