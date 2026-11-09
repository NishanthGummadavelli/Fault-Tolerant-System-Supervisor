"""Flask application factory for the dashboard.

Creating the app never starts monitoring, opens the database, or creates
files. The supervisor runs as its own process and writes to the SQLite file;
this app only reads it.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request, url_for

from dashboard.config import DashboardConfig
from dashboard.insights import FAILURE_TYPE_LABELS
from dashboard.params import ValidationError
from dashboard.routes import bp
from dashboard.services import DashboardRepository, RepositoryError

TRANSITION_LABELS = {"FAILURE_STARTED": "Failure started", "RECOVERED": "Recovered",
                     "WORSENED": "Worsened", "IMPROVED": "Improved"}
STATE_META = {
    "HEALTHY": ("\u2713", "Healthy"), "DEGRADED": ("\u25b2", "Degraded"),
    "UNHEALTHY": ("\u2715", "Unhealthy"), "STALE": ("\u25f7", "Stale"),
    "NO_DATA": ("?", "No data"),
    "CRITICAL": ("\u2715", "Critical"), "WARNING": ("\u25b2", "Warning"),
    "OPEN": ("\u25cf", "Open"), "RESOLVED": ("\u2713", "Resolved"),
}
FAILURE_LABELS = {
    "CONNECTION_FAILURE": "Connection failure (no HTTP response)",
    "HTTP_ERROR": "HTTP error response", "SLOW_RESPONSE": "Slow response",
    "SUCCESS": "Successful",
}
NAV = (
    ("dashboard.overview", "Overview"), ("dashboard.services", "Services"),
    ("dashboard.health", "Health history"), ("dashboard.incidents", "Incidents"),
    ("dashboard.diagnosis", "Diagnostics"), ("dashboard.events", "Event history"),
    ("dashboard.system", "Settings"),
)


def display_time(value) -> str:
    """Format a stored ISO timestamp for display."""
    if not value:
        return "\u2014"
    return str(value).replace("T", " ").replace("+00:00", " UTC")


def display_duration(seconds) -> str:
    """Format a duration in seconds as a short human-readable string."""
    if seconds is None:
        return "\u2014"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {sec} s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} d {hours % 24} h"


def create_app(config: DashboardConfig | None = None) -> Flask:
    """Create the dashboard application."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024   # forms are tiny; reject large bodies
    config = config or DashboardConfig.from_env()
    app.extensions["dashboard_config"] = config
    app.extensions["dashboard_repo"] = DashboardRepository(config)

    app.jinja_env.filters["display_time"] = display_time
    app.jinja_env.filters["duration"] = display_duration
    app.jinja_env.globals.update(
        STATE_META=STATE_META, FAILURE_LABELS=FAILURE_LABELS, NAV=NAV,
        TRANSITION_LABELS=TRANSITION_LABELS, FAILURE_TYPE_LABELS=FAILURE_TYPE_LABELS)
    app.register_blueprint(bp)

    @app.template_global()
    def page_url(number: int) -> str:
        """Return the current URL with a different page number."""
        args = request.args.to_dict(flat=True)
        args.pop("_partial", None)
        args["page"] = number
        return url_for(request.endpoint, **(request.view_args or {}), **args)

    @app.after_request
    def add_headers(response):
        """Add security headers and disable caching of live data."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'")
        if request.path.startswith("/api/") or request.args.get("_partial") == "1":
            response.headers["Cache-Control"] = "no-store"
        return response

    def _error(status: int, title: str, message: str):
        """Return a JSON error for API paths, otherwise an HTML page."""
        if request.path.startswith("/api/"):
            return jsonify(error=title, message=message), status
        return render_template(
            "errors/error.html", layout="base.html", refresh_seconds=0,
            status=status, title=title, message=message), status

    @app.errorhandler(ValidationError)
    def bad_request(error):
        return _error(400, "Invalid request", str(error))

    @app.errorhandler(RepositoryError)
    def unavailable(_):
        return _error(503, "Data unavailable",
                      "Monitoring data could not be read. Try again shortly.")

    @app.errorhandler(403)
    def forbidden(_):
        return _error(403, "Forbidden", "This action is not permitted.")

    @app.errorhandler(413)
    def too_large(_):
        return _error(413, "Request too large", "The submitted data was too large.")

    @app.errorhandler(404)
    def not_found(_):
        return _error(404, "Not found", "That page or record does not exist.")

    @app.errorhandler(405)
    def method_not_allowed(_):
        return _error(405, "Method not allowed", "This dashboard is read-only.")

    @app.errorhandler(500)
    def server_error(_):
        return _error(500, "Server error", "Something went wrong. Please try again.")

    return app
