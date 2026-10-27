"""HTTP routes: HTML pages and a read-only JSON API."""

from __future__ import annotations

from flask import (Blueprint, abort, current_app, jsonify, make_response,
                   redirect, render_template, request, url_for)

from dashboard import params, registration
from dashboard.insights import FAILURE_TYPE_LABELS

bp = Blueprint("dashboard", __name__)


def service_choices():
    """Return (id, name) pairs for service filter drop-downs."""
    return [(s["service_id"], s["service_name"]) for s in _repo().list_service_options()]


def _repo():
    """Return the application's repository."""
    return current_app.extensions["dashboard_repo"]


def _refresh_seconds() -> int:
    """Return the configured browser refresh interval."""
    return current_app.extensions["dashboard_config"].refresh_seconds


def page(template: str, **context):
    """Render a full page, or only its live region for ``?_partial=1``."""
    layout = "_partial.html" if request.args.get("_partial") == "1" else "base.html"
    return render_template(template, layout=layout,
                           refresh_seconds=_refresh_seconds(), **context)


# --------------------------------------------------------------- filters
def health_filters(args):
    """Validate health-history query parameters."""
    filters = {
        "service_id": params.integer(args, "service_id"),
        "state": params.choice(args, "state", params.HEALTH_STATES),
        "category": params.choice(args, "category", params.CHECK_CATEGORIES),
        "status_code": params.integer(args, "status_code", 100, 599),
        "error_type": params.error_type(args),
        "range": params.choice(args, "range", params.RANGES, "24h", upper=False),
    }
    return filters, *params.pagination(args)


def incident_filters(args):
    """Validate incident query parameters."""
    filters = {
        "severity": params.choice(args, "severity", params.SEVERITIES),
        "status": params.choice(args, "status", params.INCIDENT_STATUSES),
        "category": params.choice(args, "category", params.INCIDENT_CATEGORIES),
        "service_id": params.integer(args, "service_id"),
        "date_from": params.day(args, "date_from"),
        "date_to": params.day(args, "date_to"),
        "q": params.text(args, "q"),
        "sort": params.choice(args, "sort", params.INCIDENT_SORTS, "priority", upper=False),
        "range": params.choice(args, "range", params.RANGES, "all", upper=False),
    }
    if (filters["date_from"] and filters["date_to"]
            and filters["date_from"] > filters["date_to"]):
        raise params.ValidationError("'date_from' must not be after 'date_to'.")
    return filters, *params.pagination(args)


def chart_range(args) -> str:
    """Validate a chart time range."""
    return params.choice(args, "range", params.CHART_RANGES, "24h", upper=False)


def service_filters(args):
    """Validate service-list query parameters."""
    return {
        "q": params.text(args, "q"),
        "state": params.choice(args, "state", params.SERVICE_STATES),
        "sort": params.choice(args, "sort", params.SERVICE_SORTS, "name", upper=False),
        "order": params.choice(args, "order", ("asc", "desc"), "asc", upper=False),
    }, *params.pagination(args)


# ----------------------------------------------------------------- pages
@bp.get("/")
def overview():
    """Operational overview: counts, charts, recent activity, insights."""
    repo, rng = _repo(), chart_range(request.args)
    summary = repo.get_summary()
    counts = summary["counts"]
    donut = {"items": [
        {"label": "Healthy", "value": counts["healthy"], "tone": "ok"},
        {"label": "Degraded", "value": counts["degraded"], "tone": "warn"},
        {"label": "Unhealthy", "value": counts["unhealthy"], "tone": "bad"},
        {"label": "Stale", "value": counts["stale"], "tone": "neutral"},
        {"label": "No data", "value": counts["no_data"], "tone": "neutral"}]}
    return page(
        "dashboard.html", summary=summary, range_key=rng, donut=donut,
        response_chart=repo.response_time_series(rng),
        history_chart=repo.history_buckets(rng),
        incident_chart=repo.incident_distribution(),
        insights=repo.get_insights(summary),
        application=repo.get_application(summary),
        availability_chart=repo.availability_by_service(rng),
        transitions=repo.get_transitions(rng, limit=8),
        recent_checks=repo.list_health_checks({"range": "all"}, 1, 8)["items"],
        recent_incidents=repo.list_incidents({"sort": "priority"}, 1, 6)["items"],
        totals=repo.incident_totals())


def _render_services(errors=None, form=None, status=200, csrf=None):
    """Render the services page, optionally with form errors."""
    filters, page_no, per_page = service_filters(request.args)
    config = current_app.extensions["dashboard_config"]
    summary = _repo().get_summary()
    items = _repo().filter_services(
        summary["services"], filters["q"], filters["state"], filters["sort"],
        filters["order"] == "desc")
    meta = _paginate_list(items, page_no, per_page)
    registered = None
    reg_id = params.integer(request.args, "registered")
    if reg_id is not None:
        registered = next((s for s in summary["services"] if s["service_id"] == reg_id), None)
    token = csrf or request.cookies.get(CSRF_COOKIE) or registration.new_csrf_token()
    response = make_response(page(
        "services.html", summary=summary, items=meta.pop("items"), pg=meta,
        states=params.SERVICE_STATES, sorts=params.SERVICE_SORTS,
        registration_enabled=config.registration_enabled, csrf_token=token,
        form_errors=errors or [], form=form or {}, registered=registered))
    response.status_code = status
    if config.registration_enabled and request.cookies.get(CSRF_COOKIE) != token:
        response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="Strict",
                            secure=request.is_secure, path="/")
    return response


CSRF_COOKIE = "csrf_token"


@bp.get("/services")
def services():
    """Service inventory with search, filter, sort and pagination."""
    return _render_services()


@bp.post("/services/add")
def add_service():
    """Register an additional service (only when explicitly enabled)."""
    config = current_app.extensions["dashboard_config"]
    if not config.registration_enabled:
        abort(403)
    cookie = request.cookies.get(CSRF_COOKIE)
    if not registration.tokens_match(cookie, request.form.get("csrf_token")):
        abort(403)
    form = {"name": request.form.get("name", "")[:200], "url": request.form.get("url", "")[:2100]}
    if not registration.tokens_match(config.admin_token, request.form.get("admin_token")):
        return _render_services(["The admin token is incorrect."], form, 403, cookie)
    try:
        result = registration.register_service(config, form["name"], form["url"])
    except registration.RegistrationError as error:
        return _render_services(error.messages, form, error.status, cookie)
    return redirect(url_for("dashboard.services", registered=result["service_id"]), code=303)


def _paginate_list(items, page_no, per_page):
    """Paginate an in-memory list with the repository's metadata shape."""
    from dashboard.services import _paginate
    meta = _paginate(len(items), page_no, per_page)
    meta["items"] = items[meta["offset"]:meta["offset"] + per_page]
    return meta


@bp.get("/services/<int:service_id>")
def service_detail(service_id: int):
    """Details, history chart, incidents and diagnosis for one service."""
    rng = chart_range(request.args)
    detail = _repo().get_service_detail(service_id)
    if detail is None:
        abort(404)
    return page("service_detail.html", d=detail, range_key=rng,
                response_chart=_repo().response_time_series(rng, service_id),
                history_chart=_repo().history_buckets(rng, service_id))


@bp.get("/health")
def health():
    """Filterable, paginated health-check history."""
    filters, page_no, per_page = health_filters(request.args)
    result = _repo().list_health_checks(filters, page_no, per_page)
    rng = filters["range"] if filters["range"] in params.CHART_RANGES else "7d"
    return page("health.html", result=result, pg=result, chart_range_key=rng,
                response_chart=_repo().response_time_series(rng, filters["service_id"]),
                history_chart=_repo().history_buckets(rng, filters["service_id"]),
                availability_chart=_repo().availability_by_service(rng),
                transitions=_repo().get_transitions(rng, filters["service_id"], limit=30),
                service_options=service_choices(),
                ranges=tuple(params.RANGES), states=params.HEALTH_STATES,
                categories=params.CHECK_CATEGORIES, failure_labels=FAILURE_TYPE_LABELS)


@bp.get("/incidents")
def incidents():
    """Filterable, paginated incident list."""
    filters, page_no, per_page = incident_filters(request.args)
    result = _repo().list_incidents(filters, page_no, per_page)
    return page("incidents.html", result=result, pg=result,
                totals=_repo().incident_totals(),
                service_options=service_choices(),
                ranges=tuple(params.RANGES),
                severities=params.SEVERITIES, statuses=params.INCIDENT_STATUSES,
                categories=params.INCIDENT_CATEGORIES, sorts=params.INCIDENT_SORTS)


@bp.get("/incidents/<incident_id>")
def incident_detail(incident_id: str):
    """One incident with lifecycle, evidence and diagnosis."""
    detail = _repo().get_incident(params.identifier(incident_id))
    if detail is None:
        abort(404)
    return page("incident_detail.html", d=detail)


@bp.get("/diagnosis")
def diagnosis():
    """Diagnoses for each service, separating facts from inference."""
    summary = _repo().get_summary()
    return page("diagnosis.html", summary=summary,
                diagnoses=_repo().get_diagnoses(summary),
                insights=_repo().get_insights(summary))


@bp.get("/events")
def events():
    """Incident lifecycle event history."""
    args = request.args
    filters = {"event_type": params.choice(args, "event_type", params.EVENT_TYPES),
               "service_id": params.integer(args, "service_id")}
    page_no, per_page = params.pagination(args)
    result = _repo().list_events(filters, page_no, per_page)
    return page("events.html", result=result, pg=result,
                service_options=service_choices(),
                event_types=params.EVENT_TYPES)


@bp.get("/settings")
def settings_alias():
    """Alias for the settings page."""
    return redirect(url_for("dashboard.system"), code=301)


@bp.get("/system")
def system():
    """Monitoring status, configuration and record counts."""
    summary = _repo().get_summary()
    from dashboard.insights import incident_rules
    return page("system.html", summary=summary, info=_repo().get_system_info(),
                application=_repo().get_application(summary),
                rules=incident_rules(),
                config=current_app.extensions["dashboard_config"])


# ------------------------------------------------------------------ API
@bp.get("/api/summary")
def api_summary():
    """Return the dashboard summary as JSON."""
    return jsonify(_repo().get_summary())


@bp.get("/api/services")
def api_services():
    """Return the filtered service list as JSON."""
    filters, page_no, per_page = service_filters(request.args)
    items = _repo().filter_services(
        _repo().get_summary()["services"], filters["q"], filters["state"],
        filters["sort"], filters["order"] == "desc")
    return jsonify(_paginate_list(items, page_no, per_page))


@bp.get("/api/services/<int:service_id>")
def api_service(service_id: int):
    """Return one service's detail as JSON."""
    detail = _repo().get_service_detail(service_id)
    if detail is None:
        abort(404)
    return jsonify(detail)


@bp.get("/api/health-checks")
def api_health_checks():
    """Return paginated health checks as JSON."""
    filters, page_no, per_page = health_filters(request.args)
    return jsonify(_repo().list_health_checks(filters, page_no, per_page))


@bp.get("/api/incidents")
def api_incidents():
    """Return paginated incidents as JSON."""
    filters, page_no, per_page = incident_filters(request.args)
    return jsonify(_repo().list_incidents(filters, page_no, per_page))


@bp.get("/api/incidents/<incident_id>")
def api_incident(incident_id: str):
    """Return one incident as JSON."""
    detail = _repo().get_incident(params.identifier(incident_id))
    if detail is None:
        abort(404)
    return jsonify(detail)


@bp.get("/api/charts/response-times")
def api_chart_response():
    """Return response-time series for charts."""
    return jsonify(_repo().response_time_series(
        chart_range(request.args), params.integer(request.args, "service_id")))


@bp.get("/api/charts/history")
def api_chart_history():
    """Return check counts per time bucket for charts."""
    return jsonify(_repo().history_buckets(
        chart_range(request.args), params.integer(request.args, "service_id")))


@bp.get("/api/charts/incidents")
def api_chart_incidents():
    """Return incident distribution for charts."""
    return jsonify(_repo().incident_distribution())


@bp.get("/api/application")
def api_application():
    """Return application-level status, availability and response summary."""
    return jsonify(_repo().get_application(_repo().get_summary()))


@bp.get("/api/transitions")
def api_transitions():
    """Return per-check health transitions (failure started / recovered)."""
    rng = params.choice(request.args, "range", params.RANGES, "24h", upper=False)
    return jsonify(_repo().get_transitions(rng, params.integer(request.args, "service_id")))


@bp.get("/api/charts/availability")
def api_chart_availability():
    """Return per-service availability over a time range."""
    return jsonify(_repo().availability_by_service(chart_range(request.args)))
