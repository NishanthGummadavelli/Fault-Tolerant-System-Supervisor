"""Regression tests for independent multi-target monitoring and persistence."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitoring.database_manager import DatabaseManager
from monitoring.health_monitor import DiagnosisResult, HealthCheckResult, HealthStatus
from monitoring.supervisor import SupervisorConfig, SystemSupervisor, TargetConfig


class TestMultiTargetSupervisor(unittest.TestCase):
    """Verify target isolation, diagnosis persistence, and database migration."""

    def setUp(self):
        """Create an isolated database and a supervisor with three targets."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = DatabaseManager(Path(self.temp_dir.name) / "multi.db")
        targets = (
            TargetConfig("Backend Server 1", "http://server-1.test/"),
            TargetConfig("Backend Server 2", "http://server-2.test/"),
            TargetConfig("Application Load Balancer", "http://alb.test/"),
        )
        self.supervisor = SystemSupervisor(
            SupervisorConfig(interval=1, targets=targets),
            database=self.database,
        )

    @staticmethod
    def result(url, status, error_type=None, error=None):
        """Build a deterministic health result for one endpoint."""
        return HealthCheckResult(
            timestamp="2026-10-26T10:00:00+00:00",
            url=url,
            health=HealthStatus.HEALTHY if status == 200 else HealthStatus.UNHEALTHY,
            status_code=status,
            response_time_seconds=0.04,
            error=error,
            error_type=error_type,
        )

    @staticmethod
    def diagnosis(name):
        """Build diagnosis details that can be asserted after persistence."""
        return DiagnosisResult(
            diagnosis=name,
            likely_cause=f"Likely cause for {name}",
            recommendation=f"Recommendation for {name}",
        )

    def test_all_targets_are_checked_and_diagnoses_persist(self):
        """Persist an independent check and diagnosis for every target."""
        def fake_run_once(monitor):
            url = monitor.config.url
            healthy = url != "http://server-1.test/"
            result = self.result(
                url,
                200 if healthy else 503,
                None if healthy else "http_error",
                None if healthy else "Service Unavailable",
            )
            diagnosis = self.diagnosis("Healthy endpoint" if healthy else "HTTP server error")
            return result, diagnosis

        with patch("monitoring.supervisor.HealthMonitor.run_once", autospec=True, side_effect=fake_run_once):
            results = self.supervisor.run_once()

        self.assertEqual(set(results), {"Backend Server 1", "Backend Server 2", "Application Load Balancer"})
        with self.database.get_connection() as connection:
            rows = connection.execute(
                """SELECT s.service_name, hc.health_status, hc.status_code,
                          hc.response_time_seconds, hc.diagnosis, hc.likely_cause,
                          hc.recommendation
                   FROM health_checks hc JOIN services s USING(service_id)
                   ORDER BY s.service_name"""
            ).fetchall()

        self.assertEqual(len(rows), 3)
        by_name = {row["service_name"]: dict(row) for row in rows}
        self.assertEqual(by_name["Backend Server 1"]["status_code"], 503)
        self.assertEqual(by_name["Backend Server 1"]["diagnosis"], "HTTP server error")
        self.assertEqual(by_name["Backend Server 2"]["health_status"], "HEALTHY")
        self.assertEqual(by_name["Application Load Balancer"]["health_status"], "HEALTHY")
        self.assertEqual(by_name["Backend Server 1"]["recommendation"], "Recommendation for HTTP server error")

    def test_incident_created_for_failed_target_only(self):
        """Repeated backend failures must not create incidents for healthy targets."""
        def fake_run_once(monitor):
            url = monitor.config.url
            failed = url == "http://server-1.test/"
            result = self.result(url, 503 if failed else 200, "http_error" if failed else None)
            diagnosis = self.diagnosis("HTTP server error" if failed else "Healthy endpoint")
            return result, diagnosis

        with patch("monitoring.supervisor.HealthMonitor.run_once", autospec=True, side_effect=fake_run_once):
            self.supervisor.run_once()
            self.supervisor.run_once()

        incidents = self.database.get_incident_history()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["service_id"], self.supervisor.target_states["http://server-1.test/"]["service_id"])
        self.assertIn("HTTP 503", incidents[0]["summary"])

    def test_existing_database_is_migrated_without_losing_history(self):
        """Initialization should add diagnosis columns to an older SQLite file."""
        old_path = Path(self.temp_dir.name) / "legacy.db"
        with sqlite3.connect(old_path) as connection:
            connection.execute("""CREATE TABLE health_checks (
                health_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                health_status TEXT NOT NULL,
                status_code INTEGER,
                response_time_seconds REAL NOT NULL,
                error TEXT,
                error_type TEXT
            )""")
            connection.execute("""INSERT INTO health_checks
                (service_id, timestamp, health_status, status_code,
                 response_time_seconds, error, error_type)
                VALUES (1, '2026-10-26T10:00:00+00:00', 'HEALTHY', 200, 0.1, NULL, NULL)""")

        legacy_db = DatabaseManager(old_path)
        legacy_db.initialize_database()
        with legacy_db.get_connection() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(health_checks)")}
            old_row = connection.execute("SELECT health_status FROM health_checks").fetchone()

        self.assertTrue({"diagnosis", "likely_cause", "recommendation"}.issubset(columns))
        self.assertEqual(old_row["health_status"], "HEALTHY")

    def test_incident_resolves_after_recovery_and_records_events(self):
        """Persist incident creation and recovery for only the failed target."""
        server_one_checks = {"count": 0}

        def fake_run_once(monitor):
            url = monitor.config.url
            if url == "http://server-1.test/":
                server_one_checks["count"] += 1
                failed = server_one_checks["count"] <= 2
            else:
                failed = False

            result = self.result(
                url,
                503 if failed else 200,
                "http_error" if failed else None,
                "Service Unavailable" if failed else None,
            )
            diagnosis = self.diagnosis(
                "HTTP server error" if failed else "No immediate issue detected"
            )
            return result, diagnosis

        with patch(
            "monitoring.supervisor.HealthMonitor.run_once",
            autospec=True,
            side_effect=fake_run_once,
        ):
            for _ in range(4):
                self.supervisor.run_once()

        server_one_id = self.supervisor.target_states[
            "http://server-1.test/"
        ]["service_id"]
        incidents = self.database.get_incident_history(service_id=server_one_id)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["status"], "RESOLVED")
        self.assertIsNotNone(incidents[0]["resolved_at"])

        events = self.database.get_incident_events(incidents[0]["incident_id"])
        event_types = [event["event_type"] for event in events]
        self.assertEqual(event_types[0], "CREATED")
        self.assertIn("RESOLVED", event_types)

        # The other targets remain incident-free throughout the scenario.
        for url in ("http://server-2.test/", "http://alb.test/"):
            service_id = self.supervisor.target_states[url]["service_id"]
            self.assertEqual(self.database.get_incident_history(service_id=service_id), [])

    def test_duplicate_target_names_are_rejected(self):
        """Result keys must be unique so no target result can be overwritten."""
        with self.assertRaisesRegex(ValueError, "Duplicate target name"):
            SystemSupervisor(
                SupervisorConfig(
                    interval=1,
                    targets=(
                        TargetConfig("Backend", "http://one.test/"),
                        TargetConfig(" backend ", "http://two.test/"),
                    ),
                ),
                database=self.database,
            )


if __name__ == "__main__":
    unittest.main()
