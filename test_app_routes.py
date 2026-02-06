#!/usr/bin/env python3
"""
Test script: Verify Flask app compiles, all routes register, and key pages render.

Strategy:
  - Set dummy environment variables BEFORE any project imports
  - Mock Redis, PostgreSQL, Google Sheets, and external HTTP calls at module level
  - Import the Flask app
  - Inspect registered routes
  - Use the test client to GET /integrations
"""

import sys
import os

# ============================================================
# STEP 1: Set dummy environment variables
# ============================================================
DUMMY_ENV = {
    "DATABASE_URL": "postgresql://test:test@localhost:5432/testdb",
    "REDIS_URL": "redis://localhost:6379",
    "SECRET_KEY": "test_dummy",
    "SESSION_SECRET": "test_dummy",
    "GHL_APP_CLIENT_ID": "test_dummy",
    "GHL_APP_CLIENT_SECRET": "test_dummy",
    "STRIPE_SECRET_KEY": "sk_test_dummy_000000000000000000000000",
    "XAI_API_KEY": "test_dummy",
    "MAIL_SERVER": "smtp.example.com",
    "MAIL_PORT": "587",
    "MAIL_USERNAME": "test@example.com",
    "MAIL_PASSWORD": "test_dummy",
    "MAIL_DEFAULT_SENDER": "test@example.com",
    "YOUR_DOMAIN": "http://localhost:8080",
    "GOOGLE_CREDENTIALS": "{}",
    "SUBSCRIBER_SHEET_URL": "",
    "SUBSCRIBER_SHEET_EDIT_URL": "",
}
for key, val in DUMMY_ENV.items():
    os.environ.setdefault(key, val)

# ============================================================
# STEP 2: Mock external services BEFORE importing main.py
# ============================================================
from unittest.mock import MagicMock, patch, PropertyMock
import unittest.mock

# --- Mock Redis + RQ ---
mock_redis_conn = MagicMock()
mock_redis_conn.ping.return_value = True

mock_queue = MagicMock()
mock_queue.enqueue.return_value = MagicMock(id="fake-job-id")

# --- Mock psycopg2 ---
mock_cursor = MagicMock()
mock_cursor.fetchone.return_value = None
mock_cursor.fetchall.return_value = []
mock_cursor.description = []

mock_pg_conn = MagicMock()
mock_pg_conn.cursor.return_value = mock_cursor
mock_pg_conn.__enter__ = lambda self: self
mock_pg_conn.__exit__ = MagicMock(return_value=False)

# Patch at module level before any import pulls them in
patches = [
    # Redis
    patch("redis.from_url", return_value=mock_redis_conn),
    patch("rq.Queue", return_value=mock_queue),
    # PostgreSQL - patch at psycopg2 level
    patch("psycopg2.connect", return_value=mock_pg_conn),
    # HTTP calls (sync_subscribers fetches CSV, etc.)
    patch("requests.get", return_value=MagicMock(
        status_code=200, text="", raise_for_status=lambda: None
    )),
    # Google Sheets
    patch("gspread.authorize", return_value=MagicMock()),
]

for p in patches:
    p.start()

# ============================================================
# STEP 3: Import the Flask app
# ============================================================
print("=" * 70)
print("FLASK APP COMPILATION & ROUTE REGISTRATION TEST")
print("=" * 70)
print()

try:
    # Ensure the project directory is on sys.path
    project_dir = "/home/user/Flask-Webhook"
    if project_dir not in sys.path:
        sys.path.insert(0, project_dir)

    print("[1/5] Importing Flask app from main.py ...")
    from main import app
    print("       PASS - Flask app imported successfully.")
except Exception as e:
    print(f"       FAIL - Could not import Flask app: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# ============================================================
# STEP 4: List all registered routes
# ============================================================
print()
print("[2/5] Listing all registered routes:")
print("-" * 70)
print(f"  {'Endpoint':<35} {'Methods':<20} {'Rule'}")
print("-" * 70)

routes = []
for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
    methods = ",".join(sorted(rule.methods - {"OPTIONS", "HEAD"}))
    routes.append((rule.endpoint, methods, rule.rule))
    print(f"  {rule.endpoint:<35} {methods:<20} {rule.rule}")

print("-" * 70)
print(f"  Total routes registered: {len(routes)}")
print()

# ============================================================
# STEP 5: Verify specific required routes exist
# ============================================================
REQUIRED_ROUTES = [
    ("/integrations", "GET"),
    ("/api/integrations/save", "POST"),
    ("/api/integrations/test", "POST"),
]

print("[3/5] Verifying required routes exist:")
route_map = {}
for rule in app.url_map.iter_rules():
    route_map[rule.rule] = rule.methods

all_found = True
for path, method in REQUIRED_ROUTES:
    if path in route_map and method in route_map[path]:
        print(f"       PASS - {method} {path}")
    else:
        print(f"       FAIL - {method} {path} NOT FOUND")
        all_found = False

if all_found:
    print("       All required routes verified.")
else:
    print("       WARNING: Some required routes are missing!")

# ============================================================
# STEP 6: Test /integrations page renders
# ============================================================
print()
print("[4/5] Testing GET /integrations renders correctly ...")

try:
    with app.test_client() as client:
        resp = client.get("/integrations")
        status = resp.status_code
        body = resp.data.decode("utf-8", errors="replace")

        if status == 200:
            print(f"       PASS - Status code: {status}")
            # Check for key content markers
            checks = [
                ("page title", "Integrations" in body or "integrations" in body.lower()),
                ("CRM mention", "CRM" in body or "crm" in body.lower()),
                ("GoHighLevel", "GoHighLevel" in body or "gohighlevel" in body.lower()),
                ("HTML structure", "<html" in body.lower()),
            ]
            for label, passed in checks:
                tag = "PASS" if passed else "WARN"
                print(f"       {tag} - Response contains '{label}'")

            # Print a snippet of the response
            snippet = body[:300].replace("\n", " ").strip()
            print(f"       Response snippet: {snippet}...")
        else:
            print(f"       FAIL - Status code: {status} (expected 200)")
            print(f"       Response: {body[:500]}")
except Exception as e:
    print(f"       FAIL - Exception during request: {e}")
    import traceback
    traceback.print_exc()

# ============================================================
# STEP 7: Summary
# ============================================================
print()
print("[5/5] Additional verification - spot-check other key routes ...")
SPOT_CHECK = ["/", "/webhook", "/login", "/register", "/dashboard",
              "/demo-chat", "/stripe-webhook", "/checkout", "/support"]
for path in SPOT_CHECK:
    found = path in route_map
    tag = "PASS" if found else "FAIL"
    print(f"       {tag} - {path}")

print()
print("=" * 70)
print("TEST COMPLETE")
print("=" * 70)

# Cleanup
for p in patches:
    p.stop()
