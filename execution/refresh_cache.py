"""Smart cache: tier-aware FMP refresh queue.

Survives FMP-tier downgrades (Premium -> Basic) without losing track of what's
stale. Maintains a persistent priority queue derived from
`tracked_companies` x the `per_ticker_jobs` catalog x `fmp_endpoint_status`,
budgets the day's work against the configured tier, and gracefully defers
overflow to tomorrow.

Default tier comes from `FMP_TIER` env var or `basic` if unset. Override
per-invocation with `--tier`.

Subcommands (default = `run`):
    python execution/refresh_cache.py audit         # report buckets, no fetch
    python execution/refresh_cache.py status        # daily budget + ledger
    python execution/refresh_cache.py run           # default. audit + budgeted dispatch
    python execution/refresh_cache.py --background  # detach run; exit immediately
    python execution/refresh_cache.py archive AAPL  # set archived_at; exclude from queue
    python execution/refresh_cache.py reactivate AAPL  # clear archived_at

Manual overrides:
    --force            ignore cadence, queue every endpoint
    --max-calls N      override tier daily cap for this run
    --tickers A,B      restrict scope to specific tickers
    --only TIER        restrict to one list_type (portfolio/watchlist/...)

Operational notes:
    `--background` detaches the run and exits immediately, so callers can
    fire-and-forget. Cacher fast-exits in <1s when nothing's stale or budget
    is exhausted, so manual or scheduled invocations stay cheap. A lockfile
    prevents concurrent runs.

Tier semantics:
    basic    250 calls/day, no rate limit (we throttle to 4/sec for steady drip)
    starter  no daily cap, 300 calls/min
    premium  no daily cap, 720 calls/min (we use 720, not 750, for headroom)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

# Import the canonical endpoint catalog from save_fmp_data so audit knows
# what's expected per ticker. save_fmp_data is the source of truth.
import save_fmp_data as fmp_save  # noqa: E402
from pipeline import cadence_policy as _cadence_policy  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
CACHE_DIR = PROJECT_ROOT / ".tmp" / "cacher"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOCK_PATH = CACHE_DIR / ".lock"
QUEUE_PATH = CACHE_DIR / "queue.json"
HINTS_PATH = CACHE_DIR / "forced_stale.json"


def _load_force_stale_hints() -> set[str]:
    """Tickers that the earnings-calendar surrogate has flagged for forced refresh.

    Hints are written by `execution/schedule_pre_earnings_refresh.py` and have
    a TTL embedded; we filter expired ones at read time.
    """
    if not HINTS_PATH.exists():
        return set()
    try:
        raw = json.loads(HINTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    now = datetime.now()
    out: set[str] = set()
    for ticker, expires_str in raw.items():
        try:
            if datetime.fromisoformat(expires_str) > now:
                out.add(ticker.upper())
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Tier config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierConfig:
    """FMP subscription tier limits + sensible default rate."""

    name: str
    calls_per_day: int      # daily cap; sys.maxsize for unlimited
    calls_per_sec: float    # rate-limit target


_UNLIMITED = sys.maxsize

TIERS: dict[str, TierConfig] = {
    "basic":   TierConfig("basic",   calls_per_day=250,        calls_per_sec=4.0),
    "starter": TierConfig("starter", calls_per_day=_UNLIMITED, calls_per_sec=5.0),
    "premium": TierConfig("premium", calls_per_day=_UNLIMITED, calls_per_sec=12.0),
}


def resolve_tier(explicit: str | None) -> TierConfig:
    name = (explicit or os.environ.get("FMP_TIER") or "basic").lower()
    if name not in TIERS:
        raise ValueError(f"Unknown tier {name!r}; expected one of {sorted(TIERS)}")
    return TIERS[name]


# ---------------------------------------------------------------------------
# Cadence + priority
# ---------------------------------------------------------------------------


# How long an endpoint stays fresh, by list_type x endpoint class. Hours.
# `none` = tracked but de-emphasized; refresh quarterly to keep history alive
# without burning budget on names the user isn't actively analyzing.
_LIST_TYPE_BASE_FRESH_H: dict[str, int] = {
    "portfolio":    24,
    "watchlist":    24,
    "evaluation":   24,
    "none":         24 * 90,
    "etf":          24 * 30,
    "index_member": 24 * 30,
}

# Endpoint classification — drives priority weights and cadence multipliers.
# An endpoint's class is matched by substring on the path; first match wins.
_ENDPOINT_CLASSES: list[tuple[str, str]] = [
    ("discounted-cash-flow",    "time_sensitive"),
    ("ratings",                 "time_sensitive"),
    ("grades",                  "time_sensitive"),
    ("price-target",            "time_sensitive"),
    ("ratios-ttm",              "time_sensitive"),
    ("metrics-ttm",             "time_sensitive"),
    ("statements-ttm",          "time_sensitive"),
    ("statement-ttm",           "time_sensitive"),
    ("financial-scores",        "time_sensitive"),
    ("profile",                 "time_sensitive"),
    ("shares-float",            "time_sensitive"),
    ("market-capitalization",   "time_sensitive"),
    ("analyst-estimates",       "time_sensitive"),
    ("price-eod",               "time_sensitive"),
    ("growth",                  "growth"),
    ("segmentation",            "segment"),
    ("segments",                "segment"),
    ("statement",               "statement"),
    ("balance-sheet",           "statement"),
    ("cashflow",                "statement"),
    ("income-",                 "statement"),
    ("ratios",                  "statement"),
    ("key-metrics",             "statement"),
    ("enterprise-values",       "statement"),
    ("owner-earnings",          "statement"),
    ("financial-reports",       "statement"),
    ("form-10-k-json",          "statement"),
    ("peers",                   "reference"),
    ("executives",              "reference"),
    ("employee",                "reference"),
]

# Cadence multipliers per endpoint class. 1.0 = use list_type default freshness;
# >1.0 = stays fresh longer (e.g. quarterly statements don't need daily refresh).
# The numeric values mirror src/pipeline/cadence_policy.py — for portfolio +
# watchlist (base = 24h = 1d), `mult` equals the freshness in days, so
# statement=14.0 here matches STATEMENT_STALE_DAYS=14 there. Update both
# when editing the policy.
_CLASS_CADENCE_MULT: dict[str, float] = {
    "time_sensitive": float(_cadence_policy.TIME_SENSITIVE_STALE_DAYS),
    "growth":         float(_cadence_policy.GROWTH_STALE_DAYS),
    "segment":        float(_cadence_policy.STATEMENT_STALE_DAYS),
    "statement":      float(_cadence_policy.STATEMENT_STALE_DAYS),
    "reference":      float(_cadence_policy.REFERENCE_STALE_DAYS),
}

_CLASS_PRIORITY_WEIGHT: dict[str, int] = {
    "time_sensitive": 0,
    "growth":         100,
    "segment":        200,
    "statement":      300,
    "reference":      400,
}

_LIST_TYPE_PRIORITY_WEIGHT: dict[str, int] = {
    "portfolio":    0,
    "evaluation":   500,
    "watchlist":    1000,
    "none":         2000,
    "etf":          3000,
    "index_member": 4000,
}

# Status -> bucket. Drives priority bonus and retry eligibility.
_BUCKET_PRIORITY_BONUS: dict[str, int] = {
    "missing":         -50,   # never-pulled: do these first
    "stale":           0,
    "failed_recent":   _UNLIMITED,  # don't retry within retry_window
    "failed_retry_ok": 30,    # error/forbidden but enough time has passed
    "fresh":           _UNLIMITED,  # never queue
}

# How long to wait before retrying a failed endpoint, by failure class.
_RETRY_WINDOW_DAYS: dict[str, int] = {
    "forbidden": 30,   # tier-restricted: don't hammer until tier likely changed
    "error":     1,    # transient errors: try again tomorrow
    "empty":     7,    # endpoint returned empty list; rare but real
}


def classify_endpoint(endpoint: str) -> str:
    for substr, klass in _ENDPOINT_CLASSES:
        if substr in endpoint:
            return klass
    return "reference"


def cadence_hours(list_type: str, endpoint_class: str) -> float:
    base = _LIST_TYPE_BASE_FRESH_H.get(list_type, 24 * 30)
    mult = _CLASS_CADENCE_MULT.get(endpoint_class, 1.0)
    return base * mult


# ---------------------------------------------------------------------------
# Queue items + audit
# ---------------------------------------------------------------------------


@dataclass
class QueueItem:
    ticker: str
    list_type: str
    endpoint: str
    period: str
    suffix: str
    endpoint_class: str
    bucket: str               # missing | stale | failed_retry_ok | failed_recent | fresh
    last_pulled: datetime | None
    last_status: str | None
    days_overdue: int
    priority: int

    def to_manifest_entry(self) -> dict[str, str]:
        return {"ticker": self.ticker, "endpoint": self.endpoint, "period": self.period}


@dataclass
class AuditReport:
    generated_at: datetime
    items: list[QueueItem]
    counts: dict[str, int] = field(default_factory=dict)

    def queueable(self) -> list[QueueItem]:
        return [i for i in self.items if i.bucket in ("missing", "stale", "failed_retry_ok")]


def _all_active_tickers(conn: sqlite3.Connection,
                        only_list_types: frozenset[str] | None,
                        explicit_tickers: list[str] | None) -> list[tuple[str, str]]:
    """Return [(ticker, list_type), ...] for active (non-archived) rows."""
    cur = conn.cursor()
    sql = (
        "SELECT ticker, list_type FROM tracked_companies "
        "WHERE archived_at IS NULL"
    )
    params: list[object] = []
    if only_list_types:
        placeholders = ",".join("?" for _ in only_list_types)
        sql += f" AND list_type IN ({placeholders})"
        params.extend(only_list_types)
    if explicit_tickers:
        placeholders = ",".join("?" for _ in explicit_tickers)
        sql += f" AND ticker IN ({placeholders})"
        params.extend(t.upper() for t in explicit_tickers)
    sql += " ORDER BY ticker"
    cur.execute(sql, params)
    return [(r[0], r[1]) for r in cur.fetchall()]


def _existing_status_rows(
    conn: sqlite3.Connection, tickers: list[str]
) -> dict[tuple[str, str, str], tuple[str, datetime | None]]:
    """Map (ticker, endpoint, period) -> (status, last_pulled) for given tickers."""
    if not tickers:
        return {}
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in tickers)
    cur.execute(
        f"SELECT ticker, endpoint, period, status, last_pulled "
        f"FROM fmp_endpoint_status WHERE ticker IN ({placeholders})",
        tickers,
    )
    out: dict[tuple[str, str, str], tuple[str, datetime | None]] = {}
    for ticker, endpoint, period, status, last_pulled_str in cur.fetchall():
        last_pulled: datetime | None = None
        if last_pulled_str:
            try:
                last_pulled = datetime.fromisoformat(last_pulled_str)
            except ValueError:
                pass
        out[(ticker, endpoint, period or "")] = (status, last_pulled)
    return out


def _classify_into_bucket(
    list_type: str,
    endpoint: str,
    endpoint_class: str,
    status: str | None,
    last_pulled: datetime | None,
    now: datetime,
    force: bool,
) -> tuple[str, int]:
    """Return (bucket, days_overdue). days_overdue is sortable; -1 for missing."""
    if force:
        if status is None:
            return ("missing", 99999)
        return ("stale", 99999)

    if status is None:
        # Never attempted — highest priority for analyzed list_types.
        return ("missing", 99999)

    if status == "ok":
        cadence_h = cadence_hours(list_type, endpoint_class)
        if last_pulled is None:
            return ("stale", 99999)
        age_h = (now - last_pulled).total_seconds() / 3600
        if age_h < cadence_h:
            return ("fresh", 0)
        return ("stale", int((age_h - cadence_h) / 24))

    # Failed-class statuses: forbidden, error, empty
    retry_days = _RETRY_WINDOW_DAYS.get(status, 30)
    if last_pulled is None:
        return ("failed_retry_ok", 0)
    age_d = (now - last_pulled).days
    if age_d < retry_days:
        return ("failed_recent", 0)
    return ("failed_retry_ok", age_d - retry_days)


def _priority(list_type: str, endpoint_class: str, bucket: str, days_overdue: int) -> int:
    """Lower = sooner. Composite of list_type, endpoint class, bucket bonus, age."""
    return (
        _LIST_TYPE_PRIORITY_WEIGHT.get(list_type, 9000)
        + _CLASS_PRIORITY_WEIGHT.get(endpoint_class, 500)
        + _BUCKET_PRIORITY_BONUS.get(bucket, 0)
        - min(days_overdue, 99)   # more overdue = higher priority (subtract)
    )


def audit(
    conn: sqlite3.Connection,
    *,
    only_list_types: frozenset[str] | None = None,
    explicit_tickers: list[str] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> AuditReport:
    """Build the full audit report. Cheap (~sub-second on 2,500 tickers)."""
    now = now or datetime.now()
    active = _all_active_tickers(conn, only_list_types, explicit_tickers)
    tickers = [t for t, _ in active]
    status_index = _existing_status_rows(conn, tickers)
    forced_tickers = _load_force_stale_hints()

    items: list[QueueItem] = []
    for ticker, list_type in active:
        for job in fmp_save.per_ticker_jobs(ticker, list_type=list_type):
            endpoint = job["path"]
            period = job["period"] or ""
            suffix = job["suffix"]
            endpoint_class = classify_endpoint(endpoint)

            existing = status_index.get((ticker, endpoint, period))
            status = existing[0] if existing else None
            last_pulled = existing[1] if existing else None

            # Earnings-calendar hint: force time-sensitive endpoints to stale
            # for tickers reporting in the next 7 days (or recently reported).
            ticker_force = force or (
                ticker in forced_tickers and endpoint_class in ("time_sensitive", "statement", "segment")
            )

            bucket, days_overdue = _classify_into_bucket(
                list_type, endpoint, endpoint_class, status, last_pulled, now, ticker_force
            )
            priority = _priority(list_type, endpoint_class, bucket, days_overdue)
            items.append(QueueItem(
                ticker=ticker,
                list_type=list_type,
                endpoint=endpoint,
                period=period,
                suffix=suffix,
                endpoint_class=endpoint_class,
                bucket=bucket,
                last_pulled=last_pulled,
                last_status=status,
                days_overdue=days_overdue,
                priority=priority,
            ))

    counts: dict[str, int] = {}
    for item in items:
        counts[item.bucket] = counts.get(item.bucket, 0) + 1

    items.sort(key=lambda i: (i.priority, i.ticker, i.endpoint, i.period))
    return AuditReport(generated_at=now, items=items, counts=counts)


# ---------------------------------------------------------------------------
# Daily budget ledger
# ---------------------------------------------------------------------------


def _budget_path(d: date | None = None) -> Path:
    d = d or date.today()
    return CACHE_DIR / f"budget_{d.isoformat()}.json"


def _read_budget_used_today() -> int:
    """How many HTTP attempts has save_fmp_data logged today?"""
    p = _budget_path()
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return int(data.get("calls_made", 0))


def remaining_budget(tier: TierConfig) -> int:
    """Calls left in today's tier cap. sys.maxsize on uncapped tiers."""
    used = _read_budget_used_today()
    if tier.calls_per_day >= _UNLIMITED:
        return _UNLIMITED
    return max(0, tier.calls_per_day - used)


# ---------------------------------------------------------------------------
# Lockfile (concurrency safety on free tier)
# ---------------------------------------------------------------------------


def _read_lock_owner() -> int | None:
    if not LOCK_PATH.exists():
        return None
    try:
        return int(LOCK_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # Windows: openProcess fails on dead PIDs. Use tasklist as a portable check.
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in r.stdout
        except (subprocess.SubprocessError, OSError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _acquire_lock() -> bool:
    """True if we got the lock; False if a live process already holds it.

    Stale locks (PID dead) get cleaned up automatically.
    """
    owner = _read_lock_owner()
    if owner is not None and _pid_alive(owner):
        return False
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _release_lock() -> None:
    if LOCK_PATH.exists() and _read_lock_owner() == os.getpid():
        LOCK_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    conn = sqlite3.connect(args.db)
    try:
        report = audit(
            conn,
            only_list_types=frozenset({args.only}) if args.only else None,
            explicit_tickers=_split_tickers(args.tickers),
            force=args.force,
        )
    finally:
        conn.close()

    queueable = report.queueable()
    summary = {
        "generated_at": report.generated_at.isoformat(timespec="seconds"),
        "total_endpoints": len(report.items),
        "by_bucket": report.counts,
        "queueable": len(queueable),
        "by_list_type": {},
        "top_10_priority": [
            {
                "priority": i.priority,
                "ticker": i.ticker,
                "list_type": i.list_type,
                "endpoint": i.endpoint,
                "period": i.period,
                "bucket": i.bucket,
                "days_overdue": i.days_overdue,
                "last_status": i.last_status,
            }
            for i in queueable[:10]
        ],
    }
    for item in queueable:
        summary["by_list_type"][item.list_type] = summary["by_list_type"].get(item.list_type, 0) + 1

    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    used = _read_budget_used_today()
    remaining = remaining_budget(tier)

    queue_summary: dict[str, object] = {"exists": QUEUE_PATH.exists()}
    if QUEUE_PATH.exists():
        try:
            data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
            queue_summary["entries"] = len(data.get("items", []))
            queue_summary["generated_at"] = data.get("generated_at")
        except (OSError, json.JSONDecodeError):
            queue_summary["error"] = "unreadable"

    lock_owner = _read_lock_owner()
    print(json.dumps({
        "tier": tier.name,
        "tier_caps": {
            "calls_per_day": (tier.calls_per_day if tier.calls_per_day < _UNLIMITED else "unlimited"),
            "calls_per_sec": tier.calls_per_sec,
        },
        "budget_today": {
            "used": used,
            "remaining": (remaining if remaining < _UNLIMITED else "unlimited"),
        },
        "lock": {"owner_pid": lock_owner, "alive": _pid_alive(lock_owner) if lock_owner else False},
        "queue": queue_summary,
    }, indent=2))
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    import db as portfolio_db  # noqa: E402
    archived = portfolio_db.archive_company(args.ticker)
    print(json.dumps({"ticker": args.ticker.upper(), "archived": archived}))
    return 0 if archived else 1


def cmd_reactivate(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    import db as portfolio_db  # noqa: E402
    reactivated = portfolio_db.reactivate_company(args.ticker)
    print(json.dumps({"ticker": args.ticker.upper(), "reactivated": reactivated}))
    return 0 if reactivated else 1


def cmd_run(args: argparse.Namespace) -> int:
    if not _acquire_lock():
        owner = _read_lock_owner()
        sys.stderr.write(f"another cacher run holds lock (pid {owner}); exiting\n")
        return 0  # not a failure; just skip

    try:
        return _run_under_lock(args)
    finally:
        _release_lock()


def _maybe_refresh_earnings_hints() -> None:
    """Run the earnings-calendar surrogate once per day before audit.

    Costs 1 FMP call. Skipped if hints file is fresher than 23h.
    """
    if HINTS_PATH.exists():
        mtime = datetime.fromtimestamp(HINTS_PATH.stat().st_mtime)
        if (datetime.now() - mtime) < timedelta(hours=23):
            return
    script = PROJECT_ROOT / "execution" / "schedule_pre_earnings_refresh.py"
    if not script.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        sys.stderr.write(f"[hints] surrogate failed: {type(e).__name__}: {e}\n")


def _run_under_lock(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    _maybe_refresh_earnings_hints()
    conn = sqlite3.connect(args.db)
    try:
        report = audit(
            conn,
            only_list_types=frozenset({args.only}) if args.only else None,
            explicit_tickers=_split_tickers(args.tickers),
            force=args.force,
        )
    finally:
        conn.close()

    queueable = report.queueable()
    if not queueable:
        print(json.dumps({"event": "no_work", "audit_counts": report.counts}, indent=2))
        return 0

    # Apply budget cap. Each queue entry costs ~1.05 HTTP attempts on average
    # (try-ladder for OK endpoints succeeds first try; failed/forbidden may
    # exhaust 5+). We use 1.0 as the conservative estimate so we don't undershoot.
    budget = args.max_calls if args.max_calls is not None else remaining_budget(tier)
    if budget <= 0:
        print(json.dumps({
            "event": "budget_exhausted",
            "tier": tier.name,
            "deferred": len(queueable),
            "audit_counts": report.counts,
        }, indent=2))
        return 0

    capped = queueable[:budget]
    deferred = len(queueable) - len(capped)

    manifest_entries = [i.to_manifest_entry() for i in capped]
    QUEUE_PATH.write_text(
        json.dumps({
            "generated_at": report.generated_at.isoformat(timespec="seconds"),
            "tier": tier.name,
            "items": manifest_entries,
            "deferred": deferred,
            "audit_counts": report.counts,
        }, indent=2),
        encoding="utf-8",
    )

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "tier": tier.name,
            "would_dispatch": len(capped),
            "deferred": deferred,
            "audit_counts": report.counts,
        }, indent=2))
        return 0

    # Set rate limit env var for save_fmp_data's TokenBucket
    env = os.environ.copy()
    env["FMP_RATE_LIMIT_PER_SEC"] = str(tier.calls_per_sec)

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "execution" / "save_fmp_data.py"),
        "--manifest", str(QUEUE_PATH),
        "--max-calls", str(budget),
    ]
    log_path = CACHE_DIR / f"run_{report.generated_at.strftime('%Y%m%dT%H%M%S')}.log"
    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"# refresh_cache: tier={tier.name} budget={budget} dispatch={len(capped)} deferred={deferred}\n")
        logf.flush()
        proc = subprocess.run(
            cmd, stdout=logf, stderr=subprocess.STDOUT, check=False, env=env,
        )

    print(json.dumps({
        "tier": tier.name,
        "dispatched": len(capped),
        "deferred": deferred,
        "fetcher_exit_code": proc.returncode,
        "log": str(log_path.relative_to(PROJECT_ROOT)),
        "audit_counts": report.counts,
    }, indent=2))
    return proc.returncode


def _spawn_background(args: argparse.Namespace) -> int:
    """Re-exec self with --background-child and detach."""
    forwarded = [
        sys.executable,
        str(PROJECT_ROOT / "execution" / "refresh_cache.py"),
        "run",
        "--background-child",
    ]
    if args.tier:
        forwarded += ["--tier", args.tier]
    if args.force:
        forwarded.append("--force")
    if args.tickers:
        forwarded += ["--tickers", args.tickers]
    if args.only:
        forwarded += ["--only", args.only]
    if args.max_calls is not None:
        forwarded += ["--max-calls", str(args.max_calls)]
    if args.db != str(DB_PATH):
        forwarded += ["--db", args.db]

    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    log_path = CACHE_DIR / f"background_{datetime.now().strftime('%Y%m%dT%H%M%S')}.log"
    with log_path.open("w", encoding="utf-8") as logf:
        subprocess.Popen(
            forwarded,
            stdout=logf, stderr=subprocess.STDOUT,
            close_fds=True, creationflags=creationflags,
        )
    print(json.dumps({"spawned_background": True, "log": str(log_path)}))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _split_tickers(s: str | None) -> list[str] | None:
    if not s:
        return None
    return [t.strip().upper() for t in s.split(",") if t.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(DB_PATH))
    common.add_argument("--tier", choices=sorted(TIERS))
    common.add_argument("--force", action="store_true",
                        help="Ignore cadence; queue every endpoint in scope")
    common.add_argument("--tickers", help="Comma-separated tickers (subset filter)")
    common.add_argument("--only", choices=sorted(_LIST_TYPE_BASE_FRESH_H),
                        help="Restrict to one list_type tier")
    common.add_argument("--max-calls", type=int, default=None,
                        help="Override tier daily cap for this invocation")

    p_run = sub.add_parser("run", parents=[common], help="Audit + dispatch (default)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Audit and write queue.json but don't invoke fetcher")
    p_run.add_argument("--background", action="store_true",
                       help="Detach and exit immediately")
    p_run.add_argument("--background-child", action="store_true",
                       help=argparse.SUPPRESS)

    sub.add_parser("audit", parents=[common], help="Audit only (no fetch)")

    p_status = sub.add_parser("status", help="Show tier, budget, queue state")
    p_status.add_argument("--tier", choices=sorted(TIERS))

    p_archive = sub.add_parser("archive", help="Archive a tracked company")
    p_archive.add_argument("ticker")

    p_reactivate = sub.add_parser("reactivate", help="Reactivate an archived company")
    p_reactivate.add_argument("ticker")

    args = ap.parse_args()

    # Default to "run" if no subcommand
    if args.cmd is None:
        # Re-parse with `run` injected
        args = ap.parse_args(["run"] + sys.argv[1:])

    if args.cmd == "audit":
        return cmd_audit(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "archive":
        return cmd_archive(args)
    if args.cmd == "reactivate":
        return cmd_reactivate(args)
    if args.cmd == "run":
        if args.background and not args.background_child:
            return _spawn_background(args)
        return cmd_run(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
