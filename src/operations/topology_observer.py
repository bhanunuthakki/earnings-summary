"""Bounded read-only listener/owner and Tailscale configuration comparison.

Only closed findings leave this adapter. Commands are hashed in the native
projection; no process command, URL response body, or environment is exported.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import tempfile
from pathlib import PureWindowsPath
from typing import cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA = r"^[0-9a-f]{64}$"
NAME = r"^[A-Za-z0-9 _.-]{1,100}$"
PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
TAILSCALE = r"C:\Program Files\Tailscale\tailscale.exe"
LIMIT = 128 * 1024


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ListenerConfig(Frozen):
    name: str = Field(pattern=NAME)
    port: int = Field(strict=True, ge=1, le=65535)
    addresses: tuple[str, ...] = Field(min_length=1, max_length=4)
    executable: str = Field(max_length=512)
    command_sha256: str = Field(pattern=SHA)
    service: str | None = Field(default=None, pattern=NAME)
    task: str | None = Field(default=None, pattern=NAME)
    task_path: str = Field(default="\\", pattern=r"^\\(?:[A-Za-z0-9_.-]+\\)*$")
    task_definition_sha256: str | None = Field(default=None, pattern=SHA)
    anchor_command_sha256: str | None = Field(default=None, pattern=SHA)

    @model_validator(mode="after")
    def approved(self) -> ListenerConfig:
        path = PureWindowsPath(self.executable)
        if (
            not path.is_absolute()
            or not re.fullmatch(r"[A-Za-z]:", path.drive)
            or ".." in path.parts
        ):
            raise ValueError("local executable required")
        if len(set(self.addresses)) != len(self.addresses):
            raise ValueError("duplicate address")
        for address in self.addresses:
            parsed = ipaddress.ip_address(address)
            if parsed.is_unspecified or str(parsed) != address:
                raise ValueError("explicit canonical binding required")
        if bool(self.service) == bool(self.task):
            raise ValueError("exactly one owner required")
        if self.task and not (self.task_definition_sha256 and self.anchor_command_sha256):
            raise ValueError("task identity and process anchor required")
        if self.service and (self.task_definition_sha256 or self.anchor_command_sha256):
            raise ValueError("unexpected task fields")
        return self


class TopologyConfig(Frozen):
    serve_authority: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,200}\.ts\.net:443$")
    routes: dict[str, str] = Field(min_length=1, max_length=8)
    listeners: tuple[ListenerConfig, ...] = Field(min_length=1, max_length=8)

    @field_validator("routes")
    @classmethod
    def private_routes(cls, routes: dict[str, str]) -> dict[str, str]:
        for route, target in routes.items():
            url = urlsplit(target)
            if (
                not re.fullmatch(r"/[A-Za-z0-9/_-]{0,128}", route)
                or url.scheme != "http"
                or url.hostname != "127.0.0.1"
                or not url.port
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("credential-free loopback route required")
        return routes

    @model_validator(mode="after")
    def unique(self) -> TopologyConfig:
        if len({item.port for item in self.listeners}) != len(self.listeners):
            raise ValueError("duplicate listener port")
        return self


class Process(Frozen):
    pid: int = Field(strict=True, ge=1)
    exe: str = Field(max_length=512)
    command_sha256: str = Field(pattern=SHA)
    owner_sid: str = Field(max_length=184)


class Listener(Frozen):
    port: int = Field(strict=True, ge=1, le=65535)
    address: str = Field(max_length=64)
    pid: int = Field(strict=True, ge=1)
    chain: tuple[Process, ...] = Field(min_length=1, max_length=8)


class Service(Frozen):
    pid: int = Field(strict=True, ge=0)
    state: str = Field(max_length=32)


class Task(Frozen):
    state: str = Field(max_length=32)
    enabled: bool = Field(strict=True)
    definition_sha256: str = Field(pattern=SHA)


class Projection(Frozen):
    listeners: tuple[Listener, ...] = Field(max_length=32)
    services: dict[str, Service] = Field(max_length=9)
    tasks: dict[str, Task] = Field(max_length=8)


def compare_topology(config: TopologyConfig, raw: object, serve: object) -> tuple[str, ...]:
    try:
        actual = Projection.model_validate(raw)
        findings: set[str] = set()
        for expected in config.listeners:
            rows = [row for row in actual.listeners if row.port == expected.port]
            if not rows:
                findings.add("listener_missing")
                continue
            if (
                len(rows) != len(expected.addresses)
                or {row.address for row in rows} != set(expected.addresses)
                or len({row.pid for row in rows}) != 1
            ):
                findings.add("listener_binding_drift")
            for row in rows:
                chain = row.chain
                process = chain[0]
                pids = [part.pid for part in chain]
                owner = actual.services.get(expected.service or "Schedule")
                approved = (
                    process.pid == row.pid
                    and len(set(pids)) == len(pids)
                    and process.exe.casefold() == expected.executable.casefold()
                    and process.command_sha256 == expected.command_sha256
                    and all(part.owner_sid == "S-1-5-18" for part in chain)
                    and owner is not None
                    and owner.state == "Running"
                    and owner.pid in pids
                )
                if expected.task:
                    task = actual.tasks.get(expected.task)
                    approved = (
                        approved
                        and task is not None
                        and task.state == "Running"
                        and task.enabled
                        and task.definition_sha256 == expected.task_definition_sha256
                        and any(
                            part.command_sha256 == expected.anchor_command_sha256
                            for part in chain[1:]
                        )
                    )
                if not approved:
                    findings.add("listener_owner_drift")
        if any(
            row.port not in {item.port for item in config.listeners} for row in actual.listeners
        ):
            return ("topology_unavailable",)
        if not isinstance(serve, dict):
            return ("topology_unavailable",)
        serve = cast(dict[str, object], serve)
        funnel = serve.get("AllowFunnel", {})
        if not isinstance(funnel, dict):
            return ("topology_unavailable",)
        funnel = cast(dict[str, object], funnel)
        if any(type(value) is not bool for value in funnel.values()):
            return ("topology_unavailable",)
        if any(funnel.values()):
            findings.add("funnel_exposure")
        expected_serve = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {
                config.serve_authority: {
                    "Handlers": {
                        route: {"Proxy": target} for route, target in config.routes.items()
                    }
                }
            },
        }
        if json.dumps(
            {key: value for key, value in serve.items() if key != "AllowFunnel"}, sort_keys=True
        ) != json.dumps(expected_serve, sort_keys=True):
            findings.add("serve_route_drift")
        return tuple(sorted(findings))
    except (ValueError, TypeError, KeyError, AttributeError):
        return ("topology_unavailable",)


def _json_command(args: list[str], timeout: int) -> object:
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            args, stdout=output, stderr=subprocess.DEVNULL, timeout=timeout, check=False
        )
        output.seek(0)
        raw = output.read(LIMIT + 1)
    if result.returncode or len(raw) > LIMIT:
        raise ValueError("topology projection unavailable")
    return json.loads(raw.decode("utf-8-sig"))


def native_projection(config: TopologyConfig) -> tuple[object, object]:
    # Configuration values inserted below have restrictive validators; executable
    # and routing values are compared only in Python and never become commands.
    services = sorted({item.service or "Schedule" for item in config.listeners})
    script = (
        "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
    )
    script += "$h=[Security.Cryptography.SHA256]::Create();function Hash([string]$v){([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($v)))).Replace('-','').ToLower()};"
    script += "$services=@{};$tasks=@{};"
    for service in services:
        script += f"$s=Get-CimInstance Win32_Service -Filter \"Name='{service}'\";if($s){{$services['{service}']=@{{pid=[long]$s.ProcessId;state=[string]$s.State}}}};"
    if any(item.task for item in config.listeners):
        script += "$scheduler=New-Object -ComObject Schedule.Service;$scheduler.Connect();"
    for item in config.listeners:
        if item.task:
            script += f"$t=Get-ScheduledTask -TaskPath '{item.task_path}' -TaskName '{item.task}';"
            script += "$x=Export-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath;$folder=if($t.TaskPath -eq '\\'){'\\'}else{$t.TaskPath.TrimEnd('\\')};$sd=$scheduler.GetFolder($folder).GetTask($t.TaskName).GetSecurityDescriptor(7);$identity=(@($x,$sd)|ConvertTo-Json -Compress);"
            script += f"$tasks['{item.task}']=@{{state=[string]$t.State;enabled=[bool]$t.Settings.Enabled;definition_sha256=(Hash $identity)}};"
    ports = ",".join(str(item.port) for item in config.listeners)
    script += f"$ports=@({ports});$connections=@(Get-NetTCPConnection -State Listen -ErrorAction Stop|Where-Object{{$ports -contains $_.LocalPort}}|Select-Object -First 33);if($connections.Count -gt 32){{throw 'listener scope excess'}};"
    script += "$rows=@();foreach($c in $connections){$chain=@();$processId=[long]$c.OwningProcess;for($depth=0;$depth -lt 8 -and $processId -gt 0;$depth++){$p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$processId);if(!$p){break};$sid=Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid;if($sid.ReturnValue -ne 0){throw 'owner unavailable'};$chain+=@{pid=[long]$p.ProcessId;exe=[string]$p.ExecutablePath;command_sha256=(Hash ([string]$p.CommandLine));owner_sid=[string]$sid.Sid};if(@($services.Values|Where-Object{$_.pid -eq $processId}).Count -gt 0){break};$processId=[long]$p.ParentProcessId};$rows+=@{port=[int]$c.LocalPort;address=[string]$c.LocalAddress;pid=[long]$c.OwningProcess;chain=@($chain)}};"
    script += "[Console]::Write((@{listeners=@($rows);services=$services;tasks=$tasks}|ConvertTo-Json -Depth 8 -Compress));"
    native = _json_command([PS, "-NoProfile", "-NonInteractive", "-Command", script], 30)
    serve = _json_command([TAILSCALE, "serve", "status", "--json"], 5)
    return native, serve


def observe_topology(config: TopologyConfig) -> tuple[str, ...]:
    try:
        return compare_topology(config, *native_projection(config))
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return ("topology_unavailable",)
