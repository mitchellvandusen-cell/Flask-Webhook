import logging
import os
import requests
import time as time_module
from datetime import datetime, date, time, timedelta, timezone
import psycopg2
from db import get_db_connection

logger = logging.getLogger(__name__)

def send_sms_via_ghl(contact_id: str, message: str, api_key: str, location_id: str):
    """
    Sends an SMS via GoHighLevel API.
    Includes a safety check against the database to prevent duplicate messages
    from being sent within a 5-minute window.
    """
    if not contact_id or contact_id == "unknown":
        logger.warning("Invalid contact_id, cannot send SMS")
        return False
    
    if not api_key or not location_id:
        logger.warning(f"GHL_API_KEY or GHL_LOCATION_ID missing for location {location_id}, cannot send SMS")
        return False

    # === DUPLICATE SAFETY CHECK ===
    # We check 'contact_messages' (not nlp_memory) to align with db.py
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            # Check if we sent this exact message to this contact in the last 5 minutes
            cur.execute("""
                SELECT 1 FROM contact_messages
                WHERE contact_id = %s
                AND message_type = 'assistant'
                AND created_at > CURRENT_TIMESTAMP - INTERVAL '5 minutes'
                AND message_text = %s
                LIMIT 1
            """, (contact_id, message.strip()))
            
            if cur.fetchone():
                logger.info(f"Duplicate send prevented for {contact_id} — exact message sent recently")
                cur.close()
                conn.close()
                return True  # Return True so the bot thinks it succeeded and moves on
            
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning(f"Duplicate check failed: {e}")
            if conn: conn.close()

    # === SENDING LOGIC ===
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
            # Short timeout (15s) to prevent the worker from hanging indefinitely
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            
            if response.status_code in [200, 201]:
                logger.info(f"SMS sent successfully to {contact_id} on attempt {attempt + 1}")
                return True
            else:
                logger.error(f"GHL SMS failed on attempt {attempt + 1}: {response.status_code} {response.text}")
        
        except Exception as e:
            logger.error(f"SMS send exception on attempt {attempt + 1}: {e}")

        # Wait before retrying (unless it's the last attempt)
        if attempt < max_retries - 1:
            time_module.sleep(retry_delay)

    logger.error(f"Failed to send SMS to {contact_id} after {max_retries} attempts")
    return False