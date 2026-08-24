"""Hermetic single-owner runtime decisions and typed BHA-80 receipts."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from integrations.portfolio_tracker_v1 import HealthV1

RUNTIME_RECEIPT_SCHEMA_VERSION = "1"
PORTFOLIO_TRACKER_RUNTIME_SURFACE_FIELD = "portfolio_tracker_runtime"
LEASE_STALE_AFTER_SECONDS = 3_600.0
LEASE_RELEASE_DEADLINE_SECONDS = 0.5


class RuntimeConfig(BaseModel):
    listener_owner: str
    daily_refresh_owner: str
    idempotency_key: str


class LeaseReleaseError(RuntimeError):
    """A runtime operation cannot claim success while its lease is orphaned."""


class ListenerObservation(BaseModel):
    healthy: bool
    owner: str | None = None
    pid: int | None = None
    job_id: str | None = None
    health_checked_at: datetime | None = None
    health: HealthV1 | None = None


def health_is_healthy(health: HealthV1 | None, *, now: datetime) -> bool:
    """Require provider health, database health, freshness, and coherent clock."""

    if (
        health is None
        or health.status != "ok"
        or not health.database_ok
        or health.active_account_count < 1
        or health.is_stale
    ):
        return False
    if health.generated_at.tzinfo is None or now.tzinfo is None:
        return False
    return health.generated_at <= now


def parse_tracker_bind_url(api_url: str) -> tuple[str, int] | None:
    """Parse a configured localhost API URL into safe uvicorn bind arguments."""

    parsed = urlsplit(api_url)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not parsed.hostname:
        return None
    try:
        port = parsed.port or 80
    except ValueError:
        return None
    if not 1 <= port <= 65_535:
        return None
    if not is_loopback_bind_host(parsed.hostname):
        return None
    return parsed.hostname, port


def is_loopback_bind_host(host: str) -> bool:
    """Return true only for literal IPv4/IPv6 loopback addresses.

    Hostnames are intentionally rejected: resolving a name is outside this
    bounded configuration check and could turn a nominally local listener
    into a network-facing one.
    """

    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class SchedulerEvidence(BaseModel):
    task_name: str
    terminal_result: str
    observed_at: datetime


class RefreshEvidence(BaseModel):
    owner: str | None = None
    snapshot_as_of: str | None = None
    completed_at: datetime | None = None
    terminal_result: str | None = None


class RuntimeReceipt(BaseModel):
    """Separate evidence planes; none implies no observation, never success."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = RUNTIME_RECEIPT_SCHEMA_VERSION
    idempotency_key: str
    lifecycle_state: Literal["already_running", "started", "ownership_conflict", "failed"]
    recorded_at: datetime
    listener: ListenerObservation
    scheduler: SchedulerEvidence | None = None
    refresh: RefreshEvidence | None = None
    failure_detail: str | None = None

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("runtime receipt timestamp must be timezone-aware")
        return value


def derive_daily_refresh_idempotency_key(recorded_at: datetime) -> str:
    """Derive the daily key from the producer clock; callers cannot supply it."""

    return f"portfolio-tracker-refresh:{recorded_at.astimezone(UTC).date().isoformat()}"


ProcessLiveness = Literal["alive", "dead", "unknown"]


def _windows_last_error() -> int | None:
    """Read the Win32 error seam when available; POSIX test doubles lack it."""

    get_last_error = getattr(ctypes, "get_last_error", None)
    if not callable(get_last_error):
        return None
    return cast("Callable[[], int]", get_last_error)()


def _pid_liveness(pid: int) -> ProcessLiveness:
    if pid <= 0:
        return "dead"
    if sys.platform == "win32":
        # Windows has no signal-0 equivalent. Query-only OpenProcess is
        # non-destructive and avoids sending even a nominal signal.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
        except AttributeError:
            # Lightweight test doubles need only the same callable surface.
            pass
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # A missing Win32 error seam is also inconclusive: never classify a
            # failed query as dead merely because a POSIX ctypes build lacks it.
            return "unknown" if _windows_last_error() in {None, 5} else "dead"
        exit_code = ctypes.c_ulong()
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return "unknown"
            return "alive" if exit_code.value == 259 else "dead"  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    return "alive"


def _pid_is_alive(pid: int) -> bool:
    """Compatibility boolean; lease decisions use the tri-state probe."""

    return _pid_liveness(pid) == "alive"


def endpoint_owner_matches_pid(
    host: str, port: int, pid: int, *, require_exclusive: bool = False
) -> bool | None:
    """Prove a TCP listener endpoint belongs to ``pid`` or its child, or fail closed.

    Windows' typed ``netstat -ano`` output is the supported local probe. A
    missing/opaque probe returns ``None`` rather than treating a healthy HTTP
    responder as owned by the tracker.
    """

    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", None) != 0:
        return None
    if not isinstance(pid, int) or pid <= 0:
        return False

    def normalize(value: str) -> str:
        value = value.strip().strip("[]")
        try:
            return f"ip:{ip_address(value).compressed}"
        except ValueError:
            return f"name:{value.rstrip('.').casefold()}"

    expected_host = normalize(host)

    def parse_local_endpoint(value: str) -> tuple[str, int] | None:
        value = value.strip()
        if value.startswith("["):
            close = value.rfind("]")
            if close <= 1 or close + 2 > len(value) or value[close + 1] != ":":
                return None
            host_part, port_text = value[1:close], value[close + 2 :]
        else:
            host_part, separator, port_text = value.rpartition(":")
            if not separator or not host_part:
                return None
        try:
            parsed_port = int(port_text)
        except ValueError:
            return None
        return normalize(host_part), parsed_port

    listeners: list[tuple[str, int, int]] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields or fields[0].upper() != "TCP":
            continue
        if len(fields) < 4 or fields[3].upper() != "LISTENING":
            continue
        parsed = parse_local_endpoint(fields[1])
        if parsed is None:
            if require_exclusive:
                return False
            continue
        if len(fields) < 5:
            if require_exclusive:
                return False
            continue
        try:
            listener_pid = int(fields[4])
        except ValueError:
            if require_exclusive and parsed[1] == port:
                return False
            continue
        if listener_pid <= 0:
            if require_exclusive and parsed[1] == port:
                return False
            continue
        listeners.append((parsed[0], parsed[1], listener_pid))

    if require_exclusive:
        # The BHA-80 always-on service can only claim the endpoint when there
        # is one listener, on exactly the requested local address and port,
        # owned by its supervised process tree. A wildcard socket or any companion listener is
        # ambiguous even when it happens to share the child PID.
        port_listeners = [entry for entry in listeners if entry[1] == port]
        if len(port_listeners) != 1:
            return False
        local_host, local_port, listener_pid = port_listeners[0]
        if (local_host, local_port) != (expected_host, port):
            return False
        return _windows_pid_is_descendant_of(listener_pid, pid)

    return any(
        local_host == expected_host and local_port == port and listener_pid == pid
        for local_host, local_port, listener_pid in listeners
    )


def _windows_pid_is_descendant_of(child_pid: int, ancestor_pid: int) -> bool:
    """Return true only when each observed parent leads from child to ancestor.

    Windows virtual-environment redirectors can leave the listening interpreter
    below the ``Popen`` launcher. This bounded parent walk preserves ownership
    without allowing a sibling or independently launched loopback server.
    """

    current_pid = child_pid
    seen: set[int] = set()
    for _ in range(32):
        if current_pid == ancestor_pid:
            return True
        if current_pid in seen:
            return False
        seen.add(current_pid)
        parent_pid = _windows_parent_pid(current_pid)
        if parent_pid is None or parent_pid <= 0:
            return False
        current_pid = parent_pid
    return False


def _windows_parent_pid(pid: int) -> int | None:
    """Read one Windows process parent PID, returning unknown evidence as ``None``."""

    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$process=Get-CimInstance -ClassName Win32_Process "
                    f"-Filter 'ProcessId={pid}' -ErrorAction Stop;"
                    "if($null -eq $process){exit 1};"
                    "[Console]::WriteLine([int]$process.ParentProcessId)"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(values) != 1:
        return None
    try:
        parent_pid = int(values[0])
    except ValueError:
        return None
    return parent_pid if parent_pid >= 0 else None


class AtomicFileLease:
    """A small O_EXCL lease usable by one local runtime owner across processes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._held = False
        self._token: str | None = None
        self._identity: tuple[int, int] | None = None
        self._raw: bytes | None = None
        self.last_conflict_detail: str | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if (
            self._held
            and self._token is not None
            and self._identity is not None
            and self._raw is not None
            and self._path_matches(raw=self._raw, identity=self._identity, token=self._token)
        ):
            return True
        for attempt in range(2):
            try:
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 0 and self._recover_stale():
                    continue
                return False
            token = uuid4().hex
            raw = f"{os.getpid()}|{time.time():.6f}|{token}".encode("ascii")
            try:
                os.write(descriptor, raw)
                identity = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            self._token = token
            self._identity = (identity.st_dev, identity.st_ino)
            self._raw = raw
            self._held = True
            return True
        return False

    def _recover_stale(self) -> bool:
        guard = self._path.with_name(f".{self._path.name}.takeover")
        try:
            descriptor = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self.last_conflict_detail = "another lease contender is inspecting stale ownership"
            return False
        try:
            return self._recover_stale_locked()
        finally:
            os.close(descriptor)
            guard.unlink(missing_ok=True)

    def _recover_stale_locked(self) -> bool:
        try:
            raw = self._path.read_bytes()[:256]
            fields = raw.decode("ascii").split("|")
            if len(fields) != 3:
                raise ValueError("legacy lease metadata")
            pid = int(fields[0])
            created_at = float(fields[1])
            token = fields[2]
            if not token:
                raise ValueError("empty lease token")
            age = max(0.0, time.time() - created_at)
        except (OSError, UnicodeError, ValueError):
            self.last_conflict_detail = "lease exists with unreadable owner evidence"
            return False
        liveness = _pid_liveness(pid)
        if age < LEASE_STALE_AFTER_SECONDS or liveness != "dead":
            self.last_conflict_detail = (
                f"lease held by pid {pid} token={token} liveness={liveness} age_seconds={age:.1f}"
            )
            return False
        # Claim the observed stale inode with one atomic namespace operation.
        # A contender that wins this rename owns the quarantine; a replacement
        # lease at the original path can never be unlinked by this owner.
        quarantine = self._path.with_name(f".{self._path.name}.quarantine.{uuid4().hex}")
        try:
            os.rename(self._path, quarantine)
        except OSError:
            self.last_conflict_detail = "stale lease changed during takeover; retry required"
            return False
        if quarantine.read_bytes()[:256] != raw:
            self.last_conflict_detail = "stale lease changed before atomic takeover"
            # The current path was replaced before the rename. Restore the
            # quarantined replacement rather than deleting unknown ownership.
            if not self._path.exists():
                try:
                    os.rename(quarantine, self._path)
                except OSError:
                    self.last_conflict_detail = "stale replacement restoration lost a race"
            return False
        quarantine.unlink(missing_ok=True)
        return True

    def _path_matches(self, *, raw: bytes, identity: tuple[int, int], token: str) -> bool:
        """Re-read identity and token before any destructive lease operation."""

        try:
            current = self._path.stat()
            current_raw = self._path.read_bytes()[:256]
            current_fields = current_raw.decode("ascii").split("|")
        except OSError:
            return False
        except (UnicodeError, ValueError):
            return False
        return (
            (current.st_dev, current.st_ino) == identity
            and current_raw == raw
            and len(current_fields) == 3
            and current_fields[-1] == token
        )

    def release(self, *, deadline_seconds: float = LEASE_RELEASE_DEADLINE_SECONDS) -> bool:
        """Release only our lease, retrying briefly around takeover races.

        ``False`` is a typed deferred/failure result; ownership metadata stays
        held so a caller can retry after the competing takeover guard clears.
        """

        if not self._held:
            return True
        guard = self._path.with_name(f".{self._path.name}.takeover")
        deadline = time.monotonic() + max(0.0, deadline_seconds)
        delay = 0.01
        while True:
            try:
                descriptor = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                self.last_conflict_detail = "lease release deferred; takeover is in progress"
                if time.monotonic() >= deadline:
                    self.last_conflict_detail = "lease release deadline exceeded; retry required"
                    return False
                time.sleep(delay)
                delay = min(delay * 2, 0.05)
                continue
            try:
                if not (
                    self._token is not None
                    and self._identity is not None
                    and self._raw is not None
                    and self._path_matches(
                        raw=self._raw, identity=self._identity, token=self._token
                    )
                ):
                    self.last_conflict_detail = "lease release skipped; ownership identity changed"
                    self._clear_ownership()
                    return True
                try:
                    self._path.unlink(missing_ok=True)
                except OSError:
                    self.last_conflict_detail = "lease release lost a concurrent race"
                else:
                    if not self._path.exists():
                        self._clear_ownership()
                        return True
            finally:
                os.close(descriptor)
                guard.unlink(missing_ok=True)
            if time.monotonic() >= deadline:
                self.last_conflict_detail = "lease release deadline exceeded; retry required"
                return False
            time.sleep(delay)
            delay = min(delay * 2, 0.05)

    def _clear_ownership(self) -> None:
        self._held = False
        self._token = None
        self._identity = None
        self._raw = None


class PortfolioTrackerRuntimeManager:
    """Decide whether to launch without owning a live process implementation."""

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        inspect_listener: Callable[[], ListenerObservation],
        start_listener: Callable[[RuntimeConfig], None],
        now: Callable[[], datetime],
        lease: AtomicFileLease | None = None,
        sleep: Callable[[float], None] = time.sleep,
        startup_timeout_seconds: float = 10.0,
    ) -> None:
        self._config = config
        self._inspect_listener = inspect_listener
        self._start_listener = start_listener
        self._now = now
        self._lease = lease
        self._sleep = sleep
        self._startup_timeout_seconds = startup_timeout_seconds

    def ensure_running(self, *, receipt_path: Path | None = None) -> RuntimeReceipt:
        if self._lease is not None and not self._lease.acquire():
            return RuntimeReceipt(
                idempotency_key=self._config.idempotency_key,
                lifecycle_state="ownership_conflict",
                recorded_at=self._now(),
                listener=ListenerObservation(healthy=False),
                failure_detail=self._lease.last_conflict_detail
                or "another portfolio-tracker runtime owner holds the atomic lease",
            )
        try:
            receipt = self._ensure_running()
            return (
                write_runtime_receipt(receipt_path, receipt)
                if receipt_path is not None
                else receipt
            )
        finally:
            if (
                self._lease is not None
                and not self._lease.release()
                and (not self._lease.acquire() or not self._lease.release())
            ):
                raise LeaseReleaseError(
                    self._lease.last_conflict_detail or "runtime lease release failed"
                )

    def _ensure_running(self) -> RuntimeReceipt:
        try:
            before = self._inspect_listener()
        except Exception as exc:
            return self._failed(type(exc).__name__, ListenerObservation(healthy=False))
        before_now = self._now()
        if before.owner is not None and before.owner != self._config.listener_owner:
            return RuntimeReceipt(
                idempotency_key=self._config.idempotency_key,
                lifecycle_state="ownership_conflict",
                recorded_at=before_now,
                listener=before,
                failure_detail="listener is healthy or occupied by an unexpected owner",
            )
        if before.healthy and not health_is_healthy(before.health, now=before_now):
            return self._failed("listener_health_invalid", before)
        if before.healthy:
            return RuntimeReceipt(
                idempotency_key=self._config.idempotency_key,
                lifecycle_state="already_running",
                recorded_at=before_now,
                listener=before,
            )
        if before.owner is not None:
            return self._failed("listener_owner_unverified", before)
        try:
            self._start_listener(self._config)
        except Exception as exc:
            return self._failed(type(exc).__name__, before)
        deadline = time.monotonic() + self._startup_timeout_seconds
        after = before
        while True:
            try:
                after = self._inspect_listener()
            except Exception as exc:
                return self._failed(type(exc).__name__, after)
            if after.owner is not None and after.owner != self._config.listener_owner:
                return RuntimeReceipt(
                    idempotency_key=self._config.idempotency_key,
                    lifecycle_state="ownership_conflict",
                    recorded_at=self._now(),
                    listener=after,
                    failure_detail="listener came up under an unexpected owner",
                )
            if (
                after.healthy
                and health_is_healthy(after.health, now=self._now())
                and after.owner == self._config.listener_owner
            ):
                return RuntimeReceipt(
                    idempotency_key=self._config.idempotency_key,
                    lifecycle_state="started",
                    recorded_at=self._now(),
                    listener=after,
                )
            if time.monotonic() >= deadline:
                return self._failed("listener_health_timeout", after)
            self._sleep(0.05)

    def _failed(self, detail: str, listener: ListenerObservation) -> RuntimeReceipt:
        return RuntimeReceipt(
            idempotency_key=self._config.idempotency_key,
            lifecycle_state="failed",
            recorded_at=self._now(),
            listener=listener.model_copy(update={"healthy": False}),
            failure_detail=detail,
        )


def write_runtime_receipt(path: Path, receipt: RuntimeReceipt) -> RuntimeReceipt:
    """Atomically persist and re-parse a receipt before reporting success."""

    path.parent.mkdir(parents=True, exist_ok=True)
    existing: RuntimeReceipt | None = None
    with suppress(FileNotFoundError, OSError, ValidationError, ValueError):
        existing = RuntimeReceipt.model_validate_json(path.read_bytes())
    if existing is not None:
        # Listener/start and refresh/scheduler producers own separate typed
        # planes but share one lease. Never erase a plane merely because this
        # writer did not observe it; preserve the last valid plane atomically.
        incoming_is_older = receipt.recorded_at < existing.recorded_at
        receipt = receipt.model_copy(
            update={
                "recorded_at": max(receipt.recorded_at, existing.recorded_at),
                "listener": _merge_listener_evidence(existing, receipt),
                "scheduler": _merge_scheduler_evidence(existing, receipt),
                "refresh": _merge_refresh_evidence(existing, receipt),
                **(
                    {
                        "idempotency_key": existing.idempotency_key,
                        "lifecycle_state": existing.lifecycle_state,
                        "failure_detail": existing.failure_detail,
                    }
                    if incoming_is_older
                    else {}
                ),
            }
        )
    elif receipt.scheduler is None:
        receipt = receipt.model_copy(
            update={
                "scheduler": SchedulerEvidence(
                    task_name="PortfolioTrackerDaily",
                    terminal_result="activation_required",
                    observed_at=receipt.recorded_at,
                )
            }
        )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(receipt.model_dump_json(), encoding="utf-8")
        temporary.replace(path)
        return RuntimeReceipt.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise RuntimeError(f"runtime receipt persistence failed: {type(exc).__name__}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _receipt_time(receipt: RuntimeReceipt) -> datetime:
    return receipt.recorded_at


def _listener_time(receipt: RuntimeReceipt) -> datetime:
    return receipt.listener.health_checked_at or _receipt_time(receipt)


def _merge_listener_evidence(
    existing: RuntimeReceipt, incoming: RuntimeReceipt
) -> ListenerObservation:
    candidate = incoming.listener
    # A current observation is authoritative. Missing attribution remains
    # unproven; prior identity is historical evidence, never a backfill.
    if _listener_time(incoming) < _listener_time(existing):
        return existing.listener
    return candidate


def _merge_refresh_evidence(
    existing: RuntimeReceipt, incoming: RuntimeReceipt
) -> RefreshEvidence | None:
    if incoming.refresh is None:
        return existing.refresh
    if existing.refresh is None:
        return incoming.refresh
    incoming_time = incoming.refresh.completed_at or incoming.recorded_at
    existing_time = existing.refresh.completed_at or existing.recorded_at
    return incoming.refresh if incoming_time >= existing_time else existing.refresh


def _merge_scheduler_evidence(
    existing: RuntimeReceipt, incoming: RuntimeReceipt
) -> SchedulerEvidence | None:
    if incoming.scheduler is None:
        return existing.scheduler
    if existing.scheduler is None:
        return incoming.scheduler
    return (
        incoming.scheduler
        if incoming.scheduler.observed_at >= existing.scheduler.observed_at
        else existing.scheduler
    )


def produce_daily_refresh_receipt(
    *,
    api_url: str,
    receipt_path: Path,
    now: datetime,
    daily_refresh_owner: str | None = None,
    scheduler_task_name: str | None = None,
    listener_owner: str | None = None,
) -> RuntimeReceipt:
    """Record one read-only daily refresh observation; never starts a listener."""

    from integrations.portfolio_position import (
        supported_schema_major,
        validate_equity_evidence,
        validate_positions_snapshot,
        validate_snapshot_account_coverage,
    )
    from integrations.portfolio_tracker_v1 import PositionsV1Result, TrackerV1Client

    # ``listener_owner`` remains a compatibility parameter for older callers.
    # A read-only probe never attributes the API listener. It becomes an
    # attributable daily completion only when the canonical Scheduler wrapper
    # supplies both its owner and task identity.
    key = derive_daily_refresh_idempotency_key(now)
    scheduled_context = daily_refresh_owner is not None and scheduler_task_name is not None
    lease = AtomicFileLease(receipt_path.with_suffix(".lease"))
    if not lease.acquire():
        return RuntimeReceipt(
            idempotency_key=key,
            lifecycle_state="ownership_conflict",
            recorded_at=now,
            listener=ListenerObservation(healthy=False),
            failure_detail=lease.last_conflict_detail
            or "another Portfolio Tracker writer owns the lease",
        )
    try:
        client = TrackerV1Client(base_url=api_url)
        health_fetch = client.get_health()
        health = health_fetch.data
        listener = ListenerObservation(
            healthy=health_is_healthy(health, now=now),
            health_checked_at=now,
            health=health,
        )
        snapshot_fetch = client.get_portfolio_snapshot() if health_fetch.available else None
        refresh = None
        terminal = "failed"
        detail: str | None = health_fetch.error
        if health is None or not health_is_healthy(health, now=now):
            detail = "tracker health is degraded, stale, unavailable, or clock-incoherent"
        elif snapshot_fetch is None or not snapshot_fetch.available or snapshot_fetch.data is None:
            detail = (
                snapshot_fetch.error
                if snapshot_fetch is not None
                else "portfolio snapshot read unavailable"
            )
        else:
            snapshot = snapshot_fetch.data
            if not supported_schema_major(snapshot.meta.schema_version):
                detail = "portfolio snapshot schema major is unsupported"
            elif not snapshot.meta.currency.strip():
                detail = "portfolio snapshot currency is missing"
            elif any(
                account.value_currency.strip() != snapshot.meta.currency.strip()
                for account in snapshot.accounts
            ):
                detail = "portfolio snapshot account currency does not match envelope currency"
            elif snapshot.meta.is_partial or snapshot.equity_fraction.is_partial:
                detail = "portfolio snapshot is partial"
            elif snapshot.meta.is_stale or snapshot.equity_fraction.is_stale:
                detail = "portfolio snapshot is stale"
            elif snapshot.meta.account_coverage.lagging_account_ids:
                ids = ", ".join(
                    str(account_id)
                    for account_id in snapshot.meta.account_coverage.lagging_account_ids
                )
                detail = f"portfolio snapshot account coverage is lagging for account ids: {ids}"
            elif (equity_error := validate_equity_evidence(snapshot)) is not None:
                detail = equity_error[1]
            elif (snapshot.meta.as_of is None) != (health.latest_snapshot_date is None):
                detail = "health and positions must both provide the portfolio snapshot date"
            elif snapshot.meta.as_of is None and health.latest_snapshot_date is None:
                detail = "portfolio snapshot has no observation date"
            elif snapshot.meta.as_of != health.latest_snapshot_date:
                detail = "health and positions snapshot dates do not match"
            else:
                positions = PositionsV1Result(
                    snapshot_date=snapshot.meta.as_of,
                    total_market_value=snapshot.total_market_value,
                    positions=snapshot.positions,
                    by_tax_treatment=snapshot.by_tax_treatment,
                    notes=[],
                )
                coverage_error = validate_snapshot_account_coverage(snapshot, health)
                reconciliation_error = validate_positions_snapshot(positions)
                if coverage_error is not None:
                    detail = coverage_error[1]
                elif reconciliation_error is None:
                    terminal = "success"
                    detail = None
                    refresh = RefreshEvidence(
                        owner=daily_refresh_owner if scheduled_context else None,
                        snapshot_as_of=snapshot.meta.as_of.isoformat()
                        if snapshot.meta.as_of is not None
                        else None,
                        completed_at=(now if scheduled_context else None),
                        terminal_result=("success" if scheduled_context else "activation_required"),
                    )
                else:
                    detail = reconciliation_error[1]
        if scheduled_context and refresh is None:
            # The scheduled wrapper is an attributable writer even when its
            # probe failed. Preserve that failure instead of retaining a
            # previous successful daily plane as if this invocation had not
            # happened.
            refresh = RefreshEvidence(
                owner=daily_refresh_owner,
                terminal_result="failed",
            )
        scheduler: SchedulerEvidence | None = None
        if scheduled_context:
            assert scheduler_task_name is not None
            scheduler = SchedulerEvidence(
                task_name=scheduler_task_name,
                terminal_result="success" if terminal == "success" else "failed",
                observed_at=now,
            )
        receipt = RuntimeReceipt(
            idempotency_key=key,
            lifecycle_state="already_running" if terminal == "success" else "failed",
            recorded_at=now,
            listener=listener,
            refresh=refresh,
            scheduler=scheduler,
            failure_detail=detail,
        )
        return write_runtime_receipt(receipt_path, receipt)
    finally:
        if not lease.release():
            raise LeaseReleaseError(lease.last_conflict_detail or "daily lease release failed")


__all__ = [
    "LEASE_RELEASE_DEADLINE_SECONDS",
    "PORTFOLIO_TRACKER_RUNTIME_SURFACE_FIELD",
    "AtomicFileLease",
    "LeaseReleaseError",
    "ListenerObservation",
    "PortfolioTrackerRuntimeManager",
    "RefreshEvidence",
    "RuntimeConfig",
    "RuntimeReceipt",
    "SchedulerEvidence",
    "derive_daily_refresh_idempotency_key",
    "produce_daily_refresh_receipt",
    "write_runtime_receipt",
]
