"""Tests for evidence-based HTTP health diagnosis."""

import unittest

from monitoring.health_monitor import (
    DiagnosisResult,
    HealthCheckResult,
    HealthStatus,
    MonitoringConfig,
    diagnose_health,
)


class TestHealthDiagnosis(unittest.TestCase):
    """Ensure common endpoint failures produce useful diagnostic guidance."""

    def setUp(self):
        """Create one validated configuration for deterministic diagnosis tests."""
        self.config = MonitoringConfig(
            url="http://service.test/",
            timeout=3.0,
            interval=10.0,
            healthy_threshold=0.5,
            unhealthy_threshold=1.0,
        )

    def result(self, *, status_code, response_time=0.1, error=None, error_type=None):
        """Build a health-check result without making a network request."""
        health = HealthStatus.HEALTHY
        if status_code is None or not 200 <= status_code < 300 or response_time >= 1.0:
            health = HealthStatus.UNHEALTHY
        elif response_time >= 0.5:
            health = HealthStatus.DEGRADED
        return HealthCheckResult(
            timestamp="2026-10-26T10:00:00+00:00",
            url=self.config.url,
            health=health,
            status_code=status_code,
            response_time_seconds=response_time,
            error=error,
            error_type=error_type,
        )

    def test_http_server_error_is_diagnosed(self):
        """HTTP 5xx responses should recommend checking server logs."""
        diagnosis = diagnose_health(
            self.result(status_code=503, error="Service Unavailable", error_type="http_error"),
            self.config,
        )
        self.assertEqual(diagnosis.diagnosis, "HTTP server error")
        self.assertIn("logs", diagnosis.recommendation.lower())

    def test_timeout_is_diagnosed(self):
        """Timeout failures should be distinguished from HTTP errors."""
        diagnosis = diagnose_health(
            self.result(status_code=None, error="timed out", error_type="timeout"),
            self.config,
        )
        self.assertEqual(diagnosis.diagnosis, "Request timeout")

    def test_connection_refused_is_diagnosed(self):
        """Connection refusal should point to the listener and network rules."""
        diagnosis = diagnose_health(
            self.result(status_code=None, error="refused", error_type="connection_refused"),
            self.config,
        )
        self.assertEqual(diagnosis.diagnosis, "Connection refused")
        self.assertIn("Apache", diagnosis.recommendation)

    def test_slow_successful_response_is_diagnosed(self):
        """Slow HTTP 200 responses should be classified as latency problems."""
        diagnosis = diagnose_health(
            self.result(status_code=200, response_time=1.2), self.config
        )
        self.assertEqual(diagnosis.diagnosis, "Excessive response latency")

    def test_healthy_response_has_no_immediate_issue(self):
        """Fast HTTP 2xx responses should produce a healthy diagnosis."""
        diagnosis = diagnose_health(
            self.result(status_code=200, response_time=0.1), self.config
        )
        self.assertEqual(diagnosis.diagnosis, "No immediate issue detected")


if __name__ == "__main__":
    unittest.main()
