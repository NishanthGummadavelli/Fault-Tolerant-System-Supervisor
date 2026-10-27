
"""
Fault-Tolerant System Supervisor
Stage 2: Health Monitoring and Failure Diagnosis

Monitors an HTTP/HTTPS endpoint, classifies its health, identifies
possible failure causes, and recommends appropriate troubleshooting actions.
"""

import argparse
import socket
import time

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


# Default monitoring configuration
TARGET_URL = "http://localhost/"
REQUEST_TIMEOUT = 3.0
HEALTHY_THRESHOLD = 0.5
UNHEALTHY_THRESHOLD = 1.0
MONITOR_INTERVAL = 10.0


class HealthStatus(str, Enum):
    """Represent the possible health states of a monitored service."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


@dataclass(frozen=True)
class HealthCheckResult:
    """Store the outcome of a single health check."""

    timestamp: str
    url: str
    health: HealthStatus
    status_code: int | None
    response_time_seconds: float
    error: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class DiagnosisResult:
    """Store the diagnosis and recommended troubleshooting action."""

    diagnosis: str
    likely_cause: str
    recommendation: str


@dataclass(frozen=True)
class MonitoringConfig:
    """Store and organize the health-monitoring configuration."""

    url: str = TARGET_URL
    timeout: float = REQUEST_TIMEOUT
    interval: float = MONITOR_INTERVAL
    healthy_threshold: float = HEALTHY_THRESHOLD
    unhealthy_threshold: float = UNHEALTHY_THRESHOLD


def validate_url(url: str) -> bool:
    """Return True if the URL has a valid HTTP/HTTPS structure."""

    try:
        parsed_url = urlsplit(url)

        return (
            parsed_url.scheme in {"http", "https"}
            and bool(parsed_url.hostname)
            and parsed_url.port != 0
        )
    except ValueError:
        # Invalid port syntax or malformed URL.
        return False


def validate_config(config: MonitoringConfig) -> None:
    """Validate monitoring settings and raise ValueError if invalid."""

    if not validate_url(config.url):
        raise ValueError(
            "Invalid URL. Provide a valid HTTP or HTTPS URL."
        )

    if config.timeout <= 0:
        raise ValueError("Timeout must be greater than zero.")

    if config.interval <= 0:
        raise ValueError("Monitoring interval must be greater than zero.")

    if config.healthy_threshold <= 0:
        raise ValueError("Healthy threshold must be greater than zero.")

    if config.unhealthy_threshold <= config.healthy_threshold:
        raise ValueError(
            "Unhealthy threshold must be greater than the healthy threshold."
        )


def classify_health(
    status_code: int | None,
    response_time: float,
    config: MonitoringConfig,
) -> HealthStatus:
    """Classify service health using HTTP status and response time."""

    # Missing HTTP status means that the request did not succeed.
    if status_code is None:
        return HealthStatus.UNHEALTHY

    # Only successful 2xx responses count as healthy HTTP responses.
    if not 200 <= status_code < 300:
        return HealthStatus.UNHEALTHY

    # Apply the configured response-time thresholds.
    if response_time >= config.unhealthy_threshold:
        return HealthStatus.UNHEALTHY

    if response_time >= config.healthy_threshold:
        return HealthStatus.DEGRADED

    return HealthStatus.HEALTHY


def check_server(config: MonitoringConfig) -> HealthCheckResult:
    """Perform one HTTP health check and record its result."""

    start_time = time.perf_counter()
    status_code = None
    error_message = None
    error_type = None

    # Identify the supervisor in the request's User-Agent header.
    request = Request(
        config.url,
        headers={"User-Agent": "Fault-Tolerant-System-Supervisor/1.0"},
    )

    try:
        # Open the endpoint and obtain its HTTP response status.
        with urlopen(request, timeout=config.timeout) as response:
            status_code = response.status

    except HTTPError as error:
        # HTTPError represents an HTTP response such as 404 or 503.
        status_code = error.code
        error_message = str(error.reason or "HTTP error")
        error_type = "http_error"
        error.close()

    except (TimeoutError, socket.timeout) as error:
        error_message = str(error) or "The request timed out."
        error_type = "timeout"

    except URLError as error:
        # urllib can wrap connection failures inside URLError.
        reason = error.reason
        error_message = str(reason)

        if isinstance(reason, (TimeoutError, socket.timeout)):
            error_type = "timeout"
        elif isinstance(reason, socket.gaierror):
            # The hostname could not be resolved to an IP address.
            error_type = "dns_failure"
        elif isinstance(reason, ConnectionRefusedError):
            error_type = "connection_refused"
        elif isinstance(reason, OSError) and reason.errno == 111:
            # errno 111 is ECONNREFUSED on Linux.
            error_type = "connection_refused"
        else:
            error_type = "connection_error"

    except OSError as error:
        error_message = str(error)

        if isinstance(error, socket.gaierror):
            error_type = "dns_failure"
        elif isinstance(error, ConnectionRefusedError):
            error_type = "connection_refused"
        elif isinstance(error, (TimeoutError, socket.timeout)):
            error_type = "timeout"
        else:
            error_type = "connection_error"

    except ValueError as error:
        # Handle invalid request parameters or URL-related errors.
        error_message = str(error)
        error_type = "connection_error"

    # Calculate elapsed time even when the request fails.
    response_time = time.perf_counter() - start_time

    # Determine the service's health from the collected evidence.
    health = classify_health(status_code, response_time, config)

    return HealthCheckResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        url=config.url,
        health=health,
        status_code=status_code,
        response_time_seconds=round(response_time, 3),
        error=error_message,
        error_type=error_type,
    )


def diagnose_health(
    result: HealthCheckResult,
    config: MonitoringConfig,
) -> DiagnosisResult:
    """Identify a likely problem and recommend the next troubleshooting step."""

    status_code = result.status_code
    response_time = result.response_time_seconds

    # Diagnose connection-level failures before checking HTTP status.
    if result.error_type == "timeout":
        return DiagnosisResult(
            diagnosis="Request timeout",
            likely_cause=(
                "The endpoint did not respond within the configured timeout."
            ),
            recommendation=(
                "Check server responsiveness, network connectivity, and "
                "whether the timeout setting is appropriate."
            ),
        )

    if result.error_type == "connection_refused":
        return DiagnosisResult(
            diagnosis="Connection refused",
            likely_cause=(
                "The destination may not be accepting connections on "
                "the requested port."
            ),
            recommendation=(
                "Verify that Apache or the application is running and "
                "listening on the expected port. Check the EC2 security "
                "group and host firewall."
            ),
        )

    if result.error_type == "dns_failure":
        return DiagnosisResult(
            diagnosis="DNS resolution failure",
            likely_cause=(
                "The hostname could not be resolved to an IP address."
            ),
            recommendation=(
                "Verify the hostname in the configured URL, the DNS "
                "settings of the monitoring host, and that the load "
                "balancer or instance DNS name still exists."
            ),
        )

    if result.error_type == "connection_error":
        return DiagnosisResult(
            diagnosis="Connection failure",
            likely_cause=(
                "The endpoint may be unreachable because of a network, "
                "DNS, or connection configuration problem."
            ),
            recommendation=(
                "Verify the URL, current EC2 public IP, network connectivity, "
                "and relevant security-group rules."
            ),
        )

    # Diagnose HTTP responses according to their status codes.
    if status_code is not None and 300 <= status_code < 400:
        return DiagnosisResult(
            diagnosis="HTTP redirect",
            likely_cause=(
                "The endpoint returned a redirect instead of a final "
                "successful response."
            ),
            recommendation=(
                "Inspect the Location header and verify the redirect "
                "configuration and target URL."
            ),
        )

    if status_code is not None and 500 <= status_code < 600:
        return DiagnosisResult(
            diagnosis="HTTP server error",
            likely_cause=(
                "The server or application reported an internal error."
            ),
            recommendation=(
                "Inspect Apache and application logs, then investigate "
                "server load, application errors, and dependencies."
            ),
        )

    if status_code is not None and 400 <= status_code < 500:
        return DiagnosisResult(
            diagnosis="HTTP client/request error",
            likely_cause=(
                "The server rejected the request or could not find "
                "the requested resource."
            ),
            recommendation=(
                "Check the URL, requested resource, permissions, and "
                "server configuration."
            ),
        )

    if status_code is None:
        return DiagnosisResult(
            diagnosis="No HTTP response received",
            likely_cause=(
                result.error or "The endpoint did not return an HTTP status."
            ),
            recommendation=(
                "Check network connectivity, endpoint availability, "
                "and the configured timeout."
            ),
        )

    # Diagnose slow successful responses separately from HTTP errors.
    if response_time >= config.unhealthy_threshold:
        return DiagnosisResult(
            diagnosis="Excessive response latency",
            likely_cause=(
                "The successful HTTP response exceeded the unhealthy "
                "response-time threshold."
            ),
            recommendation=(
                "Investigate server resource usage, application processing "
                "time, and network latency."
            ),
        )

    if response_time >= config.healthy_threshold:
        return DiagnosisResult(
            diagnosis="Elevated response latency",
            likely_cause=(
                "The endpoint responded successfully, but its response "
                "time exceeded the healthy threshold."
            ),
            recommendation=(
                "Continue monitoring and compare response times across "
                "multiple checks before concluding that a persistent "
                "performance problem exists."
            ),
        )

    return DiagnosisResult(
        diagnosis="No immediate issue detected",
        likely_cause=(
            "The endpoint returned a successful HTTP response within "
            "the healthy response-time threshold."
        ),
        recommendation=(
            "Continue monitoring to detect future failures or "
            "performance degradation."
        ),
    )


def display_result(
    check_number: int,
    result: HealthCheckResult,
    diagnosis: DiagnosisResult,
) -> None:
    """Display health-check results and failure diagnosis."""

    print("\n" + "=" * 65)
    print(f"FAULT-TOLERANT SYSTEM SUPERVISOR | Check #{check_number}")
    print("=" * 65)
    print(f"Target URL       : {result.url}")
    print(f"Health Status    : {result.health.value}")
    print(f"HTTP Status      : {result.status_code or 'Unavailable'}")
    print(
        f"Response Time    : {result.response_time_seconds:.3f} seconds"
    )
    print(f"Timestamp (UTC)  : {result.timestamp}")

    if result.error:
        print(f"Observed Error   : {result.error}")

    print("-" * 65)
    print(f"Diagnosis        : {diagnosis.diagnosis}")
    print(f"Likely Cause     : {diagnosis.likely_cause}")
    print(f"Recommendation   : {diagnosis.recommendation}")
    print("=" * 65)


def report_state_change(
    previous_status: HealthStatus | None,
    current_status: HealthStatus,
) -> None:
    """Report changes in health state, including recovery."""

    if previous_status is None:
        print(f"Initial health state: {current_status.value}")
    elif previous_status != current_status:
        print(
            f"STATE CHANGE: {previous_status.value} -> "
            f"{current_status.value}"
        )

        if current_status == HealthStatus.HEALTHY:
            print("RECOVERY: The endpoint is healthy again.")


class HealthMonitor:
    """Coordinate health checks, diagnosis, and continuous monitoring."""

    def __init__(self, config: MonitoringConfig):
        """Initialize the monitor with validated configuration."""

        self.config = config
        self.check_count = 0
        self.previous_status = None

    def run_once(self) -> tuple[HealthCheckResult, DiagnosisResult]:
        """Perform one health check, diagnose the result, and display it."""

        self.check_count += 1

        result = check_server(self.config)
        diagnosis = diagnose_health(result, self.config)

        display_result(self.check_count, result, diagnosis)

        report_state_change(
            self.previous_status,
            result.health,
        )

        self.previous_status = result.health

        return result, diagnosis

    def monitor(self) -> None:
        """Run health checks continuously until interrupted by the user."""

        print("Starting continuous health monitoring...")
        print(f"Monitoring interval: {self.config.interval} seconds")
        print("Press Ctrl+C to stop monitoring.")

        try:
            while True:
                self.run_once()

                # Wait before beginning the next check.
                time.sleep(self.config.interval)

        except KeyboardInterrupt:
            print("\nMonitoring stopped by user.")
            print(f"Total checks performed: {self.check_count}")


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Monitor an HTTP/HTTPS endpoint and diagnose "
            "possible service failures."
        )
    )

    parser.add_argument(
        "--url",
        default=TARGET_URL,
        help="Target HTTP/HTTPS URL to monitor.",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=REQUEST_TIMEOUT,
        help="Request timeout in seconds.",
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=MONITOR_INTERVAL,
        help="Delay in seconds between consecutive checks.",
    )

    parser.add_argument(
        "--healthy-threshold",
        type=float,
        default=HEALTHY_THRESHOLD,
        help="Response times below this value are classified as healthy.",
    )

    parser.add_argument(
        "--unhealthy-threshold",
        type=float,
        default=UNHEALTHY_THRESHOLD,
        help="Response times at or above this value are unhealthy.",
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Perform one health check and exit.",
    )

    return parser


def main() -> int:
    """Validate arguments and start the requested monitoring mode."""

    parser = build_parser()
    args = parser.parse_args()

    config = MonitoringConfig(
        url=args.url,
        timeout=args.timeout,
        interval=args.interval,
        healthy_threshold=args.healthy_threshold,
        unhealthy_threshold=args.unhealthy_threshold,
    )

    try:
        validate_config(config)
    except ValueError as error:
        parser.error(str(error))

    monitor = HealthMonitor(config)

    if args.once:
        result, _ = monitor.run_once()

        # Return an exit code suitable for scripts and automated tests.
        if result.health == HealthStatus.HEALTHY:
            return 0

        if result.health == HealthStatus.DEGRADED:
            return 1

        return 2

    monitor.monitor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
