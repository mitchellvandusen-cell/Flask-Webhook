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
from flask import Flask, render_template, render_template_string, request, redirect, url_for, flash, session, make_response
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
                save_persistent_alert, get_persistent_alerts, dismiss_persistent_alert)
from sync_subscribers import sync_subscribers
from reply_sanitizer import sanitize_reply
from llm_caller import generate_clean_reply

# === ADMIN WHITELIST (Free Access - No Subscription Required) ===
ADMIN_EMAILS = [
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
                SELECT email, parent_agency_email, invite_token, onboarding_status
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
            if match['parent_agency_email'] and match['invite_token']:
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

        if role in ['agency_owner', 'admin']:
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
    # 1. Security Check
    if current_user.role != 'agency_owner':
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
        return render_template('agency_dashboard.html',
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
                email,              -- Owner email (for billing/parent link)
                agent_email,        -- Individual agent's email
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
                'agent_email': sub['agent_email'] or 'No Agent Email',
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
    return render_template('agency_dashboard.html',
                           form=form,
                           access_token_display=access_token_display,
                           refresh_token_display=refresh_token_display,
                           token_readonly=token_field_state,
                           expires_in_str=expires_in_str,
                           sub=current_user,
                           profile=profile,
                           sub_accounts=sub_accounts,
                           stats=agency_stats,
                           user=current_user)
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

    from crm_adapters.factory import CRM_CONFIG_FIELDS, CRM_DISPLAY_NAMES
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
        crm_config_fields=CRM_CONFIG_FIELDS,
        crm_display_names=CRM_DISPLAY_NAMES
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

    client_id = os.getenv("GHL_CLIENT_ID")
    domain = os.getenv("YOUR_DOMAIN")
    if not client_id or not domain:
        logger.error(f"OAuth initiate failed: GHL_CLIENT_ID={'set' if client_id else 'MISSING'}, YOUR_DOMAIN={'set' if domain else 'MISSING'}")
        flash("OAuth is not configured. Please contact support.", "error")
        return redirect(url_for('dashboard'))
    redirect_uri = f"{domain}/oauth/callback"

    # Required scopes — must match marketplace app configuration in GHL developer portal.
    # If you add/remove scopes here, update the marketplace app settings too.
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
        # TODO: Add these scopes once GHL marketplace approval completes (~10 days):
        #   "locations.readonly"  — enables /locations/ API for sub-account discovery
        #   "users.readonly"      — enables /users/ API for user info lookup
        # Without locations.readonly, the callback falls back to the primary locationId from the token.
    ]
    scope_string = " ".join(scopes)

    # State identifies this as a website subscriber (already paid via Stripe)
    # vs a marketplace install (hasn't subscribed yet)
    state = "website_user"

    # Build OAuth URL using public marketplace app
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

    logger.info(f"Initiating marketplace OAuth flow for {current_user.email}. Redirecting to: {oauth_url}")
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

    return None, last_err


@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")

    logger.info(f"OAuth callback hit: state={state}, code={'present' if code else 'MISSING'}")

    if not code:
        logger.warning("OAuth callback: No authorization code in request params")
        flash("No authorization code received.", "danger")
        return redirect(url_for('home'))

    try:
        # Determine flow based on state parameter
        # state="website_user" → Stripe/website subscriber connecting Lead Connector
        # No state (or anything else) → Marketplace installation
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
        client_id = os.getenv("GHL_CLIENT_ID")
        client_secret = os.getenv("GHL_CLIENT_SECRET")
        domain = os.getenv("YOUR_DOMAIN")

        if not client_id or not client_secret or not domain:
            logger.error(f"OAuth env vars missing: GHL_CLIENT_ID={'set' if client_id else 'MISSING'}, "
                        f"GHL_CLIENT_SECRET={'set' if client_secret else 'MISSING'}, "
                        f"YOUR_DOMAIN={'set' if domain else 'MISSING'}")
            flash("OAuth is not configured. Please contact support.", "danger")
            return redirect(url_for('home'))

        # 1. Exchange Code for Token (with retry on transient failures)
        token_url = "https://services.leadconnectorhq.com/oauth/token"
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "user_type": "Location",
            "redirect_uri": f"{domain}/oauth/callback"
        }

        token_resp, token_err = _ghl_api_call('POST', token_url, data=payload,
                                               timeout=15, label="Token exchange")
        if token_resp is None:
            logger.error(f"Token exchange failed after retries: {token_err}")
            flash("Failed to connect to Lead Connector. Please try again.", "danger")
            return redirect(url_for('home'))

        if not token_resp.ok:
            logger.error(f"Token exchange rejected: {token_resp.status_code} {token_resp.text[:500]}")
            if token_resp.status_code == 400:
                flash("Authorization code expired or invalid. Please try connecting again.", "danger")
            else:
                flash("Failed to exchange authorization code. Please try again.", "danger")
            return redirect(url_for('home'))

        try:
            token_data = token_resp.json()
        except ValueError:
            logger.error(f"Token exchange returned non-JSON: {token_resp.text[:500]}")
            flash("Unexpected response from Lead Connector. Please try again.", "danger")
            return redirect(url_for('home'))

        access_token = token_data.get('access_token')
        if not access_token:
            logger.error(f"Token exchange missing access_token: {json.dumps(token_data)[:500]}")
            flash("Authorization failed — no access token received. Please try again.", "danger")
            return redirect(url_for('home'))

        primary_location_id = token_data.get('locationId')
        refresh_token = token_data.get('refresh_token')
        expires_in = token_data.get('expires_in', 86400)

        logger.info(f"Step 1 complete: Token exchange OK. locationId={primary_location_id}, expires_in={expires_in}")

        headers = {'Authorization': f'Bearer {access_token}', 'Version': '2021-07-28'}

        # 2. Get user info (with retry)
        me_resp, me_err = _ghl_api_call('GET', "https://services.leadconnectorhq.com/users/me",
                                         headers=headers, timeout=10, label="/users/me")
        me_data = {}
        user_email = None
        user_name = 'Agency Admin'

        if me_resp and me_resp.ok:
            try:
                me_data = me_resp.json()
                user_email = me_data.get('email')
                user_name = me_data.get('name', 'Agency Admin')
            except ValueError:
                logger.error(f"/users/me returned non-JSON: {me_resp.text[:300]}")
        elif me_resp:
            logger.error(f"/users/me failed: {me_resp.status_code} {me_resp.text[:300]}")
            if me_resp.status_code in (401, 403):
                logger.error("SCOPE ISSUE: /users/me returned 401/403 — token may lack required scopes")
        else:
            logger.error(f"/users/me unreachable: {me_err}")

        if not user_email:
            logger.error(f"Could not retrieve user email. me_data={json.dumps(me_data)[:300]}")
            flash("Could not retrieve your email from Lead Connector. Please try again.", "danger")
            return redirect(url_for('home'))

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
        sub_accounts = fetch_all_ghl_items(
            "https://services.leadconnectorhq.com/locations/",
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

                # Determine OAuth app type
                app_type = 'private' if is_website_user else 'marketplace'

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
                        full_name = EXCLUDED.full_name,
                        subscription_tier = EXCLUDED.subscription_tier,
                        max_seats = EXCLUDED.max_seats,
                        active_seats = EXCLUDED.active_seats,
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        token_expires_at = EXCLUDED.token_expires_at,
                        timezone = EXCLUDED.timezone,
                        crm_user_id = EXCLUDED.crm_user_id,
                        oauth_app_type = EXCLUDED.oauth_app_type,
                        updated_at = NOW()
                """, (
                    user_email, primary_location_id, primary_name, plan_tier,
                    max_seats, active_seats, access_token, refresh_token,
                    expires_in, primary_timezone or 'America/Chicago', me_data.get('id'), app_type
                ))

            # --- B. Sub-accounts (or individual user) ---
            # 1. Pagination: Using fetch_all_ghl_items() to get ALL locations (not just first 20)
            # 2. Token Sharing: All sub-accounts get the agency token (prevents token starvation)
            # 3. No N+1 Queries: Removed user fetch loop to prevent HTTP 504 timeouts
            #
            # FIX: For Stripe subscribers connecting OAuth, a row already exists with
            # a temp location_id. We must update that row first so the INSERT below
            # doesn't violate the UNIQUE constraint on email.
            if is_website_user and primary_location_id:
                cur.execute("""
                    UPDATE subscribers
                    SET location_id = %s,
                        access_token = %s,
                        refresh_token = %s,
                        token_expires_at = NOW() + interval '%s seconds',
                        crm_user_id = COALESCE(%s, crm_user_id),
                        oauth_app_type = 'private',
                        onboarding_status = 'claimed',
                        updated_at = NOW()
                    WHERE email = %s AND location_id LIKE 'temp_%%'
                """, (primary_location_id, access_token, refresh_token,
                      expires_in, me_data.get('id'), user_email))
                rows_updated = cur.rowcount
                if rows_updated > 0:
                    logger.info(f"Updated temp row for {user_email} with real location_id {primary_location_id}")
                else:
                    logger.warning(f"No temp_ row found for {user_email} — may already be claimed or email mismatch")

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

            logger.info(f"Step 7c: Provisioning {len(locations_to_provision)} subscriber rows "
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

                role = 'agency_sub_account_user' if use_agency_flow else 'individual'
                parent_agency_email = user_email if use_agency_flow else None
                email_this = user_email
                app_type = 'private' if is_website_user else 'marketplace'

                cur.execute("""
                    INSERT INTO subscribers (
                        location_id, email, agent_email, full_name, role, subscription_tier,
                        parent_agency_email, access_token, refresh_token,
                        token_expires_at, timezone, crm_user_id,
                        onboarding_status, oauth_app_type, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW() + interval '%s seconds',
                        %s, %s, %s, %s, NOW(), NOW()
                    )
                    ON CONFLICT (location_id) DO UPDATE SET
                        email = EXCLUDED.email,
                        agent_email = CASE
                            WHEN subscribers.agent_email IS NULL THEN EXCLUDED.agent_email
                            ELSE subscribers.agent_email
                        END,
                        full_name = EXCLUDED.full_name,
                        role = EXCLUDED.role,
                        subscription_tier = EXCLUDED.subscription_tier,
                        parent_agency_email = EXCLUDED.parent_agency_email,
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        token_expires_at = EXCLUDED.token_expires_at,
                        timezone = EXCLUDED.timezone,
                        crm_user_id = COALESCE(EXCLUDED.crm_user_id, subscribers.crm_user_id),
                        oauth_app_type = EXCLUDED.oauth_app_type,
                        updated_at = NOW()
                """, (
                    sub_id, email_this, agent_email, agent_name, role, plan_tier,
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

        # 8b. Persistent alerts for scope/location issues
        if using_location_fallback:
            try:
                alert_msg = (
                    "Your Lead Connector account connected successfully, but the "
                    "locations.readonly scope is not yet approved in the GHL marketplace. "
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

        # 8c. Welcome email on install
        try:
            from send_email_api import send_email_via_api
            domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
            welcome_html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333;">
                <div style="text-align: center; margin-bottom: 30px;">
                    <h1 style="color: #00c853; margin-bottom: 5px;">Welcome to InsuranceGrokBot</h1>
                    <p style="color: #888; font-size: 14px;">Your AI-powered insurance sales assistant</p>
                </div>

                <p>Hi {user_name},</p>

                <p>Your Lead Connector account has been successfully connected. Here is everything you need to get started:</p>

                <div style="background: #f8f9fa; border-radius: 12px; padding: 20px; margin: 20px 0;">
                    <p style="margin: 8px 0;"><strong>Subscribe and activate your bot:</strong><br>
                    <a href="{domain_url}" style="color: #00c853;">{domain_url}</a></p>

                    <p style="margin: 8px 0;"><strong>Questions or FAQ:</strong><br>
                    <a href="{domain_url}/support" style="color: #00c853;">{domain_url}/support</a></p>

                    <p style="margin: 8px 0;"><strong>Dashboard, CRM integration, and setup guide:</strong><br>
                    <a href="{domain_url}/dashboard" style="color: #00c853;">{domain_url}/dashboard</a></p>

                    <p style="margin: 8px 0;"><strong>Onboarding status:</strong><br>
                    <a href="{domain_url}/onboarding-status" style="color: #00c853;">{domain_url}/onboarding-status</a></p>
                </div>

                <p>If you have any questions about navigating your dashboard, integrating your CRM, or anything else, visit our support page or reply to this email.</p>

                <p style="margin-top: 30px; color: #888; font-size: 13px;">
                    — The InsuranceGrokBot Team
                </p>
            </body>
            </html>
            """
            email_sent = send_email_via_api(
                to_email=user_email,
                subject="Welcome to InsuranceGrokBot",
                html_body=welcome_html,
                text_body=f"Welcome to InsuranceGrokBot, {user_name}! "
                          f"Subscribe: {domain_url} | Dashboard: {domain_url}/dashboard | "
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
            flash("Account created but login failed. Please log in manually.", "warning")
            return redirect(url_for('login'))

        # MARKETPLACE INSTALLATION: Route to home page so user can subscribe
        if not is_website_user:
            logger.info(f"Marketplace install complete for {user_email} — routing to home for subscription")
            flash("App installed! Please subscribe to activate your bot.", "success")
            return redirect("/")

        # PRIVATE APP FLOW: Route through OAuth loading screen
        logger.info(f"Private app OAuth complete for {user_email} — routing to loading screen")
        return redirect("/oauth/loading")

    except requests.RequestException as e:
        logger.error(f"OAuth network error: {e}", exc_info=True)
        flash("Failed to connect to Lead Connector. Please try again.", "danger")
        return redirect(url_for('home'))
    except Exception as e:
        logger.error(f"Critical OAuth failure: {e}", exc_info=True)
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