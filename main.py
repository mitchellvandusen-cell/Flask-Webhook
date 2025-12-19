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
    cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS notes TEXT;")
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

from datetime import datetime, timedelta, time

def get_available_slots():
    """Fetch real available slots from Google Calendar for today and tomorrow."""
    if not calendar_service:
        # Fallback if no credentials
        return "2pm or 4pm today, or 11am tomorrow"

    now = datetime.utcnow()
    today_start = datetime.combine(now.date(), time.min).isoformat() + 'Z'
    tomorrow_end = (now.date() + timedelta(days=2)).isoformat() + 'T23:59:59Z'

    # Your desired working hours (adjust as needed)
    work_start = time(8, 0)  # 8am
    work_end = time(20, 0)   # 8pm
    slot_duration = timedelta(minutes=30)  # 30-min slots

    try:
        events_result = calendar_service.events().list(
            calendarId=GOOGLE_CALENDAR_ID or 'primary',
            timeMin=today_start,
            timeMax=tomorrow_end,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        busy_times = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))
            busy_times.append((datetime.fromisoformat(start.replace('Z', '+00:00'))),
                              datetime.fromisoformat(end.replace('Z', '+00:00')))

        # Generate possible slots
        possible_slots = []
        current = datetime.combine(now.date(), work_start)
        end_date = now.date() + timedelta(days=1)

        while current.date() <= end_date:
            if current >= now:  # Only future slots
                slot_end = current + slot_duration
                is_busy = any(
                    busy_start < slot_end and busy_end > current
                    for busy_start, busy_end in busy_times
                )
                if not is_busy:
                    time_str = current.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
                    day_str = "today" if current.date() == now.date() else "tomorrow"
                    possible_slots.append(f"{time_str} {day_str}")
            current += slot_duration
            if current.time() > work_end:
                current = datetime.combine(current.date() + timedelta(days=1), work_start)

        # Return top 3-4 available slots
        if possible_slots:
            if len(possible_slots) >= 3:
                return f"{possible_slots[0]}, {possible_slots[1]}, or {possible_slots[2]}"
            else:
                return " or ".join(possible_slots)
        else:
            return "11am, 2pm, or 4pm tomorrow"  # Ultimate fallback

    except Exception as e:
        logger.warning(f"Calendar fetch failed: {e}")
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
def fetch_ghl_contact_notes(contact_id: str) -> str:
    """Fetch contact notes from GHL contact details API"""
    if not contact_id or contact_id == "unknown":
        return ""

    api_key = os.environ.get("GHL_API_KEY")
    location_id = os.environ.get("GHL_LOCATION_ID")
    if not api_key or not location_id:
        logger.warning("Missing GHL API key or location ID for notes fetch")
        return ""

    url = f"https://services.leadconnectorhq.com/location/{location_id}/contact/details/{contact_id}/"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            notes = data.get("notes", "") or ""
            return notes.strip()[:400]  # Keep it short for SMS context
        else:
            logger.warning(f"GHL notes fetch failed: {response.status_code} {response.text}")
    except Exception as e:
        logger.warning(f"GHL notes exception: {e}")

    return ""

def handle_missed_appointment(contact_id: str, first_name: str, message: str = ""):
    """
    Focused re-engagement for missed appointments.
    - Empathy + reminder of value
    - Pulls notes from DB if available
    - Offers real available times from calendar
    - Assumes interest (they booked once)
    - Quick re-book
    """
    # Hard opt-out protection
    msg_lower = message.lower().strip()
    opt_out_phrases = ["stop", "unsubscribe", "remove me", "do not contact", "opt out", "cancel"]
    if any(phrase in msg_lower for phrase in opt_out_phrases) and len(msg_lower.split()) <= 3:
        reply = "Got it — you've been removed. Take care."
        reply = reply.replace("—", "-").replace("–", "-").replace("―", "-")
        send_sms_via_ghl(contact_id, reply)
        return reply

    # Pull notes from DB (if you have contact_qualification or notes field)
    notes = ""
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor()
        cur.execute("SELECT notes, motivating_goal FROM contact_qualification WHERE contact_id = %s", (contact_id,))
        row = cur.fetchone()
        if row and row[0]:
            notes = row[0].strip()
        if row and row[1]:
            notes = f" (you mentioned {row[1]})" if not notes else notes + f" — you mentioned {row[1]}"
        conn.close()
    except:
        notes = ""

    # First message — empathy + reminder + real times
    if not message:
        reply = f"Hey {first_name}, it's Mitchell — we had a call scheduled but looks like we missed each other. No worries, life happens!{notes} Still want to go over those options? Which works better — {get_available_slots()}?"
    else:
        # They replied — check for time preference or agreement
        if any(time_word in msg_lower for time_word in ["today", "tomorrow", "morning", "afternoon", "evening", "pm", "am"]):
            reply = f"Perfect — let's lock that in. I'll send a calendar invite right over. Talk soon!"
        elif any(agree in msg_lower for agree in ["yes", "sure", "sounds good", "interested", "let's do it", "okay"]):
            reply = f"Awesome — which works better: {get_available_slots()}?"
        elif any(no in msg_lower for no in ["no", "not", "busy", "can't"]):
            reply = "No problem at all — when's a better week for you? I can work around your schedule."
        else:
            reply = f"Got it — just want to make sure we're still good to find something that fits{notes}. Which works better — {get_available_slots()}?"

    # Clean reply (no em dashes)
    reply = reply.replace("—", "-").replace("–", "-").replace("―", "-")

    send_sms_via_ghl(contact_id, reply)
    return reply

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
        
def book_appointment(contact_id: str, first_name: str, selected_time: str):
    """Create a calendar event when lead agrees to a time"""
    if not calendar_service:
        logger.warning("No calendar_service — can't book")
        return False

    # Parse selected_time (e.g., "3pm tomorrow")
    try:
        from datetime import datetime, timedelta, time
        now = datetime.utcnow()
        time_str = selected_time.lower()
        if "tomorrow" in time_str:
            date = now.date() + timedelta(days=1)
        else:
            date = now.date()

        hour_map = {
            "11am": 11, "2pm": 14, "3pm": 15, "4pm": 16
        }
        hour = next((h for k, h in hour_map.items() if k in time_str), 15)  # default 3pm

        start_time = datetime.combine(date, time(hour, 0))
        end_time = start_time + timedelta(minutes=30)

        event = {
            'summary': f"Life Insurance Review - {first_name}",
            'description': f"Appointment with {first_name} (contact_id: {contact_id})",
            'start': {
                'dateTime': start_time.isoformat() + 'Z',
                'timeZone': 'America/Chicago',  # Change to your timezone
            },
            'end': {
                'dateTime': end_time.isoformat() + 'Z',
                'timeZone': 'America/Chicago',
            },
            'attendees': [
                {'email': 'mitchvandusenlife@gmail.com'},  # Your email
            ],
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 60},
                    {'method': 'popup', 'minutes': 10},
                ],
            },
        }

        event = calendar_service.events().insert(calendarId='primary', body=event).execute()
        logger.info(f"Appointment booked: {event.get('htmlLink')}")
        return True
    except Exception as e:
        logger.error(f"Failed to book appointment: {e}")
        return False
    
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
"Mitchells" job = State underwriter for insurance, I do not work for one single company, I work with over 90 companies to help get people,
the best coverage for their needs at the best price. My goal = uncover gaps in coverage → expose consequences of inaction → book call to review options.
if someone asks "who do you work for?" or "which company are you with?" always say "I am an underwriter with the state, I dont work with any one company or product I work with every carirer to make sure youre not leaving money on the table."

CRITICAL RULES:
- NEVER say "free" or "free policy review"
- NEVER accept "no thank you", "I'm covered", "not interested" as final — these are objections
- ONLY stop if they say: "STOP", "unsubscribe", "do not contact me", "remove me", "opt out"
- NEVER repeat a question that's already been asked or answered
- NEVER use em dashes (—), en dashes (–), or fancy punctuation.
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
- NEVER ASK "SAY NO" QUESTIONS — e.g., "Are you still interested?" or "Do you want to move forward?" "Are you still looking?" "Do you want life insurance?" — these lead to dead ends.NEVER NEVER NEVER!
- Use the underwriting context to address health objections and tie back to why they need to review now.
- Use the proven patterns to mimic successful responses.
- Use the NLP context to remember past answers and avoid repeating questions.

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
- Not to use sales tactics; "rates are still solid if we lock something in soon." until a gap is found. OR they explicitely say they are "not covered" and "looking for coverage" or "what coverage?" or "I dont have any"
- every message should provide a valuable justification for you reaching out; new living benefits, cons of employment coverage ie retirement, layoffs, benefit changes, no ownership and more. 
- Find their specific need and tie it back to why they need to review their coverage now.
- ask what their goal was/is for life insurance and if they got/found/havent found what they're looking for? follow up with why?
- ask questions when it makes sense to ask questions, answer questions and finish with a question. 
- When asking for something, use "never split the difference" technique: "Mind if I ask...", "Would it be a ridiculous idea to...", "You're going to hate me for asking, but..."
- Use stories and examples to illustrate points (Brian Tracy style)
- Use assumptive closes: "When we hop on the call...", "Once we get you reviewed..." <- if they show interest
- Use consequesnce questions to find gaps: "What happens if...", "How would that impact...", "What would you do if..."
- If someone responds "I need insurance.", "im interested", "I want to see options", "show me what you got", "lets look at options", "how much would it cost" Book the call, do NOT act, sound, react excitingly: this is normal, expected, and exactly what you're trying to get them to say.

DIVORCE / EX-SPOUSE RULES:
- Never assume current spouse or children with ex
- If lead mentions ex as beneficiary — clarify: "Got it — you want to make sure your ex is taken care of too?"
- If divorce mentioned — "Life changes like that often mean coverage needs updating. Who are you wanting to protect now?"
- Never use weird phrases like "lay an egg" — keep it natural

ASSUMPTION RULE:
- In sales, clarify — do not assume
- If family status unclear — ask: "Who are you looking to protect these days?"
- If beneficiaries unclear — ask: "Who would you want the coverage to go to?"

Goal: Uncover gaps → expose consequences → book call naturally
GAP SELLING FOCUS:
- A gap is ANY difference between current reality and desired outcome
- Valid gaps include: missing living benefits, loss of coverage from divorce, or previous financial harship (no longer in that hardship), employer policy ending at retirement, inadequate coverage for family, term expiring, overpaying, no cash value growth
- Make inaction painful — ask consequence questions ("What happens if you retire and that coverage goes away?")
- The lead's perception is reality — if they feel the gap, it's real
DIVORCE AS GAP:
- Losing coverage in divorce = major gap
- Common after divorce: no coverage, outdated beneficiaries, new family needs
- Treat "haven't had since divorce" as strong pain point
- Use consequence questions: "What would happen to [current family/kids] if something happened and there was no coverage in place?"
LOST JOB AS GAP:
- Losing employer coverage = major gap
- Common pain points: no ownership, limited benefits, coverage ends with job
- Use consequence questions: "If you were to leave that job, what would happen to your coverage?"
- Emphasize portability and ownership benefits of personal policies
DONT HAVE POLICY AS GAP:
- If they say "I don't have coverage" or "I'm not covered", treat as strong gap
- Use assumptive closes to book call quickly
- ask whats held them back from getting coverage so far
- Use consequence questions methodically to expose pain points of being uninsured
NEPQ FRAMEWORK:
- Problem awareness ("What made you start thinking about life insurance?")
- Implication questions ("How would that impact your family if...?")
- Need payoff questions ("If we could find something that fits your budget and covers what matters most to you, would that work?")
- Consequence questions to expose pain of inaction

FIND THE PROBLEM, HAMMER THE CONSEQUENCES, OFFER THE SOLUTION (BOOK THE CALL)

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
BUYING SIGNALS — CLOSE IMMEDIATELY (BOOK THE CALL) WHEN HEARD:
These phrases mean the lead is READY — offer times right away:
- "need to look at options"
- "want to see options"
- "let's look at some"
- "show me what you got"
- "what are the options"
- "how much would it be"
- "what would it cost"
- "interested in seeing"
- "tell me more"
- "sounds good"
- "let's do it"

When any of these appear:
- Respond with: "Perfect — if we could find something that fits your budget and covers what matters most, would that work for you?"
- If they say yes (or anything positive) → "Great — which works better: {get_available_slots()} tomorrow?"

Never miss these — they are strong intent to buy.

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
    gap_keywords = ["not enough", "expires", "don't have", "I need", "lost in the divorce", "Ive been looking", "I want to get it", "more coverage", "no living benefits", "through work", "retire", "overpay", "too expensive", "doesn't cover"]
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
from datetime import datetime

@app.route("/missed", methods=["POST"])
def missed_appointment_webhook():
    data = request.json or {}
    if not data:
        return jsonify({"status": "error", "error": "No JSON payload"}), 400

    data_lower = {k.lower(): v for k, v in data.items()}

    # Root-level custom fields from your GHL screenshot
    contact_id = data_lower.get("contact_id", "unknown")
    first_name = data_lower.get("first_name", "there")
    message = data_lower.get("message", "").strip()
    # After contact_id and first_name extraction
    notes = fetch_ghl_contact_notes(contact_id)

    # Fallback to motivating_goal from DB
    if not notes:
        try:
            conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
            cur = conn.cursor()
            cur.execute("SELECT motivating_goal FROM contact_qualification WHERE contact_id = %s", (contact_id,))
            row = cur.fetchone()
            if row and row[0]:
                notes = f"You mentioned {row[0].replace('_', ' ').title()}"
            conn.close()
        except:
            pass

    reminder = f" {notes}" if notes else ""

    if not message:
        reply = f"Hey {first_name}, it's Mitchell — we had a call scheduled but looks like we missed each other. No worries!{reminder} Still want to go over those options? Which works better — {get_available_slots()}?"
    # === PULL NOTES + MOTIVATING GOAL FROM DB ===
    notes = ""
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor()
        cur.execute("""
            SELECT notes, motivating_goal 
            FROM contact_qualification 
            WHERE contact_id = %s
        """, (contact_id,))
        row = cur.fetchone()
        if row:
            if row[0]:  # notes
                notes = row[0].strip()
            if row[1]:  # motivating_goal
                goal = row[1].replace("_", " ").title()
                notes = f"{notes} — you mentioned {goal}" if notes else f"you mentioned {goal}"
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to pull notes for missed appointment: {e}")

    # Normalize message for responses
    msg_lower = message.lower()

    # === HARD OPT-OUT (safe for "stop sending times") ===
    opt_out_phrases = ["stop", "unsubscribe", "remove me", "do not contact me", "opt out"]
    if any(phrase in msg_lower for phrase in opt_out_phrases) and len(msg_lower.split()) <= 3:
        reply = "Got it — you've been removed. Take care."
        send_sms_via_ghl(contact_id, reply.replace("—", "-"))
        return jsonify({"status": "success", "reply": reply})

    # === FIRST MESSAGE — empathy + reminder + real times ===
    if not message:
        reply = f"Hey {first_name}, it's Mitchell — we had a call scheduled but looks like we missed each other. No worries at all, life happens!{notes} Still want to go over those options? Which works better — {get_available_slots()}?"
    else:
        # === RESPONSE HANDLING ===
        if any(time_word in msg_lower for time_word in ["today", "tomorrow", "morning", "afternoon", "evening", "pm", "am", "o'clock"]):
            reply = "Perfect — let's lock that in. I'll send a calendar invite right over. Talk soon!"
        elif any(agree in msg_lower for agree in ["yes", "sure", "sounds good", "interested", "let's do it", "okay", "i'm in"]):
            reply = f"Awesome — which works better: {get_available_slots()}?"
        elif any(no in msg_lower for no in ["no", "not", "busy", "can't", "later"]):
            reply = "No problem — when's a better week for you? I can work around your schedule."
        else:
            reply = f"Got it — just want to make sure we're still good to find something that fits{notes}. Which works better — {get_available_slots()}?"

    # Clean reply
    reply = reply.replace("—", "-").replace("–", "-").replace("―", "-")

    send_sms_via_ghl(contact_id, reply)

    return jsonify({"status": "success", "reply": reply})
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)