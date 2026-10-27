"""Tests for the complete dashboard: pages, API, filters, incidents, charts."""

import json
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dashboard import insights
from dashboard.config import DashboardConfig
from monitoring.health_monitor import (
    HealthCheckResult, HealthStatus, MonitoringConfig, diagnose_health)
from tests.test_dashboard import DashboardTestCase, ago

PAGES = ["/", "/services", "/health", "/incidents", "/diagnosis", "/events", "/system"]
API = ["/api/summary", "/api/services", "/api/health-checks", "/api/incidents",
       "/api/charts/response-times", "/api/charts/history", "/api/charts/incidents"]


class PagesCase(DashboardTestCase):
    def make_scenario(self):
        """Service with healthy, slow and failed checks plus two incidents."""
        sid = self.add_service("Web", "http://web.test/")
        for i in range(12):
            self.add_check(sid, HealthStatus.HEALTHY, 600 - i * 10, rt=0.1)
        self.add_check(sid, HealthStatus.DEGRADED, 400, rt=0.7)
        self.add_check(sid, HealthStatus.UNHEALTHY, 300, status=503, rt=0.2,
                       error="Service Unavailable", error_type="http_error")
        self.add_check(sid, HealthStatus.UNHEALTHY, 200, status=None, rt=0.003,
                       error="refused", error_type="connection_refused")
        self.add_check(sid, HealthStatus.HEALTHY, 5, rt=0.1)
        self.add_incident("INC-A", "http://web.test/", severity="CRITICAL",
                          status="OPEN", category="SERVICE_UNAVAILABLE")
        self.add_incident("INC-B", "http://web.test/", severity="WARNING",
                          status="RESOLVED")
        return sid


class TestPagesRender(PagesCase):
    def test_all_pages_with_no_database(self):
        for path in PAGES + API:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertFalse(self.db_path.exists())

    def test_all_pages_with_empty_database(self):
        self.db()
        for path in PAGES + API:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_all_pages_populated(self):
        sid = self.make_scenario()
        for path in PAGES + API + [f"/services/{sid}", "/incidents/INC-A", "/api/incidents/INC-A",
                                   f"/api/services/{sid}"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_no_inline_script_or_style_and_security_headers(self):
        self.make_scenario()
        for path in PAGES:
            response = self.client.get(path)
            html = response.get_data(as_text=True)
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
            self.assertNotIn(' style="', html)
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_partial_response_has_no_page_shell(self):
        self.make_scenario()
        full = self.client.get("/services").get_data(as_text=True)
        part = self.client.get("/services?_partial=1")
        self.assertIn("<html", full)
        self.assertNotIn("<html", part.get_data(as_text=True))
        self.assertIn("Web", part.get_data(as_text=True))
        self.assertEqual(part.headers["Cache-Control"], "no-store")

    def test_pages_are_read_only(self):
        sid = self.make_scenario()
        for path in PAGES + [f"/services/{sid}", "/incidents/INC-A"] + API:
            self.assertEqual(self.client.post(path).status_code, 405, path)

    def test_corrupt_database_gives_503_everywhere_without_leaking(self):
        self.db_path.write_bytes(b"not sqlite" * 100)
        for path in PAGES + API:
            response = self.client.get(path)
            with self.subTest(path=path):
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(self.tmp.name, response.get_data(as_text=True))
                self.assertNotIn("Traceback", response.get_data(as_text=True))


class TestHealthHistory(PagesCase):
    def test_filters(self):
        self.make_scenario()
        get = lambda q: self.client.get("/api/health-checks?range=all&" + q).get_json()
        self.assertEqual(get("")["total"], 16)
        self.assertEqual(get("state=UNHEALTHY")["total"], 2)
        self.assertEqual(get("category=CONNECTION_FAILURE")["total"], 1)
        self.assertEqual(get("category=HTTP_ERROR")["total"], 1)
        self.assertEqual(get("category=SLOW_RESPONSE")["total"], 1)
        self.assertEqual(get("category=SUCCESS")["total"], 13)
        self.assertEqual(get("status_code=503")["total"], 1)
        self.assertEqual(get("error_type=connection_refused")["total"], 1)
        self.assertEqual(get("service_id=999")["total"], 0)

    def test_time_range_filter(self):
        self.make_scenario()
        count = lambda r: self.client.get(f"/api/health-checks?range={r}").get_json()["total"]
        self.assertEqual(count("1h"), 16)
        self.assertEqual(count("all"), 16)
        sid = self.add_service("Old", "http://old.test/")
        self.add_check(sid, HealthStatus.HEALTHY, 3 * 86400)
        self.assertEqual(count("24h"), 16)
        self.assertEqual(count("7d"), 17)

    def test_failed_request_has_no_latency(self):
        self.make_scenario()
        items = self.client.get("/api/health-checks?category=CONNECTION_FAILURE").get_json()["items"]
        self.assertIsNone(items[0]["latency_seconds"])
        self.assertEqual(items[0]["failure_kind"], "CONNECTION_FAILURE")

    def test_pagination_bounds_and_clamping(self):
        self.make_scenario()
        for i in range(30):
            self.add_check(1, HealthStatus.HEALTHY, 700 + i)
        page = self.client.get("/api/health-checks?range=all&per_page=10&page=2").get_json()
        self.assertEqual((page["page"], page["pages"], len(page["items"]), page["total"]), (2, 5, 10, 46))
        last = self.client.get("/api/health-checks?range=all&per_page=10&page=99999").get_json()
        self.assertEqual((last["page"], len(last["items"])), (5, 6))
        timestamps = [i["timestamp"] for i in page["items"]]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_invalid_parameters_return_400(self):
        self.make_scenario()
        bad = ["state=BOGUS", "page=abc", "page=0", "per_page=7", "range=bogus",
               "status_code=99", "status_code=x", "service_id=-1", "category=nope",
               "error_type=a b;drop"]
        for q in bad:
            for base in ("/api/health-checks?", "/health?"):
                response = self.client.get(base + q)
                with self.subTest(q=q, base=base):
                    self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/health-checks?state=x").get_json()["error"], "Invalid request")

    def test_sql_injection_text_is_inert(self):
        self.make_scenario()
        for path in ("/incidents?q=%27%20OR%201%3D1%20--", "/services?q=%27%3B%20DROP%20TABLE%20incidents%3B--"):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/api/incidents").get_json()["total"], 2)


class TestIncidents(PagesCase):
    def test_priority_order_and_filters(self):
        self.make_scenario()
        items = self.client.get("/api/incidents").get_json()["items"]
        self.assertEqual([i["incident_id"] for i in items], ["INC-A", "INC-B"])
        f = lambda q: [i["incident_id"] for i in self.client.get("/api/incidents?" + q).get_json()["items"]]
        self.assertEqual(f("severity=WARNING"), ["INC-B"])
        self.assertEqual(f("status=OPEN"), ["INC-A"])
        self.assertEqual(f("category=SERVICE_UNAVAILABLE"), ["INC-A"])
        self.assertEqual(f("q=inc-b"), ["INC-B"])
        self.assertEqual(f("q=Web"), ["INC-A", "INC-B"])
        self.assertEqual(f("service_id=1"), ["INC-A", "INC-B"])
        self.assertEqual(f("service_id=2"), [])
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(len(f(f"date_from={today}&date_to={today}")), 2)
        self.assertEqual(f("date_to=2000-01-01"), [])
        self.assertEqual(f("q=%25"), [])  # LIKE wildcard is escaped

    def test_validation(self):
        for q in ("severity=HIGH", "status=x", "date_from=2026-02-30", "date_from=yesterday",
                  "date_from=2026-05-02&date_to=2026-05-01", "sort=drop", "q=" + "x" * 200):
            self.assertEqual(self.client.get("/incidents?" + q).status_code, 400, q)

    def test_detail_with_lifecycle_and_evidence(self):
        sid = self.make_scenario()
        incident = SimpleNamespace(
            incident_id="INC-A", service_url="http://web.test/",
            category="SERVICE_UNAVAILABLE", severity="CRITICAL", status="RESOLVED",
            summary="recovered", created_at=ago(300), last_updated_at=ago(5),
            resolved_at=ago(5), occurrences=4)
        self.db().update_incident(incident)
        html = self.client.get("/incidents/INC-A").get_data(as_text=True)
        data = self.client.get("/api/incidents/INC-A").get_json()
        self.assertEqual([e["event_type"] for e in data["events"]], ["CREATED", "RESOLVED"])
        self.assertIn("Resolved at", html)
        self.assertIn("2 consecutive checks", html)           # manager's failure_limit
        self.assertIn("inferred, not confirmed", html)
        self.assertIn("from stored record", html)
        self.assertIn("4 recorded occurrence(s)", html)
        self.assertTrue(data["lead_in"])
        self.assertTrue(all(c["timestamp"] <= data["incident"]["created_at"] for c in data["lead_in"]))
        self.assertTrue(all(c["timestamp"] > data["incident"]["created_at"] for c in data["during"]))
        self.assertEqual(data["incident"]["event_count"], 2)

    def test_escalation_event_is_shown(self):
        self.make_scenario()
        self.db().update_incident(SimpleNamespace(
            incident_id="INC-B", service_url="http://web.test/", category="ELEVATED_LATENCY",
            severity="CRITICAL", status="OPEN", summary="worse", created_at=ago(60),
            last_updated_at=ago(1), resolved_at=None, occurrences=3))
        html = self.client.get("/incidents/INC-B").get_data(as_text=True)
        self.assertIn("ESCALATED", html)
        self.assertIn("WARNING \u2192 CRITICAL", html)

    def test_open_incident_on_stale_service_is_marked_unverified(self):
        sid = self.add_service("Gone", "http://gone.test/")
        self.add_check(sid, HealthStatus.UNHEALTHY, 5000, status=None)
        self.add_incident("INC-S", "http://gone.test/", severity="CRITICAL")
        html = self.client.get("/incidents/INC-S").get_data(as_text=True)
        self.assertIn("unverified", html)
        self.assertNotIn("Ongoing.", html)

    def test_open_incident_on_fresh_service_is_ongoing(self):
        sid = self.add_service("Live", "http://live.test/")
        self.add_check(sid, HealthStatus.UNHEALTHY, 2, status=503)
        self.add_incident("INC-L", "http://live.test/", severity="CRITICAL")
        self.assertIn("Ongoing.", self.client.get("/incidents/INC-L").get_data(as_text=True))

    def test_unknown_and_malformed_incident_ids(self):
        self.make_scenario()
        self.assertEqual(self.client.get("/incidents/NOPE").status_code, 404)
        self.assertEqual(self.client.get("/api/incidents/NOPE").status_code, 404)
        self.assertEqual(self.client.get("/incidents/a%20b").status_code, 400)

    def test_events_page_and_filter(self):
        self.make_scenario()
        all_events = self.client.get("/events").get_data(as_text=True)
        self.assertIn("INC-A", all_events)
        self.assertEqual(self.client.get("/events?event_type=RESOLVED").status_code, 200)
        self.assertEqual(self.client.get("/events?event_type=BOGUS").status_code, 400)


class TestServicesAndCharts(PagesCase):
    def test_service_filters_sort_and_pagination(self):
        self.populate()
        get = lambda q: [s["name"] for s in self.client.get("/api/services?" + q).get_json()["items"]]
        self.assertEqual(get("sort=name"), ["Alpha", "Bravo", "Charlie", "Delta", "Echo"])
        self.assertEqual(get("sort=name&order=desc")[0], "Echo")
        self.assertEqual(get("state=UNHEALTHY"), ["Charlie"])
        self.assertEqual(get("state=STALE"), ["Delta"])
        self.assertEqual(get("state=NO_DATA"), ["Echo"])
        self.assertEqual(get("q=brav"), ["Bravo"])
        self.assertEqual(get("sort=state")[0], "Charlie")           # unhealthy first
        self.assertEqual(self.client.get("/api/services?per_page=10&page=9").get_json()["page"], 1)
        self.assertEqual(self.client.get("/services?sort=bad").status_code, 400)

    def test_service_detail(self):
        sid = self.make_scenario()
        html = self.client.get(f"/services/{sid}").get_data(as_text=True)
        self.assertIn("Success rate", html)
        self.assertIn("Most recent problem", html)
        data = self.client.get(f"/api/services/{sid}").get_json()
        self.assertEqual(data["stats"]["24h"]["checks"], 16)
        self.assertEqual(data["stats"]["24h"]["success_rate"], round(100 * 14 / 16, 1))
        self.assertEqual(self.client.get("/services/999").status_code, 404)
        self.assertEqual(self.client.get("/services/abc").status_code, 404)

    def test_stale_service_is_not_reported_healthy_on_detail(self):
        sid = self.add_service("Old", "http://old.test/")
        self.add_check(sid, HealthStatus.HEALTHY, 9000)
        html = self.client.get(f"/services/{sid}").get_data(as_text=True)
        self.assertIn("not verified current health", html)

    def test_response_series_excludes_failures_from_line(self):
        self.make_scenario()
        series = self.client.get("/api/charts/response-times?range=1h").get_json()["series"][0]
        self.assertEqual(len(series["points"]), 14)
        self.assertEqual(len(series["failures"]), 2)
        self.assertTrue(all(p["v"] >= 100 for p in series["points"]))   # ms, none zero
        self.assertEqual({f["kind"] for f in series["failures"]}, {"HTTP_ERROR", "CONNECTION_FAILURE"})

    def test_history_buckets(self):
        self.make_scenario()
        data = self.client.get("/api/charts/history?range=1h").get_json()
        total = sum(b["HEALTHY"] + b["DEGRADED"] + b["UNHEALTHY"] for b in data["buckets"])
        self.assertEqual(total, 16)
        self.assertEqual(data["bucket_seconds"], 300)

    def test_chart_parameter_validation(self):
        for q in ("range=30d", "range=all", "range=zzz", "service_id=x"):
            self.assertEqual(self.client.get("/api/charts/history?" + q).status_code, 400, q)

    def test_overview_shows_real_data_and_empty_states(self):
        self.assertIn("No services are registered", self.client.get("/").get_data(as_text=True)
                      if self.db() else "")
        self.make_scenario()
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Web", html)
        self.assertIn("INC-A", html)
        self.assertIn("grouped into 2 incident(s)", html)


class TestInsightsAndDiagnosis(unittest.TestCase):
    def service(self, **kw):
        base = {"service_id": 1, "name": "Svc", "state": "HEALTHY", "status_code": 200,
                "last_check_at": "t", "age_seconds": 3.0, "stale_after_seconds": 30.0,
                "healthy_threshold_seconds": 0.5}
        return {**base, **kw}

    def test_all_clear_requires_no_open_incidents(self):
        now = datetime.now(timezone.utc)
        ok = insights.build_insights([self.service()], {}, [], [], 0, now)
        self.assertEqual(ok[0]["title"], "All services healthy")
        self.assertEqual(insights.build_insights([self.service()], {}, [], [], 1, now), [])

    def test_observations_cite_evidence(self):
        now = datetime.now(timezone.utc)
        items = insights.build_insights(
            [self.service(state="UNHEALTHY", status_code=503),
             self.service(service_id=2, name="Slow", state="DEGRADED"),
             self.service(service_id=3, name="Old", state="STALE", age_seconds=999.0),
             self.service(service_id=4, name="New", state="NO_DATA")],
            {2: [0.6] * 6}, [], [], 0, now)
        titles = " | ".join(i["title"] for i in items)
        for expected in ("Svc is unhealthy", "Slow is degraded", "Slow responds slowly",
                         "No recent data for Old", "New has no health checks"):
            self.assertIn(expected, titles)
        self.assertTrue(all(i["evidence"] for i in items))

    def test_recovery_only_when_latest_state_is_healthy(self):
        now = datetime.now(timezone.utc)
        resolved = [{"incident_id": "I", "service_id": 1, "resolved_at": "t"}]
        self.assertTrue(any("recovered" in i["title"] for i in insights.build_insights(
            [self.service()], {}, resolved, [], 0, now)))
        self.assertFalse(any("recovered" in i["title"] for i in insights.build_insights(
            [self.service(state="UNHEALTHY")], {}, resolved, [], 0, now)))

    def test_diagnosis_matches_monitoring_engine(self):
        service = {"service_url": "http://x/", "timeout_seconds": 3.0,
                   "monitoring_interval_seconds": 10, "healthy_threshold_seconds": 0.5,
                   "unhealthy_threshold_seconds": 1.0}
        check = {"timestamp": "2026-01-01T00:00:00+00:00", "health_status": "UNHEALTHY",
                 "status_code": 503, "response_time_seconds": 0.2,
                 "error": "Service Unavailable", "error_type": "http_error"}
        got = insights.diagnose_check(check, service)
        expected = diagnose_health(
            HealthCheckResult(timestamp=check["timestamp"], url="http://x/", health=HealthStatus.UNHEALTHY,
                              status_code=503, response_time_seconds=0.2, error="Service Unavailable",
                              error_type="http_error"),
            MonitoringConfig(url="http://x/", timeout=3.0, interval=10.0,
                             healthy_threshold=0.5, unhealthy_threshold=1.0))
        self.assertEqual((got["diagnosis"], got["likely_cause"], got["recommendation"]),
                         (expected.diagnosis, expected.likely_cause, expected.recommendation))
        self.assertIn("HTTP status code: 503.", got["observed"])

    def test_incident_rules_come_from_the_manager(self):
        self.assertEqual(insights.incident_rules(),
                         {"degraded_limit": 3, "failure_limit": 2, "recovery_limit": 2})


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestChartScripts(PagesCase):
    def test_renderers_run_on_real_api_data(self):
        self.make_scenario()
        payload = {
            "timeseries": self.client.get("/api/charts/response-times?range=1h").get_json(),
            "stackedbars": self.client.get("/api/charts/history?range=1h").get_json(),
            "hbars": self.client.get("/api/charts/incidents").get_json(),
            "donut": {"items": [{"label": "Healthy", "value": 2, "tone": "ok"}]},
        }
        script = Path(__file__).with_name("charts_smoke.js")
        run = subprocess.run(["node", str(script)], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual(out["timeseries"]["circles"], 14)   # one dot per 2xx check
        self.assertGreater(out["stackedbars"]["rects"], 0)
        self.assertEqual(out["hbars"]["li"], 4)
        self.assertFalse(any(v["error"] for v in out.values()))


if __name__ == "__main__":
    unittest.main()
