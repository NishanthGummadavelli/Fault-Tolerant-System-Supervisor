"""Validated registration of additional monitoring targets.

Registering a service only records it in the shared SQLite database. A
*running* supervisor starts monitoring it at the beginning of its next cycle;
the dashboard never runs health checks itself. Registration is disabled
unless an admin token is configured, and it never resolves hostnames or
contacts the endpoint.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
import secrets
from urllib.parse import urlsplit

from dashboard.config import DashboardConfig
from monitoring.database_manager import DatabaseManager

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()/-]{0,59}$")
MAX_URL_LENGTH = 2048
BLOCKED_HOSTS = {"metadata.google.internal", "metadata"}
DEFAULT_SETTINGS = {"timeout_seconds": 3.0, "monitoring_interval_seconds": 10,
                    "healthy_threshold_seconds": 0.5, "unhealthy_threshold_seconds": 1.0}


class RegistrationError(Exception):
    """Raised with user-safe messages when registration is refused."""

    def __init__(self, messages: list[str], status: int = 400) -> None:
        """Store the messages and the HTTP status to report."""
        super().__init__("; ".join(messages))
        self.messages = messages
        self.status = status


def new_csrf_token() -> str:
    """Return a random token for the double-submit CSRF check."""
    return secrets.token_urlsafe(32)


def tokens_match(expected: str | None, supplied: str | None) -> bool:
    """Compare secrets in constant time; empty values never match."""
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected.encode(), supplied.encode())


def normalize_url(url: str) -> str:
    """Normalize a URL for duplicate detection (case, default port, path)."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    port = parts.port
    default = 443 if parts.scheme == "https" else 80
    netloc = f"[{host}]" if ":" in host else host
    if port and port != default:
        netloc += f":{port}"
    return f"{parts.scheme.lower()}://{netloc}{parts.path or '/'}" + (
        f"?{parts.query}" if parts.query else "")


def validate_name(name: str | None) -> tuple[str, list[str]]:
    """Validate a service name; return (clean name, errors)."""
    clean = (name or "").strip()
    if not clean:
        return clean, ["Service name is required."]
    if not NAME_RE.match(clean):
        return clean, ["Service name must be 1-60 characters: letters, digits, spaces and . _ - ( ) /"]
    return clean, []


def validate_url(url: str | None) -> tuple[str, list[str]]:
    """Validate an endpoint URL; return (clean URL, errors)."""
    clean = (url or "").strip()
    if not clean:
        return clean, ["Endpoint URL is required."]
    if len(clean) > MAX_URL_LENGTH or re.search(r"\s", clean):
        return clean, ["Endpoint URL is too long or contains whitespace."]
    try:
        parts = urlsplit(clean)
        host, port = parts.hostname, parts.port
    except ValueError:
        return clean, ["Endpoint URL is malformed."]
    if parts.scheme not in ("http", "https"):
        return clean, ["Only http:// and https:// URLs are allowed."]
    if not host:
        return clean, ["Endpoint URL must include a host name."]
    if parts.username or parts.password:
        return clean, ["Credentials in URLs are not allowed."]
    if port == 0:
        return clean, ["Port must be between 1 and 65535."]
    if parts.fragment:
        return clean, ["Endpoint URL must not contain a fragment (#)."]
    if host.lower() in BLOCKED_HOSTS:
        return clean, ["That host is not allowed."]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_link_local or address.is_unspecified
                    or address.is_multicast or address.is_reserved):
        return clean, ["Link-local, unspecified, multicast and reserved addresses are not allowed."]
    return clean, []


def register_service(config: DashboardConfig, name: str | None, url: str | None) -> dict:
    """Validate and register a new service through the existing DatabaseManager."""
    name, errors = validate_name(name)
    url, url_errors = validate_url(url)
    errors += url_errors
    if errors:
        raise RegistrationError(errors, 400)

    manager = DatabaseManager(config.database_path)
    manager.initialize_database()
    existing = manager.list_services(enabled_only=False)

    if len(existing) >= config.max_services:
        raise RegistrationError([f"The limit of {config.max_services} services has been reached."], 409)
    duplicates = []
    if any(e["service_name"].strip().casefold() == name.casefold() for e in existing):
        duplicates.append("A service with that name is already registered.")
    if any(normalize_url(e["service_url"]) == normalize_url(url) for e in existing):
        duplicates.append("A service with that endpoint is already registered.")
    if duplicates:
        raise RegistrationError(duplicates, 409)

    # New services reuse the settings of the first registered service so that
    # staleness and thresholds match how the supervisor is being run.
    settings = dict(DEFAULT_SETTINGS)
    if existing:
        base = existing[0]
        settings = {key: base[key] for key in DEFAULT_SETTINGS}
    service_id = manager.register_service(
        service_name=name, service_url=url, monitoring_source="dashboard", **settings)
    return {"service_id": service_id, "name": name, "url": url}
