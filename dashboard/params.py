"""Validation of user-supplied query parameters.

Every filter that reaches the repository passes through these helpers, so
unexpected values are rejected with HTTP 400 instead of reaching SQL.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

RANGES = {
    "1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600,
    "7d": 7 * 86400, "30d": 30 * 86400, "all": None,
}
CHART_RANGES = ("1h", "6h", "24h", "7d")
PER_PAGE_CHOICES = (10, 25, 50, 100)
DEFAULT_PER_PAGE = 25
MAX_PAGE = 100000

HEALTH_STATES = ("HEALTHY", "DEGRADED", "UNHEALTHY")
SERVICE_STATES = HEALTH_STATES + ("STALE", "NO_DATA")
SEVERITIES = ("WARNING", "CRITICAL")
INCIDENT_STATUSES = ("OPEN", "RESOLVED")
INCIDENT_CATEGORIES = ("ELEVATED_LATENCY", "SERVICE_UNAVAILABLE")
EVENT_TYPES = ("CREATED", "UPDATED", "ESCALATED", "RESOLVED")
CHECK_CATEGORIES = (
    "SUCCESS", "SLOW_RESPONSE", "HTTP_ERROR", "CONNECTION_FAILURE",
)
SERVICE_SORTS = ("name", "state", "response", "last_check", "incidents")
INCIDENT_SORTS = ("priority", "newest", "updated", "severity", "occurrences")

_ERROR_TYPE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class ValidationError(ValueError):
    """Raised when a query parameter has an unsupported value."""


def _raw(args, name):
    value = args.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def choice(args, name, allowed, default=None, upper=True):
    """Return a parameter restricted to a fixed set of values."""
    value = _raw(args, name)
    if value is None:
        return default
    candidate = value.upper() if upper else value.lower()
    if candidate not in allowed:
        raise ValidationError(f"Invalid value for '{name}'.")
    return candidate


def integer(args, name, minimum=1, maximum=2**31 - 1):
    """Return an optional bounded integer parameter."""
    value = _raw(args, name)
    if value is None:
        return None
    if not re.fullmatch(r"\d{1,10}", value):
        raise ValidationError(f"Invalid value for '{name}'.")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValidationError(f"Invalid value for '{name}'.")
    return number


def text(args, name, max_length=80):
    """Return optional free text (used only as a bound LIKE parameter)."""
    value = _raw(args, name)
    if value is None:
        return None
    if len(value) > max_length:
        raise ValidationError(f"'{name}' is too long.")
    return value


def error_type(args, name="error_type"):
    """Return an optional error-type token such as 'timeout'."""
    value = _raw(args, name)
    if value is None:
        return None
    if not _ERROR_TYPE_RE.match(value):
        raise ValidationError(f"Invalid value for '{name}'.")
    return value


def identifier(value):
    """Validate an incident identifier taken from a URL."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationError("Invalid identifier.")
    return value


def day(args, name):
    """Return an optional YYYY-MM-DD date."""
    value = _raw(args, name)
    if value is None:
        return None
    try:
        return date.fromisoformat(value) if len(value) == 10 else _bad(name)
    except ValueError:
        _bad(name)


def _bad(name):
    raise ValidationError(f"'{name}' must be a date in YYYY-MM-DD format.")


def pagination(args):
    """Return (page, per_page) with safe bounds."""
    page = integer(args, "page", 1, MAX_PAGE) or 1
    per_page = integer(args, "per_page", 1, 100) or DEFAULT_PER_PAGE
    if per_page not in PER_PAGE_CHOICES:
        raise ValidationError("Invalid value for 'per_page'.")
    return page, per_page


def range_cutoff(range_key: str, now: datetime) -> str | None:
    """Return the ISO cutoff timestamp for a range key (None for 'all')."""
    seconds = RANGES[range_key]
    if seconds is None:
        return None
    return (now - timedelta(seconds=seconds)).astimezone(
        timezone.utc).isoformat(timespec="seconds")
