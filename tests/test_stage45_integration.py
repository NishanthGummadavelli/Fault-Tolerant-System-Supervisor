
"""
Fault-Tolerant System Supervisor
Stage 4.5: Integration and Persistence Verification

Verify that health checks and incident lifecycle records can be stored
in SQLite and retrieved after creating a new database connection.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from monitoring.database_manager import DatabaseManager
from monitoring.health_monitor import HealthCheckResult, HealthStatus
from monitoring.incident_manager import (
    Incident,
    IncidentCategory,
    IncidentSeverity,
    IncidentStatus,
)


class TestStage45Integration(unittest.TestCase):
    """Test database integration using isolated temporary databases."""

    def setUp(self) -> None:
        """Create a temporary database and register a test service."""
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "integration.db"

        self.database_manager = DatabaseManager(self.database_path)
        self.database_manager.initialize_database()

        self.service_url = "http://test-service.local/"
        self.service_id = self.database_manager.register_service(
            service_name="Stage 4.5 Integration Service",
            service_url=self.service_url,
        )

    def tearDown(self) -> None:
        """Remove the temporary database after each test."""
        self.temp_directory.cleanup()

    @staticmethod
    def timestamp() -> datetime:
        """Return the current timezone-aware UTC timestamp."""
        return datetime.now(timezone.utc)

    def test_health_check_and_incident_lifecycle_persist(self) -> None:
        """Verify health checks and the incident lifecycle persist."""
        # Save a simulated health-check result without network access.
        health_result = HealthCheckResult(
            timestamp=self.timestamp(),
            url=self.service_url,
            health=HealthStatus.UNHEALTHY,
            status_code=None,
            response_time_seconds=0.0,
            error="Simulated connection failure",
            error_type="ConnectionError",
        )

        health_check_id = self.database_manager.save_health_check(
            self.service_id,
            health_result,
        )

        self.assertGreater(health_check_id, 0)

        # Create and persist an incident for the registered service.
        incident = Incident(
            incident_id="INC-TEST-001",
            service_url=self.service_url,
            category=IncidentCategory.SERVICE_UNAVAILABLE,
            severity=IncidentSeverity.WARNING,
            status=IncidentStatus.OPEN,
            summary="Test service is unavailable",
            created_at=self.timestamp(),
            last_updated_at=self.timestamp(),
        )

        saved_incident_id = self.database_manager.save_incident(incident)

        self.assertEqual(saved_incident_id, incident.incident_id)

        # Escalate the incident and persist the severity change.
        incident.severity = IncidentSeverity.CRITICAL
        incident.summary = "Test service remains unavailable"
        incident.last_updated_at = self.timestamp()

        self.database_manager.update_incident(incident)

        # Resolve the incident and persist its resolution timestamp.
        incident.status = IncidentStatus.RESOLVED
        incident.resolved_at = self.timestamp()
        incident.last_updated_at = incident.resolved_at

        self.database_manager.update_incident(incident)

        # Create a new manager to verify persistence across connections.
        reopened_manager = DatabaseManager(self.database_path)
        reopened_manager.initialize_database()

        # Verify the health-check record remains available.
        health_history = reopened_manager.get_health_check_history(
            self.service_id
        )

        self.assertEqual(len(health_history), 1)
        self.assertEqual(health_history[0]["health_status"], "UNHEALTHY")
        self.assertEqual(
            health_history[0]["error"],
            "Simulated connection failure",
        )

        # Verify the resolved incident remains in the database.
        incident_history = reopened_manager.get_incident_history(
            service_id=self.service_id,
            status="RESOLVED",
        )

        self.assertEqual(len(incident_history), 1)
        self.assertEqual(
            incident_history[0]["incident_id"],
            "INC-TEST-001",
        )
        self.assertEqual(incident_history[0]["severity"], "CRITICAL")
        self.assertEqual(incident_history[0]["status"], "RESOLVED")

        # Verify that the lifecycle events were retained.
        events = reopened_manager.get_incident_events("INC-TEST-001")
        event_types = [event["event_type"] for event in events]

        self.assertIn("CREATED", event_types)
        self.assertIn("ESCALATED", event_types)
        self.assertIn("RESOLVED", event_types)

    def test_incident_events_are_ordered_chronologically(self) -> None:
        """Verify that incident events are returned in chronological order."""
        incident = Incident(
            incident_id="INC-TEST-002",
            service_url=self.service_url,
            category=IncidentCategory.ELEVATED_LATENCY,
            severity=IncidentSeverity.WARNING,
            status=IncidentStatus.OPEN,
            summary="Elevated latency detected",
            created_at=self.timestamp(),
            last_updated_at=self.timestamp(),
        )

        self.database_manager.save_incident(incident)

        # Escalate the incident to generate a second lifecycle event.
        incident.severity = IncidentSeverity.CRITICAL
        incident.last_updated_at = self.timestamp()

        self.database_manager.update_incident(incident)

        events = self.database_manager.get_incident_events("INC-TEST-002")
        timestamps = [event["timestamp"] for event in events]
        event_types = [event["event_type"] for event in events]

        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual(event_types[0], "CREATED")
        self.assertIn("ESCALATED", event_types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
