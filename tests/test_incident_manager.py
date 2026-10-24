
"""
Fault-Tolerant System Supervisor
Stage 3.3: Incident Detection Tests

Test incident creation, escalation, correlation, and recovery
using simulated health-check results.
"""
import unittest
from types import SimpleNamespace

from monitoring.health_monitor import HealthStatus
from monitoring.incident_manager import (
    IncidentCategory,
    IncidentManager,
    IncidentSeverity,
    IncidentStatus,
)


def create_result(
    health: HealthStatus,
    response_time: float,
    status_code: int | None = 200,
    url: str = "http://test-service.local/",
) -> SimpleNamespace:
    """Create a simulated health-check result without network access."""

    return SimpleNamespace(
        url=url,
        health=health,
        status_code=status_code,
        response_time_seconds=response_time,
        error=None,
        error_type=None,
    )


class TestIncidentManager(unittest.TestCase):
    """Verify incident detection, correlation, escalation, and recovery."""

    def setUp(self) -> None:
        """Create a fresh incident manager for each test."""

        self.manager = IncidentManager()

    def test_three_degraded_checks_open_warning(self) -> None:
        """Three consecutive degraded checks should open a warning."""

        result = create_result(HealthStatus.DEGRADED, 0.7)

        self.assertEqual(self.manager.process_result(result), [])
        self.assertEqual(self.manager.process_result(result), [])

        events = self.manager.process_result(result)
        incidents = self.manager.get_open_incidents()

        self.assertEqual(len(incidents), 1)
        self.assertEqual(
            incidents[0].category,
            IncidentCategory.ELEVATED_LATENCY,
        )
        self.assertEqual(
            incidents[0].severity,
            IncidentSeverity.WARNING,
        )
        self.assertTrue(events)

    def test_degradation_followed_by_unhealthy_latency(self) -> None:
        """Two degraded checks followed by unhealthy latency should be critical."""

        degraded = create_result(HealthStatus.DEGRADED, 0.7)
        unhealthy = create_result(HealthStatus.UNHEALTHY, 1.2)

        self.manager.process_result(degraded)
        self.manager.process_result(degraded)
        events = self.manager.process_result(unhealthy)

        incidents = self.manager.get_open_incidents()

        self.assertEqual(len(incidents), 1)
        self.assertEqual(
            incidents[0].category,
            IncidentCategory.ELEVATED_LATENCY,
        )
        self.assertEqual(
            incidents[0].severity,
            IncidentSeverity.CRITICAL,
        )
        self.assertTrue(events)

    def test_two_availability_failures_open_critical(self) -> None:
        """Two consecutive availability failures should open a critical incident."""

        failure = create_result(
            HealthStatus.UNHEALTHY,
            response_time=0.0,
            status_code=None,
        )

        self.assertEqual(self.manager.process_result(failure), [])
        events = self.manager.process_result(failure)

        incidents = self.manager.get_open_incidents()

        self.assertEqual(len(incidents), 1)
        self.assertEqual(
            incidents[0].category,
            IncidentCategory.SERVICE_UNAVAILABLE,
        )
        self.assertEqual(
            incidents[0].severity,
            IncidentSeverity.CRITICAL,
        )
        self.assertTrue(events)

    def test_matching_failures_update_existing_incident(self) -> None:
        """Further matching failures should update an open incident."""

        failure = create_result(
            HealthStatus.UNHEALTHY,
            response_time=0.0,
            status_code=None,
        )

        self.manager.process_result(failure)
        self.manager.process_result(failure)

        original = self.manager.get_open_incidents()[0]
        original_id = original.incident_id
        original_occurrences = original.occurrences

        self.manager.process_result(failure)
        self.manager.process_result(failure)

        incidents = self.manager.get_open_incidents()

        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].incident_id, original_id)
        self.assertGreater(incidents[0].occurrences, original_occurrences)
        self.assertEqual(len(self.manager.get_incident_history()), 1)

    def test_two_healthy_checks_resolve_incident(self) -> None:
        """Two consecutive healthy checks should resolve an open incident."""

        degraded = create_result(HealthStatus.DEGRADED, 0.7)
        healthy = create_result(HealthStatus.HEALTHY, 0.2)

        # Create a warning incident.
        self.manager.process_result(degraded)
        self.manager.process_result(degraded)
        self.manager.process_result(degraded)

        self.assertEqual(len(self.manager.get_open_incidents()), 1)

        # The first healthy check should not resolve it.
        self.manager.process_result(healthy)
        self.assertEqual(len(self.manager.get_open_incidents()), 1)

        # The second consecutive healthy check confirms recovery.
        events = self.manager.process_result(healthy)

        self.assertEqual(len(self.manager.get_open_incidents()), 0)
        self.assertTrue(events)

        history = self.manager.get_incident_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].status, IncidentStatus.RESOLVED)
        self.assertIsNotNone(history[0].resolved_at)

    def test_latency_incident_escalates_to_critical(self):
        """Verify that an existing latency incident escalates to critical severity."""

        manager = IncidentManager()

        # Three degraded checks should create a WARNING incident.
        for _ in range(3):
            manager.process_result(
                create_result(HealthStatus.DEGRADED, 0.7)
            )

        incidents = manager.get_open_incidents()
        self.assertEqual(len(incidents), 1)

        original_incident_id = incidents[0].incident_id
        self.assertEqual(incidents[0].severity, IncidentSeverity.WARNING)

        # Two consecutive unhealthy-latency checks should escalate the incident.
        for _ in range(2):
            manager.process_result(
                create_result(HealthStatus.UNHEALTHY, 1.2, status_code=200)
            )

        incidents = manager.get_open_incidents()

        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].incident_id, original_incident_id)
        self.assertEqual(incidents[0].severity, IncidentSeverity.CRITICAL)

    def test_new_incident_created_after_recovery(self):
        """Verify that a new failure after recovery creates a separate incident."""

        manager = IncidentManager()

        # Three degraded checks should create the first incident.
        for _ in range(3):
            manager.process_result(
                create_result(HealthStatus.DEGRADED, 0.7)
            )

        first_incident = manager.get_open_incidents()[0]
        first_incident_id = first_incident.incident_id

        # Two healthy checks should resolve the first incident.
        for _ in range(2):
            manager.process_result(
                create_result(HealthStatus.HEALTHY, 0.2, status_code=200)
            )

        self.assertEqual(len(manager.get_open_incidents()), 0)

        history = manager.get_incident_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].status, IncidentStatus.RESOLVED)

        # A new sequence of degraded checks should create another incident.
        for _ in range(3):
            manager.process_result(
                create_result(HealthStatus.DEGRADED, 0.7)
            )

        open_incidents = manager.get_open_incidents()
        history = manager.get_incident_history()

        self.assertEqual(len(open_incidents), 1)
        self.assertNotEqual(open_incidents[0].incident_id, first_incident_id)
        self.assertEqual(len(history), 2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
