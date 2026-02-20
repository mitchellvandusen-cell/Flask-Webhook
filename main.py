# main.py - Asynchronous Version (2026)
import logging
import re
import uuid
import stripe
import os
import gspread
import json
import redis
import requests
import secrets
import httpx
from openai import OpenAI
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from flask import Flask, render_template, render_template_string, request, redirect, url_for, flash, session, make_response, abort
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_mail import Mail, Message
from flask_wtf import FlaskForm
from flask import jsonify as flask_jsonify
from wtforms import StringField, PasswordField, SubmitField, TextAreaField, SelectField
from wtforms.validators import DataRequired, Email, EqualTo
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from rq import Queue
from psycopg2.extras import RealDictCursor

# === IMPORTS ===
from db import (get_subscriber_info_hybrid, get_db_connection, return_db_connection,
                init_db, User, get_db_connection_with_retry, log_webhook_event,
                save_persistent_alert, get_persistent_alerts, dismiss_persistent_alert,
                get_users_needing_reminders, mark_reminder_sent,
                save_marketplace_install, mark_install_oauth_complete,
                get_incomplete_installs, get_all_marketplace_installs,
                mark_setup_email_sent, find_marketplace_email,
                save_contracted_carriers, get_contracted_carriers,
                get_bot_settings, save_bot_settings, BOT_SETTINGS_DEFAULTS,
                create_api_key_for_user, revoke_api_key, save_outbound_webhook_url,
                get_ai_minute_balance, credit_ai_minutes, get_ai_minute_purchases,
                get_ai_minute_usage)
from carrier_list import CARRIER_LIST, CARRIER_MAP, get_carrier_names, validate_carrier_keys
from sync_subscribers import sync_subscribers
from reply_sanitizer import sanitize_reply
from llm_caller import generate_clean_reply

# === ADMIN WHITELIST (Free Access - No Subscription Required) ===
ADMIN_EMAILS = [
    "admin",
    "mitchell_vandusen@hotmail.com",
    "mitchvandusenlife@gmail.com",
    "mitchell.vandusen@gmail.com",
]
# CRITICAL IMPORT: This connects main.py to the logic in tasks.py
from tasks import process_webhook_task  
from memory import get_known_facts, get_narrative, get_recent_messages 
from individual_profile import build_comprehensive_profile 
from utils import make_json_serializable, clean_ai_reply
from prompt import CORE_UNIFIED_MINDSET, DEMO_OPENER_ADDITIONAL_INSTRUCTIONS
from contact_validator import validate_and_resolve_contact
load_dotenv()

app = Flask(__name__)

# --- PII Redaction Filter for Production Logs ---
class PIIRedactionFilter(logging.Filter):
    """Redacts phone numbers and email addresses from log messages."""
    import re as _re
    _phone_re = _re.compile(r'\b(\+?1?[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b')
    _email_re = _re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = self._phone_re.sub('[PHONE]', record.msg)
            record.msg = self._email_re.sub('[EMAIL]', record.msg)
        return True

# Logging - structured for production
_pii_filter = PIIRedactionFilter()
_handler = logging.StreamHandler()
_handler.addFilter(_pii_filter)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    handlers=[_handler]
)
logger = logging.getLogger(__name__)

def safe_jsonify(data):
    # Pass 'data' into the function so it can process the dictionary/list
    return flask_jsonify(make_json_serializable(data))

# === REDIS & RQ SETUP ===
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
redis_conn = None
q_production = None
q_demo = None

def get_redis_connection():
    """Create a Redis connection with proper timeouts so it fails fast instead of hanging."""
    return redis.from_url(
        redis_url,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
        health_check_interval=30,
    )

def ensure_redis():
    """Reconnect to Redis if the connection is dead. Returns True if healthy."""
    global redis_conn, q_production, q_demo
    try:
        if redis_conn:
            redis_conn.ping()
            return True
    except (redis.ConnectionError, redis.TimeoutError, OSError):
        logger.warning("⚠️ Redis connection lost, attempting reconnect...")
        redis_conn = None

    try:
        redis_conn = get_redis_connection()
        redis_conn.ping()  # Real connectivity check
        q_production = Queue('production', connection=redis_conn)
        q_demo       = Queue('demo',       connection=redis_conn)
        logger.info("✅ Redis connection established")
        return True
    except (redis.ConnectionError, redis.TimeoutError, OSError) as e:
        logger.error(f"❌ Redis reconnect failed: {e}")
        redis_conn = None
        q_production = None
        q_demo = None
        return False

# Initial connection at startup
ensure_redis()

# === INITIALIZATION ===
sync_subscribers()
init_db()

# === REGISTER API BLUEPRINT ===
from api_v1 import api_bp
app.register_blueprint(api_bp)

# === REGISTER VOICE BRIDGE BLUEPRINT + WEBSOCKET ===
from flask_sock import Sock
from voice_bridge import voice_bp, run_voice_stream
app.register_blueprint(voice_bp)
sock = Sock(app)

@sock.route('/voice/stream')
def ws_voice_stream(ws):
    """WebSocket endpoint for Twilio Media Streams <-> XAI Voice bridge."""
    run_voice_stream(ws)

# == SECRET SESSION ==
app.secret_key = os.getenv("SESSION_SECRET", "fallback-insecure-key")

# == SESSION COOKIE CONFIG (for iframe embedding in LeadConnector) ==
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['SESSION_COOKIE_SECURE'] = True

# === API CLIENT ===
XAI_API_KEY = os.getenv("XAI_API_KEY")
client = None
if XAI_API_KEY:
    client = OpenAI(
        api_key=XAI_API_KEY,
        base_url="https://api.x.ai/v1"
    )

# == STRIPE & DOMAIN ==
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
YOUR_DOMAIN = os.getenv("YOUR_DOMAIN", "http://localhost:8080")

# == FLASK-MAIL CONFIGURATION ==
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', os.getenv('MAIL_USERNAME'))
mail = Mail(app)

# Google Sheets Setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}"))

worksheet = None
if creds_dict:
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        sheet_url = os.getenv("SUBSCRIBER_SHEET_EDIT_URL")
        if sheet_url:
            sh = gc.open_by_url(sheet_url)
            worksheet = sh.sheet1
            logger.info("Google Sheet connected")
    except Exception as e:
        logger.error(f"Google Sheet connection failed: {e}")

# Flask-Login Setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

# Forms
class RegisterForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    location_id = StringField("Your Lead Connector Location ID", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    confirm = PasswordField("Confirm Password", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("Create Account")

class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Login")

class ConfigForm(FlaskForm):
    location_id = StringField("Location ID", validators=[DataRequired()])
    crm_user_id = StringField("CRM USER ID")
    calendar_id = StringField("Calendar ID", validators=[DataRequired()])
    timezone = StringField("Timezone (e.g. America/Chicago)", validators=[DataRequired()])
    bot_name = StringField("Bot First Name", validators=[DataRequired()])
    initial_message = StringField("Optional Initial Message")
    personal_website = StringField("Personal Website (optional)")
    submit = SubmitField("Save Settings")

class ReviewForm(FlaskForm):
    name = StringField("Full Name", validators=[DataRequired()])
    role = StringField("Job Title", validators=[DataRequired()])
    text = TextAreaField("Your Experience", validators=[DataRequired()])
    stars = SelectField("Rating", choices=[('5', '5 Stars'), ('4', '4 Stars'), ('3', '3 Stars'), ('2', '2 Stars'), ('1', '1 Star')], validators=[DataRequired()])
    submit = SubmitField("Submit Review")


@app.route('/api/demo/reset', methods=['POST'])
def demo_reset():
    # Call the bold function we just built
    opener = generate_demo_opener()
    return flask_jsonify({"message": opener})


@app.route('/api/fetch-calendars', methods=['GET'])
@login_required
def fetch_calendars():
    """
    Fetch all calendars from Lead Connector for the current user's location.
    Returns a list of calendars with id and name.
    """
    location_id = current_user.location_id
    access_token = current_user.access_token

    if not location_id or not access_token:
        return flask_jsonify({"error": "Missing location_id or access_token"}), 400

    # Handle demo mode
    if access_token == 'DEMO':
        return flask_jsonify({
            "calendars": [
                {"id": "demo_cal_1", "name": "Demo Calendar 1"},
                {"id": "demo_cal_2", "name": "Demo Calendar 2"}
            ]
        })

    # Unified endpoint works for both OAuth and PIT tokens
    url = f"https://services.leadconnectorhq.com/calendars/?locationId={location_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Extract calendar id and name from response
        calendars = []
        if 'calendars' in data:
            for cal in data['calendars']:
                calendars.append({
                    "id": cal.get('id'),
                    "name": cal.get('name', 'Unnamed Calendar')
                })

        return flask_jsonify({"calendars": calendars})

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch calendars for location {location_id}: {e}")
        return flask_jsonify({"error": "Failed to fetch calendars from Lead Connector"}), 500


def generate_demo_opener():
    if not client:
        return "Quick question are you still with that life insurance plan you mentioned before? There's some new living benefits people have been asking me about and I wanted to make sure yours doesnt just pay out when you're dead."
    try:
        system_content = (
            CORE_UNIFIED_MINDSET.format(bot_first_name="DEMOGROKBOT")
            + "\n\n"
            + DEMO_OPENER_ADDITIONAL_INSTRUCTIONS
        )
        # Generate opener with structural reasoning separation
        cleaned_content = generate_clean_reply(
            client=client,
            system_prompt=system_content,
            user_message="Generate unique opener.",
            bot_name="DEMOGROKBOT",
            max_tokens=130,
            temperature=0.8,
        )

        if not cleaned_content:
            logger.error(f"OPENER: LLM could not produce clean reply. Using fallback.")
            return "Quick question are you still with that life insurance plan you mentioned before? There's some new living benefits people have been asking me about and I wanted to make sure yours doesnt just pay out when you're dead."

        cleaned_content = cleaned_content.replace('"', '')
        # Run specific cleaner for opener formatting
        cleaned_content = clean_ai_reply(cleaned_content)

        # Ensure minimum quality
        if len(cleaned_content) < 10 or not any(c.isalpha() for c in cleaned_content):
            logger.error(f"🚨 OPENER BLOCKED LOW-QUALITY: '{cleaned_content}' - Using fallback")
            return "Quick question are you still with that life insurance plan you mentioned before? There's some new living benefits people have been asking me about and I wanted to make sure yours doesnt just pay out when you're dead."

        return cleaned_content
    except Exception as e:
        logger.error(f"Demo opener failed: {e}")
        return "Quick question are you still with that life insurance plan you mentioned before? There's some new living benefits people have been asking me about and I wanted to make sure yours doesnt just pay out when you're dead."

# =====================================================
#  FLEXIBLE FIELD EXTRACTION - Handles ALL Variations
# =====================================================
def extract_field_flexible(payload, field_name, search_nested=True):
    """
    Extract a field from payload supporting ALL naming conventions:
    - snake_case: contact_id, location_id, user_id
    - camelCase: contactId, locationId, userId
    - PascalCase: ContactId, LocationId, UserId
    - UPPERCASE: CONTACT_ID, CONTACTID, ContactID
    - With spaces: CONTACT ID, Contact Id

    Also searches nested structures (extras, data, meta) if enabled.

    Args:
        payload: The webhook payload dict
        field_name: The normalized field name (e.g., "contact_id")
        search_nested: Whether to search nested dicts (default True)

    Returns:
        The field value or None if not found
    """
    # Generate all possible variations
    base_name = field_name.replace("_", "").lower()  # e.g., "contactid"

    # Split into words (e.g., "contact_id" -> ["contact", "id"])
    words = field_name.split("_")

    variations = [
        field_name,                                    # contact_id
        field_name.upper(),                           # CONTACT_ID
        field_name.replace("_", " "),                 # contact id
        field_name.replace("_", " ").upper(),         # CONTACT ID
        field_name.replace("_", " ").title(),         # Contact Id
        base_name,                                    # contactid
        base_name.upper(),                            # CONTACTID
        "".join(w.capitalize() for w in words),       # ContactId (PascalCase)
        words[0] + "".join(w.capitalize() for w in words[1:]),  # contactId (camelCase)
    ]

    # Try all variations in root payload
    for var in variations:
        if var in payload:
            value = payload[var]
            if value is not None and str(value).strip():
                return value

    # Search nested structures if enabled
    if search_nested:
        nested_keys = ["extras", "data", "meta", "contact", "location", "user", "calendar"]
        for nested_key in nested_keys:
            if nested_key in payload and isinstance(payload[nested_key], dict):
                for var in variations:
                    if var in payload[nested_key]:
                        value = payload[nested_key][var]
                        if value is not None and str(value).strip():
                            return value

    return None

def normalize_payload_universal(payload):
    """
    Normalize ANY Lead Connector payload structure to consistent snake_case format.
    Handles marketplace apps, custom webhooks, and any future formats.
    """
    # Common ID fields to normalize
    id_fields = [
        "contact_id", "location_id", "user_id", "calendar_id",
        "appointment_id", "opportunity_id", "workflow_id", "company_id",
        "conversation_id", "message_id", "task_id", "pipeline_id"
    ]

    # Common data fields
    data_fields = [
        "first_name", "last_name", "full_name", "email", "phone",
        "address", "city", "state", "zip", "country",
        "age", "date_of_birth", "gender", "intent", "message",
        "agent", "status", "type", "direction", "body"
    ]

    all_fields = id_fields + data_fields

    normalized = {}

    # Extract all known fields using flexible search
    for field in all_fields:
        value = extract_field_flexible(payload, field, search_nested=True)
        if value is not None:
            normalized[field] = value

    # Preserve original payload for reference
    normalized["_original_payload"] = payload
    normalized["_is_marketplace"] = payload.get("isMarketplaceAction", False)

    return normalized

# =====================================================
#  THE ASYNC WEBHOOK ENDPOINT
# =====================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    if not ensure_redis():
        logger.critical("Redis/RQ unavailable after reconnect attempt")
        return flask_jsonify({"status": "error", "reason": "redis_unavailable"}), 503

    payload = request.get_json(silent=True) or request.form.to_dict() or {}

    # Normalize payload to accept all field name variations
    payload = normalize_payload_universal(payload)

    # Extract values
    location_id = payload.get("location_id")
    contact_id = payload.get("contact_id")
    message_body = payload.get("message") or payload.get("body")

    # Validate contact_id
    if not contact_id or str(contact_id).strip().lower() in ["unknown", "none", "null", ""] or len(str(contact_id).strip()) < 5:
        logger.critical(f"🚨 WEBHOOK REJECTED | contact_id={contact_id} | location_id={location_id}")
        import json
        logger.critical(f"🚨 Original payload: {json.dumps(payload.get('_original_payload', {}), default=str)}")
        return flask_jsonify({"status": "rejected", "reason": "invalid_contact_id"}), 400

    # Success - log and continue
    logger.info(f"📨 Webhook received and normalized | contact_id={contact_id} | location_id={location_id} | Passing to Redis")

    # 1. DEMO SPEED OPTIMIZATION: Write User Msg Immediately
    # This ensures the UI updates instantly when they hit send.
    if location_id in ['DEMO_LOC', 'DEMO'] and contact_id and message_body:
        conn = None
        cur = None
        try:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO contact_messages (contact_id, message_type, message_text)
                    VALUES (%s, 'lead', %s)
                    ON CONFLICT DO NOTHING
                """, (contact_id, message_body))
                conn.commit()
        except Exception as e:
            logger.error(f"Instant demo write failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if cur:
                cur.close()
            if conn:
                return_db_connection(conn)


#   2. Enqueue the Brain
    is_demo = location_id in ['DEMO_LOC', 'DEMO', 'TEST_LOCATION_456']
    is_reply = message_body and message_body.strip()

    for attempt in range(2):
        try:
            target_queue = q_demo if is_demo else q_production
            job = target_queue.enqueue(
                process_webhook_task,
                payload,
                job_timeout=120,
                result_ttl=86400,
                at_front=is_reply  # Replies skip to front of queue
            )
            return safe_jsonify({"status": "queued", "job_id": job.id}), 202

        except (redis.ConnectionError, redis.TimeoutError, OSError) as e:
            if attempt == 0:
                logger.warning(f"⚠️ Enqueue failed (attempt 1), reconnecting: {e}")
                if not ensure_redis():
                    logger.error("❌ Redis reconnect failed on retry")
                    return safe_jsonify({"status": "error", "reason": "redis_unavailable"}), 503
            else:
                logger.error(f"❌ Enqueue failed after retry: {e}")
                return safe_jsonify({"status": "error", "reason": "redis_enqueue_failed"}), 503

        except Exception as e:
            logger.error(f"Queue failed: {e}")
            return safe_jsonify({"status": "error"}), 500

# =====================================================
#  GHL MARKETPLACE APP.INSTALLED WEBHOOK
#  Captures install events even if OAuth redirect never fires
# =====================================================

@app.route("/webhook/app-installed", methods=["POST"])
def app_installed_webhook():
    """
    GHL Marketplace 'app.installed' webhook listener.

    When someone installs the app from GHL Marketplace, GHL sends this webhook
    BEFORE the OAuth redirect. This captures the install even if:
    - The redirect URL is broken
    - The user closes the tab
    - OAuth fails silently

    Configure in GHL Developer Portal > Webhooks > app.installed
    URL: https://yourdomain.com/webhook/app-installed
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    # Log immediately — this is critical for visibility
    logger.info(f"=== APP.INSTALLED WEBHOOK === payload keys: {list(payload.keys())}")
    log_webhook_event("marketplace", "app_installed", "info",
                      f"App install webhook received",
                      details={"payload": payload})

    # Extract what we can from the payload
    data = payload.get("data", payload)
    company_id = data.get("companyId") or data.get("company_id") or payload.get("companyId") or ""
    location_id = data.get("locationId") or data.get("location_id") or payload.get("locationId") or ""
    user_email = data.get("email") or data.get("userEmail") or ""
    user_name = data.get("name") or data.get("userName") or data.get("firstName") or ""

    # Save to marketplace_installs table
    install_id = save_marketplace_install(payload)

    if install_id:
        log_webhook_event("marketplace", "app_installed_saved", "success",
                          f"Install #{install_id} saved: company={company_id}, "
                          f"location={location_id}, email={user_email}, name={user_name}")

        # If we have an email, send a welcome/setup email immediately
        if user_email:
            try:
                from send_email_api import send_email_via_api
                domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
                display_name = user_name or "there"

                subject = "Welcome to InsuranceGrokBot — Complete Your Setup"
                html_body = _build_install_welcome_email(display_name, domain_url)
                text_body = (
                    f"Hi {display_name}, thanks for installing InsuranceGrokBot! "
                    f"Complete your setup to start converting leads automatically: "
                    f"{domain_url}/oauth/initiate"
                )
                sent = send_email_via_api(
                    to_email=user_email,
                    subject=subject,
                    html_body=html_body,
                    text_body=text_body
                )
                if sent:
                    mark_setup_email_sent(install_id)
                    log_webhook_event("marketplace", "install_welcome_email", "success",
                                      f"Welcome email sent to {user_email} for install #{install_id}")
                    logger.info(f"Install welcome email sent to {user_email}")
                else:
                    log_webhook_event("marketplace", "install_welcome_email", "error",
                                      f"Failed to send welcome email to {user_email}")
            except Exception as email_err:
                logger.error(f"Install welcome email error: {email_err}")
                log_webhook_event("marketplace", "install_welcome_email", "error",
                                  f"Email error: {email_err}")

        # Notify admin about new installs
        try:
            for admin_email in ADMIN_EMAILS[:1]:  # Notify first admin
                save_persistent_alert(
                    admin_email, location_id or "marketplace",
                    "new_install", "info",
                    "New Marketplace Install",
                    f"New app install: {user_name or 'Unknown'} ({user_email or 'no email'}) "
                    f"— Company: {company_id or 'N/A'}, Location: {location_id or 'N/A'}"
                )
        except Exception:
            pass

    return safe_jsonify({"status": "received", "install_id": install_id}), 200


def _build_install_welcome_email(name: str, domain_url: str) -> str:
    """Build premium welcome email for marketplace install — guides them through OAuth setup."""
    inner = f'''
<tr>
<td style="padding: 0 40px 30px;">
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">
        Welcome aboard, {name}!
    </h1>
    <p style="margin: 0; font-size: 16px; color: #aaa; line-height: 1.5;">
        You just installed <strong style="color: #00c853;">InsuranceGrokBot</strong> &mdash; your AI-powered
        insurance sales assistant that works your leads 24/7.
    </p>
</td>
</tr>

<!-- Setup Required Notice -->
<tr>
<td style="padding: 0 40px 25px;">
    <table cellpadding="0" cellspacing="0" width="100%" style="background: rgba(255,107,53,0.08); border: 1px solid rgba(255,107,53,0.2); border-radius: 12px;">
    <tr>
    <td style="padding: 20px 24px;">
        <p style="margin: 0 0 4px; font-size: 15px; font-weight: 700; color: #ff6b35;">
            One Step Left to Activate Your Bot
        </p>
        <p style="margin: 0; font-size: 14px; color: #ccc; line-height: 1.5;">
            Click the button below to connect your Lead Connector CRM. This authorizes InsuranceGrokBot
            to respond to your leads, book appointments, and manage conversations automatically.
        </p>
    </td>
    </tr>
    </table>
</td>
</tr>

<!-- CTA Button -->
<tr>
<td align="center" style="padding: 0 40px 30px;">
    <table cellpadding="0" cellspacing="0">
    <tr>
    <td style="background: linear-gradient(135deg, #00c853 0%, #00e676 100%); border-radius: 12px; padding: 16px 48px;">
        <a href="{domain_url}/oauth/initiate" style="color: #000; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: 0.5px;">
            Connect Your CRM Now &rarr;
        </a>
    </td>
    </tr>
    </table>
</td>
</tr>

<!-- What Happens Next -->
<tr>
<td style="padding: 0 40px 25px;">
    <h2 style="margin: 0 0 16px; font-size: 18px; font-weight: 700; color: #fff;">What Happens After You Connect:</h2>
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06);">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(0,200,83,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">1</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">Instant Lead Response</strong> &mdash; Bot replies within 5 seconds, 24/7</p>
                </td>
            </tr></table>
        </td>
    </tr>
    <tr>
        <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06);">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(0,200,83,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">2</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">Smart Qualification</strong> &mdash; Asks the right insurance questions automatically</p>
                </td>
            </tr></table>
        </td>
    </tr>
    <tr>
        <td style="padding: 10px 0;">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(0,200,83,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">3</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">Auto Book Appointments</strong> &mdash; Checks your calendar and books meetings</p>
                </td>
            </tr></table>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- Stats Row -->
<tr>
<td style="padding: 0 40px 30px;">
    <table cellpadding="0" cellspacing="0" width="100%" style="background: rgba(255,255,255,0.03); border-radius: 12px; border: 1px solid rgba(255,255,255,0.06);">
    <tr>
        <td align="center" style="padding: 20px; width: 33%; border-right: 1px solid rgba(255,255,255,0.06);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853;">5s</div>
            <div style="font-size: 12px; color: #888; margin-top: 4px;">Response Time</div>
        </td>
        <td align="center" style="padding: 20px; width: 34%; border-right: 1px solid rgba(255,255,255,0.06);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853;">24/7</div>
            <div style="font-size: 12px; color: #888; margin-top: 4px;">Always On</div>
        </td>
        <td align="center" style="padding: 20px; width: 33%;">
            <div style="font-size: 28px; font-weight: 800; color: #00c853;">Auto</div>
            <div style="font-size: 12px; color: #888; margin-top: 4px;">Booking</div>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- Secondary CTA -->
<tr>
<td style="padding: 0 40px 20px;">
    <p style="margin: 0; font-size: 13px; color: #777; text-align: center;">
        Need help? Reply to this email or visit
        <a href="{domain_url}/support" style="color: #00c853; text-decoration: none;">our support page</a>.
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)


# =====================================================
#  BELOW THIS LINE: KEEP YOUR EXISTING @app.route("/")
#  AND OTHER UI CODE EXACTLY AS IT IS
# =====================================================
                    
@app.route("/")
def home():
    return render_template('home.html')

@app.route("/comparison")
def comparison():
    return render_template('comparison.html')

@app.route("/comparison/text-drip")
def comparison_text_drip():
    return render_template('comparison-text-drip.html')

@app.route("/dialer")
def dialer():
    return render_template('dialer.html')

@app.route("/getting-started")
def getting_started():
    return render_template('getting-started.html')

import uuid # Make sure this is imported at top of file

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if not endpoint_secret:
        logger.error("STRIPE_WEBHOOK_SECRET env var is not set — cannot verify webhook signature")
        return '', 500

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook signature failed")
        return '', 400
    except Exception as e:
        logger.error(f"Stripe webhook error: {e}")
        return '', 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session.customer
        email = session.customer_details.email.lower() if session.customer_details.email else None

        # ── AI Minutes one-time purchase ──
        if session.metadata.get("purchase_type") == "ai_minutes" and email:
            pkg_minutes = int(session.metadata.get("package_minutes", 0))
            pkg_label = session.metadata.get("package_label", "")
            amount = session.amount_total or 0
            credit_ai_minutes(
                email=email,
                minutes=pkg_minutes,
                stripe_session_id=session.id,
                stripe_payment_intent=session.payment_intent,
                package_label=pkg_label,
                amount_cents=amount,
            )
            logger.info(f"✅ AI Minutes: Credited {pkg_minutes} minutes to {email}")
            return '', 200

        # 1. EXTRACT METADATA
        target_role = session.metadata.get("target_role", "individual")
        target_tier = session.metadata.get("target_tier", "individual")

        if email and customer_id:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    
                    # 2. PROVISION SUBSCRIBER (The "Merged" Table)
                    # We generate a temp ID because 'location_id' cannot be null
                    temp_id = f"temp_{uuid.uuid4().hex[:8]}"
                    
                    cur.execute("""
                        INSERT INTO subscribers (
                            location_id, email, stripe_customer_id, role, subscription_tier,
                            crm_user_id, bot_first_name, timezone
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (email) DO UPDATE SET
                            stripe_customer_id = EXCLUDED.stripe_customer_id,
                            role = EXCLUDED.role,
                            subscription_tier = EXCLUDED.subscription_tier;
                    """, (temp_id, email, customer_id, target_role, target_tier,
                          '', 'Grok', 'America/Chicago'))
                    
                    # 3. SYNC TO AGENCY BILLING TABLE (Optional Redundancy)
                    if target_role == "agency_owner":
                        max_seats = 10 if target_tier == "starter" else 9999
                        # Fixed column name from 'tier' to 'subscription_tier' to match your DB schema
                        cur.execute("""
                            INSERT INTO agency_billing (agency_email, subscription_tier, max_seats, active_seats)
                            VALUES (%s, %s, %s, 0)
                            ON CONFLICT (agency_email) DO UPDATE SET
                                subscription_tier = EXCLUDED.subscription_tier,
                                max_seats = EXCLUDED.max_seats;
                        """, (email, target_tier, max_seats))

                    conn.commit()
                    logger.info(f"✅ Provisioned {target_tier.upper()} {target_role} account for: {email}")

                    # 4. REDUNDANT SYNC TO GOOGLE SHEETS (Optional Backup)
                    # You can keep this block if you still want the backup
                    try:
                        from main import gc, sheet_url
                        if gc and sheet_url:
                            sh = gc.open_by_url(sheet_url)
                            user_sheet = sh.worksheet("Users") # You might want to rename this tab to 'Subscribers' in sheets too later
                            user_sheet.append_row([email, "", "", "", "", target_role, customer_id, datetime.now().isoformat()])
                    except Exception as sheet_err:
                        logger.warning(f"Sheet redundant sync skipped: {sheet_err}")

                except Exception as e:
                    logger.error(f"Post-checkout database sync failed: {e}")
                    conn.rollback()
                finally:
                    cur.close()
                    return_db_connection(conn)

    return '', 200
@app.route("/register", methods=["GET", "POST"])
def register():
    """
    Registration - Marketplace Only.
    User must have already installed the app from the Lead Connector Marketplace,
    which creates their record via /oauth/callback.
    This page just lets them set a password.

    Sub-users should use /claim-account instead.
    """
    form = RegisterForm()

    # Pre-fill from OAuth redirect
    if request.method == "GET":
        url_location_id = request.args.get('location_id')
        if url_location_id:
            form.location_id.data = url_location_id
            flash("Lead Connector connected! Your location ID is pre-filled. Set a password to finish.", "success")

    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        submitted_location_id = form.location_id.data.strip()
        password = form.password.data

        # 1. Check if email already registered → redirect to login
        existing_user = User.get(email)
        if existing_user:
            flash("Email already registered. Please log in.", "info")
            return redirect(url_for("login"))

        conn = get_db_connection()
        if not conn:
            flash("Database unavailable. Please try again later.", "error")
            return redirect("/register")

        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)

            # 2. Check if location_id already exists in subscribers (from OAuth/Marketplace)
            cur.execute("""
                SELECT email, parent_agency_email, onboarding_status
                FROM subscribers
                WHERE location_id = %s
                LIMIT 1
            """, (submitted_location_id,))
            match = cur.fetchone()

            if not match:
                # Location not found → user hasn't installed from Marketplace yet
                flash("Location ID not found. You must install the app from the Lead Connector Marketplace first.", "error")
                return redirect("/register")

            password_hash = generate_password_hash(password)

            # Check if this is a sub-user who should use /claim-account
            if match['parent_agency_email'] and match.get('onboarding_status') == 'invited':
                flash("This is a sub-account. Please use the invitation link sent to your email to claim your account.", "info")
                return redirect(url_for("login"))

            # Verify email matches (security check)
            if match['email'] != email:
                flash("Location ID does not match your email. Please reconnect via OAuth.", "error")
                return redirect("/register")

            # Update password in existing record
            cur.execute("""
                UPDATE subscribers
                SET password_hash = %s,
                    onboarding_status = 'claimed',
                    updated_at = NOW()
                WHERE location_id = %s
            """, (password_hash, submitted_location_id))

            conn.commit()
            logger.info(f"Post-OAuth registration completed: {email}")
            flash("Account created successfully! Welcome aboard.", "success")
            return redirect(url_for("login"))

        except Exception as e:
            conn.rollback()
            logger.error(f"Registration failed for {email}: {e}")
            flash("Account creation failed. Please try again or contact support.", "error")
            return redirect("/register")
        finally:
            cur.close()
            return_db_connection(conn)

    return render_template('register.html', form=form)

# Updated /login (pull from subscribers table)
@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
   
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        print(f"[LOGIN DEBUG] Attempting login for: '{email}'")
       
        # Fetch user from subscribers table
        user = User.get(email)
       
        if not user:
            print("[LOGIN DEBUG] No user found in subscribers table")
            flash("No account found with that email.", "error")
            return render_template("login.html", form=form)

        if not user.password_hash:
            print("[LOGIN DEBUG] User has no password set yet")
            flash("You haven't set a password yet. Please check your email or complete checkout first.", "error")
            return render_template("login.html", form=form)

        if not check_password_hash(user.password_hash, form.password.data):
            print("[LOGIN DEBUG] Incorrect password")
            flash("Incorrect password.", "error")
            return render_template("login.html", form=form)
       
        # Password correct → log in
        print("[LOGIN DEBUG] Login successful - role:", user.role)
        login_user(user)

        # Always route to the appropriate dashboard
        # The dashboard itself handles showing what needs to be done
        # (pulsing Connect button, missing fields, subscription prompt, etc.)
        role = (user.role or 'individual').lower()
        is_admin = user.email.lower() in [e.lower() for e in ADMIN_EMAILS]

        # Admins go to individual dashboard by default (agency-dashboard is optional)
        if is_admin:
            return redirect(url_for("dashboard"))
        elif role in ['agency_owner']:
            return redirect(url_for("agency_dashboard"))
        else:
            return redirect(url_for("dashboard"))
    return render_template("login.html", form=form)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/")


# ============================================================
# FORGOT / RESET PASSWORD
# ============================================================

def _get_reset_serializer():
    return URLSafeTimedSerializer(app.config['SECRET_KEY'])


def _send_reset_email(to_email, reset_url):
    """Send a password-reset link via Flask-Mail."""
    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #2563eb;">Password Reset</h2>
            <p>We received a request to reset your InsuranceGrokBot password.</p>
            <p>Click the button below to choose a new password:</p>
            <div style="text-align: center; margin: 30px 0;">
                <a href="{reset_url}"
                   style="background-color: #2563eb; color: white; padding: 14px 28px;
                          text-decoration: none; border-radius: 8px; font-weight: bold;
                          display: inline-block;">
                    Reset My Password
                </a>
            </div>
            <p style="color: #666; font-size: 14px;">
                This link expires in 30 minutes. If you didn't request this,
                you can safely ignore this email.
            </p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #999; font-size: 12px;">
                InsuranceGrokBot - AI-Powered Insurance Sales Assistant<br>
                <a href="{YOUR_DOMAIN}" style="color: #2563eb;">{YOUR_DOMAIN}</a>
            </p>
        </div>
    </body>
    </html>
    """
    msg = Message(
        subject="InsuranceGrokBot - Password Reset",
        recipients=[to_email],
        html=html_body,
        body=f"Reset your password: {reset_url}\n\nThis link expires in 30 minutes."
    )
    mail.send(msg)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot-password.html")

    email = request.form.get("email", "").strip().lower()
    # Always show the same message regardless of whether the account exists
    flash("If an account is registered with that email, you will receive a reset link.", "info")

    if email:
        # Check both subscribers and agency_billing tables
        user = User.get(email)
        if not user:
            # Try agency_billing
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor(cursor_factory=RealDictCursor)
                    cur.execute("SELECT agency_email FROM agency_billing WHERE agency_email = %s", (email,))
                    user = cur.fetchone()
                except Exception:
                    pass
                finally:
                    return_db_connection(conn)

        if user:
            try:
                s = _get_reset_serializer()
                token = s.dumps(email, salt='password-reset')
                reset_url = f"{YOUR_DOMAIN}/reset-password/{token}"
                _send_reset_email(email, reset_url)
                logger.info(f"Password reset email sent to {email}")
            except Exception as e:
                logger.error(f"Failed to send reset email to {email}: {e}")

    return redirect("/forgot-password")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    # Validate the token (30-minute expiry)
    s = _get_reset_serializer()
    try:
        email = s.loads(token, salt='password-reset', max_age=1800)
    except SignatureExpired:
        flash("This reset link has expired. Please request a new one.", "error")
        return redirect("/forgot-password")
    except BadSignature:
        flash("Invalid reset link.", "error")
        return redirect("/forgot-password")

    if request.method == "GET":
        return render_template("reset-password.html", token=token, email=email)

    # POST: Save new password
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if not password or len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(f"/reset-password/{token}")

    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(f"/reset-password/{token}")

    password_hash = generate_password_hash(password)
    conn = get_db_connection()
    if not conn:
        flash("Database unavailable. Please try again.", "error")
        return redirect(f"/reset-password/{token}")

    try:
        cur = conn.cursor()
        # Update subscribers table
        cur.execute("""
            UPDATE subscribers SET password_hash = %s, updated_at = NOW()
            WHERE LOWER(email) = %s
        """, (password_hash, email.lower()))

        # Also update agency_billing if they're an agency owner
        cur.execute("""
            UPDATE agency_billing SET password_hash = %s, updated_at = NOW()
            WHERE LOWER(agency_email) = %s
        """, (password_hash, email.lower()))

        conn.commit()
        logger.info(f"Password reset completed for {email}")
        flash("Password reset successfully! You can now log in.", "success")
        return redirect("/login")
    except Exception as e:
        conn.rollback()
        logger.error(f"Password reset DB error for {email}: {e}")
        flash("Something went wrong. Please try again.", "error")
        return redirect(f"/reset-password/{token}")
    finally:
        cur.close()
        return_db_connection(conn)


@app.route("/agency-dashboard", methods=["GET", "POST"])
@login_required
def agency_dashboard():
    # 1. Security Check — admins can always access
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    if current_user.role != 'agency_owner' and not is_admin:
        flash("Access restricted to agency owners only.", "error")
        return redirect("/dashboard")

    # --- SUBSCRIPTION VERIFICATION ---
    # Check if agency owner has active Stripe subscription
    # Admin whitelist bypasses subscription requirement
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    needs_subscription = not current_user.stripe_customer_id and not is_admin

    if needs_subscription:
        # Enforce the tier detected during onboarding (prevents large agency buying starter)
        detected_tier = current_user.subscription_tier or 'agency_starter'
        return render_template('agency-dashboard.html',
            needs_subscription=True,
            detected_tier=detected_tier,
            agency_starter_price=797.99,   # Agency Starter: $797.99/month
            agency_pro_price=1597.99,      # Agency Pro: $1597.99/month
            form=ConfigForm(),  # Empty form
            access_token_display='',
            refresh_token_display='',
            token_readonly='',
            expires_in_str='',
            sub=current_user,
            profile={
                'full_name': current_user.full_name or '',
                'phone': current_user.phone or '',
                'bio': current_user.bio or ''
            },
            carrier_list=CARRIER_LIST,
            selected_carriers=[],
            bot_settings=dict(BOT_SETTINGS_DEFAULTS),
            sub_accounts=[],
            stats={
                'max_seats': 0,
                'active_seats': 0,
                'tier': 'Not Subscribed'
            },
            user=current_user
        )

    conn = get_db_connection()
    if not conn:
        flash("System error: Database unavailable.", "error")
        return redirect("/dashboard")
    form = ConfigForm()
    # --- 1. HANDLE SAVING CONFIG (POST) ---
    if request.method == 'POST' and not form.validate_on_submit():
        logger.warning(f"Agency form validation failed for {current_user.email}: {form.errors}")
        flash("Please fill in all required fields.", "error")

    if form.validate_on_submit():
        if not conn:
            flash("Database connection failed", "error")
        else:
            try:
                cur = conn.cursor()
                # Get calendar_name from hidden field
                calendar_name = request.form.get('calendar_name', '')

                # Update the AGENCY_BILLING table for owner config
                cur.execute("""
                    UPDATE agency_billing
                    SET location_id = %s,
                        calendar_id = %s,
                        calendar_name = %s,
                        crm_user_id = %s,
                        bot_first_name = %s,
                        timezone = %s,
                        initial_message = %s,
                        personal_website = %s,
                        updated_at = NOW()
                    WHERE agency_email = %s
                """, (
                    form.location_id.data,
                    form.calendar_id.data,
                    calendar_name,
                    form.crm_user_id.data,
                    form.bot_name.data,
                    form.timezone.data,
                    form.initial_message.data,
                    form.personal_website.data or None,
                    current_user.email
                ))
                conn.commit()
                flash("Settings saved successfully!", "success")
                return redirect(url_for('agency_dashboard'))
            except Exception as e:
                conn.rollback()
                flash(f"Error saving settings: {str(e)}", "error")
            finally:
                cur.close()
    # --- 2. PRE-FILL FORM (GET) ---
    if request.method == 'GET':
        form.location_id.data = current_user.location_id
        form.calendar_id.data = current_user.calendar_id
        form.crm_user_id.data = current_user.crm_user_id
        form.bot_name.data = current_user.bot_first_name
        form.timezone.data = current_user.timezone
        form.initial_message.data = current_user.initial_message
        form.personal_website.data = current_user.personal_website
    # --- 3. TOKEN LOGIC ---
    access_token_display = ''
    refresh_token_display = ''
    expires_in_str = ''
    token_field_state = ''
    if current_user.access_token:
        token_field_state = 'readonly'
        at = current_user.access_token
        access_token_display = at[:8] + '...' + at[-4:] if len(at) > 12 else at
       
        # Calculate Expiry
        if current_user.token_expires_at:
            expires_at = current_user.token_expires_at
            if isinstance(expires_at, str):
                try: expires_at = datetime.fromisoformat(expires_at)
                except: expires_at = datetime.now()
               
            delta = expires_at - datetime.now()
            if delta.total_seconds() > 0:
                expires_in_str = f"Expires in {int(delta.total_seconds() // 3600)}h {int((delta.total_seconds() % 3600) // 60)}m"
            else:
                expires_in_str = "Token Expired"
        else:
            expires_in_str = "Persistent"
    # --- 4. PROFILE DATA ---
    profile = {
        'full_name': current_user.full_name or '',
        'phone': current_user.phone or '',
        'bio': current_user.bio or ''
    }
    # Data Containers
    sub_accounts = []
    agency_stats = {
        'max_seats': 10,       # Default fallback
        'active_seats': 0,
        'tier': 'Agency Starter'
    }
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 2. Fetch Agency Billing Specs (The Limits)
        cur.execute("""
            SELECT subscription_tier, max_seats
            FROM agency_billing
            WHERE agency_email = %s
        """, (current_user.email,))
        billing_row = cur.fetchone()
       
        if billing_row:
            agency_stats['max_seats'] = billing_row['max_seats']
            agency_stats['tier'] = billing_row['subscription_tier'].replace('_', ' ').title()
        # 3. Fetch All Sub-Accounts (The Agents)
        # We grab everything needed to display the list and status
        cur.execute("""
            SELECT
                location_id,
                full_name,          -- This holds the Location Name (from onboarding)
                email,              -- Owner/agent email
                bot_first_name,
                timezone,
                access_token,       -- Used to check connection status
                subscription_tier,
                token_expires_at,
                created_at,
                refresh_token,      -- Added for display
                onboarding_status,  -- pending/invited/claimed
                invite_sent_at      -- When invitation was sent
            FROM subscribers
            WHERE parent_agency_email = %s
            ORDER BY created_at DESC
        """, (current_user.email,))
       
        raw_subs = cur.fetchall()
       
        # 4. Process for Display (Robust Status Checking)
        current_time = datetime.now()
       
        for sub in raw_subs:
            # Determine if the bot is actually active for this location
            # Logic: Must have an access token AND it shouldn't be expired (if expiry exists)
            is_connected = False
            if sub['access_token']:
                if sub['token_expires_at']:
                    # Convert string to datetime if needed (psycopg2 usually handles this)
                    expires = sub['token_expires_at']
                    if isinstance(expires, str):
                        try: expires = datetime.fromisoformat(expires)
                        except: expires = datetime.now() # Fail safe
                   
                    is_connected = expires > current_time
                else:
                    is_connected = True # Persistent token
           
            sub_accounts.append({
                'name': sub['full_name'] or 'Unnamed Location',
                'location_id': sub['location_id'],
                'email': sub['email'] or 'No Email Assigned',
                'agent_email': sub['email'] or 'No Agent Email',
                'status': 'Active' if is_connected else 'Pending Auth',
                'status_class': 'success' if is_connected else 'warning',
                'tier': sub['subscription_tier'].replace('_', ' ').title(),
                'bot_name': sub['bot_first_name'],
                'timezone': sub['timezone'],
                'access_token': sub['access_token'],  # For display (truncated in template)
                'refresh_token': sub['refresh_token'],  # Added
                'onboarding_status': sub['onboarding_status'] or 'pending',
                'invite_sent_at': sub['invite_sent_at']
            })
        # 5. Self-Healing Stats
        # Instead of trusting the counter in the billing table, we count the REAL rows.
        agency_stats['active_seats'] = len(sub_accounts)
    except Exception as e:
        logger.error(f"Agency Dashboard Error: {e}")
        flash("Error loading agency data.", "error")
    finally:
        cur.close()
        return_db_connection(conn)
    agency_carriers = get_contracted_carriers(current_user.email)
    agency_bot_settings = get_bot_settings(current_user.email)
    return render_template('agency-dashboard.html',
                           form=form,
                           access_token_display=access_token_display,
                           refresh_token_display=refresh_token_display,
                           token_readonly=token_field_state,
                           expires_in_str=expires_in_str,
                           sub=current_user,
                           profile=profile,
                           sub_accounts=sub_accounts,
                           stats=agency_stats,
                           user=current_user,
                           carrier_list=CARRIER_LIST,
                           selected_carriers=agency_carriers,
                           bot_settings=agency_bot_settings)
def save_profile():
    data = request.get_json()
    if not data:
        return flask_jsonify({"error": "No data provided"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500

    try:
        cur = conn.cursor()
        
        # Update the User table
        cur.execute("""
            UPDATE users 
            SET user_name = %s,
                phone = %s,
                bio = %s
            WHERE email = %s
        """, (
            data.get('name'), 
            data.get('phone'), 
            data.get('bio'), 
            current_user.email
        ))
        
        conn.commit()
        return flask_jsonify({"status": "success", "message": "Profile updated"})
        
    except Exception as e:
        conn.rollback()
        return flask_jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)

@app.route("/app")
def app_entry():
    """Smart entry point for GHL Custom Page sidebar link.
    Handles all states: logged in/out, setup complete/incomplete."""
    if current_user.is_authenticated:
        # Check if setup is complete
        is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
        has_token = bool(current_user.access_token)
        has_subscription = bool(current_user.stripe_customer_id) or is_admin
        loc_ok = bool(current_user.location_id and not str(current_user.location_id).startswith("temp_"))
        cal_ok = bool(current_user.calendar_id)
        bot_ok = bool(current_user.bot_first_name)
        setup_complete = has_token and has_subscription and loc_ok and cal_ok and bot_ok

        if setup_complete:
            if current_user.role == 'agency_owner':
                return redirect("/agency-dashboard")
            return redirect("/dashboard")
        return redirect("/onboarding-status")

    # Not logged in — send to login (they installed from GHL, they have an account)
    return redirect("/login")

@app.route("/onboarding-status")
@login_required
def onboarding_status():
    """Live onboarding checkpoint — reads real account state."""
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]

    has_token = bool(current_user.access_token)
    has_password = bool(current_user.password_hash)
    has_subscription = bool(current_user.stripe_customer_id) or is_admin
    loc_ok = bool(current_user.location_id and not str(current_user.location_id).startswith("temp_"))
    cal_ok = bool(current_user.calendar_id)
    bot_ok = bool(current_user.bot_first_name)
    config_ok = loc_ok and cal_ok and bot_ok
    all_done = has_token and has_password and has_subscription and config_ok

    # Shared URL variables for next_url logic and step action buttons
    user_type = 'agency' if current_user.role == 'agency_owner' else 'individual'
    dashboard_url = '/agency-dashboard' if current_user.role == 'agency_owner' else '/dashboard'
    tier = current_user.subscription_tier or 'individual'
    if tier == 'agency_pro':
        checkout_url = '/checkout/agency-pro'
    elif tier == 'agency_starter':
        checkout_url = '/checkout/agency-starter'
    else:
        checkout_url = '/checkout'

    # Figure out the correct "next action" URL (priority order)
    if not has_subscription:
        next_url = checkout_url
    elif not has_password:
        next_url = f'/set-password?type={user_type}'
    elif not has_token:
        next_url = '/oauth/initiate'
    else:
        next_url = dashboard_url

    steps = [
        {"label": "Connect Your CRM", "done": has_token, "icon": "fa-plug",
         "help": "Links your Lead Connector account so the bot can read and send messages.",
         "url": "/oauth/initiate", "button_text": "Connect Now"},
        {"label": "Activate Subscription", "done": has_subscription, "icon": "fa-credit-card",
         "help": "Choose your plan to turn on all bot features.",
         "url": checkout_url, "button_text": "Subscribe Now"},
        {"label": "Create Your Password", "done": has_password, "icon": "fa-lock",
         "help": "You'll use this to log in from now on.",
         "url": f"/set-password?type={user_type}", "button_text": "Set Password"},
        {"label": "Configure Your Bot", "done": config_ok, "icon": "fa-sliders",
         "help": "Pick your calendar, name your bot, and confirm your location.",
         "url": dashboard_url, "button_text": "Open Dashboard"},
    ]

    completed_count = sum(1 for s in steps if s["done"])

    return render_template('onboarding-status.html',
        steps=steps,
        completed_count=completed_count,
        total_steps=len(steps),
        all_done=all_done,
        next_url=next_url,
    )


@app.route("/support")
def support_page():
    """Self-service support and troubleshooting hub."""
    return render_template('support.html')

@app.route("/setup-guide")
def setup_guide():
    """Comprehensive step-by-step setup guide."""
    return render_template('setup-guide.html')


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    if current_user.role == 'agency_owner':
        return redirect(url_for("agency_dashboard"))

    # --- SUBSCRIPTION VERIFICATION ---
    # Check if user has active Stripe subscription
    # Admin whitelist bypasses subscription requirement
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    needs_subscription = not current_user.stripe_customer_id and not is_admin

    if needs_subscription:
        # User needs to subscribe - show subscription required page
        return render_template('dashboard.html',
            needs_subscription=True,
            subscription_price=98.99,  # Individual plan: $98.99/month
            form=ConfigForm(),  # Empty form
            access_token_display='',
            refresh_token_display='',
            token_readonly='',
            expires_in_str='',
            sub=current_user,
            profile={
                'full_name': current_user.full_name or '',
                'phone': current_user.phone or '',
                'bio': current_user.bio or ''
            }
        )

    form = ConfigForm()
    conn = get_db_connection()
   
    # --- 1. HANDLE SAVING CONFIG (POST) ---
    if request.method == 'POST' and not form.validate_on_submit():
        # Log validation failures so they're never silent again
        logger.warning(f"Dashboard form validation failed for {current_user.email}: {form.errors}")
        flash("Please fill in all required fields.", "error")

    if form.validate_on_submit():
        if not conn:
            flash("Database connection failed", "error")
        else:
            try:
                cur = conn.cursor()
                # Get calendar_name from hidden field
                calendar_name = request.form.get('calendar_name', '')

                # Update the SUBSCRIBERS table
                cur.execute("""
                    UPDATE subscribers
                    SET location_id = %s,
                        calendar_id = %s,
                        calendar_name = %s,
                        crm_user_id = %s,
                        bot_first_name = %s,
                        timezone = %s,
                        initial_message = %s,
                        personal_website = %s,
                        updated_at = NOW()
                    WHERE email = %s
                """, (
                    form.location_id.data,
                    form.calendar_id.data,
                    calendar_name,
                    form.crm_user_id.data,
                    form.bot_name.data,
                    form.timezone.data,
                    form.initial_message.data,
                    form.personal_website.data or None,
                    current_user.email
                ))
                conn.commit()
                flash("Settings saved successfully!", "success")
                return redirect(url_for('dashboard'))
            except Exception as e:
                conn.rollback()
                flash(f"Error saving settings: {str(e)}", "error")
            finally:
                cur.close()
                return_db_connection(conn)
    # --- 2. PRE-FILL FORM (GET) ---
    # Since current_user is now loaded from 'subscribers', we can use it directly
    if request.method == 'GET':
        form.location_id.data = current_user.location_id
        form.calendar_id.data = current_user.calendar_id
        form.crm_user_id.data = current_user.crm_user_id
        form.bot_name.data = current_user.bot_first_name
        form.timezone.data = current_user.timezone
        form.initial_message.data = current_user.initial_message
        form.personal_website.data = current_user.personal_website
    # --- 3. TOKEN LOGIC ---
    # We can read this directly from current_user now too!
    access_token_display = ''
    refresh_token_display = ''
    expires_in_str = ''
    token_field_state = ''
    if current_user.access_token:
        token_field_state = 'readonly'
        at = current_user.access_token
        access_token_display = at[:8] + '...' + at[-4:] if len(at) > 12 else at
       
        # Calculate Expiry
        if current_user.token_expires_at:
            expires_at = current_user.token_expires_at
            # Handle string vs datetime object just in case
            if isinstance(expires_at, str):
                try: expires_at = datetime.fromisoformat(expires_at)
                except: expires_at = datetime.now()
               
            delta = expires_at - datetime.now()
            if delta.total_seconds() > 0:
                expires_in_str = f"Expires in {int(delta.total_seconds() // 3600)}h {int((delta.total_seconds() % 3600) // 60)}m"
            else:
                expires_in_str = "Token Expired"
        else:
            expires_in_str = "Persistent"
    # --- 4. PROFILE DATA ---
    profile = {
        'full_name': current_user.full_name or '',
        'phone': current_user.phone or '',
        'bio': current_user.bio or ''
    }
    # --- 5. ONBOARDING STATE FLAGS ---
    needs_oauth = not bool(current_user.access_token)
    show_congrats = request.args.get('setup') == 'complete'

    loc_ok = bool(current_user.location_id and not str(current_user.location_id).startswith("temp_"))
    cal_ok = bool(current_user.calendar_id)
    bot_ok = bool(current_user.bot_first_name)
    tz_ok = bool(current_user.timezone)
    msg_ok = bool(current_user.initial_message)

    missing_fields = []
    if not bot_ok:
        missing_fields.append('bot_name')
    if not tz_ok:
        missing_fields.append('timezone')
    if not msg_ok:
        missing_fields.append('initial_message')
    if not loc_ok:
        missing_fields.append('location_id')
    if not cal_ok:
        missing_fields.append('calendar_id')

    # --- 6. PLACEHOLDER / INCOMPLETE ACCOUNT DETECTION ---
    is_placeholder = bool(current_user.email and current_user.email.endswith('@placeholder.grokbot'))
    is_incomplete = bool(not current_user.crm_user_id or not current_user.location_id)

    # --- 7. CONTRACTED CARRIERS ---
    selected_carriers = get_contracted_carriers(current_user.email)

    # --- 8. BOT SETTINGS ---
    bot_settings = get_bot_settings(current_user.email)

    from crm_adapters.factory import CRM_CONFIG_FIELDS, CRM_DISPLAY_NAMES

    # --- 9. VOICE CONFIG ---
    voice_config = current_user.voice_config or {}

    return render_template('dashboard.html',
        form=form,
        access_token_display=access_token_display,
        refresh_token_display=refresh_token_display,
        token_readonly=token_field_state,
        expires_in_str=expires_in_str,
        sub=current_user,
        profile=profile,
        needs_oauth=needs_oauth,
        show_congrats=show_congrats,
        missing_fields=missing_fields,
        is_placeholder=is_placeholder,
        is_incomplete=is_incomplete,
        carrier_list=CARRIER_LIST,
        selected_carriers=selected_carriers,
        bot_settings=bot_settings,
        crm_config_fields=CRM_CONFIG_FIELDS,
        crm_display_names=CRM_DISPLAY_NAMES,
        voice_config=voice_config
    )
@app.route("/save-profile", methods=["POST"])
@login_required
def save_profile():
    data = request.get_json()
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        if current_user.role == 'agency_owner':
            # Update AGENCY_BILLING
            cur.execute("""
                UPDATE agency_billing
                SET full_name = %s,
                    phone = %s,
                    bio = %s,
                    updated_at = NOW()
                WHERE agency_email = %s
            """, (
                data.get('name'),
                data.get('phone'),
                data.get('bio'),
                current_user.email
            ))
        else:
            # Update SUBSCRIBERS
            cur.execute("""
                UPDATE subscribers
                SET full_name = %s,
                    phone = %s,
                    bio = %s,
                    updated_at = NOW()
                WHERE email = %s
            """, (
                data.get('name'),
                data.get('phone'),
                data.get('bio'),
                current_user.email
            ))
        conn.commit()
        return flask_jsonify({"status": "success", "message": "Profile updated"})
    except Exception as e:
        conn.rollback()
        return flask_jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)

@app.route("/api/voice-config", methods=["GET"])
@login_required
def get_voice_config():
    """Return current voice configuration."""
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        config = (row['voice_config'] if row else {}) or {}
        return flask_jsonify({"voice_config": config})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)


@app.route("/api/voice-config", methods=["POST"])
@login_required
def save_voice_config():
    """Save voice AI configuration (voice settings, dialer preferences)."""
    data = request.get_json()
    if not data:
        return flask_jsonify({"error": "No data provided"}), 400

    # Load existing voice_config to preserve auto-provisioned fields
    # (e.g. twilio_sub_account_sid, twilio_phone_number, etc.)
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500
    existing_vc = {}
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if row and row['voice_config']:
            existing_vc = row['voice_config'] if isinstance(row['voice_config'], dict) else {}
    except Exception:
        pass
    finally:
        return_db_connection(conn)

    # Start from existing config to preserve provisioned IDs and number pools
    voice_config = dict(existing_vc)
    # Update with form-submitted fields (user-facing settings only)
    voice_config.update({
        "enabled":               bool(data.get("enabled", False)),
        "voice":                 (data.get("voice") or "ara").strip().lower(),
        "voice_bot_name":        (data.get("voice_bot_name") or "").strip(),
        "voice_instructions":    (data.get("voice_instructions") or "").strip(),
        "call_script":           (data.get("call_script") or "").strip(),
        # Dialer settings
        "dial_attempts":         int(data.get("dial_attempts") or 2),
        "auto_record":           bool(data.get("auto_record", True)),
        "auto_transcribe":       bool(data.get("auto_transcribe", False)),
        "local_presence":        bool(data.get("local_presence", False)),
        "transfer_number":       (data.get("transfer_number") or "").strip(),
    })
    # Twilio sub-account fields are set by auto-provisioning only — never
    # overwritten by the user saving voice settings from the dashboard.

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers
            SET voice_config = %s::jsonb,
                updated_at = NOW()
            WHERE email = %s
        """, (json.dumps(voice_config), current_user.email))
        rows_updated = cur.rowcount
        conn.commit()
        cur.close()
        if rows_updated == 0:
            logger.error(f"Voice config save matched 0 rows for email={current_user.email!r}")
            return flask_jsonify({"error": "Account not found — please log out and back in"}), 400
        logger.info(f"Voice config saved for {current_user.email}: enabled={voice_config['enabled']}")
        return flask_jsonify({"status": "success", "voice_config": voice_config})
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save voice config: {e}")
        return flask_jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)


@app.route("/api/carriers", methods=["GET"])
@login_required
def get_carriers():
    """Return the master carrier list + this agent's selections."""
    selected = get_contracted_carriers(current_user.email)
    return flask_jsonify({
        "carriers": CARRIER_LIST,
        "selected": selected
    })

@app.route("/api/carriers", methods=["POST"])
@login_required
def save_carriers():
    """Save this agent's contracted carrier selections."""
    data = request.get_json()
    if not data or "carriers" not in data:
        return flask_jsonify({"error": "Missing carriers list"}), 400
    carriers = validate_carrier_keys(data["carriers"])
    ok = save_contracted_carriers(current_user.email, carriers)
    if ok:
        return flask_jsonify({"status": "success", "saved": carriers, "count": len(carriers)})
    return flask_jsonify({"error": "Failed to save carriers"}), 500


@app.route("/api/bot-settings", methods=["GET"])
@login_required
def get_bot_settings_api():
    """Return this subscriber's bot settings merged with defaults."""
    settings = get_bot_settings(current_user.email)
    return flask_jsonify({"settings": settings, "defaults": BOT_SETTINGS_DEFAULTS})

@app.route("/api/bot-settings", methods=["POST"])
@login_required
def save_bot_settings_api():
    """Save this subscriber's bot settings."""
    data = request.get_json()
    if not data or "settings" not in data:
        return flask_jsonify({"error": "Missing settings object"}), 400

    incoming = data["settings"]
    # Validate and sanitize — only allow known keys
    clean = {}
    for key, default_val in BOT_SETTINGS_DEFAULTS.items():
        if key in incoming:
            val = incoming[key]
            # Type-check against the default
            if isinstance(default_val, bool):
                clean[key] = bool(val)
            elif isinstance(default_val, int):
                clean[key] = max(0, min(5, int(val)))  # clamp 0-5
            elif isinstance(default_val, list):
                clean[key] = val if isinstance(val, list) else []
            elif isinstance(default_val, str):
                clean[key] = str(val)[:2000]  # cap length
            else:
                clean[key] = val

    ok = save_bot_settings(current_user.email, clean)
    if ok:
        return flask_jsonify({"status": "success", "saved": clean})
    return flask_jsonify({"error": "Failed to save settings"}), 500


# ═══════════════════════════════════════════════════════════════
# API KEY MANAGEMENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route("/api/generate-key", methods=["POST"])
@login_required
def generate_key_endpoint():
    """Generate a new API key + webhook secret for the authenticated user."""
    result = create_api_key_for_user(current_user.email)
    if "error" in result:
        return flask_jsonify({"error": result["error"]}), 500
    return flask_jsonify({
        "status": "success",
        "api_key": result["api_key"],
        "webhook_secret": result["webhook_secret"],
        "message": "Store your API key securely. It will not be shown again in full."
    })


@app.route("/api/revoke-key", methods=["POST"])
@login_required
def revoke_key_endpoint():
    """Revoke the user's current API key."""
    ok = revoke_api_key(current_user.email)
    if ok:
        return flask_jsonify({"status": "success", "message": "API key revoked."})
    return flask_jsonify({"error": "Failed to revoke key"}), 500


@app.route("/api/webhook-url", methods=["POST"])
@login_required
def save_webhook_url_endpoint():
    """Save the user's outbound webhook URL for API reply delivery."""
    data = request.get_json()
    if not data or not data.get("url"):
        return flask_jsonify({"error": "Missing 'url' field"}), 400
    url = data["url"].strip()
    if not url.startswith("https://"):
        return flask_jsonify({"error": "Webhook URL must use HTTPS"}), 400
    ok = save_outbound_webhook_url(current_user.email, url)
    if ok:
        return flask_jsonify({"status": "success", "url": url})
    return flask_jsonify({"error": "Failed to save webhook URL"}), 500


@app.route("/api/api-status", methods=["GET"])
@login_required
def api_status_endpoint():
    """Return current API key status for dashboard display."""
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 503
    try:
        cur = conn.cursor()
        # Check subscribers first
        cur.execute("""
            SELECT api_key, webhook_secret, outbound_webhook_url, api_key_created_at
            FROM subscribers WHERE email = %s LIMIT 1
        """, (current_user.email,))
        row = cur.fetchone()
        if not row:
            cur.execute("""
                SELECT api_key, webhook_secret, outbound_webhook_url, api_key_created_at
                FROM agency_billing WHERE agency_email = %s LIMIT 1
            """, (current_user.email,))
            row = cur.fetchone()
        cur.close()
        if not row:
            return flask_jsonify({"has_key": False})

        api_key = row.get("api_key") or ""
        return flask_jsonify({
            "has_key": bool(api_key),
            "key_prefix": (api_key[:12] + "..." + api_key[-4:]) if len(api_key) > 16 else "",
            "webhook_url": row.get("outbound_webhook_url") or "",
            "webhook_secret_preview": (row.get("webhook_secret") or "")[:10] + "..." if row.get("webhook_secret") else "",
            "created_at": str(row.get("api_key_created_at") or ""),
        })
    except Exception as e:
        logger.error(f"api_status_endpoint error: {e}")
        return flask_jsonify({"error": "Failed to fetch status"}), 500
    finally:
        return_db_connection(conn)


@app.route("/create-portal-session", methods=["POST"])
@login_required
def create_portal_session():
    try:
        # Get stripe_customer_id from current logged-in user
        if not current_user.stripe_customer_id:
            flash("No subscription found! Please subscribe first", "error")
            return redirect("/dashboard")

        session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=f"{YOUR_DOMAIN}/dashboard",
        )
        return redirect(session.url)
    except Exception as e:
        logger.error(f"Portal error: {e}")
        flash("Unable to open billing portal", "error")
        return redirect("/dashboard")


# At the top, add a demo-specific contact ID
DEMO_CONTACT_ID = "demo_web_visitor"

def run_demo_janitor():
    """
    Deletes all demo data older than 30 minutes.
    Keeps the DB very light.
    """
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()

        # 1. Clean Messages
        cur.execute("""
            DELETE FROM contact_messages
            WHERE contact_id LIKE 'demo_%'
            AND created_at < NOW() - INTERVAL '30 minutes';
        """)

        # 2. Clean Facts
        cur.execute("""
            DELETE FROM contact_facts
            WHERE contact_id LIKE 'demo_%'
            AND created_at < NOW() - INTERVAL '30 minutes';
        """)

        # 3. Clean Narratives
        cur.execute("""
            DELETE FROM contact_narratives
            WHERE contact_id LIKE 'demo_%'
            AND updated_at < NOW() - INTERVAL '30 minutes';
        """)

        conn.commit()

    except Exception as e:
        logger.error(f"Janitor cleanup failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            cur.close()
        except Exception:
            pass
        return_db_connection(conn)

@app.route("/integrations")
def integrations_page():
    """Public-facing integrations page showing supported CRM platforms."""
    from crm_adapters.factory import list_available_crms, CRM_DISPLAY_NAMES
    crms = list_available_crms()
    return render_template('integrations.html', crms=crms, crm_names=CRM_DISPLAY_NAMES)


@app.route("/api/save-config", methods=["POST"])
@login_required
def api_save_config():
    """AJAX endpoint to save bot configuration. Returns JSON for overlay feedback."""
    data = request.get_json()
    if not data:
        return safe_jsonify({"success": False, "error": "No data provided"}), 400

    conn = get_db_connection()
    if not conn:
        return safe_jsonify({"success": False, "error": "Database connection failed"}), 500

    try:
        cur = conn.cursor()
        calendar_name = data.get('calendar_name', '')

        if current_user.role == 'agency_owner':
            cur.execute("""
                UPDATE agency_billing
                SET location_id = %s,
                    calendar_id = %s,
                    calendar_name = %s,
                    crm_user_id = %s,
                    bot_first_name = %s,
                    timezone = %s,
                    initial_message = %s,
                    personal_website = %s,
                    updated_at = NOW()
                WHERE agency_email = %s
            """, (
                data.get('location_id', ''),
                data.get('calendar_id', ''),
                calendar_name,
                data.get('crm_user_id', ''),
                data.get('bot_name', ''),
                data.get('timezone', ''),
                data.get('initial_message', ''),
                data.get('personal_website') or None,
                current_user.email
            ))
        else:
            cur.execute("""
                UPDATE subscribers
                SET location_id = %s,
                    calendar_id = %s,
                    calendar_name = %s,
                    crm_user_id = %s,
                    bot_first_name = %s,
                    timezone = %s,
                    initial_message = %s,
                    personal_website = %s,
                    updated_at = NOW()
                WHERE email = %s
            """, (
                data.get('location_id', ''),
                data.get('calendar_id', ''),
                calendar_name,
                data.get('crm_user_id', ''),
                data.get('bot_name', ''),
                data.get('timezone', ''),
                data.get('initial_message', ''),
                data.get('personal_website') or None,
                current_user.email
            ))

        rows = cur.rowcount
        conn.commit()
        if rows == 0:
            logger.error(f"API config save: UPDATE matched 0 rows for {current_user.email} "
                        f"(role={current_user.role}). Row may not exist in the target table.")
            return safe_jsonify({"success": False, "error": "No matching account found in database"}), 404
        logger.info(f"Config saved via API for {current_user.email} ({rows} row updated)")
        return safe_jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        logger.error(f"API config save failed for {current_user.email}: {e}")
        return safe_jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


@app.route("/api/integrations/save", methods=["POST"])
@login_required
def save_integration_config():
    """Save CRM integration settings for the logged-in subscriber."""
    data = request.get_json()
    if not data:
        return safe_jsonify({"error": "No data provided"}), 400

    crm_type = data.get("crm_type", "ghl").strip().lower()
    crm_config = data.get("crm_config", {})

    # Validate crm_type
    from crm_adapters.factory import CRM_REGISTRY
    if crm_type not in CRM_REGISTRY:
        return safe_jsonify({"error": f"Unsupported CRM type: {crm_type}"}), 400

    conn_db = get_db_connection()
    if not conn_db:
        return safe_jsonify({"error": "Database error"}), 500

    try:
        cur = conn_db.cursor()
        # Update both tables depending on user type
        if current_user.role == 'agency_owner':
            cur.execute("""
                UPDATE agency_billing SET crm_type = %s, crm_config = %s, updated_at = NOW()
                WHERE agency_email = %s
            """, (crm_type, json.dumps(crm_config), current_user.email))
        else:
            cur.execute("""
                UPDATE subscribers SET crm_type = %s, crm_config = %s, updated_at = NOW()
                WHERE email = %s
            """, (crm_type, json.dumps(crm_config), current_user.email))

        conn_db.commit()
        logger.info(f"Integration saved: {crm_type} for {current_user.email}")
        return safe_jsonify({"success": True, "crm_type": crm_type})
    except Exception as e:
        logger.error(f"Failed to save integration config: {e}")
        conn_db.rollback()
        return safe_jsonify({"error": "Failed to save configuration"}), 500
    finally:
        cur.close()
        return_db_connection(conn_db)


@app.route("/api/integrations/test", methods=["POST"])
@login_required
def test_integration():
    """Test CRM credentials by attempting a validation call."""
    data = request.get_json()
    if not data:
        return safe_jsonify({"error": "No data provided"}), 400

    crm_type = data.get("crm_type", "ghl").strip().lower()
    crm_config = data.get("crm_config", {})

    # Build a temporary subscriber_data dict for the adapter
    subscriber_data = {
        "access_token": crm_config.get("access_token", current_user.access_token or ""),
        "location_id": current_user.location_id or "",
        "calendar_id": current_user.calendar_id or "",
        "timezone": current_user.timezone or "America/Chicago",
        "crm_user_id": current_user.crm_user_id or "",
        "crm_type": crm_type,
        "crm_config": crm_config,
    }

    try:
        from crm_adapters.factory import get_crm_adapter
        adapter = get_crm_adapter(crm_type, subscriber_data)
        result = adapter.validate_credentials()
        return safe_jsonify(result)
    except Exception as e:
        logger.error(f"Integration test failed: {e}")
        return safe_jsonify({"valid": False, "message": str(e)}), 500


@app.route("/api/logs", methods=["GET"])
@login_required
def get_webhook_logs_api():
    """Fetch webhook logs for the current user's location."""
    from db import get_webhook_logs
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({"logs": [], "total": 0})

    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    event_type = request.args.get("event_type", "").strip() or None
    status_filter = request.args.get("status", "").strip() or None

    logs = get_webhook_logs(location_id, limit=limit, offset=offset,
                            event_type=event_type, status=status_filter)

    # Serialize datetime objects (append Z so JS knows it's UTC)
    for log in logs:
        if log.get("created_at"):
            log["created_at"] = log["created_at"].isoformat() + "Z"

    return safe_jsonify({"logs": logs, "total": len(logs)})


@app.route("/api/alerts", methods=["GET"])
@login_required
def api_get_alerts():
    """Fetch undismissed persistent alerts for the current user."""
    alerts = get_persistent_alerts(current_user.email)
    for a in alerts:
        if a.get("created_at"):
            a["created_at"] = a["created_at"].isoformat() + "Z"
    return safe_jsonify({"alerts": alerts})


@app.route("/api/alerts/<int:alert_id>/dismiss", methods=["POST"])
@login_required
def api_dismiss_alert(alert_id):
    """Dismiss a persistent alert."""
    dismiss_persistent_alert(alert_id, current_user.email)
    return safe_jsonify({"success": True})


def _is_admin_request():
    """
    Check if the current request is authorized as admin.
    Accepts EITHER:
      1. Logged-in user whose email is in ADMIN_EMAILS
      2. ?key={CRON_SECRET} query parameter
      3. Authorization: Bearer {CRON_SECRET} header
    Returns True if authorized.
    """
    # Method 1: Logged-in admin
    try:
        if current_user.is_authenticated:
            if current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]:
                return True
    except Exception:
        pass

    # Method 2: Secret key (same as cron endpoint)
    cron_secret = os.getenv("CRON_SECRET", "")
    if cron_secret:
        auth_header = request.headers.get("Authorization", "")
        query_key = request.args.get("key", "")
        if auth_header == f"Bearer {cron_secret}" or query_key == cron_secret:
            return True

    return False


# ══════════════════════════════════════════════════════════════════════════════
# SUPER ADMIN / GOD MODE
# ══════════════════════════════════════════════════════════════════════════════

def super_admin_required(f):
    """Decorator: only allows users with role='super_admin'."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_super_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/god-mode")
@login_required
@super_admin_required
def god_mode_dashboard():
    """Super Admin dashboard: view all users on the platform."""
    conn = get_db_connection()
    if not conn:
        return "Database error", 500
    try:
        cur = conn.cursor()
        # Pull everyone from both tables
        cur.execute("""
            SELECT email, full_name, role, subscription_tier, stripe_status,
                   location_id, created_at, onboarding_status, oauth_app_type,
                   'subscriber' AS source
            FROM subscribers
            ORDER BY created_at DESC
        """)
        subscribers = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT agency_email AS email, full_name, role, subscription_tier,
                   stripe_status, location_id, created_at,
                   'active' AS onboarding_status, oauth_app_type,
                   'agency_billing' AS source
            FROM agency_billing
            ORDER BY created_at DESC
        """)
        agency_owners = [dict(r) for r in cur.fetchall()]

        cur.close()

        # Merge, deduplicate by email (agency_billing wins for shared emails)
        seen = {}
        for u in agency_owners + subscribers:
            if u['email'] not in seen:
                seen[u['email']] = u
        all_users = sorted(seen.values(), key=lambda u: u.get('created_at') or '', reverse=True)

        return render_template('god_mode.html',
            all_users=all_users,
            impersonating=session.get('impersonating_as'),
        )
    finally:
        return_db_connection(conn)


@app.route("/admin/impersonate/<path:target_email>", methods=["POST"])
@login_required
@super_admin_required
def impersonate_user(target_email):
    """Log in as any user without their password. Saves original admin to session."""
    target = User.get(target_email)
    if not target:
        flash(f"User not found: {target_email}", "danger")
        return redirect(url_for('god_mode_dashboard'))

    # Stash the real admin email so we can return later
    session['original_admin_email'] = current_user.email
    session['impersonating_as'] = target.email

    login_user(target)
    logger.info(f"[GOD MODE] {session['original_admin_email']} impersonating {target.email}")

    # Drop them into the appropriate dashboard
    if target.is_agency_owner:
        return redirect(url_for('agency_dashboard'))
    return redirect(url_for('dashboard'))


@app.route("/admin/revert", methods=["POST"])
@login_required
def revert_impersonation():
    """Exit impersonation and return to the super admin account."""
    original_email = session.pop('original_admin_email', None)
    session.pop('impersonating_as', None)

    if original_email:
        admin_user = User.get(original_email)
        if admin_user:
            login_user(admin_user)
            logger.info(f"[GOD MODE] Reverted to {original_email}")
            return redirect(url_for('god_mode_dashboard'))

    return redirect(url_for('dashboard'))


@app.route("/admin/god-mode/logs/<path:location_id>")
@login_required
@super_admin_required
def god_mode_logs(location_id):
    """God Mode: view webhook logs for any location."""
    from db import get_webhook_logs
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = int(request.args.get("offset", 0))
    event_type = request.args.get("event_type", "").strip() or None
    status_filter = request.args.get("status", "").strip() or None

    logs = get_webhook_logs(location_id, limit=limit, offset=offset,
                            event_type=event_type, status=status_filter)
    for log in logs:
        if log.get("created_at"):
            log["created_at"] = log["created_at"].isoformat() + "Z"

    return safe_jsonify({"logs": logs, "total": len(logs), "location_id": location_id})


@app.route("/admin/god-mode/subscriber/<path:location_id>")
@login_required
@super_admin_required
def god_mode_subscriber_detail(location_id):
    """God Mode: view full subscriber details including oauth_app_type, token status."""
    conn = get_db_connection()
    if not conn:
        return safe_jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT location_id, email, full_name, role, subscription_tier,
                   stripe_status, oauth_app_type, access_token IS NOT NULL AS has_access_token,
                   refresh_token IS NOT NULL AS has_refresh_token, token_expires_at,
                   onboarding_status, created_at, updated_at
            FROM subscribers WHERE location_id = %s
        """, (location_id,))
        row = cur.fetchone()
        if not row:
            return safe_jsonify({"error": "Subscriber not found"}), 404
        data = dict(row)
        for k in ('token_expires_at', 'created_at', 'updated_at'):
            if data.get(k):
                data[k] = data[k].isoformat() + "Z"
        return safe_jsonify(data)
    finally:
        cur.close()
        return_db_connection(conn)


@app.route("/api/cron/send-reminders", methods=["GET", "POST"])
def api_send_reminders():
    """
    Cron-triggered endpoint: sends 24h and 72h reminder emails to users
    who installed but haven't subscribed yet.
    Accepts auth via:
      - Authorization: Bearer {CRON_SECRET} header
      - ?key={CRON_SECRET} query parameter (for cron services like cron-job.org)
    """
    cron_secret = os.getenv("CRON_SECRET", "")
    auth_header = request.headers.get("Authorization", "")
    query_key = request.args.get("key", "")
    authorized = cron_secret and (
        auth_header == f"Bearer {cron_secret}" or query_key == cron_secret
    )
    if not authorized:
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from send_email_api import send_email_via_api
        domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")

        users = get_users_needing_reminders()
        sent_count = 0
        errors = []

        # Skip admin emails — they don't need reminders
        admin_emails_lower = [e.lower() for e in ADMIN_EMAILS]

        for user in users:
            email = user.get("email")
            if not email or email.lower() in admin_emails_lower:
                continue

            name = user.get("full_name") or "there"
            reminder_type = user.get("reminder_type")
            user_type = user.get("user_type", "individual")
            missing = user.get("missing_fields", [])

            try:
                if reminder_type == "24h":
                    subject = "Your AI Sales Assistant is Ready — Let's Get You Live"
                    html_body = _build_reminder_24h_email(name, domain_url, user_type, missing)
                    text_body = (
                        f"Hi {name}, your InsuranceGrokBot account was created 24 hours ago. "
                        f"Complete your setup to start converting leads automatically: {domain_url}/dashboard"
                    )
                else:
                    subject = "You're Missing Leads Right Now — Activate InsuranceGrokBot"
                    html_body = _build_reminder_72h_email(name, domain_url, user_type, missing)
                    text_body = (
                        f"Hi {name}, it's been 3 days since you signed up for InsuranceGrokBot. "
                        f"Your bot is waiting to work your leads 24/7: {domain_url}/dashboard"
                    )

                sent = send_email_via_api(
                    to_email=email,
                    subject=subject,
                    html_body=html_body,
                    text_body=text_body
                )

                if sent:
                    mark_reminder_sent(email, reminder_type, user_type)
                    log_webhook_event(
                        user.get("location_id", "unknown"),
                        f"reminder_{reminder_type}",
                        "success",
                        f"{reminder_type} reminder sent to {email} (missing: {', '.join(missing)})"
                    )
                    sent_count += 1
                    logger.info(f"Reminder {reminder_type} sent to {email} | missing={missing}")
                else:
                    errors.append(f"{email}: send failed")
                    logger.warning(f"Reminder {reminder_type} failed for {email}")
            except Exception as e:
                errors.append(f"{email}: {str(e)}")
                logger.error(f"Reminder email error for {email}: {e}")

        return safe_jsonify({
            "success": True,
            "checked": len(users),
            "sent": sent_count,
            "errors": errors
        })

    except Exception as e:
        logger.error(f"Cron send-reminders crashed: {e}", exc_info=True)
        return safe_jsonify({
            "success": False,
            "error": str(e)
        }), 200  # Return 200 so cron-job.org doesn't mark as failed


@app.route("/api/admin/send-email", methods=["GET", "POST"])
def api_admin_send_email():
    """
    Admin endpoint: send an email to anyone.
    Usage: /api/admin/send-email?key=SECRET&to=email@example.com&subject=Hello&message=Your+message+here
    """
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required. Use ?key=YOUR_CRON_SECRET"}), 403

    to_email = request.args.get("to") or (request.get_json(silent=True) or {}).get("to")
    subject = request.args.get("subject", "Update from InsuranceGrokBot")
    message = request.args.get("message", "")

    if not to_email:
        return safe_jsonify({"error": "Missing 'to' parameter"}), 400
    if not message:
        return safe_jsonify({"error": "Missing 'message' parameter"}), 400

    from send_email_api import send_email_via_api
    domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")

    # Build a clean branded email with the custom message
    inner = f'''
<tr>
<td style="padding: 0 40px 30px;">
    <h1 style="margin: 0 0 16px; font-size: 24px; font-weight: 800; color: #ffffff; line-height: 1.3;">
        {subject}
    </h1>
    <div style="font-size: 15px; color: #ccc; line-height: 1.7;">
        {message.replace(chr(10), "<br>")}
    </div>
</td>
</tr>
<tr>
<td align="center" style="padding: 0 40px 30px;">
    <table cellpadding="0" cellspacing="0">
    <tr>
    <td style="background: linear-gradient(135deg, #00c853 0%, #00e676 100%); border-radius: 12px; padding: 16px 48px;">
        <a href="{domain_url}/login" style="color: #000; font-size: 17px; font-weight: 800; text-decoration: none;">
            Go to Dashboard &rarr;
        </a>
    </td>
    </tr>
    </table>
</td>
</tr>
'''
    html_body = _email_wrapper(inner, domain_url)
    text_body = f"{subject}\n\n{message}\n\nDashboard: {domain_url}/login"

    sent = send_email_via_api(to_email=to_email, subject=subject,
                              html_body=html_body, text_body=text_body)
    if sent:
        return safe_jsonify({"success": True, "message": f"Email sent to {to_email}"})
    else:
        return safe_jsonify({"error": f"Failed to send email to {to_email}"}), 500


@app.route("/api/admin/marketplace-installs", methods=["GET"])
def api_marketplace_installs():
    """Admin endpoint: view all marketplace installs and their OAuth status.
    Auth: login as admin OR ?key={CRON_SECRET}"""
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required. Use ?key=YOUR_CRON_SECRET"}), 403

    show = request.args.get("show", "all")  # "all", "incomplete", "complete"

    if show == "incomplete":
        installs = get_incomplete_installs()
    else:
        installs = get_all_marketplace_installs()
        if show == "complete":
            installs = [i for i in installs if i.get("oauth_completed")]

    # Serialize datetimes
    for inst in installs:
        for key in ["created_at", "oauth_completed_at", "setup_email_sent_at"]:
            if inst.get(key):
                inst[key] = inst[key].isoformat() + "Z"

    return safe_jsonify({
        "success": True,
        "count": len(installs),
        "filter": show,
        "installs": installs
    })


@app.route("/api/admin/marketplace-installs/<int:install_id>/send-setup-email", methods=["POST"])
def api_send_install_setup_email(install_id):
    """Admin action: manually send setup email to a marketplace installer.
    Auth: login as admin OR ?key={CRON_SECRET}"""
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required. Use ?key=YOUR_CRON_SECRET"}), 403

    # Get the specific install
    installs = get_all_marketplace_installs()
    target = next((i for i in installs if i["id"] == install_id), None)
    if not target:
        return safe_jsonify({"error": "Install not found"}), 404

    email = target.get("user_email")
    if not email:
        return safe_jsonify({"error": "No email address for this install"}), 400

    from send_email_api import send_email_via_api
    domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
    name = target.get("user_name") or "there"

    subject = "Complete Your InsuranceGrokBot Setup"
    html_body = _build_install_welcome_email(name, domain_url)
    text_body = (
        f"Hi {name}, complete your InsuranceGrokBot setup to start converting leads: "
        f"{domain_url}/oauth/initiate"
    )

    sent = send_email_via_api(to_email=email, subject=subject,
                              html_body=html_body, text_body=text_body)
    if sent:
        mark_setup_email_sent(install_id)
        log_webhook_event("marketplace", "admin_setup_email", "success",
                          f"Admin sent setup email to {email} for install #{install_id}")
        return safe_jsonify({"success": True, "message": f"Setup email sent to {email}"})
    else:
        return safe_jsonify({"error": f"Failed to send email to {email}"}), 500


@app.route("/api/admin/marketplace-installs/send-all-setup-emails", methods=["POST"])
def api_send_all_setup_emails():
    """Admin action: send setup emails to ALL incomplete installs that have an email.
    Auth: login as admin OR ?key={CRON_SECRET}"""
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required. Use ?key=YOUR_CRON_SECRET"}), 403

    from send_email_api import send_email_via_api
    domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")

    incomplete = get_incomplete_installs()
    sent_count = 0
    errors = []

    for inst in incomplete:
        email = inst.get("user_email")
        if not email:
            continue
        if inst.get("setup_email_sent"):
            continue  # Already sent

        name = inst.get("user_name") or "there"
        subject = "Complete Your InsuranceGrokBot Setup"
        html_body = _build_install_welcome_email(name, domain_url)
        text_body = (
            f"Hi {name}, complete your InsuranceGrokBot setup to start converting leads: "
            f"{domain_url}/oauth/initiate"
        )

        try:
            sent = send_email_via_api(to_email=email, subject=subject,
                                      html_body=html_body, text_body=text_body)
            if sent:
                mark_setup_email_sent(inst["id"])
                sent_count += 1
                logger.info(f"Setup email sent to {email} for install #{inst['id']}")
            else:
                errors.append(f"{email}: send failed")
        except Exception as e:
            errors.append(f"{email}: {str(e)}")

    return safe_jsonify({
        "success": True,
        "sent": sent_count,
        "skipped": len(incomplete) - sent_count - len(errors),
        "errors": errors
    })


@app.route("/api/admin/discover-installs", methods=["GET", "POST"])
def api_discover_installs():
    """
    Admin endpoint: Query GHL API to discover who installed the marketplace app.
    Uses the GHL OAuth installedLocations endpoint to find all locations
    that have the app installed, even if OAuth callback never completed.

    This is the only way to find the 'lost' installers since GHL's marketplace
    dashboard only shows totals, not individual location details.

    Auth: login as admin OR ?key={CRON_SECRET}
    """
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required"}), 403

    client_id = os.getenv("GHL_CLIENT_ID")
    client_secret = os.getenv("GHL_CLIENT_SECRET")

    if not client_id or not client_secret:
        return safe_jsonify({"error": "GHL_CLIENT_ID and GHL_CLIENT_SECRET must be set"}), 500

    # The public app ID from the marketplace
    # User can override via query param if they have multiple apps
    app_id = request.args.get("appId", client_id)

    results = {
        "app_id": app_id,
        "installed_locations": [],
        "errors": [],
        "cross_reference": []
    }

    # --- Method 1: Try GHL's installedLocations endpoint ---
    # This endpoint requires a Company-level access token or app-level token
    # First, try to get a fresh app token via client_credentials
    try:
        token_url = "https://services.leadconnectorhq.com/oauth/token"
        token_payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
        token_resp = requests.post(token_url, data=token_payload, timeout=15)
        if token_resp.ok:
            app_token = token_resp.json().get("access_token")
            if app_token:
                # Fetch installed locations
                install_url = "https://services.leadconnectorhq.com/oauth/installedLocations"
                params = {"appId": app_id, "limit": 100, "skip": 0}
                headers = {
                    "Authorization": f"Bearer {app_token}",
                    "Version": "2021-07-28",
                    "Accept": "application/json"
                }
                install_resp = requests.get(install_url, headers=headers, params=params, timeout=15)
                if install_resp.ok:
                    data = install_resp.json()
                    locations = data.get("locations", data.get("data", []))
                    results["installed_locations"] = locations
                    results["method"] = "installedLocations_api"
                    logger.info(f"Discovered {len(locations)} installed locations via GHL API")
                else:
                    results["errors"].append(
                        f"installedLocations API: {install_resp.status_code} — {install_resp.text[:300]}"
                    )

                # Also try the /oauth/installedLocations/search endpoint
                try:
                    search_url = "https://services.leadconnectorhq.com/oauth/installedLocations"
                    all_locations = []
                    skip = 0
                    while True:
                        params = {"appId": app_id, "limit": 100, "skip": skip}
                        page_resp = requests.get(search_url, headers=headers, params=params, timeout=15)
                        if page_resp.ok:
                            page_data = page_resp.json()
                            page_locations = page_data.get("locations", page_data.get("data", []))
                            if not page_locations:
                                break
                            all_locations.extend(page_locations)
                            skip += len(page_locations)
                            if len(page_locations) < 100:
                                break
                        else:
                            break
                    if all_locations:
                        results["installed_locations"] = all_locations
                except Exception:
                    pass
            else:
                results["errors"].append("client_credentials token had no access_token")
        else:
            results["errors"].append(
                f"client_credentials grant: {token_resp.status_code} — {token_resp.text[:300]}"
            )
    except Exception as e:
        results["errors"].append(f"API discovery error: {str(e)}")

    # --- Method 2: Cross-reference with our database ---
    # Show which locations in our DB have the app vs. which are missing
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            # Get all known subscribers
            cur.execute("""
                SELECT location_id, email, full_name, access_token IS NOT NULL as has_token,
                       calendar_id IS NOT NULL as has_calendar,
                       stripe_customer_id IS NOT NULL as has_stripe,
                       created_at, install_completed_at
                FROM subscribers
                WHERE location_id NOT LIKE 'temp_%%'
                ORDER BY created_at DESC
            """)
            db_users = [dict(r) for r in cur.fetchall()]
            for u in db_users:
                for key in ["created_at", "install_completed_at"]:
                    if u.get(key):
                        u[key] = u[key].isoformat() + "Z"
            results["db_subscribers"] = db_users

            # Also get agency billing entries
            cur.execute("""
                SELECT agency_email, company_id, access_token IS NOT NULL as has_token,
                       created_at
                FROM agency_billing
                ORDER BY created_at DESC
            """)
            db_agencies = [dict(r) for r in cur.fetchall()]
            for a in db_agencies:
                if a.get("created_at"):
                    a["created_at"] = a["created_at"].isoformat() + "Z"
            results["db_agencies"] = db_agencies

            # Get marketplace install records
            cur.execute("""
                SELECT * FROM marketplace_installs ORDER BY created_at DESC
            """)
            mkt_installs = [dict(r) for r in cur.fetchall()]
            for m in mkt_installs:
                for key in ["created_at", "oauth_completed_at", "setup_email_sent_at"]:
                    if m.get(key):
                        m[key] = m[key].isoformat() + "Z"
            results["marketplace_installs"] = mkt_installs

            cur.close()
            return_db_connection(conn)

            # Cross-reference: which GHL-installed locations are NOT in our DB?
            db_location_ids = {u["location_id"] for u in db_users if u.get("location_id")}
            for loc in results.get("installed_locations", []):
                loc_id = loc.get("locationId") or loc.get("location_id") or loc.get("_id")
                in_db = loc_id in db_location_ids if loc_id else False
                results["cross_reference"].append({
                    "location_id": loc_id,
                    "name": loc.get("name") or loc.get("locationName", "Unknown"),
                    "email": loc.get("email", ""),
                    "company_id": loc.get("companyId", ""),
                    "in_our_database": in_db,
                    "status": "connected" if in_db else "LOST — needs OAuth"
                })
    except Exception as e:
        results["errors"].append(f"DB cross-reference error: {str(e)}")

    # Log the discovery for audit trail
    log_webhook_event("admin", "discover_installs", "info",
                      f"Admin discovered {len(results.get('installed_locations', []))} installs, "
                      f"{len(results.get('cross_reference', []))} cross-referenced",
                      details={"errors": results["errors"]})

    return safe_jsonify(results)


def _build_setup_checklist_html(missing: list, domain_url: str, user_type: str) -> str:
    """Build a visual setup checklist showing what's done and what's remaining."""
    dashboard = f"{domain_url}/agency-dashboard" if user_type == "agency_owner" else f"{domain_url}/dashboard"
    steps = [
        ("account", "Create Account", dashboard),
        ("crm_connection", "Connect Your CRM", f"{domain_url}/oauth/initiate"),
        ("location_id", "Link Location", dashboard),
        ("calendar", "Set Up Calendar", dashboard),
        ("subscription", "Activate Subscription", dashboard),
    ]
    rows = ""
    for key, label, link in steps:
        is_missing = key in missing
        if key == "account":
            is_missing = False  # they have an account if they're getting this email
        if is_missing:
            rows += f'''
            <tr>
                <td style="padding: 12px 16px; border-bottom: 1px solid #f0f0f0; width: 40px; vertical-align: middle;">
                    <div style="width: 28px; height: 28px; border-radius: 50%; border: 2px solid #ff6b35; display: flex; align-items: center; justify-content: center;">
                        <span style="color: #ff6b35; font-size: 16px; font-weight: bold; line-height: 28px;">&bull;</span>
                    </div>
                </td>
                <td style="padding: 12px 0; border-bottom: 1px solid #f0f0f0; vertical-align: middle;">
                    <a href="{link}" style="color: #ff6b35; font-weight: 600; text-decoration: none; font-size: 15px;">{label}</a>
                    <span style="background: #fff3e0; color: #e65100; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 4px; margin-left: 8px;">NEEDED</span>
                </td>
            </tr>'''
        else:
            rows += f'''
            <tr>
                <td style="padding: 12px 16px; border-bottom: 1px solid #f0f0f0; width: 40px; vertical-align: middle;">
                    <div style="width: 28px; height: 28px; border-radius: 50%; background: #00c853; display: flex; align-items: center; justify-content: center;">
                        <span style="color: white; font-size: 14px; font-weight: bold; line-height: 28px;">&#10003;</span>
                    </div>
                </td>
                <td style="padding: 12px 0; border-bottom: 1px solid #f0f0f0; vertical-align: middle;">
                    <span style="color: #888; font-size: 15px; text-decoration: line-through;">{label}</span>
                    <span style="color: #00c853; font-size: 11px; font-weight: 700; margin-left: 8px;">DONE</span>
                </td>
            </tr>'''
    return f'<table cellpadding="0" cellspacing="0" style="width: 100%;">{rows}</table>'


def _email_wrapper(inner_html: str, domain_url: str) -> str:
    """Wrap email content in a premium dark-themed email shell."""
    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; background-color: #0a0a0a; font-family: 'Segoe UI', Arial, sans-serif;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background-color: #0a0a0a;">
<tr><td align="center" style="padding: 40px 20px;">

<!-- Main Card -->
<table cellpadding="0" cellspacing="0" width="600" style="max-width: 600px; width: 100%; background: linear-gradient(145deg, #141428 0%, #0d0d1a 100%); border-radius: 20px; border: 1px solid rgba(255,255,255,0.06); box-shadow: 0 20px 60px rgba(0,0,0,0.5);">

<!-- Header Bar -->
<tr>
<td style="padding: 0;">
    <div style="height: 4px; background: linear-gradient(90deg, #00c853, #00e676, #69f0ae, #00c853); border-radius: 20px 20px 0 0;"></div>
</td>
</tr>

<!-- Logo -->
<tr>
<td align="center" style="padding: 35px 40px 20px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: rgba(0,200,83,0.1); border: 1px solid rgba(0,200,83,0.2); border-radius: 14px; padding: 12px 24px;">
            <span style="font-size: 22px; font-weight: 800; color: #ffffff; letter-spacing: -0.5px;">Insurance<span style="color: #00c853;">Grok</span>Bot</span>
        </td>
    </tr></table>
</td>
</tr>

<!-- Content -->
{inner_html}

<!-- Footer -->
<tr>
<td style="padding: 30px 40px 35px; border-top: 1px solid rgba(255,255,255,0.05);">
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td align="center">
            <p style="margin: 0 0 12px; font-size: 13px; color: #555;">
                <a href="{domain_url}/support" style="color: #00c853; text-decoration: none;">Support</a>
                &nbsp;&nbsp;|&nbsp;&nbsp;
                <a href="{domain_url}/dashboard" style="color: #00c853; text-decoration: none;">Dashboard</a>
                &nbsp;&nbsp;|&nbsp;&nbsp;
                <a href="{domain_url}/terms" style="color: #00c853; text-decoration: none;">Terms</a>
            </p>
            <p style="margin: 0; font-size: 12px; color: #444;">
                InsuranceGrokBot &mdash; AI-Powered Insurance Sales Assistant
            </p>
        </td>
    </tr>
    </table>
</td>
</tr>

</table>
<!-- End Main Card -->

</td></tr></table>
</body>
</html>'''


def _build_reminder_24h_email(name: str, domain_url: str, user_type: str, missing: list = None) -> str:
    """Build the 24-hour reminder — premium marketing email with setup checklist."""
    missing = missing or []
    dashboard = f"{domain_url}/agency-dashboard" if user_type == "agency_owner" else f"{domain_url}/dashboard"
    checklist = _build_setup_checklist_html(missing, domain_url, user_type)

    # Dynamic hero message based on what's missing
    if "crm_connection" in missing:
        hero_subtitle = "Connect your CRM to unleash your AI assistant"
        action_text = "Connect My CRM"
        action_url = f"{domain_url}/oauth/initiate"
    elif "subscription" in missing:
        hero_subtitle = "Subscribe to activate your AI sales machine"
        action_text = "Activate My Bot"
        action_url = dashboard
    else:
        hero_subtitle = "Complete your setup to start closing leads"
        action_text = "Finish Setup"
        action_url = dashboard

    inner = f'''
<tr>
<td align="center" style="padding: 0 40px 10px;">
    <!-- Hero Icon -->
    <div style="width: 80px; height: 80px; border-radius: 50%; background: linear-gradient(135deg, rgba(0,200,83,0.15) 0%, rgba(0,200,83,0.05) 100%); border: 2px solid rgba(0,200,83,0.25); margin: 0 auto 20px; line-height: 80px; text-align: center;">
        <span style="font-size: 36px;">&#9889;</span>
    </div>
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">Your Bot is Almost Live</h1>
    <p style="margin: 0; font-size: 16px; color: #aaa; line-height: 1.5;">{hero_subtitle}</p>
</td>
</tr>

<!-- Personal greeting -->
<tr>
<td style="padding: 25px 40px 15px;">
    <p style="margin: 0; font-size: 16px; color: #ddd; line-height: 1.7;">
        Hi {name},
    </p>
    <p style="margin: 12px 0 0; font-size: 15px; color: #bbb; line-height: 1.7;">
        You signed up for InsuranceGrokBot 24 hours ago. You're almost there — your AI-powered sales assistant is configured and ready to start responding to leads, qualifying prospects, and booking appointments on your calendar.
    </p>
</td>
</tr>

<!-- Setup Progress -->
<tr>
<td style="padding: 10px 40px 25px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px; margin: 10px 0;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 1.5px;">Setup Progress</p>
        {checklist}
    </div>
</td>
</tr>

<!-- Stats Row -->
<tr>
<td style="padding: 0 40px 25px;">
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td width="33%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-radius: 12px 0 0 12px; border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853; line-height: 1;">5s</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px;">Response Time</div>
        </td>
        <td width="34%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-left: 1px solid rgba(0,200,83,0.1); border-right: 1px solid rgba(0,200,83,0.1); border-top: 1px solid rgba(0,200,83,0.1); border-bottom: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853; line-height: 1;">24/7</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px;">Availability</div>
        </td>
        <td width="33%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-radius: 0 12px 12px 0; border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853; line-height: 1;">Auto</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px;">Booking</div>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- CTA Button -->
<tr>
<td align="center" style="padding: 5px 40px 30px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: linear-gradient(135deg, #00c853 0%, #00e676 100%); border-radius: 14px; box-shadow: 0 4px 20px rgba(0,200,83,0.3);">
            <a href="{action_url}" style="display: inline-block; padding: 18px 48px; color: #000000; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: -0.3px;">
                {action_text} &rarr;
            </a>
        </td>
    </tr></table>
    <p style="margin: 14px 0 0; font-size: 13px; color: #666;">Takes less than 2 minutes to complete</p>
</td>
</tr>

<!-- Testimonial -->
<tr>
<td style="padding: 0 40px 25px;">
    <div style="background: rgba(255,255,255,0.02); border-left: 3px solid #00c853; padding: 16px 20px; border-radius: 0 12px 12px 0;">
        <p style="margin: 0 0 8px; font-size: 14px; color: #ccc; font-style: italic; line-height: 1.6;">
            "I had 3 appointments booked by the end of my first week without lifting a finger. GrokBot qualifies leads better than most humans."
        </p>
        <p style="margin: 0; font-size: 12px; color: #00c853; font-weight: 600;">
            &mdash; Independent Agent, Texas
        </p>
    </div>
</td>
</tr>

<!-- Support nudge -->
<tr>
<td style="padding: 0 40px 10px;">
    <p style="margin: 0; font-size: 14px; color: #888; line-height: 1.6; text-align: center;">
        Need help setting up? <a href="{domain_url}/support" style="color: #00c853; text-decoration: none; font-weight: 600;">Visit our support page</a> or just reply to this email.
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)


def _build_reminder_72h_email(name: str, domain_url: str, user_type: str, missing: list = None) -> str:
    """Build the 72-hour reminder — urgency-driven premium marketing email."""
    missing = missing or []
    dashboard = f"{domain_url}/agency-dashboard" if user_type == "agency_owner" else f"{domain_url}/dashboard"
    checklist = _build_setup_checklist_html(missing, domain_url, user_type)

    if "crm_connection" in missing:
        action_text = "Connect CRM & Go Live"
        action_url = f"{domain_url}/oauth/initiate"
    elif "subscription" in missing:
        action_text = "Subscribe & Activate Now"
        action_url = dashboard
    else:
        action_text = "Complete Setup Now"
        action_url = dashboard

    inner = f'''
<tr>
<td align="center" style="padding: 0 40px 10px;">
    <!-- Urgency Icon -->
    <div style="width: 80px; height: 80px; border-radius: 50%; background: linear-gradient(135deg, rgba(255,107,53,0.15) 0%, rgba(255,152,0,0.05) 100%); border: 2px solid rgba(255,107,53,0.3); margin: 0 auto 20px; line-height: 80px; text-align: center;">
        <span style="font-size: 36px;">&#9203;</span>
    </div>
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">Leads Are Slipping Away</h1>
    <p style="margin: 0; font-size: 16px; color: #ff9800; line-height: 1.5; font-weight: 600;">3 days without your bot = missed revenue</p>
</td>
</tr>

<tr>
<td style="padding: 25px 40px 15px;">
    <p style="margin: 0; font-size: 16px; color: #ddd; line-height: 1.7;">
        Hi {name},
    </p>
    <p style="margin: 12px 0 0; font-size: 15px; color: #bbb; line-height: 1.7;">
        It's been 3 days since you created your InsuranceGrokBot account. Every hour your bot isn't active, new leads are going unworked, follow-ups are being missed, and potential clients are moving on to the next agent who responds first.
    </p>
</td>
</tr>

<!-- The Cost of Waiting -->
<tr>
<td style="padding: 10px 40px 20px;">
    <div style="background: linear-gradient(135deg, rgba(255,107,53,0.08) 0%, rgba(255,152,0,0.04) 100%); border: 1px solid rgba(255,152,0,0.2); border-radius: 16px; padding: 24px;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #ff9800; text-transform: uppercase; letter-spacing: 1.5px;">The Cost of Waiting</p>
        <table cellpadding="0" cellspacing="0" width="100%">
            <tr>
                <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="40" style="vertical-align: top;"><span style="font-size: 20px;">&#128168;</span></td>
                        <td style="vertical-align: top;">
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">Leads go cold in 5 minutes</span><br>
                            <span style="color: #999; font-size: 13px;">78% of buyers choose the agent who responds first. Your bot responds in seconds.</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="40" style="vertical-align: top;"><span style="font-size: 20px;">&#128197;</span></td>
                        <td style="vertical-align: top;">
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">Missed appointments = missed commission</span><br>
                            <span style="color: #999; font-size: 13px;">GrokBot qualifies and books automatically — no back-and-forth texting.</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 10px 0;">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="40" style="vertical-align: top;"><span style="font-size: 20px;">&#127769;</span></td>
                        <td style="vertical-align: top;">
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">Nights and weekends covered</span><br>
                            <span style="color: #999; font-size: 13px;">Leads come in at 11 PM. Your bot is there. Without it, they text your competitor.</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
        </table>
    </div>
</td>
</tr>

<!-- Setup Progress -->
<tr>
<td style="padding: 5px 40px 20px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #ff9800; text-transform: uppercase; letter-spacing: 1.5px;">Your Setup Status</p>
        {checklist}
    </div>
</td>
</tr>

<!-- CTA Button -->
<tr>
<td align="center" style="padding: 10px 40px 25px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: linear-gradient(135deg, #ff6b35 0%, #ff9800 100%); border-radius: 14px; box-shadow: 0 4px 20px rgba(255,107,53,0.35);">
            <a href="{action_url}" style="display: inline-block; padding: 18px 48px; color: #ffffff; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: -0.3px;">
                {action_text} &rarr;
            </a>
        </td>
    </tr></table>
    <p style="margin: 14px 0 0; font-size: 13px; color: #666;">Your competitors are already using AI. Don't fall behind.</p>
</td>
</tr>

<!-- Before/After -->
<tr>
<td style="padding: 0 40px 25px;">
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td width="48%" style="background: rgba(239,68,68,0.06); border: 1px solid rgba(239,68,68,0.15); border-radius: 12px; padding: 20px; vertical-align: top;">
            <p style="margin: 0 0 10px; font-size: 12px; font-weight: 700; color: #ef4444; text-transform: uppercase; letter-spacing: 1px;">Without GrokBot</p>
            <p style="margin: 0; font-size: 13px; color: #999; line-height: 1.7;">
                &#10060; Leads wait hours for a reply<br>
                &#10060; Manual follow-up texting<br>
                &#10060; Missed after-hours leads<br>
                &#10060; No qualifying before calls
            </p>
        </td>
        <td width="4%">&nbsp;</td>
        <td width="48%" style="background: rgba(0,200,83,0.06); border: 1px solid rgba(0,200,83,0.15); border-radius: 12px; padding: 20px; vertical-align: top;">
            <p style="margin: 0 0 10px; font-size: 12px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 1px;">With GrokBot</p>
            <p style="margin: 0; font-size: 13px; color: #999; line-height: 1.7;">
                &#10004; 5-second response time<br>
                &#10004; Automated smart follow-ups<br>
                &#10004; 24/7 lead coverage<br>
                &#10004; Pre-qualified appointments
            </p>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- Support -->
<tr>
<td style="padding: 0 40px 10px;">
    <p style="margin: 0; font-size: 14px; color: #888; line-height: 1.6; text-align: center;">
        Stuck on something? <a href="{domain_url}/support" style="color: #ff9800; text-decoration: none; font-weight: 600;">Get help here</a> or reply to this email and we'll walk you through it.
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)


@app.route("/disclaimers")
def disclaimers():
    return render_template('disclaimers.html')

@app.route("/terms")
def terms():
    return render_template('terms.html')

@app.route("/contact")
def contact():
    return render_template('contact.html')

@app.route("/privacy")
def privacy():
    return render_template('privacy.html')
# =====================================================
# DEMO CHAT - SIMPLE REQUEST/RESPONSE (No logs, no polling)
# =====================================================

@app.route("/demo/chat", methods=["POST"])
def demo_chat_api():
    """
    User sends message → Gets response back directly.
    No polling. No log reading. Just like a normal chat API.
    """
    data = request.get_json()
    contact_id = data.get("contact_id")
    message = data.get("message", "").strip()

    if not contact_id or not contact_id.startswith("demo_"):
        return flask_jsonify({"error": "Invalid session"}), 400

    if not message:
        return flask_jsonify({"error": "Empty message"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 503

    try:
        cur = conn.cursor()

        # 1. CRASH FIX: Save user message with "ON CONFLICT DO NOTHING"
        # This prevents the app from crashing if the lead repeats themselves (e.g. "No", "No")
        cur.execute("""
            INSERT INTO contact_messages (contact_id, message_type, message_text)
            VALUES (%s, 'lead', %s)
            ON CONFLICT DO NOTHING
        """, (contact_id, message))
        conn.commit()

        # 2. Get conversation history
        cur.execute("""
            SELECT message_type, message_text
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at DESC
            LIMIT 16
        """, (contact_id,))

        rows = cur.fetchall()
        recent_exchanges = []
        for row in reversed(rows):
            role = "lead" if row['message_type'] == 'lead' else "assistant"
            recent_exchanges.append({"role": role, "text": row['message_text']})

        cur.close()
        return_db_connection(conn)

        # 3. Use your full brain
        from sales_director import generate_strategic_directive
        from prompt import build_system_prompt

        director_output = generate_strategic_directive(
            contact_id=contact_id,
            message=message,
            first_name="Demo User",
            age=None,
            address=None
        )

        if "Silence required" in director_output["tactical_narrative"]:
            return flask_jsonify({"reply": "", "stage": "closed"})

        calendar_slots = ""
        if director_output["stage"] == "booking":
            calendar_slots = "Tomorrow at 2:00 PM, Tomorrow at 4:30 PM, Friday at 10:00 AM"

        system_prompt = build_system_prompt(
            bot_first_name="Grok",
            timezone="America/Chicago",
            profile_str=director_output["profile_str"],
            tactical_narrative=director_output["tactical_narrative"],
            known_facts=director_output["known_facts"],
            story_narrative=director_output["story_narrative"],
            stage=director_output["stage"],
            recent_exchanges=recent_exchanges[-8:],
            message=message,
            calendar_slots=calendar_slots,
            context_nudge="",
            lead_vendor=""
        )

        # Demo: replace any unsubstituted {bot_first_name} literals and set identity
        system_prompt = system_prompt.replace("{bot_first_name}", "GrokBot")
        system_prompt += (
            "\n\nDEMO IDENTITY RULE: If someone asks who you are, who they are talking to, "
            "or what this is, you MUST respond with something like: "
            "'This is GrokBot. I'm an independent life insurance agent. I'm currently in "
            "demo mode, in production I'll identify as whatever name you assign me in your "
            "dashboard.' Then follow up with a question to keep the conversation going. "
            "Do not dodge identity questions. Do not deflect. Answer directly then ask a question."
        )

        grok_messages = [{"role": "system", "content": system_prompt}]
        for msg in recent_exchanges[-8:]:
            role = "user" if msg["role"] == "lead" else "assistant"
            grok_messages.append({"role": role, "content": msg["text"]})
        grok_messages.append({"role": "user", "content": message})

        # 4. Call Grok with structural reasoning separation
        reply = generate_clean_reply(
            client=client,
            full_messages=grok_messages,
            bot_name="GrokBot",
        )

        if not reply:
            logger.error(f"DEMO: LLM could not produce clean reply. Using fallback.")
            reply = "What's your main concern about coverage right now?"

        reply = reply.replace("—", ",").replace("–", ",").strip()

        # Ensure minimum quality
        if len(reply) < 5 or not any(c.isalpha() for c in reply):
            logger.error(f"🚨 DEMO BLOCKED LOW-QUALITY MESSAGE: '{reply}' - Using fallback")
            reply = "What's your main concern about coverage right now?"

        # 5. Save bot response - ALSO CRASH PROOF
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO contact_messages (contact_id, message_type, message_text)
                VALUES (%s, 'assistant', %s)
                ON CONFLICT DO NOTHING
            """, (contact_id, reply))
            conn.commit()
            cur.close()
            return_db_connection(conn)

        # 6. Return response directly to frontend
        return flask_jsonify({
            "reply": reply,
            "stage": director_output["stage"]
        })

    except Exception as e:
        logger.error(f"Demo chat error: {e}", exc_info=True)
        # Even if DB fails completely, try to reply so the UI doesn't hang
        return flask_jsonify({
            "reply": "I hear you. Could you clarify that last part?",
            "error": str(e)
        }), 200

@app.route("/demo/init", methods=["POST"])
def demo_init_api():
    """Initialize or resume a demo session."""
    data = request.get_json() or {}
    session_id = data.get("session_id") or str(uuid.uuid4())
    contact_id = f"demo_{session_id}"

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 503

    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM contact_messages WHERE contact_id = %s", (contact_id,))
        count = cur.fetchone()['cnt']

        if count == 0:
            opener = generate_demo_opener()
            cur.execute("""
                INSERT INTO contact_messages (contact_id, message_type, message_text)
                VALUES (%s, 'assistant', %s)
            """, (contact_id, opener))
            conn.commit()
            cur.close()
            return_db_connection(conn)
            return flask_jsonify({"contact_id": contact_id, "opener": opener, "status": "new"})

        cur.execute("""
            SELECT message_type, message_text 
            FROM contact_messages 
            WHERE contact_id = %s 
            ORDER BY created_at ASC
        """, (contact_id,))

        history = [{"role": "bot" if r['message_type'] == 'assistant' else "user", "content": r['message_text']} for r in cur.fetchall()]
        cur.close()
        return_db_connection(conn)

        return flask_jsonify({"contact_id": contact_id, "history": history, "status": "existing"})

    except Exception as e:
        logger.error(f"Demo init error: {e}")
        return flask_jsonify({"error": str(e)}), 500

@app.route("/demo/reset", methods=["POST"])
def demo_reset_api():
    """Clear session and start fresh."""
    data = request.get_json() or {}
    old_id = data.get("contact_id")

    conn = get_db_connection()
    if conn and old_id and old_id.startswith("demo_"):
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM contact_messages WHERE contact_id = %s", (old_id,))
            cur.execute("DELETE FROM contact_facts WHERE contact_id = %s", (old_id,))
            cur.execute("DELETE FROM contact_narratives WHERE contact_id = %s", (old_id,))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"❌ Demo reset DELETE failed for {old_id}: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                return_db_connection(conn)

    new_id = f"demo_{uuid.uuid4()}"
    opener = generate_demo_opener()

    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contact_messages (contact_id, message_type, message_text)
            VALUES (%s, 'assistant', %s)
        """, (new_id, opener))
        conn.commit()
        cur.close()
        return_db_connection(conn)

    return flask_jsonify({"contact_id": new_id, "opener": opener})

@app.route("/demo-chat")
def demo_chat():
    try:
        run_demo_janitor()
    except Exception as e:
        logger.error(f"❌ Demo janitor failed: {e}")
    return render_template('demo.html')
# =====================================================
# HYBRID GET LOGS (REDIS + SQL FALLBACK)
# =====================================================
@app.route("/get-logs", methods=["GET"])
def get_logs():
    contact_id = request.args.get("contact_id")

    if not contact_id:
        return flask_jsonify({"logs": []})

    # Only allow demo/test contacts
    if not contact_id.startswith(('demo_', 'test_')):
        return flask_jsonify({"logs": []})

    db_conn = get_db_connection()
    if not db_conn:
        return flask_jsonify({"logs": []})

    logs = []
    try:
        cur = db_conn.cursor(cursor_factory=RealDictCursor)

        # 1. Fetch Messages
        cur.execute("""
            SELECT message_type, message_text, created_at 
            FROM contact_messages 
            WHERE contact_id = %s 
            ORDER BY created_at ASC
        """, (contact_id,))

        for r in cur.fetchall():
            ts = r['created_at'].isoformat() if hasattr(r['created_at'], 'isoformat') else str(r['created_at'])
            role = "bot" if r['message_type'] in ['assistant', 'bot'] else "lead"
            logs.append({
                "role": role,
                "type": f"{'Bot' if role == 'bot' else 'Lead'} Message",
                "content": r['message_text'],
                "timestamp": ts
            })

        # 3. Fetch/Build Narrative
        facts = get_known_facts(contact_id)
        narrative = get_narrative(contact_id)

        if not narrative and facts:
            try:
                facts_text = " ".join(facts).lower()
                first_name = None
                age = None

                name_match = re.search(r"first name: (\w+)", facts_text, re.IGNORECASE)
                if name_match:
                    first_name = name_match.group(1).capitalize()

                age_match = re.search(r"age: (\d+)", facts_text)
                if age_match:
                    age = age_match.group(1)

                rebuilt = build_comprehensive_profile(
                    story_narrative="",
                    known_facts=facts,
                    first_name=first_name,
                    age=age
                )
                narrative = str(rebuilt[0]) if isinstance(rebuilt, tuple) else str(rebuilt)
            except Exception as e:
                logger.warning(f"Profile rebuild failed: {e}")

        if narrative:
            logs.append({
                "timestamp": datetime.now().isoformat(),
                "type": "Full Human Identity Narrative",
                "content": narrative
            })

        return safe_jsonify({"logs": logs})

    except Exception as e:
        logger.error(f"get_logs error: {e}")
        return flask_jsonify({"logs": []})

    finally:
        cur.close()
        return_db_connection(db_conn)


@app.route("/download-transcript", methods=["GET"])
def download_transcript():
    contact_id = request.args.get("contact_id")
    if not contact_id:
        return flask_jsonify({"error": "Missing contact_id parameter"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ────────────────────────────────────────────────
        # Security / Ownership Check
        # ────────────────────────────────────────────────
        allowed = False
        location_id = None
        is_demo = contact_id.startswith(('demo_', 'test_'))

        if is_demo:
            # Demo/test contacts: allow anonymous access (no login required)
            allowed = True
            location_id = contact_id  # for transcript header
        else:
            # Real contacts: require login + ownership
            if not current_user.is_authenticated:
                return flask_jsonify({"error": "Please log in to download real transcripts"}), 401

            if current_user.role == 'agency_owner':
                cur.execute("""
                    SELECT location_id 
                    FROM subscribers 
                    WHERE location_id = %s 
                      AND parent_agency_email = %s
                    LIMIT 1
                """, (contact_id, current_user.email))
                row = cur.fetchone()
                if row:
                    allowed = True
                    location_id = row['location_id']
            else:
                if contact_id == current_user.location_id:
                    allowed = True
                    location_id = current_user.location_id

        if not allowed:
            return flask_jsonify({"error": "You do not have permission to download this transcript"}), 403

        # ────────────────────────────────────────────────
        # Fetch real data (same as before)
        # ────────────────────────────────────────────────
        cur.execute("""
            SELECT message_type, message_text, created_at
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at ASC
        """, (contact_id,))
        messages = cur.fetchall()

        facts = get_known_facts(contact_id)
        narrative = get_narrative(contact_id)

        # ... rest of your profile rebuild logic unchanged ...

        # Build transcript (unchanged)
        lines = []
        lines.append("INSURANCEGROKBOT CONVERSATION TRANSCRIPT")
        lines.append("=" * 60)
        lines.append(f"Contact ID:       {contact_id}")
        lines.append(f"Downloaded by:    {'Anonymous (Demo)' if is_demo else current_user.email}")
        lines.append(f"Date:             {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
        lines.append(f"Location ID:      {location_id or '—'}")
        # ... rest of transcript building unchanged ...

        transcript = "\n".join(lines)

        filename = f"InsuranceGrokBot_transcript_{contact_id}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        response = make_response(transcript)
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        response.headers["Content-Type"] = "text/plain; charset=utf-8"
        return response

    except Exception as e:
        logger.error(f"Transcript download error for {contact_id}: {e}", exc_info=True)
        return flask_jsonify({"error": "Failed to generate transcript"}), 500
    finally:
        if 'cur' in locals():
            cur.close()
        if conn:
            return_db_connection(conn)

@app.route("/checkout")
def checkout():
    try:
        # 1. Check if price ID is configured
        price_id = os.getenv("STRIPE_PRICE_ID")
        if not price_id:
            logger.error("STRIPE_PRICE_ID environment variable is not set!")
            return render_template_string("""
                <div style="background:#050505; color:#fff; height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                    <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; max-width:500px;">
                        <h2 style="color:#ff4444;">Configuration Error</h2>
                        <p style="color:#aaa;">The Individual plan price ID is not configured. Please contact support.</p>
                        <p style="color:#666; font-size:0.85rem; margin-top:20px;">Error Code: MISSING_PRICE_ID</p>
                        <a href="/" style="color:#00ff88; text-decoration:none; margin-top:20px; display:inline-block;">← Back to Home</a>
                    </div>
                </div>
            """), 500

        # 2. Pre-fill email if user is logged in
        customer_email = current_user.email if current_user.is_authenticated else None

        # 3. Create Stripe checkout session
        logger.info(f"Creating Individual checkout with price_id: {price_id}")
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{
                "price": price_id,
                "quantity": 1,
            }],
            allow_promotion_codes=True,
            customer_email=customer_email,

            metadata={
                "user_email": customer_email,
                "target_role": "individual",
                "target_tier": "individual",
                "source": "website"
            },
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                "user_email": customer_email,
                "target_role": "individual",
                "target_tier": "individual"
                },

            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )
        return redirect(session.url, code=303)
    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe Invalid Request Error (Individual): {e}")
        return render_template_string("""
            <div style="background:#050505; color:#fff; height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; max-width:500px;">
                    <h2 style="color:#ff4444;">Stripe Configuration Error</h2>
                    <p style="color:#aaa;">There's an issue with the payment configuration. Please contact support.</p>
                    <p style="color:#666; font-size:0.85rem; margin-top:20px;">Error: {{ error }}</p>
                    <a href="/" style="color:#00ff88; text-decoration:none; margin-top:20px; display:inline-block;">← Back to Home</a>
                </div>
            </div>
        """, error=str(e)), 500
    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        return render_template_string("""
            <div style="background:#050505; color:#fff; height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; max-width:500px;">
                    <h2 style="color:#ff4444;">Checkout Error</h2>
                    <p style="color:#aaa;">Unable to create checkout session. Please contact support.</p>
                    <p style="color:#666; font-size:0.85rem; margin-top:20px;">Error Code: {{ error }}</p>
                    <a href="/" style="color:#00ff88; text-decoration:none; margin-top:20px; display:inline-block;">← Back to Home</a>
                </div>
            </div>
        """, error=str(e)), 500
    
@app.route("/checkout/agency-starter")
def checkout_agency_starter():
    """
    AGENCY STARTER GUEST CHECKOUT:
    - No login required (Webhook provisions account after payment).
    - Seat count verification happens AFTER OAuth connection, not before payment.
    - Works with Private App OAuth flow (white-label compatible).
    """
    try:
        # 1. Check if price ID is configured
        price_id = os.getenv("STRIPE_AGENCY_STARTER_PRICE_ID")
        if not price_id:
            logger.error("STRIPE_AGENCY_STARTER_PRICE_ID environment variable is not set!")
            return render_template_string("""
                <div style="background:#050505; color:#fff; height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                    <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; max-width:500px;">
                        <h2 style="color:#ff4444;">Configuration Error</h2>
                        <p style="color:#aaa;">The Agency Starter price ID is not configured. Please contact support.</p>
                        <p style="color:#666; font-size:0.85rem; margin-top:20px;">Error Code: MISSING_PRICE_ID</p>
                        <a href="/" style="color:#00ff88; text-decoration:none; margin-top:20px; display:inline-block;">← Back to Home</a>
                    </div>
                </div>
            """), 500

        # 2. Optional email grab for existing users
        customer_email = current_user.email if current_user.is_authenticated else None

        # 3. Create Stripe checkout session
        logger.info(f"Creating Agency Starter checkout with price_id: {price_id}")
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=customer_email,
            line_items=[{
                "price": price_id,
                "quantity": 1,
            }],
            allow_promotion_codes=True,

            # Metadata for webhook provisioning
            metadata={
                "target_role": "agency_owner",
                "target_tier": "agency_starter",
                "source": "website_checkout"
            },
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                    "target_role": "agency_owner",
                    "target_tier": "agency_starter"
                }
            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )
        return redirect(session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe Invalid Request Error (Agency Starter): {e}")
        return render_template_string("""
            <div style="background:#050505; color:#fff; height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; max-width:500px;">
                    <h2 style="color:#ff4444;">Stripe Configuration Error</h2>
                    <p style="color:#aaa;">There's an issue with the payment configuration. Please contact support.</p>
                    <p style="color:#666; font-size:0.85rem; margin-top:20px;">Error: {{ error }}</p>
                    <a href="/" style="color:#00ff88; text-decoration:none; margin-top:20px; display:inline-block;">← Back to Home</a>
                </div>
            </div>
        """, error=str(e)), 500
    except Exception as e:
        logger.error(f"Agency Starter checkout error: {e}")
        return render_template_string("""
            <div style="background:#050505; color:#fff; height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; max-width:500px;">
                    <h2 style="color:#ff4444;">Checkout Error</h2>
                    <p style="color:#aaa;">Unable to create checkout session. Please contact support.</p>
                    <p style="color:#666; font-size:0.85rem; margin-top:20px;">Error Code: {{ error }}</p>
                    <a href="/" style="color:#00ff88; text-decoration:none; margin-top:20px; display:inline-block;">← Back to Home</a>
                </div>
            </div>
        """, error=str(e)), 500

@app.route("/cancel")
def cancel():
    return render_template('cancel.html')


# ── AI Minutes Marketplace ─────────────────────────────────────────────

AI_MINUTE_PACKAGES = [
    {"minutes": 500,   "label": "Starter",      "env_key": "500AI_PRICE_ID"},
    {"minutes": 2000,  "label": "Growth",        "env_key": "2000AI_PRICE_ID"},
    {"minutes": 5000,  "label": "Professional",  "env_key": "5000AI_PRICE_ID"},
    {"minutes": 10000, "label": "Enterprise",    "env_key": "10000AI_PRICE_ID"},
]

@app.route("/ai-minutes/balance")
@login_required
def ai_minutes_balance():
    """Get current AI minute balance for the logged-in user."""
    bal = get_ai_minute_balance(current_user.email)
    return flask_jsonify(bal)


@app.route("/ai-minutes/packages")
@login_required
def ai_minutes_packages():
    """List available AI minute packages with pricing."""
    packages = []
    for pkg in AI_MINUTE_PACKAGES:
        price_id = os.getenv(pkg["env_key"], "")
        if not price_id:
            continue
        # Try to fetch price from Stripe for display
        price_display = None
        try:
            price_obj = stripe.Price.retrieve(price_id)
            price_display = price_obj.unit_amount  # cents
        except Exception:
            pass
        packages.append({
            "minutes": pkg["minutes"],
            "label": pkg["label"],
            "price_cents": price_display,
            "available": bool(price_id),
        })
    return flask_jsonify({"packages": packages})


@app.route("/ai-minutes/checkout", methods=["POST"])
@login_required
def ai_minutes_checkout():
    """Create a Stripe checkout session for an AI minute package."""
    data = request.get_json() or {}
    minutes = data.get("minutes")
    if not minutes:
        return flask_jsonify({"error": "Missing 'minutes' parameter"}), 400

    # Find matching package
    pkg = next((p for p in AI_MINUTE_PACKAGES if p["minutes"] == int(minutes)), None)
    if not pkg:
        return flask_jsonify({"error": f"No package found for {minutes} minutes"}), 400

    price_id = os.getenv(pkg["env_key"], "")
    if not price_id:
        return flask_jsonify({"error": f"Price ID not configured for {pkg['label']} package"}), 500

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=current_user.email,
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={
                "purchase_type": "ai_minutes",
                "package_minutes": str(pkg["minutes"]),
                "package_label": pkg["label"],
                "user_email": current_user.email,
            },
            success_url=f"{YOUR_DOMAIN}/dashboard?ai_minutes_success=1",
            cancel_url=f"{YOUR_DOMAIN}/dashboard?ai_minutes_cancel=1",
        )
        return flask_jsonify({"checkout_url": checkout_session.url})
    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe AI minutes checkout error: {e}")
        return flask_jsonify({"error": "Payment configuration error. Contact support."}), 500
    except Exception as e:
        logger.error(f"AI minutes checkout error: {e}")
        return flask_jsonify({"error": "Unable to create checkout session."}), 500


@app.route("/ai-minutes/usage")
@login_required
def ai_minutes_usage():
    """Get AI minute usage history."""
    purchases = get_ai_minute_purchases(current_user.email)
    usage = get_ai_minute_usage(current_user.email)
    # Serialize datetimes
    for p in purchases:
        for k in ('created_at', 'completed_at'):
            if p.get(k):
                p[k] = p[k].isoformat()
    for u in usage:
        if u.get('created_at'):
            u['created_at'] = u['created_at'].isoformat()
    return flask_jsonify({"purchases": purchases, "usage": usage})


@app.route("/checkout/agency-pro")
def checkout_agency_pro():
    """
    ENTERPRISE GUEST CHECKOUT:
    - No login required (Webhook provisions account after payment).
    - Includes 'Agency Domain' validation field to deter single-user buyers.
    """
    try:
        # 1. Check if price ID is configured
        price_id = os.getenv("STRIPE_AGENCY_PRO_PRICE_ID")
        if not price_id:
            logger.error("STRIPE_AGENCY_PRO_PRICE_ID environment variable is not set!")
            return render_template_string("""
                <div style="background:#050505; color:#fff; height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                    <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; max-width:500px;">
                        <h2 style="color:#ff4444;">Configuration Error</h2>
                        <p style="color:#aaa;">The Agency Pro price ID is not configured. Please contact support.</p>
                        <p style="color:#666; font-size:0.85rem; margin-top:20px;">Error Code: MISSING_PRICE_ID</p>
                        <a href="/" style="color:#00ff88; text-decoration:none; margin-top:20px; display:inline-block;">← Back to Home</a>
                    </div>
                </div>
            """), 500

        # 2. Non-blocking email grab (Saves time for existing users)
        customer_email = current_user.email if current_user.is_authenticated else None

        # 3. Create the Stripe Session
        logger.info(f"Creating Agency Pro checkout with price_id: {price_id}")
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=customer_email,
            line_items=[{
                "price": price_id,
                "quantity": 1,
            }],
            allow_promotion_codes=True,
            
            # THE VALIDATION SPEED-BUMP:
            # This asks for their whitelabel domain. Single users won't have this.
            custom_fields=[
                {
                    "key": "agency_whitelabel_domain",
                    "label": {
                        "type": "custom",
                        "custom": "Agency Domain (e.g. app.youragency.com)"
                    },
                    "type": "text",
                }
            ],

            # IMPORTANT: This metadata is the "Key" for your Webhook to create the account
            metadata={
                "target_role": "agency_owner",
                "target_tier": "agency_pro",
                "source": "high_ticket_portal"
            },
            
            # Using absolute paths for reliability
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )

        return redirect(session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe Invalid Request Error (Agency Pro): {e}")
        return render_template_string("""
            <div style="background:#050505; color:#fff; height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; max-width:500px;">
                    <h2 style="color:#ff4444;">Stripe Configuration Error</h2>
                    <p style="color:#aaa;">There's an issue with the payment configuration. Please contact support.</p>
                    <p style="color:#666; font-size:0.85rem; margin-top:20px;">Error: {{ error }}</p>
                    <a href="/" style="color:#00ff88; text-decoration:none; margin-top:20px; display:inline-block;">← Back to Home</a>
                </div>
            </div>
        """, error=str(e)), 500
    except Exception as e:
        logger.critical(f"Pro Checkout Launch Error: {e}")
        return render_template_string("""
            <div style="background:#050505; color:#fff; height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; max-width:500px;">
                    <h2 style="color:#ff4444;">Checkout Error</h2>
                    <p style="color:#aaa;">The Enterprise Portal is temporarily unavailable. Please contact support.</p>
                    <p style="color:#666; font-size:0.85rem; margin-top:20px;">Error Code: {{ error }}</p>
                    <a href="/" style="color:#00ff88; text-decoration:none; margin-top:20px; display:inline-block;">← Back to Home</a>
                </div>
            </div>
        """, error=str(e)), 500
    
@app.route("/success")
def success():
    session_id = request.args.get("session_id")
    email = None
    customer_id = None

    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            email = checkout_session.customer_details.email.lower() if checkout_session.customer_details.email else None
            customer_id = checkout_session.customer
        except Exception as e:
            logger.error(f"Stripe session retrieve failed: {e}")

    if not email:
        flash("Could not verify payment. Please contact support.", "error")
        return redirect("/")

    # Ensure user record exists in DB (handles race condition with Stripe webhook)
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            temp_id = f"temp_{uuid.uuid4().hex[:8]}"
            cur.execute("""
                INSERT INTO subscribers (
                    location_id, email, stripe_customer_id, role, subscription_tier,
                    crm_user_id, bot_first_name, timezone
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                    stripe_customer_id = COALESCE(EXCLUDED.stripe_customer_id, subscribers.stripe_customer_id),
                    updated_at = NOW()
            """, (temp_id, email, customer_id, 'individual', 'individual',
                  '', 'Grok', 'America/Chicago'))
            conn.commit()
            logger.info(f"Success page: ensured user record exists for {email}")
        except Exception as e:
            conn.rollback()
            logger.error(f"Success page user provision error: {e}")
        finally:
            cur.close()
            return_db_connection(conn)

    user = User.get(email)
    if user:
        # If user already has a password, send to login
        if user.password_hash:
            flash("Payment confirmed! Please log in to continue.", "success")
            return redirect("/login")

        # Auto-login for the create-password form (which posts to @login_required /set-password)
        if not current_user.is_authenticated:
            login_user(user)
            logger.info(f"Auto-login after checkout for {email}")

    # Always show create password page after checkout
    # Even if DB provisioning failed, show the page - user can still set password
    return render_template('checkout-success-generate-password.html', email=email)

@app.route("/set-password", methods=["GET", "POST"])
@login_required
def set_password():
    """
    Password setup for users after OAuth.
    GET: Show password setup form
    POST: Process password and save to database
    """
    user_type = request.args.get("type", "individual")  # 'agency' or 'individual'

    if request.method == "GET":
        # Show password setup form
        return render_template('set_password.html',
                             email=current_user.email,
                             user_type=user_type)

    # POST: Process password setup
    password = request.form.get("password")
    confirm = request.form.get("confirm_password")

    if not password or len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(f"/set-password?type={user_type}")

    if password != confirm:
        flash("Passwords do not match.", "danger")
        return redirect(f"/set-password?type={user_type}")

    password_hash = generate_password_hash(password)
    conn = get_db_connection()

    if not conn:
        flash("Database unavailable. Please try again.", "error")
        return redirect(f"/set-password?type={user_type}")

    try:
        cur = conn.cursor()

        if current_user.role == 'agency_owner':
            # Update agency_billing table
            cur.execute("""
                UPDATE agency_billing
                SET password_hash = %s, updated_at = NOW()
                WHERE agency_email = %s
            """, (password_hash, current_user.email))
        else:
            # Update subscribers table
            cur.execute("""
                UPDATE subscribers
                SET password_hash = %s,
                    onboarding_status = 'claimed',
                    updated_at = NOW()
                WHERE email = %s
            """, (password_hash, current_user.email))

        conn.commit()
        logger.info(f"Password set for {current_user.email} ({current_user.role})")

        # Log out so user must log in with their new password
        logout_user()
        flash("Password created successfully! Please log in.", "success")
        return redirect("/login")

    except Exception as e:
        conn.rollback()
        logger.error(f"Set password error for {current_user.email}: {e}")
        flash("Error setting password. Please try again.", "error")
        return redirect(f"/set-password?type={user_type}")
    finally:
        cur.close()
        return_db_connection(conn)

@app.route("/refresh")
def refresh_subscribers():
    try:
        sync_subscribers()
        return "Synced", 200
    except:
        return "Failed", 500

@app.route("/oauth/initiate")
@login_required
def oauth_initiate():
    """
    Initiates OAuth flow with Lead Connector.
    Works BEFORE marketplace approval (using private app credentials).

    User clicks "Connect with Lead Connector" → Redirected to consent page → Back to /oauth/callback

    SECURITY: Requires active login and valid Stripe subscription.
    """
    # --- SUBSCRIPTION VERIFICATION ---
    # Check if user has active Stripe subscription
    # Admin whitelist bypasses subscription requirement
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    needs_subscription = not current_user.stripe_customer_id and not is_admin

    if needs_subscription:
        flash("You must have an active subscription to connect Lead Connector. Please subscribe first.", "error")
        logger.warning(f"OAuth initiate blocked for {current_user.email} - no active subscription")

        # Redirect based on user role
        if current_user.role == 'agency_owner':
            return redirect(url_for('agency_dashboard'))
        else:
            return redirect(url_for('dashboard'))

    # Toggle: set USE_PRIVATE_APP=true in .env to route all installs through
    # the private app while waiting for public marketplace scope approval.
    use_private = os.getenv("USE_PRIVATE_APP", "").lower() in ("true", "1", "yes")

    if use_private:
        client_id = os.getenv("PRIVATE_APP_CLIENT_ID")
        env_label = "PRIVATE_APP_CLIENT_ID"
    else:
        client_id = os.getenv("GHL_CLIENT_ID")
        env_label = "GHL_CLIENT_ID"

    domain = os.getenv("YOUR_DOMAIN")
    if not client_id or not domain:
        logger.error(f"OAuth initiate failed: {env_label}={'set' if client_id else 'MISSING'}, YOUR_DOMAIN={'set' if domain else 'MISSING'}")
        flash("OAuth is not configured. Please contact support.", "error")
        return redirect(url_for('dashboard'))
    redirect_uri = f"{domain}/oauth/callback"

    # Required scopes — must match the app configuration in GHL developer portal.
    scopes = [
        "calendars.readonly",           # List calendars, free slots
        "calendars/events.readonly",    # Read calendar events
        "calendars/events.write",       # Book appointments
        "calendars/groups.readonly",    # Calendar group listing
        "conversations/message.write",  # Send SMS
        "conversations/message.readonly",  # Read inbound messages
        "conversations.write",          # Conversation management
        "conversations.readonly",       # Search conversations (ghl_api.py)
        "contacts.readonly",            # Contact lookup & validation
        "oauth.readonly",              # Token info check (ghl_calendar.py)
    ]
    # Private app already has all scopes approved — include them now.
    # For the public app these are still pending marketplace review.
    if use_private:
        scopes += [
            "locations.readonly",       # Sub-account discovery
            "users.readonly",           # User info lookup
            "opportunities.readonly",   # Pipeline & stage listing for dialer filters
        ]
    scope_string = " ".join(scopes)

    # State: "private_app" when using private app, "website_user" otherwise.
    # The callback uses this to pick the right credentials for token exchange.
    state = "private_app" if use_private else "website_user"

    # CRITICAL: URL-encode scope string — raw spaces break parameter parsing
    # and cause GHL scope validation failures + state parameter loss
    from urllib.parse import urlencode
    oauth_params = urlencode({
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'scope': scope_string,
        'state': state
    })
    oauth_url = f"https://marketplace.gohighlevel.com/oauth/chooselocation?{oauth_params}"

    logger.info(f"Initiating OAuth flow for {current_user.email} (private={use_private}). Redirecting to: {oauth_url}")
    return redirect(oauth_url)

def fetch_all_ghl_items(base_url, headers, item_key='locations', max_pages=50):
    """
    Helper to handle Lead Connector pagination (fetching all locations/users).
    Prevents onboarding failures when agencies have >20 locations.

    Args:
        base_url: Initial API endpoint URL
        headers: Authorization headers
        item_key: JSON key containing items (e.g., 'locations', 'users')
        max_pages: Safety limit to prevent infinite loops

    Returns:
        List of all items across all pages
    """
    items = []
    url = base_url
    page_count = 0

    while url and page_count < max_pages:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if not resp.ok:
                if resp.status_code in (401, 403):
                    logger.warning(f"SCOPE MISSING: /{item_key}/ returned {resp.status_code} — "
                                  f"'{item_key}.readonly' scope likely not granted. "
                                  f"Falling back to token-based data.")
                else:
                    logger.error(f"Failed to fetch {item_key} (page {page_count+1}): "
                                f"{resp.status_code} {resp.text[:300]}")
                break

            data = resp.json()
            batch = data.get(item_key, [])
            items.extend(batch)
            page_count += 1

            logger.info(f"Fetched {len(batch)} {item_key} from page {page_count} (total: {len(items)})")

            # Lead Connector pagination: check both 'meta.nextPageUrl' and direct 'nextPageUrl'
            meta = data.get('meta', {})
            next_url = meta.get('nextPageUrl') or data.get('nextPageUrl')

            if next_url:
                # Handle both absolute and relative URLs
                if next_url.startswith('http'):
                    url = next_url
                else:
                    url = f"https://services.leadconnectorhq.com{next_url}"
            else:
                # No more pages
                url = None

        except Exception as e:
            logger.error(f"Pagination error fetching {item_key} (page {page_count+1}): {e}")
            break

    logger.info(f"✅ Pagination complete: {len(items)} total {item_key} fetched across {page_count} pages")
    return items

def _ghl_api_call(method, url, headers=None, data=None, timeout=15, label="GHL API"):
    """
    Make a GHL API call with 1 automatic retry on transient errors (5xx, timeout, connection).
    Returns (response, error_message). On success error_message is None.
    """
    last_err = None
    for attempt in range(2):
        try:
            if method == 'POST':
                resp = requests.post(url, data=data, headers=headers, timeout=timeout)
            else:
                resp = requests.get(url, headers=headers, timeout=timeout)

            if resp.status_code < 500:
                return resp, None

            last_err = f"{label} returned {resp.status_code}"
            logger.warning(f"{label} attempt {attempt+1}/2 got {resp.status_code}, "
                         f"body={resp.text[:300]}")
        except requests.Timeout:
            last_err = f"{label} timed out after {timeout}s"
            logger.warning(f"{label} attempt {attempt+1}/2 timed out")
        except requests.ConnectionError as e:
            last_err = f"{label} connection error: {e}"
            logger.warning(f"{label} attempt {attempt+1}/2 connection error: {e}")
        except Exception as e:
            last_err = f"{label} unexpected error: {e}"
            logger.warning(f"{label} attempt {attempt+1}/2 unexpected error: {e}")

    return None, last_err


@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")

    # LOG EVERYTHING from the very start — this is the #1 debugging tool
    logger.info(f"=== OAUTH CALLBACK START === state={state}, code={'present' if code else 'MISSING'}, "
                f"args={dict(request.args)}")

    # Always log to webhook_logs so it's visible in dashboard even if everything else fails
    try:
        log_webhook_event("oauth_global", "oauth_callback_hit", "info",
                          f"OAuth callback received: state={state}, code={'yes' if code else 'NO'}",
                          details={"args": dict(request.args)})
    except Exception:
        pass

    if not code:
        logger.warning("OAuth callback: No authorization code in request params")
        try:
            log_webhook_event("oauth_global", "oauth_callback_error", "error",
                              "No authorization code in callback params",
                              details={"args": dict(request.args)})
        except Exception:
            pass
        flash("No authorization code received.", "danger")
        return redirect(url_for('home'))

    try:
        # Determine flow based on state parameter
        # state="website_user" → Stripe/website subscriber connecting Lead Connector
        # state="private_app"  → Logged-in user reconnecting via /oauth/initiate
        # No state             → Marketplace / private app install link from GHL
        is_website_user = (state == "website_user") or (state == "private_app")

        if is_website_user:
            # --- SUBSCRIPTION VERIFICATION FOR WEBSITE USERS ---
            # Website users must have an active Stripe subscription.
            # Marketplace installations bypass this check.
            if not current_user.is_authenticated:
                flash("You must be logged in to connect Lead Connector.", "error")
                logger.warning("OAuth callback blocked - user not authenticated")
                return redirect(url_for('login'))

            is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
            needs_subscription = not current_user.stripe_customer_id and not is_admin

            if needs_subscription:
                flash("Active subscription required to connect Lead Connector. Please subscribe first.", "error")
                logger.warning(f"OAuth callback blocked for {current_user.email} - no active subscription")

                if current_user.role == 'agency_owner':
                    return redirect(url_for('agency_dashboard'))
                else:
                    return redirect(url_for('dashboard'))

            logger.info(f"OAuth callback: Website user flow for {current_user.email}")
        else:
            logger.info("OAuth callback: Marketplace installation flow")

        # --- VALIDATE ENV VARS ---
        # Pick credentials based on how the OAuth flow was initiated:
        #   state="private_app" → use private app credentials
        #   state="website_user" → use credentials based on USE_PRIVATE_APP env
        #   state=None           → GHL marketplace/app install link — auto-detect
        #
        # DUAL-APP SUPPORT: When both public (marketplace) and private apps exist,
        # GHL install links never include state. We try the primary credential set
        # first, then fall back to the other if the exchange fails. This correctly
        # handles customers who installed either app.
        use_private_env = os.getenv("USE_PRIVATE_APP", "").lower() in ("true", "1", "yes")

        # Build both credential sets for auto-detection when state=None
        marketplace_client_id = os.getenv("GHL_CLIENT_ID")
        marketplace_client_secret = os.getenv("GHL_CLIENT_SECRET")
        private_client_id = os.getenv("PRIVATE_APP_CLIENT_ID")
        private_client_secret = os.getenv("PRIVATE_APP_SECRET_ID")
        has_marketplace_creds = bool(marketplace_client_id and marketplace_client_secret)
        has_private_creds = bool(private_client_id and private_client_secret)

        if state == "private_app":
            # Explicit private app OAuth initiate flow
            is_private_app = True
            client_id = private_client_id
            client_secret = private_client_secret
            cred_label = "PRIVATE_APP"
        elif state is None and has_marketplace_creds and has_private_creds:
            # DUAL-APP MODE: Both apps configured — we'll auto-detect during token exchange
            # Start with marketplace (public) credentials as primary attempt
            is_private_app = False
            client_id = marketplace_client_id
            client_secret = marketplace_client_secret
            cred_label = "AUTO-DETECT (trying marketplace first)"
        elif state is None and use_private_env:
            # Only private app configured
            is_private_app = True
            client_id = private_client_id
            client_secret = private_client_secret
            cred_label = "PRIVATE_APP (only app)"
        else:
            # Default: marketplace credentials
            is_private_app = False
            client_id = marketplace_client_id
            client_secret = marketplace_client_secret
            cred_label = "GHL (marketplace)"
        domain = os.getenv("YOUR_DOMAIN")

        if not client_id or not client_secret or not domain:
            logger.error(f"OAuth env vars missing ({cred_label}): client_id={'set' if client_id else 'MISSING'}, "
                        f"client_secret={'set' if client_secret else 'MISSING'}, "
                        f"YOUR_DOMAIN={'set' if domain else 'MISSING'}")
            flash("OAuth is not configured. Please contact support.", "danger")
            return redirect(url_for('home'))
        logger.info(f"OAuth callback using {cred_label} credentials (state={state})")

        # 1. Exchange Code for Token (with retry on transient failures)
        # TRY BOTH user_types: "Location" first, then "Company" for agency-level installs.
        # GHL marketplace installs can be at Location OR Company level depending on
        # how the user installed the app. If Location fails with 400, try Company.
        #
        # DUAL-APP: If both credential sets exist and state=None, also try the other
        # app's credentials if the first set fails (auto-detect which app was installed).
        token_url = "https://services.leadconnectorhq.com/oauth/token"

        # Build list of credential sets to try (primary first, fallback second)
        cred_sets = [{"client_id": client_id, "client_secret": client_secret,
                      "label": cred_label, "is_private": is_private_app}]
        if state is None and has_marketplace_creds and has_private_creds:
            # Add fallback credential set (the one we didn't try first)
            cred_sets.append({"client_id": private_client_id, "client_secret": private_client_secret,
                              "label": "PRIVATE_APP (fallback)", "is_private": True})

        token_data = None
        token_user_type_used = None
        for cred_set in cred_sets:
            base_payload = {
                "client_id": cred_set["client_id"],
                "client_secret": cred_set["client_secret"],
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{domain}/oauth/callback"
            }

            for user_type in ["Location", "Company"]:
                payload = {**base_payload, "user_type": user_type}
                logger.info(f"Token exchange attempt with user_type={user_type}, creds={cred_set['label']}")

                token_resp, token_err = _ghl_api_call('POST', token_url, data=payload,
                                                       timeout=15, label=f"Token exchange ({user_type}, {cred_set['label']})")

                if token_resp is None:
                    logger.warning(f"Token exchange ({user_type}, {cred_set['label']}) unreachable: {token_err}")
                    continue

                if token_resp.ok:
                    try:
                        token_data = token_resp.json()
                        token_user_type_used = user_type
                        is_private_app = cred_set["is_private"]
                        cred_label = cred_set["label"]
                        logger.info(f"Token exchange SUCCESS with user_type={user_type}, creds={cred_set['label']}")
                        break
                    except ValueError:
                        logger.error(f"Token exchange ({user_type}, {cred_set['label']}) returned non-JSON: {token_resp.text[:500]}")
                        continue
                elif token_resp.status_code == 400:
                    logger.warning(f"Token exchange ({user_type}, {cred_set['label']}) got 400: {token_resp.text[:300]} — trying next")
                    continue
                else:
                    logger.error(f"Token exchange ({user_type}, {cred_set['label']}) rejected: {token_resp.status_code} {token_resp.text[:500]}")
                continue

            if token_data:
                break  # Found working credentials, stop trying other credential sets

        if not token_data:
            err_msg = f"Token exchange failed for all user_types (Location, Company)"
            logger.error(err_msg)
            try:
                log_webhook_event("oauth_global", "oauth_token_exchange_failed", "error",
                                  err_msg, details={"state": state, "code_present": bool(code)})
            except Exception:
                pass
            flash("Failed to connect to Lead Connector. Please try again.", "danger")
            return redirect(url_for('home'))

        access_token = token_data.get('access_token')
        if not access_token:
            err_msg = f"Token exchange missing access_token: {json.dumps(token_data)[:500]}"
            logger.error(err_msg)
            try:
                log_webhook_event("oauth_global", "oauth_no_access_token", "error",
                                  err_msg, details=token_data)
            except Exception:
                pass
            flash("Authorization failed — no access token received. Please try again.", "danger")
            return redirect(url_for('home'))

        primary_location_id = token_data.get('locationId')
        company_id = token_data.get('companyId')
        refresh_token = token_data.get('refresh_token')
        expires_in = token_data.get('expires_in', 86400)

        logger.info(f"Step 1 complete: Token exchange OK via user_type={token_user_type_used}. "
                    f"locationId={primary_location_id}, companyId={company_id}, expires_in={expires_in}")

        # If Company-level install, locationId may be empty — we get locations from /locations/ later
        # Log this critical info for debugging
        try:
            log_webhook_event(primary_location_id or company_id or "unknown", "oauth_token_success", "success",
                              f"Token exchange OK: user_type={token_user_type_used}, "
                              f"locationId={primary_location_id}, companyId={company_id}",
                              details={"user_type_used": token_user_type_used,
                                       "locationId": primary_location_id,
                                       "companyId": company_id,
                                       "scopes": token_data.get('scope', 'unknown')})
        except Exception:
            pass

        headers = {'Authorization': f'Bearer {access_token}', 'Version': '2021-07-28'}

        # 2. Get user info (with retry)
        me_resp, me_err = _ghl_api_call('GET', "https://services.leadconnectorhq.com/users/me",
                                         headers=headers, timeout=10, label="/users/me")
        me_data = {}
        user_email = None
        user_name = None
        # Pre-populate me_data with userId from token (fallback if /users/me fails)
        ghl_user_id = token_data.get('userId')
        if ghl_user_id:
            me_data['id'] = ghl_user_id

        if me_resp and me_resp.ok:
            try:
                me_data = me_resp.json()
                user_email = me_data.get('email')
                user_name = me_data.get('name')
            except ValueError:
                logger.error(f"/users/me returned non-JSON: {me_resp.text[:300]}")
        elif me_resp:
            logger.error(f"/users/me failed: {me_resp.status_code} {me_resp.text[:300]}")
            if me_resp.status_code in (401, 403):
                logger.error("SCOPE ISSUE: /users/me returned 401/403 — token may lack required scopes")
        else:
            logger.error(f"/users/me unreachable: {me_err}")

        # --- ROBUST EMAIL RECOVERY CHAIN ---
        # OAuth must NEVER fail. Try every source, create placeholder as last resort.
        # Each step is independent — if one fails, the next one tries.

        # Fallback 1: token_data may include email
        if not user_email:
            user_email = token_data.get('userEmail') or token_data.get('email')
            if user_email:
                logger.info(f"Fallback 1: Got email from token_data: {user_email}")

        # Fallback 2: user is already logged in (website users OR private app installs)
        # Private app installs come through as marketplace flow (is_website_user=False)
        # but the user may already be logged in — use their email instead of a placeholder.
        if not user_email and current_user.is_authenticated:
            user_email = current_user.email
            user_name = current_user.full_name or user_name
            logger.info(f"Fallback 2: Using logged-in user's email: {user_email}")

        # Fallback 3: BRIDGE — check marketplace_installs table
        if not user_email:
            market_data = find_marketplace_email(
                location_id=primary_location_id, company_id=company_id
            )
            if market_data:
                user_email = market_data.get('user_email')
                user_name = market_data.get('user_name') or user_name
                logger.info(f"Fallback 3: BRIDGED email from marketplace_installs: {user_email}")

        # Fallback 4: check existing subscribers by userId
        if not user_email:
            ghl_user_id = token_data.get('userId')
            if ghl_user_id:
                try:
                    conn_lookup = get_db_connection()
                    if conn_lookup:
                        cur_lookup = conn_lookup.cursor()
                        cur_lookup.execute("SELECT email FROM subscribers WHERE crm_user_id = %s LIMIT 1", (ghl_user_id,))
                        found = cur_lookup.fetchone()
                        if found:
                            user_email = found['email']
                            logger.info(f"Fallback 4: Found email via userId lookup: {user_email}")
                        cur_lookup.close()
                        return_db_connection(conn_lookup)
                except Exception:
                    pass

        # Fallback 5: PLACEHOLDER — create a temporary identity so onboarding completes
        if not user_email:
            ghl_user_id = token_data.get('userId') or 'unknown'
            user_email = f"install_{ghl_user_id}@placeholder.grokbot"
            user_name = "New User (Update Email)"
            logger.warning(f"Fallback 5: ALL email sources exhausted. Using placeholder: {user_email}")

            # Log to webhook_logs for visibility
            try:
                log_webhook_event(primary_location_id or "unknown", "oauth_placeholder_account", "warning",
                                  f"Placeholder account created: {user_email} — userId={ghl_user_id}, "
                                  f"locationId={primary_location_id}, companyId={company_id}",
                                  details={"userId": ghl_user_id,
                                           "locationId": primary_location_id,
                                           "companyId": company_id,
                                           "token_keys": list(token_data.keys())})
            except Exception:
                pass

            # ADMIN ALERT: email the admin so they can manually resolve
            try:
                from send_email_api import send_email_via_api
                admin_target = ADMIN_EMAILS[0] if ADMIN_EMAILS else "mitchell_vandusen@hotmail.com"
                domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
                alert_inner = f'''
<tr><td style="padding: 20px 40px 30px;">
    <h1 style="margin: 0 0 16px; font-size: 22px; font-weight: 800; color: #ff6b35;">Ghost Install Detected</h1>
    <p style="font-size: 15px; color: #ccc; line-height: 1.6;">
        A user installed the app but Lead Connector permissions blocked their email.
        A placeholder account was created so they can access the dashboard.
    </p>
    <table cellpadding="0" cellspacing="0" width="100%" style="background: rgba(255,255,255,0.04); border-radius: 10px; border: 1px solid rgba(255,255,255,0.08); margin: 20px 0;">
        <tr><td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #888; width: 130px;">LC User ID</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #fff; font-family: monospace;">{ghl_user_id}</td></tr>
        <tr><td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #888;">Location ID</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #fff; font-family: monospace;">{primary_location_id or 'N/A'}</td></tr>
        <tr><td style="padding: 12px 16px; color: #888;">Company ID</td>
            <td style="padding: 12px 16px; color: #fff; font-family: monospace;">{company_id or 'N/A'}</td></tr>
    </table>
    <p style="font-size: 14px; color: #aaa;">Search this Location ID in your Lead Connector Agency View to find the user's real email, then update their record in the database.</p>
</td></tr>'''
                send_email_via_api(
                    to_email=admin_target,
                    subject="Ghost Install — Action Required",
                    html_body=_email_wrapper(alert_inner, domain_url),
                    text_body=f"Ghost install: userId={ghl_user_id}, locationId={primary_location_id}, companyId={company_id}"
                )
                logger.info(f"Admin ghost-install alert sent to {admin_target}")
            except Exception as e:
                logger.error(f"Failed to send admin ghost-install alert: {e}")

            # Save to persistent_alerts too
            try:
                save_persistent_alert(
                    ADMIN_EMAILS[0] if ADMIN_EMAILS else "admin",
                    primary_location_id or "unknown",
                    "ghost_install", "warning",
                    "Ghost Install — Email Unknown",
                    f"User installed app but email couldn't be retrieved. "
                    f"userId={ghl_user_id}, locationId={primary_location_id}, companyId={company_id}. "
                    f"Placeholder account: {user_email}"
                )
            except Exception:
                pass

        logger.info(f"Step 2 complete: User info retrieved. email={user_email}")

        # 3. Detect agency status (with retry and safe JSON parsing)
        is_agency_owner = False
        agencies = []

        agency_resp, agency_err = _ghl_api_call('GET', "https://services.leadconnectorhq.com/agencies/",
                                                  headers=headers, timeout=10, label="/agencies/")

        if agency_resp and agency_resp.ok:
            try:
                agencies = agency_resp.json().get('agencies', [])
                is_agency_owner = len(agencies) > 0
            except (ValueError, KeyError, AttributeError):
                logger.warning(f"/agencies/ returned unparseable response: {agency_resp.text[:300]}")
                agencies = []
        elif agency_resp and agency_resp.status_code < 500:
            # 4xx — user likely doesn't have agency access, treat as individual
            logger.info(f"/agencies/ returned {agency_resp.status_code} — treating as individual user")
        else:
            # Transient failure after retries — log but continue as individual
            # Better to onboard as individual than to fail entirely
            logger.warning(f"/agencies/ unavailable ({agency_err}), defaulting to individual classification")

        logger.info(f"Step 3 complete: Agency detection. is_agency={is_agency_owner}, count={len(agencies)}")

        # 4. Fetch all locations (sub-accounts) with PAGINATION
        # GHL API v2 uses /locations/search with companyId, not /locations/
        locations_url = "https://services.leadconnectorhq.com/locations/search"
        if company_id:
            locations_url += f"?companyId={company_id}"
        sub_accounts = fetch_all_ghl_items(
            locations_url,
            headers,
            item_key='locations'
        )
        num_subs = len(sub_accounts)
        logger.info(f"Step 4 complete: {num_subs} locations fetched for {user_email}")

        # Fallback: if /locations/ returned 0 results but we have a locationId from the token,
        # synthesize a minimal location entry so onboarding still works.
        # This happens when 'locations.readonly' scope isn't available (pending marketplace approval).
        using_location_fallback = False
        if num_subs == 0 and primary_location_id:
            using_location_fallback = True
            logger.warning(f"locations.readonly scope likely missing — /locations/ returned 0 results "
                          f"but token has locationId={primary_location_id}. Using fallback location entry.")
            sub_accounts = [{
                'id': primary_location_id,
                'name': user_name or 'Primary Location',
                'timezone': None  # Will default to America/Chicago downstream
            }]
            num_subs = 1
            logger.info(f"Fallback: synthesized 1 location entry from token's locationId")

        # 5. Determine tier and whether to use agency onboarding flow
        # KEY DISTINCTION: Being an agency owner in GHL ≠ subscribing to an agency plan.
        # An agency owner may only want the bot for themselves (individual plan).
        # Website users already chose their plan via Stripe — respect that choice.
        if is_website_user:
            plan_tier = current_user.subscription_tier or 'individual'
            use_agency_flow = plan_tier in ('agency_starter', 'agency_pro')
            logger.info(f"Website user: subscribed tier={plan_tier}, GHL agency={is_agency_owner}, "
                        f"using agency flow={use_agency_flow}")
        else:
            # Marketplace install: auto-detect from GHL account structure
            plan_tier = 'individual'
            if is_agency_owner:
                plan_tier = 'agency_pro' if num_subs >= 15 else 'agency_starter'
            use_agency_flow = is_agency_owner

        # 6. Get primary location details
        primary_sub = next((s for s in sub_accounts if s['id'] == primary_location_id), None)
        primary_name = primary_sub.get('name', 'Unknown Location') if primary_sub else user_name
        primary_timezone = primary_sub.get('timezone', None) if primary_sub else None

        logger.info(f"Step 5-6 complete: tier={plan_tier}, agency_flow={use_agency_flow}, primary_location={primary_name}")

        # 7. Database operations (with retry — critical path)
        conn = get_db_connection_with_retry(max_attempts=3)
        if not conn:
            logger.error("OAuth callback: Database connection failed after 3 retries — cannot complete onboarding")
            flash("Database temporarily unavailable. Please try connecting again in a few minutes.", "danger")
            return redirect(url_for('home'))

        try:
            cur = conn.cursor()

            # --- A. Agency Owner Primary Location (only if subscribed to agency tier) ---
            logger.info(f"Step 7a: use_agency_flow={use_agency_flow}, is_website_user={is_website_user}, primary_location_id={primary_location_id}")
            if use_agency_flow:
                # Agency Starter: max 14 seats, Agency Pro: unlimited
                max_seats = 9999 if plan_tier == 'agency_pro' else 14
                active_seats = max(0, num_subs - 1)  # Exclude primary

                # Determine OAuth app type — private app gets its own type so
                # token refresh uses the correct client credentials
                app_type = 'private' if is_private_app else ('website' if is_website_user else 'marketplace')

                cur.execute("""
                    INSERT INTO agency_billing (
                        agency_email, location_id, full_name, subscription_tier,
                        max_seats, active_seats, access_token, refresh_token,
                        token_expires_at, timezone, crm_user_id, oauth_app_type,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW() + interval '%s seconds', %s, %s, %s, NOW(), NOW()
                    )
                    ON CONFLICT (agency_email) DO UPDATE SET
                        location_id = EXCLUDED.location_id,
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        token_expires_at = EXCLUDED.token_expires_at,
                        crm_user_id = COALESCE(EXCLUDED.crm_user_id, agency_billing.crm_user_id),
                        oauth_app_type = EXCLUDED.oauth_app_type,
                        updated_at = NOW()
                """, (
                    user_email, primary_location_id, primary_name, plan_tier,
                    max_seats, active_seats, access_token, refresh_token,
                    expires_in, primary_timezone or 'America/Chicago', me_data.get('id'), app_type
                ))

            # --- B. Reconnect / Reinstall Sync ---
            # If the user already exists in subscribers (by email OR location_id),
            # just sync OAuth tokens without wiping any existing data (password,
            # stripe, voice_config, calendar, bot settings, etc.).
            # This handles: reinstalls, public→private app switches, re-auths.
            app_type = 'private' if is_private_app else ('website' if is_website_user else 'marketplace')

            # B1. Check for existing subscriber by email (covers temp_ and real location_ids)
            cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (user_email,))
            existing_row = cur.fetchone()

            if existing_row and primary_location_id:
                existing_loc = existing_row['location_id']
                # Sync OAuth tokens onto existing row — update location_id if it
                # was a temp placeholder, otherwise keep existing data intact.
                # Agency owners need role='agency_owner' in subscribers so
                # is_agency_owner works (User.get checks subscribers first).
                sync_role = 'agency_owner' if use_agency_flow else None

                cur.execute("""
                    UPDATE subscribers
                    SET location_id = %s,
                        access_token = %s,
                        refresh_token = %s,
                        token_expires_at = NOW() + interval '%s seconds',
                        crm_user_id = COALESCE(%s, crm_user_id),
                        oauth_app_type = %s,
                        role = COALESCE(%s, role),
                        parent_agency_email = COALESCE(%s, parent_agency_email),
                        onboarding_status = CASE
                            WHEN onboarding_status IN ('pending', 'invited') THEN 'claimed'
                            ELSE onboarding_status
                        END,
                        updated_at = NOW()
                    WHERE email = %s
                """, (primary_location_id, access_token, refresh_token,
                      expires_in, me_data.get('id'), app_type,
                      sync_role, user_email if use_agency_flow else None,
                      user_email))
                logger.info(f"Synced OAuth tokens for existing subscriber {user_email} "
                           f"(location: {existing_loc} → {primary_location_id}, app_type={app_type})")

            # --- C. Subscriber rows ---
            # Agency flow: provision ALL sub-accounts (skip primary — handled in agency_billing)
            # Individual flow: only provision the primary location
            # This ensures agency owners who subscribe as individual don't get all
            # sub-accounts provisioned — they just want their own location.
            #
            # FALLBACK MODE: When locations.readonly is unavailable, we only have the
            # primary location from the token. Agency owners still need at least their
            # primary location provisioned as a subscriber row so the bot works.
            # Sub-accounts will be discovered once the scope is approved.
            if use_agency_flow and not using_location_fallback:
                locations_to_provision = [s for s in sub_accounts if s['id'] != primary_location_id]
            else:
                # Individual flow OR agency-in-fallback: provision the primary location
                locations_to_provision = [s for s in sub_accounts if s['id'] == primary_location_id]

            if using_location_fallback and use_agency_flow:
                logger.warning(f"Agency owner {user_email} in FALLBACK MODE: only provisioning primary "
                              f"location {primary_location_id}. Sub-accounts will be added once "
                              f"locations.readonly scope is approved.")

            # Skip locations that were already synced above (the primary for the
            # logged-in user).  Only INSERT truly new locations (agency sub-accounts).
            if existing_row and primary_location_id:
                locations_to_provision = [s for s in locations_to_provision
                                         if s['id'] != primary_location_id]

            logger.info(f"Step 7c: Provisioning {len(locations_to_provision)} NEW subscriber rows "
                        f"(agency_flow={use_agency_flow}, fallback={using_location_fallback}, "
                        f"total_ghl_locations={num_subs})")

            for sub in locations_to_provision:
                sub_id = sub['id']
                sub_name = sub.get('name', 'Unknown Location')
                sub_timezone = sub.get('timezone')

                agent_email = user_email
                agent_name = sub_name
                agent_crm_user_id = me_data.get('id')

                # Agency tokens work for all locations under that agency
                access_token_this = access_token
                refresh_token_this = refresh_token

                # Agency owner's own location (fallback mode) → 'agency_owner'
                # Other agency locations → 'agency_sub_account_user'
                is_owner_location = (sub_id == primary_location_id)
                if use_agency_flow and is_owner_location:
                    role = 'agency_owner'
                elif use_agency_flow:
                    role = 'agency_sub_account_user'
                else:
                    role = 'individual'
                parent_agency_email = user_email if use_agency_flow else None
                email_this = user_email

                cur.execute("""
                    INSERT INTO subscribers (
                        location_id, email, full_name, role, subscription_tier,
                        parent_agency_email, access_token, refresh_token,
                        token_expires_at, timezone, crm_user_id,
                        onboarding_status, oauth_app_type, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW() + interval '%s seconds',
                        %s, %s, %s, %s, NOW(), NOW()
                    )
                    ON CONFLICT (location_id) DO UPDATE SET
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        token_expires_at = EXCLUDED.token_expires_at,
                        crm_user_id = COALESCE(EXCLUDED.crm_user_id, subscribers.crm_user_id),
                        oauth_app_type = EXCLUDED.oauth_app_type,
                        updated_at = NOW()
                """, (
                    sub_id, email_this, agent_name, role, plan_tier,
                    parent_agency_email, access_token_this, refresh_token_this,
                    expires_in,
                    sub_timezone or 'America/Chicago', agent_crm_user_id,
                    'pending', app_type
                ))

            conn.commit()
            logger.info(f"Step 7 complete: Onboarded {user_email} (tier={plan_tier}, agency_flow={use_agency_flow}) "
                        f"— provisioned {len(locations_to_provision)} locations out of {num_subs} total in GHL.")

        except Exception as e:
            conn.rollback()
            logger.error(f"Database onboarding error for {user_email}: {e}", exc_info=True)
            flash("Error completing setup. Please contact support.", "danger")
            return redirect(url_for('home'))
        finally:
            cur.close()
            return_db_connection(conn)

        # --- 8. POST-ONBOARDING: Logging, Alerts, Email ---

        # 8a. Log onboarding event to webhook_logs (visible in dashboard)
        try:
            log_webhook_event(
                location_id=primary_location_id,
                event_type="oauth_onboarding",
                status="success",
                summary=f"OAuth onboarding complete for {user_email}",
                details={
                    "email": user_email,
                    "tier": plan_tier,
                    "agency_flow": use_agency_flow,
                    "locations_provisioned": len(locations_to_provision),
                    "total_ghl_locations": num_subs,
                    "fallback_mode": using_location_fallback,
                    "is_website_user": is_website_user,
                }
            )
        except Exception as e:
            logger.warning(f"Failed to log onboarding event: {e}")

        # 8a-2. Stamp install_completed_at for reminder email scheduling
        try:
            _conn = get_db_connection_with_retry(2)
            if _conn:
                _cur = _conn.cursor()
                if use_agency_flow:
                    _cur.execute(
                        "UPDATE agency_billing SET install_completed_at = NOW() WHERE agency_email = %s AND install_completed_at IS NULL",
                        (user_email,))
                else:
                    _cur.execute(
                        "UPDATE subscribers SET install_completed_at = NOW() WHERE email = %s AND install_completed_at IS NULL",
                        (user_email,))
                _conn.commit()
                _cur.close()
                return_db_connection(_conn)
                logger.info(f"Install timestamp set for {user_email}")
        except Exception as e:
            logger.warning(f"Failed to set install_completed_at: {e}")

        # 8a-3. Mark marketplace install as OAuth-complete (links back to app.installed webhook)
        try:
            if primary_location_id:
                mark_install_oauth_complete(location_id=primary_location_id)
            if company_id:
                mark_install_oauth_complete(company_id=company_id)
            logger.info(f"Marketplace install marked OAuth-complete: location={primary_location_id}, company={company_id}")
        except Exception as e:
            logger.debug(f"mark_install_oauth_complete note: {e}")

        # 8b. Persistent alerts for scope/location issues
        if using_location_fallback:
            try:
                alert_msg = (
                    "Your Lead Connector account connected successfully, but the "
                    "locations.readonly scope is not yet approved in the Lead Connector marketplace. "
                    "Your primary location is active and the bot is operational. "
                )
                if use_agency_flow:
                    alert_msg += (
                        f"However, your sub-account locations could not be discovered. "
                        f"They will be auto-provisioned once the scope is approved. "
                        f"Contact support if this persists beyond 10 days."
                    )
                else:
                    alert_msg += "No action needed. This will resolve automatically."

                save_persistent_alert(
                    email=user_email,
                    alert_type="scope_locations_readonly",
                    title="Scope Pending: locations.readonly",
                    message=alert_msg,
                    severity="warning" if use_agency_flow else "info",
                    location_id=primary_location_id
                )
                log_webhook_event(
                    location_id=primary_location_id,
                    event_type="scope_issue",
                    status="warning",
                    summary="locations.readonly scope unavailable — using fallback",
                    details={"fallback": True, "agency_flow": use_agency_flow}
                )
            except Exception as e:
                logger.warning(f"Failed to save scope alert: {e}")

        # 8c. Welcome email on install (premium branded template)
        try:
            from send_email_api import send_email_via_api
            domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
            dashboard_link = f"{domain_url}/agency-dashboard" if use_agency_flow else f"{domain_url}/dashboard"

            welcome_inner = f'''
<tr>
<td align="center" style="padding: 0 40px 10px;">
    <div style="width: 80px; height: 80px; border-radius: 50%; background: linear-gradient(135deg, rgba(0,200,83,0.15) 0%, rgba(0,200,83,0.05) 100%); border: 2px solid rgba(0,200,83,0.25); margin: 0 auto 20px; line-height: 80px; text-align: center;">
        <span style="font-size: 36px;">&#127881;</span>
    </div>
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">Welcome to InsuranceGrokBot</h1>
    <p style="margin: 0; font-size: 16px; color: #aaa; line-height: 1.5;">Your AI-powered insurance sales assistant is ready</p>
</td>
</tr>

<tr>
<td style="padding: 25px 40px 15px;">
    <p style="margin: 0; font-size: 16px; color: #ddd; line-height: 1.7;">Hi {user_name},</p>
    <p style="margin: 12px 0 0; font-size: 15px; color: #bbb; line-height: 1.7;">
        Your Lead Connector account has been successfully connected. GrokBot is configured and standing by to handle your leads, qualify prospects with real insurance knowledge, and book appointments directly on your calendar.
    </p>
</td>
</tr>

<!-- Quick Links -->
<tr>
<td style="padding: 10px 40px 20px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 1.5px;">Get Started</p>
        <table cellpadding="0" cellspacing="0" width="100%">
            <tr>
                <td style="padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="36" style="vertical-align: middle;"><span style="font-size: 18px;">&#128640;</span></td>
                        <td style="vertical-align: middle;">
                            <a href="{dashboard_link}" style="color: #00c853; font-weight: 600; text-decoration: none; font-size: 15px;">Your Dashboard</a>
                            <span style="color: #888; font-size: 13px;"> &mdash; Configure your bot, set your calendar, customize settings</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="36" style="vertical-align: middle;"><span style="font-size: 18px;">&#128172;</span></td>
                        <td style="vertical-align: middle;">
                            <a href="{domain_url}/support" style="color: #00c853; font-weight: 600; text-decoration: none; font-size: 15px;">Support & FAQ</a>
                            <span style="color: #888; font-size: 13px;"> &mdash; Questions about setup, integration, or billing</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 12px 0;">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="36" style="vertical-align: middle;"><span style="font-size: 18px;">&#128203;</span></td>
                        <td style="vertical-align: middle;">
                            <a href="{domain_url}/onboarding-status" style="color: #00c853; font-weight: 600; text-decoration: none; font-size: 15px;">Onboarding Status</a>
                            <span style="color: #888; font-size: 13px;"> &mdash; Track your setup progress in real time</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
        </table>
    </div>
</td>
</tr>

<!-- What GrokBot Does -->
<tr>
<td style="padding: 5px 40px 20px;">
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td width="33%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-radius: 12px 0 0 12px; border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 24px; margin-bottom: 6px;">&#9889;</div>
            <div style="font-size: 13px; color: #ccc; font-weight: 600;">Instant Replies</div>
            <div style="font-size: 11px; color: #888; margin-top: 2px;">5-second response</div>
        </td>
        <td width="34%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 24px; margin-bottom: 6px;">&#128218;</div>
            <div style="font-size: 13px; color: #ccc; font-weight: 600;">Expert Knowledge</div>
            <div style="font-size: 11px; color: #888; margin-top: 2px;">Real insurance IQ</div>
        </td>
        <td width="33%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-radius: 0 12px 12px 0; border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 24px; margin-bottom: 6px;">&#128197;</div>
            <div style="font-size: 13px; color: #ccc; font-weight: 600;">Auto Booking</div>
            <div style="font-size: 11px; color: #888; margin-top: 2px;">Straight to calendar</div>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- CTA -->
<tr>
<td align="center" style="padding: 10px 40px 25px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: linear-gradient(135deg, #00c853 0%, #00e676 100%); border-radius: 14px; box-shadow: 0 4px 20px rgba(0,200,83,0.3);">
            <a href="{dashboard_link}" style="display: inline-block; padding: 18px 48px; color: #000000; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: -0.3px;">
                Open My Dashboard &rarr;
            </a>
        </td>
    </tr></table>
</td>
</tr>

<tr>
<td style="padding: 0 40px 10px;">
    <p style="margin: 0; font-size: 14px; color: #888; line-height: 1.6; text-align: center;">
        Questions? <a href="{domain_url}/support" style="color: #00c853; text-decoration: none; font-weight: 600;">Visit support</a> or just reply to this email.
    </p>
</td>
</tr>
'''
            welcome_html = _email_wrapper(welcome_inner, domain_url)
            email_sent = send_email_via_api(
                to_email=user_email,
                subject="Welcome to InsuranceGrokBot — Your AI Assistant is Ready",
                html_body=welcome_html,
                text_body=f"Welcome to InsuranceGrokBot, {user_name}! "
                          f"Dashboard: {dashboard_link} | "
                          f"Support: {domain_url}/support | Status: {domain_url}/onboarding-status"
            )
            if email_sent:
                logger.info(f"Welcome email sent to {user_email}")
                log_webhook_event(primary_location_id, "welcome_email", "success",
                                  f"Welcome email sent to {user_email}")
            else:
                logger.warning(f"Welcome email failed for {user_email}")
                log_webhook_event(primary_location_id, "welcome_email", "error",
                                  f"Welcome email failed for {user_email}")
        except Exception as e:
            logger.warning(f"Welcome email error for {user_email}: {e}")

        # --- 9. Login and redirect ---
        user = User.get(user_email)
        if user:
            login_user(user)
            logger.info(f"Step 9 complete: Logged in {user_email}")
        else:
            logger.error(f"User.get({user_email}) returned None after successful DB commit — login failed")
            # Still continue for marketplace installs — they don't need to be logged in
            if is_website_user:
                flash("Account created but login failed. Please log in manually.", "warning")
                return redirect(url_for('login'))

        # MARKETPLACE INSTALLATION: Route to dashboard (they're now logged in)
        if not is_website_user:
            logger.info(f"=== MARKETPLACE INSTALL COMPLETE for {user_email} ===")
            try:
                log_webhook_event(primary_location_id or "unknown", "oauth_complete", "success",
                                  f"Marketplace install complete for {user_email} "
                                  f"(tier={plan_tier}, user_type_used={token_user_type_used})")
            except Exception:
                pass
            if user:
                flash("App installed successfully! Complete your dashboard setup to activate your bot.", "success")
                if use_agency_flow:
                    return redirect(url_for('agency_dashboard'))
                return redirect(url_for('dashboard'))
            else:
                # User couldn't be logged in — send to login page
                flash("App installed! Please log in or create a password to access your dashboard.", "success")
                return redirect(url_for('login'))

        # PRIVATE APP FLOW: Route through OAuth loading screen
        logger.info(f"=== PRIVATE APP OAUTH COMPLETE for {user_email} ===")
        try:
            log_webhook_event(primary_location_id or "unknown", "oauth_complete", "success",
                              f"Private app OAuth complete for {user_email} (tier={plan_tier})")
        except Exception:
            pass
        return redirect("/oauth/loading")

    except requests.RequestException as e:
        logger.error(f"OAuth network error: {e}", exc_info=True)
        try:
            log_webhook_event("oauth_global", "oauth_network_error", "error",
                              f"OAuth network error: {e}")
        except Exception:
            pass
        flash("Failed to connect to Lead Connector. Please try again.", "danger")
        return redirect(url_for('home'))
    except Exception as e:
        logger.error(f"Critical OAuth failure: {e}", exc_info=True)
        try:
            log_webhook_event("oauth_global", "oauth_critical_error", "error",
                              f"Critical OAuth failure: {e}")
        except Exception:
            pass
        flash("An unexpected error occurred. Please try again or contact support.", "danger")
        return redirect(url_for('home'))

# ============================================================
# OAUTH LOADING SCREEN & ONBOARDING CHECK API
# ============================================================

@app.route("/oauth/loading")
@login_required
def oauth_loading():
    """Loading screen shown after OAuth to visualize data gathering progress."""
    return render_template('oauth-loading.html')


@app.route("/api/onboarding-check")
@login_required
def onboarding_check():
    """API endpoint for the OAuth loading screen to check data status in real-time."""
    user = User.get(current_user.email)
    if not user:
        return flask_jsonify({"error": "User not found"}), 404

    loc_ok = bool(user.location_id and not str(user.location_id).startswith("temp_"))

    checks = [
        {
            "key": "location_id",
            "label": "Location ID",
            "status": "success" if loc_ok else "pending",
            "value": user.location_id if loc_ok else None
        },
        {
            "key": "user_id",
            "label": "CRM User ID",
            "status": "success" if user.crm_user_id else "pending",
            "value": user.crm_user_id
        },
        {
            "key": "access_token",
            "label": "Access Token",
            "status": "success" if user.access_token else "pending",
            "value": "Connected" if user.access_token else None
        },
        {
            "key": "refresh_token",
            "label": "Recovery Token",
            "status": "success" if user.refresh_token else "pending",
            "value": "Connected" if user.refresh_token else None
        },
        {
            "key": "calendars",
            "label": "Calendars",
            "status": "success" if user.calendar_id else "pending",
            "value": user.calendar_name if user.calendar_id else "Available after dashboard config"
        },
    ]

    # Core items that must be connected (calendars configured later in dashboard)
    core_keys = ["location_id", "user_id", "access_token", "refresh_token"]
    all_connected = all(c["status"] == "success" for c in checks if c["key"] in core_keys)

    return flask_jsonify({
        "checks": checks,
        "all_connected": all_connected,
        "email": user.email
    })


# ============================================================
# SUB-USER INVITE SYSTEM
# ============================================================

def send_invite_email(to_email: str, agent_name: str, agency_name: str, invite_url: str):
    """
    Send the onboarding invite email to a sub-account user.
    """
    subject = f"You're invited to InsuranceGrokBot by {agency_name}"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #2563eb;">Welcome to InsuranceGrokBot!</h2>

            <p>Hi {agent_name},</p>

            <p><strong>{agency_name}</strong> has set up an AI-powered sales assistant for your location
            and invited you to activate your account.</p>

            <p>Click the button below to set your password and get started:</p>

            <div style="text-align: center; margin: 30px 0;">
                <a href="{invite_url}"
                   style="background-color: #2563eb; color: white; padding: 14px 28px;
                          text-decoration: none; border-radius: 8px; font-weight: bold;
                          display: inline-block;">
                    Activate My Account
                </a>
            </div>

            <p style="color: #666; font-size: 14px;">
                This link expires in 7 days. If you didn't expect this email,
                please contact your agency administrator.
            </p>

            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">

            <p style="color: #999; font-size: 12px;">
                InsuranceGrokBot - AI-Powered Insurance Sales Assistant<br>
                <a href="{YOUR_DOMAIN}" style="color: #2563eb;">
                    {YOUR_DOMAIN}
                </a>
            </p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
    Welcome to InsuranceGrokBot!

    Hi {agent_name},

    {agency_name} has set up an AI-powered sales assistant for your location
    and invited you to activate your account.

    Click here to set your password and get started:
    {invite_url}

    This link expires in 7 days.

    - InsuranceGrokBot Team
    """

    try:
        msg = Message(
            subject=subject,
            recipients=[to_email],
            html=html_body,
            body=text_body
        )
        mail.send(msg)
        logger.info(f"Invite email sent to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send invite email to {to_email}: {e}")
        raise


@app.route("/api/agency/invite-sub-user", methods=["POST"])
@login_required
def invite_sub_user():
    """
    Agency owner invites a sub-account user to create their login.
    Sends email with unique claim link.
    """
    if current_user.role != 'agency_owner':
        return flask_jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    location_id = data.get("location_id")
    target_email = data.get("email")  # Can override the auto-detected email

    if not location_id:
        return flask_jsonify({"error": "Missing location_id"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Verify this location belongs to the agency owner
        cur.execute("""
            SELECT location_id, full_name, agent_email, onboarding_status
            FROM subscribers
            WHERE location_id = %s AND parent_agency_email = %s
        """, (location_id, current_user.email))

        sub = cur.fetchone()
        if not sub:
            return flask_jsonify({"error": "Location not found or not owned by you"}), 404

        # Determine email to use
        invite_email = target_email or sub['agent_email']
        if not invite_email:
            return flask_jsonify({"error": "No email found for this location. Please provide one."}), 400

        # Check if already claimed
        if sub['onboarding_status'] == 'claimed':
            return flask_jsonify({"error": "This user has already claimed their account"}), 400

        # Generate unique invite token
        invite_token = secrets.token_urlsafe(32)

        # Update subscriber with invite info
        cur.execute("""
            UPDATE subscribers
            SET agent_email = %s,
                invite_token = %s,
                invite_sent_at = NOW(),
                onboarding_status = 'invited',
                updated_at = NOW()
            WHERE location_id = %s
        """, (invite_email, invite_token, location_id))

        conn.commit()

        # Build invite URL
        invite_url = f"{YOUR_DOMAIN}/claim-account?token={invite_token}"

        # Send email
        try:
            send_invite_email(
                to_email=invite_email,
                agent_name=sub['full_name'],
                agency_name=current_user.full_name or "Your Agency",
                invite_url=invite_url
            )
            logger.info(f"Invite sent to {invite_email} for location {location_id}")
        except Exception as email_err:
            logger.error(f"Email send failed: {email_err}")
            # Still return success - they can use the link manually
            return flask_jsonify({
                "status": "partial",
                "message": "Invite created but email failed to send",
                "invite_url": invite_url  # Fallback: give them the link
            })

        return flask_jsonify({
            "status": "success",
            "message": f"Invite sent to {invite_email}"
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"Invite sub-user error: {e}")
        return flask_jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


@app.route("/claim-account", methods=["GET", "POST"])
def claim_account():
    """
    Sub-user claims their account using the invite token.
    GET: Show the claim form
    POST: Process the password and activate account
    """
    token = request.args.get("token") or request.form.get("token")

    if not token:
        flash("Invalid or missing invite link.", "danger")
        return redirect(url_for('home'))

    conn = get_db_connection()
    if not conn:
        flash("System error. Please try again.", "danger")
        return redirect(url_for('home'))

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Find the subscriber with this token
        cur.execute("""
            SELECT location_id, agent_email, full_name, onboarding_status, invite_sent_at
            FROM subscribers
            WHERE invite_token = %s
        """, (token,))

        sub = cur.fetchone()

        if not sub:
            flash("Invalid or expired invite link.", "danger")
            return redirect(url_for('home'))

        if sub['onboarding_status'] == 'claimed':
            flash("This account has already been claimed. Please log in.", "info")
            return redirect(url_for('login'))

        # Check if invite is expired (7 days)
        if sub['invite_sent_at']:
            from datetime import timedelta
            expiry = sub['invite_sent_at'] + timedelta(days=7)
            if datetime.now() > expiry:
                flash("This invite link has expired. Please ask your agency owner to resend.", "danger")
                return redirect(url_for('home'))

        if request.method == 'GET':
            # Show the claim form
            return render_template('claim_account.html',
                                   email=sub['agent_email'],
                                   name=sub['full_name'],
                                   token=token)

        # POST: Process the claim
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        if not password or len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template('claim_account.html',
                                   email=sub['agent_email'],
                                   name=sub['full_name'],
                                   token=token)

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template('claim_account.html',
                                   email=sub['agent_email'],
                                   name=sub['full_name'],
                                   token=token)

        # Hash password and activate account
        password_hash = generate_password_hash(password)

        cur.execute("""
            UPDATE subscribers
            SET password_hash = %s,
                email = %s,
                invite_token = NULL,
                invite_claimed_at = NOW(),
                onboarding_status = 'claimed',
                updated_at = NOW()
            WHERE location_id = %s
        """, (password_hash, sub['agent_email'], sub['location_id']))

        conn.commit()

        logger.info(f"Account claimed: {sub['agent_email']} for location {sub['location_id']}")
        flash("Account activated! You can now log in.", "success")
        return redirect(url_for('login'))

    except Exception as e:
        conn.rollback()
        logger.error(f"Claim account error: {e}")
        flash("An error occurred. Please try again.", "danger")
        return redirect(url_for('home'))
    finally:
        cur.close()
        return_db_connection(conn)


@app.route("/api/agency/resend-invite", methods=["POST"])
@login_required
def resend_invite():
    """Re-send invite email to a sub-account user."""
    if current_user.role != 'agency_owner':
        return flask_jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    location_id = data.get("location_id")

    if not location_id:
        return flask_jsonify({"error": "Missing location_id"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT location_id, full_name, agent_email, invite_token, onboarding_status
            FROM subscribers
            WHERE location_id = %s AND parent_agency_email = %s
        """, (location_id, current_user.email))

        sub = cur.fetchone()
        if not sub:
            return flask_jsonify({"error": "Location not found"}), 404

        if sub['onboarding_status'] == 'claimed':
            return flask_jsonify({"error": "User has already claimed their account"}), 400

        if not sub['agent_email']:
            return flask_jsonify({"error": "No email on file for this user"}), 400

        # Generate new token
        new_token = secrets.token_urlsafe(32)

        cur.execute("""
            UPDATE subscribers
            SET invite_token = %s,
                invite_sent_at = NOW(),
                onboarding_status = 'invited',
                updated_at = NOW()
            WHERE location_id = %s
        """, (new_token, location_id))

        conn.commit()

        # Send email
        invite_url = f"{YOUR_DOMAIN}/claim-account?token={new_token}"

        try:
            send_invite_email(
                to_email=sub['agent_email'],
                agent_name=sub['full_name'],
                agency_name=current_user.full_name or "Your Agency",
                invite_url=invite_url
            )
        except Exception as email_err:
            logger.error(f"Resend email failed: {email_err}")
            return flask_jsonify({
                "status": "partial",
                "message": "Token refreshed but email failed",
                "invite_url": invite_url
            })

        return flask_jsonify({
            "status": "success",
            "message": f"Invite re-sent to {sub['agent_email']}"
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"Resend invite error: {e}")
        return flask_jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


@app.route("/api/agency/invite-all", methods=["POST"])
@login_required
def invite_all_sub_users():
    """Invite all sub-account users who haven't been invited yet."""
    if current_user.role != 'agency_owner':
        return flask_jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Get all pending sub-accounts with emails
        cur.execute("""
            SELECT location_id, full_name, agent_email
            FROM subscribers
            WHERE parent_agency_email = %s
              AND onboarding_status = 'pending'
              AND agent_email IS NOT NULL
        """, (current_user.email,))

        pending = cur.fetchall()

        if not pending:
            return flask_jsonify({
                "status": "info",
                "message": "No pending users with emails found"
            })

        invited_count = 0
        failed_count = 0

        for sub in pending:
            try:
                # Generate token
                invite_token = secrets.token_urlsafe(32)

                # Update subscriber
                cur.execute("""
                    UPDATE subscribers
                    SET invite_token = %s,
                        invite_sent_at = NOW(),
                        onboarding_status = 'invited',
                        updated_at = NOW()
                    WHERE location_id = %s
                """, (invite_token, sub['location_id']))

                # Send email
                invite_url = f"{YOUR_DOMAIN}/claim-account?token={invite_token}"
                send_invite_email(
                    to_email=sub['agent_email'],
                    agent_name=sub['full_name'],
                    agency_name=current_user.full_name or "Your Agency",
                    invite_url=invite_url
                )
                invited_count += 1

            except Exception as e:
                logger.error(f"Failed to invite {sub['agent_email']}: {e}")
                failed_count += 1

        conn.commit()

        return flask_jsonify({
            "status": "success",
            "invited": invited_count,
            "failed": failed_count,
            "message": f"Invited {invited_count} users ({failed_count} failed)"
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"Bulk invite error: {e}")
        return flask_jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)

@app.route("/api/agency/logs/<location_id>", methods=["GET"])
@login_required
def get_agency_logs(location_id):
    """Get webhook logs for a specific sub-account location (agency owners only)."""
    if current_user.role != 'agency_owner':
        return flask_jsonify({"error": "Access denied"}), 403
    from db import get_webhook_logs
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    event_type = request.args.get("event_type", "").strip() or None
    status_filter = request.args.get("status", "").strip() or None
    logs = get_webhook_logs(location_id, limit=limit, offset=offset,
                            event_type=event_type, status=status_filter)
    for log in logs:
        if log.get("created_at"):
            log["created_at"] = log["created_at"].isoformat() + "Z"
    return safe_jsonify({"logs": logs, "total": len(logs)})

# =====================================================
# AGENCY LOGIN - FULL UNIFIED IMPLEMENTATION
# =====================================================

@app.route("/agency-login", methods=["GET", "POST"])
def agency_login():
    if current_user.is_authenticated:
        # Already logged in → redirect based on role (prevents confusion)
        if current_user.role == 'agency_owner':
            return redirect(url_for('agency_dashboard'))
        else:
            flash("You're already logged in as a standard user. Use the agent dashboard.", "info")
            return redirect(url_for('dashboard'))
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.get_from_agency(email)
        if not user:
            flash("No account found with that email.", "error")
            logger.info(f"Agency login attempt - email not found: {email}")
            return render_template("agency-login.html", form=form)
        if not check_password_hash(user.password_hash, form.password.data):
            flash("Incorrect password.", "error")
            logger.warning(f"Agency login failed - wrong password for {email}")
            return render_template("agency-login.html", form=form)
        # Role gate (core security check)
        if user.role != 'agency_owner':
            flash("Access Denied: This portal is for agency owners only. Please use the standard login.", "error")
            logger.info(f"Non-agency user attempted agency login: {email} (role: {user.role})")
            return redirect(url_for('login'))
        # Success: log in
        login_user(user, remember=form.remember.data)  # respect "Remember Me"
        logger.info(f"Agency owner logged in successfully: {email}")
        # Optional: next URL support (redirect where they came from)
        next_url = request.args.get('next')
        if next_url and '//' not in next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect(url_for('agency_dashboard'))
    return render_template("agency-login.html", form=form)

@app.route("/reviews", methods=["GET", "POST"])
def reviews():
    form = ReviewForm()

    # --- HANDLE FORM SUBMISSION ---
    if form.validate_on_submit():
        # In a real app, save to DB here.
        # For now, we simulate success.
        flash("Thank you! Your review has been submitted for approval.", "success")
        return redirect(url_for('reviews'))

    # --- MOCK DATABASE ---
    all_reviews = [
        {"name": "Sarah Jenkins", "role": "Agency Owner", "text": "This bot literally saved my business. I went from booking 2 appointments a week to 15.", "stars": 5},
        {"name": "Mike Ross", "role": "Solo Agent", "text": "It works okay, but I had some issues with the setup.", "stars": 3},
        {"name": "David K.", "role": "Life Insurance Broker", "text": "I was skeptical about the AI, but it handles objections better than my human setters.", "stars": 5},
        {"name": "Emily Chen", "role": "Marketing Director", "text": "Good tool, decent price. Not perfect though.", "stars": 4},
        {"name": "Marcus T.", "role": "Independent Agent", "text": "The integration is seamless. It feels native to Lead Connector.", "stars": 5},
        {"name": "Jason V.", "role": "Independent Agent", "text": "I've tried every bot on the market. This is the only one that understands underwriting.", "stars": 5}
    ]

    # Filter: Show only 5-star reviews
    visible_reviews = [r for r in all_reviews if r['stars'] == 5]
    return render_template('reviews.html', reviews=visible_reviews, form=form)

@app.route("/website-bot-webhook", methods=["POST"])
def website_bot_webhook():
    """
    Smart routing chat - qualifies visitors, answers questions, routes to action.
    No AI needed, instant responses, actually sells.
    """
    payload = request.get_json(silent=True) or {}
    user_message = payload.get('message', '').strip()

    if not user_message:
        return flask_jsonify({"status": "error"}), 400

    msg_lower = user_message.lower()

    # =====================================================
    # INIT & QUALIFICATION
    # =====================================================

    if user_message == "INIT_CHAT":
        return flask_jsonify({
            "text": "Hey! I'm actually the product you're looking at right now. Quick question - are you a solo agent or do you run an agency?",
            "options": [
                {"label": "Solo Agent", "value": "individual"},
                {"label": "Agency Owner", "value": "agency"}
            ]
        })

    # =====================================================
    # INDIVIDUAL PATH
    # =====================================================

    if user_message == "individual":
        return flask_jsonify({
            "text": "Nice. So right now you're manually following up with leads, right? Or maybe you've got some basic automation that sounds like a robot?",
            "options": [
                {"label": "Yeah, manual follow-up", "value": "individual_manual"},
                {"label": "I have automation but it sucks", "value": "individual_bad_auto"},
                {"label": "Just curious what this is", "value": "individual_curious"}
            ]
        })

    if user_message == "individual_manual":
        return flask_jsonify({
            "text": "That's where most leads die. You get busy, forget to follow up, and that lead who was warm 3 days ago is now cold. I fix that. I respond instantly - even at 2am - and I actually sound human. Want to see how I handle a cold lead?",
            "options": [
                {"label": "Show me", "value": "demo"},
                {"label": "What does it cost?", "value": "pricing_individual"}
            ]
        })

    if user_message == "individual_bad_auto":
        return flask_jsonify({
            "text": "Let me guess - keyword triggers, canned responses, and leads can tell it's a bot within 2 messages? I'm different. I use 5 actual sales methodologies - NEPQ, Gap Selling, Chris Voss tactics. I handle objections, remember everything about the lead, and book appointments on your calendar. Want to see?",
            "options": [
                {"label": "Try the demo", "value": "demo"},
                {"label": "What's it cost?", "value": "pricing_individual"}
            ]
        })

    if user_message == "individual_curious":
        return flask_jsonify({
            "text": "Short version: I'm an AI that responds to your insurance leads via SMS. But I'm not a dumb chatbot - I use real sales frameworks, remember the entire conversation history, handle objections like a human setter, and book appointments directly on your calendar. All while you sleep.",
            "options": [
                {"label": "See it in action", "value": "demo"},
                {"label": "How is this different?", "value": "comparison"},
                {"label": "Pricing", "value": "pricing_individual"}
            ]
        })

    # =====================================================
    # AGENCY PATH
    # =====================================================

    if user_message == "agency":
        return flask_jsonify({
            "text": "Nice. How many agents do you have under you right now?",
            "options": [
                {"label": "Under 10", "value": "agency_small"},
                {"label": "10-50", "value": "agency_medium"},
                {"label": "50+", "value": "agency_large"}
            ]
        })

    if user_message == "agency_small":
        return flask_jsonify({
            "text": "Perfect size to start. Here's what I solve for you: inconsistent follow-up across your team. Some agents are great, some let leads rot. With me, every sub-account gets the same AI setter - same brain, same methodology, but books to THEIR calendar. You get a dashboard to see everything. $797.99/month covers up to 14 agents.",
            "options": [
                {"label": "How does that work exactly?", "value": "agency_how"},
                {"label": "Show me the demo", "value": "demo"},
                {"label": "What's included?", "value": "agency_features"}
            ]
        })

    if user_message in ["agency_medium", "agency_large"]:
        return flask_jsonify({
            "text": "At your scale, lead leakage is probably costing you six figures a year. Here's what I do: every single sub-account gets an AI setter. Same training, same methodology, same quality - but each one books to that agent's calendar. One dashboard for you to monitor everything. Unlimited sub-accounts for $1,597.99/month flat.",
            "options": [
                {"label": "How does multi-tenant work?", "value": "agency_how"},
                {"label": "Show me the demo", "value": "demo"},
                {"label": "What makes this different?", "value": "comparison"}
            ]
        })

    if user_message == "agency_how":
        return flask_jsonify({
            "text": "Simple: You connect your Lead Connector agency account. I automatically see all your sub-accounts. Each one gets their own instance of me - same sales brain, but configured for their calendar and timezone. When a lead texts into Location A, I respond as Location A's setter and book on their calendar. You see all conversations from one dashboard. Your agents don't need to do anything.",
            "options": [
                {"label": "What do my agents see?", "value": "agency_agent_view"},
                {"label": "Try the demo", "value": "demo"},
                {"label": "Pricing", "value": "pricing_agency"}
            ]
        })

    if user_message == "agency_agent_view":
        return flask_jsonify({
            "text": "Your agents see conversations happening in their Lead Connector inbox like normal. They can jump in anytime if needed. But mostly they just see appointments showing up on their calendar with qualified leads. The AI does the grunt work, they do the closing.",
            "options": [
                {"label": "That sounds good", "value": "demo"},
                {"label": "What's pricing?", "value": "pricing_agency"}
            ]
        })

    if user_message == "agency_features":
        return flask_jsonify({
            "text": "Agency Starter ($797.99/mo) includes: Up to 14 sub-accounts, multi-tenant dashboard, shared memory across your agency, priority support, all 5 sales methodologies, auto-booking to each agent's calendar, and underwriting pre-qualification. 7-day free trial.",
            "options": [
                {"label": "Start free trial", "value": "signup_agency_starter"},
                {"label": "See it work first", "value": "demo"},
                {"label": "What if I have more than 10?", "value": "agency_pro_info"}
            ]
        })

    if user_message == "agency_pro_info":
        return flask_jsonify({
            "text": "Agency Pro is $1,597.99/month for unlimited sub-accounts. Same features plus dedicated high-speed queue (faster responses) and white-glove onboarding. No cap on agents - scale as big as you want, price stays the same.",
            "options": [
                {"label": "Get started", "value": "signup_agency_pro"},
                {"label": "Try demo first", "value": "demo"}
            ]
        })

    # =====================================================
    # FEATURES & COMPARISON
    # =====================================================

    if user_message == "comparison" or "different" in msg_lower or "vs" in msg_lower or "compare" in msg_lower:
        return flask_jsonify({
            "text": "Most bots use keyword matching - they're dumb. I use 5 real sales frameworks: NEPQ for emotional gaps, Chris Voss tactics for objections, Gap Selling to create urgency, plus Straight Line and Zig Ziglar methods. I also have persistent memory - I remember everything about every lead forever. And I understand underwriting, so I pre-qualify before the call.",
            "redirect": "/comparison"
        })

    if "memory" in msg_lower or "remember" in msg_lower:
        return flask_jsonify({
            "text": "I remember everything. If a lead mentioned their wife's name 3 months ago, I still know it. If they said they had diabetes, I factor that into underwriting. No awkward 'what was your name again?' moments. This is why I can re-engage cold leads that other bots can't.",
            "options": [
                {"label": "See it in action", "value": "demo"},
                {"label": "What else is different?", "value": "comparison"}
            ]
        })

    if "underwriting" in msg_lower or "pre-qualify" in msg_lower or "health" in msg_lower:
        return flask_jsonify({
            "text": "I ask the right health questions before they ever get on your calendar. Diabetes? Heart issues? Smoker? I know what carriers need and I gather that info naturally in conversation. You get on calls with qualified leads, not people who can't get approved.",
            "options": [
                {"label": "Show me how", "value": "demo"},
                {"label": "Pricing", "value": "pricing_individual"}
            ]
        })

    if "methodology" in msg_lower or "framework" in msg_lower or "nepq" in msg_lower or "sales" in msg_lower:
        return flask_jsonify({
            "text": "I blend 5 proven frameworks: NEPQ (emotional gap questions), Gap Selling (current state vs future state), Chris Voss (labeling, no-oriented questions), Straight Line (always advancing), and Zig Ziglar (help first, objections = requests for clarity). This isn't scripted - I adapt to each conversation.",
            "options": [
                {"label": "See it handle objections", "value": "demo"},
                {"label": "Pricing", "value": "pricing_individual"}
            ]
        })

    if "book" in msg_lower or "calendar" in msg_lower or "appointment" in msg_lower:
        return flask_jsonify({
            "text": "I connect directly to your Lead Connector calendar. When a lead is ready, I show them available slots and book it - no links to click, no friction. The appointment shows up on your calendar with all the context: what they said, their health info, what objections came up. You walk into the call prepared.",
            "options": [
                {"label": "Try the demo", "value": "demo"},
                {"label": "Pricing", "value": "pricing_individual"}
            ]
        })

    # =====================================================
    # PRICING
    # =====================================================

    if user_message == "pricing_individual" or (("price" in msg_lower or "cost" in msg_lower or "how much" in msg_lower) and "agency" not in msg_lower):
        return flask_jsonify({
            "text": "$98.99/month. Unlimited conversations, full memory, all 5 sales methodologies, calendar auto-booking, underwriting logic. 7-day free trial to make sure it works for you.",
            "options": [
                {"label": "Start free trial", "value": "signup_individual"},
                {"label": "See it first", "value": "demo"}
            ]
        })

    if user_message == "pricing_agency" or ("price" in msg_lower and "agency" in msg_lower):
        return flask_jsonify({
            "text": "Two options: Agency Starter is $797.99/month for up to 14 sub-accounts. Agency Pro is $1,597.99/month for 15+ sub-accounts (unlimited). Both include the full multi-tenant dashboard and all features. 7-day trial on Starter.",
            "options": [
                {"label": "Agency Starter ($797.99)", "value": "signup_agency_starter"},
                {"label": "Agency Pro ($1,597.99)", "value": "signup_agency_pro"},
                {"label": "See demo first", "value": "demo"}
            ]
        })

    # =====================================================
    # SIGNUP ROUTES
    # =====================================================

    if user_message == "demo":
        return flask_jsonify({
            "text": "Let's do it. I'll show you exactly how I talk to a cold insurance lead.",
            "redirect": "/demo-chat"
        })

    if user_message == "signup_individual":
        return flask_jsonify({
            "text": "Let's get you set up. 7-day free trial, cancel anytime.",
            "redirect": "/checkout"
        })

    if user_message == "signup_agency_starter":
        return flask_jsonify({
            "text": "Good choice. 7-day free trial for up to 14 sub-accounts.",
            "redirect": "/checkout/agency-starter"
        })

    if user_message == "signup_agency_pro":
        return flask_jsonify({
            "text": "Let's scale. Unlimited sub-accounts, one flat price.",
            "redirect": "/checkout/agency-pro"
        })

    # =====================================================
    # FAQ / OBJECTION HANDLING
    # =====================================================

    if "trial" in msg_lower or "free" in msg_lower:
        return flask_jsonify({
            "text": "7-day free trial on Individual and Agency Starter plans. Full access, no card required to try the demo. Cancel anytime during trial.",
            "options": [
                {"label": "Start trial", "value": "signup_individual"},
                {"label": "Try demo first", "value": "demo"}
            ]
        })

    if "ghl" in msg_lower or "gohighlevel" in msg_lower or "highlevel" in msg_lower or "crm" in msg_lower or "lead connector" in msg_lower:
        return flask_jsonify({
            "text": "I integrate directly with Lead Connector. You connect via OAuth (one click), and I automatically see your contacts, calendars, and conversations. Works with any plan - agency or location level.",
            "options": [
                {"label": "See integration", "value": "demo"},
                {"label": "Get started", "value": "signup_individual"}
            ]
        })

    if "support" in msg_lower or "help" in msg_lower or "setup" in msg_lower:
        return flask_jsonify({
            "text": "Setup takes about 5 minutes - connect Lead Connector, configure your calendar, done. All plans include support. Agency Pro includes white-glove onboarding where we set everything up for you.",
            "options": [
                {"label": "Start setup", "value": "signup_individual"},
                {"label": "Questions first", "value": "contact"}
            ]
        })

    if user_message == "contact" or "contact" in msg_lower or "talk to" in msg_lower or "human" in msg_lower:
        return flask_jsonify({
            "text": "Want to talk to the team?",
            "redirect": "/contact"
        })

    # =====================================================
    # FALLBACK
    # =====================================================

    return flask_jsonify({
        "text": "Best way to understand what I do is to see it. I'll show you how I handle a real cold insurance lead.",
        "options": [
            {"label": "Show me", "value": "demo"},
            {"label": "Just tell me pricing", "value": "pricing_individual"}
        ]
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)