
"""
Database management module for the Fault-Tolerant System Supervisor.

This module initializes the SQLite database, creates the required tables
and indexes, manages database connections, and provides methods for
service registration and health-check persistence.

Database tables:
    1. services
    2. health_checks
    3. incidents
    4. incident_events
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


# Resolve the project root from this file's location.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Store the database in the project's data directory.
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "fault_tolerant_supervisor.db"


class DatabaseManager:
    """Manage database initialization, connections, and persistence."""

    def __init__(
        self,
        database_path: str | Path = DATABASE_PATH,
        timeout: float = 10.0,
    ) -> None:
        """Initialize the database manager with a path and timeout."""
        if timeout <= 0:
            raise ValueError("Database timeout must be greater than zero.")

        self.database_path = Path(database_path)
        self.timeout = timeout

        # Ensure the parent directory exists before opening the database.
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def current_timestamp() -> str:
        """Return the current UTC timestamp in ISO 8601 format."""
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def get_connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured SQLite connection and close it after use."""
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
        )

        # Return query results that can be accessed using column names.
        connection.row_factory = sqlite3.Row

        try:
            # Enforce foreign-key constraints for this connection.
            connection.execute("PRAGMA foreign_keys = ON;")

            # Use a performance-oriented synchronization setting with WAL.
            connection.execute("PRAGMA synchronous = NORMAL;")

            # Commit successful transactions and roll back failed ones.
            with connection:
                yield connection
        finally:
            # Always release the database connection.
            connection.close()

    def initialize_database(self) -> None:
        """Create the four database tables and their supporting indexes."""
        with self.get_connection() as connection:
            # Enable WAL mode for improved reader-writer concurrency.
            connection.execute("PRAGMA journal_mode = WAL;")

            # Table 1: Store the services being monitored.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS services (
                    service_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_name TEXT NOT NULL,
                    service_url TEXT NOT NULL UNIQUE,

                    timeout_seconds REAL NOT NULL DEFAULT 3.0
                        CHECK (timeout_seconds > 0),

                    monitoring_interval_seconds INTEGER NOT NULL DEFAULT 10
                        CHECK (monitoring_interval_seconds > 0),

                    healthy_threshold_seconds REAL NOT NULL DEFAULT 0.5
                        CHECK (healthy_threshold_seconds >= 0),

                    unhealthy_threshold_seconds REAL NOT NULL DEFAULT 1.0
                        CHECK (
                            unhealthy_threshold_seconds >
                            healthy_threshold_seconds
                        ),

                    is_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (is_enabled IN (0, 1)),

                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # Table 2: Store every health-check result.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS health_checks (
                    health_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,

                    health_status TEXT NOT NULL
                        CHECK (
                            health_status IN (
                                'HEALTHY',
                                'DEGRADED',
                                'UNHEALTHY'
                            )
                        ),

                    status_code INTEGER,

                    response_time_seconds REAL NOT NULL
                        CHECK (response_time_seconds >= 0),

                    error TEXT,
                    error_type TEXT,

                    FOREIGN KEY (service_id)
                        REFERENCES services(service_id)
                        ON DELETE RESTRICT
                )
            """)

            # Table 3: Store active and resolved incidents.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    service_id INTEGER NOT NULL,

                    category TEXT NOT NULL
                        CHECK (
                            category IN (
                                'ELEVATED_LATENCY',
                                'SERVICE_UNAVAILABLE'
                            )
                        ),

                    severity TEXT NOT NULL
                        CHECK (severity IN ('WARNING', 'CRITICAL')),

                    status TEXT NOT NULL
                        CHECK (status IN ('OPEN', 'RESOLVED')),

                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_updated_at TEXT NOT NULL,
                    resolved_at TEXT,

                    occurrences INTEGER NOT NULL DEFAULT 1
                        CHECK (occurrences > 0),

                    FOREIGN KEY (service_id)
                        REFERENCES services(service_id)
                        ON DELETE RESTRICT
                )
            """)

            # Table 4: Preserve incident lifecycle events.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS incident_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT NOT NULL,

                    event_type TEXT NOT NULL
                        CHECK (
                            event_type IN (
                                'CREATED',
                                'UPDATED',
                                'ESCALATED',
                                'RESOLVED'
                            )
                        ),

                    description TEXT NOT NULL,
                    previous_severity TEXT,
                    new_severity TEXT,
                    previous_status TEXT,
                    new_status TEXT,
                    timestamp TEXT NOT NULL,

                    FOREIGN KEY (incident_id)
                        REFERENCES incidents(incident_id)
                        ON DELETE RESTRICT
                )
            """)

            # Index health-check history by service and timestamp.
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_health_checks_history
                ON health_checks (service_id, timestamp DESC)
            """)

            # Optimize queries that retrieve open incidents.
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_incidents_active_lookup
                ON incidents (service_id, status)
                WHERE status = 'OPEN'
            """)

            # Index incident events in chronological order.
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_incident_events_ledger
                ON incident_events (incident_id, timestamp ASC)
            """)

    def register_service(
        self,
        service_name: str,
        service_url: str,
        timeout_seconds: float = 3.0,
        monitoring_interval_seconds: int = 10,
        healthy_threshold_seconds: float = 0.5,
        unhealthy_threshold_seconds: float = 1.0,
    ) -> int:
        """Register a service or return its existing database ID."""
        # Normalize inputs before validating or querying the database.
        service_name = service_name.strip()
        service_url = service_url.strip()

        if not service_name:
            raise ValueError("Service name cannot be empty.")

        if not service_url:
            raise ValueError("Service URL cannot be empty.")

        if timeout_seconds <= 0:
            raise ValueError("Timeout must be greater than zero.")

        if monitoring_interval_seconds <= 0:
            raise ValueError("Monitoring interval must be greater than zero.")

        if healthy_threshold_seconds < 0:
            raise ValueError("Healthy threshold cannot be negative.")

        if unhealthy_threshold_seconds <= healthy_threshold_seconds:
            raise ValueError(
                "Unhealthy threshold must exceed the healthy threshold."
            )

        timestamp = self.current_timestamp()

        with self.get_connection() as connection:
            # Avoid registering the same URL more than once.
            existing_service = connection.execute(
                """
                SELECT service_id
                FROM services
                WHERE service_url = ?
                """,
                (service_url,),
            ).fetchone()

            if existing_service:
                return int(existing_service["service_id"])

            # Insert the new service using parameterized SQL.
            cursor = connection.execute(
                """
                INSERT INTO services (
                    service_name,
                    service_url,
                    timeout_seconds,
                    monitoring_interval_seconds,
                    healthy_threshold_seconds,
                    unhealthy_threshold_seconds,
                    is_enabled,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    service_name,
                    service_url,
                    timeout_seconds,
                    monitoring_interval_seconds,
                    healthy_threshold_seconds,
                    unhealthy_threshold_seconds,
                    timestamp,
                    timestamp,
                ),
            )

            return int(cursor.lastrowid)

    def save_health_check(
        self,
        service_id: int,
        result: Any,
    ) -> int:
        """Save a health-check result and return its database record ID."""
        # Support both enum-based and string-based health statuses.
        health_status = getattr(result.health, "value", result.health)

        if health_status not in {"HEALTHY", "DEGRADED", "UNHEALTHY"}:
            raise ValueError(f"Invalid health status: {health_status}")

        if result.response_time_seconds < 0:
            raise ValueError("Response time cannot be negative.")

        # Convert datetime objects into ISO 8601 timestamp strings.
        timestamp = result.timestamp

        if isinstance(timestamp, datetime):
            timestamp = timestamp.isoformat()

        if not isinstance(timestamp, str) or not timestamp.strip():
            raise ValueError("Health-check timestamp must be a non-empty string.")

        with self.get_connection() as connection:
            # Store the health-check result against its registered service.
            cursor = connection.execute(
                """
                INSERT INTO health_checks (
                    service_id,
                    timestamp,
                    health_status,
                    status_code,
                    response_time_seconds,
                    error,
                    error_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    service_id,
                    timestamp,
                    health_status,
                    result.status_code,
                    result.response_time_seconds,
                    result.error,
                    result.error_type,
                ),
            )

            return int(cursor.lastrowid)

    def get_health_check_history(
        self,
        service_id: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return the most recent health-check records for a service."""
        if limit <= 0:
            raise ValueError("History limit must be greater than zero.")

        with self.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    health_check_id,
                    service_id,
                    timestamp,
                    health_status,
                    status_code,
                    response_time_seconds,
                    error,
                    error_type
                FROM health_checks
                WHERE service_id = ?
                ORDER BY timestamp DESC, health_check_id DESC
                LIMIT ?
                """,
                (service_id, limit),
            ).fetchall()

            # Convert database rows into dictionaries for easier integration.
            return [dict(row) for row in rows]


    @staticmethod
    def _enum_value(value: Any) -> str:
        """Return an enum's value or a string representation."""
        return str(getattr(value, "value", value))

    @staticmethod
    def _timestamp_value(value: Any) -> str:
        """Convert a datetime or string timestamp into a validated string."""
        if isinstance(value, datetime):
            return value.isoformat()

        if isinstance(value, str) and value.strip():
            return value

        raise ValueError("Timestamp must be a datetime or non-empty string.")

    def _insert_incident_event(
        self,
        connection: sqlite3.Connection,
        incident_id: str,
        event_type: str,
        description: str,
        timestamp: str,
        previous_severity: str | None = None,
        new_severity: str | None = None,
        previous_status: str | None = None,
        new_status: str | None = None,
    ) -> int:
        """Insert an incident event using an existing database connection."""
        allowed_events = {"CREATED", "UPDATED", "ESCALATED", "RESOLVED"}

        if event_type not in allowed_events:
            raise ValueError(f"Invalid incident event type: {event_type}")

        if not description.strip():
            raise ValueError("Event description cannot be empty.")

        cursor = connection.execute(
            """
            INSERT INTO incident_events (
                incident_id,
                event_type,
                description,
                previous_severity,
                new_severity,
                previous_status,
                new_status,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                event_type,
                description,
                previous_severity,
                new_severity,
                previous_status,
                new_status,
                timestamp,
            ),
        )

        return int(cursor.lastrowid)

    def save_incident(self, incident: Any) -> str:
        """Save a new incident and its creation event in one transaction."""
        incident_id = str(incident.incident_id).strip()
        service_url = str(incident.service_url).strip()
        category = self._enum_value(incident.category)
        severity = self._enum_value(incident.severity)
        status = self._enum_value(incident.status)
        summary = str(incident.summary).strip()
        created_at = self._timestamp_value(incident.created_at)
        updated_at = self._timestamp_value(incident.last_updated_at)
        resolved_at = (
            self._timestamp_value(incident.resolved_at)
            if incident.resolved_at is not None
            else None
        )
        occurrences = incident.occurrences

        if not incident_id:
            raise ValueError("Incident ID cannot be empty.")

        if not service_url:
            raise ValueError("Incident service URL cannot be empty.")

        if not summary:
            raise ValueError("Incident summary cannot be empty.")

        if occurrences < 1:
            raise ValueError("Incident occurrences must be at least one.")

        with self.get_connection() as connection:
            # Find the registered service associated with this incident.
            service = connection.execute(
                """
                SELECT service_id
                FROM services
                WHERE service_url = ?
                """,
                (service_url,),
            ).fetchone()

            if service is None:
                raise ValueError(f"Service is not registered: {service_url}")

            # Insert the incident and its creation event atomically.
            connection.execute(
                """
                INSERT INTO incidents (
                    incident_id,
                    service_id,
                    category,
                    severity,
                    status,
                    summary,
                    created_at,
                    last_updated_at,
                    resolved_at,
                    occurrences
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    service["service_id"],
                    category,
                    severity,
                    status,
                    summary,
                    created_at,
                    updated_at,
                    resolved_at,
                    occurrences,
                ),
            )

            self._insert_incident_event(
                connection=connection,
                incident_id=incident_id,
                event_type="CREATED",
                description=summary,
                timestamp=created_at,
                new_severity=severity,
                new_status=status,
            )

        return incident_id

    def update_incident(self, incident: Any) -> None:
        """Update an existing incident and record its lifecycle change."""
        incident_id = str(incident.incident_id).strip()
        category = self._enum_value(incident.category)
        severity = self._enum_value(incident.severity)
        status = self._enum_value(incident.status)
        summary = str(incident.summary).strip()
        updated_at = self._timestamp_value(incident.last_updated_at)
        resolved_at = (
            self._timestamp_value(incident.resolved_at)
            if incident.resolved_at is not None
            else None
        )
        occurrences = incident.occurrences

        if not incident_id:
            raise ValueError("Incident ID cannot be empty.")

        if not summary:
            raise ValueError("Incident summary cannot be empty.")

        if occurrences < 1:
            raise ValueError("Incident occurrences must be at least one.")

        with self.get_connection() as connection:
            # Read the previous state to determine the appropriate event.
            existing = connection.execute(
                """
                SELECT severity, status
                FROM incidents
                WHERE incident_id = ?
                """,
                (incident_id,),
            ).fetchone()

            if existing is None:
                raise ValueError(f"Incident not found: {incident_id}")

            previous_severity = existing["severity"]
            previous_status = existing["status"]

            # Determine the lifecycle event from the state transition.
            if previous_status != "RESOLVED" and status == "RESOLVED":
                event_type = "RESOLVED"
            elif (
                previous_severity != "CRITICAL"
                and severity == "CRITICAL"
            ):
                event_type = "ESCALATED"
            else:
                event_type = "UPDATED"

            # Persist the latest incident state.
            connection.execute(
                """
                UPDATE incidents
                SET category = ?,
                    severity = ?,
                    status = ?,
                    summary = ?,
                    last_updated_at = ?,
                    resolved_at = ?,
                    occurrences = ?
                WHERE incident_id = ?
                """,
                (
                    category,
                    severity,
                    status,
                    summary,
                    updated_at,
                    resolved_at,
                    occurrences,
                    incident_id,
                ),
            )

            # Keep the lifecycle history in the same transaction.
            self._insert_incident_event(
                connection=connection,
                incident_id=incident_id,
                event_type=event_type,
                description=summary,
                timestamp=updated_at,
                previous_severity=previous_severity,
                new_severity=severity,
                previous_status=previous_status,
                new_status=status,
            )

    def record_incident_event(
        self,
        incident_id: str,
        event_type: str,
        description: str,
        previous_severity: str | None = None,
        new_severity: str | None = None,
        previous_status: str | None = None,
        new_status: str | None = None,
        timestamp: datetime | str | None = None,
    ) -> int:
        """Record an additional lifecycle event and return its event ID."""
        incident_id = incident_id.strip()

        if not incident_id:
            raise ValueError("Incident ID cannot be empty.")

        event_timestamp = self._timestamp_value(
            timestamp if timestamp is not None else self.current_timestamp()
        )

        with self.get_connection() as connection:
            return self._insert_incident_event(
                connection=connection,
                incident_id=incident_id,
                event_type=event_type,
                description=description,
                timestamp=event_timestamp,
                previous_severity=previous_severity,
                new_severity=new_severity,
                previous_status=previous_status,
                new_status=new_status,
            )

    def get_incident_history(
        self,
        service_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Retrieve incident history with optional service and status filters."""
        if limit <= 0:
            raise ValueError("History limit must be greater than zero.")

        if status is not None and status not in {"OPEN", "RESOLVED"}:
            raise ValueError("Status must be OPEN or RESOLVED.")

        query = """
            SELECT
                incident_id,
                service_id,
                category,
                severity,
                status,
                summary,
                created_at,
                last_updated_at,
                resolved_at,
                occurrences
            FROM incidents
            WHERE 1 = 1
        """
        parameters: list[Any] = []

        if service_id is not None:
            query += " AND service_id = ?"
            parameters.append(service_id)

        if status is not None:
            query += " AND status = ?"
            parameters.append(status)

        query += """
            ORDER BY created_at DESC, incident_id DESC
            LIMIT ?
        """
        parameters.append(limit)

        with self.get_connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [dict(row) for row in rows]

    def get_incident_events(
        self,
        incident_id: str,
    ) -> list[dict[str, Any]]:
        """Retrieve an incident's events in chronological order."""
        incident_id = incident_id.strip()

        if not incident_id:
            raise ValueError("Incident ID cannot be empty.")

        with self.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    event_id,
                    incident_id,
                    event_type,
                    description,
                    previous_severity,
                    new_severity,
                    previous_status,
                    new_status,
                    timestamp
                FROM incident_events
                WHERE incident_id = ?
                ORDER BY timestamp ASC, event_id ASC
                """,
                (incident_id,),
            ).fetchall()

            return [dict(row) for row in rows]

    def get_table_names(self) -> list[str]:
        """Return the names of user-created tables in the database."""
        with self.get_connection() as connection:
            rows = connection.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """).fetchall()

            return [row["name"] for row in rows]

    def get_index_names(self) -> list[str]:
        """Return the names of user-created indexes in the database."""
        with self.get_connection() as connection:
            rows = connection.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
            """).fetchall()

            return [row["name"] for row in rows]
