
"""
Fault-Tolerant System Supervisor
Stage 3: Incident Manager Demonstration

Demonstrates incident creation, escalation, recovery, and recurrence
using simulated health-check results.
"""

import sys
from pathlib import Path

# Allow this script to import the monitoring modules from its directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from health_monitor import HealthStatus
from incident_manager import IncidentManager


def create_result(health, response_time, status_code=200, error=None):
    """Create a simulated health-check result for demonstration."""

    from datetime import datetime, timezone
    from types import SimpleNamespace

    return SimpleNamespace(
        timestamp=datetime.now(timezone.utc),
        url="http://demo-service.local/",
        health=health,
        status_code=status_code,
        response_time_seconds=response_time,
        error=error,
        error_type=None,
    )


def display_incidents(manager, title):
    """Display open incidents and incident history in the terminal."""

    print(f"\n{'=' * 65}")
    print(title)
    print("=" * 65)

    open_incidents = manager.get_open_incidents()

    if not open_incidents:
        print("No open incidents.")
    else:
        print("\nOPEN INCIDENTS:")

        for incident in open_incidents:
            print(f"Incident ID : {incident.incident_id}")
            print(f"Service     : {incident.service_url}")
            print(f"Category    : {incident.category.value}")
            print(f"Severity    : {incident.severity.value}")
            print(f"Status      : {incident.status.value}")
            print(f"Summary     : {incident.summary}")
            print(f"Occurrences : {incident.occurrences}")
            print("-" * 40)

    history = manager.get_incident_history()

    print("\nINCIDENT HISTORY:")

    if not history:
        print("No incident history yet.")
    else:
        for incident in history:
            print(
                f"{incident.incident_id} | "
                f"{incident.category.value} | "
                f"{incident.severity.value} | "
                f"{incident.status.value} | "
                f"Occurrences: {incident.occurrences}"
            )


def main():
    """Run demonstration scenarios for incident detection and recovery."""

    manager = IncidentManager()

    print("\nFAULT-TOLERANT SYSTEM SUPERVISOR")
    print("STAGE 3: INCIDENT DETECTION DEMONSTRATION")

    # Scenario 1: Three degraded checks create a WARNING incident.
    print("\nScenario 1: Sustained latency degradation")

    for check_number in range(1, 4):
        print(f"Health check {check_number}: DEGRADED (0.700 seconds)")
        manager.process_result(
            create_result(HealthStatus.DEGRADED, 0.7)
        )

    display_incidents(manager, "After sustained degradation")

    # Scenario 2: Unhealthy latency escalates the existing incident.
    print("\nScenario 2: Escalation to CRITICAL")

    for check_number in range(1, 3):
        print(
            f"Health check {check_number}: "
            "UNHEALTHY latency (1.200 seconds)"
        )
        manager.process_result(
            create_result(HealthStatus.UNHEALTHY, 1.2)
        )

    display_incidents(manager, "After latency escalation")

    # Scenario 3: Two healthy checks resolve the incident.
    print("\nScenario 3: Service recovery")

    for check_number in range(1, 3):
        print(f"Health check {check_number}: HEALTHY (0.200 seconds)")
        manager.process_result(
            create_result(HealthStatus.HEALTHY, 0.2)
        )

    display_incidents(manager, "After service recovery")

    # Scenario 4: A new failure after recovery creates a new incident.
    print("\nScenario 4: New failure after recovery")

    for check_number in range(1, 4):
        print(f"Health check {check_number}: DEGRADED (0.700 seconds)")
        manager.process_result(
            create_result(HealthStatus.DEGRADED, 0.7)
        )

    display_incidents(manager, "After recurrence")

    print("\nDemonstration completed.")


if __name__ == "__main__":
    main()
