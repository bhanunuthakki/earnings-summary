"""Per-ticker on-disk artifact inventory for the command-center drill-down.

Pure filesystem reads — no DB. Answers "what artifacts exist for this ticker
right now, and how fresh are they?" across the brief outputs, the DCF workbook,
the thesis/diligence files, the raw sources (transcripts / IR docs / FMP cache),
and the cached LLM artifacts.

Single files report exists/mtime/size; multi-file groups (globs, directories)
report the latest mtime + a count so the drill-down can show "12 files, newest
3d ago" without listing every one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(slots=True)
class Artifact:
    category: str  # "Brief" | "Valuation" | "Thesis" | "Sources" | "LLM cache"
    label: str
    path: str  # repo-relative display path (the glob/dir for groups)
    exists: bool
    modified_at: str | None  # ISO8601 UTC
    size_bytes: int | None
    count: int | None  # number of files for glob/dir groups; None for singles

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_artifact_inventory(repo_root: Path, ticker: str) -> list[Artifact]:
    """Enumerate every artifact kind for `ticker`. Always returns the full set
    (missing ones flagged `exists=False`) so the drill-down shows what *could*
    exist, not just what does."""
    t = ticker.upper()
    research = f"output/research/{t}"
    out: list[Artifact] = [
        _latest(repo_root, "Brief", "Workspace report (HTML)", f"{research}/*_workspace.html"),
        _latest(repo_root, "Brief", "Report (Markdown)", f"{research}/*_report.md"),
        _latest(repo_root, "Brief", "Sections (JSON)", f"{research}/*_sections.json"),
        _latest(repo_root, "Valuation", "Reference DCF dump (xlsx)", f"{research}/*_dcf.xlsx"),
        _single(repo_root, "Valuation", "Canonical DCF workbook", f"dcf/{t}.xlsx"),
        _single(repo_root, "Thesis", "Holdings JSON", f"micro_thesis/holdings/{t}.json"),
        _single(repo_root, "Thesis", "Diligence template", f"micro_thesis/diligence/{t}.md"),
        _glob_dir(repo_root, "Sources", "Raw transcripts", "transcripts/raw", f"{t}_*.txt"),
        _glob_dir(repo_root, "Sources", "IR documents", f"ir_documents/{t}", "**/*"),
        _glob_dir(repo_root, "Sources", "FMP cache", "data/historical/fmp", f"{t}_*.json"),
        _single(
            repo_root, "LLM cache", "Company description", f"data/company_description/{t}.json"
        ),
        _single(repo_root, "LLM cache", "Bear case", f"data/bear_case/{t}.json"),
        _single(repo_root, "LLM cache", "Valuation basis", f"data/valuation_basis/{t}.json"),
        _single(repo_root, "LLM cache", "Q&A topics", f"data/qa_topics/{t}.json"),
        _single(repo_root, "LLM cache", "SayDo filter", f"data/saydo_filter/{t}.json"),
        _single(
            repo_root, "LLM cache", "Filing intelligence", f"data/filing_intelligence/{t}.json"
        ),
        _glob_dir(repo_root, "LLM cache", "Ticker-specific", f"data/ticker_specific/{t}", "*.json"),
    ]
    return out


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat()


def _single(repo_root: Path, category: str, label: str, rel: str) -> Artifact:
    p = repo_root / rel
    if p.is_file():
        st = p.stat()
        return Artifact(category, label, rel, True, _iso(st.st_mtime), st.st_size, None)
    return Artifact(category, label, rel, False, None, None, None)


def _latest(repo_root: Path, category: str, label: str, glob_rel: str) -> Artifact:
    """Latest file matching a repo-relative glob (e.g. output/research/NU/*_workspace.html)."""
    matches = sorted(p for p in repo_root.glob(glob_rel) if p.is_file())
    if not matches:
        return Artifact(category, label, glob_rel, False, None, None, 0)
    latest = matches[-1]
    st = latest.stat()
    return Artifact(
        category,
        label,
        latest.relative_to(repo_root).as_posix(),
        True,
        _iso(st.st_mtime),
        st.st_size,
        len(matches),
    )


def _glob_dir(repo_root: Path, category: str, label: str, rel_dir: str, pattern: str) -> Artifact:
    """A directory group: count files matching `pattern` under `rel_dir`, report
    the newest mtime. `pattern` may be recursive ("**/*")."""
    base = repo_root / rel_dir
    if not base.is_dir():
        return Artifact(category, label, rel_dir, False, None, None, 0)
    files = [p for p in base.glob(pattern) if p.is_file()]
    if not files:
        return Artifact(category, label, rel_dir, False, None, None, 0)
    newest = max(files, key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    return Artifact(category, label, rel_dir, True, _iso(newest.stat().st_mtime), total, len(files))
