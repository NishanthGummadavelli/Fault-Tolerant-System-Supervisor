"""
Unit tests for database initialization, service registration,
health-check persistence, and incident lifecycle persistence.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from monitoring.database_manager import DatabaseManager
from monitoring.health_monitor import HealthCheckResult, HealthStatus


class TestDatabaseManager(unittest.TestCase):
    """Test database operations using isolated temporary databases."""

    def setUp(self) -> None:
        """Create and initialize a temporary database for each test."""
        self.temp_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_directory.name) / "test.db"

        self.database_manager = DatabaseManager(database_path)
        self.database_manager.initialize_database()
        self.service_id = self.database_manager.register_service(
            service_name="Test Service",
            service_url="http://localhost:8000/",
        )

    def tearDown(self) -> None:
        """Remove the temporary database and its associated files."""
        self.temp_directory.cleanup()

    def make_incident(
        self,
        incident_id: str = "INC-001",
        severity: str = "WARNING",
        status: str = "OPEN",
        occurrences: int = 1,
        created_at: datetime | None = None,
        last_updated_at: datetime | None = None,
        resolved_at: datetime | None = None,
        summary: str = "Elevated latency detected.",
    ) -> SimpleNamespace:
        """Build a simple incident object matching the database manager API."""
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            incident_id=incident_id,
            service_url="http://localhost:8000/",
            category="ELEVATED_LATENCY",
            severity=severity,
            status=status,
            summary=summary,
            created_at=created_at or now,
            last_updated_at=last_updated_at or now,
            occurrences=occurrences,
            resolved_at=resolved_at,
        )

    def test_all_four_tables_are_created(self) -> None:
        """Verify that all four expected tables exist."""
        expected_tables = {
            "services",
            "health_checks",
            "incidents",
            "incident_events",
        }
        actual_tables = set(self.database_manager.get_table_names())
        self.assertEqual(actual_tables, expected_tables)

    def test_expected_indexes_are_created(self) -> None:
        """Verify that all three performance indexes exist."""
        expected_indexes = {
            "idx_health_checks_history",
            "idx_incidents_active_lookup",
            "idx_incident_events_ledger",
        }
        actual_indexes = set(self.database_manager.get_index_names())
        self.assertTrue(expected_indexes.issubset(actual_indexes))

    def test_foreign_keys_are_enabled(self) -> None:
        """Verify that foreign-key enforcement is enabled."""
        with self.database_manager.get_connection() as connection:
            result = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(result, 1)

    def test_initialization_is_idempotent(self) -> None:
        """Verify that repeated initialization does not duplicate tables."""
        self.database_manager.initialize_database()
        self.database_manager.initialize_database()
        expected_tables = {
            "services",
            "health_checks",
            "incidents",
            "incident_events",
        }
        self.assertEqual(set(self.database_manager.get_table_names()), expected_tables)

    def test_invalid_timeout_is_rejected(self) -> None:
        """Verify that a non-positive connection timeout is rejected."""
        with self.assertRaises(ValueError):
            DatabaseManager(Path(self.temp_directory.name) / "invalid.db", timeout=0)

    def test_register_service(self) -> None:
        """Verify that a new service is registered successfully."""
        service_id = self.database_manager.register_service(
            service_name="Another Service",
            service_url="http://localhost:9000/",
        )
        self.assertGreater(service_id, 0)

    def test_duplicate_service_registration(self) -> None:
        """Verify that registering the same URL returns its existing ID."""
        first_id = self.database_manager.register_service(
            service_name="Test Service",
            service_url="http://localhost:8000/",
        )
        second_id = self.database_manager.register_service(
            service_name="Another Name",
            service_url="http://localhost:8000/",
        )
        self.assertEqual(first_id, second_id)

    def test_save_health_check(self) -> None:
        """Verify that a health-check result is persisted correctly."""
        result = HealthCheckResult(
            timestamp=datetime.now(timezone.utc),
            url="http://localhost:8000/",
            health=HealthStatus.HEALTHY,
            status_code=200,
            response_time_seconds=0.25,
            error=None,
            error_type=None,
        )
        record_id = self.database_manager.save_health_check(self.service_id, result)
        self.assertGreater(record_id, 0)
        history = self.database_manager.get_health_check_history(self.service_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["health_status"], "HEALTHY")
        self.assertEqual(history[0]["status_code"], 200)
        self.assertAlmostEqual(history[0]["response_time_seconds"], 0.25)

    def test_health_check_history_limit(self) -> None:
        """Verify that history retrieval respects the requested limit."""
        for response_time in (0.1, 0.2, 0.3):
            result = HealthCheckResult(
                timestamp=datetime.now(timezone.utc),
                url="http://localhost:8000/",
                health=HealthStatus.HEALTHY,
                status_code=200,
                response_time_seconds=response_time,
                error=None,
                error_type=None,
            )
            self.database_manager.save_health_check(self.service_id, result)
        history = self.database_manager.get_health_check_history(self.service_id, limit=2)
        self.assertEqual(len(history), 2)

    def test_empty_service_name_is_rejected(self) -> None:
        """Verify that a service with an empty name cannot be registered."""
        with self.assertRaises(ValueError):
            self.database_manager.register_service(
                service_name="   ",
                service_url="http://localhost:9000/",
            )

    def test_invalid_health_check_history_limit_is_rejected(self) -> None:
        """Verify that a non-positive history limit is rejected."""
        with self.assertRaises(ValueError):
            self.database_manager.get_health_check_history(service_id=self.service_id, limit=0)

    def test_save_incident_and_creation_event(self) -> None:
        """Verify that saving an incident creates a CREATED event."""
        incident = self.make_incident()
        incident_id = self.database_manager.save_incident(incident)
        self.assertEqual(incident_id, "INC-001")

        history = self.database_manager.get_incident_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["incident_id"], "INC-001")
        self.assertEqual(history[0]["severity"], "WARNING")
        self.assertEqual(history[0]["status"], "OPEN")

        events = self.database_manager.get_incident_events("INC-001")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "CREATED")

    def test_save_incident_requires_registered_service(self) -> None:
        """Verify that incidents cannot reference an unregistered service."""
        incident = self.make_incident()
        incident.service_url = "http://unknown-service/"
        with self.assertRaises(ValueError):
            self.database_manager.save_incident(incident)

    def test_update_incident_records_updated_event(self) -> None:
        """Verify that ordinary incident updates are persisted and logged."""
        now = datetime.now(timezone.utc)
        incident = self.make_incident(created_at=now, last_updated_at=now)
        self.database_manager.save_incident(incident)

        updated = self.make_incident(
            occurrences=2,
            created_at=now,
            last_updated_at=now + timedelta(seconds=10),
            summary="Latency remains elevated.",
        )
        self.database_manager.update_incident(updated)

        history = self.database_manager.get_incident_history()
        self.assertEqual(history[0]["occurrences"], 2)
        self.assertEqual(history[0]["summary"], "Latency remains elevated.")
        events = self.database_manager.get_incident_events("INC-001")
        self.assertEqual([event["event_type"] for event in events], ["CREATED", "UPDATED"])

    def test_severity_escalation_records_escalated_event(self) -> None:
        """Verify that WARNING-to-CRITICAL escalation is recorded."""
        now = datetime.now(timezone.utc)
        self.database_manager.save_incident(
            self.make_incident(created_at=now, last_updated_at=now)
        )
        escalated = self.make_incident(
            severity="CRITICAL",
            occurrences=3,
            created_at=now,
            last_updated_at=now + timedelta(seconds=5),
            summary="Service latency is critically high.",
        )
        self.database_manager.update_incident(escalated)

        history = self.database_manager.get_incident_history()
        self.assertEqual(history[0]["severity"], "CRITICAL")
        events = self.database_manager.get_incident_events("INC-001")
        self.assertEqual(events[-1]["event_type"], "ESCALATED")
        self.assertEqual(events[-1]["previous_severity"], "WARNING")
        self.assertEqual(events[-1]["new_severity"], "CRITICAL")

    def test_resolving_incident_records_resolved_event(self) -> None:
        """Verify that resolving an incident updates status and event history."""
        now = datetime.now(timezone.utc)
        self.database_manager.save_incident(
            self.make_incident(created_at=now, last_updated_at=now)
        )
        resolved_at = now + timedelta(seconds=15)
        resolved = self.make_incident(
            status="RESOLVED",
            occurrences=2,
            created_at=now,
            last_updated_at=resolved_at,
            resolved_at=resolved_at,
            summary="Service recovered.",
        )
        self.database_manager.update_incident(resolved)

        history = self.database_manager.get_incident_history(status="RESOLVED")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "RESOLVED")
        self.assertIsNotNone(history[0]["resolved_at"])
        events = self.database_manager.get_incident_events("INC-001")
        self.assertEqual(events[-1]["event_type"], "RESOLVED")
        self.assertEqual(events[-1]["previous_status"], "OPEN")
        self.assertEqual(events[-1]["new_status"], "RESOLVED")

    def test_incident_history_filters_by_service_and_status(self) -> None:
        """Verify incident history supports service and status filters."""
        now = datetime.now(timezone.utc)
        self.database_manager.save_incident(
            self.make_incident(incident_id="INC-001", created_at=now, last_updated_at=now)
        )
        self.database_manager.save_incident(
            self.make_incident(
                incident_id="INC-002",
                severity="CRITICAL",
                created_at=now + timedelta(seconds=1),
                last_updated_at=now + timedelta(seconds=1),
            )
        )
        open_history = self.database_manager.get_incident_history(
            service_id=self.service_id,
            status="OPEN",
        )
        self.assertEqual(len(open_history), 2)
        self.assertTrue(all(item["status"] == "OPEN" for item in open_history))

    def test_incident_history_limit_must_be_positive(self) -> None:
        """Verify that incident history rejects a non-positive limit."""
        with self.assertRaises(ValueError):
            self.database_manager.get_incident_history(limit=0)

    def test_incident_events_are_returned_chronologically(self) -> None:
        """Verify lifecycle events are returned in chronological order."""
        now = datetime.now(timezone.utc)
        self.database_manager.save_incident(
            self.make_incident(created_at=now, last_updated_at=now)
        )
        self.database_manager.record_incident_event(
            incident_id="INC-001",
            event_type="UPDATED",
            description="Additional diagnostic information.",
            timestamp=now + timedelta(seconds=5),
        )
        events = self.database_manager.get_incident_events("INC-001")
        self.assertEqual([event["event_type"] for event in events], ["CREATED", "UPDATED"])

    def test_record_incident_event_rejects_unknown_incident(self) -> None:
        """Verify event recording respects the incident foreign key."""
        with self.assertRaises(Exception):
            self.database_manager.record_incident_event(
                incident_id="INC-404",
                event_type="UPDATED",
                description="This incident does not exist.",
            )


if __name__ == "__main__":
    unittest.main()
