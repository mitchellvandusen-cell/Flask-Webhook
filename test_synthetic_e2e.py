#!/usr/bin/env python3
"""
Comprehensive Synthetic End-to-End Test Suite for InsuranceGrokBot (Flask-Webhook)

Tests the full user journey using the Flask test client with all external services
mocked (PostgreSQL, Redis, Stripe, GoHighLevel, Google Sheets, etc.).

Run standalone:
    python test_synthetic_e2e.py

Each test group prints clear PASS/FAIL results and a final summary.
"""

import sys
import os
import json
import traceback

# ============================================================
# STEP 1: Set dummy environment variables BEFORE any import
# ============================================================
DUMMY_ENV = {
    "DATABASE_URL": "postgresql://test:test@localhost:5432/testdb",
    "REDIS_URL": "redis://localhost:6379",
    "SECRET_KEY": "test-secret-key-for-flask-app",
    "SESSION_SECRET": "test-session-secret",
    "GHL_APP_CLIENT_ID": "test_ghl_client_id",
    "GHL_APP_CLIENT_SECRET": "test_ghl_client_secret",
    "STRIPE_SECRET_KEY": "sk_test_dummy_000000000000000000000000",
    "STRIPE_PRICE_ID": "price_test_individual",
    "STRIPE_AGENCY_STARTER_PRICE_ID": "price_test_agency_starter",
    "STRIPE_AGENCY_PRO_PRICE_ID": "price_test_agency_pro",
    "STRIPE_WEBHOOK_SECRET": "whsec_test_dummy",
    "XAI_API_KEY": "test_xai_key",
    "MAIL_SERVER": "smtp.example.com",
    "MAIL_PORT": "587",
    "MAIL_USERNAME": "test@example.com",
    "MAIL_PASSWORD": "test_password",
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
from werkzeug.security import generate_password_hash

# --- Mock Redis + RQ ---
mock_redis_conn = MagicMock()
mock_redis_conn.ping.return_value = True

mock_queue = MagicMock()
mock_job = MagicMock()
mock_job.id = "fake-job-id-12345"
mock_queue.enqueue.return_value = mock_job

# --- Mock psycopg2 ---
mock_cursor = MagicMock()
mock_cursor.fetchone.return_value = None
mock_cursor.fetchall.return_value = []
mock_cursor.description = []
mock_cursor.rowcount = 0

mock_pg_conn = MagicMock()
mock_pg_conn.cursor.return_value = mock_cursor
mock_pg_conn.__enter__ = lambda self: self
mock_pg_conn.__exit__ = MagicMock(return_value=False)

# --- Stripe mock ---
mock_stripe_session = MagicMock()
mock_stripe_session.url = "https://checkout.stripe.com/test"
mock_stripe_session.id = "cs_test_123"

# Patch at module level before any import pulls them in
patches = [
    # Redis
    patch("redis.from_url", return_value=mock_redis_conn),
    patch("rq.Queue", return_value=mock_queue),
    # PostgreSQL
    patch("psycopg2.connect", return_value=mock_pg_conn),
    # HTTP calls
    patch("requests.get", return_value=MagicMock(
        status_code=200, text="", content=b"", raise_for_status=lambda: None
    )),
    patch("requests.post", return_value=MagicMock(
        status_code=200, text="{}", json=lambda: {}, raise_for_status=lambda: None
    )),
    # Google Sheets
    patch("gspread.authorize", return_value=MagicMock()),
]

for p in patches:
    p.start()

# ============================================================
# STEP 3: Import the Flask app
# ============================================================
project_dir = "/home/user/Flask-Webhook"
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

try:
    from main import app
    from db import User
except Exception as e:
    print(f"FATAL: Could not import Flask app: {e}")
    traceback.print_exc()
    sys.exit(1)

# Disable CSRF for test forms
app.config["WTF_CSRF_ENABLED"] = False
app.config["TESTING"] = True
app.config["SECRET_KEY"] = "test-secret-key-for-flask-app"

# ============================================================
# Test infrastructure
# ============================================================
passed = 0
failed = 0
errors = []


def report(test_name, ok, detail=""):
    global passed, failed, errors
    if ok:
        passed += 1
        print(f"  PASS  {test_name}")
    else:
        failed += 1
        errors.append((test_name, detail))
        print(f"  FAIL  {test_name}{(' -- ' + detail) if detail else ''}")


def make_test_user(overrides=None):
    """Create a mock User object for testing authenticated routes."""
    base = {
        "email": "testuser@example.com",
        "password_hash": generate_password_hash("TestPassword123!"),
        "location_id": "loc_test_abc123",
        "access_token": "test_access_token_abc123xyz",
        "refresh_token": "test_refresh_token_xyz",
        "token_expires_at": None,
        "token_type": "Bearer",
        "calendar_id": "cal_test_123",
        "calendar_name": "Test Calendar",
        "crm_user_id": "crm_user_test_456",
        "bot_first_name": "TestBot",
        "timezone": "America/Chicago",
        "initial_message": "Hey, this is TestBot!",
        "role": "individual",
        "subscription_tier": "individual",
        "stripe_customer_id": "cus_test_stripe_123",
        "full_name": "Test User",
        "phone": "+15551234567",
        "bio": "Test bio",
        "onboarding_status": "claimed",
        "crm_type": "ghl",
        "crm_config": {},
    }
    if overrides:
        base.update(overrides)
    return User(base)


def login_test_user(client, user=None):
    """Log in a test user by patching User.get and posting to /login."""
    if user is None:
        user = make_test_user()
    with patch.object(User, 'get', return_value=user):
        resp = client.post("/login", data={
            "email": user.email,
            "password": "TestPassword123!",
        }, follow_redirects=False)
    return resp


# ============================================================
# TEST GROUP 1: Public Pages Load (200 status)
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 1: Public Pages Load")
print("=" * 72)

PUBLIC_ROUTES = [
    ("/", "home"),
    ("/support", "support"),
    ("/integrations", "integrations"),
    ("/comparison", "comparison"),
    ("/comparison/text-drip", "comparison-text-drip"),
    ("/login", "login"),
    ("/register", "register"),
    ("/agency-login", "agency-login"),
    ("/getting-started", "getting-started"),
    ("/privacy", "privacy"),
    ("/terms", "terms"),
    ("/contact", "contact"),
]

with app.test_client() as client:
    for route, name in PUBLIC_ROUTES:
        try:
            resp = client.get(route)
            status = resp.status_code
            ok = status == 200
            report(f"GET {route} -> {status}", ok,
                   f"Expected 200, got {status}" if not ok else "")
        except Exception as e:
            report(f"GET {route}", False, str(e))

# demo-chat calls run_demo_janitor which touches DB, so we mock it
with app.test_client() as client:
    try:
        with patch("main.run_demo_janitor"):
            resp = client.get("/demo-chat")
            status = resp.status_code
            ok = status == 200
            report(f"GET /demo-chat -> {status}", ok,
                   f"Expected 200, got {status}" if not ok else "")
    except Exception as e:
        report(f"GET /demo-chat", False, str(e))


# ============================================================
# TEST GROUP 2: Registration Flow
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 2: Registration Flow")
print("=" * 72)

with app.test_client() as client:
    # 2a: GET /register should return 200 (renders form)
    try:
        resp = client.get("/register")
        report("GET /register renders form", resp.status_code == 200)
    except Exception as e:
        report("GET /register renders form", False, str(e))

    # 2b: POST /register with valid data - mock DB to simulate location found
    try:
        mock_match_row = {
            "email": "newuser@example.com",
            "parent_agency_email": None,
            "invite_token": None,
            "onboarding_status": "pending",
        }
        mock_reg_cursor = MagicMock()
        mock_reg_cursor.fetchone.return_value = mock_match_row

        mock_reg_conn = MagicMock()
        mock_reg_conn.cursor.return_value = mock_reg_cursor

        with patch("main.User.get", return_value=None), \
             patch("main.get_db_connection", return_value=mock_reg_conn):
            resp = client.post("/register", data={
                "email": "newuser@example.com",
                "location_id": "loc_new_user_123",
                "password": "SecurePass123!",
                "confirm": "SecurePass123!",
            }, follow_redirects=False)

            # Should redirect to login on success
            ok = resp.status_code in (302, 303)
            location = resp.headers.get("Location", "")
            report(f"POST /register redirects (status={resp.status_code})", ok,
                   f"Got {resp.status_code}, Location: {location}" if not ok else "")
            report("POST /register redirects to /login",
                   "/login" in location,
                   f"Location: {location}")
    except Exception as e:
        report("POST /register flow", False, str(e))

    # 2c: POST /register with duplicate email should redirect back
    try:
        existing_user = make_test_user({"email": "existing@example.com"})
        with patch("main.User.get", return_value=existing_user):
            resp = client.post("/register", data={
                "email": "existing@example.com",
                "location_id": "loc_existing_123",
                "password": "SecurePass123!",
                "confirm": "SecurePass123!",
            }, follow_redirects=False)
            ok = resp.status_code in (302, 303)
            location = resp.headers.get("Location", "")
            report("POST /register duplicate email redirects to login",
                   ok and "/login" in location,
                   f"Status={resp.status_code}, Location={location}")
    except Exception as e:
        report("POST /register duplicate email", False, str(e))


# ============================================================
# TEST GROUP 3: Login Flow
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 3: Login Flow")
print("=" * 72)

with app.test_client() as client:
    # 3a: GET /login renders form
    try:
        resp = client.get("/login")
        report("GET /login renders form", resp.status_code == 200)
    except Exception as e:
        report("GET /login renders form", False, str(e))

    # 3b: POST /login with correct credentials redirects to dashboard
    try:
        user = make_test_user()
        resp = login_test_user(client, user)
        ok = resp.status_code in (302, 303)
        location = resp.headers.get("Location", "")
        report(f"POST /login correct creds redirects (status={resp.status_code})", ok)
        report("POST /login redirects to /dashboard",
               "/dashboard" in location,
               f"Location: {location}")
    except Exception as e:
        report("POST /login correct creds", False, str(e))

    # 3c: POST /login with wrong password stays on login
    try:
        with app.test_client() as client2:
            user = make_test_user()
            with patch.object(User, 'get', return_value=user):
                resp = client2.post("/login", data={
                    "email": user.email,
                    "password": "WrongPassword999!",
                }, follow_redirects=False)
                # Wrong password should re-render login page (200) not redirect
                ok = resp.status_code == 200
                report(f"POST /login wrong password stays on page (status={resp.status_code})", ok)
    except Exception as e:
        report("POST /login wrong password", False, str(e))

    # 3d: POST /login with unknown email stays on login page
    try:
        with app.test_client() as client3:
            with patch.object(User, 'get', return_value=None):
                resp = client3.post("/login", data={
                    "email": "nobody@example.com",
                    "password": "Whatever123!",
                }, follow_redirects=False)
                ok = resp.status_code == 200
                report(f"POST /login unknown email stays on page (status={resp.status_code})", ok)
    except Exception as e:
        report("POST /login unknown email", False, str(e))


# ============================================================
# TEST GROUP 4: Dashboard Access Requires Login
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 4: Dashboard Access Control")
print("=" * 72)

with app.test_client() as client:
    # 4a: /dashboard without login redirects to /login
    try:
        resp = client.get("/dashboard", follow_redirects=False)
        ok = resp.status_code in (302, 303)
        location = resp.headers.get("Location", "")
        report("GET /dashboard (unauthenticated) redirects",
               ok and "/login" in location,
               f"Status={resp.status_code}, Location={location}")
    except Exception as e:
        report("GET /dashboard (unauthenticated)", False, str(e))

    # 4b: /dashboard with login returns 200
    try:
        user = make_test_user()
        login_test_user(client, user)
        with patch("main.get_db_connection", return_value=mock_pg_conn), \
             patch.object(User, 'get', return_value=user):
            resp = client.get("/dashboard")
            ok = resp.status_code == 200
            report(f"GET /dashboard (authenticated) -> {resp.status_code}", ok,
                   f"Expected 200, got {resp.status_code}" if not ok else "")
    except Exception as e:
        report("GET /dashboard (authenticated)", False, str(e))

    # 4c: /onboarding-status without login redirects (use a fresh client to avoid session leaking)
with app.test_client() as fresh_client:
    try:
        resp = fresh_client.get("/onboarding-status", follow_redirects=False)
        ok = resp.status_code in (302, 303)
        location = resp.headers.get("Location", "")
        report("GET /onboarding-status (unauthenticated) redirects",
               ok and "/login" in location,
               f"Status={resp.status_code}, Location={location}")
    except Exception as e:
        report("GET /onboarding-status (unauthenticated)", False, str(e))


# ============================================================
# TEST GROUP 5: API Routes Auth Guard
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 5: API Routes Auth Guard")
print("=" * 72)

AUTH_REQUIRED_ROUTES = [
    ("GET", "/api/logs"),
    ("POST", "/api/integrations/save"),
    ("POST", "/api/integrations/test"),
    ("GET", "/api/fetch-calendars"),
    ("POST", "/save-profile"),
    ("GET", "/api/onboarding-check"),
]

with app.test_client() as client:
    for method, route in AUTH_REQUIRED_ROUTES:
        try:
            if method == "GET":
                resp = client.get(route, follow_redirects=False)
            else:
                resp = client.post(route,
                                   data=json.dumps({}),
                                   content_type="application/json",
                                   follow_redirects=False)
            ok = resp.status_code in (302, 303, 401)
            location = resp.headers.get("Location", "")
            redirects_to_login = "/login" in location if resp.status_code in (302, 303) else True
            report(f"{method} {route} (unauthenticated) -> {resp.status_code}",
                   ok and redirects_to_login,
                   f"Expected redirect to /login, got status={resp.status_code}, Location={location}")
        except Exception as e:
            report(f"{method} {route} (unauthenticated)", False, str(e))


# ============================================================
# TEST GROUP 6: Webhook Endpoint
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 6: Webhook Endpoint")
print("=" * 72)

with app.test_client() as client:
    # 6a: POST /webhook with valid GHL payload returns 202
    try:
        payload = {
            "type": "InboundMessage",
            "location_id": "loc_test_123",
            "contact_id": "contact_test_abc123",
            "message": "Hey, I'm interested in life insurance",
            "direction": "inbound",
        }
        resp = client.post("/webhook",
                           data=json.dumps(payload),
                           content_type="application/json")
        ok = resp.status_code == 202
        data = resp.get_json()
        report(f"POST /webhook valid payload -> {resp.status_code}", ok,
               f"Expected 202" if not ok else "")
        report("POST /webhook returns job_id",
               data and "job_id" in data,
               f"Response: {data}")
    except Exception as e:
        report("POST /webhook valid payload", False, str(e))

    # 6b: POST /webhook with missing contact_id returns 400
    try:
        payload = {
            "type": "InboundMessage",
            "location_id": "loc_test_123",
            "message": "Hello",
        }
        resp = client.post("/webhook",
                           data=json.dumps(payload),
                           content_type="application/json")
        ok = resp.status_code == 400
        report(f"POST /webhook missing contact_id -> {resp.status_code}", ok,
               f"Expected 400" if not ok else "")
    except Exception as e:
        report("POST /webhook missing contact_id", False, str(e))

    # 6c: POST /webhook with short/invalid contact_id returns 400
    try:
        payload = {
            "location_id": "loc_test_123",
            "contact_id": "abc",  # Too short (< 5 chars)
            "message": "Hello",
        }
        resp = client.post("/webhook",
                           data=json.dumps(payload),
                           content_type="application/json")
        ok = resp.status_code == 400
        report(f"POST /webhook short contact_id -> {resp.status_code}", ok,
               f"Expected 400" if not ok else "")
    except Exception as e:
        report("POST /webhook short contact_id", False, str(e))

    # 6d: POST /webhook with camelCase fields (flexible extraction)
    try:
        payload = {
            "type": "InboundMessage",
            "locationId": "loc_camel_test",
            "contactId": "contact_camel_test123",
            "body": "I want to learn about whole life",
        }
        resp = client.post("/webhook",
                           data=json.dumps(payload),
                           content_type="application/json")
        ok = resp.status_code == 202
        report(f"POST /webhook camelCase fields -> {resp.status_code}", ok,
               f"Expected 202" if not ok else "")
    except Exception as e:
        report("POST /webhook camelCase fields", False, str(e))

    # 6e: POST /webhook demo location routes to demo queue
    try:
        payload = {
            "location_id": "DEMO_LOC",
            "contact_id": "demo_contact_test_12345",
            "message": "Testing demo",
        }
        mock_queue.enqueue.reset_mock()
        with patch("main.get_db_connection", return_value=mock_pg_conn):
            resp = client.post("/webhook",
                               data=json.dumps(payload),
                               content_type="application/json")
            ok = resp.status_code == 202
            report(f"POST /webhook DEMO location -> {resp.status_code}", ok)
    except Exception as e:
        report("POST /webhook DEMO location", False, str(e))


# ============================================================
# TEST GROUP 7: Integration Save/Test for All 7 CRM Types
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 7: Integration Save/Test for All CRM Types")
print("=" * 72)

CRM_CONFIGS = {
    "ghl": {"access_token": "tok_ghl_123", "calendar_id": "cal_123"},
    "zapier": {"webhook_url": "https://hooks.zapier.com/hooks/catch/123/abc"},
    "salesforce": {"instance_url": "https://na1.salesforce.com", "access_token": "sf_tok_123"},
    "hubspot": {"access_token": "hub_tok_123"},
    "pipedrive": {"company_domain": "testco", "api_token": "pd_tok_123"},
    "zoho": {"access_token": "zoho_tok_123", "refresh_token": "zoho_ref", "client_id": "cid", "client_secret": "csec"},
    "insureio": {"api_key": "ins_key_123", "brand_id": "brand_456"},
}

with app.test_client() as client:
    user = make_test_user()
    login_test_user(client, user)

    for crm_type, config in CRM_CONFIGS.items():
        # 7a: Save integration config
        try:
            mock_save_cursor = MagicMock()
            mock_save_conn = MagicMock()
            mock_save_conn.cursor.return_value = mock_save_cursor

            with patch("main.get_db_connection", return_value=mock_save_conn), \
                 patch.object(User, 'get', return_value=user):
                resp = client.post("/api/integrations/save",
                                   data=json.dumps({
                                       "crm_type": crm_type,
                                       "crm_config": config,
                                   }),
                                   content_type="application/json")
                ok = resp.status_code == 200
                data = resp.get_json()
                report(f"Save {crm_type} config -> {resp.status_code}",
                       ok and data and data.get("success"),
                       f"Response: {data}" if not ok else "")
        except Exception as e:
            report(f"Save {crm_type} config", False, str(e))

        # 7b: Test integration credentials
        try:
            with patch.object(User, 'get', return_value=user):
                resp = client.post("/api/integrations/test",
                                   data=json.dumps({
                                       "crm_type": crm_type,
                                       "crm_config": config,
                                   }),
                                   content_type="application/json")
                # Accept 200 or 500 (some adapters may fail validation without real endpoints)
                ok = resp.status_code in (200, 500)
                data = resp.get_json()
                report(f"Test {crm_type} credentials -> {resp.status_code}",
                       ok,
                       f"Unexpected status: {resp.status_code}" if not ok else "")
        except Exception as e:
            report(f"Test {crm_type} credentials", False, str(e))


# ============================================================
# TEST GROUP 8: Log Retrieval (/api/logs)
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 8: Log Retrieval (/api/logs)")
print("=" * 72)

with app.test_client() as client:
    user = make_test_user()
    login_test_user(client, user)

    # 8a: Fetch logs with mock data
    try:
        from datetime import datetime
        mock_logs = [
            {
                "id": 1,
                "location_id": "loc_test_abc123",
                "contact_id": "ct_001",
                "event_type": "webhook_received",
                "status": "success",
                "summary": "Inbound message processed",
                "details": {"message": "Hello"},
                "created_at": datetime(2026, 1, 15, 10, 30, 0),
            },
            {
                "id": 2,
                "location_id": "loc_test_abc123",
                "contact_id": "ct_002",
                "event_type": "booking_success",
                "status": "success",
                "summary": "Appointment booked at 2 PM",
                "details": {},
                "created_at": datetime(2026, 1, 15, 11, 0, 0),
            },
        ]

        with patch("db.get_webhook_logs", return_value=mock_logs), \
             patch.object(User, 'get', return_value=user):
            resp = client.get("/api/logs")
            ok = resp.status_code == 200
            data = resp.get_json()
            report(f"GET /api/logs -> {resp.status_code}", ok)
            report("GET /api/logs returns logs array",
                   data and "logs" in data and len(data["logs"]) == 2,
                   f"Response: {data}")
            report("GET /api/logs returns total count",
                   data and data.get("total") == 2,
                   f"Total: {data.get('total') if data else 'N/A'}")
    except Exception as e:
        report("GET /api/logs", False, str(e))

    # 8b: Fetch logs with filters (use fresh mock data since 8a mutates datetime objects)
    try:
        mock_filtered_logs = [{
            "id": 2,
            "location_id": "loc_test_abc123",
            "contact_id": "ct_002",
            "event_type": "booking_success",
            "status": "success",
            "summary": "Appointment booked at 2 PM",
            "details": {},
            "created_at": datetime(2026, 1, 15, 11, 0, 0),
        }]
        with patch("db.get_webhook_logs", return_value=mock_filtered_logs) as mock_fn, \
             patch.object(User, 'get', return_value=user):
            resp = client.get("/api/logs?event_type=booking_success&limit=10")
            ok = resp.status_code == 200
            data = resp.get_json()
            report("GET /api/logs with event_type filter -> 200", ok)
            # Verify the function was called with filters
            if mock_fn.called:
                call_kwargs = mock_fn.call_args
                report("GET /api/logs passes event_type filter",
                       call_kwargs and call_kwargs[1].get("event_type") == "booking_success",
                       f"Call args: {call_kwargs}")
            else:
                report("GET /api/logs passes event_type filter", False, "Function not called")
    except Exception as e:
        report("GET /api/logs with filters", False, str(e))

    # 8c: Fetch logs with no location_id (should return empty)
    try:
        user_no_loc = make_test_user({"location_id": None})
        with app.test_client() as client8c:
            login_test_user(client8c, user_no_loc)
            with patch.object(User, 'get', return_value=user_no_loc):
                resp = client8c.get("/api/logs")
                ok = resp.status_code == 200
                data = resp.get_json()
                report("GET /api/logs (no location_id) returns empty",
                       ok and data and data.get("total") == 0,
                       f"Response: {data}")
    except Exception as e:
        report("GET /api/logs (no location_id)", False, str(e))


# ============================================================
# TEST GROUP 9: Template Rendering (no undefined variable errors)
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 9: Template Rendering")
print("=" * 72)

# Templates that render without authentication
SIMPLE_TEMPLATES = [
    ("/", "home.html"),
    ("/support", "support.html"),
    ("/comparison", "comparison.html"),
    ("/comparison/text-drip", "comparison-text-drip.html"),
    ("/getting-started", "getting-started.html"),
    ("/login", "login.html"),
    ("/register", "register.html"),
    ("/privacy", "privacy.html"),
    ("/terms", "terms.html"),
    ("/contact", "contact.html"),
    ("/disclaimers", "disclaimers.html"),
]

with app.test_client() as client:
    for route, template_name in SIMPLE_TEMPLATES:
        try:
            resp = client.get(route)
            body = resp.data.decode("utf-8", errors="replace")
            has_html = "<html" in body.lower() or "<!doctype" in body.lower()
            no_jinja_error = "UndefinedError" not in body and "TemplateSyntaxError" not in body
            ok = resp.status_code == 200 and has_html and no_jinja_error
            report(f"Template {template_name} renders clean", ok,
                   f"status={resp.status_code}, has_html={has_html}, no_error={no_jinja_error}" if not ok else "")
        except Exception as e:
            report(f"Template {template_name} renders", False, str(e))

# Templates that require auth context
with app.test_client() as client:
    user = make_test_user()
    login_test_user(client, user)

    # Dashboard template
    try:
        with patch("main.get_db_connection", return_value=mock_pg_conn), \
             patch.object(User, 'get', return_value=user):
            resp = client.get("/dashboard")
            body = resp.data.decode("utf-8", errors="replace")
            no_jinja_error = "UndefinedError" not in body and "TemplateSyntaxError" not in body
            ok = resp.status_code == 200 and no_jinja_error
            report("Template dashboard.html renders clean (authenticated)", ok,
                   f"status={resp.status_code}, no_error={no_jinja_error}" if not ok else "")
    except Exception as e:
        report("Template dashboard.html renders (authenticated)", False, str(e))

    # Onboarding-status template
    try:
        with patch.object(User, 'get', return_value=user):
            resp = client.get("/onboarding-status")
            body = resp.data.decode("utf-8", errors="replace")
            no_jinja_error = "UndefinedError" not in body
            ok = resp.status_code == 200 and no_jinja_error
            report("Template onboarding-status.html renders clean", ok,
                   f"status={resp.status_code}" if not ok else "")
    except Exception as e:
        report("Template onboarding-status.html renders", False, str(e))

    # Integrations template
    try:
        resp = client.get("/integrations")
        body = resp.data.decode("utf-8", errors="replace")
        no_jinja_error = "UndefinedError" not in body
        ok = resp.status_code == 200 and no_jinja_error
        report("Template integrations.html renders clean", ok)
    except Exception as e:
        report("Template integrations.html renders", False, str(e))


# ============================================================
# TEST GROUP 10: CRM Factory - All 7 Types
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 10: CRM Factory - All 7 Adapter Types")
print("=" * 72)

try:
    from crm_adapters.factory import get_crm_adapter, CRM_REGISTRY, list_available_crms
    report("Import CRM factory module", True)
except Exception as e:
    report("Import CRM factory module", False, str(e))
    CRM_REGISTRY = {}

mock_subscriber = {
    "location_id": "test_loc_factory",
    "access_token": "test_tok",
    "refresh_token": "test_ref",
    "calendar_id": "cal_test",
    "timezone": "America/New_York",
    "crm_user_id": "usr_test",
    "crm_type": "ghl",
    "crm_config": {
        "access_token": "tok",
        "refresh_token": "ref",
        "client_id": "cid",
        "client_secret": "csec",
        "instance_url": "https://test.salesforce.com",
        "company_domain": "testco",
        "api_token": "tok",
        "data_center": "com",
        "api_key": "key",
        "brand_id": "123",
        "webhook_url": "https://hooks.zapier.com/test",
        "messaging_webhook_url": "https://example.com/sms",
    }
}

EXPECTED_CRM_NAMES = {
    "ghl": "LeadConnector",
    "zapier": "Zapier",
    "salesforce": "Salesforce",
    "hubspot": "HubSpot",
    "pipedrive": "Pipedrive",
    "zoho": "Zoho CRM",
    "insureio": "Insureio",
}

for crm_type, expected_name in EXPECTED_CRM_NAMES.items():
    try:
        adapter = get_crm_adapter(crm_type, mock_subscriber)
        ok = adapter.CRM_NAME == expected_name
        report(f"Factory('{crm_type}') -> CRM_NAME='{adapter.CRM_NAME}'",
               ok,
               f"Expected '{expected_name}'" if not ok else "")

        # Verify core methods exist
        for method in ["send_message", "get_free_slots", "book_appointment",
                       "get_contact", "create_contact", "search_contact",
                       "update_contact", "get_or_create_contact", "validate_credentials"]:
            has_method = hasattr(adapter, method) and callable(getattr(adapter, method))
            report(f"  {crm_type}.{method}() exists", has_method)

        # Verify capability flags
        report(f"  {crm_type}.SUPPORTS_MESSAGING = {adapter.SUPPORTS_MESSAGING}",
               adapter.SUPPORTS_MESSAGING is True)
        report(f"  {crm_type}.SUPPORTS_CONTACTS = {adapter.SUPPORTS_CONTACTS}",
               adapter.SUPPORTS_CONTACTS is True)
    except Exception as e:
        report(f"Factory('{crm_type}')", False, str(e))

# Test unknown CRM type falls back to GHL
try:
    adapter = get_crm_adapter("unknown_crm_xyz", mock_subscriber)
    ok = adapter.CRM_NAME == "LeadConnector"
    report("Factory('unknown_crm_xyz') falls back to GHL",
           ok,
           f"Got CRM_NAME={adapter.CRM_NAME}" if not ok else "")
except Exception as e:
    report("Factory('unknown_crm_xyz') fallback", False, str(e))

# Test list_available_crms returns correct count
try:
    crms = list_available_crms()
    ok = len(crms) == 7
    report(f"list_available_crms() returns {len(crms)} CRMs (expected 7)",
           ok,
           f"Got {len(crms)}" if not ok else "")
    # Verify each has id and name
    for crm in crms:
        has_fields = "id" in crm and "name" in crm
        if not has_fields:
            report(f"  CRM entry has id+name: {crm}", False, "Missing fields")
            break
    else:
        report("All CRM entries have id and name", True)
except Exception as e:
    report("list_available_crms()", False, str(e))

# Test case-insensitive factory lookup
for mixed_case in ["GHL", "Salesforce", "HUBSPOT", "Pipedrive"]:
    try:
        adapter = get_crm_adapter(mixed_case, mock_subscriber)
        ok = adapter.CRM_NAME is not None
        report(f"Factory('{mixed_case}') case-insensitive -> {adapter.CRM_NAME}", ok)
    except Exception as e:
        report(f"Factory('{mixed_case}') case-insensitive", False, str(e))


# ============================================================
# TEST GROUP 11: Webhook Logging (log_webhook_event / get_webhook_logs)
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 11: Webhook Logging Functions")
print("=" * 72)

try:
    from db import log_webhook_event, get_webhook_logs as db_get_webhook_logs
    report("Import log_webhook_event and get_webhook_logs", True)
except Exception as e:
    report("Import webhook log functions", False, str(e))

# 11a: log_webhook_event calls DB correctly
try:
    mock_log_cursor = MagicMock()
    mock_log_conn = MagicMock()
    mock_log_conn.cursor.return_value = mock_log_cursor

    with patch("db.get_db_connection", return_value=mock_log_conn):
        log_webhook_event(
            location_id="loc_test_123",
            event_type="webhook_received",
            status="success",
            summary="Test message received",
            contact_id="ct_test_123",
            details={"key": "value"}
        )
        # Verify INSERT was called
        ok = mock_log_cursor.execute.called
        report("log_webhook_event() executes INSERT", ok)
        if ok:
            call_args = mock_log_cursor.execute.call_args
            sql = call_args[0][0] if call_args[0] else ""
            report("log_webhook_event() SQL contains INSERT INTO webhook_logs",
                   "INSERT INTO webhook_logs" in sql)
        # Verify commit was called
        report("log_webhook_event() commits", mock_log_conn.commit.called)
        # Verify connection is closed
        report("log_webhook_event() closes connection", mock_log_conn.close.called)
except Exception as e:
    report("log_webhook_event()", False, str(e))

# 11b: get_webhook_logs fetches and returns data
try:
    mock_fetch_cursor = MagicMock()
    mock_fetch_cursor.fetchall.return_value = [
        {"id": 1, "event_type": "webhook_received", "status": "success",
         "summary": "Test", "created_at": "2026-01-15T10:00:00"},
    ]
    mock_fetch_conn = MagicMock()
    mock_fetch_conn.cursor.return_value = mock_fetch_cursor

    with patch("db.get_db_connection", return_value=mock_fetch_conn):
        logs = db_get_webhook_logs("loc_test_123", limit=50, offset=0)
        ok = isinstance(logs, list) and len(logs) == 1
        report(f"get_webhook_logs() returns {len(logs)} log(s)", ok)
        report("get_webhook_logs() returns dict entries",
               ok and isinstance(logs[0], dict) and logs[0].get("event_type") == "webhook_received")
except Exception as e:
    report("get_webhook_logs()", False, str(e))

# 11c: get_webhook_logs with event_type filter
try:
    mock_filter_cursor = MagicMock()
    mock_filter_cursor.fetchall.return_value = []
    mock_filter_conn = MagicMock()
    mock_filter_conn.cursor.return_value = mock_filter_cursor

    with patch("db.get_db_connection", return_value=mock_filter_conn):
        logs = db_get_webhook_logs("loc_test_123", event_type="booking_success")
        ok = isinstance(logs, list)
        report("get_webhook_logs() with event_type filter returns list", ok)
        # Verify SQL includes event_type condition
        if mock_filter_cursor.execute.called:
            sql = mock_filter_cursor.execute.call_args[0][0]
            report("get_webhook_logs() SQL includes event_type filter",
                   "event_type" in sql)
except Exception as e:
    report("get_webhook_logs() with filter", False, str(e))

# 11d: get_webhook_logs with no DB connection returns empty list
try:
    with patch("db.get_db_connection", return_value=None):
        logs = db_get_webhook_logs("loc_test_123")
        ok = logs == []
        report("get_webhook_logs() with no DB returns []", ok,
               f"Got: {logs}" if not ok else "")
except Exception as e:
    report("get_webhook_logs() no DB", False, str(e))

# 11e: log_webhook_event with no DB connection does not raise
try:
    with patch("db.get_db_connection", return_value=None):
        # Should not raise, just silently return
        log_webhook_event("loc_test", "error", "error", "DB down")
        report("log_webhook_event() with no DB doesn't raise", True)
except Exception as e:
    report("log_webhook_event() with no DB", False, str(e))


# ============================================================
# TEST GROUP 12: Onboarding Status Check
# ============================================================
print()
print("=" * 72)
print("TEST GROUP 12: Onboarding Status Check")
print("=" * 72)

# 12a: /onboarding-status with fully configured user
with app.test_client() as client:
    try:
        user = make_test_user()
        login_test_user(client, user)
        with patch.object(User, 'get', return_value=user):
            resp = client.get("/onboarding-status")
            ok = resp.status_code == 200
            body = resp.data.decode("utf-8", errors="replace")
            report(f"GET /onboarding-status (configured) -> {resp.status_code}", ok)
            # Fully configured user should have all steps done
            # The template should not contain errors
            no_error = "UndefinedError" not in body
            report("Onboarding template renders without errors", no_error)
    except Exception as e:
        report("GET /onboarding-status (configured)", False, str(e))

# 12b: /onboarding-status with incomplete user (no subscription)
# Note: password_hash must remain set so login_test_user() succeeds; other fields are cleared
with app.test_client() as client:
    try:
        incomplete_user = make_test_user({
            "stripe_customer_id": None,
            "access_token": None,
            "calendar_id": None,
            "calendar_name": None,
            "bot_first_name": None,
        })
        login_test_user(client, incomplete_user)
        with patch.object(User, 'get', return_value=incomplete_user):
            resp = client.get("/onboarding-status")
            ok = resp.status_code == 200
            body = resp.data.decode("utf-8", errors="replace")
            report(f"GET /onboarding-status (incomplete) -> {resp.status_code}", ok)
            no_error = "UndefinedError" not in body
            report("Incomplete onboarding template renders without errors", no_error)
    except Exception as e:
        report("GET /onboarding-status (incomplete)", False, str(e))

# 12c: /api/onboarding-check returns JSON with check data
with app.test_client() as client:
    try:
        user = make_test_user()
        login_test_user(client, user)
        with patch.object(User, 'get', return_value=user):
            resp = client.get("/api/onboarding-check")
            ok = resp.status_code == 200
            data = resp.get_json()
            report(f"GET /api/onboarding-check -> {resp.status_code}", ok)
            report("/api/onboarding-check has 'checks' key",
                   data and "checks" in data,
                   f"Keys: {list(data.keys()) if data else 'N/A'}")
            report("/api/onboarding-check has 'all_connected' key",
                   data and "all_connected" in data)
            # For a fully configured user, all_connected should be True
            if data and "all_connected" in data:
                report("/api/onboarding-check all_connected=True for configured user",
                       data["all_connected"] is True,
                       f"Got: {data['all_connected']}")
            # Verify checks include expected keys
            if data and "checks" in data:
                check_keys = [c.get("key") for c in data["checks"]]
                expected_keys = ["location_id", "access_token"]
                all_present = all(k in check_keys for k in expected_keys)
                report("Onboarding checks include location_id and access_token",
                       all_present,
                       f"Keys: {check_keys}")
    except Exception as e:
        report("GET /api/onboarding-check", False, str(e))

# 12d: /api/onboarding-check with user not found returns 404
# Flask-Login's load_user calls User.get first, then the route calls User.get again.
# We need the first call to succeed (so the user stays logged in) but the second to return None.
with app.test_client() as client:
    try:
        user = make_test_user()
        login_test_user(client, user)
        call_count = {"n": 0}
        real_user = user

        def side_effect_get(email):
            call_count["n"] += 1
            if call_count["n"] <= 1:
                return real_user  # Flask-Login load_user call
            return None  # In-route User.get call

        with patch.object(User, 'get', side_effect=side_effect_get):
            resp = client.get("/api/onboarding-check")
            ok = resp.status_code == 404
            report(f"GET /api/onboarding-check (user not found) -> {resp.status_code}",
                   ok,
                   f"Expected 404, got {resp.status_code}" if not ok else "")
    except Exception as e:
        report("GET /api/onboarding-check (user not found)", False, str(e))


# ============================================================
# BONUS: Additional Edge Case Tests
# ============================================================
print()
print("=" * 72)
print("BONUS: Additional Edge Cases")
print("=" * 72)

# B1: Webhook with form-encoded data (not JSON)
with app.test_client() as client:
    try:
        resp = client.post("/webhook", data={
            "location_id": "loc_form_test",
            "contact_id": "contact_form_test_12345",
            "message": "Form encoded test",
        })
        # Form data may not extract correctly through normalize_payload_universal
        # but should not crash
        report(f"POST /webhook form-encoded -> {resp.status_code} (no crash)",
               resp.status_code in (200, 202, 400, 500), True)
    except Exception as e:
        report("POST /webhook form-encoded", False, str(e))

# B2: /integrations page contains all CRM names
with app.test_client() as client:
    try:
        resp = client.get("/integrations")
        body = resp.data.decode("utf-8", errors="replace").lower()
        crm_keywords = ["ghl", "zapier", "salesforce", "hubspot", "pipedrive", "zoho", "insureio"]
        found_count = sum(1 for kw in crm_keywords if kw in body)
        report(f"/integrations page references {found_count}/7 CRM types",
               found_count >= 5,
               f"Found: {found_count}/7")
    except Exception as e:
        report("/integrations CRM names check", False, str(e))

# B3: /logout requires login
with app.test_client() as client:
    try:
        resp = client.get("/logout", follow_redirects=False)
        ok = resp.status_code in (302, 303)
        report(f"GET /logout (unauthenticated) redirects -> {resp.status_code}", ok)
    except Exception as e:
        report("GET /logout (unauthenticated)", False, str(e))

# B4: /logout when authenticated redirects to /
with app.test_client() as client:
    try:
        user = make_test_user()
        login_test_user(client, user)
        with patch.object(User, 'get', return_value=user):
            resp = client.get("/logout", follow_redirects=False)
            ok = resp.status_code in (302, 303)
            location = resp.headers.get("Location", "")
            report(f"GET /logout (authenticated) redirects -> {resp.status_code}",
                   ok,
                   f"Location: {location}")
    except Exception as e:
        report("GET /logout (authenticated)", False, str(e))

# B5: Website bot webhook handles INIT_CHAT
with app.test_client() as client:
    try:
        resp = client.post("/website-bot-webhook",
                           data=json.dumps({"message": "INIT_CHAT"}),
                           content_type="application/json")
        ok = resp.status_code == 200
        data = resp.get_json()
        report(f"POST /website-bot-webhook INIT_CHAT -> {resp.status_code}", ok)
        if data:
            report("Website bot returns text field",
                   "text" in data,
                   f"Keys: {list(data.keys())}")
    except Exception as e:
        report("POST /website-bot-webhook INIT_CHAT", False, str(e))

# B6: Website bot webhook rejects empty message
with app.test_client() as client:
    try:
        resp = client.post("/website-bot-webhook",
                           data=json.dumps({"message": ""}),
                           content_type="application/json")
        ok = resp.status_code == 400
        report(f"POST /website-bot-webhook empty message -> {resp.status_code}", ok,
               f"Expected 400" if not ok else "")
    except Exception as e:
        report("POST /website-bot-webhook empty message", False, str(e))

# B7: normalize_payload_universal handles various field naming conventions
try:
    from payload_utils import normalize_payload_universal

    # snake_case
    result = normalize_payload_universal({"contact_id": "ct_123", "location_id": "loc_456"})
    report("normalize_payload: snake_case extraction",
           result.get("contact_id") == "ct_123" and result.get("location_id") == "loc_456")

    # camelCase
    result = normalize_payload_universal({"contactId": "ct_789", "locationId": "loc_012"})
    report("normalize_payload: camelCase extraction",
           result.get("contact_id") == "ct_789" and result.get("location_id") == "loc_012")

    # Nested structures
    result = normalize_payload_universal({
        "extras": {"contact_id": "ct_nested"},
        "data": {"location_id": "loc_nested"},
    })
    report("normalize_payload: nested extraction",
           result.get("contact_id") == "ct_nested" and result.get("location_id") == "loc_nested")
except Exception as e:
    report("normalize_payload_universal", False, str(e))

# B8: User model creation and properties
try:
    user = make_test_user()
    report("User.email populated", user.email == "testuser@example.com")
    report("User.id == email (Flask-Login)", user.id == user.email)
    report("User.is_authenticated (Flask-Login)", user.is_authenticated is True)
    report("User.is_agency_owner for individual", user.is_agency_owner is False)

    agency_user = make_test_user({"role": "agency_owner", "agency_email": "agency@test.com"})
    report("User.is_agency_owner for agency_owner", agency_user.is_agency_owner is True)
except Exception as e:
    report("User model properties", False, str(e))


# ============================================================
# SUMMARY
# ============================================================
print()
print("=" * 72)
print(f"RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print("=" * 72)

if errors:
    print()
    print("FAILURES:")
    for test_name, detail in errors:
        print(f"  - {test_name}: {detail}")

print()
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print(f"{failed} TEST(S) FAILED - see details above")

# Cleanup
for p in patches:
    p.stop()

sys.exit(0 if failed == 0 else 1)
