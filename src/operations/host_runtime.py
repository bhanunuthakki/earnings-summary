"""Private-configured, passive host-owner observations; no recovery authority.

The producer alone probes the host. Loaders only read one bounded cached receipt.
Wire models contain no commands, paths, response bodies, or credential material.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from operations.backup_observer import BackupConfig, observe_backup
from operations.topology_observer import TopologyConfig, observe_topology

SHA = r"^[0-9a-f]{64}$"
NAME = r"^[A-Za-z0-9 _.-]{1,100}$"
LIMIT = 128 * 1024
RELATIVE_CONFIG = Path(".private-state/operations/host-owners.config.json")
RELATIVE_RECEIPT = Path(".tmp/operations/host-runtime.latest.json")
WINDOWS_POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
State = Literal["current", "missing", "stale", "invalid", "unavailable"]
Cadence = Literal[
    "continuous", "five_minutes", "daily", "weekly", "disabled", "service", "unspecified"
]
Finding = Literal[
    "probe_unavailable",
    "missing",
    "definition_unapproved",
    "definition_drift",
    "source_definition_unapproved",
    "source_definition_unavailable",
    "source_definition_drift",
    "enablement_drift",
    "service_stopped",
    "service_start_mode_drift",
    "task_failed",
    "completion_missing",
    "completion_stale",
    "next_run_missed",
    "overlap_ignored",
    "readiness_failed",
    "readiness_unavailable",
    "watchdog_missing",
    "watchdog_invalid",
    "watchdog_stale",
    "watchdog_error",
    "watchdog_recovery_unverified",
    "ledger_invalid",
    "maintenance",
    "recovery_pending",
    "recovery_overdue",
    "recovery_age_unknown",
    "backup_receipt_unavailable",
    "backup_evidence_unavailable",
    "backup_evidence_invalid",
    "backup_completion_stale",
    "topology_unavailable",
    "listener_missing",
    "listener_binding_drift",
    "listener_owner_drift",
    "serve_route_drift",
    "funnel_exposure",
]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OwnerConfig(Frozen):
    name: str = Field(pattern=NAME)
    kind: Literal["task", "service"]
    declared_cadence: Cadence = "unspecified"
    task_path: str = Field(default="\\", pattern=r"^\\(?:[A-Za-z0-9_.-]+\\)*$")
    expected_enabled: bool = Field(default=True, strict=True)
    expected_definition_sha256: str | None = Field(default=None, pattern=SHA)
    definition_files: dict[str, str] = Field(default_factory=dict, max_length=16)
    definition_files_required: bool = Field(default=False, strict=True)
    cadence_max_age_seconds: int | None = Field(default=None, ge=60, le=366 * 86400)
    expected_start_mode: Literal["Auto", "Manual", "Disabled"] = "Auto"
    readiness_url: str | None = None
    readiness_required: bool = Field(default=False, strict=True)
    allowed_running_overlap_results: tuple[int, ...] = Field(default=(), max_length=4)
    evidence: Literal["none", "watchdog", "backup_unavailable", "backup"] = "none"
    backup: BackupConfig | None = None

    @model_validator(mode="after")
    def evidence_config(self) -> OwnerConfig:
        if (self.evidence == "backup") != (self.backup is not None):
            raise ValueError("backup evidence requires explicit sources")
        return self

    @field_validator("definition_files")
    @classmethod
    def approved_local_files(cls, value: dict[str, str]) -> dict[str, str]:
        from pathlib import PureWindowsPath

        if len({key.casefold() for key in value}) != len(value):
            raise ValueError("duplicate definition source")
        for name, digest in value.items():
            path = PureWindowsPath(name)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or re.fullmatch(r"[A-Za-z]:", path.drive) is None
                or re.fullmatch(SHA, digest) is None
            ):
                raise ValueError("definition source must be an approved local file hash")
        return value

    @field_validator("readiness_url")
    @classmethod
    def loopback_only(cls, value: str | None) -> str | None:
        if value is not None:
            url = urlsplit(value)
            if (
                url.scheme != "http"
                or url.hostname != "127.0.0.1"
                or not url.port
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("readiness must be an explicit credential-free loopback URL")
        return value


class HostConfig(Frozen):
    version: Literal[1] = 1
    topology: TopologyConfig | None = None
    owners: tuple[OwnerConfig, ...] = Field(min_length=1, max_length=16)
    watchdog_health_path: str | None = None
    watchdog_max_age_seconds: int = Field(default=900, ge=60, le=3600)
    recovery_max_age_seconds: int = Field(default=900, ge=60, le=3600)

    @model_validator(mode="after")
    def unique(self) -> HostConfig:
        keys = [(o.kind, o.name.casefold()) for o in self.owners]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate owner")
        if self.topology is not None and ("service", "tailscale") not in keys:
            raise ValueError("topology requires the Tailscale owner")
        if self.watchdog_health_path is not None:
            from pathlib import PureWindowsPath

            path = PureWindowsPath(self.watchdog_health_path)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or re.fullmatch(r"[A-Za-z]:", path.drive) is None
            ):
                raise ValueError("watchdog source must be explicitly absolute")
        return self


class Timed(Frozen):
    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def aware(cls, value: object) -> object:
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("timestamp must be aware")
        return value


class HostOwner(Timed):
    name: str = Field(pattern=NAME)
    kind: Literal["task", "service"]
    declared_cadence: Cadence = "unspecified"
    state: Literal[
        "Ready",
        "Running",
        "Disabled",
        "Stopped",
        "Paused",
        "StartPending",
        "StopPending",
        "Missing",
        "Unknown",
    ]
    enabled: bool | None = Field(default=None, strict=True)
    definition_sha256: str | None = Field(default=None, pattern=SHA)
    definition_match: bool | None = Field(default=None, strict=True)
    source_definition_match: bool | None = Field(default=None, strict=True)
    source_definition_sha256: str | None = Field(default=None, pattern=SHA)
    last_attempted_at: datetime | None = None
    last_successful_at: datetime | None = None
    next_expected_at: datetime | None = None
    last_result: int | None = Field(default=None, strict=True)
    cadence_max_age_seconds: int | None = Field(default=None, ge=60)
    readiness: Literal["not_configured", "ready", "failed", "unavailable"] = "not_configured"
    findings: tuple[Finding, ...] = Field(default=(), max_length=24)
    ledger_valid: bool | None = Field(default=None, strict=True)
    maintenance: bool | None = Field(default=None, strict=True)
    recovery_pending: bool | None = Field(default=None, strict=True)
    pending_since: datetime | None = None

    @model_validator(mode="after")
    def pending_is_explicit(self) -> HostOwner:
        if (
            self.recovery_pending
            and self.pending_since is None
            and "recovery_age_unknown" not in self.findings
        ):
            raise ValueError("pending recovery age unavailable without an explicit finding")
        return self


class HostReceipt(Timed):
    schema_version: Literal["host_runtime_receipt.v1"] = "host_runtime_receipt.v1"
    observed_at: datetime
    config_sha256: str | None = Field(default=None, pattern=SHA)
    state: Literal["current", "invalid", "unavailable"] = "current"
    owners: tuple[HostOwner, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def coherent(self) -> HostReceipt:
        if self.state == "current" and (not self.owners or self.config_sha256 is None):
            raise ValueError("current receipt requires reviewed scope")
        keys = [(o.kind, o.name.casefold()) for o in self.owners]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate owner observation")
        for row in self.owners:
            for stamp in (row.last_attempted_at, row.last_successful_at, row.pending_since):
                if stamp is not None and stamp > self.observed_at + timedelta(minutes=5):
                    raise ValueError("owner evidence timestamp is in the future")
        return self


def payload_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class HostRuntimeBundle(Timed):
    schema_version: Literal["host_runtime.v1"] = "host_runtime.v1"
    served_at: datetime
    serving_origin_sha256: str = Field(pattern=SHA)
    code_instance_sha256: str = Field(pattern=SHA)
    receipt_state: State
    receipt: HostReceipt | None = None
    content_sha256: str = Field(pattern=SHA)

    @model_validator(mode="after")
    def coherent(self) -> HostRuntimeBundle:
        if self.content_sha256 != payload_sha(
            self.model_dump(mode="json", exclude={"content_sha256"})
        ):
            raise ValueError("host bundle hash mismatch")
        if self.receipt_state in {"current", "stale"} and self.receipt is None:
            raise ValueError("host receipt missing")
        if (
            self.receipt_state == "current"
            and self.receipt is not None
            and self.receipt.state != "current"
        ):
            raise ValueError("host receipt state mismatch")
        return self


def bounded_read(path: Path) -> bytes:
    if path.is_symlink() or path.resolve(strict=True) != path.absolute():
        raise ValueError("source not direct")
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("source oversized")
    return data


def read_cached(
    root: Path, now: datetime, max_age: timedelta = timedelta(minutes=20)
) -> tuple[State, HostReceipt | None]:
    try:
        receipt = HostReceipt.model_validate_json(bounded_read(root / RELATIVE_RECEIPT))
    except FileNotFoundError:
        return "missing", None
    except (ValueError, OSError):
        return "invalid", None
    if receipt.observed_at > now + timedelta(minutes=5):
        return "invalid", None
    if now - receipt.observed_at > max_age:
        return "stale", receipt
    return receipt.state, receipt


def build_host_bundle(
    root: Path, now: datetime, origin: str, code_identity: str
) -> HostRuntimeBundle:
    state, receipt = read_cached(root, now)
    values = dict(
        served_at=now,
        serving_origin_sha256=hashlib.sha256(origin.encode()).hexdigest(),
        code_instance_sha256=hashlib.sha256(code_identity.encode()).hexdigest(),
        receipt_state=state,
        receipt=receipt,
    )
    provisional = HostRuntimeBundle.model_construct(
        _fields_set=set(values), **values, content_sha256="0" * 64
    )
    return HostRuntimeBundle.model_validate(
        {
            **provisional.model_dump(mode="json"),
            "content_sha256": payload_sha(
                provisional.model_dump(mode="json", exclude={"content_sha256"})
            ),
        }
    )


def attention_count(state: State, receipt: HostReceipt | None) -> int:
    if state != "current" or receipt is None:
        return 1
    return sum(any(f != "overlap_ignored" for f in row.findings) for row in receipt.owners)


def _stamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("naive timestamp")
    return result


def _system_probe(owner: OwnerConfig) -> dict[str, object]:
    # Names are a closed safe character set; no user commands or credential text.
    head = (
        "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
    )
    if owner.kind == "task":
        script = (
            head
            + f"try{{$t=Get-ScheduledTask -TaskPath '{owner.task_path}' -TaskName '{owner.name}' -ErrorAction Stop}}"
            + "catch{if($_.CategoryInfo.Category -eq 'ObjectNotFound'){[Console]::Write('{\"state\":\"Missing\"}');exit}else{throw}};"
        )
        script += (
            'if(!$t){[Console]::Write(\'{"state":"Missing"}\');exit};$i=$t|Get-ScheduledTaskInfo;'
            "$x=Export-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath;"
            "$scheduler=New-Object -ComObject Schedule.Service;$scheduler.Connect();"
            "$folderPath=if($t.TaskPath -eq '\\'){'\\'}else{$t.TaskPath.TrimEnd('\\')};"
            "$sd=$scheduler.GetFolder($folderPath).GetTask($t.TaskName).GetSecurityDescriptor(0x7);"
            "$x=(@($x,$sd)|ConvertTo-Json -Compress);"
            "$h=[Security.Cryptography.SHA256]::Create();$d=([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($x)))).Replace('-','').ToLower();"
            "$r=@{state=[string]$t.State;enabled=[bool]$t.Settings.Enabled;definition_sha256=$d;"
            "multiple_instances=[string]$t.Settings.MultipleInstances;last_result=[long]$i.LastTaskResult;"
            "last_attempted_at=$(if($i.LastRunTime.Year-gt1900){$i.LastRunTime.ToUniversalTime().ToString('o')}else{$null});"
            "next_expected_at=$(if($i.NextRunTime.Year-gt1900){$i.NextRunTime.ToUniversalTime().ToString('o')}else{$null})};"
            "[Console]::Write(($r|ConvertTo-Json -Compress))"
        )
    else:
        script = head + f"$s=Get-CimInstance Win32_Service -Filter \"Name='{owner.name}'\";"
        script += (
            'if(!$s){[Console]::Write(\'{"state":"Missing"}\');exit};'
            f"$security=@(& sc.exe sdshow '{owner.name}');if($LASTEXITCODE -ne 0){{throw 'service security query failed'}};"
            "$sd=@($security|ForEach-Object{$_.Trim()}|Where-Object{$_ -match '^[OGDS]:'});"
            "if($sd.Count -ne 1){throw 'invalid service security projection'};"
            "$p=Get-ItemProperty -LiteralPath ('HKLM:\\SYSTEM\\CurrentControlSet\\Services\\'+$s.Name);"
            "$fa=if($null -eq $p.FailureActions){$null}else{[Convert]::ToBase64String([byte[]]$p.FailureActions)};"
            "$policy=[ordered]@{start=$p.Start;delayed=$p.DelayedAutoStart;dependencies=@($p.DependOnService);failure_actions=$fa;noncrash=$p.FailureActionsOnNonCrashFailures};"
            "$identity=(@([string]$s.PathName,[string]$s.StartName,[string]$s.ServiceType,$sd[0],$policy)|ConvertTo-Json -Depth 4 -Compress);"
            "$h=[Security.Cryptography.SHA256]::Create();$d=([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity)))).Replace('-','').ToLower();"
            "$r=@{state=[string]$s.State;start_mode=[string]$s.StartMode;definition_sha256=$d};"
            "[Console]::Write(($r|ConvertTo-Json -Compress))"
        )
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            [WINDOWS_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        output.seek(0)
        raw = output.read(LIMIT + 1)
    if result.returncode or len(raw) > LIMIT:
        raise ValueError("probe failed")
    row = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(row, dict):
        raise ValueError("invalid probe")
    return cast(dict[str, object], row)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def _readiness(url: str) -> bool:
    # Explicit literal loopback only, no proxies, redirects, body persistence.
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url), timeout=3) as response:
            return response.status == 200
    except HTTPError:
        return False


def observe_owner(
    owner: OwnerConfig, now: datetime, *, previous: HostOwner | None = None
) -> HostOwner:
    try:
        raw = _system_probe(owner)
        findings: list[Finding] = []
        row = HostOwner.model_validate(
            dict(
                name=owner.name,
                kind=owner.kind,
                declared_cadence=owner.declared_cadence,
                state=raw.get("state", "Unknown"),
                enabled=raw.get("enabled"),
                definition_sha256=raw.get("definition_sha256"),
                last_attempted_at=_stamp(raw.get("last_attempted_at")),
                next_expected_at=_stamp(raw.get("next_expected_at")),
                last_result=raw.get("last_result"),
                cadence_max_age_seconds=owner.cadence_max_age_seconds,
            )
        )
        if row.last_attempted_at and row.last_attempted_at > now + timedelta(minutes=5):
            raise ValueError("future task timestamp")
        if row.state == "Missing":
            return row.model_copy(update={"findings": ("missing",)})
        if row.state == "Unknown":
            return row.model_copy(update={"findings": ("probe_unavailable",)})
        matched = (
            row.definition_sha256 == owner.expected_definition_sha256
            if owner.expected_definition_sha256
            else None
        )
        row = row.model_copy(update={"definition_match": matched})
        if matched is None:
            findings.append("definition_unapproved")
        elif not matched:
            findings.append("definition_drift")
        if owner.kind == "service":
            if row.state != "Running":
                findings.append("service_stopped")
            if raw.get("start_mode") != owner.expected_start_mode:
                findings.append("service_start_mode_drift")
        else:
            if row.state not in {"Ready", "Running", "Disabled"}:
                raise ValueError("invalid task state")
            if row.enabled != owner.expected_enabled:
                findings.append("enablement_drift")
            success = previous.last_successful_at if previous else None
            if row.state != "Running" and row.last_result == 0 and row.last_attempted_at:
                success = row.last_attempted_at
            row = row.model_copy(update={"last_successful_at": success})
            if owner.expected_enabled and row.enabled:
                overlap = (
                    row.state == "Running"
                    and raw.get("multiple_instances") == "IgnoreNew"
                    and row.last_result in owner.allowed_running_overlap_results
                )
                if overlap:
                    findings.append("overlap_ignored")
                elif row.state != "Running" and row.last_result not in (None, 0, 267009):
                    findings.append("task_failed")
                if row.state != "Running" and owner.cadence_max_age_seconds:
                    if success is None:
                        findings.append("completion_missing")
                    elif (now - success).total_seconds() > owner.cadence_max_age_seconds:
                        findings.append("completion_stale")
                if (
                    row.state != "Running"
                    and row.next_expected_at
                    and row.next_expected_at < now - timedelta(minutes=20)
                ):
                    findings.append("next_run_missed")
        if owner.readiness_url:
            try:
                ready = _readiness(owner.readiness_url)
                readiness = "ready" if ready else "failed"
                if not ready:
                    findings.append("readiness_failed")
            except (OSError, ValueError):
                readiness = "unavailable"
                findings.append("readiness_unavailable")
            row = row.model_copy(update={"readiness": readiness})
        elif owner.readiness_required:
            findings.append("readiness_unavailable")
            row = row.model_copy(update={"readiness": "unavailable"})
        if owner.evidence == "backup_unavailable":
            findings.append("backup_receipt_unavailable")
        if not owner.definition_files and owner.definition_files_required:
            findings.append("source_definition_unapproved")
        elif owner.definition_files:
            try:
                actual_hashes = [
                    hashlib.sha256(bounded_read(Path(name))).hexdigest()
                    for name in sorted(owner.definition_files, key=str.casefold)
                ]
                expected_hashes = [
                    owner.definition_files[name]
                    for name in sorted(owner.definition_files, key=str.casefold)
                ]
                matched = actual_hashes == expected_hashes
                row = row.model_copy(
                    update={
                        "source_definition_match": matched,
                        "source_definition_sha256": payload_sha(actual_hashes),
                    }
                )
                if not matched:
                    findings.append("source_definition_drift")
            except (OSError, ValueError):
                findings.append("source_definition_unavailable")
        return row.model_copy(update={"findings": tuple(findings)})
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return HostOwner(
            name=owner.name,
            kind=owner.kind,
            declared_cadence=owner.declared_cadence,
            state="Unknown",
            findings=("probe_unavailable",),
        )


def watchdog_evidence(
    row: HostOwner, config: HostConfig, now: datetime, previous: HostOwner | None
) -> HostOwner:
    findings = list(row.findings)
    if config.watchdog_health_path is None:
        return row.model_copy(update={"findings": (*findings, "watchdog_missing")})
    try:
        raw = json.loads(bounded_read(Path(config.watchdog_health_path)))
        stamp = _stamp(raw["observed_at"])
        if stamp is None or stamp > now + timedelta(minutes=5):
            raise ValueError("invalid watchdog time")
        for key in ("recovery_ledger_valid", "maintenance_disabled", "recovery_pending"):
            if type(raw[key]) is not bool:
                raise ValueError("invalid watchdog field")
        if raw["state"] not in {
            "healthy",
            "maintenance",
            "degraded",
            "unhealthy",
            "recovery_pending",
            "error",
        }:
            raise ValueError("invalid watchdog state")
        expected_exit = {
            "healthy": 0,
            "maintenance": 0,
            "degraded": 0,
            "recovery_pending": 0,
            "unhealthy": 1,
            "error": 2,
        }[raw["state"]]
        if (
            raw.get("mode") != "recover"
            or type(raw.get("exit_code")) is not int
            or raw.get("exit_code") != expected_exit
        ):
            findings.append("watchdog_recovery_unverified")
        if raw["state"] == "healthy" and (
            not raw["recovery_ledger_valid"]
            or raw["maintenance_disabled"]
            or raw["recovery_pending"]
        ):
            findings.append("watchdog_invalid")
        if (raw["state"] == "maintenance" and not raw["maintenance_disabled"]) or (
            raw["state"] == "recovery_pending" and not raw["recovery_pending"]
        ):
            findings.append("watchdog_invalid")
        if (now - stamp).total_seconds() > config.watchdog_max_age_seconds:
            findings.append("watchdog_stale")
        if raw["state"] in {"error", "unhealthy", "degraded"}:
            findings.append("watchdog_error")
        if not raw["recovery_ledger_valid"]:
            findings.append("ledger_invalid")
        if raw["maintenance_disabled"]:
            findings.append("maintenance")
        pending = None
        if raw["recovery_pending"]:
            pending = previous.pending_since if previous and previous.recovery_pending else None
            findings.append(
                "recovery_age_unknown"
                if pending is None
                else "recovery_overdue"
                if (now - pending).total_seconds() > config.recovery_max_age_seconds
                else "recovery_pending"
            )
        return row.model_copy(
            update={
                "findings": tuple(findings),
                "ledger_valid": raw["recovery_ledger_valid"],
                "maintenance": raw["maintenance_disabled"],
                "recovery_pending": raw["recovery_pending"],
                "pending_since": pending,
            }
        )
    except FileNotFoundError:
        findings.append("watchdog_missing")
    except (OSError, ValueError, KeyError, TypeError):
        findings.append("watchdog_invalid")
    return row.model_copy(update={"findings": tuple(findings)})


def collect_host_receipt(root: Path, now: datetime) -> HostReceipt:
    try:
        raw = bounded_read(root / RELATIVE_CONFIG)
        config = HostConfig.model_validate_json(raw)
    except FileNotFoundError:
        return HostReceipt(observed_at=now, state="unavailable")
    except (OSError, ValueError):
        return HostReceipt(observed_at=now, state="invalid")
    config_sha = hashlib.sha256(raw).hexdigest()
    _, old = read_cached(root, now)
    previous = (
        {(r.kind, r.name): r for r in old.owners} if old and old.config_sha256 == config_sha else {}
    )
    topology_findings = observe_topology(config.topology) if config.topology else ()
    rows: list[HostOwner] = []
    for owner in config.owners:
        prior = previous.get((owner.kind, owner.name))
        row = observe_owner(owner, now, previous=prior)
        if owner.evidence == "watchdog":
            row = watchdog_evidence(row, config, now, prior)
        if owner.backup is not None:
            backup = observe_backup(owner.backup, now)
            row = row.model_copy(
                update={
                    "findings": tuple(
                        sorted(
                            (set(row.findings) - {"completion_missing", "completion_stale"})
                            | set(backup.findings)
                        )
                    ),
                    "last_successful_at": backup.completed_at,
                }
            )
        if owner.kind == "service" and owner.name.casefold() == "tailscale":
            row = row.model_copy(
                update={"findings": tuple(sorted(set(row.findings) | set(topology_findings)))}
            )
        rows.append(row)
    return HostReceipt(observed_at=now, config_sha256=config_sha, owners=tuple(rows))


def publish_host_receipt(root: Path, receipt: HostReceipt) -> None:
    target = root / RELATIVE_RECEIPT
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.resolve(strict=True) != target.parent.absolute() or target.is_symlink():
        raise ValueError("receipt destination not direct")
    fd, temporary = tempfile.mkstemp(prefix=".host-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(receipt.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
