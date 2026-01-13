# ghl_message.py - Send SMS via GoHighLevel (Flawless 2026)
import logging
import os
import time as time_module
import requests
from datetime import datetime, timedelta
from db import get_db_connection

logger = logging.getLogger(__name__)

GHL_MESSAGES_URL = "https://services.leadconnectorhq.com/conversations/messages"

def send_sms_via_ghl(
    contact_id: str,
    message: str,
    access_token: str,
    location_id: str,
    max_retries: int = 3,
    retry_delay: int = 5
) -> bool:
    """
    Sends an SMS via GoHighLevel Conversations API.
    - Uses modern OAuth Bearer token (access_token)
    - Includes duplicate prevention (5-min window via DB check)
    - Retries on transient failures
    - Demo-safe: returns True without sending if access_token == 'DEMO'
    """
    if not contact_id or contact_id == "unknown":
        logger.warning("Cannot send SMS: invalid contact_id")
        return False

    if not access_token or not location_id:
        logger.warning(f"Cannot send SMS: missing token or location_id for {contact_id}")
        return False

    # Demo mode short-circuit
    if access_token == 'DEMO':
        logger.info(f"DEMO MODE: Simulated SMS send to {contact_id} | msg='{message[:50]}...'")
        # In demo, we still save the message (handled in tasks.py), so return success
        return True

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
                return True  # Treat as success (already sent)
        except Exception as e:
            logger.error(f"Duplicate check failed: {e}")
        finally:
            cur.close()
            conn.close()

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

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(GHL_MESSAGES_URL, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()

            logger.info(f"SMS sent successfully to {contact_id} on attempt {attempt}")
            return True

        except requests.HTTPError as e:
            status = e.response.status_code if e.response else 0
            logger.warning(f"GHL SMS attempt {attempt} failed ({status}): {e.response.text if e.response else 'No response'}")
            if status == 429:  # Rate limit — longer wait
                time_module.sleep(10)
            elif status in (401, 403):  # Auth issue — don't retry
                logger.error(f"Auth failure — aborting retries")
                break
        except requests.RequestException as e:
            logger.warning(f"GHL SMS attempt {attempt} network error: {e}")
        
        if attempt < max_retries:
            time_module.sleep(retry_delay * attempt)  # Exponential backoff feel

    logger.error(f"Failed to send SMS to {contact_id} after {max_retries} attempts")
    return False