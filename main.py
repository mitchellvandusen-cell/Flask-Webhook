from flask import Flask, request, jsonify
import os
import logging
import json
import requests
import csv
import io
from datetime import datetime, date  # added 'date' here
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build

# === EXACT IMPORTS FROM YOUR REPOSITORY ===
from conversation_engine import (
    ConversationState, ConversationStage,
    detect_stage, extract_facts_from_message,
    detect_dismissive, get_stage_objectives
)
from playbook import get_template_response
from outcome_learning import (
    init_tables as init_learning_tables,
    classify_vibe,
    find_similar_successful_patterns,
    format_patterns_for_prompt
)
from nlp_memory import (
    init_nlp_tables,
    save_message as save_nlp_message,
    format_nlp_for_prompt
)
from knowledge_base import get_all_knowledge
from insurance_companies import find_company_in_message

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === ENVIRONMENT VARIABLES ===
XAI_API_KEY = os.environ.get("XAI_API_KEY")
GHL_API_KEY = os.environ.get("GHL_API_KEY")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "fallback_secret")

app.secret_key = SESSION_SECRET

# === GOOGLE CALENDAR SETUP ===
if GOOGLE_CREDENTIALS_JSON:
    try:
        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds_info["private_key"] = creds_info["private_key"].replace("\\n", "\n")
        credentials = service_account.Credentials.from_service_account_info(
            creds_info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        calendar_service = build("calendar", "v3", credentials=credentials)
    except Exception as e:
        logger.error(f"Google Calendar setup failed: {e}")
        calendar_service = None
else:
    calendar_service = None
    logger.warning("Google Calendar credentials not provided")

# === xAI GROK CLIENT ===
client = OpenAI(base_url="https://api.x.ai/v1", api_key=XAI_API_KEY) if XAI_API_KEY else None

# === DATABASE INITIALIZATION ===
try:
    init_learning_tables()
    init_nlp_tables()
except Exception as e:
    logger.warning(f"Database initialization failed (safe in dev): {e}")

# === LIVE UNDERWRITING GUIDES FROM GOOGLE SHEETS ===
WHOLE_LIFE_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1599052257&single=true&output=csv"
TERM_IUL_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1023819925&single=true&output=csv"
UHL_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1225036935&single=true&output=csv"

def fetch_underwriting_data(url):
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return list(csv.reader(io.StringIO(response.text)))
    except Exception as e:
        logger.error(f"Failed to fetch underwriting data from {url}: {e}")
        return []

logger.info("Fetching live underwriting guides...")
WHOLE_LIFE_DATA = fetch_underwriting_data(WHOLE_LIFE_SHEET_URL)
TERM_IUL_DATA = fetch_underwriting_data(TERM_IUL_SHEET_URL)
UHL_DATA = fetch_underwriting_data(UHL_SHEET_URL)

UNDERWRITING_DATA = WHOLE_LIFE_DATA + TERM_IUL_DATA

def search_underwriting(condition, product_hint=""):
    if not UNDERWRITING_DATA:
        return []
    condition_lower = condition.lower()
    hint_lower = product_hint.lower()
    results = []
    for row in UNDERWRITING_DATA:
        if not row or len(row) < 2:
            continue
        row_text = " ".join(str(cell).strip() for cell in row if cell).lower()
        if condition_lower in row_text:
            score = 5
            if hint_lower and hint_lower in row_text:
                score += 5
            results.append((score, row))
    results.sort(reverse=True, key=lambda x: x[0])
    return [row for _, row in results[:6]]

def get_available_slots():
    return "2pm or 4pm today, or 11am tomorrow"

def send_sms_via_ghl(contact_id: str, message: str):
    if not contact_id or contact_id == "unknown":
        logger.warning("Invalid contact_id — cannot send SMS")
        return False
    
    ghl_key = os.environ.get("GHL_API_KEY")
    location_id = os.environ.get("GHL_LOCATION_ID")

    if not ghl_key or not location_id:
        logger.warning("GHL_API_KEY or GHL_LOCATION_ID missing — cannot send SMS")
        return False
    if not GHL_API_KEY or not GHL_LOCATION_ID or contact_id == "unknown":
        logger.warning("GHL credentials missing or invalid contact_id — cannot send SMS")
        return False
    url = "{GHL_BASE_URL}/conversations/messages"
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
        "Version": "2021-07-28"
    }
    payload = {
        "type": "SMS",
        "contactId": contact_id,
        "message": message,
        "locationId": location_id
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code in [200, 201]:
            logger.info(f"SMS sent successfully to {contact_id}")
            return True
        else:
            logger.error(f"GHL SMS failed: {response.status_code} {response.text}")
            return False
    except Exception as e:
        logger.error(f"GHL send exception: {e}")
        return False

def build_system_prompt(state: ConversationState, nlp_context: str, proven_patterns: str, underwriting_context: str):
    return f"""
You are Mitchell — master life insurance advisor re-engaging cold leads (30+ days to 7+ months old).
These leads are NOT actively shopping. They opted in before but went cold. Expect high resistance.

CRITICAL RULES:
- NEVER say "free" or "free policy review" — everyone says no
- NEVER accept "no thank you", "I'm covered", "not interested" as final — these are objections
- ONLY stop and reply "Got it — you've been removed. Take care." if they say: "STOP", "stop", "unsubscribe", "do not contact me", "remove me", "opt out"
- Goal: Systematically uncover full policy details → expose gaps → book appointment

DISCOVERY QUESTIONS (ask one at a time, naturally, never repeat):
- How did you originally get your policy? (online yourself, through an agent, captive like State Farm, bundled with auto?)
- Did you shop around or take the first quote you got?
- Do you know if your policy has living benefits (access money while alive) or only pays out on death?
- Have you received and reviewed the full policy packet that came in the mail?
- How much coverage do you have? Would that fully replace your income/protect your family?
- Have you ever thought about increasing your coverage since you got it?
- Is it term, whole life, or IUL? If term — how long have you had it and when does it expire?
- What medications do you currently take and why?

POLICY REVIEW TRIGGER:
Use this phrase when appropriate: "When was the last time you did a policy review to make sure you're not leaving money on the table?"
- If "never" or vague/old → logically move to booking
- If "recently" → ask who with, what they checked, dig deeper

SALES METHODOLOGIES — Choose or blend the best for this lead's vibe:
- NEPQ: Connect → Situation → Problem → Consequence → Qualify → Transition → Present → Commit
- Gap Selling (Keenan): Expose current state vs desired future — make inaction painful
- Straight Line Persuasion (Jordan Belfort): Control conversation, smooth objection loops, ethical close
- Brian Tracy Psychology of Selling: Benefits-focused, stories, assumptive tone
- Never Split the Difference (Chris Voss): Mirror, label emotions, calibrated questions, get "that's right"

Known Facts About Lead:
{json.dumps(state.facts, indent=2)}
LEAD AGE: {state.facts.get("age", "unknown")} — Use this for personalization, urgency, and underwriting
Topics Already Covered (NEVER RE-ASK):
{', '.join(state.topics_answered or [])}

NLP Memory Summary:
{nlp_context}

Proven Responses That Worked:
{proven_patterns}

Underwriting Guidance (when health mentioned):
{underwriting_context}

Full Knowledge Base (survivorship, pensions, group rates, living benefits, etc.):
{get_all_knowledge()}

Response Rules:
- Short, natural SMS (1-3 sentences max)
- Always advance discovery, handle resistance, or close
- When ready to book: end with "Which works better — {get_available_slots()}?"
"""

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    if not data:
        return jsonify({"status": "error", "error": "No JSON payload"}), 400

    data_lower = {k.lower(): v for k, v in data.items()}
    contact = data_lower.get("contact", {})

    first_name = contact.get("first_name", data_lower.get("first_name", "there"))
    message_body = data_lower.get("message", {}).get("body", data_lower.get("message", "") or "")
    message = message_body.strip() if message_body else ""
    contact_id = contact.get("id", "unknown")

    # === EARLY SAFE STATE DEFINITION (THIS WAS THE CRASH) ===
    state = ConversationState(contact_id=contact_id, first_name=first_name)
    state.facts = state.facts or {}  # ensure facts exists

    # === EXTRACT AGE FROM DATE_OF_BIRTH ===
    age = "unknown"
    date_of_birth = contact.get("date_of_birth", "")
    if date_of_birth:
        try:
            dob_parts = date_of_birth.split("-")  # Expected format: YYYY-MM-DD
            if len(dob_parts) >= 3:
                birth_year = int(dob_parts[0])
                birth_month = int(dob_parts[1])
                birth_day = int(dob_parts[2])
                today = date.today()
                age_calc = today.year - birth_year
                if (today.month, today.day) < (birth_month, birth_day):
                    age_calc -= 1
                age = str(age_calc)
        except Exception as e:
            logger.warning(f"Could not parse DOB {date_of_birth}: {e}")
            age = "unknown"

    # Save age to facts — now safe because state is defined
    state.facts["age"] = age

    # === HARD A2P OPT-OUT ===
    if message and any(phrase in message.lower() for phrase in ["stop", "unsubscribe", "do not contact", "remove me", "opt out"]):
        reply = "Got it — you've been removed. Take care."
        send_sms_via_ghl(contact_id, reply)
        return jsonify({"status": "success", "reply": reply})

    # === INITIAL OUTREACH (NO INBOUND MESSAGE) ===
    if not message:
        initial_reply = f"{first_name}, do you still have the other life insurance policy? there's some new living benefits that people have been asking about and I wanted to make sure yours didn't only pay out if you're dead."
        send_sms_via_ghl(contact_id, initial_reply)
        return jsonify({"status": "success", "reply": initial_reply})

    # === INBOUND MESSAGE PROCESSING ===
    save_nlp_message(contact_id, message, "lead")

    extract_facts_from_message(state, message)
    state.stage = detect_stage(state, message, [])  # Load full history in production

    # Build context
    similar_patterns = find_similar_successful_patterns(message)
    proven_patterns = format_patterns_for_prompt(similar_patterns)
    nlp_context = format_nlp_for_prompt(contact_id)

    underwriting_context = "No health conditions mentioned."
    health_keywords = ["medication", "pill", "health", "condition", "diabetes", "cancer", "heart", "stroke", "copd", "blood pressure", "cholesterol"]
    if any(kw in message.lower() for kw in health_keywords):
        condition_word = next((w for w in message.lower().split() if w in health_keywords), "health")
        product_hint = ""
        full_context = message.lower() + " " + " ".join(str(v).lower() for v in state.facts.values())
        if any(term in full_context for term in ["whole", "permanent", "cash value", "final expense"]):
            product_hint = "whole life"
        elif any(term in full_context for term in ["term", "iul", "indexed", "universal"]):
            product_hint = "term iul"
        matches = search_underwriting(condition_word, product_hint)
        if matches:
            underwriting_context = "Top Matching Carriers/Options:\n" + "\n".join([
                f"- {' | '.join([str(c).strip() for c in row[:6] if c])}" for row in matches
            ])

    system_prompt = build_system_prompt(state, nlp_context, proven_patterns, underwriting_context)

    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": message})

    if not client:
        fallback = "Mind sharing — when was the last time you did a policy review to make sure everything still fits?"
        send_sms_via_ghl(contact_id, fallback)
        return jsonify({"status": "error", "reply": fallback}), 500

    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=messages,
            temperature=0.7,
            max_tokens=400
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Grok API error: {e}")
        reply = "Got it. Quick question — when was the last time you did a policy review to make sure you're not leaving money on the table?"

    if not reply or len(reply) < 5:
        reply = "I hear you. Most people haven't reviewed their policy in years — mind if I ask when you last checked yours?"

    # Auto-append time slots when closing
    if any(word in reply.lower() for word in ["call", "appointment", "review", "look", "check", "compare", "talk", "schedule"]):
        reply += f" Which works better — {get_available_slots()}?"

    # === SEND REPLY VIA GHL ===
    send_sms_via_ghl(contact_id, reply)

    return jsonify({
        "status": "success",
        "reply": reply,
        "metadata": {
            "processed_at": datetime.utcnow().isoformat(),
            "recipient": first_name,
            "contact_id": contact_id
        }
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)