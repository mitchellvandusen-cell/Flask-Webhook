from flask import Flask, request, jsonify
import os
import logging
import re
import json
import requests
import csv
import io
from datetime import date, datetime, time, timedelta
from openai import OpenAI
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import psycopg2
load_dotenv()  # Loads variables from .env file

# === EXACT IMPORTS FROM YOUR REPOSITORY ===
from conversation_engine import (
    ConversationState, ConversationStage,
    detect_stage, extract_facts_from_message,
    detect_dismissive,
)

from ghl_message import send_sms_via_ghl

from unified_brain import (get_unified_brain,
    get_decision_prompt,
)
from prompt import build_system_prompt

from ghl_calendar import consolidated_calendar_op

from outcome_learning import (
    classify_vibe,
    get_learning_context,
    init_tables, 
)

from memory import (
    save_message,
    get_contact_messages,
    get_topic_breakdown,
    get_contact_nlp_summary,
    get_topics_already_discussed,
    get_recent_agent_messages,
    format_nlp_for_prompt,
)
from insurance_companies import (
    find_company_in_message,
    normalize_company_name,
    get_company_context,
)
from underwriting import (
    fetch_underwriting_data,
    get_underwriting_context,
)
from knowledge_base import (
    get_relevant_knowledge,
    identify_triggers,
    format_knowledge_for_prompt,
)
from db import (
    get_db_connection,
    init_nlp_tables,
)
from age import calculate_age_from_dob

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load underwriting guide once at startup
try:
    fetch_underwriting_data()
    logger.info("Underwriting guide loaded and cached")
except Exception as e:
    logger.warning(f"Could not load underwriting guide: {e} — proceeding without")

# === ENVIRONMENT VARIABLES ===
XAI_API_KEY = os.getenv("XAI_API_KEY")
GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID")
GHL_CALENDAR_ID = os.getenv("GHL_CALENDAR_ID")
GHL_USER_ID = os.getenv("GHL_USER_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET", "fallback_secret_in_dev_only")  # change fallback in prod!

# Required for Flask sessions and security
app.secret_key = SESSION_SECRET

# === GHL CONNECTION STATUS LOGGING ===
if all([GHL_API_KEY, GHL_LOCATION_ID, GHL_CALENDAR_ID]):
    logger.info("GHL Logger fully connected: API Key, Location ID, and Calendar ID present")
else:
    logger.warning("GHL Logger NOT fully connected:")
    if not GHL_API_KEY:
        logger.warning("   → Missing GHL_API_KEY")
    if not GHL_LOCATION_ID:
        logger.warning("   → Missing GHL_LOCATION_ID")
    if not GHL_CALENDAR_ID:
        logger.warning("   → Missing GHL_CALENDAR_ID")

if GHL_USER_ID:
    logger.info(f"GHL User ID loaded: {GHL_USER_ID}")
else:
    logger.warning("GHL_USER_ID not set — booking may fail with 422 error")

if not XAI_API_KEY:
    logger.warning("XAI_API_KEY missing — Grok calls will fail")

# === xAI GROK CLIENT ===
if XAI_API_KEY:
    client = OpenAI(base_url="https://api.x.ai/v1", api_key=XAI_API_KEY)
    logger.info("Grok client initialized successfully")
else:
    client = None
    logger.warning("XAI_API_KEY missing — Grok calls will use fallback response")

# === DATABASE INITIALIZATION ===
logger.info("Initializing database tables...")

try:
    init_nlp_tables()  # db system first
    logger.info("NLP memory tables initialized")
except Exception as e:
    logger.warning(f"NLP tables init failed (continuing anyway): {e}")

try:
    init_tables()  # Outcome learning second
    logger.info("Outcome learning tables initialized")
except Exception as e:
    logger.warning(f"Outcome learning tables init failed (continuing anyway): {e}")

logger.info("Database initialization complete (failures are non-fatal in dev)")

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}
    if not payload:
            logger.warning("Webhook reveived with no JSON payload")
            return jsonify({"status": "error", "error": "No JSON payload"})
    # Normalize keys to lowercase for easier access
    data = {k.lower(): v for k, v in payload.items()}
    contact_id = (
        data.get("contact_id") or
        data.get("contactid") or
        data.get("contact", {}).get("id") if isinstance(data.get("contact"), dict) else None or
        "unknown"
    )
    first_name = data.get("first_name", "there").capitalize()
    # Extract message body safely
    raw_message = data.get("message", {})
    if isinstance(raw_message, dict):
        message = raw_message.get("body", "").strip()
    else:
        message = str(raw_message).strip()

    logger.info(f"WEBHOOK RECIEVED | CONTACT ID: {contact_id} | Name: {first_name} | Message: '{message}'")

    if contact_id == "unknown":
        logger.warning("Webhook rejected: unknown contact_id")
        return jsonify({"status": "error", "error": "Invalid or missing contact_id"}), 400

    # === DUPLICATE WEBHOOK CHECK (idempotency) ===
    message_id = data.get("message_id") or data.get("id")  # use normalized 'data'
    if message_id:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM processed_webhooks WHERE webhook_id = %s", (message_id,))
                if cur.fetchone():
                    cur.close()
                    conn.close()
                    logger.info(f"Duplicate webhook detected: {message_id} for contact {contact_id} — skipping")
                    return jsonify({"status": "success", "message": "Already processed"}), 200
                
                cur.execute("INSERT INTO processed_webhooks (webhook_id) VALUES (%s)", (message_id,))
                conn.commit()
                logger.debug(f"Webhook {message_id} marked as processed")
            except Exception as e:
                logger.error(f"Error checking/inserting processed webhook {message_id}: {e}")
                conn.rollback()
            finally:
                cur.close()
                conn.close()
        else:
            logger.warning("Could not connect to DB for duplicate check — proceeding anyway")
    else:
        logger.debug("No message_id in webhook — cannot check for duplicates")

    # Extract Age from DOB
    date_of_birth = data.get("contact", {}).get("date_of_birth", "")
    age = calculate_age_from_dob(date_of_birth)

    # === LOAD REAL MEMORY & CONTEXT FROM ALL MODULES ===
    # 1. Full message history
    recent_messages = get_contact_messages(contact_id, limit=50)

    # 2. Recent agent messages (for repetition check)
    recent_agent_messages = get_recent_agent_messages(contact_id, limit=10)

    # 3. NLP summary and topics
    nlp_summary = get_contact_nlp_summary(contact_id)
    topic_breakdown = get_topic_breakdown(contact_id)
    topics_discussed = get_topics_already_discussed(contact_id)

    # 4. Formatted NLP memory for prompt
    nlp_context = format_nlp_for_prompt(contact_id)

    # 5. Knowledge base triggers
    triggers = identify_triggers(message)
    relevant_knowledge = get_relevant_knowledge(triggers)
    knowledge_section = format_knowledge_for_prompt(relevant_knowledge)

    # 6. Full unified brain (always included)
    unified_brain = get_unified_brain()

    # 7. Proven patterns + burn history
    learning_context = get_learning_context(contact_id, message)
    proven_patterns = learning_context

    company_context = ""

    # 8. Lead vibe
    vibe = classify_vibe(message)
    lead_vibe = vibe.value

    # === BUILD CONVERSATION STATE ===
    state = ConversationState(contact_id=contact_id, first_name=first_name)

    # Use real message count for exchange_count (how many times we've sent a message)
    agent_messages = [m for m in recent_messages if m["message_type"] == "agent"]
    state.exchange_count = len(agent_messages)

    # Use real topics from memory (persistent across sessions)
    state.topics_asked = get_topics_already_discussed(contact_id)

    # Initial facts — start empty, will be filled by extract_facts_from_message
    state.facts = {}

    # Detect current stage using real history
    state.stage = detect_stage(state, message or "", recent_messages)

    # Extract facts from current message and update state
    if message:
        new_facts = extract_facts_from_message(state, message)
        logger.info(f"Extracted facts: {new_facts}")

        # Handle dismissive responses
        soft_dismissive, hard_dismissive = detect_dismissive(message)
        if hard_dismissive:
            logger.info("Hard dismissive detected - stopping")
            return jsonify({"status": "stopped", "reason": "opt_out"})
        if soft_dismissive:
            state.soft_dismissive_count += 1
        # === UNDERWRITING, COMPANY & CALENDAR CONTEXT (only if message) ===
        # Underwriting / health context
        underwriting_context = get_underwriting_context(message)

        # Company / carrier detection
        company_context = ""
        raw_company = find_company_in_message(message)
        if raw_company:
            normalized = normalize_company_name(raw_company)
            if normalized:
                logger.info(f"Detected known carrier: {normalized}")
                company_context = get_company_context(normalized)
            else:
                company_context = f"Lead mentioned '{raw_company}' — not a recognized carrier"

        # Calendar / booking intent detection
        calendar_slots = ""
        lower_message = message.lower()
        booking_keywords = [
            "when can we talk", "what times", "available", "schedule", "book",
            "appointment", "meet", "when are you free", "availability",
            "let's set up", "time works", "calendar", "call me", "free on"
        ]
        if any(keyword in lower_message for keyword in booking_keywords):
            logger.info("Booking intent detected — fetching calendar slots")
            try:
                calendar_slots = consolidated_calendar_op('fetch_slots')
            except Exception as e:
                logger.error(f"Failed to fetch calendar slots: {e}")
                calendar_slots = "my usual times: weekday mornings or afternoons"

    # === DECISION/THINKING PROMPT (structured reasoning) ===
    decision_prompt = get_decision_prompt(
        message=message or "",
        context=nlp_context + "\n" + knowledge_section + "\n" + company_context + "\n" + underwriting_context,
        stage=state.stage.value if state.stage else "initial_outreach",
        trigger_suggestion="",
        proven_patterns=proven_patterns,
        triggers_found=triggers
    )
    # === BUILD SYSTEM PROMPT ===
    system_prompt = build_system_prompt(
        state=state,
        contact_id=contact_id,
        message=message or "",
        nlp_context=nlp_context,
        proven_patterns=proven_patterns,
        underwriting_context=underwriting_context,
        company_context=company_context,
        knowledge_section=knowledge_section,
        unified_brain=unified_brain,
        lead_vibe=lead_vibe,
        decision_prompt=decision_prompt,  # structured thinking
        is_follow_up=False,  # you can compute this if needed later
        follow_up_num=0       # you can compute this if needed later
    )

    # === BUILD GROK MESSAGE LIST (real history + system prompt) ===
    messages = []

    # Add full conversation history from database (chronological)
    for msg in recent_messages:
        role = "user" if msg["message_type"] == "lead" else "assistant"
        messages.append({"role": role, "content": msg["message_text"]})

    # Add current lead message if present
    if message:
        messages.append({"role": "user", "content": message})

    # System prompt first (Grok requires it at the beginning)
    messages.insert(0, {"role": "system", "content": system_prompt})

    # === GENERATE REPLY WITH GROK ===
    if not client:
        reply = "Mind sharing when the last time was you looked over your coverage?"
    else:
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
            reply = "Got it, quick question, when was the last time you reviewed your policy?"

    # Fallback if reply is empty or too short
    if not reply or len(reply) < 5:
        reply = "I hear you, most people haven't checked in a while. Mind if I ask when you last looked?"

    # === SEND AND SAVE REPLY ===
    send_sms_via_ghl(contact_id, reply)
    save_message(contact_id, reply, "assistant")

    logger.info(f"Reply sent to {contact_id}: {reply[:100]}...")

    # === RETURN RESPONSE ===
    return jsonify({
        "status": "success",
        "reply": reply
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)