"""Canonical managed-service identities used by runtime governance."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ServiceRole(StrEnum):
    """Stable logical identities independent of deploy-time service names."""

    DASHBOARD = "dashboard"


class ManagedService(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ServiceRole
    name: str
    purpose: str


_MANAGED_SERVICES = (
    ManagedService(
        role=ServiceRole.DASHBOARD,
        name="es-dashboard",
        purpose="localhost research application",
    ),
)
_MANAGED_SERVICES_BY_ROLE = {service.role: service for service in _MANAGED_SERVICES}


def managed_services() -> tuple[ManagedService, ...]:
    return _MANAGED_SERVICES


def managed_service_names() -> tuple[str, ...]:
    return tuple(service.name for service in _MANAGED_SERVICES)


def managed_service_for_role(role: ServiceRole) -> ManagedService:
    """Return the service bound to a stable operational role."""

    return _MANAGED_SERVICES_BY_ROLE[role]


__all__ = [
    "ManagedService",
    "ServiceRole",
    "managed_service_for_role",
    "managed_service_names",
    "managed_services",
]
