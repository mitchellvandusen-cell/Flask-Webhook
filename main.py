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
    """
    Registration - Supports TWO paths:
    1. Post-OAuth: User already in DB from OAuth, just sets password
    2. Website/Stripe: New user, creates entry in DB manually

    Sub-users should use /claim-account instead.
    """
    form = RegisterForm()

    # Pre-fill from OAuth redirect or Stripe checkout
    if request.method == "GET":
        url_location_id = request.args.get('location_id')
        if url_location_id:
            form.location_id.data = url_location_id
            flash("GoHighLevel connected! Your location ID is pre-filled. Set a password to finish.", "success")

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

            # 2. Check if location_id already exists in subscribers (from OAuth)
            cur.execute("""
                SELECT email, parent_agency_email, invite_token, onboarding_status
                FROM subscribers
                WHERE location_id = %s
                LIMIT 1
            """, (submitted_location_id,))
            match = cur.fetchone()

            password_hash = generate_password_hash(password)

            if match:
                # PATH A: Post-OAuth Registration
                # User already in DB from OAuth, just set password

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

            else:
                # PATH B: Website/Stripe Registration
                # User NOT in DB yet, create new entry
                # This is for users who:
                # - Paid via Stripe on website
                # - Haven't done OAuth yet
                # - Manually entering their location_id

                logger.info(f"Creating new subscriber entry for Stripe/manual registration: {email}")

                cur.execute("""
                    INSERT INTO subscribers (
                        location_id, email, password_hash, full_name, role,
                        subscription_tier, onboarding_status,
                        timezone, bot_first_name,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, 'individual',
                        'individual', 'claimed',
                        'America/Chicago', 'Grok',
                        NOW(), NOW()
                    )
                """, (
                    submitted_location_id,
                    email,
                    password_hash,
                    form.email.data  # Use email as name initially
                ))

                conn.commit()
                logger.info(f"Manual/Stripe registration completed: {email}")
                flash("Account created successfully! You can now connect your GoHighLevel account from the dashboard.", "success")
                return redirect(url_for("login"))

        except Exception as e:
            conn.rollback()
            logger.error(f"Registration failed for {email}: {e}")
            flash("Account creation failed. Please try again or contact support.", "error")
            return redirect("/register")
        finally:
            cur.close()
            conn.close()

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

        # 1. Save user message
        cur.execute("""
            INSERT INTO contact_messages (contact_id, message_type, message_text)
            VALUES (%s, 'lead', %s)
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
        conn.close()

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
        if director_output["stage"] == "closing":
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

        grok_messages = [{"role": "system", "content": system_prompt}]
        for msg in recent_exchanges[-8:]:
            role = "user" if msg["role"] == "lead" else "assistant"
            grok_messages.append({"role": role, "content": msg["text"]})
        grok_messages.append({"role": "user", "content": message})

        # 4. Call Grok
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=grok_messages,
            temperature=0.85,
            max_tokens=200,
        )

        reply = response.choices[0].message.content.strip()

        # Clean reply
        reply = re.sub(r'<thinking>[\s\S]*?</thinking>', '', reply)
        reply = re.sub(r'</?reply>', '', reply)
        reply = re.sub(r'<[^>]+>', '', reply).strip()
        reply = reply.replace("—", ",").replace("–", ",").strip()

        # 5. Save bot response
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO contact_messages (contact_id, message_type, message_text)
                VALUES (%s, 'assistant', %s)
            """, (contact_id, reply))
            conn.commit()
            cur.close()
            conn.close()

        # 6. Return response directly to frontend
        return flask_jsonify({
            "reply": reply,
            "stage": director_output["stage"]
        })

    except Exception as e:
        logger.error(f"Demo chat error: {e}", exc_info=True)
        return flask_jsonify({
            "reply": "What's your main concern about coverage right now?",
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
            conn.close()
            return flask_jsonify({"contact_id": contact_id, "opener": opener, "status": "new"})

        cur.execute("""
            SELECT message_type, message_text 
            FROM contact_messages 
            WHERE contact_id = %s 
            ORDER BY created_at ASC
        """, (contact_id,))

        history = [{"role": "bot" if r['message_type'] == 'assistant' else "user", "content": r['message_text']} for r in cur.fetchall()]
        cur.close()
        conn.close()

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
        except:
            pass
        finally:
            conn.close()

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
        conn.close()

    return flask_jsonify({"contact_id": new_id, "opener": opener})

@app.route("/demo-chat")
def demo_chat():
    try:
        run_demo_janitor()
    except:
        pass
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

        # 2. Fetch Facts
        facts = get_known_facts(contact_id)
        if facts:
            logs.append({
                "timestamp": datetime.now().isoformat(),
                "type": "Known Facts",
                "content": "\n".join([f"• {f}" for f in facts])
            })

        # 3. Fetch/Build Narrative
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
        db_conn.close()


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
        flash("Password set successfully! You can now log in anytime.", "success")

        # Redirect based on role
        if current_user.role == 'agency_owner':
            return redirect("/agency-dashboard")
        else:
            return redirect("/dashboard")

    except Exception as e:
        conn.rollback()
        logger.error(f"Set password error for {current_user.email}: {e}")
        flash("Error setting password. Please try again.", "error")
        return redirect(f"/set-password?type={user_type}")
    finally:
        cur.close()
        conn.close()

@app.route("/refresh")
def refresh_subscribers():
    try:
        sync_subscribers()
        return "Synced", 200
    except:
        return "Failed", 500

@app.route("/oauth/initiate")
def oauth_initiate():
    """
    Initiates OAuth flow with GoHighLevel.
    Works BEFORE marketplace approval (using private app credentials).

    User clicks "Connect with GoHighLevel" → Redirected to GHL consent page → Back to /oauth/callback
    """
    client_id = os.getenv("PRIVATE_APP_CLIENT_ID")
    redirect_uri = f"{os.getenv('YOUR_DOMAIN')}/oauth/callback"

    # Required scopes for the app
    scopes = [
        "locations.readonly",
        "users.readonly",
        "contacts.write",
        "contacts.readonly",
        "opportunities.readonly",
        "opportunities.write",
        "calendars.readonly",
        "calendars.write",
        "conversations.readonly",
        "conversations.write",
        "conversations/message.readonly",
        "conversations/message.write"
    ]
    scope_string = " ".join(scopes)

    # Use state parameter to identify this as private app flow (Stripe/website users)
    state = "private_app"

    # Build OAuth URL
    oauth_url = (
        f"https://marketplace.gohighlevel.com/oauth/chooselocation?"
        f"response_type=code&"
        f"redirect_uri={redirect_uri}&"
        f"client_id={client_id}&"
        f"scope={scope_string}&"
        f"state={state}"
    )

    logger.info(f"Initiating private app OAuth flow. Redirecting to: {oauth_url}")
    return redirect(oauth_url)

@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code:
        flash("No authorization code received.", "danger")
        return redirect(url_for('home'))

    try:
        # Determine which OAuth app was used based on state parameter
        # state="private_app" → Stripe/website users connecting GHL
        # No state → GHL marketplace installation
        is_private_app = (state == "private_app")

        if is_private_app:
            client_id = os.getenv("PRIVATE_APP_CLIENT_ID")
            client_secret = os.getenv("PRIVATE_APP_SECRET_ID")
            logger.info("OAuth callback: Using private app credentials (Stripe/website flow)")
        else:
            client_id = os.getenv("GHL_CLIENT_ID")
            client_secret = os.getenv("GHL_CLIENT_SECRET")
            logger.info("OAuth callback: Using marketplace app credentials (GHL marketplace flow)")

        # 1. Exchange Code for Token
        token_url = "https://services.leadconnectorhq.com/oauth/token"
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
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

        # 6b. Fetch users for each location (to get agent emails)
        location_users = {}  # {location_id: [list of users]}

        if is_agency_owner:
            for sub in sub_accounts:
                loc_id = sub['id']
                try:
                    # GHL API: Get users assigned to this location
                    users_resp = requests.get(
                        f"https://services.leadconnectorhq.com/locations/{loc_id}/users",
                        headers=headers,
                        timeout=10
                    )
                    if users_resp.ok:
                        users_data = users_resp.json().get('users', [])
                        location_users[loc_id] = users_data
                        logger.info(f"Found {len(users_data)} users for location {loc_id}")
                except Exception as e:
                    logger.warning(f"Could not fetch users for location {loc_id}: {e}")
                    location_users[loc_id] = []

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

                    # Get the first user's email for this location (if any)
                    agent_email = None
                    agent_name = sub_name
                    agent_crm_user_id = None

                    loc_users = location_users.get(sub_id, [])
                    if loc_users:
                        # Get the first (primary) user for this location
                        primary_user = loc_users[0]
                        agent_email = primary_user.get('email')
                        agent_name = primary_user.get('name') or sub_name
                        agent_crm_user_id = primary_user.get('id')
                        logger.info(f"Location {sub_id} has agent: {agent_email}")

                    access_token_this = access_token if is_primary else None
                    refresh_token_this = refresh_token if is_primary else None

                    role = 'agency_sub_account_user' if is_agency_owner else 'individual'
                    parent_agency_email = user_email if is_agency_owner else None
                    email_this = user_email  # Owner's email for billing/parent link

                    cur.execute("""
                        INSERT INTO subscribers (
                            location_id, email, agent_email, full_name, role, subscription_tier,
                            parent_agency_email, access_token, refresh_token,
                            token_expires_at, timezone, crm_user_id,
                            onboarding_status, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s THEN NOW() + interval '%s seconds' ELSE NULL END,
                            %s, %s, %s, NOW(), NOW()
                        )
                        ON CONFLICT (location_id) DO UPDATE SET
                            email = EXCLUDED.email,
                            agent_email = COALESCE(EXCLUDED.agent_email, subscribers.agent_email),
                            full_name = EXCLUDED.full_name,
                            role = EXCLUDED.role,
                            subscription_tier = EXCLUDED.subscription_tier,
                            parent_agency_email = EXCLUDED.parent_agency_email,
                            access_token = CASE WHEN %s THEN EXCLUDED.access_token ELSE subscribers.access_token END,
                            refresh_token = CASE WHEN %s THEN EXCLUDED.refresh_token ELSE subscribers.refresh_token END,
                            token_expires_at = CASE WHEN %s THEN EXCLUDED.token_expires_at ELSE subscribers.token_expires_at END,
                            timezone = EXCLUDED.timezone,
                            crm_user_id = COALESCE(EXCLUDED.crm_user_id, subscribers.crm_user_id),
                            updated_at = NOW()
                    """, (
                        sub_id, email_this, agent_email, agent_name, role, plan_tier,
                        parent_agency_email, access_token_this, refresh_token_this,
                        is_primary, expires_in,
                        sub_timezone or 'America/Chicago', agent_crm_user_id,
                        'pending',  # onboarding_status
                        is_primary, is_primary, is_primary
                    ))

                conn.commit()
                logger.info(f"Successfully onboarded {user_email} ({'agency' if is_agency_owner else 'individual'}) with {num_subs} locations.")

                # Check if user needs to set password
                needs_password = False
                if is_agency_owner:
                    cur.execute("SELECT password_hash FROM agency_billing WHERE agency_email = %s", (user_email,))
                    row = cur.fetchone()
                    needs_password = not row or not row[0]
                else:
                    cur.execute("SELECT password_hash FROM subscribers WHERE email = %s", (user_email,))
                    row = cur.fetchone()
                    needs_password = not row or not row[0]

            except Exception as e:
                conn.rollback()
                logger.error(f"Database onboarding error: {e}", exc_info=True)
                flash("Error completing setup. Please contact support.", "danger")
                return redirect(url_for('home'))
            finally:
                cur.close()
                conn.close()

        # Login the user via Flask-Login
        user = User.get(user_email)
        if user:
            login_user(user)

        # Success redirect - check if password needed
        flash(f"Success! {num_subs} locations connected.", "success")

        if needs_password:
            if is_agency_owner:
                return redirect("/set-password?type=agency")
            else:
                return redirect(f"/register?location_id={primary_location_id}")

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
        conn.close()


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
        conn.close()


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
        conn.close()


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
        conn.close()

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
            "text": "Perfect size to start. Here's what I solve for you: inconsistent follow-up across your team. Some agents are great, some let leads rot. With me, every sub-account gets the same AI setter - same brain, same methodology, but books to THEIR calendar. You get a dashboard to see everything. $800/month covers up to 10 agents.",
            "options": [
                {"label": "How does that work exactly?", "value": "agency_how"},
                {"label": "Show me the demo", "value": "demo"},
                {"label": "What's included?", "value": "agency_features"}
            ]
        })

    if user_message in ["agency_medium", "agency_large"]:
        return flask_jsonify({
            "text": "At your scale, lead leakage is probably costing you six figures a year. Here's what I do: every single sub-account gets an AI setter. Same training, same methodology, same quality - but each one books to that agent's calendar. One dashboard for you to monitor everything. Unlimited sub-accounts for $1,600/month flat.",
            "options": [
                {"label": "How does multi-tenant work?", "value": "agency_how"},
                {"label": "Show me the demo", "value": "demo"},
                {"label": "What makes this different?", "value": "comparison"}
            ]
        })

    if user_message == "agency_how":
        return flask_jsonify({
            "text": "Simple: You connect your GHL agency account. I automatically see all your sub-accounts. Each one gets their own instance of me - same sales brain, but configured for their calendar and timezone. When a lead texts into Location A, I respond as Location A's setter and book on their calendar. You see all conversations from one dashboard. Your agents don't need to do anything.",
            "options": [
                {"label": "What do my agents see?", "value": "agency_agent_view"},
                {"label": "Try the demo", "value": "demo"},
                {"label": "Pricing", "value": "pricing_agency"}
            ]
        })

    if user_message == "agency_agent_view":
        return flask_jsonify({
            "text": "Your agents see conversations happening in their GHL inbox like normal. They can jump in anytime if needed. But mostly they just see appointments showing up on their calendar with qualified leads. The AI does the grunt work, they do the closing.",
            "options": [
                {"label": "That sounds good", "value": "demo"},
                {"label": "What's pricing?", "value": "pricing_agency"}
            ]
        })

    if user_message == "agency_features":
        return flask_jsonify({
            "text": "Agency Starter ($800/mo) includes: Up to 10 sub-accounts, multi-tenant dashboard, shared memory across your agency, priority support, all 5 sales methodologies, auto-booking to each agent's calendar, and underwriting pre-qualification. 7-day free trial.",
            "options": [
                {"label": "Start free trial", "value": "signup_agency_starter"},
                {"label": "See it work first", "value": "demo"},
                {"label": "What if I have more than 10?", "value": "agency_pro_info"}
            ]
        })

    if user_message == "agency_pro_info":
        return flask_jsonify({
            "text": "Agency Pro is $1,600/month for unlimited sub-accounts. Same features plus dedicated high-speed queue (faster responses) and white-glove onboarding. No cap on agents - scale as big as you want, price stays the same.",
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
            "text": "I connect directly to your GHL calendar. When a lead is ready, I show them available slots and book it - no links to click, no friction. The appointment shows up on your calendar with all the context: what they said, their health info, what objections came up. You walk into the call prepared.",
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
            "text": "$100/month. Unlimited conversations, full memory, all 5 sales methodologies, calendar auto-booking, underwriting logic. 7-day free trial to make sure it works for you.",
            "options": [
                {"label": "Start free trial", "value": "signup_individual"},
                {"label": "See it first", "value": "demo"}
            ]
        })

    if user_message == "pricing_agency" or ("price" in msg_lower and "agency" in msg_lower):
        return flask_jsonify({
            "text": "Two options: Agency Starter is $800/month for up to 10 sub-accounts. Agency Pro is $1,600/month for unlimited. Both include the full multi-tenant dashboard and all features. 7-day trial on Starter.",
            "options": [
                {"label": "Agency Starter ($800)", "value": "signup_agency_starter"},
                {"label": "Agency Pro ($1,600)", "value": "signup_agency_pro"},
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
            "text": "Good choice. 7-day free trial for up to 10 sub-accounts.",
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

    if "ghl" in msg_lower or "gohighlevel" in msg_lower or "highlevel" in msg_lower or "crm" in msg_lower:
        return flask_jsonify({
            "text": "I integrate directly with GoHighLevel. You connect via OAuth (one click), and I automatically see your contacts, calendars, and conversations. Works with any GHL plan - agency or location level.",
            "options": [
                {"label": "See integration", "value": "demo"},
                {"label": "Get started", "value": "signup_individual"}
            ]
        })

    if "support" in msg_lower or "help" in msg_lower or "setup" in msg_lower:
        return flask_jsonify({
            "text": "Setup takes about 5 minutes - connect GHL, configure your calendar, done. All plans include support. Agency Pro includes white-glove onboarding where we set everything up for you.",
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