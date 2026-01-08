# main.py - Clean Final Version (2026)
import logging
import re
import uuid
import stripe
from openai import OpenAI
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify, session
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
from age import calculate_age_from_dob
from sync_subscribers import sync_subscribers

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

    # 1. Identity Lookup
    location_id = payload.get("locationId")
    if not location_id:
        return jsonify({"status": "error", "message": "Missing locationId"}), 400

    is_demo = (location_id == 'DEMO_ACCOUNT_SALES_ONLY')

    if is_demo:
        subscriber = {
            'bot_first_name': 'Grok',
            'crm_api_key': 'DEMO',
            'crm_user_id': '',
            'calendar_id': '',
            'timezone': 'America/Chicago'
        }
        contact_id = payload.get("contact_id")
        if not contact_id:
            logger.warning("Demo webhook missing contact_id — rejecting")
            return jsonify({"status": "error", "message": "Invalid demo session"}), 400
    else:
        subscriber = get_subscriber_info(location_id)
        if not subscriber or not subscriber.get('bot_first_name'):
            logger.error(f"Identity not configured for {location_id}")
            return jsonify({"status": "error", "message": "Not configured"}), 404
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
    lead_vendor = payload.get("lead_vendor", "")

    if len(assistant_messages) == 0 and initial_message:
        reply = initial_message

        save_message(contact_id, reply, "assistant")
        if not is_demo and crm_api_key != 'DEMO':
            send_sms_via_ghl(contact_id, reply, crm_api_key, location_id)

        return jsonify({
            "status": "success",
            "reply": reply
        })
    

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
        context_nudge=context_nudge
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

@app.route("/") # Website 
def home():
    home_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>InsuranceGrokBot | AI Lead Re-engagement</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }
        body { background-color: var(--dark-bg); color: #fff; font-family: 'Montserrat', sans-serif; scroll-behavior: smooth; }
        .navbar { background-color: rgba(0,0,0,0.9); border-bottom: 1px solid #222; }
        .navbar-brand { font-weight: 700; color: #fff !important; text-shadow: 0 0 5px var(--neon-glow); }
        .highlight { color: var(--accent); text-shadow: 0 0 5px var(--neon-glow); }
        .card h3, .card h4 { color: var(--accent) !important; text-shadow: 0 0 5px var(--neon-glow); }
        .hero-section { padding: 120px 0; background: radial-gradient(circle at center, #111 0%, #000 100%); position: relative; overflow: hidden; }
        .hero-section::before { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(to bottom, rgba(0,255,136,0.1) 0%, transparent 100%); opacity: 0.5; pointer-events: none; }
        .demo-button { display: inline-block; text-decoration: none; background: linear-gradient(135deg, var(--accent), #00b36d); color: #000; font-weight: 700; border: none; padding: 15px 40px; border-radius: 50px; box-shadow: 0 5px 20px var(--neon-glow); transition: 0.3s; }
        .demo-button:hover { transform: scale(1.05); box-shadow: 0 10px 30px var(--neon-glow); }
        .comparison-card { background: linear-gradient(to bottom, #1a1a1a, #0a0a0a); border: 1px solid #333; border-radius: 15px; padding: 20px; box-shadow: 0 5px 15px rgba(0,0,0,0.5); transition: 0.3s; }
        .comparison-card:hover { box-shadow: 0 5px 20px var(--neon-glow); transform: translateY(-5px); }
        .comparison-card h4 { color: var(--accent); text-shadow: 0 0 5px var(--neon-glow); }
        .comparison-card ul { list-style-type: none; padding: 0; }
        .comparison-card li { margin-bottom: 10px; display: flex; align-items: center; }
        .comparison-card li::before { content: '\\2714'; color: var(--accent); margin-right: 10px; font-size: 1.2em; }
        .card { background: linear-gradient(to bottom, #1a1a1a, #0a0a0a); border: none; border-radius: 15px; box-shadow: 0 5px 15px rgba(0,0,0,0.5); transition: 0.3s; }
        .card:hover { box-shadow: 0 5px 20px var(--neon-glow); transform: translateY(-5px); }
        h2, h4 { letter-spacing: 1px; text-transform: uppercase; }
    </style>
</head>
<body>
<nav class="navbar navbar-expand-lg sticky-top">
    <div class="container">
        <a class="navbar-brand" href="/">INSURANCE<span class="highlight">GROK</span>BOT</a>
        
        <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav" aria-controls="navbarNav" aria-expanded="false" aria-label="Toggle navigation">
            <span class="navbar-toggler-icon" style="filter: invert(1);"></span>
        </button>
        
        <div class="collapse navbar-collapse" id="navbarNav">
            <ul class="navbar-nav ms-auto align-items-center">
                <li class="nav-item"><a href="#abilities" class="nav-link">Abilities</a></li>
                <li class="nav-item"><a href="#comparison-section" class="nav-link">Comparison</a></li>
                <li class="nav-item"><a href="#compatibility" class="nav-link">Compatibility</a></li>
                <li class="nav-item"><a href="#sales-knowledge" class="nav-link">Sales Logic</a></li>
                <li class="nav-item"><a href="#pricing" class="nav-link">Pricing</a></li>
                <li class="nav-item"><a href="/demo-chat" class="nav-link">Demo</a></li>
                
                <!-- Dynamic Login / Logout -->
                <li class="nav-item ms-4">
                    {% if current_user.is_authenticated %}
                        <span class="navbar-text me-3 text-light">Hello, {{ current_user.email }}</span>
                        <a href="/dashboard" class="btn btn-outline-light me-2">Dashboard</a>
                        <a href="/logout" class="btn btn-outline-danger">Logout</a>
                    {% else %}
                        <a href="/login" class="btn btn-outline-light me-2">Log In</a>
                        <a href="/register" class="btn btn-primary" style="background: #00ff88; border: none; color: #000; font-weight: bold;">Sign Up</a>
                    {% endif %}
                </li>
            </ul>
        </div>
    </div>
</nav>

<header class="hero-section text-center position-relative">
    <div class="container position-relative" style="z-index: 2;">
        <h1 class="display-3 fw-bold mb-4">The Most Durable Life Insurance Lead Re-engagement Assistant</h1>
        <p class="lead mb-5 text-secondary">Powered by <span class="highlight">xAI's Grok</span>. Built by life insurance agents for life insurance agents.</p>
        
        <!-- Primary CTA: Buy Now (Stripe) -->
        <a href="/checkout" class="demo-button" style="font-size: 36px; padding: 25px 70px;">
            Subscribe Now – $100/mo
        </a>

        <p class="mt-5">
            <a href="/demo-chat" style="color:#888; text-decoration:underline; font-size:20px;">
                Or try the demo first →
            </a>
        </p>

        <p class="mt-4">
            <a href="/getting-started" style="color:#00ff88; font-size:20px; text-decoration:underline;">
                New here? Follow the setup guide →
            </a>
        </p>

        <p class="mt-4 text-secondary">
            <small>No contract • Cancel anytime • Instant activation</small>
        </p>
    </div>
</header>

<section id="abilities" class="py-5 bg-dark">
    <div class="container">
        <h2 class="fw-bold text-center mb-5">Current Abilities</h2>
        <div class="row g-4">
            <div class="col-md-4"><div class="card p-4"><h3>Multi-Tenant</h3><p class="text-secondary">Handles leads across different agencies with unique identities and data isolation.</p></div></div>
            <div class="col-md-4"><div class="card p-4"><h3>Deep Discovery</h3><p class="text-secondary">Automated fact-finding to identify gaps in existing work or personal coverage.</p></div></div>
            <div class="col-md-4"><div class="card p-4"><h3>24/7 Re-engagement</h3><p class="text-secondary">Picks up old leads and works them until they are ready to talk to an agent.</p></div></div>
        </div>
    </div>
</section>

<section id="comparison-section" class="py-5 bg-black">
    <div class="container text-center">
        <h2 class="mb-5 fw-bold">Others vs. InsuranceGrokBot</h2>
        <div class="row g-4">
            <div class="col-md-4">
                <div class="comparison-card">
                    <h4>Features</h4>
                    <ul>
                        <li>5 Different Sales Systems</li>
                        <li>Complete Insurance Logic</li>
                        <li>Extensive Underwriting Knowledge</li>
                        <li>Persistence</li>
                        <li>Only books leads that have a need</li>
                    </ul>
                </div>
            </div>
            <div class="col-md-4">
                <div class="comparison-card">
                    <h4>Standard Bot</h4>
                    <ul>
                        <li>Hardcoded Scripts</li>
                        <li>Gives up on "No"</li>
                        <li>Generic</li>
                    </ul>
                </div>
            </div>
            <div class="col-md-4">
                <div class="comparison-card">
                    <h4 class="highlight">GrokBot</h4>
                    <ul>
                        <li>Real-time Reasoning</li>
                        <li>NEPQ Objection Handling</li>
                        <li>Insurance Specific</li>
                    </ul>
                </div>
            </div>
        </div>
    </div>
</section>

<section id="compatibility" class="py-5 bg-dark">
    <div class="container">
        <h2 class="fw-bold text-center mb-5">Built for Every CRM</h2>
        <div class="row g-3 text-center">
            <div class="col-md-4"><div class="card p-4"><h4>GoHighLevel</h4><p class="small text-secondary">Native webhook support. Easy setup.</p></div></div>
            <div class="col-md-4"><div class="card p-4"><h4>HubSpot</h4><p class="small text-secondary">Workflow triggers. Easy setup.</p></div></div>
            <div class="col-md-4"><div class="card p-4"><h4>Pipedrive</h4><p class="small text-secondary">Activity-based webhooks. Easy setup.</p></div></div>
            <div class="col-md-4"><div class="card p-4"><h4>Zoho CRM</h4><p class="small text-secondary">Automation rules. Semi-easy setup.</p></div></div>
            <div class="col-md-4"><div class="card p-4"><h4>Salesforce</h4><p class="small text-secondary">Enterprise outbound messaging. Semi-easy.</p></div></div>
            <div class="col-md-4"><div class="card p-4"><h4>Zapier</h4><p class="small text-secondary">The universal bridge. Easy setup.</p></div></div>
        </div>
    </div>
</section>

<section id="sales-knowledge" class="py-5 bg-black">
    <div class="container">
        <h2 class="fw-bold highlight mb-4">The Master Sales Logic</h2>
        <div class="row g-5">
            <div class="col-md-6">
                <h4>Jeremy Miner's NEPQ</h4>
                <p class="text-secondary">Neuro-Emotional Persuasion Questions focus on getting the lead to persuade themselves. By asking the right questions, the bot uncovers the "Gap" between their current situation and their needs.</p>
                <h4>Jordan Belfort's Straight Line</h4>
                <p class="text-secondary">The bot is programmed to maintain control of the sale. It loops back to the benefits of the policy while building massive certainty in the lead's mind.</p>
            </div>
            <div class="col-md-6">
                <h4>Gap Selling & Psychology of Selling</h4>
                <p class="text-secondary">Using Keenan’s 'Gap Selling' and Brian Tracy’s 'Psychology of Selling', this bot identifies the lead's pain points and refuses to back down from smoke-screen objections. It is designed to manage the conversation until a result is achieved.</p>
            </div>
        </div>
    </div>
</section>

<section id="pricing" class="py-5 bg-dark text-center">
    <div class="container">
        <h2 class="fw-bold highlight mb-4">Pricing</h2>
        <div class="card p-5 mx-auto" style="max-width: 500px; box-shadow: 0 0 20px var(--neon-glow);">
            <h3 class="display-4 fw-bold">$100<small class="fs-4">/mo</small></h3>
            <p class="lead" style="color: #00ff88; font-weight: bold;">Early Adopter Rate</p>
            <p class="text-secondary">Limited to the first 50 people. Don't let old leads go to waste.</p>
            <a href="/checkout" class="btn btn-primary w-100 mt-4" style="font-size: 20px; padding: 15px;">
                SUBSCRIBE NOW
            </a>
        </div>
    </div>
</section>

<section id="contact" class="py-5 bg-black">
    <div class="container text-center" style="max-width: 600px;">
        <h2 class="fw-bold mb-4">Ready to Automate?</h2>
        <form action="mailto:mitchell_vandusen@hotmal.com" method="post" enctype="text/plain">
            <input type="text" name="name" class="form-control mb-3 bg-dark text-white border-secondary" placeholder="Name" required>
            <input type="email" name="email" class="form-control mb-3 bg-dark text-white border-secondary" placeholder="Email" required>
            <textarea name="msg" class="form-control mb-4 bg-dark text-white border-secondary" placeholder="Your CRM and Lead Volume..." rows="4"></textarea>
            <button type="submit" class="btn btn-primary btn-lg w-100">CONTACT US</button>
        </form>
    </div>
</section>

<footer class="py-4 text-center border-top border-secondary bg-black">
    <p class="text-secondary">&copy; 2026 InsuranceGrokBot. Built by Life Insurance Agents for Life Insurance Agents.</p>
</footer>
</body>
</html>
"""
    return render_template_string(home_html)

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except:
        return '', 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session.customer
        email = session.customer_details.email.lower()

        if email and customer_id:
            # Create user if not exists
            if not User.get(email):
                password_hash = generate_password_hash(str(uuid.uuid4()))  # Random password
                User.create(email, password_hash, customer_id)
                logger.info(f"Created paid user {email}")

            # Update stripe_customer_id
            conn = get_db_connection()
            conn.execute("UPDATE users SET stripe_customer_id = ? WHERE email = ?", (customer_id, email))
            conn.commit()
            conn.close()

    return '', 200

@app.route("/register", methods=["GET", "POST"])
def register():
    # Admin bypass — only you use this URL
    if request.args.get("admin") == "true":
        form = RegisterForm()
        if form.validate_on_submit():
            email = form.email.data.lower()
            if User.get(email):
                flash("Email already registered", "error")
                return redirect("/register?admin=true")
            
            password_hash = generate_password_hash(form.password.data)
            if User.create(email, password_hash):
                flash(f"Account created for {email}!", "success")
                # Optional: auto-login the new user
                new_user = User.get(email)
                login_user(new_user)
                return redirect("/dashboard")
            else:
                flash("Creation failed", "error")
        return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Admin Register - InsuranceGrokBot</title>
    <style>
        body { background:#000; color:#fff; font-family:Arial; text-align:center; padding:100px; }
        h1 { color:#00ff88; font-size:48px; margin-bottom:40px; }
        .form-group { margin:30px 0; }
        label { font-size:20px; display:block; margin-bottom:10px; }
        input { width:400px; max-width:90%; padding:15px; background:#111; border:1px solid #333; color:#fff; border-radius:8px; font-size:18px; }
        button { padding:15px 60px; background:#00ff88; color:#000; border:none; border-radius:8px; font-size:20px; cursor:pointer; margin-top:20px; }
        button:hover { background:#00cc70; }
        .flash { padding:15px; background:#1a1a1a; border-radius:8px; margin:20px auto; max-width:500px; }
        .flash-error { border-left:5px solid #ff6b6b; }
        .flash-success { border-left:5px solid #00ff88; }
    </style>
</head>
<body>
    <h1>Admin Account Creation</h1>

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
            {{ form.email.label }}<br>
            {{ form.email(class="form-control", placeholder="email@example.com") }}
        </div>
        <div class="form-group">
            {{ form.password.label }}<br>
            {{ form.password(class="form-control") }}
        </div>
        <div class="form-group">
            {{ form.confirm.label }}<br>
            {{ form.confirm(class="form-control") }}
        </div>
        {{ form.submit }}
    </form>

    <p style="margin-top:40px;"><a href="/dashboard" style="color:#00ff88;">← Dashboard</a></p>
</body>
</html>
        """, form=form)

    # Normal users — registration closed
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Registration Closed</title>
    <style>
        body { background:#000; color:#fff; font-family:Arial; text-align:center; padding:100px; }
        h1 { color:#00ff88; font-size:48px; }
        p { font-size:24px; }
        a { color:#00ff88; font-size:20px; text-decoration:underline; }
    </style>
</head>
<body>
    <h1>Registration Closed</h1>
    <p>Accounts are created automatically after you subscribe.</p>
    <p><a href="/checkout">Subscribe Now →</a></p>
    <p style="margin-top:40px;"><a href="/">← Back to Home</a></p>
</body>
</html>
    """)

@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.lower()
        user = User.get(email)
        if user and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            flash("Logged in successfully!")
            return redirect("/dashboard")
        else:
            flash("Invalid email or password")
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Log In - InsuranceGrokBot</title>
    <style>
        body { background:#000; color:#fff; font-family:Arial; text-align:center; padding:100px; }
        h1 { color:#00ff88; font-size:48px; }
        .form-group { margin:20px 0; }
        label { font-size:20px; }
        input { width:400px; max-width:90%; padding:15px; background:#111; border:1px solid #333; color:#fff; border-radius:8px; font-size:18px; }
        button { padding:15px 60px; background:#00ff88; color:#000; border:none; border-radius:8px; font-size:20px; cursor:pointer; }
        button:hover { background:#00cc70; }
        .link { color:#00ff88; text-decoration:underline; font-size:18px; }
    </style>
</head>
<body>
    <h1>Log In</h1>
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            {% for message in messages %}
                <p style="color:#ff6b6b;">{{ message }}</p>
            {% endfor %}
        {% endif %}
    {% endwith %}
    <form method="post" action="">
        {{ form.hidden_tag() }}
        <div class="form-group">
            {{ form.email.label }}<br>
            {{ form.email(class="form-control") }}
        </div>
        <div class="form-group">
            {{ form.password.label }}<br>
            {{ form.password(class="form-control") }}
        </div>
        {{ form.submit }}
    </form>
    <p class="mt-4">
        <a href="/register" class="link">Don't have an account? Sign up</a>
    </p>
    <p><a href="/" class="link">← Back to home</a></p>
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
        headers = ["Email", "location_id", "calendar_id", "crm_api_key", "crm_user_id", "bot_first_name", "timezone", "initial_message"]
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

    email_idx = col_index("Email")
    location_idx = col_index("location_id")
    calendar_idx = col_index("calendar_id")
    api_key_idx = col_index("crm_api_key")
    user_id_idx = col_index("crm_user_id")
    bot_name_idx = col_index("bot_first_name")
    timezone_idx = col_index("timezone")
    initial_msg_idx = col_index("initial_message")

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
        ]

        try:
            if user_row_num:
                worksheet.update(f"A{user_row_num}:H{user_row_num}", [data])
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
    <style>
        body { background:#000; color:#fff; font-family:Arial; padding:40px; }
        .container { max-width:900px; margin:auto; }
        h1 { font-size:48px; text-align:center; color:#00ff88; }
        .nav-tabs .nav-link { color:#aaa; border-color:#333; }
        .nav-tabs .nav-link.active { color:#00ff88; background:#111; border-color:#00ff88 #00ff88 #111; }
        .nav-tabs { border-bottom:1px solid #333; }
        .tab-content { margin-top:30px; }
        .form-group { margin:25px 0; }
        label { display:block; margin-bottom:8px; font-size:18px; }
        input { width:100%; padding:12px; background:#111; border:1px solid #333; color:#fff; border-radius:8px; font-size:16px; }
        button { padding:15px 40px; background:#00ff88; color:#000; border:none; border-radius:8px; font-size:20px; cursor:pointer; }
        button:hover { background:#00cc70; }
        .logout { position:absolute; top:20px; right:20px; color:#00ff88; font-size:18px; }
        .alert { padding:15px; background:#1a1a1a; border-radius:8px; margin:20px 0; }
        .alert-success { border-left:5px solid #00ff88; }
        .alert-error { border-left:5px solid #ff6b6b; }
        .card { background:#111; border:1px solid #333; border-radius:15px; padding:30px; margin:30px 0; }
        code { background:#222; padding:2px 6px; border-radius:4px; color:#00ff88; }
        /* Light text for guide and billing */
        .guide-text, .billing-text { color:#fff !important; }
        .guide-text h3 { color:#00ff88; }
        .guide-text li { color:#ddd; }
    </style>
</head>
<body>
    <div class="container">
        <a href="/logout" class="logout">Logout</a>
        <h1>Dashboard</h1>
        <p style="text-align:center; font-size:20px;">Welcome back, <strong>{{ current_user.email }}</strong></p>

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
                <h2 style="color:#00ff88; text-align:center;">Configure Your Bot</h2>
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
                        {{ form.crm_user_id(class="form-control", placeholder="e.g. BhWQCdIwX0C – required for calendar") }}
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

                    <div style="text-align:center; margin-top:40px;">
                        {{ form.submit(class="btn") }}
                    </div>
                </form>
            </div>

            <!-- CRM Setup Guide Tab -->
            <div class="tab-pane fade" id="guide">
                <div class="card guide-text">
                    <h2 style="color:#00ff88; text-align:center;">CRM Setup Guide - GHL is template</h2>
                    <p style="text-align:center; margin-bottom:30px;">Follow these steps to connect InsuranceGrokBot to your CRM</p>
                    {% raw %}
                    <div style="text-align:left;">
                        <h3 style="color:#00ff88;">Step 1: Create "Re-engage Leads" Workflow</h3>
                        <ol>
                            <li>Go to <strong>Automations → Workflows → Create Workflow</strong></li>
                            <li><strong>Trigger</strong>: Tag Applied (create a tag like "Re-engage text")</li>
                            <li>Add <strong>Wait</strong>: 5–30 minutes</li>
                            <li>Add <strong>Webhook</strong>:
                                <ul>
                                    <li>URL: <code>https://insurancegrokbot.click/webhook</code></li>
                                    <li>Method: POST</li>
                                    <li>Body fields (use correct crm "{{}}"):
                                        <ul>
                                            <li><code>intent</code>: "the intent of the message" </li>
                                            <li><code>first_name</code>: "{{contact.first_name}}" </li>
                                            <li><code>age</code>: "{{contact.custom_fields.age or 'unknown'}}" </li>
                                            <li><code>contact_address</code>: "{{contact.address1}}" </li>
                                            <li><code>agent_name</code>: "Your Name" (or "{{user.full_name}}") </li>
                                            <li><code>message</code>: "{{message.body}}" </li>
                                        </ul>
                                    </li>
                                </ul>
                            </li>
                            <li>Add <strong>Condition</strong>: If appointment booked → stop workflow</li>
                            <li>Else → Wait + same webhook → repeat</li>
                        </ol>

                        <h3 style="color:#00ff88; margin-top:40px;">Step 2: Create "AI SMS Handler" Workflow</h3>
                        <ol>
                            <li>New Workflow</li>
                            <li><strong>Trigger</strong>: Inbound SMS with tag "Re-engage text"</li>
                            <li>Add <strong>Wait</strong>: 2 minutes</li>
                            <li>Add <strong>Webhook</strong> (same URL and fields)</li>
                        </ol>

                        <h3 style="color:#00ff88; margin-top:40px;">Daily SMS Limits</h3>
                        <ul>
                            <li>GHL starts at <strong>100 outbound SMS/day</strong></li>
                            <li>Increases automatically when previous limit hit (250 next day, then higher)</li>
                            <li>Check in GHL Settings → Phone Numbers</li>
                        </ul>

                        <p style="text-align:center; margin-top:40px; font-weight:bold;">
                            Once set up, the bot runs 24/7 — no more dead leads.
                        </p>
                    </div>
                    {% endraw %}
                </div>
            </div>

            <!-- Billing Tab -->
            <div class="tab-pane fade" id="billing">
                <div class="card billing-text">
                    <h2 style="color:#00ff88;">Billing</h2>
                    <p>Update payment method, view invoices, or cancel subscription</p>
                    <form method="post" action="/create-portal-session">
                        <button type="submit">Manage Billing on Stripe →</button>
                    </form>
                </div>
            </div>
        </div>

        <p style="text-align:center; margin-top:60px;">
            <a href="/" style="color:#00ff88;">← Back to Home</a>
        </p>
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
            flash("No subscription found — subscribe first", "error")
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
    # Generate unique session ID for this visitor
    if 'demo_session_id' not in session:
        session['demo_session_id'] = str(uuid.uuid4())

    demo_session_id = session['demo_session_id']

    demo_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Chat with GrokBot</title>
    <style>
        * {{ box-sizing: border-box; }}
        html, body {{
            height: 100%;
            margin: 0;
            padding: 0;
            background: #f5f5f7;
            display: flex;
            justify-content: center;
            align-items: center;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            overflow: hidden;
        }}

        .iphone-frame {{
            width: 375px;
            max-width: 100%;
            height: 100vh;
            max-height: 812px;
            background: #000;
            border-radius: 40px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.4);
            padding: 40px 12px 80px;
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}

        .iphone-frame::before {{
            content: '';
            position: absolute;
            top: 10px;
            left: 50%;
            transform: translateX(-50%);
            width: 160px;
            height: 30px;
            background: #000;
            border-radius: 20px;
            z-index: 10;
        }}

        .chat-screen {{
            flex: 1;
            overflow-y: auto;
            padding: 20px 10px 10px;
            background: #fff;
            display: flex;
            flex-direction: column;
            -webkit-overflow-scrolling: touch;
        }}

        .msg {{
            max-width: 80%;
            padding: 10px 15px;
            border-radius: 20px;
            margin-bottom: 12px;
            word-wrap: break-word;
            align-self: flex-start;
        }}

        .bot-msg {{
            background: #e5e5ea;
            color: #000;
            border-bottom-left-radius: 5px;
        }}

        .user-msg {{
            background: #007aff;
            color: #fff;
            align-self: flex-end;
            border-bottom-right-radius: 5px;
        }}

        .input-area {{
            position: relative;
            margin: 10px 10px 20px;
            display: flex;
            background: #fff;
            border-radius: 25px;
            padding: 8px 15px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}

        #user-input {{
            flex: 1;
            border: none;
            outline: none;
            font-size: 16px;
            background: transparent;
        }}

        #send-btn {{
            background: #007aff;
            color: white;
            border: none;
            border-radius: 50%;
            width: 36px;
            height: 36px;
            margin-left: 10px;
            font-size: 18px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .chat-screen::-webkit-scrollbar {{ display: none; }}
    </style>
</head>
<body>
    <div class="iphone-frame">
        <div id="chat-screen" class="chat-screen">
            <div class="msg bot-msg">Hey! I saw you were looking for coverage recently. Do you actually have a plan in place right now, or are you starting from scratch?</div>
        </div>
        <div class="input-area">
            <input type="text" id="user-input" placeholder="Type your message..." autofocus>
            <button id="send-btn">↑</button>
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
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        locationId: 'DEMO_ACCOUNT_SALES_ONLY',
                        contact_id: SESSION_ID,
                        first_name: 'Visitor',
                        message: {{body: msg}}
                    }})
                }});
                const data = await res.json();
                chat.innerHTML += `<div class="msg bot-msg">${{data.reply}}</div>`;
            }} catch(e) {{
                chat.innerHTML += `<div class="msg bot-msg">Sorry — connection issue. Try again?</div>`;
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
    <style>
        body { 
            background: #000; 
            color: #fff; 
            font-family: 'Montserrat', Arial, sans-serif; 
            padding: 40px; 
            margin: 0; 
        }
        a { color: #00ff88; }
    </style>
</head>
<body>
    <style>
      [data-custom-class='body'], [data-custom-class='body'] * {
              background: transparent !important;
            }
    [data-custom-class='title'], [data-custom-class='title'] * {
              font-family: Arial !important;
    font-size: 26px !important;
    color: #000000 !important;
            }
    [data-custom-class='subtitle'], [data-custom-class='subtitle'] * {
              font-family: Arial !important;
    color: #595959 !important;
    font-size: 14px !important;
            }
    [data-custom-class='heading_1'], [data-custom-class='heading_1'] * {
              font-family: Arial !important;
    font-size: 19px !important;
    color: #000000 !important;
            }
    [data-custom-class='heading_2'], [data-custom-class='heading_2'] * {
              font-family: Arial !important;
    font-size: 17px !important;
    color: #000000 !important;
            }
    [data-custom-class='body_text'], [data-custom-class='body_text'] * {
              color: #595959 !important;
    font-size: 14px !important;
    font-family: Arial !important;
            }
    [data-custom-class='link'], [data-custom-class='link'] * {
              color: #3030F1 !important;
    font-size: 14px !important;
    font-family: Arial !important;
    word-break: break-word !important;
            }
    </style>
          <span style="display: block;margin: 0 auto 3.125rem;width: 11.125rem;height: 2.375rem;background: url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNzgiIGhlaWdodD0iMzgiIHZpZXdCb3g9IjAgMCAxNzggMzgiPgogICAgPGcgZmlsbD0ibm9uZSIgZmlsbC1ydWxlPSJldmVub2RkIj4KICAgICAgICA8cGF0aCBmaWxsPSIjRDFEMUQxIiBkPSJNNC4yODMgMjQuMTA3Yy0uNzA1IDAtMS4yNTgtLjI1Ni0xLjY2LS43NjhoLS4wODVjLjA1Ny41MDIuMDg2Ljc5Mi4wODYuODd2Mi40MzRILjk4NXYtOC42NDhoMS4zMzJsLjIzMS43NzloLjA3NmMuMzgzLS41OTQuOTUtLjg5MiAxLjcwMi0uODkyLjcxIDAgMS4yNjQuMjc0IDEuNjY1LjgyMi40MDEuNTQ4LjYwMiAxLjMwOS42MDIgMi4yODMgMCAuNjQtLjA5NCAxLjE5OC0uMjgyIDEuNjctLjE4OC40NzMtLjQ1Ni44MzMtLjgwMyAxLjA4LS4zNDcuMjQ3LS43NTYuMzctMS4yMjUuMzd6TTMuOCAxOS4xOTNjLS40MDUgMC0uNy4xMjQtLjg4Ni4zNzMtLjE4Ny4yNDktLjI4My42Ni0uMjkgMS4yMzN2LjE3N2MwIC42NDUuMDk1IDEuMTA3LjI4NyAxLjM4Ni4xOTIuMjguNDk1LjQxOS45MS40MTkuNzM0IDAgMS4xMDEtLjYwNSAxLjEwMS0xLjgxNiAwLS41OS0uMDktMS4wMzQtLjI3LTEuMzI5LS4xODItLjI5NS0uNDY1LS40NDMtLjg1Mi0uNDQzem01LjU3IDEuNzk0YzAgLjU5NC4wOTggMS4wNDQuMjkzIDEuMzQ4LjE5Ni4zMDQuNTEzLjQ1Ny45NTQuNDU3LjQzNyAwIC43NS0uMTUyLjk0Mi0uNDU0LjE5Mi0uMzAzLjI4OC0uNzUzLjI4OC0xLjM1MSAwLS41OTUtLjA5Ny0xLjA0LS4yOS0xLjMzOC0uMTk0LS4yOTctLjUxLS40NDUtLjk1LS40NDUtLjQzOCAwLS43NTMuMTQ3LS45NDYuNDQzLS4xOTQuMjk1LS4yOS43NDItLjI5IDEuMzR6bTQuMTUzIDBjMCAuOTc3LS4yNTggMS43NDItLjc3NCAyLjI5My0uNTE1LjU1Mi0xLjIzMy44MjctMi4xNTQuODI3LS41NzYgMC0xLjA4NS0uMTI2LTEuNTI1LS4zNzhhMi41MiAyLjUyIDAgMCAxLTEuMDE1LTEuMDg4Yy0uMjM3LS40NzMtLjM1NS0xLjAyNC0uMzU1LTEuNjU0IDAtLjk4MS4yNTYtMS43NDQuNzY4LTIuMjg4LjUxMi0uNTQ1IDEuMjMyLS44MTcgMi4xNi0uODE3LjU3NiAwIDEuMDg1LjEyNiAxLjUyNS4zNzYuNDQuMjUxLjc3OS42MSAxLjAxNSAxLjA4LjIzNi40NjkuMzU1IDEuMDE5LjM1NSAxLjY0OXpNMTkuNzEgMjRsLS40NjItMi4xLS42MjMtMi42NTNoLS4wMzdMMTcuNDkzIDI0SDE1LjczbC0xLjcwOC02LjAwNWgxLjYzM2wuNjkzIDIuNjU5Yy4xMS40NzYuMjI0IDEuMTMzLjMzOCAxLjk3MWguMDMyYy4wMTUtLjI3Mi4wNzctLjcwNC4xODgtMS4yOTRsLjA4Ni0uNDU3Ljc0Mi0yLjg3OWgxLjgwNGwuNzA0IDIuODc5Yy4wMTQuMDc5LjAzNy4xOTUuMDY3LjM1YTIwLjk5OCAyMC45OTggMCAwIDEgLjE2NyAxLjAwMmMuMDIzLjE2NS4wMzYuMjk5LjA0LjM5OWguMDMyYy4wMzItLjI1OC4wOS0uNjExLjE3Mi0xLjA2LjA4Mi0uNDUuMTQxLS43NTQuMTc3LS45MTFsLjcyLTIuNjU5aDEuNjA2TDIxLjQ5NCAyNGgtMS43ODN6bTcuMDg2LTQuOTUyYy0uMzQ4IDAtLjYyLjExLS44MTcuMzMtLjE5Ny4yMi0uMzEuNTMzLS4zMzguOTM3aDIuMjk5Yy0uMDA4LS40MDQtLjExMy0uNzE3LS4zMTctLjkzNy0uMjA0LS4yMi0uNDgtLjMzLS44MjctLjMzem0uMjMgNS4wNmMtLjk2NiAwLTEuNzIyLS4yNjctMi4yNjYtLjgtLjU0NC0uNTM0LS44MTYtMS4yOS0uODE2LTIuMjY3IDAtMS4wMDcuMjUxLTEuNzg1Ljc1NC0yLjMzNC41MDMtLjU1IDEuMTk5LS44MjUgMi4wODctLjgyNS44NDggMCAxLjUxLjI0MiAxLjk4Mi43MjUuNDcyLjQ4NC43MDkgMS4xNTIuNzA5IDIuMDA0di43OTVoLTMuODczYy4wMTguNDY1LjE1Ni44MjkuNDE0IDEuMDkuMjU4LjI2MS42Mi4zOTIgMS4wODUuMzkyLjM2MSAwIC43MDMtLjAzNyAxLjAyNi0uMTEzYTUuMTMzIDUuMTMzIDAgMCAwIDEuMDEtLjM2djEuMjY4Yy0uMjg3LjE0My0uNTkzLjI1LS45Mi4zMmE1Ljc5IDUuNzkgMCAwIDEtMS4xOTEuMTA0em03LjI1My02LjIyNmMuMjIyIDAgLjQwNi4wMTYuNTUzLjA0OWwtLjEyNCAxLjUzNmExLjg3NyAxLjg3NyAwIDAgMC0uNDgzLS4wNTRjLS41MjMgMC0uOTMuMTM0LTEuMjIyLjQwMy0uMjkyLjI2OC0uNDM4LjY0NC0uNDM4IDEuMTI4VjI0aC0xLjYzOHYtNi4wMDVoMS4yNGwuMjQyIDEuMDFoLjA4Yy4xODctLjMzNy40MzktLjYwOC43NTYtLjgxNGExLjg2IDEuODYgMCAwIDEgMS4wMzQtLjMwOXptNC4wMjkgMS4xNjZjLS4zNDcgMC0uNjIuMTEtLjgxNy4zMy0uMTk3LjIyLS4zMS41MzMtLjMzOC45MzdoMi4yOTljLS4wMDctLjQwNC0uMTEzLS43MTctLjMxNy0uOTM3LS4yMDQtLjIyLS40OC0uMzMtLjgyNy0uMzN6bS4yMyA1LjA2Yy0uOTY2IDAtMS43MjItLjI2Ny0yLjI2Ni0uOC0uNTQ0LS41MzQtLjgxNi0xLjI5LS44MTYtMi4yNjcgMC0xLjAwNy4yNTEtMS43ODUuNzU0LTIuMzM0LjUwNC0uNTUgMS4yLS44MjUgMi4wODctLjgyNS44NDkgMCAxLjUxLjI0MiAxLjk4Mi43MjUuNDczLjQ4NC43MDkgMS4xNTIuNzA5IDIuMDA0di43OTVoLTMuODczYy4wMTguNDY1LjE1Ni44MjkuNDE0IDEuMDkuMjU4LjI2MS42Mi4zOTIgMS4wODUuMzkyLjM2MiAwIC43MDQtLjAzNyAxLjAyNi0uMTEzYTUuMTMzIDUuMTMzIDAgMCAwIDEuMDEtLjM2djEuMjY4Yy0uMjg3LjE0My0uNTkzLjI1LS45MTkuMzJhNS43OSA1Ljc5IDAgMCAxLTEuMTkyLjEwNHptNS44MDMgMGMtLjcwNiAwLTEuMjYtLjI3NS0xLjY2My0uODIyLS40MDMtLjU0OC0uNjA0LTEuMzA3LS42MDQtMi4yNzggMC0uOTg0LjIwNS0xLjc1Mi42MTUtMi4zMDEuNDEtLjU1Ljk3NS0uODI1IDEuNjk1LS44MjUuNzU1IDAgMS4zMzIuMjk0IDEuNzI5Ljg4MWguMDU0YTYuNjk3IDYuNjk3IDAgMCAxLS4xMjQtMS4xOTh2LTEuOTIyaDEuNjQ0VjI0SDQ2LjQzbC0uMzE3LS43NzhoLS4wN2MtLjM3Mi41OTEtLjk0Ljg4Ni0xLjcwMi44ODZ6bS41NzQtMS4zMDZjLjQyIDAgLjcyNi0uMTIxLjkyMS0uMzY1LjE5Ni0uMjQzLjMwMi0uNjU3LjMyLTEuMjR2LS4xNzhjMC0uNjQ0LS4xLTEuMTA2LS4yOTgtMS4zODYtLjE5OS0uMjc5LS41MjItLjQxOS0uOTctLjQxOWEuOTYyLjk2MiAwIDAgMC0uODUuNDY1Yy0uMjAzLjMxLS4zMDQuNzYtLjMwNCAxLjM1IDAgLjU5Mi4xMDIgMS4wMzUuMzA2IDEuMzMuMjA0LjI5Ni40OTYuNDQzLjg3NS40NDN6bTEwLjkyMi00LjkyYy43MDkgMCAxLjI2NC4yNzcgMS42NjUuODMuNC41NTMuNjAxIDEuMzEyLjYwMSAyLjI3NSAwIC45OTItLjIwNiAxLjc2LS42MiAyLjMwNC0uNDE0LjU0NC0uOTc3LjgxNi0xLjY5LjgxNi0uNzA1IDAtMS4yNTgtLjI1Ni0xLjY1OS0uNzY4aC0uMTEzbC0uMjc0LjY2MWgtMS4yNTF2LTguMzU3aDEuNjM4djEuOTQ0YzAgLjI0Ny0uMDIxLjY0My0uMDY0IDEuMTg3aC4wNjRjLjM4My0uNTk0Ljk1LS44OTIgMS43MDMtLjg5MnptLS41MjcgMS4zMWMtLjQwNCAwLS43LjEyNS0uODg2LjM3NC0uMTg2LjI0OS0uMjgzLjY2LS4yOSAxLjIzM3YuMTc3YzAgLjY0NS4wOTYgMS4xMDcuMjg3IDEuMzg2LjE5Mi4yOC40OTUuNDE5LjkxLjQxOS4zMzcgMCAuNjA1LS4xNTUuODA0LS40NjUuMTk5LS4zMS4yOTgtLjc2LjI5OC0xLjM1IDAtLjU5MS0uMS0xLjAzNS0uMy0xLjMzYS45NDMuOTQzIDAgMCAwLS44MjMtLjQ0M3ptMy4xODYtMS4xOTdoMS43OTRsMS4xMzQgMy4zNzljLjA5Ni4yOTMuMTYzLjY0LjE5OCAxLjA0MmguMDMzYy4wMzktLjM3LjExNi0uNzE3LjIzLTEuMDQybDEuMTEyLTMuMzc5aDEuNzU3bC0yLjU0IDYuNzczYy0uMjM0LjYyNy0uNTY2IDEuMDk2LS45OTcgMS40MDctLjQzMi4zMTItLjkzNi40NjgtMS41MTIuNDY4LS4yODMgMC0uNTYtLjAzLS44MzMtLjA5MnYtMS4zYTIuOCAyLjggMCAwIDAgLjY0NS4wN2MuMjkgMCAuNTQzLS4wODguNzYtLjI2Ni4yMTctLjE3Ny4zODYtLjQ0NC41MDgtLjgwM2wuMDk2LS4yOTUtMi4zODUtNS45NjJ6Ii8+CiAgICAgICAgPGcgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoNzMpIj4KICAgICAgICAgICAgPGNpcmNsZSBjeD0iMTkiIGN5PSIxOSIgcj0iMTkiIGZpbGw9IiNFMEUwRTAiLz4KICAgICAgICAgICAgPHBhdGggZmlsbD0iI0ZGRiIgZD0iTTIyLjQ3NCAxNS40NDNoNS4xNjJMMTIuNDM2IDMwLjRWMTAuMzYzaDE1LjJsLTUuMTYyIDUuMDh6Ii8+CiAgICAgICAgPC9nPgogICAgICAgIDxwYXRoIGZpbGw9IiNEMkQyRDIiIGQ9Ik0xMjEuNTQ0IDE0LjU2di0xLjcyOGg4LjI3MnYxLjcyOGgtMy4wMjRWMjRoLTIuMjR2LTkuNDRoLTMuMDA4em0xMy43NDQgOS41NjhjLTEuMjkgMC0yLjM0MS0uNDE5LTMuMTUyLTEuMjU2LS44MS0uODM3LTEuMjE2LTEuOTQ0LTEuMjE2LTMuMzRzLjQwOC0yLjQ3NyAxLjIyNC0zLjMwNGcuODE2LS44MjcgMS44NzItMS4yNCAzLjE2OC0xLjI0czIuMzYuNDAzIDMuMTkyIDEuMjA4Yy44MzIuODA1IDEuMjQ4IDEuODggMS4yNDggMy4yMjQgMCAuMzEtLjAyMS41OTctLjA2NC44NjRoLTYuNDY0Yy4wNTMuNTc2LjI2NyAxLjA0LjY0IDEuMzkyLjM3My4zNTIuODQ4LjUyOCAxLjQyNC41MjguNzc5IDAgMS4zNTUtLjMyIDEuNzI4LS45NmgyLjQzMmEzLjg5MSAzLjg5MSAwIDAgMS0xLjQ4OCAyLjA2NGMtLjczNi41MzMtMS42MjcuOC0yLjY3Mi44em0xLjQ4LTYuNjg4Yy0uNC0uMzUyLS44ODMtLjUyOC0xLjQ0OC0uNTI4cy0xLjAzNy4xNzYtMS40MTYuNTI4Yy0uMzc5LjM1Mi0uNjA1LjgyMS0uNjggMS40MDhoNC4xOTJjLS4wMzItLjU4Ny0uMjQ4LTEuMDU2LS42NDgtMS40MDh6bTcuMDE2LTIuMzA0djEuNTY4Yy41OTctMS4xMyAxLjQ2MS0xLjY5NiAyLjU5Mi0xLjY5NnYyLjMwNGgtLjU2Yy0uNjcyIDAtMS4xNzkuMTY4LTEuNTIuNTA0LS4zNDEuMzM2LS41MTIuOTE1LS41MTIgMS43MzZWMjRoLTIuMjU2di04Ljg2NGgyLjI1NnpNMTY0LjkzNiAyNFYxMi4xNmgyLjI1NlYyNGgtMi4yNTZ6bTcuMDQtLjE2bC0zLjQ3Mi04LjcwNGgyLjUyOGwyLjI1NiA2LjMwNCAyLjM4NC02LjMwNGgyLjM1MmwtNS41MzYgMTMuMDU2aC0yLjM1MmwxLjg0LTQuMzUyeiIvPgogICAgPC9nPgo8L3N2Zz4K) center no-repeat;"></span>
      <div data-custom-class="body">
      <div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="title" style="line-height: 1.5;"><bdt class="block-component"><span style="font-size: 19px;"></bdt><bdt class="question"><strong><h1>TERMS OF SERVICE</h1></strong></bdt><bdt class="statement-end-if-in-editor"></bdt></span></div><div class="MsoNormal" data-custom-class="subtitle" style="line-height: 1.5;"><strong>Last updated</strong> <bdt class="question"><strong>January 07, 2026</strong></bdt></div><div class="MsoNormal" style="line-height: 1.1;"><br></div><div style="line-height: 1.5;"><br></div><div style="line-height: 1.5;"><strong><span data-custom-class="heading_1"><h2>AGREEMENT TO OUR LEGAL TERMS</h2></span></strong></div></div><div align="center" style="text-align: left;"><div class="MsoNormal" id="agreement" style="line-height: 1.5;"><a name="_6aa3gkhykvst"></a></div></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">We are <bdt class="question noTranslate" data-id="9d459c4e-c548-e5cb-7729-a118548965d2">__________</bdt><bdt class="block-component"></bdt> (<bdt class="block-component"></bdt>"<strong>Company</strong>," "<strong>we</strong>," "<strong>us</strong>," "<strong>our</strong>"<bdt class="statement-end-if-in-editor"></bdt>)<span style="font-size:11.0pt;line-height:115%;
Arial;mso-fareast-font-family:Calibri;color:#595959;mso-themecolor:text1;
mso-themetint:166;"><span style="font-size:11.0pt;line-height:115%;
Arial;mso-fareast-font-family:Calibri;color:#595959;mso-themecolor:text1;
mso-themetint:166;"><span style="font-size:11.0pt;line-height:115%;
Arial;mso-fareast-font-family:Calibri;color:#595959;mso-themecolor:text1;
mso-themetint:166;"><bdt class="question"><bdt class="block-component">.</bdt></span><bdt class="block-component"></bdt></span></span></span></span></span></span></div></div><div align="center" style="line-height: 1;"><br></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">We operate <bdt class="block-component"></bdt>the website <span style="color: rgb(0, 58, 250);"><bdt class="question noTranslate">[insurancegrokbot.click](http://insurancegrokbot.click)</bdt></span> (the <bdt class="block-component"></bdt>"<strong>Site</strong>"<bdt class="statement-end-if-in-editor"></bdt>)<bdt class="block-component"></bdt><bdt class="block-component"></bdt>, as well as any other related products and services that refer or link to these legal terms (the <bdt class="block-component"></bdt>"<strong>Legal Terms</strong>"<bdt class="statement-end-if-in-editor"></bdt>) (collectively, the <bdt class="block-component"></bdt>"<strong>Services</strong>"<bdt class="statement-end-if-in-editor"></bdt>).<bdt class="block-component"></bdt></span></div><div class="MsoNormal" style="line-height: 1;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><bdt class="question">InsuranceGrokBot is a third-party AI-powered SMS conversation assistant designed exclusively for life insurance agents and agencies using the GoHighLevel platform.
The app integrates via webhooks to automatically re-engage cold or unresponsive insurance leads through intelligent, human-like text message conversations. Powered by xAI's Grok language model, InsuranceGrokBot conducts discovery, handles objections, uncovers coverage gaps, and schedules appointments directly into the user's GoHighLevel calendar.
Key features include:
- Persistent, multi-turn SMS conversations
- Fact extraction and memory across messages
- Objection handling using proven sales methodologies (NEPQ, Gap Selling, Straight Line Persuasion)
- Calendar availability checking and appointment booking
- Multi-tenant support for agencies
The service is provided on a subscription basis through the GoHighLevel Marketplace. Users are responsible for ensuring all communications comply with applicable laws (TCPA, CAN-SPAM, insurance regulations, etc.). No personal data is stored beyond what is necessary for conversation context within the user's own GoHighLevel account.</bdt></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><bdt class="statement-end-if-in-editor"></bdt></span></div><div class="MsoNormal" style="line-height: 1;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">You can contact us by <bdt class="block-component">email at <bdt class="question noTranslate">__________</bdt><bdt class="block-component"></bdt> or by mail to <bdt class="question noTranslate">__________</bdt><bdt class="block-component"></bdt>, <bdt class="question noTranslate">__________</bdt><bdt class="block-component"></bdt><bdt class="block-component"></bdt><bdt class="block-component"><bdt class="block-component">, </bdt><bdt class="question noTranslate">__________</bdt><bdt class="statement-end-if-in-editor"></bdt></bdt>.</span></div><div class="MsoNormal" style="line-height: 1;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">These Legal Terms constitute a legally binding agreement made between you, whether personally or on behalf of an entity (<bdt class="block-component"></bdt>"<strong>you</strong>"<bdt class="statement-end-if-in-editor"></bdt>), and <bdt class="question noTranslate">__________</bdt>, concerning your access to and use of the Services. You agree that by accessing the Services, you have read, understood, and agreed to be bound by all of these Legal Terms. IF YOU DO NOT AGREE WITH ALL OF THESE LEGAL TERMS, THEN YOU ARE EXPRESSLY PROHIBITED FROM USING THE SERVICES AND YOU MUST DISCONTINUE USE IMMEDIATELY.<bdt class="block-component"></bdt></span></div><div class="MsoNormal" style="line-height: 1;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">Supplemental terms and conditions or documents that may be posted on the Services from time to time are hereby expressly incorporated herein by reference. We reserve the right, in our sole discretion, to make changes or modifications to these Legal Terms <bdt class="block-component"></bdt>at any time and for any reason<bdt class="statement-end-if-in-editor"></bdt>. We will alert you about any changes by updating the <bdt class="block-component"></bdt>"Last updated"<bdt class="statement-end-if-in-editor"></bdt> date of these Legal Terms, and you waive any right to receive specific notice of each such change. It is your responsibility to periodically review these Legal Terms to stay informed of updates. You will be subject to, and will be deemed to have been made aware of and to have accepted, the changes in any revised Legal Terms by your continued use of the Services after the date such revised Legal Terms are posted.<bdt class="else-block"></bdt></span></div></div><div align="center" style="line-height: 1;"><br></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><bdt class="block-container if" data-type="if" id="a2595956-7028-dbe5-123e-d3d3a93ed076"><bdt data-type="conditional-block"><bdt data-type="body"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><bdt class="block-component"></bdt>The
Services are intended for users who are at least 18 years old. Persons under the age
of 18 are not permitted to use or register for the Services.</span></bdt></bdt><bdt data-type="conditional-block"><bdt class="block-component"></bdt></bdt></div><div class="MsoNormal" style="line-height: 1;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;">We recommend that you print a copy of these Legal Terms for your records.</div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="heading_1" style="line-height: 1.5;"><strong><h2>TABLE OF CONTENTS</h2></strong></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#services"><span data-custom-class="link"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">1. OUR SERVICES</span></span></span></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#ip"><span style="color: rgb(0, 58, 250);"><span data-custom-class="body_text">2. INTELLECTUAL PROPERTY RIGHTS</span></span></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#userreps"></a><a data-custom-class="link" href="#userreps"><span style="color: rbg(0, 58, 250); font-size: 15px; line-height: 1.5;"><span data-custom-class="body_text">3. USER REPRESENTATIONS</span></span></a></div><div class="MsoNormal" style="line-height: 1.5;"><span style="font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt></span></span> <a data-custom-class="link" href="#products"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#products"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt></span></span></a> <a data-custom-class="link" href="#purchases"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#purchases"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt>4. PURCHASES AND PAYMENT<bdt class="statement-end-if-in-editor"></bdt></span></span></a></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"><span style="font-size: 15px;"></span></bdt><a data-custom-class="link" href="#subscriptions"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">5. SUBSCRIPTIONS</span></span></a><bdt class="statement-end-if-in-editor"><span style="font-size: 15px;"></span></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><span style="font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt></span></span> <a data-custom-class="link" href="#software"></a> <a data-custom-class="link" href="#software"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#software"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt></span></span></a> <a data-custom-class="link" href="#prohibited"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#prohibited"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">6. PROHIBITED ACTIVITIES</span></span></a> <a data-custom-class="link" href="#ugc"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#ugc"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">7. USER GENERATED CONTRIBUTIONS</span></span></a> <a data-custom-class="link" href="#license"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#license"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">8. CONTRIBUTION <bdt class="block-component"></bdt>LICENSE<bdt class="statement-end-if-in-editor"></bdt></span></span></a> <a data-custom-class="link" href="#reviews"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#reviews"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt></span></span></a> <a data-custom-class="link" href="#mobile"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#mobile"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt></span></span></a> <a data-custom-class="link" href="#socialmedia"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#socialmedia"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt></span></span></a> <a data-custom-class="link" href="#thirdparty"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#thirdparty"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt></span></span></a> <a data-custom-class="link" href="#advertisers"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#advertisers"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt>9. ADVERTISERS<bdt class="statement-end-if-in-editor"></bdt></span></span></a> <a data-custom-class="link" href="#sitemanage"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#sitemanage"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">10. SERVICES MANAGEMENT</span></span></a> <a data-custom-class="link" href="#ppyes"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#ppyes"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt></span></span></a> <a data-custom-class="link" href="#ppno"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#ppno"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt>11. PRIVACY POLICY<bdt class="statement-end-if-in-editor"></bdt></span></span></a> <a data-custom-class="link" href="#dmca"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#dmca"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt><bdt class="block-component"></bdt><bdt class="statement-end-if-in-editor"></bdt></span></span></a></div><div class="MsoNormal" style="line-height: 1.5;"><span style="font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt><bdt class="block-component"></bdt><bdt class="block-component"></bdt></span></span> <a data-custom-class="link" href="#terms"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#terms"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">12. TERM AND TERMINATION</span></span></a> <a data-custom-class="link" href="#modifications"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#modifications"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">13. MODIFICATIONS AND INTERRUPTIONS</span></span></a> <a data-custom-class="link" href="#law"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#law"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">14. GOVERNING LAW</span></span></a> <a data-custom-class="link" href="#disputes"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#disputes"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">15. DISPUTE RESOLUTION</span></span></a> <a data-custom-class="link" href="#corrections"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#corrections"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">16. CORRECTIONS</span></span></a> <a data-custom-class="link" href="#disclaimer"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#disclaimer"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">17. DISCLAIMER</span></span></a> <a data-custom-class="link" href="#liability"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#liability"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">18. LIMITATIONS OF LIABILITY</span></span></a> <a data-custom-class="link" href="#indemnification"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#indemnification"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">19. INDEMNIFICATION</span></span></a> <a data-custom-class="link" href="#userdata"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#userdata"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">20. USER DATA</span></span></a> <a data-custom-class="link" href="#electronic"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#electronic"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">21. ELECTRONIC COMMUNICATIONS, TRANSACTIONS, AND SIGNATURES</span></span></a> <a data-custom-class="link" href="#california"></a></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"><span style="font-size: 15px;"></span></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#california"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text"><bdt class="block-component"></bdt>22. CALIFORNIA USERS AND RESIDENTS<bdt class="statement-end-if-in-editor"></bdt></span></span></a> <a data-custom-class="link" href="#misc"></a></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#misc"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">23. MISCELLANEOUS</span></span></a> <a data-custom-class="link" href="#contact"></a></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></span></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><a data-custom-class="link" href="#contact"><span style="color: rgb(0, 58, 250); font-size: 15px;"><span data-custom-class="body_text">24. CONTACT US</span></span></a></div></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="heading_1" style="line-height: 1.5;"><a name="_b6y29mp52qvx"></a></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="services" style="line-height: 1.5;"><strong><span style="font-size: 19px; line-height: 1.5;"><h2>1. OUR SERVICES</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">The information provided when using the Services is not intended for distribution to or use by any person or entity in any jurisdiction or country where such distribution or use would be contrary to law or regulation or which would subject us to any registration requirement within such jurisdiction or country. Accordingly, those persons who choose to access the Services from other locations do so on their own initiative and are solely responsible for compliance with local laws, if and to the extent local laws are applicable.<bdt class="block-component"></bdt></span><bdt class="block-component"><span style="font-size: 15px;"></span></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">The Services are not tailored to comply with industry-specific regulations (Health Insurance Portability and Accountability Act (HIPAA), Federal Information Security Management Act (FISMA), etc.), so if your interactions would be subjected to such laws, you may not use the Services. You may not use the Services in a way that would violate the Gramm-Leach-Bliley Act (GLBA).<bdt class="block-component"></bdt><bdt class="statement-end-if-in-editor"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><br></div></div><div align="center" data-custom-class="heading_1" style="text-align: left; line-height: 1.5;"><strong><span id="ip" style="font-size: 19px; line-height: 1.5;"><h2>2. INTELLECTUAL PROPERTY RIGHTS</h2></span></strong></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="heading_2" style="line-height: 1.5;"><strong><h3>Our intellectual property</h3></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">We are the owner or the licensee of all intellectual property rights in our Services, including all source code, databases, functionality, software, website designs, audio, video, text, photographs, and graphics in the Services (collectively, the <bdt class="block-component"></bdt>"Content"<bdt class="statement-end-if-in-editor"></bdt>), as well as the trademarks, service marks, and logos contained therein (the <bdt class="block-component"></bdt>"Marks"<bdt class="statement-end-if-in-editor"></bdt>).</span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">Our Content and Marks are protected by copyright and trademark laws (and various other intellectual property rights and unfair competition laws) and treaties<bdt class="block-component"></bdt> in the United States and<bdt class="statement-end-if-in-editor"></bdt> around the world.</span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">The Content and Marks are provided in or through the Services <bdt class="block-component"></bdt>"AS IS"<bdt class="statement-end-if-in-editor"></bdt> for your <bdt class="block-component"></bdt>personal, non-commercial use or internal business purpose<bdt class="statement-end-if-in-editor"></bdt> only.</span></div><div class="MsoNormal" data-custom-class="heading_2" style="line-height: 1.5;"><strong><h3>Your use of our Services</h3></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">Subject to your compliance with these Legal Terms, including the <bdt class="block-component"></bdt>"<bdt class="statement-end-if-in-editor"></bdt></span><a data-custom-class="link" href="#prohibited"><span style="color: rgb(0, 58, 250); font-size: 15px;">PROHIBITED ACTIVITIES</span></a><span style="font-size: 15px;"><bdt class="block-component"></bdt>"<bdt class="statement-end-if-in-editor"></bdt> section below, we grant you a non-exclusive, non-transferable, revocable <bdt class="block-component"></bdt>license<bdt class="statement-end-if-in-editor"></bdt> to:</span></div><ul><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">access the Services; and</span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">download or print a copy of any portion of the Content to which you have properly gained access,</span></li></ul><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">solely for your <bdt class="block-component"></bdt>personal, non-commercial use or internal business purpose<bdt class="statement-end-if-in-editor"></bdt>.</span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">Except as set out in this section or elsewhere in our Legal Terms, no part of the Services and no Content or Marks may be copied, reproduced,
aggregated, republished, uploaded, posted, publicly displayed, encoded,
translated, transmitted, distributed, sold, licensed, or otherwise exploited
for any commercial purpose whatsoever, without our express prior written
permission.</span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">If you wish to make any use of the Services, Content, or Marks other than as set out in this section or elsewhere in our Legal Terms, please address your request to: <bdt class="question noTranslate">__________</bdt>. If we ever grant you the permission to post, reproduce, or publicly display any part of our Services or Content, you must identify us as the owners or licensors of the Services, Content, or Marks and ensure that any copyright or proprietary notice appears or is visible on posting, reproducing, or displaying our Content.</span></div></div><div align="center" style="line-height: 1.5;"><br></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">We reserve all rights not expressly granted to you in and to the Services, Content, and Marks.</span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">Any breach of these Intellectual Property Rights will constitute a material breach of our Legal Terms and your right to use our Services will terminate immediately.</span></div><div class="MsoNormal" data-custom-class="heading_2" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:1.5;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><strong><h3>Your submissions<bdt class="block-component"></strong></bdt></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">Please review this section and the <bdt class="block-component"></bdt>"<bdt class="statement-end-if-in-editor"></bdt><a data-custom-class="link" href="#prohibited"><span style="color: rgb(0, 58, 250);">PROHIBITED ACTIVITIES</span></a><bdt class="block-component"></bdt>"<bdt class="statement-end-if-in-editor"></bdt> section carefully prior to using our Services to understand the (a) rights you give us and (b) obligations you have when you post or upload any content through the Services.</span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;"><strong>Submissions:</strong> By directly sending us any question, comment, suggestion, idea, feedback, or other information about the Services (<bdt class="block-component"></bdt>"Submissions"<bdt class="statement-end-if-in-editor"></bdt>), you agree to assign to us all intellectual property rights in such Submission. You agree that we shall own this Submission and be entitled to its unrestricted use and dissemination for any lawful purpose, commercial or otherwise, without acknowledgment or compensation to you.<bdt class="block-component"></bdt></span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;"><strong>You are responsible for what you post or upload:</strong> By sending us Submissions<bdt class="block-component"></bdt> through any part of the Services<bdt class="block-component"></bdt> you:</span></div><ul><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">confirm that you have read and agree with our <bdt class="block-component"></bdt>"<bdt class="statement-end-if-in-editor"></bdt></span><a data-custom-class="link" href="#prohibited"><span style="color: rgb(0, 58, 250); font-size: 15px;">PROHIBITED ACTIVITIES</span></a><span style="font-size: 15px;"><bdt class="block-component"></bdt>"<bdt class="statement-end-if-in-editor"></bdt> and will not post, send, publish, upload, or transmit through the Services any Submission<bdt class="block-component"></bdt> that is illegal, harassing, hateful, harmful, defamatory, obscene, bullying, abusive, discriminatory, threatening to any person or group, sexually explicit, false, inaccurate, deceitful, or misleading;</span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">to the extent permissible by applicable law, waive any and all moral rights to any such Submission<bdt class="block-component"></bdt>;</span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">warrant that any such Submission<bdt class="block-component"></bdt> are original to you or that you have the necessary rights and <bdt class="block-component"></bdt>licenses<bdt class="statement-end-if-in-editor"></bdt> to submit such Submissions<bdt class="block-component"></bdt> and that you have full authority to grant us the above-mentioned rights in relation to your Submissions<bdt class="block-component"></bdt>; and</span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;">warrant and represent that your Submissions<bdt class="block-component"></bdt> do not constitute confidential information.</span></li></ul><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;">You are solely responsible for your Submissions<bdt class="block-component"></bdt> and you expressly agree to reimburse us for any and all losses that we may suffer because of your breach of (a) this section, (b) any third party’s intellectual property rights, or (c) applicable law.<bdt class="block-component"></bdt><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><br></div></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="heading_1" id="userreps" style="line-height: 1.5;"><a name="_5hg7kgyv9l8z"></a><strong><span style="line-height: 1.5; font-family: Arial; font-size: 19px;"><h2>3. USER REPRESENTATIONS</h2></span></strong></div></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">By using the Services, you represent and warrant that:</span><bdt class="block-container if" data-type="if" id="d2d82ca8-275f-3f86-8149-8a5ef8054af6"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="user_account_option" data-type="statement"></bdt> </bdt><span style="color: rgb(89, 89, 89); font-size: 11pt;">(</span><span style="color: rgb(89, 89, 89); font-size: 14.6667px;">1</span><span style="color: rgb(89, 89, 89); font-size: 11pt;">) you have the legal capacity and you agree to comply with these Legal Terms;</span><bdt class="block-container if" data-type="if" id="8d4c883b-bc2c-f0b4-da3e-6d0ee51aca13"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="user_u13_option" data-type="statement"></bdt> </bdt><span style="color: rgb(89, 89, 89); font-size: 11pt;">(</span><span style="color: rgb(89, 89, 89); font-size: 14.6667px;">2</span><span style="color: rgb(89, 89, 89); font-size: 11pt;">) you are not a
minor in the jurisdiction in which you reside<bdt class="block-container if" data-type="if" id="76948fab-ec9e-266a-bb91-948929c050c9"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="user_o18_option" data-type="statement"></bdt></bdt>; (</span><span style="color: rgb(89, 89, 89); font-size: 14.6667px;">3</span><span style="color: rgb(89, 89, 89); font-size: 11pt;">) you will not access the Services through automated or non-human means, whether through a bot, script or
otherwise; (</span><span style="color: rgb(89, 89, 89); font-size: 14.6667px;">4</span><span style="color: rgb(89, 89, 89); font-size: 11pt;">) you will not use the Services for any illegal or <bdt class="block-component"></bdt>unauthorized<bdt class="statement-end-if-in-editor"></bdt> purpose; and (</span><span style="color: rgb(89, 89, 89); font-size: 14.6667px;">5</span><span style="color: rgb(89, 89, 89); font-size: 11pt;">) your use of the Services will not violate any applicable law or regulation.</span><span style="color: rgb(89, 89, 89); font-size: 14.6667px;"></span></div></div><div align="center" style="line-height: 1.5;"><br></div><div align="center" style="text-align: left;"><div class="MsoNormal" style="text-align: justify; line-height: 115%;"><div class="MsoNormal" style="line-height: 17.25px;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">If you provide any information that is untrue, inaccurate, not current, or incomplete, we have the right to suspend or terminate your account and refuse any and all current or future use of the Services (or any portion thereof).</span></div><div class="MsoNormal" style="line-height: 1.1; text-align: left;"><bdt class="block-component"></bdt></span></div></bdt></bdt> <bdt class="block-component"><span style="font-size: 15px;"></bdt></span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><bdt class="block-component"><span style="font-size: 15px;"></span></bdt></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div></div></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="heading_1" id="purchases" style="line-height: 1.5;"><a name="_ynub0jdx8pob"></a><strong><span style="line-height: 1.5; font-family: Arial; font-size: 19px;"><h2>4. PURCHASES AND PAYMENT</h2></span></strong></div></div><div align="center" style="text-align: left;"><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"><span style="font-size: 15px;"></span></bdt></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">We accept the following forms of payment:</span></div><div class="MsoNormal" style="text-align:justify;line-height:115%;"><div class="MsoNormal" style="text-align: left; line-height: 1;"><br></div></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; margin-left: 20px;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><bdt class="forloop-component"></bdt>-  <bdt class="question noTranslate">Visa</bdt></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; margin-left: 20px;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><bdt class="forloop-component"></bdt>-  <bdt class="question noTranslate">Mastercard</bdt></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; margin-left: 20px;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><bdt class="forloop-component"></bdt>-  <bdt class="question noTranslate">Discover</bdt></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; margin-left: 20px;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><bdt class="forloop-component"></bdt></span></div><div class="MsoNormal" style="line-height: 1;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><br></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">You agree to provide current, complete, and accurate purchase and account information for all purchases made via the Services. You further agree to promptly update account and payment information, including email address, payment method, and payment card expiration date, so that we can complete your transactions and contact you as needed. Sales tax will be added to the price of purchases as deemed required by us. We may change prices at any time. All payments shall be </span><span style="font-size: 15px; line-height: 115%; font-family: Arial; color: rgb(89, 89, 89);">in <bdt class="question">US dollars</bdt>.</span></div></div><div align="center" style="line-height: 1.5;"><br></div><div align="center" style="text-align: left;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">You agree to pay all charges at the prices then in effect for your purchases and any applicable shipping fees, and you <bdt class="block-component"></bdt>authorize<bdt class="statement-end-if-in-editor"></bdt> us to charge your chosen payment provider for any such amounts upon placing your order. We reserve the right to correct any errors or mistakes in pricing, even if we have already requested or received payment.</span></div></div><div align="center" style="line-height: 1.5;"><br></div><div align="center" style="text-align: left; line-height: 1.5;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">We reserve the right to refuse any order placed through the Services. We may, in our sole discretion, limit or cancel quantities purchased per person, per household, or per order. These restrictions may include orders placed by or under the same customer account, the same payment method, and/or orders that use the same billing or shipping address. We reserve the right to limit or prohibit orders that, in our sole <bdt class="block-component"></bdt>judgment<bdt class="statement-end-if-in-editor"></bdt>, appear to be placed by dealers, resellers, or distributors.</span><span style="line-height: 115%; font-family: Arial; color: rgb(89, 89, 89);"><bdt data-type="conditional-block" style="color: rgb(10, 54, 90); text-align: left;"><bdt class="block-component" data-record-question-key="return_option" data-type="statement" style="font-size: 15px;"></bdt></bdt></span></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"><span style="font-size: 15px;"></span></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="subscriptions" style="line-height: 1.5;"><strong><span style="font-size: 19px; line-height: 1.5;"><h2>5. SUBSCRIPTIONS</h2></span></strong></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" data-custom-class="heading_2" style="line-height: 1.5;"><strong><span style="font-size: 15px; line-height: 1.5;"><h3>Billing and Renewal</h3></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 15px;"><bdt class="block-component"></bdt>Your subscription will continue and automatically renew unless <bdt class="block-component"></bdt>canceled<bdt class="statement-end-if-in-editor"></bdt>. You consent to our charging your payment method on a recurring basis without requiring your prior approval for each recurring charge, until such time as you cancel the applicable order.<bdt class="block-component"></bdt> The length of your billing cycle <bdt class="block-component"></bdt>is monthly<bdt class="block-component"></bdt>.<bdt class="statement-end-if-in-editor"></bdt><bdt class="else-block"></bdt></span></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"><span style="font-size: 15px;"></bdt></span><bdt class="block-component"><span style="font-size: 15px;"></span></bdt></div><div class="MsoNormal" data-custom-class="heading_2" style="line-height: 1.5;"><span style="font-size: 15px; line-height: 1.5;"><strong><h3>Cancellation</h3></strong></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><bdt class="block-component"><span style="font-size: 15px;"></span></bdt><span style="font-size: 15px;">All purchases are non-refundable. <bdt class="statement-end-if-in-editor"></bdt><bdt class="block-component"></bdt>You can cancel your subscription at any time by contacting us using the contact information provided below.<bdt class="else-block"></bdt> Your cancellation will take effect at the end of the current paid term. If you have any questions or are unsatisfied with our Services, please email us at <bdt class="question noTranslate">__________</bdt>.<bdt class="statement-end-if-in-editor"></bdt><br></span></div><div class="MsoNormal" data-custom-class="heading_2" style="line-height: 1.5;"><strong><span style="font-size: 15px; line-height: 1.5;"><h3>Fee Changes</h3></span></strong></div><span style="font-size: 15px;"><span data-custom-class="body_text">We may, from time to time, make changes to the subscription fee and will communicate any price changes to you in accordance with applicable law.</span></span><div class="MsoNormal" style="line-height: 1.5;"><span style="font-size: 15px;"><bdt class="statement-end-if-in-editor"></bdt></span><bdt class="block-component"><span style="font-size: 15px;"></bdt></span></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-component"></bdt></div><div class="MsoNormal" style="text-align: justify; line-height: 1.5;"><span style="line-height: 115%; font-family: Arial; color: rgb(89, 89, 89);"><bdt data-type="conditional-block" style="color: rgb(10, 54, 90); text-align: left;"><bdt data-type="body"><div class="MsoNormal" style="font-size: 15px; line-height: 1.5;"><br></div></bdt></bdt></span><div class="MsoNormal" data-custom-class="heading_1" id="prohibited" style="text-align: left; line-height: 1.5;"><strong><span style="line-height: 1.5; font-size: 19px;"><h2>6. PROHIBITED ACTIVITIES</h2></span></strong></div></div><div class="MsoNormal" style="text-align: justify; line-height: 1;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">You may not access or use the Services for any purpose other than that for which we make the Services available. The Services may not be used in connection with any commercial <bdt class="block-component"></bdt>endeavors<bdt class="statement-end-if-in-editor"></bdt> except those that are specifically endorsed or approved by us.</span></div></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" style="text-align: justify; line-height: 1;"><div class="MsoNormal" style="line-height: 17.25px;"><div class="MsoNormal" style="line-height: 1.1;"><div class="MsoNormal" style="line-height: 17.25px;"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">As a user of the Services, you agree not to:</span></div></div><ul><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-size: 15px; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Systematically retrieve data or other content from the Services to create or compile, directly or indirectly, a collection, compilation, database, or directory without written permission from us.</span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Trick, defraud, or mislead us and other users, especially in any attempt to learn sensitive account information such as user passwords.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Circumvent, disable, or otherwise interfere with security-related features of the Services, including features that prevent or restrict the use or copying of any Content or enforce limitations on the use of the Services and/or the Content contained therein.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Disparage, tarnish, or otherwise harm, in our opinion, us and/or the Services.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Use any information obtained from the Services in order to harass, abuse, or harm another person.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Make improper use of our support services or submit false reports of abuse or misconduct.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Use the Services in a manner inconsistent with any applicable laws or regulations.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Engage in <bdt class="block-component"></bdt>unauthorized<bdt class="statement-end-if-in-editor"></bdt> framing of or linking to the Services.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Upload or transmit (or attempt to upload or to transmit) viruses, Trojan horses, or other material, including excessive use of capital letters and spamming (continuous posting of repetitive text), that interferes with any party’s uninterrupted use and enjoyment of the Services or modifies, impairs, disrupts, alters, or interferes with the use, features, functions, operation, or maintenance of the Services.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Engage in any automated use of the system, such as using scripts to send comments or messages, or using any data mining, robots, or similar data gathering and extraction tools.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Delete the copyright or other proprietary rights notice from any Content.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Attempt to impersonate another user or person or use the username of another user.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Upload or transmit (or attempt to upload or to transmit) any material that acts as a passive or active information collection or transmission mechanism, including without limitation, clear graphics interchange formats (<bdt class="block-component"></bdt>"gifs"<bdt class="statement-end-if-in-editor"></bdt>), 1×1 pixels, web bugs, cookies, or other similar devices (sometimes referred to as <bdt class="block-component"></bdt>"spyware" or "passive collection mechanisms" or "pcms"<bdt class="statement-end-if-in-editor"></bdt>).</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Interfere with, disrupt, or create an undue burden on the Services or the networks or services connected to the Services.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Harass, annoy, intimidate, or threaten any of our employees or agents engaged in providing any portion of the Services to you.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Attempt to bypass any measures of the Services designed to prevent or restrict access to the Services, or any portion of the Services.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Copy or adapt the Services' software, including but not limited to Flash, PHP, HTML, JavaScript, or other code.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Except as permitted by applicable law, decipher, decompile, disassemble, or reverse engineer any of the software comprising or in any way making up a part of the Services.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Except as may be the result of standard search engine or Internet browser usage, use, launch, develop, or distribute any automated system, including without limitation, any spider, robot, cheat utility, scraper, or offline reader that accesses the Services, or use or launch any <bdt class="block-component"></bdt>unauthorized<bdt class="statement-end-if-in-editor"></bdt> script or other software.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Use a buying agent or purchasing agent to make purchases on the Services.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Make any <bdt class="block-component"></bdt>unauthorized<bdt class="statement-end-if-in-editor"></bdt> use of the Services, including collecting usernames and/or email addresses of users by electronic or other means for the purpose of sending unsolicited email, or creating user accounts by automated means or under false <bdt class="block-component"></bdt>pretenses<bdt class="statement-end-if-in-editor"></bdt>.</span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><span style="line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);">Use the Services as part of any effort to compete with us or otherwise use the Services and/or the Content for any revenue-generating <bdt class="block-component"></bdt>endeavor<bdt class="statement-end-if-in-editor"></bdt> or commercial enterprise.</span><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);font-family: sans-serif; font-style: normal; font-variant-ligatures: normal; font-variant-caps: normal; font-weight: 400; letter-spacing: normal; orphans: 2; text-align: justify; text-indent: -29.4px; text-transform: none; white-space: normal; widows: 2; word-spacing: 0px; -webkit-text-stroke-width: 0px; background-color: rgb(255, 255, 255); text-decoration-style: initial; text-decoration-color: initial; color: rgb(89, 89, 89);"><bdt class="forloop-component"></bdt></span></span></span></span></span></li><li class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="question">Sell or otherwise transfer your profile.</bdt><bdt class="forloop-component"></bdt></span></li></ul><div class="MsoNormal"><br></div><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt data-type="body"><div class="MsoNormal" data-custom-class="heading_1" id="ugc" style="line-height: 1.5;"><strong><span style="line-height: 1.5; font-size: 19px;"><h2>7. USER GENERATED CONTRIBUTIONS</h2></span></strong></div></bdt></bdt></bdt> <bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt data-type="body"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if" id="24327c5d-a34f-f7e7-88f1-65a2f788484f" style="text-align: left;"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="user_post_content_option" data-type="statement"></bdt><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">The Services does not offer users to submit or post content.<bdt class="block-component"></bdt> We may provide you with the opportunity to create, submit, post, display, transmit, perform, publish, distribute, or broadcast content and materials to us or on the Services, including but not limited to text, writings, video, audio, photographs, graphics, comments, suggestions, or personal information or other material (collectively, <bdt class="block-component"></bdt>"Contributions"<bdt class="statement-end-if-in-editor"></bdt>). Contributions may be viewable by other users of the Services and through third-party websites.<bdt class="block-component"></bdt> When you create or make available any Contributions, you thereby represent and warrant that:<span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="statement-end-if-in-editor"><bdt class="block-component"></bdt></bdt></span></span></span></div></bdt></bdt></bdt></div></div><div class="MsoNormal" style="line-height: 17.25px;"><ul style="font-size: medium;text-align: left;"><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">The creation, distribution, transmission, public display, or performance, and the accessing, downloading, or copying of your Contributions do not and will not infringe the proprietary rights, including but not limited to the copyright, patent, trademark, trade secret, or moral rights of any third party.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">You are the creator and owner of or have the necessary <bdt class="block-component"></bdt>licenses<bdt class="statement-end-if-in-editor"></bdt>, rights, consents, releases, and permissions to use and to <bdt class="block-component"></bdt>authorize<bdt class="statement-end-if-in-editor"></bdt> us, the Services, and other users of the Services to use your Contributions in any manner contemplated by the Services and these Legal Terms.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">You have the written consent, release, and/or permission of each and every identifiable individual person in your Contributions to use the name or likeness of each and every such identifiable individual person to enable inclusion and use of your Contributions in any manner contemplated by the Services and these Legal Terms.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions are not false, inaccurate, or misleading.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions are not unsolicited or <bdt class="block-component"></bdt>unauthorized<bdt class="statement-end-if-in-editor"></bdt> advertising, promotional materials, pyramid schemes, chain letters, spam, mass mailings, or other forms of solicitation.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions are not obscene, lewd, lascivious, filthy, violent, harassing, <bdt class="block-component"></bdt>libelous<bdt class="statement-end-if-in-editor"></bdt>, slanderous, or otherwise objectionable (as determined by us).</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions do not ridicule, mock, disparage, intimidate, or abuse anyone.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions are not used to harass or threaten (in the legal sense of those terms) any other person and to promote violence against a specific person or class of people.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions do not violate any applicable law, regulation, or rule.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions do not violate the privacy or publicity rights of any third party.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions do not violate any applicable law concerning child pornography, or otherwise intended to protect the health or well-being of minors.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions do not include any offensive comments that are connected to race, national origin, gender, sexual preference, or physical handicap.</span></span></span></li><li data-custom-class="body_text" style="line-height: 1.5;"><span style="color: rgb(89, 89, 89);"><span style="font-size: 14px;"><span data-custom-class="body_text">Your Contributions do not otherwise violate, or link to material that violates, any provision of these Legal Terms, or any applicable law or regulation.</span></span></span></li></ul><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt data-type="body"><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">Any use of the Services in violation of the foregoing violates these Legal Terms and may result in, among other things, termination or suspension of your rights to use the Services.</span></div></bdt></bdt></bdt></div></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" style="text-align: justify; line-height: 1;"><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt data-type="body"><div class="MsoNormal" data-custom-class="heading_1" id="license" style="line-height: 1.5;"><strong><span style="line-height: 1.5; font-size: 19px;"><h2>8. CONTRIBUTION <bdt class="block-component"></bdt>LICENSE<bdt class="statement-end-if-in-editor"></bdt></h2></span></strong></div></bdt></bdt></bdt></div><div class="MsoNormal" style="line-height: 1;"><bdt class="block-container if" data-type="if" id="a088ddfb-d8c1-9e58-6f21-958c3f4f0709" style="text-align: left;"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="user_post_content_option" data-type="statement"></bdt></span></bdt></bdt></bdt></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">You and Services agree that we may access, store, process, and use any information and personal data that you provide<bdt class="block-component"></bdt> and your choices (including settings).</span></span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">By submitting suggestions or other feedback regarding the Services, you agree that we can use and share such feedback for any purpose without compensation to you.<bdt class="block-component"></bdt></span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">We do not assert any ownership over your Contributions. You retain full ownership of all of your Contributions and any intellectual property rights or other proprietary rights associated with your Contributions. We are not liable for any statements or representations in your Contributions provided by you in any area on the Services. You are solely responsible for your Contributions to the Services and you expressly agree to exonerate us from any and all responsibility and to refrain from any legal action against us regarding your Contributions.<bdt class="statement-end-if-in-editor"></bdt></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt class="statement-end-if-in-editor" data-type="close"></bdt></bdt></span></span></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5;"><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="review_option" data-type="statement"></bdt></bdt></span></span></span></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="mobile_app_option" data-type="statement"></bdt></bdt></span></span></span></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="socialnetwork_link_option" data-type="statement"></span></div></bdt></bdt></bdt> <bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="3rd_party_option" data-type="statement"></bdt></bdt></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="advertiser_option" data-type="statement"></bdt></bdt></bdt></div><div class="MsoNormal" data-custom-class="heading_1" id="advertisers" style="line-height: 1.5;"><strong><h2>9. ADVERTISERS</h2></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">We allow advertisers to display their advertisements and other information in certain areas of the Services, such as sidebar advertisements or banner advertisements. We simply provide the space to place such advertisements, and we have no other relationship with advertisers.</span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt class="statement-end-if-in-editor" data-type="close"></bdt></bdt></div><div class="MsoNormal" data-custom-class="heading_1" id="sitemanage" style="line-height: 1.5;"><strong><span style="line-height: 1.5; font-size: 19px;"><h2>10. SERVICES MANAGEMENT</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;">We reserve the right, but not the obligation, to: (1) monitor the Services for violations of these Legal Terms; (2) take appropriate legal action against anyone who, in our sole discretion, violates the law or these Legal Terms, including without limitation, reporting such user to law enforcement authorities; (3) in our sole discretion and without limitation, refuse, restrict access to, limit the availability of, or disable (to the extent technologically feasible) any of your Contributions or any portion thereof; (4) in our sole discretion and without limitation, notice, or liability, to remove from the Services or otherwise disable all files and content that are excessive in size or are in any way burdensome to our systems; and (5) otherwise manage the Services in a manner designed to protect our rights and property and to facilitate the proper functioning of the Services.</div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="privacy_policy_option" data-type="statement"></bdt></bdt><bdt class="block-container if" data-type="if"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="privacy_policy_followup" data-type="statement" style="font-size: 14.6667px;"></bdt></bdt></bdt></div><div class="MsoNormal" data-custom-class="heading_1" id="ppno" style="line-height: 1.5;"><strong><h2>11. PRIVACY POLICY</h2></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">We care about data privacy and security. By using the Services, you agree to be bound by our Privacy Policy posted on the Services, which is incorporated into these Legal Terms. Please be advised the Services are hosted in <span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-component"></bdt>the <bdt class="question noTranslate">United States</bdt><bdt class="block-component"></bdt></span><bdt class="block-component"></bdt>. If you access the Services from any other region of the world with laws or other requirements governing personal data collection, use, or disclosure that differ from applicable laws in <span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-component"></bdt>the <bdt class="question noTranslate">United States</bdt><bdt class="block-component"></bdt></span><bdt class="block-component"></bdt>, then through your continued use of the Services, you are transferring your data to <span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-component"></bdt>the <bdt class="question noTranslate">United States</bdt><bdt class="block-component"></bdt></span><bdt class="block-component"></bdt>, and you expressly consent to have your data transferred to and processed in <span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-component"></bdt>the <bdt class="question noTranslate">United States</bdt><bdt class="block-component"></bdt></span><bdt class="block-component"></bdt>.<bdt class="block-container if" data-type="if" id="547bb7bb-ecf2-84b9-1cbb-a861dc3e14e7"><bdt data-type="conditional-block"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if" id="547bb7bb-ecf2-84b9-1cbb-a861dc3e14e7"><bdt data-type="conditional-block"><bdt data-type="body"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-component"></bdt><bdt class="block-container if" data-type="if" id="547bb7bb-ecf2-84b9-1cbb-a861dc3e14e7"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="user_u13_option" data-type="statement"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="statement-end-if-in-editor"></bdt></span></bdt></bdt></span></bdt></bdt></bdt></span></bdt></bdt></span></div><div class="MsoNormal" style="line-height: 1.5;"><br></div><div class="MsoNormal" style="line-height: 1.5;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if"><bdt data-type="conditional-block"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if"><bdt data-type="conditional-block"><bdt data-type="body"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if"><bdt class="statement-end-if-in-editor" data-type="close"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="statement-end-if-in-editor"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if"><bdt data-type="conditional-block"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if"><bdt data-type="conditional-block"><bdt data-type="body"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if"><bdt class="statement-end-if-in-editor" data-type="close"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="statement-end-if-in-editor"></bdt></span></bdt></bdt></span></bdt></bdt></bdt></span></bdt></bdt></span></bdt></span></bdt></bdt></span></bdt></bdt></bdt></span></bdt></bdt></span></div><div class="MsoNormal" style="line-height: 1.5;"><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="copyright_agent_option" data-type="statement"><bdt class="block-component"></bdt><bdt class="block-component"></bdt></bdt><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt class="statement-end-if-in-editor" data-type="close"></bdt></bdt></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><bdt class="block-component"></bdt><bdt class="block-container if" data-type="if" style="text-align: left;"><bdt class="statement-end-if-in-editor" data-type="close"><bdt class="block-component"></bdt></bdt><bdt class="block-component"></bdt></div><div class="MsoNormal" data-custom-class="heading_1" id="terms" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-size: 19px;"><h2>12. TERM AND TERMINATION</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">These Legal Terms shall remain in full force and effect while you use the Services. WITHOUT LIMITING ANY OTHER PROVISION OF THESE LEGAL TERMS, WE RESERVE THE RIGHT TO, IN OUR SOLE DISCRETION AND WITHOUT NOTICE OR LIABILITY, DENY ACCESS TO AND USE OF THE SERVICES (INCLUDING BLOCKING CERTAIN IP ADDRESSES), TO ANY PERSON FOR ANY REASON OR FOR NO REASON, INCLUDING WITHOUT LIMITATION FOR BREACH OF ANY REPRESENTATION, WARRANTY, OR COVENANT CONTAINED IN THESE LEGAL TERMS OR OF ANY APPLICABLE LAW OR REGULATION. WE MAY TERMINATE YOUR USE OR PARTICIPATION IN THE SERVICES OR DELETE <bdt class="block-container if" data-type="if" id="a6e121c2-36b4-5066-bf9f-a0a33512e768"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="user_account_option" data-type="statement"></bdt></bdt>ANY CONTENT OR INFORMATION THAT YOU POSTED AT ANY TIME, WITHOUT WARNING, IN OUR SOLE DISCRETION.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">If we terminate or suspend your account for any reason, you are prohibited from registering and creating a new account under your name, a fake or borrowed name, or the name of any third party, even if you may be acting on behalf of the third party. In addition to terminating or suspending your account, we reserve the right to take appropriate legal action, including without limitation pursuing civil, criminal, and injunctive redress.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="modifications" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-size: 19px;"><h2>13. MODIFICATIONS AND INTERRUPTIONS</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">We reserve the right to change, modify, or remove the contents of the Services at any time or for any reason at our sole discretion without notice. However, we have no obligation to update any information on our Services.<bdt class="block-component"></bdt> We will not be liable to you or any third party for any modification, price change, suspension, or discontinuance of the Services.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">We cannot guarantee the Services will be available at all times. We may experience hardware, software, or other problems or need to perform maintenance related to the Services, resulting in interruptions, delays, or errors. We reserve the right to change, revise, update, suspend, discontinue, or otherwise modify the Services at any time or for any reason without notice to you. You agree that we have no liability whatsoever for any loss, damage, or inconvenience caused by your inability to access or use the Services during any downtime or discontinuance of the Services. Nothing in these Legal Terms will be construed to obligate us to maintain and support the Services or to supply any corrections, updates, or releases in connection therewith.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="law" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-size: 19px;"><h2>14. GOVERNING LAW</h2></span></strong></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-component"></bdt></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);">These Legal Terms and your use of the Services are governed by and construed in accordance with the laws of <bdt class="block-container if" data-type="if" id="b86653c1-52f0-c88c-a218-e300b912dd6b"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="governing_law" data-type="statement"></bdt><bdt data-type="body">the State of <bdt class="block-container question question-in-editor" data-id="b61250bd-6b61-32ea-a9e7-4a02690297c3" data-type="question noTranslate">Texas</bdt></bdt></bdt><bdt class="statement-end-if-in-editor" data-type="close"></bdt></bdt> applicable to agreements made and to be entirely performed within<bdt class="block-container if" data-type="if" id="b86653c1-52f0-c88c-a218-e300b912dd6b" style="font-size: 14.6667px;"><bdt data-type="conditional-block"> <span style="font-size: 11pt; line-height: 16.8667px; color: rgb(89, 89, 89);"><bdt class="block-container if" data-type="if" id="b86653c1-52f0-c88c-a218-e300b912dd6b"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="governing_law" data-type="statement"></bdt><bdt data-type="body">the State of <bdt class="block-container question question-in-editor" data-id="b61250bd-6b61-32ea-a9e7-4a02690297c3" data-type="question noTranslate">Texas</bdt></bdt></bdt><bdt class="statement-end-if-in-editor" data-type="close"></bdt></bdt><span style="font-size: 14.6667px;">, </span>without regard to its conflict of law principles.<bdt class="block-component"></bdt></span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="disputes" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-size: 19px;"><h2>15. DISPUTE RESOLUTION</h2></span></strong></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><bdt class="block-component"></bdt></bdt></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><bdt class="block-component"></bdt></div><div class="MsoNormal" data-custom-class="heading_2" style="line-height: 1.5; text-align: left;"><strong><h3>Binding Arbitration</h3></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><bdt class="block-component"><span style="font-size: 15px;"></span></bdt><span style="font-size: 15px;">If the Parties are unable to resolve a Dispute through informal negotiations, the Dispute (except those Disputes expressly excluded below) will be finally and exclusively resolved by binding arbitration. YOU UNDERSTAND THAT WITHOUT THIS PROVISION, YOU WOULD HAVE THE RIGHT TO SUE IN COURT AND HAVE A JURY TRIAL. <bdt class="block-component"></bdt>The arbitration shall be commenced and conducted under the Commercial Arbitration Rules of the American Arbitration Association (<bdt class="block-component"></bdt>"AAA"<bdt class="statement-end-if-in-editor"></bdt>) and, where appropriate, the AAA’s Supplementary Procedures for Consumer Related Disputes (<bdt class="block-component"></bdt>"AAA Consumer Rules"<bdt class="statement-end-if-in-editor"></bdt>), both of which are available at the <span style="font-size: 15px; line-height: 16.8667px; color: rgb(0, 58, 250);"><a data-custom-class="link" href="<http://www.adr.org>" rel="noopener noreferrer" target="_blank">American Arbitration Association (AAA) website</a></span>. Your arbitration fees and your share of arbitrator compensation shall be governed by the AAA Consumer Rules and, where appropriate, limited by the AAA Consumer Rules. <bdt class="else-block"></bdt>The arbitration may be conducted in person, through the submission of documents, by phone, or online. The arbitrator will make a decision in writing, but need not provide a statement of reasons unless requested by either Party. The arbitrator must follow applicable law, and any award may be challenged if the arbitrator fails to do so. Except where otherwise required by the applicable <bdt class="block-component"></bdt>AAA<bdt class="else-block"></bdt> rules or applicable law, the arbitration will take place in <bdt class="block-component"></bdt><bdt class="block-component"></bdt><bdt class="question noTranslate">Texas</bdt><bdt class="statement-end-if-in-editor"></bdt>. Except as otherwise provided herein, the Parties may litigate in court to compel arbitration, stay proceedings pending arbitration, or to confirm, modify, vacate, or enter <bdt class="block-component"></bdt>judgment<bdt class="statement-end-if-in-editor"></bdt> on the award entered by the arbitrator.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;">If for any reason, a Dispute proceeds in court rather than arbitration, the Dispute shall be commenced or prosecuted in the</span> <bdt class="block-component" style="font-size: 15px;"></bdt><span style="font-size: 15px;"> state and federal courts</span><bdt class="statement-end-if-in-editor" style="font-size: 15px;"></bdt><span style="font-size: 15px;"> located in</span><bdt class="block-component" style="font-size: 15px;"></bdt><bdt class="block-component" style="font-size: 15px;"> </bdt><bdt class="question noTranslate" style="font-size: 15px;">__________</bdt><bdt class="statement-end-if-in-editor" style="font-size: 15px;"></bdt><span style="font-size: 15px;">, and the Parties hereby consent to, and waive all <bdt class="block-component"></bdt>defenses<bdt class="statement-end-if-in-editor"></bdt> of lack of personal jurisdiction, and forum non conveniens with respect to venue and jurisdiction in such<bdt class="block-component"></bdt> state and federal courts<bdt class="statement-end-if-in-editor"></bdt>. Application of the United Nations Convention on Contracts for the International Sale of Goods and the Uniform Computer Information Transaction Act (UCITA) are excluded from these Legal Terms.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><bdt class="block-component"></bdt>If this provision is found to be illegal or unenforceable, then neither Party will elect to arbitrate any Dispute falling within that portion of this provision found to be illegal or unenforceable and such Dispute shall be decided by a court of competent jurisdiction within the courts listed for jurisdiction above, and the Parties agree to submit to the personal jurisdiction of that court.<bdt class="block-component"></bdt></bdt></div><div class="MsoNormal" data-custom-class="heading_2" style="line-height: 1.5; text-align: left;"><strong><h3>Restrictions</h3></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;">The Parties agree that any arbitration shall be limited to the Dispute between the Parties individually. To the full extent permitted by law, (a) no arbitration shall be joined with any other proceeding; (b) there is no right or authority for any Dispute to be arbitrated on a class-action basis or to <bdt class="block-component"></bdt>utilize<bdt class="statement-end-if-in-editor"></bdt> class action procedures; and (c) there is no right or authority for any Dispute to be brought in a purported representative capacity on behalf of the general public or any other persons.</div><div class="MsoNormal" data-custom-class="heading_2" style="line-height: 1.5; text-align: left;"><bdt class="block-component"></bdt><strong><h3>Exceptions to Arbitration</h3></strong> <bdt class="else-block"></bdt></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><bdt class="block-component"></bdt>The Parties agree that the following Disputes are not subject to the above provisions concerning binding arbitration: (a) any Disputes seeking to enforce or protect, or concerning the validity of, any of the intellectual property rights of a Party; (b) any Dispute related to, or arising from, allegations of theft, piracy, invasion of privacy, or <bdt class="block-component"></bdt>unauthorized<bdt class="statement-end-if-in-editor"></bdt> use; and (c) any claim for injunctive relief. If this provision is found to be illegal or unenforceable, then neither Party will elect to arbitrate any Dispute falling within that portion of this provision found to be illegal or unenforceable and such Dispute shall be decided by a court of competent jurisdiction within the courts listed for jurisdiction above, and the Parties agree to submit to the personal jurisdiction of that court.<bdt class="else-block"></bdt></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><bdt class="statement-end-if-in-editor"><bdt class="statement-end-if-in-editor"></bdt></bdt></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="corrections" style="line-height: 1.5; text-align: left;"><strong><span style="font-size: 19px; line-height: 1.5;"><h2>16. CORRECTIONS</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;">There may be information on the Services that contains typographical errors, inaccuracies, or omissions, including descriptions, pricing, availability, and various other information. We reserve the right to correct any errors, inaccuracies, or omissions and to change or update the information on the Services at any time, without prior notice.</div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="disclaimer" style="line-height: 1.5; text-align: left;"><span style="font-size: 19px; line-height: 1.5; color: rgb(0, 0, 0);"><strong><h2>17. DISCLAIMER</h2></strong></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">THE SERVICES ARE PROVIDED ON AN AS-IS AND AS-AVAILABLE BASIS. YOU AGREE THAT YOUR USE OF THE SERVICES WILL BE AT YOUR SOLE RISK. TO THE FULLEST EXTENT PERMITTED BY LAW, WE DISCLAIM ALL WARRANTIES, EXPRESS OR IMPLIED, IN CONNECTION WITH THE SERVICES AND YOUR USE THEREOF, INCLUDING, WITHOUT LIMITATION, THE IMPLIED WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT. WE MAKE NO WARRANTIES OR REPRESENTATIONS ABOUT THE ACCURACY OR COMPLETENESS OF THE SERVICES' CONTENT OR THE CONTENT OF ANY WEBSITES OR MOBILE APPLICATIONS LINKED TO THE SERVICES AND WE WILL ASSUME NO LIABILITY OR RESPONSIBILITY FOR ANY (1) ERRORS, MISTAKES, OR INACCURACIES OF CONTENT AND MATERIALS, (2) PERSONAL INJURY OR PROPERTY DAMAGE, OF ANY NATURE WHATSOEVER, RESULTING FROM YOUR ACCESS TO AND USE OF THE SERVICES, (3) ANY <bdt class="block-component"></bdt>UNAUTHORIZED<bdt class="statement-end-if-in-editor"></bdt> ACCESS TO OR USE OF OUR SECURE SERVERS AND/OR ANY AND ALL PERSONAL INFORMATION AND/OR FINANCIAL INFORMATION STORED THEREIN, (4) ANY INTERRUPTION OR CESSATION OF TRANSMISSION TO OR FROM THE SERVICES, (5) ANY BUGS, VIRUSES, TROJAN HORSES, OR THE LIKE WHICH MAY BE TRANSMITTED TO OR THROUGH THE SERVICES BY ANY THIRD PARTY, AND/OR (6) ANY ERRORS OR OMISSIONS IN ANY CONTENT AND MATERIALS OR FOR ANY LOSS OR DAMAGE OF ANY KIND INCURRED AS A RESULT OF THE USE OF ANY CONTENT POSTED, TRANSMITTED, OR OTHERWISE MADE AVAILABLE VIA THE SERVICES. WE DO NOT WARRANT, ENDORSE, GUARANTEE, OR ASSUME RESPONSIBILITY FOR ANY PRODUCT OR SERVICE ADVERTISED OR OFFERED BY A THIRD PARTY THROUGH THE SERVICES, ANY HYPERLINKED WEBSITE, OR ANY WEBSITE OR MOBILE APPLICATION FEATURED IN ANY BANNER OR OTHER ADVERTISING, AND WE WILL NOT BE A PARTY TO OR IN ANY WAY BE RESPONSIBLE FOR MONITORING ANY TRANSACTION BETWEEN YOU AND ANY THIRD-PARTY PROVIDERS OF PRODUCTS OR SERVICES. AS WITH THE PURCHASE OF A PRODUCT OR SERVICE THROUGH ANY MEDIUM OR IN ANY ENVIRONMENT, YOU SHOULD USE YOUR BEST <bdt class="block-component"></bdt>JUDGMENT<bdt class="statement-end-if-in-editor"></bdt> AND EXERCISE CAUTION WHERE APPROPRIATE.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="liability" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-family: Arial; font-size: 19px;"><h2>18. LIMITATIONS OF LIABILITY</h2></span></strong></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><span data-custom-class="body_text">IN NO EVENT WILL WE OR OUR DIRECTORS, EMPLOYEES, OR AGENTS BE LIABLE TO YOU OR ANY THIRD PARTY FOR ANY DIRECT, INDIRECT, CONSEQUENTIAL, EXEMPLARY, INCIDENTAL, SPECIAL, OR PUNITIVE DAMAGES, INCLUDING LOST PROFIT, LOST REVENUE, LOSS OF DATA, OR OTHER DAMAGES ARISING FROM YOUR USE OF THE SERVICES, EVEN IF WE HAVE BEEN ADVISED OF THE POSSIBILITY OF SUCH DAMAGES.</span> <bdt class="block-container if" data-type="if" id="3c3071ce-c603-4812-b8ca-ac40b91b9943"><span data-custom-class="body_text"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="limitations_liability_option" data-type="statement"></bdt><bdt data-type="body">NOTWITHSTANDING ANYTHING TO THE CONTRARY CONTAINED HEREIN, OUR LIABILITY TO YOU FOR ANY CAUSE WHATSOEVER AND REGARDLESS OF THE FORM OF THE ACTION, WILL AT ALL TIMES BE LIMITED TO <bdt class="block-container if" data-type="if" id="73189d93-ed3a-d597-3efc-15956fa8e04e"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="limitations_liability_option" data-type="statement"></bdt><bdt data-type="body">THE
AMOUNT PAID, IF ANY, BY YOU TO US<bdt class="block-container if" data-type="if" id="19e172cb-4ccf-1904-7c06-4251800ba748"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="limilation_liability_time_option" data-type="statement"> </bdt><bdt data-type="body"><span style="font-size: 11pt; color: rgb(89, 89, 89); text-transform: uppercase;">DURING THE <bdt class="block-container question question-in-editor" data-id="5dd68d46-ed6f-61c7-cd66-6b3f424b6bdd" data-type="question">one (1)</bdt> mONTH PERIOD PRIOR TO ANY CAUSE OF ACTION ARISING</span></bdt></bdt><bdt class="statement-end-if-in-editor" data-type="close"></bdt></bdt></bdt></bdt><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="limitations_liability_option" data-type="statement">.</span></bdt> </bdt></span><span data-custom-class="body_text">CERTAIN US STATE LAWS AND INTERNATIONAL LAWS DO NOT ALLOW LIMITATIONS ON IMPLIED WARRANTIES OR THE EXCLUSION OR LIMITATION OF CERTAIN DAMAGES. IF THESE LAWS APPLY TO YOU, SOME OR ALL OF THE ABOVE DISCLAIMERS OR LIMITATIONS MAY NOT APPLY TO YOU, AND YOU MAY HAVE ADDITIONAL RIGHTS.</span></bdt></bdt></span><bdt class="statement-end-if-in-editor" data-type="close"><span data-custom-class="body_text"></span></bdt></bdt></span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="indemnification" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-family: Arial; font-size: 19px;"><h2>19. INDEMNIFICATION</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">You agree to
defend, indemnify, and hold us harmless, including our subsidiaries,
affiliates, and all of our respective officers, agents, partners, and
employees, from and against any loss, damage, liability, claim, or demand, including
reasonable attorneys’ fees and expenses, made by any third party due to or
arising out of: <bdt class="block-container if" data-type="if" id="475fffa5-05ca-def8-ac88-f426b238903c"><bdt data-type="conditional-block"><bdt class="block-component" data-record-question-key="user_post_content_option" data-type="statement"></bdt></bdt>(<span style="font-size: 14.6667px;">1</span>) use of the Services; (<span style="font-size: 14.6667px;">2</span>) breach of these Legal Terms; (<span style="font-size: 14.6667px;">3</span>) any breach of your representations and warranties set forth in these Legal Terms; (<span style="font-size: 14.6667px;">4</span>) your violation of the rights of a third party, including but not limited to intellectual property rights; or (<span style="font-size: 14.6667px;">5</span>) any overt harmful act toward any other user of the Services with whom you connected via the Services. Notwithstanding the foregoing, we reserve the right, at your expense, to assume the exclusive <bdt class="block-component"></bdt>defense<bdt class="statement-end-if-in-editor"></bdt> and control of any matter for which you are required to indemnify us, and you agree to cooperate, at your expense, with our <bdt class="block-component"></bdt>defense<bdt class="statement-end-if-in-editor"></bdt> of such claims. We will use reasonable efforts to notify you of any such claim, action, or proceeding which is subject to this indemnification upon becoming aware of it.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="userdata" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-family: Arial; font-size: 19px;"><h2>20. USER DATA</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">We will maintain
certain data that you transmit to the Services for the purpose of managing the
performance of the Services, as well as data relating to your use of the Services. Although we perform regular routine backups
of data, you are solely responsible for all data that you transmit or that
relates to any activity you have undertaken using the Services. You agree
that we shall have no liability to you for any loss or corruption of any such
data, and you hereby waive any right of action against us arising from any such
loss or corruption of such data.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="electronic" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-family: Arial; font-size: 19px;"><h2>21. ELECTRONIC COMMUNICATIONS, TRANSACTIONS, AND SIGNATURES</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">Visiting the Services, sending us emails, and completing online forms constitute electronic communications. You consent to receive electronic communications, and you agree that all agreements, notices, disclosures, and other communications we provide to you electronically, via email and on the Services, satisfy any legal requirement that such communication be in writing. YOU HEREBY AGREE TO THE USE OF ELECTRONIC SIGNATURES, CONTRACTS, ORDERS, AND OTHER RECORDS, AND TO ELECTRONIC DELIVERY OF NOTICES, POLICIES, AND RECORDS OF TRANSACTIONS INITIATED OR COMPLETED BY US OR VIA THE SERVICES. You hereby waive any rights or requirements under any statutes, regulations, rules, ordinances, or other laws in any jurisdiction which require an original signature or delivery or retention of non-electronic records, or to payments or the granting of credits by any means other than electronic means.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><bdt class="block-component"><span style="font-size: 15px;"></bdt></span><bdt class="block-component"></bdt></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="california" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-family: Arial; font-size: 19px;"><h2>22. CALIFORNIA USERS AND RESIDENTS</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">If any complaint
with us is not satisfactorily resolved, you can contact the Complaint
Assistance Unit of the Division of Consumer Services of the California
Department of Consumer Affairs in writing at 1625 North Market Blvd., Suite N
112, Sacramento, California 95834 or by telephone at (800) 952-5210 or (916)
445-1254.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><bdt class="statement-end-if-in-editor"></bdt></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="misc" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 1.5; font-family: Arial; font-size: 19px;"><h2>23. MISCELLANEOUS</h2></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">These Legal Terms and any policies or operating rules posted by us on the Services or in respect to the Services constitute the entire agreement and understanding between you and us. Our failure to exercise or enforce any right or provision of these Legal Terms shall not operate as a waiver of such right or provision. These Legal Terms operate to the fullest extent permissible by law. We may assign any or all of our rights and obligations to others at any time. We shall not be responsible or liable for any loss, damage, delay, or failure to act caused by any cause beyond our reasonable control. If any provision or part of a provision of these Legal Terms is determined to be unlawful, void, or unenforceable, that provision or part of the provision is deemed severable from these Legal Terms and does not affect the validity and enforceability of any remaining provisions. There is no joint venture, partnership, employment or agency relationship created between you and us as a result of these Legal Terms or use of the Services. You agree that these Legal Terms will not be construed against us by virtue of having drafted them. You hereby waive any and all <bdt class="block-component"></bdt>defenses<bdt class="statement-end-if-in-editor"></bdt> you may have based on the electronic form of these Legal Terms and the lack of signing by the parties hereto to execute these Legal Terms.</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><bdt class="block-component"><span style="font-size: 15px;"></bdt></span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="heading_1" id="contact" style="line-height: 1.5; text-align: left;"><strong><span style="line-height: 115%; font-family: Arial;"><span style="font-size: 19px; line-height: 1.5;"><h2>24. CONTACT US</h2></span></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;">In order to resolve a complaint regarding the Services or to receive further information regarding use of the Services, please contact us at:</span></div><div class="MsoNormal" style="line-height: 1.5; text-align: left;"><br></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><span style="color: rgb(89, 89, 89);"><bdt class="question noTranslate"><strong>__________</strong></bdt><strong><bdt class="block-component"></bdt></span><bdt class="block-component"></bdt></span></span></span></span></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><span style="font-size: 15px;"><strong><span style="color: rgb(89, 89, 89);"><bdt class="question"><bdt class="block-component"></bdt></bdt><bdt class="block-component"></bdt><bdt class="block-component"></bdt></span></strong><strong><span style="color: rgb(89, 89, 89);"><bdt class="block-component"></strong></bdt></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><strong><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><strong><bdt class="block-component"></bdt></strong></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><strong><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><strong><bdt class="block-component"></bdt></strong></span></strong></span></strong></div><div class="MsoNormal" data-custom-class="body_text" style="line-height: 1.5; text-align: left;"><strong><span style="font-size:11.0pt;line-height:115%;font-family:Arial;
Calibri;color:#595959;mso-themecolor:text1;mso-themetint:166;"><strong><bdt class="question"><bdt class="block-component"></bdt></bdt></strong></span></strong></div></div><div style="display: none;"><a class="terms123" href="[https://app.termly.io/dsar/9966e826-b893-4e51-823b-610b7b9fdba4"></a></div></div>
</body>
</html>
    """
    return render_template_string(terms_html)

@app.route("/checkout")
def checkout():
    checkout_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Subscribe - InsuranceGrokBot</title>
        <style>
            body { background:#000; color:#fff; font-family:Arial; text-align:center; padding:100px; }
            h1 { font-size:48px; color:#00ff88; }
            h2 { font-size:60px; margin:30px 0; }
            button { padding:20px 60px; font-size:28px; background:#00ff88; color:#000; border:none; border-radius:12px; cursor:pointer; }
            button:hover { background:#00cc70; }
        </style>
        <script src="https://js.stripe.com/v3/"></script>
    </head>
    <body>
        <h1>InsuranceGrokBot</h1>
        <p style="font-size:24px;">The AI that re-engages your cold leads 24/7</p>
        <h2>$100 / month</h2>
        <button id="checkout-button">Buy Now</button>

        <script>
            const stripe = Stripe('pk_live_51Sn2B3CcnqOm4PhLCrorp8AmVvz6yOOL8JCgDMIO7teIhS1RPjFoMIcuzTIFR71IXTo4IMyScSVzJjwn5mgoRvvQ00Rg3BHYNQ');  // ← Replace with your real pk_live_ or pk_test_

            document.getElementById('checkout-button').addEventListener('click', () => {
                fetch('/create-checkout-session', { method: 'POST' })
                    .then(response => response.json())
                    .then(data => {
                        stripe.redirectToCheckout({ sessionId: data.sessionId });
                    })
                    .catch(err => {
                        console.error('Error:', err);
                        alert('Something went wrong — try again');
                    });
            });
        </script>
    </body>
    </html>
    """
    return render_template_string(checkout_html)

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{
                "price": os.getenv("STRIPE_PRICE_ID"),
                "quantity": 1,
            }],
            allow_promotion_codes=True,
            success_url=f"{YOUR_DOMAIN}/success",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )
        return jsonify({"sessionId": session.id})
    except Exception as e:
        return jsonify(error=str(e)), 403

@app.route("/cancel")
def cancel():
    cancel_html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <title>Checkout Canceled</title>
  <style>
    body { font-family: Arial; background: #000; color: #fff; text-align: center; padding: 100px; }
    a { color: #00ff88; }
  </style>
</head>
<body>
  <h1>Checkout Canceled</h1>
  <p>No worries, come back anytime.</p>
  <p><a href="/">Back to Home</a></p>
</body>
</html>
"""
    return render_template_string(cancel_html)
@app.route("/success")
def success_html():
    success_html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <title>Subscription Successful!</title>
  <style>
    body { font-family: Arial; background: #000; color: #fff; text-align: center; padding: 100px; }
    a { color: #00ff88; font-size: 20px; }
  </style>
</head>
<body>
  <h1>Thank You!</h1>
  <p>Your subscription to InsuranceGrokBot is now active.</p>
  <p><a href="/dashboard">Go to your dashboard to configure your bot</a></p>
</body>
</html>
"""
    return render_template_string(success_html)

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
    # GHL will append ?locationId=... & other params
    location_id = request.args.get("locationId")
    success_html = f"""
    <!DOCTYPE html>
    <html>
    <head><title>Install Complete</title></head>
    <body style="background:#000;color:#fff;text-align:center;padding:100px;font-family:Arial;">
        <h1>✅ InsuranceGrokBot Installed Successfully!</h1>
        <p>Location ID: {location_id or 'Not provided'}</p>
        <p>Your bot is now active please create a workflow in CRM with webhook for response.</p>
        <p><a href="/dashboard" style="color:#00ff88;font-size:20px;">Go to Dashboard → Configure Bot</a></p>
        <p><a href="/" style="color:#888;">← Back to Home</a></p>
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
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Getting Started - InsuranceGrokBot</title>
        <style>
            body { background:#000; color:#fff; font-family:Arial; text-align:center; padding:60px; }
            h1 { font-size:48px; color:#00ff88; }
            .step { margin:40px auto; max-width:600px; font-size:20px; }
            .btn { display:inline-block; padding:15px 40px; background:#00ff88; color:#000; font-weight:bold; text-decoration:none; border-radius:12px; font-size:22px; margin:20px; }
            .btn:hover { background:#00cc70; }
        </style>
    </head>
    <body>
        <h1>Welcome to InsuranceGrokBot</h1>
        <p style="font-size:24px;">Get set up in 3 simple steps</p>

        <div class="step">
            <h2>1. Create Your Account</h2>
            <p>Sign up with your email — takes 10 seconds</p>
            <a href="/register" class="btn">Sign Up Now</a>
        </div>

        <div class="step">
            <h2>2. Subscribe</h2>
            <p>$100/month — cancel anytime</p>
            <a href="/checkout" class="btn">Subscribe ($100/mo)</a>
        </div>

        <div class="step">
            <h2>3. Configure Your Bot</h2>
            <p>Log in and paste your GoHighLevel Location ID + API Key</p>
            <a href="/login" class="btn">Log In → Dashboard</a>
        </div>

        <p style="margin-top:80px;">
            <a href="/demo-chat" style="color:#00ff88;">Try the demo chat</a> | 
            <a href="/" style="color:#888;">← Back</a>
        </p>
    </body>
    </html>
    """
    return render_template_string(html)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)