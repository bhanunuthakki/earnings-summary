"""Retention for database backup FILES — the counterpart to ``execution/db_gc.py``.

``db_gc.py`` prunes rows *inside* ``portfolio.db``. Nothing owned the backup files
themselves, and at the 2026-08-02 audit that gap held ~61 GB: four unmanaged pools
(``data/`` loose ``*.bak``, ``data/backups/``, ``.tmp/``, ``.git/archive/``) written
by ad-hoc migration and cutover scripts under eight different filename conventions,
none registered with any retention. The only file retention that existed lived
inside ``cron/backup_db.py`` and governed the Drive snapshot folder alone.

This is the registered owner for those pools.

Policy (``--keep-monthly`` / ``--keep-recent``)
----------------------------------------------
Keep the newest backup in each calendar month, plus the N most recent overall.
Everything else is pruned. Survivors older than ``--gzip-after-days`` are gzipped:
an uncompressed SQLite file compresses ~5x here (1.77 GB -> 0.34 GB measured), so
compressing the tail costs nothing in recoverability and most of the bytes.

The canonical daily snapshots in the Drive folder are NOT in scope — ``backup_db.py``
already prunes those, and a second owner for one pool is how retention races start.

Safety
------
* Dry run by default; ``--apply`` writes.
* The live DB and its ``-wal``/``-shm`` are excluded structurally, not by pattern.
* ``*.json`` receipts/manifests are never touched — they are the cutover evidence
  ledger and are orders of magnitude smaller than what they document.
* Idempotent: a second run over an already-pruned tree plans nothing.

Usage
-----
    python execution/backup_file_gc.py                  # dry run, show the plan
    python execution/backup_file_gc.py --apply          # prune + gzip
    python execution/backup_file_gc.py --keep-recent 4 --apply
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = REPO_ROOT / "data" / "portfolio.db"

DEFAULT_KEEP_MONTHLY = 1
DEFAULT_KEEP_RECENT = 2
DEFAULT_GZIP_AFTER_DAYS = 7

# A backup file is a *copy of a database*. Receipts (.json), logs, and the live DB
# are not. Matching is on the filename, anchored to the registered pools below.
BACKUP_SUFFIXES = (".db", ".bak", ".gz")
# ``pre0262``/``pre_gc_``/``pre-activation`` are all in active use here, so the
# revision form (pre + digits) needs its own rule: a literal "pre_" list missed
# ``portfolio_pre0263_20260802.db.gz`` entirely, which is the single most common
# naming convention in this repo.
BACKUP_MARKER_RE = re.compile(
    r"(bak|backup|snapshot|rehearsal|rollback|precutover|candidate|reconcile"
    r"|pre[-_]?\d{3,4}|pre[-_](gc|activation|ledger|m4|transcript|disclosure))",
    re.IGNORECASE,
)

# Never prunable, regardless of pool or age. ``portfolio_gc_archive.db`` is where
# db_gc MOVES pruned rows so the prune stays reversible — deleting it silently
# converts every past "reversible" prune into permanent data loss.
PROTECTED_NAMES = frozenset({"portfolio_gc_archive.db"})
# These are the only possible row-level recovery source for destructive schema
# changes. Keep them until the deletion catalog records a successful data
# restore drill; monthly/recent retention must never silently age them out.
RECOVERY_ANCHOR_RE = re.compile(r"(?:pre[-_.]?\d{3,4}|bak[_-]?squash)", re.IGNORECASE)


def log_event(event: str, **fields: object) -> None:
    """One JSON line per event on stderr; stdout carries the plan only."""
    print(json.dumps({"event": event, **fields}, default=str), file=sys.stderr)


class BackupFile(BaseModel):
    path: Path
    size_bytes: int
    mtime: datetime
    pool: str

    @property
    def month(self) -> str:
        return self.mtime.strftime("%Y-%m")


class Plan(BaseModel):
    # default_factory=list infers list[Unknown] under pyright; the parameterized
    # alias is itself a callable returning the right type, so the annotation holds.
    keep: list[BackupFile] = Field(default_factory=list[BackupFile])
    prune: list[BackupFile] = Field(default_factory=list[BackupFile])
    gzip: list[BackupFile] = Field(default_factory=list[BackupFile])

    @property
    def prune_bytes(self) -> int:
        return sum(f.size_bytes for f in self.prune)

    @property
    def gzip_bytes(self) -> int:
        return sum(f.size_bytes for f in self.gzip)


def registered_pools(root: Path) -> dict[str, Path]:
    """The backup destinations this tool owns. Adding a new ad-hoc backup location
    without registering it here is how the 61 GB accumulated — keep this list and
    the scripts that write backups in sync."""
    return {
        "data-loose": root / "data",
        "data-backups": root / "data" / "backups",
        # db_gc writes pre-prune snapshots here (pre_gc_<stamp>_portfolio.db, 1.77 GB
        # apiece). It was unregistered at the 2026-08-02 audit and had no owner.
        "data-archive": root / "data" / "archive",
        "tmp": root / ".tmp",
        "git-archive": root / ".git" / "archive",
    }


def _is_backup_file(path: Path) -> bool:
    name = path.name.lower()
    if "portfolio" not in name:
        return False
    if not any(s in name for s in BACKUP_SUFFIXES):
        return False
    return BACKUP_MARKER_RE.search(name) is not None


def _excluded(path: Path) -> bool:
    """The live DB, its sidecars, and the reversible-prune store — structurally,
    never by pattern, so a retention bug cannot reach them."""
    if path.name in PROTECTED_NAMES:
        return True
    if RECOVERY_ANCHOR_RE.search(path.name):
        return True
    return path in {LIVE_DB, Path(f"{LIVE_DB}-wal"), Path(f"{LIVE_DB}-shm")}


def discover(root: Path) -> list[BackupFile]:
    found: dict[Path, BackupFile] = {}
    for pool, base in registered_pools(root).items():
        if not base.exists():
            continue
        # data-loose is the data/ root only; the nested pools own their own subtrees.
        entries = base.iterdir() if pool == "data-loose" else base.rglob("*")
        for p in entries:
            if not p.is_file() or _excluded(p) or p in found:
                continue
            if p.suffix.lower() == ".json":
                continue
            # -wal/-shm are not independent snapshots; they are sidecars of a base
            # file and must share its fate. Retaining them as separate candidates
            # lets retention delete a sidecar while keeping its base (or worse, the
            # base while keeping the sidecar) and call the result a backup.
            if p.name.endswith(("-wal", "-shm")):
                continue
            if not _is_backup_file(p):
                continue
            st = p.stat()
            found[p] = BackupFile(
                path=p,
                size_bytes=st.st_size,
                mtime=datetime.fromtimestamp(st.st_mtime),
                pool=pool,
            )
    return sorted(found.values(), key=lambda f: f.mtime)


def build_plan(
    files: list[BackupFile],
    *,
    keep_monthly: int,
    keep_recent: int,
    gzip_after_days: int,
    now: datetime | None = None,
) -> Plan:
    now = now or datetime.now()
    plan = Plan()
    if not files:
        return plan

    keep_paths: set[Path] = set()
    by_month: dict[str, list[BackupFile]] = defaultdict(list)
    for f in files:
        by_month[f.month].append(f)
    for month_files in by_month.values():
        newest = sorted(month_files, key=lambda f: f.mtime, reverse=True)
        for f in newest[:keep_monthly]:
            keep_paths.add(f.path)
    for f in sorted(files, key=lambda f: f.mtime, reverse=True)[:keep_recent]:
        keep_paths.add(f.path)

    for f in files:
        if f.path in keep_paths:
            plan.keep.append(f)
            age_days = (now - f.mtime).days
            if age_days >= gzip_after_days and f.path.suffix.lower() != ".gz":
                plan.gzip.append(f)
        else:
            plan.prune.append(f)
    return plan


def _gzip_in_place(path: Path) -> tuple[Path, int]:
    """Compress to ``<name>.gz`` and remove the original. Returns (new_path, saved)."""
    target = path.with_name(path.name + ".gz")
    before = path.stat().st_size
    with open(path, "rb") as raw, gzip.open(target, "wb", compresslevel=6) as gz:
        shutil.copyfileobj(raw, gz)
    after = target.stat().st_size
    path.unlink()
    return target, before - after


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retention for database backup FILES (see db_gc.py for rows).",
    )
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--keep-monthly", type=int, default=DEFAULT_KEEP_MONTHLY)
    parser.add_argument("--keep-recent", type=int, default=DEFAULT_KEEP_RECENT)
    parser.add_argument("--gzip-after-days", type=int, default=DEFAULT_GZIP_AFTER_DAYS)
    args = parser.parse_args(argv)

    if args.keep_monthly < 1 or args.keep_recent < 1:
        parser.error("--keep-monthly and --keep-recent must be at least 1")

    free_before = shutil.disk_usage(args.root).free
    files = discover(args.root)
    plan = build_plan(
        files,
        keep_monthly=args.keep_monthly,
        keep_recent=args.keep_recent,
        gzip_after_days=args.gzip_after_days,
    )
    log_event(
        "scanned",
        files=len(files),
        total_gb=round(sum(f.size_bytes for f in files) / 1e9, 3),
        free_gb=round(free_before / 1e9, 2),
    )

    for f in plan.prune:
        log_event("plan_prune", path=str(f.path), gb=round(f.size_bytes / 1e9, 3))
    for f in plan.gzip:
        log_event("plan_gzip", path=str(f.path), gb=round(f.size_bytes / 1e9, 3))

    pruned_bytes = 0
    saved_bytes = 0
    errors = 0
    if args.apply:
        for f in plan.prune:
            try:
                f.path.unlink()
                pruned_bytes += f.size_bytes
                for suffix in ("-wal", "-shm"):
                    sidecar = f.path.with_name(f.path.name + suffix)
                    if sidecar.exists():
                        pruned_bytes += sidecar.stat().st_size
                        sidecar.unlink()
            except OSError as exc:
                errors += 1
                log_event("prune_failed", path=str(f.path), error=str(exc))
        for f in plan.gzip:
            try:
                _, saved = _gzip_in_place(f.path)
                saved_bytes += saved
            except OSError as exc:
                errors += 1
                log_event("gzip_failed", path=str(f.path), error=str(exc))

    free_after = shutil.disk_usage(args.root).free
    summary = {
        "applied": bool(args.apply),
        "scanned": len(files),
        "keep": len(plan.keep),
        "prune": len(plan.prune),
        "gzip": len(plan.gzip),
        "prune_gb": round((pruned_bytes if args.apply else plan.prune_bytes) / 1e9, 3),
        "gzip_saved_gb": round(saved_bytes / 1e9, 3) if args.apply else None,
        "free_gb_before": round(free_before / 1e9, 2),
        "free_gb_after": round(free_after / 1e9, 2),
        "errors": errors,
    }
    print(json.dumps(summary, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
