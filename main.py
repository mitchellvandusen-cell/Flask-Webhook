# main.py - Clean Final Version (2026)
import logging
import re
import uuid
import stripe
from openai import OpenAI
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify, session, make_response
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo
import stripe
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
from dotenv import load_dotenv
import sqlite3
from threading import Thread
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

# === IMPORTS ===
from age import calculate_age_from_dob
from prompt import build_system_prompt
from memory import save_message, save_new_facts, get_known_facts, get_narrative, get_recent_messages
from outcome_learning import classify_vibe
from ghl_message import send_sms_via_ghl
from ghl_calendar import consolidated_calendar_op
from db import get_subscriber_info, get_db_connection, init_db, User
from sync_subscribers import sync_subscribers
from individual_profile import build_comprehensive_profile
from sales_director import generate_strategic_directive
load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === INITIALIZATION ===
sync_subscribers()  # Run on startup
init_db() 

subscribers_cache = {}
cache_last_updated = None

# == SECRET SESSION ==
app.secret_key = os.getenv("SESSION_SECRET", "fallback-insecure-key")
if not app.secret_key:
    logger.warning("SESSION_SECRET not set — sessions will not work properly!")
    app.secret_key = "fallback-insecure-key"

# === API CLIENT ===
XAI_API_KEY = os.getenv("XAI_API_KEY")
client = OpenAI(base_url="https://api.x.ai/v1", api_key=XAI_API_KEY) if XAI_API_KEY else None

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
            logger.info("Google Sheet connected for dashboard writes")
        else:
            logger.warning("SUBSCRIBER_SHEET_URL not set — dashboard writes disabled")
    except Exception as e:
        logger.error(f"Google Sheet connection failed: {e}")
        worksheet = None
else:
    logger.error("GOOGLE_CREDENTIALS not set — dashboard writes disabled")

# Flask-Login Setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(user_id):
    try:
        return User.get(user_id)
    except Exception as e:
        logger.error(f"Failed to load user {user_id}: {e}")
        return None

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

@app.route("/webhook", methods=["POST"])
def webhook():
    # === STEP 1: INITIAL PAYLOAD & IDENTITY ===
    # Use fallback to form data if JSON header is missing
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    logger.info(f"FULL PAYLOAD RECIEVED: {payload}")
    
    if not payload:
        return jsonify({"status": "error", "error": "No payload received"}), 400

    # Robust ID Extraction from the 'location' object GHL automatically sends
    location_id = (
        payload.get("location", {}).get("id") or 
        payload.get("location_id") or 
        payload.get("locationId")
    )
    
    if not location_id:
        logger.error("Identity not configured for location None")
        return jsonify({"status": "error", "message": "Location ID missing"}), 400
    
    is_demo = (location_id == 'DEMO_ACCOUNT_SALES_ONLY' or location_id == 'TEST_LOCATION_456')
    contact_id = payload.get("contact_id") or payload.get("contactid") or payload.get("contact", {}).get("id") or "unknown"
    
    if is_demo:
        subscriber = {
            'bot_first_name': 'Grok',
            'crm_api_key': 'DEMO', 
            'crm_user_id': '',
            'calendar_id': '',
            'timezone': 'America/Chicago',
            'initial_message': "Hey! Quick question, are you still with that life insurance plan you mentioned before?",
            'location_id': 'DEMO'
        }
    else:
        subscriber = get_subscriber_info(location_id)
        if not subscriber or not subscriber.get('bot_first_name'):
            logger.error(f"Identity not configured for location {location_id}")
            return jsonify({"status": "error", "message": "Not configured"}), 404

    # === STEP 2: METADATA & PRE-LOAD FACTS ===
    first_name = payload.get("first_name") or ""
    dob_str = payload.get("age") or ""
    address = payload.get("address") or ""
    intent = payload.get("intent") or ""
    lead_vendor = payload.get("lead_vendor", "")
    age = calculate_age_from_dob(date_of_birth=dob_str) if dob_str else None

    # Load initial knowledge into DB
    initial_facts = []
    if first_name: initial_facts.append(f"First name: {first_name}")
    if age and age != "unknown": initial_facts.append(f"Age: {age}")
    if address: initial_facts.append(f"Address: {address}")
    if intent: initial_facts.append(f"Intent: {intent}")
    
    if initial_facts and contact_id != "unknown":
        save_new_facts(contact_id, initial_facts)

    # === STEP 3: MESSAGE EXTRACTION & IDEMPOTENCY ===
    raw_message = payload.get("message", {})
    message = raw_message.get("body", "").strip() if isinstance(raw_message, dict) else str(raw_message).strip()
    
    # Allow empty message for initiation/outreach webhooks
    if not message:
        logger.info(f"Empty message received - treating as initiation for {contact_id}")

    # Idempotency (Prevent duplicate processing)
    if not is_demo:
        message_id = payload.get("message_id") or payload.get("id")
        if message_id:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1 FROM processed_webhooks WHERE webhook_id = %s", (message_id,))
                    if cur.fetchone():
                        logger.info(f"Message {message_id} already processed. Skipping.")
                        return jsonify({"status": "success", "message": "Already processed"}), 200
                    cur.execute("INSERT INTO processed_webhooks (webhook_id) VALUES (%s)", (message_id,))
                    conn.commit()
                except Exception as e:
                    logger.error(f"Idempotency error: {e}")
                finally:
                    conn.close()

    # Save Lead Message (Synchronous)
    if message:
        save_message(contact_id, message, "lead")

    # === STEP 4: CONVERSATION LOGIC (NO THREADING) ===
    # We execute everything here to keep the Gunicorn worker alive until complete
    try:
        # 1. Identity from Subscriber
        bot_first_name = subscriber['bot_first_name']
        crm_api_key = subscriber['crm_api_key']
        timezone = subscriber.get('timezone', 'America/Chicago')
        
        # 2. CALL THE SALES DIRECTOR
        director_output = generate_strategic_directive(
            contact_id=contact_id,
            message=message,
            first_name=first_name,
            age=age,
            address=address
        )
        
        # 3. UNPACK DIRECTIVE
        profile_str = director_output["profile_str"]
        tactical_narrative = director_output["tactical_narrative"]
        current_stage = director_output["stage"]
        underwriting_ctx = director_output["underwriting_context"]
        known_facts = director_output["known_facts"]
        story_narrative = director_output["story_narrative"]
        recent_exchanges = director_output["recent_exchanges"]
        
        # 4. OPERATIONAL CHECKS
        try: vibe = classify_vibe(message).value if message else "neutral"
        except: vibe = "neutral"
        
        calendar_slots = ""
        if not is_demo and current_stage == "closing":
            calendar_slots = consolidated_calendar_op("fetch_slots", subscriber)
            
        context_nudge = ""
        if message and "covered" in message.lower(): 
            context_nudge = "Lead claims coverage."
        final_nudge = f"{context_nudge}\n{underwriting_ctx}".strip()

        # Ghost Check (Outreach / Initiation logic)
        initial_message = subscriber.get('initial_message', '').strip()
        assistant_messages = [m for m in recent_exchanges if m["role"] == "assistant"]
        
        reply = ""
        if not message and len(assistant_messages) == 0 and initial_message:
            reply = initial_message
        else:
            # 5. BUILD PROMPT
            system_prompt = build_system_prompt(
                bot_first_name=bot_first_name,
                timezone=timezone,
                profile_str=profile_str,
                tactical_narrative=tactical_narrative,
                known_facts=known_facts,
                story_narrative=story_narrative,
                stage=current_stage,
                recent_exchanges=recent_exchanges,
                message=message,
                calendar_slots=calendar_slots,
                context_nudge=final_nudge,
                lead_vendor=lead_vendor
            )

            # 6. GROK CALL
            grok_messages = [{"role": "system", "content": system_prompt}]
            for msg in recent_exchanges:
                role = "user" if msg["role"] == "lead" else "assistant"
                grok_messages.append({"role": role, "content": msg["text"]})
            
            if message:
                grok_messages.append({"role": "user", "content": message})

            try:
                response = client.chat.completions.create(
                    model="grok-4-1-fast-reasoning", # Ensure correct model name
                    messages=grok_messages,
                    temperature=0.7,
                    max_tokens=500
                )
                reply = response.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"Grok Error: {e}")
                reply = "Fair enough. Just to clarify, was it the timing that was off or something else?"

        # 7. CLEANUP & SAVE
        # Now we clean the 'reply' variable we just captured
        reply = re.sub(r'<thinking>[\s\S]*?</thinking>', '', reply)
        reply = re.sub(r'</?reply>', '', reply)
        reply = re.sub(r'<[^>]+>', '', reply).strip()
        
        # Punctuation and character normalization
        reply = reply.replace("—", ",").replace("–", ",").replace("−", ",")
        reply = reply.replace("…", "...").replace("’", "'").replace("“", '"').replace("”", '"')
        reply = reply.strip()
        
        if reply:
            save_message(contact_id, reply, "assistant")

            # 8. SEND VIA GHL
            if not is_demo and crm_api_key != 'DEMO':
                send_sms_via_ghl(contact_id, reply, crm_api_key, location_id)
                logger.info(f"Successfully sent reply to {contact_id}")

        return jsonify({"status": "success", "reply": reply}), 200
    except Exception as e:
        logger.error(f"Critical error in processing: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
                    
@app.route("/")
def home():
    home_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>InsuranceGrokBot | AI Lead Re-engagement</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">
    <meta name="description" content="The most advanced AI SMS bot for life insurance lead re-engagement. Powered by Grok.">
    <meta name="theme-color" content="#00ff88">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0f0f0f; --text-secondary: #aaa; --neon-glow: 0 0 30px rgba(0, 255, 136, 0.4); --red-x: #ff4444; --green-check: #00ff88; }
        body { background: var(--dark-bg); color: #fff; font-family: 'Montserrat', sans-serif; line-height: 1.7; }
        .navbar { background: rgba(0,0,0,0.95); backdrop-filter: blur(10px); }
        .navbar-brand { font-weight: 700; font-size: 1.6rem; color: #fff !important; margin-right: 15px !important; }
        .highlight { color: var(--accent); text-shadow: var(--neon-glow); }
        .navbar-nav { align-items: center !important; gap 2px; }
        .nav-item, .btn-nav { height: 50px !important; display: flex; align-items: center; }
        .nav-link { color: #ddd !important; font-weight: 700 !important; font-size: 0.8rem !important; text-transform: uppercase; letter-spacing: 0.5px; padding 0 12 px !important; height: 40px; white-space: nowrap !important; display: flex; align-items: center; transition: color 0.3s ease; border-radius: 4px; }
        .nav-link:hover { color: var(--accent) !important; background: rgba(255, 255, 255, 0.05); text-shadow: var(--neon-glow); }
        .nav-btn-group { display: flex; align-items: center; gap: 10px; margin-left: 15px; }
        .btn-nav { width: 120px !important; !important; display: flex !important; align-items: center; justify-content: center; font-weight: 800 !important; text-transform: uppercase; font-size: 0.9rem !important; padding: 0 !important; text-decoration: none; border-radius: 4px !important; transition: all 0.3s ease; }
        .btn-login-custom:hover { background: #111; border: 1px solid #333; color: var(--accent) !important; }
        .btn-login-custom { background: #000; border 1px solid #333; color: #fff !important; }
        .btn-signup-custom { background: var(--accent); border 2px solid var(--accent); color: #000 !important; box-shadow: var(--neon-glow); }
        .btn-signup-custom:hover { background: #00cc6a; border-color: #00cc6a; transform: translateY(-2px); box-shadow: 0 0 40px rhba(0, 255, 136, 0.6); }
        .btn-primary { display: inline-block; background: #00ff88; color: #000; font-weight: 700; font-size: 1.6rem; padding: 18px 50px; border-radius: 50px; box-shadow: 0 6px 20px rgba(0, 255, 136, 0.3); text-decoration: none; transition: all 0.3s ease; border: none; letter-spacing: 0.5px; }
        .btn-primary:hover { background: #00ee80; box-shadow: 0 12px 30px rgba(0, 255, 136, 0.5); transform: translateY(-4px); }
        .hero { padding: 140px 20px 100px; text-align: center; min-height: 100vh; display: flex; align-items: center; justify-content: center; background: radial-gradient(circle at center, #111 0%, #000 80%); }
        .hero h1 { font-size: 3.5rem; font-weight: 700; line-height: 1.2; margin-bottom: 30px; text-shadow: var(--neon-glow); }
        .section { padding: 100px 20px; }
        .section-title { font-size: 3rem; font-weight: 700; text-align: center; margin-bottom: 80px; color: var(--accent); text-shadow: var(--neon-glow); }
        .feature-card { background: var(--card-bg); border-radius: 20px; padding: 40px; text-align: center; box-shadow: 0 10px 30px rgba(0,0,0,0.5); transition: all 0.4s; height: 100%; }
        .feature-card:hover { transform: translateY(-15px); box-shadow: 0 20px 50px rgba(0, 255, 136, 0.3); }
        .feature-card h3 { font-size: 1.8rem; margin-bottom: 20px; color: var(--accent); }
        .comparison-wrapper { max-width: 1000px; margin: 0 auto; background: var(--card-bg); border-radius: 20px; overflow: hidden; box-shadow: 0 15px 40px rgba(0,0,0,0.6); }
        .comparison-table { width: 100%; border-collapse: collapse; }
        .comparison-table th { padding: 30px; font-size: 1.8rem; font-weight: 700; text-align: center; }
        .comparison-table td { padding: 25px 20px; vertical-align: middle; font-size: 1.3rem; border-bottom: 1px solid #222; }
        .feature-col { text-align: left; padding-left: 40px; }
        .standard-col, .grok-col { text-align: center; font-size: 3.5rem; }
        .check { color: var(--green-check); }
        .cross { color: var(--red-x); }
        .sales-logic { background: var(--card-bg); border-radius: 20px; padding: 60px; box-shadow: 0 15px 40px rgba(0,0,0,0.6); max-width: 1000px; margin: 0 auto; }
        .sales-logic h3 { color: var(--accent); font-size: 2rem; margin-bottom: 20px; }
        .pricing-card { background: linear-gradient(135deg, #111, #000); border: 2px solid var(--accent); border-radius: 30px; padding: 60px; text-align: center; max-width: 600px; margin: 0 auto; box-shadow: 0 20px 60px rgba(0, 255, 136, 0.3); }
        .price { font-size: 6rem; font-weight: 700; color: var(--accent); text-shadow: var(--neon-glow); }
        /* Hamburger Menu Styling */
        .navbar-toggler { border: 1px solid var(--accent); padding: 8px !impportant; background: rgba(0, 255, 136, 0.1) !important; }
        .navbar-toggler-icon {
            background-image: none !important;
            display: flex;
            flex-direction: column;
            justify-content: space-around;
            width: 25px;
            height: 18px;
        }
        .navbar-toggler-icon::before,
        .navbar-toggler-icon::after,
        .navbar-toggler-icon span {
            display: block;
            width: 100%;
            height: 3px;
            background-color: var(--accent) !important;
            border-radius: 10px;
            transition: all 0.3s ease;
        }
        /* Custom Dropdown for Auth Users */
        .auth-dropdown {
            background: transparent;
            border: none;
            color: var(--accent);
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0 10px;
            display: flex;
            align-items: center;
        }

        .dropdown-menu-dark {
            background-color: #0a0a0a !important;
            border: 1px solid #333 !important;
            box-shadow: var(--neon-glow);
            margin-top: 15px !important;
        }

        .dropdown-item {
            color: #fff !important;
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.8rem;
            padding: 10px 20px !important;
        }

        .dropdown-item:hover {
            background-color: #111 !important;
            color: var(--accent) !important;
        }
        footer { padding: 80px 20px; text-align: center; color: var(--text-secondary); border-top: 1px solid #222; }
        @media (max-width: 768px) {
            .navbar-collapse { background: #111; padding: 20px; border-radius: 15px; border: 1px solid var(--accent); margin-top: 15px; box-shadow: var(--neon-glow); }
            .nav-item { width: 100%; text-align: center; margin: 10px 0; border-bottom: 1px solid #222; }
            .navbar-nav { font-size: 0.7rem; }
            .nav-link { padding: 0 5px !imporant; }
            .nav-btn-group { flex-direction: column; width: 100% margin-left: 0; gap: 15px; }
            .btn-nav { width: 100% !important; }
            .hero h1 { font-size: 2.8rem; }
            .hero p.lead { font-size: 1.4rem; }
            .btn-primary { font-size: 1.4rem; padding: 18px 40px; }
            .comparison-table thead { display: none; }
            .comparison-table tr { display: block; margin: 25px 0; background: #111; border-radius: 15px; padding: 25px; }
            .comparison-table td { display: block; text-align: center; padding: 12px 0; border: none; }
            .feature-col { text-align: center; font-weight: bold; font-size: 1.4rem; margin-bottom: 20px; padding-left: 0; }
            .standard-col::before { content: "Standard Bots: "; font-weight: bold; color: var(--red-x); display: block; margin-bottom: 10px; }
            .grok-col::before { content: "InsuranceGrokBot: "; font-weight: bold; color: var(--accent); display: block; margin-bottom: 10px; }
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg fixed-top">
        <div class="container">
            <a class="navbar-brand" href="/">INSURANCE<span class="highlight">GROK</span>BOT</a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon" style="filter: invert(1);"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav align-items-center">
                    <li class="nav-item"><a href="#features" class="nav-link">Features</a></li>
                    <li class="nav-item"><a href="#comparison" class="nav-link">Why GrokBot Wins</a></li>
                    <li class="nav-item"><a href="#logic" class="nav-link">Sales Logic</a></li>
                    <li class="nav-item"><a href="#pricing" class="nav-link">Pricing</a></li>
                    <li class="nav-item"><a href="/getting-started" class="nav-link">Getting Started</a></li>
                    <li class="nav-item"><a href="/demo-chat" class="nav-link">Live Demo</a></li>
                </ul>

                {% if current_user.is_authenticated %}
                    <div class="dropdown">
                        <button class="auth-dropdown" type="button" id="authMenu" data-bs-toggle="dropdown" aria-expanded="false">
                            <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <line x1="3" y1="12" x2="21" y2="12"></line>
                                <line x1="3" y1="6" x2="21" y2="6"></line>
                                <line x1="3" y1="18" x2="21" y2="18"></line>
                            </svg>
                        </button>
                        <ul class="dropdown-menu dropdown-menu-end dropdown-menu-dark" aria-labelledby=:authMenu">
                            <li class="dropdown-item"><a href="/dashboard">Dashboard</a></li>
                            <li><hr class="dropdown-divider" style="border-color: #222;"></li>
                            <li class="dropdown-item"><a href="/logout">Logout</a></li>
                        </ul>
                    </div>
                {% else %}
                    <div class="nav-btn-group d-none d-sm-flex">
                        <a href="/login" class="nav-link me-2" style="font-size: 0.8rem;">Log In</a>
                        <a href="/register" class="btn-nav btn-signup-custom" style="padding: 5px 15px !important; height: 35px !important; font-size: 0.7rem !important;">Sign Up</a>
                    </div>
                {% endif %}
            </div>
        </div>
    </nav>

    <section class="hero">
        <div class="container">
            <h1>The Most Advanced Life Insurance<br>Lead Re-engagement AI Ever Built</h1>
            <p class="lead">Powered by xAI's Grok. Trained on thousands of real insurance conversations.<br>Books appointments from leads that have been cold for months.</p>
            <div class="text-center mt-5">
                <a href="/checkout" class="btn-primary">Subscribe Now $100/mth</a>
                <p class="mt-3">
                    <a href="/demo-chat" style="color:#888; text-decoration:underline; font-size:1.4rem;">
                        Or try the live demo first →
                    </a>
                </p>
                <p class="mt-3 text-secondary"><small>No contract. Cancel anytime. Instant activation.</small></p>
            </div>
        </div>
    </section>

    <section id="features" class="section">
        <div class="container">
            <h2 class="section-title">What Makes InsuranceGrokBot Different</h2>
            <div class="row g-5">
                <div class="col-md-4">
                    <div class="feature-card">
                        <h3>Real Human Memory</h3>
                        <p>Remembers every fact from every message across the entire conversation. Never repeats questions. Builds a complete profile over time.</p>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="feature-card">
                        <h3>5 Proven Sales Frameworks</h3>
                        <p>Blends NEPQ, Gap Selling, Straight Line Persuasion, Never Split the Difference, and Psychology of Selling in real time based on lead responses.</p>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="feature-card">
                        <h3>Extensive Underwriting Knowledge</h3>
                        <p>Trained on carrier guidelines, health conditions, and build charts. Knows when a lead is likely insurable and asks the right questions.</p>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="feature-card">
                        <h3>Never Gives Up</h3>
                        <p>Most bots stop at "no". GrokBot loops, reframes, and persists until the lead either books or truly has no need.</p>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="feature-card">
                        <h3>Only Books Qualified Leads</h3>
                        <p>Won't waste your time with appointments from leads who have no gap or aren't interested. Only schedules when there's real potential.</p>
                    </div>
                </div>
                <div class="col-md-4">
                    <div class="feature-card">
                        <h3>Multi-Tenant Ready</h3>
                        <p>Agencies can manage hundreds of agents with complete data isolation and custom identities per location.</p>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <section id="comparison" class="section bg-black">
        <div class="container">
            <h2 class="section-title">Why InsuranceGrokBot Dominates</h2>
            <div class="table-responsive">
                <table class="comparison-table">
                    <thead>
                        <tr>
                            <th class="feature-col">Feature</th>
                            <th class="standard-col">Standard Bots</th>
                            <th class="grok-col">InsuranceGrokBot</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td class="feature-col">Real-time reasoning with Grok</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                        <tr>
                            <td class="feature-col">5 blended sales frameworks (NEPQ, Gap Selling, etc.)</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                        <tr>
                            <td class="feature-col">Full underwriting & health knowledge</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                        <tr>
                            <td class="feature-col">Persistent memory & Narrative Observer</td>
                            <td class="standard-col">Limited</td>
                            <td class="grok-col">Complete</td>
                        </tr>
                        <tr>
                            <td class="feature-col">Handles complex objections emotionally</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </section>

    <section id="logic" class="section">
        <div class="container">
            <h2 class="section-title">Master-Level Sales Logic Built In</h2>
            <div class="sales-logic">
                <div class="row">
                    <div class="col-md-6 mb-5">
                        <h3>Jeremy Miner’s NEPQ</h3>
                        <p>Neuro-Emotional Persuasion Questioning. The bot asks problem-awareness questions that make leads persuade themselves.</p>
                    </div>
                    <div class="col-md-6 mb5">
                        <h3>Never Split the Difference</h3>
                        <p>Chris Voss negotiation tactics. Uses calibrated questions, labels, and mirrors to handle objections.</p>
                    </div>
                    <div class="col-md-6 mb-5">
                        <h3>Jordan Belfort’s Straight Line</h3>
                        <p>Maintains control of the conversation, loops back to benefits, and builds certainty.</p>
                    </div>
                    <div class="col-md-6 mb-5">
                        <h3>Gap Selling + Psychology of Selling</h3>
                        <p>Identifies the gap between current and desired state while using emotional drivers.</p>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <section id="pricing" class="section bg-black">
        <div class="container text-center">
            <h2 class="section-title">Simple, Transparent Pricing</h2>
            <div class="pricing-card">
                <div class="price">$100<span style="font-size:2rem;">/mth</span></div>
                <p style="font-size:1.7rem; margin:30px 0;">Early Adopter Rate</p>
                <ul style="text-align:left; max-width:400px; margin:30px auto; font-size:1.4rem;">
                    <li>Unlimited conversations</li>
                    <li>Full narrative memory</li>
                    <li>All 5 sales frameworks</li>
                    <li>Calendar booking</li>
                    <li>Multi-tenant support</li>
                </ul>
                <a href="/checkout" class="btn-primary">Subscribe Now</a>
                <p class="mt-4 text-secondary">No contract. Cancel anytime.</p>
            </div>
        </div>
    </section>

    <footer>
        <div class="container">
            <p>&copy; 2026 InsuranceGrokBot.</p>
            <p><a href="/terms" style="color:var(--text-secondary);">Terms</a> • <a href="/privacy" style="color:var(--text-secondary);">Privacy</a></p>
        </div>
    </footer>
</body>
</html>
    """
    return render_template_string(home_html)

@app.route("/getting-started")
def getting_started():
    getting_started_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Launch Sequence | InsuranceGrokBot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <style>
        :root { 
            --accent: #00ff88; 
            --dark-bg: #000; 
            --card-bg: #0a0a0a; 
            --neon-glow: 0 0 30px rgba(0, 255, 136, 0.4); 
        }
        
        body { 
            background: var(--dark-bg); 
            color: #fff; 
            font-family: 'Montserrat', sans-serif; 
            line-height: 1.6; 
        }

        /* Paragraphs and List Text specifically White */
        p, .step-text { 
            color: #ffffff !important; 
            font-size: 1.05rem;
            letter-spacing: 0.2px;
        }

        /* HYBRID NAV */
        .navbar { 
            background: rgba(0,0,0,0.95); 
            backdrop-filter: blur(10px); 
            border-bottom: 1px solid #222; 
        }
        .navbar-brand { font-weight: 700; color: #fff !important; text-decoration: none; }
        .highlight { color: var(--accent); text-shadow: var(--neon-glow); }
        
        .nav-link { color: #fff !important; font-weight: 700; text-transform: uppercase; font-size: 0.8rem; }
        .nav-link:hover { color: var(--accent) !important; }

        /* HAMBURGER MENU */
        .auth-dropdown { 
            background: transparent; 
            border: none; 
            color: var(--accent); 
            cursor: pointer; 
            padding: 0 10px; 
            display: flex; 
            align-items: center; 
        }
        .dropdown-menu-dark { 
            background-color: #000 !important; 
            border: 1px solid var(--accent) !important; 
            box-shadow: var(--neon-glow); 
            margin-top: 15px !important; 
        }
        .dropdown-item { color: #fff !important; text-transform: uppercase; font-weight: 700; font-size: 0.8rem; }
        .dropdown-item:hover { background: #111 !important; color: var(--accent) !important; }

        /* PATH CARDS */
        .card-path { 
            background: var(--card-bg); 
            border: 2px solid #1a1a1a; 
            border-radius: 30px; 
            padding: 50px; 
            height: 100%; 
            transition: all 0.4s ease;
        }
        .card-path:hover { 
            border-color: var(--accent); 
            box-shadow: var(--neon-glow);
            transform: translateY(-5px);
        }

        .step-item { display: flex; align-items: flex-start; margin-bottom: 22px; }
        .step-num { 
            font-weight: 800; 
            color: var(--accent); 
            font-size: 1.2rem; 
            min-width: 50px; 
            font-family: monospace;
        }

        h3 { color: var(--accent); font-weight: 800; text-transform: uppercase; margin-bottom: 30px; }

        /* BUTTONS */
        .btn-launch { 
            display: block; 
            width: 100%; 
            text-align: center; 
            padding: 20px; 
            border-radius: 50px; 
            font-weight: 800; 
            text-transform: uppercase; 
            text-decoration: none; 
            transition: 0.3s; 
            margin-top: 30px;
            font-size: 1.1rem;
        }
        .btn-mkt { background: #fff; color: #000; }
        .btn-web { background: var(--accent); color: #000; box-shadow: var(--neon-glow); }
        .btn-web:hover { transform: scale(1.03); }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg fixed-top">
        <div class="container">
            <a class="navbar-brand" href="/">INSURANCE<span class="highlight">GROK</span>BOT</a>
            <div class="d-flex align-items-center ms-auto">
                <ul class="navbar-nav d-flex flex-row me-3">
                    <li class="nav-item"><a href="/#features" class="nav-link px-3">Features</a></li>
                    <li class="nav-item"><a href="/getting-started" class="nav-link px-3 highlight">Get Started</a></li>
                </ul>

                {% if current_user.is_authenticated %}
                <div class="dropdown">
                    <button class="auth-dropdown" type="button" id="authMenu" data-bs-toggle="dropdown">
                        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                            <line x1="3" y1="12" x2="21" y2="12"></line>
                            <line x1="3" y1="6" x2="21" y2="6"></line>
                            <line x1="3" y1="18" x2="21" y2="18"></line>
                        </svg>
                    </button>
                    <ul class="dropdown-menu dropdown-menu-end dropdown-menu-dark">
                        <li><a class="dropdown-item" href="/dashboard">Dashboard</a></li>
                        <li><hr class="dropdown-divider" style="border-color: #333;"></li>
                        <li><a class="dropdown-item text-danger" href="/logout">Logout</a></li>
                    </ul>
                </div>
                {% endif %}
            </div>
        </div>
    </nav>

    <div class="container" style="padding-top: 160px; padding-bottom: 100px;">
        <h1 class="text-center mb-5" style="font-weight: 800; font-size: 3.5rem; letter-spacing: -2px;">SELECT YOUR <span class="highlight">ENTRY</span></h1>
        
        <div class="row g-5">
            <div class="col-lg-6">
                <div class="card-path">
                    <h3>Marketplace Integration</h3>
                    <p class="mb-5">Direct GHL authorization for agencies.</p>
                    
                    <div class="step-item">
                        <div class="step-num">01</div>
                        <div class="step-text">Open the <strong>GHL Marketplace</strong>.</div>
                    </div>
                    <div class="step-item">
                        <div class="step-num">02</div>
                        <div class="step-text">Search for <strong>Insurance Grok Bot</strong>.</div>
                    </div>
                    <div class="step-item">
                        <div class="step-num">03</div>
                        <div class="step-text">Execute <strong>Install</strong> to bridge your sub-account.</div>
                    </div>
                    <div class="step-item">
                        <div class="step-num">04</div>
                        <div class="step-text">Secure your unique 8-digit <strong>Activation Code</strong>.</div>
                    </div>
                    <div class="step-item">
                        <div class="step-num">05</div>
                        <div class="step-text">Complete registration with your <strong>Email + Code</strong>.</div>
                    </div>
                    
                    <a href="https://marketplace.gohighlevel.com/" class="btn-launch btn-mkt">Marketplace Setup</a>
                </div>
            </div>

            <div class="col-lg-6">
                <div class="card-path">
                    <h3>Direct Activation</h3>
                    <p class="mb-5">Standard setup for independent high-volume closers.</p>
                    
                    <div class="step-item">
                        <div class="step-num">01</div>
                        <div class="step-text">Hit <strong>Subscribe Now</strong> to secure your license.</div>
                    </div>
                    <div class="step-item">
                        <div class="step-num">02</div>
                        <div class="step-text">Complete checkout via <strong>Stripe</strong>.</div>
                    </div>
                    <div class="step-item">
                        <div class="step-num">03</div>
                        <div class="step-text">Create your <strong>Secure Password</strong>.</div>
                    </div>
                    <div class="step-item">
                        <div class="step-num">04</div>
                        <div class="step-text">Access the <strong>Intelligence Dashboard</strong>.</div>
                    </div>
                    <div class="step-item">
                        <div class="step-num">05</div>
                        <div class="step-text">Input your <strong>CRM Keys</strong> to sync the bot.</div>
                    </div>
                    
                    <a href="/checkout" class="btn-launch btn-web">Start Subscription</a>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""
    return render_template_string(getting_started_html)


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("ENDPOINT_SECRET")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except:
        return '', 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session.customer
        email = session.customer_details.email.lower() if session.customer_details.email else None

        if email and customer_id:
            user = User.get(email)
            if not user:
                User.create(email, password_hash=None, stripe_customer_id=customer_id)
                logger.info(f"Created paid user {email} (password pending)")
            else:
                conn = get_db_connection()
                conn.execute("UPDATE users SET stripe_customer_id = ? WHERE email = ?", (customer_id, email))
                conn.commit()
                conn.close()

            # Write to Sheet
            if worksheet:
                try:
                    values = worksheet.get_all_values()
                    header = values[0] if values else []
                    header_lower = [h.strip().lower() for h in header]

                    def col_index(name):
                        try:
                            return header_lower.index(name.lower())
                        except ValueError:
                            new_col = len(header) + 1
                            worksheet.update_cell(1, new_col, name)
                            header.append(name)
                            header_lower.append(name.lower())
                            return new_col - 1

                    email_idx = col_index("Email")
                    stripe_idx = col_index("stripe_customer_id")

                    row_num = None
                    for i, row in enumerate(values[1:], start=2):
                        if len(row) > email_idx and row[email_idx].strip().lower() == email:
                            row_num = i
                            break

                    if row_num:
                        worksheet.update_cell(row_num, stripe_idx + 1, customer_id)
                    else:
                        new_row = [""] * len(header)
                        new_row[email_idx] = email
                        new_row[stripe_idx] = customer_id
                        worksheet.append_row(new_row)
                except Exception as e:
                    logger.error(f"Sheet Stripe save failed: {e}")

    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = subscription.customer
        # Logic to remove user or mark inactive would go here
        pass

    return '', 200

@app.route("/register", methods=["GET", "POST"])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        code = form.code.data.upper().strip() if form.code.data else ""

        if User.get(email):
            flash("Email already registered.", "error")
            return redirect("/login")

        is_valid = False
        used_code_row = None

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
                        if len(row) > code_idx and row[code_idx].strip().upper() == code:
                            if used_idx != -1 and len(row) > used_idx and row[used_idx] == "1":
                                flash("Code already used.", "error")
                                return redirect("/register")
                            used_code_row = i
                            is_valid = True
                            break
                    if is_valid and used_code_row and used_idx != -1:
                        worksheet.update_cell(used_code_row, used_idx + 1, "1")
                        # LINK EMAIL TO ROW: This bridges the GHL gap
                        if email_idx == -1: # Add column if missing
                            new_col = len(values[0]) + 1
                            worksheet.update_cell(1, new_col, "email")
                            email_idx = new_col - 1
                        worksheet.update_cell(used_code_row, email_idx + 1, email)
            except Exception as e:
                logger.error(f"Code validation error: {e}")

        if not is_valid:
            flash("Invalid code or no subscription.", "error")
            return redirect("/register")

        password_hash = generate_password_hash(form.password.data)
        if User.create(email, password_hash):
            flash("Account created! Log in.", "success")
            return redirect("/login")
        else:
            flash("Creation failed.", "error")

    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Register</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; text-align:center; padding:60px 20px; }
        .container { max-width:600px; margin:0 auto; background:var(--card-bg); padding:60px; border-radius:20px; border:1px solid #333; }
        h1 { color:var(--accent); margin-bottom:40px; }
        input { width:100%; padding:20px; margin:10px 0; background:#111; border:1px solid #333; color:#fff; border-radius:12px; }
        button { padding:20px 60px; background:var(--accent); color:#000; font-weight:700; border:none; border-radius:50px; cursor:pointer; margin-top:20px; }
        .flash { padding:15px; margin-bottom:20px; background:#222; border-left:5px solid var(--accent); }
    </style>
</head>
<body>
    <div class="container">
        <h1>Create Account</h1>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="post">
            {{ form.hidden_tag() }}
            {{ form.email(placeholder="Email") }}
            {{ form.code(placeholder="Confirmation Code (if from GHL)") }}
            {{ form.password(placeholder="Password") }}
            {{ form.confirm(placeholder="Confirm Password") }}
            {{ form.submit() }}
        </form>
        <p style="margin-top:30px;"><a href="/login" style="color:var(--accent);">Already have an account? Log in</a></p>
    </div>
</body>
</html>
    """, form=form)

@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.get(form.email.data.lower())
        if user and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            return redirect("/dashboard")
        flash("Invalid credentials", "error")

    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; text-align:center; padding:60px 20px; }
        .container { max-width:600px; margin:0 auto; background:var(--card-bg); padding:60px; border-radius:20px; border:1px solid #333; }
        h1 { color:var(--accent); margin-bottom:40px; }
        input { width:100%; padding:20px; margin:10px 0; background:#111; border:1px solid #333; color:#fff; border-radius:12px; }
        button { padding:20px 60px; background:var(--accent); color:#000; font-weight:700; border:none; border-radius:50px; cursor:pointer; margin-top:20px; }
        .flash { padding:15px; margin-bottom:20px; background:#222; border-left:5px solid #ff4444; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Log In</h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="post">
            {{ form.hidden_tag() }}
            {{ form.email(placeholder="Email") }}
            {{ form.password(placeholder="Password") }}
            {{ form.submit() }}
        </form>
        <p style="color: #aaa; margin-top:30px;"><a href="/register" style="color:var(--accent);">Need an account? Register</a></p>
    </div>
</body>
</html>
    """, form=form)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/")

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    global worksheet

    form = ConfigForm()

    # Get all values from sheet (safe)
    values = worksheet.get_all_values() if worksheet else []
    if not values:
        headers = ["email", "location_id", "calendar_id", "crm_api_key", "crm_user_id", "bot_first_name", "timezone", "initial_message", "stripe_customer_id", "confirmation_code", "code_used"]
        if worksheet:
            worksheet.append_row(headers)
        values = [headers]

    header = values[0] if values else []
    header_lower = [h.strip().lower() for h in header]

    def col_index(name):
        try:
            return header_lower.index(name.lower())
        except ValueError:
            return -1

    email_idx = col_index("email")
    location_idx = col_index("location_id")
    calendar_idx = col_index("calendar_id")
    api_key_idx = col_index("crm_api_key")
    user_id_idx = col_index("crm_user_id")
    bot_name_idx = col_index("bot_first_name")
    timezone_idx = col_index("timezone")
    initial_msg_idx = col_index("initial_message")
    stripe_idx = col_index("stripe_customer_id")

    # Find user's row
    user_row_num = None
    for i, row in enumerate(values[1:], start=2):
        if email_idx >= 0 and len(row) > email_idx and row[email_idx].strip().lower() == current_user.email.lower():
            user_row_num = i
            break

    if form.validate_on_submit() and worksheet:
        data = [
            current_user.email,
            form.location_id.data or "",
            form.calendar_id.data or "",
            form.crm_api_key.data or "",
            form.crm_user_id.data or "",
            form.bot_name.data or "Grok",
            form.timezone.data or "America/Chicago",
            form.initial_message.data or "",
            current_user.stripe_customer_id or ""
        ]

        try:
            if user_row_num:
                # Update only the columns we manage, leaving others (like code) intact
                # Note: This is simplified; in production, you'd map columns precisely
                worksheet.update(f"A{user_row_num}:I{user_row_num}", [data])
            else:
                worksheet.append_row(data)
            sync_subscribers()
            flash("Settings saved and bot updated instantly!", "success")
        except Exception as e:
            logger.error(f"Sheet write failed: {e}")
            flash("Error saving settings", "error")

        return redirect("/dashboard")

    # Pre-fill form
    if user_row_num and values:
        row = values[user_row_num - 1]
        if location_idx >= 0 and len(row) > location_idx: form.location_id.data = row[location_idx]
        if calendar_idx >= 0 and len(row) > calendar_idx: form.calendar_id.data = row[calendar_idx]
        if api_key_idx >= 0 and len(row) > api_key_idx: form.crm_api_key.data = row[api_key_idx]
        if user_id_idx >= 0 and len(row) > user_id_idx: form.crm_user_id.data = row[user_id_idx]
        if bot_name_idx >= 0 and len(row) > bot_name_idx: form.bot_name.data = row[bot_name_idx]
        if timezone_idx >= 0 and len(row) > timezone_idx: form.timezone.data = row[timezone_idx]
        if initial_msg_idx >= 0 and len(row) > initial_msg_idx: form.initial_message.data = row[initial_msg_idx]

    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - InsuranceGrokBot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; padding:40px 20px; min-height:100vh; }
        .container { max-width:900px; margin:auto; }
        h1 { color:var(--accent); font-size:3.5rem; text-shadow:var(--neon-glow); text-align:center; margin-bottom:20px; }
        .welcome { text-align:center; font-size:1.8rem; margin-bottom:40px; }
        .logout { position:absolute; top:20px; right:20px; color:var(--accent); font-size:1.4rem; text-decoration:underline; }
        .nav-tabs { border-bottom:1px solid #333; margin-bottom:40px; }
        .nav-tabs .nav-link { color:#aaa; border-color:#333; font-size:1.6rem; padding:15px 30px; }
        .nav-tabs .nav-link.active { color:var(--accent); background:#111; border-color:var(--accent) var(--accent) #111; }
        .tab-content { margin-top:30px; }
        .form-group { margin:30px 0; }
        label { display:block; margin-bottom:10px; font-size:1.4rem; color:#ddd; }
        input { width:100%; padding:16px; background:#111; border:1px solid #333; color:#fff; border-radius:12px; font-size:1.4rem; }
        input::placeholder { color:#888; }
        button { padding:18px 50px; background:var(--accent); color:#000; border:none; border-radius:50px; font-size:1.8rem; cursor:pointer; box-shadow:var(--neon-glow); margin-top:20px; }
        button:hover { background:#00cc70; transform:scale(1.05); }
        .alert { padding:20px; background:#1a1a1a; border-radius:12px; margin:20px 0; font-size:1.4rem; }
        .alert-success { border-left:5px solid var(--accent); }
        .alert-error { border-left:5px solid #ff6b6b; }
        .card { background:var(--card-bg); border:1px solid #333; border-radius:15px; padding:40px; margin:30px 0; box-shadow:0 10px 30px var(--neon-glow); }
        .guide-text h3 { color:var(--accent); margin:40px 0 20px; font-size:2rem; }
        .guide-text li { color:#ddd; margin:15px 0; font-size:1.4rem; }
        code { background:#222; padding:6px 12px; border-radius:8px; color:var(--accent); font-family:monospace; }
        .back { text-align:center; margin-top:80px; }
        .back a { color:#888; font-size:1.6rem; text-decoration:underline; }
        @media (max-width: 768px) {
            h1 { font-size:2.8rem; }
            .welcome { font-size:1.6rem; }
            .nav-tabs .nav-link { font-size:1.4rem; padding:12px 20px; }
            .form-group { margin:25px 0; }
            label { font-size:1.3rem; }
            input { font-size:1.3rem; }
            button { font-size:1.6rem; padding:16px 40px; }
            .alert { font-size:1.3rem; }
            .card { padding:30px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <a href="/logout" class="logout">Logout</a>
        <h1>Dashboard</h1>
        <p class="welcome">Welcome back, <strong>{{ current_user.email }}</strong></p>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert {{ 'alert-success' if category == 'success' else 'alert-error' }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <ul class="nav nav-tabs justify-content-center mb-5">
            <li class="nav-item">
                <a class="nav-link active" data-bs-toggle="tab" href="#config">Configuration</a>
            </li>
            <li class="nav-item">
                <a class="nav-link" data-bs-toggle="tab" href="#guide">GHL Setup Guide</a>
            </li>
            <li class="nav-item">
                <a class="nav-link" data-bs-toggle="tab" href="#billing">Billing</a>
            </li>
        </ul>

        <div class="tab-content">
            <div class="tab-pane active" id="config">
                <div class="card">
                    <h2 style="color:var(--accent); text-align:center;">Configure Your Bot</h2>
                    <form method="post">
                        {{ form.hidden_tag() }}

                        <div class="form-group">
                            {{ form.location_id.label }}
                            {{ form.location_id(class="form-control", placeholder="e.g. k7lOZdwaMruhP") }}
                        </div>

                        <div class="form-group">
                            {{ form.calendar_id.label }}
                            {{ form.calendar_id(class="form-control", placeholder="e.g. S4KnucrFaXO76") }}
                        </div>

                        <div class="form-group">
                            {{ form.crm_api_key.label }}
                            {{ form.crm_api_key(class="form-control", placeholder="e.g. pit-ae0fh932-a8c") }}
                        </div>

                        <div class="form-group">
                            {{ form.crm_user_id.label }}
                            {{ form.crm_user_id(class="form-control", placeholder="e.g. BhWQCdIwX0C, required for calendar") }}
                        </div>

                        <div class="form-group">
                            {{ form.timezone.label }}
                            {{ form.timezone(class="form-control", placeholder="e.g. America/Chicago") }}
                        </div>

                        <div class="form-group">
                            {{ form.bot_name.label }}
                            {{ form.bot_name(class="form-control", placeholder="e.g. Mitch") }}
                        </div>

                        <div class="form-group">
                            {{ form.initial_message.label }}
                            {{ form.initial_message(class="form-control", placeholder="Optional custom first message") }}
                        </div>

                        <div style="color:var(--accent); text-align:center; margin-top:50px;">
                            {{ form.submit(class="button") }}
                        </div>
                    </form>
                </div>
            </div>

            <div class="tab-pane fade" id="guide">
                <div class="card guide-text">
                    <h2 style="color:var(--accent); text-align:center;">GoHighLevel Setup Guide</h2>
                    <p style="color #fff; text-align:center; margin-bottom:30px;">Follow these steps to connect InsuranceGrokBot to your GHL account</p>
                    {% raw %}
                    <div style="text-align:left;">
                        <h3 style="color:var(--accent);">Step 1: Create "Re-engage Leads" Workflow</h3>
                        <ol>
                            <li>Go to <strong>Automations, Workflows, Create Workflow</strong></li>
                            <li><strong>Trigger</strong>: Tag Applied (create a tag like "Re-engage text")</li>
                            <li>Add <strong>Wait</strong>: 5 to 30 minutes</li>
                            <li>Add <strong>Webhook</strong>:
                                <ul>
                                    <li>URL: <code>https://insurancegrokbot.click/webhook</code></li>
                                    <li>Method: POST</li>
                                    <li>Body fields (use correct crm "{{}}"):
                                        <ul>
                                            <li><code>intent</code>: "the intent of the message"</li>
                                            <li><code>first_name</code>: "{{contact.first_name}}"</li>
                                            <li><code>age</code>: "{{contact.date_of_birth or 'unknown'}}"</li>
                                            <li><code>contact_address</code>: "{{contact.full_address}}"</li>
                                            <li><code>agent_name</code>: "Your Name" (or "{{user.full_name}}")</li>
                                        </ul>
                                    </li>
                                </ul>
                            </li>
                            <li>Add <strong>Condition</strong>: If appointment booked, stop workflow</li>
                            <li>Else, Wait + same webhook, repeat</li>
                            <li><strong>IMPORTANT:</strong> Go to Workflow Settings -> Enable "Allow Re-entry" so this works more than once per contact.</li>
                        </ol>

                        <h3 style="color:var(--accent); margin-top:40px;">Step 2: Create "AI SMS Handler" Workflow</h3>
                        <ol>
                            <li>New Workflow</li>
                            <li><strong>Trigger</strong>: Inbound SMS with tag "Re-engage text"</li>
                            <li>Add <strong>Wait</strong>: 2 minutes</li>
                            <li>Add <strong>Webhook</strong> (same URL and fields but this time add custom field - ""message"" & {{message.body}})</li>
                            <li>Enable "Allow Re-entry" in settings.</li>
                        </ol>

                        <h3 style="color:var(--accent); margin-top:40px;">Daily SMS Limits</h3>
                        <ul>
                            <li>GHL starts at <strong>100 outbound SMS/day</strong></li>
                            <li>Increases automatically when previous limit hit (250 next day, then higher)</li>
                            <li>Check in GHL Settings, Phone Numbers</li>
                        </ul>

                        <p style="color: #aaa; text-align:center; margin-top:40px; font-weight:bold;">
                            Once set up, the bot runs 24/7, no more dead leads.
                        </p>
                    </div>
                    {% endraw %}
                </div>
            </div>

            <div class="tab-pane fade" id="billing">
                <div class="card billing-text">
                    <h2 style="color:var(--accent);">Billing</h2>
                    <p style="color: #aaa;">Update payment method, view invoices, or cancel subscription</p>
                    
                    {% if current_user.stripe_customer_id %}
                        <form method="post" action="/create-portal-session">
                            <button type="submit">Manage Billing on Stripe</button>
                        </form>
                    {% else %}
                        <p style="color: #aaa; margin-bottom: 20px;">You are subscribed via GHL Marketplace.</p>
                        <a href="https://marketplace.gohighlevel.com/" target="_blank" style="display:inline-block; padding:18px 50px; background:var(--accent); color:#000; border:none; border-radius:50px; font-size:1.8rem; text-decoration:none; font-weight:700;">Manage Marketplace Subscription</a>
                    {% endif %}
                </div>
            </div>
        </div>

        <div class="back">
            <a href="/">Back to Home</a>
        </div>
    </div>
</body>
</html>
    """, form=form)

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
    run_demo_janitor()
    new_id = str(uuid.uuid4())
    session['demo_session_id'] = new_id
    demo_contact_id = f"demo_{new_id}"

    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM contact_messages WHERE contact_id = %s", (demo_contact_id,))
            cur.execute("DELETE FROM contact_facts WHERE contact_id = %s", (demo_contact_id,))
            cur.execute("DELETE FROM contact_narratives WHERE contact_id = %s", (demo_contact_id,))
            conn.commit()
        except Exception as e:
            logger.error(f"Demo reset failed: {e}")
        finally:
            cur.close()
            conn.close()

    demo_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Live AI Demo - InsuranceGrokBot</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{ --accent: #00ff88; --safe-top: env(safe-area-inset-top, 20px); --safe-bottom: env(safe-area-inset-bottom, 20px); }}
        body {{ background: #000; color: #fff; font-family: 'Montserrat', sans-serif; height: 100vh; margin: 0; overflow: hidden; }}
        
        /* Layout Wrapper */
        .main-wrapper {{ display: flex; width: 100vw; height: 100vh; }}

        /* Left Column: The Phone */
        .chat-col {{ 
            flex: 1; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            background: radial-gradient(circle at center, #1a1a1a 0%, #000 70%);
            padding: var(--safe-top) 10px var(--safe-bottom) 10px;
            box-sizing: border-box;
        }}
        
        /* Right Column: The Brain */
        .log-col {{ 
            width: 450px; 
            background: #0a0a0a; 
            display: flex; 
            flex-direction: column; 
            padding: 25px; 
            box-shadow: -5px 0 20px rgba(0,0,0,0.5);
            border-left: 1px solid #222;
        }}
        
        /* Phone UI */
        .phone {{ 
            width: 100%;
            max-width: 380px;
            height: 85vh; 
            background: #000; 
            border: 8px solid #333; 
            border-radius: 45px; 
            display: flex; 
            flex-direction: column; 
            position: relative; 
            overflow: hidden;
            box-shadow: 0 20px 50px rgba(0, 255, 136, 0.1);
        }}
        
        .notch {{
            position: absolute; top: 0; left: 50%; transform: translateX(-50%);
            width: 150px; height: 30px; background: #333; border-bottom-left-radius: 18px; border-bottom-right-radius: 18px;
            z-index: 10;
        }}

        .screen {{ 
            flex: 1; 
            padding: 45px 15px 20px; 
            overflow-y: auto; 
            display: flex; 
            flex-direction: column; 
            gap: 12px; 
            scrollbar-width: none;
            background: #000;
        }}
        .screen::-webkit-scrollbar {{ display: none; }}

        .input-area {{ 
            padding: 15px; 
            background: #111; 
            display: flex; 
            gap: 10px; 
            border-top: 1px solid #222;
            z-index: 11;
        }}
        
        input {{ 
            flex: 1; 
            padding: 12px 15px; 
            border-radius: 25px; 
            border: 1px solid #333; 
            background: #222; 
            color: #fff; 
            outline: none; 
            font-size: 16px;
        }}
        
        button.send-btn {{ 
            width: 45px; height: 45px; 
            border-radius: 50%; 
            border: none; 
            background: #00ff88; 
            color: #000;
            display: flex; align-items: center; justify-content: center;
            cursor: pointer; 
        }}
        
        .msg {{ 
            padding: 12px 16px; 
            border-radius: 18px; 
            max-width: 85%; 
            font-size: 14px; 
            display: block;
            width: auto;
            line-height: 1.4; 
            overflow-wrap: break-word;
            word-wrap: break-word; /* Fix for text wrapping */
            overflow-wrap: breakword;
            white-space: pre-wrap; /* Fix for text wrapping */
            animation: popIn 0.3s ease-out;
            flex-shrink: 0;
        }}
        @keyframes popIn {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}

        .bot {{ background: #262626; align-self: flex-start; color: #e0e0e0; border-bottom-left-radius: 4px; }}
        .user {{ background: #00ff88; align-self: flex-end; color: #000; border-bottom-right-radius: 4px; font-weight: 600; }}
        
        /* Log UI */
        h3 {{ color: #00ff88; margin-top: 0; font-size: 14px; text-transform: uppercase; border-bottom: 1px solid #333; padding-bottom: 15px; }}
        #logs {{ flex: 1; overflow-y: auto; font-family: 'Courier New', monospace; font-size: 12px; }}

        .log-entry {{ margin-bottom: 20px; border-left: 2px solid #333; padding-left: 15px; }}
        .log-time {{ color: #666; font-size: 10px; }}
        .log-type {{ color: #00ff88; font-weight: bold; }}
        .log-content {{ color: #ccc; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }}

        .controls {{ margin-top: 20px; display: flex; gap: 10px; }}
        .btn {{ flex: 1; padding: 12px; border-radius: 8px; font-weight: 600; font-size: 13px; cursor: pointer; text-decoration: none; display: flex; align-items: center; justify-content: center; }}
        .reset-btn {{ background: transparent; border: 1px solid #ff4444; color: #ff4444; }}
        .download-btn {{ background: #222; color: #fff; border: 1px solid #444; }}
        @media (max-width: 900px) {{
            /* Hide the brain activity logs on mobile to focus on the chat */
            .log-col {{ 
                display: none !important; 
            }}

            /* Main wrapper occupies the full dynamic height available */
            .main-wrapper {{
                height: 100dvh;
                margin-top: 0;
            }}

            .chat-col {{
                padding: 0;
                height: 100dvh;
            }}

            /* THE CRITICAL FIX: Phone container respects safe areas */
            .phone {{ 
                width: 100%; 
                max-width: none; 
                border: none; 
                border-radius: 0;
                /* Dynamic height minus the top/bottom safe areas for Safari/Chrome */
                height: calc(100dvh - var(--safe-top) - var(--safe-bottom)); 
                margin-top: var(--safe-top);
                margin-bottom: var(--safe-bottom);
                box-shadow: none;
            }}

            /* Adjust the notch for mobile layout */
            .notch {{ 
                top: 0; 
            }}
        }}
    </style>
</head>
<body>

<div class="main-wrapper">
    <div class="chat-col">
        <div class="phone">
            <div class="notch"></div>
            <div class="screen" id="chat">
                <div class="msg bot">Quick question, are you still with that life insurance plan you mentioned before?</div>
            </div>
            <div class="input-area">
                <input type="text" id="msgInput" placeholder="Type your reply..." autofocus autocomplete="off">
                <button class="send-btn" onclick="send()">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="22" y1="2" x2="11" y2="13"></line>
                        <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
                    </svg>
                </button>
            </div>
        </div>
    </div>

    <div class="log-col">
        <h3>Live Brain Activity</h3>
        <div id="logs">
            <div style="color:#666; margin-top:20px;">Waiting for user input...</div>
        </div>
        
        <div class="controls">
            <a href="/download-transcript?contact_id={demo_contact_id}" target="_blank" class="btn download-btn">
                Download Log
            </a>
            <button class="btn reset-btn" onclick="resetSession();">Reset Session</button>
        </div>
    </div>
</div>

<script>
    const CONTACT_ID = '{demo_contact_id}';
    const chat = document.getElementById('chat');
    const logs = document.getElementById('logs');
    const input = document.getElementById('msgInput');
    let lastLogCount = 0;

    async function send() {{
        const text = input.value.trim();
        if (!text) return;
        
        chat.innerHTML += `<div class="msg user">${{text}}</div>`;
        input.value = '';
        chat.scrollTop = chat.scrollHeight;

        try {{
            const res = await fetch('/webhook', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    locationId: 'DEMO_ACCOUNT_SALES_ONLY', 
                    contact_id: CONTACT_ID,
                    message: {{ body: text }},
                    first_name: 'Demo User'
                }})
            }});
            const data = await res.json();
            
            if (data.reply) {{
                chat.innerHTML += `<div class="msg bot">${{data.reply}}</div>`;
                chat.scrollTop = chat.scrollHeight;
            }}
            fetchLogs();
        }} catch (err) {{
            console.error(err);
        }}
    }}

    async function fetchLogs() {{
        try {{
            const res = await fetch(`/get-logs?contact_id=${{CONTACT_ID}}`);
            const data = await res.json();
            
            if (data.logs && data.logs.length > 0) {{
                logs.innerHTML = data.logs.map(l => `
                    <div class="log-entry">
                        <div class="log-time">${{l.timestamp.split('T')[1]?.split('.')[0] || l.timestamp}}</div>
                        <div class="log-type">${{l.type}}</div>
                        <div class="log-content">${{l.content}}</div>
                    </div>
                `).join('');
                
                if (data.logs.length > lastLogCount) {{
                    logs.scrollTop = logs.scrollHeight;
                    lastLogCount = data.logs.length;
                }}
            }}
        }} catch (err) {{
            console.error("Log fetch error:", err);
        }}
    }}

    async function resetSession() {{
        if (confirm("Delete all data and start fresh?")) {{
            await fetch('/reset-demo', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{ contact_id: CONTACT_ID }})
            }});
            window.location.reload();
        }}
    }}

    input.addEventListener('keypress', (e) => {{
        if (e.key === 'Enter') send();
    }});

    setInterval(fetchLogs, 2500);
</script>
</body>
</html>
    """
    return render_template_string(demo_html, demo_contact_id=demo_contact_id)

@app.route("/terms")
def terms():
    terms_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Terms and Conditions - InsuranceGrokBot</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">
    <meta name="description" content="Official Terms and Conditions for InsuranceGrokBot.">
    <meta name="theme-color" content="#00ff88">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; padding:60px 20px; min-height:100vh; }
        .container { max-width:900px; margin:auto; background:var(--card-bg); padding:60px; border-radius:20px; border:1px solid #333; box-shadow:0 10px 30px var(--neon-glow); }
        h1 { color:var(--accent); font-size:4rem; text-shadow:var(--neon-glow); text-align:center; margin-bottom:40px; }
        h2 { color:var(--accent); font-size:2.5rem; margin:50px 0 25px; }
        p { font-size:1.4rem; margin:20px 0; color:#ddd; line-height:1.8; }
        ul { padding-left:40px; margin:30px 0; }
        li { font-size:1.4rem; margin:20px 0; color:#ddd; line-height:1.6; }
        .back { text-align:center; margin-top:80px; }
        .back a { color:#888; font-size:1.6rem; text-decoration:underline; }
        @media (max-width: 768px) {
            h1 { font-size:3rem; }
            h2 { font-size:2.2rem; }
            p, li { font-size:1.3rem; }
            .container { padding:40px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Terms and Conditions</h1>
        <p style="text-align:center; color:#aaa; margin-bottom:60px;">Last updated: January 09, 2026</p>
        <h2>1. Agreement to Terms</h2>
        <p>By using InsuranceGrokBot, you agree to these Terms and Conditions. If you do not agree, you may not use the service.</p>
        <h2>2. Description of Service</h2>
        <p>InsuranceGrokBot is an AI-powered SMS assistant for life insurance agents using GoHighLevel. It re-engages cold leads, conducts discovery, handles objections, and books appointments into your calendar.</p>
        <p>The service is provided on a subscription basis. You are responsible for compliance with all applicable laws (TCPA, CAN-SPAM, insurance regulations).</p>
        <h2>3. Subscription and Payment</h2>
        <p>Subscription is $100/month, billed via Stripe. You may cancel anytime. No refunds for partial months.</p>
        <h2>4. Account Responsibility</h2>
        <p>You are responsible for maintaining the security of your account and password. You agree to notify us immediately of any unauthorized use.</p>
        <h2>5. Prohibited Use</h2>
        <p>You may not use the service for any illegal or unauthorized purpose, including but not limited to:</p>
        <ul>
            <li>Sending spam or unsolicited messages</li>
            <li>Violating TCPA or other communication laws</li>
            <li>Misrepresenting yourself or the bot</li>
            <li>Using the service for non-insurance purposes</li>
        </ul>
        <h2>6. Intellectual Property</h2>
        <p>The service, including all code, design, and content, is owned by InsuranceGrokBot. You may not copy, modify, or reverse engineer any part of the service.</p>
        <h2>7. Limitation of Liability</h2>
        <p>InsuranceGrokBot is provided "as is". We are not liable for any damages arising from use of the service, including lost leads, failed appointments, or regulatory violations.</p>
        <h2>8. Termination</h2>
        <p>We may terminate or suspend your access at any time, without notice, for any reason, including violation of these terms.</p>
        <h2>9. Changes to Terms</h2>
        <p>We may update these terms at any time. Continued use after changes constitutes acceptance.</p>
        <h2>10. Contact</h2>
        <p>For questions about these terms, contact support via the dashboard or email.</p>
        <div class="back">
            <a href="/">Back to Home</a>
        </div>
    </div>
</body>
</html>
    """
    return render_template_string(terms_html)

@app.route("/test-page")
def test_page():
    # Generate unique test contact ID per session
    if 'test_session_id' not in session:
        session['test_session_id'] = str(uuid.uuid4())
    test_contact_id = f"test_{session['test_session_id']}"

    # Database Reset Logic
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM contact_messages WHERE contact_id = %s", (test_contact_id,))
            cur.execute("DELETE FROM contact_facts WHERE contact_id = %s", (test_contact_id,))
            cur.execute("DELETE FROM contact_narratives WHERE contact_id = %s", (test_contact_id,))
            conn.commit()
            logger.info(f"Reset test session: {test_contact_id}")
        except Exception as e:
            logger.error(f"DB reset failed for {test_contact_id}: {e}")
        finally:
            cur.close()
            conn.close()

    test_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1.0, user-scalable=no">
    <title>Test Chat - InsuranceGrokBot</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --safe-top: env(safe-area-inset-top);
            --safe-bottom: env(safe-area-inset-bottom);
            --accent: #00ff88;
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        
        body, html {{
            height: 100%;
            width: 100%;
            background: #000;
            font-family: 'Montserrat', sans-serif;
            overflow: hidden;
            color: white;
        }}

        /* Main Container: Splits Chat and Logs */
        .main-wrapper {{
            display: flex;
            height: 100vh;
            width: 100vw;
        }}

        /* --- CHAT COLUMN --- */
        .chat-column {{
            flex: 1;
            display: flex;
            justify-content: center;
            align-items: center;
            background: #121212;
            padding: 20px;
        }}

        /* The Phone Frame - DYNAMIC SCALING FIXED HERE */
        .phone-frame {{
            width: 100%;
            max-width: 380px;
            height: 90vh; /* Scalable height */
            max-height: 800px; /* Upper limit */
            background: #000;
            display: flex;
            flex-direction: column;
            position: relative;
            border: 8px solid #333;
            border-radius: 45px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.8);
            overflow: hidden;
        }}

        /* iPhone Elements */
        .notch {{
            width: 150px;
            height: 25px;
            background: #333;
            border-radius: 0 0 15px 15px;
            position: absolute;
            top: 0;
            left: 50%;
            transform: translateX(-50%);
            z-index: 10;
        }}

        .chat-area {{
            flex: 1;
            overflow-y: auto;
            padding: 40px 15px 15px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            background: #000;
            scrollbar-width: none;
        }}
        .chat-area::-webkit-scrollbar {{ display: none; }}

        .msg {{
            max-width: 85%;
            padding: 10px 15px;
            border-radius: 18px;
            font-size: 15px;
            line-height: 1.4;
        }}
        .bot-msg {{ background: #262626; color: white; align-self: flex-start; border-bottom-left-radius: 4px; }}
        .user-msg {{ background: #007AFF; color: white; align-self: flex-end; border-bottom-right-radius: 4px; font-weight: 600; }}

        /* INPUT AREA - PINNED TO BOTTOM OF FRAME */
        .input-area {{
            background: #1a1a1a;
            padding: 12px 15px;
            display: flex;
            align-items: center;
            gap: 10px;
            border-top: 1px solid #333;
        }}

        #user-input {{
            flex: 1;
            background: #262626;
            border: 1px solid #444;
            border-radius: 20px;
            padding: 10px 15px;
            color: white;
            font-size: 16px;
            outline: none;
        }}

        #send-btn {{
            background: #007AFF;
            border: none;
            border-radius: 50%;
            width: 38px;
            height: 38px;
            display: flex;
            justify-content: center;
            align-items: center;
            cursor: pointer;
            transition: 0.2s;
        }}
        #send-btn:hover {{ background: #0063d1; }}

        /* --- LOG COLUMN --- */
        .log-column {{
            width: 400px;
            background: #0a0a0a;
            border-left: 2px solid #222;
            display: flex;
            flex-direction: column;
            padding: 20px;
        }}

        #logs {{
            flex: 1;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 11px;
            color: var(--accent);
            background: #000;
            padding: 15px;
            border-radius: 10px;
            border: 1px solid #111;
        }}

        .log-entry {{ margin-bottom: 8px; border-bottom: 1px solid #111; padding-bottom: 4px; }}
        .log-ts {{ color: #555; }}
        .log-type {{ font-weight: bold; color: #fff; }}

        /* Mobile Adjustments */
        @media (max-width: 800px) {{
            .log-column {{ display: none; }}
            .phone-frame {{ height: 100vh; max-height: none; border: none; border-radius: 0; }}
        }}
    </style>
</head>
<body>

    <div class="main-wrapper">
        <div class="chat-column">
            <div class="phone-frame">
                <div class="notch"></div>
                
                <div class="chat-area" id="chat-screen">
                    <div class="msg bot-msg">
                        Hey! Quick question, are you still with that life insurance plan you mentioned before?
                    </div>
                </div>

                <div class="input-area">
                    <input type="text" id="user-input" placeholder="iMessage" autocomplete="off" autofocus>
                    <button id="send-btn" type="button">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="white"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
                    </button>
                </div>
            </div>
        </div>

        <div class="log-column">
            <h3 style="margin-bottom: 15px; color: var(--accent); letter-spacing: 1px;">DEBUG CONSOLE</h3>
            <div id="logs">
                <div class="log-entry">Waiting for interaction...</div>
            </div>
            <div style="margin-top: 15px; display: flex; gap: 10px;">
                <button onclick="location.reload()" style="flex:1; padding: 12px; border-radius: 8px; border:none; background:#ff4444; color:white; font-weight:bold; cursor:pointer;">Reset Chat</button>
            </div>
        </div>
    </div>

    <script>
        const TEST_CONTACT_ID = '{test_contact_id}';
        const input = document.getElementById('user-input');
        const sendBtn = document.getElementById('send-btn');
        const chat = document.getElementById('chat-screen');
        const logsDiv = document.getElementById('logs');

        async function sendMessage() {{
            const msg = input.value.trim();
            if (!msg) return;

            chat.innerHTML += `<div class="msg user-msg">${{msg}}</div>`;
            input.value = '';
            chat.scrollTop = chat.scrollHeight;

            try {{
                const res = await fetch('/webhook', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        user_id: '',
                        location_id: 'TEST_LOCATION_456',
                        contact_id: TEST_CONTACT_ID,
                        first_name: 'Test User',
                        message: {{ body: msg }}
                    }})
                }});

                const data = await res.json();
                if(data.reply) {{
                    chat.innerHTML += `<div class="msg bot-msg">${{data.reply}}</div>`;
                    chat.scrollTop = chat.scrollHeight;
                }}
                fetchLogs();
            }} catch (e) {{
                console.error("Test Error:", e);
            }}
        }}

        sendBtn.onclick = sendMessage;
        input.onkeydown = (e) => {{ if(e.key === 'Enter') sendMessage(); }};

        function fetchLogs() {{
            fetch(`/get-logs?contact_id=${{TEST_CONTACT_ID}}`)
                .then(res => res.json())
                .then(data => {{
                    if(data.logs && data.logs.length > 0) {{
                        logsDiv.innerHTML = data.logs.map(l => `
                            <div class="log-entry">
                                <span class="log-ts">[${{l.timestamp.split('T')[1].split('.')[0]}}]</span> 
                                <span class="log-type">${{l.type}}</span><br>
                                ${{l.content}}
                            </div>
                        `).join('');
                        logsDiv.scrollTop = logsDiv.scrollHeight;
                    }}
                }});
        }}

        // Poll for logs every 3 seconds
        setInterval(fetchLogs, 3000);
    </script>
</body>
</html>
    """
    return render_template_string(test_html, test_contact_id=test_contact_id)

@app.route("/get-logs", methods=["GET"])
def get_logs():
    contact_id = request.args.get("contact_id")

    # === FIX 1: Allow "demo_" IDs so the new page works ===
    if not contact_id or (not contact_id.startswith("test_") and not contact_id.startswith("demo_")):
        # Just return empty logs instead of crashing or warning heavily
        return jsonify({"logs": []}) 

    conn = get_db_connection()
    if not conn:
        logger.error("Database connection failed in get_logs")
        return jsonify({"error": "Database connection failed"}), 500

    logs = []

    try:
        cur = conn.cursor()

        # === 1. Messages with real timestamps ===
        cur.execute("""
            SELECT message_type, message_text, created_at
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at ASC
        """, (contact_id,))
        messages = cur.fetchall()

        for msg_type, text, created_at in messages:
            role = "Lead" if msg_type == "lead" else "Bot"
            
            # Handle Date vs String
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
        
        # Simple regex fallbacks
        name_match = re.search(r"first name: (\w+)", facts_text, re.IGNORECASE)
        if name_match: first_name = name_match.group(1).capitalize()
        
        age_match = re.search(r"age: (\d+)", facts_text)
        if age_match: age = age_match.group(1)
        
        addr_match = re.search(r"address/location: (.*)", facts_text, re.IGNORECASE)
        if addr_match: address = addr_match.group(1).strip()

        # Default text if build fails
        narrative_text = "Narrative pending..."

        try:
            profile_narrative = build_comprehensive_profile(
                story_narrative=story_narrative,
                known_facts=facts,
                first_name=first_name,
                age=age,
                address=address
            )

            # Unpack Tuple if necessary
            if isinstance(profile_narrative, tuple):
                narrative_text = profile_narrative[0]
            else:
                narrative_text = str(profile_narrative)

        except Exception as e:
            logger.error(f"Profile build error in logs: {e}")
            narrative_text = f"Error building profile: {str(e)}"

        # === FIX 2: Use 'narrative_text' (String) not 'profile_narrative' (Object) ===
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "type": "Full Human Identity Narrative",
            "content": narrative_text 
        })

    except Exception as e:
        logger.error(f"Error in get_logs: {e}")
        logs.append({"timestamp": datetime.now().isoformat(), "type": "Error", "content": str(e)})
    finally:
        cur.close()
        conn.close()

    return jsonify({"logs": logs})

@app.route("/reset-test", methods=["GET"])
def reset_test():
    contact_id = request.args.get("contact_id")
    
    # Security: Only allow test_ prefixed contacts
    if not contact_id or not contact_id.startswith("test_"):
        logger.warning(f"Invalid reset attempt: {contact_id}")
        return jsonify({"error": "Invalid test contact ID"}), 400

    conn = get_db_connection()
    if not conn:
        logger.error("Database connection failed during reset")
        return jsonify({"error": "Database connection failed"}), 500

    try:
        cur = conn.cursor()
        
        # Delete messages, facts, and narrative for this test contact only
        cur.execute("DELETE FROM contact_messages WHERE contact_id = %s", (contact_id,))
        cur.execute("DELETE FROM contact_facts WHERE contact_id = %s", (contact_id,))
        cur.execute("DELETE FROM contact_narratives WHERE contact_id = %s", (contact_id,))
        
        conn.commit()
        
        logger.info(f"Successfully reset test contact {contact_id}")
        
        return jsonify({
            "status": "reset success",
            "message": f"Test session {contact_id} cleared",
            "cleared_contact": contact_id
        }), 200
        
    except Exception as e:
        conn.rollback()  # Important: rollback on error
        logger.error(f"Reset failed for {contact_id}: {e}")
        return jsonify({"error": "Failed to reset test data"}), 500
        
    finally:
        cur.close()
        conn.close()

@app.route("/download-transcript", methods=["GET"])
def download_transcript():
    contact_id = request.args.get("contact_id")
    if not contact_id or not contact_id.startswith("test_"):
        return jsonify({"error": "Invalid test contact"}), 400

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
            subscription_data={
                "metadata": {
                    "source": "website"
                }
            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )
        return redirect(session.url, code=303)
    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Payment Error</title>
    <style>body{font-family:sans-serif;text-align:center;padding:50px;background:#000;color:#fff;} h1{color:#ff6b6b;}</style>
</head>
<body>
    <h1>Payment Initialization Error</h1>
    <p>Please try again or contact support.</p>
    <a href="/" style="color:#00ff88;">Back to Home</a>
</body>
</html>
        """), 500

@app.route("/cancel")
def cancel():
    cancel_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Checkout Canceled</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        body { background:#000; color:#fff; font-family:'Montserrat',sans-serif; text-align:center; padding:100px 20px; }
        h1 { color:#00ff88; margin-bottom:20px; }
        a { color:#aaa; text-decoration:underline; }
    </style>
</head>
<body>
    <h1>Checkout Canceled</h1>
    <p>No charges were made.</p>
    <a href="/">Return Home</a>
</body>
</html>
    """
    return render_template_string(cancel_html)

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

    # If we have an email, check if user exists. 
    # If they exist but have no password (created via webhook), show set password.
    if email:
        user = User.get(email)
        if user and not user.password_hash:
            return render_template_string(f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Set Password</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        body {{ background:#000; color:#fff; font-family:'Montserrat',sans-serif; padding:50px; text-align:center; }}
        input {{ padding:15px; width:300px; margin:10px; border-radius:10px; border:none; }}
        button {{ padding:15px 40px; background:#00ff88; border:none; border-radius:50px; font-weight:bold; cursor:pointer; }}
    </style>
</head>
<body>
    <h1>Set Your Password</h1>
    <p>For account: {email}</p>
    <form action="/set-password" method="post">
        <input type="hidden" name="email" value="{email}">
        <input type="password" name="password" placeholder="New Password" required><br>
        <input type="password" name="confirm" placeholder="Confirm Password" required><br>
        <button type="submit">Save & Login</button>
    </form>
</body>
</html>
            """)

    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Success</title>
    <style>body{background:#000;color:#fff;font-family:sans-serif;text-align:center;padding:100px;}</style>
</head>
<body>
    <h1>Payment Successful!</h1>
    <p>Your account is active.</p>
    <a href="/login" style="color:#00ff88;">Click here to Log In</a>
</body>
</html>
    """)

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
    location_id = request.args.get("locationId") or request.args.get("location_id")
    api_key = request.args.get("apiKey") or request.args.get("api_key")
    user_id = request.args.get("userId") or request.args.get("user_id") or request.args.get("user.id")
    calendar_id = request.args.get("calendarId") or request.args.get("calendar_id")

    if not location_id:
        return "Error: Missing LocationID", 400

    confirmation_code = str(uuid.uuid4())[:8].upper()

    if worksheet:
        try:
            values = worksheet.get_all_values()
            if not values: values = []
            
            # Ensure headers exist
            expected_headers = ["email", "location_id", "calendar_id", "crm_api_key", "crm_user_id", "bot_first_name", "timezone", "initial_message", "stripe_customer_id", "confirmation_code", "code_used"]
            if not values or values[0] != expected_headers:
                worksheet.update('A1:K1', [expected_headers])
                values = [expected_headers] + values

            header = values[0]
            header_lower = [h.strip().lower() for h in header]
            def c_idx(n): 
                try: return header_lower.index(n.lower())
                except: return -1

            loc_idx = c_idx("location_id")
            code_idx = c_idx("confirmation_code")
            
            # Check if row exists for this location
            row_num = None
            if loc_idx != -1:
                for i, row in enumerate(values[1:], start=2):
                    if len(row) > loc_idx and row[loc_idx] == location_id:
                        row_num = i
                        break
            
            # Prepare row data (simplified mapping for brevity)
            # In production, map indices carefully. Here we append if new.
            new_row = [""] * len(expected_headers)
            # Fill knowns...
            if loc_idx >= 0: new_row[loc_idx] = location_id
            if c_idx("crm_api_key") >= 0: new_row[c_idx("crm_api_key")] = api_key or ""
            if c_idx("confirmation_code") >= 0: new_row[c_idx("confirmation_code")] = confirmation_code
            if c_idx("crm_user_id") >=0: new_row[c_idx("crm_user_id")] = user_id
            if c_idx("code_used") >= 0: new_row[c_idx("code_used")] = "0"

            if row_num:
                # Update specific cells to preserve other data? 
                # For safety in this context, we overwrite the code to ensure the user sees the new one.
                worksheet.update_cell(row_num, code_idx+1, confirmation_code)
            else:
                worksheet.append_row(new_row)

            sync_subscribers()
        except Exception as e:
            logger.error(f"OAuth Sheet Error: {e}")

    return render_template_string(f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Install Complete</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        body {{ background:#000; color:#fff; font-family:'Montserrat',sans-serif; padding:50px; text-align:center; }}
        .box {{ background:#111; padding:30px; border-radius:20px; display:inline-block; border:1px solid #333; }}
        .code {{ font-size:3em; color:#00ff88; letter-spacing:5px; margin:20px 0; font-weight:bold; }}
        a {{ color:#fff; text-decoration:underline; }}
    </style>
</head>
<body>
    <h1>Installation Successful!</h1>
    <p>Please copy your confirmation code below to register your account:</p>
    <div class="box">
        <div class="code">{confirmation_code}</div>
    </div>
    <br><br>
    <a href="/register" style="font-size:1.5em;">Click here to Register</a>
</body>
</html>
    """)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)