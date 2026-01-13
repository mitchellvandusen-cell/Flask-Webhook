# tasks.py - The Background Engine (2026) - Flawless & Demo-Optimized
import logging
import re
import os
import time
import httpx
from openai import OpenAI
from db import get_subscriber_info, get_db_connection, get_message_count, sync_messages_to_db
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

def process_webhook_task(payload: dict):
    """
    Main webhook processor — handles demo + real GHL traffic.
    Fully resilient, demo-safe, and optimized for /demo-chat.
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

        is_demo = location_id in {'DEMO', 'DEMO_LOC' 'DEMO_ACCOUNT_SALES_ONLY', 'TEST_LOCATION_456'}
        
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
            subscriber = get_subscriber_info(location_id)
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
            elif db_count <= 3:  # Small gap threshold
                logger.info(f"🧐 Small DB count ({db_count}) for {contact_id} — syncing recent")
                ghl_history = fetch_targeted_ghl_history(contact_id, location_id, auth_token, limit=10)
                sync_messages_to_db(contact_id, location_id, ghl_history)

        # === Message Extraction ===
        raw_message = payload.get("message", {})
        message = raw_message.get("body", "").strip() if isinstance(raw_message, dict) else str(raw_message).strip()
        message_id = payload.get("message_id") or payload.get("id")

        # Idempotency (real GHL only)
        if not is_demo and message_id:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1 FROM processed_webhooks WHERE webhook_id = %s", (message_id,))
                    if cur.fetchone():
                        logger.warning(f"⚠ SKIP: Already processed webhook {message_id}")
                        return {"status": "skipped", "reason": "duplicate webhook"}
                    cur.execute("INSERT INTO processed_webhooks (webhook_id) VALUES (%s)", (message_id,))
                    conn.commit()
                except Exception as e:
                    logger.error(f"Idempotency check failed: {e}")
                finally:
                    cur.close()
                    conn.close()

        if message:
            save_message(contact_id, message, "lead")

        # Skip trivial messages (save Grok cost)
        if not message or len(message.strip()) < 5 or message.strip().lower() in {"ok", "yes", ".", "k", "cool", "thanks"}:
            logger.debug(f"Skipping Grok call — trivial message: {message[:50]}")
            return {"status": "skipped", "reason": "trivial message"}

        # === Core Conversation Logic ===
        bot_first_name = subscriber['bot_first_name']
        timezone = subscriber.get('timezone', 'America/Chicago')

        director_output = generate_strategic_directive(
            contact_id=contact_id,
            message=message,
            first_name=first_name,
            age=age,
            address=address
        )

        recent_exchanges = director_output["recent_exchanges"]

        # Calendar fetch only when needed
        calendar_slots = ""
        if not is_demo and director_output["stage"] == "closing":
            calendar_slots = consolidated_calendar_op("fetch_slots", subscriber)

        context_nudge = ""
        if message and "covered" in message.lower():
            context_nudge = "Lead claims coverage."
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
                stage=director_output["stage"],
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
                    temperature=0.85,  # Higher for natural variability
                    max_tokens=150,    # SMS-length limit
                    frequency_penalty=0.65,
                    presence_penalty=0.4,
                    timeout=15.0       # Prevent hanging
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
            logger.info(f"📨 SENDING: '{reply[:30]}...'")

            if not is_demo:
                sent = send_sms_via_ghl(contact_id, reply, auth_token, location_id)
                if sent:
                    save_message(contact_id, reply, "assistant")
                    logger.info("✅ Message sent to GHL")
                else:
                    logger.warning("SMS send failed — saved locally")
            else:
                # Demo mode: always save (no real send)
                save_message(contact_id, reply, "assistant")
                logger.info("⚠ DEMO MODE: Message saved internally")

        return {"status": "success", "reply_sent": bool(reply)}

    except Exception as e:
        logger.critical(f"💣 CRITICAL TASK FAILURE | contact={contact_id}: {str(e)}", exc_info=True)
        return {"status": "error", "reason": str(e)}
    finally:
        elapsed = time.time() - start_time
        logger.info(f"⏹ TASK END | contact={contact_id} | took {elapsed:.2f}s")