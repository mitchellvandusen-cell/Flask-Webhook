from flask import Flask, request, jsonify
import os
import logging
import json
import requests
import csv
import io
import re
from datetime import datetime, date, time, timedelta, timezone
from openai import OpenAI
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

creds = Credentials.from_authorized_user_info(
    {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
    },
    scopes=["https://www.googleapis.com/auth/calendar"]
)

if creds.expired or not creds.valid:
    creds.refresh(Request())

calendar_service = build("calendar", "v3", credentials=creds)
logging.info("Google Calendar connected via OAuth2 refresh token")

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
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "fallback_secret")
GHL_CALENDAR_ID = os.environ.get("GHL_CALENDAR_ID")

app.secret_key = SESSION_SECRET

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
# === GOOGLE CALENDAR VIA OAUTH2 REFRESH TOKEN ===
calendar_service = None
try:
    creds = Credentials.from_authorized_user_info(
        {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "refresh_token": os.environ.get("GOOGLE_REFRESH_TOKEN"),
        },
        scopes=["https://www.googleapis.com/auth/calendar"]
    )

    if creds.expired or not creds.valid:
        creds.refresh(Request())

    calendar_service = build("calendar", "v3", credentials=creds)
    logger.info("Google Calendar connected via OAuth2 refresh token — SUCCESS")
except Exception as e:
    logger.error(f"Google Calendar OAuth2 failed: {e}")
    # === GOOGLE CALENDAR VIA OAUTH2 REFRESH TOKEN ===
    calendar_service = None
    try:
        if not os.environ.get("GOOGLE_CLIENT_ID") or not os.environ.get("GOOGLE_CLIENT_SECRET") or not os.environ.get("GOOGLE_REFRESH_TOKEN"):
            logger.error("Missing Google OAuth2 env vars (CLIENT_ID, CLIENT_SECRET, or REFRESH_TOKEN)")
        else:
            creds = Credentials.from_authorized_user_info(
                {
                    "client_id": os.environ["GOOGLE_CLIENT_ID"],
                    "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                    "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
                },
                scopes=["https://www.googleapis.com/auth/calendar"]
            )

            if creds.expired or not creds.valid:
                logger.info("Google OAuth2 token expired — refreshing")
                creds.refresh(Request())

            calendar_service = build("calendar", "v3", credentials=creds)
            logger.info("Google Calendar connected via OAuth2 — SUCCESS")
    except Exception as e:
        logger.error(f"Google Calendar OAuth2 setup failed: {e}")
        calendar_service = None
    

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

from datetime import datetime, date, time, timedelta, timezone

def create_ghl_appointment(contact_id: str, first_name: str, selected_time: str) -> bool:
    """Create an appointment in GoHighLevel when lead agrees to a time"""
    api_key = os.environ.get("GHL_API_KEY")
    location_id = os.environ.get("GHL_LOCATION_ID")
    if not api_key or not location_id:
        logger.error("Missing GHL credentials for appointment creation")
        return False

    # You need to set this to your GHL calendar ID (find in GHL → Calendars → your calendar → URL has calendarId=...)
    calendar_id = os.environ.get("GHL_CALENDAR_ID")  # e.g., "abc123xyz"

    if not calendar_id:
        logger.error("GHL_CALENDAR_ID not set — can't create appointment")
        return False

    # Parse selected_time into start time (you can make this smarter later)
    # For now, assume format like "2pm tomorrow" or "11am today"
    from datetime import datetime, timedelta, timezone
    now = datetime.now()
    if "tomorrow" in selected_time.lower():
        date = (now + timedelta(days=1)).date()
    else:
        date = now.date()

    time_map = {
        "11am": "11:00", "2pm": "14:00", "4pm": "16:00",
        "10:30": "10:30", "3pm": "15:00", "6pm": "18:00"
    }
    time_str = "14:00"  # default 2pm
    for key, val in time_map.items():
        if key in selected_time.lower():
            time_str = val
            break

    start_time = datetime.combine(date, datetime.strptime(time_str, "%H:%M").time())
    end_time = start_time + timedelta(minutes=30)

    url = "https://services.leadconnectorhq.com/appointments"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json"
    }

    payload = {
        "locationId": location_id,
        "calendarId": calendar_id,
        "contactId": contact_id,
        "startTime": start_time.isoformat(),
        "endTime": end_time.isoformat(),
        "title": f"Life Insurance Review - {first_name}",
        "appointmentStatus": "confirmed"
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code in [200, 201]:
            logger.info(f"GHL appointment created for {contact_id} at {start_time}")
            return True
        else:
            logger.error(f"GHL appointment failed: {response.status_code} {response.text}")
            return False
    except Exception as e:
        logger.error(f"GHL appointment exception: {e}")
        return False

def get_available_slots():
    if not calendar_service:
        logger.warning("No calendar_service — using fallback slots")
        return "11am, 2pm, or 4pm tomorrow"
    
    logger.info("Calendar service available — fetching real slots")

    try:
        # Use consistent UTC for everything
        now_utc = datetime.now(timezone.utc)
        today_utc = datetime.combine(now_utc.date(), time.min, tzinfo=timezone.utc)
        tomorrow_end_utc = today_utc + timedelta(days=2)

        events_result = calendar_service.events().list(
            calendarId='primary',
            timeMin=today_utc.isoformat(),
            timeMax=tomorrow_end_utc.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])

        busy_periods = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            end = event['end'].get('dateTime', event['end'].get('date'))

            if 'dateTime' in event['start']:
                start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
            else:
                # All-day event
                start_dt = datetime.fromisoformat(start + 'T00:00:00+00:00')
                end_dt = datetime.fromisoformat(end + 'T00:00:00+00:00')

            busy_periods.append((start_dt, end_dt))

        # Generate slots...
        local_offset = timedelta(hours=-6)  # Your timezone
        work_start_local = time(8, 0)
        work_end_local = time(20, 0)

        available_slots = []
        current_date_local = now_utc.astimezone(timezone(local_offset)).date()

        for day_offset in [0, 1]:
            target_date_local = current_date_local + timedelta(days=day_offset)
            current_local = datetime.combine(target_date_local, work_start_local)
            current_utc = current_local.astimezone(timezone.utc)

            while current_local.time() <= work_end_local:
                slot_end_local = current_local + timedelta(minutes=30)
                slot_end_utc = slot_end_local.astimezone(timezone.utc)

                if current_utc >= now_utc:
                    is_busy = any(
                        busy_start < slot_end_utc and busy_end > current_utc
                        for busy_start, busy_end in busy_periods
                    )
                    if not is_busy:
                        time_str = current_local.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
                        day_str = "today" if day_offset == 0 else "tomorrow"
                        available_slots.append(f"{time_str} {day_str}")

                current_local += timedelta(minutes=30)

        # === RETURN SLOTS (MUST BE HERE, NOT IN EXCEPT) ===
        if available_slots:
            top_3 = available_slots[:3]
            if len(top_3) >= 3:
                return f"{top_3[0]}, {top_3[1]}, or {top_3[2]}"
            else:
                return " or ".join(top_3)

        return "11am, 2pm, or 4pm tomorrow"

    except Exception as e:
        logger.error(f"Calendar failed: {e}")
        return "11am, 2pm, or 4pm tomorrow"
    
def parse_history_for_topics_asked(contact_id: str, conversation_history: list) -> set:
    """
    Scan conversation history for agent questions and mark new topics asked.
    Returns set of newly detected topics.
    """
    if not contact_id or not conversation_history:
        return set()

    # Load current known topics
    current_state = get_qualification_state(contact_id)
    existing_topics = set(current_state.get("topics_asked", []))

    AGENT_QUESTION_PATTERNS = {
        "motivation": [
            r"what (got|made|brought|triggered) you",
            r"why did you.*look",
            r"what originally",
            r"what made you want",
            r"something.*had you looking"
        ],
        "living_benefits": [
            r"living benefits?",
            r"access.*while.*alive",
            r"accelerated.*benefit",
            r"pay while.*alive"
        ],
        "portability": [
            r"(continue|keep|follow|portable).*after.*(retire|leave|job)",
            r"what happens.*when you (retire|leave|switch)"
        ],
        "employer_coverage": [
            r"through work",
            r"employer.*(policy|coverage)",
            r"job.*(covers|insurance)",
            r"group.*benefit"
        ],
        "policy_type": [
            r"(term|whole|permanent|universal).*or",
            r"what (kind|type) of policy"
        ],
        "family": [
            r"(married|spouse|wife|husband)",
            r"(kids|children|child)"
        ],
        "coverage_amount": [
            r"how much coverage",
            r"face amount",
            r"death benefit.*amount"
        ],
        "carrier": [
            r"who.*with",
            r"which (company|carrier)"
        ],
        "health": [
            r"health conditions?",
            r"taking.*medications",
            r"any medical"
        ],
        "other_policies": [
            r"any other (policies|coverage)",
            r"anything else"
        ],
    }

    topics_found = set()

    for msg in conversation_history:
        if not isinstance(msg, str):
            continue
        msg_lower = msg.lower()

        # More flexible agent message detection
        is_agent = any(prefix in msg_lower for prefix in ["you:", "mitchell:", "assistant:"]) or \
                   ("lead:" not in msg_lower and len(msg_lower.split()) > 5)

        if not is_agent:
            continue

        for topic, patterns in AGENT_QUESTION_PATTERNS.items():
            if topic in existing_topics or topic in topics_found:
                continue
            if any(re.search(pattern, msg_lower) for pattern in patterns):
                topics_found.add(topic)
                break  # one topic per message is enough

    # Batch save new topics
    new_topics = topics_found - existing_topics
    for topic in new_topics:
        mark_topic_asked(contact_id, topic)

    return new_topics

def handle_missed_appointment(contact_id: str, first_name: str, message: str = "") -> str:
    """
    Focused re-engagement for missed appointments.
    - Empathy + reminder of value
    - Pulls notes/motivating_goal from DB
    - Offers real available times
    - Assumes interest (they booked once)
    - Quick re-book path
    """
    if not contact_id or contact_id == "unknown":
        logger.warning("handle_missed_appointment called with invalid contact_id")
        return "Error processing request."

    msg_lower = message.lower().strip() if message else ""

    # === PULL PERSONALIZED NOTES ===
    notes = ""
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor()
        cur.execute("SELECT notes, motivating_goal FROM contact_qualification WHERE contact_id = %s", (contact_id,))
        row = cur.fetchone()
        conn.close()

        if row:
            db_notes = row[0].strip() if row[0] else ""
            goal = row[1].strip().replace("_", " ").title() if row[1] else ""

            if db_notes and goal:
                notes = f" {db_notes} — you mentioned {goal.lower()}"
            elif db_notes:
                notes = f" {db_notes}"
            elif goal:
                notes = f" — you mentioned {goal.lower()}"
    except Exception as e:
        logger.warning(f"Failed to fetch notes for missed appt {contact_id}: {e}")

    # === BUILD REPLY ===
    available_slots = get_available_slots()

    if not message:
        # First outreach after missed call
        reply = f"Hey {first_name}, it's Mitchell — looks like we missed each other on that call. No worries at all, life gets busy!{notes} Still want to go over those options and get you protected? Which works better — {available_slots}?"
    else:
        # They replied
        # More precise time detection
        if re.search(r"\b(tomorrow|today|morning|afternoon|evening|\d{1,2}(:\d{2})?\s*(am|pm)|o'?clock)\b", msg_lower):
            reply = "Perfect — let's lock that in. I'll send a calendar invite right over. Talk soon!"
            # Optional: trigger actual booking here if you parse time
        elif any(word in msg_lower for word in ["yes", "sure", "sounds good", "interested", "let's do it", "okay", "yeah", "works"]):
            reply = f"Awesome — which works better: {available_slots}?"
        elif any(word in msg_lower for word in ["no", "not", "busy", "can't", "later", "reschedule"]):
            reply = "No problem at all — totally understand. When's a better week for you? I can work around your schedule."
        else:
            reply = f"Got it — just want to make sure we're still good to find something that fits{notes}. Which works better — {available_slots}?"

    # === SEND REPLY ===
    send_sms_via_ghl(contact_id, reply)
    logger.info(f"Missed appointment re-engagement sent to {contact_id}: '{reply[:100]}...'")

    return reply
def add_to_qualification_array(contact_id: str, field: str, value: str) -> bool:
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

        # Safe: use parameterized query with explicit column reference (no f-string injection)
        cur.execute(f"""
            UPDATE contact_qualification
            SET {field} = ARRAY(
                SELECT DISTINCT unnest(
                    COALESCE({field}, ARRAY[]::TEXT[]) || ARRAY[%s]::TEXT[]
                )
            ),
            updated_at = CURRENT_TIMESTAMP
            WHERE contact_id = %s
        """, (value, contact_id))

        # If no row existed, insert it
        if cur.rowcount == 0:
            cur.execute(f"""
                INSERT INTO contact_qualification (contact_id, {field})
                VALUES (%s, ARRAY[%s]::TEXT[])
            """, (contact_id, value))

        conn.commit()
        conn.close()
        logger.info(f"Added '{value}' to {field} for contact {contact_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to add to {field} for {contact_id}: {e}")
        try:
            conn.rollback()
        except:
            pass
        finally:
            try:
                conn.close()
            except:
                pass
        return False
    
from datetime import datetime, date, time, timedelta, timezone
def make_json_serializable(obj):
    """Convert datetime objects to ISO strings for JSON serialization"""
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat() if obj else None
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_serializable(item) for item in obj]
    return obj

def get_qualification_state(contact_id: str) -> dict:
    """Fetch the full qualification row for a contact (or create if missing)."""
    if not contact_id or contact_id == "unknown":
        return {}

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
            # Create new record if doesn't exist
            cur.execute("""
                INSERT INTO contact_qualification (contact_id, total_exchanges)
                VALUES (%s, 0)
                RETURNING *
            """, (contact_id,))
            row = cur.fetchone()
            result = dict(row)
            conn.commit()

        conn.close()
        return result

    except Exception as e:
        logger.warning(f"Could not get qualification state for {contact_id}: {e}")
        try:
            conn.close()
        except:
            pass
        return {}

def update_qualification_state(contact_id: str, updates: dict) -> bool:
    """Update scalar fields (boolean, text, integer) for a contact."""
    if not contact_id or not updates:
        return False

    try:
        import psycopg2
        from psycopg2.extras import DictCursor

        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor(cursor_factory=DictCursor)

        # Ensure the row exists (UPSERT base)
        cur.execute("""
            INSERT INTO contact_qualification (contact_id)
            VALUES (%s)
            ON CONFLICT (contact_id) DO NOTHING
        """, (contact_id,))

        if not updates:
            conn.commit()
            conn.close()
            return True

        # Use parameterized query with explicit column list for safety
        columns = list(updates.keys())
        set_clause = ", ".join([f"{col} = %s" for col in columns])
        set_clause += ", updated_at = CURRENT_TIMESTAMP"

        values = list(updates.values())
        values.append(contact_id)  # for WHERE clause

        query = f"""
            UPDATE contact_qualification
            SET {set_clause}
            WHERE contact_id = %s
        """

        cur.execute(query, values)

        updated = cur.rowcount > 0
        conn.commit()
        conn.close()

        logger.info(f"Updated qualification for {contact_id}: {list(updates.keys())}")
        return updated

    except Exception as e:
        logger.error(f"Failed to update qualification state for {contact_id}: {e}")
        try:
            conn.rollback()
        except:
            pass
        finally:
            try:
                conn.close()
            except:
                pass
        return False

def extract_and_update_qualification(contact_id, message, conversation_history=None):
    """
    Extract key facts from the current message (and optionally history)
    and update the contact_qualification table permanently.
    """
    if not contact_id or not message:
        return {}

    updates = {}
    all_text = message.lower()
    if conversation_history:
        all_text = " ".join([m.lower().replace("lead:", "").replace("you:", "") for m in conversation_history]) + " " + all_text

    msg_lower = message.lower()

    # === COVERAGE STATUS (more precise with context) ===
    if re.search(r"\b(i have|yes i have|got|already have|yes)\b.*\b(life insurance|life coverage|life policy)\b", all_text):
        updates["has_policy"] = True
    elif re.search(r"\b(no|don't have|dont have|never had|not covered|no life)\b.*\b(life insurance|life coverage|life policy)\b", all_text):
        updates["has_policy"] = False

    # === POLICY SOURCE ===
    if re.search(r"\b(my own|personal|private|individual|not through work|not from work|own policy)\b", all_text):
        updates["is_personal_policy"] = True
        updates["is_employer_based"] = False
        add_to_qualification_array(contact_id, "topics_asked", "employer_coverage")

    if re.search(r"\b(through|from|at|via)\b.*\b(work|job|employer|company|group|benefit)\b", all_text):
        updates["is_employer_based"] = True
        updates["is_personal_policy"] = False
        add_to_qualification_array(contact_id, "topics_asked", "employer_coverage")

    # === POLICY TYPE ===
    if re.search(r"\bterm\b", all_text) and not re.search(r"\breturn of premium\b", all_text):
        updates["is_term"] = True
        add_to_qualification_array(contact_id, "topics_asked", "policy_type")

    if re.search(r"\bwhole life\b", all_text):
        updates["is_whole_life"] = True
        add_to_qualification_array(contact_id, "topics_asked", "policy_type")

    if re.search(r"\biul\b|indexed universal|universal life indexed", all_text):
        updates["is_iul"] = True
        add_to_qualification_array(contact_id, "topics_asked", "policy_type")

    # === GUARANTEED ISSUE ===
    if re.search(r"\b(guaranteed|no exam|no medical|colonial penn|globe life|gerber|aarp|final expense|burial)\b", all_text):
        updates["is_guaranteed_issue"] = True

    # === FACE AMOUNT (more robust) ===
    amount_patterns = [
        r'\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(million|billion)?\s*k?',
        r'\b(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(million|billion)?\s*k?\b'
    ]
    for pattern in amount_patterns:
        match = re.search(pattern, all_text)
        if match:
            amount_str = match.group(1).replace(",", "")
            multiplier = match.group(2).lower() if match.group(2) else ""
            amount = float(amount_str)
            if "million" in multiplier:
                amount *= 1000000
            elif "billion" in multiplier:
                amount *= 1000000000
            # Convert to "500k" format
            if amount >= 1000:
                amount_k = round(amount / 1000)
                updates["face_amount"] = f"{amount_k}k"
            else:
                updates["face_amount"] = str(int(amount))
            break

    # === CARRIER ===
    carrier = find_company_in_message(message)
    if carrier:
        updates["carrier"] = carrier

    # === FAMILY STATUS (with negatives) ===
    if re.search(r"\b(wife|husband|spouse|married|partner)\b", all_text):
        updates["has_spouse"] = True
    elif re.search(r"\b(single|divorced|widowed|no spouse|no wife|no husband)\b", all_text):
        updates["has_spouse"] = False

    kids_match = re.search(r"\b(\d+)\b.*\b(kids?|children|child|son|daughter)\b", all_text)
    if kids_match:
        updates["num_kids"] = int(kids_match.group(1))
    elif re.search(r"\b(no kids|no children|don't have kids|zero kids)\b", all_text):
        updates["num_kids"] = 0

    # === AGE EXTRACTION ===
    age_match = re.search(r"\b(i'?m|\bam)\s+(\d{1,2})\b", all_text)
    if age_match:
        age_num = int(age_match.group(2))
        if 18 <= age_num <= 100:  # reasonable range
            updates["age"] = age_num

    # === HEALTH CONDITIONS (with negatives) ===
    health_map = {
        "diabetes": r"\bdiabetes|diabetic",
        "heart": r"\bheart.*(attack|issue|problem|condition|stent|bypass|cardiac)",
        "cancer": r"\bcancer|tumor|chemo|radiation",
        "copd": r"\bcopd|emphysema|oxygen|breathing.*issue",
        "stroke": r"\bstroke",
        "blood_pressure": r"\b(high blood pressure|hypertension)",
        "sleep_apnea": r"\bsleep apnea|cpap",
    }
    for key, pattern in health_map.items():
        if re.search(pattern, all_text):
            add_to_qualification_array(contact_id, "health_conditions", key)
        # Optional: add negative detection
        # elif re.search(fr"\bno\s+{key}\b", all_text):
        #     # Could remove from array if previously added

    # === TOBACCO (with stronger negative detection) ===
    if re.search(r"\b(smoke|cigarette|vape|tobacco|nicotine)\b", all_text) and not re.search(r"\b(quit|stopped|used to|don't|never)\b", all_text):
        updates["tobacco_user"] = True
    elif re.search(r"\b(don't|dont|never|quit|stopped|no longer)\b.*\b(smoke|cigarette|vape)\b", all_text):
        updates["tobacco_user"] = False

    # === MOTIVATING GOAL (expanded) ===
    goal_map = {
        "income_replacement": r"replace.*income|salary|paycheck|if I die|breadwinner",
        "family_protection": r"protect.*(family|wife|husband|kids|children)",
        "cover_mortgage": r"mortgage|house.*(paid|cover)|home loan",
        "final_expense": r"final expense|funeral|burial|cremation|when I pass",
        "debt_payoff": r"debt|pay off.*(loan|credit|card)",
        "college_fund": r"college|education|school|tuition|kids.? college",
        "leave_legacy": r"leave.*(legacy|inheritance|something behind|pass on)",
        "retirement_supplement": r"retirement|retire|golden years",
        "add_coverage": r"add|more|additional|increase|supplement|on top"
    }
    for goal, pattern in goal_map.items():
        if re.search(pattern, all_text):
            updates["motivating_goal"] = goal
            break

    # === BLOCKERS ===
    if re.search(r"\b(too busy|swamped|no time|crazy schedule)\b", all_text):
        add_to_qualification_array(contact_id, "blockers", "too_busy")
    if re.search(r"\b(too expensive|can't afford|cost|price|budget|money)\b", all_text):
        add_to_qualification_array(contact_id, "blockers", "cost_concern")
    if re.search(r"\b(not interested|no thanks|already covered|all set|I'm good|handled)\b", all_text):
        add_to_qualification_array(contact_id, "blockers", "not_interested")
    if re.search(r"\b(need to think|talk to spouse|sleep on it|consider)\b", all_text):
        add_to_qualification_array(contact_id, "blockers", "need_to_think")

    # Apply updates
    if updates:
        update_qualification_state(contact_id, updates)

    return updates

def mark_topic_asked(contact_id: str, topic: str):
    """Mark a topic as asked to prevent repeat questions in future replies."""
    if not contact_id or not topic:
        return

    try:
        import psycopg2

        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor()

        # Correct way to append to TEXT[] column without duplicates
        cur.execute("""
            INSERT INTO contact_qualification (contact_id, topics_asked)
            VALUES (%s, ARRAY[%s]::TEXT[])
            ON CONFLICT (contact_id) DO UPDATE
            SET topics_asked = (
                SELECT ARRAY(
                    SELECT DISTINCT unnest(
                        COALESCE(contact_qualification.topics_asked, ARRAY[]::TEXT[]) || ARRAY[%s]::TEXT[]
                    )
                )
            ),
            updated_at = CURRENT_TIMESTAMP
        """, (contact_id, topic, topic))

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

    try:
        from datetime import datetime, date, time, timedelta, timezone
        now = datetime.now(timezone.utc)
        time_str = selected_time.lower().strip()

        # Determine date: today or tomorrow
        if any(word in time_str for word in ["tomorrow", "tmr", "tom", "next day"]):
            target_date = (now + timedelta(days=1)).date()
        else:
            target_date = now.date()

        # Extract hour and minute with flexible parsing
        hour = 15  # default 3pm
        minute = 0

        # Look for explicit times like "2pm", "2:30", "3 o'clock", "3:30pm"
        time_patterns = [
            r'(\d{1,2}):?(\d{2})?\s*(pm|p\.m\.|am|a\.m\.|o\'?clock)?',
            r'(\d{1,2})\s*(pm|p\.m\.|am|a\.m\.|o\'?clock)'
        ]

        for pattern in time_patterns:
            match = re.search(pattern, time_str)
            if match:
                h = int(match.group(1))
                m = int(match.group(2)) if match.group(2) else 0
                period = match.group(3).lower() if match.group(3) else ""

                if period in ["pm", "p.m."] and h != 12:
                    h += 12
                elif period in ["am", "a.m."] and h == 12:
                    h = 0

                hour = h
                minute = m
                break

        # Fallback: if words like "morning", "afternoon", "evening"
        if hour == 15:  # no explicit time found
            if any(word in time_str for word in ["morning", "am"]):
                hour = 11  # 11am
            elif any(word in time_str for word in ["afternoon", "pm"]):
                hour = 14  # 2pm
            elif any(word in time_str for word in ["evening", "night"]):
                hour = 18  # 6pm

        # Clamp to reasonable hours (8am - 8pm)
        hour = max(8, min(20, hour))

        start_time = datetime.combine(target_date, time(hour, minute))
        end_time = start_time + timedelta(minutes=30)

        event = {
            'summary': f"Life Insurance Review - {first_name}",
            'description': f"Appointment with {first_name} (contact_id: {contact_id})\nSelected time: {selected_time}",
            'start': {
                'dateTime': start_time.isoformat() + 'Z',
                'timeZone': 'America/Chicago',
            },
            'end': {
                'dateTime': end_time.isoformat() + 'Z',
                'timeZone': 'America/Chicago',
            },
            'attendees': [
                {'email': 'mitchvandusenlife@gmail.com'},
            ],
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 60},
                    {'method': 'popup', 'minutes': 10},
                ],
            },
        }

        created_event = calendar_service.events().insert(calendarId='primary', body=event).execute()
        logger.info(f"Appointment booked: {created_event.get('htmlLink')}")
        return True

    except Exception as e:
        logger.error(f"Failed to book appointment: {e}")
        return False

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
    
def build_system_prompt(state: ConversationState, nlp_context: str, proven_patterns: str, underwriting_context: str, is_follow_up: bool = False, follow_up_num: int = 0):
    age = state.facts.get("age", "unknown")
    
    # Build blocked questions list
    blocked_questions = []
    topics_asked = set(state.topics_asked) if hasattr(state, 'topics_asked') and state.topics_asked else set()
    
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

    # === STRONG FOLLOW-UP PROTECTION ===
    follow_up_section = ""
    if is_follow_up and follow_up_num > 0:
        follow_up_section = f"""
=== THIS IS FOLLOW-UP #{follow_up_num} AFTER NO RESPONSE ===
- DO NOT repeat or closely paraphrase the initial outreach message.
- The first message asked: "do you still have the other life insurance policy?" and mentioned new living benefits.
- You MUST create a completely fresh, different opener and angle.
- Good new angles: current favorable rates, family protection needs, no-exam options, quick 15-min review, living benefits value, policy review urgency.
- Vary the structure and opening every single time.
- Be natural, conversational, and curious.
- Always end with a soft question or value hook to encourage a reply.
"""

    return f"""
You are Mitchell — master life insurance advisor re-engaging cold leads (30+ days to 7+ months old).
These leads opted in before but went cold. Expect high resistance. Lots of objections. 
"Mitchells" job = State underwriter for insurance, I do not work for one single company, I work with over 90 companies to help get people the best coverage for their needs at the best price. My goal = uncover gaps in coverage → expose consequences of inaction → book call to review options.
if someone asks "who do you work for?" or "which company are you with?" always say "I am an underwriter with the state, I dont work with any one company or product I work with every carrier to make sure youre not leaving money on the table."

{follow_up_section}

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
{json.dumps(make_json_serializable(state.facts), indent=2)}

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
- Not to use sales tactics; "rates are still solid if we lock something in soon." until a gap is found. OR they explicitly say they are "not covered" and "looking for coverage" or "what coverage?" or "I dont have any"
- every message should provide a valuable justification for you reaching out; new living benefits, cons of employment coverage ie retirement, layoffs, benefit changes, no ownership and more. 
- Find their specific need and tie it back to why they need to review their coverage now.
- ask what their goal was/is for life insurance and if they got/found/havent found what they're looking for? follow up with why?
- ask questions when it makes sense to ask questions, answer questions and finish with a question. 
- When asking for something, use "never split the difference" technique: "Mind if I ask...", "Would it be a ridiculous idea to...", "You're going to hate me for asking, but..."
- Use stories and examples to illustrate points (Brian Tracy style)
- Use assumptive closes: "When we hop on the call...", "Once we get you reviewed..." <- if they show interest
- Use consequence questions to find gaps: "What happens if...", "How would that impact...", "What would you do if..."
- If someone responds "I need insurance.", "im interested", "I want to see options", "show me what you got", "lets look at options", "how much would it cost" Book the call, do NOT act, sound, react excitedly: this is normal, expected, and exactly what you're trying to get them to say.
- If previous message was "are you still with that other life insurance policy? Theres some new living benefits people have been asking me about and I wanted to make sure yours doesnt just pay out when you die?" Create a new engaging question that has a high likelihood of response.

LIVING BENEFITS PROBE — ALWAYS ASK AFTER "YES" TO OPENER:
- If lead confirms they have a policy ("yes", "still have it", "have one", "got one")
- Your NEXT message MUST probe living benefits:
  - "Okay — are you certain yours has living benefits where you can access the money if you become terminally, chronically, or critically ill?"
  - Or "Got it — you wouldn't want to pay for something that only pays when you die, right? Are you sure yours has living benefits?"
- This is the #1 gap — do not skip it
- Do not move to other questions until living benefits status is known

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
- Valid gaps include: missing living benefits, loss of coverage from divorce, or previous financial hardship (no longer in that hardship), employer policy ending at retirement, inadequate coverage for family, term expiring, overpaying, no cash value growth
- Make inaction painful — ask consequence questions ("What happens if you retire and that coverage goes away?")
- The lead's perception is reality — if they feel the gap, it's real

[All the rest of your excellent prompt remains exactly as written — no changes needed below]

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
    contact_id = data_lower.get("contact_id") or data_lower.get("contactid") or data_lower.get("contact", {}).get("id") or "unknown"
    first_name = data_lower.get("first_name", "there")
    raw_message = data_lower.get("message", "")
    message = raw_message.get("body", "").strip() if isinstance(raw_message, dict) else str(raw_message).strip()

    logger.info(f"WEBHOOK | ID: {contact_id} | Name: {first_name} | Msg: '{message}'")

    # === EXTRACT AGE ===
    age = "unknown"
    contact = data_lower.get("contact", {})
    date_of_birth = contact.get("date_of_birth", "")
    if date_of_birth:
        try:
            from datetime import datetime, date, time, timedelta, timezone
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
            logger.warning(f"Could not parse DOB: {e}")

    # Load qualification state early
    qualification_state = get_qualification_state(contact_id)
    total_exchanges = qualification_state.get('total_exchanges', 0)

    # === OUTBOUND / FIRST CONTACT / FOLLOW-UP HANDLING ===
    if not message:
        logger.info(f"OUTBOUND | total_exchanges: {total_exchanges}")

        if total_exchanges == 0:
            # FIRST MESSAGE EVER — YOUR EXACT VERBATIM TEXT
            reply_text = f"{first_name}, do you still have the other life insurance policy? there's some new living benefits that people have been asking about and I wanted to make sure yours didn't only pay out if you're dead."
            
            send_sms_via_ghl(contact_id, reply_text)
            update_qualification_state(contact_id, {'total_exchanges': 1})
            logger.info("First message sent — total_exchanges = 1")
            return jsonify({"status": "success", "reply": reply_text, "first_send": True})

        # FOLLOW-UP — continue to Grok generation below
        is_follow_up = True
        follow_up_num = total_exchanges
    else:
        # INBOUND MESSAGE
        save_nlp_message(contact_id, message, "lead")
        extract_and_update_qualification(contact_id, message)
        is_follow_up = False
        follow_up_num = 0

    # === RELOAD STATE AFTER ANY UPDATES ===
    qualification_state = get_qualification_state(contact_id)
    total_exchanges = qualification_state.get('total_exchanges', 0)

    # === BUILD CONVERSATION STATE ===
    state = ConversationState(contact_id=contact_id, first_name=first_name)
    state.facts = qualification_state or {}
    state.topics_asked = qualification_state.get('topics_asked', [])
    state.stage = detect_stage(state, message or "", [])
    state.exchange_count = total_exchanges

    # === BUILD CONTEXT ===
    similar_patterns = find_similar_successful_patterns(message or "")
    proven_patterns = format_patterns_for_prompt(similar_patterns)
    nlp_context = format_nlp_for_prompt(contact_id)

    underwriting_context = "No health conditions mentioned."
    if message:
        health_keywords = ["medication", "pill", "health", "condition", "diabetes", "cancer", "heart", "stroke", "copd", "blood pressure", "cholesterol"]
        if any(kw in message.lower() for kw in health_keywords):
            condition_word = next((w for w in message.lower().split() if w in health_keywords), "health")
            product_hint = ""
            full_context = message.lower() + " " + " ".join(str(v).lower() for v in state.facts.values() if v)
            if any(term in full_context for term in ["whole", "permanent", "cash value", "final expense"]):
                product_hint = "whole life"
            elif any(term in full_context for term in ["term", "iul", "indexed", "universal"]):
                product_hint = "term iul"
            matches = search_underwriting(condition_word, product_hint)
            if matches:
                underwriting_context = "Top Matching Carriers/Options:\n" + "\n".join([
                    f"- {' | '.join([str(c).strip() for c in row[:6] if c])}" for row in matches
                ])

    # === GAP & AGREEMENT DETECTION ===
    if message:
        gap_keywords = ["not enough", "expires", "don't have", "I need", "lost in the divorce", "Ive been looking", "I want to get it", "more coverage", "no living benefits", "through work", "retire", "overpay", "too expensive", "doesn't cover", "canceled", "what life insurance?", "got too expensive"]
        if any(kw in message.lower() for kw in gap_keywords):
            state.facts["gap_identified"] = True

        agreement_keywords = ["yes", "sounds good", "interested", "let's do it", "tell me more", "i'm in", "sure", "okay"]
        if any(kw in message.lower() for kw in agreement_keywords):
            state.facts["verbal_agreement"] = True

    # === BUILD SYSTEM PROMPT WITH FOLLOW-UP PROTECTION ===
    system_prompt = build_system_prompt(
        state, nlp_context, proven_patterns, underwriting_context,
        is_follow_up=is_follow_up, follow_up_num=follow_up_num
    )

    # === GROK MESSAGES ===
    messages = [{"role": "system", "content": system_prompt}]
    if message:
        messages.append({"role": "user", "content": message})

    # === GROK GENERATION ===
    if not client:
        reply = "Mind sharing, when was the last time you reviewed your coverage?"
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
            reply = "Got it, quick question, when was the last time you checked your policy?"

    if not reply or len(reply) < 5:
        reply = "I hear you, most people haven't reviewed in years. Mind if I ask when you last looked?"

    # === SEND REPLY ===
    send_sms_via_ghl(contact_id, reply)

    # === INCREMENT COUNTER ===
    update_qualification_state(contact_id, {'total_exchanges': total_exchanges + 1})
    logger.info(f"Message sent — total_exchanges now: {total_exchanges + 1}")

    return jsonify({
        "status": "success",
        "reply": reply,
        "is_follow_up": is_follow_up,
        "total_exchanges_after": total_exchanges + 1
    })

@app.route("/missed", methods=["POST"])
def missed_appointment_webhook():
    data = request.json or {}
    if not data:
        return jsonify({"status": "error", "error": "No JSON payload"}), 400

    data_lower = {k.lower(): v for k, v in data.items()}
    contact_id = data_lower.get("contact_id", "unknown")
    first_name = data_lower.get("first_name", "there")

    # Safe message extraction
    raw_message = data_lower.get("message", "")
    if isinstance(raw_message, dict):
        message = raw_message.get("body", "").strip()
    else:
        message = str(raw_message).strip()

    logger.info(f"MISSED APPT WEBHOOK | ID: {contact_id} | Name: {first_name} | Msg: '{message}'")

    if contact_id == "unknown":
        return jsonify({"status": "error", "error": "Invalid contact"}), 400

    msg_lower = message.lower() if message else ""

    # === PERSONALIZED CONTEXT ===
    notes_context = ""
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor()
        cur.execute("SELECT notes, motivating_goal FROM contact_qualification WHERE contact_id = %s", (contact_id,))
        row = cur.fetchone()
        conn.close()

        if row:
            db_notes = row[0].strip() if row[0] else ""
            goal = row[1].strip().replace("_", " ").title() if row[1] else ""
            parts = [p for p in [db_notes, f"you mentioned {goal.lower()}"] if p]
            if parts:
                notes_context = " " + " — ".join(parts)
    except Exception as e:
        logger.warning(f"Failed to fetch notes for missed appt {contact_id}: {e}")

    available_slots = get_available_slots()

    # === FIRST RE-ENGAGEMENT ===
    if not message:
        reply = f"Hey {first_name}, it's Mitch, looks like we missed each other on that call. No worries, life gets busy!{notes_context} Still want to review your options? Which works better — {available_slots}?"
    else:
        # More accurate time detection
        if re.search(r"\b(tomorrow|today|morning|afternoon|evening|\d{1,2}(:\d{2})?\s*(am|pm))\b", msg_lower):
            if create_ghl_appointment(contact_id, first_name, message):
                reply = "Perfect — appointment booked! I'll send the details shortly."
            else:
                reply = "That time looks busy — how about another slot?"
            # Future: parse exact time and call book_appointment()
        elif any(word in msg_lower for word in ["yes", "sure", "sounds good", "interested", "let's do it", "okay", "yeah", "good"]):
            reply = f"Awesome, which works better: {available_slots}?"
        elif any(word in msg_lower for word in ["no", "not", "busy", "can't", "later", "next week"]):
            reply = "No problem, totally understand. When's a better week for you?"
        else:
            reply = f"Got it — just checking we're still good to find something that fits{notes_context}. Which works better — {available_slots}?"

    send_sms_via_ghl(contact_id, reply)
    logger.info(f"Missed appt reply sent: '{reply[:100]}...'")

    return jsonify({"status": "success", "reply": reply})

@app.route("/debug_calendar")
def debug_calendar():
    if not calendar_service:
        return {"error": "calendar_service is None — OAuth2 failed"}

    try:
        events_result = calendar_service.events().list(
            calendarId='primary',
            timeMin=datetime.now(timezone.utc).isoformat() + 'Z',
            timeMax=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat() + 'Z',
            singleEvents=True,
            maxResults=5
        ).execute()

        events = events_result.get('items', [])
        return {
            "status": "success",
            "events_count": len(events),
            "events": [{"summary": e.get('summary'), "start": e.get('start')} for e in events]
        }
    except Exception as e:
        return {"error": str(e)}
    
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)