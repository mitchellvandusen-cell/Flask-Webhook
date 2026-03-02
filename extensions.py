# extensions.py — Shared state, constants, and helper utilities for InsuranceGrokBot
#
# Rule: This module NEVER imports from main.py or any blueprint.
# Blueprints import from extensions.py; extensions.py does not import from blueprints.

import os
import logging
import redis
from rq import Queue
from functools import wraps
from flask import jsonify as flask_jsonify, request, abort
from flask_login import current_user
from flask_mail import Mail
from utils import make_json_serializable

# Flask-Mail instance — initialized with app.init_app(app) in main.py
mail = Mail()

logger = logging.getLogger(__name__)

# ── Admin / access control ────────────────────────────────────────────────────

ADMIN_EMAILS = [
    "admin",
    "mitchell_vandusen@hotmail.com",
    "mitchvandusenlife@gmail.com",
    "mitchell.vandusen@gmail.com",
]

# ── Domain ────────────────────────────────────────────────────────────────────

YOUR_DOMAIN = os.getenv("YOUR_DOMAIN", "http://localhost:8080")

# ── Demo constants ────────────────────────────────────────────────────────────

DEMO_CONTACT_ID = "demo_web_visitor"

# ── JSON helper ───────────────────────────────────────────────────────────────

def safe_jsonify(data):
    """Serialize data to JSON, converting non-serializable types gracefully."""
    return flask_jsonify(make_json_serializable(data))


# ── Admin request guard ───────────────────────────────────────────────────────

def _is_admin_request() -> bool:
    """
    Return True if the current request is authorized as admin via one of:
      1. Logged-in user whose email is in ADMIN_EMAILS
      2. ?key={CRON_SECRET} query parameter
      3. Authorization: Bearer {CRON_SECRET} header
    """
    try:
        if current_user.is_authenticated:
            if current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]:
                return True
    except Exception:
        pass

    cron_secret = os.getenv("CRON_SECRET", "")
    if cron_secret:
        auth_header = request.headers.get("Authorization", "")
        query_key = request.args.get("key", "")
        if auth_header == f"Bearer {cron_secret}" or query_key == cron_secret:
            return True

    return False


# ── Super-admin decorator ─────────────────────────────────────────────────────

def super_admin_required(f):
    """Route decorator: only allows users with role='super_admin'."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_super_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── xAI / OpenAI client ───────────────────────────────────────────────────────
# Initialized lazily so blueprints can import this module without needing an app context.

client = None  # Set to OpenAI instance by main.py at startup


def get_client():
    """Return the xAI client, initializing if needed."""
    global client
    if client is None:
        try:
            from openai import OpenAI
            api_key = os.getenv("XAI_API_KEY")
            if api_key:
                client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        except Exception as e:
            logger.error(f"Failed to initialize xAI client: {e}")
    return client


# ── Legacy Google Sheets (optional backup) ────────────────────────────────────
# Set by main.py during initialization if Google Sheets credentials are present.
gc = None
sheet_url = None


# ── Redis / RQ ────────────────────────────────────────────────────────────────

_redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
redis_conn = None
q_production = None
q_demo = None
q_website = None


def get_redis_connection():
    """Create a Redis connection with fast-fail timeouts."""
    return redis.from_url(
        _redis_url,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
        health_check_interval=30,
    )


def ensure_redis() -> bool:
    """Reconnect to Redis if the connection is dead. Returns True if healthy."""
    global redis_conn, q_production, q_demo, q_website
    try:
        if redis_conn:
            redis_conn.ping()
            return True
    except (redis.ConnectionError, redis.TimeoutError, OSError):
        logger.warning("⚠️ Redis connection lost, attempting reconnect...")
        redis_conn = None

    try:
        redis_conn = get_redis_connection()
        redis_conn.ping()
        q_production = Queue('production', connection=redis_conn)
        q_demo       = Queue('demo',       connection=redis_conn)
        q_website = Queue('website', connection=redis_conn)
        logger.info("✅ Redis connection established")
        return True
    except (redis.ConnectionError, redis.TimeoutError, OSError) as e:
        logger.error(f"❌ Redis reconnect failed: {e}")
        redis_conn = None
        q_production = None
        q_demo = None
        q_website = None
        return False
