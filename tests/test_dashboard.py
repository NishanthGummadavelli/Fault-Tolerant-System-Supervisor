"""Tests for the read-only dashboard (Stage 1)."""

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dashboard import create_app
from dashboard.config import DashboardConfig
from dashboard.services import (
    DashboardRepository,
    RepositoryError,
    classify_freshness,
    failure_kind,
    redact_url,
    stale_after_seconds,
)
from monitoring.database_manager import DatabaseManager
from monitoring.health_monitor import HealthCheckResult, HealthStatus

NOW = datetime.now(timezone.utc)


def ago(seconds):
    return NOW - timedelta(seconds=seconds)


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "dash.db"
        self.config = DashboardConfig(database_path=self.db_path)
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # Helpers that write through the project's own DatabaseManager.
    def db(self):
        manager = DatabaseManager(self.db_path)
        manager.initialize_database()
        return manager

    def add_service(self, name, url, interval=10):
        return self.db().register_service(
            service_name=name, service_url=url,
            monitoring_interval_seconds=interval,
        )

    def add_check(self, service_id, health, seconds_ago, status=200, rt=0.1,
                  error=None, error_type=None):
        self.db().save_health_check(service_id, HealthCheckResult(
            timestamp=ago(seconds_ago), url="x", health=health,
            status_code=status, response_time_seconds=rt,
            error=error, error_type=error_type,
        ))

    def add_incident(self, incident_id, url, severity="WARNING",
                     status="OPEN", category="ELEVATED_LATENCY"):
        self.db().save_incident(SimpleNamespace(
            incident_id=incident_id, service_url=url, category=category,
            severity=severity, status=status, summary="test incident",
            created_at=ago(60), last_updated_at=ago(30),
            resolved_at=None, occurrences=1,
        ))

    def populate(self):
        """healthy, degraded, unhealthy (fresh), stale, never-checked."""
        h = self.add_service("Alpha", "http://alpha.test/")
        d = self.add_service("Bravo", "http://bravo.test/")
        u = self.add_service("Charlie", "http://charlie.test/")
        s = self.add_service("Delta", "http://delta.test/")
        self.add_service("Echo", "http://echo.test/")
        for i in range(25):  # many history rows must not inflate service count
            self.add_check(h, HealthStatus.HEALTHY, 5 + i)
        self.add_check(d, HealthStatus.DEGRADED, 4, rt=0.7)
        self.add_check(u, HealthStatus.UNHEALTHY, 3, status=503, rt=0.2,
                       error="Service Unavailable", error_type="http_error")
        self.add_check(s, HealthStatus.HEALTHY, 3600)
        return {"h": h, "d": d, "u": u, "s": s}


class TestAppCreation(DashboardTestCase):
    def test_create_app_has_no_side_effects(self):
        self.assertFalse(self.db_path.exists())

    def test_config_from_env_defaults_and_clamping(self):
        cfg = DashboardConfig.from_env({})
        self.assertEqual(cfg.refresh_seconds, 10)
        cfg = DashboardConfig.from_env({
            "DASHBOARD_REFRESH_SECONDS": "abc",
            "DASHBOARD_STALE_MULTIPLIER": "nan",
            "SUPERVISOR_DB_PATH": "/tmp/x.db",
        })
        self.assertEqual(cfg.refresh_seconds, 10)
        self.assertEqual(cfg.stale_multiplier, 3.0)
        self.assertEqual(cfg.database_path, Path("/tmp/x.db"))
        self.assertEqual(
            DashboardConfig.from_env({"DASHBOARD_REFRESH_SECONDS": "0"}).refresh_seconds, 10)
        self.assertEqual(
            DashboardConfig.from_env({"DASHBOARD_REFRESH_SECONDS": "30"}).refresh_seconds, 30)


class TestEmptyStates(DashboardTestCase):
    def test_missing_database_does_not_create_file(self):
        response = self.client.get("/api/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data_status"], "NO_DATABASE")
        self.assertFalse(self.db_path.exists())
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertFalse(self.db_path.exists())

    def test_empty_file_is_not_initialized(self):
        self.db_path.write_bytes(b"")
        data = self.client.get("/api/summary").get_json()
        self.assertEqual(data["data_status"], "NOT_INITIALIZED")

    def test_initialized_database_without_services(self):
        self.db()
        data = self.client.get("/api/summary").get_json()
        self.assertEqual(data["data_status"], "NO_SERVICES")
        self.assertEqual(data["counts"]["total_services"], 0)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"No services", page.data)

    def test_service_without_checks_is_no_data_not_healthy(self):
        self.add_service("Solo", "http://solo.test/")
        data = self.client.get("/api/summary").get_json()
        self.assertEqual(data["data_status"], "NO_CHECKS")
        self.assertEqual(data["counts"]["no_data"], 1)
        self.assertEqual(data["counts"]["healthy"], 0)


class TestPopulated(DashboardTestCase):
    def test_counts_are_per_service_not_per_check(self):
        self.populate()
        c = self.client.get("/api/summary").get_json()["counts"]
        self.assertEqual(c["total_services"], 5)
        self.assertEqual(
            (c["healthy"], c["degraded"], c["unhealthy"], c["stale"], c["no_data"]),
            (1, 1, 1, 1, 1),
        )

    def test_partial_data_status_and_notice(self):
        self.populate()
        data = self.client.get("/api/summary").get_json()
        self.assertEqual(data["data_status"], "PARTIAL")
        self.assertTrue(data["notices"])

    def test_all_stale_reports_stale_not_healthy(self):
        sid = self.add_service("Old", "http://old.test/")
        self.add_check(sid, HealthStatus.HEALTHY, 7200)
        data = self.client.get("/api/summary").get_json()
        self.assertEqual(data["data_status"], "STALE")
        self.assertEqual(data["services"][0]["state"], "STALE")
        self.assertEqual(data["services"][0]["last_known_health"], "HEALTHY")
        self.assertEqual(data["counts"]["healthy"], 0)

    def test_failure_details(self):
        ids = self.populate()
        by_name = {s["name"]: s for s in
                   self.client.get("/api/summary").get_json()["services"]}
        self.assertEqual(by_name["Charlie"]["failure_kind"], "HTTP_ERROR")
        self.assertEqual(by_name["Charlie"]["status_code"], 503)
        self.assertEqual(by_name["Bravo"]["failure_kind"], "SLOW_RESPONSE")
        self.assertIsNone(by_name["Alpha"]["failure_kind"])

    def test_failed_request_has_no_latency_value(self):
        sid = self.add_service("Down", "http://down.test/")
        self.add_check(sid, HealthStatus.UNHEALTHY, 2, status=None, rt=0.002,
                       error="refused", error_type="connection_refused")
        svc = self.client.get("/api/summary").get_json()["services"][0]
        self.assertEqual(svc["failure_kind"], "CONNECTION_FAILURE")
        self.assertIsNone(svc["response_time_seconds"])

    def test_incident_counts_and_unverified_flag(self):
        ids = self.populate()
        self.add_incident("I-1", "http://charlie.test/", severity="CRITICAL")
        self.add_incident("I-2", "http://alpha.test/", severity="WARNING")
        self.add_incident("I-3", "http://delta.test/", severity="CRITICAL")  # stale svc
        self.add_incident("I-4", "http://bravo.test/", status="RESOLVED")
        data = self.client.get("/api/summary").get_json()
        c = data["counts"]
        self.assertEqual(c["open_incidents"], 3)
        self.assertEqual(c["critical_incidents"], 2)
        self.assertEqual(c["open_incidents_unverified"], 1)
        self.assertTrue(any("left over" in n for n in data["notices"]))

    def test_html_and_json_agree(self):
        self.populate()
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for name in (b"Alpha", b"Bravo", b"Charlie"):
            self.assertIn(name, page.data)

    def test_service_name_is_escaped(self):
        sid = self.add_service("<script>alert(1)</script>", "http://x.test/")
        self.add_check(sid, HealthStatus.HEALTHY, 2)
        page = self.client.get("/")
        self.assertNotIn(b"<script>alert(1)</script>", page.data)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", page.data)


class TestApiBehaviour(DashboardTestCase):
    def test_json_headers_and_shape(self):
        self.populate()
        response = self.client.get("/api/summary")
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            {"generated_at", "data_status", "latest_check_at",
             "counts", "services", "notices"},
            set(response.get_json()),
        )

    def test_corrupt_database_returns_503_without_leaking_details(self):
        self.db_path.write_bytes(b"this is not a sqlite database" * 50)
        api = self.client.get("/api/summary")
        self.assertEqual(api.status_code, 503)
        self.assertNotIn(str(self.tmp.name), api.get_data(as_text=True))
        page = self.client.get("/")
        self.assertEqual(page.status_code, 503)
        self.assertNotIn(self.tmp.name.encode(), page.data)

    def test_unknown_routes(self):
        api = self.client.get("/api/nope")
        self.assertEqual(api.status_code, 404)
        self.assertEqual(api.mimetype, "application/json")
        page = self.client.get("/nope")
        self.assertEqual(page.status_code, 404)
        self.assertIn(b"Not found", page.data)

    def test_read_only_methods(self):
        for method in ("post", "put", "delete"):
            for path in ("/", "/api/summary"):
                self.assertEqual(getattr(self.client, method)(path).status_code, 405)


class TestReadOnlyAndSafety(DashboardTestCase):
    def test_repository_connection_cannot_write(self):
        self.populate()
        repo = DashboardRepository(self.config)
        with repo._connect() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM health_checks")
        self.assertEqual(
            len(DatabaseManager(self.db_path).get_health_check_history(1, 100)), 25)

    def test_repository_error_on_bad_database(self):
        self.db_path.write_bytes(b"garbage" * 100)
        with self.assertRaises(RepositoryError):
            DashboardRepository(self.config).get_summary()

    def test_web_requests_never_start_monitoring(self):
        self.populate()
        threads_before = threading.active_count()
        with patch("monitoring.supervisor.SystemSupervisor.run_forever") as forever, \
             patch("monitoring.supervisor.SystemSupervisor.run_once") as once, \
             patch("monitoring.health_monitor.check_server") as check, \
             patch("monitoring.health_monitor.HealthMonitor.run_once") as monitor_once:
            for _ in range(5):
                self.client.get("/")
                self.client.get("/api/summary")
        for mocked in (forever, once, check, monitor_once):
            mocked.assert_not_called()
        self.assertEqual(threading.active_count(), threads_before)

    def test_dashboard_source_does_not_import_the_supervisor(self):
        root = Path(__file__).resolve().parent.parent / "dashboard"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("monitoring.supervisor", text, path.name)
            self.assertNotIn("SystemSupervisor", text, path.name)


class TestPureHelpers(unittest.TestCase):
    def test_redact_url(self):
        self.assertEqual(
            redact_url("http://user:pw@example.com:8080/path?token=abc#frag"),
            "http://example.com:8080/path")
        self.assertEqual(redact_url("not a url"), "[unparseable URL]")
        self.assertEqual(redact_url("http://[::1]:5000/x"), "http://[::1]:5000/x")

    def test_stale_window_and_boundary(self):
        limit = stale_after_seconds(10, 3, 3.0)
        self.assertEqual(limit, 33)
        row = lambda s: {"timestamp": (NOW - timedelta(seconds=s)).isoformat(),
                         "health_status": "HEALTHY"}
        self.assertEqual(classify_freshness(row(32), limit, NOW)[0], "HEALTHY")
        self.assertEqual(classify_freshness(row(34), limit, NOW)[0], "STALE")
        self.assertEqual(classify_freshness(None, limit, NOW)[0], "NO_DATA")
        bad = {"timestamp": "garbage", "health_status": "HEALTHY"}
        self.assertEqual(classify_freshness(bad, limit, NOW)[0], "STALE")

    def test_failure_kind(self):
        self.assertIsNone(failure_kind(200, "HEALTHY"))
        self.assertEqual(failure_kind(None, "UNHEALTHY"), "CONNECTION_FAILURE")
        self.assertEqual(failure_kind(500, "UNHEALTHY"), "HTTP_ERROR")
        self.assertEqual(failure_kind(200, "DEGRADED"), "SLOW_RESPONSE")


if __name__ == "__main__":
    unittest.main()
