"""Tests for infrastructure roles, application status, diagnosis, registration,
supervisor pickup of dashboard-registered services, and database compatibility."""

import collections
import re
import socket
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from dashboard import create_app, insights, registration
from dashboard.config import DashboardConfig, parse_roles
from monitoring.database_manager import DatabaseManager
from monitoring.health_monitor import (
    DiagnosisResult, HealthCheckResult, HealthMonitor, HealthStatus,
    MonitoringConfig, check_server, diagnose_health)
from monitoring.supervisor import SupervisorConfig, SystemSupervisor, TargetConfig

ALB, B1, B2 = "Application Load Balancer", "Backend Server 1", "Backend Server 2"
URLS = {B1: "http://b1.test/", B2: "http://b2.test/", ALB: "http://alb.test/"}


def ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "infra.db"
        self.make_app()

    def make_app(self, **overrides):
        self.config = DashboardConfig(database_path=self.path, **overrides)
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def db(self):
        manager = DatabaseManager(self.path)
        manager.initialize_database()
        return manager

    def add(self, name, url=None, interval=10):
        return self.db().register_service(
            name, url or URLS.get(name, f"http://{name.replace(' ', '-').lower()}.test/"),
            monitoring_interval_seconds=interval)

    def check(self, sid, health, seconds_ago=2, status=200, rt=0.1, error=None,
              error_type=None, diagnosis=None):
        result = HealthCheckResult(
            timestamp=ago(seconds_ago), url="x", health=HealthStatus(health),
            status_code=status, response_time_seconds=rt, error=error, error_type=error_type)
        self.db().save_health_check(sid, result, diagnosis=diagnosis)

    def infrastructure(self, alb="HEALTHY", b1="HEALTHY", b2="HEALTHY"):
        ids = {}
        for name, health in ((ALB, alb), (B1, b1), (B2, b2)):
            ids[name] = self.add(name)
            if health is None:
                continue
            bad = health == "UNHEALTHY"
            self.check(ids[name], health, status=None if bad else 200,
                       error="refused" if bad else None,
                       error_type="connection_refused" if bad else None)
        return ids

    def api(self, path):
        return self.client.get(path).get_json()


class TestApplicationStatus(Base):
    def svc(self, name, role, state):
        return {"name": name, "role": role, "state": state}

    def status(self, alb, b1, b2):
        services = []
        if alb: services.append(self.svc(ALB, "load_balancer", alb))
        if b1: services.append(self.svc(B1, "backend", b1))
        if b2: services.append(self.svc(B2, "backend", b2))
        return insights.application_status(services)["code"]

    def test_decision_table(self):
        H, D, U, S, N = "HEALTHY", "DEGRADED", "UNHEALTHY", "STALE", "NO_DATA"
        cases = [
            ((H, H, H), "OPERATIONAL"),
            ((H, U, H), "DEGRADED_REDUNDANCY"),     # one backend down, ALB up
            ((H, U, U), "OUTAGE"),                  # both backends down; ALB 200 does not hide it
            ((U, U, U), "OUTAGE"),
            ((U, H, H), "ENDPOINT_DOWN"),           # ALB unreachable, backends fine
            ((H, D, H), "DEGRADED_PERFORMANCE"),
            ((H, H, S), "PARTIAL_UNKNOWN"),
            ((H, U, S), "AT_RISK"),
            ((S, H, H), "ENDPOINT_UNVERIFIED"),
            ((S, S, S), "STALE"),
            ((N, N, N), "UNKNOWN"),
        ]
        for (alb, b1, b2), expected in cases:
            with self.subTest(alb=alb, b1=b1, b2=b2):
                self.assertEqual(self.status(alb, b1, b2), expected)

    def test_without_alb_never_claims_end_to_end(self):
        self.assertEqual(self.status(None, "HEALTHY", "HEALTHY"), "BACKENDS_HEALTHY")

    def test_no_roles_and_no_services(self):
        self.assertEqual(insights.application_status([])["code"], "UNKNOWN")
        other = [{"name": "Blog", "role": "other", "state": "HEALTHY"}]
        self.assertEqual(insights.application_status(other)["code"], "NO_ROLES")

    def test_roles_inferred_and_configured(self):
        self.assertEqual(insights.resolve_role(ALB, ())[0], "load_balancer")
        self.assertEqual(insights.resolve_role(B1, ())[0], "backend")
        self.assertEqual(insights.resolve_role("Billing API", ())[0], "other")
        configured = parse_roles("Billing API=backend; Edge=alb;bad entry;X=wizard")
        self.assertEqual(insights.resolve_role("billing api", configured), ("backend", "configured"))
        self.assertEqual(insights.resolve_role("Edge", configured)[0], "load_balancer")
        self.assertEqual(len(configured), 2)    # malformed and unknown roles ignored

    def test_overview_banner_for_outage_and_degraded(self):
        self.infrastructure(alb="HEALTHY", b1="UNHEALTHY", b2="UNHEALTHY")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Application outage", html)
        self.assertIn("status-banner--bad", html)
        self.assertEqual(self.api("/api/application")["status"]["code"], "OUTAGE")

    def test_degraded_redundancy_page(self):
        self.infrastructure(b2="UNHEALTHY")
        status = self.api("/api/application")["status"]
        self.assertEqual(status["code"], "DEGRADED_REDUNDANCY")
        self.assertIn("may remain available", status["detail"])

    def test_stale_data_is_never_shown_operational(self):
        ids = self.infrastructure()
        with sqlite3.connect(self.path) as conn:       # age every check by an hour
            conn.execute("UPDATE health_checks SET timestamp = ?", (ago(3600),))
        status = self.api("/api/application")["status"]
        self.assertEqual(status["code"], "STALE")
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Application operational", html)
        self.assertIn("Stale", html)

    def test_configured_roles_override_names(self):
        self.make_app(service_roles=parse_roles("Backend Server 1=other"))
        self.infrastructure()
        roles = {s["name"]: s["role"] for s in self.api("/api/summary")["services"]}
        self.assertEqual(roles[B1], "other")
        self.assertEqual(roles[B2], "backend")


class TestInfrastructureOverview(Base):
    def test_three_targets_and_extra_service_shown_without_ui_changes(self):
        self.infrastructure()
        extra = self.add("Billing API", "http://billing.test/")
        self.check(extra, "HEALTHY")
        html = self.client.get("/").get_data(as_text=True)
        for name in (ALB, B1, B2, "Billing API"):
            self.assertIn(name, html)
        self.assertEqual(html.count('class="card infra-card'), 4)
        self.assertLess(html.index("Load balancer"), html.index("Backend server"))  # ALB card first

    def test_card_contents_and_metrics_from_real_records(self):
        ids = self.infrastructure(b1="UNHEALTHY")
        self.check(ids[B1], "UNHEALTHY", 1, status=503, error="Service Unavailable",
                   error_type="http_error", diagnosis=DiagnosisResult(
                       "HTTP server error", "The server reported an internal error.", "Inspect logs."))
        summary = self.api("/api/summary")
        counts = summary["counts"]
        self.assertEqual((counts["total_services"], counts["healthy"], counts["unhealthy"]), (3, 2, 1))
        card = next(s for s in summary["services"] if s["name"] == B1)
        self.assertEqual((card["status_code"], card["failure_type"]), (503, "HTTP_5XX"))
        self.assertEqual(card["diagnosis"], "HTTP server error")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("HTTP server error", html)
        self.assertIn("inferred", html)

    def test_availability_formula_uses_alb_checks_in_24h(self):
        ids = self.infrastructure()
        for _ in range(3):
            self.check(ids[ALB], "HEALTHY", 5)
        self.check(ids[ALB], "UNHEALTHY", 4, status=503, error_type="http_error")
        self.check(ids[ALB], "HEALTHY", 90000)            # outside the 24 h window
        app = self.api("/api/application")
        self.assertEqual(app["availability"]["checks"], 5)       # 1 initial + 3 + 1 failure
        self.assertEqual(app["availability"]["value"], 80.0)     # 4 of 5 returned 2xx
        self.assertEqual(app["availability"]["basis"], ALB)
        self.assertIn("2xx", app["availability"]["formula"])

    def test_no_alb_means_no_availability_number(self):
        sid = self.add(B1); self.check(sid, "HEALTHY")
        app = self.api("/api/application")
        self.assertIsNone(app["availability"]["value"])
        self.assertIn("—", self.client.get("/").get_data(as_text=True))

    def test_average_response_ignores_stale_and_failed(self):
        ids = self.infrastructure()
        sid = self.add("Old", "http://old.test/"); self.check(sid, "HEALTHY", 5000, rt=9.0)
        down = self.add("Down", "http://down.test/"); self.check(down, "UNHEALTHY", 2, status=None, rt=3.0, error_type="timeout")
        app = self.api("/api/application")
        self.assertEqual(app["response"]["samples"], 3)
        self.assertEqual(app["response"]["average_seconds"], 0.1)

    def test_unknown_and_stale_states_on_cards(self):
        self.infrastructure(b2=None)           # server 2 never checked
        stale = self.add("Stale Service", "http://stale.test/"); self.check(stale, "HEALTHY", 7000)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Never checked", html)
        self.assertIn("Stale", html)
        states = {s["name"]: s["state"] for s in self.api("/api/summary")["services"]}
        self.assertEqual((states[B2], states["Stale Service"]), ("NO_DATA", "STALE"))

    def test_empty_database_application_status(self):
        app = self.api("/api/application")
        self.assertEqual(app["status"]["code"], "UNKNOWN")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertFalse(self.path.exists())


class TestDiagnosis(Base):
    def test_failure_type_classification(self):
        cases = [
            ("timeout", None, "TIMEOUT"), ("connection_refused", None, "CONNECTION_REFUSED"),
            ("dns_failure", None, "DNS_FAILURE"), ("connection_error", None, "NETWORK_ERROR"),
            ("http_error", 404, "HTTP_4XX"), ("http_error", 503, "HTTP_5XX"),
            (None, 302, "UNEXPECTED_STATUS"), (None, 200, "SLOW_RESPONSE"), (None, None, "UNKNOWN")]
        for error_type, code, expected in cases:
            with self.subTest(error_type=error_type, code=code):
                self.assertEqual(insights.failure_type(code, "UNHEALTHY", error_type), expected)
        self.assertIsNone(insights.failure_type(200, "HEALTHY", None))

    def test_stored_diagnosis_is_preferred_and_labelled(self):
        sid = self.add(B1)
        self.check(sid, "UNHEALTHY", 3, status=None, error_type="timeout", error="timed out",
                   diagnosis=DiagnosisResult("Stored verdict X", "Stored cause", "Stored advice"))
        html = self.client.get("/diagnosis").get_data(as_text=True)
        self.assertIn("Stored verdict X", html)
        self.assertIn("recorded by the supervisor at check time", html)
        self.assertIn("inferred, not confirmed", html)
        self.assertIn("from stored record", html)

    def test_old_records_fall_back_to_recomputed_diagnosis(self):
        sid = self.add(B1)
        self.check(sid, "UNHEALTHY", 3, status=None, error_type="connection_refused", error="refused")
        html = self.client.get(f"/services/{sid}").get_data(as_text=True)
        self.assertIn("Connection refused", html)
        self.assertIn("recomputed from the stored check", html)

    def test_incident_pages_show_failure_and_events(self):
        sid = self.add(B1)
        for i in range(3):
            self.check(sid, "UNHEALTHY", 20 - i, status=None, error_type="timeout", error="timed out",
                       diagnosis=DiagnosisResult("Request timeout", "No response in time", "Check server"))
        self.db().save_incident(SimpleNamespace(
            incident_id="INC-T", service_url=URLS[B1], category="SERVICE_UNAVAILABLE",
            severity="CRITICAL", status="OPEN", summary="down",
            created_at=datetime.now(timezone.utc) - timedelta(seconds=15),
            last_updated_at=datetime.now(timezone.utc), resolved_at=None, occurrences=2))
        listing = self.client.get("/incidents").get_data(as_text=True)
        self.assertIn("Request timeout", listing)
        detail = self.api("/api/incidents/INC-T")
        self.assertEqual(detail["latest_failure"]["failure_label"], "Request timeout")
        self.assertEqual(detail["incident"]["event_count"], 1)
        self.assertEqual(self.client.get("/incidents?range=1h").status_code, 200)
        self.assertEqual(self.client.get("/incidents?range=zzz").status_code, 400)


class TestDnsClassification(unittest.TestCase):
    def config(self):
        return MonitoringConfig(url="http://nonexistent.invalid/", timeout=1.0, interval=10.0,
                                healthy_threshold=0.5, unhealthy_threshold=1.0)

    def test_dns_error_is_its_own_type_and_diagnosis(self):
        error = URLError(socket.gaierror(-2, "Name or service not known"))
        with patch("monitoring.health_monitor.urlopen", side_effect=error):
            result = check_server(self.config())
        self.assertEqual(result.error_type, "dns_failure")
        verdict = diagnose_health(result, self.config())
        self.assertEqual(verdict.diagnosis, "DNS resolution failure")

    def test_other_connection_errors_unchanged(self):
        for reason, expected in ((ConnectionRefusedError(), "connection_refused"),
                                 (OSError("unreachable"), "connection_error")):
            with patch("monitoring.health_monitor.urlopen", side_effect=URLError(reason)):
                self.assertEqual(check_server(self.config()).error_type, expected)


class FakeMonitors:
    """Drive HealthMonitor.run_once per URL: states are 'ok', 'down' or 'refused'."""

    def __init__(self):
        self.state = collections.defaultdict(lambda: "ok")

    def __call__(self, monitor):
        url, mode = monitor.config.url, self.state[monitor.config.url]
        healthy = mode == "ok"
        result = HealthCheckResult(
            timestamp=datetime.now(timezone.utc).isoformat(), url=url,
            health=HealthStatus.HEALTHY if healthy else HealthStatus.UNHEALTHY,
            status_code=200 if healthy else (503 if mode == "down" else None),
            response_time_seconds=0.05,
            error=None if healthy else "failure",
            error_type=None if healthy else ("http_error" if mode == "down" else "connection_refused"))
        return result, diagnose_health(result, monitor.config)


class TestSupervisorScenarios(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeMonitors()
        patcher = patch.object(HealthMonitor, "run_once", autospec=True, side_effect=self.fake)
        patcher.start(); self.addCleanup(patcher.stop)
        self.supervisor = SystemSupervisor(
            SupervisorConfig(interval=1, targets=tuple(TargetConfig(n, u) for n, u in URLS.items())),
            database=self.db())

    def cycles(self, n):
        for _ in range(n):
            self.supervisor.run_once()

    def test_individual_then_simultaneous_failure_then_recovery(self):
        self.cycles(3)
        self.assertEqual(self.api("/api/application")["status"]["code"], "OPERATIONAL")

        self.fake.state[URLS[B1]] = "refused"
        self.cycles(4)
        app = self.api("/api/application")["status"]
        self.assertEqual(app["code"], "DEGRADED_REDUNDANCY")
        incidents = self.api("/api/incidents")["items"]
        self.assertEqual([i["service_name"] for i in incidents], [B1])

        self.fake.state[URLS[B2]] = "down"             # now both backends fail
        self.cycles(5)
        app = self.api("/api/application")["status"]
        self.assertEqual(app["code"], "OUTAGE")
        incidents = self.api("/api/incidents")["items"]
        self.assertEqual(sorted(i["service_name"] for i in incidents), [B1, B2])
        # One incident per failing backend, not one per polling cycle.
        self.assertEqual(len(incidents), 2)
        self.assertTrue(all(i["status"] == "OPEN" for i in incidents))
        banner = self.client.get("/").get_data(as_text=True)
        self.assertIn("Application outage", banner)
        self.assertIn("Connection refused", banner)

        for url in (URLS[B1], URLS[B2]):
            self.fake.state[url] = "ok"
        self.cycles(3)
        self.assertEqual(self.api("/api/application")["status"]["code"], "OPERATIONAL")
        incidents = self.api("/api/incidents")["items"]
        self.assertTrue(all(i["status"] == "RESOLVED" for i in incidents))
        detail = self.api("/api/incidents/" + incidents[0]["incident_id"])
        types = [e["event_type"] for e in detail["events"]]
        self.assertEqual((types[0], types[-1]), ("CREATED", "RESOLVED"))
        recovered = [t for t in self.api("/api/transitions?range=1h") if t["kind"] == "RECOVERED"]
        self.assertGreaterEqual(len(recovered), 2)
        self.assertIn("Recovered", self.client.get("/").get_data(as_text=True))

    def test_availability_chart_uses_real_counts(self):
        self.fake.state[URLS[B1]] = "refused"
        self.cycles(4)
        items = {i["label"].split(" (")[0]: i["value"] for i in self.api("/api/charts/availability?range=1h")["items"]}
        self.assertEqual(items[B1], 0.0)
        self.assertEqual(items[B2], 100.0)


class TestRegistration(Base):
    TOKEN = "unit-test-token"

    def enable(self):
        self.make_app(allow_registration=True, admin_token=self.TOKEN)

    def csrf(self):
        html = self.client.get("/services").get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return match.group(1) if match else None

    def post(self, name, url, token=None, csrf="auto"):
        csrf = self.csrf() if csrf == "auto" else csrf
        return self.client.post("/services/add", data={
            "name": name, "url": url, "admin_token": token or self.TOKEN, "csrf_token": csrf or ""})

    def test_disabled_by_default(self):
        self.assertEqual(self.client.post("/services/add", data={"name": "x", "url": "http://x/"}).status_code, 403)
        html = self.client.get("/services").get_data(as_text=True)
        self.assertNotIn("Register service", html)
        self.assertIn("disabled", html)

    def test_allow_flag_without_token_stays_disabled(self):
        self.make_app(allow_registration=True, admin_token=None)
        self.assertFalse(self.config.registration_enabled)
        self.assertEqual(self.client.post("/services/add", data={}).status_code, 403)

    def test_env_configuration(self):
        cfg = DashboardConfig.from_env({"DASHBOARD_ALLOW_REGISTRATION": "1", "DASHBOARD_ADMIN_TOKEN": " s3 "})
        self.assertTrue(cfg.registration_enabled)
        self.assertNotIn("s3", repr(cfg))                  # token never leaks via repr
        self.assertFalse(DashboardConfig.from_env({"DASHBOARD_ALLOW_REGISTRATION": "1"}).registration_enabled)

    def test_successful_registration_is_recorded_but_not_claimed_as_monitored(self):
        self.enable()
        response = self.post("Billing API", "http://billing.test:8080/health")
        self.assertEqual(response.status_code, 303)
        self.assertIn("registered=", response.headers["Location"])
        row = DatabaseManager(self.path).list_services()[0]
        self.assertEqual((row["service_name"], row["monitoring_source"]), ("Billing API", "dashboard"))
        page = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("does <em>not</em> mean it is being monitored", page)
        self.assertIn("awaiting first check", page)
        svc = self.api("/api/summary")["services"][0]
        self.assertEqual((svc["state"], svc["awaiting_first_check"]), ("NO_DATA", True))

    def test_invalid_input_is_rejected_without_writing(self):
        self.enable()
        bad = [("", "http://ok.test/"), ("Name", ""), ("<script>x</script>", "http://ok.test/"),
               ("Name", "ftp://ok.test/"), ("Name", "javascript:alert(1)"), ("Name", "http://u:p@ok.test/"),
               ("Name", "http://ok.test:0/"), ("Name", "http://ok test/"), ("Name", "http:///nohost"),
               ("Name", "http://169.254.169.254/latest/meta-data/"), ("Name", "http://0.0.0.0/"),
               ("Name", "http://ok.test/#frag"), ("x" * 61, "http://ok.test/"),
               ("Name", "http://" + "a" * 2100 + ".test/")]
        for name, url in bad:
            with self.subTest(name=name[:20], url=url[:30]):
                response = self.post(name, url)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(self.path.exists())           # rejected input never touches the database

    def test_error_messages_are_escaped(self):
        self.enable()
        html = self.post("<b>bad</b>", "http://ok.test/").get_data(as_text=True)
        self.assertNotIn("<b>bad</b>", html)

    def test_duplicates_rejected_by_name_and_normalized_url(self):
        self.enable()
        self.assertEqual(self.post("Billing API", "http://Billing.test/").status_code, 303)
        self.assertEqual(self.post("billing api", "http://other.test/").status_code, 409)
        self.assertEqual(self.post("Other", "HTTP://billing.TEST:80").status_code, 409)
        self.assertEqual(len(DatabaseManager(self.path).list_services()), 1)

    def test_service_limit(self):
        self.make_app(allow_registration=True, admin_token=self.TOKEN, max_services=1)
        self.assertEqual(self.post("One", "http://one.test/").status_code, 303)
        self.assertEqual(self.post("Two", "http://two.test/").status_code, 409)

    def test_csrf_and_admin_token_enforced(self):
        self.enable()
        self.csrf()
        self.assertEqual(self.post("A", "http://a.test/", csrf="").status_code, 403)
        self.assertEqual(self.post("A", "http://a.test/", csrf="forged").status_code, 403)
        wrong = self.post("A", "http://a.test/", token="wrong")
        self.assertEqual(wrong.status_code, 403)
        self.assertIn("admin token is incorrect", wrong.get_data(as_text=True))
        self.assertFalse(self.path.exists())
        self.assertNotIn(self.TOKEN, wrong.get_data(as_text=True))

    def test_csrf_cookie_flags(self):
        self.enable()
        cookie = self.client.get("/services").headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_new_settings_reuse_existing_service_settings(self):
        self.enable()
        self.add("Seed", "http://seed.test/", interval=5)
        self.post("Billing API", "http://billing.test/")
        rows = {r["service_name"]: r for r in DatabaseManager(self.path).list_services()}
        self.assertEqual(rows["Billing API"]["monitoring_interval_seconds"], 5)

    def test_supervisor_picks_up_registered_service_and_only_those(self):
        self.enable()
        old = self.add("Leftover", "http://leftover.test/")      # source 'supervisor' (an earlier run)
        self.post("Billing API", "http://billing.test/")
        fake = FakeMonitors(); fake.state["http://billing.test/"] = "ok"; fake.state["http://base.test/"] = "ok"
        with patch.object(HealthMonitor, "run_once", autospec=True, side_effect=fake):
            supervisor = SystemSupervisor(
                SupervisorConfig(interval=1, targets=(TargetConfig("Base", "http://base.test/"),)),
                database=DatabaseManager(self.path))
            self.assertNotIn("http://billing.test/", supervisor.target_states)
            supervisor.run_once()                       # sync happens at the start of the cycle
            self.assertIn("http://billing.test/", supervisor.target_states)
            self.assertNotIn("http://leftover.test/", supervisor.target_states)
            supervisor.run_once()
        states = {s["name"]: s["state"] for s in self.api("/api/summary")["services"]}
        self.assertEqual(states["Billing API"], "HEALTHY")
        self.assertEqual(states["Leftover"], "NO_DATA")
        self.assertEqual(len(DatabaseManager(self.path).get_health_check_history(
            [s for s in self.api("/api/summary")["services"] if s["name"] == "Billing API"][0]["service_id"], limit=10)), 2)

    def test_supervisor_skips_invalid_registered_row_once(self):
        manager = self.db()
        manager.register_service("Weird", "ftp://weird.test/", monitoring_source="dashboard")
        with patch.object(HealthMonitor, "run_once", autospec=True, side_effect=FakeMonitors()):
            supervisor = SystemSupervisor(
                SupervisorConfig(interval=1, targets=(TargetConfig("Base", "http://base.test/"),)),
                database=manager)
            with self.assertLogs(level="WARNING") as logs:
                supervisor.run_once()
            supervisor.run_once()
        self.assertEqual(sum("Skipping registered service" in m for m in logs.output), 1)
        self.assertNotIn("ftp://weird.test/", supervisor.target_states)

    def test_registration_module_helpers(self):
        self.assertTrue(registration.tokens_match("a", "a"))
        self.assertFalse(registration.tokens_match("a", ""))
        self.assertFalse(registration.tokens_match(None, "a"))
        self.assertEqual(registration.normalize_url("http://Host:80"), "http://host/")


class TestDatabaseCompatibility(Base):
    def make_legacy_database(self):
        """Create the pre-diagnosis, pre-source schema with real rows."""
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE services (service_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_name TEXT NOT NULL, service_url TEXT NOT NULL UNIQUE,
                    timeout_seconds REAL NOT NULL DEFAULT 3.0,
                    monitoring_interval_seconds INTEGER NOT NULL DEFAULT 10,
                    healthy_threshold_seconds REAL NOT NULL DEFAULT 0.5,
                    unhealthy_threshold_seconds REAL NOT NULL DEFAULT 1.0,
                    is_enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE health_checks (health_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_id INTEGER NOT NULL, timestamp TEXT NOT NULL, health_status TEXT NOT NULL,
                    status_code INTEGER, response_time_seconds REAL NOT NULL, error TEXT, error_type TEXT);
                CREATE TABLE incidents (incident_id TEXT PRIMARY KEY, service_id INTEGER NOT NULL,
                    category TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL,
                    created_at TEXT NOT NULL, last_updated_at TEXT NOT NULL, resolved_at TEXT,
                    occurrences INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE incident_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, description TEXT, previous_severity TEXT, new_severity TEXT,
                    previous_status TEXT, new_status TEXT, timestamp TEXT NOT NULL);
            """)
            now = ago(0)
            conn.execute("INSERT INTO services (service_name, service_url, created_at, updated_at) VALUES (?,?,?,?)",
                         (B1, URLS[B1], now, now))
            conn.execute("INSERT INTO health_checks (service_id, timestamp, health_status, status_code, response_time_seconds, error, error_type) VALUES (1,?,?,?,?,?,?)",
                         (ago(2), "UNHEALTHY", None, 0.01, "refused", "connection_refused"))
            conn.execute("INSERT INTO incidents VALUES ('OLD-1',1,'SERVICE_UNAVAILABLE','CRITICAL','OPEN','down',?,?,NULL,2)", (ago(30), ago(5)))
            conn.execute("INSERT INTO incident_events (incident_id,event_type,description,timestamp) VALUES ('OLD-1','CREATED','opened',?)", (ago(30),))

    def test_dashboard_reads_legacy_database_without_modifying_it(self):
        self.make_legacy_database()
        before = self.path.read_bytes()
        for path in ("/", "/services", "/health", "/incidents", "/incidents/OLD-1", "/diagnosis", "/events", "/system", "/services/1"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.path.read_bytes(), before)
        svc = self.api("/api/summary")["services"][0]
        self.assertEqual((svc["state"], svc["failure_type"], svc["monitoring_source"]),
                         ("UNHEALTHY", "CONNECTION_REFUSED", "supervisor"))
        self.assertIn("recomputed from the stored check", self.client.get("/diagnosis").get_data(as_text=True))

    def test_migration_adds_columns_and_preserves_records(self):
        self.make_legacy_database()
        DatabaseManager(self.path).initialize_database()
        DatabaseManager(self.path).initialize_database()          # idempotent
        with sqlite3.connect(self.path) as conn:
            service_cols = {r[1] for r in conn.execute("PRAGMA table_info(services)")}
            check_cols = {r[1] for r in conn.execute("PRAGMA table_info(health_checks)")}
            self.assertIn("monitoring_source", service_cols)
            self.assertTrue({"diagnosis", "likely_cause", "recommendation"} <= check_cols)
            self.assertEqual(conn.execute("SELECT service_name, monitoring_source FROM services").fetchall(),
                             [(B1, "supervisor")])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM health_checks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM incident_events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT occurrences FROM incidents").fetchone()[0], 2)
        self.assertEqual(self.api("/api/incidents/OLD-1")["incident"]["occurrences"], 2)

    def test_registration_works_after_migrating_a_legacy_database(self):
        self.make_legacy_database()
        self.make_app(allow_registration=True, admin_token="t")
        html = self.client.get("/services").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        response = self.client.post("/services/add", data={"name": "Extra", "url": "http://extra.test/",
                                                           "admin_token": "t", "csrf_token": token})
        self.assertEqual(response.status_code, 303)
        names = {r["service_name"] for r in DatabaseManager(self.path).list_services()}
        self.assertEqual(names, {B1, "Extra"})


class TestApiErrors(Base):
    def test_new_endpoint_validation_and_missing_data(self):
        for path in ("/api/transitions?range=bogus", "/api/transitions?service_id=x",
                     "/api/charts/availability?range=30d", "/api/charts/availability?range=zzz"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"], "Invalid request")
        for path in ("/api/application", "/api/transitions", "/api/charts/availability"):
            self.assertEqual(self.client.get(path).status_code, 200)       # missing database
        self.assertEqual(self.client.get("/settings").status_code, 301)
        self.assertEqual(self.client.get("/api/incidents/NOPE").status_code, 404)

    def test_corrupt_database_returns_503_on_new_endpoints(self):
        self.path.write_bytes(b"garbage" * 200)
        for path in ("/api/application", "/api/transitions", "/api/charts/availability", "/system"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 503, path)
            self.assertNotIn(self.tmp.name, response.get_data(as_text=True))

    def test_settings_page_never_shows_the_admin_token(self):
        self.make_app(allow_registration=True, admin_token="super-secret-token-123")
        html = self.client.get("/system").get_data(as_text=True)
        self.assertNotIn("super-secret-token-123", html)
        self.assertIn("enabled", html)

    def test_transitions_and_availability_empty_states(self):
        self.db()
        self.assertEqual(self.api("/api/transitions"), [])
        self.assertEqual(self.api("/api/charts/availability")["items"], [])
        self.assertIn("No health-state changes", self.client.get("/health").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
