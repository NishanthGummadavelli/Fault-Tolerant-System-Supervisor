# Fault-Tolerant System Supervisor

```{=html}
<p align="center">
```
`<strong>`{=html}Cloud Computing Project \| Team
CodeCrafters`</strong>`{=html}`<br>`{=html} Monitoring · Failure
Detection · Load Balancing · Incident Recovery
```{=html}
</p>
```
```{=html}
<p align="center">
```
`<img alt="AWS" src="https://img.shields.io/badge/Cloud-AWS-orange">`{=html}
`<img alt="Python" src="https://img.shields.io/badge/Python-3.12-blue">`{=html}
`<img alt="Dashboard" src="https://img.shields.io/badge/Dashboard-Flask-black">`{=html}
`<img alt="Database" src="https://img.shields.io/badge/Database-SQLite-lightgrey">`{=html}
`<img alt="Status" src="https://img.shields.io/badge/Status-Implemented%20%26%20Tested-success">`{=html}
```{=html}
</p>
```
A cloud-based fault-tolerance demonstration deployed on **Amazon Web
Services (AWS)**. The project monitors backend health, detects service
failures, records incidents and recovery, and presents infrastructure
information through a Flask dashboard.

------------------------------------------------------------------------

## Contents

-   [Project overview](#project-overview)
-   [Objectives](#objectives)
-   [Implemented architecture](#implemented-architecture)
-   [AWS infrastructure](#aws-infrastructure)
-   [Technology stack](#technology-stack)
-   [How it works](#how-it-works)
-   [Monitoring and incident
    lifecycle](#monitoring-and-incident-lifecycle)
-   [Dashboard](#dashboard)
-   [Repository structure](#repository-structure)
-   [Run on AWS](#run-on-aws)
-   [Configuration](#configuration)
-   [Failure and recovery
    demonstration](#failure-and-recovery-demonstration)
-   [Verification results](#verification-results)
-   [Security and operational notes](#security-and-operational-notes)
-   [Scope and limitations](#scope-and-limitations)
-   [Automated tests](#automated-tests)
-   [Team](#team)

------------------------------------------------------------------------

## Project overview

The **Fault-Tolerant System Supervisor** combines an AWS-hosted
application environment with an independent monitoring process.

The application is hosted on two backend EC2 instances. An **Application
Load Balancer (ALB)** distributes incoming application requests across
registered backends and performs target health checks.

A separate Supervisor EC2 instance runs the Python monitoring process.
It checks backend endpoints, classifies their health, stores results in
SQLite, and manages incident records. A Flask dashboard reads this
information and displays infrastructure status, health history, response
times, and incidents.

The supervisor observes and records infrastructure health. **It does not
automatically stop, start, replace EC2 instances, or modify ALB target
registrations.** Traffic distribution and target health routing are
handled by the AWS Application Load Balancer.

## Objectives

-   Monitor multiple backend endpoints independently.
-   Detect unavailable, slow, and unhealthy services.
-   Persist timestamped health checks and diagnostic information.
-   Track incidents without creating duplicate incidents for repeated
    failures.
-   Confirm recovery after consecutive successful checks.
-   Distribute application traffic across backend instances using an
    ALB.
-   Provide a dashboard for infrastructure health, monitoring history,
    and incident visibility.
-   Demonstrate continued application availability when one backend
    becomes unavailable and another healthy target remains.

## Implemented architecture

``` mermaid
flowchart TB
    U[User / Browser] --> ALB[Application Load Balancer]
    ALB --> B1[Backend EC2 01<br/>Apache + CloudApp]
    ALB --> B2[Backend EC2 02<br/>Apache + CloudApp]

    subgraph VPC[AWS VPC]
        ALB
        B1
        B2
        S[Supervisor EC2<br/>Python Monitoring Process]
        D[(SQLite Database)]
        F[Flask Monitoring Dashboard]
        S -->|HTTP health checks| B1
        S -->|HTTP health checks| B2
        S -->|Persist checks and incidents| D
        F -->|Read-only queries| D
    end
```

### Monitoring flow

1.  The supervisor sends HTTP requests to each configured backend
    endpoint.
2.  Each response is classified as `HEALTHY`, `DEGRADED`, or
    `UNHEALTHY`.
3.  The check result and diagnostic details are stored in SQLite.
4.  Repeated failures are associated with an incident.
5.  Consecutive successful checks confirm recovery and resolve the
    incident.
6.  The Flask dashboard reads the database and displays the recorded
    information.

> The dashboard does not perform health checks. It only displays data
> written by the supervisor. When monitoring is stopped, historical
> records remain available and eventually become stale.

## AWS infrastructure

The current deployment uses these AWS components:

  -----------------------------------------------------------------------
  Component                           Purpose
  ----------------------------------- -----------------------------------
  Amazon VPC                          Isolated network for project
                                      resources

  Public and private subnets          Network segmentation

  Internet Gateway and route tables   Internet connectivity where
                                      required

  Security Groups                     Network access control

  Supervisor EC2                      Runs Python monitoring and Flask
                                      dashboard

  Backend EC2 01                      Hosts CloudApp using Apache

  Backend EC2 02                      Hosts CloudApp using Apache

  Application Load Balancer           Distributes application traffic and
                                      checks target health

  SQLite                              Stores service registrations,
                                      health checks, incidents, and
                                      incident events
  -----------------------------------------------------------------------

### Current deployment endpoints

These are the environment-specific values used in the current
deployment. They may change if the infrastructure is recreated.

  -------------------------------------------------------------------------------------------------
  Resource                            Current endpoint
  ----------------------------------- -------------------------------------------------------------
  Supervisor EC2 private IP           `10.0.11.82`

  Backend EC2 01 private IP           `10.0.9.129`

  Backend EC2 02 private IP           `10.0.20.10`

  Application Load Balancer           `fts-application-alb-858756406.us-east-1.elb.amazonaws.com`
  -------------------------------------------------------------------------------------------------

The supervisor runs inside the VPC and reaches the backends using their
private IP addresses. Users access CloudApp through the ALB DNS name.

## Technology stack

  -----------------------------------------------------------------------
  Layer                               Technologies
  ----------------------------------- -----------------------------------
  Cloud platform                      AWS

  Compute                             Amazon EC2

  Networking                          VPC, subnets, route tables,
                                      Internet Gateway, Security Groups

  Traffic distribution                Application Load Balancer

  Web server                          Apache HTTP Server

  Application                         HTML and CSS (CloudApp static
                                      demonstration site)

  Monitoring                          Python, HTTP health checks

  Dashboard                           Flask, HTML, CSS, JavaScript

  Database                            SQLite

  Version control                     Git and GitHub
  -----------------------------------------------------------------------

**Scope clarification:** Auto Scaling, Amazon CloudWatch, and Amazon RDS
appeared in the initial target architecture but are not part of the
currently verified implementation described here. The current monitoring
and incident database uses SQLite on the Supervisor EC2 instance.

## How it works

### Application traffic

-   A user opens CloudApp through the ALB DNS name.
-   The ALB forwards requests to healthy registered backend targets.
-   Both backend EC2 instances serve the CloudApp page using Apache.
-   Each backend has its own page identity, helping identify which
    backend served a response.

Both backend instances contain the shared CSS asset. This is
intentional: the browser may request HTML and CSS separately, and the
ALB can route those requests independently.

### Independent health monitoring

The supervisor monitors the two backend private IP endpoints directly.
It records the HTTP status, response time, health classification,
timestamp, and available diagnostic information.

### Data persistence

Monitoring data is stored in:

``` text
data/fault_tolerant_supervisor.db
```

The database contains service registrations, health checks, incidents,
and incident event history. The dashboard opens the database in
read-only query mode.

### Dashboard updates

The Flask dashboard periodically refreshes its displayed data. The
default browser refresh interval is 10 seconds. Refreshing reads stored
information; it does not trigger a new backend health check.

## Monitoring and incident lifecycle

### Health states

  -----------------------------------------------------------------------
  State                               Meaning
  ----------------------------------- -----------------------------------
  `HEALTHY`                           Successful HTTP response within the
                                      healthy response-time threshold

  `DEGRADED`                          Successful response, but response
                                      time is elevated

  `UNHEALTHY`                         Endpoint is unavailable, returns an
                                      unsuccessful HTTP status, or
                                      exceeds the unhealthy response-time
                                      threshold

  `STALE` (dashboard state)           Latest recorded check is too old to
                                      represent current health

  `NO_DATA` (dashboard state)         No health check has been recorded
                                      for the service
  -----------------------------------------------------------------------

`STALE` and `NO_DATA` are dashboard presentation states, not health
states written by the supervisor.

### Incident lifecycle

-   Repeated availability failures open a critical incident.
-   Elevated latency can open a warning incident; persistent severe
    latency can escalate it.
-   Further matching failures update the existing incident and increase
    its occurrence count.
-   Two consecutive healthy checks confirm recovery and resolve the open
    incident.
-   Creation, updates, escalation, and resolution are recorded in
    `incident_events`.

### Diagnostic information

The monitor records evidence and a best-effort diagnosis, including:

-   Request timeout
-   Connection refused
-   DNS resolution failure
-   Connection failure
-   HTTP client error (4xx)
-   HTTP server error (5xx)
-   Elevated or excessive response latency

These diagnoses are likely causes inferred from the observed response,
not definitive proof of the underlying infrastructure fault.

## Dashboard

The Flask dashboard is in `dashboard/`. It reads monitoring records from
the shared SQLite database and does not start or control the supervisor.

  -----------------------------------------------------------------------
  Page                                Information displayed
  ----------------------------------- -----------------------------------
  Overview                            Infrastructure status, service
                                      cards, health percentages, summary
                                      metrics, charts, insights, recent
                                      checks, and incidents

  Services                            Registered services, status,
                                      filtering, sorting, pagination, and
                                      service details

  Health history                      Recorded checks, health states,
                                      response times, and failure
                                      information

  Incidents                           Incident status, severity,
                                      category, occurrence count, and
                                      lifecycle details

  Diagnostics                         Observed failures and likely causes

  Event history                       Incident creation, updates, and
                                      resolution events

  Settings                            Database status, service
                                      information, role mapping, and
                                      registration configuration
  -----------------------------------------------------------------------

### Infrastructure health percentage

The overview calculates health percentage for the two directly monitored
backend services:

``` text
Health percentage = (HEALTHY checks / All recorded checks) × 100
```

The overall percentage uses the combined check counts of Backend Server
1 and Backend Server 2 for the selected time range. This is a historical
measurement for that range and should be considered alongside each
service's current freshness state.

### Application status and freshness

-   Fresh successful checks can be displayed as healthy.
-   Old checks are labelled stale and are not treated as current health
    evidence.
-   Services that have never been checked are shown as having no data.
-   A healthy ALB response alone does not prove that every backend is
    healthy.

The default stale window is based on the monitoring interval, request
timeout, and configured stale multiplier.

## Repository structure

``` text
fault-tolerant-system-supervisor/
│
├── monitoring/
│   ├── supervisor.py
│   ├── health_monitor.py
│   ├── incident_manager.py
│   └── database_manager.py
│
├── dashboard/
│   ├── app.py
│   ├── config.py
│   ├── routes.py
│   ├── services.py
│   ├── insights.py
│   ├── registration.py
│   ├── params.py
│   ├── templates/
│   └── static/
│
├── data/
│   └── fault_tolerant_supervisor.db
│
├── tests/
├── scripts/
├── requirements-dashboard.txt
└── README.md
```

The database and local backup files are runtime data and should not be
committed to the source repository.

## Run on AWS

Run the supervisor and dashboard on the **Supervisor EC2 instance**.
Both backend EC2 instances must be running with Apache serving CloudApp.

### Prerequisites

-   Python 3.12 or a compatible Python version.
-   Repository available on the Supervisor EC2 instance.
-   Project virtual environment and dependencies installed.
-   Both backend EC2 instances running.
-   Apache active on both backend instances.
-   Network rules allowing the supervisor to reach backend HTTP port 80.

### 1. Connect to the Supervisor EC2 instance

Use AWS Console → EC2 → Instances → `fts-supervisor-server-01` → Connect
→ EC2 Instance Connect.

From the repository root:

``` bash
cd ~/fault-tolerant-system-supervisor
source .venv/bin/activate
```

### 2. Start the Flask dashboard (Terminal 1)

Open one terminal session:

``` bash
cd ~/fault-tolerant-system-supervisor
source .venv/bin/activate

flask --app 'dashboard.app:create_app' run \
  --host 127.0.0.1 \
  --port 5000
```

This binds the dashboard to the Supervisor instance's localhost. It is
suitable for local access, not a public production deployment.

### 3. Start health monitoring (Terminal 2)

Open a second terminal session on the same Supervisor EC2 instance:

``` bash
cd ~/fault-tolerant-system-supervisor
source .venv/bin/activate

python -m monitoring.supervisor \
  --target "Backend Server 01=http://10.0.9.129/" \
  --target "Backend Server 02=http://10.0.20.10/" \
  --timeout 3 \
  --interval 10 \
  --healthy-threshold 0.5 \
  --unhealthy-threshold 1.0
```

The supervisor checks both backends every 10 seconds and stores the
results in the configured SQLite database.

### 4. Stop the processes

When the demonstration or monitoring session is complete:

-   Press `Ctrl+C` in the health-monitoring terminal.
-   Press `Ctrl+C` in the dashboard terminal.

The EC2 instances and Apache services are independent of the
browser-based Instance Connect sessions. Stop EC2 instances when they
are no longer needed to avoid unnecessary running costs.

## Configuration

The supervisor accepts multiple targets using repeated `--target`
arguments. The current AWS command monitors the two backend private IP
endpoints directly.

Dashboard environment variables:

  --------------------------------------------------------------------------------
  Variable                         Default                 Description
  -------------------------------- ----------------------- -----------------------
  `SUPERVISOR_DB_PATH`             Project database path   SQLite database read by
                                                           the dashboard

  `DASHBOARD_REFRESH_SECONDS`      `10`                    Browser refresh
                                                           interval (2--300
                                                           seconds)

  `DASHBOARD_STALE_MULTIPLIER`     `3.0`                   Multiplier used to
                                                           determine stale data

  `DASHBOARD_SERVICE_ROLES`        None                    Optional service role
                                                           mapping

  `DASHBOARD_ALLOW_REGISTRATION`   Off                     Enables optional
                                                           service registration
                                                           when set to `1` and an
                                                           admin token is
                                                           configured

  `DASHBOARD_ADMIN_TOKEN`          None                    Required for optional
                                                           service registration

  `DASHBOARD_MAX_SERVICES`         `50`                    Maximum service
                                                           registration limit
  --------------------------------------------------------------------------------

Do not commit tokens, passwords, private keys, or other credentials to
GitHub.

## Failure and recovery demonstration

The project has been tested with a controlled backend web-server
failure.

### Demonstration sequence

1.  Confirm both backend targets are healthy.

2.  Stop Apache on Backend Server 01:

    ``` bash
    sudo systemctl stop httpd
    ```

3.  Observe the supervisor reporting the backend as unhealthy and
    recording the failure.

4.  Observe the ALB target group marking Backend Server 01 unhealthy
    after its configured health checks.

5.  Open CloudApp through the ALB. With Backend Server 02 healthy, the
    application continues to respond through the remaining healthy
    target.

6.  Restart Apache on Backend Server 01:

    ``` bash
    sudo systemctl start httpd
    ```

7.  Verify that Backend Server 01 returns HTTP 200 again.

8.  Observe consecutive healthy checks and the incident recovery event.

**Important:** Perform failure simulation only on project resources that
your team owns. The supervisor detects and records failure; Apache
recovery in this demonstration is performed manually.

## Verification results

The following results were observed during AWS integration and dashboard
verification:

  -----------------------------------------------------------------------
  Verification                        Observed result
  ----------------------------------- -----------------------------------
  CloudApp on Backend EC2 01          Loaded successfully

  CloudApp on Backend EC2 02          Loaded successfully

  Application Load Balancer           Served CloudApp through a healthy
                                      backend

  Backend health monitoring           Both backends returned HTTP 200

  Monitoring interval                 10 seconds

  SQLite persistence                  New health-check records appeared
                                      in the existing database

  Dashboard endpoint                  Returned HTTP 200

  Health percentage calculation       Correctly reflected checks for the
                                      two monitored backends

  Verified health percentage snapshot 100% overall (204 healthy / 204
                                      total checks)

  Backend 01 failure simulation       Connection failure detected; ALB
                                      continued serving through Backend
                                      02

  Backend recovery                    Healthy checks recorded and the
                                      earlier critical incident resolved

  Incident event history              Creation, updates, and resolution
                                      retained
  -----------------------------------------------------------------------

The 100% health percentage is a snapshot of checks recorded during the
selected time range at verification time. It is not a guarantee of
future availability.

## Security and operational notes

-   The supervisor should reach backend instances through private VPC
    addresses.
-   The ALB is the public application entry point; backend instances
    should not require unrestricted public HTTP access.
-   The Flask dashboard has no login in the current implementation. Do
    not leave port `5000` open to the public internet.
-   Restrict SSH access to trusted IP addresses.
-   Temporary Security Group access may be enabled for the faculty
    demonstration. After the demonstration, review and restrict all
    inbound rules.
-   Do not publish AWS credentials, private keys, access tokens, or
    sensitive instance information in the repository.
-   Stop unused EC2 instances and other billable resources when project
    work is paused.

## Scope and limitations

-   The supervisor performs health monitoring, diagnosis, and incident
    tracking; it does not automatically repair infrastructure.
-   AWS ALB target health checks and routing operate independently of
    the Python supervisor.
-   The current supervisor command monitors the two backend EC2 private
    IP endpoints directly. The dashboard may also contain older ALB and
    probe registrations; these can appear stale if they are not actively
    monitored.
-   The dashboard displays stored data. Refreshing it does not generate
    new health checks.
-   The current database is SQLite on the Supervisor EC2 instance.
    Amazon RDS is not used in the verified deployment.
-   Auto Scaling and CloudWatch are not part of the current verified
    implementation.

## Automated tests

From the repository root, activate the virtual environment and install
dashboard dependencies if required:

``` bash
python -m pip install -r requirements-dashboard.txt
```

Run the test suite:

``` bash
python -m unittest discover -s tests -v
```

Dashboard tests require Flask in the active Python environment.

------------------------------------------------------------------------

## Team

**Team Name:** CodeCrafters\
**Project:** Fault-Tolerant System Supervisor

```{=html}
<p align="center">
```
`<strong>`{=html}Built with Python and
AWS`</strong>`{=html}`<br>`{=html} Monitoring infrastructure. Detecting
failures. Observing recovery.
```{=html}
</p>
```

