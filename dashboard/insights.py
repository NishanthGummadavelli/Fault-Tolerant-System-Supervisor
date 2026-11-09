"""Pure functions that explain stored monitoring data.

Nothing here touches the database or the network. Diagnoses are produced by
the project's own ``diagnose_health`` function, so the dashboard never keeps a
second set of rules or thresholds. Incident explanations restate the
IncidentManager's own detection limits, read from its constructor defaults.
"""

from __future__ import annotations

import inspect
import re
import statistics
from datetime import datetime

from monitoring.health_monitor import (
    HealthCheckResult,
    HealthStatus,
    MonitoringConfig,
    diagnose_health,
)
from monitoring.incident_manager import IncidentManager

MAX_TEXT = 300


def _clip(value, limit=MAX_TEXT):
    """Shorten long error text for display."""
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= limit else value[: limit - 1] + "\u2026"


def incident_rules() -> dict:
    """Return the IncidentManager's default detection limits."""
    params = inspect.signature(IncidentManager.__init__).parameters
    return {
        name: params[name].default
        for name in ("degraded_limit", "failure_limit", "recovery_limit")
    }


def diagnose_check(check: dict, service: dict) -> dict:
    """Diagnose one stored check with the existing diagnosis logic.

    Returns the observed facts (taken directly from the record) separately
    from the interpretation, which is inferred and not confirmed.
    """
    config = MonitoringConfig(
        url=service["service_url"],
        timeout=float(service["timeout_seconds"]),
        interval=float(service["monitoring_interval_seconds"]),
        healthy_threshold=float(service["healthy_threshold_seconds"]),
        unhealthy_threshold=float(service["unhealthy_threshold_seconds"]),
    )
    result = HealthCheckResult(
        timestamp=check["timestamp"],
        url=service["service_url"],
        health=HealthStatus(check["health_status"]),
        status_code=check["status_code"],
        response_time_seconds=float(check["response_time_seconds"]),
        error=check["error"],
        error_type=check["error_type"],
    )
    # Prefer the diagnosis saved by the supervisor; recompute only for older
    # records created before diagnosis persistence was introduced.
    stored_diagnosis = check.get("diagnosis")
    source = ("recorded by the supervisor at check time" if stored_diagnosis
              else "recomputed from the stored check (older record)")
    if stored_diagnosis:
        verdict = type("StoredDiagnosis", (), {
            "diagnosis": stored_diagnosis,
            "likely_cause": check.get("likely_cause") or "Not recorded.",
            "recommendation": check.get("recommendation") or "Continue monitoring.",
        })()
    else:
        verdict = diagnose_health(result, config)

    observed = [f"Recorded health state: {check['health_status']}."]
    if check["status_code"] is None:
        observed.append("No HTTP response was received.")
    else:
        observed.append(f"HTTP status code: {check['status_code']}.")
        observed.append(
            f"Response time: {check['response_time_seconds']:.3f} s "
            f"(healthy below {config.healthy_threshold:g} s, unhealthy at "
            f"or above {config.unhealthy_threshold:g} s)."
        )
    if check["error_type"]:
        observed.append(f"Error type: {_clip(check['error_type'], 64)}.")
    if check["error"]:
        observed.append(f"Error message: {_clip(check['error'])}")

    return {
        "checked_at": check["timestamp"],
        "observed": observed,
        "diagnosis": verdict.diagnosis,
        "likely_cause": verdict.likely_cause,
        "recommendation": verdict.recommendation,
        "source": source,
        "failure_type": failure_type(check["status_code"], check["health_status"],
                                     check["error_type"]),
    }


def explain_incident(incident: dict) -> dict:
    """Explain why an incident exists, using the manager's real rules."""
    rules = incident_rules()
    category, severity = incident["category"], incident["severity"]

    if category == "SERVICE_UNAVAILABLE":
        opened = (
            f"Opened after {rules['failure_limit']} consecutive checks "
            "with no HTTP response or a non-2xx status code."
        )
    elif severity == "CRITICAL":
        opened = (
            "Latency incident that reached CRITICAL: response time met the "
            "unhealthy threshold while sustained degradation was recorded "
            "or repeated."
        )
    else:
        opened = (
            f"Opened after {rules['degraded_limit']} consecutive checks "
            "at or above the healthy response-time threshold."
        )

    if incident["status"] == "RESOLVED":
        closing = (
            f"Resolved after {rules['recovery_limit']} consecutive "
            "healthy checks."
        )
    else:
        closing = (
            f"Stays open until {rules['recovery_limit']} consecutive "
            "healthy checks are recorded."
        )

    occurrences = incident["occurrences"]
    grouping = (
        f"{occurrences} recorded occurrence(s) are grouped under this one "
        "incident instead of raising separate alerts."
        if occurrences > 1 else
        "No repeat occurrences recorded beyond the initial detection."
    )
    return {
        "why_opened": opened,
        "closing": closing,
        "grouping": grouping,
        "rules_note": (
            "Rules shown are the IncidentManager defaults; the supervisor "
            "does not override them."
        ),
    }


def build_insights(
    services: list[dict],
    latency_samples: dict[int, list[float]],
    resolved_recent: list[dict],
    opened_recent: list[dict],
    open_incidents: int,
    now: datetime,
) -> list[dict]:
    """Derive observations, each backed by stored records."""
    items: list[dict] = []
    by_id = {s["service_id"]: s for s in services}

    for s in services:
        name = s["name"]
        base = {"service_id": s["service_id"]}
        if s["state"] == "UNHEALTHY":
            items.append({**base, "tone": "bad", "title": f"{name} is unhealthy",
                "evidence": f"Latest check at {s['last_check_at']} was recorded UNHEALTHY"
                            + (f" (HTTP {s['status_code']})." if s["status_code"] is not None
                               else " with no HTTP response.")})
        elif s["state"] == "DEGRADED":
            items.append({**base, "tone": "warn", "title": f"{name} is degraded",
                "evidence": f"Latest check at {s['last_check_at']} was recorded DEGRADED."})
        elif s["state"] == "STALE":
            age = s["age_seconds"]
            items.append({**base, "tone": "warn", "title": f"No recent data for {name}",
                "evidence": (f"Latest check is {int(age)} s old; expected within "
                             f"{int(s['stale_after_seconds'])} s." if age is not None else
                             "The latest check timestamp could not be read.")
                            + " Its current health is unverified."})
        elif s["state"] == "NO_DATA":
            items.append({**base, "tone": "info", "title": f"{name} has no health checks",
                "evidence": "The service is registered but no check has been recorded."})

        samples = latency_samples.get(s["service_id"], [])
        if len(samples) >= 5 and s["state"] in ("HEALTHY", "DEGRADED", "UNHEALTHY"):
            median = statistics.median(samples)
            if median >= s["healthy_threshold_seconds"]:
                items.append({**base, "tone": "warn",
                    "title": f"{name} responds slowly",
                    "evidence": f"Median of the last {len(samples)} successful responses is "
                                f"{median:.3f} s, at or above its {s['healthy_threshold_seconds']:g} s "
                                "healthy threshold."})

    for inc in opened_recent:
        items.append({"tone": "bad" if inc["severity"] == "CRITICAL" else "warn",
            "service_id": inc["service_id"], "incident_id": inc["incident_id"],
            "title": f"New {inc['severity'].lower()} incident {inc['incident_id']}",
            "evidence": f"{inc['category'].replace('_', ' ').title()} incident opened at "
                        f"{inc['created_at']} and is still open."})

    for inc in resolved_recent:
        svc = by_id.get(inc["service_id"])
        if svc and svc["state"] == "HEALTHY":
            items.append({"tone": "ok", "service_id": inc["service_id"],
                "incident_id": inc["incident_id"],
                "title": f"{svc['name']} recovered",
                "evidence": f"Incident {inc['incident_id']} was resolved at {inc['resolved_at']} "
                            "and the latest check is HEALTHY."})

    if (not items and services and open_incidents == 0
            and all(s["state"] == "HEALTHY" for s in services)):
        items.append({"tone": "ok", "service_id": None,
            "title": "All services healthy",
            "evidence": f"All {len(services)} service(s) reported HEALTHY within their "
                        "expected reporting window, with no open incidents."})
    return items


# ----------------------------------------------------------------------
# Infrastructure roles, failure types and overall application status
# ----------------------------------------------------------------------
ROLE_LABELS = {"load_balancer": "Load balancer", "backend": "Backend server",
               "other": "Service"}
ROLE_ORDER = {"load_balancer": 0, "backend": 1, "other": 2}

FAILURE_TYPE_LABELS = {
    "TIMEOUT": "Request timeout",
    "CONNECTION_REFUSED": "Connection refused",
    "DNS_FAILURE": "DNS resolution failure",
    "NETWORK_ERROR": "Network or connection error",
    "HTTP_4XX": "HTTP 4xx response",
    "HTTP_5XX": "HTTP 5xx response",
    "UNEXPECTED_STATUS": "Unexpected HTTP status",
    "SLOW_RESPONSE": "Excessive response time",
    "UNKNOWN": "Unclassified failure",
}


def failure_type(status_code, health_status, error_type) -> str | None:
    """Classify a stored check into a diagnostic failure type.

    Returns None for a healthy check. Only recorded fields are used.
    """
    if health_status == "HEALTHY":
        return None
    by_error = {"timeout": "TIMEOUT", "connection_refused": "CONNECTION_REFUSED",
                "dns_failure": "DNS_FAILURE", "connection_error": "NETWORK_ERROR"}
    if error_type in by_error:
        return by_error[error_type]
    if status_code is None:
        return "UNKNOWN"
    if 400 <= status_code < 500:
        return "HTTP_4XX"
    if 500 <= status_code < 600:
        return "HTTP_5XX"
    if not 200 <= status_code < 300:
        return "UNEXPECTED_STATUS"
    return "SLOW_RESPONSE"


def resolve_role(name: str, configured: tuple) -> tuple[str, str]:
    """Return (role, source): explicit configuration first, else the name."""
    folded = name.strip().casefold()
    for configured_name, role in configured:
        if configured_name == folded:
            return role, "configured"
    if "load balancer" in folded or "load-balancer" in folded or re.search(r"\balb\b", folded):
        return "load_balancer", "inferred from name"
    if "backend" in folded:
        return "backend", "inferred from name"
    return "other", "not classified"


def application_status(services: list[dict]) -> dict:
    """Combine ALB and backend states into one application-level verdict.

    Uses only services whose role is load_balancer or backend, and only
    fresh states count as evidence: STALE and NO_DATA are "unverified",
    never healthy. A healthy ALB never proves that every backend is healthy.
    """
    albs = [s for s in services if s.get("role") == "load_balancer"]
    backends = [s for s in services if s.get("role") == "backend"]

    def names(items, state):
        return [s["name"] for s in items if s["state"] == state]

    def result(code, tone, title, detail):
        return {"code": code, "tone": tone, "title": title, "detail": detail,
                "alb": [{"name": s["name"], "state": s["state"]} for s in albs],
                "backends": [{"name": s["name"], "state": s["state"]} for s in backends]}

    if not services:
        return result("UNKNOWN", "info", "Status unknown",
                      "No services are registered, so application status cannot be determined.")
    if not albs and not backends:
        return result("NO_ROLES", "info", "Application status not available",
                      "No load balancer or backend roles were found. Name services accordingly or set "
                      "DASHBOARD_SERVICE_ROLES; individual service states are shown below.")

    evidence = albs + backends
    fresh = [s for s in evidence if s["state"] in ("HEALTHY", "DEGRADED", "UNHEALTHY")]
    if not fresh:
        if any(s["state"] == "STALE" for s in evidence):
            return result("STALE", "warn", "Status stale",
                          "No monitored target has reported recently, so the application's current status "
                          "cannot be verified. The supervisor may not be running.")
        return result("UNKNOWN", "info", "Status unknown",
                      "The monitored targets have never been checked.")

    b_unhealthy, b_healthy = names(backends, "UNHEALTHY"), names(backends, "HEALTHY")
    b_degraded = names(backends, "DEGRADED")
    b_unknown = [s["name"] for s in backends if s["state"] in ("STALE", "NO_DATA")]
    alb_state = albs[0]["state"] if albs else None
    alb_note = (f" The load balancer endpoint check is {alb_state.replace('_', ' ').lower()}."
                if albs else "")

    if backends and len(b_unhealthy) == len(backends):
        return result("OUTAGE", "bad", "Application outage",
                      f"All {len(backends)} backend server(s) are unhealthy: {', '.join(b_unhealthy)}."
                      + alb_note)
    if alb_state == "UNHEALTHY":
        return result("ENDPOINT_DOWN", "bad", "Application endpoint unavailable",
                      "The load balancer endpoint is failing its health check from the supervisor's "
                      "perspective." + (f" Unhealthy backends: {', '.join(b_unhealthy)}." if b_unhealthy else ""))
    if b_unhealthy and not b_healthy:
        return result("AT_RISK", "bad", "Backend failure, remaining capacity unverified",
                      f"{', '.join(b_unhealthy)} unhealthy; the other backend(s) have no recent data."
                      + alb_note)
    if b_unhealthy:
        return result("DEGRADED_REDUNDANCY", "warn", "Degraded redundancy",
                      f"{', '.join(b_unhealthy)} unhealthy while {len(b_healthy)} backend(s) remain healthy. "
                      "The application may remain available through the remaining backend(s)." + alb_note)
    if b_degraded or alb_state == "DEGRADED":
        slow = b_degraded + (names(albs, "DEGRADED") if alb_state == "DEGRADED" else [])
        return result("DEGRADED_PERFORMANCE", "warn", "Degraded performance",
                      f"Slow responses from {', '.join(slow)}.")
    if b_unknown:
        return result("PARTIAL_UNKNOWN", "warn", "Partly unverified",
                      f"No recent data for {', '.join(b_unknown)}; remaining targets look healthy.")
    if albs and alb_state in ("STALE", "NO_DATA"):
        return result("ENDPOINT_UNVERIFIED", "warn", "Endpoint unverified",
                      "Backends are healthy, but the load balancer endpoint has no recent check.")
    if albs:
        return result("OPERATIONAL", "ok", "Application operational",
                      f"The load balancer endpoint and all {len(backends)} backend server(s) are healthy.")
    return result("BACKENDS_HEALTHY", "ok", "Backends healthy",
                  f"All {len(backends)} backend server(s) are healthy. No load balancer is monitored, "
                  "so end-to-end availability is not verified.")
