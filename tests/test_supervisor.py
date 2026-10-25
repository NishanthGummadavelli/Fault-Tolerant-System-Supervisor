"""Integration tests for the fault-tolerant system supervisor."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitoring.database_manager import DatabaseManager
from monitoring.health_monitor import (
    DiagnosisResult,
    HealthCheckResult,
    HealthStatus,
)
from monitoring.supervisor import SupervisorConfig, SystemSupervisor


class TestSystemSupervisor(unittest.TestCase):
    """Test supervisor coordination, configuration, and persistence."""

    def setUp(self):
        """Initialize an isolated database and supervisor for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "supervisor_test.db"

        self.database = DatabaseManager(database_path)

        self.config = SupervisorConfig(
            service_name="Supervisor Test Service",
            url="http://example.test/",
            timeout=3.0,
            interval=1.0,
            healthy_threshold=0.5,
            unhealthy_threshold=1.0,
        )

        self.supervisor = SystemSupervisor(
            config=self.config,
            database=self.database,
        )

    def tearDown(self):
        """Clean up temporary database files after each test."""
        self.temp_dir.cleanup()

    def make_result(self, health):
        """Create a simulated health-check result for testing."""
        if health == HealthStatus.HEALTHY:
            status_code = 200
            response_time = 0.05
            error = None
            error_type = None
        else:
            status_code = None
            response_time = 0.0
            error = "Simulated connection failure"
            error_type = "ConnectionError"

        return HealthCheckResult(
            timestamp="2026-10-25T12:00:00+00:00",
            url=self.config.url,
            health=health,
            status_code=status_code,
            response_time_seconds=response_time,
            error=error,
            error_type=error_type,
        )

    def make_diagnosis(self):
        """Create a simulated diagnosis for a health-check result."""
        return DiagnosisResult(
            diagnosis="Test diagnosis",
            likely_cause="Simulated test result",
            recommendation="Continue monitoring",
        )

    @patch("monitoring.supervisor.HealthMonitor.run_once")
    def test_run_once_persists_health_check(self, mock_run_once):
        """Verify that a healthy check is persisted in the database."""
        mock_run_once.return_value = (
            self.make_result(HealthStatus.HEALTHY),
            self.make_diagnosis(),
        )

        self.supervisor.run_once()

        history = self.database.get_health_check_history(
            service_id=self.supervisor.service_id,
        )

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["health_status"], "HEALTHY")
        self.assertEqual(history[0]["status_code"], 200)

        mock_run_once.assert_called_once()

    @patch("monitoring.supervisor.HealthMonitor.run_once")
    def test_run_once_processes_unhealthy_results(self, mock_run_once):
        """Verify that repeated failures create a persistent incident."""
        mock_run_once.return_value = (
            self.make_result(HealthStatus.UNHEALTHY),
            self.make_diagnosis(),
        )

        # The incident manager requires consecutive failures before opening
        # a service-unavailable incident.
        self.supervisor.run_once()
        self.supervisor.run_once()

        history = self.database.get_health_check_history(
            service_id=self.supervisor.service_id,
        )

        self.assertEqual(len(history), 2)
        self.assertTrue(
            all(
                record["health_status"] == "UNHEALTHY"
                for record in history
            )
        )

        incidents = self.database.get_incident_history(
            service_id=self.supervisor.service_id,
        )

        self.assertTrue(
            any(
                incident["category"] == "SERVICE_UNAVAILABLE"
                for incident in incidents
            ),
            "Expected a SERVICE_UNAVAILABLE incident after repeated failures.",
        )

    def test_invalid_configuration_is_rejected(self):
        """Verify that invalid supervisor configurations raise ValueError."""
        invalid_configs = [
            SupervisorConfig(service_name=""),
            SupervisorConfig(interval=0.5),
            SupervisorConfig(timeout=0),
            SupervisorConfig(
                healthy_threshold=1.0,
                unhealthy_threshold=0.5,
            ),
        ]

        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    SystemSupervisor(
                        config=config,
                        database=self.database,
                    )


if __name__ == "__main__":
    unittest.main()

"""Integration tests for the fault-tolerant system supervisor."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitoring.database_manager import DatabaseManager
from monitoring.health_monitor import (
    DiagnosisResult,
    HealthCheckResult,
    HealthStatus,
)
from monitoring.supervisor import SupervisorConfig, SystemSupervisor


class TestSystemSupervisor(unittest.TestCase):
    """Test supervisor coordination, configuration, and persistence."""

    def setUp(self):
        """Initialize an isolated database and supervisor for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "supervisor_test.db"

        self.database = DatabaseManager(database_path)

        self.config = SupervisorConfig(
            service_name="Supervisor Test Service",
            url="http://example.test/",
            timeout=3.0,
            interval=1.0,
            healthy_threshold=0.5,
            unhealthy_threshold=1.0,
        )

        self.supervisor = SystemSupervisor(
            config=self.config,
            database=self.database,
        )

    def tearDown(self):
        """Clean up temporary database files after each test."""
        self.temp_dir.cleanup()

    def make_result(self, health):
        """Create a simulated health-check result for testing."""
        if health == HealthStatus.HEALTHY:
            status_code = 200
            response_time = 0.05
            error = None
            error_type = None
        else:
            status_code = None
            response_time = 0.0
            error = "Simulated connection failure"
            error_type = "ConnectionError"

        return HealthCheckResult(
            timestamp="2026-10-25T12:00:00+00:00",
            url=self.config.url,
            health=health,
            status_code=status_code,
            response_time_seconds=response_time,
            error=error,
            error_type=error_type,
        )

    def make_diagnosis(self):
        """Create a simulated diagnosis for a health-check result."""
        return DiagnosisResult(
            diagnosis="Test diagnosis",
            likely_cause="Simulated test result",
            recommendation="Continue monitoring",
        )

    @patch("monitoring.supervisor.HealthMonitor.run_once")
    def test_run_once_persists_health_check(self, mock_run_once):
        """Verify that a healthy check is persisted in the database."""
        mock_run_once.return_value = (
            self.make_result(HealthStatus.HEALTHY),
            self.make_diagnosis(),
        )

        self.supervisor.run_once()

        history = self.database.get_health_check_history(
            service_id=self.supervisor.service_id,
        )

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["health_status"], "HEALTHY")
        self.assertEqual(history[0]["status_code"], 200)

        mock_run_once.assert_called_once()

    @patch("monitoring.supervisor.HealthMonitor.run_once")
    def test_run_once_processes_unhealthy_results(self, mock_run_once):
        """Verify that repeated failures create a persistent incident."""
        mock_run_once.return_value = (
            self.make_result(HealthStatus.UNHEALTHY),
            self.make_diagnosis(),
        )

        # The incident manager requires consecutive failures before opening
        # a service-unavailable incident.
        self.supervisor.run_once()
        self.supervisor.run_once()

        history = self.database.get_health_check_history(
            service_id=self.supervisor.service_id,
        )

        self.assertEqual(len(history), 2)
        self.assertTrue(
            all(
                record["health_status"] == "UNHEALTHY"
                for record in history
            )
        )

        incidents = self.database.get_incident_history(
            service_id=self.supervisor.service_id,
        )

        self.assertTrue(
            any(
                incident["category"] == "SERVICE_UNAVAILABLE"
                for incident in incidents
            ),
            "Expected a SERVICE_UNAVAILABLE incident after repeated failures.",
        )

    def test_invalid_configuration_is_rejected(self):
        """Verify that invalid supervisor configurations raise ValueError."""
        invalid_configs = [
            SupervisorConfig(service_name=""),
            SupervisorConfig(interval=0.5),
            SupervisorConfig(timeout=0),
            SupervisorConfig(
                healthy_threshold=1.0,
                unhealthy_threshold=0.5,
            ),
        ]

        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    SystemSupervisor(
                        config=config,
                        database=self.database,
                    )


if __name__ == "__main__":
    unittest.main()
