from flask import Flask, request, jsonify
import os
import logging
import json
import requests
import csv
import io
import re
from datetime import datetime, date  # added 'date' here
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build

try:
    import psycopg2
    conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
    cur = conn.cursor()
    # 1) Ensure table exists first
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contact_qualification (
        contact_id TEXT PRIMARY KEY,
        topics_asked TEXT[] DEFAULT ARRAY[]::TEXT[],
        key_quotes TEXT[] DEFAULT ARRAY[]::TEXT[],
        blockers TEXT[] DEFAULT ARRAY[]::TEXT[],
        health_conditions TEXT[] DEFAULT ARRAY[]::TEXT[],
        health_details TEXT[] DEFAULT ARRAY[]::TEXT[],
        total_exchanges INTEGER DEFAULT 0,
        dismissive_count INTEGER DEFAULT 0,
        has_policy BOOLEAN,
        is_personal_policy BOOLEAN,
        is_employer_based BOOLEAN,
        is_term BOOLEAN,
        is_whole_life BOOLEAN,
        is_iul BOOLEAN,
        is_guaranteed_issue BOOLEAN,
        term_length INTEGER,
        face_amount TEXT,
        carrier TEXT,
        has_spouse BOOLEAN,
        num_kids INTEGER,
        tobacco_user BOOLEAN,
        age INTEGER,
        retiring_soon BOOLEAN,
        motivating_goal TEXT,
        has_other_policies BOOLEAN,
        medications TEXT,
        is_booked BOOLEAN DEFAULT FALSE,
        is_qualified BOOLEAN DEFAULT FALSE,
        appointment_time TEXT,
        already_handled BOOLEAN DEFAULT FALSE,
        objection_path TEXT,
        waiting_for_health BOOLEAN DEFAULT FALSE,
        waiting_for_other_policies BOOLEAN DEFAULT FALSE,
        waiting_for_goal BOOLEAN DEFAULT FALSE,
        carrier_gap_found BOOLEAN DEFAULT FALSE,
        appointment_declined BOOLEAN DEFAULT FALSE,
        waiting_for_medications BOOLEAN DEFAULT FALSE,
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
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS blockers TEXT[] DEFAULT ARRAY[]::TEXT[];")
    conn.commit()
    conn.close()
    print("DB fixed: ensured contact_qualification table + required columns")
except Exception as e:
    logging.warning(f"DB INIT WARNING: {e}")
finally:
    try:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()
    except Exception:
        pass
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

def parse_history_for_topics_asked(contact_id, conversation_history):
    if not contact_id or not conversation_history:
        return set()

    # Load current topics_asked from DB
    current_state = get_qualification_state(contact_id)  # from your original code
    existing_topics = set(current_state.get("topics_asked", [])) if current_state else set()

    AGENT_QUESTION_PATTERNS = {
        "motivation": [r"what (got|made|brought) you", r"why did you", r"what originally", r"what made you want"],
        "living_benefits": [r"living benefits", r"access.*while.*alive", r"accelerated.*benefit"],
        "portability": [r"(continue|keep).*after.*retire", r"portable", r"when you leave"],
        "employer_coverage": [r"through work", r"employer.*policy", r"job.*coverage"],
        "policy_type": [r"term or (whole|permanent)", r"what (kind|type) of policy"],
        "family": [r"(married|spouse)", r"(kids|children)"],
        "coverage_amount": [r"how much coverage", r"face amount"],
        "carrier": [r"who.*with", r"which (company|carrier)"],
        "health": [r"health conditions", r"taking.*medications"],
        "other_policies": [r"any other (policies|coverage)"],
    }

    topics_found = set()

    for msg in conversation_history:
        msg_lower = msg.lower() if isinstance(msg, str) else ""
        is_agent = "you:" in msg_lower or not msg_lower.startswith("lead:")
        if is_agent:
            for topic, patterns in AGENT_QUESTION_PATTERNS.items():
                if topic in existing_topics or topic in topics_found:
                    continue
                for pattern in patterns:
                    if re.search(pattern, msg_lower):
                        topics_found.add(topic)
                        break

    # Save new topics
    for topic in topics_found - existing_topics:
        mark_topic_asked(contact_id, topic)  # your original function

    return topics_found

def remove_dashes(text: str) -> str:
    """Remove all types of dashes from text and clean up spacing."""
    if not text:
        return text
    # Replace em dash, en dash, and hyphen with space
    text = text.replace("—", " ").replace("–", " ").replace("-", " ")
    # Collapse multiple spaces into one
    text = " ".join(text.split())
    return text

def add_to_qualification_array(contact_id, field, value):
    """
    Add a value to an array field in contact_qualification (topics_asked, blockers, etc.)
    Avoids duplicates. Safe for TEXT[] columns.
    """
    if not contact_id or not field or not value:
        return False

    allowed_fields = {
        'topics_asked', 'key_quotes',
        'blockers', 'health_conditions', 'health_details'
    }
    if field not in allowed_fields:
        logger.warning(f"Invalid array field '{field}' for add_to_qualification_array")
        return False

    try:
        import psycopg2
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
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
        conn.close()
        return True
    except Exception as e:
        logger.warning(f"Failed to add to {field}: {e}")
        return False
    
def get_qualification_state(contact_id):
    """Fetch the full qualification row for a contact (or create if missing)."""
    if not contact_id:
        return None
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM contact_qualification WHERE contact_id = %s", (contact_id,))
        row = cur.fetchone()
        if row:
            result = dict(row)
        else:
            # Create new record
            cur.execute("""
                INSERT INTO contact_qualification (contact_id)
                VALUES (%s)
                RETURNING *
            """, (contact_id,))
            row = cur.fetchone()
            result = dict(row) if row else {}
            conn.commit()
        conn.close()
        return result
    except Exception as e:
        logger.warning(f"Could not get qualification state: {e}")
        return {}

def update_qualification_state(contact_id, updates):
    """Update scalar fields (boolean, text, integer) for a contact."""
    if not contact_id or not updates:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor()
        # Ensure row exists
        cur.execute("""
            INSERT INTO contact_qualification (contact_id)
            VALUES (%s)
            ON CONFLICT (contact_id) DO NOTHING
        """, (contact_id,))
        # Build SET clause
        set_parts = [f"{k} = %s" for k in updates.keys()]
        set_parts.append("updated_at = CURRENT_TIMESTAMP")
        values = list(updates.values())
        values.append(contact_id)
        query = f"UPDATE contact_qualification SET {', '.join(set_parts)} WHERE contact_id = %s"
        cur.execute(query, values)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning(f"Could not update qualification state: {e}")
        return False

def extract_and_update_qualification(contact_id, message, conversation_history=None):
    """
    Extract key facts from the current message (and optionally history)
    and update the contact_qualification table permanently.
    This is what makes the bot remember answers forever.
    """
    if not contact_id or not message:
        return {}

    updates = {}
    all_text = message.lower()
    if conversation_history:
        all_text = " ".join([m.lower().replace("lead:", "").replace("you:", "") for m in conversation_history]) + " " + all_text

    # === COVERAGE STATUS ===
    if re.search(r"\b(have|got|already|yes)\b.*\b(coverage|policy|insurance|protected)\b", all_text):
        updates["has_policy"] = True
    if re.search(r"\b(no|don't|dont|never|not)\b.*\b(coverage|policy|insurance)\b", all_text):
        updates["has_policy"] = False

    # === POLICY SOURCE (employer vs personal) ===
    if re.search(r"\b(my own|personal|private|individual|not through work|not from work)\b", all_text):
        updates["is_personal_policy"] = True
        updates["is_employer_based"] = False
        add_to_qualification_array(contact_id, "topics_asked", "employer_coverage")
        
    if re.search(r"\b(through|from|at|via)\b.*\b(work|job|employer|company|group)\b", all_text):
        updates["is_employer_based"] = True
        updates["is_personal_policy"] = False
        add_to_qualification_array(contact_id, "topics_asked", "employer_coverage")
        
    # === POLICY TYPE ===
    if re.search(r"\bterm\b", all_text):
        updates["is_term"] = True
        add_to_qualification_array(contact_id, "topics_asked", "policy_type")
        
    if re.search(r"\bwhole life\b", all_text):
        updates["is_whole_life"] = True
        add_to_qualification_array(contact_id, "topics_asked", "policy_type")
        
    if re.search(r"\biul\b|indexed universal", all_text):
        updates["is_iul"] = True
        add_to_qualification_array(contact_id, "topics_asked", "policy_type")
        

    # === GUARANTEED ISSUE / FINAL EXPENSE ===
    if re.search(r"\b(guaranteed|no exam|colonial penn|globe life|gerber|aarp)\b", all_text):
        updates["is_guaranteed_issue"] = True

    # === FACE AMOUNT ===
    amount_match = re.search(r"\b(\$?(\d{1,3}(,\d{3})*|\d+)k?)\b", all_text)
    if amount_match:
        amount = amount_match.group(1).replace(",", "").replace("$", "")
        if "k" not in amount.lower():
            amount = str(int(int(amount) / 1000)) + "k" if int(amount) >= 1000 else amount
        updates["face_amount"] = amount.upper()

    # === CARRIER ===
    carrier = find_company_in_message(message)
    if carrier:
        updates["carrier"] = carrier

    # === FAMILY ===
    if re.search(r"\b(wife|husband|spouse|married)\b", all_text):
        updates["has_spouse"] = True
    if re.search(r"\b(single|divorced|widowed)\b", all_text):
        updates["has_spouse"] = False

    kids_match = re.search(r"\b(\d+)\b.*\b(kids|children|child)\b", all_text)
    if kids_match:
        updates["num_kids"] = int(kids_match.group(1))

    # === HEALTH CONDITIONS (add to array) ===
    health_found = []
    health_map = {
        "diabetes": r"diabetes|diabetic",
        "heart": r"heart|stent|bypass|cardiac",
        "cancer": r"cancer|tumor|chemo",
        "copd": r"copd|oxygen|breathing|emphysema",
        "stroke": r"stroke",
        "blood_pressure": r"blood pressure|hypertension",
        "sleep_apnea": r"sleep apnea|cpap",
    }
    for key, pattern in health_map.items():
        if re.search(pattern, all_text):
            health_found.append(key)
            add_to_qualification_array(contact_id, "health_conditions", key)

    # === TOBACCO ===
    if re.search(r"\b(smoke|tobacco|cigarette|vape|nicotine)\b", all_text):
        updates["tobacco_user"] = True
    if re.search(r"\b(don't|dont|never|quit|stopped)\b.*\b(smoke)\b", all_text):
        updates["tobacco_user"] = False

    # === MOTIVATING GOAL ===
    goal_map = {
        "add_coverage": r"add|more|additional|extra|supplement|on top",
        "cover_mortgage": r"mortgage|house|home.*(paid|cover)",
        "final_expense": r"final expense|funeral|burial|cremation",
        "family_protection": r"protect.*(family|wife|husband|kids)",
        "income_replacement": r"replace.*income|salary",
        "leave_legacy": r"leave.*(legacy|inheritance|something behind)"
    }
    for goal, pattern in goal_map.items():
        if re.search(pattern, all_text):
            updates["motivating_goal"] = goal
            break

    # === BLOCKERS ===
    if re.search(r"\b(too busy|swamped|no time)\b", all_text):
        add_to_qualification_array(contact_id, "blockers", "too_busy")
    if re.search(r"\b(too expensive|can't afford|cost)\b", all_text):
        add_to_qualification_array(contact_id, "blockers", "cost_concern")
    if re.search(r"\b(not interested|no thanks|already covered)\b", all_text):
        add_to_qualification_array(contact_id, "blockers", "not_interested")

    # Apply all scalar updates
    if updates:
        update_qualification_state(contact_id, updates)

    return updates

def mark_topic_asked(contact_id: str, topic: str):
    """Mark a topic as asked to prevent repeat questions in future replies."""
    if not contact_id or not topic:
        return

    try:
        import psycopg2
        from psycopg2.extras import Json

        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor()

        # Use UPSERT to add topic to topics_asked array if not already present
        cur.execute("""
            INSERT INTO contact_qualification (contact_id, topics_asked)
            VALUES (%s, %s::jsonb)
            ON CONFLICT (contact_id) 
            DO UPDATE SET 
                topics_asked = (
                    SELECT jsonb_agg(DISTINCT value)
                    FROM jsonb_array_elements(
                        COALESCE(contact_qualification.topics_asked, '[]'::jsonb) || %s::jsonb
                    )
                ),
                updated_at = CURRENT_TIMESTAMP
        """, (contact_id, Json([topic]), Json([topic])))

        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Marked topic '{topic}' as asked for contact {contact_id}")
    except Exception as e:
        logger.error(f"Failed to mark topic asked: {e}")

def send_sms_via_ghl(contact_id: str, message: str):
    if not contact_id or contact_id == "unknown":
        logger.warning("Invalid contact_id — cannot send SMS")
        return False

    api_key = os.environ.get("GHL_API_KEY")
    location_id = os.environ.get("GHL_LOCATION_ID")

    if not api_key or not location_id:
        logger.warning("GHL_API_KEY or GHL_LOCATION_ID missing — cannot send SMS")
        return False

    url = "https://services.leadconnectorhq.com/conversations/messages"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json"
    }

    payload = {
        "type": "SMS",
        "contactId": contact_id,
        "message": message,
        "locationId": location_id
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        if response.status_code in [200, 201]:
            logger.info(f"SMS sent successfully to {contact_id}")
            return True
        else:
            logger.error(f"GHL SMS failed: {response.status_code} {response.text}")
            return False
    except Exception as e:
        logger.error(f"SMS send exception: {e}")
        return False
def build_system_prompt(state: ConversationState, nlp_context: str, proven_patterns: str, underwriting_context: str):
    age = state.facts.get("age", "unknown")
    
    # Build blocked questions list (Python logic — outside f-string)
    blocked_questions = []
    topics_asked = state.topics_asked or set()
    
    if "employer_coverage" in topics_asked and state.facts.get("is_personal_policy"):
        blocked_questions.append("Employer/portability questions — this is a personal policy")
    if state.facts.get("has_living_benefits") is not None:
        blocked_questions.append("Living benefits status already known")
    if state.facts.get("carrier"):
        blocked_questions.append("Carrier already known")
    if state.facts.get("is_term") or state.facts.get("is_whole_life"):
        blocked_questions.append("Policy type already known")

    blocked_section = ""
    if blocked_questions:
        blocked_section = f"""
=== DO NOT ASK THESE (already known or irrelevant) ===
{chr(10).join('- ' + q for q in blocked_questions)}
=== ASKING THEM MAKES YOU SOUND ROBOTIC ===
"""

    return f"""
You are Mitchell — master life insurance advisor re-engaging cold leads (30+ days to 7+ months old).
These leads opted in before but went cold. Expect high resistance. Alot of objections. 

CRITICAL RULES:
- NEVER say "free" or "free policy review"
- NEVER accept "no thank you", "I'm covered", "not interested" as final — these are objections
- ONLY stop if they say: "STOP", "stop", "unsubscribe", "do not contact me", "remove me", "opt out"
- NEVER repeat a question that's already been asked or answered
- NEVER use em dashes (—), en dashes (–), or fancy punctuation — use standard hyphen - only
- If they ask about price early: "Great question! Let's hop on a quick call and go over options. Which works better — {get_available_slots()}?"
- DO NOT LET THEM GET OFF EASY — use NEPQ + Gap Selling to uncover gaps, expose consequences, and book calls
- Use the CONTEXT sections heavily to sound informed and human
- OVERCOME OBJECTIONS naturally — never say "I understand" or "I get it"
- BE READY FOR QUICK NO'S AND OBJECTIONS — have responses ready
- NEVER NEVER NEVER USE THE WORDS "following up" OR "checking in" — sounds robotic and salesy
- ALWAYS address objections with empathy and understanding, but keep steering back to booking a call
- Provide value in every message — new info, questions, insights
- Every message should have a valid reason for them to reply. Never send a closed statement.
- If client says they are "not covered" and "looking for coverage", you can be more direct about booking a call.

LEAD AGE: {age} ← USE THIS HEAVILY
- Personalize: "Most people your age...", "At {age}, rates are still good if we act now"
- Urgency: "Rates only go up with age"
- Product focus: under 50 → term/IUL; 50-64 → whole life; 65+ → final expense + living benefits

Known Facts:
{json.dumps(state.facts, indent=2)}

TOPICS ALREADY COVERED (NEVER RE-ASK):
{', '.join(topics_asked) if topics_asked else "None yet"}
GAP IDENTIFIED: {state.facts.get("gap_identified", False)}
VERBAL AGREEMENT: {state.facts.get("verbal_agreement", False)}

{blocked_section}

Response Style:
- Casual, friendly Texas vibe ("Hey", "Gotcha", "Mind if I ask")
- Short, natural SMS (1-3 sentences max)
- Use contractions: "you've", "I'm", "it's"
- First names sparingly, only for emphasis
- Not to use sales tactics; "rates are still solid if we lock something in soon." until a gap is found. OR they explicitely say they are "not covered" and "looking for coverage"
- every message should provide a valuable justification for you reaching out; new living benefits, cons of employment coverage ie retirement, layoffs, benefit changes, no ownership and more. 
- Find their specific need and tie it back to why they need to review their coverage now.
- ask what their goal was/is for life insurance and if they got/found/havent found what they're looking for? follow up with why?
- ask questions when it makes sense to ask questions, answer questions and finish with a question. 

Goal: Uncover gaps → expose consequences → book call naturally
GAP SELLING FOCUS:
- A gap is ANY difference between current reality and desired outcome
- Valid gaps include: missing living benefits, employer policy ending at retirement, inadequate coverage for family, term expiring, overpaying, no cash value growth
- Make inaction painful — ask consequence questions ("What happens if you retire and that coverage goes away?")
- The lead's perception is reality — if they feel the gap, it's real
NEPQ FRAMEWORK:

SALES METHODOLOGIES — Blend the best for this lead:
- NEPQ: Connect → Situation → Problem → Consequence → Qualify → Transition → Present → Commit
- Gap Selling: Current vs desired future — make inaction painful
- Straight Line Persuasion: Control flow, smooth objections
- Brian Tracy: Benefits, stories, assumptive close
- Never Split the Difference: Mirror, label, calibrated questions, "that's right"

CLOSING RULES — ONLY OFFER TIMES IF ALL 3 CONDITIONS MET:
1. You have identified a VALID GAP (e.g., missing living benefits, employer policy ends at retirement, inadequate coverage, overpriced carrier, health changes since policy issued)
2. The lead has shown VERBAL AGREEMENT ("yes", "sounds good", "interested", "let's do it", "tell me more", "I'm in")
3. You have asked for permission ("Mind if I share some times that work?" or "Would you be open to hopping on a quick call?")

If these 3 are not met — DO NOT OFFER TIMES. Continue discovery or objection handling.

When all 3 are met — offer exactly two specific times:
"Which works better — 2pm today or 11am tomorrow?"

Never ask open-ended "when works for you?"
Never offer times without a gap and agreement.

POLICY REVIEW TRIGGER:
"When was the last time you did a policy review to make sure you're not leaving money on the table?"

Proven Responses That Worked:
{proven_patterns}

Underwriting Guidance:
{underwriting_context}

Full Knowledge Base:
{get_all_knowledge()}

Final Rule: Always advance the sale. Short. Natural. Helpful.
When ready to book: "Which works better — {get_available_slots()}?"
"""
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    if not data:
        return jsonify({"status": "error", "error": "No JSON payload"}), 400

    data_lower = {k.lower(): v for k, v in data.items()}
    # GHL CUSTOM DATA — root-level fields from your screenshot
    contact_id = data_lower.get("contact_id", "unknown")
    first_name = data_lower.get("first_name", "there")
    # Safe message extraction — handles string or dict
    raw_message = data_lower.get("message", "")
    if isinstance(raw_message, dict):
        message = raw_message.get("body", "").strip()
    else:
        message = str(raw_message).strip()

    # Safety fallback for other GHL formats
    if contact_id == "unknown":
        contact_id = data_lower.get("contactid", "unknown")
    if contact_id == "unknown":
        nested = data_lower.get("contact", {})
        contact_id = nested.get("id") or "unknown"

    # DEBUG LOGS — keep until SMS sends
    logger.info(f"Raw payload keys: {list(data.keys())}")
    logger.info(f"Raw 'contact_id' value: {data.get('contact_id')}")
    logger.info(f"Final contact_id: '{contact_id}'")
    logger.info(f"First name: '{first_name}'")
    logger.info(f"Message: '{message}'")
    # === EXTRACT AGE FROM DATE_OF_BIRTH ===
    age = "unknown"
    contact = data_lower.get("contact", {})
    date_of_birth = contact.get("date_of_birth", "")
    if date_of_birth:
        try:
            from datetime import date
            dob_parts = date_of_birth.split("-")
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

    # === INITIAL OUTREACH (NO INBOUND MESSAGE) ===
    if not message:
        initial_reply = f"{first_name}, do you still have the other life insurance policy? there's some new living benefits that people have been asking about and I wanted to make sure yours didn't only pay out if you're dead."
        send_sms_via_ghl(contact_id, initial_reply)
        return jsonify({"status": "success", "reply": initial_reply})

    # === INBOUND MESSAGE PROCESSING ===
    save_nlp_message(contact_id, message, "lead")

    # Load or create qualification state
    qualification_state = get_qualification_state(contact_id)
    # In production: load conversation_history from DB
    conversation_history = []  # ← replace with real history load

    # Parse history to backfill topics_asked
    parse_history_for_topics_asked(contact_id, conversation_history)
    # Extract facts and update DB
    extract_and_update_qualification(contact_id, message)  # your existing function

    # Reload fresh state after updates
    qualification_state = get_qualification_state(contact_id)

    # Create conversation state for prompt
    state = ConversationState(contact_id=contact_id, first_name=first_name)
    state.facts = qualification_state or {}
    extract_facts_from_message(state, message)
    state.stage = detect_stage(state, message, [])
    state = ConversationState(contact_id=contact_id, first_name=first_name)
    state.facts = state.facts or {}
    state.topics_asked = state.topics_asked or set()
    # Track if a gap has been identified
    state.facts["gap_identified"] = state.facts.get("gap_identified", False)

    # Simple gap detection (expand as needed)
    gap_keywords = ["not enough", "expires", "no living benefits", "through work", "retire", "overpay", "too expensive", "doesn't cover"]
    if any(kw in message.lower() for kw in gap_keywords):
        state.facts["gap_identified"] = True

    # Track verbal agreement
    agreement_keywords = ["yes", "sounds good", "interested", "let's do it", "tell me more", "i'm in", "sure", "okay"]
    if any(kw in message.lower() for kw in agreement_keywords):
        state.facts["verbal_agreement"] = True  # ← Add this
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