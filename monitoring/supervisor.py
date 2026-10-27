"""Coordinate multi-target health monitoring, diagnosis, and incident storage."""

import argparse
import logging
import time
import uuid

from dataclasses import dataclass, field

from monitoring.database_manager import DatabaseManager
from monitoring.health_monitor import (
    HealthMonitor,
    MonitoringConfig,
    validate_config,
)
from monitoring.incident_manager import IncidentManager


@dataclass(frozen=True)
class TargetConfig:
    """Describe one independently monitored HTTP/HTTPS target."""

    service_name: str
    url: str


@dataclass
class SupervisorConfig:
    """Store shared settings and one or more monitored targets."""

    service_name: str = "Application Service"
    url: str = "http://127.0.0.1/"
    timeout: float = 3.0
    interval: float = 10.0
    healthy_threshold: float = 0.5
    unhealthy_threshold: float = 1.0
    targets: tuple[TargetConfig, ...] = field(default_factory=tuple)


class SystemSupervisor:
    """Monitor each target independently and persist its evidence and incidents."""

    def __init__(self, config, database=None):
        """Validate configuration, register targets, and initialize per-target state."""
        if config.interval < 1:
            raise ValueError("Interval must be at least 1 second.")
        if config.timeout <= 0:
            raise ValueError("Timeout must be positive.")
        if not 0 < config.healthy_threshold < config.unhealthy_threshold:
            raise ValueError("Thresholds must satisfy 0 < healthy < unhealthy.")

        targets = config.targets or (TargetConfig(config.service_name, config.url),)
        if not targets:
            raise ValueError("At least one monitoring target is required.")

        seen_urls = set()
        seen_names = set()
        for target in targets:
            normalized_name = target.service_name.strip().casefold()
            if not normalized_name:
                raise ValueError("Target name cannot be empty.")
            if normalized_name in seen_names:
                raise ValueError(
                    f"Duplicate target name: {target.service_name.strip()}"
                )
            seen_names.add(normalized_name)
            monitor_config = MonitoringConfig(
                url=target.url,
                timeout=config.timeout,
                interval=config.interval,
                healthy_threshold=config.healthy_threshold,
                unhealthy_threshold=config.unhealthy_threshold,
            )
            validate_config(monitor_config)
            if target.url in seen_urls:
                raise ValueError(f"Duplicate target URL: {target.url}")
            seen_urls.add(target.url)

        self.config = config
        self.targets = tuple(targets)
        self.run_id = uuid.uuid4().hex[:12]
        self.database = database if database is not None else DatabaseManager()
        self.database.initialize_database()
        self.target_states = {}
        self.persisted_incidents = {}
        self.incident_id_map = {}
        self._skipped_urls = set()

        # Each endpoint gets its own monitor and incident counters so one
        # backend's failures or recovery cannot affect another backend.
        for target in self.targets:
            monitor_config = MonitoringConfig(
                url=target.url,
                timeout=config.timeout,
                interval=config.interval,
                healthy_threshold=config.healthy_threshold,
                unhealthy_threshold=config.unhealthy_threshold,
            )
            service_id = self.database.register_service(
                service_name=target.service_name,
                service_url=target.url,
                timeout_seconds=config.timeout,
                monitoring_interval_seconds=max(1, int(config.interval)),
                healthy_threshold_seconds=config.healthy_threshold,
                unhealthy_threshold_seconds=config.unhealthy_threshold,
            )
            self.target_states[target.url] = {
                "target": target,
                "service_id": service_id,
                "health_monitor": HealthMonitor(monitor_config),
                "incident_manager": IncidentManager(
                    healthy_threshold=config.healthy_threshold,
                    unhealthy_threshold=config.unhealthy_threshold,
                ),
            }

        # Preserve the original single-target attributes for existing callers.
        first_state = self.target_states[self.targets[0].url]
        self.service_id = first_state["service_id"]
        self.health_monitor = first_state["health_monitor"]
        self.incident_manager = first_state["incident_manager"]

        logging.info("Supervisor initialized for %d target(s).", len(self.targets))
        for target in self.targets:
            logging.info("Monitoring %s at %s", target.service_name, target.url)

    @staticmethod
    def _incident_signature(incident):
        """Create a signature to detect incident lifecycle changes."""
        return (
            incident.category.value,
            incident.severity.value,
            incident.status.value,
            incident.summary,
            incident.occurrences,
            incident.last_updated_at,
            incident.resolved_at,
        )

    def _persistent_id(self, service_id, incident):
        """Create a database-safe ID unique to this run, target, and incident."""
        key = (service_id, incident.incident_id)
        if key not in self.incident_id_map:
            self.incident_id_map[key] = (
                f"SUP-{self.run_id}-S{service_id}-{incident.incident_id}"
            )
        return self.incident_id_map[key]

    def _persist_incidents(self, state):
        """Insert and update persistent incident records for one target."""
        service_id = state["service_id"]
        manager = state["incident_manager"]

        for incident in manager.get_incident_history():
            internal_id = incident.incident_id
            persistent_id = self._persistent_id(service_id, incident)
            signature = self._incident_signature(incident)

            if persistent_id not in self.persisted_incidents:
                try:
                    incident.incident_id = persistent_id
                    self.database.save_incident(incident)
                finally:
                    incident.incident_id = internal_id
                self.persisted_incidents[persistent_id] = signature
            elif self.persisted_incidents[persistent_id] != signature:
                try:
                    incident.incident_id = persistent_id
                    self.database.update_incident(incident)
                finally:
                    incident.incident_id = internal_id
                self.persisted_incidents[persistent_id] = signature

    def sync_registered_targets(self):
        """Start monitoring services registered through the dashboard.

        Called at the start of every cycle. Only rows whose source is
        "dashboard" are considered, so services left in the database by
        earlier runs are never monitored unexpectedly. Invalid rows are
        skipped once and logged.
        """
        try:
            rows = self.database.list_services(source="dashboard")
        except Exception:
            logging.exception("Could not read dashboard-registered services.")
            return

        known_names = {t.service_name.strip().casefold() for t in self.targets}
        for row in rows:
            url = row["service_url"]
            if url in self.target_states or url in self._skipped_urls:
                continue
            name = row["service_name"].strip()
            monitor_config = MonitoringConfig(
                url=url,
                timeout=self.config.timeout,
                interval=self.config.interval,
                healthy_threshold=self.config.healthy_threshold,
                unhealthy_threshold=self.config.unhealthy_threshold,
            )
            try:
                if name.casefold() in known_names:
                    raise ValueError(f"Duplicate target name: {name}")
                validate_config(monitor_config)
            except ValueError as error:
                logging.warning("Skipping registered service %s: %s", name, error)
                self._skipped_urls.add(url)
                continue

            target = TargetConfig(name, url)
            self.target_states[url] = {
                "target": target,
                "service_id": row["service_id"],
                "health_monitor": HealthMonitor(monitor_config),
                "incident_manager": IncidentManager(
                    healthy_threshold=self.config.healthy_threshold,
                    unhealthy_threshold=self.config.unhealthy_threshold,
                ),
            }
            self.targets = self.targets + (target,)
            known_names.add(name.casefold())
            logging.info("Now monitoring dashboard-registered service %s at %s", name, url)

    def run_once(self):
        """Check every configured target and return results keyed by target name."""
        self.sync_registered_targets()
        results = {}
        for target in self.targets:
            state = self.target_states[target.url]
            try:
                result, diagnosis = state["health_monitor"].run_once()
                # Store both raw evidence and the exact diagnosis from this check.
                self.database.save_health_check(
                    state["service_id"], result, diagnosis=diagnosis
                )
                events = state["incident_manager"].process_result(result)
                self._persist_incidents(state)
                for event in events:
                    logging.warning("%s | Incident event: %s", target.service_name, event)
                logging.info(
                    "%s | health=%s | diagnosis=%s",
                    target.service_name,
                    result.health.value,
                    diagnosis.diagnosis,
                )
                results[target.service_name] = (result, diagnosis)
            except Exception:
                # Isolate unexpected monitoring/storage errors so other targets
                # still receive their check during this cycle.
                logging.exception("Monitoring cycle failed for %s", target.service_name)
                results[target.service_name] = None
        return results

    def run_forever(self):
        """Repeat all target checks at the configured interval until interrupted."""
        logging.info("Continuous monitoring interval: %s seconds", self.config.interval)
        while True:
            started = time.monotonic()
            self.run_once()
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, self.config.interval - elapsed))


def _parse_target(value):
    """Parse a CLI target in the form NAME=URL."""
    if "=" not in value:
        raise argparse.ArgumentTypeError("Target must use NAME=URL format.")
    name, url = value.split("=", 1)
    if not name.strip() or not url.strip():
        raise argparse.ArgumentTypeError("Target name and URL cannot be empty.")
    return TargetConfig(name.strip(), url.strip())


def build_parser():
    """Create the CLI parser while retaining the original single-target options."""
    parser = argparse.ArgumentParser(
        description="Fault-Tolerant System Supervisor with multi-target monitoring"
    )
    parser.add_argument("--url", default="http://127.0.0.1/", help="Legacy single target URL.")
    parser.add_argument("--name", default="Application Service", help="Legacy single target name.")
    parser.add_argument("--target", action="append", type=_parse_target, default=[],
                        help="Target to monitor; repeat as --target 'NAME=URL'.")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--healthy-threshold", type=float, default=0.5)
    parser.add_argument("--unhealthy-threshold", type=float, default=1.0)
    return parser


def main():
    """Start multi-target monitoring using command-line configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    args = build_parser().parse_args()
    config = SupervisorConfig(
        service_name=args.name,
        url=args.url,
        timeout=args.timeout,
        interval=args.interval,
        healthy_threshold=args.healthy_threshold,
        unhealthy_threshold=args.unhealthy_threshold,
        targets=tuple(args.target),
    )

    try:
        SystemSupervisor(config).run_forever()
    except KeyboardInterrupt:
        logging.info("Supervisor stopped by user.")
    except (ValueError, OSError) as error:
        logging.error("Supervisor could not start: %s", error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
