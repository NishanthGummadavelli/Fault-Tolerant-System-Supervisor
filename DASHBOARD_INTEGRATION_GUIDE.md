FAULT-TOLERANT SYSTEM SUPERVISOR: DASHBOARD INTEGRATION

This ZIP is the complete project (monitoring engine, dashboard, scripts and tests). See README.md for
installation, configuration, running, demonstrations and migration steps.

Quick start
  python -m pip install -r requirements-dashboard.txt
  python -m unittest discover -s tests -v
  python -m monitoring.supervisor --interval 10 --target "Backend Server 1=URL" --target "Backend Server 2=URL" --target "Application Load Balancer=URL"
  flask --app "dashboard.app:create_app" run --host 127.0.0.1 --port 5000

Notes
- The dashboard reads the same SQLite database as the supervisor and never starts monitoring.
- Use the demo database (data/demo.db) for demonstrations, never the production database.
- Back up data/ before upgrading; the supervisor migrates the database automatically and keeps all rows.
- Not verified here: live AWS endpoints and visual review in a real browser.
