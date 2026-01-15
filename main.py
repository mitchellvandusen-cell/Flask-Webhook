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
# Updated to pull from the new DB structure
from db import get_db_connection, init_db, User, get_known_facts, get_narrative, get_recent_messages, get_subscriber_info_hybrid
from sync_subscribers import sync_subscribers
from tasks import process_webhook_task  
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

# =====================================================
# WEBSITE CHAT WEBHOOK (Hybrid Architecture)
# =====================================================
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
        "faq": "/faq", "help": "/faq"
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

# =====================================================
# LOGIN ROUTES (SEPARATED TABLES)
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        
        # STRICT LOOKUP: Only check subscribers table
        user = User.get_from_subscribers(email)
        
        if not user:
            flash("Account not found. If you are an Agency Owner, please use the Agency Login.", "warning")
            return render_template("login.html", form=form)
            
        # SECURITY FIX: Ensure password_hash exists AND matches
        if not user.password_hash or not check_password_hash(user.password_hash, form.password.data):
            flash("Incorrect password.", "error")
            return render_template("login.html", form=form)
            
        login_user(user)
        return redirect(url_for("dashboard"))

    return render_template("login.html", form=form)

@app.route("/agency-login", methods=["GET", "POST"])
def agency_login():
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        
        # STRICT LOOKUP: Only check agency_billing table
        user = User.get_from_agency(email)
        
        if not user:
            flash("Agency account not found.", "error")
            return render_template("agency-login.html", form=form)
            
        # SECURITY FIX
        if not user.password_hash or not check_password_hash(user.password_hash, form.password.data):
            flash("Incorrect password.", "error")
            return render_template("agency-login.html", form=form)
            
        login_user(user)
        return redirect(url_for("agency_dashboard"))

    return render_template("agency-login.html", form=form)

# =====================================================
# OAUTH CALLBACK (THE SORTING ENGINE)
# =====================================================
@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    if not code: return redirect(url_for('home'))

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
        
        primary_location_id = data.get('locationId')
        access_token = data['access_token']
        refresh_token = data.get('refresh_token')
        expires_in = data.get('expires_in', 86400)

        headers = {'Authorization': f'Bearer {access_token}', 'Version': '2021-07-28'}

        me_resp = requests.get("https://services.leadconnectorhq.com/users/me", headers=headers, timeout=10)
        me_data = me_resp.json() if me_resp.ok else {}
        user_email = me_data.get('email')
        
        agency_resp = requests.get("https://services.leadconnectorhq.com/agencies/", headers=headers, timeout=10)
        agencies = agency_resp.json().get('agencies', [])
        is_agency_owner = len(agencies) > 0

        locations_resp = requests.get("https://services.leadconnectorhq.com/locations/", headers=headers, timeout=15)
        sub_accounts = locations_resp.json().get('locations', [])
        num_subs = len(sub_accounts)

        conn = get_db_connection()
        if not conn: raise Exception("DB Connection failed")
        cur = conn.cursor()

        # PATH A: AGENCY OWNER -> AGENCY BILLING
        if is_agency_owner and user_email:
            plan_tier = 'agency_pro' if num_subs >= 10 else 'agency_starter'
            max_seats = 9999 if plan_tier == 'agency_pro' else 10
            
            # Upsert Agency Table
            cur.execute("""
                INSERT INTO agency_billing (
                    agency_email, subscription_tier, max_seats, active_seats,
                    location_id, access_token, refresh_token, token_expires_at,
                    crm_user_id, role
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + interval '%s seconds', %s, 'agency_owner')
                ON CONFLICT (agency_email) DO UPDATE SET
                    subscription_tier = EXCLUDED.subscription_tier,
                    active_seats = %s,
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    token_expires_at = EXCLUDED.token_expires_at,
                    updated_at = NOW();
            """, (
                user_email, plan_tier, max_seats, num_subs, 
                primary_location_id, access_token, refresh_token, expires_in, me_data.get('id'),
                num_subs
            ))

            # Populate Subscribers for Sub-Accounts
            for sub in sub_accounts:
                sub_id = sub['id']
                cur.execute("""
                    INSERT INTO subscribers (
                        location_id, email, full_name, role, parent_agency_email,
                        timezone, bot_first_name, subscription_tier
                    )
                    VALUES (%s, %s, %s, 'agency_user', %s, %s, 'Grok', %s)
                    ON CONFLICT (location_id) DO UPDATE SET
                        parent_agency_email = EXCLUDED.parent_agency_email,
                        role = 'agency_user',
                        timezone = EXCLUDED.timezone;
                """, (
                    sub_id, 
                    sub.get('email', ''), 
                    sub.get('name', 'Unknown Location'),
                    user_email,
                    sub.get('timezone', 'America/Chicago'),
                    plan_tier
                ))
            
            flash(f"Agency Connected! {num_subs} locations synced.", "success")
            target_url = "/agency-dashboard"

        # PATH B: INDIVIDUAL -> SUBSCRIBERS
        else:
            cur.execute("""
                INSERT INTO subscribers (
                    location_id, email, full_name, role,
                    access_token, refresh_token, token_expires_at,
                    crm_user_id, timezone, subscription_tier
                )
                VALUES (%s, %s, %s, 'individual', %s, %s, NOW() + interval '%s seconds', %s, %s, 'individual')
                ON CONFLICT (location_id) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    token_expires_at = EXCLUDED.token_expires_at,
                    updated_at = NOW();
            """, (
                primary_location_id, user_email, me_data.get('name', ''),
                access_token, refresh_token, expires_in,
                me_data.get('id'), 'America/Chicago'
            ))
            
            flash("Account Connected Successfully!", "success")
            target_url = "/dashboard"

        conn.commit()
        cur.close()
        conn.close()
        return redirect(target_url)

    except Exception as e:
        logger.error(f"OAuth Error: {e}")
        flash("Connection failed.", "error")
        return redirect("/")

# =====================================================
# DASHBOARDS (SEPARATED)
# =====================================================

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    # Only for Individuals or Agency Users
    if current_user.role == 'agency_owner':
        return redirect(url_for('agency_dashboard'))
    
    form = ConfigForm()
    conn = get_db_connection()
    
    # Save Config
    if form.validate_on_submit():
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE subscribers 
                    SET location_id = %s, calendar_id = %s, crm_user_id = %s,
                        bot_first_name = %s, timezone = %s, initial_message = %s, updated_at = NOW()
                    WHERE location_id = %s
                """, (
                    form.location_id.data, form.calendar_id.data, form.crm_user_id.data,
                    form.bot_name.data, form.timezone.data, form.initial_message.data,
                    current_user.location_id
                ))
                conn.commit()
                flash("Settings saved successfully!", "success")
            except Exception as e:
                conn.rollback()
                flash(f"Error saving settings: {str(e)}", "error")
            finally:
                cur.close()
                conn.close()
        return redirect(url_for('dashboard'))

    # Pre-fill
    if request.method == 'GET':
        form.location_id.data = current_user.location_id
        form.calendar_id.data = current_user.calendar_id
        form.crm_user_id.data = current_user.crm_user_id
        form.bot_name.data = current_user.bot_first_name
        form.timezone.data = current_user.timezone
        form.initial_message.data = current_user.initial_message

    access_token_display = ''
    refresh_token_display = ''
    expires_in_str = 'Not Connected'
    token_readonly = ''

    if current_user.access_token:
        token_readonly = 'readonly'
        at = current_user.access_token
        access_token_display = at[:8] + '...' + at[-4:] if len(at) > 12 else at
        
        if current_user.refresh_token:
            rt = current_user.refresh_token
            refresh_token_display = rt[:8] + '...' + rt[-4:] if len(rt) > 12 else rt

        if current_user.token_expires_at:
            # Handle str vs datetime safely
            expires_at = current_user.token_expires_at
            if isinstance(expires_at, str):
                try: expires_at = datetime.fromisoformat(expires_at)
                except: expires_at = datetime.now()
            
            delta = expires_at - datetime.now()
            if delta.total_seconds() > 0:
                expires_in_str = f"Expires in {int(delta.total_seconds()//3600)}h {int((delta.total_seconds()%3600)//60)}m"
            else:
                expires_in_str = "Token Expired"
        else:
            expires_in_str = "Persistent Connection"

    profile = {
        'full_name': current_user.full_name or '',
        'phone': current_user.phone or '',
        'bio': current_user.bio or ''
    }

    return render_template('dashboard.html',
        form=form,
        access_token_display=access_token_display,
        refresh_token_display=refresh_token_display,
        token_readonly=token_readonly,
        expires_in_str=expires_in_str,
        sub=current_user, 
        profile=profile,
        agency_seats_count=0 
    )

@app.route("/agency-dashboard")
@login_required
def agency_dashboard():
    if not current_user.is_agency_owner:
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    if not conn:
        flash("System error.", "error")
        return redirect("/dashboard")

    agency_stats = {'max_seats': 10, 'active_seats': 0, 'tier': 'Starter'}
    sub_accounts = []

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Get Billing
        cur.execute("SELECT max_seats, subscription_tier FROM agency_billing WHERE agency_email = %s", (current_user.email,))
        billing = cur.fetchone()
        if billing:
            agency_stats['max_seats'] = billing['max_seats']
            agency_stats['tier'] = billing['subscription_tier'].replace('_', ' ').title()

        # Get Sub-Accounts
        cur.execute("""
            SELECT location_id, full_name, bot_first_name, timezone, 
                   access_token, subscription_tier 
            FROM subscribers 
            WHERE parent_agency_email = %s
            ORDER BY created_at DESC
        """, (current_user.email,))
        
        rows = cur.fetchall()
        for row in rows:
            is_connected = bool(row['access_token'])
            sub_accounts.append({
                'location_id': row['location_id'],
                'name': row['full_name'] or 'Unnamed Location',
                'bot_name': row['bot_first_name'],
                'timezone': row['timezone'],
                'tier': row['subscription_tier'],
                'status': 'Active' if is_connected else 'Pending',
                'token': row['access_token'][:12] + "..." if is_connected else "Not Connected",
                'access_token': row['access_token']
            })
        agency_stats['active_seats'] = len(sub_accounts)
            
    except Exception as e:
        logger.error(f"Agency Dash Error: {e}")
    finally:
        conn.close()

    return render_template('agency_dashboard.html', 
                           sub_accounts=sub_accounts, 
                           stats=agency_stats,
                           user=current_user)

@app.route("/save-profile", methods=["POST"])
@login_required
def save_profile():
    data = request.get_json()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        if current_user.role == 'agency_owner':
            table = 'agency_billing'
            pk_col = 'agency_email'
            pk_val = current_user.email
        else:
            table = 'subscribers'
            pk_col = 'location_id'
            pk_val = current_user.location_id
            
        query = f"UPDATE {table} SET full_name = %s, phone = %s, bio = %s, updated_at = NOW() WHERE {pk_col} = %s"
        cur.execute(query, (data.get('name'), data.get('phone'), data.get('bio'), pk_val))
        
        conn.commit()
        return flask_jsonify({"status": "success"})
    except Exception as e:
        return flask_jsonify({"error": str(e)}), 500
    finally:
        conn.close()

# =====================================================
# HYBRID GET LOGS (REDIS + SQL FALLBACK)
# =====================================================
@app.route("/get-logs", methods=["GET"])
def get_logs():
    contact_id = request.args.get("contact_id")
    if not contact_id: return flask_jsonify({"logs": []}) 

    # 1. REDIS CHECK
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
        except: pass

    # 2. SQL FALLBACK (Demo/Test Only)
    if not contact_id.startswith("test_") and not contact_id.startswith("demo_"):
        return flask_jsonify({"logs": []}) 

    db_conn = get_db_connection()
    if not db_conn: return flask_jsonify({"logs": []})

    try:
        cur = db_conn.cursor(cursor_factory=RealDictCursor)
        # Fetch Messages
        cur.execute("SELECT message_type, message_text, created_at FROM contact_messages WHERE contact_id = %s ORDER BY created_at ASC", (contact_id,))
        rows = cur.fetchall()
        
        logs = []
        for r in rows:
            ts = r['created_at'].isoformat() if hasattr(r['created_at'], 'isoformat') else str(r['created_at'])
            role = "Bot" if r['message_type'] in ['assistant', 'bot'] else "Lead"
            logs.append({"role": role.lower(), "type": f"{role} Message", "content": r['message_text'], "timestamp": ts})
        
        # Fetch Narrative (if exists)
        try:
            facts = get_known_facts(contact_id)
            if facts:
                logs.append({"timestamp": datetime.now().isoformat(), "type": "Known Facts", "content": "\n".join(facts)})
                
            narrative = get_narrative(contact_id)
            if narrative:
                # Rebuild profile text if needed or just show raw narrative
                # Here we assume the narrative stored in DB is the final text
                logs.append({"timestamp": datetime.now().isoformat(), "type": "Full Human Identity Narrative", "content": narrative})
            elif facts:
                # If no narrative but facts exist, try to build it on the fly
                profile_text, _ = build_comprehensive_profile("", facts)
                logs.append({"timestamp": datetime.now().isoformat(), "type": "Full Human Identity Narrative", "content": profile_text})
        except Exception as e:
            logger.warning(f"Profile build in logs skipped: {e}")

        return safe_jsonify({"logs": logs})
    except Exception as e:
        logger.error(f"SQL Logs Error: {e}")
        return flask_jsonify({"logs": []})
    finally:
        db_conn.close()

# =====================================================
# UTILITIES & OTHER ROUTES
# =====================================================

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/")

@app.route("/")
def home():
    return render_template('home.html')

@app.route("/comparison")
def comparison():
    return render_template('comparison.html')

@app.route("/getting-started")
def getting_started():
    return render_template('getting-started.html')

@app.route("/register", methods=["GET", "POST"])
def register():
    form = RegisterForm()
    if request.method == "GET" and request.args.get('code'):
        form.code.data = request.args.get('code')
        flash("GoHighLevel connected successfully.", "success")

    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        # Default registration goes to Subscribers
        if User.get_from_subscribers(email):
            flash("Email already registered.", "error")
            return redirect("/login")

        pw_hash = generate_password_hash(form.password.data)
        if User.create(email, pw_hash):
            flash("Account created! Please log in.", "success")
            return redirect("/login")
        else:
            flash("Creation failed.", "error")

    return render_template('register.html', form=form)

# Static Pages
@app.route("/reviews", methods=["GET", "POST"])
def reviews():
    form = ReviewForm()
    if form.validate_on_submit():
        flash("Review submitted!", "success")
        return redirect(url_for('reviews'))
    
    all_reviews = [
        {"name": "Sarah J.", "role": "Agency Owner", "text": "Great tool.", "stars": 5}
    ]
    return render_template('reviews.html', reviews=all_reviews, form=form)

@app.route("/terms")
def terms(): return render_template('terms.html')

@app.route("/privacy")
def privacy(): return render_template('privacy.html')

@app.route("/disclaimers")
def disclaimers(): return render_template('disclaimers.html')

@app.route("/contact")
def contact(): return render_template('contact.html')

# === STRIPE & GHL WEBHOOKS ===
@app.route("/webhook", methods=["POST"])
def webhook():
    # Production/Demo GHL Webhook Logic
    if not q_production or not q_demo:
        return flask_jsonify({"status": "error"}), 503

    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    loc_id = payload.get("location_id") or payload.get("location", {}).get("id")
    
    is_demo = loc_id in ['DEMO_LOC', 'DEMO', 'TEST_LOCATION_456']
    target_q = q_demo if is_demo else q_production
    
    try:
        job = target_q.enqueue(process_webhook_task, payload, job_timeout=120, result_ttl=86400)
        return safe_jsonify({"status": "queued", "job_id": job.id}), 202
    except Exception as e:
        logger.error(f"Queue failed: {e}")
        return safe_jsonify({"status": "error"}), 500

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        return '', 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email = session.customer_details.email.lower()
        customer_id = session.customer
        target_role = session.metadata.get("target_role", "individual")
        target_tier = session.metadata.get("target_tier", "individual")

        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                if target_role == 'agency_owner':
                    cur.execute("""
                        INSERT INTO agency_billing (agency_email, stripe_customer_id, subscription_tier)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (agency_email) DO UPDATE SET
                        stripe_customer_id = EXCLUDED.stripe_customer_id,
                        subscription_tier = EXCLUDED.subscription_tier;
                    """, (email, customer_id, target_tier))
                else:
                    # Individual
                    temp_id = f"temp_{uuid.uuid4().hex[:8]}"
                    cur.execute("""
                        INSERT INTO subscribers (location_id, email, stripe_customer_id, role, subscription_tier)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (email) DO UPDATE SET
                        stripe_customer_id = EXCLUDED.stripe_customer_id,
                        role = EXCLUDED.role,
                        subscription_tier = EXCLUDED.subscription_tier;
                    """, (temp_id, email, customer_id, target_role, target_tier))
                conn.commit()
            finally:
                conn.close()

    return '', 200

# === DEMO CHAT ===
@app.route("/demo-chat")
def demo_chat():
    clean_id = request.args.get('session_id') or str(uuid.uuid4())
    return render_template('demo.html', clean_id=clean_id, demo_contact_id=f"demo_{clean_id}", initial_msg="Hello! Ask me about life insurance underwriting.")

@app.route('/api/demo/reset', methods=['POST'])
def demo_reset():
    # Trigger logic reset
    return flask_jsonify({"message": "Demo Reset Complete."})

@app.route("/download-transcript", methods=["GET"])
def download_transcript():
    return "Transcript Download Feature Active"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)