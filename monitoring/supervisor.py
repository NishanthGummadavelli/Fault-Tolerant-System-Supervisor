import argparse
import logging
import time
import uuid

from dataclasses import dataclass

from monitoring.database_manager import DatabaseManager
from monitoring.health_monitor import HealthMonitor, MonitoringConfig
from monitoring.incident_manager import IncidentManager


@dataclass
class SupervisorConfig:
    """Store supervisor configuration."""

    service_name: str = "Application Service"
    url: str = "http://127.0.0.1/"
    timeout: float = 3.0
    interval: float = 10.0
    healthy_threshold: float = 0.5
    unhealthy_threshold: float = 1.0


class SystemSupervisor:
    """Coordinate health checks, incident management, and persistence."""

    def __init__(self, config, database=None):
        """Initialize monitoring components and register the service."""
        if not config.service_name.strip():
            raise ValueError("Service name cannot be empty.")
        if config.interval < 1:
            raise ValueError("Interval must be at least 1 second.")
        if config.timeout <= 0:
            raise ValueError("Timeout must be positive.")
        if not 0 < config.healthy_threshold < config.unhealthy_threshold:
            raise ValueError("Thresholds must satisfy 0 < healthy < unhealthy.")

        self.config = config
        self.run_id = uuid.uuid4().hex[:12]
        self.database = database if database is not None else DatabaseManager()
        self.database.initialize_database()

        monitor_config = MonitoringConfig(
            url=config.url,
            timeout=config.timeout,
            interval=config.interval,
            healthy_threshold=config.healthy_threshold,
            unhealthy_threshold=config.unhealthy_threshold,
        )
        self.health_monitor = HealthMonitor(monitor_config)
        self.incident_manager = IncidentManager(
            healthy_threshold=config.healthy_threshold,
            unhealthy_threshold=config.unhealthy_threshold,
        )

        self.service_id = self.database.register_service(
            service_name=config.service_name,
            service_url=config.url,
            timeout_seconds=config.timeout,
            monitoring_interval_seconds=int(config.interval),
            healthy_threshold_seconds=config.healthy_threshold,
            unhealthy_threshold_seconds=config.unhealthy_threshold,
        )

        self.persisted_incidents = {}
        self.incident_id_map = {}

        logging.info(
            "Supervisor initialized for %s; service ID=%s",
            config.service_name,
            self.service_id,
        )

    @staticmethod
    def _incident_signature(incident):
        """Create a signature to detect incident changes."""
        return (
            incident.category.value,
            incident.severity.value,
            incident.status.value,
            incident.summary,
            incident.occurrences,
            incident.last_updated_at,
            incident.resolved_at,
        )

    def _persistent_id(self, incident):
        """Generate a database ID unique to this supervisor run."""
        internal_id = incident.incident_id
        if internal_id not in self.incident_id_map:
            self.incident_id_map[internal_id] = (
                f"SUP-{self.run_id}-{internal_id}"
            )
        return self.incident_id_map[internal_id]

    def _persist_incidents(self):
        """Insert new incidents and update existing incident records."""
        for incident in self.incident_manager.get_incident_history():
            internal_id = incident.incident_id
            persistent_id = self._persistent_id(incident)
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

    def run_once(self):
        """Execute one health check and persist its results."""
        result, diagnosis = self.health_monitor.run_once()
        self.database.save_health_check(self.service_id, result)

        events = self.incident_manager.process_result(result)
        self._persist_incidents()

        for event in events:
            logging.warning("Incident event: %s", event)

        logging.info(
            "Cycle complete: health=%s, diagnosis=%s",
            result.health.value,
            diagnosis.diagnosis,
        )

    def run_forever(self):
        """Repeat monitoring until interrupted."""
        logging.info(
            "Continuous monitoring interval: %s seconds",
            self.config.interval,
        )

        while True:
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                logging.exception("Monitoring cycle failed.")

            elapsed = time.monotonic() - started
            time.sleep(max(0.0, self.config.interval - elapsed))


def build_parser():
    """Create the supervisor command-line parser."""
    parser = argparse.ArgumentParser(
        description="Fault-Tolerant System Supervisor"
    )
    parser.add_argument("--url", default="http://127.0.0.1/")
    parser.add_argument("--name", default="Application Service")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--healthy-threshold", type=float, default=0.5)
    parser.add_argument("--unhealthy-threshold", type=float, default=1.0)
    return parser


def main():
    """Start the supervisor using command-line configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    args = build_parser().parse_args()

    try:
        config = SupervisorConfig(
            service_name=args.name,
            url=args.url,
            timeout=args.timeout,
            interval=args.interval,
            healthy_threshold=args.healthy_threshold,
            unhealthy_threshold=args.unhealthy_threshold,
        )
        SystemSupervisor(config).run_forever()
    except KeyboardInterrupt:
        logging.info("Supervisor stopped by user.")
    except (ValueError, OSError) as error:
        logging.error("Supervisor could not start: %s", error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
