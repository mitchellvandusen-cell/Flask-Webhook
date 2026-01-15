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
from flask_wtf import FlaskForm
from flask import jsonify as flask_jsonify
from wtforms import StringField, PasswordField, SubmitField, TextAreaField, SelectField
from wtforms.validators import DataRequired, Email, EqualTo
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from rq import Queue
from psycopg2.extras import RealDictCursor

# === IMPORTS ===
from db import get_subscriber_info_hybrid, get_db_connection, init_db, User
from sync_subscribers import sync_subscribers
# CRITICAL IMPORT: This connects main.py to the logic in tasks.py
from tasks import process_webhook_task  
from memory import get_known_facts, get_narrative, get_recent_messages 
from individual_profile import build_comprehensive_profile 
from utils import make_json_serializable, clean_ai_reply
from prompt import CORE_UNIFIED_MINDSET, DEMO_OPENER_ADDITIONAL_INSTRUCTIONS
from website_chat_logic import process_async_chat_task
load_dotenv()

app = Flask(__name__)
# Logging - structured for production
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def safe_jsonify(data):
    # Pass 'data' into the function so it can process the dictionary/list
    return flask_jsonify(make_json_serializable(data))

# === REDIS & RQ SETUP ===
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
try:
    conn = redis.from_url(redis_url)
    
    # Create TWO queues
    q_production = Queue('production', connection=conn) # High Priority
    q_demo       = Queue('demo',       connection=conn) # Low Priority
    
    logger.info("✅ Redis Connection Successful")
except Exception as e:
    logger.error(f"❌ Redis Connection Failed: {e}")

# === INITIALIZATION ===
sync_subscribers()
init_db() 

# == SECRET SESSION ==
app.secret_key = os.getenv("SESSION_SECRET", "fallback-insecure-key")

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
    location_id = StringField("Your GoHighLevel Location ID", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    confirm = PasswordField("Confirm Password", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("Create Account")

class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Login")

class ConfigForm(FlaskForm):
    location_id = StringField("Location ID", validators=[DataRequired()])
    crm_api_key = StringField("CRM API Key", validators=[DataRequired()])
    crm_user_id = StringField("CRM USER ID", validators=[DataRequired()])
    calendar_id = StringField("Calendar ID", validators=[DataRequired()])
    timezone = StringField("Timezone (e.g. America/Chicago)", validators=[DataRequired()])
    bot_name = StringField("Bot First Name", validators=[DataRequired()])
    initial_message = StringField("Optional Initial Message")
    submit = SubmitField("Save Settings")

class ReviewForm(FlaskForm):
    name = StringField("Full Name", validators=[DataRequired()])
    role = StringField("Job Title", validators=[DataRequired()])
    text = TextAreaField("Your Experience", validators=[DataRequired()])
    stars = SelectField("Rating", choices=[('5', '5 Stars'), ('4', '4 Stars'), ('3', '3 Stars'), ('2', '2 Stars'), ('1', '1 Star')], validators=[DataRequired()])
    submit = SubmitField("Submit Review")

@app.route("/website-bot-webhook", methods=["POST"])
def website_bot_webhook():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    contact_id = payload.get('contact_id')
    user_message = payload.get('message')

    if not contact_id or not user_message:
        return flask_jsonify({"status": "error"}), 400

    redis_key = f"chat_logs:{contact_id}"
    user_type_key = f"user_type:{contact_id}"
    
    # --- A: FAST LOGIC (Instant Reply) ---
    if user_message == "INIT_CHAT":
        welcome_msg = "Hello! I'm the InsuranceGrokBot assistant. To customize my answers, are you an Individual Agent or an Agency Owner?"
        options = [
            {"label": "Individual Agent", "value": "individual"},
            {"label": "Agency Owner", "value": "agency"}
        ]
        log = {"role": "assistant", "type": "Bot Message", "content": welcome_msg, "timestamp": datetime.utcnow().isoformat()}
        if conn: conn.rpush(redis_key, json.dumps(log))
        return flask_jsonify({"text": welcome_msg, "options": options})

    if user_message in ["individual", "agency"]:
        if conn: conn.set(user_type_key, user_message)
        reply = "Understood. " + ("I'll focus on scaling teams." if user_message == "agency" else "I'll focus on personal automation.")
        log = {"role": "assistant", "type": "Bot Message", "content": reply, "timestamp": datetime.utcnow().isoformat()}
        if conn: conn.rpush(redis_key, json.dumps(log))
        return flask_jsonify({"text": reply})

    msg_lower = user_message.lower()
    redirect_map = {
        "price": "/#pricing", "cost": "/#pricing", "plan": "/#pricing",
        "compare": "/comparison", "vs": "/comparison",
        "faq": "/faq", "help": "/faq", "get started": "/getting-started"
    }
    for key, url in redirect_map.items():
        if key in msg_lower:
            reply = f"I can help with that. Let me take you to the {key} section."
            log = {"role": "assistant", "type": "Bot Message", "content": reply, "timestamp": datetime.utcnow().isoformat()}
            if conn: conn.rpush(redis_key, json.dumps(log))
            return flask_jsonify({"text": reply, "redirect": url})

    # --- B: ASYNC LOGIC (Demo Worker) ---
    user_log = {"role": "lead", "type": "User Message", "content": user_message, "timestamp": datetime.utcnow().isoformat()}
    if conn:
        conn.rpush(redis_key, json.dumps(user_log))
        conn.expire(redis_key, 86400)

    try:
        job = q_demo.enqueue(
            process_async_chat_task,
            {"contact_id": contact_id, "message": user_message},
            job_timeout=60,
            result_ttl=600
        )
        return flask_jsonify({"status": "processing", "job_id": job.id}), 202
    except Exception as e:
        logger.error(f"Queue Error: {e}")
        return flask_jsonify({"text": "System overload. Please try again."}), 500
    

@app.route('/api/demo/reset', methods=['POST'])
def demo_reset():
    # Call the bold function we just built
    opener = generate_demo_opener()
    return flask_jsonify({"message": opener})

def generate_demo_opener():
    if not client:
        return "Quick question are you still with that life insurance plan you mentioned before? There's some new living benefits people have been asking me about and I wanted to make sure yours doesnt just pay out when you're dead."
    try:
        system_content = (
            CORE_UNIFIED_MINDSET.format(bot_first_name="DEMOGROKBOT")
            + "\n\n"
            + DEMO_OPENER_ADDITIONAL_INSTRUCTIONS
        )
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": "Generate unique opener."}
            ],
            temperature=0.8,
            max_tokens=130
        )

        # 1. Get raw text & basic cleanup (strip whitespace, remove quotes)
        raw_text = response.choices[0].message.content.strip().replace('"', '')
        
        # 2. Run your specific cleaner
        cleaned_content = clean_ai_reply(raw_text)
        
        return cleaned_content
    except Exception as e:
        logger.error(f"Demo opener failed: {e}")
        return "Quick question are you still with that life insurance plan you mentioned before? There's some new living benefits people have been asking me about and I wanted to make sure yours doesnt just pay out when you're dead."
# =====================================================
#  THE ASYNC WEBHOOK ENDPOINT
# =====================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    if not q_production or not q_demo:
        logger.critical("Redis/RQ unavailable")
        return flask_jsonify({"status": "error"}), 503

    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    location_id = payload.get("location_id") or payload.get("location", {}).get("id")
    contact_id = payload.get("contact_id")
    message_body = payload.get("message", {}).get("body") or payload.get("message")

    # 1. DEMO SPEED OPTIMIZATION: Write User Msg Immediately
    # This ensures the UI updates instantly when they hit send.
    if location_id in ['DEMO_LOC', 'DEMO'] and contact_id and message_body:
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
                cur.close()
                conn.close()
        except Exception as e:
            logger.error(f"Instant demo write failed: {e}")


#   2. Enqueue the Brain
    try:
        # CHECK IF DEMO
        is_demo = location_id in ['DEMO_LOC', 'DEMO', 'TEST_LOCATION_456']
        
        # Select the appropriate queue
        target_queue = q_demo if is_demo else q_production
        
        job = target_queue.enqueue(
            process_webhook_task,
            payload,
            job_timeout=120,
            result_ttl=86400
        )
        return safe_jsonify({"status": "queued", "job_id": job.id, "queue": target_queue.name}), 202
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

@app.route("/getting-started")
def getting_started():
    return render_template('getting-started.html')

import uuid # Make sure this is imported at top of file

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

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
                            location_id, email, stripe_customer_id, role, subscription_tier
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (email) DO UPDATE SET
                            stripe_customer_id = EXCLUDED.stripe_customer_id,
                            role = EXCLUDED.role,
                            subscription_tier = EXCLUDED.subscription_tier;
                    """, (temp_id, email, customer_id, target_role, target_tier))
                    
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
                    conn.close()

    return '', 200
@app.route("/register", methods=["GET", "POST"])
def register():
    form = RegisterForm()  # ← Make sure RegisterForm uses location_id, not code (see below)

    # Pre-fill from OAuth redirect
    if request.method == "GET":
        url_location_id = request.args.get('location_id')
        if url_location_id:
            form.location_id.data = url_location_id
            flash("GoHighLevel connected! Your location ID is pre-filled. Set a password to finish.", "success")

    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        submitted_location_id = form.location_id.data.strip()

        # 1. Check if email already exists → redirect to login
        existing_user = User.get(email)
        if existing_user:
            flash("Email already registered. Please log in.", "info")
            return redirect(url_for("login"))

        # 2. Verify the location_id exists in subscribers (proof from OAuth)
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT email FROM subscribers 
                    WHERE location_id = %s
                    LIMIT 1
                """, (submitted_location_id,))
                match = cur.fetchone()
                
                if not match:
                    flash("Invalid or unverified location ID. Please reconnect via GoHighLevel or contact support.", "error")
                    return redirect("/register")
                
                # Optional: Check if email matches (extra security)
                if match[0] != email:
                    flash("Location ID does not match your email. Please reconnect.", "error")
                    return redirect("/register")
                
            except Exception as e:
                logger.error(f"Location ID verification failed: {e}")
                flash("System error during verification. Please try again.", "error")
                return redirect("/register")
            finally:
                cur.close()
                conn.close()
        else:
            flash("Database unavailable. Please try again later.", "error")
            return redirect("/register")

        # 3. All checks passed → create account
        password_hash = generate_password_hash(form.password.data)
        if User.create(email, password_hash):
            # Optional: Update subscribers with confirmed status or link
            flash("Account created successfully! Welcome aboard.", "success")
            return redirect(url_for("login"))
        else:
            flash("Account creation failed. Please try again or contact support.", "error")

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
       
        if not check_password_hash(user.password_hash, form.password.data):
            print("[LOGIN DEBUG] Incorrect password")
            flash("Incorrect password.", "error")
            return render_template("login.html", form=form)
       
        # Password correct → log in
        print("[LOGIN DEBUG] Login successful - role:", user.role)
        login_user(user)
       
        # Normalize role checks
        role = (user.role or 'individual').lower()
       
        if role in ['individual', 'individual_user', 'user', 'agency_sub_account_user']:
            return redirect(url_for("dashboard"))
        elif role in ['agency_owner', 'admin']:
            return redirect(url_for("agency_dashboard"))
        else:
            flash("Your account role is not configured correctly. Contact support.", "warning")
            return redirect(url_for("dashboard"))
    return render_template("login.html", form=form)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/")

@app.route("/agency-dashboard", methods=["GET", "POST"])
@login_required
def agency_dashboard():
    # 1. Security Check
    if current_user.role != 'agency_owner':
        flash("Access restricted to agency owners only.", "error")
        return redirect("/dashboard")
    conn = get_db_connection()
    if not conn:
        flash("System error: Database unavailable.", "error")
        return redirect("/dashboard")
    form = ConfigForm()
    # --- 1. HANDLE SAVING CONFIG (POST) ---
    if form.validate_on_submit():
        if not conn:
            flash("Database connection failed", "error")
        else:
            try:
                cur = conn.cursor()
                # Update the AGENCY_BILLING table for owner config
                cur.execute("""
                    UPDATE agency_billing
                    SET location_id = %s,
                        calendar_id = %s,
                        crm_user_id = %s,
                        bot_first_name = %s,
                        timezone = %s,
                        initial_message = %s,
                        updated_at = NOW()
                    WHERE agency_email = %s
                """, (
                    form.location_id.data,
                    form.calendar_id.data,
                    form.crm_user_id.data,
                    form.bot_name.data,
                    form.timezone.data,
                    form.initial_message.data,
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
                email,              -- Agent Email
                bot_first_name,
                timezone,
                access_token,       -- Used to check connection status
                subscription_tier,
                token_expires_at,
                created_at,
                refresh_token       -- Added for display
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
                'status': 'Active' if is_connected else 'Pending Auth',
                'status_class': 'success' if is_connected else 'warning',
                'tier': sub['subscription_tier'].replace('_', ' ').title(),
                'bot_name': sub['bot_first_name'],
                'timezone': sub['timezone'],
                'access_token': sub['access_token'],  # For display (truncated in template)
                'refresh_token': sub['refresh_token']  # Added
            })
        # 5. Self-Healing Stats
        # Instead of trusting the counter in the billing table, we count the REAL rows.
        agency_stats['active_seats'] = len(sub_accounts)
    except Exception as e:
        logger.error(f"Agency Dashboard Error: {e}")
        flash("Error loading agency data.", "error")
    finally:
        cur.close()
        conn.close()
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
        conn.close()

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    if current_user.role == 'agency_owner':
        return redirect(url_for("agency_dashboard"))
   
    form = ConfigForm()
    conn = get_db_connection()
   
    # --- 1. HANDLE SAVING CONFIG (POST) ---
    if form.validate_on_submit():
        if not conn:
            flash("Database connection failed", "error")
        else:
            try:
                cur = conn.cursor()
                # Update the SUBSCRIBERS table
                cur.execute("""
                    UPDATE subscribers
                    SET location_id = %s,
                        calendar_id = %s,
                        crm_user_id = %s,
                        bot_first_name = %s,
                        timezone = %s,
                        initial_message = %s,
                        updated_at = NOW()
                    WHERE email = %s
                """, (
                    form.location_id.data,
                    form.calendar_id.data,
                    form.crm_user_id.data,
                    form.bot_name.data,
                    form.timezone.data,
                    form.initial_message.data,
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
                conn.close()
    # --- 2. PRE-FILL FORM (GET) ---
    # Since current_user is now loaded from 'subscribers', we can use it directly
    if request.method == 'GET':
        form.location_id.data = current_user.location_id
        form.calendar_id.data = current_user.calendar_id
        form.crm_user_id.data = current_user.crm_user_id
        form.bot_name.data = current_user.bot_first_name
        form.timezone.data = current_user.timezone
        form.initial_message.data = current_user.initial_message
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
    # Pass 'sub' as current_user because the template might expect a dict-like object
    return render_template('dashboard.html',
        form=form,
        access_token_display=access_token_display,
        refresh_token_display=refresh_token_display,
        token_readonly=token_field_state,
        expires_in_str=expires_in_str,
        sub=current_user,
        profile=profile
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
        conn.close()

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
    Deletes all demo data older than 2 hours.
    Keeps the DB very light.
    """
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            
            # 1. Clean Messages (older than 2 hours)
            cur.execute("""
                DELETE FROM contact_messages 
                WHERE contact_id LIKE 'demo_%' 
                AND created_at < NOW() - INTERVAL '30 minutes';
            """)
            
            # 2. Clean Facts (older than 2 hours)
            cur.execute("""
                DELETE FROM contact_facts 
                WHERE contact_id LIKE 'demo_%' 
                AND created_at < NOW() - INTERVAL '30 minutes';
            """)

            # 3. Clean Narratives (older than 2 hours)
            cur.execute("""
                DELETE FROM contact_narratives 
                WHERE contact_id LIKE 'demo_%' 
                AND updated_at < NOW() - INTERVAL '30 minutes';
            """)

            conn.commit()
            
        except Exception as e:
            logger.error(f"Janitor cleanup failed: {e}")
        finally:
            cur.close()
            conn.close()

@app.route("/demo-chat")
def demo_chat():
    try:
        run_demo_janitor()
    except:
        pass

    # 1. PERSISTENCE CHECK
    existing_id = request.args.get('session_id')
    clean_id = str(uuid.uuid4())

    initial_msg = "" 

    if existing_id:
        try:
            clean_id = str(uuid.UUID(existing_id))
        except ValueError:
            pass 

    session['demo_session_id'] = clean_id
    demo_contact_id = f"demo_{clean_id}"

    # 2. NEW SESSION LOGIC
    if not existing_id:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM contact_messages WHERE contact_id = %s", (demo_contact_id,))
                cur.execute("DELETE FROM contact_facts WHERE contact_id = %s", (demo_contact_id,))
                cur.execute("DELETE FROM contact_narratives WHERE contact_id = %s", (demo_contact_id,))

                initial_msg = generate_demo_opener()

                cur.execute("""
                    INSERT INTO contact_messages (contact_id, message_type, message_text, created_at)
                    VALUES (%s, 'assistant', %s, NOW())
                """, (demo_contact_id, initial_msg))
                conn.commit()
            except Exception as e:
                logger.error(f"Demo Init Error: {e}")
            finally:
                cur.close()
                conn.close()

    return render_template('demo.html', clean_id=clean_id, demo_contact_id=demo_contact_id, initial_msg=initial_msg)

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
# HYBRID GET LOGS (REDIS + SQL FALLBACK)
# =====================================================
@app.route("/get-logs", methods=["GET"])
def get_logs():
    contact_id = request.args.get("contact_id")
    if not contact_id: return flask_jsonify({"logs": []}) 

    # 1. REDIS CHECK (Fast Lane for Active Chats)
    # We check Redis first. If logs exist here, it's a website visitor.
    # This avoids touching the SQL database for simple chat widgets.
    if conn:
        try:
            redis_key = f"chat_logs:{contact_id}"
            if conn.exists(redis_key):
                raw = conn.lrange(redis_key, 0, -1)
                logs = []
                for x in raw:
                    try:
                        d = json.loads(x)
                        logs.append(d)
                    except: continue
                if logs: return safe_jsonify({"logs": logs})
        except Exception as e:
            # Non-blocking error: log it and proceed to SQL
            logger.warning(f"Redis lookup failed in get_logs: {e}")

    # 2. SQL FALLBACK (Heavy Lane for Demo/Analysis)
    # If Redis was empty, we assume this is a stored Demo/Test session.
    # We apply strict security here to protect real lead data.
    if not contact_id.startswith("test_") and not contact_id.startswith("demo_"):
        return flask_jsonify({"logs": []}) 

    db_conn = get_db_connection()
    if not db_conn: return flask_jsonify({"logs": []})

    logs = []
    try:
        cur = db_conn.cursor(cursor_factory=RealDictCursor)
        
        # A. Fetch Messages
        cur.execute("""
            SELECT message_type, message_text, created_at 
            FROM contact_messages 
            WHERE contact_id = %s 
            ORDER BY created_at ASC
        """, (contact_id,))
        rows = cur.fetchall()
        
        for r in rows:
            ts = r['created_at'].isoformat() if hasattr(r['created_at'], 'isoformat') else str(r['created_at'])
            role = "Bot" if r['message_type'] in ['assistant', 'bot'] else "Lead"
            logs.append({
                "role": role.lower(), 
                "type": f"{role} Message", 
                "content": r['message_text'], 
                "timestamp": ts
            })
        
        # B. Fetch Facts & Narrative (The "Brain" Display)
        facts = get_known_facts(contact_id)
        if facts:
            logs.append({
                "timestamp": datetime.now().isoformat(), 
                "type": "Known Facts", 
                "content": "\n".join([f"• {f}" for f in facts])
            })

        narrative = get_narrative(contact_id)
        
        # C. Logic: If narrative is missing but facts exist, rebuild it on the fly
        # This preserves your "Gold for debugging" logic
        if not narrative and facts:
            try:
                # Basic parsing to help the builder
                facts_text = " ".join(facts).lower()
                first_name = None
                age = None
                
                # Simple extraction to feed the builder
                import re
                name_match = re.search(r"first name: (\w+)", facts_text, re.IGNORECASE)
                if name_match: first_name = name_match.group(1).capitalize()
                
                age_match = re.search(r"age: (\d+)", facts_text)
                if age_match: age = age_match.group(1)

                rebuilt_narrative = build_comprehensive_profile(
                    story_narrative="", 
                    known_facts=facts,
                    first_name=first_name,
                    age=age
                )
                
                # Handle tuple return if builder returns (text, confidence)
                if isinstance(rebuilt_narrative, tuple): 
                    narrative = str(rebuilt_narrative[0])
                else:
                    narrative = str(rebuilt_narrative)
                    
            except Exception as e:
                logger.warning(f"Profile rebuild in logs failed: {e}")

        if narrative:
            logs.append({
                "timestamp": datetime.now().isoformat(), 
                "type": "Full Human Identity Narrative", 
                "content": narrative
            })

        return safe_jsonify({"logs": logs})

    except Exception as e:
        logger.error(f"SQL Logs Error: {e}")
        return flask_jsonify({"logs": []})
    finally:
        db_conn.close()

    
@app.route("/reset-test", methods=["GET"])
def reset_test():
    contact_id = request.args.get("contact_id")
    
    # Security: Only allow test_ prefixed contacts
    if not contact_id or not contact_id.startswith("test_"):
        logger.warning(f"Invalid reset attempt: {contact_id}")
        return flask_jsonify({"error": "Invalid test contact ID"}), 400

    conn = get_db_connection()
    if not conn:
        logger.error("Database connection failed during reset")
        return flask_jsonify({"error": "Database connection failed"}), 500

    try:
        cur = conn.cursor()
        
        # Delete messages, facts, and narrative for this test contact only
        cur.execute("DELETE FROM contact_messages WHERE contact_id = %s", (contact_id,))
        cur.execute("DELETE FROM contact_facts WHERE contact_id = %s", (contact_id,))
        cur.execute("DELETE FROM contact_narratives WHERE contact_id = %s", (contact_id,))
        
        conn.commit()
        
        logger.info(f"Successfully reset test contact {contact_id}")
        
        return flask_jsonify({
            "status": "reset success",
            "message": f"Test session {contact_id} cleared",
            "cleared_contact": contact_id
        }), 200
        
    except Exception as e:
        conn.rollback()  # Important: rollback on error
        logger.error(f"Reset failed for {contact_id}: {e}")
        return flask_jsonify({"error": "Failed to reset test data"}), 500
        
    finally:
        cur.close()
        conn.close()

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
            conn.close()

        # ────────────────────────────────────────────────
        # Send as downloadable .txt file
        # ────────────────────────────────────────────────
        filename = f"InsuranceGrokBot_transcript_{contact_id}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        response = make_response(transcript)
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        response.headers["Content-Type"] = "text/plain; charset=utf-8"
        return response

@app.route("/checkout")
def checkout():
    try:
        # Pre-fill email if user is logged in
        customer_email = current_user.email if current_user.is_authenticated else None
        
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{
                "price": os.getenv("STRIPE_PRICE_ID"),
                "quantity": 1,
            }],
            allow_promotion_codes=True,
            customer_email=customer_email,  # Pre-fill email here

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
    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        return render_template('checkout.html'), 500
    
@app.route("/checkout/agency-starter")
def checkout_agency_starter():
    try:
        # 1. Verification Step: User must be logged in to check their seat count
        if not current_user.is_authenticated:
            # If they aren't logged in, they can't be 'verified' for the 1 or 10 limit
            flash("Please log in to verify your agency seat eligibility.", "warning")
            return redirect("/login")

        customer_email = current_user.email
        
        # 2. Count current seats using your Hybrid Logic
        # We search our data to see how many sub-accounts this email currently 'owns'
        from db import get_db_connection
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM subscribers WHERE parent_agency_email = %s", (customer_email,))
        current_seat_count = cur.fetchone()[0]
        cur.close()
        conn.close()

        # 3. Apply your strict Business Rule: Only 1 or 10 allowed
        if current_seat_count not in [1, 10]:
            logger.warning(f"Eligibility Denied: {customer_email} has {current_seat_count} seats.")
            return render_template_string("""
                <div style="background:var(--dark-bg); color:var(--text-primary); height:100vh; display:flex; align-items:center; justify-content:center; font-family:'Outfit', sans-serif;">
                    <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center; background:var(--card-glass); backdrop-filter:blur(20px);">
                        <h2 style="color:#ff4444;">Eligibility Restriction</h2>
                        <p>The Agency Starter plan is strictly for agencies with 1 or 10 sub-accounts.</p>
                        <p>Current seats detected: <strong>{{ count }}</strong></p>
                        <a href="/dashboard" style="color:var(--accent);">Return to Dashboard</a>
                    </div>
                </div>
            """, count=current_seat_count)

        # 4. Proceed to Stripe if they pass the check
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{
                "price": os.getenv("STRIPE_AGENCY_STARTER_PRICE_ID"),
                "quantity": 1,
            }],
            customer_email=customer_email,
            metadata={
                "user_email": customer_email,
                "target_role": "agency_owner",
                "target_tier": "agency_starter",
                "seat_count_at_purchase": current_seat_count
            },
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                    "user_email": customer_email,
                    "target_role": "agency_owner",
                    "target_tier": "agency_starter"
                }
            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )
        return redirect(session.url, code=303)

    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        return "Internal Server Error", 500

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
        # 1. Non-blocking email grab (Saves time for existing users)
        customer_email = current_user.email if current_user.is_authenticated else None

        # 2. Create the Stripe Session
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=customer_email,
            line_items=[{
                "price": os.getenv("STRIPE_AGENCY_PRO_PRICE_ID"),
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
                        "custom": "GHL Agency Whitelabel Domain (e.g. app.youragency.com)"
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
            subscription_data={
                "trial_period_days": 0,
                "metadata": {
                    "target_role": "agency_owner",
                    "target_tier": "agency_pro"
                }
            },
            
            # Using absolute paths for reliability
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )

        return redirect(session.url, code=303)

    except Exception as e:
        logger.critical(f"Pro Checkout Launch Error: {e}")
        return "The Enterprise Portal is temporarily unavailable. Please contact support.", 500
    
@app.route("/success")
def success():
    session_id = request.args.get("session_id")
    email = None

    if session_id:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            email = session.customer_details.email.lower() if session.customer_details.email else None
        except Exception as e:
            logger.error(f"Stripe session retrieve failed: {e}")

    # SCENARIO 1: User exists but needs a password (created via Webhook)
    if email:
        user = User.get(email)
        if user and not user.password_hash:
            return render_template('checkout-success-generate-password.html', email=email)

    # SCENARIO 2: Generic Success (Already has password or just viewing receipt)
    return render_template('checkout-success-login.html', email=email)

@app.route("/set-password", methods=["POST"])
def set_password():
    email = request.form.get("email").lower()
    password = request.form.get("password")
    confirm = request.form.get("confirm")

    if password != confirm:
        flash("Passwords do not match")
        return redirect(url_for('success'))  # Redirect back to success page logic if possible or show error

    user = User.get(email)
    if user:
        password_hash = generate_password_hash(password)
        conn = get_db_connection()
        conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (password_hash, email))
        conn.commit()
        conn.close()
        login_user(user)
        flash("Password set successfully!")
        return redirect("/dashboard")

    flash("User not found.")
    return redirect("/")

@app.route("/refresh")
def refresh_subscribers():
    try:
        sync_subscribers()
        return "Synced", 200
    except:
        return "Failed", 500

@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    if not code:
        flash("No authorization code received.", "danger")
        return redirect(url_for('home'))

    try:
        # 1. Exchange Code for Token
        token_url = "https://services.leadconnectorhq.com/oauth/token"
        payload = {
            "client_id": os.getenv("GHL_CLIENT_ID"),
            "client_secret": os.getenv("GHL_CLIENT_SECRET"),
            "grant_type": "authorization_code",
            "code": code,
            "user_type": "Location",
            "redirect_uri": f"{os.getenv('YOUR_DOMAIN')}/oauth/callback"
        }
        response = requests.post(token_url, data=payload, timeout=15)
        response.raise_for_status()
        token_data = response.json()

        primary_location_id = token_data.get('locationId')
        access_token = token_data['access_token']
        refresh_token = token_data.get('refresh_token')
        expires_in = token_data.get('expires_in', 86400)

        headers = {'Authorization': f'Bearer {access_token}', 'Version': '2021-07-28'}

        # 2. Get user info
        me_resp = requests.get("https://services.leadconnectorhq.com/users/me", headers=headers, timeout=10)
        me_data = me_resp.json() if me_resp.ok else {}

        user_email = me_data.get('email')
        user_name = me_data.get('name', 'Agency Admin')

        if not user_email:
            flash("Could not retrieve user email from GoHighLevel.", "danger")
            return redirect(url_for('home'))

        # 3. Detect agency status
        agency_resp = requests.get("https://services.leadconnectorhq.com/agencies/", headers=headers, timeout=10)
        agencies = agency_resp.json().get('agencies', [])
        is_agency_owner = len(agencies) > 0

        # 4. Fetch all locations (sub-accounts)
        locations_resp = requests.get("https://services.leadconnectorhq.com/locations/", headers=headers, timeout=15)
        sub_accounts = locations_resp.json().get('locations', [])
        num_subs = len(sub_accounts)

        # 5. Determine tier
        plan_tier = 'individual'
        if is_agency_owner:
            plan_tier = 'agency_pro' if num_subs >= 10 else 'agency_starter'

        # 6. Get primary location details
        primary_sub = next((s for s in sub_accounts if s['id'] == primary_location_id), None)
        primary_name = primary_sub.get('name', 'Unknown Location') if primary_sub else user_name
        primary_timezone = primary_sub.get('timezone', None) if primary_sub else None

        # 7. Database operations
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()

                # --- A. Agency Owner Primary Location ---
                if is_agency_owner:
                    max_seats = 9999 if plan_tier == 'agency_pro' else 10
                    active_seats = max(0, num_subs - 1)  # Exclude primary

                    cur.execute("""
                        INSERT INTO agency_billing (
                            agency_email, location_id, full_name, subscription_tier,
                            max_seats, active_seats, access_token, refresh_token,
                            token_expires_at, timezone, crm_user_id,
                            created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            NOW() + interval '%s seconds', %s, %s, NOW(), NOW()
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
                            updated_at = NOW()
                    """, (
                        user_email, primary_location_id, primary_name, plan_tier,
                        max_seats, active_seats, access_token, refresh_token,
                        expires_in, primary_timezone or 'America/Chicago', me_data.get('id')
                    ))

                # --- B. Sub-accounts (or individual user) ---
                for sub in sub_accounts:
                    sub_id = sub['id']
                    sub_name = sub.get('name', 'Unknown Location')
                    sub_timezone = sub.get('timezone')

                    is_primary = (sub_id == primary_location_id)

                    # Skip primary if agency owner (already handled above)
                    if is_agency_owner and is_primary:
                        continue

                    access_token_this = access_token if is_primary else None
                    refresh_token_this = refresh_token if is_primary else None
                    crm_user_id_this = me_data.get('id') if is_primary else None

                    role = 'agency_sub_account_user' if is_agency_owner else 'individual'
                    parent_agency_email = user_email if is_agency_owner else None
                    email_this = user_email  # Initially link to owner

                    cur.execute("""
                        INSERT INTO subscribers (
                            location_id, email, full_name, role, subscription_tier,
                            parent_agency_email, access_token, refresh_token,
                            token_expires_at, timezone, crm_user_id,
                            created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s THEN NOW() + interval '%s seconds' ELSE NULL END,
                            %s, %s, NOW(), NOW()
                        )
                        ON CONFLICT (location_id) DO UPDATE SET
                            email = EXCLUDED.email,
                            full_name = EXCLUDED.full_name,
                            role = EXCLUDED.role,
                            subscription_tier = EXCLUDED.subscription_tier,
                            parent_agency_email = EXCLUDED.parent_agency_email,
                            access_token = CASE WHEN %s THEN EXCLUDED.access_token ELSE subscribers.access_token END,
                            refresh_token = CASE WHEN %s THEN EXCLUDED.refresh_token ELSE subscribers.refresh_token END,
                            token_expires_at = CASE WHEN %s THEN EXCLUDED.token_expires_at ELSE subscribers.token_expires_at END,
                            timezone = EXCLUDED.timezone,
                            crm_user_id = CASE WHEN %s THEN EXCLUDED.crm_user_id ELSE subscribers.crm_user_id END,
                            updated_at = NOW()
                    """, (
                        sub_id, email_this, sub_name, role, plan_tier,
                        parent_agency_email, access_token_this, refresh_token_this,
                        is_primary, expires_in,
                        sub_timezone, crm_user_id_this,
                        is_primary, is_primary, is_primary, is_primary
                    ))

                conn.commit()
                logger.info(f"Successfully onboarded {user_email} ({'agency' if is_agency_owner else 'individual'}) with {num_subs} locations.")

            except Exception as e:
                conn.rollback()
                logger.error(f"Database onboarding error: {e}", exc_info=True)
                flash("Error completing setup. Please contact support.", "danger")
                return redirect(url_for('home'))
            finally:
                cur.close()
                conn.close()

        # Success redirect
        flash(f"Success! {num_subs} locations connected.", "success")
        if is_agency_owner:
            return redirect("/agency-dashboard")
        return redirect("/dashboard")

    except requests.RequestException as e:
        logger.error(f"OAuth network error: {e}")
        flash("Failed to connect to GoHighLevel. Please try again.", "danger")
        return redirect(url_for('home'))
    except Exception as e:
        logger.error(f"Critical OAuth failure: {e}", exc_info=True)
        flash("An unexpected error occurred. Please try again or contact support.", "danger")
        return redirect(url_for('home'))
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
        {"name": "Marcus T.", "role": "Independent Agent", "text": "The integration is seamless. It feels native to GoHighLevel.", "stars": 5},
        {"name": "Jason V.", "role": "Independent Agent", "text": "I've tried every bot on the market. This is the only one that understands underwriting.", "stars": 5}
    ]

    # Filter: Show only 5-star reviews
    visible_reviews = [r for r in all_reviews if r['stars'] == 5]
    return render_template('reviews.html', reviews=visible_reviews, form=form)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)