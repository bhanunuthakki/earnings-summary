"""Build pairwise Say-Do analyses from existing `.tmp/*_summary.txt` files.

Sole producer of the `.tmp/SayDo_*.txt` files that feed the brief's §6 Say-Do
section. Walks `.tmp/{TICKER}_Q*_{YEAR}_(?:investor_update_)?summary.txt` (written
by `execution/process_ir_documents.py`) in chronological order and writes a
`.tmp/SayDo_{TICKER}_Q{prev}_{prev_yr}_Q{curr}_{curr_yr}.txt` for every
consecutive pair via `llm_client.generate_pairwise_analysis`. The
`_investor_update_summary` variant covers tickers like MELI/NU that publish
investor letters in lieu of traditional call transcripts. Idempotent —
re-runs skip pairs whose SayDo file already exists.

Usage:
    python execution/build_saydo_pairs.py --ticker MELI
    python execution/build_saydo_pairs.py --all
    python execution/build_saydo_pairs.py --all --refresh
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import hashlib  # noqa: E402

import db  # noqa: E402
from llm.batch import BatchRequest, saydo_custom_id, write_jsonl  # noqa: E402
from llm_artifact_store import UpsertRequest, upsert  # noqa: E402
from llm_client import (  # noqa: E402
    _build_pairwise_analysis_prompt,
    compose_anchor_block,
    generate_pairwise_analysis,
    load_bear_anchor,
    load_ir_anchor,
    load_thesis_anchor,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_SUMMARY_RX = re.compile(
    r"^(?P<ticker>[A-Z][A-Z0-9.]*)_Q(?P<q>[1-4])_(?P<y>\d{4})_(?:investor_update_)?summary\.txt$"
)


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    tickers = _resolve_tickers(repo_root, args)
    if not tickers:
        print("[]")
        return 0

    if args.prepare_batch:
        return _prepare_batch_mode(tickers, repo_root, refresh=args.refresh)

    summary: list[dict[str, object]] = []
    for ticker in tickers:
        result = _process_ticker(ticker, repo_root, refresh=args.refresh)
        summary.append(result)
        msg_parts = [
            f"summaries={result['summaries_found']}",
            f"pairs_written={result['pairs_written']}",
            f"pairs_skipped={result['pairs_skipped']}",
            f"elapsed={result['elapsed_ms']}ms",
        ]
        if result.get("error"):
            msg_parts.append(f"error={result['error']}")
        print(f"[{ticker}] " + " ".join(msg_parts), file=sys.stderr)
    print(json.dumps({"summary": summary}, indent=2))
    return 0


def _prepare_batch_mode(tickers: list[str], repo_root: Path, *, refresh: bool) -> int:
    """Collect every pending SayDo pair across `tickers` into a single
    Anthropic message-batches JSONL file. Does NOT submit — operator pipes
    the file into the SDK's batches API on the overnight cron path.

    Outputs:
      .tmp/saydo_batch/saydo_batch_<TIMESTAMP>.jsonl
      .tmp/saydo_batch/saydo_batch_<TIMESTAMP>.manifest.json (audit trail)
    """
    tmp = repo_root / ".tmp"
    batch_dir = tmp / "saydo_batch"
    batch_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = batch_dir / f"saydo_batch_{stamp}.jsonl"
    manifest_path = batch_dir / f"saydo_batch_{stamp}.manifest.json"

    requests: list[BatchRequest] = []
    manifest_entries: list[dict[str, object]] = []
    for ticker in tickers:
        summaries = _list_summaries(tmp, ticker)
        anchor_block = compose_anchor_block(
            load_thesis_anchor(repo_root, ticker),
            load_bear_anchor(repo_root, ticker),
            load_ir_anchor(repo_root, ticker),
        )
        for i in range(1, len(summaries)):
            prev = summaries[i - 1]
            curr = summaries[i]
            custom_id = saydo_custom_id(
                ticker=ticker,
                prev_quarter=str(prev["quarter"]),
                prev_year=int(prev["year"]),
                curr_quarter=str(curr["quarter"]),
                curr_year=int(curr["year"]),
            )
            out_path = tmp / f"{custom_id}.txt"
            if out_path.exists() and not refresh:
                continue
            prompt = _build_pairwise_analysis_prompt(prev, curr, anchor_block=anchor_block)
            requests.append(BatchRequest(custom_id=custom_id, prompt=prompt))
            manifest_entries.append(
                {
                    "custom_id": custom_id,
                    "ticker": ticker,
                    "expected_output": str(out_path.relative_to(repo_root)),
                }
            )

    written = write_jsonl(requests, jsonl_path)
    manifest_path.write_text(
        json.dumps(
            {
                "stamp": stamp,
                "tickers": tickers,
                "requests": manifest_entries,
                "jsonl": str(jsonl_path.relative_to(repo_root)),
                "submit_hint": (
                    "Submit via the governed subscription transport: "
                    "python execution/submit_saydo_batch.py "
                    f"--jsonl {jsonl_path.relative_to(repo_root)} "
                    f"--repo-root {repo_root}"
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "mode": "prepare_batch",
                "requests_written": written,
                "jsonl": str(jsonl_path.relative_to(repo_root)),
                "manifest": str(manifest_path.relative_to(repo_root)),
            },
            indent=2,
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker")
    g.add_argument("--all", action="store_true", help="All portfolio + watchlist tickers")
    p.add_argument(
        "--refresh", action="store_true", help="Re-generate even if SayDo file already exists"
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    p.add_argument(
        "--prepare-batch",
        action="store_true",
        help="Write a JSONL of Anthropic messages.batches.create requests to "
        ".tmp/saydo_batch/ instead of issuing synchronous LLM calls. "
        "Overnight cron submits the batch via the SDK; result drops back "
        "into .tmp/SayDo_*.txt with the same filenames the synchronous "
        "path would have written.",
    )
    return p.parse_args()


def _resolve_tickers(repo_root: Path, args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return [args.ticker.upper()]
    db_path = repo_root / "data" / "portfolio.db"
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    cur = conn.cursor()
    cur.execute(
        f"SELECT DISTINCT ticker FROM tracked_companies "
        f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} ORDER BY ticker"
    )
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


def _process_ticker(ticker: str, repo_root: Path, refresh: bool) -> dict[str, object]:
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    tmp = repo_root / ".tmp"
    summaries = _list_summaries(tmp, ticker)
    pairs_written = 0
    pairs_skipped = 0
    error: str | None = None

    # Anchor is constant across all pairs for this ticker — load once.
    anchor_block = compose_anchor_block(
        load_thesis_anchor(repo_root, ticker),
        load_bear_anchor(repo_root, ticker),
        load_ir_anchor(repo_root, ticker),
    )

    anchor_sha = hashlib.sha256(anchor_block.encode("utf-8")).hexdigest()
    db_path = repo_root / "data" / "portfolio.db"

    for i in range(1, len(summaries)):
        prev = summaries[i - 1]
        curr = summaries[i]
        # Filename + quarter strings use the canonical "Qn" form (e.g. "Q1") to
        # match the SayDo regex in saydo.py and the prompt template in llm_client.
        out_path = (
            tmp
            / f"SayDo_{ticker}_{prev['quarter']}_{prev['year']}_{curr['quarter']}_{curr['year']}.txt"
        )
        if out_path.exists() and not refresh:
            pairs_skipped += 1
            continue
        try:
            text = generate_pairwise_analysis(prev, curr, anchor_block=anchor_block)
        except Exception as e:
            error = (
                f"{prev['quarter']} {prev['year']}→{curr['quarter']} {curr['year']}: "
                f"{type(e).__name__}: {e}"
            )
            break
        out_path.write_text(text, encoding="utf-8")
        pairs_written += 1
        # Also upsert into llm_artifact_store so the artifact dirty chain
        # picks up SayDo invalidations when thesis text changes. cache_inputs
        # carry anchor_sha + the two summary contents so a thesis edit
        # produces a new input_sha256 -> the prior artifact is superseded
        # automatically instead of living on under the old filename.
        if db_path.exists():
            fiscal_period = f"{prev['quarter']}_{prev['year']}_to_{curr['quarter']}_{curr['year']}"
            with suppress(Exception):  # never fail the SayDo write on artifact store
                upsert(
                    UpsertRequest(
                        ticker=ticker,
                        purpose="saydo_pair",
                        fiscal_period=fiscal_period,
                        content_md=text,
                        cache_inputs=[
                            f"anchor_sha={anchor_sha}",
                            f"prev_summary_sha={hashlib.sha256(str(prev['text']).encode('utf-8')).hexdigest()}",
                            f"curr_summary_sha={hashlib.sha256(str(curr['text']).encode('utf-8')).hexdigest()}",
                            f"period={fiscal_period}",
                        ],
                        prompt_version="v1",
                    ),
                    db_path=db_path,
                )

    return {
        "ticker": ticker,
        "summaries_found": len(summaries),
        "pairs_written": pairs_written,
        "pairs_skipped": pairs_skipped,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "error": error,
    }


def _list_summaries(tmp: Path, ticker: str) -> list[dict[str, object]]:
    """Return list of {quarter, year, text} ordered chronologically."""
    if not tmp.exists():
        return []
    out: list[tuple[int, int, Path]] = []
    for p in tmp.iterdir():
        if not p.is_file():
            continue
        m = _SUMMARY_RX.match(p.name)
        if not m or m.group("ticker") != ticker:
            continue
        out.append((int(m.group("q")), int(m.group("y")), p))
    out.sort(key=lambda x: (x[1], x[0]))
    return [
        {"quarter": f"Q{q}", "year": y, "text": p.read_text(encoding="utf-8")} for q, y, p in out
    ]


if __name__ == "__main__":
    raise SystemExit(main())
