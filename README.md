# Fault-Tolerant System Supervisor

## Team CodeCrafters

A cloud-based fault-tolerant system designed to monitor application
health, detect infrastructure failures, and maintain application
availability using AWS cloud services.

## Project Objective

The objective of this project is to design and implement a cloud-based
system that can monitor application infrastructure, detect unhealthy
resources, distribute traffic across healthy resources, and recover
from failures with minimal service interruption.

## Target Architecture

The planned system will use AWS cloud services and infrastructure
components including:

- Amazon VPC
- Amazon EC2
- Application Load Balancer
- Auto Scaling
- Amazon CloudWatch
- Amazon RDS

## Development Approach

The project will be developed incrementally.

### Phase 1 — Project Foundation

- Project repository
- Git/GitHub integration
- Project structure
- Documentation

### Phase 2 — Cloud Foundation

- AWS networking
- VPC
- Subnets
- Route tables
- Security groups
- EC2

### Phase 3 — Fault-Tolerance Infrastructure

- Multiple EC2 instances
- Application Load Balancer
- Health checks
- Auto Scaling
- CloudWatch monitoring

### Phase 4 — Application Integration

- Application deployment
- Database integration
- Health/status information
- Monitoring integration

### Phase 5 — Failure and Recovery Demonstration

- Simulate instance failure
- Detect unhealthy instance
- Redirect traffic
- Replace failed instance
- Verify application availability

## Project Status

The multi-target monitoring core, SQLite health-history and diagnosis persistence,
incident lifecycle tracking and the Flask dashboard are implemented and covered by
automated tests (see "Tests" below). The end-to-end check against the real AWS backends
and ALB has **not** been performed; it needs the actual reachable endpoints. The dashboard
layout has been verified through rendered HTML and automated tests, not yet by eye in a browser.

## Independent Multi-Target Monitoring

The supervisor can monitor the two EC2 backends and the Application Load Balancer
in the same cycle. The ALB remains responsible for traffic routing; the supervisor
observes health, stores check evidence and diagnosis in SQLite, and manages incidents.
It does not stop/restart EC2 instances or change ALB target registrations.

### Configure targets

Run from the repository root. Replace the example URLs with the actual reachable
backend URLs or private IP URLs when the supervisor runs inside the VPC:

```bash
python -m monitoring.supervisor --interval 10 \
  --target "Backend Server 1=http://BACKEND_1_HOST/" \
  --target "Backend Server 2=http://BACKEND_2_HOST/" \
  --target "Application Load Balancer=http://YOUR_ALB_DNS_NAME/"
```

The `--target` option can be repeated. If no `--target` is supplied, the original
`--url` and `--name` single-target mode remains available:

```bash
python -m monitoring.supervisor --url http://127.0.0.1:8000/ \
  --name "Local Test Service" --interval 10
```

Each target is registered as a separate service in the existing SQLite database.
Every check stores its timestamp, health status, HTTP status, response time, error
classification, diagnosis, likely cause, and recommended action. Older databases are
migrated automatically by adding the three nullable diagnosis columns; existing rows
and the existing four-table layout are retained.

### Incident lifecycle

- Repeated availability failures open a critical incident.
- Sustained elevated latency opens a warning; persistent severe latency can escalate it.
- Further matching failures update the same open incident and increase its occurrence count.
- Two consecutive healthy checks confirm recovery and resolve open incidents.
- Creation, updates, escalation, and resolution are recorded in `incident_events`.

### Network requirements

The supervisor must be able to reach each configured URL. If it runs on an EC2 instance
inside the VPC, use backend private IPs/URLs and ensure security-group rules allow the
supervisor to reach Apache on port 80. The ALB URL should be the DNS name assigned by AWS.
Do not expose the local Flask dashboard publicly without adding suitable authentication
and network access controls.

### Diagnosis categories

The monitor records evidence and a best-effort diagnosis for each check:

- **Request timeout** — the endpoint did not respond before the configured timeout.
- **Connection refused** — the destination did not accept the connection; verify Apache/application status and port rules.
- **Connection failure** — likely DNS, routing, security-group, firewall, or endpoint configuration issue.
- **HTTP client/request error (4xx)** — inspect the URL, resource path, and access rules.
- **HTTP server error (5xx)** — inspect Apache/application logs and server dependencies.
- **Elevated/excessive response latency** — investigate load, processing time, and network latency.

These are likely causes inferred from the observed response, not proof of the underlying infrastructure fault.

### Failure classification

Besides the categories above, a hostname that cannot be resolved is now recorded as
`dns_failure` and diagnosed as "DNS resolution failure". The monitor cannot reliably tell a
connect timeout from a read timeout, so both appear as "Request timeout".

## Dashboard

A read-only Flask dashboard in `dashboard/` presents what the supervisor has stored in SQLite. It
never runs health checks, never starts the supervisor, and opens the database with `query_only`.

| Page | Contents |
|---|---|
| Overview | Application-status banner; one card per monitored target (load balancer first, then backends, then others) with status, endpoint, HTTP status, response time, last checked, last successful, data freshness and latest diagnosis; summary metrics; charts; insights; failures and recoveries; recent checks and incidents |
| Services | Search, state filter, sorting, pagination; role, state and open incidents; per-service detail; optional Add Service form |
| Health history | Filters (service, state, failure type, HTTP status, error type, time range), pagination, response-time, state-count and availability charts, failure/recovery timeline |
| Incidents | Filters (status, severity, category, service, time range, dates, text), latest observed failure, event count and duration; detail page with lifecycle timeline, evidence, rules and diagnosis |
| Diagnostics | Per-service diagnosis, observed facts separated from inferred causes |
| Event history | Recorded incident lifecycle events |
| Settings | Data status, record counts, role mapping, registration status, per-service settings |

### Application status (infrastructure view)

The banner combines the load balancer and backend states. Only *fresh* checks count as evidence:
stale or never-checked targets are "unverified", never healthy. A healthy ALB response never proves
that every backend is healthy.

| Evidence | Banner |
|---|---|
| ALB and all backends healthy | Application operational |
| Some backends unhealthy, at least one healthy | Degraded redundancy (application may remain available) |
| **All** backends unhealthy | **Application outage** (the ALB's own check is reported alongside) |
| ALB check unhealthy | Application endpoint unavailable (from the supervisor's perspective) |
| A backend or the ALB slow | Degraded performance |
| Some targets without recent data | Partly unverified / endpoint unverified / at risk |
| No recent data at all | Status stale |
| Never checked or nothing registered | Status unknown |
| No load balancer target configured | "Backends healthy", end-to-end availability not verified |

Roles come from `DASHBOARD_SERVICE_ROLES` when set, otherwise from the service name ("load balancer"
or "ALB" means load balancer; "backend" means backend server). Any other service is listed as a plain
service, so additional targets appear without UI changes.

**Application availability** = HTTP 2xx checks / all checks in the last 24 hours, measured on the load
balancer endpoint. Slow but successful responses count as available. With no load balancer target no
number is shown. Per-service availability charts use the same formula over the selected range.

### Freshness

A target is **stale** when its latest check is older than `DASHBOARD_STALE_MULTIPLIER x monitoring
interval + request timeout` (default 3 x interval + timeout). Stale targets keep their last recorded state,
labelled as such. The dashboard polls its own lightweight HTML endpoints every `DASHBOARD_REFRESH_SECONDS`
(default 10); this is periodic polling of stored data, not live monitoring.

### Environment variables (no secrets in source control)

| Variable | Default | Meaning |
|---|---|---|
| `SUPERVISOR_DB_PATH` | engine default (`data/fault_tolerant_supervisor.db`) | Database the dashboard reads |
| `DASHBOARD_REFRESH_SECONDS` | 10 (2 to 300) | Browser polling interval |
| `DASHBOARD_STALE_MULTIPLIER` | 3.0 (1.5 to 100) | Staleness window |
| `DASHBOARD_SERVICE_ROLES` | none | `Name=role;Name=role` with role `alb`, `backend` or `other` |
| `DASHBOARD_ALLOW_REGISTRATION` | off | `1` allows the Add Service form (also needs the token) |
| `DASHBOARD_ADMIN_TOKEN` | none | Required to register services; never displayed or logged |
| `DASHBOARD_MAX_SERVICES` | 50 | Registration cap |

### Run locally (development)

```bash
python -m pip install -r requirements-dashboard.txt

# Terminal 1: supervisor (replace the example URLs)
python -m monitoring.supervisor --interval 10 \
  --target "Backend Server 1=http://BACKEND_1_HOST/" \
  --target "Backend Server 2=http://BACKEND_2_HOST/" \
  --target "Application Load Balancer=http://YOUR_ALB_DNS_NAME/"

# Terminal 2: dashboard, bound to localhost (debug mode is off)
flask --app "dashboard.app:create_app" run --host 127.0.0.1 --port 5000
```

Open http://127.0.0.1:5000/. Both processes use the same default database file, so no extra
configuration is needed. Do not use `--debug` on a server and do not bind the dashboard to `0.0.0.0`
unless access controls are in place: the dashboard has no login. Exposing it remotely requires at least
a security group limited to your IP, or an SSH tunnel (`ssh -L 5000:127.0.0.1:5000 user@host`), and a
production server such as gunicorn (`gunicorn --bind 127.0.0.1:8000 "dashboard.app:create_app()"`).

### Configure Backend Server 1, Backend Server 2 and the ALB

Pass the three URLs as `--target "NAME=URL"` as shown above. Use names containing "Backend" and
"Load Balancer" (or set `DASHBOARD_SERVICE_ROLES`) so the dashboard knows each role. Each target is
checked independently every cycle and has its own incidents.

### Add an additional service

*Safest way:* add another `--target "NAME=URL"` and restart the supervisor (one short gap in monitoring).

*From the dashboard (optional):* set both variables before starting the dashboard, then use Services,
"Add a service":

```bash
export DASHBOARD_ALLOW_REGISTRATION=1
export DASHBOARD_ADMIN_TOKEN='choose-a-long-random-value'     # do not commit this
```

The form validates the name and URL (http/https only, no credentials, no link-local or metadata
addresses), rejects duplicate names and endpoints, checks a CSRF token and the admin token, and writes the
service to the database through `DatabaseManager`. **That only records it.** A supervisor running this
version of the code starts monitoring a registered service at the beginning of its next cycle; the page
shows it as "awaiting first check" until its first check appears. A supervisor started from older code
will not pick it up until restarted. New services reuse the settings (interval, timeout, thresholds) of the
first registered service, and the supervisor monitors them with its own shared settings. Only services
registered from the dashboard are picked up this way; other rows in the database are never monitored
automatically. There is no AWS auto-discovery.

### Demonstrate individual failure, simultaneous failure and recovery

*Simulated, local, safe (tested):* the script runs the real supervisor against three local servers
that behave like the ALB and two backends, writing to a separate database:

```bash
python scripts/demo_infrastructure.py --database data/demo.db --interval 2
SUPERVISOR_DB_PATH=data/demo.db flask --app "dashboard.app:create_app" run --port 5000   # second terminal
```

Phases: normal; Server 1 stops (degraded redundancy, ALB still answers); Server 1 recovers; both backends
fail (application outage, two incidents, one per backend, updated not duplicated); both recover (incidents
resolve after two healthy checks). `scripts/demo_failure.py` is the earlier single-service demonstration.

*On AWS (not tested by this project's automated tests):* the supervisor never stops, starts or replaces
instances. To demonstrate on the real environment, a team member stops the web server on one backend
(for example `sudo systemctl stop httpd`), observes the dashboard, then stops it on the second, then starts
both again. Do this only on resources you own; no dashboard control performs any of it.

### Replace the old project with this version without losing data

1. Stop the supervisor and dashboard. 2. Copy the database file (`data/fault_tolerant_supervisor.db`) and
any local configuration somewhere safe. 3. Extract this ZIP over the project folder (or into a new
folder and copy `data/` back); `data/` and `*.db` are git-ignored and are not in the ZIP, so nothing is
overwritten. 4. `python -m pip install -r requirements-dashboard.txt`. 5. Start the supervisor: it
adds the new `monitoring_source` column automatically (existing rows keep the value `supervisor`; no data
is deleted). The dashboard also reads an un-migrated database correctly before that happens.

## Tests

```bash
python -m pip install -r requirements-dashboard.txt
python -m unittest discover -s tests -v
```

### Core tests only

From the repository root:

```bash
python -m unittest tests.test_database_manager tests.test_incident_manager tests.test_supervisor tests.test_stage45_integration tests.test_multi_target_supervisor tests.test_health_diagnosis -v
```

The core monitoring code uses the Python standard library. To run dashboard tests, first install the dashboard dependency:

```bash
python -m pip install -r requirements-dashboard.txt
python -m unittest discover -s tests -v
```

The dashboard test command requires Flask to be installed in the active Python environment.
