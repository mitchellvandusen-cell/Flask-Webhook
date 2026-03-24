# main.py — Flask application factory and startup
#
# All HTTP routes live in blueprints/ (auth, public, webhooks, discord, slack,
# cron, billing, demo, admin, agency, dashboard, oauth, google_calendar,
# calendar, inbox).  This file wires them together with the Flask app, Redis,
# DB init, Flask-Login, Flask-Mail, i18n, and the voice WebSocket bridge.

import logging
import re
import os
import json
import hashlib
import glob as globmod
from datetime import timedelta
import redis
import stripe
import gspread
from openai import OpenAI
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from flask import Flask, request, send_from_directory
from flask import jsonify as flask_jsonify
# CSRF disabled globally via WTF_CSRF_ENABLED=False
from flask_wtf.csrf import generate_csrf  # still needed for template context
from flask_login import LoginManager, current_user
from rq import Queue

from db import init_db, User, clean_subaccount_contamination, backfill_agency_owners_to_subscribers
from sync_subscribers import sync_subscribers
from utils import make_json_serializable

load_dotenv()

app = Flask(__name__)

# ── Automatic Cache Busting ──────────────────────────────────────────────────
# Hash static CSS/JS files at startup. Changes to any file produce a new hash,
# forcing browsers to fetch fresh assets without manual version bumps.

def _compute_static_version():
    """Hash mtime of all CSS/JS files in static/ to produce a short cache-bust string."""
    h = hashlib.md5()
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    for fp in sorted(globmod.glob(os.path.join(static_dir, '**', '*'), recursive=True)):
        if fp.endswith(('.css', '.js')):
            try:
                h.update(f"{fp}:{os.path.getmtime(fp)}".encode())
            except OSError:
                pass
    return h.hexdigest()[:10]

STATIC_VERSION = _compute_static_version()
app.jinja_env.globals['_sv'] = STATIC_VERSION

# ── PII Redaction Filter for Production Logs ─────────────────────────────────

class PIIRedactionFilter(logging.Filter):
    """Redacts phone numbers and email addresses from log messages."""
    _phone_re = re.compile(r'\b(\+?1?[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')
    _email_re = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = self._phone_re.sub('[PHONE]', record.msg)
            record.msg = self._email_re.sub('[EMAIL]', record.msg)
        return True

_pii_filter = PIIRedactionFilter()
_handler = logging.StreamHandler()
_handler.addFilter(_pii_filter)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    handlers=[_handler],
    force=True,
)
logger = logging.getLogger(__name__)


def safe_jsonify(data):
    return flask_jsonify(make_json_serializable(data))


# ── Redis & RQ ───────────────────────────────────────────────────────────────

redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
redis_conn = None
q_production = None
q_demo = None
q_website = None
q_intelligence = None


def get_redis_connection():
    """Create a Redis connection with proper timeouts so it fails fast."""
    return redis.from_url(
        redis_url,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
        health_check_interval=30,
    )


def ensure_redis():
    """Reconnect to Redis if the connection is dead. Returns True if healthy."""
    global redis_conn, q_production, q_demo, q_website, q_intelligence
    try:
        if redis_conn:
            redis_conn.ping()
            return True
    except (redis.ConnectionError, redis.TimeoutError, OSError):
        logger.warning("Redis connection lost, attempting reconnect...")
        redis_conn = None

    try:
        redis_conn = get_redis_connection()
        redis_conn.ping()
        q_production    = Queue('production',   connection=redis_conn)
        q_demo          = Queue('demo',         connection=redis_conn)
        q_website       = Queue('website',      connection=redis_conn)
        q_intelligence  = Queue('intelligence', connection=redis_conn)
        logger.info("Redis connection established")
        return True
    except (redis.ConnectionError, redis.TimeoutError, OSError) as e:
        logger.error(f"Redis reconnect failed: {e}")
        redis_conn = None
        q_production = None
        q_demo = None
        q_website = None
        q_intelligence = None
        return False


ensure_redis()

# ── Error Feed (event-driven monitoring) ─────────────────────────────────────
from error_feed import attach_error_handler, get_recent_errors
attach_error_handler("flask-webhook", lambda: redis_conn)

# ── Initialization ───────────────────────────────────────────────────────────

# sync_subscribers() — disabled, Google Sheet no longer used
init_db()

# Re-apply our logging config after init_db() — Alembic's fileConfig()
# reads alembic.ini and replaces the root logger (format + level).
# This restores our PII filter, format, and INFO level.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    handlers=[_handler],
    force=True,
)

# One-shot cleanup: wipe stale master-account Trust Hub / A2P data from sub-account voice_configs
clean_subaccount_contamination()
# Backfill: ensure agency owners exist in subscribers table for operational code
backfill_agency_owners_to_subscribers()

from token_encryption import initialize_encryption
initialize_encryption()

# ── Eagerly share GHL OAuth creds to Redis + DB for worker processes ──────────
# Workers lack GHL_CLIENT_ID/GHL_CLIENT_SECRET env vars and rely on the web
# service to publish them to Redis and DB (app_settings table).
# has_oauth_credentials() calls _load_oauth_credentials() which shares env-var
# creds to Redis + DB as a side effect.  Verify both destinations; retry if
# either failed (common during simultaneous service deploys on Railway).
from ghl_api import has_oauth_credentials as _check_oauth
_check_oauth()
try:
    import extensions as _ext
    _redis_ok = False
    _db_ok = False
    if _ext.ensure_redis():
        _redis_ok = bool(_ext.redis_conn.get("igb:ghl_oauth_creds"))
    # Check DB too — it's the persistent fallback when Redis is flushed
    try:
        from db_legacy import get_db_connection, return_db_connection as _ret_conn
        _ck = get_db_connection()
        if _ck:
            try:
                _cur = _ck.cursor()
                _cur.execute("SELECT 1 FROM app_settings WHERE key = 'ghl_oauth_creds'")
                _db_ok = bool(_cur.fetchone())
                _cur.close()
            finally:
                _ret_conn(_ck)
    except Exception:
        pass
    if not _redis_ok or not _db_ok:
        import time as _t; _t.sleep(2)
        _check_oauth(force_recheck=True)
        logger.info(f"OAuth cred publish retry: Redis={'OK' if _redis_ok else 'RETRIED'}, "
                   f"DB={'OK' if _db_ok else 'RETRIED'}")
except Exception:
    pass

# ── API v1 Blueprint ─────────────────────────────────────────────────────────

from api_v1 import api_bp
app.register_blueprint(api_bp)

# ── Voice Bridge Blueprint (HTTP routes only — WebSockets moved to FastAPI) ──
# WebSocket endpoints /voice/stream and /voice/listen-stream are handled by
# voice_server.py (FastAPI/uvicorn on port 8081). TwiML routes point there
# via VOICE_WSS_URL env var. All HTTP routes remain here in Flask.

from voice_bridge import voice_bp

app.register_blueprint(voice_bp)


# ── Session & cookie config ──────────────────────────────────────────────────

_secret = os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
if not _secret:
    # In production, a missing secret key means all sessions break on restart/scale.
    # Fail loudly so this is caught during deployment, not after users lose sessions.
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RENDER") or os.getenv("GUNICORN_CMD_ARGS"):
        raise RuntimeError(
            "FATAL: SESSION_SECRET or SECRET_KEY environment variable is not set. "
            "All user sessions will be invalidated on every restart or horizontal scale event. "
            "Set SESSION_SECRET to a stable random string (e.g., python -c 'import secrets; print(secrets.token_hex(32))')."
        )
    # Local development only — generate a random key with a loud warning
    import secrets as _s
    _secret = _s.token_hex(32)
    logger.warning("SESSION_SECRET / SECRET_KEY not set — using random key. "
                    "This is only acceptable for local development. "
                    "Set SESSION_SECRET before deploying to production.")
app.secret_key = _secret
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['SESSION_COOKIE_SECURE'] = True

# ── CSRF Protection — DISABLED ──────────────────────────────────────────────
# CSRF is disabled globally. The platform uses session-based auth with
# SameSite=None cookies over HTTPS, plus API routes use Bearer tokens.
# The overhead of maintaining per-blueprint CSRF exemptions was causing
# persistent 400 errors across the entire application.
app.config['WTF_CSRF_ENABLED'] = False


@app.before_request
def handle_cors_preflight():
    """Handle OPTIONS preflight requests for GHL Custom JS cross-origin calls.
    These endpoints use JWT Bearer tokens (not cookies) so credentials mode is not needed.
    Access-Control-Allow-Origin: * is safe here — auth is enforced by the JWT itself."""
    if request.method == 'OPTIONS':
        path = request.path
        if path.startswith('/api/ghl/') or path.startswith('/voice/'):
            resp = app.make_response('')
            resp.status_code = 204
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-Requested-With'
            resp.headers['Access-Control-Max-Age'] = '86400'
            return resp  # Short-circuits route processing for preflight


@app.after_request
def add_iframe_headers(response):
    """Security headers + selective iframe/CORS for embed and GHL Custom JS routes."""
    path = request.path

    # Only allow framing on routes that are designed to be embedded (CRM iframes, GHL Custom JS)
    if path.startswith('/embed/') or path.startswith('/api/ghl/') or path.startswith('/hubspot/crm-card'):
        response.headers.pop('X-Frame-Options', None)
        response.headers['Content-Security-Policy'] = "frame-ancestors *"
    else:
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'self'"

    # Microphone permission for voice routes
    if path.startswith('/voice/') or path.startswith('/embed/') or path.startswith('/dashboard'):
        response.headers['Permissions-Policy'] = 'microphone=*, camera=*, autoplay=*'

    # CORS for GHL Custom JS — uses JWT Bearer tokens, not cookies, so * origin is safe
    if path.startswith('/api/ghl/') or path.startswith('/voice/'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-Requested-With'
    return response


# ── PWA Service Worker (must be served from root scope) ──────────────────────

@app.route('/sw.js')
def service_worker():
    return send_from_directory(app.static_folder, 'sw.js',
                               mimetype='application/javascript')

@app.route('/manifest.json')
def pwa_manifest():
    return send_from_directory(app.static_folder, 'manifest.json',
                               mimetype='application/json')


# ── Shared state (extensions.py) ─────────────────────────────────────────────

import extensions as _ext

# xAI API client
XAI_API_KEY = os.getenv("XAI_API_KEY")
client = None
if XAI_API_KEY:
    client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
    _ext.client = client

# Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# Flask-Mail
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', os.getenv('MAIL_USERNAME'))
_ext.mail.init_app(app)

# Google Sheets (legacy backup)
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}"))
if creds_dict:
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        sheet_url = os.getenv("SUBSCRIBER_SHEET_EDIT_URL")
        if sheet_url:
            sh = gc.open_by_url(sheet_url)
            logger.info("Google Sheet connected")
        _ext.gc = gc
        _ext.sheet_url = sheet_url
    except Exception as e:
        logger.error(f"Google Sheet connection failed: {e}")

# ── Flask-Login ──────────────────────────────────────────────────────────────

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "auth.login"
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
app.config['REMEMBER_COOKIE_SECURE'] = True
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'


@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)


# ── i18n ─────────────────────────────────────────────────────────────────────

from translations import _t, detect_language, SUPPORTED_LANGUAGES


@app.context_processor
def inject_i18n():
    """Make _t() helper, current lang code, and language list available in every template."""
    if hasattr(current_user, 'preferred_language') and current_user.preferred_language:
        lang = current_user.preferred_language
    else:
        lang = detect_language(request.headers.get('Accept-Language', ''))
    return {"_t": _t, "lang": lang, "SUPPORTED_LANGUAGES": SUPPORTED_LANGUAGES,
            "csrf_token": generate_csrf}


# ── Blueprint registrations ──────────────────────────────────────────────────

from blueprints.auth import auth_bp
from blueprints.public import public_bp
from blueprints.webhooks import webhooks_bp
from blueprints.discord import discord_bp
from blueprints.slack import slack_bp
from blueprints.cron import cron_bp
from blueprints.billing import billing_bp
from blueprints.demo import demo_bp
from blueprints.admin import admin_bp
from blueprints.agency import agency_bp
from blueprints.dashboard import dashboard_bp
from blueprints.oauth import oauth_bp
from blueprints.google_calendar import google_calendar_bp
from blueprints.calendar import calendar_bp
from blueprints.inbox import inbox_bp
from voice.numbers import numbers_bp
from blueprints.team import team_bp
from blueprints.contacts_import import contacts_import_bp
from blueprints.workflows import workflows_bp

# HubSpot CRM integration blueprints
from crm_providers.hubspot.oauth import hubspot_oauth_bp
from crm_providers.hubspot.inbound import hubspot_webhook_bp
from crm_providers.hubspot.crm_card import hubspot_card_bp

# Embeddable panel routes (CRM iframes, Chrome extension)
from blueprints.embed import embed_bp

# GHL Custom JS API endpoints (JWT-authenticated)
from blueprints.ghl_embed import ghl_embed_bp

app.register_blueprint(auth_bp)
app.register_blueprint(public_bp)
app.register_blueprint(webhooks_bp)
app.register_blueprint(discord_bp)
app.register_blueprint(slack_bp)
app.register_blueprint(cron_bp)
app.register_blueprint(billing_bp)
app.register_blueprint(demo_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(agency_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(oauth_bp)
app.register_blueprint(google_calendar_bp)
app.register_blueprint(calendar_bp)
app.register_blueprint(inbox_bp)
app.register_blueprint(numbers_bp)
app.register_blueprint(team_bp)
app.register_blueprint(contacts_import_bp)
app.register_blueprint(workflows_bp)
app.register_blueprint(hubspot_oauth_bp)
app.register_blueprint(hubspot_webhook_bp)
app.register_blueprint(hubspot_card_bp)
app.register_blueprint(embed_bp)
app.register_blueprint(ghl_embed_bp)

# Dashboard AI Agent Assistant
from blueprints.assistant import assistant_bp
app.register_blueprint(assistant_bp)

logger.info("All modular blueprints registered successfully.")

# ── CSRF exemptions (must be AFTER blueprint registration) ───────────────────
# Webhooks, voice, API routes use token/signature auth, not session cookies.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
