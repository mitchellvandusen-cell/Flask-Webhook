# ghl_message.py - Send SMS via Lead Connector (Flawless 2026)
import logging
import os
import time as time_module
import requests
from datetime import datetime, timedelta
from db import get_db_connection, return_db_connection
from reply_sanitizer import is_safe_to_send

logger = logging.getLogger(__name__)

GHL_MESSAGES_URL = "https://services.leadconnectorhq.com/conversations/messages"


def send_sms_via_ghl(
    contact_id: str,
    message: str,
    access_token: str,
    location_id: str,
    max_retries: int = 3,
    retry_delay: int = 5
) -> tuple:
    """
    Sends an SMS via GoHighLevel Conversations API.

    Returns 3-tuple: (success, fail_reason, http_detail)
        (True,  None,         None)  — SMS sent successfully
        (True,  'duplicate',  None)  — Already sent recently (dedup)
        (False, 'auth',       {...}) — 401/403 auth failure (token expired/revoked)
        (False, 'rate_limit', {...}) — 429 rate limited after all retries
        (False, 'network',    {...}) — Network/timeout error after all retries
        (False, 'safety',     None)  — Blocked by safety filter
        (False, 'invalid',    None)  — Invalid contact_id or missing token
        (False, 'error',      {...}) — Other/unknown error

    http_detail dict (when present):
        {"status_code": int, "response_body": str, "attempts": int}
    """
    if not contact_id or contact_id == "unknown":
        logger.warning("Cannot send SMS: invalid contact_id")
        return False, 'invalid', None

    if not access_token or not location_id:
        logger.warning(f"Cannot send SMS: missing token or location_id for {contact_id}")
        return False, 'invalid', None

    # Demo mode short-circuit
    if access_token == 'DEMO':
        logger.info(f"DEMO MODE: Simulated SMS send to {contact_id} | msg='{message[:50]}...'")
        return True, None, None

    # SAFETY NET: Block messages contaminated with LLM reasoning/system prompt artifacts
    if not is_safe_to_send(message):
        logger.error(f"MESSAGE BLOCKED BY SAFETY NET for {contact_id} — contains system prompt artifacts")
        return False, 'safety', None

    # Duplicate prevention: check if same message sent in last 5 min
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT 1 FROM contact_messages
                WHERE contact_id = %s
                  AND message_type = 'assistant'
                  AND message_text = %s
                  AND created_at > NOW() - INTERVAL '5 minutes'
                LIMIT 1
            """, (contact_id, message.strip()))
            if cur.fetchone():
                logger.warning(f"SKIP DUPLICATE SMS: same message sent recently to {contact_id}")
                return True, 'duplicate', None  # Treat as success (already sent)
        except Exception as e:
            logger.error(f"Duplicate check failed: {e}")
        finally:
            cur.close()
            return_db_connection(conn)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }

    payload = {
        "type": "SMS",
        "contactId": contact_id,
        "message": message.strip(),
        "locationId": location_id
    }

    last_failure = 'error'
    last_status = 0
    last_body = ''
    total_attempts = 0
    for attempt in range(1, max_retries + 1):
        total_attempts = attempt
        try:
            resp = requests.post(GHL_MESSAGES_URL, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()

            logger.info(f"SMS sent successfully to {contact_id} on attempt {attempt}")
            return True, None, None

        except requests.HTTPError as e:
            last_status = e.response.status_code if e.response else 0
            last_body = (e.response.text[:500] if e.response else 'No response')
            logger.warning(f"GHL SMS attempt {attempt} failed (HTTP {last_status}): {last_body}")
            if last_status == 429:  # Rate limit — longer wait
                last_failure = 'rate_limit'
                time_module.sleep(10)
            elif last_status in (401, 403):  # Auth issue — don't retry with same token
                logger.error(f"Auth failure (HTTP {last_status}) — aborting retries for token recovery")
                return False, 'auth', {
                    "status_code": last_status,
                    "response_body": last_body,
                    "attempts": attempt,
                }
        except requests.RequestException as e:
            last_failure = 'network'
            last_body = str(e)[:500]
            logger.warning(f"GHL SMS attempt {attempt} network error: {e}")

        if attempt < max_retries:
            time_module.sleep(retry_delay * attempt)  # Exponential backoff feel

    logger.error(f"Failed to send SMS to {contact_id} after {max_retries} attempts ({last_failure})")
    return False, last_failure, {
        "status_code": last_status,
        "response_body": last_body,
        "attempts": total_attempts,
    }
