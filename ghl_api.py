# ghl_api.py - GHL OAuth & API Helpers (Flawless 2026)
import requests
import logging
import os
from datetime import datetime, timedelta
from db import get_subscriber_info, update_subscriber_token

logger = logging.getLogger(__name__)

GHL_TOKEN_URL = "https://services.leadconnectorhq.com/oauth/token"
GHL_HEADERS = {"Version": "2021-04-15", "Content-Type": "application/json"}

def get_valid_token(location_id: str) -> str | None:
    """
    Returns a valid Bearer access token or None on failure.
    Refreshes if expired (5-min buffer). Falls back to persistent token if no refresh_token.
    """
    if location_id in {'DEMO', 'DEMO_ACCOUNT_SALES_ONLY', 'TEST_LOCATION_456'}:
        logger.debug(f"Demo mode: returning 'DEMO' token for {location_id}")
        return 'DEMO'

    sub = get_subscriber_info(location_id)
    if not sub:
        logger.error(f"No subscriber config for {location_id}")
        return None

    access_token = sub.get('access_token') or sub.get('crm_api_key')
    refresh_token = sub.get('refresh_token')
    expires_at = sub.get('token_expires_at')

    # Persistent/private token (no refresh_token)
    if not refresh_token:
        if access_token:
            logger.debug(f"Using persistent token for {location_id}")
            return access_token
        logger.error(f"No access_token or refresh_token for {location_id}")
        return None

    # Check expiry with buffer
    if expires_at and expires_at > datetime.now() + timedelta(minutes=5):
        return access_token

    # Refresh
    logger.info(f"🔄 Refreshing token for {location_id}")
    payload = {
        "client_id": os.getenv("GHL_CLIENT_ID"),
        "client_secret": os.getenv("GHL_CLIENT_SECRET"),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "user_type": "Location"
    }

    try:
        resp = requests.post(GHL_TOKEN_URL, data=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        new_access = data.get('access_token')
        new_refresh = data.get('refresh_token')
        expires_in = data.get('expires_in', 86400)  # default 24h

        if not new_access:
            logger.error(f"Refresh response missing access_token: {resp.text}")
            return None

        update_subscriber_token(location_id, new_access, new_refresh, expires_in)
        logger.info(f"Token refreshed for {location_id}")
        return new_access

    except requests.HTTPError as e:
        logger.error(f"Token refresh HTTP error {e.response.status_code}: {e.response.text}")
        # If 400/401, refresh token likely invalid — force re-auth next time
        return None
    except Exception as e:
        logger.error(f"Token refresh failed: {e}", exc_info=True)
        return None

def fetch_targeted_ghl_history(contact_id: str, location_id: str, access_token: str = None, limit: int = 20) -> list:
    """
    Fetches messages for the specific contact's conversation.
    Returns list of {'role': str, 'text': str, 'timestamp': str} or empty on failure.
    Handles malformed API responses gracefully (e.g., strings instead of dicts).
    """
    if not access_token:
        access_token = get_valid_token(location_id)
        if not access_token:
            logger.error(f"No valid token for history fetch {location_id}/{contact_id}")
            return []

    headers = {**GHL_HEADERS, "Authorization": f"Bearer {access_token}"}

    try:
        # Step 1: Find conversation ID
        search_url = f"https://services.leadconnectorhq.com/conversations/search?locationId={location_id}&contactId={contact_id}"
        search_res = requests.get(search_url, headers=headers, timeout=10)
        search_res.raise_for_status()
        convos = search_res.json().get("conversations", [])

        if not convos:
            logger.warning(f"No conversation found for {contact_id} in {location_id}")
            return []

        convo_id = convos[0]["id"]

        # Step 2: Fetch messages
        msg_url = f"https://services.leadconnectorhq.com/conversations/{convo_id}/messages?limit={limit}"
        msg_res = requests.get(msg_url, headers=headers, timeout=10)
        msg_res.raise_for_status()

        raw_messages = msg_res.json().get("messages", [])
        formatted_history = []

        for m in raw_messages:
            # Safety: skip if not a dict
            if not isinstance(m, dict):
                logger.warning(f"Skipping invalid message item (not dict): {m}")
                continue

            # Safe key access
            direction = m.get("direction", "inbound")
            message_text = m.get("body", m.get("text", "[No text]"))
            timestamp = m.get("dateAdded", m.get("created_at", "Unknown"))

            role = "assistant" if direction == "outbound" else "lead"
            formatted_history.append({
                "role": role,
                "text": str(message_text).strip(),
                "timestamp": timestamp
            })

        logger.info(f"Fetched {len(formatted_history)} valid messages for {contact_id}")
        return formatted_history[::-1]  # oldest first

    except requests.RequestException as e:
        logger.error(f"GHL history fetch failed {location_id}/{contact_id}: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected history error {location_id}/{contact_id}: {e}", exc_info=True)
        return []