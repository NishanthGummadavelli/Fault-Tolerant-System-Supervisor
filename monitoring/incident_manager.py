
"""
Fault-Tolerant System Supervisor
Stage 3: Incident Detection and Correlation

Defines incident data models and manages incident detection,
correlation, severity escalation, and recovery.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from monitoring.health_monitor import HealthStatus


class IncidentStatus(str, Enum):
    """Represent the lifecycle status of an incident."""

    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class IncidentSeverity(str, Enum):
    """Represent the seriousness of a detected incident."""

    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class IncidentCategory(str, Enum):
    """Represent the category of a detected service problem."""

    ELEVATED_LATENCY = "ELEVATED_LATENCY"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


@dataclass
class Incident:
    """Store the structured information associated with a service incident."""

    incident_id: str
    service_url: str
    category: IncidentCategory
    severity: IncidentSeverity
    status: IncidentStatus
    summary: str
    created_at: datetime
    last_updated_at: datetime
    occurrences: int = 1
    resolved_at: datetime | None = None


def current_timestamp() -> datetime:
    """Return the current timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class IncidentManager:
    """Detect, correlate, escalate, and resolve service incidents."""

    def __init__(
        self,
        healthy_threshold: float = 0.5,
        unhealthy_threshold: float = 1.0,
        degraded_limit: int = 3,
        failure_limit: int = 2,
        recovery_limit: int = 2,
    ) -> None:
        """Initialize incident storage, detection thresholds, and counters."""

        if not 0 < healthy_threshold < unhealthy_threshold:
            raise ValueError(
                "Thresholds must satisfy 0 < healthy < unhealthy."
            )

        if min(degraded_limit, failure_limit, recovery_limit) < 1:
            raise ValueError("Detection limits must be at least 1.")

        self.healthy_threshold = healthy_threshold
        self.unhealthy_threshold = unhealthy_threshold
        self.degraded_limit = degraded_limit
        self.failure_limit = failure_limit
        self.recovery_limit = recovery_limit

        # Preserve all incidents, including resolved incidents, for history.
        self.incidents: list[Incident] = []

        # Index open incidents by service URL and incident category.
        self.open_incidents: dict[
            tuple[str, IncidentCategory], Incident
        ] = {}

        # Maintain independent consecutive-result counters per service.
        self.counters: dict[str, dict[str, int]] = {}

        self._next_incident_number = 1

    def _get_counters(self, service_url: str) -> dict[str, int]:
        """Return or initialize consecutive health-check counters."""

        if service_url not in self.counters:
            self.counters[service_url] = {
                "degraded": 0,
                "latency_unhealthy": 0,
                "availability_failure": 0,
                "healthy": 0,
            }

        return self.counters[service_url]

    def _classify_result(
        self, result: object
    ) -> tuple[str, IncidentCategory | None]:
        """Classify a health-check result using HTTP status and latency."""

        health = getattr(result, "health", None)
        status_code = getattr(result, "status_code", None)
        response_time = getattr(result, "response_time_seconds", None)

        # Missing or unsuccessful HTTP status indicates an availability issue.
        if status_code is None or not 200 <= status_code < 300:
            return (
                "availability_failure",
                IncidentCategory.SERVICE_UNAVAILABLE,
            )

        # A successful response can still be unhealthy due to high latency.
        if response_time is not None:
            if response_time >= self.unhealthy_threshold:
                return (
                    "latency_unhealthy",
                    IncidentCategory.ELEVATED_LATENCY,
                )

            if response_time >= self.healthy_threshold:
                return "degraded", IncidentCategory.ELEVATED_LATENCY

        # Use the health monitor's shared enum for classification.
        if health == HealthStatus.HEALTHY:
            return "healthy", None

        if health == HealthStatus.DEGRADED:
            return "degraded", IncidentCategory.ELEVATED_LATENCY

        if health == HealthStatus.UNHEALTHY:
            return (
                "availability_failure",
                IncidentCategory.SERVICE_UNAVAILABLE,
            )

        raise ValueError(
            f"Unsupported health status: {health!r}"
        )

    def _create_incident(
        self,
        service_url: str,
        category: IncidentCategory,
        severity: IncidentSeverity,
        summary: str,
    ) -> Incident:
        """Create and store a new incident with a unique identifier."""

        now = current_timestamp()
        incident_id = f"INC-{self._next_incident_number:03d}"
        self._next_incident_number += 1

        incident = Incident(
            incident_id=incident_id,
            service_url=service_url,
            category=category,
            severity=severity,
            status=IncidentStatus.OPEN,
            summary=summary,
            created_at=now,
            last_updated_at=now,
        )

        # Keep a permanent in-memory history and an index of open incidents.
        self.incidents.append(incident)
        self.open_incidents[(service_url, category)] = incident

        return incident

    def _update_incident(
        self,
        incident: Incident,
        summary: str,
        severity: IncidentSeverity | None = None,
    ) -> None:
        """Update an existing incident and escalate its severity if needed."""

        incident.occurrences += 1
        incident.last_updated_at = current_timestamp()
        incident.summary = summary

        # Escalate warnings to critical; never downgrade an active incident.
        if severity == IncidentSeverity.CRITICAL:
            incident.severity = IncidentSeverity.CRITICAL

    def _open_or_update(
        self,
        service_url: str,
        category: IncidentCategory,
        severity: IncidentSeverity,
        summary: str,
    ) -> tuple[Incident, bool]:
        """Update a matching open incident or create a new one."""

        key = (service_url, category)
        existing = self.open_incidents.get(key)

        if existing is not None:
            self._update_incident(existing, summary, severity)
            return existing, False

        return (
            self._create_incident(
                service_url, category, severity, summary
            ),
            True,
        )

    def _resolve_service_incidents(self, service_url: str) -> list[str]:
        """Resolve all open incidents belonging to a recovered service."""

        resolved_ids: list[str] = []
        now = current_timestamp()

        # Preserve resolved incidents in history while removing them from
        # the active-incident index.
        for key, incident in list(self.open_incidents.items()):
            if key[0] != service_url:
                continue

            incident.status = IncidentStatus.RESOLVED
            incident.resolved_at = now
            incident.last_updated_at = now

            del self.open_incidents[key]
            resolved_ids.append(incident.incident_id)

        return resolved_ids

    @staticmethod
    def _failure_summary(result: object) -> str:
        """Build an incident summary from observed HTTP and connection evidence."""
        status_code = getattr(result, "status_code", None)
        error_type = getattr(result, "error_type", None)
        error = getattr(result, "error", None)
        response_time = getattr(result, "response_time_seconds", None)

        if status_code is not None and not 200 <= status_code < 300:
            return f"Service returned HTTP {status_code}; endpoint is unavailable."
        if error_type == "timeout":
            return "Health check timed out before the endpoint responded."
        if error_type == "connection_refused":
            return "Connection refused; the endpoint may not be listening."
        if error_type == "connection_error":
            return f"Connection failure: {error or 'endpoint could not be reached'}."
        if response_time is not None:
            return f"Endpoint response time is unhealthy ({response_time:.3f} seconds)."
        return "The service is unavailable or returning HTTP errors."

    def process_result(self, result: object) -> list[str]:
        """Process one health check and return any incident events."""

        service_url = getattr(result, "url", None)

        if not service_url:
            raise ValueError(
                "Health-check result must contain a service URL."
            )

        counters = self._get_counters(service_url)
        result_type, category = self._classify_result(result)

        # Preserve the previous degraded streak before resetting counters.
        previous_degraded_streak = counters["degraded"]

        events: list[str] = []

        # Maintain true consecutive streaks. A different result type resets
        # the other streaks, including when moving between latency categories.
        for counter_name in counters:
            if counter_name == result_type:
                counters[counter_name] += 1
            else:
                counters[counter_name] = 0

        if result_type == "healthy":
            # Require consecutive healthy checks before confirming recovery.
            if counters["healthy"] >= self.recovery_limit:
                resolved_ids = self._resolve_service_incidents(service_url)

                if resolved_ids:
                    events.append(
                        f"Resolved incidents: {', '.join(resolved_ids)}"
                    )

                # Reset the recovery streak after processing recovery.
                counters["healthy"] = 0

            return events

        if result_type == "degraded":
            # Open a warning only after sustained elevated latency.
            if counters["degraded"] == self.degraded_limit:
                incident, created = self._open_or_update(
                    service_url,
                    IncidentCategory.ELEVATED_LATENCY,
                    IncidentSeverity.WARNING,
                    (
                        f"Response latency remains elevated "
                        f"({getattr(result, 'response_time_seconds', 0.0):.3f} seconds)."
                    ),
                )

                action = "Opened" if created else "Updated"
                events.append(
                    f"{action} {incident.incident_id}: "
                    f"{incident.category.value} "
                    f"({incident.severity.value})"
                )

            return events

        if result_type == "latency_unhealthy":
            existing_latency_incident = self.open_incidents.get(
                (service_url, IncidentCategory.ELEVATED_LATENCY)
            )

            # Escalate immediately if an earlier warning exists or two
            # degraded checks preceded this unhealthy latency result.
            sustained_degradation = previous_degraded_streak >= 2

            repeated_unhealthy_latency = (
                counters["latency_unhealthy"] >= self.failure_limit
            )

            if (
                sustained_degradation
                or repeated_unhealthy_latency
                or existing_latency_incident is not None
            ):
                incident, created = self._open_or_update(
                    service_url,
                    IncidentCategory.ELEVATED_LATENCY,
                    IncidentSeverity.CRITICAL,
                    self._failure_summary(result),
                )

                action = "Opened" if created else "Updated"
                events.append(
                    f"{action} {incident.incident_id}: "
                    f"{incident.category.value} "
                    f"({incident.severity.value})"
                )

            return events

        if result_type == "availability_failure":
            # Require consecutive availability failures before opening
            # a critical service-unavailability incident.
            if counters["availability_failure"] >= self.failure_limit:
                incident, created = self._open_or_update(
                    service_url,
                    IncidentCategory.SERVICE_UNAVAILABLE,
                    IncidentSeverity.CRITICAL,
                    self._failure_summary(result),
                )

                action = "Opened" if created else "Updated"
                events.append(
                    f"{action} {incident.incident_id}: "
                    f"{incident.category.value} "
                    f"({incident.severity.value})"
                )

            return events

        return events

    def get_open_incidents(self) -> list[Incident]:
        """Return a list of currently open incidents."""

        return list(self.open_incidents.values())

    def get_incident_history(self) -> list[Incident]:
        """Return all incidents, including resolved incidents."""

        return list(self.incidents)
