# tasks.py - The Background Engine (2026) - FULLY FIXED VERSION
# Fixes: Booking execution, idempotency race condition, typos
import logging
import re
import os
import time
from typing import Tuple, Optional
from openai import OpenAI
from db import get_subscriber_info_hybrid, get_db_connection, get_message_count, sync_messages_to_db
from memory import save_message, save_new_facts
from sales_director import generate_strategic_directive
from age import calculate_age_from_dob
from prompt import build_system_prompt
from ghl_message import send_sms_via_ghl
from reply_sanitizer import sanitize_reply
from llm_caller import generate_clean_reply
from ghl_calendar import consolidated_calendar_op
from ghl_api import fetch_targeted_ghl_history, get_valid_token, fetch_contact_data_from_ghl
from contact_validator import validate_and_resolve_contact 

logger = logging.getLogger('rq.worker')

import re

# === API CLIENT ===
XAI_API_KEY = os.getenv("XAI_API_KEY")

client = None
if XAI_API_KEY:
    client = OpenAI(
        api_key=XAI_API_KEY,
        base_url="https://api.x.ai/v1"
    )


def is_valid_contact_id(contact_id: str) -> bool:
    """
    Strict contact_id validation to prevent cross-contamination.

    Returns True only if contact_id meets ALL criteria:
    - Not None/empty
    - Not "unknown" or similar placeholder
    - At least 5 characters
    - Alphanumeric with allowed separators (-, _)
    - Not obviously invalid (e.g., "test", "null", "undefined")
    """
    if not contact_id or not isinstance(contact_id, str):
        return False

    contact_id = contact_id.strip()

    # Check minimum length
    if len(contact_id) < 5:
        return False

    # Check for placeholder values
    invalid_values = ["unknown", "none", "null", "undefined", "test", "placeholder", "temp"]
    if contact_id.lower() in invalid_values:
        return False

    # Allow demo_ prefixed IDs (demo mode)
    if contact_id.lower().startswith("demo_"):
        return True

    # Check for valid characters (alphanumeric, -, _)
    if not re.match(r'^[a-zA-Z0-9_-]+$', contact_id):
        return False

    return True


def count_consecutive_bot_messages(recent_exchanges: list) -> int:
    """
    Count how many consecutive bot messages were sent without a lead response.
    Returns the count of most recent consecutive bot messages.
    """
    if not recent_exchanges:
        return 0

    consecutive_bot = 0
    # Iterate backwards through exchanges (most recent first)
    for exchange in reversed(recent_exchanges):
        if exchange.get("role") == "bot":
            consecutive_bot += 1
        else:
            # Hit a lead message, stop counting
            break

    return consecutive_bot


def _extract_times_from_text(text: str) -> list:
    """
    Extract all time references from a text string.
    Returns list of dicts: [{"hour": 14, "minute": 0, "original": "2:00 PM", "day_hint": "tomorrow"}, ...]
    """
    results = []
    if not text:
        return results

    text_lower = text.lower()

    # Match times with am/pm like "9:00 am", "4:30pm", "2 pm", "10am"
    time_pattern = r'(\d{1,2}):?(\d{2})?\s*(pm|p\.m\.|am|a\.m\.)'
    for match in re.finditer(time_pattern, text_lower):
        h = int(match.group(1))
        m = int(match.group(2) or 0)
        period = match.group(3).lower().replace(".", "")  # "p.m." -> "pm"
        if "pm" in period and h != 12:
            h += 12
        elif "am" in period and h == 12:
            h = 0

        # Look for day context near this match (within 30 chars)
        context_start = max(0, match.start() - 30)
        context_end = min(len(text_lower), match.end() + 30)
        context = text_lower[context_start:context_end]
        day_hint = ""
        if "tomorrow" in context:
            day_hint = "tomorrow"
        for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
            if day in context:
                day_hint = day
                break

        results.append({
            "hour": h,
            "minute": m,
            "original": match.group().strip(),
            "day_hint": day_hint
        })

    return results


def _match_lead_time_to_bot_times(lead_msg: str, bot_msg: str) -> Optional[str]:
    """
    When the lead says a bare number like "4" or "the 2 one", find the matching
    time from the bot's offered times and return a full time string with AM/PM.

    Returns a formatted time string like "4:00 pm tomorrow" or None if no match.
    """
    if not lead_msg or not bot_msg:
        return None

    lead_lower = lead_msg.lower().strip()
    bot_times = _extract_times_from_text(bot_msg)

    if not bot_times:
        return None

    # Extract the bare number from the lead's message
    bare_num_match = re.search(r'\b(\d{1,2})\b', lead_lower)
    if not bare_num_match:
        return None

    lead_num = int(bare_num_match.group(1))

    # Try to match against bot's offered times
    for bt in bot_times:
        # Match hour (12h format): 4 matches 16:00 (4 PM) and 4:00 (4 AM)
        bot_hour_12 = bt["hour"] % 12 or 12
        if lead_num == bot_hour_12 or lead_num == bt["hour"]:
            period = "am" if bt["hour"] < 12 else "pm"
            minute_str = f":{bt['minute']:02d}" if bt["minute"] else ":00"
            day_part = f" {bt['day_hint']}" if bt["day_hint"] else ""
            result = f"{bot_hour_12}{minute_str} {period}{day_part}"
            logger.info(f"📅 TIME MATCH: Lead said '{lead_msg}' -> matched to bot's '{bt['original']}' -> booking '{result}'")
            return result

    # If lead said a number 1-7 with no match but bot offered PM times, assume PM
    if 1 <= lead_num <= 7:
        pm_times = [bt for bt in bot_times if bt["hour"] >= 12]
        if pm_times:
            # Default to PM since bot was offering afternoon slots
            day_hint = bot_times[0]["day_hint"] if bot_times else ""
            day_part = f" {day_hint}" if day_hint else ""
            result = f"{lead_num}:00 pm{day_part}"
            logger.info(f"📅 TIME INFER PM: Lead said '{lead_msg}' -> no exact match, inferring PM -> booking '{result}'")
            return result

    return None


def detect_booking_request(message: str, recent_exchanges: list, stage: str) -> Tuple[bool, Optional[str]]:
    """
    Context-aware booking detection.
    Returns (is_booking_request, extracted_time_string)

    Key insight: If bot just offered times and lead responds with ANY acceptance,
    that's a booking request even without explicit "book" keywords.
    """
    logger.info(f"🔍 BOOKING DETECTION START | message='{message}' | stage='{stage}' | exchanges_count={len(recent_exchanges)}")

    if not message:
        logger.warning("🚫 BOOKING DETECTION: No message provided")
        return False, None

    msg_lower = message.lower().strip()

    # === CONTEXT CHECK: Did bot just offer time slots? ===
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    last_bot_msg = bot_msgs[-1]['text'].lower() if bot_msgs else ""
    last_bot_msg_original = bot_msgs[-1]['text'] if bot_msgs else ""

    logger.debug(f"🔍 BOOKING CONTEXT | last_bot_msg_preview='{last_bot_msg[:100]}'...")

    # Detect if bot offered times in last message
    time_offer_indicators = [
        "i've got", "i have", "available", "how about", "works for you",
        "tomorrow", "pm", "am", "morning", "afternoon", "slot",
        "does", "work", "free at", "open at", "2:00", "3:00", "4:00",
        "9:00", "10:00", "11:00", "friday", "monday", "tuesday"
    ]
    bot_offered_times = any(indicator in last_bot_msg for indicator in time_offer_indicators)

    # Pre-extract structured times from bot's message for matching
    bot_time_structs = _extract_times_from_text(last_bot_msg_original) if bot_offered_times else []

    # === EXPLICIT BOOKING KEYWORDS (works anytime) ===
    explicit_booking_keywords = [
        "book", "schedule", "set up", "setup", "appointment",
        "let's do", "lets do", "i'll take", "ill take",
        "sign me up", "put me down", "lock it in", "lock me in"
    ]
    has_explicit_intent = any(kw in msg_lower for kw in explicit_booking_keywords)

    # === TIME PATTERNS ===
    time_patterns = [
        r'\d{1,2}:\d{2}\s*(am|pm|a\.m\.|p\.m\.)?',  # 9:00 am, 2:30pm
        r'\d{1,2}\s*(am|pm|a\.m\.|p\.m\.)',          # 9am, 2pm
        r'\b\d{1,2}\b(?=\s|$|,|\.|!)',               # Just "2" or "9" (when context is clear)
        r'tomorrow',
        r'today',
        r'monday|tuesday|wednesday|thursday|friday|saturday|sunday',
        r'morning|afternoon|evening',
    ]

    time_match = None
    for pattern in time_patterns:
        match = re.search(pattern, msg_lower)
        if match:
            time_match = match.group()
            break

    has_time_reference = time_match is not None

    # === ACCEPTANCE PHRASES (only valid if bot offered times) ===
    acceptance_phrases = [
        "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "k",
        "sounds good", "perfect", "great", "works", "that works",
        "works for me", "i can do", "i'm free", "im free", "good for me",
        "let's do it", "lets do it", "do it", "go for it", "down",
        "fine", "cool", "bet", "alright"
    ]
    is_acceptance = any(phrase in msg_lower for phrase in acceptance_phrases)

    # Log detection signals
    logger.info(f"🔍 BOOKING SIGNALS | bot_offered_times={bot_offered_times} | has_explicit_intent={has_explicit_intent} | has_time_reference={has_time_reference} | is_acceptance={is_acceptance}")

    # === NEGATIVE TIME REJECTION ===
    # "I can't do 2pm" or "2pm doesn't work" should NOT trigger booking at that time.
    # The lead is declining, not accepting. Let the LLM handle the response naturally.
    rejection_phrases = [
        "can't do", "cant do", "cannot do",
        "won't work", "wont work", "will not work",
        "doesn't work", "doesnt work", "does not work",
        "not available", "not free", "unavailable",
        "can't make", "cant make", "cannot make",
        "won't be able", "wont be able",
        "busy then", "busy at",
        "no good", "not going to work", "not gonna work",
    ]
    has_rejection = any(phrase in msg_lower for phrase in rejection_phrases)

    if has_rejection and not is_acceptance and not has_explicit_intent:
        logger.info(f"🚫 BOOKING REJECTION: Lead declined time/availability | msg='{message[:50]}'")
        return False, None

    # === HELPER: Resolve time string using bot context ===
    def _resolve_time_with_context(lead_message: str, use_bot_first: bool = False) -> str:
        """
        Resolve a booking time string by cross-referencing lead's message with bot's offered times.
        If use_bot_first=True, extract the first time from bot's message (for simple acceptance).
        """
        if use_bot_first and bot_time_structs:
            # Simple acceptance: pick the first time the bot offered
            bt = bot_time_structs[0]
            h12 = bt["hour"] % 12 or 12
            period = "am" if bt["hour"] < 12 else "pm"
            day_part = f" {bt['day_hint']}" if bt.get("day_hint") else ""
            resolved = f"{h12}:{bt['minute']:02d} {period}{day_part}"
            logger.info(f"📅 RESOLVED (first offered): '{resolved}' from bot times")
            return resolved

        # Try to match lead's bare number against bot's offered times
        if bot_time_structs:
            matched = _match_lead_time_to_bot_times(lead_message, last_bot_msg_original)
            if matched:
                return matched

        # Fallback: return the lead's message as-is for ghl_calendar to parse
        return lead_message

    # === DECISION LOGIC ===

    # Case 1: Explicit booking request with time (always book)
    if has_explicit_intent and has_time_reference:
        resolved = _resolve_time_with_context(message)
        logger.info(f"BOOKING CASE 1: Explicit + Time | msg='{message[:50]}' | resolved='{resolved}'")
        return True, resolved

    # Case 2: Bot offered times + lead mentions time reference
    if bot_offered_times and has_time_reference:
        resolved = _resolve_time_with_context(message)
        logger.info(f"BOOKING CASE 2: Bot offered + Time reference | msg='{message[:50]}' | resolved='{resolved}'")
        return True, resolved

    # Case 3: Bot offered times + simple acceptance (grab FIRST time from bot's msg)
    if bot_offered_times and is_acceptance and not has_time_reference:
        resolved = _resolve_time_with_context(message, use_bot_first=True)
        logger.info(f"BOOKING CASE 3: Bot offered + Simple acceptance | resolved='{resolved}'")
        return True, resolved

    # Case 4: Stage is BOOKING + any acceptance
    if stage == "booking" and is_acceptance:
        if has_time_reference:
            resolved = _resolve_time_with_context(message)
        else:
            resolved = _resolve_time_with_context(message, use_bot_first=True)
        logger.info(f"BOOKING CASE 4: Closing stage + Acceptance | resolved='{resolved}'")
        return True, resolved

    # Case 5: Explicit "that time works" / "works for me"
    time_acceptance_phrases = ["that time", "that works", "works for me", "good time", "that's fine"]
    if bot_offered_times and any(phrase in msg_lower for phrase in time_acceptance_phrases):
        resolved = _resolve_time_with_context(message, use_bot_first=True)
        logger.info(f"BOOKING CASE 5: Time acceptance phrase | resolved='{resolved}'")
        return True, resolved

    logger.info(f"🚫 BOOKING DETECTION: No cases matched | msg='{message}'")
    logger.debug(f"   Reasons: bot_offered={bot_offered_times}, explicit={has_explicit_intent}, time_ref={has_time_reference}, acceptance={is_acceptance}, stage={stage}")
    return False, None


def _collect_unanswered_lead_messages(contact_id: str, current_message: str) -> str:
    """
    Collects all consecutive unanswered lead messages (sent within the last 60s)
    and combines them into one message string.

    This handles the case where a lead sends 3 rapid messages:
      "hey" / "yeah im looking" / "for my wife and kids"
    Instead of responding to each separately, we combine them:
      "hey. yeah im looking. for my wife and kids"

    Returns the combined message (or original if only one message).
    """
    conn = get_db_connection()
    if not conn:
        return current_message

    try:
        cur = conn.cursor()
        # Get recent lead messages that have no bot reply after them.
        # We look at the last 60 seconds of lead messages, walking backward
        # until we hit a bot message (which means everything before it was already answered).
        cur.execute("""
            SELECT message_type, message_text, created_at
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at DESC
            LIMIT 10
        """, (contact_id,))
        rows = cur.fetchall()

        if not rows:
            return current_message

        # Collect consecutive lead messages from the end (most recent first)
        unanswered = []
        for row in rows:
            msg_type = row['message_type'] if isinstance(row, dict) else row[0]
            msg_text = row['message_text'] if isinstance(row, dict) else row[1]
            if msg_type == 'lead':
                unanswered.append(msg_text.strip())
            else:
                # Hit a bot message — everything before this was already answered
                break

        if len(unanswered) <= 1:
            return current_message

        # Reverse to chronological order and combine
        unanswered.reverse()
        combined = ". ".join(unanswered)
        logger.info(f"📦 BATCHED {len(unanswered)} lead messages into one | contact={contact_id} | combined='{combined[:100]}'")
        return combined

    except Exception as e:
        logger.error(f"Message batching failed for {contact_id}: {e}")
        return current_message
    finally:
        if 'cur' in locals():
            cur.close()
        if conn:
            conn.close()


def process_webhook_task(payload: dict):
    """
    Main webhook processor — handles demo + real GHL traffic.
    Fully resilient, demo-safe, with booking execution.

    IMPORTANT: Payload is already normalized by main.py's normalize_payload_universal()
    All fields are in clean snake_case format (contact_id, location_id, etc.)
    """
    start_time = time.time()

    # Payload is already normalized - just read the clean fields
    contact_id_raw = payload.get("contact_id")
    location_id = payload.get("location_id")

    # 🚨 LOG: Chef (worker) received the order ticket from kitchen (Redis)
    logger.info(f"🔍 TASK STARTED | contact_id={contact_id_raw} | first_name={payload.get('first_name')} | location_id={location_id}")

    # 🚨 USE PAYLOAD AS-IS: GHL sent this data, trust it (this is the order ticket)
    # Only validate if we detect a problem below (can't find contact, name mismatch, etc.)
    contact_id = contact_id_raw

    if not contact_id or not is_valid_contact_id(contact_id):
        logger.warning(f"🚨 TASK RECEIVED INVALID CONTACT_ID | Attempting validation | contact_id={contact_id}")
        contact_id = validate_and_resolve_contact(payload)

        if not contact_id or not is_valid_contact_id(contact_id):
            logger.error(f"🚨 TASK REJECTED - INVALID CONTACT | contact_id={contact_id_raw} | location_id={location_id}")
            return {"status": "error", "reason": "invalid_contact_id"}

        logger.info(f"✅ CONTACT VALIDATED | original={contact_id_raw} | resolved={contact_id}")

    logger.info(f"▶ START PROCESSING | location={location_id} | contact={contact_id}")

    try:
        if not location_id:
            logger.error("❌ ABORT: No location_id")
            return {"status": "error", "reason": "missing location_id"}

        is_demo = location_id in {'DEMO', 'DEMO_LOC', 'DEMO_ACCOUNT_SALES_ONLY', 'TEST_LOCATION_456'}
        
        if is_demo:
            subscriber = {
                'bot_first_name': 'Grok',
                'access_token': 'DEMO',
                'crm_user_id': '',
                'calendar_id': '',
                'timezone': 'America/Chicago',
                'initial_message': "Hey! Quick question — are you still with that life insurance plan you mentioned before?",
                'location_id': 'DEMO'
            }
            auth_token = 'DEMO'
        else:
            subscriber = get_subscriber_info_hybrid(location_id)
            if not subscriber:
                logger.error(f"❌ ABORT: No subscriber config for {location_id}")
                return {"status": "error", "reason": "no subscriber config"}

            auth_token = get_valid_token(location_id)
            if not auth_token:
                logger.error(f"❌ ABORT: Token refresh failed for {location_id}")
                return {"status": "error", "reason": "token refresh failed"}

        # Inject fresh token
        subscriber['access_token'] = auth_token

        # === USE PAYLOAD DATA AS-IS (Source of Truth from GHL) ===
        # DO NOT fetch from GHL API unless we detect a problem
        # The payload contains everything we need - GHL already sent it
        first_name = payload.get("first_name") or ""
        dob_str = payload.get("age") or ""
        address = payload.get("address") or ""
        intent = payload.get("intent") or ""
        lead_vendor = payload.get("lead_vendor", "")
        age = calculate_age_from_dob(date_of_birth=dob_str) if dob_str else None

        logger.info(f"✅ USING PAYLOAD DATA | contact_id={contact_id} | first_name={first_name}")

        initial_facts = []
        if first_name: initial_facts.append(f"First name: {first_name}")
        if age and age != "unknown": initial_facts.append(f"Age: {age}")
        # PRIVACY: Do NOT save address as a fact - only for backend context
        # if address: initial_facts.append(f"Address: {address}")
        if intent: initial_facts.append(f"Intent: {intent}")

        if initial_facts and contact_id != "unknown":
            save_new_facts(contact_id, initial_facts)

        # === History Sync (only if DB empty or gap) ===
        db_count = get_message_count(contact_id)
        if not is_demo:
            if db_count == 0:
                logger.info(f"🚨 DB empty for {contact_id} — fetching full GHL history")
                ghl_history = fetch_targeted_ghl_history(contact_id, location_id, auth_token, limit=50)
                sync_messages_to_db(contact_id, location_id, ghl_history)
            elif db_count <= 3:
                logger.info(f"🧐 Small DB count ({db_count}) for {contact_id} — syncing recent")
                ghl_history = fetch_targeted_ghl_history(contact_id, location_id, auth_token, limit=10)
                sync_messages_to_db(contact_id, location_id, ghl_history)

        # === Message Extraction ===
        # Normalized payload has "message" and "body" as top-level fields
        # Handle both direct string and nested dict formats
        raw_message = payload.get("message", {})
        if isinstance(raw_message, dict):
            message = raw_message.get("body", "").strip()
        else:
            # If message is already a string (normalized), use it
            message = str(raw_message).strip() if raw_message else ""

        # If still empty, try top-level "body" field (normalized)
        if not message:
            message = payload.get("body", "").strip()

        # message_id is normalized, but fallback to "id" for safety
        message_id = payload.get("message_id") or payload.get("id")

        # === FIXED: Atomic Idempotency Check ===
        if not is_demo and message_id:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    # Use INSERT ... ON CONFLICT DO NOTHING and check rowcount
                    cur.execute("""
                        INSERT INTO processed_webhooks (webhook_id) 
                        VALUES (%s) 
                        ON CONFLICT (webhook_id) DO NOTHING
                    """, (message_id,))
                    conn.commit()
                    
                    if cur.rowcount == 0:
                        # Row already existed - duplicate webhook
                        logger.warning(f"⚠ SKIP: Already processed webhook {message_id}")
                        return {"status": "skipped", "reason": "duplicate webhook"}
                except Exception as e:
                    logger.error(f"Idempotency check failed: {e}")
                finally:
                    cur.close()
                    conn.close()

        if message:
            save_message(contact_id, message, "lead")

        # === TCPA STOP WORD CHECK ===
        # If the lead says stop/unsubscribe/blocked, we MUST stop messaging.
        # Check BEFORE booking or response generation.
        TCPA_STOP_WORDS = ["stop", "unsubscribe", "blocked"]
        if message:
            msg_lower = message.lower()
            for stop_word in TCPA_STOP_WORDS:
                # Check for exact word match (not substring)
                if re.search(rf'\b{stop_word}\b', msg_lower):
                    logger.info(f"🛑 TCPA OPT-OUT: '{stop_word}' detected from contact {contact_id} | msg='{message}'")
                    # Do NOT book, do NOT respond - just acknowledge internally
                    return {"status": "opt_out", "reason": f"TCPA stop word: {stop_word}", "contact_id": contact_id}

        # === MESSAGE BATCHING ===
        # If a lead sends 3 messages in 30 seconds, we want ONE response to all 3.
        # After saving, wait briefly for more messages to arrive, then collect
        # all unanswered lead messages into a single combined message.
        if message:
            time.sleep(3)  # Brief pause to let rapid follow-up messages arrive and get queued/saved
            message = _collect_unanswered_lead_messages(contact_id, message)

        # === Core Conversation Logic ===
        bot_first_name = subscriber.get('bot_first_name', 'Grok')
        timezone = subscriber.get('timezone', 'America/Chicago')

        # Process ALL messages - trust the LLM to understand context
        # "k", "ya", "ok" are all valid text responses that need processing

        director_output = generate_strategic_directive(
            contact_id=contact_id,
            message=message,
            first_name=first_name,
            age=age,
            address=address
        )

        recent_exchanges = director_output["recent_exchanges"]

        # ============================================================
        # BOOKING DETECTION & EXECUTION
        # ============================================================
        booking_made = False
        is_booking_request, booking_time_str = detect_booking_request(
            message=message,
            recent_exchanges=recent_exchanges,
            stage=director_output["stage"]
        )
        
        # Determine CRM type for adapter routing
        crm_type = subscriber.get("crm_type", "ghl") or "ghl"
        use_crm_adapter = crm_type.lower() not in ("ghl", "gohighlevel")

        if is_booking_request and booking_time_str:
            logger.info(f"📅 BOOKING REQUEST DETECTED for contact {contact_id} | crm_type={crm_type}")

            if is_demo:
                logger.info(f"📅 DEMO MODE: Simulating booking for {contact_id}")
                booking_made = True
            elif use_crm_adapter:
                # Non-GHL CRM: Use adapter system
                try:
                    from crm_adapters.factory import get_adapter_for_subscriber
                    adapter = get_adapter_for_subscriber(subscriber)
                    booking_result = adapter.book_appointment(
                        contact_id=contact_id,
                        first_name=first_name,
                        selected_time=booking_time_str
                    )
                    if booking_result:
                        logger.info(f"✅ APPOINTMENT BOOKED via {adapter.CRM_NAME} for {contact_id}")
                        booking_made = True
                    else:
                        logger.warning(f"⚠️ BOOKING FAILED via {adapter.CRM_NAME} for {contact_id}")
                except Exception as adapter_err:
                    logger.error(f"CRM adapter booking error: {adapter_err}", exc_info=True)
            else:
                # GHL: Use existing direct code path (unchanged)
                booking_result = consolidated_calendar_op(
                    operation="book",
                    subscriber_data=subscriber,
                    contact_id=contact_id,
                    first_name=first_name,
                    selected_time=booking_time_str
                )

                if booking_result:
                    logger.info(f"✅ APPOINTMENT BOOKED for {contact_id}")
                    booking_made = True
                else:
                    logger.warning(f"⚠️ BOOKING FAILED for {contact_id} - Grok will handle response")

        # === Calendar fetch logic (for offering slots - only if NOT already booking) ===
        calendar_slots = ""
        if director_output["stage"] == "booking" and not booking_made:
            if is_demo:
                calendar_slots = "Tomorrow at 2:00 PM, Tomorrow at 4:30 PM, or Friday at 10:00 AM"
            elif use_crm_adapter:
                # Non-GHL: Use adapter for slot fetch
                try:
                    from crm_adapters.factory import get_adapter_for_subscriber
                    adapter = get_adapter_for_subscriber(subscriber)
                    calendar_slots = adapter.get_free_slots()
                except Exception as adapter_err:
                    logger.error(f"CRM adapter get_free_slots error: {adapter_err}")
                    calendar_slots = "let me check my calendar and get back to you with some times"
            else:
                # GHL: Use existing direct code path (unchanged)
                calendar_slots = consolidated_calendar_op("fetch_slots", subscriber)

        context_nudge = ""
        if message and "covered" in message.lower():
            context_nudge = "Lead claims coverage."

        # Add booking context
        if booking_made:
            context_nudge += """
⚠️ APPOINTMENT JUST BOOKED SUCCESSFULLY.

Confirm the specific time that was booked. Let them know a calendar invite is coming. Stop selling immediately. Do not ask for phone number, email, or any contact info. You already have it. You are texting them.

Do not continue the sales conversation. The appointment is booked. Confirm it in your own words and end warmly."""
            logger.info(f"✅ BOOKING CONFIRMATION ADDED TO PROMPT | contact={contact_id}")
        else:
            # CRITICAL: Prevent AI from hallucinating bookings
            context_nudge += "\n⚠️ CRITICAL: NO APPOINTMENT HAS BEEN BOOKED YET. Do NOT tell the lead they are booked. Do NOT confirm an appointment. Only offer times or ask which time works best."
            logger.info(f"🚫 NO BOOKING YET | contact={contact_id}")

        # Note: Follow-up strategy (including humor at 5+ unanswered) is now handled
        # by sales_director's tactical_narrative via _build_followup_guidance().
        # No duplicate re-engagement block needed here.

        # Combine all context: nudge + underwriting + company intel
        extra_context = director_output['underwriting_context']
        if director_output.get('company_context'):
            extra_context = f"{extra_context}\n[COMPANY INTEL] {director_output['company_context']}".strip()
        final_nudge = f"{context_nudge}\n{extra_context}".strip()

        # === INITIAL MESSAGE BYPASS ===
        # If the subscriber configured an initial_message and this is the very first
        # contact (no inbound message, no conversation history), send it verbatim.
        # All subsequent messages are LLM-generated.
        initial_msg = subscriber.get('initial_message', '').strip()
        reply = ""

        if initial_msg and not message and not recent_exchanges:
            reply = initial_msg
            logger.info(f"📨 USING CONFIGURED INITIAL MESSAGE | contact={contact_id} | msg='{reply[:60]}'")
        else:
            # === NORMAL LLM FLOW ===
            system_prompt = build_system_prompt(
                bot_first_name=bot_first_name,
                timezone=timezone,
                profile_str=director_output["profile_str"],
                tactical_narrative=director_output["tactical_narrative"],
                known_facts=director_output["known_facts"],
                story_narrative=director_output["story_narrative"],
                stage="closed" if booking_made else director_output["stage"],
                recent_exchanges=recent_exchanges,
                message=message,
                calendar_slots=calendar_slots,
                context_nudge=final_nudge,
                lead_vendor=lead_vendor
            )

            # === STRUCTURAL REASONING SEPARATION ===
            # generate_clean_reply handles:
            # 1. Making the API call
            # 2. Extracting reasoning_content vs content (like ChatGPT/Claude do)
            # 3. If reasoning leaks into content: retry with a focused "response-only" call
            # 4. Sanitization as final safety net
            reply = generate_clean_reply(
                client=client,
                system_prompt=system_prompt,
                user_message=message,
                bot_name=bot_first_name,
            )

        if not reply:
            logger.error(f"LLM produced no usable reply after retry. Using fallback. contact={contact_id}")
            reply = "Hey, just checking in. Anything new on your end with coverage?"

        # Layer 2: Strip markdown (SMS is plain text)
        reply = re.sub(r'\*\*([^*]+)\*\*', r'\1', reply)  # **bold** -> bold
        reply = re.sub(r'\*([^*]+)\*', r'\1', reply)       # *italic* -> italic
        reply = re.sub(r'__([^_]+)__', r'\1', reply)       # __underline__ -> underline
        reply = re.sub(r'_([^_]+)_', r'\1', reply)         # _italic_ -> italic
        reply = reply.replace("—", ",").replace("–", ",").replace("…", "...").strip()

        # Layer 3: Block placeholder/variable text
        FORBIDDEN_SUBSTRINGS = [
            "message_text", "{{", "}}", "contact_id", "location_id",
            "access_token", "[object Object]", "placeholder", "test message"
        ]
        FORBIDDEN_EXACT = ["none", "null", "undefined", "nan"]

        reply_lower = reply.lower().strip()
        is_forbidden = (
            any(p.lower() in reply_lower for p in FORBIDDEN_SUBSTRINGS) or
            reply_lower in FORBIDDEN_EXACT
        )
        if is_forbidden:
            logger.error(f"BLOCKED VARIABLE/PLACEHOLDER: '{reply}' — using fallback")
            reply = "Hey, just checking in. Anything new on your end with coverage?"

        # Trust the LLM - no length restrictions on replies
        # Sometimes "Got it" or "Ok!" is the perfect response

        # Log if AI might have used wrong name, but SEND IT ANYWAY (this is a sales bot, not a pushover)
        if first_name and reply:
            first_lower = first_name.lower().strip()
            reply_lower = reply.lower()

            # Just log for monitoring - don't block the message
            if first_lower not in reply_lower:
                logger.info(f"ℹ️ Name '{first_name}' not in reply (may be intentional or AI variation)")

        if reply:
            logger.info(f"📨 SENDING: '{reply[:50]}...'")

            if not is_demo:
                if use_crm_adapter:
                    # Non-GHL CRM: Use adapter for messaging
                    try:
                        from crm_adapters.factory import get_adapter_for_subscriber
                        adapter = get_adapter_for_subscriber(subscriber)
                        if adapter.SUPPORTS_MESSAGING:
                            sent = adapter.send_message(contact_id, reply)
                        else:
                            # CRM doesn't support messaging - use GHL as messaging fallback
                            # (some users use Zapier for booking but GHL for SMS)
                            sent = send_sms_via_ghl(contact_id, reply, auth_token, location_id)
                    except Exception as adapter_err:
                        logger.error(f"CRM adapter send_message error: {adapter_err}")
                        sent = False
                else:
                    # GHL: Use existing direct code path (unchanged)
                    sent = send_sms_via_ghl(contact_id, reply, auth_token, location_id)

                if sent:
                    save_message(contact_id, reply, "assistant")
                    logger.info(f"✅ Message sent via {crm_type.upper()}")
                else:
                    logger.warning("Message send failed — saved locally")
                    save_message(contact_id, reply, "assistant")
            else:
                save_message(contact_id, reply, "assistant")
                logger.info("⚠ DEMO MODE: Message saved internally")

        return {"status": "success", "reply_sent": bool(reply), "booking_made": booking_made}

    except Exception as e:
        logger.critical(f"💣 CRITICAL TASK FAILURE | contact={contact_id}: {str(e)}", exc_info=True)
        return {"status": "error", "reason": str(e)}
    finally:
        elapsed = time.time() - start_time
        logger.info(f"⏹ TASK END | contact={contact_id} | took {elapsed:.2f}s")