"""Read-only data access and calculations for the dashboard.

Everything here only reads rows written by the supervisor process. The
connection comes from the project's DatabaseManager and is switched to
``query_only`` so the dashboard cannot modify monitoring data. Health states
are taken from the stored ``health_status`` column; no thresholds are
re-implemented here. All SQL is parameterized, and user-controlled sort
options are mapped to fixed SQL fragments.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from dashboard import insights, params
from dashboard.config import DashboardConfig
from monitoring.database_manager import DatabaseManager

# Dashboard-only presentation states (never stored by the supervisor).
STATE_HEALTHY = "HEALTHY"
STATE_DEGRADED = "DEGRADED"
STATE_UNHEALTHY = "UNHEALTHY"
STATE_STALE = "STALE"      # latest check is too old to describe current health
STATE_NO_DATA = "NO_DATA"  # service registered but never checked

HEALTH_STATES = (STATE_HEALTHY, STATE_DEGRADED, STATE_UNHEALTHY)
REQUIRED_TABLES = {"services", "health_checks", "incidents", "incident_events"}
MAX_SERVICES = 500
MAX_CHART_SERVICES = 8
MAX_POINTS_PER_SERIES = 400
MAX_HISTORY_ROWS = 5000
MAX_INSIGHT_SERVICES = 50

_STATE_ORDER = {STATE_UNHEALTHY: 0, STATE_DEGRADED: 1, STATE_STALE: 2,
                STATE_NO_DATA: 3, STATE_HEALTHY: 4}
_BUCKET_SECONDS = {"1h": 300, "6h": 1800, "24h": 3600, "7d": 21600}

_CATEGORY_SQL = {
    "SUCCESS": "(hc.status_code BETWEEN 200 AND 299 AND hc.health_status = 'HEALTHY')",
    "SLOW_RESPONSE": "(hc.status_code BETWEEN 200 AND 299 AND hc.health_status != 'HEALTHY')",
    "HTTP_ERROR": "(hc.status_code IS NOT NULL AND (hc.status_code < 200 OR hc.status_code > 299))",
    "CONNECTION_FAILURE": "(hc.status_code IS NULL)",
}
_INCIDENT_ORDER = {
    "priority": "CASE i.status WHEN 'OPEN' THEN 0 ELSE 1 END, "
                "CASE i.severity WHEN 'CRITICAL' THEN 0 ELSE 1 END, "
                "i.last_updated_at DESC, i.incident_id DESC",
    "newest": "i.created_at DESC, i.incident_id DESC",
    "updated": "i.last_updated_at DESC, i.incident_id DESC",
    "severity": "CASE i.severity WHEN 'CRITICAL' THEN 0 ELSE 1 END, "
                "i.created_at DESC, i.incident_id DESC",
    "occurrences": "i.occurrences DESC, i.created_at DESC, i.incident_id DESC",
}
_INCIDENT_SELECT = """
    SELECT i.incident_id, i.service_id, s.service_name, s.service_url,
           i.category, i.severity, i.status, i.summary, i.created_at,
           i.last_updated_at, i.resolved_at, i.occurrences,
           (SELECT COUNT(*) FROM incident_events e
             WHERE e.incident_id = i.incident_id) AS event_count
    FROM incidents i JOIN services s ON s.service_id = i.service_id
"""


class RepositoryError(Exception):
    """Raised when stored data cannot be read."""


def redact_url(url: str) -> str:
    """Remove credentials, query string and fragment from a URL."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "[unparseable URL]"
    if not host:
        return "[unparseable URL]"
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def parse_timestamp(value) -> datetime | None:
    """Parse a stored ISO 8601 timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    """Format a datetime as second-precision UTC ISO text."""
    return moment.isoformat(timespec="seconds") if moment else None


def _norm(value) -> str | None:
    """Normalize a stored timestamp string for display (UTC, seconds)."""
    return _iso(parse_timestamp(value)) or (value if value else None)


def stale_after_seconds(interval: float, timeout: float, multiplier: float) -> float:
    """Age after which a service's latest check is considered stale.

    multiplier * monitoring interval, plus the request timeout because a
    check can take up to ``timeout`` seconds before it is recorded.
    """
    return multiplier * interval + timeout


def failure_kind(status_code, health_status) -> str | None:
    """Describe *how* a non-healthy check failed, from stored fields."""
    if health_status == "HEALTHY":
        return None
    if status_code is None:
        return "CONNECTION_FAILURE"
    if not 200 <= status_code < 300:
        return "HTTP_ERROR"
    return "SLOW_RESPONSE"


def check_category(status_code, health_status) -> str:
    """Classify a stored check (SUCCESS or one of the failure kinds)."""
    return failure_kind(status_code, health_status) or "SUCCESS"


def classify_freshness(latest_check, stale_after: float, now: datetime):
    """Return (presentation state, age in seconds) for a service."""
    if latest_check is None:
        return STATE_NO_DATA, None
    moment = parse_timestamp(latest_check["timestamp"])
    if moment is None:
        return STATE_STALE, None  # freshness cannot be verified
    age = max(0.0, (now - moment).total_seconds())
    if age > stale_after:
        return STATE_STALE, age
    return latest_check["health_status"], age


def _paginate(total: int, page: int, per_page: int) -> dict:
    """Return pagination metadata, clamping the page into range."""
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    return {"page": page, "pages": pages, "per_page": per_page,
            "total": total, "offset": (page - 1) * per_page,
            "has_prev": page > 1, "has_next": page < pages}


def _like(term: str) -> str:
    """Build an escaped LIKE pattern for user search text."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _decorate_check(row) -> dict:
    """Add derived display fields to a health-check row."""
    item = dict(row)
    item["timestamp"] = _norm(item["timestamp"])
    code = item["status_code"]
    for column in ("diagnosis", "likely_cause", "recommendation"):
        item.setdefault(column, None)   # absent in databases not yet migrated
    item["failure_type"] = insights.failure_type(
        code, item["health_status"], item.get("error_type"))
    item["failure_label"] = insights.FAILURE_TYPE_LABELS.get(item["failure_type"])
    item["failure_kind"] = failure_kind(code, item["health_status"])
    item["category"] = check_category(code, item["health_status"])
    # Elapsed time of a request that got no HTTP response is not latency.
    item["latency_seconds"] = item["response_time_seconds"] if code is not None else None
    return item


def _decorate_incident(row, now: datetime) -> dict:
    """Normalize incident timestamps and add its duration."""
    item = dict(row)
    created = parse_timestamp(item["created_at"])
    ended = parse_timestamp(item["resolved_at"]) if item["resolved_at"] else now
    for key in ("created_at", "last_updated_at", "resolved_at"):
        item[key] = _norm(item[key]) if item.get(key) else None
    item["service_url"] = redact_url(item["service_url"])
    item["duration_seconds"] = (
        max(0.0, (ended - created).total_seconds()) if created and ended else None
    )
    return item


class DashboardRepository:
    """Query stored monitoring data without modifying it."""

    def __init__(self, config: DashboardConfig) -> None:
        """Store the dashboard configuration."""
        self.config = config

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def _connect(self):
        """Open a read-only connection. Never creates the database file."""
        path = self.config.database_path
        if not path.is_file():
            raise FileNotFoundError(path)
        with DatabaseManager(path).get_connection() as connection:
            connection.execute("PRAGMA query_only = ON;")
            yield connection

    @staticmethod
    def _schema_ready(conn) -> bool:
        """Return True when all four monitoring tables exist."""
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        return REQUIRED_TABLES <= tables

    def _query(self, func, empty):
        """Run ``func(conn)``; return ``empty()`` if there is no database."""
        try:
            with self._connect() as conn:
                if not self._schema_ready(conn):
                    return empty()
                return func(conn)
        except FileNotFoundError:
            return empty()
        except sqlite3.Error as error:
            raise RepositoryError("Monitoring data could not be read.") from error

    def _stale_limit(self, srow) -> float:
        """Stale window for one service row."""
        return stale_after_seconds(
            srow["monitoring_interval_seconds"], srow["timeout_seconds"],
            self.config.stale_multiplier)

    @staticmethod
    def _latest_check(conn, service_id: int):
        """Return the newest stored check for a service, or None."""
        row = conn.execute(
            """SELECT * FROM health_checks WHERE service_id = ?
               ORDER BY timestamp DESC, health_check_id DESC LIMIT 1""",
            (service_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- summary
    @staticmethod
    def _empty_summary(now: datetime, data_status: str, message: str) -> dict:
        """Summary used when no usable database exists."""
        return {
            "generated_at": _iso(now), "data_status": data_status,
            "latest_check_at": None,
            "counts": {"total_services": 0, "healthy": 0, "degraded": 0,
                       "unhealthy": 0, "stale": 0, "no_data": 0,
                       "open_incidents": 0, "critical_incidents": 0,
                       "open_incidents_unverified": 0},
            "services": [], "notices": [message],
        }

    def get_summary(self, now: datetime | None = None) -> dict:
        """Return current service states and incident counts."""
        now = now or datetime.now(timezone.utc)
        try:
            with self._connect() as conn:
                if not self._schema_ready(conn):
                    return self._empty_summary(
                        now, "NOT_INITIALIZED",
                        "The monitoring database has not been initialized "
                        "yet. Start the supervisor to create its tables.")
                return self._build_summary(conn, now)
        except FileNotFoundError:
            return self._empty_summary(
                now, "NO_DATABASE",
                "No monitoring database was found. Start the supervisor "
                "process to begin recording data.")
        except sqlite3.Error as error:
            raise RepositoryError("Monitoring data could not be read.") from error

    def _build_summary(self, conn, now: datetime) -> dict:
        """Assemble the summary from the open database connection."""
        service_rows = conn.execute(
            """SELECT * FROM services
               ORDER BY service_name COLLATE NOCASE, service_id LIMIT ?""",
            (MAX_SERVICES,)).fetchall()

        open_by_service: dict[int, int] = {}
        critical_total = 0
        for row in conn.execute(
                """SELECT service_id, severity, COUNT(*) AS total
                   FROM incidents WHERE status = 'OPEN'
                   GROUP BY service_id, severity"""):
            open_by_service[row["service_id"]] = (
                open_by_service.get(row["service_id"], 0) + row["total"])
            if row["severity"] == "CRITICAL":
                critical_total += row["total"]

        # Count-based success rate over the last 24 hours (2xx / all checks).
        day_cutoff = _iso(now - timedelta(hours=24))
        availability = {r["service_id"]: (r["ok"], r["total"]) for r in conn.execute(
            """SELECT service_id, COUNT(*) AS total,
                      SUM(CASE WHEN status_code BETWEEN 200 AND 299
                               THEN 1 ELSE 0 END) AS ok
               FROM health_checks WHERE timestamp >= ? GROUP BY service_id""",
            (day_cutoff,))}

        counts = {"total_services": len(service_rows), "healthy": 0,
                  "degraded": 0, "unhealthy": 0, "stale": 0, "no_data": 0,
                  "open_incidents": sum(open_by_service.values()),
                  "critical_incidents": critical_total,
                  "open_incidents_unverified": 0}
        key_for_state = {STATE_HEALTHY: "healthy", STATE_DEGRADED: "degraded",
                         STATE_UNHEALTHY: "unhealthy", STATE_STALE: "stale",
                         STATE_NO_DATA: "no_data"}

        services, newest = [], None
        for row in service_rows:
            latest = self._latest_check(conn, row["service_id"])
            limit = self._stale_limit(row)
            state, age = classify_freshness(latest, limit, now)
            counts[key_for_state[state]] += 1

            open_count = open_by_service.get(row["service_id"], 0)
            if state in (STATE_STALE, STATE_NO_DATA):
                counts["open_incidents_unverified"] += open_count

            checked_at = parse_timestamp(latest["timestamp"]) if latest else None
            if checked_at and (newest is None or checked_at > newest):
                newest = checked_at

            healthy_row = conn.execute(
                """SELECT timestamp FROM health_checks
                   WHERE service_id = ? AND health_status = 'HEALTHY'
                   ORDER BY timestamp DESC, health_check_id DESC LIMIT 1""",
                (row["service_id"],)).fetchone()

            status_code = latest["status_code"] if latest else None
            ok, total = availability.get(row["service_id"], (0, 0))
            role, role_source = insights.resolve_role(
                row["service_name"], self.config.service_roles)
            source = dict(row).get("monitoring_source") or "supervisor"
            ftype = (insights.failure_type(status_code, latest["health_status"],
                                           latest["error_type"]) if latest else None)
            services.append({
                "service_id": row["service_id"], "name": row["service_name"],
                "role": role, "role_label": insights.ROLE_LABELS[role],
                "role_source": role_source, "monitoring_source": source,
                "role_order": insights.ROLE_ORDER[role],
                "awaiting_first_check": latest is None and source == "dashboard",
                "failure_type": ftype,
                "failure_label": insights.FAILURE_TYPE_LABELS.get(ftype),
                "diagnosis": latest.get("diagnosis") if latest else None,
                "likely_cause": latest.get("likely_cause") if latest else None,
                "recommendation": latest.get("recommendation") if latest else None,
                "successful_checks_24h": ok or 0,
                "url": redact_url(row["service_url"]), "state": state,
                "last_known_health": latest["health_status"] if latest else None,
                "status_code": status_code,
                "response_time_seconds": (
                    latest["response_time_seconds"]
                    if latest and status_code is not None else None),
                "failure_kind": (failure_kind(status_code, latest["health_status"])
                                 if latest else None),
                "error": insights._clip(latest["error"]) if latest else None,
                "error_type": latest["error_type"] if latest else None,
                "last_check_at": _iso(checked_at),
                "last_healthy_at": _norm(healthy_row["timestamp"]) if healthy_row else None,
                "age_seconds": round(age, 1) if age is not None else None,
                "stale_after_seconds": round(limit, 1),
                "open_incidents": open_count,
                "checks_24h": total,
                "availability_24h": round(100.0 * ok / total, 1) if total else None,
                "interval_seconds": row["monitoring_interval_seconds"],
                "healthy_threshold_seconds": row["healthy_threshold_seconds"],
                "unhealthy_threshold_seconds": row["unhealthy_threshold_seconds"],
            })

        summary = {"generated_at": _iso(now), "latest_check_at": _iso(newest),
                   "counts": counts, "services": services, "notices": []}
        self._apply_data_status(summary)
        return summary

    @staticmethod
    def _apply_data_status(summary: dict) -> None:
        """Set the overall data status and explanatory notices."""
        counts, notices = summary["counts"], summary["notices"]
        fresh = counts["healthy"] + counts["degraded"] + counts["unhealthy"]
        total = counts["total_services"]
        if total == 0:
            status = "NO_SERVICES"
            notices.append("No services are registered yet.")
        elif fresh == total:
            status = "FRESH"
        elif fresh > 0:
            status = "PARTIAL"
            notices.append("Some services have no recent health checks. "
                           "Their displayed state is not current.")
        elif counts["stale"] == 0:
            status = "NO_CHECKS"
            notices.append("Services are registered but no health check has been recorded.")
        else:
            status = "STALE"
            notices.append("No service has reported recently. The supervisor may not "
                           "be running; shown values are the last recorded ones.")
        if counts["open_incidents_unverified"]:
            notices.append(
                f"{counts['open_incidents_unverified']} open incident(s) belong to "
                "services without recent data and may be left over from a stopped "
                "supervisor.")
        summary["data_status"] = status

    # ---------------------------------------------------- service listings
    @staticmethod
    def filter_services(services: list[dict], q=None, state=None,
                        sort="name", descending=False) -> list[dict]:
        """Search, filter and sort the summary's service list."""
        items = services
        if q:
            needle = q.lower()
            items = [s for s in items if needle in s["name"].lower()]
        if state:
            items = [s for s in items if s["state"] == state]
        keys = {
            "name": lambda s: s["name"].lower(),
            "state": lambda s: (_STATE_ORDER[s["state"]], s["name"].lower()),
            "response": lambda s: (s["response_time_seconds"] is None,
                                   s["response_time_seconds"] or 0),
            "last_check": lambda s: (s["last_check_at"] is None, s["last_check_at"] or ""),
            "incidents": lambda s: -s["open_incidents"],
        }
        return sorted(items, key=keys[sort], reverse=descending)

    def get_service_detail(self, service_id: int, now=None) -> dict | None:
        """Return one service with history, incidents and diagnosis."""
        now = now or datetime.now(timezone.utc)

        def run(conn):
            srow = conn.execute("SELECT * FROM services WHERE service_id = ?",
                                (service_id,)).fetchone()
            if srow is None:
                return None
            service = dict(srow)
            service["service_url"] = redact_url(service["service_url"])
            service["role"], service["role_source"] = insights.resolve_role(
                service["service_name"], self.config.service_roles)
            service["role_label"] = insights.ROLE_LABELS[service["role"]]
            latest = self._latest_check(conn, service_id)
            state, age = classify_freshness(latest, self._stale_limit(srow), now)
            recent = [_decorate_check(r) for r in conn.execute(
                """SELECT hc.*, s.service_name FROM health_checks hc
                   JOIN services s ON s.service_id = hc.service_id
                   WHERE hc.service_id = ?
                   ORDER BY hc.timestamp DESC, hc.health_check_id DESC LIMIT 25""",
                (service_id,))]
            incidents = [_decorate_incident(r, now) for r in conn.execute(
                _INCIDENT_SELECT + " WHERE i.service_id = ? ORDER BY "
                + _INCIDENT_ORDER["priority"] + " LIMIT 20", (service_id,))]
            problem = conn.execute(
                """SELECT * FROM health_checks WHERE service_id = ?
                   AND health_status != 'HEALTHY'
                   ORDER BY timestamp DESC, health_check_id DESC LIMIT 1""",
                (service_id,)).fetchone()
            raw_service = dict(srow)
            stats = {}
            for key, hours in (("24h", 24), ("7d", 168)):
                row = conn.execute(
                    """SELECT COUNT(*) AS total,
                              SUM(CASE WHEN status_code BETWEEN 200 AND 299
                                       THEN 1 ELSE 0 END) AS ok
                       FROM health_checks WHERE service_id = ? AND timestamp >= ?""",
                    (service_id, _iso(now - timedelta(hours=hours)))).fetchone()
                stats[key] = {"checks": row["total"],
                              "success_rate": round(100.0 * row["ok"] / row["total"], 1)
                              if row["total"] else None}
            return {
                "service": service, "state": state, "age_seconds": age,
                "stale_after_seconds": self._stale_limit(srow),
                "latest": _decorate_check(
                    {**latest, "service_id": service_id}) if latest else None,
                "recent_checks": recent, "incidents": incidents, "stats": stats,
                "diagnosis_latest": insights.diagnose_check(latest, raw_service) if latest else None,
                "diagnosis_problem": insights.diagnose_check(dict(problem), raw_service)
                if problem else None,
            }
        return self._query(run, lambda: None)

    # ------------------------------------------------------ health history
    def list_health_checks(self, filters: dict, page: int, per_page: int,
                           now=None) -> dict:
        """Return a filtered, paginated page of health checks."""
        now = now or datetime.now(timezone.utc)
        where, args = ["1 = 1"], []
        if filters.get("service_id") is not None:
            where.append("hc.service_id = ?"); args.append(filters["service_id"])
        if filters.get("state"):
            where.append("hc.health_status = ?"); args.append(filters["state"])
        if filters.get("category"):
            where.append(_CATEGORY_SQL[filters["category"]])  # fixed fragments
        if filters.get("status_code") is not None:
            where.append("hc.status_code = ?"); args.append(filters["status_code"])
        if filters.get("error_type"):
            where.append("hc.error_type = ?"); args.append(filters["error_type"])
        cutoff = params.range_cutoff(filters.get("range", "24h"), now)
        if cutoff:
            where.append("hc.timestamp >= ?"); args.append(cutoff)
        clause = " AND ".join(where)

        def run(conn):
            total = conn.execute(
                f"SELECT COUNT(*) FROM health_checks hc WHERE {clause}", args).fetchone()[0]
            meta = _paginate(total, page, per_page)
            rows = conn.execute(
                f"""SELECT hc.*, s.service_name
                    FROM health_checks hc JOIN services s ON s.service_id = hc.service_id
                    WHERE {clause}
                    ORDER BY hc.timestamp DESC, hc.health_check_id DESC
                    LIMIT ? OFFSET ?""", [*args, per_page, meta["offset"]]).fetchall()
            items = [_decorate_check(r) for r in rows]
            for item in items:
                item["error"] = insights._clip(item["error"])
            return {"items": items, **meta}
        return self._query(run, lambda: {"items": [], **_paginate(0, 1, per_page)})

    def list_service_options(self) -> list[dict]:
        """Return (id, name) pairs for filter drop-downs."""
        return self._query(
            lambda c: [dict(r) for r in c.execute(
                """SELECT service_id, service_name FROM services
                   ORDER BY service_name COLLATE NOCASE LIMIT ?""", (MAX_SERVICES,))],
            lambda: [])

    # ----------------------------------------------------------- incidents
    def list_incidents(self, filters: dict, page: int, per_page: int, now=None) -> dict:
        """Return a filtered, sorted, paginated page of incidents."""
        now = now or datetime.now(timezone.utc)
        where, args = ["1 = 1"], []
        for column, key in (("i.severity", "severity"), ("i.status", "status"),
                            ("i.category", "category"), ("i.service_id", "service_id")):
            if filters.get(key) is not None:
                where.append(f"{column} = ?"); args.append(filters[key])
        cutoff = params.range_cutoff(filters.get("range", "all"), now)
        if cutoff:
            where.append("i.created_at >= ?"); args.append(cutoff)
        if filters.get("date_from"):
            where.append("i.created_at >= ?"); args.append(filters["date_from"].isoformat())
        if filters.get("date_to"):
            where.append("i.created_at < ?")
            args.append((filters["date_to"] + timedelta(days=1)).isoformat())
        if filters.get("q"):
            where.append("(i.incident_id LIKE ? ESCAPE '\\' OR i.summary LIKE ? ESCAPE '\\'"
                         " OR s.service_name LIKE ? ESCAPE '\\')")
            args.extend([_like(filters["q"])] * 3)
        clause = " AND ".join(where)
        order = _INCIDENT_ORDER[filters.get("sort", "priority")]

        def run(conn):
            total = conn.execute(
                f"""SELECT COUNT(*) FROM incidents i
                    JOIN services s ON s.service_id = i.service_id
                    WHERE {clause}""", args).fetchone()[0]
            meta = _paginate(total, page, per_page)
            rows = conn.execute(
                f"{_INCIDENT_SELECT} WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                [*args, per_page, meta["offset"]]).fetchall()
            items = [_decorate_incident(r, now) for r in rows]
            for item in items:
                item["latest_failure"] = self._latest_failure(conn, item)
            return {"items": items, **meta}
        return self._query(run, lambda: {"items": [], **_paginate(0, 1, per_page)})

    @staticmethod
    def _latest_failure(conn, incident: dict) -> dict | None:
        """Return the newest non-healthy check recorded up to the incident's end."""
        end = incident["resolved_at"] or incident["last_updated_at"]
        # Stored timestamps carry sub-second digits; include the whole final second.
        moment = parse_timestamp(end)
        if moment:
            end = _iso(moment + timedelta(seconds=1))
        row = conn.execute(
            """SELECT * FROM health_checks WHERE service_id = ?
               AND health_status != 'HEALTHY' AND timestamp <= ?
               ORDER BY timestamp DESC, health_check_id DESC LIMIT 1""",
            (incident["service_id"], end)).fetchone()
        if row is None:
            return None
        check = _decorate_check(row)
        return {"timestamp": check["timestamp"], "failure_label": check["failure_label"],
                "diagnosis": check["diagnosis"], "status_code": check["status_code"]}

    def incident_totals(self) -> dict:
        """Return counts that show how occurrences are grouped."""
        def run(conn):
            row = conn.execute(
                """SELECT COUNT(*) AS incidents, COALESCE(SUM(occurrences), 0) AS occurrences,
                          COALESCE(SUM(status = 'OPEN'), 0) AS open_count
                   FROM incidents""").fetchone()
            return dict(row)
        return self._query(run, lambda: {"incidents": 0, "occurrences": 0, "open_count": 0})

    def get_incident(self, incident_id: str, now=None) -> dict | None:
        """Return one incident with events, evidence and diagnosis."""
        now = now or datetime.now(timezone.utc)

        def run(conn):
            row = conn.execute(_INCIDENT_SELECT + " WHERE i.incident_id = ?",
                               (incident_id,)).fetchone()
            if row is None:
                return None
            incident = _decorate_incident(row, now)
            srow = conn.execute("SELECT * FROM services WHERE service_id = ?",
                                (incident["service_id"],)).fetchone()
            events = []
            for e in conn.execute(
                    """SELECT event_id, event_type, description, previous_severity,
                              new_severity, previous_status, new_status, timestamp
                       FROM incident_events WHERE incident_id = ?
                       ORDER BY timestamp ASC, event_id ASC LIMIT 500""", (incident_id,)):
                item = dict(e); item["timestamp"] = _norm(item["timestamp"])
                events.append(item)

            # No foreign key links checks to incidents, so evidence is chosen
            # by service and time: the checks just before detection, then
            # those recorded while the incident was active.
            created_raw = row["created_at"]
            end_raw = row["resolved_at"] or _iso(now)
            lead_in = [_decorate_check(r) for r in conn.execute(
                """SELECT hc.*, s.service_name FROM health_checks hc
                   JOIN services s ON s.service_id = hc.service_id
                   WHERE hc.service_id = ? AND hc.timestamp <= ?
                   ORDER BY hc.timestamp DESC, hc.health_check_id DESC LIMIT 5""",
                (incident["service_id"], created_raw))][::-1]
            during = [_decorate_check(r) for r in conn.execute(
                """SELECT hc.*, s.service_name FROM health_checks hc
                   JOIN services s ON s.service_id = hc.service_id
                   WHERE hc.service_id = ? AND hc.timestamp > ? AND hc.timestamp <= ?
                   ORDER BY hc.timestamp DESC, hc.health_check_id DESC LIMIT 25""",
                (incident["service_id"], created_raw, end_raw))]
            evidence = lead_in + during[::-1]

            diagnosis = None
            problem = [c for c in evidence if c["health_status"] != "HEALTHY"]
            chosen = (problem or evidence or [None])[-1]
            if chosen is not None:
                diagnosis = insights.diagnose_check(chosen, dict(srow))

            latest = self._latest_check(conn, incident["service_id"])
            state, age = classify_freshness(latest, self._stale_limit(srow), now)
            return {"incident": incident, "events": events,
                    "lead_in": lead_in, "during": during, "diagnosis": diagnosis,
                    "explanation": insights.explain_incident(incident),
                    "service_state": state, "service_age_seconds": age,
                    "latest_failure": self._latest_failure(conn, incident)}
        return self._query(run, lambda: None)

    def list_events(self, filters: dict, page: int, per_page: int) -> dict:
        """Return a paginated list of incident lifecycle events."""
        where, args = ["1 = 1"], []
        if filters.get("event_type"):
            where.append("e.event_type = ?"); args.append(filters["event_type"])
        if filters.get("service_id") is not None:
            where.append("i.service_id = ?"); args.append(filters["service_id"])
        clause = " AND ".join(where)
        base = """FROM incident_events e
                  JOIN incidents i ON i.incident_id = e.incident_id
                  JOIN services s ON s.service_id = i.service_id"""

        def run(conn):
            total = conn.execute(f"SELECT COUNT(*) {base} WHERE {clause}", args).fetchone()[0]
            meta = _paginate(total, page, per_page)
            rows = conn.execute(
                f"""SELECT e.event_id, e.incident_id, e.event_type, e.description,
                           e.previous_severity, e.new_severity, e.previous_status,
                           e.new_status, e.timestamp, i.service_id, s.service_name
                    {base} WHERE {clause}
                    ORDER BY e.timestamp DESC, e.event_id DESC LIMIT ? OFFSET ?""",
                [*args, per_page, meta["offset"]]).fetchall()
            items = []
            for r in rows:
                item = dict(r); item["timestamp"] = _norm(item["timestamp"])
                items.append(item)
            return {"items": items, **meta}
        return self._query(run, lambda: {"items": [], **_paginate(0, 1, per_page)})

    # -------------------------------------------------------------- charts
    def response_time_series(self, range_key: str, service_id=None, now=None) -> dict:
        """Return latency points (2xx only) and failure markers per service."""
        now = now or datetime.now(timezone.utc)
        cutoff = params.range_cutoff(range_key, now)

        def run(conn):
            where, args = "", []
            if service_id is not None:
                where, args = "WHERE service_id = ?", [service_id]
            srows = conn.execute(
                f"""SELECT * FROM services {where}
                    ORDER BY service_name COLLATE NOCASE, service_id LIMIT ?""",
                [*args, MAX_CHART_SERVICES + 1]).fetchall()
            series, truncated = [], len(srows) > MAX_CHART_SERVICES
            for srow in srows[:MAX_CHART_SERVICES]:
                rows = conn.execute(
                    """SELECT timestamp, health_status, status_code, error_type
                       , response_time_seconds FROM health_checks
                       WHERE service_id = ? AND timestamp >= ?
                       ORDER BY timestamp DESC, health_check_id DESC LIMIT ?""",
                    (srow["service_id"], cutoff, MAX_POINTS_PER_SERIES)).fetchall()
                truncated = truncated or len(rows) == MAX_POINTS_PER_SERIES
                points, failures = [], []
                for r in reversed(rows):
                    code, when = r["status_code"], _norm(r["timestamp"])
                    if code is not None and 200 <= code < 300:
                        points.append({"t": when, "v": round(r["response_time_seconds"] * 1000, 1),
                                       "state": r["health_status"], "status": code})
                    else:
                        failures.append({"t": when, "status": code,
                                         "kind": failure_kind(code, r["health_status"]),
                                         "error_type": r["error_type"]})
                series.append({"id": srow["service_id"], "name": srow["service_name"],
                               "points": points, "failures": failures,
                               "gap_seconds": self._stale_limit(srow),
                               "healthy_ms": srow["healthy_threshold_seconds"] * 1000,
                               "unhealthy_ms": srow["unhealthy_threshold_seconds"] * 1000})
            return {"range": range_key, "range_start": cutoff, "range_end": _iso(now),
                    "series": series, "truncated": truncated, "unit": "ms"}
        return self._query(run, lambda: {"range": range_key, "range_start": cutoff,
                                         "range_end": _iso(now), "series": [],
                                         "truncated": False, "unit": "ms"})

    def history_buckets(self, range_key: str, service_id=None, now=None) -> dict:
        """Return per-bucket counts of checks by recorded health status."""
        now = now or datetime.now(timezone.utc)
        size = _BUCKET_SECONDS[range_key]
        start = (now - timedelta(seconds=params.RANGES[range_key])).timestamp()
        first = int(start // size) * size
        last = int(now.timestamp() // size) * size
        buckets = {t: {"HEALTHY": 0, "DEGRADED": 0, "UNHEALTHY": 0}
                   for t in range(first, last + size, size)}

        def run(conn):
            where, args = "", []
            if service_id is not None:
                where, args = "AND service_id = ?", [service_id]
            rows = conn.execute(
                f"""SELECT timestamp, health_status FROM health_checks
                    WHERE timestamp >= ? {where}
                    ORDER BY timestamp DESC LIMIT ?""",
                [params.range_cutoff(range_key, now), *args, MAX_HISTORY_ROWS]).fetchall()
            for r in rows:
                moment = parse_timestamp(r["timestamp"])
                if moment is None:
                    continue
                key = int(moment.timestamp() // size) * size
                if key in buckets:
                    buckets[key][r["health_status"]] += 1
            return len(rows) == MAX_HISTORY_ROWS
        truncated = self._query(run, lambda: False)
        return {"range": range_key, "bucket_seconds": size, "truncated": truncated,
                "buckets": [{"start": datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds"),
                             **counts} for t, counts in sorted(buckets.items())]}

    def incident_distribution(self) -> dict:
        """Return incident counts grouped by status and severity."""
        def run(conn):
            found = {(r["status"], r["severity"]): r["total"] for r in conn.execute(
                "SELECT status, severity, COUNT(*) AS total FROM incidents GROUP BY status, severity")}
            tones = {"CRITICAL": "bad", "WARNING": "warn"}
            return {"items": [
                {"label": f"{status.title()} \u00b7 {sev.title()}",
                 "value": found.get((status, sev), 0), "tone": tones[sev]}
                for status in ("OPEN", "RESOLVED") for sev in ("CRITICAL", "WARNING")]}
        return self._query(run, lambda: {"items": []})

    # ------------------------------------------------------------ insights
    def get_insights(self, summary: dict, now=None) -> list[dict]:
        """Return observations derived from the current stored records."""
        now = now or datetime.now(timezone.utc)
        day_ago = _iso(now - timedelta(hours=24))

        def run(conn):
            samples = {}
            for s in summary["services"][:MAX_INSIGHT_SERVICES]:
                if s["state"] in HEALTH_STATES:
                    samples[s["service_id"]] = [r[0] for r in conn.execute(
                        """SELECT response_time_seconds FROM health_checks
                           WHERE service_id = ? AND status_code BETWEEN 200 AND 299
                           ORDER BY timestamp DESC, health_check_id DESC LIMIT 10""",
                        (s["service_id"],))]
            resolved = [dict(r) | {"resolved_at": _norm(r["resolved_at"])} for r in conn.execute(
                """SELECT incident_id, service_id, resolved_at FROM incidents
                   WHERE status = 'RESOLVED' AND resolved_at >= ?
                   ORDER BY resolved_at DESC LIMIT 20""", (day_ago,))]
            opened = [dict(r) | {"created_at": _norm(r["created_at"])} for r in conn.execute(
                """SELECT incident_id, service_id, severity, category, created_at
                   FROM incidents WHERE status = 'OPEN' AND created_at >= ?
                   ORDER BY created_at DESC LIMIT 20""", (day_ago,))]
            return samples, resolved, opened
        samples, resolved, opened = self._query(run, lambda: ({}, [], []))
        return insights.build_insights(
            summary["services"], samples, resolved, opened,
            summary["counts"]["open_incidents"], now)

    def get_diagnoses(self, summary: dict) -> list[dict]:
        """Return diagnoses for each service's latest and last problem check."""
        def run(conn):
            out = []
            for s in summary["services"]:
                srow = conn.execute("SELECT * FROM services WHERE service_id = ?",
                                    (s["service_id"],)).fetchone()
                latest = self._latest_check(conn, s["service_id"])
                problem = conn.execute(
                    """SELECT * FROM health_checks WHERE service_id = ?
                       AND health_status != 'HEALTHY'
                       ORDER BY timestamp DESC, health_check_id DESC LIMIT 1""",
                    (s["service_id"],)).fetchone()
                open_ids = [r[0] for r in conn.execute(
                    """SELECT incident_id FROM incidents WHERE service_id = ?
                       AND status = 'OPEN' ORDER BY created_at DESC LIMIT 5""",
                    (s["service_id"],))]
                out.append({"service": s,
                            "latest": insights.diagnose_check(latest, dict(srow)) if latest else None,
                            "problem": insights.diagnose_check(dict(problem), dict(srow)) if problem else None,
                            "open_incident_ids": open_ids})
            return out
        return self._query(run, lambda: [])

    def get_system_info(self) -> dict:
        """Return record counts and the monitored time span."""
        def run(conn):
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in sorted(REQUIRED_TABLES)}
            span = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM health_checks").fetchone()
            return {"available": True, "counts": counts,
                    "first_check_at": _norm(span[0]), "last_check_at": _norm(span[1])}
        return self._query(run, lambda: {"available": False, "counts": {},
                                         "first_check_at": None, "last_check_at": None})

    # ------------------------------------------------- application level
    def get_application(self, summary: dict) -> dict:
        """Return application status, availability and response-time summary.

        Availability = HTTP 2xx checks / all checks over the last 24 hours,
        measured on the load balancer endpoint (what a user would reach).
        Slow but successful responses count as available. With no load
        balancer target there is no end-to-end measurement, so none is shown.
        """
        services = summary["services"]
        status = insights.application_status(services)
        albs = [s for s in services if s["role"] == "load_balancer"]
        checks = sum(s["checks_24h"] for s in albs)
        ok = sum(s["successful_checks_24h"] for s in albs)
        availability = {
            "window_hours": 24,
            "value": round(100.0 * ok / checks, 1) if checks else None,
            "checks": checks,
            "basis": albs[0]["name"] if albs else None,
            "formula": "HTTP 2xx checks \u00f7 all checks in the last 24 hours on the load balancer endpoint",
        }
        times = [s["response_time_seconds"] for s in services
                 if s["state"] in HEALTH_STATES and s["response_time_seconds"] is not None]
        response = {"average_seconds": round(sum(times) / len(times), 3) if times else None,
                    "samples": len(times)}
        return {"status": status, "availability": availability, "response": response}

    def availability_by_service(self, range_key: str, now=None) -> dict:
        """Return per-service availability (2xx share) as chart items."""
        now = now or datetime.now(timezone.utc)
        cutoff = params.range_cutoff(range_key, now)

        def run(conn):
            rows = conn.execute(
                """SELECT s.service_name, COUNT(h.health_check_id) AS total,
                          COALESCE(SUM(CASE WHEN h.status_code BETWEEN 200 AND 299
                                            THEN 1 ELSE 0 END), 0) AS ok
                   FROM services s LEFT JOIN health_checks h
                        ON h.service_id = s.service_id AND h.timestamp >= ?
                   GROUP BY s.service_id ORDER BY s.service_name COLLATE NOCASE LIMIT ?""",
                (cutoff, MAX_SERVICES)).fetchall()
            items = []
            for r in rows:
                if not r["total"]:
                    continue    # no checks in the window: nothing to claim
                pct = round(100.0 * r["ok"] / r["total"], 1)
                tone = "ok" if pct >= 99 else "warn" if pct >= 90 else "bad"
                items.append({"label": f"{r['service_name']} ({r['total']} checks)",
                              "value": pct, "tone": tone, "suffix": "%"})
            return {"items": items, "max": 100, "range": range_key}
        return self._query(run, lambda: {"items": [], "max": 100, "range": range_key})

    def get_transitions(self, range_key: str, service_id=None, now=None,
                        limit: int = 60) -> list[dict]:
        """Return per-check health changes (failure started, recovered, ...).

        These are changes between consecutive stored checks, not incidents:
        the incident manager applies its own multi-check rules.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = params.range_cutoff(range_key, now)
        rank = {"HEALTHY": 0, "DEGRADED": 1, "UNHEALTHY": 2}

        def run(conn):
            where, args = "WHERE s.service_id = ?" if service_id is not None else "", []
            if service_id is not None:
                args.append(service_id)
            services = conn.execute(
                f"SELECT s.service_id, s.service_name FROM services s {where} "
                "ORDER BY s.service_name COLLATE NOCASE LIMIT ?", [*args, MAX_SERVICES]).fetchall()
            events = []
            for svc in services:
                rows = conn.execute(
                    """SELECT timestamp, health_status, status_code, error_type
                       FROM health_checks WHERE service_id = ?
                       AND (? IS NULL OR timestamp >= ?)
                       ORDER BY timestamp DESC, health_check_id DESC LIMIT 2000""",
                    (svc["service_id"], cutoff, cutoff)).fetchall()
                previous = None
                for row in reversed(rows):
                    if previous is not None and previous["health_status"] != row["health_status"]:
                        before, after = rank[previous["health_status"]], rank[row["health_status"]]
                        kind = ("RECOVERED" if after == 0 else
                                "FAILURE_STARTED" if before == 0 else
                                "WORSENED" if after > before else "IMPROVED")
                        ftype = insights.failure_type(row["status_code"], row["health_status"],
                                                      row["error_type"])
                        events.append({
                            "service_id": svc["service_id"], "service_name": svc["service_name"],
                            "timestamp": _norm(row["timestamp"]), "kind": kind,
                            "from": previous["health_status"], "to": row["health_status"],
                            "failure_label": insights.FAILURE_TYPE_LABELS.get(ftype),
                            "status_code": row["status_code"]})
                    previous = row
            events.sort(key=lambda e: e["timestamp"] or "", reverse=True)
            return events[:limit]
        return self._query(run, lambda: [])
