"""execution/process_report_comments.py
----------------------------------------
Loop through every OPEN comment on a (ticker, report_date) report and
take action based on the comment's `intent`.

  drop_kpi         → remove the named KPI from micro_thesis/holdings/<T>.json
  edit_thesis      → ask Opus to revise the thesis paragraph using the comment as
                     guidance; write the revised thesis back
  ask_question     → ask Opus the question (with ReportSpec context); append
                     the answer to follow_up_thread as an `assistant` turn
  fix_data         → log a TODO in directives/data_fixes.md (manual ticket)
  rewrite_section  → ask Opus to rewrite the targeted section (company
                     overview / valuation rationale / bear failure mode);
                     write the rewrite back to its cache

Comments with `intent = null` get auto-classified via a Haiku bucketer first.

Default mode is `--dry-run` (print the plan, don't touch files). Pass
`--apply` to execute. `--clear` drops all addressed + dismissed comments
after the run.

Usage:
  python execution/process_report_comments.py --ticker NU
  python execution/process_report_comments.py --ticker NU --apply
  python execution/process_report_comments.py --ticker NU --apply --clear
  python execution/process_report_comments.py --all
"""

from __future__ import annotations

import argparse
import json
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
) -> dict[str, object]:
    """Loop open comments, route by intent, return a summary dict."""
    open_comments = comments.list_comments(repo_root, ticker, report_date, status="open")
    results: list[dict[str, object]] = []
    for c in open_comments:
        intent = c.intent or _classify_intent(c)
        if intent != c.intent and apply:
            comments.update_comment(
                repo_root, ticker, report_date, c.id, intent=intent
            )
        try:
            resolution = _route(repo_root, ticker, report_date, c, intent, apply=apply)
            results.append({
                "id": c.id, "intent": intent, "status": "ok",
                "resolution": resolution,
            })
            if apply:
                if intent == "ask_question" and resolution.get("answer"):
                    comments.update_comment(
                        repo_root, ticker, report_date, c.id,
                        append_thread=ThreadEntry(
                            role="assistant", text=resolution["answer"]
                        ),
                        status="addressed",
                        resolution_note=resolution.get("summary", ""),
                    )
                else:
                    comments.update_comment(
                        repo_root, ticker, report_date, c.id,
                        status="addressed",
                        resolution_note=resolution.get("summary", "applied"),
                    )
        except Exception as e:
            results.append({
                "id": c.id, "intent": intent, "status": "error",
                "error": f"{type(e).__name__}: {e}",
            })

    cleared = 0
    if clear and apply:
        cleared = comments.clear_addressed(repo_root, ticker, report_date)

    return {
        "ticker": ticker,
        "report_date": report_date.isoformat(),
        "processed": len(results),
        "applied": apply,
        "cleared": cleared,
        "results": results,
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
    if intent == "ask_question":
        return _route_ask_question(repo_root, ticker, report_date, c)
    if intent == "fix_data":
        return _route_fix_data(repo_root, ticker, c, apply=apply)
    if intent == "rewrite_section":
        return _route_rewrite_section(repo_root, ticker, c, apply=apply)
    return {"summary": f"no router for intent={intent!r} — left as open"}


# ---------------------------------------------------------------------------
# Intent routers
# ---------------------------------------------------------------------------


def _route_drop_kpi(
    repo_root: Path, ticker: str, c: Comment, apply: bool
) -> dict[str, object]:
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


def _route_edit_thesis(
    repo_root: Path, ticker: str, c: Comment, apply: bool
) -> dict[str, object]:
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
        "dry_run": not apply,
    }


def _route_ask_question(
    repo_root: Path, ticker: str, report_date: date, c: Comment
) -> dict[str, object]:
    """Answer the analyst's question using thesis + bear anchors as context."""
    anchor = load_thesis_anchor(repo_root, ticker)
    bear = load_bear_anchor(repo_root, ticker)
    context = "\n\n---\n\n".join([b for b in (anchor, bear) if b]) or "(no thesis on file)"
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


def _route_fix_data(
    repo_root: Path, ticker: str, c: Comment, apply: bool
) -> dict[str, object]:
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

- drop_kpi: user wants to remove this KPI from the thesis
- edit_thesis: user wants the thesis paragraph rewritten with the change they describe
- ask_question: user is asking a question or wanting more info
- fix_data: user is reporting a data error / inaccuracy
- rewrite_section: user wants a specific section (company overview, valuation rationale, failure mode) rewritten

Anchor type: {c.anchor.type}
Anchor key: {c.anchor.key}
Comment: \"\"\"{c.comment}\"\"\"

Reply with just one of: drop_kpi, edit_thesis, ask_question, fix_data, rewrite_section
"""
    raw = call_llm(prompt, purpose="intake_classifier").strip().lower()
    valid = {"drop_kpi", "edit_thesis", "ask_question", "fix_data", "rewrite_section"}
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
    p.add_argument("--report-date", type=date.fromisoformat,
                   help="ISO date of the report to process (default: latest)")
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    p.add_argument("--apply", action="store_true", help="actually mutate files; default is dry-run")
    p.add_argument("--clear", action="store_true", help="drop addressed+dismissed comments after processing")
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
            repo_root, t, rd, apply=args.apply, clear=args.clear
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
