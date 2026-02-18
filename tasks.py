# tasks.py - The Background Engine (2026) - FULLY FIXED VERSION
# Fixes: Booking execution, idempotency race condition, typos
import logging
import re
import os
import time
from typing import Tuple, Optional
from openai import OpenAI
from db import get_subscriber_info_hybrid, get_db_connection, return_db_connection, get_message_count, sync_messages_to_db, log_webhook_event, get_bot_settings_by_location, BOT_SETTINGS_DEFAULTS
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

    # Pass 2: Match times WITHOUT am/pm like "7:30", "at about 7:30"
    # Negative lookahead avoids re-matching times already caught with AM/PM
    bare_time_pattern = r'(\d{1,2}):(\d{2})(?!\s*(?:pm|p\.m\.|am|a\.m\.))'
    for match in re.finditer(bare_time_pattern, text_lower):
        h = int(match.group(1))
        m = int(match.group(2))

        # Infer AM/PM from business hours context
        # Insurance sales calls typically happen during business hours
        if 1 <= h <= 7:
            h += 12  # Assume PM (1:00-7:59 -> 13:00-19:59)
        elif h == 12:
            h = 12  # Noon
        # 8-11 stays as-is (morning hours)

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
    When the lead says a time like "2pm", "4", or "the 2 one", find the matching
    time from the bot's offered times and return a full time string WITH the day.

    This is critical: if bot said "Tuesday at 2pm or Wednesday at 10am" and lead
    says "2pm", we need to return "2:00 pm tuesday" so the booking code books
    the right date, not today.

    Returns a formatted time string like "4:00 pm tuesday" or None if no match.
    """
    if not lead_msg or not bot_msg:
        return None

    lead_lower = lead_msg.lower().strip()
    bot_times = _extract_times_from_text(bot_msg)

    if not bot_times:
        return None

    # --- Step 1: Parse the lead's time (with or without AM/PM) ---
    lead_times = _extract_times_from_text(lead_msg)
    lead_hour = None
    lead_minute = 0

    if lead_times:
        # Lead gave explicit time like "2pm" or "4:30 pm"
        lead_hour = lead_times[0]["hour"]
        lead_minute = lead_times[0]["minute"]
    else:
        # Try bare number like "4" or "the 2 one"
        bare_num_match = re.search(r'\b(\d{1,2})\b', lead_lower)
        if bare_num_match:
            lead_num = int(bare_num_match.group(1))
            # Infer AM/PM from bot's offered context
            if 1 <= lead_num <= 7:
                lead_hour = lead_num + 12  # assume PM for business hours
            elif 8 <= lead_num <= 11:
                lead_hour = lead_num  # assume AM
            elif lead_num == 12:
                lead_hour = 12  # noon
            else:
                lead_hour = lead_num

    if lead_hour is None:
        return None

    # --- Step 2: Match against bot's offered times ---
    for bt in bot_times:
        # Match hour: compare both 24h and 12h representations
        bot_hour_12 = bt["hour"] % 12 or 12
        lead_hour_12 = lead_hour % 12 or 12
        hour_match = (lead_hour == bt["hour"]) or (lead_hour_12 == bot_hour_12)
        minute_match = (lead_minute == bt["minute"]) or (lead_minute == 0 and bt["minute"] == 0)

        if hour_match and minute_match:
            period = "am" if bt["hour"] < 12 else "pm"
            minute_str = f":{bt['minute']:02d}" if bt["minute"] else ":00"
            day_part = f" {bt['day_hint']}" if bt.get("day_hint") else ""
            result = f"{bot_hour_12}{minute_str} {period}{day_part}"
            logger.info(f"📅 TIME MATCH: Lead said '{lead_msg}' -> matched to bot's '{bt['original']}' (day={bt.get('day_hint','none')}) -> booking '{result}'")
            return result

    # --- Step 3: Fuzzy match (within 30 min) ---
    for bt in bot_times:
        diff = abs(bt["hour"] * 60 + bt["minute"] - (lead_hour * 60 + lead_minute))
        if diff <= 30:
            bot_hour_12 = bt["hour"] % 12 or 12
            period = "am" if bt["hour"] < 12 else "pm"
            minute_str = f":{bt['minute']:02d}" if bt["minute"] else ":00"
            day_part = f" {bt['day_hint']}" if bt.get("day_hint") else ""
            result = f"{bot_hour_12}{minute_str} {period}{day_part}"
            logger.info(f"📅 TIME FUZZY MATCH: Lead said '{lead_msg}' -> close to bot's '{bt['original']}' -> booking '{result}'")
            return result

    # --- Step 4: No match but lead gave valid time — carry forward first bot day_hint ---
    if lead_times:
        # Lead said "2pm" but no bot time matched — still use bot's day context
        # if the lead didn't mention their own day
        lead_has_day = lead_times[0].get("day_hint", "")
        if not lead_has_day and bot_times:
            # Use the day_hint from the closest bot time
            first_day = bot_times[0].get("day_hint", "")
            if first_day:
                h12 = lead_hour % 12 or 12
                period = "am" if lead_hour < 12 else "pm"
                result = f"{h12}:{lead_minute:02d} {period} {first_day}"
                logger.info(f"📅 TIME DAY INHERIT: Lead said '{lead_msg}' -> no exact match, inheriting day '{first_day}' -> booking '{result}'")
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

    # Detect if bot offered ACTUAL appointment times in last message
    # First: extract structured times (e.g., "2:00 pm", "9 am") - strongest signal
    bot_time_structs = _extract_times_from_text(last_bot_msg_original)

    # If structured times found, bot definitely offered times
    # If not, check for strong time-offering phrases (NOT loose words like "am", "does", "work")
    if bot_time_structs:
        bot_offered_times = True
    else:
        # Only match phrases that clearly indicate time slot offers
        strong_time_phrases = [
            "i've got", "how about", "works for you", "free at", "open at",
            "available at", "slot", "what time works", "when works",
            "schedule for", "book for", "set up for"
        ]
        # Also match day+time combos (e.g., "tomorrow morning", "friday afternoon")
        day_words = ["tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        time_words = ["morning", "afternoon", "evening"]
        has_day_time = any(d in last_bot_msg for d in day_words) and any(t in last_bot_msg for t in time_words)
        bot_offered_times = has_day_time or any(phrase in last_bot_msg for phrase in strong_time_phrases)

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
    # IMPORTANT: Use word-boundary regex to prevent substring false positives
    # e.g., "k" in "think" was causing false bookings on hostile messages
    acceptance_phrases = [
        r"\byes\b", r"\byeah\b", r"\byep\b", r"\byup\b", r"\bsure\b",
        r"\bok\b", r"\bokay\b", r"\bsounds good\b", r"\bperfect\b",
        r"\bgreat\b", r"\bthat works\b", r"\bworks for me\b",
        r"\bi can do\b", r"\bi'm free\b", r"\bim free\b", r"\bgood for me\b",
        r"\blet's do it\b", r"\blets do it\b", r"\bgo for it\b",
        r"\bfine\b", r"\bcool\b", r"\bbet\b", r"\balright\b"
    ]
    is_acceptance = any(re.search(phrase, msg_lower) for phrase in acceptance_phrases)

    # Additional guard: long messages with question marks are NOT simple acceptance
    # Real acceptance is short ("yes", "sounds good", "ok cool")
    is_hostile_or_question = len(msg_lower) > 60 or msg_lower.count("?") >= 1
    if is_acceptance and is_hostile_or_question:
        logger.info(f"🔍 ACCEPTANCE OVERRIDDEN: Message too long ({len(msg_lower)} chars) or has questions ({msg_lower.count('?')}q) | msg='{message[:80]}'")
        is_acceptance = False

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
    # GUARD: Only trigger if bot actually had PARSEABLE times (not just vague phrases)
    # AND the lead's message is genuinely a short acceptance (not a hostile question)
    if bot_offered_times and is_acceptance and not has_time_reference:
        if not bot_time_structs:
            logger.info(f"🚫 BOOKING CASE 3 SKIPPED: bot_offered_times=True but no parseable times in bot msg | msg='{message[:60]}'")
        else:
            resolved = _resolve_time_with_context(message, use_bot_first=True)
            logger.info(f"BOOKING CASE 3: Bot offered + Simple acceptance | resolved='{resolved}'")
            return True, resolved

    # Case 4: Stage is BOOKING + acceptance — ONLY if bot offered specific times recently
    # This prevents random messages from triggering bookings just because stage is "booking"
    # e.g., "Back to work I go" should never book just because conversation was in booking stage
    if stage == "booking" and is_acceptance and bot_offered_times:
        if has_time_reference:
            resolved = _resolve_time_with_context(message)
        else:
            resolved = _resolve_time_with_context(message, use_bot_first=True)
        logger.info(f"BOOKING CASE 4: Booking stage + Bot offered times + Acceptance | resolved='{resolved}'")
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
            return_db_connection(conn)


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
    log_webhook_event(location_id, "webhook_received", "info",
                      f"Webhook from {payload.get('first_name', 'unknown')}",
                      contact_id=contact_id_raw,
                      details={"message_preview": str(payload.get("message", ""))[:100]})

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
        is_api_source = payload.get("_source") == "universal_api"

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
        elif is_api_source:
            # API-sourced request — subscriber info comes from DB, no GHL token needed
            subscriber = get_subscriber_info_hybrid(location_id)
            if not subscriber:
                logger.error(f"❌ ABORT: No subscriber config for API source {location_id}")
                return {"status": "error", "reason": "no subscriber config"}
            auth_token = subscriber.get('access_token') or ''
            logger.info(f"🔌 API SOURCE | location={location_id} | contact={contact_id}")
        else:
            subscriber = get_subscriber_info_hybrid(location_id)
            if not subscriber:
                logger.error(f"❌ ABORT: No subscriber config for {location_id}")
                return {"status": "error", "reason": "no subscriber config"}

            auth_token = get_valid_token(location_id)
            if not auth_token:
                logger.error(f"❌ ABORT: Token refresh failed for {location_id}")
                return {"status": "error", "reason": "token refresh failed"}

        # Inject fresh token (empty for API sources without GHL)
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

        # === History Sync (only if DB empty or gap, skip for API sources) ===
        db_count = get_message_count(contact_id)
        if not is_demo and not is_api_source:
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
                    return_db_connection(conn)

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
        personal_website = subscriber.get('personal_website') or ""

        # === Contracted Carriers ===
        contracted_carriers = subscriber.get('contracted_carriers') or []
        if isinstance(contracted_carriers, str):
            import json as _json
            try:
                contracted_carriers = _json.loads(contracted_carriers)
            except Exception:
                contracted_carriers = []

        # === Bot Settings ===
        bot_settings = get_bot_settings_by_location(location_id)

        # Process ALL messages - trust the LLM to understand context
        # "k", "ya", "ok" are all valid text responses that need processing

        director_output = generate_strategic_directive(
            contact_id=contact_id,
            message=message,
            first_name=first_name,
            age=age,
            address=address,
            bot_settings=bot_settings,
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
            log_webhook_event(location_id, "booking_attempt", "info",
                              f"Booking requested: {booking_time_str}",
                              contact_id=contact_id, details={"time": booking_time_str, "crm_type": crm_type})

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
                        log_webhook_event(location_id, "booking_success", "success",
                                          f"Booked via {adapter.CRM_NAME}: {booking_time_str}",
                                          contact_id=contact_id, details={"crm": adapter.CRM_NAME, "time": booking_time_str})
                    else:
                        logger.warning(f"⚠️ BOOKING FAILED via {adapter.CRM_NAME} for {contact_id}")
                        log_webhook_event(location_id, "booking_failed", "error",
                                          f"Booking failed via {adapter.CRM_NAME}",
                                          contact_id=contact_id, details={"crm": adapter.CRM_NAME})
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
                    log_webhook_event(location_id, "booking_success", "success",
                                      f"Booked via LeadConnector: {booking_time_str}",
                                      contact_id=contact_id, details={"crm": "LeadConnector", "time": booking_time_str})
                else:
                    logger.warning(f"⚠️ BOOKING FAILED for {contact_id} - Grok will handle response")
                    log_webhook_event(location_id, "booking_failed", "error",
                                      "Booking failed via LeadConnector",
                                      contact_id=contact_id)

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

        # === LEAD RE-ENGAGEMENT CHECK ===
        # If re-engagement is disabled and this is a follow-up (no inbound message),
        # skip responding entirely — let the lead come to us.
        if not message and not bot_settings.get("lead_reengagement", True):
            bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
            if len(bot_msgs) >= 1:
                logger.info(f"🚫 RE-ENGAGEMENT DISABLED | Skipping follow-up for {contact_id}")
                return {"status": "skipped", "reason": "lead_reengagement disabled", "contact_id": contact_id}

        # === INITIAL MESSAGE / OUTBOUND DRIP BYPASS ===
        initial_msg = subscriber.get('initial_message', '').strip()
        outbound_msgs = bot_settings.get("outbound_messages", [])
        reply = ""

        if not message and not recent_exchanges and initial_msg:
            # First ever contact — use configured initial message
            reply = initial_msg
            logger.info(f"📨 USING CONFIGURED INITIAL MESSAGE | contact={contact_id} | msg='{reply[:60]}'")

        elif not message and outbound_msgs:
            # Custom outbound drip: check how many bot messages have been sent,
            # send the next custom template if available
            bot_msgs_sent = len([m for m in recent_exchanges if m['role'] == 'assistant'])
            # The initial_message counts as message 0, custom drip starts at index 0
            # after the initial message. If no initial_message, drip starts immediately.
            drip_index = bot_msgs_sent - (1 if initial_msg else 0)
            if 0 <= drip_index < len(outbound_msgs):
                reply = outbound_msgs[drip_index]
                logger.info(f"📨 OUTBOUND DRIP #{drip_index + 1}/{len(outbound_msgs)} | contact={contact_id} | msg='{reply[:60]}'")

        if not reply:
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
                lead_vendor=lead_vendor,
                personal_website=personal_website,
                contracted_carriers=contracted_carriers,
                bot_settings=bot_settings,
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

            if is_api_source:
                # API-sourced: deliver reply via outbound webhook
                from webhook_delivery import deliver_webhook, build_api_reply_payload
                webhook_url = payload.get("_outbound_webhook_url", "")
                webhook_secret = payload.get("_webhook_secret", "")
                api_metadata = payload.get("_api_metadata", {})

                out_payload = build_api_reply_payload(
                    contact_id=contact_id,
                    reply=reply,
                    booking_made=booking_made,
                    metadata=api_metadata,
                )
                success, status_code, error = deliver_webhook(
                    url=webhook_url, payload=out_payload, secret=webhook_secret
                )
                save_message(contact_id, reply, "assistant")
                if success:
                    logger.info(f"✅ API reply delivered via webhook -> {status_code}")
                    log_webhook_event(location_id, "api_webhook_sent", "success",
                                      f"Reply delivered via webhook ({len(reply)} chars)",
                                      contact_id=contact_id, details={"preview": reply[:80], "status_code": status_code})
                else:
                    logger.warning(f"⚠️ API webhook delivery failed: {error}")
                    log_webhook_event(location_id, "api_webhook_failed", "error",
                                      f"Webhook delivery failed: {error}",
                                      contact_id=contact_id, details={"error": error, "status_code": status_code})

            elif not is_demo:
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
                    log_webhook_event(location_id, "message_sent", "success",
                                      f"Reply sent ({len(reply)} chars)",
                                      contact_id=contact_id, details={"preview": reply[:80]})
                else:
                    logger.warning("Message send failed — saved locally")
                    save_message(contact_id, reply, "assistant")
                    log_webhook_event(location_id, "message_failed", "error",
                                      "Message send failed",
                                      contact_id=contact_id)
            else:
                save_message(contact_id, reply, "assistant")
                logger.info("⚠ DEMO MODE: Message saved internally")

        return {"status": "success", "reply_sent": bool(reply), "booking_made": booking_made}

    except Exception as e:
        logger.critical(f"💣 CRITICAL TASK FAILURE | contact={contact_id}: {str(e)}", exc_info=True)
        log_webhook_event(location_id, "error", "error",
                          f"Task failure: {str(e)[:200]}",
                          contact_id=contact_id, details={"error": str(e)[:500]})
        return {"status": "error", "reason": str(e)}
    finally:
        elapsed = time.time() - start_time
        logger.info(f"⏹ TASK END | contact={contact_id} | took {elapsed:.2f}s")