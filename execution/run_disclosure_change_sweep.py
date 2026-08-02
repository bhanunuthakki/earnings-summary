"""Run the disclosure-change detector stack as one resumable operation.

The detector scripts remain independent Layer-3 entrypoints.  This file owns
only deterministic routing, checkpointing, and failure aggregation:

* with ``--tickers``, run the new-accession fast path for those names;
* with no explicit scope, run only tracked names whose stored SEC accession
  set has grown since the last successful sweep;
* advance a ticker's checkpoint only after every detector succeeds.

Usage:
    python execution/run_disclosure_change_sweep.py
    python execution/run_disclosure_change_sweep.py --tickers NU,WIX
    python execution/run_disclosure_change_sweep.py --all-current
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.selection import selected_filing_sections_relation  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


class _Completed(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., _Completed]


class SweepState(BaseModel):
    """Durable, schema-validated accession checkpoint."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    processed_accessions: dict[str, list[str]] = Field(default_factory=dict)


class SweepResult(BaseModel):
    """Machine-readable run result written to stdout."""

    model_config = ConfigDict(frozen=True)

    tickers: tuple[str, ...]
    completed_tickers: tuple[str, ...]
    failed_tickers: tuple[str, ...]
    steps_completed: int
    failures: int
    state_path: str


STEPS: tuple[str, ...] = (
    "detect_disclosure_changes.py",
    "detect_discontinued_metrics.py",
    "detect_guidance_lifecycle.py",
    "detect_section_similarity.py",
    "detect_transcript_disclosure_events.py",
    "detrend_disclosure_events.py",
    "classify_disclosure_specificity.py",
    # Thesis-materiality elevation gate (owner ruling 2026-08-02): runs LAST so
    # it judges what the detectors + classifier just wrote. Rows it does not
    # judge (no thesis on file, tabular backlog, transient LLM failure) stay
    # NULL, which every surface treats as NOT elevated; the next sweep retries.
    "judge_disclosure_materiality.py",
)


def _load_state(path: Path) -> SweepState:
    if not path.exists():
        return SweepState()
    return SweepState.model_validate_json(path.read_text(encoding="utf-8"))


def _save_state(path: Path, state: SweepState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(
        state.model_dump_json(indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _current_accessions(db_path: Path) -> dict[str, list[str]]:
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    try:
        filing_sections_relation = selected_filing_sections_relation(conn).sql
        rows = conn.execute(
            f"""
            SELECT fs.ticker, fs.accession_number
            FROM {filing_sections_relation} AS fs
            JOIN tracked_companies AS tc ON tc.ticker = fs.ticker
            WHERE fs.accession_number IS NOT NULL
              AND TRIM(fs.accession_number) != ''
              AND COALESCE(tc.instrument_type, '') != 'etf'
            GROUP BY fs.ticker, fs.accession_number
            ORDER BY fs.ticker, fs.accession_number
            """  # nosec B608 -- trusted internal SQL shape; values remain bound
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, list[str]] = {}
    for ticker_raw, accession_raw in rows:
        ticker = str(ticker_raw).upper()
        out.setdefault(ticker, []).append(str(accession_raw))
    return out


def _selected_tickers(
    *,
    current: dict[str, list[str]],
    state: SweepState,
    explicit: Sequence[str] | None,
    all_current: bool,
) -> tuple[str, ...]:
    if explicit is not None:
        return tuple(sorted({ticker.strip().upper() for ticker in explicit if ticker.strip()}))
    if all_current:
        return tuple(sorted(current))
    selected: list[str] = []
    for ticker, accessions in current.items():
        prior = set(state.processed_accessions.get(ticker, ()))
        if not set(accessions).issubset(prior):
            selected.append(ticker)
    return tuple(sorted(selected))


def _command(
    *,
    project_root: Path,
    db_path: Path,
    ticker: str,
    script: str,
) -> list[str]:
    return [
        sys.executable,
        str(project_root / "execution" / script),
        "--tickers",
        ticker,
        "--db-path",
        str(db_path),
    ]


def run(
    *,
    db_path: Path,
    state_path: Path,
    project_root: Path = PROJECT_ROOT,
    tickers: Sequence[str] | None = None,
    all_current: bool = False,
    bootstrap_current: bool = False,
    runner: Runner = subprocess.run,
) -> SweepResult:
    """Run the detector stack and checkpoint only fully successful tickers."""

    log = logging.getLogger("run_disclosure_change_sweep")
    state = _load_state(state_path)
    current = _current_accessions(db_path)
    if bootstrap_current:
        state.processed_accessions = {
            ticker: list(accessions) for ticker, accessions in current.items()
        }
        _save_state(state_path, state)
        bootstrapped = tuple(sorted(current))
        return SweepResult(
            tickers=bootstrapped,
            completed_tickers=bootstrapped,
            failed_tickers=(),
            steps_completed=0,
            failures=0,
            state_path=str(state_path),
        )
    selected = _selected_tickers(
        current=current,
        state=state,
        explicit=tickers,
        all_current=all_current,
    )
    completed: list[str] = []
    failed: list[str] = []
    steps_completed = 0

    for ticker in selected:
        ticker_failed = False
        for script in STEPS:
            proc = runner(
                _command(
                    project_root=project_root,
                    db_path=db_path,
                    ticker=ticker,
                    script=script,
                ),
                cwd=str(project_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                detail = ""
                stderr = getattr(proc, "stderr", "")
                if stderr:
                    detail = redact(stderr.strip().splitlines()[-1])[:500]
                log.error(
                    {
                        "event": "disclosure_step_failed",
                        "ticker": ticker,
                        "script": script,
                        "returncode": proc.returncode,
                        "detail": detail,
                    }
                )
                failed.append(ticker)
                ticker_failed = True
                break
            steps_completed += 1
            log.info(
                {
                    "event": "disclosure_step_complete",
                    "ticker": ticker,
                    "script": script,
                }
            )

        if ticker_failed:
            continue
        completed.append(ticker)
        state.processed_accessions[ticker] = list(current.get(ticker, ()))
        _save_state(state_path, state)

    if not state_path.exists():
        _save_state(state_path, state)
    return SweepResult(
        tickers=selected,
        completed_tickers=tuple(completed),
        failed_tickers=tuple(failed),
        steps_completed=steps_completed,
        failures=len(failed),
        state_path=str(state_path),
    )


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated fast-path names")
    parser.add_argument(
        "--all-current",
        action="store_true",
        help="Run every tracked name with at least one stored SEC accession",
    )
    parser.add_argument(
        "--bootstrap-current",
        action="store_true",
        help=(
            "Checkpoint the current accession corpus without running detectors. "
            "Use only after an operator has verified the corpus was already processed."
        ),
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=PROJECT_ROOT / "data" / "portfolio.db",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "disclosure_change_sweep" / "state.json",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    selected_modes = sum(
        bool(value) for value in (args.tickers, args.all_current, args.bootstrap_current)
    )
    if selected_modes > 1:
        parser.error("--tickers, --all-current, and --bootstrap-current are mutually exclusive")

    _configure_logging(args.verbose)
    explicit = (
        tuple(token.strip() for token in args.tickers.split(",") if token.strip())
        if args.tickers
        else None
    )
    result = run(
        db_path=args.db_path.resolve(),
        state_path=args.state_path.resolve(),
        tickers=explicit,
        all_current=args.all_current,
        bootstrap_current=args.bootstrap_current,
    )
    json.dump(result.model_dump(mode="json"), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return min(result.failures, 255)


if __name__ == "__main__":
    raise SystemExit(main())
