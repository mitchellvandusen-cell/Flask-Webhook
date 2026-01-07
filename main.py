# main.py - Clean Final Version (2026)

from flask import Flask, request, jsonify, render_template_string
import os
import logging
import re
import uuid
from flask import session
from openai import OpenAI
from dotenv import load_dotenv

# === NEW MINIMAL IMPORTS ===
from prompt import build_system_prompt
from memory import save_message, get_recent_messages, save_new_facts, get_known_facts
from conversation_engine import ConversationState
from outcome_learning import classify_vibe
from ghl_message import send_sms_via_ghl
from ghl_calendar import consolidated_calendar_op
from underwriting import get_underwriting_context
from insurance_companies import find_company_in_message, normalize_company_name, get_company_context
from db import get_subscriber_info, get_db_connection
from age import calculate_age_from_dob
from sync_subscribers import sync_subscribers

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app.secret_key = os.getenv("SESSION_SECRET")
if not app.secret_key:
    logger.warning("SESSION_SECRET not set — sessions will not work properly!")
    app.secret_key = "fallback-insecure-key"
# === API CLIENT ===
XAI_API_KEY = os.getenv("XAI_API_KEY")
client = OpenAI(base_url="https://api.x.ai/v1", api_key=XAI_API_KEY) if XAI_API_KEY else None

# === INITIALIZATION ===
sync_subscribers()  # Run on startup

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
            'ghl_api_key': 'DEMO',
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
    ghl_api_key = subscriber['ghl_api_key']
    timezone = subscriber.get('timezone', 'America/Chicago')

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
    known_facts = [] if is_demo else get_known_facts(contact_id)
    vibe = classify_vibe(message).value
    recent_exchanges = get_recent_messages(contact_id, limit=8)

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
        if ghl_api_key != 'DEMO':
            send_sms_via_ghl(contact_id, reply, ghl_api_key, location_id)

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
        <a class="navbar-brand" href="#">INSURANCE<span class="highlight">GROK</span>BOT</a>
        <div class="collapse navbar-collapse">
            <ul class="navbar-nav ms-auto">
                <li class="nav-item"><a href="#abilities" class="nav-link">Abilities</a></li>
                <li class="nav-item"><a href="#comparison-section" class="nav-link">Comparison</a></li>
                <li class="nav-item"><a href="#compatibility" class="nav-link">Compatibility</a></li>
                <li class="nav-item"><a href="#sales-knowledge" class="nav-link">Sales Logic</a></li>
                <li class="nav-item"><a href="#pricing" class="nav-link">Pricing</a></li>
                <li class="nav-item"><a href="#contact" class="nav-link text-white fw-bold">Get Started</a></li>
            </ul>
        </div>
    </div>
</nav>

<header class="hero-section text-center position-relative">
    <div class="container position-relative" style="z-index: 2;">
        <h1 class="display-3 fw-bold mb-4">The Most Durable Life Insurance Lead Re-engagement Assistant</h1>
        <p class="lead mb-5 text-secondary">Powered by <span class="highlight">xAI's Grok</span>. Built by life insurance agents for life insurance agents.</p>
        <a href="/demo-chat" class="demo-button">Try the Assistant Here</a>
    </div>
</header>

<section id="abilities" class="py-5 bg-dark">
    <div class="container">
        <h2 class="fw-bold text-center mb-5">Current Abilities</h2>
        <div class="row g-4">
            <div class="col-md-4"><div class="card p-4"><h3>Multi-Tenant</h3><p class="text-secondary">Handles leads across different agencies with unique identities and data isolation.</p></div></div>
            <div class="col-md-4"><div class="card p-4"><h3>Deep Discovery</h3><p class="text-secondary">Automated fact-finding to identify gaps in existing work or pension coverage.</p></div></div>
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
                    <h4>Feature</h4>
                    <ul>
                        <li>Logic</li>
                        <li>Persistence</li>
                        <li>Knowledge</li>
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
</body>
</html>
"""
    return render_template_string(home_html)

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