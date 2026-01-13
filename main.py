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
from openai import OpenAI
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from flask import Flask, render_template_string, request, redirect, url_for, flash, session, make_response
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf import FlaskForm
from flask import jsonify as flask_jsonify
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from rq import Queue

# === IMPORTS ===
from db import get_subscriber_info, get_db_connection, init_db, User
from sync_subscribers import sync_subscribers
# CRITICAL IMPORT: This connects main.py to the logic in tasks.py
from tasks import process_webhook_task  
from memory import get_known_facts, get_narrative, get_recent_messages 
from individual_profile import build_comprehensive_profile 
from utils import make_json_serializable

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

def generate_demo_opener():
    if not client:
        return "Quick question are you still with that life insurance plan you mentioned before? There's some new living benefits people have been asking me about and I wanted to make sure yours doesnt just pay out when you're dead."
    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[
                {"role": "system", "content": """
You are an expert Life Insurance Sales Agent.
Write ONE short, natural SMS (15-25 words) to re-engage an old lead.
VARY approach each time. Tone: casual, professional, high-status.
No "Hi", "Hello", "Name". Start directly.
Examples:
- "Quick question, did you ever get that life insurance policy sorted?"
- "Still looking at coverage options or did you put that on hold?"
- "Circling back on your file, still with the same life insurance policy?"
                """},
                {"role": "user", "content": "Generate unique opener."}
            ],
            temperature=0.95,
            max_tokens=50
        )
        return response.choices[0].message.content.strip().replace('"', '')
    except Exception as e:
        logger.error(f"Demo opener failed: {e}")
        return "Quick question are you still with that life insurance plan you mentioned before? There's some new living benefits people have been asking me about and I wanted to make sure yours doesnt just pay out when you're dead."
# =====================================================
#  THE ASYNC WEBHOOK ENDPOINT
# =====================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    if not q:
        logger.critical("Redis/RQ unavailable — webhook dropped")
        return flask_jsonify({"status": "error", "message": "Queue unavailable"}), 503

    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    if not payload:
        logger.warning("Webhook received empty payload")
        return flask_jsonify({"status": "error", "message": "No payload"}), 400

    location_id = (
        payload.get("location", {}).get("id") or
        payload.get("location_id") or
        payload.get("locationId")
    )
    if not location_id:
        logger.warning("Webhook missing location_id")
        return flask_jsonify({"status": "error", "message": "Location ID missing"}), 400

    try:
        job = q.enqueue(
            process_webhook_task,
            payload,
            job_timeout=120,
            job_id=f"webhook-{uuid.uuid4().hex[:12]}",
            result_ttl=86400
        )
        logger.info(f"Queued webhook job {job.id} | location={location_id}")
        return flask_jsonify({"status": "queued", "job_id": job.id}), 202
    except Exception as e:
        logger.error(f"Queue enqueue failed: {e}", exc_info=True)
        return flask_jsonify({"status": "error", "message": "Internal queue error"}), 500

# =====================================================
#  BELOW THIS LINE: KEEP YOUR EXISTING @app.route("/") 
#  AND OTHER UI CODE EXACTLY AS IT IS
# =====================================================
                    
@app.route("/")
def home():
    home_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>InsuranceGrokBot — AI That Reopens Cold Leads</title>
    <meta name="description" content="The most advanced AI SMS solution for life insurance agents. Re-engages cold leads, books appointments, powered by Grok.">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/aos@2.3.4/dist/aos.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/aos@2.3.4/dist/aos.js"></script>
    <style>
        :root {
            --accent: #00ff88;
            --dark-bg: #000;
            --card-bg: #0a0a0a;
            --text-primary: #ffffff;
            --text-secondary: #cccccc;
            --glow: 0 0 40px rgba(0, 255, 136, 0.25);
            --transition: all 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            background: var(--dark-bg);
            color: var(--text-primary);
            font-family: 'Inter', sans-serif;
            line-height: 1.6;
            overflow-x: hidden;
        }
        .navbar {
            background: rgba(0,0,0,0.85);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid rgba(255,255,255,0.05);
            padding: 1.2rem 0;
        }
        .navbar-brand {
            font-weight: 800;
            font-size: 1.8rem;
            letter-spacing: -0.5px;
            color: #fff !important;
        }
        .nav-link {
            color: var(--text-secondary) !important;
            font-weight: 500;
            padding: 0.6rem 1.2rem !important;
            transition: var(--transition);
        }
        .nav-link:hover {
            color: var(--accent) !important;
        }
        .btn-outline-accent {
            border: 2px solid var(--accent);
            color: var(--accent);
            padding: 0.6rem 1.8rem;
            border-radius: 50px;
            font-weight: 600;
            transition: var(--transition);
        }
        .btn-outline-accent:hover {
            background: var(--accent);
            color: #000;
            box-shadow: var(--glow);
            transform: translateY(-2px);
        }
        .hero {
            min-height: 100vh;
            display: flex;
            align-items: center;
            position: relative;
            background: linear-gradient(135deg, circle at 20% 30%, rgba(0,255,136,0.08) 0%, transparent 50%);
            overflow: hidden;
        }
        .hero::before {
            content: '';
            position: absolute;
            inset: 0;
            background: radial-gradient(circle at 80% 70%, rgba(0,255,136,0.06) 0%, transparent 50%);
            pointer-events: none;
        }
        .hero-content {
            position: relative;
            z-index: 2;
            max-width: 1100px;
            margin: auto;
            padding: 0 20px;
            text-align: center;
        }
        .hero h1 {
            font-size: clamp(3.5rem, 8vw, 7rem);
            font-weight: 800;
            line-height: 1.05;
            margin-bottom: 1.5rem;
            background: linear-gradient(90deg, #fff, var(--accent), #fff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-size: 200% auto;
            animation: gradientFlow 8s ease infinite;
        }
        @keyframes gradientFlow {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        .hero .lead {
            font-size: 1.4rem;
            max-width: 700px;
            margin: 0 auto 2.5rem;
            color: var(--text-secondary);
            opacity: 0;
            animation: fadeInUp 1s forwards 0.5s;
        }
        .hero .btn-group {
            display: flex;
            gap: 1.5rem;
            justify-content: center;
            flex-wrap: wrap;
        }
        .btn-primary {
            background: var(--accent);
            color: #000;
            padding: 1rem 2.5rem;
            border-radius: 50px;
            font-weight: 700;
            font-size: 1.2rem;
            box-shadow: var(--glow);
            transition: var(--transition);
            border: none;
        }
        .btn-primary:hover {
            transform: translateY(-4px) scale(1.03);
            box-shadow: 0 20px 60px rgba(0,255,136,0.4);
        }
        .btn-outline {
            border: 2px solid var(--accent);
            color: var(--accent);
            padding: 1rem 2.5rem;
            border-radius: 50px;
            font-weight: 700;
            font-size: 1.2rem;
            transition: var(--transition);
        }
        .btn-outline:hover {
            background: var(--accent);
            color: #000;
            transform: translateY(-4px);
        }
        .section {
            padding: 120px 20px;
        }
        .section-title {
            font-size: 3.5rem;
            font-weight: 800;
            text-align: center;
            margin-bottom: 80px;
            background: linear-gradient(90deg, #fff, var(--accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .feature-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 2.5rem;
        }
        .feature-card {
            background: rgba(10,10,10,0.8);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(0,255,136,0.15);
            border-radius: 20px;
            padding: 40px 30px;
            transition: var(--transition);
            text-align: center;
        }
        .feature-card:hover {
            transform: translateY(-15px);
            border-color: var(--accent);
            box-shadow: var(--glow);
        }
        .feature-icon {
            font-size: 3.5rem;
            color: var(--accent);
            margin-bottom: 1.5rem;
        }
        .comparison-table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(10,10,10,0.8);
            border-radius: 20px;
            overflow: hidden;
            border: 1px solid rgba(0,255,136,0.15);
        }
        .comparison-table th, .comparison-table td {
            padding: 25px;
            text-align: center;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .comparison-table th {
            background: rgba(0,255,136,0.05);
            color: var(--accent);
            font-weight: 700;
            font-size: 1.4rem;
        }
        .check { color: var(--accent); font-size: 1.8rem; }
        .cross { color: #ff4444; font-size: 1.8rem; }
        .pricing-card {
            background: linear-gradient(135deg, rgba(10,10,10,0.9), #000);
            border: 2px solid var(--accent);
            border-radius: 30px;
            padding: 60px 40px;
            text-align: center;
            max-width: 600px;
            margin: 0 auto;
            box-shadow: var(--glow);
        }
        .price {
            font-size: 6rem;
            font-weight: 800;
            color: var(--accent);
            margin-bottom: 1rem;
        }
        footer {
            padding: 80px 20px;
            text-align: center;
            border-top: 1px solid #222;
            background: rgba(0,0,0,0.8);
        }
        footer a {
            color: var(--text-secondary);
            margin: 0 15px;
            text-decoration: none;
            transition: color 0.3s;
        }
        footer a:hover { color: var(--accent); }
        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(40px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .animate-fade { animation: fadeInUp 1.2s forwards; }
        @media (max-width: 992px) {
            .hero h1 { font-size: 3.8rem; }
            .section-title { font-size: 2.8rem; }
            .feature-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg fixed-top">
        <div class="container">
            <a class="navbar-brand" href="/">InsuranceGrokBot</a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav ms-auto">
                    <li class="nav-item"><a class="nav-link" href="#features">Features</a></li>
                    <li class="nav-item"><a class="nav-link" href="/comparison">Comparison</a></li>
                    <li class="nav-item"><a class="nav-link" href="#logic">How It Works</a></li>
                    <li class="nav-item"><a class="nav-link" href="#pricing">Pricing</a></li>
                    <li class="nav-item"><a class="nav-link" href="/getting-started">Get Started</a></li>
                    <li class="nav-item"><a class="nav-link" href="/demo-chat">Live Demo</a></li>
                    <li class="nav-item ms-lg-4">
                        <a href="/login" class="btn btn-outline-accent">Log In</a>
                    </li>
                    <li class="nav-item ms-3">
                        <a href="/register" class="btn btn-primary">Sign Up</a>
                    </li>
                </ul>
            </div>
        </div>
    </nav>

    <section class="hero">
        <div class="hero-content animate-fade">
            <h1>Reopen Cold Leads. Book Appointments. Automatically.</h1>
            <p class="lead">The most intelligent AI SMS platform for life insurance agents — powered by Grok from xAI.</p>
            <div class="btn-group">
                <a href="/checkout" class="btn btn-primary">Start Free Trial</a>
                <a href="/demo-chat" class="btn btn-outline-accent">Watch Live Demo</a>
            </div>
        </div>
    </section>

    <section id="features" class="section">
        <div class="container">
            <h2 class="section-title">Built for Real Results</h2>
            <div class="feature-grid">
                <div class="feature-card">
                    <div class="feature-icon">🧠</div>
                    <h4>Human-Like Memory</h4>
                    <p>Remembers every detail across conversations. No repeated questions. Builds trust fast.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">⚡</div>
                    <h4>5 Elite Frameworks</h4>
                    <p>Blends NEPQ, Gap Selling, Straight Line, Chris Voss, and Zig Ziglar — adapts in real time.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">🛡️</div>
                    <h4>Underwriting Intelligence</h4>
                    <p>Knows carrier rules live. Spots red flags early. Suggests the right products.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">🔥</div>
                    <h4>Never Stops</h4>
                    <p>Loops, reframes, persists. Turns "no" into bookings — or identifies real dead leads.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">🎯</div>
                    <h4>Qualified Only</h4>
                    <p>Books appointments with genuine need/gap. No tire-kickers wasting your calendar.</p>
                </div>
                <div class="feature-card">
                    <div class="feature-icon">🏢</div>
                    <h4>Agency-Ready</h4>
                    <p>Multi-tenant, isolated data, custom branding per location. Scale without chaos.</p>
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
            <p>&copy; 2026 InsuranceGrokBot. All rights reserved.</p>
            <p>
                <a href="/terms" style="color:var(--text-secondary);">Terms</a> • 
                <a href="/privacy" style="color:var(--text-secondary);">Privacy</a> • 
                <a href="/disclaimers" style="color:var(--text-secondary);">Disclaimers</a>
            </p>
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
        if email and customer_id:
            user = User.get(email)
            if not user:
                User.create(email, None, customer_id)
                logger.info(f"Created user from Stripe: {email}")
            else:
                conn = get_db_connection()
                if conn:
                    try:
                        cur = conn.cursor()
                        cur.execute("UPDATE users SET stripe_customer_id = %s WHERE email = %s", (customer_id, email))
                        conn.commit()
                    except Exception as e:
                        logger.error(f"Stripe DB update failed: {e}")
                    finally:
                        cur.close()
                        conn.close()

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

    # Fetch Sheet Data & Headers
    values = worksheet.get_all_values() if worksheet else []
    if not values:
        headers = ["email", "location_id", "calendar_id", "access_token", "refresh_token", "crm_user_id", "bot_first_name", "timezone", "initial_message", "stripe_customer_id", "confirmation_code", "code_used", "user_name", "phone", "bio"]
        if worksheet:
            worksheet.append_row(headers)
        values = [headers]

    header = values[0]
    header_lower = [h.strip().lower() for h in header]

    def col_index(name):
        try:
            return header_lower.index(name.lower())
        except ValueError:
            return -1

    # Map indices
    email_idx = col_index("email")
    location_idx = col_index("location_id")
    calendar_idx = col_index("calendar_id")
    access_token_idx = col_index("access_token")
    refresh_token_idx = col_index("refresh_token")
    user_id_idx = col_index("crm_user_id")
    bot_name_idx = col_index("bot_first_name")
    timezone_idx = col_index("timezone")
    initial_msg_idx = col_index("initial_message")
    stripe_idx = col_index("stripe_customer_id")
    user_name_idx = col_index("user_name")
    phone_idx = col_index("phone")
    bio_idx = col_index("bio")

    # Find user's row
    user_row_num = None
    for i, row in enumerate(values[1:], start=2):
        if email_idx >= 0 and len(row) > email_idx and row[email_idx].strip().lower() == current_user.email.lower():
            user_row_num = i
            break

    # Pre-fill form (your existing code)
    if user_row_num and values:
        row = values[user_row_num - 1]
        if location_idx >= 0 and len(row) > location_idx: form.location_id.data = row[location_idx]
        # ... rest of pre-fill ...

    # Fetch current tokens & subscriber config (FIXED)
    location_id = None
    if user_row_num and values:
        row = values[user_row_num - 1]
        if location_idx >= 0 and len(row) > location_idx:
            location_id = row[location_idx].strip()

    sub = get_subscriber_info(location_id) if location_id else None

    # Safe display values
    access_token_display = 'Not set'
    refresh_token_display = 'Not set'
    expires_in_str = 'Not set'

    if sub:
        access_token_full = sub.get('access_token', 'Not set')
        access_token_display = access_token_full[:8] + '...' + access_token_full[-4:] if len(access_token_full) > 12 else access_token_full
        
        refresh_token_full = sub.get('refresh_token', 'Not set')
        refresh_token_display = refresh_token_full[:8] + '...' + refresh_token_full[-4:] if len(refresh_token_full) > 12 else refresh_token_full
        
        expires_at = sub.get('token_expires_at')
        if expires_at:
            delta = expires_at - datetime.now()
            hours = delta.total_seconds() // 3600
            minutes = (delta.total_seconds() % 3600) // 60
            expires_in_str = f"Expires in {int(hours)}h {int(minutes)}m"
        else:
            expires_in_str = "Persistent (no expiry)"

    # Now pass these to template
    return render_template_string(
"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - InsuranceGrokBot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; min-height:100vh; }
        .container-fluid { padding: 40px 20px; }
        .sidebar { background:var(--card-bg); border-right:1px solid #333; height:100vh; position:fixed; width:300px; padding:20px; overflow-y:auto; }
        .main-content { margin-left:320px; padding:20px; }
        h1 { color:var(--accent); text-shadow:var(--neon-glow); }
        .card { background:var(--card-bg); border:1px solid #333; border-radius:15px; box-shadow:0 10px 30px rgba(0,255,136,0.1); }
        .form-label { color:#ddd; font-weight:600; }
        .input-group-text { background:#111; border:1px solid #333; color:var(--accent); }
        .btn-copy { background:#222; border:1px solid #444; color:#fff; }
        .token-expiry { color:#aaa; font-size:0.9rem; }
        .toggle-btn { cursor:pointer; color:var(--accent); font-size:1.2rem; }
        @media (max-width: 992px) {
            .sidebar { position:relative; width:100%; height:auto; border-right:none; border-bottom:1px solid #333; }
            .main-content { margin-left:0; }
        }
    </style>
</head>
<body>
    <div class="d-flex">
        <!-- Side Menu -->
        <div class="sidebar">
            <h4 class="text-center mb-4" style="color:var(--accent);">Your Configuration</h4>
            <div class="mb-3">
                <label class="form-label">Location ID</label>
                <div class="input-group">
                    <input type="text" class="form-control bg-dark text-white" value="{{ form.location_id.data or '' }}" readonly>
                    <button class="btn btn-copy" onclick="copyToClipboard('{{ form.location_id.data or '' }}')">Copy</button>
                </div>
            </div>
            <div class="mb-3">
                <label class="form-label">Access Token</label>
                <div class="input-group">
                    <input type="text" class="form-control bg-dark text-white" value="{{ access_token_display }}" readonly>
                    <button class="btn btn-copy" onclick="copyToClipboard('{{ sub.get('access_token', '') }}')">Copy</button>
                </div>
                <div class="token-expiry">{{ expires_in_str }}</div>
            </div>
            <div class="mb-3">
                <label class="form-label">Refresh Token</label>
                <div class="input-group">
                    <input type="text" class="form-control bg-dark text-white" value="{{ refresh_token_display }}" readonly>
                    <button class="btn btn-copy" onclick="copyToClipboard('{{ sub.get('refresh_token', '') }}')">Copy</button>
                </div>
            </div>
            <!-- More fields here... -->
            <hr class="bg-secondary">
            <h5 class="text-center" style="color:var(--accent);">User Profile</h5>
            <div class="mb-3">
                <label class="form-label">Full Name</label>
                <input type="text" class="form-control bg-dark text-white" value="{{ row[user_name_idx] if user_row_num else '' }}" id="user_name">
            </div>
            <div class="mb-3">
                <label class="form-label">Phone</label>
                <input type="tel" class="form-control bg-dark text-white" value="{{ row[phone_idx] if user_row_num else '' }}" id="phone">
            </div>
            <div class="mb-3">
                <label class="form-label">Bio</label>
                <textarea class="form-control bg-dark text-white" rows="3" id="bio">{{ row[bio_idx] if user_row_num else '' }}</textarea>
            </div>
            <button class="btn btn-primary w-100" onclick="saveProfile()">Save Profile</button>
            <h5 class="text-center mb-4" style="color:var(--accent);">Support</h5>
            <div class="side-item">
                <a href="/contact">Contact Us</a>
            </div>
        </div>

        <!-- Main Content -->
        <div class="main-content">
            <h1>Dashboard</h1>
            <p class="welcome">Welcome back, <strong>{{ current_user.email }}</strong></p>

            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert {{ 'alert-success' if category == 'success' else 'alert-danger' }}">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}

            <!-- Tabs -->
            <ul class="nav nav-tabs mb-4">
                <li class="nav-item"><a class="nav-link active" data-bs-toggle="tab" href="#config">Configuration</a></li>
                <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#guide">Marketplace Setup</a></li>
                <li class="nav-item"><a class="nav-link" data-bs-toggle="tab" href="#billing">Billing</a></li>
            </ul>

            <div class="tab-content">
                <div class="tab-pane fade show active" id="config">
                    <div class="card p-4">
                        <h3 style="color:var(--accent);">Bot Settings</h3>
                        <form method="post">
                            {{ form.hidden_tag() }}
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    {{ form.location_id.label(class="form-label") }}
                                    {{ form.location_id(class="form-control bg-dark text-white") }}
                                </div>
                                <div class="col-md-6 mb-3">
                                    {{ form.calendar_id.label(class="form-label") }}
                                    {{ form.calendar_id(class="form-control bg-dark text-white") }}
                                </div>
                                <div class="col-md-6 mb-3">
                                    {{ form.crm_api_key.label(class="form-label") }}
                                    {{ form.crm_api_key(class="form-control bg-dark text-white") }}
                                </div>
                                <div class="col-md-6 mb-3">
                                    {{ form.crm_user_id.label(class="form-label") }}
                                    {{ form.crm_user_id(class="form-control bg-dark text-white") }}
                                </div>
                                <div class="col-md-6 mb-3">
                                    {{ form.timezone.label(class="form-label") }}
                                    {{ form.timezone(class="form-control bg-dark text-white") }}
                                </div>
                                <div class="col-md-6 mb-3">
                                    {{ form.bot_name.label(class="form-label") }}
                                    {{ form.bot_name(class="form-control bg-dark text-white") }}
                                </div>
                                <div class="col-12 mb-3">
                                    {{ form.initial_message.label(class="form-label") }}
                                    {{ form.initial_message(class="form-control bg-dark text-white") }}
                                </div>
                            </div>
                            <button type="submit" class="btn btn-primary w-100">Save Settings</button>
                        </form>
                    </div>
                </div>

                <div class="tab-pane fade" id="guide">
                    <div class="card p-4">
                        <h3 style="color:var(--accent);">Marketplace Setup Guide</h3>
                        <p>Follow these steps to connect via the GoHighLevel Marketplace.</p>
                        <ol class="list-group list-group-numbered">
                            <li class="list-group-item bg-dark text-white border-0">Log in to your GoHighLevel account.</li>
                            <li class="list-group-item bg-dark text-white border-0">Go to Marketplace in the left sidebar.</li>
                            <li class="list-group-item bg-dark text-white border-0">Search for "Insurance Grok Bot" and click Install.</li>
                            <li class="list-group-item bg-dark text-white border-0">Approve the scopes (contacts, conversations, calendars, etc.).</li>
                            <li class="list-group-item bg-dark text-white border-0">After install, your tokens and location details are automatically imported and stored.</li>
                            <li class="list-group-item bg-dark text-white border-0">Log in here and verify everything in your dashboard.</li>
                        </ol>
                        <a href="https://marketplace.gohighlevel.com/" target="_blank" class="btn btn-primary mt-4">Open GHL Marketplace</a>
                    </div>
                </div>

                <div class="tab-pane fade" id="billing">
                    <div class="card p-4">
                        <h3 style="color:var(--accent);">Billing & Subscription</h3>
                        {% if current_user.stripe_customer_id %}
                            <p>Manage your subscription, update payment method, or view invoices.</p>
                            <form method="post" action="/create-portal-session">
                                <button type="submit" class="btn btn-primary">Open Stripe Portal</button>
                            </form>
                        {% else %}
                            <p>Your subscription is managed via the GoHighLevel Marketplace.</p>
                            <a href="https://marketplace.gohighlevel.com/" target="_blank" class="btn btn-primary">Manage in Marketplace</a>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        function copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => {
                alert("Copied to clipboard!");
            }).catch(err => {
                console.error("Copy failed", err);
            });
        }

        function saveProfile() {
            const name = document.getElementById('user_name').value;
            const phone = document.getElementById('phone').value;
            const bio = document.getElementById('bio').value;
            fetch('/save-profile', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name, phone, bio})
            }).then(r => r.json()).then(d => alert(d.message || 'Saved!'));
        }
    </script>
</body>
</html>
    """, form=form, access_token_display=access_token_display, refresh_token_display=refresh_token_display, expires_in_str=expires_in_str, sub=sub)

@app.route("/save-profile", methods=["POST"])
@login_required
def save_profile():
    data = request.json
    name = data.get('name')
    phone = data.get('phone')
    bio = data.get('bio')

    # Save to sheet (similar to config save)
    if worksheet:
        # Find row (same logic as dashboard)
        values = worksheet.get_all_values()
        header_lower = [h.strip().lower() for h in values[0]]
        user_name_idx = header_lower.index("user_name") if "user_name" in header_lower else -1
        phone_idx = header_lower.index("phone") if "phone" in header_lower else -1
        bio_idx = header_lower.index("bio") if "bio" in header_lower else -1

        user_row_num = None
        for i, row in enumerate(values[1:], start=2):
            if row and row[header_lower.index("email")].strip().lower() == current_user.email.lower():
                user_row_num = i
                break

        if user_row_num:
            row_data = values[user_row_num - 1]
            if user_name_idx >= 0: row_data[user_name_idx] = name or ""
            if phone_idx >= 0: row_data[phone_idx] = phone or ""
            if bio_idx >= 0: row_data[bio_idx] = bio or ""
            worksheet.update(f"A{user_row_num}", [row_data])
            return flask_jsonify({"message": "Profile updated!"})

    return flask_jsonify({"message": "Profile saved (but sheet not found)"}), 200

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

    # 1. PERSISTENCE CHECK
    existing_id = request.args.get('session_id')
    clean_id = str(uuid.uuid4())  # Default to new

    initial_msg = ""  # Placeholder

    if existing_id:
        try:
            clean_id = str(uuid.UUID(existing_id))
            # Resume: no new opener — JS loads history
        except ValueError:
            pass  # Invalid → new

    session['demo_session_id'] = clean_id
    demo_contact_id = f"demo_{clean_id}"

    # 2. NEW SESSION = NEW OPENER (only if truly new)
    if not existing_id:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                # Clear old data
                cur.execute("DELETE FROM contact_messages WHERE contact_id = %s", (demo_contact_id,))
                cur.execute("DELETE FROM contact_facts WHERE contact_id = %s", (demo_contact_id,))
                cur.execute("DELETE FROM contact_narratives WHERE contact_id = %s", (demo_contact_id,))

                # Generate unique opener
                initial_msg = generate_demo_opener()

                # Inject into DB
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

    demo_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, interactive-widget=resizes-content">
    <title>Live AI Demo - InsuranceGrokBot</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/aos@2.3.4/dist/aos.css" rel="stylesheet">
    <style>
        :root {{ 
            --accent: #00ff88; 
            --safe-top: env(safe-area-inset-top, 20px); 
            --safe-bottom: env(safe-area-inset-bottom, 20px); 
        }}
        body {{ 
            background: #000; 
            color: #fff; 
            font-family: 'Montserrat', sans-serif; 
            height: 100dvh; 
            margin: 0; 
            overflow: hidden; 
        }}
        .main-wrapper {{ 
            display: flex; 
            width: 100vw; 
            height: 100dvh; 
        }}
        .chat-col {{ 
            flex: 1; 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            background: radial-gradient(circle at center, #1a1a1a 0%, #000 70%); 
            padding: var(--safe-top) 10px var(--safe-bottom) 10px; 
        }}
        .phone {{ 
            width: 100%; 
            max-width: 380px; 
            height: 90dvh; 
            max-height: 850px; 
            background: #000; 
            border: 8px solid #333; 
            border-radius: 40px; 
            display: flex; 
            flex-direction: column; 
            position: relative; 
            overflow: hidden; 
            box-shadow: 0 20px 50px rgba(0, 255, 136, 0.1); 
        }}
        .notch {{ 
            position: absolute; 
            top: 0; 
            left: 50%; 
            transform: translateX(-50%); 
            width: 150px; 
            height: 30px; 
            background: #333; 
            border-bottom-left-radius: 18px; 
            border-bottom-right-radius: 18px; 
            z-index: 10; 
        }}
        
        @media (max-width: 600px) {{
            .chat-col {{ padding: 0; background: #000; }}
            .phone {{ height: 100dvh; max-height: none; border: none; border-radius: 0; padding-top: var(--safe-top); padding-bottom: var(--safe-bottom); }}
            .notch {{ display: none; }}
            .screen {{ padding-bottom: 100px; }}
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
            padding: 12px 15px;
            background: #111;
            display: flex;
            gap: 10px;
            border-top: 1px solid #222;
            z-index: 11;
            min-height: 60px;
            align-items: flex-end;
        }}
        .grow-wrap {{
            flex: 1;
            display: grid;
            position: relative;
        }}
        .grow-wrap::after {{
            content: attr(data-replicated-value) " ";
            white-space: pre-wrap;
            overflow-wrap: break-word;
            visibility: hidden;
            grid-area: 1 / 1 / 2 / 2;
            padding: inherit;
            font: inherit;
            border: inherit;
            border-radius: inherit;
            line-height: inherit;
            margin: 0;
            pointer-events: none;
        }}
        .grow-wrap textarea {{
            grid-area: 1 / 1 / 2 / 2;
            resize: none;
            overflow: hidden;
            padding: 12px 16px;
            border-radius: 22px;
            border: 1px solid #333;
            background: #222;
            color: #fff;
            outline: none;
            font-size: 16px;
            font-family: inherit;
            line-height: 1.4;
            min-height: 44px;
            max-height: 160px;
            overflow-y: auto;
            overflow-wrap: break-word;
            white-space: pre-wrap;
        }}
        button.send-btn {{
            width: 45px;
            height: 45px;
            border-radius: 50%;
            border: none;
            background: var(--accent);
            color: #000;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
        }}
       
        .msg {{
            padding: 12px 16px;
            border-radius: 18px;
            max-width: 85%;
            font-size: 14px;
            line-height: 1.4;
            white-space: pre-wrap;
            animation: popIn 0.3s ease-out;
        }}
        .bot {{
            background: #262626;
            align-self: flex-start;
            color: #e0e0e0;
            border-bottom-left-radius: 4px;
        }}
        .user {{
            background: var(--accent);
            align-self: flex-end;
            color: #000;
            border-bottom-right-radius: 4px;
            font-weight: 600;
        }}
        @keyframes popIn {{
            from {{ opacity: 0; transform: translateY(10px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
       
        .log-col {{
            width: 450px;
            background: #0a0a0a;
            display: flex;
            flex-direction: column;
            padding: 25px;
            border-left: 1px solid #222;
        }}
        #logs {{
            flex: 1;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 12px;
        }}
        .log-entry {{
            margin-bottom: 20px;
            border-left: 2px solid #333;
            padding-left: 15px;
        }}
        .controls {{
            margin-top: 20px;
            display: flex;
            gap: 10px;
        }}
        .btn {{
            flex: 1;
            padding: 12px;
            border-radius: 8px;
            font-weight: 600;
            font-size: 13px;
            cursor: pointer;
            text-decoration: none;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .reset-btn {{
            background: transparent;
            border: 1px solid #ff4444;
            color: #ff4444;
        }}
        .download-btn {{
            background: #222;
            color: #fff;
            border: 1px solid #444;
        }}
        @media (max-width: 900px) {{
            .log-col {{ display: none !important; }}
        }}
        .side-menu {{
            position: fixed;
            top: 0;
            right: 0;
            height: 100vh;
            width: 280px;
            background: #0a0a0a;
            border-left: 1px solid #333;
            transform: translateX(100%);
            transition: transform 0.3s ease;
            padding: 30px 20px;
            z-index: 1000;
            box-shadow: -10px 0 20px rgba(0,0,0,0.5);
        }}
        .side-menu.open {{
            transform: translateX(0);
        }}
        .side-btn {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: var(--accent);
            color: #000;
            border: none;
            border-radius: 50%;
            width: 50px;
            height: 50px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            z-index: 1100;
            box-shadow: var(--neon-glow);
        }}
        .side-btn i {{ font-size: 1.5rem; }}
        .side-item {{ margin-bottom: 15px; }}
        .side-item a {{ color: #fff; font-size: 1.1rem; display: block; padding: 10px; border-radius: 8px; background: #111; text-align: center; text-decoration: none; }}
        .side-item a:hover {{ background: #222; color: var(--accent); }}
        .thinking {{
            display: none;
            background: #262626;
            align-self: flex-start;
            padding: 10px 15px;
            border-radius: 18px;
            border-bottom-left-radius: 4px;
            font-size: 18px;
            color: #e0e0e0;
        }}
        .thinking.show {{ display: block; }}
        .dot {{ animation: dotBlink 1.4s infinite ease-in-out; }}
        .dot:nth-child(2) {{ animation-delay: 0.2s; }}
        .dot:nth-child(3) {{ animation-delay: 0.4s; }}
        @keyframes dotBlink {{
            0% {{ opacity: 0.3; }}
            50% {{ opacity: 1; }}
            100% {{ opacity: 0.3; }}
        }}
        .low-notice {{
            position: absolute;
            top: 40px;
            left: 50%;
            transform: translateX(-50%);
            background: #ff4444;
            color: #fff;
            padding: 10px 20px;
            border-radius: 8px;
            font-size: 14px;
            z-index: 20;
            box-shadow: 0 4px 10px rgba(0,0,0,0.5);
        }}
    </style>
</head>
<body>
<div class="main-wrapper">
    <div class="chat-col">
        <div class="phone">
            <div class="notch"></div>
            <div class="screen" id="chat">
                <!-- JS loads opener -->
            </div>
            <div class="input-area">
                <div class="grow-wrap">
                    <textarea id="chat-input" placeholder="Type a message..." rows="1" autofocus autocomplete="off"></textarea>
                </div>
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
        <h3 style="color:#00ff88; text-transform:uppercase; border-bottom:1px solid #333; padding-bottom:15px; margin-top:0;">Live Brain Activity</h3>
        <div id="logs">
            <div style="color:#666; margin-top:20px;">Waiting for user input...</div>
        </div>
        <div class="controls">
            <a href="/download-transcript?contact_id={{ demo_contact_id }}" target="_blank" class="btn download-btn">Download Log</a>
            <button class="btn reset-btn" onclick="resetSession();">Reset Session</button>
        </div>
    </div>
</div>

<!-- Side Menu (hidden, slide-out) -->
<div class="side-menu" id="sideMenu">
    <h4 style="color:var(--accent); margin-bottom:20px;">Options</h4>
    <div class="side-item">
        <a href="#" onclick="resetSession();">Refresh Session</a>
    </div>
    <div class="side-item">
        <a href="/download-transcript?contact_id={{ demo_contact_id }}" target="_blank">Download Logs</a>
    </div>
</div>

<button class="side-btn" onclick="toggleSideMenu()"><i class="fas fa-bars"></i></button>

<!-- Low Battery Notice (hidden) -->
<div class="low-notice" id="lowBatteryNotice" style="display:none;">
    Low Battery - 2 min left. Page will refresh and start new session.
</div>

<script>
    // PERSISTENCE
    const url = new URL(window.location);
    if (!url.searchParams.has('session_id')) {{
        url.searchParams.set('session_id', '{{ clean_id }}');
        window.history.replaceState({{}}, '', url);
    }}

    const CONTACT_ID = '{{ demo_contact_id }}';
    const chat = document.getElementById('chat');

    // Pass opener from Python (empty on resume)
    const STARTING_MSG = '{{ initial_msg }}';

    let msgCount = 0;

    async function syncData() {{
        try {{
            const res = await fetch(`/get-logs?contact_id=${{CONTACT_ID}}`);
            const data = await res.json();
           
            if (data.logs && data.logs.length > 0) {{
                const messages = data.logs.filter(l => l.type.includes('Message'));
               
                if (messages.length > msgCount) {{
                    const newMessages = messages.slice(msgCount);
                   
                    const dynamicMsgs = newMessages.map(msg => {{
                        // Skip if exact match to STARTING_MSG (safety net)
                        if (STARTING_MSG && msg.content.trim() === STARTING_MSG.trim() && msgCount === 0) {{
                            return '';
                        }}
                       
                        const isBot = msg.type.includes('Bot') || msg.type.includes('Assistant');
                        return `<div class="msg ${{isBot ? 'bot' : 'user'}}">${{msg.content}}</div>`;
                    }}).join('');
                   
                    if (dynamicMsgs) {{
                        chat.insertAdjacentHTML('beforeend', dynamicMsgs);
                        chat.scrollTop = chat.scrollHeight;
                    }}
                    msgCount = messages.length;
                }}
               
                // Update logs (brain activity)
                if (data.logs) {{
                    document.getElementById('logs').innerHTML = data.logs.map(l => `
                        <div class="log-entry">
                            <span class="log-ts">[${{l.timestamp.split('T')[1].split('.')[0]}}]</span>
                            <span class="log-type">${{l.type}}</span><br>
                            ${{l.content}}
                        </div>
                    `).join('');
                    document.getElementById('logs').scrollTop = document.getElementById('logs').scrollHeight;
                }}
            }}
        }} catch (err) {{
            console.error("Sync error:", err);
        }}
    }}

    function send() {{
        const msg = document.getElementById('chat-input').value.trim();
        if (!msg) return;

        chat.innerHTML += `<div class="msg user">${{msg}}</div>`;
        document.getElementById('chat-input').value = '';
        chat.scrollTop = chat.scrollHeight;

        const thinkingBubble = showThinking();

        // Play iMessage swoosh sound
        new Audio('https://www.soundjay.com/buttons/swoosh-1.mp3').play();

        fetch('/webhook', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                location_id: 'TEST_LOCATION_456',
                contact_id: CONTACT_ID,
                first_name: 'Demo User',
                message: {{ body: msg }}
            }})
        }}).then(r => r.json()).then(d => {{
            if (d.reply) {{
                chat.innerHTML += `<div class="msg bot">${{d.reply}}</div>`;
                chat.scrollTop = chat.scrollHeight;
            }}
            syncData();
        }}).catch(err => console.error("Send error:", err))
        .finally(() => hideThinking(thinkingBubble));
    }}

    // Send on Enter (no Shift)
    document.getElementById('chat-input').addEventListener('keypress', e => {{
        if (e.key === 'Enter' && !e.shiftKey) {{
            e.preventDefault();
            send();
        }}
    }});

    // Focus auto-scroll
    document.getElementById('chat-input').addEventListener('focus', () => {{
        setTimeout(() => chat.scrollTop = chat.scrollHeight, 300);
    }});

    // Thinking Bubble
    function showThinking() {{
        const thinking = document.createElement('div');
        thinking.classList.add('thinking', 'msg', 'bot');
        thinking.innerHTML = '<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>';
        chat.appendChild(thinking);
        chat.scrollTop = chat.scrollHeight;
        return thinking;
    }}

    function hideThinking(bubble) {{
        if (bubble) bubble.remove();
    }}

    // Battery Depletion (10 min timer, low at 2 min)
    let batteryLevel = 100;
    const batteryTimer = setInterval(() => {{
        batteryLevel -= (100 / 600);  // Deplete over 10 min (600s)
        if (batteryLevel <= 20) {{
            document.getElementById('lowBatteryNotice').style.display = 'block';
        }}
        if (batteryLevel <= 0) {{
            resetSession();
        }}
    }}, 1000);

    // Toggle Side Menu
    function toggleSideMenu() {{
        const menu = document.getElementById('sideMenu');
        menu.classList.toggle('open');
    }}

    // Reset session
    function resetSession() {{
        window.location.href = '/demo-chat?session_id=' + crypto.randomUUID();
    }}

    // Initial load + polling
    syncData();  // Load opener immediately
    setInterval(syncData, 2000);

    // Initialize AOS animations (must be after DOM ready)
    AOS.init({{
        duration: 1200,
        once: true,
        offset: 120,
        easing: 'ease-out'
    }});
</script>
</body>
</html>
    """
    return render_template_string(demo_html, clean_id=clean_id, demo_contact_id=demo_contact_id, initial_msg=initial_msg)

@app.route("/disclaimers")
def disclaimers():
    disclaimers_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Disclaimers - InsuranceGrokBot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); }
        body { background:var(--dark-bg); color:#fff; font-family:'Montserrat',sans-serif; padding:80px 20px; min-height:100vh; }
        .container { max-width:900px; margin:auto; background:var(--card-bg); padding:60px; border-radius:20px; border:1px solid #333; box-shadow:0 10px 30px var(--neon-glow); }
        h1 { color:var(--accent); text-shadow:var(--neon-glow); text-align:center; margin-bottom:40px; }
        p, li { font-size:1.1rem; line-height:1.8; color:#ddd; }
        ul { padding-left:30px; margin:30px 0; }
        .back { text-align:center; margin-top:60px; }
        .back a { color:var(--accent); font-size:1.4rem; text-decoration:none; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Disclaimers</h1>
        
        <h3>AI-Generated Content</h3>
        <p>InsuranceGrokBot uses artificial intelligence (powered by xAI's Grok models) to generate responses. AI can make mistakes, provide inaccurate information, or misunderstand context. All responses should be treated as informational only and not as professional advice.</p>
        
        <h3>Not Financial, Legal, or Insurance Advice</h3>
        <p>Nothing on this platform constitutes financial, legal, insurance, tax, or medical advice. Always consult licensed professionals (insurance agents, financial advisors, attorneys, etc.) before making decisions about coverage, policies, or any related matters.</p>
        
        <h3>No Affiliation</h3>
        <p>InsuranceGrokBot is an independent tool created by a third party. It is not affiliated with, endorsed by, or officially connected to xAI, GoHighLevel, or any insurance carrier. References to third-party services are for informational purposes only.</p>
        
        <h3>Limitation of Liability</h3>
        <p>Use of this service is at your own risk. The creators are not liable for any damages, losses, or consequences (direct or indirect) arising from use of InsuranceGrokBot, including but not limited to inaccurate information, missed opportunities, or reliance on AI-generated content.</p>
        
        <h3>Accuracy & Updates</h3>
        <p>Information (including underwriting rules, carrier data, and pricing) is pulled from public sources and may not always be current or complete. Always verify with official sources.</p>
        
        <h3>Privacy & Data</h3>
        <p>Demo conversations are stored temporarily and deleted automatically. Registered users' data is handled per our <a href="/privacy" style="color:var(--accent);">Privacy Policy</a>.</p>

        <div class="back">
            <a href="/">← Back to Home</a>
        </div>
    </div>
</body>
</html>
    """
    return render_template_string(disclaimers_html)

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

@app.route("/contact")
def contact():
    contact_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Contact Us - InsuranceGrokBot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); --text-secondary: #aaa; }
        body { background: var(--dark-bg); color: #fff; font-family: 'Montserrat', sans-serif; padding: 80px 20px; min-height: 100vh; }
        .container { max-width: 900px; margin: auto; background: var(--card-bg); padding: 60px; border-radius: 20px; border: 1px solid #333; box-shadow: 0 10px 30px var(--neon-glow); }
        h1 { color: var(--accent); text-shadow: var(--neon-glow); text-align: center; margin-bottom: 40px; font-weight: 800; font-size: 3rem; }
        p, li { font-size: 1.2rem; color: #ddd; line-height: 1.8; }
        .email-link { color: var(--accent); font-weight: 700; text-decoration: none; font-size: 1.5rem; }
        .email-link:hover { text-decoration: underline; }
        .back { text-align: center; margin-top: 60px; }
        .back a { color: var(--accent); font-size: 1.4rem; text-decoration: none; }
        @media (max-width: 768px) {
            h1 { font-size: 2.5rem; }
            .container { padding: 40px 20px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Contact Us</h1>
        
        <p class="text-center mb-5">We’re here to help with any questions about InsuranceGrokBot, setup, billing, or support.</p>
        
        <div class="text-center mb-5">
            <p style="font-size: 1.4rem;">The best way to reach us is by email:</p>
            <a href="mailto:support@insurancegrokbot.click" class="email-link">support@insurancegrokbot.click</a>
        </div>
        
        <p class="text-center">We typically respond within 24–48 hours (often faster). Please include as much detail as possible about your question or issue (e.g., location ID, error messages, screenshots if relevant).</p>
        
        <p class="text-center mt-4">Thank you for using InsuranceGrokBot — we appreciate your feedback and support!</p>

        <div class="back">
            <a href="/">← Back to Home</a>
        </div>
    </div>
</body>
</html>
    """
    return render_template_string(contact_html)

@app.route("/privacy")
def privacy():
    privacy_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Privacy Policy - InsuranceGrokBot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; --neon-glow: rgba(0, 255, 136, 0.5); --text-secondary: #aaa; }
        body { background: var(--dark-bg); color: #fff; font-family: 'Montserrat', sans-serif; line-height: 1.8; padding: 80px 20px; min-height: 100vh; }
        .container { max-width: 900px; margin: auto; background: var(--card-bg); padding: 60px; border-radius: 20px; border: 1px solid #333; box-shadow: 0 10px 30px var(--neon-glow); }
        h1 { color: var(--accent); text-shadow: var(--neon-glow); text-align: center; margin-bottom: 40px; font-weight: 800; font-size: 3rem; }
        h2, h3 { color: var(--accent); margin: 50px 0 20px; font-weight: 700; }
        p, li { font-size: 1.1rem; color: #ddd; }
        ul { padding-left: 30px; margin: 20px 0; }
        strong { color: #fff; }
        .back { text-align: center; margin-top: 60px; }
        .back a { color: var(--accent); font-size: 1.4rem; text-decoration: none; }
        hr { border-color: #333; margin: 40px 0; }
        @media (max-width: 768px) {
            h1 { font-size: 2.5rem; }
            .container { padding: 40px 20px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Privacy Policy</h1>
        <p style="text-align: center; color: var(--text-secondary);">Last Updated: January 12, 2026</p>

        <p>InsuranceGrokBot (“we,” “us,” or “our”) operates insuranceregrokbot.click and the associated AI-powered SMS messaging service (the “Service”). We are committed to protecting your privacy. This Privacy Policy explains how we collect, use, disclose, and safeguard your information when you use our website, dashboard, demo chat, or any connected integrations.</p>

        <p>By using the Service, you agree to the practices described in this policy. If you do not agree, please do not use the Service.</p>

        <hr>

        <h2>1. Information We Collect</h2>

        <h3>a. Information You Provide</h3>
        <ul>
            <li>Email address and password (hashed) when you register or log in</li>
            <li>GoHighLevel configuration details (location ID, access/refresh tokens, calendar ID, CRM user ID, bot name, timezone, initial message) when you configure your bot</li>
            <li>Optional profile info (full name, phone, bio) if you choose to provide it</li>
            <li>Demo chat messages (stored temporarily under a demo-specific ID)</li>
        </ul>

        <h3>b. Information Automatically Collected</h3>
        <ul>
            <li>Device/browser data: IP address, browser type, OS, pages visited, time/date of access</li>
            <li>Usage data: Interactions with dashboard, demo chat, sent/received SMS (for logged-in users)</li>
            <li>Cookies & similar technologies for session management and analytics</li>
        </ul>

        <h3>c. Information from Third Parties</h3>
        <ul>
            <li><strong>GoHighLevel</strong>: Access/refresh tokens, location ID, CRM user ID, calendar ID, contact data (name, phone, address, DOB), and conversation history when you connect via OAuth or keys</li>
            <li><strong>Stripe</strong>: Payment data (customer ID, subscription status — we do not store card details)</li>
            <li><strong>Google Sheets</strong>: Your entered settings are stored in your linked sheet via authorized service account</li>
        </ul>

        <hr>

        <h2>2. How We Use Your Information</h2>
        <ul>
            <li>To provide and improve the Service (AI SMS conversations, appointment booking)</li>
            <li>To authenticate users and secure sessions</li>
            <li>To process payments and manage subscriptions via Stripe</li>
            <li>To sync and store your GoHighLevel configuration</li>
            <li>To generate AI responses using Grok (xAI) — conversation data is sent only during active sessions</li>
            <li>To analyze usage (aggregated/anonymized) and debug issues</li>
            <li>To communicate about your account or support</li>
            <li>For legal compliance, fraud prevention, and enforcing our Terms</li>
        </ul>

        <hr>

        <h2>3. Information Shared with Third Parties</h2>
        <p>We do <strong>not</strong> sell your personal information. We share only as needed:</p>
        <ul>
            <li><strong>GoHighLevel</strong>: Messages, contacts, and bookings are processed through their APIs using your tokens</li>
            <li><strong>xAI (Grok)</strong>: Conversation messages are sent to generate replies (real-time only, not used for training)</li>
            <li><strong>Stripe</strong>: Payment processing</li>
            <li><strong>Google</strong>: Your settings in your own Google Sheet</li>
            <li><strong>Service providers</strong>: Hosting (Railway), Redis/RQ, logging — with data processing agreements</li>
            <li><strong>Legal</strong>: If required by law, subpoena, or to protect rights/safety</li>
        </ul>

        <hr>

        <h2>4. AI & Data Processing Disclosure</h2>
        <ul>
            <li>We use xAI’s Grok models to generate SMS replies.</li>
            <li>AI may produce errors, hallucinations, or inaccurate information. Always verify important details independently.</li>
            <li>Conversation data sent to Grok is processed in real time. We do not store it long-term beyond your session history.</li>
            <li>We do not use your data to train Grok or any AI model.</li>
        </ul>

        <hr>

        <h2>5. Data Retention</h2>
        <ul>
            <li><strong>Demo chat</strong>: Deleted automatically after 30 minutes of inactivity</li>
            <li><strong>Registered users</strong>: Configuration and profile data retained until account deletion or subscription cancellation</li>
            <li><strong>Conversation history</strong>: Retained as needed for the Service (you control via GHL)</li>
            <li>Anonymized usage data may be kept indefinitely for analytics and improvement</li>
        </ul>

        <hr>

        <h2>6. Your Rights & Choices</h2>
        <ul>
            <li>Access, correct, or delete your data — contact support via dashboard</li>
            <li>Opt-out of marketing emails (if any) — use unsubscribe link</li>
            <li>Delete account — log in, contact support, or remove your row from your Google Sheet</li>
        </ul>

        <hr>

        <h2>7. Security</h2>
        <p>We use reasonable measures (encryption in transit, secure tokens, access controls) to protect your data. No system is 100% secure — we cannot guarantee absolute protection.</p>

        <hr>

        <h2>8. Children’s Privacy</h2>
        <p>Our Service is not directed to individuals under 18. We do not knowingly collect data from children.</p>

        <hr>

        <h2>9. International Transfers</h2>
        <p>Data may be processed in the United States or other countries. By using the Service, you consent to this transfer.</p>

        <hr>

        <h2>10. Changes to This Policy</h2>
        <p>We may update this Privacy Policy. Changes will be posted here with a new “Last Updated” date. Continued use after changes means acceptance.</p>

        <hr>

        <h2>11. Contact Us</h2>
        <p>For questions about this Privacy Policy or your data, use the support form in your dashboard or email support@insuranceregrokbot.click.</p>

        <div class="back">
            <a href="/">← Back to Home</a>
        </div>
    </div>
</body>
</html>
    """
    return render_template_string(privacy_html)

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
        cur = conn.cursor()

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
        safe_logs = make_json_serializable
        return flask_jsonify({"logs: safe_logs})"})
    except Exception as e:
        logger.error(f"Error in get_logs: {e}")
        logs.append({"error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

    return flask_jsonify({"logs": logs})

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
    """
    Seamless GHL OAuth Callback with Dynamic Sheet Mapping.
    1. Exchanges code for tokens.
    2. Saves to DB.
    3. Maps data to Google Sheet based on ACTUAL headers (no fixed order required).
    4. Redirects to Registration.
    """
    code = request.args.get("code")
    if not code:
        return "Error: No authorization code received.", 400

    token_url = "https://services.leadconnectorhq.com/oauth/token"
    payload = {
        "client_id": os.getenv("GHL_CLIENT_ID"),
        "client_secret": os.getenv("GHL_CLIENT_SECRET"),
        "grant_type": "authorization_code",
        "code": code,
        "user_type": "Location",
        "redirect_uri": f"{YOUR_DOMAIN}/oauth/callback" 
    }

    try:
        # 1. Exchange Code
        response = requests.post(token_url, data=payload)
        data = response.json()
        
        if 'access_token' not in data:
            logger.error(f"OAuth Exchange Failed: {data}")
            return f"Error: {data.get('error_description', 'Token exchange failed')}", 400

        access_token = data['access_token']
        refresh_token = data['refresh_token']
        expires_in = data['expires_in']
        location_id = data.get('locationId')

        # 2. Save to DB
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO subscribers (
                location_id, access_token, refresh_token, token_expires_at, 
                token_type, crm_api_key
            ) VALUES (
                %s, %s, %s, NOW() + interval '%s seconds', 'Bearer', %s
            )
            ON CONFLICT (location_id) DO UPDATE SET
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                token_expires_at = EXCLUDED.token_expires_at,
                updated_at = NOW();
        """, (location_id, access_token, refresh_token, expires_in, access_token))
        conn.commit()
        cur.close()
        conn.close()
        
        # 3. Dynamic Sheet Writing ( The Mapper )
        if worksheet:
            try:
                # Fetch all data to get headers
                all_values = worksheet.get_all_values()
                if not all_values:
                    # If sheet is empty, init headers
                    headers = ["email", "location_id", "access_token", "refresh_token", "bot_first_name", "timezone", "confirmation_code"]
                    worksheet.append_row(headers)
                    all_values = [headers]
                
                # Normalize headers to lowercase for matching
                headers = [h.strip().lower() for h in all_values[0]]
                
                # Prepare the data we WANT to write
                data_map = {
                    "location_id": location_id,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "bot_first_name": "Grok",
                    "timezone": "America/Chicago",
                    "confirmation_code": "OAUTH_AUTO", # Flag that this was auto-linked
                    "code_used": "1"
                }
                
                # Construct the row list based on the sheet's actual header order
                row_to_append = [""] * len(headers) # Start with empty strings
                
                for col_name, value in data_map.items():
                    # Find identifying part of header (e.g., 'access_token' in 'access_token')
                    # We look for exact match or partial match if you have messy headers
                    try:
                        # Try exact match first
                        if col_name in headers:
                            idx = headers.index(col_name)
                            row_to_append[idx] = value
                        # Handle 'crm_api_key' legacy mapping to 'access_token'
                        elif col_name == "access_token" and "crm_api_key" in headers:
                            idx = headers.index("crm_api_key")
                            row_to_append[idx] = value
                    except ValueError:
                        pass # Column not found, skip writing that field
                
                # Write the mapped row
                worksheet.append_row(row_to_append)
                
            except Exception as e:
                logger.error(f"Sheet append failed: {e}")

        # 4. Redirect to Register (Seamless)
        return redirect(url_for('register', location_id=location_id))

    except Exception as e:
        logger.error(f"OAuth Callback Error: {e}")
        return "Internal Server Error during installation", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)