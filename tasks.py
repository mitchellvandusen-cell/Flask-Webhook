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
from ghl_calendar import consolidated_calendar_op
from ghl_api import fetch_targeted_ghl_history, get_valid_token 

logger = logging.getLogger('rq.worker')

# === API CLIENT ===
XAI_API_KEY = os.getenv("XAI_API_KEY")

client = None
if XAI_API_KEY:
    client = OpenAI(
        api_key=XAI_API_KEY,
        base_url="https://api.x.ai/v1"
    )


def detect_booking_request(message: str, recent_exchanges: list, stage: str) -> Tuple[bool, Optional[str]]:
    """
    Context-aware booking detection.
    Returns (is_booking_request, extracted_time_string)
    
    Key insight: If bot just offered times and lead responds with ANY acceptance,
    that's a booking request even without explicit "book" keywords.
    """
    if not message:
        return False, None
        
    msg_lower = message.lower().strip()
    
    # === CONTEXT CHECK: Did bot just offer time slots? ===
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    last_bot_msg = bot_msgs[-1]['text'].lower() if bot_msgs else ""
    
    # Detect if bot offered times in last message
    time_offer_indicators = [
        "i've got", "i have", "available", "how about", "works for you",
        "tomorrow", "pm", "am", "morning", "afternoon", "slot",
        "does", "work", "free at", "open at", "2:00", "3:00", "4:00",
        "9:00", "10:00", "11:00", "friday", "monday", "tuesday"
    ]
    bot_offered_times = any(indicator in last_bot_msg for indicator in time_offer_indicators)
    
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
    
    # === DECISION LOGIC ===
    
    # Case 1: Explicit booking request with time (always book)
    if has_explicit_intent and has_time_reference:
        logger.info(f"BOOKING CASE 1: Explicit + Time | msg='{message[:50]}'")
        return True, message
    
    # Case 2: Bot offered times + lead mentions time reference
    if bot_offered_times and has_time_reference:
        logger.info(f"BOOKING CASE 2: Bot offered + Time reference | msg='{message[:50]}'")
        return True, message
    
    # Case 3: Bot offered times + simple acceptance (grab time from bot's msg)
    if bot_offered_times and is_acceptance and not has_time_reference:
        logger.info(f"BOOKING CASE 3: Bot offered + Simple acceptance | msg='{message[:50]}'")
        return True, last_bot_msg  # Use bot's message for time extraction
    
    # Case 4: Stage is CLOSING + any acceptance
    if stage == "closing" and is_acceptance:
        logger.info(f"BOOKING CASE 4: Closing stage + Acceptance | msg='{message[:50]}'")
        return True, message if has_time_reference else last_bot_msg
    
    # Case 5: Explicit "that time works" / "works for me" 
    time_acceptance_phrases = ["that time", "that works", "works for me", "good time", "that's fine"]
    if bot_offered_times and any(phrase in msg_lower for phrase in time_acceptance_phrases):
        logger.info(f"BOOKING CASE 5: Time acceptance phrase | msg='{message[:50]}'")
        return True, last_bot_msg
    
    return False, None


def process_webhook_task(payload: dict):
    """
    Main webhook processor — handles demo + real GHL traffic.
    Fully resilient, demo-safe, with booking execution.
    """
    start_time = time.time()
    contact_id = payload.get("contact_id") or "unknown"
    location_id = (
        payload.get("location", {}).get("id") or
        payload.get("location_id") or
        payload.get("locationId")
    )
    logger.info(f"▶ START TASK | loc={location_id} | contact={contact_id}")

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

        # === Metadata & Pre-load Facts ===
        first_name = payload.get("first_name") or ""
        dob_str = payload.get("age") or ""
        address = payload.get("address") or ""
        intent = payload.get("intent") or ""
        lead_vendor = payload.get("lead_vendor", "")
        age = calculate_age_from_dob(date_of_birth=dob_str) if dob_str else None

        initial_facts = []
        if first_name: initial_facts.append(f"First name: {first_name}")
        if age and age != "unknown": initial_facts.append(f"Age: {age}")
        if address: initial_facts.append(f"Address: {address}")
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
        raw_message = payload.get("message", {})
        message = raw_message.get("body", "").strip() if isinstance(raw_message, dict) else str(raw_message).strip()
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

        # Skip trivial messages (save Grok cost)
        if not message or message.strip().lower() in {".", ",", "k"}:
            logger.debug(f"Skipping Grok call — trivial message: {message[:50] if message else 'empty'}")
            return {"status": "skipped", "reason": "trivial message"}

        # === Core Conversation Logic ===
        bot_first_name = subscriber.get('bot_first_name', 'Grok')
        timezone = subscriber.get('timezone', 'America/Chicago')

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
        
        if is_booking_request and booking_time_str:
            logger.info(f"📅 BOOKING REQUEST DETECTED for contact {contact_id}")
            
            if is_demo:
                logger.info(f"📅 DEMO MODE: Simulating booking for {contact_id}")
                booking_made = True
            else:
                # Real booking via GHL API
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
        if director_output["stage"] == "closing" and not booking_made:
            if is_demo:
                # FIXED: Typo "Tomrorow" -> "Tomorrow"
                calendar_slots = "Tomorrow at 2:00 PM, Tomorrow at 4:30 PM, or Friday at 10:00 AM"
            else:
                calendar_slots = consolidated_calendar_op("fetch_slots", subscriber)

        context_nudge = ""
        if message and "covered" in message.lower():
            context_nudge = "Lead claims coverage."
        
        # Add booking context
        if booking_made:
            context_nudge += "\n⚠️ APPOINTMENT JUST BOOKED SUCCESSFULLY. Confirm the time warmly, thank them, and STOP selling."
        
        final_nudge = f"{context_nudge}\n{director_output['underwriting_context']}".strip()

        # Ghost / initial outreach check
        initial_message = subscriber.get('initial_message', '').strip()
        assistant_messages = [m for m in recent_exchanges if m["role"] == "assistant"]

        reply = ""
        if not message and len(assistant_messages) == 0 and initial_message and not is_demo:
            reply = initial_message
            logger.info("👻 GHOST MODE: Sending initial outreach")
        else:
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

            grok_messages = [{"role": "system", "content": system_prompt}]
            for msg in recent_exchanges:
                role = "user" if msg["role"] == "lead" else "assistant"
                grok_messages.append({"role": role, "content": msg["text"]})
            if message:
                grok_messages.append({"role": "user", "content": message})

            try:
                response = client.chat.completions.create(
                    model="grok-4-1-fast-reasoning",
                    messages=grok_messages,
                    temperature=0.85,
                    max_tokens=200,
                )
                reply = response.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"❌ GROK FAILURE: {e}", exc_info=True)
                reply = "Got it — let's circle back when you're free. Anything specific on your mind about coverage?"

        # Cleanup reply
        reply = re.sub(r'<thinking>[\s\S]*?</thinking>', '', reply)
        reply = re.sub(r'</?reply>', '', reply)
        reply = re.sub(r'<[^>]+>', '', reply).strip()
        reply = reply.replace("—", ",").replace("–", ",").replace("…", "...").strip()

        if reply:
            logger.info(f"📨 SENDING: '{reply[:50]}...'")

            if not is_demo:
                sent = send_sms_via_ghl(contact_id, reply, auth_token, location_id)
                if sent:
                    save_message(contact_id, reply, "assistant")
                    logger.info("✅ Message sent to GHL")
                else:
                    logger.warning("SMS send failed — saved locally")
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