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
    return flask_jsonify(make_json_serializable)

# === REDIS & RQ SETUP ===
# This connects to the Redis service via the variable you added in Railway
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
try:
    conn = redis.from_url(redis_url)
    q = Queue(connection=conn)
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
    code = StringField("Confirmation Code (from GHL install)", validators=[]) 
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
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[
                {"role": "system", "content": """
You are an expert Life Insurance Sales Agent trying to re-engage a cold lead via SMS text message. Your goal is to generate interest and get them to respond so you can book an appointment.
Tone: Helpful, curious, not salesy, laid-back, casual, conversational, no corporate-speak, no emojis, no endearing words, no jargon.
CRITICAL RULES:
Must  include the topic of Life Insurance in some form or way; or come across as a spammer if you dont, up to you. 
No "Hi", "Hello", "Hey", or "This is [Name]".
Start with a general problem, issue, or confusion around their policy, seed general doubts about coverage, or hint at new benefits.
You're first message is meant to get a response, not to sell right away, so avoid hard CTAs. 
If they don't respond you didnt do your job. Can only book appointments if they respond first. Dont shoot yourself in the foot getting too eager.
NO CLOSING ATTEMPTS. !important!
NEVER ASK TWO QUESTIONS IN A SINGLE RESPONSE. !IMPORTANT! reformulate reply to have a single open-ended question. may include a statement but must have only one question.!important!
                """},
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
    if not q:
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

    # 2. Enqueue the Brain
    try:
        job = q.enqueue(
            process_webhook_task,
            payload,
            job_timeout=120,
            result_ttl=86400
        )
        return flask_jsonify({"status": "queued", "job_id": job.id}), 202
    except Exception as e:
        logger.error(f"Queue failed: {e}")
        return flask_jsonify({"status": "error"}), 500

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
    form = RegisterForm()
    
    # --- SEAMLESS PRE-FILL ---
    # If sent here by OAuth, grab the code from the URL
    if request.method == "GET":
        url_code = request.args.get('code')
        if url_code:
            form.code.data = url_code
            # Optional: You can flash a message saying "Connected! Create your account."
            flash("GoHighLevel connected successfully. Complete setup below.", "success")

    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        code = form.code.data.upper().strip() if form.code.data else ""

        if User.get(email):
            flash("Email already registered.", "error")
            return redirect("/login")

        is_valid = False
        used_code_row = None

        # Logic: Check Stripe OR Check Sheet Code
        if current_user.is_authenticated and current_user.stripe_customer_id:
            is_valid = True
        elif code and worksheet:
            try:
                values = worksheet.get_all_values()
                header_lower = [h.strip().lower() for h in values[0]]
                
                code_idx = header_lower.index("confirmation_code") if "confirmation_code" in header_lower else -1
                used_idx = header_lower.index("code_used") if "code_used" in header_lower else -1
                email_idx = header_lower.index("email") if "email" in header_lower else -1
                
                if code_idx != -1:
                    for i, row in enumerate(values[1:], start=2):
                        # Verify Code Matches
                        if len(row) > code_idx and row[code_idx].strip().upper() == code:
                            # Verify Not Used
                            if used_idx != -1 and len(row) > used_idx and row[used_idx] == "1":
                                flash("Code already used.", "error")
                                return redirect("/register")
                            
                            used_code_row = i
                            is_valid = True
                            break
                    
                    if is_valid and used_code_row and used_idx != -1:
                        # MARK AS USED
                        worksheet.update_cell(used_code_row, used_idx + 1, "1")
                        
                        # SAVE EMAIL TO SHEET
                        if email_idx == -1: 
                            new_col = len(values[0]) + 1
                            worksheet.update_cell(1, new_col, "email")
                            email_idx = new_col - 1
                        worksheet.update_cell(used_code_row, email_idx + 1, email)
            except Exception as e:
                logger.error(f"Code validation error: {e}")

        if not is_valid:
            flash("Invalid code or no subscription found.", "error")
            return redirect("/register")

        password_hash = generate_password_hash(form.password.data)
        if User.create(email, password_hash):
            flash("Account created successfully! Please log in.", "success")
            return redirect("/login")
        else:
            flash("Creation failed.", "error")

    # The HTML template remains exactly the same
    # because form.code.data is now pre-filled by the GET logic above
    return render_template('register.html', form=form)

@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        print(f"[LOGIN DEBUG] Attempting login for: '{email}'")
        
        # Fetch user from subscribers table (merged data)
        user = User.get_from_subscribers(email)
        
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
        
        if role in ['individual', 'individual_user', 'user']:
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

@app.route("/agency-dashboard")
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
                created_at
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
                'timezone': sub['timezone']
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
        # Update SUBSCRIBERS instead of users
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

@app.route("/get-logs", methods=["GET"])
def get_logs():
    contact_id = request.args.get("contact_id")

    if not contact_id or (not contact_id.startswith("test_") and not contact_id.startswith("demo_")):
        return flask_jsonify({"logs": []}) 

    conn = get_db_connection()
    if not conn:
        logger.error("Database connection failed in get_logs")
        return flask_jsonify({"error": "Database connection failed"}), 500

    logs = []

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # === 1. Messages with real timestamps ===
        cur.execute("""
            SELECT message_type, message_text, created_at
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at ASC
        """, (contact_id,))
        messages = cur.fetchall()

        for row in messages:
            # === CRITICAL FIX START ===
            # Your DB returns a Dictionary, so we must access by key!
            msg_type = row['message_type']
            text = row['message_text']
            created_at = row['created_at']
            # === CRITICAL FIX END ===

            role = "Lead" if msg_type == "lead" else "Bot"
            
            timestamp = "Unknown"
            if created_at:
                if isinstance(created_at, str):
                    timestamp = created_at
                elif hasattr(created_at, 'isoformat'):
                    timestamp = created_at.isoformat()
                else:
                    timestamp = str(created_at)
            
            logs.append({"timestamp": timestamp, "type": f"{role} Message", "content": text.strip()})

        facts = get_known_facts(contact_id)
        fact_content = "\n".join([f"• {f}" for f in facts]) if facts else "No facts extracted yet"
        logs.append({"timestamp": datetime.now().isoformat(), "type": "Known Facts", "content": fact_content})
        
        # Extract basics for profile rebuild
        first_name = None
        age = None
        address = None
        facts_text = " ".join(facts).lower()

        story_narrative = get_narrative(contact_id)
        
        name_match = re.search(r"first name: (\w+)", facts_text, re.IGNORECASE)
        if name_match: first_name = name_match.group(1).capitalize()
        
        age_match = re.search(r"age: (\d+)", facts_text)
        if age_match: age = age_match.group(1)
        
        addr_match = re.search(r"address/location: (.*)", facts_text, re.IGNORECASE)
        if addr_match: address = addr_match.group(1).strip()

        narrative_text = "Narrative pending..."

        try:
            profile_narrative = build_comprehensive_profile(
                story_narrative=story_narrative,
                known_facts=facts,
                first_name=first_name,
                age=age,
                address=address
            )

            if isinstance(profile_narrative, tuple):
                narrative_text = profile_narrative[0]
            else:
                narrative_text = str(profile_narrative)

        except Exception as e:
            logger.error(f"Profile build error in logs: {e}")
            narrative_text = f"Error building profile: {str(e)}"

        logs.append({
            "timestamp": datetime.now().isoformat(),
            "type": "Full Human Identity Narrative",
            "content": narrative_text 
        })
        safe_logs = make_json_serializable(logs)
        return flask_jsonify({"logs": safe_logs})
        
    except Exception as e:
        logger.error(f"Error in get_logs: {e}")
        logs.append({"logs": []})
    finally:
        if 'cur' in locals() and cur:
            cur.close()
        if conn:
            conn.close()

    

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
    if not contact_id or not contact_id.startswith("test_"):
        return flask_jsonify({"error": "Invalid test contact"}), 400

    # Fetch data from DB
    messages = get_recent_messages(contact_id, limit=50)
    facts = get_known_facts(contact_id)
    story_narrative = get_narrative(contact_id)

    # Rebuild the current profile narrative (this is gold for debugging!)
    first_name = None
    age = None
    address = None
    for fact in facts:
        if "First name:" in fact:
            first_name = fact.split(":", 1)[1].strip()
        elif "Age:" in fact:
            age = fact.split(":", 1)[1].strip()
        elif "Address/location:" in fact:
            address = fact.split(":", 1)[1].strip()

    profile_narrative = build_comprehensive_profile(
        story_narrative=story_narrative,
        known_facts=facts,
        first_name=first_name,
        age=age,
        address=address
    )

    # Build transcript
    transcript_lines = []
    transcript_lines.append("INSURANCEGROKBOT TEST TRANSCRIPT")
    transcript_lines.append("=" * 50)
    transcript_lines.append(f"Contact ID: {contact_id}")
    transcript_lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    transcript_lines.append("")

    transcript_lines.append("CURRENT BOT UNDERSTANDING (Narrative + Facts)")
    transcript_lines.append("-" * 50)
    transcript_lines.extend(profile_narrative.split("\n"))
    transcript_lines.append("")

    transcript_lines.append("CONVERSATION HISTORY")
    transcript_lines.append("-" * 30)
    for msg in messages:
        role = "USER" if msg['role'] == "lead" else "BOT"
        # msg['text'] is stored, timestamp is not in get_recent_messages return, so simplified here
        transcript_lines.append(f"{role}: {msg['text']}")
        transcript_lines.append("")

    transcript = "\n".join(transcript_lines)

    # Send as downloadable file
    response = make_response(transcript)
    response.headers["Content-Disposition"] = f"attachment; filename=grokbot_transcript_{contact_id}.txt"
    response.headers["Content-Type"] = "text/plain"
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
                "target_role": "individual_user",
                "target_tier": "individual",
                "source": "website"
            },
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                "user_email": customer_email,
                "target_role": "individual_user",
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
                <div style="background:#050505; color:white; height:100vh; display:flex; align-items:center; justify-content:center; font-family:sans-serif;">
                    <div style="padding:40px; border:1px solid #ff4444; border-radius:20px; text-align:center;">
                        <h2 style="color:#ff4444;">Eligibility Restriction</h2>
                        <p>The Agency Starter plan is strictly for agencies with 1 or 10 sub-accounts.</p>
                        <p>Current seats detected: <strong>{{ count }}</strong></p>
                        <a href="/dashboard" style="color:#007AFF;">Return to Dashboard</a>
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

    # 1. Exchange Code for Token (The "Entry Key")
    token_url = "https://services.leadconnectorhq.com/oauth/token"
    payload = {
        "client_id": os.getenv("GHL_CLIENT_ID"),
        "client_secret": os.getenv("GHL_CLIENT_SECRET"),
        "grant_type": "authorization_code",
        "code": code,
        "user_type": "Location", 
        "redirect_uri": f"{os.getenv('YOUR_DOMAIN')}/oauth/callback"
    }

    try:
        response = requests.post(token_url, data=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        # This is the location the User actually selected during install
        primary_location_id = data.get('locationId')
        access_token = data['access_token']
        refresh_token = data.get('refresh_token')
        expires_in = data.get('expires_in', 86400)

        headers = {'Authorization': f'Bearer {access_token}', 'Version': '2021-07-28'}

        # 2. Identify the User (Who is installing this?)
        me_resp = requests.get("https://services.leadconnectorhq.com/users/me", headers=headers, timeout=10)
        me_data = me_resp.json() if me_resp.ok else {}
        
        user_email = me_data.get('email')
        user_name = me_data.get('name', 'Agency Admin')
        
        # 3. Detect Agency Status (Are they the Boss?)
        # We try to list agencies. If we can, they are likely an Agency Admin/Owner.
        agency_resp = requests.get("https://services.leadconnectorhq.com/agencies/", headers=headers, timeout=10)
        agencies = agency_resp.json().get('agencies', [])
        is_agency_owner = len(agencies) > 0
        
        # 4. FETCH ALL SUB-ACCOUNTS (The "Scan")
        # This pulls every location this user has access to.
        locations_resp = requests.get("https://services.leadconnectorhq.com/locations/", headers=headers, timeout=15)
        sub_accounts = locations_resp.json().get('locations', [])
        num_subs = len(sub_accounts)

        # 5. Determine Tier based on Size
        plan_tier = 'individual'
        if is_agency_owner:
            if num_subs >= 10:
                plan_tier = 'agency_pro'  # 10+ accounts
            else:
                plan_tier = 'agency_starter' # 1-9 accounts

        # 6. DATABASE OPERATIONS (The "Onboarding")
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()

                # --- A. Setup Agency Billing Record ---
                # This ensures the Agency Dashboard works immediately
                if is_agency_owner and user_email:
                    max_seats = 9999 if plan_tier == 'agency_pro' else 10
                    cur.execute("""
                        INSERT INTO agency_billing (agency_email, subscription_tier, max_seats, active_seats)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (agency_email) DO UPDATE SET
                            subscription_tier = EXCLUDED.subscription_tier,
                            active_seats = %s; 
                    """, (user_email, plan_tier, max_seats, num_subs, num_subs))

                # --- B. Onboard Every Single Sub-Account ---
                for sub in sub_accounts:
                    sub_id = sub['id']
                    sub_name = sub.get('name', 'Unknown Location')
                    sub_timezone = sub.get('timezone', 'America/Chicago')
                    
                    # TOKEN LOGIC:
                    # If this is the Primary Location (the one selected at install), we have the token!
                    # If this is a Sub-Account, we DO NOT have a token yet (unless Agency OAuth is used).
                    # We still create the row so it shows in the dashboard.
                    is_primary = (sub_id == primary_location_id)
                    
                    # We try to find the specific User ID for this location map
                    # (This is the specific request you wanted to keep)
                    crm_user_id = None
                    if is_primary:
                        # We can only reliably fetch this for the primary location with the current token
                        crm_user_id = me_data.get('id')

                    # UPSERT into Subscribers Table
                    cur.execute("""
                        INSERT INTO subscribers (
                            location_id, 
                            email, 
                            full_name,
                            role, 
                            subscription_tier,
                            parent_agency_email,
                            access_token, 
                            refresh_token, 
                            token_expires_at,
                            bot_first_name, 
                            timezone,
                            crm_user_id,
                            created_at,
                            updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, 
                            CASE WHEN %s THEN NOW() + interval '%s seconds' ELSE NULL END,
                            'Grok', %s, %s, NOW(), NOW()
                        )
                        ON CONFLICT (location_id) DO UPDATE SET
                            -- Always update these:
                            parent_agency_email = EXCLUDED.parent_agency_email,
                            subscription_tier = EXCLUDED.subscription_tier,
                            timezone = EXCLUDED.timezone,
                            
                            -- Only update Auth Info if this is the Primary location we just authenticated
                            access_token = CASE WHEN %s THEN EXCLUDED.access_token ELSE subscribers.access_token END,
                            refresh_token = CASE WHEN %s THEN EXCLUDED.refresh_token ELSE subscribers.refresh_token END,
                            token_expires_at = CASE WHEN %s THEN EXCLUDED.token_expires_at ELSE subscribers.token_expires_at END,
                            crm_user_id = CASE WHEN %s THEN EXCLUDED.crm_user_id ELSE subscribers.crm_user_id END,
                            updated_at = NOW();
                    """, (
                        sub_id,
                        user_email, # All subs linked to owner initially
                        sub_name,   # Store Location Name as 'Full Name' initially
                        'agency_owner' if is_primary else 'individual',
                        plan_tier,
                        user_email, # Parent Agency Email
                        access_token if is_primary else None,
                        refresh_token if is_primary else None,
                        is_primary, expires_in,
                        sub_timezone,
                        crm_user_id,
                        
                        # Conditional parameters for the ON CONFLICT checks
                        is_primary, is_primary, is_primary, is_primary
                    ))

                conn.commit()
                logger.info(f"🚀 Fully Onboarded Agency {user_email} with {num_subs} locations.")

            except Exception as e:
                conn.rollback()
                logger.error(f"SQL Error during Onboarding: {e}")
                flash("Error setting up agency database.", "danger")
            finally:
                cur.close()
                conn.close()

        # 7. Redundant Backup (Google Sheets) - Only if you still want it
        # ... (Your existing sheet logic here) ...

        # 8. Redirect Logic
        flash(f"Success! {num_subs} locations connected.", "success")
        if is_agency_owner:
            return redirect("/agency-dashboard")
        return redirect("/dashboard")

    except requests.RequestException as e:
        logger.error(f"OAuth network error: {e}")
        flash("Connection to GoHighLevel failed.", "danger")
        return redirect(url_for('home'))
    except Exception as e:
        logger.error(f"OAuth callback critical failure: {e}", exc_info=True)
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
        user = User.get(email)

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