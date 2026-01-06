from flask import Flask, request, jsonify, render_template_string
import os
import io
import logging
import requests  
import time      
import json
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
import datetime
from datetime import date, datetime as dt
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# === REPOSITORY IMPORTS ===
from conversation_engine import (
    ConversationState, detect_stage, 
    extract_facts_from_message, detect_dismissive
)
from ghl_message import send_sms_via_ghl
from unified_brain import get_unified_brain, get_decision_prompt
from prompt import build_system_prompt
from ghl_calendar import consolidated_calendar_op
from outcome_learning import classify_vibe, get_learning_context, init_tables
from memory import (
    save_message, get_contact_messages, 
    get_topics_already_discussed, get_recent_agent_messages,
    format_nlp_for_prompt
)
from insurance_companies import (
    find_company_in_message, normalize_company_name, get_company_context
)
from underwriting import get_underwriting_context, UNDERWRITING_DATA
from knowledge_base import (
    get_relevant_knowledge, identify_triggers, format_knowledge_for_prompt
)
from db import get_db_connection, init_nlp_tables, get_subscriber_info
from age import calculate_age_from_dob
from sync_subscribers import sync_subscribers

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DATABASE & SYNC INITIALIZATION ---

def init_db():
    """Initializes the subscriber table and runs the first sync."""
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        logging.error("DATABASE_URL not found!")
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # Create the table if it's a brand new database
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                ghl_location_id TEXT PRIMARY KEY,
                ghl_calendar_id TEXT,
                ghl_api_key TEXT,
                ghl_user_id TEXT,
                bot_first_name TEXT,
                timezone TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        
        # Trigger the sync from Google Sheets immediately on startup
        logging.info("Running initial subscriber sync...")
        sync_subscribers() 
    except Exception as e:
        logging.error(f"Initialization failed: {e}")

# Call the initialization
init_db()

# === API CLIENTS ===
XAI_API_KEY = os.getenv("XAI_API_KEY")
client = OpenAI(base_url="https://api.x.ai/v1", api_key=XAI_API_KEY) if XAI_API_KEY else None

# === DATABASE INITIALIZATION ===
try:
    init_nlp_tables()
    init_tables()
    logger.info("Database tables initialized successfully.")
except Exception as e:
    logger.error(f"Database init error: {e}")

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"status": "error", "error": "No JSON payload"}), 400

    # 1. Identity Lookup (The Multi-Tenant Core)
    location_id = payload.get("locationId")
    if not location_id:
        return jsonify({"status": "error", "message": "Missing locationId"}), 400
    # BYPASS FOR WEBSITE DEMO
    if location_id == 'DEMO_ACCOUNT_SALES_ONLY':
        subscriber = {
            'bot_first_name': 'Grok',
            'ghl_api_key': 'DEMO',
            'timezone': 'America/Chicago',
            'custom_instructions': "DEMO MODE: Focus 100% on selling life insurance. Use NEPQ to uncover gaps. Do not book appointments."
        }
    else:
        subscriber = get_subscriber_info(location_id)

    if not subscriber or not subscriber.get('bot_first_name'):
        logger.error(f"CRITICAL: Identity not configured for location {location_id}")
        return jsonify({"status": "error", "message": "Identity not configured"}), 404

    bot_first_name = subscriber.get('bot_first_name')
    ghl_api_key = subscriber.get('ghl_api_key')
    timezone = subscriber.get('timezone', 'America/Chicago')

    # 2. Extract Lead Data
    data = {k.lower(): v for k, v in payload.items()}
    contact_id = data.get("contact_id") or data.get("contactid") or data.get("contact", {}).get("id")
    
    if not contact_id:
        return jsonify({"status": "error", "error": "Missing contact_id"}), 400
    
    first_name = data.get("first_name", "there").capitalize()
    raw_message = data.get("message", {})
    message = raw_message.get("body", "").strip() if isinstance(raw_message, dict) else str(raw_message).strip()

    # 3. Idempotency (Duplicate Webhook Check)
    message_id = data.get("message_id") or data.get("id")
    if message_id:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM processed_webhooks WHERE webhook_id = %s", (message_id,))
                if cur.fetchone():
                    conn.close()
                    return jsonify({"status": "success", "message": "Already processed"}), 200
                cur.execute("INSERT INTO processed_webhooks (webhook_id) VALUES (%s)", (message_id,))
                conn.commit()
            except Exception as e:
                logger.error(f"Idempotency check failed: {e}")
            finally:
                conn.close()

    # 4. Context Gathering
    date_of_birth = data.get("contact", {}).get("date_of_birth", "")
    age = calculate_age_from_dob(date_of_birth)
    
    recent_messages = get_contact_messages(contact_id, limit=20)
    recent_agent_messages = get_recent_agent_messages(contact_id, limit=5)
    topics_discussed = get_topics_already_discussed(contact_id)
    nlp_context = format_nlp_for_prompt(contact_id)
    
    triggers = identify_triggers(message)
    knowledge_section = format_knowledge_for_prompt(get_relevant_knowledge(triggers))
    learning_context = get_learning_context(contact_id, message)
    
    # 5. Conversation Logic
    state = ConversationState(contact_id=contact_id, first_name=first_name)
    state.exchange_count = len([m for m in recent_messages if m["message_type"] == "assistant"])
    state.stage = detect_stage(state, message, recent_messages)
    
    # Dismissive Check
    soft, hard = detect_dismissive(message)
    if hard:
        return jsonify({"status": "stopped", "reason": "opt_out"}), 200

    # Underwriting & Company Detect
    underwriting_context = get_underwriting_context(message)
    company_context = ""
    raw_company = find_company_in_message(message)
    if raw_company:
        normalized = normalize_company_name(raw_company)
        company_context = get_company_context(normalized) if normalized else f"Lead mentioned {raw_company}"

    # Calendar Check
    calendar_slots = ""
    if any(k in message.lower() for k in ["schedule", "time", "available", "call", "appointment"]):
        calendar_slots = consolidated_calendar_op('fetch_slots')

    # 6. AI Generation
    decision_prompt = get_decision_prompt(
        message=message,
        context=f"{nlp_context}\n{knowledge_section}",
        stage=state.stage.value,
        proven_patterns=learning_context,
        triggers_found=triggers
    )

    system_prompt = build_system_prompt(
        state=state,
        bot_first_name=bot_first_name,
        message=message,
        nlp_context=nlp_context,
        proven_patterns=learning_context,
        underwriting_context=underwriting_context,
        company_context=company_context,
        unified_brain=get_unified_brain(),
        lead_vibe=classify_vibe(message).value,
        decision_prompt=decision_prompt,
        age=age,
        recent_agent_messages=recent_agent_messages,
        topics_discussed=topics_discussed,
        calendar_slots=calendar_slots,
        timezone=timezone
    )

    grok_messages = [{"role": "system", "content": system_prompt}]
    for msg in recent_messages[-10:]: # Last 10 messages for context
        role = "user" if msg["message_type"] == "lead" else "assistant"
        grok_messages.append({"role": role, "content": msg["message_text"]})
    if message:
        grok_messages.append({"role": "user", "content": message})

    try:
        response = client.chat.completions.create(
            model="grok-beta",
            messages=grok_messages,
            temperature=0.7
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Grok Error: {e}")
        reply = "Gotcha. Quick question—when was the last time you actually had someone look over those policy details with you?"

    if ghl_api_key != 'DEMO':
        send_sms_via_ghl(contact_id, reply, api_key=ghl_api_key, location_id=location_id)
    
    save_message(contact_id, reply, "assistant")

    # If it's a demo, send facts to update the sidebar. If not, just send the reply.
    if location_id == 'DEMO_ACCOUNT_SALES_ONLY':
        return jsonify({
            "status": "success", 
            "reply": reply,
            "facts": extract_facts_from_message(message)
        })
    
    return jsonify({"status": "success", "reply": reply})

@app.route("/") # Website 
def home():
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>InsuranceGrokBot | AI Lead Re-engagement</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap" rel="stylesheet">
        <style>
            :root { --accent: #00ff88; --dark-bg: #000; --card-bg: #0a0a0a; }
            body { background-color: var(--dark-bg); color: #fff; font-family: 'Inter', sans-serif; scroll-behavior: smooth; }
            .navbar { background-color: rgba(0,0,0,0.9); border-bottom: 1px solid #222; }
            .navbar-brand { font-weight: 700; color: #fff !important; }
            .highlight { color: var(--accent); }
            .btn-outline-danger {
                border-color: #ff4444;
                color: #ff4444;
                transition: 0.3s;
            }
            .btn-outline-danger:hover {
                background-color: #ff4444;
                color: #fff;
            }
            /* Mobile Responsive Tweak */
            @media (max-width: 768px) {
                #demo-container {
                    flex-direction: column; /* Stacks columns vertically */
                    height: auto;           /* Let it grow as long as needed */
                }
                
                #chat-column {
                    height: 500px;         /* Give the chat a fixed height on mobile */
                }

                #fact-column {
                    order: -1;             /* Moves Fact Memory to the TOP on mobile */
                    margin-bottom: 10px;
                }
                /* Inside your existing @media block */
                .input-group {
                    flex-wrap: wrap; /* Allows buttons to drop to a second line if needed */
                }

                #user-input {
                    width: 100% !important; 
                    margin-bottom: 10px;
                    border-radius: 5px !important; /* Makes it a full-width bar */
                }

                #send-btn, .btn-outline-danger {
                    flex-grow: 1; /* Makes buttons wider and easier to tap */
                    padding: 12px;
                }
            }
            /* Sections */
            .hero-section { padding: 100px 0; background: radial-gradient(circle at center, #111 0%, #000 100%); }
            .btn-primary { background-color: #fff; color: #000; border: none; font-weight: bold; padding: 12px 30px; border-radius: 5px; }
            .btn-primary:hover { background-color: var(--accent); color: #000; }
            
            /* Demo Layout */
            #demo-container { display: flex; gap: 20px; height: 600px; position: relative; }
            #chat-column { flex: 2; display: flex; flex-direction: column; background: var(--card-bg); border: 1px solid #333; border-radius: 12px; overflow: hidden; }
            #fact-column { flex: 1; background: #080808; border: 1px solid #333; border-radius: 12px; padding: 20px; }
            
            #chat-window { flex-grow: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 15px; }
            .msg { max-width: 85%; padding: 12px 18px; border-radius: 18px; font-size: 1rem; }
            .bot-msg { background: #1a1a1a; align-self: flex-start; border: 1px solid #333; }
            .user-msg { background: #fff; color: #000; align-self: flex-end; font-weight: 500; }
            
            .fact-pill { background: #111; border-left: 3px solid var(--accent); padding: 10px; margin-bottom: 10px; font-size: 0.9rem; }
            .objection-btn { background: transparent; border: 1px solid #333; color: #888; font-size: 0.85rem; border-radius: 20px; padding: 8px 12px; margin: 3px; }
            .objection-btn:hover { border-color: var(--accent); color: #fff; }

            /* Modal */
            #limit-modal { 
                display: none; position: absolute; top: 0; left: 0; width: 100%; height: 100%; 
                background: rgba(0,0,0,0.98); z-index: 2000; flex-direction: column; 
                justify-content: center; align-items: center; text-align: center; padding: 40px; border: 2px solid var(--accent); border-radius: 12px;
            }

            .progress-container { height: 6px; background: #222; border-radius: 3px; margin-top: 10px; }
            #progress-bar { height: 100%; background: var(--accent); width: 0%; border-radius: 3px; transition: 0.3s; }

            .card { background-color: var(--card-bg); border: 1px solid #222; color: #fff; transition: 0.3s; }
            .card:hover { border-color: var(--accent); }
        </style>
    </head>
    <body>

    <nav class="navbar navbar-expand-lg sticky-top">
        <div class="container">
            <a class="navbar-brand" href="#">INSURANCE<span class="highlight">GROK</span>BOT</a>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav ms-auto">
                    <li class="nav-item"><a href="#demo" class="nav-link">Live Demo</a></li>
                    <li class="nav-item"><a href="#abilities" class="nav-link">Abilities</a></li>
                    <li class="nav-item"><a href="#compatibility" class="nav-link">Compatibility</a></li>
                    <li class="nav-item"><a href="#sales-knowledge" class="nav-link">Sales Knowledge</a></li>
                    <li class="nav-item"><a href="#pricing" class="nav-link">Pricing</a></li>
                    <li class="nav-item"><a href="#contact" class="nav-link text-white fw-bold">Get Started</a></li>
                </ul>
            </div>
        </div>
    </nav>

    <header class="hero-section text-center">
        <div class="container">
            <h1 class="display-3 fw-bold mb-4">The Most Durable Life Insurance Lead Re-engagement Assistant</h1>
            <p class="lead mb-5 text-secondary">Powered by <span class="highlight">xAI's Grok</span>. Built by life insurance agents for life insurance agents.</p>
            <a href="#demo" class="btn btn-primary btn-lg px-5">TEST THE BOT LIVE</a>
        </div>
    </header>

    <section id="demo" class="py-5 bg-black border-top border-secondary">
        <div class="container">
            <div class="text-center mb-5">
                <h2 class="fw-bold">Watch Grok Build The Case</h2>
                <div class="progress-container mx-auto" style="max-width: 500px;">
                    <div id="progress-bar"></div>
                </div>
                <small id="counter-text" class="text-muted">Capacity: 0 / 70</small>
            </div>
            
            <div id="demo-container">
                <div id="limit-modal">
                    <h2 class="fw-bold mb-3">When's the best time to make more money? <span class="highlight">Today!</span></h2>
                    <p class="lead mb-4">Fill out the form below and start today. Let me work for you and get more appointments on your calendar.</p>
                    <a href="#contact" class="btn btn-primary btn-lg" onclick="closeModal()">SECURE YOUR SPOT</a>
                </div>

                <div id="chat-column">
                    <div id="chat-window">
                        <div class="msg bot-msg">Hey! I saw you were looking for coverage recently. Do you actually have a plan in place right now, or are you starting from scratch?</div>
                    </div>
                    <div class="p-3 border-top border-secondary bg-black">
                        <div class="mb-3">
                            <button class="objection-btn" onclick="sendSug('I am 55 and married.')">"I'm 55 and married"</button>
                            <button class="objection-btn" onclick="sendSug('I have a policy through my job.')">"I have work coverage"</button>
                            <button class="objection-btn" onclick="sendSug('I am too old for this now.')">"I am too old"</button>
                        </div>
                        <div class="input-group">
                            <input type="text" id="user-input" class="form-control bg-transparent text-white border-secondary" placeholder="Type an objection...">
                            <button onclick="sendMessage()" class="btn btn-light" id="send-btn">SEND</button>
                            <button onclick="resetDemo()" class="btn btn-outline-danger btn-sm ms-2">RESET</button>
                        </div>
                    </div>
                </div>

                <div id="fact-column">
                    <h5 class="fw-bold mb-4 highlight">LIVE FACT MEMORY</h5>
                    <div id="fact-list">
                        <div class="text-muted small italic">Awaiting lead details...</div>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <section id="abilities" class="py-5 bg-dark">
        <div class="container">
            <h2 class="section-title fw-bold mb-4">Current Abilities</h2>
            <div class="row g-4">
                <div class="col-md-4"><div class="card p-4 h-100"><h3>Multi-Tenant</h3><p class="text-secondary">Handles leads across different agencies with unique identities and data isolation.</p></div></div>
                <div class="col-md-4"><div class="card p-4 h-100"><h3>Deep Discovery</h3><p class="text-secondary">Automated fact-finding to identify gaps in existing work or pension coverage.</p></div></div>
                <div class="col-md-4"><div class="card p-4 h-100"><h3>24/7 Re-engagement</h3><p class="text-secondary">Picks up old leads and works them until they are ready to talk to an agent.</p></div></div>
            </div>
        </div>
    </section>

    <section class="py-5 bg-black">
        <div class="container text-center">
            <h2 class="mb-5 fw-bold">Others vs. InsuranceGrokBot</h2>
            <table class="table text-white border-secondary">
                <thead><tr><th>Feature</th><th>Standard Bot</th><th class="highlight">GrokBot</th></tr></thead>
                <tbody>
                    <tr><td>Logic</td><td>Hardcoded Scripts</td><td>Real-time Reasoning</td></tr>
                    <tr><td>Persistence</td><td>Gives up on "No"</td><td>NEPQ Objection Handling</td></tr>
                    <tr><td>Knowledge</td><td>Generic</td><td>Insurance Specific</td></tr>
                </tbody>
            </table>
        </div>
    </section>

    <section id="compatibility" class="py-5 bg-dark">
        <div class="container">
            <h2 class="fw-bold text-center mb-5">Built for Every CRM</h2>
            <div class="row g-3 text-center">
                <div class="col-md-4"><h4>GoHighLevel</h4><p class="small text-secondary">Native webhook support. Easy setup.</p></div>
                <div class="col-md-4"><h4>HubSpot</h4><p class="small text-secondary">Workflow triggers. Easy setup.</p></div>
                <div class="col-md-4"><h4>Pipedrive</h4><p class="small text-secondary">Activity-based webhooks. Easy setup.</p></div>
                <div class="col-md-4"><h4>Zoho CRM</h4><p class="small text-secondary">Automation rules. Semi-easy setup.</p></div>
                <div class="col-md-4"><h4>Salesforce</h4><p class="small text-secondary">Enterprise outbound messaging. Semi-easy.</p></div>
                <div class="col-md-4"><h4>Zapier</h4><p class="small text-secondary">The universal bridge. Easy setup.</p></div>
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
            <div class="card p-5 mx-auto" style="max-width: 500px;">
                <h3 class="display-4 fw-bold">$100<small class="fs-4">/mo</small></h3>
                <p class="lead">Early Adopter Rate</p>
                <p class="text-secondary">Limited to the first 50 people. Don't let old leads go to waste.</p>
                <a href="#contact" class="btn btn-primary w-100 mt-4">RESERVE MY SPOT</a>
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
                <button type="submit" class="btn btn-primary btn-lg w-100">SUBMIT REQUEST</button>
            </form>
        </div>
    </section>

    <footer class="py-4 text-center border-top border-secondary bg-black">
        <p class="text-secondary">&copy; 2026 InsuranceGrokBot. Built by Life Insurance Agents for Life Insurance Agents.</p>
    </footer>

    <script>
        let exchanges = 0;
        const LIMIT = 70;
        const tracked = new Set();

        async function sendMessage() {
            if(exchanges >= LIMIT) return;
            const input = document.getElementById('user-input');
            const win = document.getElementById('chat-window');
            const msg = input.value.trim();
            if(!msg) return;

            // Add User Message to UI
            win.innerHTML += `<div class="msg user-msg">${msg}</div>`;
            input.value = '';
            exchanges++;
            updateUI();

            try {
                const res = await fetch('/webhook', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        locationId: 'DEMO_ACCOUNT_SALES_ONLY', 
                        contact_id: 'WEB', 
                        first_name: 'Lead', 
                        message: {body: msg}
                    })
                });
                const data = await res.json();
                
                // Add Bot Reply to UI
                win.innerHTML += `<div class="msg bot-msg">${data.reply}</div>`;
                
                // --- SMART SIDEBAR UPDATE ---
                // This uses the "facts" list returned by your Python backend
                if (data.facts && data.facts.length > 0) {
                    const list = document.getElementById('fact-list');
                    // If this is the first fact found, clear the "Awaiting details..." text
                    if(tracked.size === 0) list.innerHTML = ''; 
                    
                    data.facts.forEach(fact => {
                        if(!tracked.has(fact)) {
                            tracked.add(fact);
                            list.innerHTML += `<div class="fact-pill">✔ ${fact}</div>`;
                        }
                    });
                }
                // -----------------------------

                exchanges++;
                updateUI();
                if(exchanges >= LIMIT) document.getElementById('limit-modal').style.display = 'flex';
            } catch(e) { 
                console.error("Connection Error:", e);
                win.innerHTML += `<div class="msg bot-msg">Sorry, I'm having trouble connecting to my brain right now.</div>`;
            }
            win.scrollTop = win.scrollHeight;
        }

        function updateUI() {
            document.getElementById('progress-bar').style.width = (exchanges/LIMIT*100) + '%';
            document.getElementById('counter-text').innerText = `Capacity: ${exchanges} / 70`;
        }

        function sendSug(t) { document.getElementById('user-input').value = t; sendMessage(); }
        function closeModal() { document.getElementById('limit-modal').style.display = 'none'; }

        // === ADD THE RESET FUNCTION HERE ===
        function resetDemo() {
            exchanges = 0;
            tracked.clear();
            document.getElementById('chat-window').innerHTML = '<div class="msg bot-msg">Hey! I saw you were looking for coverage recently. Do you actually have a plan in place right now, or are you starting from scratch?</div>';
            document.getElementById('fact-list').innerHTML = '<div class="text-muted small italic">Awaiting lead details...</div>';
            updateUI();
        }
        // ===================================
    </script>
    </body>
    </html>
    """
    return render_template_string(html_template)

@app.route("/refresh")
def refresh_subscribers():
    """Manually trigger a sync from Google Sheets via URL."""
    try:
        sync_subscribers()
        return "<h1>Success!</h1><p>Subscriber database updated from Google Sheets.</p>", 200
    except Exception as e:
        return f"<h1>Sync Failed</h1><p>{str(e)}</p>", 500
    
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)