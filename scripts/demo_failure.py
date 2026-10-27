"""Controlled failure-and-recovery demonstration (local only).

Starts a small HTTP target bound to 127.0.0.1, then runs the real
SystemSupervisor against it while the target moves through these phases:
healthy -> slow -> slower -> healthy -> HTTP 503 -> stopped (connection
refused) -> restarted -> healthy. Results are written to a separate demo
database, so production data and AWS resources are never touched.

Usage:  python scripts/demo_failure.py --database data/demo.db --interval 2
Then:   SUPERVISOR_DB_PATH=data/demo.db flask --app dashboard.app:create_app run
"""

import argparse
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitoring.database_manager import DatabaseManager  # noqa: E402
from monitoring.supervisor import SupervisorConfig, SystemSupervisor  # noqa: E402

PHASES = [  # (label, mode, number of checks)
    ("Normal operation", "ok", 4),
    ("Service becomes slow (~0.7 s)", "slow", 3),
    ("Service very slow (~1.2 s)", "slower", 2),
    ("Service recovers", "ok", 3),
    ("Service returns HTTP 503", "down", 3),
    ("Service stopped (connection refused)", "stopped", 3),
    ("Service restarted and recovers", "ok", 3),
]


class Target:
    """A tiny local HTTP service whose behaviour can be switched."""

    def __init__(self, port: int) -> None:
        """Remember the port and start in the healthy mode."""
        self.port, self.mode, self.server = port, "ok", None

    def start(self) -> None:
        """Start serving on 127.0.0.1 in a background thread."""
        target = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                delay = {"slow": 0.7, "slower": 1.2}.get(target.mode, 0)
                time.sleep(delay)
                self.send_response(503 if target.mode == "down" else 200)
                self.end_headers()
                self.wfile.write(b"demo target")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        """Stop serving so new connections are refused."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


def main() -> None:
    """Run the demonstration."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--database", default="data/demo.db")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    target = Target(args.port)
    target.start()
    supervisor = SystemSupervisor(
        SupervisorConfig(service_name="Demo target (simulated)",
                         url=f"http://127.0.0.1:{args.port}/",
                         interval=args.interval, timeout=3.0),
        database=DatabaseManager(Path(args.database)))
    print(f"Writing to {args.database}. This is a SIMULATED failure on a local target.")
    try:
        for label, mode, checks in PHASES:
            print(f"\n== {label}")
            if mode == "stopped":
                target.stop()
            elif target.server is None:
                target.start()
            target.mode = mode
            for _ in range(checks):
                started = time.monotonic()
                supervisor.run_once()
                time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    finally:
        target.stop()
    print("\nDemonstration finished. Incidents and events are now in the demo database.")


if __name__ == "__main__":
    main()
