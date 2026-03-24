# tasks.py - The Background Engine (2026)
import logging
import re
import os
import time
import json
from datetime import timedelta
from openai import OpenAI
from db import get_subscriber_info_hybrid, get_db_connection, return_db_connection, get_message_count, sync_messages_to_db, log_webhook_event, get_bot_settings_by_location, save_persistent_alert, get_auth_failed_messages, mark_webhook_log_retried, mark_webhook_log_backfill_retried, save_failed_webhook_payload, get_unretried_failed_webhooks, mark_failed_webhook_retried, get_token_failed_webhook_logs
from memory import save_message, save_new_facts
from sales_director import generate_strategic_directive
from age import calculate_age_from_dob
from prompt import build_system_prompt
from ghl_message import send_sms_via_ghl
from llm_caller import generate_clean_reply
from ghl_calendar import consolidated_calendar_op
from ghl_api import fetch_targeted_ghl_history, get_valid_token, get_valid_token_with_status, has_oauth_credentials
from contact_validator import validate_and_resolve_contact
from booking_detection import detect_booking_request, BookingDetectionResult
from message_utils import collect_unanswered_lead_messages as _collect_unanswered_lead_messages

logger = logging.getLogger('rq.worker')

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
    conversation_id = payload.get("conversation_id")

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
        is_workflow_outreach = payload.get("_workflow_outreach", False)

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

            # Pass subscriber to avoid redundant DB query inside get_valid_token
            auth_token, was_refreshed, token_error = get_valid_token_with_status(
                location_id, subscriber=subscriber)

            if not auth_token:
                oauth_type = subscriber.get('oauth_app_type', 'unknown')
                has_access = bool(subscriber.get('access_token'))
                has_refresh = bool(subscriber.get('refresh_token'))

                # NEVER abort when token is unavailable — continue through the
                # pipeline and deliver SMS via Twilio fallback instead of GHL.
                # Previously only 'no_credentials' continued; all other errors
                # (no_tokens, auth_error, all_creds_exhausted, etc.) aborted
                # the entire webhook, silently dropping messages even when the
                # subscriber had Twilio sub-account credentials that could deliver.
                if token_error == 'no_credentials':
                    logger.warning(f"⚠️ No GHL token and no OAuth credentials for {location_id} — "
                                  f"continuing with Twilio fallback path")
                else:
                    logger.error(f"⚠️ Token refresh failed for {location_id} | "
                                f"oauth_app_type={oauth_type} | has_access_token={has_access} | "
                                f"has_refresh_token={has_refresh} | error={token_error} | "
                                f"continuing with Twilio fallback")

                    # Create persistent dashboard alert so subscriber sees the issue
                    sub_email = subscriber.get('email')
                    if sub_email:
                        save_persistent_alert(
                            email=sub_email,
                            alert_type="oauth_token_failure",
                            title="CRM Connection Lost",
                            message=(
                                "Your GoHighLevel connection needs to be re-authorized. "
                                "Incoming messages are not being processed. "
                                "Please click 'Connect CRM' to reconnect."
                            ),
                            severity="error",
                            location_id=location_id
                        )

                    log_webhook_event(location_id, "error", "error",
                                      f"Token refresh failed ({token_error}) — trying Twilio fallback",
                                      contact_id=contact_id,
                                      details={"oauth_app_type": oauth_type, "error": token_error})

                    # Re-queue with backoff for transient failures (network, server errors)
                    # Don't retry auth errors — those need user action (re-auth)
                    if token_error in ('network_error', 'server_error'):
                        retry_count = payload.get("_retry_count", 0)
                        if retry_count < 3:
                            payload["_retry_count"] = retry_count + 1
                            delay_seconds = 30 * (2 ** retry_count)  # 30s, 60s, 120s
                            try:
                                import redis
                                from rq import Queue
                                redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
                                r = redis.from_url(redis_url, socket_timeout=5,
                                                   socket_connect_timeout=5)
                                q = Queue('production', connection=r)
                                q.enqueue_in(
                                    timedelta(seconds=delay_seconds),
                                    process_webhook_task,
                                    payload,
                                    job_timeout=120,
                                    result_ttl=86400,
                                )
                                logger.info(f"🔄 Re-queued task for {contact_id} with "
                                           f"{delay_seconds}s delay (retry {retry_count + 1}/3)")
                            except Exception as retry_err:
                                logger.error(f"Failed to re-queue task: {retry_err}")

                    # Persist payload so the scourer can replay it once token is fixed
                    save_failed_webhook_payload(location_id, contact_id, payload, token_error)
                    logger.info(f"💾 Saved failed webhook payload for {contact_id} "
                               f"(reason={token_error}) — scourer will retry later")

                # Continue with empty token — downstream _ghl_creds_missing
                # flag (line ~262) will skip GHL API calls and route SMS
                # through Twilio fallback instead
                auth_token = ''

            # If GHL OAuth creds are missing, log once and continue — Twilio
            # fallback will handle SMS delivery downstream
            if token_error == 'no_credentials':
                logger.warning(f"⚠️ GHL OAuth credentials missing for {location_id} — "
                              f"will use Twilio fallback for SMS delivery")

            # If we got an expired token as last resort, log a warning but continue
            if token_error == 'expired':
                logger.warning(f"⚠️ Using possibly-expired token for {location_id} — "
                              f"SMS may fail, but attempting anyway")
                # Alert subscriber that their connection needs attention
                sub_email = subscriber.get('email')
                if sub_email:
                    save_persistent_alert(
                        email=sub_email,
                        alert_type="oauth_token_expiring",
                        title="CRM Connection Needs Attention",
                        message=(
                            "Your GoHighLevel token refresh is failing. "
                            "Messages are still sending but may stop soon. "
                            "Please reconnect your CRM to prevent interruption."
                        ),
                        severity="warning",
                        location_id=location_id
                    )

        # Track if GHL OAuth credentials are ACTUALLY unusable — skip GHL
        # operations when the token itself is missing or unresolvable.
        #
        # Previously this also checked `not has_oauth_credentials()` which
        # caused ALL GHL operations to be skipped on workers without OAuth
        # env vars — even when the DB-cached token was still valid (refreshed
        # by the web service's proactive cron).  That broke history sync
        # (less AI context), CRM logging (agent can't see bot replies in GHL),
        # and calendar booking.
        #
        # Now we only skip when token_error confirms the token is unusable.
        # When the token IS valid, GHL operations proceed normally.  If the
        # token turns out to be expired at call time, each GHL function
        # handles the 401 gracefully (ghl_message returns after 1 attempt
        # when has_oauth_credentials() is False, ghl_logger is best-effort,
        # fetch_targeted_ghl_history returns empty list).
        _ghl_creds_missing = False
        if not is_demo and not is_api_source:
            _ghl_creds_missing = token_error is not None and token_error != 'expired'
            # Also skip GHL when token is expired and we're on a worker
            # that can't refresh it (no OAuth env vars).  Go straight to
            # Twilio fallback instead of wasting an HTTP call that will 401.
            if not _ghl_creds_missing and token_error == 'expired':
                if not has_oauth_credentials(force_recheck=True):
                    _ghl_creds_missing = True
                    logger.info(f"Worker without OAuth env vars + expired token — "
                               f"will skip GHL and use Twilio fallback for {location_id}")
                else:
                    # Creds just appeared in Redis since the initial token check
                    # (web service published them).  Re-fetch the token — either
                    # the cron already refreshed it in DB, or we can now refresh
                    # it ourselves with the Redis-shared creds.  This avoids a
                    # wasted 401 round-trip with the stale expired token.
                    retry_token = get_valid_token(location_id, subscriber=subscriber)
                    if retry_token and retry_token != auth_token:
                        logger.info(f"🔄 Token re-fetched after Redis creds appeared for "
                                   f"{location_id} — using fresh token")
                        auth_token = retry_token
                        token_error = None
                    else:
                        # Creds available but refresh still failed (revoked
                        # refresh_token, GHL API down, etc.).  Skip GHL to
                        # avoid wasting ~15s on a 401 round-trip + failed
                        # recovery — go straight to Twilio fallback.
                        _ghl_creds_missing = True
                        logger.warning(f"OAuth creds available but token refresh failed for "
                                      f"{location_id} — skipping GHL, using Twilio fallback")

        # Inject fresh token (empty for API sources without GHL)
        subscriber['access_token'] = auth_token

        # === USE PAYLOAD DATA AS-IS (Source of Truth from GHL) ===
        # DO NOT fetch from GHL API unless we detect a problem
        # The payload contains everything we need - GHL already sent it
        first_name = payload.get("first_name") or ""
        dob_str = payload.get("age") or ""
        address = payload.get("address") or ""
        intent = payload.get("intent") or ""
        age = calculate_age_from_dob(date_of_birth=dob_str) if dob_str else None

        # Enriched contact data from normalized payload (for individual profile)
        last_name = payload.get("last_name") or ""
        contact_city = payload.get("city") or ""
        contact_state = payload.get("state") or ""
        contact_gender = payload.get("gender") or ""
        contact_source = payload.get("source") or ""
        contact_tags = payload.get("tags") or []
        contact_custom_fields = payload.get("custom_fields") or []
        contact_company = payload.get("company_name") or ""
        # Notes require a separate GHL API call — not in webhook payload
        # They'll be fetched when available (voice prompt does this already)

        # === SMART LEAD TYPE DETECTION ===
        # Cross-references GHL tags + date imported + custom fields to determine
        # true lead freshness. Tags alone are unreliable (stale tags happen).
        from lead_resolver import resolve_lead_type
        lead_info = resolve_lead_type(
            tags=payload.get("tags"),
            date_added=payload.get("date_added"),
            custom_fields=payload.get("custom_fields"),
            source=payload.get("source"),
        )
        lead_type = lead_info["lead_type"]
        lead_vendor = lead_info["lead_vendor"]

        logger.info(
            f"✅ USING PAYLOAD DATA | contact_id={contact_id} | first_name={first_name} | "
            f"lead_type={lead_type} ({lead_info['confidence']}) | vendor={lead_vendor or 'none'} | "
            f"days={lead_info['days_since_import']} | reason={lead_info['reason']}"
        )

        initial_facts = []
        if first_name: initial_facts.append(f"First name: {first_name}")
        if age and age != "unknown": initial_facts.append(f"Age: {age}")
        # PRIVACY: Do NOT save address as a fact - only for backend context
        # if address: initial_facts.append(f"Address: {address}")
        if intent: initial_facts.append(f"Intent: {intent}")
        if lead_vendor: initial_facts.append(f"Lead vendor: {lead_vendor}")

        if initial_facts and contact_id != "unknown":
            save_new_facts(contact_id, initial_facts)

        # === History Sync (only if DB empty or gap, skip for API sources) ===
        db_count = get_message_count(contact_id)
        if not is_demo and not is_api_source and not _ghl_creds_missing:
            if db_count == 0:
                logger.info(f"🚨 DB empty for {contact_id} — fetching full GHL history")
                ghl_history = fetch_targeted_ghl_history(contact_id, location_id, auth_token, limit=50)
                sync_messages_to_db(contact_id, location_id, ghl_history)
            elif db_count <= 3:
                logger.info(f"🧐 Small DB count ({db_count}) for {contact_id} — syncing recent")
                ghl_history = fetch_targeted_ghl_history(contact_id, location_id, auth_token, limit=10)
                sync_messages_to_db(contact_id, location_id, ghl_history)

            # Re-fetch token from DB in case fetch_targeted_ghl_history refreshed it
            # internally during a 401 retry — prevents stale token from propagating
            # to downstream SMS send and other API calls
            refreshed_token = get_valid_token(location_id)
            if refreshed_token and refreshed_token != auth_token:
                logger.info(f"🔄 Token was refreshed during history sync for {location_id} — updating")
                auth_token = refreshed_token
                subscriber['access_token'] = refreshed_token

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
        if not is_demo and not is_workflow_outreach and message_id:
            conn = get_db_connection()
            if conn:
                cur = None
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
                    logger.error(f"Idempotency check failed — processing anyway to avoid message loss: {e}")
                    if conn:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                finally:
                    if cur:
                        cur.close()
                    return_db_connection(conn)

        if message and not is_workflow_outreach:
            save_message(contact_id, message, "lead")
            # Fire rich event so SSE stream can push a live inbox update to the dashboard
            _contact_name = payload.get('first_name') or payload.get('name') or 'Contact'
            log_webhook_event(
                location_id, "new_inbound_message", "info",
                f"Message from {_contact_name}",
                contact_id=contact_id,
                details={
                    "contact_name": _contact_name,
                    "message_preview": message[:80],
                    "direction": "inbound",
                }
            )

        # === TCPA STOP WORD CHECK ===
        # If the lead says stop/unsubscribe/blocked/leave me alone/etc, we MUST stop messaging.
        # Check BEFORE booking or response generation.
        # TCPA stop words — ONLY matches that are the ENTIRE message or standalone opt-outs.
        # "stop texting me" and "stop calling me" are NOT TCPA opt-outs — they are objections
        # that the bot should handle. Only bare "stop", "unsubscribe", etc. halt messaging.
        TCPA_STOP_EXACT = [
            # These match ONLY when they are the entire message (or nearly)
            "stop", "unsubscribe", "cancel",
        ]
        TCPA_STOP_PHRASES = [
            # These match as phrases within longer messages
            "remove me", "opt out", "opt me out",
            "do not call", "don't call me", "do not text", "don't text me",
            "do not contact", "don't contact me", "do not message", "don't message me",
            "put me on your do not call list", "do not call list",
        ]
        # Phrases that LOOK like opt-outs but are objections (not TCPA)
        TCPA_EXCEPTIONS = [
            "stop texting", "stop calling", "stop messaging", "stop contacting",
            "stop sending", "stop reaching out", "stop bothering",
        ]
        if message:
            msg_lower = message.lower().strip()
            # First check exceptions — if the message matches an exception pattern,
            # treat it as an objection, not a TCPA opt-out
            is_exception = any(exc in msg_lower for exc in TCPA_EXCEPTIONS)
            if not is_exception:
                # Check exact matches (bare "stop", "unsubscribe", "cancel")
                msg_words = msg_lower.split()
                if len(msg_words) <= 3 and any(
                    re.search(rf'\b{re.escape(sw)}\b', msg_lower) for sw in TCPA_STOP_EXACT
                ):
                    matched = next(sw for sw in TCPA_STOP_EXACT if re.search(rf'\b{re.escape(sw)}\b', msg_lower))
                    logger.info(f"🛑 TCPA OPT-OUT: '{matched}' detected from contact {contact_id} | msg='{message}'")
                    return {"status": "opt_out", "reason": f"TCPA stop word: {matched}", "contact_id": contact_id}
                # Check phrase matches
                for stop_phrase in TCPA_STOP_PHRASES:
                    if re.search(rf'\b{re.escape(stop_phrase)}\b', msg_lower):
                        logger.info(f"🛑 TCPA OPT-OUT: '{stop_phrase}' detected from contact {contact_id} | msg='{message}'")
                        return {"status": "opt_out", "reason": f"TCPA stop word: {stop_phrase}", "contact_id": contact_id}

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
            try:
                contracted_carriers = json.loads(contracted_carriers)
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
            lead_type=lead_type,
            last_name=last_name,
            company_name=contact_company,
            tags=contact_tags,
            custom_fields=contact_custom_fields,
            source=contact_source,
            city=contact_city,
            state=contact_state,
            gender=contact_gender,
        )

        recent_exchanges = director_output["recent_exchanges"]

        # ============================================================
        # BOOKING DETECTION & EXECUTION
        # ============================================================
        booking_made = False
        actual_booked_time = ""  # Populated with the real booked time from calendar API
        booking_result = detect_booking_request(
            message=message,
            recent_exchanges=recent_exchanges,
            stage=director_output["stage"],
            timezone=subscriber.get("timezone", "America/Chicago"),
        )
        is_booking_request = booking_result.action == "book"
        booking_time_str = booking_result.time_string
        wants_slots = booking_result.action == "offer_slots"
        logger.info(f"📅 Booking detection: action={booking_result.action} time={booking_result.time_string} reason={booking_result.reason} | contact={contact_id}")

        # If the lead is directly asking to book or asking for available times,
        # override the stage to BOOKING so the tactical directive doesn't
        # contradict by saying "wait for impact." The lead's intent is clear.
        if (is_booking_request or wants_slots) and director_output["stage"] not in ("booked", "closed"):
            if director_output["stage"] != "booking":
                logger.info(f"📅 Stage override: {director_output['stage']} → booking (lead directly requested)")
                director_output["stage"] = "booking"
        
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
                actual_booked_time = booking_time_str
            elif use_crm_adapter:
                # Non-GHL CRM: Use adapter system
                try:
                    from crm_adapters.factory import get_adapter_for_subscriber
                    adapter = get_adapter_for_subscriber(subscriber)
                    crm_book_result = adapter.book_appointment(
                        contact_id=contact_id,
                        first_name=first_name,
                        selected_time=booking_time_str
                    )
                    if crm_book_result:
                        logger.info(f"✅ APPOINTMENT BOOKED via {adapter.CRM_NAME} for {contact_id}")
                        booking_made = True
                        # Use actual booked time if adapter returned it, else fall back to requested time
                        actual_booked_time = crm_book_result if isinstance(crm_book_result, str) else booking_time_str
                        log_webhook_event(location_id, "booking_success", "success",
                                          f"Booked via {adapter.CRM_NAME}: {actual_booked_time}",
                                          contact_id=contact_id, details={"crm": adapter.CRM_NAME, "time": actual_booked_time})
                    else:
                        logger.warning(f"⚠️ BOOKING FAILED via {adapter.CRM_NAME} for {contact_id}")
                        log_webhook_event(location_id, "booking_failed", "error",
                                          f"Booking failed via {adapter.CRM_NAME}",
                                          contact_id=contact_id, details={"crm": adapter.CRM_NAME})
                except Exception as adapter_err:
                    logger.error(f"CRM adapter booking error: {adapter_err}", exc_info=True)
            else:
                # GHL: Use existing direct code path
                # Skip when OAuth creds are missing — calendar API will fail
                if _ghl_creds_missing:
                    logger.warning(f"⚠️ GHL creds missing — skipping calendar booking for {contact_id}")
                    ghl_book_result = False
                else:
                    # Pass all contact location fields for timezone resolution fallback chain
                    contact_state = payload.get('state') or ''
                    contact_phone_tz = payload.get('phone') or payload.get('contact_phone', '')
                    contact_city = payload.get('city') or ''
                    contact_zip = payload.get('zip') or payload.get('postal_code') or ''
                    contact_address = payload.get('address') or ''
                    ghl_book_result = consolidated_calendar_op(
                        operation="book",
                        subscriber_data=subscriber,
                        contact_id=contact_id,
                        first_name=first_name,
                        selected_time=booking_time_str,
                        contact_phone=contact_phone_tz,
                        contact_state=contact_state,
                        contact_city=contact_city,
                        contact_zip=contact_zip,
                        contact_address=contact_address,
                    )

                if ghl_book_result:
                    logger.info(f"✅ APPOINTMENT BOOKED for {contact_id}")
                    booking_made = True
                    # ghl_book_result is now the actual booked time string (e.g., "11:00 AM on Tuesday, March 17")
                    actual_booked_time = ghl_book_result if isinstance(ghl_book_result, str) else booking_time_str
                    log_webhook_event(location_id, "booking_success", "success",
                                      f"Booked via LeadConnector: {actual_booked_time}",
                                      contact_id=contact_id, details={"crm": "LeadConnector", "time": actual_booked_time})
                else:
                    logger.warning(f"⚠️ BOOKING FAILED for {contact_id} - Grok will handle response")
                    log_webhook_event(location_id, "booking_failed", "error",
                                      "Booking failed via LeadConnector",
                                      contact_id=contact_id)

        # === Calendar fetch logic (for offering slots - only if NOT already booking) ===
        calendar_slots = ""
        booking_attempted_and_failed = is_booking_request and booking_time_str and not booking_made
        if (director_output["stage"] == "booking" or wants_slots or booking_attempted_and_failed) and not booking_made:
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
                    calendar_slots = "CALENDAR_UNAVAILABLE"
            else:
                # GHL: Use existing direct code path
                # Skip when OAuth creds are missing — calendar API will fail
                if _ghl_creds_missing:
                    calendar_slots = "CALENDAR_UNAVAILABLE"
                else:
                    # Pass all contact location fields for timezone resolution fallback chain
                    contact_state_tz = payload.get('state') or ''
                    contact_phone_tz2 = payload.get('phone') or payload.get('contact_phone', '')
                    contact_city_tz = payload.get('city') or ''
                    contact_zip_tz = payload.get('zip') or payload.get('postal_code') or ''
                    contact_address_tz = payload.get('address') or ''
                    calendar_slots = consolidated_calendar_op(
                        "fetch_slots", subscriber,
                        contact_phone=contact_phone_tz2,
                        contact_state=contact_state_tz,
                        contact_city=contact_city_tz,
                        contact_zip=contact_zip_tz,
                        contact_address=contact_address_tz,
                    )

        # Handle calendar unavailability — convert sentinel to actionable guidance
        calendar_unavailable = False
        if calendar_slots == "CALENDAR_UNAVAILABLE":
            calendar_unavailable = True
            calendar_slots = ""  # Don't pass sentinel string to LLM
            logger.warning(f"⚠️ CALENDAR UNAVAILABLE for {location_id} — bot will ask for preferred time")

        context_nudge = ""
        if message and "covered" in message.lower():
            context_nudge = "Lead claims coverage."

        # Add booking context — all nudges must sound like a human agent's inner monologue,
        # never reference "systems", "technical issues", "calendars being unavailable", etc.
        # The lead must never suspect they are talking to anything other than a real person.
        if booking_made:
            # Use the actual booked time from the calendar API (not the raw customer request)
            booked_display = actual_booked_time or booking_time_str
            context_nudge += f"""
APPOINTMENT CONFIRMED: {booked_display}.

You just got them on the calendar for {booked_display}. Confirm the exact time in your own words. Say something like "Got you down for {booked_display}" or "Perfect, you're all set for {booked_display}." Then ask if they got the invite — they should see it in their email. Do NOT say "I'll send you a confirmation" or "a calendar invite is coming" — the invite is already sent automatically. Just confirm the time and ask "do you see the invite?" Stop selling. Do not ask for their phone number, email, or any contact info — you already have it, you are texting them. End warmly."""
            logger.info(f"✅ BOOKING CONFIRMATION ADDED TO PROMPT | contact={contact_id} | booked_time={booked_display}")
        elif booking_attempted_and_failed:
            # Booking was attempted but failed — guide AI without any technical language
            if calendar_unavailable:
                context_nudge += f"""
The lead asked for {booking_time_str} but you could not get them on the calendar right now. Do NOT tell them you are booked. Do NOT mention any issues with your calendar or any technical problems. You are a human agent — just smoothly ask what day and time work best for them. Say something like "What day works best for you — morning or afternoon?" Keep it natural. You will get them booked on their next message."""
            else:
                context_nudge += f"""
The lead asked for {booking_time_str} but that slot just got taken. Do NOT tell them they are booked. Do NOT mention any errors or issues. Just let them know that time filled up and offer the available times below instead. Say something like "Ah that one just got grabbed — but I have these open:" and present the alternatives naturally. Let them pick."""
            logger.warning(f"⚠️ BOOKING ATTEMPTED BUT FAILED — offering alternative slots | contact={contact_id} | requested={booking_time_str} | calendar_unavailable={calendar_unavailable}")
        else:
            # No booking attempted yet — prevent hallucinated confirmations
            if calendar_unavailable and (director_output["stage"] == "booking" or wants_slots):
                context_nudge += """
You do not have your schedule pulled up right now. Do NOT say "let me check my calendar" or promise to send times. Just ask what day and time work best for them — "What day works for you?" Keep it simple and natural. You will get them booked once they give you a specific time."""
            else:
                context_nudge += "\nNo appointment has been made yet. Do NOT tell the lead they are booked or confirm any appointment. Offer times from the available slots below, or ask what time works for them."
            logger.info(f"🚫 NO BOOKING YET | contact={contact_id} | calendar_unavailable={calendar_unavailable}")

        # Note: Follow-up strategy (including humor at 5+ unanswered) is now handled
        # by sales_director's tactical_narrative via _build_followup_guidance().
        # No duplicate re-engagement block needed here.

        # === PIPELINE STAGE INJECTION (from synced GHL data) ===
        pipeline_context = ""
        try:
            from ghl_sync import get_contact_pipeline_stage
            opp = get_contact_pipeline_stage(location_id, contact_id)
            if opp:
                pipeline_context = (f"\n[PIPELINE] Lead is in pipeline '{opp['pipeline_name']}' "
                                    f"at stage '{opp['stage_name']}' (status: {opp['status']})")
                if opp.get('monetary_value'):
                    pipeline_context += f" — deal value: ${opp['monetary_value']:,.0f}"
                logger.info(f"Pipeline context injected for {contact_id}: {opp['stage_name']}")
        except Exception as e:
            logger.debug(f"Pipeline stage lookup skipped: {e}")

        # Combine all context: nudge + underwriting + company intel + pipeline
        extra_context = director_output['underwriting_context']
        if director_output.get('company_context'):
            extra_context = f"{extra_context}\n[COMPANY INTEL] {director_output['company_context']}".strip()
        if pipeline_context:
            extra_context = f"{extra_context}{pipeline_context}".strip()
        final_nudge = f"{context_nudge}\n{extra_context}".strip()

        # === LEAD RE-ENGAGEMENT CHECK ===
        # If re-engagement is disabled and this is a follow-up (no inbound message),
        # skip responding entirely — let the lead come to us.
        # Workflow outreach bypasses this check (explicitly requested by workflow).
        if not is_workflow_outreach and not message and not bot_settings.get("lead_reengagement", True):
            bot_msgs = [m for m in recent_exchanges if m.get('role') == 'assistant']
            if len(bot_msgs) >= 1:
                logger.info(f"🚫 RE-ENGAGEMENT DISABLED | Skipping follow-up for {contact_id}")
                return {"status": "skipped", "reason": "lead_reengagement disabled", "contact_id": contact_id}

        # === WORKFLOW OUTREACH: Manual message bypass ===
        if is_workflow_outreach and payload.get("_manual_message"):
            reply = payload["_manual_message"]
            logger.info(f"📨 WORKFLOW MANUAL MSG | contact={contact_id} | msg='{reply[:60]}'")
            # Skip AI generation — jump straight to sending
            # (falls through to the send section below)

        # === WORKFLOW OUTREACH: Prompt hint injection ===
        if is_workflow_outreach and payload.get("_prompt_hint"):
            _wf_hint = payload["_prompt_hint"]
            final_nudge = f"{final_nudge}\n\n[WORKFLOW DIRECTIVE] {_wf_hint}".strip()
            logger.info(f"🤖 WORKFLOW AI OUTREACH | contact={contact_id} | hint='{_wf_hint[:80]}'")

        # === INITIAL MESSAGE / OUTBOUND DRIP BYPASS ===
        initial_msg = subscriber.get('initial_message', '').strip()
        outbound_msgs = bot_settings.get("outbound_messages", [])
        if not (is_workflow_outreach and payload.get("_manual_message")):
            reply = ""

        if not reply and not message and not recent_exchanges and initial_msg:
            # First ever contact — use configured initial message
            reply = initial_msg
            logger.info(f"📨 USING CONFIGURED INITIAL MESSAGE | contact={contact_id} | msg='{reply[:60]}'")

        elif not message and outbound_msgs:
            # Custom outbound drip: check how many bot messages have been sent,
            # send the next custom template if available
            bot_msgs_sent = len([m for m in recent_exchanges if m.get('role') == 'assistant'])
            # The initial_message counts as message 0, custom drip starts at index 0
            # after the initial message. If no initial_message, drip starts immediately.
            drip_index = bot_msgs_sent - (1 if initial_msg else 0)
            if 0 <= drip_index < len(outbound_msgs):
                reply = outbound_msgs[drip_index]
                logger.info(f"📨 OUTBOUND DRIP #{drip_index + 1}/{len(outbound_msgs)} | contact={contact_id} | msg='{reply[:60]}'")

        if not reply:
            # === NORMAL LLM FLOW ===
            # Effective stage for this turn — used both in system prompt and message history
            effective_stage = "closed" if booking_made else director_output["stage"]

            system_prompt = build_system_prompt(
                bot_first_name=bot_first_name,
                timezone=timezone,
                profile_str=director_output["profile_str"],
                tactical_narrative=director_output["tactical_narrative"],
                known_facts=director_output["known_facts"],
                story_narrative=director_output["story_narrative"],
                stage=effective_stage,
                recent_exchanges=recent_exchanges,
                message=message,
                calendar_slots=calendar_slots,
                context_nudge=final_nudge,
                lead_type=lead_type,
                personal_website=personal_website,
                contracted_carriers=contracted_carriers,
                bot_settings=bot_settings,
            )

            # === MULTI-TURN CONVERSATION FORMAT ===
            # Build proper multi-turn messages so the LLM sees the conversation
            # as a real back-and-forth, not a flat string buried in the system prompt.
            # The system prompt still contains all context (profile, tactical guidance,
            # narrative, etc.) but the actual conversation is structured as alternating
            # user/assistant messages, giving the LLM much better conversational coherence.
            full_messages = [{"role": "system", "content": system_prompt}]

            # Add conversation history as proper multi-turn messages
            # Use all recent_exchanges (up to 30) for full context
            for msg in recent_exchanges:
                if msg['role'] == 'lead':
                    full_messages.append({"role": "user", "content": msg['text']})
                else:
                    full_messages.append({"role": "assistant", "content": msg['text']})

            # Add the current inbound message as the final user message
            if message and message.strip():
                full_messages.append({"role": "user", "content": message})

            reply = generate_clean_reply(
                client=client,
                system_prompt=system_prompt,
                user_message=message,
                bot_name=bot_first_name,
                full_messages=full_messages,
            )

        if not reply:
            logger.error(f"LLM produced no usable reply after retry. Skipping send. contact={contact_id}")
            return

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
            logger.error(f"BLOCKED VARIABLE/PLACEHOLDER: '{reply}' — skipping send. contact={contact_id}")
            return

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
                save_message(contact_id, reply, "assistant", stage=effective_stage)
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
                sent = False
                fail_reason = None
                http_detail = None

                # Determine SMS channel: GHL (default), last_used, or direct Twilio number
                # SMS Bot tier always routes through GHL — no Twilio direct access
                sub_tier = subscriber.get('subscription_tier', 'individual')
                sms_send_via = 'ghl' if sub_tier == 'sms_bot' else subscriber.get('sms_send_via', 'ghl')

                # Resolve "last_used" to actual number from call/SMS history
                if sms_send_via == 'last_used':
                    resolved_number = None
                    try:
                        conn = get_db_connection()
                        try:
                            cur = conn.cursor()
                            # Find the most recent outbound call to this contact's phone
                            contact_phone_raw = payload.get('phone') or payload.get('contact_phone', '')
                            cur.execute("""
                                SELECT phone FROM call_history
                                WHERE location_id = %s AND contact_id = %s AND direction LIKE 'outbound%%'
                                ORDER BY created_at DESC LIMIT 1
                            """, (location_id, contact_id))
                            row = cur.fetchone()
                            if row and row[0]:
                                resolved_number = row[0]
                                logger.info(f"[last_used] Resolved to {resolved_number} from call history for {contact_id}")
                            cur.close()
                        finally:
                            return_db_connection(conn)
                    except Exception as lu_err:
                        logger.warning(f"[last_used] DB lookup failed: {lu_err}")

                    if resolved_number and resolved_number.startswith('+'):
                        sms_send_via = resolved_number
                    else:
                        # No call history found — fall back to GHL
                        logger.info(f"[last_used] No call history for {contact_id}, falling back to GHL")
                        sms_send_via = 'ghl'

                use_twilio_direct = (sms_send_via and sms_send_via.startswith('+'))

                if use_twilio_direct:
                    # Direct Twilio SMS: bypass GHL entirely, send from subscriber's Twilio number
                    try:
                        from twilio_sms import send_sms_via_twilio, get_twilio_credentials
                        sub_sid, sub_auth, from_number = get_twilio_credentials(location_id)
                        contact_phone = payload.get('phone') or payload.get('contact_phone', '')
                        if sub_sid and sub_auth and contact_phone:
                            # Use from_number from get_twilio_credentials — it returns
                            # the subscriber's chosen number when sub-account creds are
                            # available, or master_phone when falling back to master.
                            # Using sms_send_via directly would break master fallback
                            # (master account can't send from a sub-account number).
                            sent, fail_reason, http_detail = send_sms_via_twilio(
                                phone_to=contact_phone,
                                message=reply,
                                from_number=from_number,
                                twilio_sub_account_sid=sub_sid,
                                twilio_auth_token=sub_auth,
                                contact_id=contact_id,
                            )
                            if sent:
                                logger.info(f"✅ Twilio direct SMS sent to {contact_id} from {from_number}")
                                # Log to GHL via Conversation Provider so CRM stays in sync
                                # Skip when GHL creds are missing — token is empty/expired
                                if not _ghl_creds_missing and auth_token:
                                    try:
                                        from ghl_logger import log_outbound_sms_to_ghl
                                        log_outbound_sms_to_ghl(
                                            contact_id=contact_id,
                                            message=reply,
                                            access_token=auth_token,
                                            location_id=location_id,
                                            contact_phone=contact_phone,
                                        )
                                    except Exception as ghl_log_err:
                                        logger.debug(f"GHL conversation log skipped: {ghl_log_err}")
                        else:
                            # Fallback to GHL if Twilio creds missing (skip if GHL creds also missing)
                            if _ghl_creds_missing:
                                logger.warning(f"Twilio direct SMS fallback: missing Twilio AND GHL creds for {location_id}")
                                sent, fail_reason = False, 'no_credentials'
                            else:
                                logger.warning(f"Twilio direct SMS fallback: missing creds for {location_id}, using GHL")
                                sent, fail_reason, http_detail = send_sms_via_ghl(contact_id, reply, auth_token, location_id, conversation_id=conversation_id)
                    except Exception as twilio_err:
                        if _ghl_creds_missing:
                            logger.error(f"Twilio direct SMS error: {twilio_err}, GHL also unavailable")
                            sent, fail_reason = False, 'no_credentials'
                        else:
                            logger.error(f"Twilio direct SMS error: {twilio_err}, falling back to GHL")
                            sent, fail_reason, http_detail = send_sms_via_ghl(contact_id, reply, auth_token, location_id, conversation_id=conversation_id)

                elif use_crm_adapter:
                    # Non-GHL CRM: Use adapter for messaging
                    try:
                        from crm_adapters.factory import get_adapter_for_subscriber
                        adapter = get_adapter_for_subscriber(subscriber)
                        if adapter.SUPPORTS_MESSAGING:
                            sent = adapter.send_message(contact_id, reply)
                            fail_reason = None if sent else 'adapter'
                        else:
                            # CRM doesn't support messaging - use GHL as messaging fallback
                            # (some users use Zapier for booking but GHL for SMS)
                            if _ghl_creds_missing:
                                logger.warning(f"CRM adapter no messaging + GHL creds missing — "
                                              f"skipping GHL fallback for {contact_id}")
                                sent, fail_reason = False, 'no_credentials'
                            else:
                                sent, fail_reason, http_detail = send_sms_via_ghl(contact_id, reply, auth_token, location_id, conversation_id=conversation_id)
                    except Exception as adapter_err:
                        logger.error(f"CRM adapter send_message error: {adapter_err}")
                        sent = False
                        fail_reason = 'adapter'
                else:
                    # GHL (default): Use existing direct code path
                    # Skip GHL entirely when OAuth credentials are missing — avoids 3
                    # wasted retries (~45s) with an expired token that can never refresh.
                    # Go straight to Twilio fallback below instead.
                    if _ghl_creds_missing:
                        logger.warning(f"🔄 GHL OAuth creds missing — skipping GHL SMS for "
                                      f"{contact_id}, will try Twilio fallback")
                        sent = False
                        fail_reason = 'no_credentials'
                    else:
                        sent, fail_reason, http_detail = send_sms_via_ghl(contact_id, reply, auth_token, location_id, conversation_id=conversation_id)

                # === TOKEN RECOVERY ===
                # If SMS failed due to 401/403 auth, force-refresh the token and retry.
                # With OAuth env vars: performs a real token refresh via GHL API.
                # Without OAuth env vars (workers): re-reads from DB to pick up
                # tokens that were refreshed by the proactive cron job.
                if not sent and fail_reason == 'auth':
                    logger.warning(f"🔄 TOKEN RECOVERY: SMS auth failure for {contact_id} — "
                                  f"force-refreshing token for {location_id}")
                    recovered_token, was_refreshed, recovery_err = get_valid_token_with_status(
                        location_id, force_refresh=True)

                    if recovered_token and recovered_token != auth_token:
                        logger.info(f"🔄 Got fresh token for {location_id} — retrying SMS")
                        auth_token = recovered_token
                        subscriber['access_token'] = recovered_token
                        sent, fail_reason, http_detail = send_sms_via_ghl(
                            contact_id, reply, recovered_token, location_id,
                            conversation_id=conversation_id)

                        if sent:
                            logger.info(f"✅ TOKEN RECOVERY SUCCESS: SMS sent for {contact_id} "
                                       f"after token refresh")
                            log_webhook_event(location_id, "token_recovery", "success",
                                              f"Token recovered + SMS sent for {contact_id}",
                                              contact_id=contact_id)
                            # Audit and retry ALL recent auth-failed messages for this location
                            _audit_and_retry_failed_tasks(location_id, recovered_token)
                        else:
                            logger.error(f"❌ TOKEN RECOVERY: Got new token but SMS still "
                                        f"failed ({fail_reason}) for {contact_id}")
                            log_webhook_event(location_id, "token_recovery", "warning",
                                              f"Token refreshed but SMS retry failed ({fail_reason})",
                                              contact_id=contact_id)
                    elif recovered_token and recovered_token == auth_token:
                        # Same token returned — refresh didn't actually happen
                        logger.warning(f"⚠️ TOKEN RECOVERY: Force-refresh returned same token "
                                      f"for {location_id} — token may be valid but GHL "
                                      f"rejected the SMS for another reason")
                    else:
                        logger.error(f"❌ TOKEN RECOVERY FAILED: Could not refresh token "
                                    f"for {location_id} (error={recovery_err})")
                        log_webhook_event(location_id, "token_recovery", "error",
                                          f"Token recovery failed: {recovery_err}",
                                          contact_id=contact_id,
                                          details={"error": recovery_err})

                # === TWILIO SMS FALLBACK ===
                # If GHL SMS failed due to a channel issue (auth, network, server_error,
                # no_credentials) and subscriber has Twilio sub-account credentials, try
                # sending via Twilio direct as last resort. Skip fallback for message-level
                # blocks (safety, duplicate) — those apply regardless of channel.
                if not sent and sub_tier != 'sms_bot' and fail_reason not in ('safety', 'duplicate'):
                    try:
                        from twilio_sms import send_sms_via_twilio, get_twilio_credentials
                        fb_sub_sid, fb_sub_auth, fb_from_number = get_twilio_credentials(location_id)
                        contact_phone = payload.get('phone') or payload.get('contact_phone', '')
                        # If phone not in payload, try contact_cache as fallback
                        if not contact_phone and contact_id and contact_id != 'unknown':
                            try:
                                _fb_conn = get_db_connection()
                                if _fb_conn:
                                    try:
                                        _fb_cur = _fb_conn.cursor()
                                        _fb_cur.execute(
                                            "SELECT phone FROM contact_cache WHERE contact_id = %s LIMIT 1",
                                            (contact_id,))
                                        _fb_row = _fb_cur.fetchone()
                                        _fb_phone = (_fb_row.get('phone') if isinstance(_fb_row, dict)
                                                     else _fb_row[0] if _fb_row else None)
                                        if _fb_phone:
                                            contact_phone = _fb_phone
                                            logger.info(f"Resolved contact_phone from cache for {contact_id}: {contact_phone[:4]}***")
                                        _fb_cur.close()
                                    finally:
                                        return_db_connection(_fb_conn)
                            except Exception:
                                pass  # Best-effort lookup
                        if fb_sub_sid and fb_sub_auth and fb_from_number and contact_phone:
                            logger.warning(f"🔄 TWILIO FALLBACK: GHL SMS failed ({fail_reason}) — "
                                          f"attempting Twilio direct for {contact_id}")
                            sent, fail_reason, http_detail = send_sms_via_twilio(
                                phone_to=contact_phone,
                                message=reply,
                                from_number=fb_from_number,
                                twilio_sub_account_sid=fb_sub_sid,
                                twilio_auth_token=fb_sub_auth,
                                contact_id=contact_id,
                            )
                            if sent:
                                logger.info(f"✅ TWILIO FALLBACK SUCCESS: SMS sent to {contact_id} "
                                           f"via Twilio direct after GHL failure")
                                log_webhook_event(location_id, "twilio_fallback", "success",
                                                  f"SMS delivered via Twilio fallback for {contact_id}",
                                                  contact_id=contact_id)
                                # Log to GHL conversation so CRM stays in sync
                                # Skip when GHL creds are missing — token is empty
                                if not _ghl_creds_missing and auth_token:
                                    try:
                                        from ghl_logger import log_outbound_sms_to_ghl
                                        log_outbound_sms_to_ghl(
                                            contact_id=contact_id,
                                            message=reply,
                                            access_token=auth_token,
                                            location_id=location_id,
                                            contact_phone=contact_phone,
                                        )
                                    except Exception as ghl_log_err:
                                        logger.debug(f"GHL conversation log skipped (fallback): {ghl_log_err}")
                        else:
                            # Log exactly what's missing so we can diagnose
                            missing = []
                            if not fb_sub_sid:
                                missing.append("twilio_sub_account_sid")
                            if not fb_sub_auth:
                                missing.append("twilio_auth_token")
                            if not fb_from_number:
                                missing.append("from_number")
                            if not contact_phone:
                                missing.append("contact_phone")
                            logger.error(f"❌ TWILIO FALLBACK UNAVAILABLE for {contact_id}: "
                                        f"missing {', '.join(missing)} | location={location_id}")
                    except Exception as fb_err:
                        logger.warning(f"Twilio fallback not available for {contact_id}: {fb_err}")

                if sent:
                    save_message(contact_id, reply, "assistant", stage=effective_stage)
                    logger.info(f"✅ Message sent via {crm_type.upper()}")
                    log_webhook_event(location_id, "message_sent", "success",
                                      f"Reply sent ({len(reply)} chars)",
                                      contact_id=contact_id, details={"preview": reply[:80]})
                else:
                    http_status = (http_detail or {}).get('status_code', 0)
                    http_body = (http_detail or {}).get('response_body', '')
                    http_attempts = (http_detail or {}).get('attempts', 0)
                    logger.error(f"❌ SMS delivery failed for {contact_id} "
                                f"({fail_reason}, HTTP {http_status}) — "
                                f"all channels exhausted, saved locally only")
                    save_message(contact_id, reply, "assistant", stage=effective_stage)
                    log_webhook_event(location_id, "message_failed", "error",
                                      f"SMS HTTP {http_status} — {fail_reason}",
                                      contact_id=contact_id,
                                      details={"failure_reason": fail_reason or "unknown",
                                               "http_status_code": http_status,
                                               "http_response_body": http_body,
                                               "http_attempts": http_attempts,
                                               "reply": reply[:500],
                                               "contact_id": contact_id})
                    # Persist failed payload so the recovery cron can retry later
                    save_failed_webhook_payload(
                        location_id, contact_id, payload,
                        f"sms_delivery_failed:{fail_reason}")
            else:
                save_message(contact_id, reply, "assistant", stage=effective_stage)
                logger.info("⚠ DEMO MODE: Message saved internally")

        # ── Auto-refresh AI intelligence after conversation changes ──
        # Queue a background job to re-analyze this contact since new messages
        # were processed. This keeps Smart Filter classifications accurate
        # without blocking the webhook pipeline.
        try:
            import redis as _redis
            from rq import Queue as _Queue
            _r = _redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379'),
                                 socket_timeout=5, socket_connect_timeout=5)
            _q = _Queue('intelligence', connection=_r)
            _q.enqueue(
                analyze_contact_intelligence_task,
                location_id, contact_id,
                job_timeout=30,
                result_ttl=300,
            )
            logger.debug(f"🧠 Queued intelligence re-analysis for {contact_id}")
        except Exception as intel_err:
            logger.debug(f"Intelligence re-queue skipped: {intel_err}")

        # ── Check workflow triggers ──
        # After processing any webhook event, check if any active workflows
        # should be triggered by this event type.
        try:
            from workflow_engine import check_workflow_triggers
            # Determine the GHL event type from the payload
            event_type = _resolve_ghl_event_type(payload)
            if event_type and location_id and contact_id:
                event_data = {
                    "message": payload.get("message") or payload.get("body", ""),
                    "tags": payload.get("tags", []),
                    "phone": payload.get("phone", ""),
                    "pipeline_id": payload.get("pipeline_id", ""),
                    "stage_id": payload.get("stage_id", ""),
                    "calendar_id": payload.get("calendar_id", ""),
                    "dnd": payload.get("dnd"),
                    "source": payload.get("source", ""),
                }
                run_ids = check_workflow_triggers(location_id, event_type, contact_id, event_data)
                if run_ids:
                    logger.info(f"⚡ Workflow triggers fired: {len(run_ids)} runs for {event_type}")
        except Exception as wf_err:
            logger.debug(f"Workflow trigger check skipped: {wf_err}")

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


def _resolve_ghl_event_type(payload):
    """
    Determine the GHL event type from a normalized webhook payload.

    GHL webhooks include a 'type' field that maps to event types like
    'ContactCreate', 'InboundMessage', etc. We also infer event types
    from payload characteristics when the type field is ambiguous.
    """
    original = payload.get("_original_payload", {})

    # Check for explicit GHL event type
    event_type = (original.get("type") or original.get("event")
                  or payload.get("type") or payload.get("event") or "")

    # Normalize common GHL event types
    event_map = {
        "contactcreate": "ContactCreate",
        "contactupdate": "ContactUpdate",
        "contactdndupdate": "ContactDndUpdate",
        "inboundmessage": "InboundMessage",
        "outboundmessage": "OutboundMessage",
        "inboundcall": "InboundCall",
        "missedcall": "MissedCall",
        "voicemailreceived": "VoicemailReceived",
        "tagadded": "TagAdded",
        "tagremoved": "TagRemoved",
        "opportunitystageupdate": "OpportunityStageUpdate",
        "opportunitycreate": "OpportunityCreate",
        "appointmentcreate": "AppointmentCreate",
        "appointmentnoshow": "AppointmentNoShow",
        "appointmentupdate": "AppointmentUpdate",
    }

    normalized = event_map.get(event_type.lower().replace("_", "").replace(" ", ""), "")
    if normalized:
        return normalized

    # Infer from payload content
    message = payload.get("message") or payload.get("body", "")
    direction = str(payload.get("direction", "")).lower()

    if message and direction == "inbound":
        return "InboundMessage"
    if message and not direction:
        # Likely an inbound message webhook (most common)
        return "InboundMessage"
    if payload.get("appointment_id"):
        return "AppointmentCreate"
    if payload.get("opportunity_id") and payload.get("stage_id"):
        return "OpportunityStageUpdate"

    # Check for contact create patterns
    status = str(payload.get("status", "")).lower()
    if status == "new" and payload.get("contact_id") and not message:
        return "ContactCreate"

    return ""


def _audit_and_retry_failed_tasks(location_id: str, working_token: str):
    """After a successful token recovery, audit all recent auth-failed messages
    for this location and retry sending them with the working token.

    This catches messages that failed while the token was expired/revoked but
    the reply was already generated by the AI. Instead of re-running the full
    AI pipeline, we just re-send the saved reply text.

    Only retries messages from the last 60 minutes to avoid resending stale content.
    Each message is marked as 'retried' in the DB to prevent infinite retry loops.
    """
    try:
        failed_messages = get_auth_failed_messages(location_id, max_age_minutes=60, limit=50)
        if not failed_messages:
            logger.info(f"🔍 Token recovery audit: no auth-failed messages to retry "
                       f"for {location_id}")
            return

        logger.info(f"🔍 TOKEN RECOVERY AUDIT: Found {len(failed_messages)} auth-failed "
                   f"messages for {location_id} — retrying with recovered token")

        retried_count = 0
        success_count = 0

        for entry in failed_messages:
            log_id = entry.get('id')
            cid = entry.get('contact_id')
            details = entry.get('details') or {}

            # Get the reply text that was saved when the original send failed
            reply_text = details.get('reply')
            if not reply_text or not cid:
                logger.debug(f"Skipping audit entry {log_id}: missing reply or contact_id")
                mark_webhook_log_retried(log_id, success=False)
                continue

            retried_count += 1
            logger.info(f"🔄 AUDIT RETRY [{retried_count}/{len(failed_messages)}]: "
                       f"Resending to {cid} ({len(reply_text)} chars)")

            sent, fail_reason, retry_http = send_sms_via_ghl(cid, reply_text, working_token, location_id)

            if sent:
                success_count += 1
                mark_webhook_log_retried(log_id, success=True)
                log_webhook_event(location_id, "audit_retry_sent", "success",
                                  f"Audit retry: message resent to {cid}",
                                  contact_id=cid,
                                  details={"original_log_id": log_id,
                                           "preview": reply_text[:80]})
                logger.info(f"✅ AUDIT RETRY: Successfully resent to {cid}")
            else:
                mark_webhook_log_retried(log_id, success=False)
                log_webhook_event(location_id, "audit_retry_failed", "error",
                                  f"Audit retry failed for {cid} ({fail_reason})",
                                  contact_id=cid,
                                  details={"original_log_id": log_id,
                                           "failure_reason": fail_reason,
                                           "http_status_code": (retry_http or {}).get('status_code', 0),
                                           "http_response_body": (retry_http or {}).get('response_body', '')})
                logger.warning(f"❌ AUDIT RETRY: Failed to resend to {cid} ({fail_reason})")

                # If this retry also failed with auth, stop — token might be bad again
                if fail_reason == 'auth':
                    logger.error(f"🛑 AUDIT RETRY: Auth failure on retry — stopping audit "
                                f"(token may have expired again)")
                    break

            # Small delay between sends to avoid rate limiting
            time.sleep(1)

        logger.info(f"📊 TOKEN RECOVERY AUDIT COMPLETE for {location_id}: "
                   f"{success_count}/{retried_count} messages resent successfully "
                   f"(out of {len(failed_messages)} total failed)")

        if success_count > 0:
            log_webhook_event(location_id, "audit_complete", "success",
                              f"Token recovery audit: {success_count}/{retried_count} "
                              f"messages resent",
                              details={"total_found": len(failed_messages),
                                       "retried": retried_count,
                                       "succeeded": success_count})

    except Exception as e:
        logger.error(f"Token recovery audit failed for {location_id}: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ RQ TASKS: AI Intelligence Analysis (background) ═════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_contact_intelligence_task(location_id: str, contact_id: str) -> dict:
    """RQ task: Run AI intelligence analysis for a single contact.
    Called automatically after webhook processing to keep cache fresh.
    Also called on-demand for individual contact analysis.
    """
    try:
        from lead_intelligence import get_contact_intelligence
        result = get_contact_intelligence(location_id, contact_id)
        if result and result.get("temperature") != "unknown":
            score = result.get("score", 50)
            if isinstance(score, dict):
                score = score.get("score", 50)
            logger.info(f"🧠 Intelligence analyzed: {contact_id} → {result.get('temperature')} (score={score})")
            return {
                "status": "success",
                "contact_id": contact_id,
                "temperature": result.get("temperature"),
                "score": score,
            }
        return {"status": "no_result", "contact_id": contact_id}
    except Exception as e:
        logger.error(f"Intelligence analysis failed for {contact_id}: {e}")
        return {"status": "error", "contact_id": contact_id, "error": str(e)}


def analyze_contacts_batch_task(location_id: str, contact_ids: list) -> dict:
    """RQ task: Analyze a batch of contacts via bulk AI prompt (up to 500 per job).
    Uses 10 concurrent LLM calls of ~25 contacts each for maximum throughput.
    1000 contacts ≈ 2 minutes. Called by the dialer for uncached contacts.
    """
    if not contact_ids:
        return {"status": "empty", "analyzed": 0}

    from lead_intelligence import bulk_analyze_and_cache, get_bulk_cached_intelligence

    # Filter to only contacts that actually need analysis
    already_cached = get_bulk_cached_intelligence(location_id, contact_ids)
    need_analysis = [cid for cid in contact_ids if cid not in already_cached]

    if not need_analysis:
        return {"status": "success", "analyzed": 0, "total": 0}

    analyzed = bulk_analyze_and_cache(location_id, need_analysis)

    logger.info(f"🧠 Bulk batch complete: {analyzed}/{len(need_analysis)} contacts for {location_id}")
    return {"status": "success", "analyzed": analyzed, "total": len(need_analysis)}


def recover_failed_webhooks(max_age_hours: int = 24) -> dict:
    """Scourer: find all webhook tasks that failed due to token errors in the
    last N hours and attempt to reprocess them.

    Flow:
    1. Query failed_webhook_payloads table for unretried entries
    2. Group by location_id
    3. For each location, attempt to get a valid token (force-refresh)
    4. If token obtained, re-queue each failed payload via process_webhook_task
    5. Mark each payload as retried with the result

    This is designed to be called by a cron endpoint (e.g., every 15 minutes)
    and is safe to run concurrently — each payload is marked retried atomically
    before reprocessing.

    Returns:
        dict with stats: {locations_checked, locations_recovered, total_found,
                          requeued, skipped, token_failures}
    """
    stats = {
        "locations_checked": 0,
        "locations_recovered": 0,
        "total_found": 0,
        "requeued": 0,
        "skipped": 0,
        "token_failures": 0,
    }

    try:
        # Step 1: Get all unretried failed webhooks
        failed = get_unretried_failed_webhooks(max_age_hours=max_age_hours, limit=200)
        stats["total_found"] = len(failed)

        if not failed:
            logger.info(f"🔍 SCOURER: No unretried failed webhooks in last {max_age_hours}h")
            return stats

        logger.info(f"🔍 SCOURER: Found {len(failed)} unretried failed webhooks "
                   f"in last {max_age_hours}h — attempting recovery")

        # Step 2: Group by location_id
        by_location = {}
        for entry in failed:
            loc = entry['location_id']
            if loc not in by_location:
                by_location[loc] = []
            by_location[loc].append(entry)

        # Step 3: For each location, try to get a valid token
        for location_id, entries in by_location.items():
            stats["locations_checked"] += 1
            logger.info(f"🔄 SCOURER: Processing {len(entries)} failed webhooks "
                       f"for location {location_id}")

            # Force-refresh token — tries both Public and Private app credentials
            try:
                token, was_refreshed, token_err = get_valid_token_with_status(
                    location_id, force_refresh=True)
            except Exception as e:
                logger.error(f"SCOURER: Token refresh exception for {location_id}: {e}")
                token = None
                token_err = str(e)

            if not token:
                logger.warning(f"⚠️ SCOURER: Still no valid token for {location_id} "
                              f"(error={token_err}) — skipping {len(entries)} webhooks")
                stats["token_failures"] += 1
                # Don't mark as retried — leave for next scourer run
                # (token might become available after user re-auths)
                continue

            logger.info(f"✅ SCOURER: Got valid token for {location_id} — "
                       f"re-queuing {len(entries)} failed webhooks")
            stats["locations_recovered"] += 1

            # Step 4: Re-queue each failed payload
            for entry in entries:
                payload_id = entry['id']
                contact_id = entry.get('contact_id', 'unknown')
                stored_payload = entry.get('payload') or {}

                # Ensure payload is a dict (JSONB comes back as dict from psycopg2)
                if isinstance(stored_payload, str):
                    try:
                        stored_payload = json.loads(stored_payload)
                    except Exception:
                        logger.error(f"SCOURER: Invalid JSON payload for id={payload_id}")
                        mark_failed_webhook_retried(payload_id, success=False,
                                                    result="invalid_payload")
                        stats["skipped"] += 1
                        continue

                # Tag the payload so the task knows it's a scourer replay
                stored_payload["_scourer_replay"] = True
                stored_payload["_scourer_replay_id"] = payload_id

                try:
                    import redis
                    from rq import Queue
                    redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
                    r = redis.from_url(redis_url, socket_timeout=5,
                                       socket_connect_timeout=5)
                    q = Queue('production', connection=r)
                    q.enqueue(
                        process_webhook_task,
                        stored_payload,
                        job_timeout=120,
                        result_ttl=86400,
                    )
                    mark_failed_webhook_retried(payload_id, success=True,
                                                result="requeued")
                    stats["requeued"] += 1
                    logger.info(f"✅ SCOURER: Re-queued webhook for {contact_id} "
                               f"(payload_id={payload_id})")
                except Exception as enqueue_err:
                    logger.error(f"SCOURER: Failed to re-queue payload {payload_id}: "
                                f"{enqueue_err}")
                    mark_failed_webhook_retried(payload_id, success=False,
                                                result=f"enqueue_error: {str(enqueue_err)[:100]}")
                    stats["skipped"] += 1

                # Small delay to avoid hammering Redis
                time.sleep(0.5)

        logger.info(f"📊 SCOURER COMPLETE: checked {stats['locations_checked']} locations | "
                   f"recovered {stats['locations_recovered']} | "
                   f"requeued {stats['requeued']}/{stats['total_found']} webhooks | "
                   f"token failures: {stats['token_failures']}")

        if stats["requeued"] > 0:
            log_webhook_event("SYSTEM", "scourer_complete", "success",
                              f"Scourer recovered {stats['requeued']} failed webhooks "
                              f"across {stats['locations_recovered']} locations",
                              details=stats)

    except Exception as e:
        logger.error(f"SCOURER CRITICAL ERROR: {e}", exc_info=True)
        stats["error"] = str(e)

    return stats


def backfill_failed_webhooks(max_age_hours: int = 96) -> dict:
    """Recover dropped webhooks and failed SMS sends from webhook_logs.

    Works BACKWARDS from outcomes — finds webhook_received entries that never
    got a corresponding message_sent (dropped messages), regardless of whether
    an explicit error entry exists.  Also catches SMS HTTP failures where the
    reply is saved but the send failed.

    Handles TWO failure types:

    1. dropped — A webhook_received exists with no matching message_sent
       within 10 min.  Reconstructs a minimal payload and re-queues the
       full AI pipeline.

    2. sms_http_fail — The AI reply was generated but the SMS HTTP call
       failed (auth 401/403, rate-limit 429, network, etc.). The reply text
       is already saved in the log's details->>'reply', so we just re-send
       the SMS directly — no need to re-run the LLM.

    Safe to run multiple times: webhook_received entries are marked with
    backfill_retried=true; message_failed entries with retried=true.

    Returns:
        dict with stats: {total_found, dropped_found, sms_http_found,
                          locations_checked, locations_recovered, requeued,
                          skipped, token_failures, no_message}
    """
    stats = {
        "total_found": 0,
        "dropped_found": 0,
        "sms_http_found": 0,
        "locations_checked": 0,
        "locations_recovered": 0,
        "requeued": 0,
        "skipped": 0,
        "token_failures": 0,
        "no_message": 0,
    }

    try:
        # Step 1: Query webhook_logs for both failure types
        failed_logs = get_token_failed_webhook_logs(max_age_hours=max_age_hours)
        stats["total_found"] = len(failed_logs)

        if not failed_logs:
            logger.info(f"🔍 BACKFILL: No dropped/failed webhooks in webhook_logs "
                       f"for last {max_age_hours}h")
            return stats

        # Count by type
        for entry in failed_logs:
            if entry.get('entry_type') == 'sms_http_fail':
                stats["sms_http_found"] += 1
            else:
                stats["dropped_found"] += 1

        logger.info(f"🔍 BACKFILL: Found {len(failed_logs)} issues in last "
                   f"{max_age_hours}h — {stats['dropped_found']} dropped webhooks, "
                   f"{stats['sms_http_found']} SMS HTTP failures — attempting recovery")

        # Step 2: Group by location_id
        by_location = {}
        for entry in failed_logs:
            loc = entry['location_id']
            if loc not in by_location:
                by_location[loc] = []
            by_location[loc].append(entry)

        # Step 3: For each location, try to get a valid token
        for location_id, entries in by_location.items():
            stats["locations_checked"] += 1
            n_dropped = sum(1 for e in entries if e.get('entry_type') == 'dropped')
            n_sms = sum(1 for e in entries if e.get('entry_type') == 'sms_http_fail')
            logger.info(f"🔄 BACKFILL: Processing location {location_id} — "
                       f"{n_dropped} dropped, {n_sms} SMS-HTTP failures")

            # Force-refresh token
            try:
                token, was_refreshed, token_err = get_valid_token_with_status(
                    location_id, force_refresh=True)
            except Exception as e:
                logger.error(f"BACKFILL: Token refresh exception for {location_id}: {e}")
                token = None
                token_err = str(e)

            if not token:
                logger.warning(f"⚠️ BACKFILL: Still no valid token for {location_id} "
                              f"(error={token_err}) — skipping {len(entries)} entries")
                stats["token_failures"] += 1
                continue

            logger.info(f"✅ BACKFILL: Got valid token for {location_id} — "
                       f"processing {len(entries)} entries")
            stats["locations_recovered"] += 1

            # Step 4: Process each entry based on its type
            for entry in entries:
                log_id = entry['id']
                contact_id = entry.get('contact_id')
                entry_type = entry.get('entry_type', 'dropped')

                if not contact_id:
                    # Permanent — no contact_id will never be fixable, mark so
                    # we don't re-process garbage forever
                    logger.warning(f"BACKFILL: Skipping log {log_id} — no contact_id")
                    if entry_type == 'sms_http_fail':
                        mark_webhook_log_retried(log_id, success=False)
                    else:
                        mark_webhook_log_backfill_retried(log_id, success=False)
                    stats["skipped"] += 1
                    continue

                # Both sms_http_fail and dropped webhooks: re-queue through
                # the full AI pipeline.  For SMS failures the old reply is
                # stale anyway (GHL was down), so let the pipeline re-fetch
                # conversation history and generate a fresh reply.
                message_preview = entry.get('message_preview') or ''
                first_name = entry.get('first_name') or ''

                if not message_preview:
                    logger.info(f"BACKFILL: Log {log_id} for {contact_id} has no "
                               f"message_preview — will rely on GHL history sync")
                    stats["no_message"] += 1

                reconstructed_payload = {
                    "contact_id": contact_id,
                    "location_id": location_id,
                    "message": message_preview,
                    "body": message_preview,
                    "first_name": first_name,
                    "_backfill_replay": True,
                    "_backfill_log_id": log_id,
                }

                try:
                    import redis
                    from rq import Queue
                    redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
                    r = redis.from_url(redis_url, socket_timeout=5,
                                       socket_connect_timeout=5)
                    q = Queue('production', connection=r)
                    q.enqueue(
                        process_webhook_task,
                        reconstructed_payload,
                        job_timeout=120,
                        result_ttl=86400,
                    )
                    if entry_type == 'sms_http_fail':
                        mark_webhook_log_retried(log_id, success=True)
                    else:
                        mark_webhook_log_backfill_retried(log_id, success=True)
                    stats["requeued"] += 1
                    logger.info(f"✅ BACKFILL: Re-queued {entry_type} for "
                               f"{contact_id} (log_id={log_id}, "
                               f"msg={message_preview[:50] if message_preview else 'N/A'})")
                except Exception as enqueue_err:
                    # Transient — leave unmarked for next run
                    logger.error(f"BACKFILL: Failed to re-queue log {log_id}: "
                                f"{enqueue_err} — will retry on next backfill run")
                    stats["skipped"] += 1

                time.sleep(0.5)

        total_recovered = stats['requeued']
        logger.info(f"📊 BACKFILL COMPLETE: checked {stats['locations_checked']} locations | "
                   f"recovered {stats['locations_recovered']} | "
                   f"requeued {stats['requeued']} / "
                   f"{stats['total_found']} total | "
                   f"token failures: {stats['token_failures']} | "
                   f"no_message: {stats['no_message']}")

        if total_recovered > 0:
            log_webhook_event("SYSTEM", "backfill_complete", "success",
                              f"Backfill recovered {total_recovered} entries "
                              f"(re-queued) across "
                              f"{stats['locations_recovered']} locations",
                              details=stats)

    except Exception as e:
        logger.error(f"BACKFILL CRITICAL ERROR: {e}", exc_info=True)
        stats["error"] = str(e)

    return stats


# ═══════════════════════════════════════════════════════════════════════════════
# CONTACT IMPORT TASK — Background job for CSV/Excel/TXT → GHL contact creation
# ═══════════════════════════════════════════════════════════════════════════════

def import_contacts_task(import_id):
    """Process a contact import job: create/update contacts via CRM API (GHL, HubSpot, etc.)."""
    import requests as http_requests
    from blueprints.contacts_import import _normalize_phone

    GHL_API_BASE = "https://services.leadconnectorhq.com"
    API_VERSION = "2021-07-28"

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Load import record
        cur.execute("""
            SELECT location_id, column_mapping, duplicate_strategy, apply_tags, file_data
            FROM contact_imports WHERE id = %s
        """, (import_id,))
        rec = cur.fetchone()
        if not rec:
            logger.error(f"Import {import_id}: record not found")
            return

        location_id, column_mapping, dupe_strategy, apply_tags, file_data = rec
        column_mapping = column_mapping if isinstance(column_mapping, dict) else json.loads(column_mapping or '{}')
        apply_tags = apply_tags if isinstance(apply_tags, list) else json.loads(apply_tags or '[]')
        file_data = file_data if isinstance(file_data, list) else json.loads(file_data or '[]')

        # Determine CRM type for this subscriber
        subscriber = get_subscriber_info_hybrid(location_id)
        crm_type = ((subscriber or {}).get("crm_type") or "ghl").lower()
        is_ghl = crm_type in ("ghl", "gohighlevel", "")

        # Get CRM adapter for non-GHL CRMs
        crm_adapter = None
        if not is_ghl:
            try:
                from crm_adapters.factory import get_adapter_for_subscriber
                crm_adapter = get_adapter_for_subscriber(subscriber)
            except Exception as e:
                logger.error(f"Import {import_id}: failed to get {crm_type} adapter: {e}")
                cur.execute("UPDATE contact_imports SET status = 'failed' WHERE id = %s", (import_id,))
                conn.commit()
                return

        # Get GHL token (only for GHL imports)
        token = None
        headers = {}
        if is_ghl:
            token = get_valid_token(location_id)
            if not token:
                cur.execute("UPDATE contact_imports SET status = 'failed' WHERE id = %s", (import_id,))
                conn.commit()
                logger.error(f"Import {import_id}: no valid GHL token for {location_id}")
                return

            headers = {
                "Authorization": f"Bearer {token}",
                "Version": API_VERSION,
                "Content-Type": "application/json",
            }

        # Mark as processing
        cur.execute("UPDATE contact_imports SET status = 'processing' WHERE id = %s", (import_id,))
        conn.commit()

        imported = 0
        updated = 0
        skipped = 0
        failed = 0
        errors = []

        for row_idx, row in enumerate(file_data):
            try:
                # Map columns to GHL fields
                contact_data = {"locationId": location_id}
                notes_value = None

                for csv_col, ghl_field in column_mapping.items():
                    val = row.get(csv_col, "").strip()
                    if not val:
                        continue
                    if ghl_field == "phone":
                        val = _normalize_phone(val)
                        if not val:
                            continue
                    if ghl_field == "tags":
                        # Tags can be comma-separated in the CSV
                        contact_data["tags"] = [t.strip() for t in val.split(",") if t.strip()]
                        continue
                    if ghl_field == "notes":
                        notes_value = val
                        continue
                    contact_data[ghl_field] = val

                # Apply bulk tags
                if apply_tags:
                    existing_tags = contact_data.get("tags", [])
                    contact_data["tags"] = list(set(existing_tags + apply_tags))

                # Must have at least phone or email
                if not contact_data.get("phone") and not contact_data.get("email"):
                    skipped += 1
                    errors.append({"row": row_idx + 2, "error": "No phone or email"})
                    continue

                # ── Duplicate check + create/update (CRM-aware) ──
                existing_id = None

                if crm_adapter and not is_ghl:
                    # Non-GHL CRM: use adapter for duplicate check + create/update
                    if contact_data.get("phone") and dupe_strategy in ("skip", "update"):
                        try:
                            existing = crm_adapter.search_contact(
                                phone=contact_data.get("phone"),
                                email=contact_data.get("email"),
                            )
                            if existing:
                                existing_id = existing.get("id")
                        except Exception:
                            pass

                    if existing_id and dupe_strategy == "skip":
                        skipped += 1
                        continue

                    # Map GHL field names to CRM-specific field names
                    adapter_data = {
                        "first_name": contact_data.get("firstName", ""),
                        "last_name": contact_data.get("lastName", ""),
                        "email": contact_data.get("email", ""),
                        "phone": contact_data.get("phone", ""),
                    }
                    # Pass through any extra fields
                    for k, v in contact_data.items():
                        if k not in ("firstName", "lastName", "email", "phone",
                                     "locationId", "tags"):
                            adapter_data[k] = v

                    try:
                        if existing_id and dupe_strategy == "update":
                            success = crm_adapter.update_contact(existing_id, adapter_data)
                            if success:
                                updated += 1
                                if notes_value:
                                    try:
                                        from crm_providers import get_provider
                                        provider = get_provider(crm_type)
                                        crm_config = (subscriber or {}).get("crm_config") or {}
                                        crm_token = crm_config.get("access_token", "")
                                        if provider and provider.HAS_ACTIVITY_LOGGING:
                                            provider.log_note(existing_id, notes_value, crm_token)
                                    except Exception:
                                        pass
                            else:
                                failed += 1
                                errors.append({"row": row_idx + 2, "error": f"{crm_type} update failed"})
                        else:
                            new_id = crm_adapter.create_contact(adapter_data)
                            if new_id:
                                imported += 1
                                if notes_value:
                                    try:
                                        from crm_providers import get_provider
                                        provider = get_provider(crm_type)
                                        crm_config = (subscriber or {}).get("crm_config") or {}
                                        crm_token = crm_config.get("access_token", "")
                                        if provider and provider.HAS_ACTIVITY_LOGGING:
                                            provider.log_note(new_id, notes_value, crm_token)
                                    except Exception:
                                        pass
                            else:
                                failed += 1
                                errors.append({"row": row_idx + 2, "error": f"{crm_type} create failed"})
                    except Exception as e:
                        failed += 1
                        errors.append({"row": row_idx + 2, "error": f"{crm_type} error: {str(e)[:100]}"})
                else:
                    # GHL path — unchanged
                    if contact_data.get("phone") and dupe_strategy in ("skip", "update"):
                        try:
                            lookup_url = f"{GHL_API_BASE}/contacts/lookup"
                            lookup_resp = http_requests.get(
                                lookup_url,
                                headers=headers,
                                params={"phone": contact_data["phone"], "locationId": location_id},
                                timeout=10,
                            )
                            if lookup_resp.status_code == 200:
                                lookup_data = lookup_resp.json()
                                contacts_found = lookup_data.get("contacts", [])
                                if contacts_found:
                                    existing_id = contacts_found[0].get("id")
                        except Exception:
                            pass  # Lookup failed, proceed with create

                    if existing_id and dupe_strategy == "skip":
                        skipped += 1
                        continue

                    if existing_id and dupe_strategy == "update":
                        # Update existing contact
                        update_payload = {k: v for k, v in contact_data.items()
                                          if k not in ("locationId",)}
                        try:
                            resp = http_requests.put(
                                f"{GHL_API_BASE}/contacts/{existing_id}",
                                headers=headers,
                                json=update_payload,
                                timeout=10,
                            )
                            if resp.status_code in (200, 201):
                                updated += 1
                                if notes_value:
                                    _add_contact_note(GHL_API_BASE, headers, existing_id, notes_value)
                            else:
                                failed += 1
                                errors.append({"row": row_idx + 2, "error": f"Update failed: HTTP {resp.status_code}"})
                        except Exception as e:
                            failed += 1
                            errors.append({"row": row_idx + 2, "error": f"Update error: {str(e)[:100]}"})
                    else:
                        # Create new contact
                        try:
                            resp = http_requests.post(
                                f"{GHL_API_BASE}/contacts/",
                                headers=headers,
                                json=contact_data,
                                timeout=10,
                            )
                            if resp.status_code in (200, 201):
                                imported += 1
                                if notes_value:
                                    new_id = resp.json().get("contact", {}).get("id")
                                    if new_id:
                                        _add_contact_note(GHL_API_BASE, headers, new_id, notes_value)
                            elif resp.status_code == 422:
                                if dupe_strategy == "skip":
                                    skipped += 1
                                else:
                                    failed += 1
                                    errors.append({"row": row_idx + 2, "error": f"GHL rejected: {resp.text[:100]}"})
                            else:
                                failed += 1
                                errors.append({"row": row_idx + 2, "error": f"Create failed: HTTP {resp.status_code}"})
                        except Exception as e:
                            failed += 1
                            errors.append({"row": row_idx + 2, "error": f"Create error: {str(e)[:100]}"})

                # Update progress every 10 rows
                if (row_idx + 1) % 10 == 0:
                    cur.execute("""
                        UPDATE contact_imports
                        SET imported = %s, updated = %s, skipped = %s, failed = %s
                        WHERE id = %s
                    """, (imported, updated, skipped, failed, import_id))
                    conn.commit()

                # Rate limit: 0.3s between API calls
                time.sleep(0.3)

                # Refresh token every 200 rows (GHL only — non-GHL adapters auto-refresh)
                if is_ghl and (row_idx + 1) % 200 == 0:
                    new_token = get_valid_token(location_id)
                    if new_token:
                        token = new_token
                        headers["Authorization"] = f"Bearer {token}"

            except Exception as e:
                failed += 1
                errors.append({"row": row_idx + 2, "error": f"Unexpected: {str(e)[:100]}"})

        # Final update
        cur.execute("""
            UPDATE contact_imports
            SET status = 'completed', imported = %s, updated = %s, skipped = %s, failed = %s,
                error_log = %s, completed_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (imported, updated, skipped, failed, json.dumps(errors[:500]), import_id))
        conn.commit()

        logger.info(f"Import {import_id} complete: {imported} imported, {updated} updated, "
                     f"{skipped} skipped, {failed} failed out of {len(file_data)} rows")

        # Refresh local contact cache
        try:
            from db import upsert_contact_cache
            # Trigger a cache refresh by clearing the synced_at
            cur.execute("""
                UPDATE contact_cache SET synced_at = '2000-01-01'
                WHERE location_id = %s
            """, (location_id,))
            conn.commit()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Import {import_id} critical error: {e}", exc_info=True)
        try:
            cur.execute("""
                UPDATE contact_imports SET status = 'failed', error_log = %s WHERE id = %s
            """, (json.dumps([{"row": 0, "error": f"Critical: {str(e)[:200]}"}]), import_id))
            conn.commit()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def _add_contact_note(api_base, headers, contact_id, note_text):
    """Add a note to a GHL contact."""
    import requests as http_requests
    try:
        http_requests.post(
            f"{api_base}/contacts/{contact_id}/notes",
            headers=headers,
            json={"body": note_text},
            timeout=10,
        )
    except Exception:
        pass