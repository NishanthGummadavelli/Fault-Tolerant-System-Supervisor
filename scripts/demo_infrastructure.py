"""Controlled infrastructure failure demonstration (local and simulated).

Runs the real SystemSupervisor against three local HTTP targets bound to
127.0.0.1 that mimic the project's layout:

    Application Load Balancer  (answers 200 while at least one backend is up)
    Backend Server 1
    Backend Server 2

Phases: normal -> Server 1 fails -> Server 1 recovers -> BOTH backends fail
(application outage) -> both recover. Results go to a separate demo database;
no AWS resource is touched, and nothing here is a real EC2/ALB failure.

    python scripts/demo_infrastructure.py --database data/demo.db --interval 2
    SUPERVISOR_DB_PATH=data/demo.db flask --app "dashboard.app:create_app" run
"""

import argparse
import contextlib
import io
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitoring.database_manager import DatabaseManager  # noqa: E402
from monitoring.supervisor import SupervisorConfig, SystemSupervisor, TargetConfig  # noqa: E402

# (label, backend 1 mode, backend 2 mode, number of checks)
PHASES = [
    ("Normal operation", "ok", "ok", 4),
    ("Backend Server 1 stops (connection refused)", "stopped", "ok", 4),
    ("Backend Server 1 recovers", "ok", "ok", 3),
    ("BOTH backends fail (Server 1 stopped, Server 2 returns HTTP 503)", "stopped", "down", 4),
    ("Both backends recover", "ok", "ok", 3),
]


class Node:
    """A local HTTP target whose behaviour can be switched."""

    def __init__(self, port, responder=None):
        """Remember the port and an optional status-code function."""
        self.port, self.mode, self.server, self.responder = port, "ok", None, responder

    def status(self):
        """Return the HTTP status this node currently answers with."""
        if self.responder:
            return self.responder()
        return 503 if self.mode == "down" else 200

    def start(self):
        """Start serving on 127.0.0.1 in a background thread."""
        node = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(node.status())
                self.end_headers()
                self.wfile.write(b"simulated target")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        """Stop serving so new connections are refused."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def apply(self, mode):
        """Switch to a mode: ok, down (HTTP 503) or stopped (refused)."""
        self.mode = mode
        if mode == "stopped":
            self.stop()
        elif self.server is None:
            self.start()


def main():
    """Run the demonstration."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--database", default="data/demo.db")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--base-port", type=int, default=8780)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    one = Node(args.base_port + 1)
    two = Node(args.base_port + 2)
    # The simulated load balancer is "up" while any backend still answers 200.
    alb = Node(args.base_port,
               responder=lambda: 200 if (one.server and one.status() == 200)
               or (two.server and two.status() == 200) else 503)
    for node in (alb, one, two):
        node.start()

    base = f"http://127.0.0.1:{args.base_port}"
    supervisor = SystemSupervisor(
        SupervisorConfig(interval=args.interval, timeout=3.0, targets=(
            TargetConfig("Backend Server 1", f"{base[:-1]}1/"),
            TargetConfig("Backend Server 2", f"{base[:-1]}2/"),
            TargetConfig("Application Load Balancer", f"{base}/"))),
        database=DatabaseManager(Path(args.database)))
    print(f"Writing to {args.database}. SIMULATED failures on local targets only.")
    try:
        for label, mode_one, mode_two, checks in PHASES:
            print(f"\n== {label}")
            one.apply(mode_one)
            two.apply(mode_two)
            for _ in range(checks):
                started = time.monotonic()
                with contextlib.redirect_stdout(io.StringIO()):
                    supervisor.run_once()
                time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    finally:
        for node in (alb, one, two):
            node.stop()
    print("\nDemonstration finished. Open the dashboard against this database.")


if __name__ == "__main__":
    main()
