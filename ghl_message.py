import logging
import os
import requests
import time as time_module
from datetime import datetime, date, time, timedelta, timezone
import psycopg2

logger = logging.getLogger(__name__)

def send_sms_via_ghl(contact_id: str, message: str, api_key: str, location_id: str):
    if not contact_id or contact_id == "unknown":
        logger.warning("Invalid contact_id, cannot send SMS")
        return False
    
    if not api_key or not location_id:
        logger.warning(f"GHL_API_KEY or GHL_LOCATION_ID missing for location {location_id}, cannot send SMS")
        return False

    # Check for duplicate send (last 5 min)
    conn = None
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM nlp_memory
            WHERE contact_id = %s
            AND role = 'assistant'
            AND created_at > CURRENT_TIMESTAMP - INTERVAL '5 minutes'
            LIMIT 1
        """, (contact_id,))
        
        if cur.fetchone():
            logger.info(f"Duplicate send prevented for {contact_id} — recent message sent")
            cur.close()
            conn.close()
            return True  # Treat as success to avoid retry
        
        cur.close()
    except Exception as e:
        logger.warning(f"Duplicate check failed: {e}")
    finally:
        if conn:
            conn.close()

    # SMS Sending Logic
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

    max_retries = 3
    retry_delay = 5  # seconds
    for attempt in range(max_retries):
        try:
            # Short timeout to prevent the whole bot from hanging
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            
            if response.status_code in [200, 201]:
                logger.info(f"SMS sent successfully to {contact_id} on attempt {attempt + 1}")
                return True
            else:
                logger.error(f"GHL SMS failed on attempt {attempt + 1}: {response.status_code} {response.text}")
        except Exception as e:
            logger.error(f"SMS send exception on attempt {attempt + 1}: {e}")

        # Only sleep if we have retries left
        if attempt < max_retries - 1:
            time_module.sleep(retry_delay)

    logger.error(f"Failed to send SMS to {contact_id} after {max_retries} attempts")
    return False