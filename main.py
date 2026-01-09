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
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

# === NEW MINIMAL IMPORTS ===
from prompt import build_system_prompt
from memory import save_message, get_recent_messages, save_new_facts, get_known_facts
from conversation_engine import ConversationState
from outcome_learning import classify_vibe
from ghl_message import send_sms_via_ghl
from ghl_calendar import consolidated_calendar_op
from underwriting import get_underwriting_context
from insurance_companies import find_company_in_message, normalize_company_name, get_company_context
from db import get_subscriber_info, get_db_connection, init_db, User
from sync_subscribers import sync_subscribers
from individual_profile import build_comprehensive_profile
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

# Google Sheets Setup for writing from dashboard
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
        return None  # Return None on error — prevents crash

# Forms
class RegisterForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
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
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"status": "error", "error": "No JSON payload"}), 400

    # Extract from payload
    intent = payload.get("intent") or ""
    first_name = payload.get("first_name") or ""
    contact_id = payload.get("contact_id") or "unknown"
    agent_name = payload.get("agentName") or ""
    dob_str = payload.get("age") or ""  # Note: This is DOB, not age
    address = payload.get("address") or ""
    lead_vendor = payload.get("lead_vendor", "")

    from age import calculate_age_from_dob
    age = calculate_age_from_dob(date_of_birth=dob_str) if dob_str else None

    # Pre-load initial facts
    initial_facts = []
    if first_name:
        initial_facts.append(f"First name: {first_name}")
    if age and age != "unknown":
        initial_facts.append(f"Age: {age}")
    if address:
        initial_facts.append(f"Address/location: {address}")
    if intent:
        initial_facts.append(f"Intent: {intent}")
    if agent_name:
        initial_facts.append(f"Agent name: {agent_name}")

    if initial_facts and contact_id != "unknown":
        save_new_facts(contact_id, initial_facts)
        logger.info(f"Pre-loaded {len(initial_facts)} facts for {contact_id}")

    # 1. Identity Lookup
    location_id = payload.get("locationId")
    if not location_id:
        return jsonify({"status": "error", "message": "Missing locationId"}), 400

    # Determine mode
    is_demo = (location_id == 'DEMO_ACCOUNT_SALES_ONLY')
    is_test = (location_id == 'TEST_LOCATION_456')  # Used by /test-page

    if is_demo:
        # === DEMO MODE === (existing behavior — no DB persistence, fresh every time)
        subscriber = {
            'bot_first_name': 'Grok',
            'crm_api_key': 'DEMO',
            'crm_user_id': '',
            'calendar_id': '',
            'timezone': 'America/Chicago',
            'initial_message': ''  # Optional
        }
        contact_id = payload.get("contact_id")
        if not contact_id:
            logger.warning("Demo webhook missing contact_id — rejecting")
            return jsonify({"status": "error", "message": "Invalid demo session"}), 400

    elif is_test:
        # === TEST MODE === (full bot capabilities, DB active, but safe/no real API)
        subscriber = {
            'bot_first_name': 'Grok',
            'crm_api_key': 'DEMO',          # Prevents real SMS
            'crm_user_id': '',
            'calendar_id': '',
            'timezone': 'America/Chicago',
            'initial_message': ''
        }
        contact_id = payload.get("contact_id") or "unknown"
        logger.info(f"Test mode activated for contact {contact_id}")

    else:
        # === REAL PRODUCTION MODE ===
        subscriber = get_subscriber_info(location_id)
        if not subscriber or not subscriber.get('bot_first_name'):
            logger.error(f"Identity not configured for location {location_id}")
            return jsonify({"status": "error", "message": "Not configured"}), 404

        # NEW: Validate the API key from payload matches the stored one (security)
        provided_api_key = payload.get("apiKey") or payload.get("api_key") or payload.get("crm_api_key")
        stored_api_key = subscriber.get('crm_api_key')
        if provided_api_key and stored_api_key and provided_api_key != stored_api_key:
            logger.warning(f"API key mismatch for location {location_id}")
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        contact_id = payload.get("contact_id") or payload.get("contactid") or payload.get("contact", {}).get("id")
        if not contact_id:
            return jsonify({"status": "error", "error": "Missing contact_id"}), 400

    bot_first_name = subscriber['bot_first_name']
    crm_api_key = subscriber['crm_api_key']
    timezone = subscriber.get('timezone', 'America/Chicago')
    crm_user_id = subscriber['crm_user_id']
    calendar_id = subscriber['calendar_id']

    # 2. Extract Message
    raw_message = payload.get("message", {})
    message = raw_message.get("body", "").strip() if isinstance(raw_message, dict) else str(raw_message).strip()
    if not message:
        return jsonify({"status": "ignored", "reason": "empty message"}), 200

    # 3. Idempotency (real mode only — demo can have duplicates safely)
    if not is_demo:
        message_id = payload.get("message_id") or payload.get("id")
        if message_id:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1 FROM processed_webhooks WHERE webhook_id = %s", (message_id,))
                    if cur.fetchone():
                        logger.info(f"Duplicate ignored: {message_id}")
                        return jsonify({"status": "success", "message": "Already processed"}), 200
                    cur.execute("INSERT INTO processed_webhooks (webhook_id) VALUES (%s)", (message_id,))
                    conn.commit()
                except Exception as e:
                    logger.error(f"Idempotency error: {e}")
                finally:
                    cur.close()
                    conn.close()

    # Always save message (for history in real mode, minimal in demo)
    save_message(contact_id, message, "lead")

    # 4. Context Gathering
    initial_message = subscriber.get('initial_message', '').strip()
    known_facts = [] if is_demo else get_known_facts(contact_id)
    vibe = classify_vibe(message).value
    recent_exchanges = get_recent_messages(contact_id, limit=8)
    assistant_messages = [m for m in recent_exchanges if m["role"] == "assistant"]

    if len(assistant_messages) == 0 and initial_message:
        reply = initial_message
        save_message(contact_id, reply, "assistant")
        if not is_demo and crm_api_key != 'DEMO':
            send_sms_via_ghl(contact_id, reply, crm_api_key, location_id)
        return jsonify({"status": "success", "reply": reply})

    context_nudge = ""
    msg_lower = message.lower()
    if any(x in msg_lower for x in ["covered", "i'm good", "already have", "taken care of"]):
        context_nudge = "Lead claims to be covered — likely smoke screen"
    elif any(x in msg_lower for x in ["work", "job", "employer"]):
        context_nudge = "Lead mentioned work/employer coverage"

    # Calendar — DISABLED in demo (focus on selling, not booking)
    calendar_slots = ""
    if not is_demo and any(k in msg_lower for k in ["schedule", "time", "call", "appointment", "available"]):
        calendar_slots = consolidated_calendar_op("fetch_slots", subscriber)

    # Underwriting — only on health mention
    underwriting_context = ""
    medical_keywords = ["cancer", "diabetes", "heart", "stroke", "copd", "health issue", "condition", "medical", "sick"]
    if any(k in msg_lower for k in medical_keywords):
        underwriting_context = get_underwriting_context(message)

    # Company (light)
    company_context = ""
    raw_company = find_company_in_message(message)
    if raw_company:
        normalized = normalize_company_name(raw_company)
        if normalized:
            company_context = get_company_context(normalized)

    # 5. Build Prompt
    system_prompt = build_system_prompt(
        bot_first_name=bot_first_name,
        timezone=timezone,
        known_facts=known_facts,
        stage="discovery",
        vibe=vibe,
        recent_exchanges=recent_exchanges,
        message=message,
        lead_vendor=lead_vendor,
        calendar_slots=calendar_slots,
        context_nudge=context_nudge,
        lead_first_name=first_name,
        lead_age=age,
        lead_address=address
    )

    # 6. Grok Call
    grok_messages = [{"role": "system", "content": system_prompt}]
    for msg in recent_exchanges:
        role = "user" if msg["role"] == "lead" else "assistant"
        grok_messages.append({"role": role, "content": msg["text"]})
    grok_messages.append({"role": "user", "content": message})

    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=grok_messages,
            temperature=0.7,
            max_tokens=500
        )
        raw_reply = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Grok error: {e}")
        raw_reply = "Gotcha — quick question, when was the last time someone reviewed your coverage?"

    # 7. Clean Reply & Extract Facts
    reply = raw_reply
    new_facts_extracted = []

    if "<new_facts>" in raw_reply:
        try:
            block = raw_reply.split("<new_facts>")[1].split("</new_facts>")[0]
            new_facts_extracted = [line.strip(" -•").strip() for line in block.split("\n") if line.strip()]
            reply = raw_reply.split("<new_facts>")[0].strip()
        except:
            pass

    # Clean AI tells
    reply = reply.replace("—", ",").replace("–", ",").replace("…", "...")
    reply = reply.replace(""", '"').replace(""", '"')
    reply = reply.strip()

    # 8. Persistence — SKIP in demo (fresh every time)
    if not is_demo:
        if new_facts_extracted:
            save_new_facts(contact_id, new_facts_extracted)
        if crm_api_key != 'DEMO':
            send_sms_via_ghl(contact_id, reply, crm_api_key, location_id)

    save_message(contact_id, reply, "assistant")

    # 9. Response
    response_data = {
        "status": "success",
        "reply": reply
    }
    if is_demo:
        response_data["facts"] = new_facts_extracted  # Show in demo sidebar

    return jsonify(response_data)

@app.route("/")
def home():
    home_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>InsuranceGrokBot | AI Lead Re-engagement for Life Insurance Agents</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO -->
    <meta name="description" content="The most advanced AI SMS bot for life insurance lead re-engagement. Powered by Grok. Books appointments from cold leads 24/7.">
    <meta name="theme-color" content="#00ff88">

    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <style>
        :root {
            --accent: #00ff88;
            --dark-bg: #000;
            --card-bg: #0f0f0f;
            --text-secondary: #aaa;
            --neon-glow: 0 0 30px rgba(0, 255, 136, 0.4);
            --red-x: #ff4444;
            --green-check: #00ff88;
        }
        body {
            background: var(--dark-bg);
            color: #fff;
            font-family: 'Montserrat', sans-serif;
            line-height: 1.7;
        }
        .navbar {
            background: rgba(0,0,0,0.95);
            backdrop-filter: blur(10px);
        }
        .navbar-brand {
            font-weight: 700;
            font-size: 1.8rem;
            color: #fff !important;
        }
        .highlight {
            color: var(--accent);
            text-shadow: var(--neon-glow);
        }
        .nav-link {
            color: #ddd !important;
            font-weight: 600;
            padding: 0.8rem 1rem !important;
        }
        .nav-link:hover { color: var(--accent) !important; }
        .btn-primary {
            display: inline-block;
            background: #00ff88;
            color: #000;
            font-weight: 700;
            font-size: 1.6rem;
            padding: 18px 50px;
            border-radius: 50px;
            box-shadow: 0 6px 20px rgba(0, 255, 136, 0.3);
            text-decoration: none;
            transition: all 0.3s ease;
            border: none;
            letter-spacing: 0.5px;
        }
        .btn-primary:hover {
            background: #00ee80;
            box-shadow: 0 12px 30px rgba(0, 255, 136, 0.5);
            transform: translateY(-4px);
        }
        .hero {
            padding: 140px 20px 100px;
            text-align: center;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: radial-gradient(circle at center, #111 0%, #000 80%);
        }
        .hero h1 {
            font-size: 3.5rem;
            font-weight: 700;
            line-height: 1.2;
            margin-bottom: 30px;
            text-shadow: var(--neon-glow);
        }
        @media (max-width: 768px) {
            .hero h1 { font-size: 2.8rem; }
            .hero p.lead { font-size: 1.4rem; }
            .btn-primary { font-size: 1.4rem; padding: 18px 40px; }
        }
        .hero p.lead {
            font-size: 1.6rem;
            color: var(--text-secondary);
            max-width: 800px;
            margin: 0 auto 50px;
        }
        .section {
            padding: 100px 20px;
        }
        .section-title {
            font-size: 3rem;
            font-weight: 700;
            text-align: center;
            margin-bottom: 80px;
            color: var(--accent);
            text-shadow: var(--neon-glow);
        }
        .feature-card {
            background: var(--card-bg);
            border-radius: 20px;
            padding: 40px;
            text-align: center;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            transition: all 0.4s;
            height: 100%;
        }
        .feature-card:hover {
            transform: translateY(-15px);
            box-shadow: 0 20px 50px rgba(0, 255, 136, 0.3);
        }
        .feature-card h3 {
            font-size: 1.8rem;
            margin-bottom: 20px;
            color: var(--accent);
        }

        /* COMPARISON TABLE — CLEAN & PROFESSIONAL */
        .comparison-wrapper {
            max-width: 1000px;
            margin: 0 auto;
            background: var(--card-bg);
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 15px 40px rgba(0,0,0,0.6);
        }
        .comparison-table {
            width: 100%;
            border-collapse: collapse;
        }
        .comparison-table th {
            padding: 30px;
            font-size: 1.8rem;
            font-weight: 700;
            text-align: center;
        }
        .feature-header { text-align: left; padding-left: 40px; color: #fff; }
        .standard-header { color: var(--red-x); }
        .grok-header { color: var(--accent); }
        .comparison-table td {
            padding: 25px 20px;
            vertical-align: middle;
            font-size: 1.3rem;
            border-bottom: 1px solid #222;
        }
        .comparison-table tr:last-child td { border-bottom: none; }
        .feature-cell { text-align: left; padding-left: 40px; }
        .standard-cell, .grok-cell { text-align: center; font-size: 3.5rem; }
        .check { color: var(--green-check); }
        .cross { color: var(--red-x); }

        /* Sticky header on mobile */
        .sticky-header {
            position: sticky;
            top: 0;
            background: #111;
            z-index: 10;
        }

        .comparison-footer {
            text-align: center;
            margin-top: 60px;
            font-size: 1.6rem;
            color: var(--text-secondary);
            line-height: 1.8;
        }

        /* Mobile: Clean stacked cards with sticky header */
        @media (max-width: 992px) {
            .comparison-table thead { display: none; }
            .comparison-table tr {
                display: block;
                margin: 25px 0;
                background: #111;
                border-radius: 15px;
                padding: 25px;
                box-shadow: 0 8px 25px rgba(0,0,0,0.4);
            }
            .comparison-table td {
                display: block;
                text-align: center;
                padding: 12px 0;
                border: none;
            }
            .feature-cell {
                text-align: center;
                font-weight: bold;
                font-size: 1.4rem;
                margin-bottom: 20px;
                padding-left: 0;
            }
            .standard-cell::before { content: "Standard Bots: "; font-weight: bold; color: var(--red-x); display: block; margin-bottom: 10px; }
            .grok-cell::before { content: "InsuranceGrokBot: "; font-weight: bold; color: var(--accent); display: block; margin-bottom: 10px; }
            .check, .cross { font-size: 4rem; }
            .comparison-footer { font-size: 1.4rem; margin-top: 40px; }
        }

        .sales-logic {
            background: var(--card-bg);
            border-radius: 20px;
            padding: 60px;
            box-shadow: 0 15px 40px rgba(0,0,0,0.6);
            max-width: 1000px;
            margin: 0 auto;
        }
        .sales-logic h3 {
            color: var(--accent);
            font-size: 2rem;
            margin-bottom: 20px;
        }
        .pricing-card {
            background: linear-gradient(135deg, #111, #000);
            border: 2px solid var(--accent);
            border-radius: 30px;
            padding: 60px;
            text-align: center;
            max-width: 600px;
            margin: 0 auto;
            box-shadow: 0 20px 60px rgba(0, 255, 136, 0.3);
        }
        .price {
            font-size: 6rem;
            font-weight: 700;
            color: var(--accent);
            text-shadow: var(--neon-glow);
        }
        footer {
            padding: 80px 20px;
            text-align: center;
            color: var(--text-secondary);
            border-top: 1px solid #222;
        }
        @media (max-width: 768px) {
            .section { padding: 80px 20px; }
            .section-title { font-size: 2.5rem; margin-bottom: 60px; }
            .sales-logic { padding: 40px; }
            .pricing-card { padding: 50px; }
            .price { font-size: 4.5rem; }
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
                <ul class="navbar-nav ms-auto align-items-center">
                    <li class="nav-item"><a href="#features" class="nav-link">Features</a></li>
                    <li class="nav-item"><a href="#comparison" class="nav-link">Why GrokBot Wins</a></li>
                    <li class="nav-item"><a href="#logic" class="nav-link">Sales Logic</a></li>
                    <li class="nav-item"><a href="#pricing" class="nav-link">Pricing</a></li>
                    <li class="nav-item"><a href="/demo-chat" class="nav-link">Live Demo</a></li>
                    {% if current_user.is_authenticated %}
                        <li class="nav-item"><span class="navbar-text me-3">Hello, {{ current_user.email }}</span></li>
                        <li class="nav-item"><a href="/dashboard" class="btn btn-outline-light me-2">Dashboard</a></li>
                        <li class="nav-item"><a href="/logout" class="btn btn-outline-danger">Logout</a></li>
                    {% else %}
                        <li class="nav-item"><a href="/login" class="btn btn-outline-light me-2">Log In</a></li>
                        <li class="nav-item"><a href="/register" class="btn btn-primary">Sign Up</a></li>
                    {% endif %}
                </ul>
            </div>
        </div>
    </nav>

    <section class="hero">
        <div class="container">
            <h1>The Most Advanced Life Insurance<br>Lead Re-engagement AI Ever Built</h1>
            <p class="lead">Powered by xAI's Grok. Trained on thousands of real insurance conversations.<br>Books appointments from leads that have been cold for months.</p>
            
            <!-- CTA Group — centered, spaced perfectly -->
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
            <h2 class="section-title">Why InsuranceGrokBot Dominates Every Other Bot</h2>
            
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
                            <td class="feature-col">5 blended sales frameworks (NEPQ, Gap Selling, Straight Line, Never Split the Difference, Psychology of Selling)</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                        <tr>
                            <td class="feature-col">Full underwriting knowledge and health condition handling</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                        <tr>
                            <td class="feature-col">Persistent memory across entire conversation history</td>
                            <td class="standard-col">Limited</td>
                            <td class="grok-col">Complete</td>
                        </tr>
                        <tr>
                            <td class="feature-col">Never accepts "no" without proper discovery</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                        <tr>
                            <td class="feature-col">Only books leads with identified gaps</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                        <tr>
                            <td class="feature-col">Multi-tenant agency support with data isolation</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                        <tr>
                            <td class="feature-col">Calendar integration and availability checking</td>
                            <td class="standard-col">Basic</td>
                            <td class="grok-col">Advanced</td>
                        </tr>
                        <tr>
                            <td class="feature-col">Built and continuously trained by active life insurance agents</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                        <tr>
                            <td class="feature-col">Handles complex objections with emotional intelligence</td>
                            <td class="standard-col"><span class="cross">✗</span></td>
                            <td class="grok-col"><span class="check">✓</span></td>
                        </tr>
                    </tbody>
                </table>
            </div>

            <div class="text-center mt-5">
                <p style="font-size:1.6rem; color:var(--text-secondary);">Other bots use simple scripts. InsuranceGrokBot thinks like a top-producing agent.</p>
            </div>
        </div>
    </section>

            <div class="text-center mt-5">
                <p style="font-size:1.6rem; color:var(--text-secondary);">Other bots use simple scripts. InsuranceGrokBot thinks like a top-producing agent.</p>
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
                        <p>Neuro-Emotional Persuasion Questioning. The bot asks problem-awareness and consequence questions that make leads persuade themselves they need coverage.</p>
                        
                        <h3>Never Split the Difference</h3>
                        <p>Chris Voss negotiation tactics. Uses calibrated questions, labels, and mirrors to handle objections and build trust.</p>
                    </div>
                    <div class="col-md-6 mb-5">
                        <h3>Jordan Belfort’s Straight Line</h3>
                        <p>Maintains control of the conversation, loops back to benefits, and builds massive certainty in the product.</p>
                        
                        <h3>Gap Selling + Psychology of Selling</h3>
                        <p>Identifies the gap between current and desired state (Keenan) while using emotional drivers and closing psychology (Brian Tracy).</p>
                    </div>
                </div>
                <p class="text-center" style="font-size:1.3rem; margin-top:40px;">
                    THE BOT DYNAMICALLY CHOOSES THE BEST FRAMEWORK FOR EACH MOMENT BASED ON LEAD RESPONSES, SOMETHING NO OTHER SCRIPTED BOT CAN DO.
                </p>
            </div>
        </div>
    </section>

    <section id="pricing" class="section bg-black">
        <div class="container text-center">
            <h2 class="section-title">Simple, Transparent Pricing</h2>
            <div class="pricing-card">
                <div class="price">$100<span style="font-size:2rem;">/mth</span></div>
                <p style="font-size:1.6rem; margin:30px 0;">Early Adopter Rate, Limited to First 100 Agents</p>
                <ul style="text-align:left; max-width:400px; margin:30px auto; font-size:1.2rem;">
                    <li>Unlimited conversations</li>
                    <li>Full memory and fact extraction</li>
                    <li>All 5 sales frameworks</li>
                    <li>Calendar booking</li>
                    <li>Multi-tenant support</li>
                    <li>Priority updates and support</li>
                </ul>
                <a href="/checkout" class="btn-primary">Subscribe Now<br>Instant Activation</a>
                <p class="mt-4 text-secondary">No contract. Cancel anytime.</p>
            </div>
        </div>
    </section>

    <footer>
        <div class="container">
            <p>&copy; 2026 InsuranceGrokBot. Built by life insurance agents, for life insurance agents.</p>
            <p><a href="/terms" style="color:var(--text-secondary);">Terms</a> • <a href="/privacy" style="color:var(--text-secondary);">Privacy</a></p>
        </div>
    </footer>
</body>
</html>
    """
    return render_template_string(home_html)

# Update stripe_webhook to auto-save Stripe ID to Sheet on success
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
                # Create user with NO password (they'll set it later)
                User.create(email, password_hash=None, stripe_customer_id=customer_id)
                logger.info(f"Created paid user {email} (password pending)")
            else:
                # Update existing user
                conn = get_db_connection()
                conn.execute("UPDATE users SET stripe_customer_id = ? WHERE email = ?", (customer_id, email))
                conn.commit()
                conn.close()

            # NEW: Auto-save Stripe ID to Google Sheet
            if worksheet:
                try:
                    values = worksheet.get_all_values()
                    header = values[0] if values else []
                    header_lower = [h.strip().lower() for h in header]

                    def col_index(name):
                        try:
                            return header_lower.index(name.lower())
                        except ValueError:
                            # Add column if missing
                            new_col = len(header) + 1
                            worksheet.update_cell(1, new_col, name)
                            header.append(name)
                            header_lower.append(name.lower())
                            return new_col - 1  # 0-indexed

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
                        # Append new row with email + stripe_id
                        new_row = [""] * len(header)
                        new_row[email_idx] = email
                        new_row[stripe_idx] = customer_id
                        worksheet.append_row(new_row)

                    logger.info(f"Auto-saved Stripe ID {customer_id} to Sheet for {email}")
                except Exception as e:
                    logger.error(f"Sheet Stripe save failed: {e}")

    # NEW: Handle subscription cancel/deletion
    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        customer_id = subscription.customer
        email = stripe.Customer.retrieve(customer_id).email.lower() if stripe.Customer.retrieve(customer_id).email else None

        if customer_id or email:
            # Clear row in Sheet
            if worksheet:
                try:
                    values = worksheet.get_all_values()
                    header_lower = [h.strip().lower() for h in values[0]]

                    email_idx = header_lower.index("email") if "email" in header_lower else None
                    stripe_idx = header_lower.index("stripe_customer_id") if "stripe_customer_id" in header_lower else None

                    row_num = None
                    for i, row in enumerate(values[1:], start=2):
                        if (stripe_idx and len(row) > stripe_idx and row[stripe_idx] == customer_id) or \
                           (email_idx and len(row) > email_idx and row[email_idx].strip().lower() == email):
                            row_num = i
                            break

                    if row_num:
                        # Clear all cells except email (or delete row if preferred)
                        clear_row = [row[0] if i == 0 else "" for i in range(len(values[0]))]  # Keep email
                        worksheet.update(f"A{row_num}:{chr(64 + len(values[0]))}{row_num}", [clear_row])
                        logger.info(f"Cleared Sheet row for canceled sub: {customer_id} / {email}")

                        # Optional: Delete user from DB
                        conn = get_db_connection()
                        conn.execute("DELETE FROM users WHERE stripe_customer_id = ? OR email = ?", (customer_id, email))
                        conn.commit()
                        conn.close()
                except Exception as e:
                    logger.error(f"Sheet cancel clear failed: {e}")

    return '', 200


# First, update your RegisterForm class (add the code field)
class RegisterForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    code = StringField("Confirmation Code (from GHL install)", validators=[])  # Optional
    password = PasswordField("Password", validators=[DataRequired()])
    confirm = PasswordField("Confirm Password", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("Create Account")

@app.route("/register", methods=["GET", "POST"])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        code = form.code.data.upper().strip() if form.code.data else ""

        # Prevent duplicate registration
        if User.get(email):
            flash("This email is already registered, please log in.", "error")
            return redirect("/login")

        is_valid = False
        used_code_row = None

        # Option 1: User paid via Stripe (already has stripe_customer_id in session)
        if current_user.is_authenticated and current_user.stripe_customer_id:
            is_valid = True
            logger.info(f"Registering via existing Stripe subscription: {email}")

        # Option 2: User installed from GHL Marketplace and has confirmation code
        elif code and worksheet:
            try:
                values = worksheet.get_all_values()
                if not values:
                    flash("System error, please contact support", "error")
                    return redirect("/register")

                header = values[0]
                header_lower = [h.strip().lower() for h in header]

                def safe_col_index(name):
                    try:
                        return header_lower.index(name.lower())
                    except ValueError:
                        return -1

                code_idx = safe_col_index("confirmation_code")
                used_idx = safe_col_index("code_used")
                email_idx = safe_col_index("email")

                # Auto-add missing columns
                if code_idx == -1:
                    worksheet.update_cell(1, len(header) + 1, "confirmation_code")
                    code_idx = len(header)
                if used_idx == -1:
                    worksheet.update_cell(1, len(header) + 1, "code_used")
                    used_idx = len(header)

                # Find matching code
                for i, row in enumerate(values[1:], start=2):
                    row_code = row[code_idx].strip().upper() if len(row) > code_idx else ""
                    if row_code == code:
                        # Check if already used
                        is_used = len(row) > used_idx and row[used_idx].strip() == "1"
                        if is_used:
                            flash("This confirmation code has already been used.", "error")
                            return redirect("/register")

                        # Optional: verify email matches if present
                        row_email = row[email_idx].strip().lower() if email_idx >= 0 and len(row) > email_idx else ""
                        if row_email and row_email != email:
                            flash("This code is registered to a different email.", "error")
                            return redirect("/register")

                        used_code_row = i
                        is_valid = True
                        break

                if is_valid and used_code_row:
                    # Mark code as used
                    worksheet.update_cell(used_code_row, used_idx + 1, "1")
                    logger.info(f"Validated GHL confirmation code for {email}")

            except Exception as e:
                logger.error(f"Code validation error: {e}")
                flash("Error validating confirmation code, please try again", "error")
                return redirect("/register")

        # Final Validation
        if not is_valid:
            flash("Invalid confirmation code or no active subscription, please subscribe or use a valid code.", "error")
            return redirect("/register")

        # Create the user account
        password_hash = generate_password_hash(form.password.data)
        if User.create(email, password_hash):
            flash("Account created successfully! Please log in.", "success")
            return redirect("/login")
        else:
            flash("Account creation failed, please try again", "error")

    # GET request — show form
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Register - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO (no em dash) -->
    <meta name="description" content="Create your InsuranceGrokBot account, for GHL Marketplace installs or website subscribers.">
    <meta name="theme-color" content="#00ff88">

    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; text-align:center; padding:60px 20px; min-height:100vh; display:flex; align-items:center; justify-content:center; }
        .container { max-width:600px; width:100%; background:var(--card-bg); padding:60px; border-radius:20px; border:1px solid #333; box-shadow:0 10px 30px var(--neon-glow); }
        h1 { color:var(--accent); font-size:4rem; text-shadow:var(--neon-glow); margin-bottom:40px; }
        .note { font-size:1.6rem; color:#aaa; margin:30px 0; line-height:1.6; }
        .form-group { margin:30px 0; }
        label { font-size:1.6rem; display:block; margin-bottom:12px; color:#ddd; }
        input { width:100%; max-width:400px; padding:20px; background:#111; border:1px solid #333; color:#fff; border-radius:12px; font-size:1.6rem; }
        input::placeholder { color:#888; }
        button { padding:20px 60px; background:var(--accent); color:#000; font-weight:700; border:none; border-radius:50px; font-size:1.8rem; cursor:pointer; box-shadow:var(--neon-glow); margin-top:20px; }
        button:hover { background:#00cc70; transform:scale(1.05); }
        .flash { padding:20px; background:#1a1a1a; border-radius:12px; margin:20px 0; font-size:1.4rem; }
        .flash-error { border-left:5px solid #ff6b6b; }
        .flash-success { border-left:5px solid var(--accent); }
        .links { margin-top:40px; }
        .links a { color:var(--accent); text-decoration:underline; font-size:1.6rem; margin:0 15px; display:block; margin-bottom:15px; }
        .back { margin-top:60px; }
        .back a { color:#888; font-size:1.4rem; text-decoration:underline; }
        @media (max-width: 576px) {
            h1 { font-size:3rem; }
            .note { font-size:1.4rem; }
            label { font-size:1.4rem; }
            input { font-size:1.4rem; padding:18px; }
            button { font-size:1.6rem; padding:18px 50px; }
            .flash { font-size:1.3rem; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Create Your Account</h1>

        <p class="note">
            If you installed from <strong>GHL Marketplace</strong>, enter your confirmation code.<br>
            If you subscribed on this website, just use your email.
        </p>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <form method="post">
            {{ form.hidden_tag() }}

            <div class="form-group">
                {{ form.email.label }}
                {{ form.email(class="form-control", placeholder="your@email.com") }}
            </div>

            <div class="form-group">
                {{ form.code.label("Confirmation Code (GHL install only)") }}
                {{ form.code(class="form-control", placeholder="e.g. A1B2C3D4, leave blank if subscribed here") }}
            </div>

            <div class="form-group">
                {{ form.password.label }}
                {{ form.password(class="form-control") }}
            </div>

            <div class="form-group">
                {{ form.confirm.label }}
                {{ form.confirm(class="form-control") }}
            </div>

            {{ form.submit(class="button") }}
        </form>

        <div class="links">
            <a href="/checkout">Need to subscribe first?</a>
            <a href="/login">Already have an account? Log in</a>
        </div>

        <div class="back">
            <a href="/">Back to Home</a>
        </div>
    </div>
</body>
</html>
    """, form=form)

@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.lower()
        user = User.get(email)
        if user and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            flash("Logged in successfully!", "success")
            return redirect("/dashboard")
        else:
            flash("Invalid email or password", "error")

    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Log In - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO -->
    <meta name="description" content="Log in to your InsuranceGrokBot dashboard to manage your AI lead re-engagement bot.">
    <meta name="theme-color" content="#00ff88">

    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; text-align:center; padding:60px 20px; min-height:100vh; display:flex; align-items:center; justify-content:center; }
        .container { max-width:600px; width:100%; background:var(--card-bg); padding:60px; border-radius:20px; border:1px solid #333; box-shadow:0 10px 30px var(--neon-glow); }
        h1 { color:var(--accent); font-size:4rem; text-shadow:var(--neon-glow); margin-bottom:40px; }
        .form-group { margin:30px 0; }
        label { font-size:1.6rem; display:block; margin-bottom:12px; color:#ddd; }
        input { width:100%; max-width:400px; padding:20px; background:#111; border:1px solid #333; color:#fff; border-radius:12px; font-size:1.6rem; }
        input::placeholder { color:#888; }
        button { padding:20px 60px; background:var(--accent); color:#000; font-weight:700; border:none; border-radius:50px; font-size:1.8rem; cursor:pointer; box-shadow:var(--neon-glow); margin-top:20px; }
        button:hover { background:#00cc70; transform:scale(1.05); }
        .flash { padding:20px; background:#1a1a1a; border-radius:12px; margin:20px 0; font-size:1.4rem; }
        .flash-error { border-left:5px solid #ff6b6b; }
        .flash-success { border-left:5px solid var(--accent); }
        .links { margin-top:40px; }
        .links a { color:var(--accent); text-decoration:underline; font-size:1.6rem; margin:0 15px; display:block; margin-bottom:15px; }
        .back { margin-top:60px; }
        .back a { color:#888; font-size:1.4rem; text-decoration:underline; }
        @media (max-width: 576px) {
            h1 { font-size:3rem; }
            label { font-size:1.4rem; }
            input { font-size:1.4rem; padding:18px; }
            button { font-size:1.6rem; padding:18px 50px; }
            .flash { font-size:1.3rem; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Log In</h1>

        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="flash flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}

        <form method="post">
            {{ form.hidden_tag() }}

            <div class="form-group">
                {{ form.email.label }}
                {{ form.email(class="form-control", placeholder="your@email.com") }}
            </div>

            <div class="form-group">
                {{ form.password.label }}
                {{ form.password(class="form-control", placeholder="********") }}
            </div>

            {{ form.submit(class="button") }}
        </form>

        <div class="links">
            <a href="/register">Don't have an account? Register</a>
        </div>

        <div class="back">
            <a href="/">Back to Home</a>
        </div>
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
        headers = ["email", "location_id", "calendar_id", "crm_api_key", "crm_user_id", "bot_first_name", "timezone", "initial_message", "stripe_customer_id"]
        if worksheet:
            worksheet.append_row(headers)
        values = [headers]

    header = values[0] if values else []
    header_lower = [h.strip().lower() for h in header]

    def col_index(name):
        try:
            return header_lower.index(name.lower())
        except ValueError:
            try:
                return header.index(name)
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

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO -->
    <meta name="description" content="Manage your InsuranceGrokBot settings, configure GoHighLevel integration, and view billing.">
    <meta name="theme-color" content="#00ff88">

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
            <!-- Configuration Tab -->
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

                        <div style="text-align:center; margin-top:50px;">
                            {{ form.submit(class="button") }}
                        </div>
                    </form>
                </div>
            </div>

            <!-- GHL Setup Guide Tab -->
            <div class="tab-pane fade" id="guide">
                <div class="card guide-text">
                    <h2 style="color:var(--accent); text-align:center;">GoHighLevel Setup Guide</h2>
                    <p style="text-align:center; margin-bottom:30px;">Follow these steps to connect InsuranceGrokBot to your GHL account</p>
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
                                            <li><code>age</code>: "{{contact.custom_fields.age or 'unknown'}}"</li>
                                            <li><code>contact_address</code>: "{{contact.address1}}"</li>
                                            <li><code>agent_name</code>: "Your Name" (or "{{user.full_name}}")</li>
                                            <li><code>message</code>: "{{message.body}}"</li>
                                        </ul>
                                    </li>
                                </ul>
                            </li>
                            <li>Add <strong>Condition</strong>: If appointment booked, stop workflow</li>
                            <li>Else, Wait + same webhook, repeat</li>
                        </ol>

                        <h3 style="color:var(--accent); margin-top:40px;">Step 2: Create "AI SMS Handler" Workflow</h3>
                        <ol>
                            <li>New Workflow</li>
                            <li><strong>Trigger</strong>: Inbound SMS with tag "Re-engage text"</li>
                            <li>Add <strong>Wait</strong>: 2 minutes</li>
                            <li>Add <strong>Webhook</strong> (same URL and fields)</li>
                        </ol>

                        <h3 style="color:var(--accent); margin-top:40px;">Daily SMS Limits</h3>
                        <ul>
                            <li>GHL starts at <strong>100 outbound SMS/day</strong></li>
                            <li>Increases automatically when previous limit hit (250 next day, then higher)</li>
                            <li>Check in GHL Settings, Phone Numbers</li>
                        </ul>

                        <p style="text-align:center; margin-top:40px; font-weight:bold;">
                            Once set up, the bot runs 24/7, no more dead leads.
                        </p>
                    </div>
                    {% endraw %}
                </div>
            </div>

            <!-- Billing Tab -->
            <div class="tab-pane fade" id="billing">
                <div class="card billing-text">
                    <h2 style="color:var(--accent);">Billing</h2>
                    <p>Update payment method, view invoices, or cancel subscription</p>
                    <form method="post" action="/create-portal-session">
                        <button type="submit">Manage Billing on Stripe</button>
                    </form>
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

@app.route("/demo-chat")
def demo_chat():
    # Generate unique session ID for this visitor (no DB, fresh every time)
    if 'demo_session_id' not in session:
        session['demo_session_id'] = str(uuid.uuid4())

    demo_session_id = session['demo_session_id']

    demo_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <!-- Fixed viewport for iPhone safe areas (no cut-off by URL/taskbar) -->
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Live Demo - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO -->
    <meta name="description" content="Try a live demo of InsuranceGrokBot — the AI that re-engages cold life insurance leads.">
    <meta name="theme-color" content="#00ff88">

    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {{ --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        html, body {{
            height: 100%;
            margin: 0;
            padding: 0;
            background: var(--dark-bg);
            display: flex;
            justify-content: center;
            align-items: center;
            font-family: 'Montserrat', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            overflow: hidden;
        }}
        /* iPhone 16 Pro Max proportions (realistic) */
        .phone-container {{
            width: 100vw;
            height: 100vh;
            max-width: 430px; /* iPhone 16 Pro Max width */
            aspect-ratio: 9 / 19.6; /* Real iPhone 16 Pro Max ratio */
            padding: 10px;
            display: flex;
            flex-direction: column;
        }}
        .phone-frame {{
            flex: 1;
            background: #000;
            border-radius: 60px; /* iPhone 16 Pro Max corner radius */
            box-shadow: 0 40px 80px rgba(0,0,0,0.7), inset 0 0 15px rgba(255,255,255,0.05);
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}
        .notch {{
            position: absolute;
            top: 12px;
            left: 50%;
            transform: translateX(-50%);
            width: 200px;
            height: 36px;
            background: #000;
            border-radius: 24px;
            z-index: 10;
        }}
        .status-bar {{
            position: absolute;
            top: 18px;
            left: 24px;
            right: 24px;
            display: flex;
            justify-content: space-between;
            color: #fff;
            font-size: 14px;
            z-index: 11;
        }}
        .chat-area {{
            flex: 1;
            padding: 70px 24px 20px;
            overflow-y: auto;
            background: linear-gradient(to bottom, #1a1a1a, #0f0f0f);
            display: flex;
            flex-direction: column;
        }}
        .msg {{
            max-width: 80%;
            padding: 14px 20px;
            border-radius: 22px;
            margin-bottom: 16px;
            word-wrap: break-word;
            font-size: 17px;
            line-height: 1.4;
            align-self: flex-start;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }}
        .bot-msg {{
            background: #222;
            color: #fff;
            border-bottom-left-radius: 6px;
        }}
        .user-msg {{
            background: var(--accent);
            color: #000;
            align-self: flex-end;
            border-bottom-right-radius: 6px;
            font-weight: 600;
        }}
        .input-area {{
            padding: 15px 24px 40px;
            background: #111;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }}
        #user-input {{
            flex: 1;
            background: #222;
            border: none;
            border-radius: 25px;
            padding: 18px 25px;
            color: #fff;
            font-size: 18px;
            outline: none;
        }}
        #user-input::placeholder {{ color: #888; }}
        #send-btn {{
            background: #007aff; /* iMessage blue */
            border: none;
            border-radius: 50%;
            width: 44px;
            height: 44px;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 2px 8px rgba(0, 122, 255, 0.3);
            transition: background 0.2s;
        }}
        #send-btn:hover {{
            background: #0066d6;
        }}
        #send-btn svg {{
            width: 24px;
            height: 24px;
        }}
        .chat-area::-webkit-scrollbar {{ display: none; }}
    </style>
</head>
<body>
    <div class="phone-container">
        <div class="phone-frame">
            <div class="notch"></div>
            <div class="status-bar">
                <span>9:41 AM</span>
                <span>Signal • Battery</span>
            </div>
            <div id="chat-screen" class="chat-area">
                <div class="msg bot-msg">
                    Hey! Quick question — are you still with that life insurance plan you mentioned before?<br><br>
                    A lot of people have been asking about new living benefits that let you access money while you're still alive, and I wanted to make sure yours has that.
                </div>
            </div>
            <div class="input-area">
                <input type="text" id="user-input" placeholder="Type your reply..." autofocus>
                <button id="send-btn">
                    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="22 2 11 13"></polyline>
                        <polyline points="22 2 15 22 11 13 2 9 22 2"></polyline>
                    </svg>
                </button>
            </div>
        </div>
    </div>

    <script>
        const SESSION_ID = "{demo_session_id}";

        const input = document.getElementById('user-input');
        const sendBtn = document.getElementById('send-btn');
        const chat = document.getElementById('chat-screen');

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
                        locationId: 'DEMO_ACCOUNT_SALES_ONLY',
                        contact_id: SESSION_ID,
                        first_name: 'Visitor',
                        message: {{ body: msg }}
                    }})
                }});

                const data = await res.json();
                chat.innerHTML += `<div class="msg bot-msg">${{data.reply || 'Got it — thinking...'}}</div>`;
            }} catch (e) {{
                chat.innerHTML += `<div class="msg bot-msg">Connection issue — try again?</div>`;
            }}
            chat.scrollTop = chat.scrollHeight;
        }}

        input.addEventListener('keydown', e => {{
            if (e.key === 'Enter') {{
                e.preventDefault();
                sendMessage();
            }}
        }});

        sendBtn.addEventListener('click', sendMessage);
        input.focus();
    </script>
</body>
</html>
    """
    return render_template_string(demo_html)

@app.route("/terms")
def terms():
    terms_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Terms and Conditions - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO (no em dash) -->
    <meta name="description" content="Official Terms and Conditions for InsuranceGrokBot, AI-powered lead re-engagement for life insurance agents.">
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

    # On load/refresh: Reset DB for this test contact only
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM contact_messages WHERE contact_id = %s", (test_contact_id,))
            cur.execute("DELETE FROM contact_facts WHERE contact_id = %s", (test_contact_id,))
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
    <!-- Fixed viewport for iPhone safe areas (no cut-off by URL/taskbar) -->
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Test Chat - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        html, body {{
            height: 100%;
            margin: 0;
            padding: 0;
            background: #121212;
            display: flex;
            justify-content: center;
            align-items: center;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            overflow: hidden;
        }}
        /* iPhone 16 Pro Max proportions (realistic) */
        .phone-container {{
            width: 100vw;
            height: 100vh;
            max-width: 430px; /* iPhone 16 Pro Max width */
            aspect-ratio: 9 / 19.6; /* Real iPhone 16 Pro Max ratio */
            padding: 10px;
            display: flex;
            flex-direction: column;
        }}
        .phone-frame {{
            flex: 1;
            background: #000;
            border-radius: 60px; /* iPhone 16 Pro Max corner radius */
            box-shadow: 0 40px 80px rgba(0,0,0,0.7), inset 0 0 15px rgba(255,255,255,0.05);
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}
        .notch {{
            position: absolute;
            top: 12px;
            left: 50%;
            transform: translateX(-50%);
            width: 200px;
            height: 36px;
            background: #000;
            border-radius: 24px;
            z-index: 10;
        }}
        .status-bar {{
            position: absolute;
            top: 18px;
            left: 24px;
            right: 24px;
            display: flex;
            justify-content: space-between;
            color: #fff;
            font-size: 14px;
            z-index: 11;
        }}
        .chat-area {{
            flex: 1;
            padding: 70px 24px 20px;
            overflow-y: auto;
            background: linear-gradient(to bottom, #1a1a1a, #0f0f0f);
            display: flex;
            flex-direction: column;
        }}
        .msg {{
            max-width: 80%;
            padding: 14px 20px;
            border-radius: 22px;
            margin-bottom: 16px;
            word-wrap: break-word;
            font-size: 17px;
            line-height: 1.4;
            align-self: flex-start;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }}
        .bot-msg {{
            background: #222;
            color: #fff;
            border-bottom-left-radius: 6px;
        }}
        .user-msg {{
            background: #00ff88;
            color: #000;
            align-self: flex-end;
            border-bottom-right-radius: 6px;
            font-weight: 600;
        }}
        .input-area {{
            padding: 15px 24px 40px;
            background: #111;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }}
        #user-input {{
            flex: 1;
            background: #222;
            border: none;
            border-radius: 25px;
            padding: 18px 25px;
            color: #fff;
            font-size: 18px;
            outline: none;
        }}
        #user-input::placeholder {{ color: #888; }}
        #send-btn {{
            background: #007aff; /* iMessage blue */
            border: none;
            border-radius: 50%;
            width: 44px;
            height: 44px;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 2px 8px rgba(0, 122, 255, 0.3);
            transition: background 0.2s;
        }}
        #send-btn:hover {{
            background: #0066d6;
        }}
        #send-btn svg {{
            width: 24px;
            height: 24px;
        }}
        .chat-area::-webkit-scrollbar {{ display: none; }}

        /* Desktop: Side-by-side with log panel */
        @media (min-width: 768px) {{
            .container {{
                display: flex;
                width: 100%;
                height: 100%;
            }}
            .chat-column {{
                flex: 1;
                display: flex;
                justify-content: center;
                align-items: center;
                padding: 40px;
            }}
            .log-column {{
                flex: 1;
                padding: 40px;
                background: #0a0a0a;
                border-left: 1px solid #333;
            }}
            .log-panel {{
                background: #111;
                border-radius: 15px;
                padding: 30px;
                height: 100%;
                overflow-y: auto;
                box-shadow: 0 10px 20px rgba(0,0,0,0.3);
            }}
        }}

        /* Hide log panel on mobile */
        @media (max-width: 767px) {{
            .log-column {{ display: none; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="chat-column">
            <div class="phone-container">
                <div class="phone-frame">
                    <div class="notch"></div>
                    <div class="status-bar">
                        <span>11:55</span>
                        <span>Signal • Battery</span>
                    </div>
                    <div class="chat-area" id="chat-screen">
                        <div class="msg bot-msg">
                            Hey! Quick question — are you still with that life insurance plan you mentioned before?<br><br>
                            A lot of people have been asking about new living benefits that let you access money while you're still alive, and I wanted to make sure yours has that.
                        </div>
                    </div>
                    <div class="input-area">
                        <input type="text" id="user-input" placeholder="Type your reply..." autofocus>
                        <button id="send-btn">
                            <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="22 2 11 13"></polyline>
                                <polyline points="22 2 15 22 11 13 2 9 22 2"></polyline>
                            </svg>
                        </button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Log Panel (desktop only) -->
        <div class="log-column">
            <div class="log-panel">
                <h2 style="color:#00ff88; text-align:center;">Live Log Panel</h2>
                <div id="logs"></div>
                <div style="margin-top:40px; text-align:center;">
                    <button style="padding:14px 32px; background:#ff6b6b; color:#fff; border:none; border-radius:8px; cursor:pointer; font-size:18px;" onclick="resetChat()">Reset Chat</button>
                    <button style="padding:14px 32px; background:#00ff88; color:#000; border:none; border-radius:8px; cursor:pointer; font-size:18px; margin-left:20px;" onclick="downloadTranscript()">Download Transcript</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        const TEST_CONTACT_ID = '{test_contact_id}';

        const input = document.getElementById('user-input');
        const sendBtn = document.getElementById('send-btn');
        const chat = document.getElementById('chat-screen');
        const logs = document.getElementById('logs');

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
                        locationId: 'TEST_LOCATION_456',
                        contact_id: TEST_CONTACT_ID,
                        first_name: 'Test User',
                        message: {{ body: msg }},
                        age: '1980-01-01',
                        address: '123 Test St, Houston, TX'
                    }})
                }});

                const data = await res.json();
                chat.innerHTML += `<div class="msg bot-msg">${{data.reply || 'Thinking...'}}</div>`;
                chat.scrollTop = chat.scrollHeight;
                fetchLogs();
            }} catch (e) {{
                chat.innerHTML += `<div class="msg bot-msg">Connection error — try again</div>`;
                chat.scrollTop = chat.scrollHeight;
            }}
        }}

        
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' || e.key === 'Return') {
                e.preventDefault();
                
                sendMessage();
            }
        });

        sendBtn.addEventListener('click', sendMessage);

        function fetchLogs() {{
            fetch(`/get-logs?contact_id=${{TEST_CONTACT_ID}}`)
                .then(res => res.json())
                .then(data => {{
                    logs.innerHTML = '';
                    data.logs.forEach(log => {{
                        logs.innerHTML += `
                            <div style="margin-bottom:30px; padding:18px; background:#1a1a1a; border-radius:12px;">
                                <h4 style="color:#00ff88; margin-bottom:10px;">[${{log.timestamp}}] ${{log.type}}</h4>
                                <p style="white-space: pre-wrap; color:#ddd; font-size:16px;">${{log.content.replace(/\\n/g, '<br>')}}</p>
                            </div>
                        `;
                    }});
                    logs.scrollTop = logs.scrollHeight;
                }});
        }}

        async function resetChat() {{
            await fetch(`/reset-test?contact_id=${{TEST_CONTACT_ID}}`);
            chat.innerHTML = '<div class="msg bot-msg">Hey! Quick question — are you still with that life insurance plan you mentioned before?<br><br>A lot of people have been asking about new living benefits that let you access money while you're still alive, and I wanted to make sure yours has that.</div>';
            logs.innerHTML = '<p style="color:#aaa; text-align:center; padding:40px;">Chat reset — logs cleared.</p>';
            chat.scrollTop = chat.scrollHeight;
        }}

        async function downloadTranscript() {{
            const res = await fetch(`/download-transcript?contact_id=${{TEST_CONTACT_ID}}`);
            const blob = await res.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `insurancegrokbot_test_${{TEST_CONTACT_ID}}.txt`;
            a.click();
            window.URL.revokeObjectURL(url);
        }}

        setInterval(fetchLogs, 5000);
        fetchLogs();
    </script>
</body>
</html>
    """
    return render_template_string(test_html, test_contact_id=test_contact_id)

@app.route("/get-logs", methods=["GET"])
def get_logs():
    contact_id = request.args.get("contact_id")

    # Security check
    if not contact_id or not contact_id.startswith("test_"):
        logger.warning(f"Invalid log request: {contact_id}")
        return jsonify({"error": "Invalid test contact"}), 400

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
            timestamp = created_at.isoformat() if created_at else "Unknown time"
            logs.append({
                "timestamp": timestamp,
                "type": f"{role} Message",
                "content": text.strip()
            })

        # === 2. Current Known Facts ===
        facts = get_known_facts(contact_id)
        fact_content = "\n".join([f"• {f}" for f in facts]) if facts else "No facts extracted yet"
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "type": "Known Facts (Current Memory)",
            "content": fact_content
        })

        # === 3. Full Profile Narrative (What Grok Actually "Knows") ===
        # Extract basics for profile rebuild
        first_name = None
        age = None
        address = None

        facts_text = " ".join(facts).lower()
        name_match = re.search(r"first name: (\w+)", facts_text, re.IGNORECASE)
        if name_match:
            first_name = name_match.group(1).capitalize()

        age_match = re.search(r"age: (\d+)", facts_text)
        if age_match:
            age = age_match.group(1)

        addr_match = re.search(r"address/location: (.*)", facts_text, re.IGNORECASE)
        if addr_match:
            address = addr_match.group(1).strip()

        profile_narrative = build_comprehensive_profile(
            known_facts=facts,
            first_name=first_name,
            age=age,
            address=address
        )

        logs.append({
            "timestamp": datetime.now().isoformat(),
            "type": "Full Human Identity Narrative",
            "content": profile_narrative
        })

        # === 4. Bot Reasoning Summary ===
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "type": "Bot Reasoning Trace",
            "content": "• Rebuilt complete human story narrative\n• Reviewed known facts and emotional gaps\n• Selected optimal sales framework(s)\n• Generated natural, empathetic response"
        })

    except Exception as e:
        logger.error(f"Error in get_logs for {contact_id}: {e}")
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "type": "Error",
            "content": f"Failed to load logs: {str(e)}"
        })
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
        
        # Delete messages and facts for this test contact only
        cur.execute("DELETE FROM contact_messages WHERE contact_id = %s", (contact_id,))
        cur.execute("DELETE FROM contact_facts WHERE contact_id = %s", (contact_id,))
        
        conn.commit()
        
        deleted_messages = cur.rowcount  # Optional: how many rows deleted
        logger.info(f"Successfully reset test contact {contact_id} — cleared messages and facts")
        
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

    # Rebuild the current profile narrative (this is gold for debugging!)
    # Extract basics for profile (in case not in facts)
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

    transcript_lines.append("CURRENT BOT UNDERSTANDING OF THIS PERSON")
    transcript_lines.append("-" * 50)
    transcript_lines.extend(profile_narrative.split("\n"))
    transcript_lines.append("")

    transcript_lines.append("KNOWN FACTS (Raw Memory)")
    transcript_lines.append("-" * 30)
    if facts:
        for fact in facts:
            transcript_lines.append(f"• {fact}")
    else:
        transcript_lines.append("No facts extracted yet")
    transcript_lines.append("")

    transcript_lines.append("CONVERSATION HISTORY")
    transcript_lines.append("-" * 30)
    for msg in messages:
        role = "USER" if msg['role'] == "lead" else "BOT"
        timestamp = msg.get('created_at', 'Unknown time')
        if isinstance(timestamp, datetime):
            timestamp = timestamp.strftime('%Y-%m-%d %H:%M:%S')

        transcript_lines.append(f"[{timestamp}] {role}:")
        transcript_lines.append(msg['text'])
        transcript_lines.append("")

        # Add thinking trace for bot messages
        if role == "BOT":
            transcript_lines.append("<thinking>")
            transcript_lines.append("• Rebuilt full human identity narrative")
            transcript_lines.append("• Reviewed known facts and emotional gaps")
            transcript_lines.append("• Chose optimal sales framework(s) for this moment")
            transcript_lines.append("• Generated empathetic, natural response")
            transcript_lines.append("</thinking>")
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
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{
                "price": os.getenv("STRIPE_PRICE_ID"),
                "quantity": 1,
            }],
            allow_promotion_codes=True,
            customer_creation="always",
            customer_email=None,  # Forces email entry in Stripe checkout
            subscription_data={
                "metadata": {
                    "source": "website"
                }
            },
            success_url=f"{YOUR_DOMAIN}/success",
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
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Payment Error - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO (no em dash) -->
    <meta name="description" content="Payment error, please try again or contact support.">
    <meta name="theme-color" content="#ff6b6b">

    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { 
            --accent: #00ff88; 
            --error: #ff6b6b; 
            --dark-bg: #000; 
            --card-bg: #0a0a0a; 
            --neon-glow: rgba(255, 107, 107, 0.5); 
        }
        body { 
            background:var(--dark-bg); 
            color:#fff; 
            font-family:'Montserrat',sans-serif; 
            text-align:center; 
            padding:60px 20px; 
            min-height:100vh; 
            display:flex; 
            align-items:center; 
            justify-content:center; 
        }
        .container { 
            max-width:700px; 
            width:100%; 
            background:var(--card-bg); 
            padding:60px; 
            border-radius:20px; 
            border:1px solid #333; 
            box-shadow:0 10px 30px var(--neon-glow); 
        }
        h1 { 
            color:var(--error); 
            font-size:4rem; 
            text-shadow:0 0 20px var(--neon-glow); 
            margin-bottom:40px; 
        }
        p { 
            font-size:1.6rem; 
            margin:30px 0; 
            color:#ddd; 
            line-height:1.6; 
        }
        .btn { 
            display:block; 
            width:fit-content; 
            margin:30px auto; 
            padding:18px 60px; 
            background:var(--accent); 
            color:#000; 
            font-weight:700; 
            border-radius:50px; 
            box-shadow:0 5px 20px rgba(0, 255, 136, 0.5); 
            font-size:1.6rem; 
            text-decoration:none; 
            transition:0.3s; 
        }
        .btn:hover { 
            transform:scale(1.05); 
            background:#00cc70; 
        }
        .back { 
            margin-top:40px; 
        }
        .back a { 
            color:#888; 
            font-size:1.4rem; 
            text-decoration:underline; 
        }
        @media (max-width: 576px) {
            h1 { font-size:3rem; }
            p { font-size:1.4rem; }
            .btn { font-size:1.4rem; padding:16px 50px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Payment Error</h1>
        <p>Something went wrong while starting checkout.</p>
        <p>Please try again, your card has not been charged.</p>
        <a href="/" class="btn">Back to Home</a>
        <a href="/checkout" class="btn">Try Checkout Again</a>
        <div class="back">
            <a href="/getting-started">Need help? View Getting Started Guide</a>
        </div>
    </div>
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
    <title>Checkout Canceled - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO (no em dash) -->
    <meta name="description" content="Checkout canceled, no worries. Come back anytime to subscribe to InsuranceGrokBot.">
    <meta name="theme-color" content="#00ff88">

    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; text-align:center; padding:60px 20px; min-height:100vh; display:flex; align-items:center; justify-content:center; }
        .container { max-width:700px; width:100%; background:var(--card-bg); padding:60px; border-radius:20px; border:1px solid #333; box-shadow:0 10px 30px var(--neon-glow); }
        h1 { color:var(--accent); font-size:4rem; text-shadow:var(--neon-glow); margin-bottom:40px; }
        p { font-size:1.6rem; margin:30px 0; color:#ddd; line-height:1.6; }
        .btn { display:block; width:fit-content; margin:40px auto 20px; padding:18px 60px; background:var(--accent); color:#000; font-weight:700; border-radius:50px; 
               box-shadow:var(--neon-glow); font-size:1.6rem; text-decoration:none; transition:0.3s; }
        .btn:hover { transform:scale(1.05); background:#00cc70; }
        .back { margin-top:20px; }
        .back a { color:#888; font-size:1.4rem; text-decoration:underline; }
        @media (max-width: 576px) {
            h1 { font-size:3rem; }
            p { font-size:1.4rem; }
            .btn { font-size:1.4rem; padding:16px 50px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Checkout Canceled</h1>
        <p>No worries at all, your card wasn't charged.</p>
        <p>Come back anytime when you're ready to start re-engaging those old leads.</p>
        <a href="/" class="btn">Back to Home</a>
        <div class="back">
            <a href="/getting-started">Or see the Getting Started guide</a>
        </div>
    </div>
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

    if email:
        user = User.get(email)
        if user and not user.password_hash:
            # User paid but hasn't set password yet
            return render_template_string(f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Set Password - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO (no em dash) -->
    <meta name="description" content="Set your password to access InsuranceGrokBot dashboard.">
    <meta name="theme-color" content="#00ff88">

    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {{ --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }}
        body {{ background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; text-align:center; padding:60px 20px; min-height:100vh; display:flex; align-items:center; justify-content:center; }}
        .container {{ max-width:600px; width:100%; background:var(--card-bg); padding:50px; border-radius:20px; border:1px solid #333; box-shadow:0 10px 30px var(--neon-glow); }}
        h1 {{ color:var(--accent); font-size:3.5rem; text-shadow:var(--neon-glow); margin-bottom:40px; }}
        p {{ font-size:1.6rem; margin:20px 0; }}
        input {{ width:100%; max-width:400px; padding:18px; background:#111; border:1px solid #333; color:#fff; border-radius:12px; font-size:18px; margin:15px 0; }}
        input::placeholder {{ color:#888; }}
        button {{ padding:18px 50px; background:var(--accent); color:#000; font-weight:700; border:none; border-radius:50px; font-size:1.6rem; cursor:pointer; box-shadow:var(--neon-glow); margin-top:20px; }}
        button:hover {{ background:#00cc70; transform:scale(1.05); }}
        .back {{ margin-top:40px; }}
        .back a {{ color:#888; font-size:1.3rem; text-decoration:underline; }}
        @media (max-width: 576px) {{
            h1 {{ font-size:2.8rem; }}
            p {{ font-size:1.4rem; }}
            button {{ font-size:1.4rem; padding:16px 40px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Almost Done!</h1>
        <p>Set a password for your account:</p>
        <p style="font-weight:bold; font-size:1.8rem; color:var(--accent); margin:30px 0;">{email}</p>

        <form action="/set-password" method="post">
            <input type="hidden" name="email" value="{email}">
            <input type="password" name="password" placeholder="Choose a password" required>
            <input type="password" name="confirm" placeholder="Confirm password" required>
            <button type="submit">Set Password & Log In</button>
        </form>

        <div class="back">
            <a href="/">Back to Home</a>
        </div>
    </div>
</body>
</html>
            """)

    # Generic success (existing user or no email)
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Thank You - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO (no em dash) -->
    <meta name="description" content="Thank you for subscribing to InsuranceGrokBot, your AI lead re-engagement tool.">
    <meta name="theme-color" content="#00ff88">

    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {{ --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }}
        body {{ background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; text-align:center; padding:60px 20px; min-height:100vh; display:flex; align-items:center; justify-content:center; }}
        .container {{ max-width:700px; width:100%; background:var(--card-bg); padding:60px; border-radius:20px; border:1px solid #333; box-shadow:0 15px 40px var(--neon-glow); }}
        h1 {{ color:var(--accent); font-size:4rem; text-shadow:var(--neon-glow); margin-bottom:40px; }}
        p {{ font-size:1.6rem; margin:30px 0; color:#ddd; }}
        .btn {{ display:inline-block; padding:18px 50px; background:var(--accent); color:#000; font-weight:700; border-radius:50px; 
               box-shadow:var(--neon-glow); margin:20px; font-size:1.6rem; text-decoration:none; transition:0.3s; }}
        .btn:hover {{ transform:scale(1.05); background:#00cc70; }}
        .back {{ margin-top:60px; }}
        .back a {{ color:#888; font-size:1.4rem; text-decoration:underline; }}
        @media (max-width: 576px) {{
            h1 {{ font-size:3rem; }}
            p {{ font-size:1.4rem; }}
            .btn {{ font-size:1.4rem; padding:16px 40px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Thank You!</h1>
        <p>Your subscription is now active.</p>
        <p>You can start using InsuranceGrokBot right away.</p>

        <a href="/dashboard" class="btn">Go to Dashboard</a>
        <a href="/login" class="btn">Log In</a>

        <div class="back">
            <a href="/">Back to Home</a>
        </div>
    </div>
</body>
</html>
    """)

@app.route("/set-password", methods=["POST"])
def set_password():
    email = request.form.get("email").lower()
    password = request.form.get("password")
    confirm = request.form.get("confirm")

    if password != confirm:
        flash("Passwords don't match")
        return redirect("/success")

    user = User.get(email)
    if user:
        password_hash = generate_password_hash(password)
        conn = get_db_connection()
        conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (password_hash, email))
        conn.commit()
        conn.close()
        login_user(user)
        flash("Password set — welcome!")
        return redirect("/dashboard")

    flash("User not found")
    return redirect("/success")

@app.route("/refresh")
def refresh_subscribers():
    """Manually trigger a sync from Google Sheets via URL."""
    try:
        sync_subscribers()
        return "<h1>Success!</h1><p>Subscriber database updated from Google Sheets.</p>", 200
    except Exception as e:
        return f"<h1>Sync Failed</h1><p>{str(e)}</p>", 500
    
@app.route("/oauth/callback")
def oauth_callback():
    import uuid  # Add at top of file if not already there

    # Capture all params GHL sends on Marketplace install
    location_id = request.args.get("locationId")
    user_id = request.args.get("userId") or request.args.get("user_id")
    api_key = request.args.get("apiKey") or request.args.get("api_key")
    calendar_id = request.args.get("calendarId") or request.args.get("calendar_id")

    if not location_id:
        return "Error: Missing locationId from GoHighLevel", 400

    # Generate a unique confirmation code for this install
    confirmation_code = str(uuid.uuid4())[:8].upper()  # e.g. A1B2C3D4

    # AUTO-SAVE TO GOOGLE SHEET (Only on real installs)
    if worksheet:
        try:
            values = worksheet.get_all_values()
            if not values:
                values = []

            # Expected headers (in order) — ADD confirmation_code and code_used
            expected_headers = [
                "email", "location_id", "calendar_id", "crm_api_key",
                "crm_user_id", "bot_first_name", "timezone", "initial_message",
                "confirmation_code", "code_used"
            ]

            # Create headers if missing or incomplete
            if not values or values[0] != expected_headers:
                worksheet.update('A1:J1', [expected_headers])  # J = 10 columns
                values = [expected_headers] + values

            header = values[0]
            header_lower = [h.strip().lower() for h in header]

            def col_index(name):
                try:
                    return header_lower.index(name.lower())
                except ValueError:
                    return -1

            location_idx = col_index("location_id")
            calendar_idx = col_index("calendar_id")
            api_key_idx = col_index("crm_api_key")
            user_id_idx = col_index("crm_user_id")
            bot_name_idx = col_index("bot_first_name")
            timezone_idx = col_index("timezone")
            initial_msg_idx = col_index("initial_message")
            code_idx = col_index("confirmation_code")
            used_idx = col_index("code_used")

            # Find row by location_id
            row_num = None
            for i, row in enumerate(values[1:], start=2):
                if location_idx >= 0 and len(row) > location_idx and row[location_idx].strip() == location_id:
                    row_num = i
                    break

            # Build data row
            data = [""] * len(expected_headers)
            if location_idx >= 0: data[location_idx] = location_id
            if calendar_idx >= 0: data[calendar_idx] = calendar_id or ""
            if api_key_idx >= 0: data[api_key_idx] = api_key or ""
            if user_id_idx >= 0: data[user_id_idx] = user_id or ""
            if bot_name_idx >= 0: data[bot_name_idx] = "Grok"
            if timezone_idx >= 0: data[timezone_idx] = "America/Chicago"
            if initial_msg_idx >= 0: data[initial_msg_idx] = ""
            if code_idx >= 0: data[code_idx] = confirmation_code
            if used_idx >= 0: data[used_idx] = "0"  # Not used yet

            # Write to sheet
            if row_num:
                worksheet.update(f"A{row_num}:J{row_num}", [data])
            else:
                worksheet.append_row(data)

            sync_subscribers()
            logger.info(f"Synced subscribers after Marketplace install for {location_id}")

            logger.info(f"Auto-saved Marketplace install with code {confirmation_code} for location_id={location_id}")
        except Exception as e:
            logger.error(f"Sheet auto-save failed: {e}")

    # Success Page — Show Confirmation Code
    success_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Install Complete - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO -->
    <meta name="description" content="InsuranceGrokBot successfully installed via GoHighLevel Marketplace. Create your account using your confirmation code.">
    <meta name="theme-color" content="#00ff88">

    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {{ --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }}
        body {{ background-color: var(--dark-bg); color: #fff; font-family: 'Montserrat', sans-serif; padding: 60px; }}
        .container {{ max-width: 800px; margin: auto; text-align: center; }}
        h1 {{ color: var(--accent); text-shadow: 0 0 10px var(--neon-glow); font-size: 42px; margin-bottom: 40px; }}
        p {{ font-size: 20px; margin: 20px 0; color: #ddd; }}
        .info {{ background: var(--card-bg); padding: 30px; border-radius: 15px; border: 1px solid #333; box-shadow: 0 5px 20px var(--neon-glow); }}
        .code-box {{ background: #111; padding: 20px; border-radius: 15px; font-size: 36px; letter-spacing: 8px; font-weight: bold; color: var(--accent); margin: 30px 0; text-shadow: 0 0 15px var(--neon-glow); }}
        .btn {{ display: inline-block; padding: 15px 40px; background: linear-gradient(135deg, var(--accent), #00b36d); color: #000; font-weight: 700; text-decoration: none; border-radius: 50px; box-shadow: 0 5px 15px var(--neon-glow); transition: 0.3s; margin: 20px; font-size: 20px; }}
        .btn:hover {{ transform: scale(1.05); box-shadow: 0 10px 25px var(--neon-glow); }}
        .back-link {{ color: #aaa; font-size: 18px; text-decoration: underline; }}
        @media (max-width: 576px) {{
            body {{ padding: 40px 20px; }}
            h1 {{ font-size: 36px; }}
            .code-box {{ font-size: 28px; letter-spacing: 6px; }}
            .btn {{ padding: 14px 36px; font-size: 18px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>InsuranceGrokBot Installed Successfully!</h1>
        <div class="info">
            <p><strong>Location ID:</strong> {location_id or 'Not provided'}</p>
            <p><strong>User ID:</strong> {user_id or 'Not provided'}</p>
            <p><strong>API Key:</strong> {'Captured & Saved' if api_key else 'Not provided'}</p>
            <p><strong>Calendar ID:</strong> {calendar_id or 'Not provided'}</p>
        </div>
        <p>Your bot is now automatically configured!</p>
        <p>To finish setup and create your login:</p>
        <div class="code-box">{confirmation_code}</div>
        <p>Copy this code and go to our website to register your account.</p>
        <a href="/register" class="btn">Register Your Account Now</a>
        <p style="margin-top:40px;"><a href="/" class="back-link">Back to Home</a></p>
    </div>
</body>
</html>
    """
    return render_template_string(success_html)

@app.route("/getting-started")
def getting_started():
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <!-- FIXED VIEWPORT — essential for mobile -->
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Getting Started - InsuranceGrokBot</title>

    <!-- Favicon -->
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">

    <!-- SEO -->
    <meta name="description" content="Step-by-step guide to set up InsuranceGrokBot — the AI that re-engages your cold life insurance leads 24/7.">
    <meta name="theme-color" content="#00ff88">

    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; padding:40px 20px; min-height:100vh; }
        .container { max-width:1000px; margin:auto; }
        h1 { color:var(--accent); font-size:3.5rem; text-shadow:var(--neon-glow); text-align:center; margin-bottom:60px; }
        .tab-buttons { 
            display: flex; 
            flex-direction: column; 
            gap: 15px; 
            margin-bottom: 60px; 
            max-width: 600px;
            margin-left: auto;
            margin-right: auto;
        }
        .tab-btn {
            padding: 20px;
            background: #111;
            color: #aaa;
            text-align: center;
            font-size: 1.4rem;
            font-weight: 600;
            border-radius: 15px;
            border: 2px solid #333;
            transition: all 0.3s;
        }
        .tab-btn.active, .tab-btn:hover {
            background: #222;
            color: var(--accent);
            border-color: var(--accent);
            box-shadow: var(--neon-glow);
        }
        .step { 
            background: var(--card-bg); 
            border-radius: 20px; 
            padding: 40px; 
            margin:40px 0; 
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        }
        .step-number { 
            font-size:4rem; 
            font-weight:700; 
            color:var(--accent); 
            text-shadow:var(--neon-glow); 
            margin-bottom:20px; 
            text-align:center;
        }
        .step h2 { 
            color:var(--accent); 
            font-size:2rem; 
            text-align:center; 
            margin-bottom:25px;
        }
        .step p { 
            font-size:1.3rem; 
            line-height:1.8; 
            margin:20px 0;
        }
        .highlight { 
            background:#111; 
            padding:20px; 
            border-radius:12px; 
            font-family:monospace; 
            color:#ddd; 
            margin:25px 0; 
            font-size:1.1rem;
        }
        .btn { 
            display:block; 
            width: fit-content;
            margin: 40px auto;
            padding:18px 50px; 
            background:var(--accent); 
            color:#000; 
            font-weight:700; 
            border-radius:50px; 
            box-shadow:var(--neon-glow); 
            font-size:1.4rem; 
            text-decoration:none; 
            transition:0.3s;
            text-align:center;
        }
        .btn:hover { 
            transform:scale(1.05); 
            background:#00cc70; 
        }
        .screenshot-note { 
            font-style:italic; 
            color:#aaa; 
            font-size:1.1rem; 
            margin-top:15px;
            text-align:center;
        }
        .back { 
            text-align:center; 
            margin-top:80px; 
            font-size:1.3rem;
        }
        .back a { 
            color:#888; 
            text-decoration:underline; 
        }

        /* Mobile adjustments */
        @media (max-width: 768px) {
            h1 { font-size:2.8rem; margin-bottom:40px; }
            .step { padding:30px; margin:30px 0; }
            .step-number { font-size:3.5rem; }
            .step h2 { font-size:1.8rem; }
            .step p { font-size:1.2rem; }
            .highlight { font-size:1rem; padding:15px; }
            .btn { font-size:1.3rem; padding:16px 40px; }
            .tab-btn { font-size:1.3rem; padding:18px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>How to Get Your Bot Running</h1>
        <p style="text-align:center; font-size:1.6rem; margin-bottom:50px;">Choose your setup path:</p>

        <div class="tab-buttons">
            <div class="tab-btn active" onclick="openTab(event, 'website')">Subscribed on Website</div>
            <div class="tab-btn" onclick="openTab(event, 'marketplace')">Installed from GHL Marketplace</div>
        </div>

        <div id="website" class="tab-content">
            <div class="step">
                <div class="step-number">1</div>
                <h2>Subscribe to InsuranceGrokBot</h2>
                <p>Click the large green "Subscribe Now" button on the home page.</p>
                <p>You will be taken to a secure payment page to complete your $100/month subscription.</p>
                <p class="screenshot-note">You can cancel at any time with no questions asked.</p>
            </div>

            <div class="step">
                <div class="step-number">2</div>
                <h2>Complete Your Payment</h2>
                <p>Enter your card details and click "Subscribe".</p>
                <p>Your account will be created automatically using the email you provide.</p>
            </div>

            <div class="step">
                <div class="step-number">3</div>
                <h2>Create Your Password and Log In</h2>
                <p>After payment, you will be prompted to set a password.</p>
                <p>Once set, log in to access your personal dashboard.</p>
                <a href="/login" class="btn">Go to Log In</a>
            </div>

            <div class="step">
                <div class="step-number">4</div>
                <h2>Connect Your GoHighLevel Account</h2>
                <p>In your dashboard, go to the "Configuration" tab.</p>
                <p>Enter the following details from your GoHighLevel account:</p>
                <ul style="text-align:left; max-width:700px; margin:30px auto; font-size:1.3rem;">
                    <li><strong>Location ID</strong>: Found in GHL Settings or in the browser URL when viewing your agency.</li>
                    <li><strong>API Key</strong>: Go to Settings → API Keys → create and copy a new key.</li>
                    <li><strong>User ID</strong>: Go to Settings → My Profile → copy the User ID.</li>
                    <li><strong>Calendar ID</strong>: Go to Calendars → click your main calendar → copy the ID from the URL.</li>
                    <li><strong>Bot First Name</strong>: Choose a friendly name like "Alex" or "Jordan".</li>
                    <li><strong>Initial Message</strong>: Optional — the first message the bot sends (leave blank for default).</li>
                </ul>
                <p>Click the "Save Settings" button when finished.</p>
                <a href="/dashboard" class="btn">Go to Dashboard (after logging in)</a>
            </div>

            <div class="step">
                <div class="step-number">5</div>
                <h2>Create Two Workflows in GoHighLevel</h2>
                <p>Go to GoHighLevel → Automations → Workflows → Create New Workflow.</p>
                <p>Create these two workflows:</p>

                <div class="highlight">
                    <strong>Workflow 1: Re-Engage Old Leads</strong><br>
                    Trigger: Tag Added (create tag "Re-Engage")<br>
                    Add Wait: 10 minutes<br>
                    Add Webhook:<br>
                    URL: <code>https://insurancegrokbot.click/webhook</code><br>
                    Method: POST<br>
                    Body Fields:<br>
                    intent="reengage", first_name="{{contact.first_name}}", contact_id="{{contact.id}}", age="{{contact.date_of_birth}}", address="{{contact.address1}} {{contact.city}}, {{contact.state}}"
                </div>

                <div class="highlight">
                    <strong>Workflow 2: Handle Incoming Texts</strong><br>
                    Trigger: Inbound SMS (from contacts with "Re-Engage" tag)<br>
                    Add Wait: 2 minutes<br>
                    Add Webhook (same URL and fields as above)
                </div>

                <p>Apply the "Re-Engage" tag to your old leads — the bot will start texting them automatically.</p>
                <p class="screenshot-note">Need screenshots or a video? Just email support.</p>
            </div>
        </div>

        <div id="marketplace" class="tab-content" style="display:none;">
            <div class="step">
                <div class="step-number">1</div>
                <h2>Install from GoHighLevel Marketplace</h2>
                <p>In GoHighLevel, open the App Marketplace (use search if needed).</p>
                <p>Search for "InsuranceGrokBot", click the app, then click "Install App".</p>
                <p>On the next screen, click "Allow & Install".</p>
                <p class="screenshot-note">This securely connects your GHL account to the bot.</p>
            </div>

            <div class="step">
                <div class="step-number">2</div>
                <h2>Get Your Confirmation Code</h2>
                <p>After installation, you will see a success page with your unique confirmation code (example: A1B2C3D4).</p>
                <p>Copy this code — you will need it in the next step.</p>
            </div>

            <div class="step">
                <div class="step-number">3</div>
                <h2>Create Your Account on Our Website</h2>
                <p>Visit: <code>https://insurancegrokbot.click/register</code></p>
                <p>Enter your email, the confirmation code from step 2, and choose a password.</p>
                <p>Your account will be created and linked instantly.</p>
                <a href="/register" class="btn">Go to Register Page</a>
            </div>

            <div class="step">
                <div class="step-number">4</div>
                <h2>Optional: Customize Bot Settings</h2>
                <p>Log in to your dashboard and go to the "Configuration" tab.</p>
                <p>Most settings (API key, location, etc.) were set automatically during install.</p>
                <p>You can change the bot's first name or initial message if you'd like.</p>
                <p>Click "Save Settings" when done.</p>
                <a href="/dashboard" class="btn">Go to Dashboard (after logging in)</a>
            </div>

            <div class="step">
                <div class="step-number">5</div>
                <h2>Create Two Workflows in GoHighLevel</h2>
                <p>Go to Automations → Workflows → Create New Workflow.</p>
                <p>Create these two:</p>

                <div class="highlight">
                    <strong>Workflow 1: Re-Engage Old Leads</strong><br>
                    Trigger: Tag Added ("Re-Engage")<br>
                    Wait 10 minutes<br>
                    Webhook: URL <code>https://insurancegrokbot.click/webhook</code>, POST<br>
                    Fields: intent="reengage", first_name="{{contact.first_name}}", contact_id="{{contact.id}}", age="{{contact.date_of_birth}}", address="{{contact.address1}} {{contact.city}}, {{contact.state}}"
                </div>

                <div class="highlight">
                    <strong>Workflow 2: Handle Incoming Texts</strong><br>
                    Trigger: Inbound SMS (for "Re-Engage" tagged contacts)<br>
                    Wait 2 minutes<br>
                    Webhook (same as above)
                </div>

                <p>Apply the "Re-Engage" tag to your old leads — the bot begins working immediately.</p>
                <p class="screenshot-note">Need help with screenshots? Just email support.</p>
            </div>
        </div>

        <div class="back">
            <a href="/">← Back to Home</a>
        </div>
    </div>

    <script>
        function openTab(evt, tabName) {
            var i, tabcontent, tabbtns;
            tabcontent = document.getElementsByClassName("tab-content");
            for (i = 0; i < tabcontent.length; i++) {
                tabcontent[i].style.display = "none";
            }
            tabbtns = document.getElementsByClassName("tab-btn");
            for (i = 0; i < tabbtns.length; i++) {
                tabbtns[i].className = tabbtns[i].className.replace(" active", "");
            }
            document.getElementById(tabName).style.display = "block";
            evt.currentTarget.className += " active";
        }
        // Default open first tab
        document.getElementsByClassName("tab-btn")[0].click();
    </script>
</body>
</html>
    """
    return render_template_string(html)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)