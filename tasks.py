# tasks.py - The Background Engine (2026)
import logging
import re
import os
import time
from openai import OpenAI
from db import get_subscriber_info, get_db_connection, get_message_count, sync_messages_to_db
from memory import save_message, save_new_facts
from sales_director import generate_strategic_directive
from age import calculate_age_from_dob
from prompt import build_system_prompt
from outcome_learning import classify_vibe
from ghl_message import send_sms_via_ghl
from ghl_calendar import consolidated_calendar_op
from ghl_api import fetch_targeted_ghl_history, get_valid_token 

# === LOGGING SETUP ===
logger = logging.getLogger('rq.worker')

# === API CLIENTS ===
XAI_API_KEY = os.getenv("XAI_API_KEY")
client = OpenAI(base_url="https://api.x.ai/v1", api_key=XAI_API_KEY) if XAI_API_KEY else None

def process_webhook_task(payload: dict):
    start_time = time.time()

    # === STEP 1: INITIAL PAYLOAD & IDENTITY ===
    location_id = (
        payload.get("location", {}).get("id") or 
        payload.get("location_id") or 
        payload.get("locationId")
    )
    contact_id = payload.get("contact_id") or payload.get("contactid") or payload.get("contact", {}).get("id") or "unknown"
    logger.info(f"▶ START TASK: Location={location_id} | Contact={contact_id}")

    try:
        if not location_id:
            logger.error("❌ ABORT: Location ID missing in payload.")
            return

        is_demo = (location_id == 'DEMO_ACCOUNT_SALES_ONLY' or location_id == 'TEST_LOCATION_456')
        
        if is_demo:
            subscriber = {
                'bot_first_name': 'Grok',
                'access_token': 'DEMO', 
                'crm_user_id': '',
                'calendar_id': '',
                'timezone': 'America/Chicago',
                'initial_message': "Hey! Quick question, are you still with that life insurance plan you mentioned before?",
                'location_id': 'DEMO'
            }
        else:
            subscriber = get_subscriber_info(location_id)
            if not subscriber:
                logger.error(f"❌ ABORT: Identity not configured for location {location_id}")
                return

        # === TOKEN RETRIEVAL ===
        # We get a fresh/valid token here to use for subsequent calls
        auth_token = get_valid_token(location_id)
        if not is_demo and not auth_token:
            logger.error(f"❌ ABORT: Could not retrieve valid access token for {location_id}")
            return
        
        # Inject valid token back into subscriber dict for downstream functions
        subscriber['access_token'] = auth_token

        # === STEP 2: METADATA & PRE-LOAD FACTS ===
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

        # Sync History Logic
        db_count = get_message_count(contact_id)
        if not is_demo:
            if db_count == 0:
                logger.info(f"🚨 DB WIPED/EMPTY for {contact_id}. Fetching full context...")
                ghl_history = fetch_targeted_ghl_history(contact_id, location_id, auth_token, limit=50)
                sync_messages_to_db(contact_id, location_id, ghl_history)
            elif db_count == 1:
                logger.info(f"🧐 Possible memory gap. Syncing last 10...")
                ghl_history = fetch_targeted_ghl_history(contact_id, location_id, auth_token, limit=10)
                sync_messages_to_db(contact_id, location_id, ghl_history)

        # === STEP 3: MESSAGE EXTRACTION ===
        raw_message = payload.get("message", {})
        message = raw_message.get("body", "").strip() if isinstance(raw_message, dict) else str(raw_message).strip()
        message_id = payload.get("message_id") or payload.get("id")

        if not is_demo and message_id:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1 FROM processed_webhooks WHERE webhook_id = %s", (message_id,))
                    if cur.fetchone():
                        logger.warning(f"⚠ SKIP: Message {message_id} already processed.")
                        return 
                    cur.execute("INSERT INTO processed_webhooks (webhook_id) VALUES (%s)", (message_id,))
                    conn.commit()
                except Exception as e:
                    logger.error(f"⚠ DB ERROR (Idempotency): {e}")
                finally:
                    conn.close()

        if message:
            save_message(contact_id, message, "lead")

        # === STEP 4: CONVERSATION LOGIC ===
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
        
        # --- DEMO FIX: Handle Empty Context ---
        # If demo and no history, force the context to assume initiation
        if is_demo and not recent_exchanges and not message:
            logger.info("🎮 DEMO: No history, treating as fresh start")
            # We don't need to do anything special, the ghost check below handles it
        
        # Calendar Fetch
        calendar_slots = ""
        if not is_demo and director_output["stage"] == "closing":
            calendar_slots = consolidated_calendar_op("fetch_slots", subscriber)

        context_nudge = ""
        if message and "covered" in message.lower(): 
            context_nudge = "Lead claims coverage."
        final_nudge = f"{context_nudge}\n{director_output['underwriting_context']}".strip()

        # Ghost Check
        initial_message = subscriber.get('initial_message', '').strip()
        assistant_messages = [m for m in recent_exchanges if m["role"] == "assistant"]
        
        reply = ""
        if not message and len(assistant_messages) == 0 and initial_message:
            reply = initial_message
            logger.info("👻 GHOST MODE: Sending initial outreach message")
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
                    temperature=0.7, # Lowered for more natural flow
                    max_tokens=500
                )
                reply = response.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"❌ GROK FAILURE: {e}")
                reply = "Fair enough. Just to clarify, was it the timing that was off or something else?"

        # Cleanup
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
                    logger.info("✅ SUCCESS: Message sent to GHL")
            else:
                # DEMO MODE: Save immediately
                save_message(contact_id, reply, "assistant")
                logger.info("⚠ DEMO MODE: Message saved internally")

    except Exception as e:
        logger.error(f"💣 CRITICAL TASK FAILURE: {str(e)}", exc_info=True)
    finally:
        elapsed = time.time() - start_time
        logger.info(f"⏹ TASK END: Took {elapsed:.2f}s")