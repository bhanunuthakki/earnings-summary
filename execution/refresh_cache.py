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
    free     250 calls/day; /stable only — the v3/v4 fallback rungs are dropped
             (they 403 globally on free) by propagating FMP_TIER=free to the fetcher
    basic    250 calls/day, no rate limit (we throttle to 4/sec for steady drip)
    starter  no daily cap, 300 calls/min
    premium  no daily cap, 720 calls/min (we use 720, not 750, for headroom)

Clone rehearsal:
    --offline-corpus-only  adopt eligible immutable statement files through the
                           governed recovery path without credentials or network
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from typing import BinaryIO, Literal, Self, TypedDict, cast

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.python_process import managed_python_prefix  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from log_redact import redact  # noqa: E402
from models.companies import ListType  # noqa: E402
from pipeline import cadence_policy as _cadence_policy  # noqa: E402
from pipeline.fmp_recovery import (  # noqa: E402
    SCREENING_ENDPOINT_KEYS,
    CircuitConfig,
    CircuitState,
    CorpusSnapshot,
    CredentialAvailability,
    EnqueueWorkRequest,
    ExecutionMode,
    FmpSnapshotProof,
    OutcomeCode,
    PlannedWork,
    ReceiptStatus,
    RecordOutcomesRequest,
    RecoverableWorkRequest,
    RecoveryAvailability,
    WorkOutcome,
    WorkSpec,
    enqueue_work,
    make_work_id,
    record_outcomes,
    recoverable_work,
)
from pipeline.source_policy import (  # noqa: E402
    POLICY_VERSION,
    ArtifactKind,
    CollectionSource,
    decision_for,
    issuer_policy,
)
from provenance.financial_fact_resolution import (  # noqa: E402
    governed_document_fact_admission,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
ENV_FILE = PROJECT_ROOT / ".env"
CACHE_DIR = PROJECT_ROOT / ".tmp" / "cacher"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOCK_PATH = CACHE_DIR / ".lock"
OFFLINE_LOCK_PATH = CACHE_DIR / ".offline-corpus.lock"
QUEUE_PATH = CACHE_DIR / "queue.json"
HINTS_PATH = CACHE_DIR / "forced_stale.json"
FMP_DIR = PROJECT_ROOT / "data" / "historical" / "fmp"


@dataclass(frozen=True)
class FmpAuthConfig:
    """Validated FMP credential plus its non-secret configuration source."""

    api_key: str = field(repr=False)
    source: Literal["environment", "project_dotenv"]


class FmpAuthError(RuntimeError):
    """The cache cannot dispatch because no FMP credential is configured."""


@dataclass(frozen=True)
class RecoveryCredentialDecision:
    """Credential state after consulting the provider circuit first."""

    credentials: CredentialAvailability
    auth: FmpAuthConfig | None
    network_permitted: bool
    hints_permitted: bool


class _FmpJob(TypedDict):
    path: str
    period: str | None
    suffix: str


def load_fmp_auth(
    *,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> FmpAuthConfig:
    """Resolve FMP auth from the process environment, then project ``.env``.

    Scheduled processes can receive credentials through their environment and
    must not be forced to keep a checkout-local secret file. The project
    ``.env`` remains the compatibility fallback used by the rest of the FMP
    fleet.
    """
    source_environment = os.environ if environ is None else environ
    environment_value = source_environment.get("FMP_API_KEY", "").strip()
    if environment_value:
        return FmpAuthConfig(api_key=environment_value, source="environment")

    dotenv_path = ENV_FILE if env_file is None else env_file
    try:
        dotenv_value = dotenv_values(dotenv_path).get("FMP_API_KEY")
    except (OSError, UnicodeError) as exc:
        raise FmpAuthError(f"unable to read FMP configuration: {redact(exc)}") from None
    if isinstance(dotenv_value, str) and dotenv_value.strip():
        return FmpAuthConfig(api_key=dotenv_value.strip(), source="project_dotenv")

    raise FmpAuthError(
        "FMP_API_KEY is missing from the process environment and project dotenv configuration"
    )


def _prepare_fmp_auth() -> bool:
    """Load validated auth into the environment inherited by the fetcher."""
    try:
        config = load_fmp_auth()
    except FmpAuthError as exc:
        print(
            json.dumps(
                {
                    "event": "refresh_cache_config_error",
                    "error": redact(exc),
                }
            ),
            file=sys.stderr,
        )
        return False
    os.environ["FMP_API_KEY"] = config.api_key
    return True


def decide_recovery_credentials(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    auth_loader: Callable[[], FmpAuthConfig] = load_fmp_auth,
) -> RecoveryCredentialDecision:
    """Consult durable circuit state before reading auth or allowing FMP I/O."""
    row = connection.execute(
        "SELECT state,next_probe_at FROM provider_circuit_state WHERE provider='fmp'"
    ).fetchone()
    if row is not None:
        state = CircuitState(str(row["state"]))
        next_probe_at = (
            datetime.fromisoformat(str(row["next_probe_at"]))
            if row["next_probe_at"] is not None
            else None
        )
        if state is CircuitState.HALF_OPEN or (
            state is CircuitState.OPEN and next_probe_at is not None and next_probe_at > now
        ):
            return RecoveryCredentialDecision(
                credentials=CredentialAvailability.AVAILABLE,
                auth=None,
                network_permitted=False,
                hints_permitted=False,
            )
    try:
        auth = auth_loader()
    except FmpAuthError:
        return RecoveryCredentialDecision(
            credentials=CredentialAvailability.MISSING,
            auth=None,
            network_permitted=False,
            hints_permitted=False,
        )
    is_probe = row is not None and str(row["state"]) == CircuitState.OPEN.value
    return RecoveryCredentialDecision(
        credentials=CredentialAvailability.AVAILABLE,
        auth=auth,
        network_permitted=True,
        hints_permitted=not is_probe,
    )


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
    calls_per_day: int  # daily cap; sys.maxsize for unlimited
    calls_per_sec: float  # rate-limit target


_UNLIMITED = sys.maxsize

TIERS: dict[str, TierConfig] = {
    # free == post-downgrade: 250/day, and save_fmp_data/fetch_etf_data drop the
    # v3/v4 rungs (they 403 globally on free) when FMP_TIER=free is propagated.
    "free": TierConfig("free", calls_per_day=250, calls_per_sec=4.0),
    "basic": TierConfig("basic", calls_per_day=250, calls_per_sec=4.0),
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
    "portfolio": 24,
    "watchlist": 24,
    "evaluation": 24,
    "none": 24 * 90,
    "etf": 24 * 30,
    "index_member": 24 * 30,
}

# Endpoint classification — drives priority weights and cadence multipliers.
# An endpoint's class is matched by substring on the path; first match wins.
_ENDPOINT_CLASSES: list[tuple[str, str]] = [
    ("discounted-cash-flow", "time_sensitive"),
    ("ratings", "time_sensitive"),
    ("grades", "time_sensitive"),
    ("price-target", "time_sensitive"),
    ("ratios-ttm", "time_sensitive"),
    ("metrics-ttm", "time_sensitive"),
    ("statements-ttm", "time_sensitive"),
    ("statement-ttm", "time_sensitive"),
    ("financial-scores", "time_sensitive"),
    ("profile", "time_sensitive"),
    ("shares-float", "time_sensitive"),
    ("market-capitalization", "time_sensitive"),
    ("analyst-estimates", "time_sensitive"),
    ("price-eod", "time_sensitive"),
    ("growth", "growth"),
    ("segmentation", "segment"),
    ("segments", "segment"),
    ("statement", "statement"),
    ("balance-sheet", "statement"),
    ("cashflow", "statement"),
    ("income-", "statement"),
    ("ratios", "statement"),
    ("key-metrics", "statement"),
    ("enterprise-values", "statement"),
    ("owner-earnings", "statement"),
    ("financial-reports", "statement"),
    ("form-10-k-json", "statement"),
    ("peers", "reference"),
    ("executives", "reference"),
    ("employee", "reference"),
]

# Cadence multipliers per endpoint class. 1.0 = use list_type default freshness;
# >1.0 = stays fresh longer (e.g. quarterly statements don't need daily refresh).
# The numeric values mirror src/pipeline/cadence_policy.py — for portfolio +
# watchlist (base = 24h = 1d), `mult` equals the freshness in days, so
# statement=14.0 here matches STATEMENT_STALE_DAYS=14 there. Update both
# when editing the policy.
_CLASS_CADENCE_MULT: dict[str, float] = {
    "time_sensitive": float(_cadence_policy.TIME_SENSITIVE_STALE_DAYS),
    "growth": float(_cadence_policy.GROWTH_STALE_DAYS),
    "segment": float(_cadence_policy.STATEMENT_STALE_DAYS),
    "statement": float(_cadence_policy.STATEMENT_STALE_DAYS),
    "reference": float(_cadence_policy.REFERENCE_STALE_DAYS),
}

_CLASS_PRIORITY_WEIGHT: dict[str, int] = {
    "time_sensitive": 0,
    "growth": 100,
    "segment": 200,
    "statement": 300,
    "reference": 400,
}

_LIST_TYPE_PRIORITY_WEIGHT: dict[str, int] = {
    "portfolio": 0,
    "evaluation": 500,
    "watchlist": 1000,
    "none": 2000,
    "etf": 3000,
    "index_member": 4000,
}

# Status -> bucket. Drives priority bonus and retry eligibility.
_BUCKET_PRIORITY_BONUS: dict[str, int] = {
    "missing": -50,  # never-pulled: do these first
    "stale": 0,
    "failed_recent": _UNLIMITED,  # don't retry within retry_window
    "failed_retry_ok": 30,  # error/forbidden but enough time has passed
    "fresh": _UNLIMITED,  # never queue
}

# How long to wait before retrying a failed endpoint, by failure class.
_RETRY_WINDOW_DAYS: dict[str, int] = {
    "forbidden": 30,  # tier-restricted: don't hammer until tier likely changed
    "error": 1,  # transient errors: try again tomorrow
    "empty": 7,  # endpoint returned empty list; rare but real
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
    bucket: str  # missing | stale | failed_retry_ok | failed_recent | fresh
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
    counts: dict[str, int] = field(default_factory=dict[str, int])

    def queueable(self) -> list[QueueItem]:
        return [i for i in self.items if i.bucket in ("missing", "stale", "failed_retry_ok")]


RecoveryDispatcher = Callable[[sqlite3.Connection, QueueItem, PlannedWork], WorkOutcome]
CorpusAdmitter = Callable[
    [sqlite3.Connection, QueueItem, PlannedWork, Path, Path, datetime], WorkOutcome
]
BacklogItemResolver = Callable[[sqlite3.Row], QueueItem | None]


@dataclass(frozen=True)
class RecoveryRunResult:
    """One structured runtime receipt suitable for stdout and process exit."""

    run_id: str
    status: ReceiptStatus
    planned_count: int
    dispatch_count: int
    fresh_count: int
    corpus_count: int
    admitted_new_count: int
    already_applied_count: int
    failed_count: int
    circuit_state: CircuitState
    circuit_revision: int
    pending_count: int

    @property
    def exit_code(self) -> int:
        return {
            ReceiptStatus.FRESH: 0,
            ReceiptStatus.DEGRADED_CORPUS: 2,
            ReceiptStatus.PARTIAL: 3,
            ReceiptStatus.FAILED: 4,
        }[self.status]

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "planned_count": self.planned_count,
            "dispatch_count": self.dispatch_count,
            "fresh_count": self.fresh_count,
            "corpus_count": self.corpus_count,
            "admitted_new_count": self.admitted_new_count,
            "already_applied_count": self.already_applied_count,
            "failed_count": self.failed_count,
            "circuit_state": self.circuit_state.value,
            "circuit_revision": self.circuit_revision,
            "pending_count": self.pending_count,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class RawCorpusManifestEntry:
    """Stable proof that one corpus path and its bytes were not modified."""

    relative_path: str
    size_bytes: int
    content_sha256: str
    modified_at_ns: int


@dataclass(frozen=True)
class RawCorpusManifest:
    """Compact whole-corpus preservation proof used by offline rehearsal."""

    entries: tuple[RawCorpusManifestEntry, ...]
    total_bytes: int
    manifest_sha256: str


class OfflineCorpusRunResult(BaseModel):
    """Terminal receipt for a guaranteed-zero-network corpus replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: ReceiptStatus
    discovered_file_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    admitted_count: int = Field(ge=0)
    admitted_new_count: int = Field(ge=0)
    already_applied_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    corpus_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    deferred_count: int = Field(ge=0)
    excluded_by_tier_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    manifest_sha256: str
    manifest_before_sha256: str
    manifest_after_sha256: str
    manifest_unchanged: bool
    network_calls: Literal[0] = 0
    mode: Literal["offline_corpus_only"] = "offline_corpus_only"

    @model_validator(mode="after")
    def _receipt_consistency(self) -> Self:
        prefix = "offline-corpus:"
        if not self.run_id.startswith(prefix):
            raise ValueError("offline run_id must use the offline-corpus UUID namespace")
        raw_uuid = self.run_id[len(prefix) :]
        try:
            parsed_uuid = uuid.UUID(raw_uuid)
        except ValueError as exc:
            raise ValueError("offline run_id must contain a UUID") from exc
        if str(parsed_uuid) != raw_uuid or parsed_uuid.version != 4:
            raise ValueError("offline run_id must contain a canonical lowercase UUID4")
        for field_name, value in (
            ("manifest_sha256", self.manifest_sha256),
            ("manifest_before_sha256", self.manifest_before_sha256),
            ("manifest_after_sha256", self.manifest_after_sha256),
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{field_name} must be a lowercase SHA-256")
        if self.manifest_sha256 != self.manifest_before_sha256:
            raise ValueError("manifest_sha256 must identify the before manifest")
        if self.manifest_unchanged and (self.manifest_before_sha256 != self.manifest_after_sha256):
            raise ValueError("unchanged corpus must have equal before and after manifests")
        if self.admitted_count != self.admitted_new_count + self.already_applied_count:
            raise ValueError("admitted count must split into new and already-applied counts")
        if self.admitted_count != self.corpus_count:
            raise ValueError("corpus count must equal admitted count")
        if self.eligible_count != self.selected_count:
            raise ValueError("eligible count must equal selected count")
        if (
            self.discovered_file_count
            != self.selected_count + self.excluded_by_tier_count + self.skipped_count
        ):
            raise ValueError("discovered corpus arithmetic is inconsistent")
        if self.selected_count != self.admitted_count + self.failed_count + self.deferred_count:
            raise ValueError("selected work arithmetic is inconsistent")
        if self.pending_count > self.selected_count:
            raise ValueError("pending count cannot exceed selected work")
        expected_status = ReceiptStatus.FAILED
        if self.manifest_unchanged and self.admitted_count > 0:
            expected_status = (
                ReceiptStatus.PARTIAL
                if self.failed_count > 0 or self.deferred_count > 0
                else ReceiptStatus.DEGRADED_CORPUS
            )
        if self.status is not expected_status:
            raise ValueError("offline receipt status is inconsistent with its outcomes")
        return self

    @property
    def exit_code(self) -> int:
        return {
            ReceiptStatus.FRESH: 0,
            ReceiptStatus.DEGRADED_CORPUS: 2,
            ReceiptStatus.PARTIAL: 3,
            ReceiptStatus.FAILED: 4,
        }[self.status]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "run_id": self.run_id,
            "status": self.status.value,
            "discovered_file_count": self.discovered_file_count,
            "selected_count": self.selected_count,
            "admitted_count": self.admitted_count,
            "admitted_new_count": self.admitted_new_count,
            "already_applied_count": self.already_applied_count,
            "eligible_count": self.eligible_count,
            "corpus_count": self.corpus_count,
            "failed_count": self.failed_count,
            "deferred_count": self.deferred_count,
            "excluded_by_tier_count": self.excluded_by_tier_count,
            "skipped_count": self.skipped_count,
            "pending_count": self.pending_count,
            "network_calls": self.network_calls,
            "manifest_sha256": self.manifest_sha256,
            "manifest_before_sha256": self.manifest_before_sha256,
            "manifest_after_sha256": self.manifest_after_sha256,
            "manifest_unchanged": self.manifest_unchanged,
            "exit_code": self.exit_code,
        }


def _raw_corpus_manifest(path: Path) -> RawCorpusManifest:
    """Hash every regular corpus file without changing path metadata or bytes."""
    entries: list[RawCorpusManifestEntry] = []
    for file_path in _safe_corpus_files(path):
        content, stable_stat = _read_stable_corpus_file(path, file_path)
        entries.append(
            RawCorpusManifestEntry(
                relative_path=str(file_path.relative_to(path)).replace("\\", "/"),
                size_bytes=stable_stat.st_size,
                content_sha256=hashlib.sha256(content).hexdigest(),
                modified_at_ns=stable_stat.st_mtime_ns,
            )
        )
    canonical = json.dumps(
        [
            {
                "content_sha256": entry.content_sha256,
                "modified_at_ns": entry.modified_at_ns,
                "relative_path": entry.relative_path,
                "size_bytes": entry.size_bytes,
            }
            for entry in entries
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return RawCorpusManifest(
        entries=tuple(entries),
        total_bytes=sum(entry.size_bytes for entry in entries),
        manifest_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _is_reparse_point(stat_result: os.stat_result) -> bool:
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    return bool(attributes & 0x400)


def _safe_corpus_files(root: Path) -> tuple[Path, ...]:
    """Enumerate corpus files without following links or Windows reparse points."""
    if root.is_symlink():
        raise ValueError(f"unsafe corpus entry: {root}")
    if not root.exists():
        return ()
    root_stat = root.lstat()
    if root.is_symlink() or _is_reparse_point(root_stat):
        raise ValueError(f"unsafe corpus entry: {root}")
    root_resolved = root.resolve(strict=True)
    files: list[Path] = []

    def walk(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                entry_stat = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or _is_reparse_point(entry_stat):
                    raise ValueError(f"unsafe corpus entry: {entry_path}")
                try:
                    entry_path.resolve(strict=True).relative_to(root_resolved)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"unsafe corpus entry: {entry_path}") from exc
                if entry.is_dir(follow_symlinks=False):
                    walk(entry_path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(entry_path)
                else:
                    raise ValueError(f"unsafe corpus entry: {entry_path}")

    walk(root)
    return tuple(sorted(files))


def _validate_corpus_ancestors(root: Path, path: Path) -> None:
    """Reject every link/reparse/traversal component from root through path."""
    root_resolved = root.resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"unsafe corpus entry: {path}") from exc
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise ValueError(f"unsafe corpus entry: {path}")
        current = current / part
        current_stat = current.lstat()
        if current.is_symlink() or _is_reparse_point(current_stat):
            raise ValueError(f"unsafe corpus entry: {current}")
    try:
        path.resolve(strict=True).relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise ValueError(f"unsafe corpus entry: {path}") from exc


@dataclass
class _HeldCorpusFile:
    """One stable corpus file whose native handle remains locked for admission."""

    path: Path
    handle: BinaryIO
    content: bytes
    stat_result: os.stat_result

    def reread(self) -> tuple[bytes, os.stat_result]:
        self.handle.seek(0)
        content = self.handle.read()
        current = os.fstat(self.handle.fileno())
        if _file_identity(current) != _file_identity(self.stat_result):
            raise RuntimeError(f"corpus changed while held: {self.path.name}")
        if len(content) != current.st_size:
            raise RuntimeError(f"corpus changed while held: {self.path.name}")
        return content, current


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _windows_locked_fd(root: Path, path: Path) -> int:
    """Open a Windows read handle that denies concurrent writes and deletes."""
    import ctypes
    import msvcrt

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("file_attributes", ctypes.c_ulong), ("reparse_tag", ctypes.c_ulong)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    get_attribute_tag = kernel32.GetFileInformationByHandleEx
    get_attribute_tag.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    )
    get_attribute_tag.restype = ctypes.c_int
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    )
    get_final_path.restype = ctypes.c_ulong

    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ: deny writers and deletion while held
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise ctypes.WinError(ctypes.get_last_error())
    handle_value = int(handle)
    native_handle = ctypes.c_void_p(handle_value)
    try:
        tag_info = FileAttributeTagInfo()
        if not get_attribute_tag(
            native_handle,
            9,  # FileAttributeTagInfo
            ctypes.byref(tag_info),
            ctypes.sizeof(tag_info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if tag_info.file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise ValueError(f"unsafe corpus entry: {path}")

        final_path_buffer = ctypes.create_unicode_buffer(32_768)
        final_length = get_final_path(
            native_handle,
            final_path_buffer,
            len(final_path_buffer),
            0,
        )
        if final_length == 0 or final_length >= len(final_path_buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        final_path_text = final_path_buffer.value
        if final_path_text.startswith("\\\\?\\UNC\\"):
            final_path_text = "\\\\" + final_path_text[8:]
        elif final_path_text.startswith("\\\\?\\"):
            final_path_text = final_path_text[4:]
        final_path = Path(final_path_text)
        final_path.relative_to(root.resolve(strict=True))

        descriptor = msvcrt.open_osfhandle(
            handle_value,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(native_handle)
        raise
    return descriptor


@contextmanager
def _held_corpus_file(root: Path, path: Path) -> Generator[_HeldCorpusFile]:
    """Hold one fail-closed, no-follow corpus handle through its consumer."""
    _validate_corpus_ancestors(root, path)
    if os.name == "nt":
        descriptor = _windows_locked_fd(root, path)
    else:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise RuntimeError("secure corpus snapshots require O_NOFOLLOW")
        descriptor = os.open(path, os.O_RDONLY | nofollow)
    handle = os.fdopen(descriptor, "rb")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _is_reparse_point(before):
            raise ValueError(f"unsafe corpus entry: {path}")
        content = handle.read()
        after = os.fstat(descriptor)
        _validate_corpus_ancestors(root, path)
        if _file_identity(before) != _file_identity(after) or len(content) != before.st_size:
            raise RuntimeError(f"corpus changed while reading: {path.name}")
        held = _HeldCorpusFile(path=path, handle=handle, content=content, stat_result=after)
        yield held
        held.reread()
    finally:
        handle.close()


def _read_stable_corpus_file(root: Path, path: Path) -> tuple[bytes, os.stat_result]:
    """Read once from one validated handle and prove its identity stayed stable."""
    with _held_corpus_file(root, path) as held:
        return held.content, held.stat_result


def _generic_fmp_policy_sha256(ticker: str) -> str:
    payload = json.dumps(
        {
            "fmp_rules": {"endpoint_aliases": [], "label_overrides": []},
            "issuer_id": f"ticker:{ticker.upper()}",
            "policy_version": POLICY_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _policy_sha256(ticker: str) -> str:
    try:
        return issuer_policy(ticker).policy_sha256
    except ValueError:
        return _generic_fmp_policy_sha256(ticker)


def _corpus_snapshot(path: Path, *, root: Path | None = None) -> CorpusSnapshot | None:
    """Hash a stable raw file without opening a transaction or modifying it."""
    try:
        content, after = _read_stable_corpus_file(root or path.parent, path)
    except (OSError, RuntimeError, ValueError):
        return None
    captured_at = datetime.fromtimestamp(after.st_mtime, UTC).replace(tzinfo=None)
    return CorpusSnapshot(
        cache_generation_id=f"raw:{after.st_mtime_ns}:{after.st_size}",
        content_sha256=hashlib.sha256(content).hexdigest(),
        captured_at=captured_at,
    )


def _held_snapshot(content: bytes, stat_result: os.stat_result) -> CorpusSnapshot:
    captured_at = datetime.fromtimestamp(stat_result.st_mtime, UTC).replace(tzinfo=None)
    return CorpusSnapshot(
        cache_generation_id=f"raw:{stat_result.st_mtime_ns}:{stat_result.st_size}",
        content_sha256=hashlib.sha256(content).hexdigest(),
        captured_at=captured_at,
    )


def _naive_utc(value: datetime) -> datetime:
    """Normalize an aware repository clock value to the DB's naive-UTC contract."""
    if value.tzinfo is None:
        raise ValueError("repository clock must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_now() -> datetime:
    return _naive_utc(datetime.now(UTC))


def _last_pulled(
    connection: sqlite3.Connection,
    *,
    item: QueueItem,
) -> str | None:
    row = connection.execute(
        "SELECT last_pulled FROM fmp_endpoint_status WHERE ticker=? AND endpoint=? AND period=?",
        (item.ticker, item.endpoint, item.period),
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _admit_corpus(
    connection: sqlite3.Connection,
    item: QueueItem,
    planned: PlannedWork,
    raw_corpus_dir: Path,
    project_root: Path,
    observed_at: datetime,
) -> WorkOutcome:
    """Index and extract one immutable corpus artifact before accepting it."""
    if planned.lease_token is None:
        raise ValueError("corpus admission requires a typed corpus lease")
    lease_token = planned.lease_token
    path = raw_corpus_dir / f"{item.ticker}_{item.suffix}.json"
    try:
        with _held_corpus_file(raw_corpus_dir, path) as held:
            return _admit_held_corpus(
                connection,
                item,
                planned,
                raw_corpus_dir,
                project_root,
                observed_at,
                held,
            )
    except (OSError, RuntimeError, ValueError):
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=lease_token,
            outcome_code=OutcomeCode.CORPUS_UNAVAILABLE,
            observed_at=observed_at,
        )


def _admit_held_corpus(
    connection: sqlite3.Connection,
    item: QueueItem,
    planned: PlannedWork,
    raw_corpus_dir: Path,
    project_root: Path,
    observed_at: datetime,
    held: _HeldCorpusFile,
) -> WorkOutcome:
    """Complete governed admission while the selected corpus handle is locked."""
    if connection.in_transaction:
        raise RuntimeError("corpus admission cannot start inside a transaction")
    if planned.lease_token is None or planned.corpus_snapshot is None:
        raise ValueError("corpus admission requires a typed corpus lease")
    path = held.path
    before_snapshot = _held_snapshot(held.content, held.stat_result)
    before_stat = held.stat_result
    last_pulled_before = _last_pulled(connection, item=item)
    unavailable = WorkOutcome(
        work_id=planned.work_id,
        lease_token=planned.lease_token,
        outcome_code=OutcomeCode.CORPUS_UNAVAILABLE,
        observed_at=observed_at,
    )
    if before_snapshot != planned.corpus_snapshot:
        return unavailable

    corpus_snapshot = planned.corpus_snapshot
    # Parse and classify before the short document write; the admission path is
    # intentionally limited to statement endpoints that produce governed facts.
    try:
        payload: object = json.loads(held.content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return unavailable
    if not isinstance(payload, list):
        return unavailable
    records = cast(list[object], payload)
    doc_types = {
        "income_statement": "fmp_income_statement",
        "balance_sheet": "fmp_balance_sheet",
        "cash_flow": "fmp_cashflow",
    }
    statement_kind = next((key for key in doc_types if item.suffix.startswith(key)), None)
    if statement_kind is None:
        return unavailable
    doc_type = doc_types[statement_kind]
    dates: list[datetime] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        record_object = cast(dict[str, object], record)
        for key in ("date", "fillingDate"):
            value = record_object.get(key)
            if isinstance(value, str):
                try:
                    dates.append(datetime.fromisoformat(value[:10]))
                except ValueError:
                    continue
                break
    dates.sort()
    period_end = dates[-1] if dates else None
    try:
        relative_path = str(path.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    except ValueError:
        return unavailable

    connection.execute(
        "INSERT OR IGNORE INTO documents "
        "(ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,"
        "fetch_status,raw_bytes_size,source_url) "
        "VALUES (?,'fmp',?,?,?,?,?,'ok',?,NULL)",
        (
            item.ticker,
            doc_type,
            period_end,
            relative_path,
            corpus_snapshot.content_sha256,
            corpus_snapshot.captured_at,
            before_stat.st_size,
        ),
    )
    connection.commit()
    document = connection.execute(
        "SELECT id FROM documents WHERE ticker=? AND file_path=? AND sha256=?",
        (item.ticker, relative_path, corpus_snapshot.content_sha256),
    ).fetchone()
    if document is None:
        return unavailable
    document_id = int(document[0])
    if connection.in_transaction:
        raise RuntimeError("document indexing left an active transaction before extraction")

    # The fact writer rejects ungoverned documents. Evidence capture performs
    # its disk verification before its ledger writes; commit that short unit
    # before invoking the extractor.
    from provenance.evidence_backfill import ensure_legacy_document_evidence

    try:
        ensure_legacy_document_evidence(
            connection,
            repo_root=project_root,
            document_id=document_id,
        )
        connection.commit()
    except (OSError, ValueError, sqlite3.Error):
        if connection.in_transaction:
            connection.rollback()
        return unavailable
    if connection.in_transaction:
        raise RuntimeError("evidence admission left an active transaction before extraction")

    if doc_type == "fmp_income_statement":
        from compute.income_statement import extract_income_statement_facts as extractor
    elif doc_type == "fmp_balance_sheet":
        from compute.balance_sheet import extract_balance_sheet_facts as extractor
    else:
        from compute.cashflow import extract_cashflow_facts as extractor
    try:
        inserted_count = extractor(connection, document_id, project_root)
        admission = governed_document_fact_admission(
            connection,
            document_id=document_id,
            ticker=item.ticker,
            content_sha256=corpus_snapshot.content_sha256,
            inserted_count=inserted_count,
        )
    except (KeyError, OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
        if connection.in_transaction:
            connection.rollback()
        return unavailable
    if connection.in_transaction:
        raise RuntimeError("corpus extractor returned with an active transaction")

    try:
        after_content, after_stat = held.reread()
        after_snapshot = _held_snapshot(after_content, after_stat)
        path_snapshot = _corpus_snapshot(path, root=raw_corpus_dir)
    except (OSError, RuntimeError, ValueError):
        return unavailable
    raw_unchanged = (
        after_snapshot == before_snapshot
        and path_snapshot == before_snapshot
        and after_stat.st_mtime_ns == before_stat.st_mtime_ns
        and after_stat.st_size == before_stat.st_size
    )
    freshness_unchanged = _last_pulled(connection, item=item) == last_pulled_before
    if admission.status == "empty" or not raw_unchanged or not freshness_unchanged:
        return unavailable
    return WorkOutcome(
        work_id=planned.work_id,
        lease_token=planned.lease_token,
        outcome_code=OutcomeCode.CORPUS_SUCCESS,
        observed_at=observed_at,
        corpus_snapshot=planned.corpus_snapshot,
    )


def _work_spec(
    item: QueueItem,
    *,
    raw_corpus_dir: Path,
    now: datetime,
    owner_request_id: str | None,
) -> WorkSpec:
    role = ListType(item.list_type)
    requested = role is ListType.EVALUATION
    return WorkSpec(
        ticker=item.ticker,
        coverage_role=role,
        endpoint_key=item.suffix,
        period_key=item.period or "current",
        cache_generation_id=f"refresh:{now.date().isoformat()}",
        policy_sha256=_policy_sha256(item.ticker),
        requested=requested,
        owner_request_id=owner_request_id if requested else None,
        corpus_snapshot=_corpus_snapshot(
            raw_corpus_dir / f"{item.ticker}_{item.suffix}.json",
            root=raw_corpus_dir,
        ),
    )


def _status_for_counts(*, fresh: int, corpus: int, failed: int) -> ReceiptStatus:
    if fresh > 0 and failed == 0 and corpus == 0:
        return ReceiptStatus.FRESH
    if corpus > 0 and failed == 0 and fresh == 0:
        return ReceiptStatus.DEGRADED_CORPUS
    if fresh + corpus > 0:
        return ReceiptStatus.PARTIAL
    return ReceiptStatus.FAILED


def _item_from_backlog_row(row: sqlite3.Row) -> QueueItem | None:
    """Resolve durable work back to the existing provider adapter catalog."""
    import save_fmp_data as fmp_save

    ticker = str(row["ticker"])
    list_type = str(row["coverage_role"])
    jobs = cast(list[_FmpJob], fmp_save.per_ticker_jobs(ticker, list_type=list_type))
    job = next(
        (candidate for candidate in jobs if candidate["suffix"] == row["endpoint_key"]), None
    )
    if job is None:
        return None
    endpoint = job["path"]
    period = job["period"] or ""
    return QueueItem(
        ticker=ticker,
        list_type=list_type,
        endpoint=endpoint,
        period=period,
        suffix=job["suffix"],
        endpoint_class=classify_endpoint(endpoint),
        bucket="failed_retry_ok",
        last_pulled=None,
        last_status=None,
        days_overdue=0,
        priority=0,
    )


def _offline_item_from_backlog_row(row: sqlite3.Row) -> QueueItem | None:
    """Resolve offline statement work without importing the provider adapter."""
    suffix = str(row["endpoint_key"])
    endpoint_period = _OFFLINE_STATEMENT_SUFFIXES.get(suffix)
    if endpoint_period is None:
        return None
    endpoint, period = endpoint_period
    return QueueItem(
        ticker=str(row["ticker"]),
        list_type=str(row["coverage_role"]),
        endpoint=endpoint,
        period=period,
        suffix=suffix,
        endpoint_class="statement",
        bucket="failed_retry_ok",
        last_pulled=None,
        last_status=None,
        days_overdue=0,
        priority=0,
    )


def _pending_recovery_context(
    connection: sqlite3.Connection,
    *,
    raw_corpus_dir: Path,
    now: datetime,
    allowed_work_ids: frozenset[str] | None = None,
    item_resolver: BacklogItemResolver = _item_from_backlog_row,
) -> tuple[dict[str, QueueItem], tuple[RecoveryAvailability, ...]]:
    if allowed_work_ids is None:
        rows = connection.execute(
            "SELECT * FROM fmp_work_backlog WHERE state='PENDING' AND available_at <= ? "
            "ORDER BY priority DESC,created_at,ticker,work_id LIMIT 500",
            (now.isoformat(),),
        ).fetchall()
    else:
        allowed_json = json.dumps(sorted(allowed_work_ids), separators=(",", ":"))
        rows = connection.execute(
            "SELECT work.* FROM fmp_work_backlog work "
            "JOIN json_each(?) allowed ON allowed.value=work.work_id "
            "WHERE work.state='PENDING' AND work.available_at <= ? "
            "ORDER BY work.priority DESC,work.created_at,work.ticker,work.work_id LIMIT 500",
            (allowed_json, now.isoformat()),
        ).fetchall()
    items: dict[str, QueueItem] = {}
    availability: list[RecoveryAvailability] = []
    for row in rows:
        work_id = str(row["work_id"])
        item = item_resolver(row)
        if item is not None:
            items[work_id] = item
        availability.append(
            RecoveryAvailability(
                work_id=work_id,
                corpus_snapshot=(
                    _corpus_snapshot(
                        raw_corpus_dir / f"{item.ticker}_{item.suffix}.json",
                        root=raw_corpus_dir,
                    )
                    if item is not None
                    else None
                ),
            )
        )
    return items, tuple(availability)


def run_recovery_batch(
    connection: sqlite3.Connection,
    *,
    items: Sequence[QueueItem],
    credentials: CredentialAvailability,
    raw_corpus_dir: Path,
    now: datetime,
    run_id: str,
    dispatch: RecoveryDispatcher,
    corpus_admitter: CorpusAdmitter | None = None,
    project_root: Path = PROJECT_ROOT,
    worker_id: str = "refresh-cache",
    max_items: int = 500,
    provider_call_budget: int | None = None,
    owner_request_id: str | None = None,
    circuit_config: CircuitConfig | None = None,
    restrict_to_intended: bool = False,
    backlog_item_resolver: BacklogItemResolver = _item_from_backlog_row,
) -> RecoveryRunResult:
    """Persist every authorized intent, then drain a bounded priority batch."""
    if connection.in_transaction:
        raise RuntimeError("recovery runtime requires a connection with no active transaction")
    if not 1 <= max_items <= 500:
        raise ValueError("max_items must be between 1 and 500")
    if provider_call_budget is not None and provider_call_budget < 0:
        raise ValueError("provider_call_budget must be non-negative")
    intended = tuple(items)
    specs = tuple(
        _work_spec(
            item,
            raw_corpus_dir=raw_corpus_dir,
            now=now,
            owner_request_id=owner_request_id or f"refresh-cache:{run_id}",
        )
        for item in intended
    )
    config = circuit_config or CircuitConfig()
    admit_corpus = corpus_admitter or _admit_corpus
    intended_work_ids = frozenset(make_work_id(spec) for spec in specs)
    # Authorization and durable intent are complete before the first lease or call.
    for offset in range(0, len(specs), 500):
        enqueue_work(
            connection,
            EnqueueWorkRequest(
                now=now,
                circuit_config=config,
                work=specs[offset : offset + 500],
            ),
        )
    fresh_count = 0
    corpus_count = 0
    admitted_new_count = 0
    already_applied_count = 0
    failed_count = 0
    dispatch_count = 0
    cursor_now = now
    processed: set[str] = set()
    call_budget = max_items if provider_call_budget is None else provider_call_budget

    while len(processed) < max_items:
        circuit_before = connection.execute(
            "SELECT state FROM provider_circuit_state WHERE provider='fmp'"
        ).fetchone()
        if circuit_before is None:
            raise RuntimeError("FMP recovery circuit was not initialized")
        provider_calls_permitted = dispatch_count < call_budget
        closed_with_auth = (
            provider_calls_permitted
            and credentials is CredentialAvailability.AVAILABLE
            and str(circuit_before["state"]) == CircuitState.CLOSED.value
        )
        remaining = max_items - len(processed)
        # Closed-circuit HTTP leases are deliberately serial: each receipt may
        # open the circuit and must be durable before another provider call.
        limit = 1 if closed_with_auth else remaining
        item_by_id, availability = _pending_recovery_context(
            connection,
            raw_corpus_dir=raw_corpus_dir,
            now=cursor_now,
            allowed_work_ids=intended_work_ids if restrict_to_intended else None,
            item_resolver=backlog_item_resolver,
        )
        if not availability:
            break
        plan = recoverable_work(
            connection,
            RecoverableWorkRequest(
                run_id=run_id,
                worker_id=worker_id,
                now=cursor_now,
                credentials=credentials,
                provider_calls_permitted=provider_calls_permitted,
                availability=availability,
                allowed_work_ids=(
                    tuple(sorted(intended_work_ids)) if restrict_to_intended else None
                ),
                limit=limit,
            ),
        )
        if not plan.items:
            break
        newly_processed = 0
        provider_reachable_probe = False
        for planned in plan.items:
            if planned.work_id in processed:
                continue
            item = item_by_id.get(planned.work_id)
            mode = planned.execution_mode
            if mode in {ExecutionMode.ALREADY_SATISFIED, ExecutionMode.ALREADY_APPLIED_CORPUS}:
                if mode is ExecutionMode.ALREADY_SATISFIED:
                    fresh_count += 1
                else:
                    corpus_count += 1
                    already_applied_count += 1
                processed.add(planned.work_id)
                newly_processed += 1
                continue
            if mode is ExecutionMode.UNAVAILABLE or item is None:
                if provider_reachable_probe:
                    # This item was classified while the circuit was HALF_OPEN;
                    # let the next closed-circuit plan lease it for live work.
                    continue
                failed_count += 1
                processed.add(planned.work_id)
                newly_processed += 1
                continue
            if mode in {ExecutionMode.LIVE, ExecutionMode.PROBE}:
                if dispatch_count >= call_budget:
                    break
                outcome = dispatch(connection, item, planned)
                dispatch_count += 1
            elif mode is ExecutionMode.CORPUS:
                outcome = admit_corpus(
                    connection,
                    item,
                    planned,
                    raw_corpus_dir,
                    project_root,
                    cursor_now,
                )
            else:
                failed_count += 1
                processed.add(planned.work_id)
                newly_processed += 1
                continue
            if connection.in_transaction:
                raise RuntimeError("recovery action returned with an active database transaction")
            cursor_now = max(cursor_now, outcome.observed_at)
            record_outcomes(
                connection,
                RecordOutcomesRequest(
                    run_id=run_id,
                    now=cursor_now,
                    expected_work_ids=(planned.work_id,),
                    outcomes=(outcome,),
                ),
            )
            provider_reachable_probe = mode is ExecutionMode.PROBE and outcome.outcome_code in {
                OutcomeCode.LIVE_SUCCESS,
                OutcomeCode.ENDPOINT_EMPTY,
                OutcomeCode.ENDPOINT_FORBIDDEN,
                OutcomeCode.CLIENT_CONTRACT_ERROR,
            }
            if outcome.outcome_code in {
                OutcomeCode.LIVE_SUCCESS,
                OutcomeCode.ALTERNATIVE_SUCCESS,
                OutcomeCode.RECONCILED_SUCCESS,
            }:
                fresh_count += 1
            elif outcome.outcome_code is OutcomeCode.CORPUS_SUCCESS:
                corpus_count += 1
                admitted_new_count += 1
            else:
                failed_count += 1
            processed.add(planned.work_id)
            newly_processed += 1
        if newly_processed == 0:
            break

    circuit = connection.execute(
        "SELECT state,revision FROM provider_circuit_state WHERE provider='fmp'"
    ).fetchone()
    if circuit is None:
        raise RuntimeError("FMP recovery circuit was not initialized")
    pending_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM fmp_work_backlog WHERE state='PENDING'"
        ).fetchone()[0]
    )
    status = _status_for_counts(
        fresh=fresh_count,
        corpus=corpus_count,
        failed=failed_count,
    )
    return RecoveryRunResult(
        run_id=run_id,
        status=status,
        planned_count=len(specs),
        dispatch_count=dispatch_count,
        fresh_count=fresh_count,
        corpus_count=corpus_count,
        admitted_new_count=admitted_new_count,
        already_applied_count=already_applied_count,
        failed_count=failed_count,
        circuit_state=CircuitState(str(circuit["state"])),
        circuit_revision=int(circuit["revision"]),
        pending_count=pending_count,
    )


def _all_active_tickers(
    conn: Connection,
    only_list_types: frozenset[str] | None,
    explicit_tickers: list[str] | None,
) -> list[tuple[str, str]]:
    """Return [(ticker, list_type), ...] for active (non-archived) rows."""
    cur = conn.cursor()
    sql = "SELECT ticker, list_type FROM tracked_companies WHERE archived_at IS NULL"
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
    conn: Connection, tickers: list[str]
) -> dict[tuple[str, str, str], tuple[str, datetime | None]]:
    """Map (ticker, endpoint, period) -> (status, last_pulled) for given tickers."""
    if not tickers:
        return {}
    cur = conn.cursor()
    # B608: interpolation is only a generated comma-list of '?' placeholders;
    # every ticker remains a separately bound SQLite parameter.
    placeholders = ",".join("?" for _ in tickers)
    cur.execute(
        f"SELECT ticker, endpoint, period, status, last_pulled "  # nosec B608
        f"FROM fmp_endpoint_status WHERE ticker IN ({placeholders})",
        tickers,
    )
    out: dict[tuple[str, str, str], tuple[str, datetime | None]] = {}
    for ticker, endpoint, period, status, last_pulled_str in cur.fetchall():
        last_pulled: datetime | None = None
        if last_pulled_str:
            with suppress(ValueError):
                last_pulled = datetime.fromisoformat(last_pulled_str)
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
        - min(days_overdue, 99)  # more overdue = higher priority (subtract)
    )


def audit(
    conn: Connection,
    *,
    only_list_types: frozenset[str] | None = None,
    explicit_tickers: list[str] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> AuditReport:
    """Build the full audit report. Cheap (~sub-second on 2,500 tickers)."""
    # Imported lazily because save_fmp_data validates network credentials at
    # module import. Non-fetching commands such as status/archive must remain
    # usable when FMP auth is intentionally unavailable.
    import save_fmp_data as fmp_save

    now = now or datetime.now()
    active = _all_active_tickers(conn, only_list_types, explicit_tickers)
    tickers = [t for t, _ in active]
    status_index = _existing_status_rows(conn, tickers)
    forced_tickers = _load_force_stale_hints()

    items: list[QueueItem] = []
    for ticker, list_type in active:
        jobs = cast(list[_FmpJob], fmp_save.per_ticker_jobs(ticker, list_type=list_type))
        for job in jobs:
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
                ticker in forced_tickers
                and endpoint_class in ("time_sensitive", "statement", "segment")
            )

            bucket, days_overdue = _classify_into_bucket(
                list_type, endpoint, endpoint_class, status, last_pulled, now, ticker_force
            )
            priority = _priority(list_type, endpoint_class, bucket, days_overdue)
            items.append(
                QueueItem(
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
                )
            )

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
                capture_output=True,
                text=True,
                timeout=5,
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
    conn = connect_sqlite(
        args.db,
        role=SQLiteConnectionRole.READ_ONLY,
        schema_preflight=False,
    )
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
    by_list_type: dict[str, int] = {}
    summary: dict[str, object] = {
        "generated_at": report.generated_at.isoformat(timespec="seconds"),
        "total_endpoints": len(report.items),
        "by_bucket": report.counts,
        "queueable": len(queueable),
        "by_list_type": by_list_type,
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
        by_list_type[item.list_type] = by_list_type.get(item.list_type, 0) + 1

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
    print(
        json.dumps(
            {
                "tier": tier.name,
                "tier_caps": {
                    "calls_per_day": (
                        tier.calls_per_day if tier.calls_per_day < _UNLIMITED else "unlimited"
                    ),
                    "calls_per_sec": tier.calls_per_sec,
                },
                "budget_today": {
                    "used": used,
                    "remaining": (remaining if remaining < _UNLIMITED else "unlimited"),
                },
                "lock": {
                    "owner_pid": lock_owner,
                    "alive": _pid_alive(lock_owner) if lock_owner else False,
                },
                "queue": queue_summary,
            },
            indent=2,
        )
    )
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    import db as portfolio_db

    archived = portfolio_db.archive_company(args.ticker)
    print(json.dumps({"ticker": args.ticker.upper(), "archived": archived}))
    return 0 if archived else 1


def cmd_reactivate(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    import db as portfolio_db

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


def _run_offline_with_lock(args: argparse.Namespace) -> int:
    """Own a separate atomic lock without consulting process-list subprocesses."""
    token = uuid.uuid4().hex
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(OFFLINE_LOCK_PATH, flags, 0o600)
    except FileExistsError:
        print(
            json.dumps(
                {
                    "event": "offline_corpus_lock_contended",
                    "mode": "offline_corpus_only",
                    "network_calls": 0,
                    "retryable": True,
                    "exit_code": 75,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 75
    try:
        os.write(descriptor, f"{os.getpid()}:{token}".encode("ascii"))
    finally:
        os.close(descriptor)
    try:
        return _run_offline_corpus_only(args)
    finally:
        try:
            if OFFLINE_LOCK_PATH.read_text(encoding="ascii") == f"{os.getpid()}:{token}":
                OFFLINE_LOCK_PATH.unlink()
        except OSError:
            pass


def _maybe_refresh_earnings_hints(*, env: Mapping[str, str] | None = None) -> None:
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
            [*managed_python_prefix(PROJECT_ROOT), str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
            env=env,
        )
    except (subprocess.SubprocessError, OSError) as e:
        sys.stderr.write(f"[hints] surrogate failed: {type(e).__name__}: {e}\n")


def _authorized_recovery_items(
    items: Sequence[QueueItem],
    *,
    explicit_tickers: Sequence[str] | None,
) -> list[QueueItem]:
    explicitly_requested = {ticker.upper() for ticker in explicit_tickers or ()}
    authorized: list[QueueItem] = []
    for item in items:
        role = ListType(item.list_type)
        if (
            role is ListType.PORTFOLIO
            or (role is ListType.EVALUATION and item.ticker in explicitly_requested)
            or (role is ListType.INDEX_MEMBER and item.suffix in SCREENING_ENDPOINT_KEYS)
        ):
            authorized.append(item)
    return authorized


_OFFLINE_STATEMENT_SUFFIXES: dict[str, tuple[str, str]] = {
    "income_statement_annual": ("income-statement", "annual"),
    "income_statement_quarterly": ("income-statement", "quarter"),
    "balance_sheet_annual": ("balance-sheet-statement", "annual"),
    "balance_sheet_quarterly": ("balance-sheet-statement", "quarter"),
    "cash_flow_annual": ("cashflow-statement", "annual"),
    "cash_flow_quarterly": ("cashflow-statement", "quarter"),
}


def _offline_corpus_items(
    connection: sqlite3.Connection,
    *,
    raw_corpus_dir: Path,
    only_list_type: str | None,
    explicit_tickers: Sequence[str] | None,
) -> tuple[list[QueueItem], int]:
    """Map immutable statement files to policy-authorized governed work."""
    requested_tickers = {ticker.upper() for ticker in explicit_tickers or ()}
    active = {
        ticker: ListType(list_type)
        for ticker, list_type in _all_active_tickers(
            connection,
            None,
            list(requested_tickers) if requested_tickers else None,
        )
    }
    items: list[QueueItem] = []
    excluded_by_tier_count = 0
    if not raw_corpus_dir.exists():
        return items, excluded_by_tier_count
    for path in _safe_corpus_files(raw_corpus_dir):
        if path.parent != raw_corpus_dir:
            continue
        matched: tuple[str, str, str] | None = None
        for suffix, (endpoint, period) in _OFFLINE_STATEMENT_SUFFIXES.items():
            ending = f"_{suffix}.json"
            if path.name.endswith(ending):
                matched = (path.name[: -len(ending)].upper(), endpoint, period)
                break
        if matched is None:
            continue
        ticker, endpoint, period = matched
        role = active.get(ticker)
        if role is None:
            continue
        selected_roles = (
            frozenset({ListType(only_list_type)})
            if only_list_type is not None
            else frozenset({ListType.PORTFOLIO, ListType.EVALUATION})
        )
        if role not in selected_roles:
            excluded_by_tier_count += 1
            continue
        suffix = path.stem[len(ticker) + 1 :]
        requested = role is ListType.EVALUATION
        authorization = decision_for(
            role,
            CollectionSource.FMP,
            ArtifactKind.FINANCIAL_FACT,
            requested=requested,
        )
        if not authorization.allowed:
            excluded_by_tier_count += 1
            continue
        if role is ListType.INDEX_MEMBER and suffix not in SCREENING_ENDPOINT_KEYS:
            excluded_by_tier_count += 1
            continue
        items.append(
            QueueItem(
                ticker=ticker,
                list_type=role.value,
                endpoint=endpoint,
                period=period,
                suffix=suffix,
                endpoint_class="statement",
                bucket="missing",
                last_pulled=None,
                last_status=None,
                days_overdue=0,
                priority=_priority(role.value, "statement", "missing", 0),
            )
        )
    return items, excluded_by_tier_count


def _run_offline_corpus_only(args: argparse.Namespace) -> int:
    """Replay raw statement evidence without resolving or constructing provider I/O."""
    now = _utc_now()
    before_manifest = _raw_corpus_manifest(FMP_DIR)
    connection = connect_sqlite(
        args.db,
        role=SQLiteConnectionRole.WRITER,
    )
    try:
        explicit_tickers = _split_tickers(args.tickers)
        items, excluded_by_tier_count = _offline_corpus_items(
            connection,
            raw_corpus_dir=FMP_DIR,
            only_list_type=args.only,
            explicit_tickers=explicit_tickers,
        )
        run_id = f"offline-corpus:{uuid.uuid4()}"
        if items:
            intended_ids = frozenset(
                make_work_id(
                    _work_spec(
                        item,
                        raw_corpus_dir=FMP_DIR,
                        now=now,
                        owner_request_id=f"offline-corpus:{run_id}",
                    )
                )
                for item in items
            )
            recovery = run_recovery_batch(
                connection,
                items=items,
                credentials=CredentialAvailability.MISSING,
                raw_corpus_dir=FMP_DIR,
                project_root=PROJECT_ROOT,
                now=now,
                run_id=run_id,
                dispatch=_unexpected_offline_dispatch,
                max_items=500,
                provider_call_budget=0,
                owner_request_id=f"offline-corpus:{run_id}",
                restrict_to_intended=True,
                backlog_item_resolver=_offline_item_from_backlog_row,
            )
            corpus_count = recovery.corpus_count
            admitted_new_count = recovery.admitted_new_count
            already_applied_count = recovery.already_applied_count
            failed_count = _offline_failed_count(
                connection,
                run_id=run_id,
                intended_work_ids=intended_ids,
            )
            pending_count = _offline_pending_count(
                connection,
                intended_work_ids=intended_ids,
            )
        else:
            corpus_count = 0
            admitted_new_count = 0
            already_applied_count = 0
            failed_count = 0
            pending_count = 0
        processed_count = corpus_count + failed_count
        deferred_count = max(0, len(items) - processed_count)
    finally:
        connection.close()
    after_manifest = _raw_corpus_manifest(FMP_DIR)
    manifest_unchanged = after_manifest == before_manifest
    if not manifest_unchanged:
        status = ReceiptStatus.FAILED
    elif corpus_count > 0 and failed_count == 0 and deferred_count == 0:
        status = ReceiptStatus.DEGRADED_CORPUS
    elif corpus_count > 0:
        status = ReceiptStatus.PARTIAL
    else:
        status = ReceiptStatus.FAILED
    result = OfflineCorpusRunResult(
        run_id=run_id,
        status=status,
        discovered_file_count=len(before_manifest.entries),
        selected_count=len(items),
        admitted_count=corpus_count,
        admitted_new_count=admitted_new_count,
        already_applied_count=already_applied_count,
        eligible_count=len(items),
        corpus_count=corpus_count,
        failed_count=failed_count,
        deferred_count=deferred_count,
        excluded_by_tier_count=excluded_by_tier_count,
        skipped_count=max(
            0,
            len(before_manifest.entries) - len(items) - excluded_by_tier_count,
        ),
        pending_count=pending_count,
        manifest_sha256=before_manifest.manifest_sha256,
        manifest_before_sha256=before_manifest.manifest_sha256,
        manifest_after_sha256=after_manifest.manifest_sha256,
        manifest_unchanged=manifest_unchanged,
    )
    print(json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")))
    print(
        json.dumps(
            {
                "event": "offline_corpus_replay_complete",
                "exit_code": result.exit_code,
                "manifest_unchanged": result.manifest_unchanged,
                "network_calls": 0,
                "run_id": result.run_id,
                "status": result.status.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )
    return result.exit_code


def _offline_failed_count(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    intended_work_ids: frozenset[str],
) -> int:
    """Count only failed attempts belonging to this offline selection."""
    attempts = connection.execute(
        "SELECT work_id,outcome_code FROM fmp_work_attempts WHERE run_id=?",
        (run_id,),
    ).fetchall()
    return sum(
        1
        for attempt in attempts
        if str(attempt["work_id"]) in intended_work_ids
        and str(attempt["outcome_code"]) != OutcomeCode.CORPUS_SUCCESS.value
    )


def _offline_pending_count(
    connection: sqlite3.Connection,
    *,
    intended_work_ids: frozenset[str],
) -> int:
    """Count pending state only for the work selected by this offline run."""
    rows = connection.execute(
        "SELECT work_id,state FROM fmp_work_backlog WHERE state='PENDING'"
    ).fetchall()
    return sum(1 for row in rows if str(row["work_id"]) in intended_work_ids)


def _unexpected_offline_dispatch(
    _connection: sqlite3.Connection,
    _item: QueueItem,
    _planned: PlannedWork,
) -> WorkOutcome:
    """Fail closed if the recovery planner ever violates the zero-call budget."""
    raise AssertionError("offline corpus-only mode attempted provider dispatch")


def _dispatch_one(
    connection: sqlite3.Connection,
    item: QueueItem,
    planned: PlannedWork,
    *,
    tier: TierConfig,
    auth: FmpAuthConfig,
    db_path: Path,
    log_path: Path,
) -> WorkOutcome:
    """Run one logical endpoint after its durable lease has committed."""
    if connection.in_transaction:
        raise RuntimeError("FMP dispatch cannot run inside a database transaction")
    if planned.lease_token is None or planned.lease_expires_at is None:
        raise ValueError("live dispatch requires an active recovery lease")
    from save_fmp_data import FmpWorkReceipt, FmpWorkReceiptOutcome

    lease_started_at = _utc_now()
    dispatch_dir = CACHE_DIR / "dispatch" / planned.work_id[:16]
    manifest_path = dispatch_dir / "manifest.json"
    receipt_path = dispatch_dir / "work_receipt.json"
    try:
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            receipt_path.unlink()
        manifest_path.write_text(
            json.dumps({"items": [item.to_manifest_entry()]}, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.TRANSPORT_ERROR,
            observed_at=_utc_now(),
        )
    env = os.environ.copy()
    env["FMP_API_KEY"] = auth.api_key
    env["FMP_RATE_LIMIT_PER_SEC"] = str(tier.calls_per_sec)
    env["FMP_TIER"] = tier.name
    env["EARNINGS_SUMMARY_DB_PATH"] = str(db_path)
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(PROJECT_ROOT / "execution" / "save_fmp_data.py"),
        "--manifest",
        str(manifest_path),
        "--max-calls",
        "1",
        "--work-receipt",
        str(receipt_path),
    ]
    try:
        with log_path.open("a", encoding="utf-8") as logf:
            proc = subprocess.run(  # nosec B603 - fixed local interpreter/script
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                check=False,
                env=env,
                timeout=240,
            )
    except (OSError, subprocess.SubprocessError):
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.TRANSPORT_ERROR,
            observed_at=_utc_now(),
        )
    observed_at = _utc_now()
    try:
        receipt = FmpWorkReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.TRANSPORT_ERROR,
            observed_at=observed_at,
        )
    if (
        receipt.ticker != item.ticker
        or receipt.endpoint != item.endpoint
        or receipt.period != item.period
    ):
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.TRANSPORT_ERROR,
            observed_at=observed_at,
        )
    if receipt.outcome is FmpWorkReceiptOutcome.SUCCESS:
        if proc.returncode != 0 or receipt.file_path is None or receipt.content_sha256 is None:
            return WorkOutcome(
                work_id=planned.work_id,
                lease_token=planned.lease_token,
                outcome_code=OutcomeCode.TRANSPORT_ERROR,
                observed_at=observed_at,
            )
        file_path = (PROJECT_ROOT / receipt.file_path).resolve()
        try:
            file_path.relative_to(FMP_DIR.resolve())
        except ValueError:
            return WorkOutcome(
                work_id=planned.work_id,
                lease_token=planned.lease_token,
                outcome_code=OutcomeCode.TRANSPORT_ERROR,
                observed_at=observed_at,
            )
        try:
            content_sha256 = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError:
            return WorkOutcome(
                work_id=planned.work_id,
                lease_token=planned.lease_token,
                outcome_code=OutcomeCode.TRANSPORT_ERROR,
                observed_at=observed_at,
            )
        if (
            content_sha256 != receipt.content_sha256
            or receipt.captured_at < lease_started_at
            or receipt.captured_at > observed_at
        ):
            return WorkOutcome(
                work_id=planned.work_id,
                lease_token=planned.lease_token,
                outcome_code=OutcomeCode.TRANSPORT_ERROR,
                observed_at=observed_at,
            )
        if planned.cache_generation_id is None or planned.policy_sha256 is None:
            raise AssertionError("live lease lost its recovery identity")
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.LIVE_SUCCESS,
            observed_at=observed_at,
            http_status=receipt.http_status,
            fmp_snapshot=FmpSnapshotProof(
                work_id=planned.work_id,
                cache_generation_id=planned.cache_generation_id,
                policy_sha256=planned.policy_sha256,
                content_sha256=receipt.content_sha256,
                captured_at=receipt.captured_at,
            ),
        )
    outcome_by_receipt = {
        FmpWorkReceiptOutcome.ACCOUNT_UNAUTHORIZED: OutcomeCode.HTTP_UNAUTHORIZED,
        FmpWorkReceiptOutcome.ACCOUNT_PAYMENT_REQUIRED: OutcomeCode.ACCOUNT_PAYMENT_REQUIRED,
        FmpWorkReceiptOutcome.ACCOUNT_FORBIDDEN: OutcomeCode.ACCOUNT_AUTH_FORBIDDEN,
        FmpWorkReceiptOutcome.ENDPOINT_FORBIDDEN: OutcomeCode.ENDPOINT_FORBIDDEN,
        FmpWorkReceiptOutcome.RATE_LIMITED: OutcomeCode.RATE_LIMITED,
        FmpWorkReceiptOutcome.SERVER_ERROR: OutcomeCode.SERVER_ERROR,
        FmpWorkReceiptOutcome.EMPTY: OutcomeCode.ENDPOINT_EMPTY,
        FmpWorkReceiptOutcome.CONTRACT_ERROR: OutcomeCode.CLIENT_CONTRACT_ERROR,
    }
    outcome_code = outcome_by_receipt.get(receipt.outcome)
    if outcome_code is None or (
        outcome_code is OutcomeCode.CLIENT_CONTRACT_ERROR and receipt.http_status is None
    ):
        return WorkOutcome(
            work_id=planned.work_id,
            lease_token=planned.lease_token,
            outcome_code=OutcomeCode.TRANSPORT_ERROR,
            observed_at=observed_at,
        )
    return WorkOutcome(
        work_id=planned.work_id,
        lease_token=planned.lease_token,
        outcome_code=outcome_code,
        observed_at=observed_at,
        http_status=receipt.http_status,
    )


def _run_under_lock(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    now = _utc_now()
    connection = connect_sqlite(
        args.db,
        role=SQLiteConnectionRole.WRITER,
    )
    try:
        credential_decision = decide_recovery_credentials(connection, now=now)
        child_env = os.environ.copy()
        child_env["FMP_RATE_LIMIT_PER_SEC"] = str(tier.calls_per_sec)
        child_env["FMP_TIER"] = tier.name
        if credential_decision.auth is not None:
            child_env["FMP_API_KEY"] = credential_decision.auth.api_key
        if credential_decision.hints_permitted:
            _maybe_refresh_earnings_hints(env=child_env)
        explicit_tickers = _split_tickers(args.tickers)
        report = audit(
            connection,
            only_list_types=frozenset({args.only}) if args.only else None,
            explicit_tickers=explicit_tickers,
            force=args.force,
            now=now,
        )
        queueable = _authorized_recovery_items(
            report.queueable(),
            explicit_tickers=explicit_tickers,
        )
        due_backlog_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM fmp_work_backlog WHERE state='PENDING' AND available_at <= ?",
                (now.isoformat(),),
            ).fetchone()[0]
        )
        if not queueable and due_backlog_count == 0:
            print(json.dumps({"event": "no_work", "audit_counts": report.counts}, indent=2))
            return 0

        budget = args.max_calls if args.max_calls is not None else remaining_budget(tier)
        processing_preview = queueable[:500]
        deferred = max(0, len(queueable) - len(processing_preview))
        QUEUE_PATH.write_text(
            json.dumps(
                {
                    "generated_at": report.generated_at.isoformat(timespec="seconds"),
                    "tier": tier.name,
                    "items": [item.to_manifest_entry() for item in processing_preview],
                    "deferred": deferred,
                    "audit_counts": report.counts,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "tier": tier.name,
                        "would_enqueue": len(queueable),
                        "would_process": len(processing_preview),
                        "due_backlog": due_backlog_count,
                        "provider_call_budget": max(0, budget),
                        "deferred": deferred,
                        "audit_counts": report.counts,
                    },
                    indent=2,
                )
            )
            return 0

        run_id = f"refresh-cache:{now.strftime('%Y%m%dT%H%M%S')}:{os.getpid()}"
        log_path = CACHE_DIR / f"run_{now.strftime('%Y%m%dT%H%M%S')}.log"
        log_path.write_text(
            f"# refresh_cache: tier={tier.name} intended={len(queueable)} deferred={deferred}\n",
            encoding="utf-8",
        )

        def dispatch(
            conn: sqlite3.Connection,
            item: QueueItem,
            planned: PlannedWork,
        ) -> WorkOutcome:
            auth = credential_decision.auth
            if auth is None:
                raise RuntimeError("recovery plan authorized network work without FMP auth")
            return _dispatch_one(
                conn,
                item,
                planned,
                tier=tier,
                auth=auth,
                db_path=Path(args.db),
                log_path=log_path,
            )

        result = run_recovery_batch(
            connection,
            items=queueable,
            credentials=credential_decision.credentials,
            raw_corpus_dir=FMP_DIR,
            now=now,
            run_id=run_id,
            dispatch=dispatch,
            max_items=500,
            provider_call_budget=max(0, budget),
            owner_request_id=(f"cli:{run_id}" if explicit_tickers else None),
        )
        output = result.to_dict()
        output.update(
            {
                "tier": tier.name,
                "deferred": deferred,
                "log": str(log_path),
                "audit_counts": report.counts,
            }
        )
        print(json.dumps(output, indent=2))
        return result.exit_code
    finally:
        connection.close()


def _spawn_background(args: argparse.Namespace) -> int:
    """Re-exec self with --background-child and detach."""
    forwarded = [
        *managed_python_prefix(PROJECT_ROOT),
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
            stdout=logf,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=creationflags,
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
    common.add_argument(
        "--force", action="store_true", help="Ignore cadence; queue every endpoint in scope"
    )
    common.add_argument("--tickers", help="Comma-separated tickers (subset filter)")
    common.add_argument(
        "--only", choices=sorted(_LIST_TYPE_BASE_FRESH_H), help="Restrict to one list_type tier"
    )
    common.add_argument(
        "--max-calls", type=int, default=None, help="Override tier daily cap for this invocation"
    )

    p_run = sub.add_parser("run", parents=[common], help="Audit + dispatch (default)")
    p_run.add_argument(
        "--dry-run", action="store_true", help="Audit and write queue.json but don't invoke fetcher"
    )
    p_run.add_argument(
        "--offline-corpus-only",
        action="store_true",
        help="Admit existing raw statement corpus with zero credentials and network calls",
    )
    p_run.add_argument("--background", action="store_true", help="Detach and exit immediately")
    p_run.add_argument("--background-child", action="store_true", help=argparse.SUPPRESS)

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
        args = ap.parse_args(["run", *sys.argv[1:]])

    if args.cmd == "audit":
        return cmd_audit(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "archive":
        return cmd_archive(args)
    if args.cmd == "reactivate":
        return cmd_reactivate(args)
    if args.cmd == "run":
        if args.offline_corpus_only and (args.background or args.dry_run):
            ap.error("--offline-corpus-only cannot be combined with --background or --dry-run")
        if args.offline_corpus_only:
            return _run_offline_with_lock(args)
        if args.background and not args.background_child:
            return _spawn_background(args)
        return cmd_run(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
