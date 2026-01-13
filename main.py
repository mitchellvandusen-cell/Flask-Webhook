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
import httpx
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
    <title>InsuranceGrokBot — Reengage Cold Leads Like Never Before</title>
    <meta name="description" content="The most advanced AI SMS solution for life insurance agents. Re-engages cold leads, books appointments, powered by Grok.">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='%23000'/><text y='70' font-size='80' text-anchor='middle' x='50' fill='%2300ff88'>G</text></svg>" type="image/svg+xml">
    
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;700;800&family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/aos@2.3.4/dist/aos.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/aos@2.3.4/dist/aos.js"></script>

    <style>
        :root {
            --accent: #00ff88;
            --accent-hover: #ffffff;
            --dark-bg: #050505;
            --text-primary: #ffffff;
            --text-secondary: #a0a0a0;
            --glass-bg: rgba(255, 255, 255, 0.03);
            --glass-border: rgba(255, 255, 255, 0.08);
            --glow: 0 0 30px rgba(0, 255, 136, 0.15);
            --transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275);
        }

        * { margin:0; padding:0; box-sizing:border-box; }

        body {
            background-color: var(--dark-bg);
            background-image: 
                radial-gradient(circle at 15% 50%, rgba(0, 255, 136, 0.08), transparent 25%),
                radial-gradient(circle at 85% 30%, rgba(0, 100, 255, 0.05), transparent 25%);
            background-attachment: fixed;
            color: var(--text-primary);
            font-family: 'Inter', sans-serif;
            line-height: 1.6;
            overflow-x: hidden;
        }

        h1, h2, h3, h4, .navbar-brand { font-family: 'Outfit', sans-serif; }

        /* --- Navbar --- */
        .navbar {
            background: rgba(5, 5, 5, 0.8);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid var(--glass-border);
            padding: 1rem 0;
            transition: var(--transition);
        }
        .navbar-brand {
            font-weight: 800;
            font-size: 1.5rem;
            color: #fff !important;
            letter-spacing: -0.5px;
        }
        /* New helper for the Grok Green Text */
        .text-accent {
            color: var(--accent);
        }

        .nav-link {
            color: var(--text-secondary) !important;
            font-weight: 500;
            font-size: 0.95rem;
            transition: var(--transition);
        }
        .nav-link:hover { color: var(--accent) !important; }

        /* --- Buttons --- */
        .btn { border-radius: 50px; font-weight: 700; padding: 0.6rem 1.5rem; transition: var(--transition); }
        
        /* Nav specific button sizing to match widths */
        .nav-btn {
            min-width: 140px; /* Ensures both buttons are same width */
            text-align: center;
        }

        /* Force Primary to be Green */
        .btn-primary {
            background-color: var(--accent) !important;
            border: 2px solid var(--accent) !important;
            color: #000 !important;
        }
        .btn-primary:hover {
            background-color: #fff !important;
            border-color: #fff !important;
            color: #000 !important;
            box-shadow: 0 0 30px rgba(0, 255, 136, 0.6);
            transform: translateY(-3px);
        }

        .btn-outline-accent {
            border: 2px solid var(--accent);
            color: var(--accent);
            background: transparent;
        }
        .btn-outline-accent:hover {
            background: var(--accent);
            color: #000;
            box-shadow: 0 0 20px rgba(0, 255, 136, 0.4);
            transform: translateY(-3px);
        }

        /* --- Hero Section --- */
        .hero {
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
            padding-top: 80px;
            overflow: hidden;
        }
        .hero::after {
            content: '';
            position: absolute;
            width: 600px;
            height: 600px;
            background: radial-gradient(circle, rgba(0, 255, 136, 0.1) 0%, transparent 70%);
            top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            z-index: -1;
            filter: blur(40px);
            animation: pulse 5s infinite alternate;
        }
        @keyframes pulse { 0% { opacity: 0.5; transform: translate(-50%, -50%) scale(0.8); } 100% { opacity: 1; transform: translate(-50%, -50%) scale(1.1); } }

        .hero h1 {
            font-size: clamp(3rem, 6vw, 5.5rem);
            font-weight: 800;
            line-height: 1.1;
            margin-bottom: 1.5rem;
            background: linear-gradient(135deg, #fff 40%, var(--accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .hero .lead {
            font-size: 1.25rem;
            color: var(--text-secondary);
            max-width: 700px;
            margin: 0 auto 2.5rem;
        }

        /* --- Global Sections --- */
        .section { padding: 100px 0; position: relative; }
        .section-title {
            text-align: center;
            font-size: 3rem;
            font-weight: 700;
            margin-bottom: 60px;
            color: #fff;
        }
        .section-title span { color: var(--accent); }

        /* --- Glass Cards --- */
        .glass-card {
            background: var(--glass-bg);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 24px;
            padding: 40px 30px;
            transition: var(--transition);
            height: 100%;
            position: relative;
            overflow: hidden;
        }
        .glass-card:hover {
            transform: translateY(-10px);
            border-color: rgba(0, 255, 136, 0.3);
            box-shadow: 0 15px 40px -10px rgba(0, 0, 0, 0.5), 
                        inset 0 0 20px rgba(0, 255, 136, 0.05);
        }
        
        .feature-icon {
            font-size: 3rem;
            margin-bottom: 20px;
            background: linear-gradient(135deg, var(--accent), #fff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: inline-block;
        }
        .feature-card h4 { font-size: 1.4rem; font-weight: 700; margin-bottom: 15px; color: #fff; }
        .feature-card p { color: #999; font-size: 0.95rem; }

        /* --- Comparison Teaser --- */
        .glass-banner {
            background: linear-gradient(90deg, rgba(255,255,255,0.03) 0%, rgba(0,255,136,0.02) 100%);
            backdrop-filter: blur(20px);
            border: 1px solid var(--glass-border);
            border-radius: 30px;
            padding: 60px 50px;
            position: relative;
            overflow: hidden;
            transition: var(--transition);
        }
        .glass-banner:hover { border-color: rgba(0, 255, 136, 0.3); transform: translateY(-2px); }
        
        .vs-visual-container {
            display: flex; align-items: center; justify-content: center; gap: 20px; margin-bottom: 30px; opacity: 0.9;
        }
        .vs-circle {
            background: #222; color: #777; width: 40px; height: 40px; border-radius: 50%; 
            display: flex; align-items: center; justify-content: center; font-weight: 800; border: 1px solid #333;
        }
        .grok-icon i { font-size: 2.2rem; color: var(--accent); filter: drop-shadow(0 0 10px var(--accent)); }
        
        /* --- Pricing Card --- */
        .pricing-section { position: relative; overflow: hidden; }
        .pricing-card {
            max-width: 500px;
            margin: 0 auto;
            text-align: center;
            background: rgba(10, 10, 10, 0.6);
            border: 1px solid var(--glass-border);
            padding: 60px 40px;
        }
        .pricing-card:hover {
            border-color: var(--accent);
            box-shadow: 0 0 60px rgba(0, 255, 136, 0.15);
        }
        .price-tag {
            font-size: 4.5rem;
            font-weight: 800;
            color: #fff;
            line-height: 1;
        }
        .price-tag span { font-size: 1.5rem; color: var(--text-secondary); font-weight: 400; }
        .pricing-badge {
            background: rgba(0, 255, 136, 0.1);
            color: var(--accent);
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 1px;
            display: inline-block;
            margin-bottom: 20px;
        }
        .pricing-features {
            list-style: none;
            padding: 0;
            margin: 40px 0;
            text-align: left;
        }
        .pricing-features li {
            margin-bottom: 15px;
            color: #ccc;
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 1.1rem;
        }
        .pricing-features li i { color: var(--accent); }

        /* --- Footer --- */
        footer {
            border-top: 1px solid #111;
            padding: 60px 0;
            background: #020202;
            text-align: center;
            color: #555;
        }
        footer a { color: #777; text-decoration: none; margin: 0 10px; transition: 0.3s; }
        footer a:hover { color: var(--accent); }

        /* Mobile Adjustments */
        @media (max-width: 991px) {
            .hero h1 { font-size: 3rem; }
            .section-title { font-size: 2.2rem; }
            .glass-banner { padding: 40px 20px; text-align: center; }
            .feature-grid { grid-template-columns: 1fr; }
            .navbar-nav { margin: 20px 0; }
        }
    </style>
</head>
<body>

    <nav class="navbar navbar-expand-lg fixed-top">
        <div class="container">
            <a class="navbar-brand" href="/">Insurance<span class="text-accent">Grok</span>Bot</a>
            
            <button class="navbar-toggler navbar-dark" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav mx-auto align-items-center gap-4">
                    <li class="nav-item"><a class="nav-link" href="#features">Features</a></li>
                    <li class="nav-item"><a class="nav-link" href="/comparison">Comparison</a></li>
                    <li class="nav-item"><a class="nav-link" href="#pricing">Pricing</a></li>
                    <li class="nav-item"><a class="nav-link" href="/demo-chat">Demo</a></li>
                </ul>
                
                <div class="d-flex gap-3 mt-3 mt-lg-0">
                    <a href="/login" class="btn btn-outline-accent btn-sm nav-btn">Log In</a>
                    <a href="/register" class="btn btn-primary btn-sm nav-btn">Get Started</a>
                </div>
            </div>
        </div>
    </nav>

    <section class="hero">
        <div class="container text-center" style="position: relative; z-index: 2;">
            <div data-aos="fade-up">
                <span style="color:var(--accent); font-weight:700; letter-spacing:1px; text-transform:uppercase; font-size:0.9rem;">Powered by xAI's Grok</span>
                <h1>Reopen Cold Leads.<br>Book Appointments.</h1>
                <p class="lead">
                    Stop chasing dead leads manually. Let the AI handle the objections, re-spark the interest, and put meetings on your calendar.
                </p>
                <div class="d-flex justify-content-center gap-3 flex-wrap">
                    <a href="/checkout" class="btn btn-primary btn-lg">Subscribe Now ($100/mo)</a>
                    <a href="/demo-chat" class="btn btn-outline-accent btn-lg">Try the Demo</a>
                </div>
            </div>
        </div>
    </section>

    <section id="features" class="section">
        <div class="container">
            <h2 class="section-title" data-aos="fade-up">Built for <span>Results</span></h2>
            <div class="row g-4">
                <div class="col-md-4" data-aos="fade-up" data-aos-delay="100">
                    <div class="glass-card feature-card">
                        <div class="feature-icon"><i class="fa-solid fa-brain"></i></div>
                        <h4>Human-Like Memory</h4>
                        <p>It remembers everything about your client. No awkward repeated questions. It builds trust instantly.</p>
                    </div>
                </div>
                <div class="col-md-4" data-aos="fade-up" data-aos-delay="200">
                    <div class="feature-card glass-card">
                        <div class="feature-icon"><i class="fa-solid fa-bolt"></i></div>
                        <h4>5 Elite Sales Frameworks</h4>
                        <p>Blends NEPQ, Gap Selling, and Chris Voss tactics to overcome objections and close the gap.</p>
                    </div>
                </div>
                <div class="col-md-4" data-aos="fade-up" data-aos-delay="300">
                    <div class="feature-card glass-card">
                        <div class="feature-icon"><i class="fa-solid fa-calendar-check"></i></div>
                        <h4>Auto-Booking</h4>
                        <p>It doesn't just chat; it converts. The bot integrates with your calendar to book qualified appointments.</p>
                    </div>
                </div>
                <div class="col-md-4" data-aos="fade-up" data-aos-delay="400">
                    <div class="feature-card glass-card">
                        <div class="feature-icon"><i class="fa-solid fa-shield-halved"></i></div>
                        <h4>Underwriting Logic</h4>
                        <p>Smart enough to know carrier rules. It asks the right health questions before you ever get on the phone.</p>
                    </div>
                </div>
                <div class="col-md-4" data-aos="fade-up" data-aos-delay="500">
                    <div class="feature-card glass-card">
                        <div class="feature-icon"><i class="fa-solid fa-infinity"></i></div>
                        <h4>Unlimited Conversations</h4>
                        <p>Scale without limits. Whether you have 100 leads or 10,000, the bot handles them all simultaneously.</p>
                    </div>
                </div>
                <div class="col-md-4" data-aos="fade-up" data-aos-delay="600">
                    <div class="feature-card glass-card">
                        <div class="feature-icon"><i class="fa-solid fa-building-user"></i></div>
                        <h4>Agency Ready</h4>
                        <p>Multi-tenant support allows you to manage multiple agents or sub-accounts from one master dashboard.</p>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <section class="section">
        <div class="container">
            <div class="glass-banner" data-aos="zoom-in">
                <div class="row align-items-center">
                    <div class="col-lg-7 mb-4 mb-lg-0">
                        <h2 style="font-size: 2.5rem; font-weight:700; color:#fff;">Curious how we stack up?</h2>
                        <p style="color:#aaa; font-size:1.1rem;">
                            Don't settle for generic bots. See exactly why <span style="color:var(--accent);">InsuranceGrokBot</span> outperforms ChatGPT and standard automation.
                        </p>
                    </div>
                    <div class="col-lg-5 text-center position-relative">
                        <div class="vs-visual-container">
                            <div style="font-size:1.5rem; color:#555;"><i class="fa-solid fa-robot"></i></div>
                            <div class="vs-circle">VS</div>
                            <div class="grok-icon"><i class="fa-solid fa-bolt"></i></div>
                        </div>
                        <a href="/comparison" class="btn btn-primary">
                            See the Breakdown <i class="fa-solid fa-arrow-right ms-2"></i>
                        </a>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <section id="pricing" class="section pricing-section">
        <div class="container">
            <h2 class="section-title" data-aos="fade-up">Simple <span>Pricing</span></h2>
            <div class="pricing-card glass-card" data-aos="flip-up">
                <span class="pricing-badge">Early Adopter Rate</span>
                <div class="price-tag">$100<span>/mo</span></div>
                <p style="color:#888; margin-top:10px;">Cancel anytime. No contracts.</p>
                
                <ul class="pricing-features">
                    <li><i class="fa-solid fa-check"></i> Unlimited Lead Re-engagement</li>
                    <li><i class="fa-solid fa-check"></i> Full Narrative Memory</li>
                    <li><i class="fa-solid fa-check"></i> All 5 Sales Methodologies</li>
                    <li><i class="fa-solid fa-check"></i> Calendar Auto-Booking</li>
                    <li><i class="fa-solid fa-check"></i> Multi-Tenant Dashboard</li>
                </ul>

                <a href="/checkout" class="btn btn-primary w-100 py-3">Start Your Subscription</a>
            </div>
        </div>
    </section>

    <footer>
        <div class="container">
            <p style="color:#fff; font-weight:700; font-size:1.2rem; margin-bottom:10px;">Insurance<span class="text-accent">Grok</span>Bot</p>
            <p class="mb-4">The future of insurance sales automation.</p>
            <div>
                <a href="/terms">Terms</a>
                <a href="/privacy">Privacy</a>
                <a href="/disclaimers">Disclaimers</a>
            </div>
            <p style="font-size:0.8rem; margin-top:40px; opacity:0.5;">&copy; 2026 InsuranceGrokBot. All rights reserved.</p>
        </div>
    </footer>

    <script>
        AOS.init({
            duration: 1000,
            once: true,
            offset: 50
        });
    </script>
</body>
</html>
    """
    return render_template_string(home_html)

@app.route("/comparison")
def comparison():
    comparison_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>InsuranceGrokBot vs. The Rest</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;700;800&family=Inter:wght@400;600&display=swap" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/aos@2.3.4/dist/aos.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <style>
        :root {
            --primary: #00ff88;
            --primary-glow: rgba(0, 255, 136, 0.4);
            --dark-bg: #050505;
            --card-glass: rgba(255, 255, 255, 0.03);
            --card-border: rgba(255, 255, 255, 0.08);
            --text-main: #ffffff;
            --text-muted: #8892b0;
        }

        body {
            background-color: var(--dark-bg);
            background-image: 
                radial-gradient(circle at 50% 10%, rgba(0, 255, 136, 0.05), transparent 40%),
                radial-gradient(circle at 85% 80%, rgba(66, 133, 244, 0.05), transparent 40%);
            font-family: 'Inter', sans-serif;
            color: var(--text-main);
            overflow-x: hidden;
            min-height: 100vh;
        }

        /* --- Typography --- */
        h1, h2, h3, h4 { font-family: 'Outfit', sans-serif; }
        
        .main-header {
            text-align: center;
            margin: 60px 0 40px;
        }

        .main-title {
            font-size: 3.5rem;
            font-weight: 800;
            background: linear-gradient(135deg, #fff 30%, var(--primary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }
        
        .subtitle {
            font-size: 1.2rem;
            color: var(--text-muted);
            max-width: 700px;
            margin: 0 auto;
        }

        /* --- THE HERO CARD (Top Position) --- */
        .hero-card {
            background: linear-gradient(180deg, rgba(20, 20, 20, 0.8) 0%, rgba(10, 10, 10, 0.95) 100%);
            border: 1px solid var(--primary);
            box-shadow: 0 0 50px rgba(0, 255, 136, 0.08);
            border-radius: 24px;
            padding: 3rem;
            position: relative;
            margin-bottom: 60px; /* Space between Hero and Competitors */
            overflow: hidden;
        }

        /* Top shimmer line */
        .hero-card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0; height: 1px;
            background: linear-gradient(90deg, transparent, var(--primary), transparent);
            animation: shimmer 2.5s infinite;
        }

        @keyframes shimmer {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(100%); }
        }

        .hero-badge {
            display: inline-block;
            background: rgba(0, 255, 136, 0.1);
            color: var(--primary);
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 0.8rem;
            font-weight: 700;
            margin-bottom: 1rem;
            text-transform: uppercase;
            letter-spacing: 1.2px;
            border: 1px solid rgba(0, 255, 136, 0.2);
        }

        .hero-title {
            font-size: 3rem;
            font-weight: 700;
            color: #fff;
            margin-bottom: 0.5rem;
        }

        /* --- New Grid Layout for Hero Features --- */
        .hero-features-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr); /* 2 Columns strictly */
            gap: 30px 40px; /* Row gap 30px, Col gap 40px */
            margin-top: 40px;
        }

        .hero-feature-item {
            display: flex;
            align-items: flex-start; /* Align to top so icon stays up if text wraps */
            gap: 15px;
            padding-bottom: 15px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }

        .hero-feature-item:last-child, 
        .hero-feature-item:nth-last-child(2) {
            border-bottom: none; /* Remove border for last row */
        }

        .icon-box {
            background: var(--primary);
            color: #000;
            min-width: 28px;
            height: 28px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.9rem;
            margin-top: 2px; /* Visual tweak to align with text cap height */
            box-shadow: 0 0 10px rgba(0, 255, 136, 0.4);
        }

        .text-content {
            display: flex;
            flex-direction: column; /* Stacks Title on top of Desc */
        }

        .feature-title {
            font-weight: 700;
            font-size: 1.2rem;
            color: #fff;
            margin-bottom: 4px;
        }

        .feature-desc {
            font-size: 0.95rem;
            color: #aaa;
            line-height: 1.4;
        }

        /* --- Competitor Grid (Bottom) --- */
        .competitor-section-title {
            text-align: center;
            margin-bottom: 30px;
            font-size: 1.5rem;
            color: #fff;
            opacity: 0.8;
        }

        .competitor-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
        }

        .glass-card {
            background: var(--card-glass);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 2rem;
            transition: all 0.3s ease;
            position: relative;
        }

        .glass-card:hover {
            transform: translateY(-5px);
            background: rgba(255, 255, 255, 0.06);
            border-color: rgba(255, 255, 255, 0.15);
        }

        .competitor-card .card-logo {
            height: 40px;
            margin-bottom: 20px;
            filter: grayscale(100%) opacity(0.6);
            transition: all 0.3s;
        }
        .competitor-card:hover .card-logo {
            filter: grayscale(0%) opacity(1);
        }

        .competitor-list {
            list-style: none;
            padding: 0;
            margin-top: 15px;
        }
        
        .competitor-list li {
            font-size: 0.9rem;
            color: #888;
            margin-bottom: 10px;
            display: flex;
            gap: 10px;
        }
        
        .icon-cross { color: #555; }
        .icon-check-mute { color: #444; }

        /* --- Footer --- */
        .cta-container { margin: 80px 0; text-align: center; }
        .glow-btn {
            background: var(--primary);
            color: #000;
            padding: 16px 45px;
            border-radius: 50px;
            font-weight: 700;
            font-size: 1.1rem;
            text-decoration: none;
            transition: var(--transition);
            box-shadow: 0 0 20px rgba(0, 255, 136, 0.3);
            display: inline-block;
        }
        .glow-btn:hover {
            transform: scale(1.05);
            box-shadow: 0 0 40px rgba(0, 255, 136, 0.6);
            color: #000;
        }

        /* Responsive */
        @media (max-width: 992px) {
            .hero-features-grid { grid-template-columns: 1fr; gap: 20px; }
            .hero-card { padding: 2rem; }
            .hero-title { font-size: 2.2rem; }
        }
    </style>
</head>
<body>

    <div class="container" style="max-width: 1200px;">
        
        <div class="main-header" data-aos="fade-down">
            <h1 class="main-title">The Choice is Clear</h1>
            <p class="subtitle">See why agencies are switching to specialized AI.</p>
        </div>

        <div class="hero-card" data-aos="zoom-in">
            <span class="hero-badge">Recommended Choice</span>
            <h2 class="hero-title">InsuranceGrokBot</h2>
            <p style="color: #bbb; font-size: 1.15rem; max-width: 600px;">
                The only AI built specifically to re-engage dead leads and book appointments automatically.
            </p>
            
            <div class="hero-features-grid">
                
                <div class="hero-feature-item">
                    <div class="icon-box"><i class="fa-solid fa-check"></i></div>
                    <div class="text-content">
                        <span class="feature-title">Deep Insurance Knowledge</span>
                        <span class="feature-desc">Understands underwriting, policy types, and specific insurance terminology out of the box.</span>
                    </div>
                </div>

                <div class="hero-feature-item">
                    <div class="icon-box"><i class="fa-solid fa-check"></i></div>
                    <div class="text-content">
                        <span class="feature-title">5 Sales Methodologies</span>
                        <span class="feature-desc">Blends NEPQ, Gap Selling, Voss, Ziglar, and Straight Line for maximum persuasion.</span>
                    </div>
                </div>

                <div class="hero-feature-item">
                    <div class="icon-box"><i class="fa-solid fa-check"></i></div>
                    <div class="text-content">
                        <span class="feature-title">Persistent Memory</span>
                        <span class="feature-desc">Remembers client details forever. It never asks for the same information twice.</span>
                    </div>
                </div>

                <div class="hero-feature-item">
                    <div class="icon-box"><i class="fa-solid fa-check"></i></div>
                    <div class="text-content">
                        <span class="feature-title">Auto-Booking Engine</span>
                        <span class="feature-desc">Integrates directly with calendars to book appointments without human intervention.</span>
                    </div>
                </div>

                <div class="hero-feature-item">
                    <div class="icon-box"><i class="fa-solid fa-check"></i></div>
                    <div class="text-content">
                        <span class="feature-title">Agency Multi-Tenancy</span>
                        <span class="feature-desc">Scale effortlessly across multiple teams and sub-accounts from one dashboard.</span>
                    </div>
                </div>

                <div class="hero-feature-item">
                    <div class="icon-box"><i class="fa-solid fa-check"></i></div>
                    <div class="text-content">
                        <span class="feature-title">Emotional Intelligence</span>
                        <span class="feature-desc">Detects hesitation and handles objections naturally rather than using robotic scripts.</span>
                    </div>
                </div>

            </div>
        </div>

        <h3 class="competitor-section-title" data-aos="fade-up">Compare with Others</h3>
        
        <div class="competitor-grid">
            <div class="glass-card competitor-card" data-aos="fade-up" data-aos-delay="100">
                <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/0/04/ChatGPT_logo.svg/1200px-ChatGPT_logo.svg.png" alt="ChatGPT" class="card-logo">
                <h4>ChatGPT</h4>
                <ul class="competitor-list">
                    <li><i class="fa-solid fa-check icon-check-mute"></i> Great conversation</li>
                    <li><i class="fa-solid fa-xmark icon-cross"></i> No sales frameworks</li>
                    <li><i class="fa-solid fa-xmark icon-cross"></i> No underwriting logic</li>
                </ul>
            </div>

            <div class="glass-card competitor-card" data-aos="fade-up" data-aos-delay="200">
                <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/3/3e/Logo_Grok_AI_%28xAI%29_2025.png/1200px-Logo_Grok_AI_%28xAI%29_2025.png" alt="Grok" class="card-logo">
                <h4>Grok</h4>
                <ul class="competitor-list">
                    <li><i class="fa-solid fa-check icon-check-mute"></i> High intelligence</li>
                    <li><i class="fa-solid fa-xmark icon-cross"></i> Generic knowledge</li>
                    <li><i class="fa-solid fa-xmark icon-cross"></i> No persistent memory</li>
                </ul>
            </div>

            <div class="glass-card competitor-card" data-aos="fade-up" data-aos-delay="300">
                <img src="https://1000logos.net/wp-content/uploads/2024/02/Gemini-Logo.png" alt="Gemini" class="card-logo">
                <h4>Gemini</h4>
                <ul class="competitor-list">
                    <li><i class="fa-solid fa-check icon-check-mute"></i> Data analysis</li>
                    <li><i class="fa-solid fa-xmark icon-cross"></i> No insurance workflows</li>
                    <li><i class="fa-solid fa-xmark icon-cross"></i> Session based only</li>
                </ul>
            </div>

            <div class="glass-card competitor-card" data-aos="fade-up" data-aos-delay="400">
                <div style="font-size: 2rem; color: #555; margin-bottom: 20px;"><i class="fa-solid fa-robot"></i></div>
                <h4>Basic Bots</h4>
                <ul class="competitor-list">
                    <li><i class="fa-solid fa-check icon-check-mute"></i> Cheap</li>
                    <li><i class="fa-solid fa-xmark icon-cross"></i> Zero reasoning</li>
                    <li><i class="fa-solid fa-xmark icon-cross"></i> Robotic responses</li>
                </ul>
            </div>
        </div>

        <div class="cta-container" data-aos="fade-up">
            <a href="/" class="glow-btn">Back to Home</a>
        </div>
        
        <p style="text-align: center; color: #444; font-size: 0.8rem; margin-bottom: 40px;">
            * Comparison reflects features as of Jan 2026.
        </p>

    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/aos@2.3.4/dist/aos.js"></script>
    <script>
        AOS.init({ duration: 800, once: true });
    </script>
</body>
</html>
    """
    return comparison_html

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

    demo_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>Live Demo | InsuranceGrokBot</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <style>
        :root {{
            --accent: #00ff88;
            --bg-dark: #050505;
            --phone-bezel: #1c1c1e;
            --phone-screen: #000000;
            --bubble-user: #00ff88;
            --bubble-bot: #262626;
            --text-user: #000;
            --text-bot: #fff;
            --terminal-bg: #0a0a0a;
            
            /* DYNAMIC SAFE AREAS */
            --safe-top: env(safe-area-inset-top, 20px);
            --safe-bottom: env(safe-area-inset-bottom, 20px);
        }}

        * {{ box-sizing: border-box; }}

        body {{
            background-color: var(--bg-dark);
            background-image: 
                linear-gradient(rgba(255, 255, 255, 0.03) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255, 255, 255, 0.03) 1px, transparent 1px);
            background-size: 40px 40px;
            color: #fff;
            font-family: 'Outfit', sans-serif;
            
            /* 2. THE MASTER LOCKS */
            height: 100dvh;        /* Dynamic height fits screen perfectly */
            width: 100vw;          /* Lock width */
            overflow: hidden;      /* KILL all body scrolling */
            overscroll-behavior: none; /* KILL the "rubber band" bounce on iOS */
            
            margin: 0;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .container {{
            display: flex;
            gap: 40px;
            width: 100%;
            max-width: 1400px;
            height: 100%;
            padding: 20px;
            align-items: center;
            justify-content: center;
        }}

        /* --- PHONE CHASSIS (Desktop) --- */
        .phone-wrapper {{
            position: relative;
            width: 100%;
            max-width: 400px;
            height: 95%;
            max-height: 850px;
            background: var(--phone-bezel);
            border-radius: 55px;
            box-shadow: 
                0 0 0 4px #333,
                0 0 0 7px #111,
                0 30px 60px rgba(0,0,0,0.6),
                inset 0 0 20px rgba(0,0,0,0.8);
            padding: 15px; 
            flex-shrink: 0;
            z-index: 10;
        }}

        .phone-screen {{
            background: var(--phone-screen);
            width: 100%;
            height: 100%;
            border-radius: 42px; 
            position: relative;
            overflow: hidden; 
            display: flex;
            flex-direction: column;
            border: 2px solid #222;
        }}

        /* --- UI ELEMENTS --- */
        .notch-area {{
            position: absolute;
            top: 0;
            left: 50%;
            transform: translateX(-50%);
            width: 120px;
            height: 35px;
            background: #000;
            border-bottom-left-radius: 20px;
            border-bottom-right-radius: 20px;
            z-index: 100;
        }}
        
        .status-bar {{
            height: auto;
            min-height: 50px;
            width: 100%;
            display: flex;
            justify-content: space-between;
            align-items: center; /* Center icons vertically */
            padding: 15px 25px;
            font-size: 14px;
            font-weight: 600;
            z-index: 90;
            flex-shrink: 0;
            /* On Desktop, this sits under the fake notch. On mobile, we adjust. */
        }}

        .chat-area {{
            flex: 1; 
            width: 100%;
            min-height: 0; /* Prevents overflow blowout */
            overflow-y: auto; 
            padding: 10px 20px;
            display: flex;
            flex-direction: column;
            gap: 15px;
            scroll-behavior: smooth;
        }}
        .chat-area::-webkit-scrollbar {{ display: none; }}

        .input-area {{
            width: 100%;
            /* 3. INPUT PROTECTION: Ensure it sits above the home bar */
            padding: 15px 20px calc(15px + var(--safe-bottom)) 20px;
            background: rgba(20, 20, 20, 0.95);
            backdrop-filter: blur(10px);
            display: flex;
            gap: 10px;
            align-items: flex-end;
            border-top: 1px solid #222;
            flex-shrink: 0;
            z-index: 20;
        }}

        /* --- BUBBLES --- */
        .msg {{
            max-width: 85%;
            padding: 12px 18px;
            border-radius: 20px;
            font-size: 0.95rem;
            line-height: 1.4;
            position: relative;
            animation: popIn 0.3s ease-out;
            word-wrap: break-word;
        }}
        @keyframes popIn {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}

        .msg.bot {{ align-self: flex-start; background: var(--bubble-bot); color: var(--text-bot); border-bottom-left-radius: 4px; }}
        .msg.user {{ align-self: flex-end; background: var(--bubble-user); color: var(--text-user); border-bottom-right-radius: 4px; font-weight: 600; }}

        .input-field {{
            flex: 1;
            background: #2a2a2a;
            border: 1px solid #333;
            border-radius: 25px;
            padding: 12px 15px;
            color: #fff;
            font-family: 'Outfit', sans-serif;
            resize: none;
            max-height: 100px;
            min-height: 44px;
            outline: none;
        }}

        .send-btn {{
            background: var(--accent); color: #000; border: none; width: 44px; height: 44px;
            border-radius: 50%; display: flex; align-items: center; justify-content: center;
            cursor: pointer; flex-shrink: 0; box-shadow: 0 0 15px rgba(0,255,136,0.2);
        }}

        /* --- TERMINAL (Desktop Only) --- */
        .terminal-col {{
            flex: 1; height: 95%; max-height: 850px;
            background: var(--terminal-bg);
            border: 1px solid #333; border-radius: 20px;
            display: flex; flex-direction: column; overflow: hidden;
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            position: relative;
        }}
        .log-content {{ flex: 1; padding: 20px; overflow-y: auto; font-family: 'JetBrains Mono', monospace; font-size: 13px; color: #ccc; }}
        .log-entry {{ margin-bottom: 15px; padding-left:10px; border-left: 2px solid #333; }}

        /* --- 4. MOBILE SPECIFIC LOCKS --- */
        @media (max-width: 900px) {{
            .container {{ padding: 0; height: 100dvh; }}
            .terminal-col {{ display: none; }}
            
            .phone-wrapper {{ 
                width: 100%; max-width: none; height: 100%; max-height: none;
                border-radius: 0; border: none; box-shadow: none; padding: 0;
            }}
            .phone-screen {{ border-radius: 0; border: none; }}
            
            /* HIDE FAKE NOTCH ON MOBILE (Use real phone notch) */
            .notch-area {{ display: none; }} 
            
            /* PUSH STATUS BAR DOWN (To clear real notch) */
            .status-bar {{
                padding-top: calc(15px + var(--safe-top));
            }}
        }}

        /* Typing Dots */
        .typing {{ display: flex; gap: 4px; padding: 15px; align-self: flex-start; background: var(--bubble-bot); border-radius: 20px; border-bottom-left-radius: 4px; }}
        .dot {{ width: 6px; height: 6px; background: #888; border-radius: 50%; animation: bounce 1.4s infinite; }}
        .dot:nth-child(2) {{ animation-delay: 0.2s; }}
        .dot:nth-child(3) {{ animation-delay: 0.4s; }}
        @keyframes bounce {{ 0%, 100% {{ transform: translateY(0); }} 50% {{ transform: translateY(-5px); }} }}
    </style>
</head>
<body>

<div class="container">
    
    <div class="phone-wrapper">
        <div class="phone-screen">
            <div class="notch-area"></div>
            
            <div class="status-bar">
                <span id="clock">10:00</span>
                <div style="display:flex; gap:8px; align-items:center;">
                    <i class="fas fa-signal"></i>
                    <i class="fas fa-wifi"></i>
                    <i class="fas fa-battery-full"></i>
                </div>
            </div>

            <div class="chat-area" id="chat"></div>

            <div class="input-area">
                <textarea id="msgInput" class="input-field" placeholder="Message..." rows="1"></textarea>
                <button class="send-btn" onclick="sendMessage()"><i class="fas fa-paper-plane"></i></button>
            </div>
        </div>
    </div>

    <div class="terminal-col">
        <div style="padding:15px; background:#111; border-bottom:1px solid #333; display:flex; justify-content:space-between; align-items:center;">
            <span style="color:#fff; font-weight:700;"><span style="color:var(--accent); margin-right:8px;">●</span>LIVE BRAIN ACTIVITY</span>
            <span style="font-family:'JetBrains Mono'; font-size:11px; color:#555;">ID: {clean_id[:6]}</span>
        </div>
        <div class="log-content" id="logWindow">
            <div style="color:#555;">> Initializing neural connection...</div>
        </div>
        <div style="padding:15px; border-top:1px solid #333; display:flex; gap:10px;">
            <button onclick="resetSession()" style="flex:1; padding:10px; background:#222; border:1px solid #444; color:#fff; border-radius:8px; cursor:pointer;">Reset</button>
            <a href="/download-transcript?contact_id={demo_contact_id}" target="_blank" style="flex:1; padding:10px; background:#222; border:1px solid #444; color:#fff; border-radius:8px; text-align:center; text-decoration:none; font-size:13px;">Download Logs</a>
        </div>
    </div>

</div>

<audio id="snd-send" src="https://assets.mixkit.co/active_storage/sfx/2354/2354-preview.mp3"></audio>
<audio id="snd-receive" src="https://assets.mixkit.co/active_storage/sfx/2358/2358-preview.mp3"></audio>

<script>
    const CONTACT_ID = '{demo_contact_id}';
    let lastMsgCount = 0;
    
    // Helper to safely extract text from JSON strings
    function cleanContent(raw) {{
        if (!raw) return "";
        try {{           
            if (raw.trim().startsWith(String.fromCharCode(123))) {{
                const parsed = JSON.parse(raw);
                return parsed.body || parsed.message || parsed.text || raw;
            }}
        }} catch (e) {{}}
        return raw;
    }}

    function init() {{
        setInterval(() => {{
            const now = new Date();
            document.getElementById('clock').innerText = now.toLocaleTimeString([], {{hour:'2-digit', minute:'2-digit'}});
        }}, 1000);

        const input = document.getElementById('msgInput');
        input.addEventListener('input', function() {{
            this.style.height = 'auto';
            this.style.height = (this.scrollHeight) + 'px';
        }});
        input.addEventListener('keypress', function(e) {{
            if(e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); sendMessage(); }}
        }});

        syncData();
        setInterval(syncData, 2000);
    }}

    async function syncData() {{
        try {{
            const res = await fetch(`/get-logs?contact_id=${{CONTACT_ID}}`);
            const data = await res.json();
            const messages = data.logs.filter(l => l.type.includes('Message'));

            if (messages.length > lastMsgCount) {{
                // Only play sound if it's NOT the very first load
                if (lastMsgCount > 0) {{
                    const lastMsg = messages[messages.length - 1];
                    if (lastMsg.type.toLowerCase().includes('bot') || lastMsg.type.toLowerCase().includes('assistant')) {{
                         document.getElementById('snd-receive').play().catch(e=>{{}});
                    }}
                }}

                const newSlice = messages.slice(lastMsgCount);
                newSlice.forEach(msg => {{
                    const text = cleanContent(msg.content);
                    const isBot = msg.type.toLowerCase().includes('bot') || msg.type.toLowerCase().includes('assistant');
                    addBubble(text, isBot);
                }});

                lastMsgCount = messages.length;
                const typing = document.getElementById('typing-indicator');
                if(typing) typing.remove();
            }}

            if (data.logs) {{
                const logs = document.getElementById('logWindow');
                logs.innerHTML = '';
                data.logs.forEach(l => {{
                    const time = l.timestamp.split('T')[1].split('.')[0];
                    logs.insertAdjacentHTML('beforeend', `
                        <div class="log-entry">
                            <span style="color:#666">[${{time}}]</span> 
                            <span style="color:var(--accent); font-weight:700;">${{l.type}}</span><br>
                            ${{cleanContent(l.content)}}
                        </div>`);
                }});
                logs.scrollTop = logs.scrollHeight;
            }}

        }} catch (e) {{ console.error(e); }}
    }}

    function addBubble(text, isBot) {{
        const chat = document.getElementById('chat');
        const div = document.createElement('div');
        div.className = `msg ${{isBot ? 'bot' : 'user'}}`;
        div.innerText = text;
        chat.appendChild(div);
        chat.scrollTop = chat.scrollHeight;
    }}

    function showTyping() {{
        if(document.getElementById('typing-indicator')) return;
        const chat = document.getElementById('chat');
        const div = document.createElement('div');
        div.id = 'typing-indicator';
        div.className = 'typing';
        div.innerHTML = '<div class="dot"></div><div class="dot"></div><div class="dot"></div>';
        chat.appendChild(div);
        chat.scrollTop = chat.scrollHeight;
    }}

    async function sendMessage() {{
        const input = document.getElementById('msgInput');
        const txt = input.value.trim();
        if(!txt) return;

        addBubble(txt, false);
        input.value = '';
        input.style.height = 'auto';
        document.getElementById('snd-send').play().catch(e=>{{}});
        showTyping();

        try {{
            await fetch('/webhook', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    location_id: 'DEMO_LOC',
                    contact_id: CONTACT_ID,
                    first_name: 'Demo User',
                    message: {{ body: txt }}
                }})
            }});
            syncData();
        }} catch(e) {{ console.error(e); }}
    }}

    function resetSession() {{
        window.location.href = '/demo-chat?session_id=' + crypto.randomUUID();
    }}

    init();
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
        safe_logs = make_json_serializable(logs)
        return flask_jsonify({"logs": safe_logs})
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
                "trial_period_days": 7,
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