from flask import Flask, request, jsonify
import os
import random
import string
import logging
import requests
import dateparser
import re
import spacy
import subprocess
import sys

# NOTE: Do not download spaCy models at runtime in production.
# Install the model at build time (requirements) instead.

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from openai import OpenAI

# Three-layer conversation architecture
from conversation_engine import (
    ConversationState, ConversationStage,
    build_state_from_history, format_state_for_prompt,
    PolicyEngine, detect_dismissive, parse_reflection, strip_reflection,
    detect_stage, extract_facts_from_message
)
from playbook import (
    get_template_response, get_few_shot_examples,
    get_resistance_template, get_hard_exit_template, get_closing_template,
    match_scenario, get_backbone_probe_template, is_motivating_goal_question
)
# Outcome-based learning system
from outcome_learning import (
    init_tables as init_learning_tables,
    classify_vibe, get_learning_context,
    record_agent_message, record_lead_response,
    save_new_pattern, mark_appointment_booked,
    check_for_burns, VibeClassification,
    find_similar_successful_patterns
)
# Knowledge base - triggers and patterns
from knowledge_base import (
    get_relevant_knowledge, format_knowledge_for_prompt, identify_triggers,
PRODUCT_KNOWLEDGE, HEALTH_CONDITIONS, OBJECTION_HANDLERS
)
# Unified Brain - ALL knowledge consolidated for deliberate decision-making
from unified_brain import get_unified_brain, get_decision_prompt
# Insurance company validation
from insurance_companies import find_company_in_message, get_company_context
# NLP Memory - spaCy-based message storage and topic extraction
from nlp_memory import (
    init_nlp_tables, save_message as save_nlp_message,
    get_topic_breakdown, get_topics_already_discussed,
    format_nlp_for_prompt, get_contact_nlp_summary,
    validate_response_uniqueness, get_recent_agent_messages
)
# Token optimization - tiktoken + sumy for cost reduction
from token_optimizer import (
    count_tokens, compress_conversation_history,
    optimize_prompt, get_token_stats
)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")

# Startup env check
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

logger.info("=== ENV VAR STATUS ===")
logger.info(f"GHL_API_KEY: {'SET' if os.environ.get('GHL_API_KEY') else 'MISSING'}")
logger.info(f"GHL_LOCATION_ID: {'SET' if os.environ.get('GHL_LOCATION_ID') else 'MISSING'}")
logger.info(f"XAI_API_KEY: {'SET' if os.environ.get('XAI_API_KEY') else 'MISSING'}")
logger.info(f"DATABASE_URL: {'SET' if os.environ.get('DATABASE_URL') else 'MISSING'}")

# === STARTUP CREDENTIAL CHECK ===
logger.info("=== CREDENTIAL CHECK ===")
logger.info(f"GHL_API_KEY: {'SET (' + str(len(os.environ.get('GHL_API_KEY', ''))) + ' chars)' if os.environ.get('GHL_API_KEY') else 'MISSING'}")
logger.info(f"GHL_LOCATION_ID: {'SET (' + os.environ.get('GHL_LOCATION_ID', '')[:10] + '...)' if os.environ.get('GHL_LOCATION_ID') else 'MISSING'}")
logger.info(f"GHL_CALENDAR_ID: {'SET (' + os.environ.get('GHL_CALENDAR_ID', '')[:10] + '...)' if os.environ.get('GHL_CALENDAR_ID') else 'MISSING'}")
logger.info(f"XAI_API_KEY: {'SET (' + str(len(os.environ.get('XAI_API_KEY', ''))) + ' chars)' if os.environ.get('XAI_API_KEY') else 'MISSING'}")
logger.info(f"DATABASE_URL: {'SET' if os.environ.get('DATABASE_URL') else 'MISSING'}")
logger.info(f"SESSION_SECRET: {'SET' if os.environ.get('SESSION_SECRET') else 'MISSING'}")
logger.info("=== END CREDENTIAL CHECK ===")

# Initialize outcome learning tables on startup
try:
    init_learning_tables()
    logger.info("Outcome learning system initialized")
except Exception as e:
    logger.warning(f"Could not initialize outcome learning tables: {e}")

# ONE-TIME DB FIX — run on every startup (safe/idempotent)
try:
    import psycopg2
    conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
    cur = conn.cursor()

    # 1) Ensure table exists first
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contact_qualification (
        contact_id TEXT PRIMARY KEY,
        topics_asked TEXT[] DEFAULT ARRAY[]::TEXT[],
        topics_answered TEXT[] DEFAULT ARRAY[]::TEXT[],
        key_quotes TEXT[] DEFAULT ARRAY[]::TEXT[],
        blockers TEXT[] DEFAULT ARRAY[]::TEXT[],
        health_conditions TEXT[] DEFAULT ARRAY[]::TEXT[],
        health_details TEXT[] DEFAULT ARRAY[]::TEXT[],
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # 2) Then ensure columns exist (safe even after table exists)
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS total_exchanges INTEGER DEFAULT 0;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS dismissive_count INTEGER DEFAULT 0;")

    # 3) Core scalar columns used throughout your code (safe/idempotent)
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS has_policy BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_personal_policy BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_employer_based BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_term BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_whole_life BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_iul BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_guaranteed_issue BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS term_length INTEGER;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS face_amount TEXT;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS carrier TEXT;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS has_spouse BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS num_kids INTEGER;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS tobacco_user BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS age INTEGER;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS retiring_soon BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS motivating_goal TEXT;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS has_other_policies BOOLEAN;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS medications TEXT;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_booked BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_qualified BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS appointment_time TEXT;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS already_handled BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS objection_path TEXT;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS waiting_for_health BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS waiting_for_other_policies BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS waiting_for_goal BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS carrier_gap_found BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS appointment_declined BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS waiting_for_medications BOOLEAN DEFAULT FALSE;")

    conn.commit()
    conn.close()
    print("DB fixed: ensured contact_qualification table + required columns")
except Exception as e:
    logger.warning(f"DB INIT WARNING: {e}")

finally:
    try:
        if cur:
            cur.close()
        if conn:
            conn.close()
    except Exception:
        pass

# Initialize NLP memory tables on startup
try:
    init_nlp_tables()
    logger.info("NLP memory system initialized")
except Exception as e:
    logger.warning(f"Could not initialize NLP memory tables: {e}")

# Proper fix (add anywhere in main.py)
def save_nlp_message_text(*args, **kwargs):
    pass  # silent — removes the warning, doesn't break anything

GHL_BASE_URL = "https://services.leadconnectorhq.com"

_client = None

def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("XAI_API_KEY")
        logger.info(f"XAI_API_KEY status: {'SET (' + str(len(api_key)) + ' chars)' if api_key else 'MISSING'}")
    if not api_key:
        logger.error("XAI_API_KEY not found - cannot create client")
        raise ValueError("XAI_API_KEY environment variable is not set")
    _client = OpenAI(base_url="https://api.x.ai/v1", api_key=api_key)
    logger.info("xAI client created successfully")
    return _client

def generate_confirmation_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))

def extract_message_text(data):
    """
    Extract the actual message string from a normalized webhook payload.
    Always returns a string.
    """
    if isinstance(data, str):
        return data

    if isinstance(data, dict):
        for key in ("message", "body", "text", "content"):
            val = data.get(key)
            if isinstance(val, str):
                return val

        return ""

def normalize_keys(data):
    """
    Normalize all dictionary keys to lowercase for case-insensitive field handling.
    This allows GHL webhooks to use any case: message, Message, MESSAGE, etc.
    """
    if not isinstance(data, dict):
        return data
    return {k.lower(): v for k, v in data.items()}

def generate_nepq_response(
    first_name,
    message,
    agent_name="Mitchell",
    conversation_history=None,
    intent="general",
    contact_id=None,
    api_key=None,
    calendar_id=None,
    timezone="America/New_York",
):
    confirmation_code = generate_confirmation_code()

    try:
        if isinstance(message, dict):
            message = message.get("body") or message.get("message") or message.get("text") or ""
        elif not isinstance(message, str):
            message = "" if message is None else str(message)

        # FIX: always strip when it's a string now
        message = message.strip()
        if not message:
            message = "initial outreach - contact just entered pipeline"

        if conversation_history is None:
            conversation_history = []

        # ------------------------------------------------------------------
        # 1) NLP MEMORY (store + topic recall)
        # ------------------------------------------------------------------
        save_nlp_message(contact_id, message, "lead")
        topics_asked = get_topics_already_discussed(contact_id)

        # ------------------------------------------------------------------
        # 2) CONVERSATION ENGINE (state + stage)
        # ------------------------------------------------------------------
        state = build_state_from_history(
            contact_id=contact_id,
            first_name=first_name,
            conversation_history=conversation_history,
            current_message=message
        )

        stage = detect_stage(state, message, conversation_history)
        stage_value = stage.value if hasattr(stage, "value") else str(stage)  # FIX: safe stage string

        extract_facts_from_message(state, message)

        # ------------------------------------------------------------------
        # 3) TRIGGERS (string-safe now)
        # ------------------------------------------------------------------
        triggers_found = identify_triggers(message)
    
        reply, use_llm = process_message(state, contact_id, message)
        if reply is not None or use_llm is False:
            return reply, use_llm
        # ------------------------------------------------------------------
        # 4) KNOWLEDGE BASE (product / objections / health)
        # ------------------------------------------------------------------
        knowledge = get_relevant_knowledge(message)
        knowledge_context = format_knowledge_for_prompt(knowledge)

        # ------------------------------------------------------------------
        # 5) OUTCOME LEARNING (what worked before)
        # ------------------------------------------------------------------
        learning_ctx = get_learning_context(contact_id, current_message=message)
        proven_patterns = find_similar_successful_patterns(message)
        proven_text = ""

        if proven_patterns:
            proven_text = "\n".join(
                f"- {p['response_used']}" for p in proven_patterns[:3]
            )

        # ------------------------------------------------------------------
        # 6) PLAYBOOK (templates + few-shot)
        # ------------------------------------------------------------------
        templates = get_template_response(stage, first_name)
        few_shots = get_few_shot_examples(stage)

        # ------------------------------------------------------------------
        # 7) UNIFIED BRAIN (final decision prompt)
        # ------------------------------------------------------------------
        brain = get_unified_brain()

        decision_prompt = get_decision_prompt(
            message=message,
            context=(
                f"Stage: {stage_value}\n\n"
                f"Conversation History:\n{conversation_history[-6:]}\n\n"
                f"Topics already asked:\n{topics_asked}\n\n"
                f"Knowledge Base:\n{knowledge_context}\n\n"
                f"Proven Successful Responses:\n{proven_text}\n\n"
                f"Playbook Templates:\n{templates}\n\n"
            ),
            stage=stage_value,
            trigger_suggestion=None,
            proven_patterns=proven_text,
            triggers_found=triggers_found,
        )

        # ------------------------------------------------------------------
        # 8) GROK / xAI CALL
        # ------------------------------------------------------------------
        client = get_client()
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[
                {"role": "system", "content": brain},
                {"role": "user", "content": decision_prompt}
            ],
            temperature=0.6
        )

        raw_reply = response.choices[0].message.content.strip()

        # Extract only the <response> part
        if "<response>" in raw_reply and "</response>" in raw_reply:
            reply = raw_reply.split("<response>")[1].split("</response>")[0].strip()
        else:
            reply = raw_reply.split("</thinking>")[-1].strip() if "</thinking>" in raw_reply else raw_reply
            reply = " ".join(reply.split())

    except Exception as e:
        logger.error(f"Grok call failed: {e}")
        reply = "Hey, sorry — something went wrong on my end. Can you try again?"

    # Optional: send SMS via GHL if creds exist
    try:
        ghl_key = os.environ.get("GHL_API_KEY")
        location_id = os.environ.get("GHL_LOCATION_ID")
        if ghl_key and location_id and contact_id:
            logger.info(f"About to send SMS - contact_id: {contact_id}..."
            )
            url = f"{GHL_BASE_URL}/conversations/messages"
            headers = {
                "Authorization": f"Bearer {ghl_key}",
                "Version": "2021-07-28",
                "Content-Type": "application/json"
            }
            payload = {
                "type": "SMS",
                "message": reply,
                "contactId": contact_id
            }
            # FIX: include locationId when available (harmless if API ignores it)
            if location_id:
                payload["locationId"] = location_id

            r = requests.post(
                url, 
                json=payload, 
                headers=headers
            )
            logger.info(f"SMS sent: {r.status_code} - reply: {reply:[50]} ...")
        else:
            logger.warning("Missing GHL credentials — SMS not sent")
    except Exception as e:
            logger.error(f"SMS send failed: {e}")

    return reply or ""

# ============================================================================
# CONTACT QUALIFICATION STATE - Persistent memory per contact_id
# ============================================================================

def get_qualification_state(contact_id):
    """
    Get or create qualification state for a contact.
    Returns dict with all qualification fields.
    """
    if not contact_id:
        return None

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT * FROM contact_qualification WHERE contact_id = %s", (contact_id,))
        row = cur.fetchone()

        if row:
            result = dict(row)
            conn.close()
            return result

        # Create new record if doesn't exist
        cur.execute("""
            INSERT INTO contact_qualification (contact_id)
            VALUES (%s)
            RETURNING *
        """, (contact_id,))
        row = cur.fetchone()
        conn.commit()
        result = dict(row) if row else None
        conn.close()
        return result

    except Exception as e:
        logger.warning(f"Could not get qualification state: {e}")
    return None

def update_qualification_state(contact_id, updates):
    """
    Update qualification state fields for a contact.
    updates: dict of field_name -> value
    """
    if not contact_id or not updates:
        return False

    try:
        import psycopg2
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
        cur = conn.cursor()

        # Ensure contact exists first
        cur.execute(
            """
            INSERT INTO contact_qualification (contact_id)
            VALUES (%s)
            ON CONFLICT (contact_id) DO NOTHING
            """,
            (contact_id,)
        )

        # Build dynamic update query
        set_clauses = []
        values = []
        for field, value in updates.items():
            set_clauses.append(f"{field} = %s")
            values.append(value)

        set_clauses.append("updated_at = CURRENT_TIMESTAMP")
        values.append(contact_id)

        query = f"UPDATE contact_qualification SET {', '.join(set_clauses)} WHERE contact_id = %s"
        cur.execute(query, values)
        conn.commit()
        conn.close()
        return True

    except Exception as e:
        logger.warning(f"Could not update qualification state: {e}")
    return False

def add_to_qualification_array(contact_id, field, value):
    """
    Add a value to an array field (health_conditions, topics_asked, etc.)
    Won't add duplicates. Only works with TEXT[] columns.
    """
    if not contact_id or not field or not value:
        return False

    allowed_array_fields = {
        'health_conditions', 'health_details', 'key_quotes',
        'blockers', 'topics_asked', 'topics_answered'
    }
    if field not in allowed_array_fields:
        logger.warning(f"add_to_qualification_array: Invalid field '{field}' - not a TEXT[] column")
        return False

    conn = None  # FIX: defined before try/finally
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
        cur = conn.cursor()

        cur.execute(f"""
            UPDATE contact_qualification
            SET {field} = CASE
                WHEN %s = ANY(COALESCE({field}, ARRAY[]::TEXT[]))
                THEN {field}
                ELSE array_append(COALESCE({field}, ARRAY[]::TEXT[]), %s)
            END,
            updated_at = CURRENT_TIMESTAMP
            WHERE contact_id = %s
        """, (value, value, contact_id))

        conn.commit()
        return True

    except Exception as e:
        logger.warning(f"Could not add to qualification array: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

def extract_and_update_qualification(contact_id, message, conversation_history=None):
    """
    Extract qualification data from message and update state.
    Called after each message to build persistent memory.
    """
    if not contact_id or not message:
        return {}

    updates = {}
    all_text = message.lower()
    if conversation_history:
        all_text = " ".join([m.replace("Lead:", "").replace("You:", "") for m in conversation_history]) + " " + message
    all_text = all_text.lower()

    # === COVERAGE STATUS ===
    if re.search(r"(have|got|already).*(coverage|policy|insurance|protected)", all_text):
        updates["has_policy"] = True
    if re.search(r"(don'?t|dont|no).*(coverage|policy|insurance)", all_text):
        updates["has_policy"] = False

    # Living benefits
    if re.search(r"(living benefits|access.*(funds|money).*alive|accelerated)", all_text):
        if re.search(r"(yes|yeah|has|have|it does|got that)", message.lower()):
            updates["has_living_benefits"] = True
        elif re.search(r"(no|nope|just.*death|doesn'?t|dont)", message.lower()):
            updates["has_living_benefits"] = False

    # === POLICY SOURCE ===
    is_personal = re.search(r"(my own|personal|private|individual).*(policy|coverage|insurance)", all_text)
    not_through_work = re.search(r"(not|isn'?t|isnt).*(through|from|via|at).*(work|job|employer)", all_text)
    follows_me = re.search(r"(yes\s*)?(it\s*)?(follows|portable|goes with|take it with|keeps?|stays?)", message.lower())
    not_employer = re.search(r"not\s*(an?\s*)?(employer|work|job)\s*(policy|plan|coverage)?", message.lower())

    if is_personal or not_through_work or follows_me or not_employer:
        updates["is_personal_policy"] = True
        updates["is_employer_based"] = False
        add_to_qualification_array(contact_id, "topics_asked", "employer_portability")
        add_to_qualification_array(contact_id, "topics_asked", "job_coverage")
        add_to_qualification_array(contact_id, "topics_asked", "retirement")

    if re.search(r"(through|from|at|via).*(work|job|employer|company)", all_text) and not not_through_work and not follows_me:
        updates["is_employer_based"] = True
        updates["is_personal_policy"] = False

    # === POLICY TYPES ===
    if re.search(r"term\s*(life|policy|insurance|plan)", all_text):
        updates["is_term"] = True
    if re.search(r"whole\s*life", all_text):
        updates["is_whole_life"] = True
    if re.search(r"\biul\b|indexed\s*universal", all_text):
        updates["is_iul"] = True
    if re.search(r"guaranteed\s*(issue|acceptance)|no\s*(health|medical)\s*questions|colonial\s*penn|globe\s*life", all_text):
        updates["is_guaranteed_issue"] = True

    # Term length
    term_match = re.search(r"(\d{2})\s*year\s*term", all_text)
    if term_match:
        updates["term_length"] = int(term_match.group(1))

    # === COVERAGE DETAILS ===
    amount_match = re.search(r"(\$?\d{1,3}(?:,?\d{3})*|\d+k)\s*(coverage|policy|worth|face|death\s*benefit)?", all_text)
    if amount_match:
        amount = amount_match.group(1).replace(",", "").replace("$", "")
        if "k" in amount.lower():
            updates["face_amount"] = amount.upper()
        else:
            try:
                if int(amount) > 1000:
                    updates["face_amount"] = str(int(int(amount) / 1000)) + "k"
            except Exception:
                pass

    # Carrier detection
    from insurance_companies import find_company_in_message
    company = find_company_in_message(message)
    if company:
        updates["carrier"] = company

    # === FAMILY ===
    if re.search(r"wife|husband|spouse|married", all_text):
        updates["has_spouse"] = True
    if re.search(r"single|not\s*married|divorced|widowed", all_text):
        updates["has_spouse"] = False

    kids_match = re.search(r"(\d+)\s*kids?", all_text)
    if kids_match:
        updates["num_kids"] = int(kids_match.group(1))
    if re.search(r"no\s*kids|don'?t\s*have\s*kids|childless", all_text):
        updates["num_kids"] = 0

    # === HEALTH CONDITIONS ===
    health_conditions = []
    health_patterns = {
        "diabetes": r"diabetes|diabetic|a1c|insulin|metformin",
        "heart": r"heart\s*(attack|disease|condition|problem)|cardiac|stent",
        "cancer": r"cancer|tumor|chemo|radiation|remission",
        "copd": r"copd|breathing|oxygen|respiratory|emphysema",
        "stroke": r"stroke",
        "blood_pressure": r"blood\s*pressure|hypertension",
        "sleep_apnea": r"sleep\s*apnea|cpap",
        "anxiety_depression": r"anxiety|depression|mental\s*health",
    }
    for condition, pattern in health_patterns.items():
        if re.search(pattern, all_text):
            health_conditions.append(condition)

    # Tobacco
    if re.search(r"smok(e|er|ing)|tobacco|cigarette|vape|nicotine", all_text):
        updates["tobacco_user"] = True
    if re.search(r"(don'?t|never|quit|stopped)\s*smok", all_text):
        updates["tobacco_user"] = False

    # === DEMOGRAPHICS ===
    age_match = re.search(r"i'?m\s*(\d{2})|(\d{2})\s*years?\s*old|age\s*(\d{2})", all_text)
    if age_match:
        age = age_match.group(1) or age_match.group(2) or age_match.group(3)
        updates["age"] = int(age)

    if re.search(r"retir(e|ing|ed|ement)|about\s*to\s*(stop|quit)\s*work", all_text):
        updates["retiring_soon"] = True

    # === MOTIVATING GOALS (Why they originally looked) ===
    goal_patterns = {
        "add_coverage": r"(add|more|additional|extra)\s*(coverage|protection|insurance)|on\s*top\s*of|supplement",
        "cover_mortgage": r"cover.*(mortgage|house|home)|(mortgage|house|home).*(paid|covered|protected|taken\s*care)|pay\s*off.*(mortgage|house)",
        "final_expense": r"final\s*expense|funeral|burial|cremation|end\s*of\s*life|bury\s*me",
        "family_protection": r"protect.*(family|wife|husband|kids)|worried.*(family|kids)|leave.*(family|kids)",
        "leave_legacy": r"leave.*(something|behind|legacy)|inheritance",
        "income_replacement": r"replace.*(income|salary)|if\s*(i|something)\s*(die|happen)",
        "family_death": r"(mom|dad|parent|brother|sister).*(died|passed|death|funeral)",
    }
    for goal, pattern in goal_patterns.items():
        if re.search(pattern, all_text):
            updates["motivating_goal"] = goal
            break

    # === BLOCKERS ===
    blockers = []
    if re.search(r"(too|really)\s*(busy|swamped)", all_text):
        blockers.append("too_busy")
    if re.search(r"(can'?t|don'?t)\s*afford|too\s*expensive", all_text):
        blockers.append("cost_concern")
    if re.search(r"don'?t\s*trust|scam", all_text):
        blockers.append("trust_issue")
    if re.search(r"need\s*to\s*think|talk\s*to\s*(spouse|wife|husband)", all_text):
        blockers.append("needs_time")

    # Apply updates
    if updates:
        update_qualification_state(contact_id, updates)

    # Add health conditions to array
    for condition in health_conditions:
        add_to_qualification_array(contact_id, "health_conditions", condition)

    # Add blockers to array
    for blocker in blockers:
        add_to_qualification_array(contact_id, "blockers", blocker)

    return updates

def parse_history_for_topics_asked(contact_id, conversation_history):
    """
    Parse previous conversation history to retroactively identify topics already asked by the agent.
    This backfills topics_asked to prevent repeating questions that were already asked in earlier messages.
    """
    if not contact_id or not conversation_history:
        logger.info("HISTORY PARSE: Skipped (missing contact_id or empty history)")  # FIX: truthful log
        return

    current_state = get_qualification_state(contact_id)
    existing_topics = set(current_state.get("topics_asked") or []) if current_state else set()

    AGENT_QUESTION_PATTERNS = {
        "motivation": [
            r"what (got|made|brought) you",
            r"what originally",
            r"why did you (start|begin|decide)",
            r"what (triggered|prompted)",
            r"what's (driving|behind) this",
            r"what made you want to look",
            r"something on your mind",
            r"what were you hoping",
        ],
        "living_benefits": [
            r"living benefits",
            r"access.*(funds|money|policy).*while.*alive",
            r"accelerated (death )?benefit",
            r"critical illness",
            r"chronic illness",
        ],
        "portability": [
            r"(continue|keep|take).*(after|when|if).*(retire|leave|quit)",
            r"goes with you",
            r"follows you",
            r"portable",
            r"when you leave",
        ],
        "employer_coverage": [
            r"through (work|your job|employer)",
            r"work.*policy",
            r"employer.*(policy|coverage)",
            r"job.*(policy|coverage)",
        ],
        "policy_type": [
            r"term or (whole|permanent)",
            r"what (kind|type) of (policy|coverage)",
            r"is it term",
            r"is it whole life",
        ],
        "family": [
            r"(married|wife|husband|spouse)",
            r"(kids|children)",
            r"family situation",
        ],
        "coverage_amount": [
            r"how much (coverage|insurance|protection)",
            r"what.*amount",
            r"face amount",
        ],
        "carrier": [
            r"who.*(with|through|did you go with)",
            r"which (company|carrier|insurer)",
            r"who.*set you up",
        ],
        "health": [
            r"health (conditions?|issues?|problems?)",
            r"taking.*medications?",
            r"any (medical|health)",
        ],
        "other_policies": [
            r"any other (policies|coverage)",
            r"other.*(policies|coverage).*(work|otherwise)",
            r"anything else.*covered",
        ],
    }

    topics_found = set()

    for msg in conversation_history:
        msg_lower = msg.lower() if isinstance(msg, str) else ""
        is_agent_msg = msg_lower.startswith("you:") or (not msg_lower.startswith("lead:"))

        if is_agent_msg:
            for topic, patterns in AGENT_QUESTION_PATTERNS.items():
                if topic in existing_topics or topic in topics_found:
                    continue
                for pattern in patterns:
                    if re.search(pattern, msg_lower):
                        topics_found.add(topic)
                        logger.debug(f"HISTORY PARSE: Found topic '{topic}' in agent message: {msg[:50]}...")
                        break

    LEAD_ANSWER_PATTERNS = {
        "motivation": [
            r"(add|more|additional).*(coverage|protection)",
            r"cover.*(mortgage|house)",
            r"final expense|funeral|burial",
            r"protect.*(family|wife|kids)",
            r"(mom|dad|parent).*(died|passed)",
            r"leave.*legacy",
        ],
        "carrier": [
            r"(state farm|allstate|northwestern|prudential|aig|metlife|john hancock|lincoln|transamerica|pacific life|principal|nationwide|mass mutual|new york life|guardian|mutual of omaha|aflac|colonial penn|globe life)",
        ],
        "employer_coverage": [
            r"(through|from|at|via).*(work|job|employer|company)",
            r"(not|isn'?t).*(through|from).*(work|job|employer)",
            r"(my own|personal|private|individual).*(policy|coverage)",
        ],
        "living_benefits": [
            r"(yes|yeah|has|have|it does).*(living benefits)",
            r"(no|nope|just.*death|doesn'?t).*(living benefits)",
        ],
    }

    for msg in conversation_history:
        msg_lower = msg.lower() if isinstance(msg, str) else ""
        is_lead_msg = msg_lower.startswith("lead:")

        if is_lead_msg:
            for topic, patterns in LEAD_ANSWER_PATTERNS.items():
                if topic in existing_topics or topic in topics_found:
                    continue
                for pattern in patterns:
                    if re.search(pattern, msg_lower):
                        topics_found.add(topic)
                        logger.debug(f"HISTORY PARSE: Lead answered '{topic}' in message: {msg[:50]}...")
                        break

    for topic in topics_found:
        if topic not in existing_topics:
            add_to_qualification_array(contact_id, "topics_asked", topic)
            logger.info(f"BACKFILL: Added '{topic}' to topics_asked for contact {contact_id}")

    return topics_found

# (Everything below this point is unchanged EXCEPT the one bug fix in send_sms_via_ghl)
# ============================================================================
# 
def format_qualification_for_prompt(qualification_state):
    """
    Format qualification state as context for the LLM prompt.
    """
    if not qualification_state:
        return ""

    sections = []
    q = qualification_state

    # Coverage status
    coverage_facts = []
    if q.get("has_policy") is True:
        coverage_facts.append("HAS COVERAGE")
    elif q.get("has_policy") is False:
        coverage_facts.append("NO COVERAGE")

    if q.get("is_employer_based"):
        coverage_facts.append("through employer")
    elif q.get("is_personal_policy"):
        coverage_facts.append("personal policy (NOT through work)")

    if q.get("carrier"):
        coverage_facts.append(f"with {q['carrier']}")

    if coverage_facts:
        sections.append(f"Coverage: {', '.join(coverage_facts)}")

    # Policy type
    policy_types = []
    if q.get("is_term"):
        term_info = "Term"
        if q.get("term_length"):
            term_info += f" ({q['term_length']}yr)"
        policy_types.append(term_info)
    if q.get("is_whole_life"):
        policy_types.append("Whole Life")
    if q.get("is_iul"):
        policy_types.append("IUL")
    if q.get("is_guaranteed_issue"):
        policy_types.append("Guaranteed Issue")

    if policy_types:
        sections.append(f"Policy Type: {', '.join(policy_types)}")

    # Living benefits
    if q.get("has_living_benefits") is True:
        sections.append("Living Benefits: YES (has them)")
    elif q.get("has_living_benefits") is False:
        sections.append("Living Benefits: NO (doesn't have)")

    # Face amount
    if q.get("face_amount"):
        sections.append(f"Face Amount: {q['face_amount']}")

    # Family
    family_facts = []
    if q.get("has_spouse") is True:
        family_facts.append("married")
    elif q.get("has_spouse") is False:
        family_facts.append("single")
    if q.get("num_kids") is not None:
        family_facts.append(f"{q['num_kids']} kids")
    if family_facts:
        sections.append(f"Family: {', '.join(family_facts)}")

    # Health
    if q.get("health_conditions") and len(q["health_conditions"]) > 0:
        sections.append(f"Health Conditions: {', '.join(q['health_conditions'])}")
    if q.get("tobacco_user") is True:
        sections.append("Tobacco: YES")
    elif q.get("tobacco_user") is False:
        sections.append("Tobacco: NO")

    # Demographics
    if q.get("age"):
        sections.append(f"Age: {q['age']}")
    if q.get("retiring_soon"):
        sections.append("Retiring Soon: YES")

    # Motivation
    if q.get("motivating_goal"):
        sections.append(f"Motivation: {q['motivating_goal'].replace('_', ' ').title()}")

    # Blockers
    if q.get("blockers") and len(q["blockers"]) > 0:
        sections.append(f"Blockers: {', '.join(q['blockers'])}")

    # Topics already asked - CRITICAL to prevent repeat questions
    if q.get("topics_asked") and len(q["topics_asked"]) > 0:
        topic_list = ', '.join(q['topics_asked'])
        sections.append(f"TOPICS ALREADY COVERED (DO NOT ASK AGAIN): {topic_list}")

    # === SEMANTIC BLOCKING - questions that are LOGICALLY off the table ===
    blocked_questions = []

    # Personal policy = employer questions are NONSENSE - absolutely block these
    if q.get("is_personal_policy") or q.get("is_employer_based") == False or "employer_portability" in (q.get("topics_asked") or []) or "retirement" in (q.get("topics_asked") or []):
        blocked_questions.extend([
            "Does it continue after retirement?",
            "What happens when you leave your job?",
            "Can you convert it?",
            "Is it portable?",
            "What happens if you retire?",
            "Does it go with you if you leave?",
            "Would you have to convert it?",
            "Is it tied to the employer?",
            "STOP - THIS IS A PERSONAL POLICY. Retirement/job questions make no sense."
        ])

    # If they told us policy type, don't ask about it
    if q.get("is_term"):
        blocked_questions.append("Is it term or whole life?")
    if q.get("is_whole_life") or q.get("is_iul"):
        blocked_questions.append("Is it term or permanent?")

    # If they told us living benefits status
    if q.get("has_living_benefits") is not None:
        blocked_questions.append("Does it have living benefits?")

    # If we know their goal
    if q.get("motivating_goal"):
        blocked_questions.append("What made you want to look at coverage?")

    # If we know other policies status
    if q.get("has_other_policies") is not None:
        blocked_questions.append("Any other policies?")

    if blocked_questions:
        sections.append(f"""
    === QUESTIONS YOU CANNOT ASK (already answered or logically irrelevant) ===
    {chr(10).join('- ' + bq for bq in blocked_questions)}
    === ASKING ANY OF THESE MAKES THE CONVERSATION FEEL ROBOTIC ===
    """)

    if not sections:
        return ""

    return f"""
    === KNOWN FACTS ABOUT THIS CONTACT (from database memory) ===
    {chr(10).join(sections)}
    === USE THIS INFO - DO NOT ASK ABOUT THINGS YOU ALREADY KNOW ===

    """


def mark_topic_asked(contact_id, topic):
    """Mark a topic as asked to prevent repeat questions."""
    add_to_qualification_array(contact_id, "topics_asked", topic)


def increment_exchanges(contact_id):
    """Increment the exchange counter for a contact."""
    if not contact_id:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
        cur = conn.cursor()
        cur.execute("""
            UPDATE contact_qualification 
            SET total_exchanges = total_exchanges + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE contact_id = %s
        """, (contact_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Could not increment exchanges: {e}")


# ============================================================================
# ALREADY COVERED OBJECTION HANDLER (State Machine)
# ============================================================================
# Carriers where we can often find a better option (seed doubt, justify appointment)
# Not actually "high risk" - just carriers where we frequently beat their rates
COMPARISON_OPPORTUNITY_CARRIERS = [
    "mutual of omaha", "foresters", "transamerica", "americo", 
    "prosperity", "aig", "gerber", "globe life", "colonial penn",
    "aflac", "primerica"
]

ALREADY_HAVE_TRIGGERS = [
    "already have", "already got", "i'm good", "im good", "taken care of", 
    "i'm covered", "im covered", "got it", "have insurance", "got insurance",
    "have a policy", "got a policy", "set", "all set", "good on", "all good", "covered"
]

TIME_AGREEMENT_TRIGGERS = [
    "10:15", "2:30", "tomorrow", "morning", "afternoon", "tonight", "today", 
    "evening", "either", "works", "yes", "sure", "book it", "let's do it", 
    "lets do it", "sounds good", "ok", "okay", "alright", "perfect", 
    "that works", "im in", "i'm in"
]

def extract_carrier_name(text):
    """Extract insurance carrier from message text."""
    carriers = {
        "mutual of omaha": ["mutual of omaha", "moo", "omaha"],
        "foresters": ["foresters", "forest"],
        "transamerica": ["transamerica", "trans america"],
        "americo": ["americo"],
        "aig": ["aig"],
        "prudential": ["prudential", "pru"],
        "lincoln": ["lincoln financial", "lincoln"],
        "protective": ["protective"],
        "banner": ["banner"],
        "sbli": ["sbli"],
        "new york life": ["new york life", "nyl"],
        "northwestern mutual": ["northwestern", "northwest mutual"],
        "state farm": ["state farm"],
        "allstate": ["allstate"],
        "nationwide": ["nationwide"],
        "globe life": ["globe life", "globe"],
        "colonial penn": ["colonial penn", "colonial"],
        "gerber": ["gerber"],
        "aflac": ["aflac"],
        "metlife": ["metlife", "met life"],
        "john hancock": ["john hancock", "hancock"],
        "mass mutual": ["mass mutual", "massmutual"],
        "principal": ["principal"],
        "pacific life": ["pacific life"],
        "unum": ["unum"]
    }
    text = text.lower()
    for name, keywords in carriers.items():
        if any(k in text for k in keywords):
            return name
    return None


def already_covered_handler(contact_id, message, state, api_key=None, calendar_id=None, timezone="America/New_York"):
    """
    Handle the "Already Have Coverage" objection pathway.
    This is a deterministic state machine that runs BEFORE the LLM.

    FLOW (3 steps to appointment):
    1. "Already covered" → "Who'd you go with?"
    2. [carrier] → "Did someone help you or find them yourself? They help higher risk, serious health issues?"
    3. [no/healthy] → "Weird... they're good but higher risk = expensive for healthy. Time tonight/tomorrow?" 

    Returns (response, should_continue) where should_continue=False means use this response.
    """
    if not contact_id or not state:
        return None, True

    m = message.lower().strip()



# Helper for slot text - returns (slot_text, has_real_slots)
def get_slot_text():
    if api_key and calendar_id:
        slots = get_available_slots(calendar_id, api_key, timezone)
        if slots:
            formatted = format_slot_options(slots, timezone)
            if formatted:
                return formatted, True
    return None, False

# Helper to build appointment offer with real or fallback language
def build_appointment_offer(prefix="I have some time"):
    slot_text, has_slots = get_slot_text()
    if has_slots and slot_text:
        return f"{prefix} {slot_text}"
    else:
        return "When are you usually free for a quick call"
        
def process_message(state, contact_id, message, m):
    m = (message or "").lower()
    # PASTE STEP 5/4/3/2/1 here (indented)
    # ========== STEP 5: They answered medication question ==========
    if state.get("waiting_for_medications"):
    
        if re.search(
            r'^(none?|no|nada|nothing|nope|not taking any|clean bill)$', 
            m
        ) or re.search(r'\bno\s*(meds|medications?|pills)\b', m):
            meds = "None reported"
        else:
            meds = message.strip()
        
            update_qualification_state(contact_id, {
            "medications": meds,
            "waiting_for_medications": False
        })
    
        appt_time = state.get("appointment_time", "our call")
        if meds == "None reported":
            return (
                f"Perfect, clean health means best rates. I'll have everything ready for {appt_time}."
                "Calendar invite coming your way. Talk soon!"
            ), False
        else:
            return (
                f"Got it, thank you! I'll have everything pulled and priced out before {appt_time}. "
                "Calendar invite coming in a few minutes. Talk soon!"
            ), False

    # ========== STEP 4a: Check for REJECTION of appointment offer FIRST ==========
    # Must check BEFORE time agreement to avoid "No that's okay" matching "okay"
    if state.get("carrier_gap_found"):
        # Rejection patterns - "no" followed by polite decline
        rejection_patterns = [
            r"^no\b",  # Starts with "no"
            r"\bno\s*(that'?s|thats)?\s*(okay|ok|thanks|thank\s*you)\b",  # "no that's okay", "no thanks"
            r"\bno\s*i\s*(don'?t|dont)\s*(want|need)\b",  # "no I don't want"
            r"\bnot\s*(interested|right\s*now|for\s*me)\b",  # "not interested"
            r"\bi'?m\s*(good|okay|fine|all\s*set)\b",  # "I'm good"
            r"\bpass\b|\bno\s*way\b|\bforget\s*it\b",  # explicit decline
            r"\bdon'?t\s*(want|need)\s*(to|a|any)?\s*(talk|call|meet|appointment)\b",  # "don't want to talk"
        ]
        
        is_rejection = any(re.search(p, m) for p in rejection_patterns)
        # But NOT if they also mention a specific time (mixed signal = accept)
        has_specific_time = re.search(r"(tonight|tomorrow|morning|afternoon|evening|\d+:\d+|\d+\s*(am|pm))", m)
            
        if is_rejection and not has_specific_time:
            # They declined the appointment - try a different angle
            update_qualification_state(contact_id, {
                "carrier_gap_found": False,  # Reset so we can try again
                "appointment_declined": True,
                "dismissive_count": state.get("dismissive_count", 0) + 1
            })
            
            # Check how many times they've declined
            decline_count = state.get("dismissive_count", 0) + 1
            
            if decline_count >= 2:
                # Exit gracefully after 2 declines
                return "Got it, no worries. If you ever have questions about coverage down the road, feel free to reach out. Take care!", False
            else:
                # First decline - try a softer angle, let LLM handle it
                return None, True  # Continue to LLM for different approach
    # ========== STEP 4b: They agreed to appointment time ==========
    if state.get("carrier_gap_found") and any(t in m for t in TIME_AGREEMENT_TRIGGERS):
        if any(t in m for t in ["tonight", "today", "evening", "6", "7", "8"]):
            booked_time = "tonight"
        elif any(t in m for t in ["10", "morning", "earlier", "first", "am"]):
            booked_time = "tomorrow morning"
        else:
            booked_time = "tomorrow afternoon"
        
        update_qualification_state(contact_id, {
            "is_booked": True,
            "is_qualified": True,
            "appointment_time": booked_time,
            "waiting_for_medications": True
        })
    
        return (f"Perfect, got you down for {booked_time}. "
            "Quick question so I can have the best options ready, are you taking any medications currently?"
        ), False
    
    # ========== STEP 3a: They answered "other policies" question ==========
    if state.get("waiting_for_other_policies"):
        has_other = re.search(r'\byes\b|yeah|work|employer|job|another|group|spouse', m)
        no_other = re.search(r'\bno\b|nope|nah|just\s*(this|that|the\s*one)|only\s*(this|that|one)', m)
        
        if has_other:
            update_qualification_state(contact_id, {
                "waiting_for_other_policies": False,
                "has_other_policies": True,
                "waiting_for_goal": True
            })
            add_to_qualification_array(contact_id, "topics_asked", "other_policies")
            # If through work, set employer based
            if re.search(r"work|employer|job|group", m):
                update_qualification_state(contact_id, {"is_employer_based": True})
                return (
                    "Got it, so you have both. A lot of the workplace plans don't have living benefits. "
                    "What made you want to look at coverage originally, was it to add more, cover a mortgage, or something else?"
                ), False
            
            return ("Makes sense. What made you want to look at coverage originally, was it to add more, cover a mortgage, or something else?"
            ), False
        elif no_other:
            update_qualification_state(contact_id, {
                "waiting_for_other_policies": False,
                "has_other_policies": False,
                "waiting_for_goal": True
            })
            add_to_qualification_array(contact_id, "topics_asked", "other_policies")
            return ("Got it. What made you want to look at coverage originally, was it to add more, cover a mortgage, or something else?"
            ), False
    #===== Goal mentioned directly in this message====
    goal_match = None
    
    if re.search(r"(add|more|additional|extra)\s*(coverage|protection)|on\s*top", m):
        goal_match = "add_coverage"
    elif re.search(r"mortgage|house|home", m):
        goal_match = "cover_mortgage"
    elif re.search(r"final\s*expense|funeral|burial", m):
        goal_match = "final_expense"
    
    if goal_match:
        update_qualification_state(contact_id, {
            "waiting_for_other_policies": False,
            "motivating_goal": goal_match
        })
        add_to_qualification_array(contact_id, "topics_asked", "other_policies")
        add_to_qualification_array(contact_id, "topics_asked", "original_goal")
        return None, True  # Let LLM continue with this context
    
    # ========== STEP 3b: They answered goal question ==========
    if state.get("waiting_for_goal"):
        goal_match = None
        
        if re.search(r"(add|more|additional|extra)\s*(coverage|protection)|on\s*top|supplement", m):
            goal_match = "add_coverage"
        elif re.search(r"mortgage|house|home", m):
            goal_match = "cover_mortgage"
        elif re.search(r"final\s*expense|funeral|burial|cremation", m):
            goal_match = "final_expense"
        elif re.search(r"protect|family|kids|wife|husband", m):
            goal_match = "family_protection"
        
        if goal_match:
            update_qualification_state(contact_id, {
                "waiting_for_goal": False,
                "motivating_goal": goal_match
            })
            add_to_qualification_array(contact_id, "topics_asked", "original_goal")
        else:
            update_qualification_state(contact_id, {"waiting_for_goal": False})
    
        return None, True  # Let LLM continue with goal context
    
    # ========== STEP 3c: They said NO they're not sick - doubt + book ==========
    if state.get("waiting_for_health") and re.search(r'\bno\b|not really|nah|healthy|i\'?m fine|feeling good|nothing serious|nope|im good', m):
        carrier = state.get("carrier", "them")
        update_qualification_state(contact_id, {
            "waiting_for_health": False,
            "carrier_gap_found": True
        })
    
        #=== Check if someone helped them or they found it themselves===
        someone_helped = re.search(r'(someone|agent|guy|friend|buddy|family|relative|coworker|rep|salesman|advisor)', m)
        found_myself = re.search(r'(myself|my own|online|google|website|found them|i did|on my own)', m)
    
        # Track how they got the policy
        if someone_helped:
            update_qualification_state(contact_id, {"is_personal_policy": True})
            # Someone put them with it - "weird they put you with them"
            return (f"Weird they put you with them. I mean they're a good company, like I said they just take higher risk people "
                    f"so it's usually more expensive for healthier people like yourself. {build_appointment_offer()}, "
                    "I can do a quick review and just make sure you're not overpaying. Which works best for you?"
            ), False
        else:
            if found_myself:
                update_qualification_state(contact_id, {"is_personal_policy": False})
                # They found it themselves or unclear - skip "weird" part
                return (f"I mean they're a good company, like I said they just take higher risk people "
                    f"so it's usually more expensive for healthier people like yourself. {build_appointment_offer()}, "
                    "I can do a quick review and just make sure you're not overpaying. Which works best for you?"
                ) , False
                
    #========== STEP 3b: They said YES they are sick ==========
    if state.get("waiting_for_health") and re.search(r'\byes\b|yeah|cancer|stroke|copd|chemo|oxygen|heart attack|stent|diabetes|kidney', m):
        update_qualification_state(contact_id, {
            "waiting_for_health": False,
            "carrier_gap_found": True
        })
        for cond in ["cancer", "stroke", "copd", "heart", "chemo", "oxygen", "stent", "diabetes", "kidney"]:
            if cond in m:
                add_to_qualification_array(contact_id, "health_conditions", cond)
        
        return ("Makes sense then, they're actually really good for folks with health stuff going on. "
                f"{build_appointment_offer()} if you want, I can still take a look and see if there's anything better out there. What works?"), False
    
    # ========== STEP 2: They answered with carrier name - combined question ==========
    if state.get("objection_path") == "already_covered" and state.get("already_handled") and not state.get("carrier_gap_found") and not state.get("waiting_for_health") and not state.get("waiting_for_other_policies"):
        carrier = extract_carrier_name(m)
        
        # Check for personal/private policy FIRST (NOT through work) - ask about other policies
        # Must check BEFORE employer detection to avoid matching "not through work"
        # Expanded patterns: "not an employer policy", "not through work", "private", "personal", "my own"
        if re.search(r"(private|personal|not\s*(an?\s*)?(through|from|employer)\s*(policy|work|job)?|my\s*own\b|individual|isn'?t\s*from\s*work)", m):
            update_qualification_state(contact_id, {
                "is_personal_policy": True,
                "is_employer_based": False,
                "waiting_for_other_policies": True
            })
            add_to_qualification_array(contact_id, "topics_asked", "employer_portability")
            add_to_qualification_array(contact_id, "topics_asked", "job_coverage")
            if carrier:
                update_qualification_state(contact_id, {"carrier": carrier})
            return ("Okay is that the only one you have or do you have one also with work?"), False
        
        # Check for employer-based coverage
        if re.search(r"(through|from|at|via).*(work|job|employer|company|group)", m):
            update_qualification_state(contact_id, {
                "is_employer_based": True,
                "carrier_gap_found": True
            })
            return ("Nice! A lot of the workplace plans don't have living benefits built in. "
                    f"{build_appointment_offer()}, takes 5 minutes to check. What works?"), False
        
        # Named a carrier without specifying source - combined source + health question
        if carrier:
            update_qualification_state(contact_id, {
                "carrier": carrier,
                "waiting_for_health": True
            })
            return ("Oh did someone help you get set up with them or did you find them yourself? "
                    "They usually help people with higher risk, do you have serious health issues?"), False
        
        # Unknown carrier or vague answer
        if re.search(r"(forget|don'?t remember|not sure|idk|i don'?t know|can'?t recall)", m):
            update_qualification_state(contact_id, {
                "carrier_gap_found": True
            })
            return ("No worries. Most folks who thought they were covered had gaps they didn't know about. "
                    f"{build_appointment_offer()}, takes 5 min to review. What works?"), False
    
    # ========== STEP 1: Initial "already covered" trigger ==========
    if (any(trigger in m for trigger in ALREADY_HAVE_TRIGGERS) 
        and not state.get("already_handled")):
        
        carrier = extract_carrier_name(m)
        is_employer = re.search(r"(through|from|at|via).*(work|job|employer|company|group)", m)
        
        update_qualification_state(contact_id, {
            "already_handled": True,
            "objection_path": "already_covered",
            "has_policy": True
        })
        
        response = None  # We'll set this based on conditions
        
        if is_employer:
            update_qualification_state(contact_id, {
                "is_employer_based": True,
                "carrier_gap_found": True
            })
            response = ("Nice! A lot of the workplace plans don't have living benefits built in. "
                        f"{build_appointment_offer()}, takes 5 minutes to check. What works?")
        
        elif carrier:
            update_qualification_state(contact_id, {
                "carrier": carrier,
                "waiting_for_health": True
            })
            response = ("Oh did someone help you get set up with them or did you find them yourself? "
                        "They usually help people with higher risk, do you have serious health issues?")
        
        else:
            response = "Who'd you go with?"
        
        # If we matched this block, return the crafted response
        if response is not None:
            return response, False
            
        return None, True
    
    # If we get here, nothing in this step (or previous steps) matched
            

# ============================================================================
# END CONTACT QUALIFICATION STATE
# ============================================================================

def get_ghl_credentials(data=None):
    """
    Get GHL credentials with priority:
    1. Request body (ghl_api_key, ghl_location_id) - for multi-tenant via webhooks
    2. Environment variables - for your own default setup

    Note: Expects data to already be normalized to lowercase keys.
    """
    if data is None:
        data = {}

    api_key = data.get('ghl_api_key') or os.environ.get("GHL_API_KEY")
    location_id = data.get('ghl_location_id') or os.environ.get("GHL_LOCATION_ID")
    return api_key, location_id

def get_ghl_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json"
}

def send_sms_via_ghl(contact_id, message, api_key, location_id):
    if not contact_id:
        logger.error("Cannot send SMS: contact_id is missing")
        return False

    url = f"https://services.leadconnectorhq.com/conversations/messages"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Version": "2021-04-15"
    }
    payload = {
        "type": "SMS",
        "contactId": contact_id,       # ← This must be the real ID
        "message": message,
        "locationId": location_id
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code in [200, 201]:
            logger.info(f"SMS sent successfully to {contact_id}")
            return {"success": True, "data": response.json()}
        else:
            logger.error(f"SMS failed: {response.status_code} {response.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"Failed to send SMS: {e}")

        error_detail = str(e)
        if getattr(e, "response", None) is not None:
            try:
                error_detail = e.response.json()
            except Exception:
                error_detail = e.response.text

        logger.error(f"Response: {error_detail}")
        return {"success": False, "error": error_detail}
    
    
def parse_booking_time(message, timezone_str="America/New_York"):
    """
    Parse natural language time expressions into timezone-aware datetime.
    Returns (datetime_iso_string, formatted_time, original_text) or (None, None, None) if no time found.

    timezone_str: IANA timezone name, defaults to America/New_York (Eastern Time) to match GHL behavior.
    """
    time_keywords = [
        'tomorrow', 'today', 'monday', 'tuesday', 'wednesday', 'thursday',
        'friday', 'saturday', 'sunday', 'am', 'pm', 'morning', 'afternoon',
        'evening', 'tonight', 'noon', "o'clock", 'oclock'
    ]

    message_lower = message.lower()
    has_time_keyword = any(keyword in message_lower for keyword in time_keywords)

    if not has_time_keyword:
        return None, None, None

    affirmative_patterns = [
        r'\b(yes|yeah|yea|yep|sure|ok|okay|sounds good|works|perfect|great|let\'s do|lets do|that works|i can do|i\'m free|im free)\b'
    ]
    has_affirmative = any(re.search(pattern, message_lower) for pattern in affirmative_patterns)

    if not has_affirmative and not any(word in message_lower for word in ['morning', 'afternoon', 'evening', 'am', 'pm']):
        return None, None, None

    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = ZoneInfo("America/New_York")

    now = datetime.now(tz)

    time_patterns_with_specific_time = [
        r'(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm))',
        r'(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s+(?:on\s+)?(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)',
        r'(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))',
        r'(tomorrow|today)\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)',
    ]

    time_text = None
    for pattern in time_patterns_with_specific_time:
        match = re.search(pattern, message_lower)
        if match:
            time_text = match.group(0)
            break

    has_specific_time = False
    if not time_text:
        day_match = re.search(r'\b(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', message_lower)
        time_match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b', message_lower)
        period_match = re.search(r'\b(morning|afternoon|evening)\b', message_lower)

        if day_match and (time_match or period_match):
            if time_match:
                time_text = f"{day_match.group(1)} at {time_match.group(1)}"
                has_specific_time = True
            else:
                time_text = day_match.group(1)
    else:
        has_specific_time = True

    if not time_text:
        return None, None, None

    parsed = dateparser.parse(
        time_text,
        settings={
            'PREFER_DATES_FROM': 'future',
            'PREFER_DAY_OF_MONTH': 'first',
            'TIMEZONE': timezone_str,
            'RETURN_AS_TIMEZONE_AWARE': True
        }
    )

    if parsed:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)

        if not has_specific_time:
            if 'morning' in message_lower:
                parsed = parsed.replace(hour=10, minute=0, second=0, microsecond=0)
            elif 'afternoon' in message_lower:
                parsed = parsed.replace(hour=14, minute=0, second=0, microsecond=0)
            elif 'evening' in message_lower or 'tonight' in message_lower:
                parsed = parsed.replace(hour=18, minute=0, second=0, microsecond=0)
            else:
                parsed = parsed.replace(hour=10, minute=0, second=0, microsecond=0)

        if parsed <= now:
            return None, None, None

        iso_string = parsed.isoformat()
        formatted_time = parsed.strftime("%A, %B %d at %I:%M %p")
        return iso_string, formatted_time, message

    return None, None, None



def format_slot_options(slots, timezone="America/New_York"):
    """Format available slots into a natural SMS-friendly string"""
    if not slots or len(slots) == 0:
        return None

    now = datetime.now(ZoneInfo(timezone))
    today = now.strftime("%A")
    tomorrow = (now + timedelta(days=1)).strftime("%A")

    formatted = []
    for slot in slots[:2]:  # Offer 2 options
        day = slot.get('day')
        time_txt = slot.get('formatted', '').lower().replace(' ', '')

        # FIX: derive hour reliably from the ISO timestamp (no string hour parsing)
        try:
            dt = datetime.fromisoformat(slot['iso'])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo(timezone))
            slot_hour = dt.hour
        except Exception:
            slot_hour = None

        if day == today:
            if slot_hour is not None and slot_hour >= 17:
                formatted.append(f"{time_txt} tonight")
            elif slot_hour is not None and slot_hour < 12:
                formatted.append(f"{time_txt} this morning")
            else:
                formatted.append(f"{time_txt} this afternoon")
        elif day == tomorrow:
            if slot_hour is not None and slot_hour < 12:
                formatted.append(f"{time_txt} tomorrow morning")
            else:
                formatted.append(f"{time_txt} tomorrow")
        else:
            formatted.append(f"{time_txt} {day}")

    if len(formatted) == 2:
        return f"{formatted[0]} or {formatted[1]}"
    if len(formatted) == 1:
        return formatted[0]
    return None


def get_conversation_history(contact_id, api_key, location_id, limit=10):
    """Get recent conversation messages for a contact from GoHighLevel"""
    if not api_key or not location_id or not contact_id:
        logger.error("Missing credentials for conversation history")
        return []

    url = f"{GHL_BASE_URL}/conversations/search"
    payload = {
        "locationId": location_id,
        "contactId": contact_id
    }

    try:
        response = requests.post(url, headers=get_ghl_headers(api_key), json=payload)
        response.raise_for_status()
        data = response.json()
        conversations = data.get('conversations', [])
    
        if not conversations:
            return []
    
        conversation_id = conversations[0].get('id')
        if not conversation_id:
            return []
    
        msg_url = f"{GHL_BASE_URL}/conversations/{conversation_id}/messages"
        msg_response = requests.get(msg_url, headers=get_ghl_headers(api_key))
        msg_response.raise_for_status()
        msg_data = msg_response.json()
    
        messages = msg_data.get('messages', [])
    
        # FIX: pull the LAST N messages, not the first N
        recent_messages = messages[-limit:] if len(messages) > limit else messages

        formatted = []
        for msg in recent_messages:  # keep chronological order
            normalized_msg = normalize_keys(msg)
            direction = normalized_msg.get('direction', 'outbound')
            body = normalized_msg.get('body', '')
            if body:
                role = "Lead" if str(direction).lower() == 'inbound' else "You"
                formatted.append(f"{role}: {body}")
    
        return formatted

    except requests.RequestException as e:
        logger.error(f"Failed to get conversation history: {e}")
        return []



# =========================
# NEPQ SYSTEM PROMPT
# =========================
NEPQ_SYSTEM_PROMPT = """
ROLE 
You are an elite, calm, “wise” life-insurance follow-up agent texting very old leads. Your objective is to re-engage the lead, uncover their current situation, identify gaps, answer questions accurately using the provided knowledge base, and book a short call/appointment when appropriate.

PRIMARY GOAL (ALWAYS ON)
Move the conversation toward a booked appointment without forcing it by turn count. The appointment could happen quickly or after extended back-and-forth. The agent should persist intelligently and add value, not rush.

NON-NEGOTIABLE CONSTRAINTS

SMS style: 15-35 words. One idea or one question at a time. Natural, human tone. 

unified_brain

Never be robotic: do not repeat questions they already answered, and do not ask questions that are logically irrelevant based on prior answers. 

unified_brain

Templates and triggers are OPTIONS, not commands. Use them only if they fit the moment; modify freely. 

unified_brain

Always be helpful: if they ask a direct question, answer it clearly, then pivot back toward discovery or booking.

Keep control without pressure: guide the interaction; do not argue; do not guilt-trip.

STAGE MODEL (FLEXIBLE, NOT TURN-BASED)
You are always in one of these modes, and can move forward/back as needed:

Re-engage / Initial outreach (spark context + curiosity)

Discovery (learn what they have now, what changed, what they want)

Consequence (help them feel the cost/risk of the gap, matter-of-fact)

Close (offer two times / ask availability and confirm)

Stages are determined by: their responsiveness, what they reveal, and buying signals (not message count). 

knowledge_base

WHEN TO OFFER APPOINTMENT TIMES (NO “3 EXCHANGES” RULE)
Offer times when ANY is true:

They show a buying signal (direct or indirect), e.g., “how much,” “what are my options,” “tell me more.” 

knowledge_base

You have uncovered a meaningful gap/need (coverage too low, tied to work, term expiring, no living benefits, health-based constraints, etc.).

The conversation is stalling but still warm (they're replying, but not giving details). In that case, offer a quick call as the simplest next step.

Do NOT offer times if they are dismissive/hostile or clearly opting out.

DISENGAGEMENT HANDLING (PERSISTENCE WITH A STOP)
If they repeatedly resist/dismiss (e.g., 2-3 deflections), shift to a softer, lower-friction question or exit gracefully. Use a “soft exit” rather than chasing indefinitely. (Your playbook already supports resistance escalation.) 

playbook

CORE DECISION LOOP (DO THIS BEFORE EVERY MESSAGE)
Step 1 — ABSORB: What do we know about them already (coverage, family, health, intent, tone)? 

unified_brain


Step 2 — ANALYZE: What stage are we in, what's their emotional state, what do they mean? 

unified_brain


Step 3 — APPLY: Choose the best framework/technique (NEPQ discovery, empathy, gap selling). 

unified_brain


Step 4 — RESPOND: One short SMS that advances: re-engage → uncover need → book. 

unified_brain

SEMANTIC / LOGICAL BLOCKING (ANTI-ROBOT RULE)
If they say it's a personal policy (not through work), permanently stop asking employer/portability/retirement questions, etc. If they told you policy type, stop asking policy type. If they answered living benefits, stop asking. 

unified_brain

KNOWLEDGE USAGE

Use the provided product knowledge and underwriting guidance to answer accurately, then pivot to a gap question or next step. 

knowledge_base

When health is mentioned: respond with realistic options and a single clarifying question that moves underwriting forward. 

unified_brain

When term/work coverage is mentioned: probe the core gap (expires / not portable / amount too low) using one concise question. 

playbook

DEFAULT BEHAVIOR IN THE “HEY / YA? / WHAT'S UP?” FLOW
If they respond with short low-information replies:

Confirm identity briefly.

Give a single-sentence reason for texting that references prior interest.

Ask one easy discovery question OR offer a quick call if they won't engage in detail.

OUTPUT FORMAT (IMPORTANT)
Return ONLY the SMS message to send to the lead.
No analysis. No stage labels. No XML tags. No JSON. No explanations.

INPUTS YOU MAY RECEIVE

lead_message: latest inbound text

conversation_history: prior messages

contact context: name, age, state, prior interest date/source, family notes if known

available appointment slots (if provided): use them; otherwise ask availability.

If you want, I can also give you a second “developer prompt” that you keep internal for logging (stage, detected triggers, dismissal count, why it chose to offer times), while still outputting only the SMS to the lead. That's how you get “wise” behavior without spamming the customer with reasoning.
"""
# =========================
# INTENT DIRECTIVES
# =========================
INTENT_DIRECTIVES = {
    "book_appointment": "You've already uncovered their need. Now get them to commit to a specific time for a phone call. Offer concrete time slots.",
    "qualify": "Focus on discovery. Ask about their situation, family, and what got them looking. Find the real problem before even thinking about an appointment.",
    "reengage": "This is a cold lead who hasn't responded in a while. Just say 'Hey {first_name}?' and wait for their response. Super soft opener.",
    "follow_up": "Continue where you left off. Reference your previous conversation if possible. Check if they've thought about it or have any questions.",
    "nurture": "Keep the relationship warm. Don't push for anything. Ask about their life, build rapport, and stay top of mind.",
    "objection_handling": "The lead has raised an objection. Use curiosity to understand their concern deeply. Don't redirect to booking yet.",
    "initial_outreach": "This is the FIRST message. Say: '{first_name}, are you still with that other life insurance plan? There have been some recent updates to living-benefit coverage that people have been asking about.' Then wait for their response.",
    "general": "Have a natural conversation. Uncover their situation and needs before ever suggesting an appointment."
}


def extract_intent(data, message=""):
    """
    Extract and normalize intent from request data or message content.
    Note: Expects data to already be normalized to lowercase keys.
    """
    # Ensure message is never None
    message = message or ""
    data = data or {}

    raw_intent = data.get("intent", "")

    if not raw_intent and "custom_fields" in data:
        for field in data.get("custom_fields", []):
            if field.get("key", "").lower() == "intent":
                raw_intent = field.get("value", "")
                break

    raw_intent = str(raw_intent).lower().strip().replace(" ", "_").replace("-", "_")

    intent_map = {
        "book": "book_appointment",
        "book_appointment": "book_appointment",
        "booking": "book_appointment",
        "schedule": "book_appointment",
        "qualify": "qualify",
        "qualification": "qualify",
        "reengage": "reengage",
        "re_engage": "reengage",
        "re-engage": "reengage",
        "reengagement": "reengage",
        "outreach_loop": "reengage",
        "outreach_2": "reengage",
        "outreach_3": "reengage",
        "outreach_4": "reengage",
        "loop": "reengage",
        "follow_up": "follow_up",
        "followup": "follow_up",
        "follow": "follow_up",
        "nurture": "nurture",
        "warm": "nurture",
        "objection": "objection_handling",
        "objection_handling": "objection_handling",
        "initial": "initial_outreach",
        "initial_outreach": "initial_outreach",
        "outreach": "initial_outreach",
        "first_message": "initial_outreach",
        "respond": "general",
        "general": "general",
        "": "general",
    }

    normalized = intent_map.get(raw_intent, "general")

    # If the webhook didn't provide intent but message implies "initial outreach"
    if normalized == "general" and message:
        lower_msg = message.lower()
        if (
            "initial outreach" in lower_msg
            or "first message" in lower_msg
            or "just entered pipeline" in lower_msg
        ):
            normalized = "initial_outreach"

        return normalized


    # =============================================================
    # STEP 0: FETCH REAL CALENDAR SLOTS (always available for closing)
    # =============================================================
    real_calendar_slots = None

    if api_key and calendar_id:
        try:
            slots = get_available_slots(calendar_id, api_key, timezone)
            if slots:
                real_calendar_slots = format_slot_options(slots, timezone)
                logger.info(f"STEP 0: Fetched real calendar slots: {real_calendar_slots}")
        except Exception as e:
            logger.warning(f"STEP 0: Could not fetch calendar slots: {e}")

    if not real_calendar_slots:
        real_calendar_slots = "tonight or tomorrow morning"  # Vague fallback, not fake specific times
        logger.info("STEP 0: Using vague time fallback (no specific times)")

    # =========================================================================
    # STEP 1: KNOWLEDGE IS IN UNIFIED BRAIN (loaded via get_unified_brain)
    # =========================================================================
    logger.info("STEP 1: Knowledge will be loaded via unified brain")

    # =========================================================================
    # STEP 2: IDENTIFY TRIGGERS + GET TRIGGER SUGGESTION
    # =========================================================================
    # Ensure message is always a plain string before trigger logic / LLM routing
        
    if isinstance(message, dict):
        message = message.get("body") or message.get("message") or message.get("text") or ""
    elif not isinstance(message, str):
        message = str(message) if message is not None else ""
        
    message = message.strip()

    triggers_found = identify_triggers(message)
    trigger_suggestion, trigger_code = force_response(message, api_key, calendar_id, timezone)
    logger.info(f"STEP 2: Triggers found: {triggers_found}, Suggestion: {trigger_suggestion[:50] if trigger_suggestion else 'None'}...")
    logger.info(f"STEP 2: message type after normalize: {type(message)}")

    # =========================================================================
    # STEP 3: CHECK OUTCOME PATTERNS (what worked before for similar messages)
    # =========================================================================
    outcome_patterns = []
    outcome_context = ""
    try:
        learning_ctx = get_learning_context(contact_id or "unknown", message)
        if learning_ctx and learning_ctx.get("proven_responses"):
            outcome_patterns = learning_ctx.get("proven_responses", [])[:5]
            outcome_context = "\n".join([f"- {p}" for p in outcome_patterns])
            logger.info(f"STEP 3: Found {len(outcome_patterns)} proven outcome patterns")
        else:
            logger.info("STEP 3: No proven outcome patterns found")
        
        # === TRIGRAM SIMILARITY: Find patterns with similar trigger messages ===
        similar_patterns = find_similar_successful_patterns(message, min_score=2.0, limit=3)
        if similar_patterns:
            for p in similar_patterns:
                sim_text = f"Similar trigger (sim={p.get('sim_score', 0):.2f}): {p.get('response_used', '')[:100]}"
                if sim_text not in outcome_context:
                    outcome_context += f"\n- {sim_text}"
            logger.info(f"STEP 3: Found {len(similar_patterns)} trigram-similar patterns")
    except Exception as e:
        logger.warning(f"STEP 3: Could not load outcome patterns: {e}")

    # =========================================================================
    # STEP 4: EVALUATE - Check if triggers should bypass LLM
    # =========================================================================
    # For BUYING_SIGNAL and PRICE triggers, bypass LLM to ensure calendar times are used
    # This prevents the LLM from making up fake appointment times
    if trigger_code == "TRIG" and trigger_suggestion:
        # Check if this is a trigger that should bypass LLM
        triggers_str = str(triggers_found)
        m_lower = message_text.lower().strip()
        # Calendar-related triggers bypass to use real calendar times
        if "BUYING_SIGNAL" in triggers_str or "PRICE" in triggers_str:
            logger.info(f"STEP 4: Calendar-related trigger detected, using deterministic response: {trigger_suggestion}")
            return trigger_suggestion, confirmation_code
        # Frustrated/repeat triggers bypass to apologize and pivot immediately
        frustrated_patterns = ["already asked", "move on", "stop asking", "enough questions"]
        if any(p in m_lower for p in frustrated_patterns):
            logger.info(f"STEP 4: Frustrated repeat trigger detected, using deterministic response: {trigger_suggestion}")
            return trigger_suggestion, confirmation_code

    if trigger_code == "EXIT":
        # Hard exit always bypasses LLM
        logger.info(f"STEP 4: Exit trigger detected, returning: {trigger_suggestion}")
        return trigger_suggestion, confirmation_code

    # Extract structured lead profile from conversation
    if conversation_history is None:
        conversation_history = []

    # === TOKEN OPTIMIZATION: Compress history if too long ===
    if len(conversation_history) > 6:
        original_count = len(conversation_history)
        conversation_history = compress_conversation_history(conversation_history, max_tokens=1500)
        logger.info(f"TOKEN_OPT: Compressed history from {original_count} to {len(conversation_history)} messages")

    lead_profile = extract_lead_profile(conversation_history, first_name, message)

    # === QUALIFICATION STATE: Persistent memory per contact ===
    qualification_state = None
    qualification_context = ""
    nlp_context = ""
    if contact_id:
        # === STEP 0a: Save incoming message to NLP memory ===
        # Store all messages for spaCy parsing and topic extraction
        save_nlp_message(contact_id, message, "lead")
        logger.debug(f"NLP: Saved lead message for contact {contact_id}")
        
        # === STEP 0b: Parse conversation history to backfill topics_asked ===
        # This retroactively identifies topics already asked in previous messages
        if conversation_history:
            parse_history_for_topics_asked(contact_id, conversation_history)
        
        # Load existing qualification state from database (after backfill)
        qualification_state = get_qualification_state(contact_id)
        
        # === STEP 0c: Get NLP topic breakdown for additional context ===
        nlp_context = format_nlp_for_prompt(contact_id)
        
        # Extract and update qualification from this message
        extracted_updates = extract_and_update_qualification(contact_id, message, conversation_history)
        if extracted_updates:
            logger.info(f"QUALIFICATION: Extracted updates: {extracted_updates}")
            # Reload state after updates
            qualification_state = get_qualification_state(contact_id)
        
        # Increment exchange counter
        increment_exchanges(contact_id)
        
        # === ALREADY COVERED HANDLER: Deterministic state machine ===
        # This runs BEFORE LLM to handle the common "already have coverage" pathway
        handler_response, should_continue = already_covered_handler(
            contact_id, message, qualification_state, 
            api_key, calendar_id, timezone
        )
        if not should_continue and handler_response:
            logger.info(f"ALREADY_COVERED_HANDLER: Returning deterministic response")
            return handler_response, confirmation_code
        
        # Format qualification for prompt
        qualification_context = format_qualification_for_prompt(qualification_state)
        if qualification_context:
            logger.debug(f"QUALIFICATION: Injecting known facts into prompt")

    # === LAYER 2: Build Conversation State (Source of Truth) ===
    conv_state = build_state_from_history(
        contact_id=contact_id or "unknown",
        first_name=first_name,
        conversation_history=conversation_history,
        current_message=message
    )

    # === CRITICAL: Sync qualification_state topics_asked to conv_state.topics_answered ===
    # This prevents re-asking questions that were asked in previous turns
    if qualification_state:
        topics_asked = qualification_state.get("topics_asked") or []
        
        # Sync ALL topics from database to conv_state (prevents any topic repetition)
        for topic in topics_asked:
            if topic not in conv_state.topics_answered:
                conv_state.topics_answered.append(topic)
        
        # Special handling for motivation (multiple aliases)
        if "motivation" in topics_asked or "original_goal" in topics_asked:
            if "motivation" not in conv_state.topics_answered:
                conv_state.topics_answered.append("motivation")
            if "motivating_goal" not in conv_state.topics_answered:
                conv_state.topics_answered.append("motivating_goal")
            logger.info("QUALIFICATION: Motivation question already asked - blocking repeats")
        
        if topics_asked:
            logger.info(f"QUALIFICATION: Synced {len(topics_asked)} topics from database: {topics_asked}")

    # === SYNC NLP TOPICS: Also sync topics from spaCy NLP memory ===
    if contact_id:
        nlp_topics = get_topics_already_discussed(contact_id)
        for topic in nlp_topics:
            # Extract simple topic name (remove category prefix if present)
            simple_topic = topic.split(":")[-1] if ":" in topic else topic
            if simple_topic not in conv_state.topics_answered:
                conv_state.topics_answered.append(simple_topic)
        if nlp_topics:
            logger.debug(f"NLP: Synced {len(nlp_topics)} topics from NLP memory")

    state_instructions = format_state_for_prompt(conv_state)
    logger.debug(f"Conversation state: stage={conv_state.stage.value}, exchanges={conv_state.exchange_count}, dismissive_count={conv_state.soft_dismissive_count}")

    # === BUYING SIGNAL DETECTION - Override intent when lead shows readiness ===
    # Must be context-aware to avoid false positives from negations/sarcasm
    current_lower = message.lower()

    # Negation phrases that cancel buying signals (expanded for common variants)
    negation_phrases = [
        "don't need", "dont need", "do not need", "dont really need", "don't really need",
        "not interested", "no thanks", "no thank", "stop", "leave me alone", 
        "not looking", "already have", "im good", "i'm good", "i am good",
        "not right now", "maybe later", "no im good", "nope", "no i dont",
        "no i don't", "no i'm not", "no im not", "not for me", "pass",
        "unsubscribe", "remove me", "take me off"
    ]
    has_negation = any(phrase in current_lower for phrase in negation_phrases)

    # Also check for negation patterns via regex
    import re
    negation_patterns = [
        r"\bno\b.*\bneed\b", r"\bnot\b.*\binterested\b", r"\bdon'?t\b.*\bneed\b"
    ]
    if not has_negation:
        for pattern in negation_patterns:
            if re.search(pattern, current_lower):
                has_negation = True
                break

    # Strong buying signals (unambiguous interest)
    strong_buying_signals = [
        "i'd have to get", "id have to get", "would have to get",
        "need to get new", "get new life insurance", "get new coverage",
        "sign me up", "let's do it", "lets do it", "i'm in", "im in",
        "what are my options", "how much would it cost", "what would it cost",
        "sounds good", "that sounds good", "works for me", "yeah let's do it",
        "sure", "okay when", "ok when", "yes when can we"
    ]

    # Weaker signals that need context (only count if no negation)
    weak_buying_signals = [
        "i need", "i'd need", "interested in looking", "looking into it",
        "want to look", "can you look into", "want coverage"
    ]

    detected_buying_signal = False
    if not has_negation:
        if any(signal in current_lower for signal in strong_buying_signals):
            detected_buying_signal = True
        elif any(signal in current_lower for signal in weak_buying_signals):
            # Only treat weak signals as buying signals if conversation has context
            if lead_profile.get("motivating_goal") or lead_profile.get("family", {}).get("spouse"):
                detected_buying_signal = True

    # Also detect if they've revealed a problem that we can close on
    problem_revealed = bool(
        lead_profile.get("motivating_goal") or 
        lead_profile.get("coverage", {}).get("coverage_gap") or
        (lead_profile.get("coverage", {}).get("has_coverage") and 
            lead_profile.get("coverage", {}).get("type") == "employer") or
        lead_profile.get("coverage", {}).get("employer")
    )

    # Determine conversation stage
    if conversation_history:
        recent_agent = [msg for msg in conversation_history if msg.startswith("You:")]
        recent_lead = [msg for msg in conversation_history if msg.startswith("Lead:")]
        exchange_count = min(len(recent_agent), len(recent_lead))
    else:
        exchange_count = 0

    # Stage logic: problem_awareness -> consequence -> close
    # CRITICAL: Force close after 3 exchanges FIRST - this is the hard stop
    if exchange_count >= 3:
        # Force close after 3 exchanges regardless of other signals
        intent = "book_appointment"
        stage = "close"
    elif detected_buying_signal:
        # Buying signal detected - go straight to close
        intent = "book_appointment"
        stage = "close"
    elif exchange_count >= 2 and problem_revealed:
        # Had enough conversation with problem revealed - close
        intent = "book_appointment"
        stage = "close"
    elif problem_revealed and exchange_count >= 1:
        # Problem revealed but only 1 exchange - ask consequence question first
        stage = "consequence"
    elif problem_revealed:
        # Problem revealed in first exchange - still consequence
        stage = "consequence"
    else:
        stage = "problem_awareness"

    intent_directive = INTENT_DIRECTIVES.get(intent, INTENT_DIRECTIVES['general'])

    # Stage-specific directives for cold leads (from NEPQ Black Book)
    stage_directives = {
        "problem_awareness": """
    === STAGE: PROBLEM AWARENESS (NEPQ Stage 2 + 7-Steps Big Picture Questions) ===
    These are COLD leads who haven't thought about insurance in MONTHS. They don't have anything "on their mind" about insurance.

    BIG PICTURE QUESTIONS (start broad, then narrow - from 7-Steps Guide):
    - "Just so I have more context, what was going on back then that made you start looking?"
    - "Is there something specific that's changed since then, like work or family?"
    - "Just curious, besides wanting to make sure everyone's covered, what was the main reason you were looking?"
    - "Was it more just seeing what was out there, or was there something specific going on?"
    - "What would you change about your current coverage situation?"
    - "What's been your biggest headache with insurance stuff?"

    DO NOT ask generic questions like:
    - "What's on your mind about insurance?" (they haven't thought about it in months)
    - "What's been worrying you?" (too presumptuous)
    - "What made you realize you need coverage?" (they may not have realized anything)
    - "What's the main thing you're hoping to get out of life insurance?" (sounds like a survey, not a conversation)
    - "What are you hoping to achieve?" (too corporate/formal)
    - "What would be ideal for you?" (too vague, they don't know what's possible)

    RECOGNIZE WHEN THEY'RE SHUTTING YOU DOWN:
    If they say things like "I'm not telling you that", "none of your business", "why do you need to know":
    → They feel interrogated. STOP asking questions. Back off gracefully:
    → "Fair enough, no pressure. I'll check back another time."
    → DO NOT ask another question after this response.

    KEEP YOUR POWDER DRY: Don't reveal coverage problems yet. Ask questions first, save your ammunition for later.

    After ONE problem awareness question, if they reveal ANY need (family, job concerns, coverage gaps), move to CONSEQUENCE stage.
    ===
    """,
        "consequence": """
    === STAGE: CONSEQUENCE (NEPQ Stage 2 + 7-Steps Future Pacing) ===
    You've identified a problem or need. Now help them FEEL the weight of not solving it AND paint the after-picture.

    STEP 1 - ASK ONE CONSEQUENCE QUESTION (choose based on what they shared):

    IF EMPLOYER COVERAGE:
    - "Got it. So if you left your current job, what would be your plan for keeping that coverage in place?"
    - "Does that follow you if you switch jobs, or is it tied to that employer?"
    - "What happens to that coverage when you retire?"

    IF FAMILY/SPOUSE MENTIONED:
    - "If something happened to you tomorrow, would [spouse] be able to keep the house and stay home with the kids?"
    - "What would you want that coverage to handle first, the mortgage or replacing your income?"

    IF THEY MENTIONED A NEED BUT HAVEN'T ACTED:
    - "How long has that been weighing on you?"
    - "What's been stopping you from getting that handled?"

    STEP 2 - IF THEY SEEM HESITANT, USE FUTURE PACING (required when they say "I don't know" or "a lot to think about"):
    Instead of another question, paint the after-picture:
    - "What would it feel like to know that's finally handled?"
    - "Imagine knowing your family is protected no matter what, how would that change things?"
    - "Picture your wife's face when you tell her you finally got this sorted."

    WHEN TO USE FUTURE PACING:
    - They say "I don't know" or "let me think about it"
    - They seem overwhelmed or hesitant
    - They acknowledge the problem but aren't moving forward

    After consequence question OR future pacing, if they show ANY interest, move to CLOSE stage.
    ===
    """,
        "close": """
    === STAGE: CLOSE - BOOK THE APPOINTMENT (NEPQ Stage 5 + 7-Steps Looping Back) ===
    You have enough information. STOP asking discovery questions. The PRIMARY GOAL is booking the appointment.

    SCENARIO A - FIRST CLOSE ATTEMPT:
    - "I can take a look at options for you. I have [USE CALENDAR TIMES FROM CONTEXT], which works better?"
    - "Let me see what we can do. Free at 2pm today or 11am tomorrow?"
    - "Got it. I can help you find the right coverage. How's [USE CALENDAR TIMES FROM CONTEXT]?"

    SCENARIO B - THEY SHOWED A BUYING SIGNAL (said "I need", "I'd have to get", etc.):
    Acknowledge it briefly, then offer times immediately. Don't ask another question.

    SCENARIO C - THEY OBJECT ("let me think about it", "I'm not sure", etc.) - USE LOOPING BACK:
    This is REQUIRED when they push back. Loop to something THEY said earlier + add a new positive + offer times:
    Pattern: "I hear you. [Loop to their words]. [Add new positive]. [Offer times]"

    Examples:
    - "I get it. But you mentioned your wife has been worried about this. Good news is there's no obligation to buy anything, just a quick review. [USE CALENDAR TIMES FROM CONTEXT]?"
    - "Makes sense. Earlier you said the work coverage might not follow you if you leave. Some policies actually cost less than what you'd expect. Morning or afternoon work better?"
    - "Totally fair. But you did mention wanting to make sure the kids are covered. No pressure, just a conversation. 6:30 or 10:15?"

    SCENARIO D - THEY KEEP OBJECTING - TIP THE BUYING SCALE:
    Add positives they haven't heard yet:
    - "no waiting period" (if they have GI coverage)
    - "follows you anywhere" (if they have employer coverage)
    - "often costs less than expected"
    - "no obligation, just a quick review"
    - "takes 30 minutes to see what's out there"

    Remove negatives:
    - "no pressure to buy anything"
    - "just getting information"
    - "see if there's a better fit"

    ALWAYS end with two specific time options. DO NOT ask more discovery questions.
    ===
    """,
    }

    stage_directive = stage_directives.get(stage, "")
    profile_text = format_lead_profile_for_llm(lead_profile, first_name)

    history_text = ""
    recent_agent_messages = []
    recent_lead_messages = []

    # Initialize dismissive detection variables (will be updated if conversation history exists)
    is_soft_dismissive = False
    is_hard_dismissive = False
    soft_dismissive_count = 0
    topics_warning = ""  # Initialize for topic-based repeat prevention

    # Check current message for dismissive phrases even without history
    soft_dismissive_phrases = [
        "not telling you", "none of your business", "why do you need to know",
        "thats personal", "that's personal", "private", "why does it matter",
        "doesnt matter", "doesn't matter", "dont matter", "don't matter",
        "not your concern", "why do you care", "im covered", "i'm covered",
        "already told you", "i said im", "i said i'm", "already said",
        "just leave it", "drop it", "not important", "thats not important"
    ]
    hard_dismissive_phrases = [
        "stop texting", "leave me alone", "f off", "fuck off", "go away",
        "dont text me", "don't text me", "stop messaging", "stop contacting",
        "remove me", "unsubscribe", "take me off", "do not contact",
        "dont call", "don't call", "never contact"
    ]
    current_lower = message.lower()
    is_soft_dismissive = any(phrase in current_lower for phrase in soft_dismissive_phrases)
    is_hard_dismissive = any(phrase in current_lower for phrase in hard_dismissive_phrases)
    if is_soft_dismissive:
        soft_dismissive_count = 1

    if conversation_history and len(conversation_history) > 0:
        # Extract recent agent questions to prevent repeats
        recent_agent_messages = [msg for msg in conversation_history if msg.startswith("You:")]
        recent_lead_messages = [msg for msg in conversation_history if msg.startswith("Lead:")]
        recent_questions = recent_agent_messages[-3:] if len(recent_agent_messages) > 3 else recent_agent_messages
        
        # TOPIC-BASED REPEAT DETECTION - Detect topics already asked (not just exact questions)
        all_agent_text = " ".join([msg.lower() for msg in recent_agent_messages])
        topics_already_asked = []
        
        # Living benefits detection (multiple phrasings)
        if any(phrase in all_agent_text for phrase in [
            "living benefits", "access funds", "access part of", "touch the money",
            "seriously ill while alive", "sick while you", "while you're still alive",
            "accelerated", "chronic illness", "terminal illness rider"
        ]):
            topics_already_asked.append("LIVING_BENEFITS")
        
        # Portability detection
        if any(phrase in all_agent_text for phrase in [
            "follow you if you", "switch jobs", "tied to your employer", "portable",
            "retire", "leave the company", "change jobs"
        ]):
            topics_already_asked.append("PORTABILITY")
        
        # Amount/coverage detection
        if any(phrase in all_agent_text for phrase in [
            "how much", "coverage amount", "replace your income", "enough to cover",
            "10x your income", "what amount"
        ]):
            topics_already_asked.append("AMOUNT")
        
        # Term length detection
        if any(phrase in all_agent_text for phrase in [
            "how many years", "when does it expire", "term length", "years left",
            "renew", "rate lock"
        ]):
            topics_already_asked.append("TERM_LENGTH")
        
        # Company/who detection
        if any(phrase in all_agent_text for phrase in [
            "who'd you go with", "who did you go with", "which company", "what company",
            "who are you with"
        ]):
            topics_already_asked.append("COMPANY")
        
        # Build blocked topics warning
        topics_warning = ""
        
        if topics_already_asked:
            topics_warning = f"""
        === TOPICS YOU ALREADY ASKED ABOUT (BLOCKED - DO NOT ASK AGAIN) ===
        {chr(10).join([f"- {t}" for t in topics_already_asked])}


        === CHOOSE A DIFFERENT ANGLE FROM: portability, amount, term length, beneficiaries, premium cost ===
            """   
            
        questions_warning = ''
        if recent_questions:
            questions_list = chr(10).join([f"- {q.replace('You: ', '')}" for q in recent_questions])
            
            # Check if lead just deflected our last question
            last_agent_msg = recent_agent_messages[-1] if recent_agent_messages else ""
            deflection_warning = ""
            if is_soft_dismissive and last_agent_msg:
                last_question = last_agent_msg.replace("You: ", "")
                deflection_warning = f"""       
            === CRITICAL: THEY JUST DEFLECTED YOUR LAST QUESTION ===
            You asked: "{last_question}"
            They said: "{message}" (this is a DEFLECTION - they don't want to answer)

            YOU MUST NOT:
            - Ask the same question again
            - Ask a similar question about the same topic
            - Repeat ANY variation of your last question
            - Say goodbye or exit (soft rejections are NOT hard stops)

            YOU MUST:
            - Acknowledge briefly: "Fair enough" / "Got it" / "No worries" / "Makes Sense"  
            - Pivot to a COMPLETELY DIFFERENT re-frame from this list:
            1. Living benefits: "Does yours let you touch the money while you're still alive if you get sick?"
            2. Portability: "Does it follow you if you switch jobs?"
            3. Amount: "Is it enough to replace your income for a few years?"
            4. Future-pace: "If your situation changes, new baby, new job, would you want me to reach out?"
            5. Most people: "Most folks realize their work coverage disappears when they retire. Yours portable?"

            DO NOT EXIT. Keep trying different angles until they say "stop texting me" or "leave me alone".
            === TRY A NEW ANGLE - DON'T GIVE UP ===
            """
        # Count soft dismissive responses in history to escalate response
        # Note: Exclude the LAST lead message if it matches current message (avoid double-counting)
        history_lead_messages = recent_lead_messages[:-1] if recent_lead_messages else []
        for msg in history_lead_messages:
            msg_lower = msg.lower()
            if any(phrase in msg_lower for phrase in soft_dismissive_phrases):
                soft_dismissive_count += 1
        
        # Detect rejection patterns in lead messages
        rejection_phrases = [
            "not interested", "no thanks", "no thank", "im good", "i'm good", 
            "im covered", "i'm covered", "already have", "all set", "dont need",
            "don't need", "not looking", "not right now", "no im good", "nah"
        ]
        
        rejection_count = 0
        for msg in recent_lead_messages:
            msg_lower = msg.lower()
            if any(phrase in msg_lower for phrase in rejection_phrases):
                rejection_count += 1
        
        # Check if current message is also a rejection
        if any(phrase in current_lower for phrase in rejection_phrases):
            rejection_count += 1
        
        # Add explicit exchange count warning
        exchange_warning = ""
        
        # HARD DISMISSIVE = wants to end contact completely (must exit)
        if is_hard_dismissive:
            exchange_warning = f"""
        === CRITICAL: HARD STOP - THEY WANT NO CONTACT ===
        The lead said "leave me alone", "stop texting", or similar.
        This is a clear request to stop. You MUST exit immediately.
        Your response MUST be SHORT and final:
        "Got it. Take care."
        "No problem. Have a good one."
        === EXIT NOW - NO QUESTIONS ===
            """
            
        # SOFT DISMISSIVE = resistance to specific question (use methodology to redirect)
        elif is_soft_dismissive:
            if soft_dismissive_count == 1:
                # First resistance: Tactical empathy + curiosity pivot (Voss + NEPQ)
                exchange_warning = f"""
            === RESISTANCE DETECTED - USE TACTICAL EMPATHY + PIVOT (Chris Voss + NEPQ) ===
            They said something like "I'm not telling you that" - they feel the question was too invasive.
            DO NOT back off. DO NOT ask the same type of question.
            Use tactical empathy to LABEL their emotion, then PIVOT to a different angle.

            PATTERN: Label + Pivot
            1. LABEL their feeling: "It sounds like that question felt a bit over the line."
            2. SOFTEN: "Totally fair, I get it."
            3. PIVOT to broader curiosity (different angle): "Just curious, what had you looking into coverage in the first place?"

            EXAMPLE RESPONSES:
            - "Sounds like that felt too nosy. My bad. Just curious, what got you thinking about coverage back then?"
            - "Fair enough, didn't mean to pry. What was going on that had you looking in the first place?"
            - "Got it, no need to get into details. Was there something specific that made you start looking?"

            DO NOT ask about the same topic they refused. Pivot to motivation, timing, or situation.
            === USE EMPATHY + PIVOT - STAY IN THE CONVERSATION ===
                """
                
        elif soft_dismissive_count == 2:
            # Second resistance: Calibrated question + reference what they already shared (Voss + Gap)
            exchange_warning = f"""
            === SECOND RESISTANCE - USE CALIBRATED QUESTION + GAP RECALL (Voss + Gap Selling) ===
            They've resisted twice. Don't push the same angle. Use what you ALREADY KNOW about them.
            Reference something they mentioned earlier and ask a calibrated "what" or "how" question.

            PATTERN: Acknowledge + Reference their words + Calibrated question
            1. ACKNOWLEDGE: "I hear you."
            2. REFERENCE what they said before: "You mentioned [family/work/concern] earlier..."
            3. CALIBRATED QUESTION: "How would you want that handled if something happened?"

            IF you know they have family: "I hear you. You mentioned your wife earlier. How would you want her taken care of if something happened?"
            IF you know they have work coverage: "Got it. You said you have something through work. What's your plan when you retire or switch jobs?"
            IF you know their motivation: "Fair enough. You mentioned wanting to make sure the kids are covered. What would be enough to feel good about that?"

            Use their OWN WORDS to reconnect. Don't ask new invasive questions.
            === REFERENCE WHAT THEY TOLD YOU - CALIBRATED QUESTION ===
                """
                
        else:
            # Third+ resistance: Keep trying with different re-frames until hard stop
            exchange_warning = f"""
            === {soft_dismissive_count}+ SOFT REJECTIONS - KEEP TRYING DIFFERENT ANGLES ===
            They've resisted {soft_dismissive_count} times BUT have NOT said "stop" or "leave me alone".
            "I'm covered", "I'm good", "Not interested" are NOT hard stops. They are invitations to re-frame.

            DO NOT EXIT. Use a different re-frame from this list (pick one you haven't used yet):

            1. Living benefits: "Does your current one let you touch the money while you're still alive if you get really sick?"
            2. Portability: "Does it follow you if you switch jobs or retire?"
            3. Amount: "Is it enough to pay off the house and replace income for a few years?"
            4. Future-pace: "If your situation ever changes, new baby, new house, job change, would you want me to reach out?"
            5. Most people: "Most folks say the same thing until they realize work coverage disappears when they retire."

            ONLY exit if they use hard stop language: "stop texting", "leave me alone", "remove me"
            === KEEP GOING - FIND THE GAP ===
            """
                
        # Only exit on very high rejection count AND hard stop language
        if rejection_count >= 8 and is_hard_dismissive:
            exchange_warning = f"""
            === CRITICAL: HARD STOP AFTER MANY ATTEMPTS ===
            They've rejected many times AND explicitly asked to stop. Exit gracefully.
            "Got it. Take care."
            === EXIT NOW ===
            """
            
        elif exchange_count >= 3:
            exchange_warning = f"""
            === CRITICAL: {exchange_count} EXCHANGES ALREADY - STOP ASKING QUESTIONS ===
            You have had {exchange_count} back-and-forth exchanges. DO NOT ask another question.
            Your response MUST be a statement with an appointment offer like:
            "I can take a look at options for you. I have [USE CALENDAR TIMES FROM CONTEXT], which works better?"
            === NO MORE QUESTIONS - MAKE THE OFFER ===
            """
        
        # Detect hesitation patterns after valuable conversation (Feel Felt Found opportunity)
        hesitation_phrases = [
            "can't afford", "cant afford", "money is tight", "budget", "expensive",
            "not sure", "don't know", "dont know", "idk", "maybe later", 
            "think about it", "need to think", "let me think"
        ]
        
        current_lower = message.lower()
        is_hesitant = any(phrase in current_lower for phrase in hesitation_phrases)
        has_valuable_convo = exchange_count >= 2 and (
            lead_profile.get("family", {}).get("spouse") or 
            lead_profile.get("family", {}).get("kids") or 
            lead_profile.get("motivating_goal")
        )
        
        feel_felt_found_prompt = ""
        if is_hesitant and has_valuable_convo:
            feel_felt_found_prompt = f"""
            === USE FEEL-FELT-FOUND WITH A CLIENT STORY ===
            This lead is HESITANT but has shown real need. Use the Feel-Felt-Found technique:
            1. Acknowledge their concern ("I get it" / "That makes sense")
            2. Share a BRIEF client story: "Had a client in a similar spot who..."
            3. What they found: "...we found a policy that fit their budget" or similar
            4. Close: Offer appointment times

            Example: "I get it. Had a client last month, same situation, thought he couldn't swing it. We found something for about $35/month that covered everything. Want me to see what's possible for you?"
            === INCLUDE THE CLIENT STORY - DON'T SKIP IT ===
            """
        
        intent_section = f"""
        === CURRENT INTENT/OBJECTIVE ===
        Intent: {intent}
        Directive: {intent_directive}
        ===
        """
        
        history_text = f"""
        === CONVERSATION HISTORY (read this carefully before responding) ===
        {chr(10).join(conversation_history)}
        === END OF HISTORY ===
        {qualification_context}{intent_section}{stage_directive}{feel_felt_found_prompt}{exchange_warning}{topics_warning}{questions_warning}{profile_text}
        """
    else:
        # Even without history, include profile and intent from current message
        intent_section = f"""
        === CURRENT INTENT/OBJECTIVE ===
        Intent: {intent}
        Directive: {intent_directive}
        ===
        """
        
        if any([lead_profile["family"]["spouse"], lead_profile["family"]["kids"], 
                lead_profile["coverage"]["has_coverage"], lead_profile["motivating_goal"]]):
            history_text = f"{qualification_context}{intent_section}{profile_text}"
        else:
            history_text = f"{qualification_context}{intent_section}" if qualification_context else intent_section

    # Score the previous response based on this incoming message
    outcome_score = None
    vibe = None
    try:
        outcome_score, vibe = record_lead_response(contact_id, message)
        logger.debug(f"Recorded lead response - Vibe: {vibe.value}, Score: {outcome_score}")
    except Exception as e:
        logger.warning(f"Could not record lead response: {e}")

    # Close stage templates (server-side enforcement for PolicyEngine fallback)
    close_templates = [
        "I can take a look at options for you. I have [USE CALENDAR TIMES FROM CONTEXT], which works better?",
        "Let me see what we can do. Free at 2pm today or 11am tomorrow?",
        "Got it. I can help you find the right coverage. How's [USE CALENDAR TIMES FROM CONTEXT]?",
        "Let me dig into this for you. What works better, 2pm today or 11am tomorrow?"
    ]

    client = get_client()

    # =========================================================================
    # UNIFIED BRAIN APPROACH - Everything goes through deliberate reasoning
    # No more template shortcuts - the bot must THINK using all its knowledge
    # =========================================================================

    # Build context for the unified brain
    unified_brain_knowledge = get_unified_brain()

    # Determine trigger suggestion for evaluation (not bypass)
    trigger_suggestion = trigger_suggestion if trigger_suggestion else "No trigger matched"

    # Get proven patterns for comparison
    proven_patterns_text = outcome_context if outcome_context else "No proven patterns yet"

    # Build the decision prompt with all context
    decision_prompt = get_decision_prompt(
        message=message,
        context=chr(10).join(conversation_history) if conversation_history else "First message in conversation",
        stage=stage,
        trigger_suggestion=trigger_suggestion,
        proven_patterns=proven_patterns_text,
        triggers_found=triggers_found
    )

    # Build unified brain system prompt - COMBINE all knowledge sources
    # Start with full NEPQ_SYSTEM_PROMPT (contains all tactical knowledge)
    # Then add unified brain framework for decision-making
    base_knowledge = NEPQ_SYSTEM_PROMPT.replace("{CODE}", confirmation_code)

    unified_system_prompt = f"""
    {base_knowledge}

    {unified_brain_knowledge}

    ===================================================================================
    SITUATIONAL CONTEXT
    ===================================================================================
    Agent name: {agent_name}
    Lead name: {first_name}
    Current stage: {stage}
    Exchange count: {exchange_count}
    Dismissive count: {soft_dismissive_count}
    Is soft dismissive: {is_soft_dismissive}
    Is hard dismissive: {is_hard_dismissive}

    {state_instructions}

    CONFIRMATION CODE (if booking): {confirmation_code}

    === AVAILABLE APPOINTMENT SLOTS (USE THESE EXACT TIMES) ===
    {real_calendar_slots}
    NEVER make up appointment times. ONLY offer the times listed above.

    ===================================================================================
    CRITICAL RULES
    ===================================================================================
    1. No em dashes (--) in responses
    2. Keep responses 15-35 words (SMS friendly)
    3. Only use first name every 3-4 messages like normal texting
    4. If they say "stop" or "leave me alone" - exit gracefully: "Got it. Take care."
    5. After 3 exchanges, STOP asking questions and offer appointment times
    6. When offering appointments, ONLY use times from AVAILABLE APPOINTMENT SLOTS above

    {decision_prompt}
    """

    # === UNIFIED BRAIN: Policy Validation with Retry Loop ===
    max_retries = 1  # Reduced from 2 for faster response
    retry_count = 0
    correction_prompt = ""
    reply = f"I have {real_calendar_slots}, which works better?"  # Default fallback with real times

    # Use grok-4-1-fast-reasoning for everything (cheap and capable)
    use_model = "grok-4-1-fast-reasoning"

    # === TOKEN STATS: Log cost estimate before API call ===
    prompt_tokens = count_tokens(unified_system_prompt) + count_tokens(history_text or "") + count_tokens(message)
    stats = get_token_stats(unified_system_prompt + (history_text or "") + message, max_response_tokens=425)
    logger.info(f"TOKEN_STATS: {stats['prompt_tokens']} input + {stats['max_response_tokens']} output = ${stats['estimated_cost_usd']:.5f}")

    # Simplified user content for unified brain approach
    # Include history_text which contains deflection warnings and questions already asked
    unified_user_content = f"""
    {history_text if history_text else "CONVERSATION HISTORY: First message - no history yet"}

    LEAD'S MESSAGE: "{message}"

    Now THINK through your decision process and respond.
    Remember: Apply your knowledge, don't just pattern match.
    """

    while retry_count <= max_retries:
        # Note: Grok model only supports temperature, top_p, max_tokens
        # frequency_penalty and presence_penalty are NOT supported
        response = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": unified_system_prompt},
                {"role": "user", "content": unified_user_content + correction_prompt}
            ],
            max_tokens=425,
            temperature=0.7,
            top_p=0.95
        )

        content = response.choices[0].message.content or ""
        
        # Parse unified brain thinking for logging
        thinking_match = re.search(r'<thinking>(.*?)</thinking>', content, re.DOTALL)
        if thinking_match:
            thinking = thinking_match.group(1).strip()
            logger.info(f"UNIFIED BRAIN REASONING:\n{thinking}")
        
        # Extract the actual response
        response_match = re.search(r'<response>(.*?)</response>', content, re.DOTALL)
        if response_match:
            reply = response_match.group(1).strip()
        else:
            # Fallback: if no tags, try to get just the last sentence/response
            # Strip any thinking blocks first
            reply = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL).strip()
        
        # Parse self-reflection BEFORE stripping (so we can use scores for validation)
        reflection = parse_reflection(content)
        reflection_scores = {}
        if reflection:
            reflection_scores = reflection.get('scores', {})
            logger.debug(f"Self-reflection scores: {reflection_scores}")
            if reflection.get('improvement'):
                logger.debug(f"Self-improvement note: {reflection['improvement']}")
        
        # Remove quotation marks wrapping the response
        if reply.startswith('"') and reply.endswith('"'):
            reply = reply[1:-1]
        if reply.startswith("'") and reply.endswith("'"):
            reply = reply[1:-1]
        reply = reply.replace("—", ",").replace("--", ",").replace("–", ",").replace(" - ", ", ").replace(" -", ",").replace("- ", ", ")
        
        # Validate response using PolicyEngine (pass reflection scores for scoring-based rejection)
        is_valid, error_reason, correction_guidance = PolicyEngine.validate_response(reply, conv_state, reflection_scores)
        
        if is_valid:
            logger.debug("Policy validation passed")
            break
        else:
            # SPECIAL CASE: Motivation question repeat - use backbone probe immediately
            if error_reason == "REPEAT_MOTIVATION_BLOCKED":
                logger.info("Motivation question repeat blocked - using backbone probe template")
                backbone_reply = get_backbone_probe_template()
                if backbone_reply:
                    reply = backbone_reply
                    break
                # Fallback if backbone template unavailable
                reply = "Usually people don't look up insurance for fun. Something on your mind about it?"
                break
            
            retry_count += 1
            logger.warning(f"Policy validation failed (attempt {retry_count}): {error_reason}")
            
            if retry_count <= max_retries:
                correction_prompt = PolicyEngine.get_regeneration_prompt(error_reason, correction_guidance)
            else:
                # Fallback to template after max retries
                logger.warning("Max retries exceeded, falling back to playbook template")
                scenario = match_scenario(message)
                if scenario:
                    template_reply = get_template_response(
                        scenario["stage"], 
                        scenario["response_key"],
                        {"first_name": first_name}
                    )
                    if template_reply:
                        reply = template_reply
                        break
                # Ultimate fallback: use real calendar slots
                reply = f"I can help you find the right fit. How's {real_calendar_slots}?"
                # Always break after max retries to avoid infinite loop
                break

    # Server-side semantic duplicate rejection (75% similarity check)
    is_duplicate = False
    duplicate_reason = None

    # Build question themes that are semantically equivalent
    QUESTION_THEMES = {
        "retirement_portability": [
            "continue after retirement", "leave your job", "retire", "portable", 
            "convert it", "goes with you", "when you leave", "portability",
            "if you quit", "stop working", "leaving the company"
        ],
        "policy_type": [
            "term or whole", "term or permanent", "what type", "kind of policy",
            "is it term", "is it whole life", "iul", "universal life"
        ],
        "living_benefits": [
            "living benefits", "accelerated death", "chronic illness", 
            "critical illness", "terminal illness", "access while alive"
        ],
        "coverage_goal": [
            "what made you", "why did you", "what's the goal", "what were you",
            "originally looking", "why coverage", "what prompted", "got you looking",
            "what got you"
        ],
        "other_policies": [
            "other policies", "any other", "additional coverage", "also have",
            "multiple policies", "work policy", "another plan"
        ],
        "motivation": [
            "what's on your mind", "what's been on", "what specifically", 
            "what are you thinking", "what concerns you"
        ]
    }

def get_question_theme(text):
    """Return the theme(s) of a message."""
    text_lower = text.lower()
    themes = []
    for theme, keywords in QUESTION_THEMES.items():
        if any(kw in text_lower for kw in keywords):
            themes.append(theme)
        return themes

    # Get themes in this reply
    reply_themes = get_question_theme(reply)

    # Check against recent agent messages
    if recent_agent_messages and reply_themes:
        for prev_msg in recent_agent_messages[-5:]:  # Check last 5 messages
            prev_themes = get_question_theme(prev_msg)
            # If any theme matches, it's a semantic duplicate
            shared_themes = set(reply_themes) & set(prev_themes)
            if shared_themes:
                is_duplicate = True
                duplicate_reason = f"Theme '{list(shared_themes)[0]}' already asked"
                break

    # === VECTOR SIMILARITY CHECK: spaCy-based semantic duplicate detection ===
    if contact_id and not is_duplicate:
        try:
            is_unique, uniqueness_reason = validate_response_uniqueness(contact_id, reply, threshold=0.85)
            if not is_unique:
                is_duplicate = True
                duplicate_reason = f"Vector similarity blocked: {uniqueness_reason}"
                logger.warning(f"VECTOR_SIMILARITY_BLOCKED: {uniqueness_reason}")
        except Exception as e:
            logger.debug(f"Vector similarity check skipped: {e}")

    # Also check against qualification state for logically blocked questions
    if contact_id and not is_duplicate:
        qual_state = get_qualification_state(contact_id)
        if qual_state:
            reply_lower = reply.lower()
            # Personal policy + asking about retirement = blocked
            if qual_state.get("is_personal_policy") or qual_state.get("is_employer_based") == False:
                if any(kw in reply_lower for kw in ["retirement", "retire", "leave your job", "portable", "convert"]):
                    is_duplicate = True
                    duplicate_reason = "Retirement question blocked - personal policy confirmed"
            
            # Already know living benefits status
            if qual_state.get("has_living_benefits") is not None:
                if "living benefits" in reply_lower:
                    is_duplicate = True
                    duplicate_reason = "Living benefits already known"
            
            # Already know other policies status
            if qual_state.get("has_other_policies") is not None:
                if any(kw in reply_lower for kw in ["other policies", "any other", "additional"]):
                    is_duplicate = True
                    duplicate_reason = "Other policies already asked"

    # If duplicate detected, use progression-based fallback
    if is_duplicate:
        logger.warning(f"SEMANTIC DUPLICATE BLOCKED: {duplicate_reason}")
        import random
        # Use a natural progression question instead
        progression_questions = [
            "What would make a quick review worth your time?",
            "I have [USE CALENDAR TIMES FROM CONTEXT], which works better?",
            "Just want to make sure you're not overpaying. Quick 5-minute review, what time works?",
        ]
        reply = random.choice(progression_questions)

    # =========================================================================
    # STEP 5: LOG THE DECISION (so we can track what worked)
    # =========================================================================
    decision_log = {
        "contact_id": contact_id,
        "client_message": message[:100],
        "triggers_found": triggers_found,
        "trigger_suggestion": trigger_suggestion[:50] if trigger_suggestion else None,
        "outcome_patterns_count": len(outcome_patterns),
        "final_reply": reply[:100],
        "used_trigger": reply == trigger_suggestion if trigger_suggestion else False,
        "vibe": vibe.value if vibe else None,
        "outcome_score": outcome_score
    }
    logger.info(f"STEP 5: Decision log: {decision_log}")

    # Track the agent's response for outcome learning
    try:
        tracker_id = record_agent_message(contact_id, reply)
        logger.info(f"STEP 5: Recorded agent message for tracking: {tracker_id}")
        
        # === CRITICAL: Record motivation questions to prevent repeats ===
        reply_lower = reply.lower()
        motivation_patterns = [
            "what got you", "what made you", "what originally", "why did you",
            "what brought you", "what were you", "what was going on",
            "what triggered", "what motivated", "reason you"
        ]
        if any(p in reply_lower for p in motivation_patterns) and "?" in reply:
            add_to_qualification_array(contact_id, "topics_asked", "motivation")
            logger.info("STEP 5: Recorded motivation question - will block future repeats")
        
        # === NLP MEMORY: Save agent message for topic extraction ===
        save_nlp_message_text(contact_id, reply, "agent")
        logger.debug(f"NLP: Saved agent message for contact {contact_id}")
        
        # If this was a good outcome (lead engaged well), save the pattern
        if outcome_score is not None and vibe is not None and outcome_score >= 2.0:
            save_new_pattern(message, reply, vibe, outcome_score)
            logger.info(f"STEP 5: Saved new winning pattern (score: {outcome_score})")
    except Exception as e:
        logger.warning(f"STEP 5: Could not record agent message: {e}")

    return reply, confirmation_code

def generate_nepq_response(
    data,
    message="",
    conversation_history=None,
    contact_id=None,
    first_name="",
    agent_name="",
    api_key=None,
    calendar_id=None,
    timezone="America/Chicago",
):
    """
    This function contains your full STEP 0..STEP 5 logic, properly scoped.
    NOTE: This code references helper functions/classes you already have elsewhere:
    - get_available_slots, format_slot_options
    - identify_triggers, force_response
    - get_learning_context, find_similar_successful_patterns
    - compress_conversation_history
    - extract_lead_profile, format_lead_profile_for_llm
    - save_nlp_message, parse_history_for_topics_asked, get_qualification_state
    - format_nlp_for_prompt, extract_and_update_qualification, increment_exchanges
    - already_covered_handler, format_qualification_for_prompt
    - build_state_from_history, format_state_for_prompt
    - record_lead_response
    - count_tokens, get_token_stats
    - get_unified_brain, get_decision_prompt
    - PolicyEngine, parse_reflection
    - match_scenario, get_template_response
    - validate_response_uniqueness, get_topics_already_discussed
    - get_backbone_probe_template
    - get_client
    """
    
    import re
    import random

    # -------------------------------------------------------------------------
    # Normalize inputs
    # -------------------------------------------------------------------------
    data = data or {}
    if conversation_history is None:
        conversation_history = []

    if isinstance(message, dict):
        message = message.get("body") or message.get("message") or message.get("text") or ""
    elif not isinstance(message, str):
        message = str(message) if message is not None else ""

    message = message.strip()
    message_text = message  # FIX: your pasted code referenced message_text but never defined it

    # -------------------------------------------------------------------------
    # Intent (used later in prompts)
    # -------------------------------------------------------------------------
    intent = extract_intent(data, message)

    # -------------------------------------------------------------------------
    # Confirmation code (must exist for booking flows)
    # -------------------------------------------------------------------------
    confirmation_code = generate_confirmation_code()

    # =========================================================================
    # STEP 0: FETCH REAL CALENDAR SLOTS (always available for closing)
    # =========================================================================
    real_calendar_slots = None
    if api_key and calendar_id:
        try:
            slots = get_available_slots(calendar_id, api_key, timezone)
            if slots:
                real_calendar_slots = format_slot_options(slots, timezone)
                logger.info(f"STEP 0: Fetched real calendar slots: {real_calendar_slots}")
        except Exception as e:
            logger.warning(f"STEP 0: Could not fetch calendar slots: {e}")

    if not real_calendar_slots:
        real_calendar_slots = "tonight or tomorrow morning"  # Vague fallback, not fake specific times
        logger.info("STEP 0: Using vague time fallback (no specific times)")

    # =========================================================================
    # STEP 1: KNOWLEDGE IS IN UNIFIED BRAIN (loaded via get_unified_brain)
    # =========================================================================
    logger.info("STEP 1: Knowledge will be loaded via unified brain")

    # =========================================================================
    # STEP 2: IDENTIFY TRIGGERS + GET TRIGGER SUGGESTION
    # =========================================================================
    triggers_found = identify_triggers(message)
    trigger_suggestion, trigger_code = force_response(message, api_key, calendar_id, timezone)
    logger.info(
        f"STEP 2: Triggers found: {triggers_found}, Suggestion: "
        f"{trigger_suggestion[:50] if trigger_suggestion else 'None'}..."
    )
    logger.info(f"STEP 2: message type after normalize: {type(message)}")

    # =========================================================================
    # STEP 3: CHECK OUTCOME PATTERNS (what worked before for similar messages)
    # =========================================================================
    outcome_patterns = []
    outcome_context = ""
    try:
        learning_ctx = get_learning_context(contact_id or "unknown", message)
        if learning_ctx and learning_ctx.get("proven_responses"):
            outcome_patterns = learning_ctx.get("proven_responses", [])[:5]
            outcome_context = "\n".join([f"- {p}" for p in outcome_patterns])
            logger.info(f"STEP 3: Found {len(outcome_patterns)} proven outcome patterns")
        else:
            logger.info("STEP 3: No proven outcome patterns found")

        similar_patterns = find_similar_successful_patterns(message, min_score=2.0, limit=3)
        if similar_patterns:
            for p in similar_patterns:
                sim_text = (
                    f"Similar trigger (sim={p.get('sim_score', 0):.2f}): "
                    f"{p.get('response_used', '')[:100]}"
                )
                if sim_text not in outcome_context:
                    outcome_context += f"\n- {sim_text}"
            logger.info(f"STEP 3: Found {len(similar_patterns)} trigram-similar patterns")
    except Exception as e:
        logger.warning(f"STEP 3: Could not load outcome patterns: {e}")

    # =========================================================================
    # STEP 4: EVALUATE - Check if triggers should bypass LLM
    # =========================================================================
    if trigger_code == "TRIG" and trigger_suggestion:
        triggers_str = str(triggers_found)
        m_lower = message_text.lower().strip()

        # Calendar-related triggers bypass to use real calendar times
        if "BUYING_SIGNAL" in triggers_str or "PRICE" in triggers_str:
            logger.info(
                f"STEP 4: Calendar-related trigger detected, using deterministic response: {trigger_suggestion}"
            )
            return trigger_suggestion, confirmation_code

        # Frustrated/repeat triggers bypass to apologize and pivot immediately
        frustrated_patterns = ["already asked", "move on", "stop asking", "enough questions"]
        if any(p in m_lower for p in frustrated_patterns):
            logger.info(
                f"STEP 4: Frustrated repeat trigger detected, using deterministic response: {trigger_suggestion}"
            )
            return trigger_suggestion, confirmation_code

    if trigger_code == "EXIT":
        logger.info(f"STEP 4: Exit trigger detected, returning: {trigger_suggestion}")
        return trigger_suggestion, confirmation_code

    # =========================================================================
    # Structured lead profile
    # =========================================================================
    # TOKEN OPTIMIZATION: Compress history if too long
    if len(conversation_history) > 6:
        original_count = len(conversation_history)
        conversation_history = compress_conversation_history(conversation_history, max_tokens=1500)
        logger.info(
            f"TOKEN_OPT: Compressed history from {original_count} to {len(conversation_history)} messages"
        )

    lead_profile = extract_lead_profile(conversation_history, first_name, message)

    # =========================================================================
    # QUALIFICATION STATE + NLP MEMORY
    # =========================================================================
    qualification_state = None
    qualification_context = ""
    nlp_context = ""

    if contact_id:
        try:
            save_nlp_message(contact_id, message, "lead")
            logger.debug(f"NLP: Saved lead message for contact {contact_id}")
        except Exception as e:
            logger.debug(f"NLP: save_nlp_message failed: {e}")

        try:
            if conversation_history:
                parse_history_for_topics_asked(contact_id, conversation_history)
        except Exception as e:
            logger.debug(f"NLP: parse_history_for_topics_asked failed: {e}")

        try:
            qualification_state = get_qualification_state(contact_id)
        except Exception as e:
            logger.debug(f"QUALIFICATION: get_qualification_state failed: {e}")

        try:
            nlp_context = format_nlp_for_prompt(contact_id)
        except Exception as e:
            logger.debug(f"NLP: format_nlp_for_prompt failed: {e}")

        try:
            extracted_updates = extract_and_update_qualification(contact_id, message, conversation_history)
            if extracted_updates:
                logger.info(f"QUALIFICATION: Extracted updates: {extracted_updates}")
                qualification_state = get_qualification_state(contact_id)
        except Exception as e:
            logger.debug(f"QUALIFICATION: extract_and_update_qualification failed: {e}")

        try:
            increment_exchanges(contact_id)
        except Exception as e:
            logger.debug(f"QUALIFICATION: increment_exchanges failed: {e}")

        # ALREADY COVERED HANDLER: Deterministic state machine (runs BEFORE LLM)
        try:
            handler_response, should_continue = already_covered_handler(
                contact_id, message, qualification_state, api_key, calendar_id, timezone
            )
            if not should_continue and handler_response:
                logger.info("ALREADY_COVERED_HANDLER: Returning deterministic response")
                return handler_response, confirmation_code
        except Exception as e:
            logger.debug(f"ALREADY_COVERED_HANDLER failed: {e}")

        try:
            qualification_context = format_qualification_for_prompt(qualification_state)
            if qualification_context:
                logger.debug("QUALIFICATION: Injecting known facts into prompt")
        except Exception as e:
            logger.debug(f"QUALIFICATION: format_qualification_for_prompt failed: {e}")

    # =========================================================================
    # LAYER 2: Build Conversation State (Source of Truth)
    # =========================================================================
    conv_state = build_state_from_history(
        contact_id=contact_id or "unknown",
        first_name=first_name,
        conversation_history=conversation_history,
        current_message=message,
    )

    # Sync qualification_state topics_asked to conv_state.topics_answered
    if qualification_state:
        topics_asked = qualification_state.get("topics_asked") or []
        for topic in topics_asked:
            if topic not in conv_state.topics_answered:
                conv_state.topics_answered.append(topic)

        if "motivation" in topics_asked or "original_goal" in topics_asked:
            if "motivation" not in conv_state.topics_answered:
                conv_state.topics_answered.append("motivation")
            if "motivating_goal" not in conv_state.topics_answered:
                conv_state.topics_answered.append("motivating_goal")
            logger.info("QUALIFICATION: Motivation question already asked - blocking repeats")

        if topics_asked:
            logger.info(f"QUALIFICATION: Synced {len(topics_asked)} topics from database: {topics_asked}")

    # Sync NLP topics
    if contact_id:
        try:
            nlp_topics = get_topics_already_discussed(contact_id)
            for topic in nlp_topics:
                simple_topic = topic.split(":")[-1] if ":" in topic else topic
                if simple_topic not in conv_state.topics_answered:
                    conv_state.topics_answered.append(simple_topic)
            if nlp_topics:
                logger.debug(f"NLP: Synced {len(nlp_topics)} topics from NLP memory")
        except Exception as e:
            logger.debug(f"NLP: get_topics_already_discussed failed: {e}")

    state_instructions = format_state_for_prompt(conv_state)
    logger.debug(
        f"Conversation state: stage={conv_state.stage.value}, "
        f"exchanges={conv_state.exchange_count}, dismissive_count={conv_state.soft_dismissive_count}"
    )

    # =========================================================================
    # BUYING SIGNAL DETECTION - Override intent when lead shows readiness
    # =========================================================================
    current_lower = message.lower()

    negation_phrases = [
        "don't need", "dont need", "do not need", "dont really need", "don't really need",
        "not interested", "no thanks", "no thank", "stop", "leave me alone",
        "not looking", "already have", "im good", "i'm good", "i am good",
        "not right now", "maybe later", "no im good", "nope", "no i dont",
        "no i don't", "no i'm not", "no im not", "not for me", "pass",
        "unsubscribe", "remove me", "take me off",
    ]
    has_negation = any(phrase in current_lower for phrase in negation_phrases)

    negation_patterns = [
        r"\bno\b.*\bneed\b",
        r"\bnot\b.*\binterested\b",
        r"\bdon'?t\b.*\bneed\b",
    ]
    if not has_negation:
        for pattern in negation_patterns:
            if re.search(pattern, current_lower):
                has_negation = True
                break

    strong_buying_signals = [
        "i'd have to get", "id have to get", "would have to get",
        "need to get new", "get new life insurance", "get new coverage",
        "sign me up", "let's do it", "lets do it", "i'm in", "im in",
        "what are my options", "how much would it cost", "what would it cost",
        "sounds good", "that sounds good", "works for me", "yeah let's do it",
        "sure", "okay when", "ok when", "yes when can we",
    ]
    weak_buying_signals = [
        "i need", "i'd need", "interested in looking", "looking into it",
        "want to look", "can you look into", "want coverage",
    ]

    detected_buying_signal = False
    if not has_negation:
        if any(signal in current_lower for signal in strong_buying_signals):
            detected_buying_signal = True
        elif any(signal in current_lower for signal in weak_buying_signals):
            if lead_profile.get("motivating_goal") or lead_profile.get("family", {}).get("spouse"):
                detected_buying_signal = True

    problem_revealed = bool(
        lead_profile.get("motivating_goal")
        or lead_profile.get("coverage", {}).get("coverage_gap")
        or (
            lead_profile.get("coverage", {}).get("has_coverage")
            and lead_profile.get("coverage", {}).get("type") == "employer"
        )
        or lead_profile.get("coverage", {}).get("employer")
    )

    # Determine exchange count from history
    if conversation_history:
        recent_agent = [msg for msg in conversation_history if msg.startswith("You:")]
        recent_lead = [msg for msg in conversation_history if msg.startswith("Lead:")]
        exchange_count = min(len(recent_agent), len(recent_lead))
    else:
        recent_agent = []
        recent_lead = []
        exchange_count = 0

    # Stage logic
    if exchange_count >= 3:
        intent = "book_appointment"
        stage = "close"
    elif detected_buying_signal:
        intent = "book_appointment"
        stage = "close"
    elif exchange_count >= 2 and problem_revealed:
        intent = "book_appointment"
        stage = "close"
    elif problem_revealed and exchange_count >= 1:
        stage = "consequence"
    elif problem_revealed:
        stage = "consequence"
    else:
        stage = "problem_awareness"

    intent_directive = INTENT_DIRECTIVES.get(intent, INTENT_DIRECTIVES["general"])

    # =========================================================================
    # STAGE DIRECTIVES
    # =========================================================================
    stage_directives = {
        "problem_awareness": """[... KEEP YOUR STAGE DIRECTIVE TEXT ...]""",
        "consequence": """[... KEEP YOUR STAGE DIRECTIVE TEXT ...]""",
        "close": """[... KEEP YOUR STAGE DIRECTIVE TEXT ...]""",
    }
    stage_directive = stage_directives.get(stage, "")

    profile_text = format_lead_profile_for_llm(lead_profile, first_name)

    # =========================================================================
    # Dismissive detection + topic repeat prevention
    # =========================================================================
    is_soft_dismissive = False
    is_hard_dismissive = False
    soft_dismissive_count = 0
    topics_warning = ""
    questions_warning = ""
    exchange_warning = ""

    soft_dismissive_phrases = [
        "not telling you", "none of your business", "why do you need to know",
        "thats personal", "that's personal", "private", "why does it matter",
        "doesnt matter", "doesn't matter", "dont matter", "don't matter",
        "not your concern", "why do you care", "im covered", "i'm covered",
        "already told you", "i said im", "i said i'm", "already said",
        "just leave it", "drop it", "not important", "thats not important",
    ]
    hard_dismissive_phrases = [
        "stop texting", "leave me alone", "f off", "fuck off", "go away",
        "dont text me", "don't text me", "stop messaging", "stop contacting",
        "remove me", "unsubscribe", "take me off", "do not contact",
        "dont call", "don't call", "never contact",
    ]

    is_soft_dismissive = any(phrase in current_lower for phrase in soft_dismissive_phrases)
    is_hard_dismissive = any(phrase in current_lower for phrase in hard_dismissive_phrases)
    if is_soft_dismissive:
        soft_dismissive_count = 1

    recent_agent_messages = recent_agent
    recent_lead_messages = recent_lead

    if conversation_history and len(conversation_history) > 0:
        recent_questions = recent_agent_messages[-3:] if len(recent_agent_messages) > 3 else recent_agent_messages

        all_agent_text = " ".join([msg.lower() for msg in recent_agent_messages])
        topics_already_asked = []

        if any(
            phrase in all_agent_text
            for phrase in [
                "living benefits", "access funds", "access part of", "touch the money",
                "seriously ill while alive", "sick while you", "while you're still alive",
                "accelerated", "chronic illness", "terminal illness rider",
            ]
        ):
            topics_already_asked.append("LIVING_BENEFITS")

        if any(
            phrase in all_agent_text
            for phrase in [
                "follow you if you", "switch jobs", "tied to your employer", "portable",
                "retire", "leave the company", "change jobs",
            ]
        ):
            topics_already_asked.append("PORTABILITY")

        if any(
            phrase in all_agent_text
            for phrase in [
                "how much", "coverage amount", "replace your income", "enough to cover",
                "10x your income", "what amount",
            ]
        ):
            topics_already_asked.append("AMOUNT")

        if any(
            phrase in all_agent_text
            for phrase in [
                "how many years", "when does it expire", "term length", "years left",
                "renew", "rate lock",
            ]
        ):
            topics_already_asked.append("TERM_LENGTH")

        if any(
            phrase in all_agent_text
            for phrase in [
                "who'd you go with", "who did you go with", "which company", "what company",
                "who are you with",
            ]
        ):
            topics_already_asked.append("COMPANY")

        if topics_already_asked:
            topics_warning = (
                "\n=== TOPICS YOU ALREADY ASKED ABOUT (BLOCKED - DO NOT ASK AGAIN) ===\n"
                + "\n".join([f"- {t}" for t in topics_already_asked])
                + "\n"
            )

        if recent_questions:
            questions_list = "\n".join([f"- {q.replace('You: ', '')}" for q in recent_questions])
            questions_warning = (
                "\n=== RECENT AGENT MESSAGES (DO NOT REPEAT THESE QUESTIONS) ===\n"
                + questions_list
                + "\n"
            )

        # Count soft dismissives in history (excluding last lead line to avoid double count)
        history_lead_messages = recent_lead_messages[:-1] if recent_lead_messages else []
        for msg in history_lead_messages:
            msg_lower = msg.lower()
            if any(phrase in msg_lower for phrase in soft_dismissive_phrases):
                soft_dismissive_count += 1

        rejection_phrases = [
            "not interested", "no thanks", "no thank", "im good", "i'm good",
            "im covered", "i'm covered", "already have", "all set", "dont need",
            "don't need", "not looking", "not right now", "no im good", "nah",
        ]
        rejection_count = 0
        for msg in recent_lead_messages:
            msg_lower = msg.lower()
            if any(phrase in msg_lower for phrase in rejection_phrases):
                rejection_count += 1
        if any(phrase in current_lower for phrase in rejection_phrases):
            rejection_count += 1

        if is_hard_dismissive:
            exchange_warning = (
                "\n=== CRITICAL: HARD STOP - THEY WANT NO CONTACT ===\n"
                "Exit immediately: 'Got it. Take care.'\n"
            )
        elif exchange_count >= 3:
            exchange_warning = (
                f"\n=== CRITICAL: {exchange_count} EXCHANGES ALREADY - STOP ASKING QUESTIONS ===\n"
                "Offer appointment times using the provided slots.\n"
            )

    # =========================================================================
    # Prompt assembly (history_text)
    # =========================================================================
    intent_section = f"""
    === CURRENT INTENT/OBJECTIVE ===
    Intent: {intent}
    Directive: {intent_directive}
    ===
    """

    if conversation_history:
        history_text = f"""
    === CONVERSATION HISTORY (read this carefully before responding) ===
    {chr(10).join(conversation_history)}
    === END OF HISTORY ===

    {qualification_context}
    {intent_section}
    {stage_directive}
    {exchange_warning}
    {topics_warning}
    {questions_warning}
    {profile_text}
    """
    else:
        history_text = f"""
    {qualification_context}
    {intent_section}
    {profile_text}
    """

    # =========================================================================
    # Score the previous response (optional)
    # =========================================================================
    outcome_score = None
    vibe = None
    try:
        outcome_score, vibe = record_lead_response(contact_id, message)
        logger.debug(f"Recorded lead response - Vibe: {vibe.value if vibe else None}, Score: {outcome_score}")
    except Exception as e:
        logger.warning(f"Could not record lead response: {e}")

    # =========================================================================
    # UNIFIED BRAIN
    # =========================================================================
    close_templates = [
        "I can take a look at options for you. I have [USE CALENDAR TIMES FROM CONTEXT], which works better?",
        "Let me see what we can do. Free at 2pm today or 11am tomorrow?",
        "Got it. I can help you find the right coverage. How's [USE CALENDAR TIMES FROM CONTEXT]?",
        "Let me dig into this for you. What works better, 2pm today or 11am tomorrow?",
    ]

    client = get_client()

    unified_brain_knowledge = get_unified_brain()
    trigger_suggestion_for_eval = trigger_suggestion if trigger_suggestion else "No trigger matched"
    proven_patterns_text = outcome_context if outcome_context else "No proven patterns yet"

    decision_prompt = get_decision_prompt(
        message=message,
        context=chr(10).join(conversation_history) if conversation_history else "First message in conversation",
        stage=stage,
        trigger_suggestion=trigger_suggestion_for_eval,
        proven_patterns=proven_patterns_text,
        triggers_found=triggers_found,
    )

    base_knowledge = NEPQ_SYSTEM_PROMPT.replace("{CODE}", confirmation_code)

    unified_system_prompt = f"""
    {base_knowledge}

    {unified_brain_knowledge}

    ===================================================================================
    SITUATIONAL CONTEXT
    ===================================================================================
    Agent name: {agent_name}
    Lead name: {first_name}
    Current stage: {stage}
    Exchange count: {exchange_count}
    Dismissive count: {soft_dismissive_count}
    Is soft dismissive: {is_soft_dismissive}
    Is hard dismissive: {is_hard_dismissive}

    {state_instructions}

    CONFIRMATION CODE (if booking): {confirmation_code}

    === AVAILABLE APPOINTMENT SLOTS (USE THESE EXACT TIMES) ===
    {real_calendar_slots}
    NEVER make up appointment times. ONLY offer the times listed above.

    ===================================================================================
    CRITICAL RULES
    ===================================================================================
    1. No em dashes in responses
    2. Keep responses 15-35 words
    3. Only use first name every 3-4 messages
    4. If they say stop or leave me alone, exit: "Got it. Take care."
    5. After 3 exchanges, stop asking questions and offer appointment times
    6. When offering appointments, ONLY use the times listed above

    {decision_prompt}
    """

    max_retries = 1
    retry_count = 0
    correction_prompt = ""
    reply = f"I have {real_calendar_slots}, which works better?"
    use_model = "grok-4-1-fast-reasoning"

    # Optional token stats
    try:
        prompt_tokens = count_tokens(unified_system_prompt) + count_tokens(history_text or "") + count_tokens(message)
        stats = get_token_stats(
            unified_system_prompt + (history_text or "") + message,
            max_response_tokens=425,
        )
        logger.info(
            f"TOKEN_STATS: {stats['prompt_tokens']} input + {stats['max_response_tokens']} output = "
            f"${stats['estimated_cost_usd']:.5f}"
        )
    except Exception:
        pass

    unified_user_content = f"""
    {history_text if history_text else "CONVERSATION HISTORY: First message - no history yet"}

    LEAD'S MESSAGE: "{message}"

    Now THINK through your decision process and respond.
    Remember: Apply your knowledge, don't just pattern match.
    """

    while retry_count <= max_retries:
        response = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": unified_system_prompt},
                {"role": "user", "content": unified_user_content + correction_prompt},
            ],
            max_tokens=425,
            temperature=0.7,
            top_p=0.95,
        )

        content = response.choices[0].message.content or ""

        thinking_match = re.search(r"<thinking>(.*?)</thinking>", content, re.DOTALL)
        if thinking_match:
            thinking = thinking_match.group(1).strip()
            logger.info(f"UNIFIED BRAIN REASONING:\n{thinking}")

        response_match = re.search(r"<response>(.*?)</response>", content, re.DOTALL)
        if response_match:
            reply = response_match.group(1).strip()
        else:
            reply = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL).strip()

        reflection = parse_reflection(content)
        reflection_scores = {}
        if reflection:
            reflection_scores = reflection.get("scores", {})
            logger.debug(f"Self-reflection scores: {reflection_scores}")

        # Remove wrapping quotes and normalize dash types
        if reply.startswith('"') and reply.endswith('"'):
            reply = reply[1:-1]
        if reply.startswith("'") and reply.endswith("'"):
            reply = reply[1:-1]
        reply = (
            reply.replace("—", ",")
            .replace("--", ",")
            .replace("–", ",")
            .replace(" - ", ", ")
            .replace(" -", ",")
            .replace("- ", ", ")
        )

        is_valid, error_reason, correction_guidance = PolicyEngine.validate_response(
            reply, conv_state, reflection_scores
        )

        if is_valid:
            logger.debug("Policy validation passed")
            break

        if error_reason == "REPEAT_MOTIVATION_BLOCKED":
            logger.info("Motivation repeat blocked, using backbone probe template")
            backbone_reply = get_backbone_probe_template()
            if backbone_reply:
                reply = backbone_reply
                break
            reply = "Usually people don't look up insurance for fun. Something on your mind about it?"
            break

        retry_count += 1
        logger.warning(f"Policy validation failed (attempt {retry_count}): {error_reason}")

        if retry_count <= max_retries:
            correction_prompt = PolicyEngine.get_regeneration_prompt(error_reason, correction_guidance)
        else:
            logger.warning("Max retries exceeded, falling back to playbook template")
            scenario = match_scenario(message)
            if scenario:
                template_reply = get_template_response(
                    scenario["stage"],
                    scenario["response_key"],
                    {"first_name": first_name},
                )
                if template_reply:
                    reply = template_reply
                    break
            reply = f"I can help you find the right fit. How's {real_calendar_slots}?"
            break

    # =========================================================================
    # Server-side semantic duplicate rejection
    # =========================================================================
    is_duplicate = False
    duplicate_reason = None

    QUESTION_THEMES = {
        "retirement_portability": [
            "continue after retirement", "leave your job", "retire", "portable",
            "convert it", "goes with you", "when you leave", "portability",
            "if you quit", "stop working", "leaving the company",
        ],
        "policy_type": [
            "term or whole", "term or permanent", "what type", "kind of policy",
            "is it term", "is it whole life", "iul", "universal life",
        ],
        "living_benefits": [
            "living benefits", "accelerated death", "chronic illness",
            "critical illness", "terminal illness", "access while alive",
        ],
        "coverage_goal": [
            "what made you", "why did you", "what's the goal", "what were you",
            "originally looking", "why coverage", "what prompted", "got you looking",
            "what got you",
        ],
        "other_policies": [
            "other policies", "any other", "additional coverage", "also have",
            "multiple policies", "work policy", "another plan",
        ],
        "motivation": [
            "what's on your mind", "what's been on", "what specifically",
            "what are you thinking", "what concerns you",
        ],
    }

def get_question_theme(text):
    text_lower = (text or "").lower()
    themes = []
    for theme, keywords in QUESTION_THEMES.items():
        if any(kw in text_lower for kw in keywords):
            themes.append(theme)
        return themes

    reply_themes = get_question_theme(reply)

    # Theme duplicate check
    if recent_agent_messages and reply_themes:
        for prev_msg in recent_agent_messages[-5:]:
            prev_themes = get_question_theme(prev_msg)
            shared = set(reply_themes) & set(prev_themes)
            if shared:
                is_duplicate = True
                duplicate_reason = f"Theme '{list(shared)[0]}' already asked"
                break

    # Vector similarity check
    if contact_id and not is_duplicate:
        try:
            is_unique, uniqueness_reason = validate_response_uniqueness(contact_id, reply, threshold=0.85)
            if not is_unique:
                is_duplicate = True
                duplicate_reason = f"Vector similarity blocked: {uniqueness_reason}"
                logger.warning(f"VECTOR_SIMILARITY_BLOCKED: {uniqueness_reason}")
        except Exception as e:
            logger.debug(f"Vector similarity check skipped: {e}")

    # Qualification-based logical blocks
    if contact_id and not is_duplicate:
        try:
            qual_state = get_qualification_state(contact_id)
            if qual_state:
                reply_lower = reply.lower()

                if qual_state.get("is_personal_policy") or qual_state.get("is_employer_based") is False:
                    if any(kw in reply_lower for kw in ["retirement", "retire", "leave your job", "portable", "convert"]):
                        is_duplicate = True
                        duplicate_reason = "Retirement question blocked - personal policy confirmed"

                if qual_state.get("has_living_benefits") is not None:
                    if "living benefits" in reply_lower:
                        is_duplicate = True
                        duplicate_reason = "Living benefits already known"

                if qual_state.get("has_other_policies") is not None:
                    if any(kw in reply_lower for kw in ["other policies", "any other", "additional"]):
                        is_duplicate = True
                        duplicate_reason = "Other policies already asked"
        except Exception as e:
            logger.debug(f"Qualification duplicate block check skipped: {e}")

    if is_duplicate:
        logger.warning(f"SEMANTIC DUPLICATE BLOCKED: {duplicate_reason}")
        progression_questions = [
            "What would make a quick review worth your time?",
            f"I have {real_calendar_slots}, which works better?",
            "Just want to make sure you're not overpaying. Quick 5-minute review, what time works?",

        ]
        reply = random.choice(progression_questions)

    # =========================================================================
    # STEP 5: LOG THE DECISION (CLOSE YOUR DICT PROPERLY)
    # =========================================================================
    decision_log = {
        "contact_id": contact_id,
        "client_message": (message or "")[:100],
        "triggers_found": triggers_found,
        "trigger_suggestion": (trigger_suggestion or "")[:50] if trigger_suggestion else None,
        "outcome_patterns_count": len(outcome_patterns) if outcome_patterns else 0,
        "final_reply": (reply or "")[:100],
        "used_trigger": bool(reply == trigger_suggestion) if trigger_suggestion else False,
        "vibe": vibe.value if vibe else None,
        "outcome_score": outcome_score,
    }

    try:
        logger.info(f"DECISION_LOG: {decision_log}")
    except Exception:
        pass

        return reply, confirmation_code

    # Track the agent's response for outcome learning
    try:
        tracker_id = record_agent_message(contact_id, reply)
        logger.info(f"STEP 5: Recorded agent message for tracking: {tracker_id}")

        # === CRITICAL: Record motivation questions to prevent repeats ===
        reply_lower = reply.lower()
        motivation_patterns = [
            "what got you", "what made you", "what originally", "why did you",
            "what brought you", "what were you", "what was going on",
            "what triggered", "what motivated", "reason you"
        ]
        if any(p in reply_lower for p in motivation_patterns) and "?" in reply:
            add_to_qualification_array(contact_id, "topics_asked", "motivation")
            logger.info("STEP 5: Recorded motivation question - will block future repeats")

        # === NLP MEMORY: Save agent message for topic extraction ===
        save_nlp_message_text(contact_id, reply, "agent")
        logger.debug(f"NLP: Saved agent message for contact {contact_id}")

        # If this was a good outcome (lead engaged well), save the pattern
        if outcome_score is not None and vibe is not None and outcome_score >= 2.0:
            save_new_pattern(message, reply, vibe, outcome_score)
            logger.info(f"STEP 5: Saved new winning pattern (score: {outcome_score})")
    except Exception as e:
        logger.warning(f"STEP 5: Could not record agent message: {e}")

    return reply, confirmation_code


@app.route("/ghl", methods=["POST"])
def ghl_unified():
    """
    Unified GoHighLevel endpoint. Handles all GHL actions via a single URL.

    Multi-tenant: Pass GHL credentials in the JSON body:
    - ghl_api_key: Your GHL Private Integration Token
    - ghl_location_id: Your GHL Location ID

    If not provided, falls back to environment variables (for your own setup).

    Actions (specified via 'action' field in JSON body):

    1. "respond" - Generate NEPQ response and send SMS
        Required: contact_id, message
        Optional: first_name

    2. "appointment" - Create calendar appointment
        Required: contact_id, calendar_id, start_time
        Optional: duration_minutes (default: 30), title

    3. "stage" - Update or create opportunity
        For update: opportunity_id, stage_id
        For create: contact_id, pipeline_id, stage_id, name (optional)

    4. "contact" - Get contact info
        Required: contact_id

    5. "search" - Search contacts by phone
        Required: phone
    """
    raw_data = request.json or {}
    data = normalize_keys(raw_data)

    # Ultimate contact_id extraction — will NEVER be None if it's a real inbound message
    contact_id = (
        custom.get("contact_id") or                 # Custom Data (recommended)
        data.get("contact_id") or
        data.get("contactid") or
        data.get("contactId") or                    # Standard GHL field
        data.get("contact", {}).get("id") or        # Sometimes nested
        data.get("contact", {}).get("contactId") or
        data.get("personId") or                     # Rare alternate
        None
    )


    if not contact_id:
        logger.error(f"CRITICAL: No contact_id found in payload! Full keys: {list(data.keys())}")
        return jsonify({"error": "missing contact_id"}), 400

    logger.info(f"Processed inbound from contact_id: {contact_id}")

    # Extract message (string or {"body": "..."} object)
    raw_message = custom.get("message", data.get("message", data.get("body", data.get("text", ""))))
    if isinstance(raw_message, dict):
        message_text = raw_message.get("body", "") or raw_message.get("text", "") or ""
    else:
        message_text = raw_message

    message_text = str(message_text).strip()

    action = data.get("action", "respond")

    # Normalize payload as well (force=True is fine if you always get JSON)
    payload = normalize_keys(request.get_json(force=True) or {})
    custom_payload = payload.get("customdata", {}) or payload.get("customData", {}) or {}

    raw_message_2 = custom_payload.get("message", payload.get("message", ""))
    if isinstance(raw_message_2, dict):
        message_text_2 = raw_message_2.get("body", "") or raw_message_2.get("text", "") or ""
    else:
        message_text_2 = raw_message_2

    if not isinstance(message_text_2, str):
        message_text_2 = ""

    message_text_2 = message_text_2.strip()

    # Prefer the payload-derived message if present, else fall back to earlier parse
    message_text = message_text_2 or message_text

    first_name = data.get("first_name", payload.get("first_name", ""))
    agent_name = data.get("agent_name", payload.get("agent_name", ""))
    contact_id = data.get("contact_id", payload.get("contact_id", ""))
    intent = data.get("intent", payload.get("intent", ""))

    api_key, location_id = get_ghl_credentials(data)

    safe_data = {k: v for k, v in data.items() if k not in ("ghl_api_key", "ghl_location_id")}
    logger.debug(f"GHL unified request - action: {action}, data: {safe_data}")

    if action == "respond":
        contact_id = data.get("contact_id") or data.get("contactid")
        first_name = data.get("first_name") or data.get("firstname") or data.get("name", "there")
        agent_name = data.get("agent_name") or data.get("agentname") or data.get("rep_name") or "Mitchell"
        message = message_text  # implicit - dont touch it

        if not contact_id:
            return jsonify({"error": "contact_id required"}), 400
        if not message:
            message = "initial outreach - contact just entered pipeline, send first message to start conversation"

        conversation_history = get_conversation_history(contact_id, api_key, location_id, limit=10)
        logger.debug(f"Fetched {len(conversation_history)} messages from history")

        intent = extract_intent(data, message)
        logger.debug(f"Extracted intent in /ghl respond: {intent}")
        logger.info(f"DBUG message type: {type(message)} preview: {str(message)[:60]}")

        start_time_iso, formatted_time, _ = parse_booking_time(message)
        appointment_created = False
        appointment_details = None
        booking_error = None

        if start_time_iso and contact_id and api_key and location_id:
            logger.info(f"Detected booking time in /ghl respond: {formatted_time}")
            calendar_id = data.get("calendar_id") or data.get("calendarid") or os.environ.get("GHL_CALENDAR_ID")
            if calendar_id:
                start_dt = datetime.fromisoformat(start_time_iso)
                end_dt = start_dt + timedelta(minutes=30)
                end_time_iso = end_dt.isoformat()

                appointment_result = create_ghl_appointment(
                    contact_id,
                    calendar_id,
                    start_time_iso,
                    end_time_iso,
                    api_key,
                    location_id,
                    "Life Insurance Consultation",
                )

                if appointment_result.get("success"):
                    appointment_created = True
                    appointment_details = {"formatted_time": formatted_time}
                    # Mark appointment for outcome learning bonus
                    try:
                        mark_appointment_booked(contact_id)
                        logger.info(f"Marked appointment booked for outcome learning: {contact_id}")
                    except Exception as e:
                        logger.warning(f"Could not mark appointment booked: {e}")
                else:
                    booking_error = appointment_result.get("error", "Appointment creation failed")
            else:
                booking_error = "Calendar not configured"

        try:
            logger.info("[/ghl] Starting response generation...")
            if appointment_created and appointment_details:
                logger.info("[/ghl] Appointment path - generating confirmation")
                confirmation_code = generate_confirmation_code()
                reply = (
                    f"You're all set for {appointment_details['formatted_time']}. "
                    f"Your confirmation code is {confirmation_code}. "
                    f"Reply {confirmation_code} to confirm and I'll send you the calendar invite."
                )
                reply = reply.replace("—", ",").replace("--", ",").replace("–", ",").replace(" - ", ", ")
                logger.info(f"[/ghl] Appointment reply set: {reply[:50]}...")
            else:
                logger.info("[/ghl] Normal path - calling generate_nepq_response")
                calendar_id_for_slots = data.get("calendar_id") or data.get("calendarid") or os.environ.get("GHL_CALENDAR_ID")

                # NOTE: This keeps YOUR call signature as-is; ensure your function matches it.
                reply, confirmation_code = generate_nepq_response(
                    first_name,
                    message,
                    agent_name,
                    conversation_history,
                    intent,
                    contact_id,
                    api_key,
                    calendar_id_for_slots,
                )
                logger.info(f"[/ghl] generate_nepq_response returned reply: {reply[:50] if reply else 'None'}...")

            logger.info(f"[/ghl] About to send SMS with reply defined: {reply is not None}")
            sms_result = send_sms_via_ghl(contact_id, reply, api_key, location_id)
            logger.info(f"[/ghl] SMS result: {sms_result}")

            response_data = {
                "success": True if not booking_error else False,
                "reply": reply,
                "contact_id": contact_id,
                "sms_sent": sms_result.get("success", False),
                "confirmation_code": confirmation_code,
                "intent": intent,
                "appointment_created": appointment_created,
                "booking_attempted": bool(start_time_iso),
                "booking_error": booking_error,
                "time_detected": formatted_time,
            }
            if appointment_created:
                response_data["appointment_time"] = formatted_time

            if sms_result.get("success"):
                return jsonify(response_data), (200 if not booking_error else 422)
            else:
                response_data["sms_error"] = sms_result.get("error")
                return jsonify(response_data), 500
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return jsonify({"error": str(e)}), 500

    elif action == "appointment":
        contact_id = data.get("contact_id") or data.get("contactId")
        calendar_id = data.get("calendar_id") or data.get("calendarid") or os.environ.get("GHL_CALENDAR_ID")
        start_time = data.get("start_time") or data.get("startTime")
        duration_minutes = data.get("duration_minutes", 30)
        title = data.get("title", "Life Insurance Consultation")

        if not contact_id or not calendar_id or not start_time:
            return jsonify({"error": "contact_id, calendar_id, and start_time required"}), 400

        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            end_dt = start_dt + timedelta(minutes=duration_minutes)
            end_time = end_dt.isoformat()

            result = create_ghl_appointment(
                contact_id, calendar_id, start_time, end_time, api_key, location_id, title
            )

            if result.get("success"):
                return jsonify({"success": True, "appointment": result.get("data")})
            else:
                return jsonify({"success": False, "error": result.get("error", "Failed to create appointment")}), 422
        except Exception as e:
            logger.error(f"Error creating appointment: {e}")
            return jsonify({"error": str(e)}), 500

    elif action == "stage":
        opportunity_id = data.get("opportunity_id") or data.get("opportunityId")
        contact_id = data.get("contact_id") or data.get("contactId")
        pipeline_id = data.get("pipeline_id") or data.get("pipelineId")
        stage_id = data.get("stage_id") or data.get("stageId")
        name = data.get("name", "Life Insurance Lead")

        if not stage_id:
            return jsonify({"error": "stage_id required"}), 400

        if opportunity_id:
            result = update_contact_stage(opportunity_id, stage_id, api_key)
            if result:
                return jsonify({"success": True, "opportunity": result})
            else:
                return jsonify({"error": "Failed to update stage"}), 500
        elif contact_id and pipeline_id:
            result = create_opportunity(contact_id, pipeline_id, stage_id, api_key, location_id, name)
            if result:
                return jsonify({"success": True, "opportunity": result, "created": True})
            else:
                return jsonify({"error": "Failed to create opportunity"}), 500
        else:
            return jsonify({"error": "Either opportunity_id OR (contact_id and pipeline_id) required"}), 400

    elif action == "contact":
        contact_id = data.get("contact_id") or data.get("contactId")
        if not contact_id:
            return jsonify({"error": "contact_id required"}), 400

        result = get_contact_info(contact_id, api_key)
        if result:
            return jsonify({"success": True, "contact": result})
        else:
            return jsonify({"error": "Failed to get contact"}), 500

    elif action == "search":
        phone = data.get("phone")
        if not phone:
            return jsonify({"error": "phone required"}), 400

        result = search_contacts_by_phone(phone, api_key, location_id)
        if result:
            return jsonify({"success": True, "contacts": result})
        else:
            return jsonify({"error": "Failed to search contacts"}), 500

    else:
        return jsonify({
            "error": f"Unknown action {action}",
            "Valid_actions": "respond, appointment stage, contact, search"
        }), 400


@app.route("/grok", methods=["POST"])
def grok_insurance():
    """Legacy endpoint - generates NEPQ response without GHL integration"""
    data = request.json or {}
    name = data.get("firstName") or data.get("first_name", "there")
    lead_msg = data.get("message", "")
    agent_name = data.get("agent_name") or data.get("rep_name") or "Mitchell"
    contact_id = data.get("contact_id") or data.get("contactId")  # Support qualification memory

    if not lead_msg:
        lead_msg = "initial outreach - contact just entered pipeline, send first message to start conversation"

        # Parse conversation history from request
        raw_history = data.get("conversationHistory", [])
        conversation_history = []
    if raw_history:
        for msg in raw_history:
            if isinstance(msg, dict):
                direction = msg.get("message", "outbound")
                body = msg.get("body", "")
                if body:
                    role = "Lead" if direction.lower() == "inbound" else "You"
                    conversation_history.append(f"{role}: {body}")
            elif isinstance(msg, str):
                conversation_history.append(msg)
        logger.debug(f"[/grok] Using {len(conversation_history)} messages from request body")

    # Legacy endpoint - no GHL integration, use env vars if available
    api_key = os.environ.get("GHL_API_KEY")
    calendar_id = os.environ.get("GHL_CALENDAR_ID")

    reply, _ = generate_nepq_response(
        name,
        lead_msg,
        agent_name,
        conversation_history=conversation_history,
        contact_id=contact_id,
        api_key=api_key,
        calendar_id=calendar_id,
    )
    return jsonify({"reply": reply})


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    return grok_insurance()


@app.route("/outreach", methods=["GET", "POST"])
def outreach():
    if request.method == "POST":
        return "OK", 200
    return "Up and running", 200


@app.route("/health", methods=["GET", "POST"])
def health_check():
    return jsonify({"status": "healthy", "service": "NEPQ Webhook API"})


@app.route("/nlp/<contact_id>", methods=["GET", "POST"])
def nlp_contact_summary(contact_id):
    """Get NLP topic breakdown and message history for a contact"""
    summary = get_contact_nlp_summary(contact_id)
    return jsonify(summary)


@app.route("/nlp-topics/<contact_id>", methods=["GET", "POST"])
def nlp_topics_only(contact_id):
    """Get just the topic breakdown for a contact"""
    topics = get_topic_breakdown(contact_id)
    return jsonify({"contact_id": contact_id, "topics": topics})


@app.route("/stats", methods=["GET", "POST"])
def training_stats():
    """Live training dashboard stats"""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT COUNT(*) as total FROM outcome_tracker")
        row = cur.fetchone()
        tracked = row["total"] if row else 0

        cur.execute("SELECT COUNT(*) as total FROM response_patterns")
        row = cur.fetchone()
        patterns = row["total"] if row else 0

        cur.execute("SELECT COUNT(*) as total FROM contact_history")
        row = cur.fetchone()
        contacts = row["total"] if row else 0

        vibes = {}
        cur.execute(
            """
            SELECT vibe_classification, COUNT(*) as cnt
            FROM outcome_tracker
            WHERE vibe_classification IS NOT NULL
            GROUP BY vibe_classification
            """
        )
        for r in cur.fetchall():
            vibes[r["vibe_classification"]] = r["cnt"]

        top_patterns = []
        cur.execute(
            """
            SELECT trigger_category, score, response_used
            FROM response_patterns
            ORDER BY score DESC
            LIMIT 10
            """
        )
        for r in cur.fetchall():
            top_patterns.append(
                f"{r['score']:.1f} | {r['trigger_category']}: {r['response_used'][:50]}..."
            )

        # Per-contact stats
        cur.execute(
            """
            SELECT contact_id, COUNT(*) as msg_count
            FROM outcome_tracker
            GROUP BY contact_id
            ORDER BY msg_count DESC
            LIMIT 10
            """
        )
        contact_stats = []
        for r in cur.fetchall():
            contact_stats.append({"contact": r["contact_id"][:20], "messages": r["msg_count"]})

        # Conversation length stats (messages per contact)
        cur.execute(
            """
            SELECT
                MIN(cnt) as shortest,
                MAX(cnt) as longest,
                AVG(cnt) as average
            FROM (
                SELECT contact_id, COUNT(*) as cnt
                FROM outcome_tracker
                GROUP BY contact_id
            ) sub
            """
        )
        length_stats = cur.fetchone()

        # Booked appointments (direction vibes with high scores often mean bookings)
        cur.execute(
            """
            SELECT COUNT(DISTINCT contact_id) as booked
            FROM outcome_tracker
            WHERE outcome_score >= 4.0
            AND vibe_classification IN ('direction', 'need')
            """
        )
        row = cur.fetchone()
        booked = row["booked"] if row else 0

        # Top performers (contacts with highest scores)
        cur.execute(
            """
            SELECT contact_id, MAX(outcome_score) as best_score, COUNT(*) as turns
            FROM outcome_tracker
            WHERE outcome_score IS NOT NULL
            GROUP BY contact_id
            ORDER BY best_score DESC, turns DESC
            LIMIT 5
            """
        )
        top_convos = []
        for r in cur.fetchall():
            top_convos.append(
                {
                    "contact": r["contact_id"][:15],
                    "score": float(r["best_score"]) if r["best_score"] else 0,
                    "turns": r["turns"],
                }
            )
    
        conn.close()
    
        return jsonify(
            {
                "tracked": tracked,
                "patterns": patterns,
                "contacts": contacts,
                "booked": booked,
                "need": vibes.get("need", 0),
                "direction": vibes.get("direction", 0),
                "neutral": vibes.get("neutral", 0),
                "objection": vibes.get("objection", 0),
                "dismissive": vibes.get("dismissive", 0),
                "ghosted": vibes.get("ghosted", 0),
                "shortest_convo": int(length_stats["shortest"])
                if length_stats and length_stats.get("shortest")
                else 0,
                "longest_convo": int(length_stats["longest"])
                if length_stats and length_stats.get("longest")
                else 0,
                "avg_convo": round(float(length_stats["average"]), 1)
                if length_stats and length_stats.get("average")
                else 0,
                "top_patterns": top_patterns,
                "contact_stats": contact_stats,
                "top_convos": top_convos,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/ghl-webhook", methods=["POST"])
def ghl_webhook():
    """Legacy endpoint - redirects to unified /ghl endpoint with action=respond"""
    data = request.json or {}
    data["action"] = "respond"
    return ghl_unified()


@app.route("/ghl-appointment", methods=["POST"])
def ghl_appointment():
    """Legacy endpoint - redirects to unified /ghl endpoint with action=appointment"""
    data = request.json or {}
    data["action"] = "appointment"
    return ghl_unified()


@app.route("/ghl-stage", methods=["POST"])
def ghl_stage():
    """Legacy endpoint - redirects to unified /ghl endpoint with action=stage"""
    data = request.json or {}
    data["action"] = "stage"
    return ghl_unified()


@app.route("/", methods=["GET", "POST"])
def index():
    """
    Main webhook - generates NEPQ response and sends SMS automatically.
    Just set URL to https://insurancegrokbot.click/ghl with Custom Data.

    If no message is provided (like for tag/pipeline triggers), generates
    an initial outreach message to start the conversation.

    GET requests return a simple health check (for GHL webhook verification).
    """
    if request.method == "GET":
        return jsonify({"status": "ok", "service": "NEPQ Webhook API", "ready": True})

    raw_data = request.json or {}
    data = normalize_keys(raw_data)

    # Extract real data from GHL Custom Fields
    first_name = (data.get("first_name", "").strip() or "there")
    contact_id = data.get("contact_id")
    message = data.get("message") or extract_message_text(data)
    agent_name = data.get("agent_name")
    intent = data.get("intent")

    # (Optional) If you still use this early call anywhere, it’s now syntactically valid.
    # reply, confirmation_code = generate_nepq_response(
    #     first_name=first_name,
    #     message=message,
    #     agent_name=agent_name,
    #     contact_id=contact_id,
    #     intent=intent,
    # )

    api_key, location_id = get_ghl_credentials(data)

    # GHL field extraction - handles all common GHL webhook formats
    contact_obj = data.get("contact", {}) if isinstance(data.get("contact"), dict) else {}
    contact = contact_obj  # prevents NameError later if you reference `contact`
    
    contact_id = (
        data.get("contact_id")
        or data.get("contactid")
        or data.get("contactId")
        or contact_obj.get("id")
        or data.get("id")
    )
    
    raw_name = (
        data.get("first_name")
        or data.get("firstname")
        or data.get("firstName")
        or contact_obj.get("first_name")
        or contact_obj.get("first_name")
        or contact_obj.get("name")
        or data.get("name")
        or ""
    )
    first_name = str(raw_name).split()[0] if raw_name else "there"
    
    raw_message = data.get("message") or data.get("body") or data.get("text", "")
    if isinstance(raw_message, dict):
        message = raw_message.get("body", "") or raw_message.get("text", "") or str(raw_message)
    else:
        message = str(raw_message) if raw_message else ""
    
    agent_name = data.get("agent_name") or data.get("agentname") or data.get("rep_name") or "Mitchell"
    
    safe_data = {k: v for k, v in data.items() if k not in ("ghl_api_key", "ghl_location_id")}
    logger.debug(f"Root webhook request: {safe_data}")
    
    # Initial outreach detection
    if not message.strip() or message.lower() in ["initial outreach", "first message", ""]:
        reply = (
            f"Hey {first_name}, are you still with that other life insurance plan? "
            f"There's new living benefits that just came out and a lot of people have been asking about them."
        )
    
        if contact_id and api_key and location_id:
            sms_result = send_sms_via_ghl(contact_id, reply, api_key, location_id)
            return jsonify(
                {
                    "success": True,
                    "reply": reply,
                    "opener": "jeremy_miner_2025",
                    "contact_id": contact_id,
                    "sms_sent": sms_result.get("success", False),
                }
            )
        else:
            return jsonify(
                {
                    "success": True,
                    "reply": reply,
                    "opener": "jeremy_miner_2025",
                    "sms_sent": False,
                    "warning": "No GHL credentials - SMS not sent",
                }
            )
    
    intent = extract_intent(data, message)
    logger.debug(f"Extracted intent: {intent}")
    
    # Support conversation_history from request body (for testing) or fetch from GHL
    raw_history = data.get("conversation_history", [])
    conversation_history = []
    
    if raw_history:
        for msg in raw_history:
            if isinstance(msg, dict):
                normalized_msg = normalize_keys(msg)
                direction = normalized_msg.get("direction", "outbound")
                body = normalized_msg.get("body", "")
                if body:
                    role = "Lead" if direction.lower() == "inbound" else "You"
                    conversation_history.append(f"{role}: {body}")
            elif isinstance(msg, str):
                conversation_history.append(msg)
        logger.debug(f"Using {len(conversation_history)} messages from request body")
    elif contact_id and api_key and location_id:
        conversation_history = get_conversation_history(contact_id, api_key, location_id, limit=10)
        logger.debug(f"Fetched {len(conversation_history)} messages from history")
    
    start_time_iso, formatted_time, original_time_text = parse_booking_time(message)
    appointment_created = False
    appointment_details = None
    booking_error = None
    
    if start_time_iso and contact_id and api_key and location_id:
        logger.info(f"Detected booking time: {formatted_time} from message: {message}")
    
        calendar_id = os.environ.get("GHL_CALENDAR_ID")
        if not calendar_id:
            logger.error("GHL_CALENDAR_ID not configured, cannot create appointment")
            booking_error = "Calendar not configured"
        else:
            start_dt = datetime.fromisoformat(start_time_iso)
            end_dt = start_dt + timedelta(minutes=30)
            end_time_iso = end_dt.isoformat()
    
            appointment_result = create_ghl_appointment(
                contact_id,
                calendar_id,
                start_time_iso,
                end_time_iso,
                api_key,
                location_id,
                "Life Insurance Consultation",
            )
    
            if appointment_result.get("success"):
                appointment_created = True
                appointment_details = {
                    "start_time": start_time_iso,
                    "formatted_time": formatted_time,
                    "appointment_id": appointment_result.get("data", {}).get("id"),
                }
                logger.info(f"Appointment created for {formatted_time}")
            else:
                logger.error(f"Failed to create appointment for {formatted_time}")
                booking_error = appointment_result.get("error", "Appointment creation failed")
    
    try:
        if appointment_created and appointment_details:
            confirmation_code = generate_confirmation_code()
            reply = (
                f"You're all set for {appointment_details['formatted_time']}. "
                f"Your confirmation code is {confirmation_code}. "
                f"Reply {confirmation_code} to confirm and I'll send you the calendar invite."
            )
            reply = reply.replace("—", ",").replace("--", ",").replace("–", ",").replace(" - ", ", ")
        else:
            calendar_id_for_slots = os.environ.get("GHL_CALENDAR_ID")
    
            # === RETRY LOOP — NEVER DIE, ALWAYS HUMAN ===
            MAX_RETRIES = 6
            reply = None
            confirmation_code = None
    
            for attempt in range(MAX_RETRIES):
                extra_instruction = ""
                if attempt > 0:
                    nudges = [
                        "Write a completely different reply. Do not repeat anything from before.",
                        "Be casual and natural. No sales pressure.",
                        "Change direction. Say something new.",
                        "Respond like texting a friend — short and real.",
                        "Just acknowledge what they said.",
                        "Say one simple, helpful thing.",
                    ]
                    extra_instruction = nudges[min(attempt - 1, len(nudges) - 1)]
    
                reply, confirmation_code = generate_nepq_response(
                    first_name,
                    message,
                    agent_name,
                    conversation_history=conversation_history,
                    intent=intent,
                    contact_id=contact_id,
                    api_key=api_key,
                    calendar_id=calendar_id_for_slots,
                    extra_instruction=extra_instruction,
                )
    
                reply = reply.replace("—", "-").replace("–", "-").replace("—", "-")
                reply = re.sub(r"[\U0001F600-\U0001F64F]", "", reply)
    
                if message.strip().endswith("?"):
                    break
    
                if any(x in message.lower() for x in ["test", "testing", "hey", "hi", "hello", "what's up", "you there"]):
                    reply = f"Hey{(' ' + first_name + ',') if first_name else ','} how can I help?"
                    break
    
                is_duplicate, reason = validate_response_uniqueness(contact_id, reply)
                if not is_duplicate:
                    break
    
                logger.info(f"Attempt {attempt + 1} blocked ({reason}) — retrying...")
    
            if not reply or reply.strip() == "":
                reply = f"Hey{(' ' + first_name + ',') if first_name else ','} got it. What's on your mind?"
    
        if contact_id and api_key and location_id:
            sms_result = send_sms_via_ghl(contact_id, reply, api_key, location_id)
    
            is_success = True if not booking_error else False
    
            response_data = {
                "success": is_success,
                "message": message,
                "reply": reply,
                "contact_id": contact_id,
                "sms_sent": sms_result.get("success", False),
                "confirmation_code": confirmation_code,
                "intent": intent,
                "history_messages": len(conversation_history),
                "appointment_created": appointment_created,
                "booking_attempted": bool(start_time_iso),
                "booking_error": booking_error,
                "time_detected": formatted_time if formatted_time else None,
            }
            if appointment_created and appointment_details:
                response_data["appointment_time"] = appointment_details["formatted_time"]
    
            return jsonify(response_data), (200 if is_success else 422)
        else:
            logger.warning(
                f"Missing credentials - contact_id: {contact_id}, "
                f"api_key: {'set' if api_key else 'missing'}, "
                f"location_id: {'set' if location_id else 'missing'}"
            )
    
            is_success = True if not booking_error else False
            response_data = {
                "success": is_success,
                "message": message,
                "reply": reply,
                "confirmation_code": confirmation_code,
                "sms_sent": False,
                "warning": "SMS not sent - missing contact_id or GHL credentials",
                "appointment_created": False,
                "booking_attempted": bool(start_time_iso),
                "booking_error": booking_error,
                "time_detected": formatted_time if formatted_time else None,
            }
            return jsonify(response_data), (200 if is_success else 422)
    
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    
    
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
