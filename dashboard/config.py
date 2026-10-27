"""Dashboard configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from monitoring.database_manager import DATABASE_PATH

DEFAULT_REFRESH_SECONDS = 10
MIN_REFRESH_SECONDS = 2
MAX_REFRESH_SECONDS = 300

ROLE_LOAD_BALANCER = "load_balancer"
ROLE_BACKEND = "backend"
ROLE_OTHER = "other"
ROLE_ALIASES = {"alb": ROLE_LOAD_BALANCER, "load_balancer": ROLE_LOAD_BALANCER,
                "load-balancer": ROLE_LOAD_BALANCER, "backend": ROLE_BACKEND,
                "server": ROLE_BACKEND, "other": ROLE_OTHER}
DEFAULT_MAX_SERVICES = 50

DEFAULT_STALE_MULTIPLIER = 3.0
MIN_STALE_MULTIPLIER = 1.5
MAX_STALE_MULTIPLIER = 100.0


def _bounded_number(raw, default, low, high, cast):
    """Parse a number from text; fall back to the default if invalid."""
    if raw is None or not str(raw).strip():
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    if value != value or not low <= value <= high:  # NaN or out of range
        return default
    return value


def parse_roles(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse ``Name=role;Name2=role`` into (casefolded name, role) pairs.

    Unknown roles and malformed entries are ignored rather than guessed.
    """
    pairs = []
    for entry in (raw or "").split(";"):
        if "=" not in entry:
            continue
        name, role = entry.rsplit("=", 1)
        role = ROLE_ALIASES.get(role.strip().lower())
        if name.strip() and role:
            pairs.append((name.strip().casefold(), role))
    return tuple(pairs)


@dataclass(frozen=True)
class DashboardConfig:
    """Settings for the dashboard.

    refresh_seconds is how often the *browser* re-reads stored data. It is
    unrelated to the supervisor's monitoring interval and never triggers a
    health check.
    """

    database_path: Path = DATABASE_PATH
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS
    stale_multiplier: float = DEFAULT_STALE_MULTIPLIER
    # Optional explicit roles; otherwise a role is inferred from the name.
    service_roles: tuple[tuple[str, str], ...] = ()
    # Adding services from the browser is off unless explicitly enabled AND
    # protected by an admin token.
    allow_registration: bool = False
    admin_token: str | None = field(default=None, repr=False)
    max_services: int = DEFAULT_MAX_SERVICES

    @property
    def registration_enabled(self) -> bool:
        """True only when registration is allowed and a token is configured."""
        return bool(self.allow_registration and self.admin_token)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        """Build a configuration from SUPERVISOR_DB_PATH and DASHBOARD_*."""
        env = os.environ if environ is None else environ
        raw_path = (env.get("SUPERVISOR_DB_PATH") or "").strip()
        return cls(
            database_path=Path(raw_path) if raw_path else DATABASE_PATH,
            refresh_seconds=_bounded_number(
                env.get("DASHBOARD_REFRESH_SECONDS"),
                DEFAULT_REFRESH_SECONDS,
                MIN_REFRESH_SECONDS,
                MAX_REFRESH_SECONDS,
                int,
            ),
            stale_multiplier=_bounded_number(
                env.get("DASHBOARD_STALE_MULTIPLIER"),
                DEFAULT_STALE_MULTIPLIER,
                MIN_STALE_MULTIPLIER,
                MAX_STALE_MULTIPLIER,
                float,
            ),
            service_roles=parse_roles(env.get("DASHBOARD_SERVICE_ROLES")),
            allow_registration=(env.get("DASHBOARD_ALLOW_REGISTRATION", "").strip() == "1"),
            admin_token=(env.get("DASHBOARD_ADMIN_TOKEN") or "").strip() or None,
            max_services=_bounded_number(
                env.get("DASHBOARD_MAX_SERVICES"), DEFAULT_MAX_SERVICES, 1, 500, int),
        )
