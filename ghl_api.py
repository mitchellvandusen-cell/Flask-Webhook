# ghl_api.py - Lead Connector OAuth & API Helpers (Flawless 2026)
import requests
import logging
import os
import time
from datetime import datetime, timedelta
from db import get_subscriber_info_hybrid, update_subscriber_token

logger = logging.getLogger(__name__)

GHL_TOKEN_URL = "https://services.leadconnectorhq.com/oauth/token"
GHL_HEADERS = {"Version": "2021-04-15", "Content-Type": "application/json"}

def get_valid_token(location_id: str) -> str | None:
    """
    Returns a valid Bearer access token or None on failure.
    Refreshes if expired (5-min buffer). Falls back to persistent token if no refresh_token.
    FIXED: Uses correct OAuth credentials based on app type (private vs marketplace).
    """
    if location_id in {'DEMO', 'DEMO_LOC', 'TEST_LOCATION_456'}:
        logger.debug(f"Internal Mode: Skipping auth for {location_id}")
        return 'DEMO'

    sub = get_subscriber_info_hybrid(location_id)
    if not sub:
        logger.error(f"No subscriber config for {location_id}")
        return None

    access_token = sub.get('access_token') or sub.get('crm_api_key')
    refresh_token = sub.get('refresh_token')
    expires_at = sub.get('token_expires_at')
    oauth_app_type = sub.get('oauth_app_type', 'marketplace')  # Default to marketplace for legacy

    # Persistent/private token (no refresh_token)
    if not refresh_token:
        if access_token:
            logger.debug(f"Using persistent token for {location_id}")
            return access_token
        logger.error(f"No access_token or refresh_token for {location_id}")
        return None

    # Convert expires_at to datetime if it's a string
    if expires_at:
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            except Exception as e:
                logger.warning(f"Could not parse expires_at: {expires_at} | {e}")
                expires_at = None

    # Check expiry with buffer (5-min safety margin)
    # Handle both timezone-aware and naive datetimes from the DB
    if expires_at:
        now = datetime.utcnow()
        # Strip tzinfo for comparison if expires_at is offset-aware
        if hasattr(expires_at, 'tzinfo') and expires_at.tzinfo is not None:
            now = datetime.now(expires_at.tzinfo)
        if expires_at > now + timedelta(minutes=5):
            return access_token

    # DUAL-APP SUPPORT: Build credential sets to try.
    # Primary: use the stored oauth_app_type. Fallback: try the other app's creds
    # if the primary fails with an auth error (handles mismatched app type in DB).
    marketplace_creds = (os.getenv("GHL_CLIENT_ID"), os.getenv("GHL_CLIENT_SECRET"))
    private_creds = (os.getenv("PRIVATE_APP_CLIENT_ID"), os.getenv("PRIVATE_APP_SECRET_ID"))

    cred_sets = []
    if oauth_app_type == 'private':
        if private_creds[0] and private_creds[1]:
            cred_sets.append({"id": private_creds[0], "secret": private_creds[1],
                              "label": "PRIVATE APP", "type": "private"})
        # Fallback: try marketplace creds in case oauth_app_type was stored wrong
        if marketplace_creds[0] and marketplace_creds[1]:
            cred_sets.append({"id": marketplace_creds[0], "secret": marketplace_creds[1],
                              "label": "MARKETPLACE (fallback)", "type": "marketplace"})
    else:
        if marketplace_creds[0] and marketplace_creds[1]:
            cred_sets.append({"id": marketplace_creds[0], "secret": marketplace_creds[1],
                              "label": "MARKETPLACE", "type": "marketplace"})
        # Fallback: try private creds in case oauth_app_type was stored wrong
        if private_creds[0] and private_creds[1]:
            cred_sets.append({"id": private_creds[0], "secret": private_creds[1],
                              "label": "PRIVATE APP (fallback)", "type": "private"})

    if not cred_sets:
        logger.error(f"No OAuth credentials configured for app_type={oauth_app_type}")
        return None

    for cred_idx, cred in enumerate(cred_sets):
        logger.info(f"🔄 Refreshing token for {location_id} using {cred['label']} credentials")

        payload = {
            "client_id": cred["id"],
            "client_secret": cred["secret"],
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "user_type": "Location"
        }

        try:
            last_err = None
            for attempt in range(2):
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

                    # If we succeeded with a fallback credential set, fix the stored app type
                    fix_type = cred["type"] if cred["type"] != oauth_app_type else None
                    if fix_type:
                        logger.warning(f"🔧 Auto-correcting oauth_app_type for {location_id}: "
                                      f"{oauth_app_type} → {fix_type}")
                    update_subscriber_token(location_id, new_access, new_refresh,
                                           expires_in, oauth_app_type=fix_type)
                    logger.info(f"✅ Token refreshed successfully for {location_id} "
                               f"using {cred['label']}")
                    return new_access

                except requests.HTTPError as e:
                    status = e.response.status_code if e.response else 0
                    if status in (400, 401, 403):
                        # Auth error — wrong credentials for this refresh token.
                        # If we have a fallback credential set, try it instead.
                        err_text = e.response.text[:300] if e.response else 'no body'
                        if cred_idx < len(cred_sets) - 1:
                            logger.warning(f"Token refresh auth error {status} with {cred['label']} "
                                          f"— will try fallback credentials. Detail: {err_text}")
                            break  # Break retry loop, continue to next cred set
                        else:
                            logger.error(f"Token refresh auth error {status} with {cred['label']} "
                                        f"(no more fallbacks): {err_text}")
                            return None
                    # 5xx or other — retry
                    last_err = f"HTTP {status}"
                    logger.warning(f"Token refresh attempt {attempt+1}/2 failed: {status}")
                except (requests.Timeout, requests.ConnectionError) as e:
                    last_err = str(e)
                    logger.warning(f"Token refresh attempt {attempt+1}/2 network error: {e}")

                if attempt == 0:
                    time.sleep(2)  # Brief backoff before retry
            else:
                # Retry loop exhausted without break (no auth error, just network/5xx)
                logger.error(f"Token refresh failed after 2 attempts for {location_id} "
                            f"using {cred['label']}: {last_err}")
                return None

        except Exception as e:
            logger.error(f"Token refresh failed for {location_id}: {e}", exc_info=True)
            return None

    logger.error(f"Token refresh failed for {location_id}: all credential sets exhausted")
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
    if access_token == 'DEMO':
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

        # GHL returns either:
        #   {"messages": [...]}                               (older format — flat list)
        #   {"messages": {"messages": [...], "lastMessageId": ..., "nextPage": ...}}  (newer format — nested)
        # Iterating a dict yields its keys (strings), not message dicts, hence the
        # "Skipping invalid message item (not dict): lastMessageId" warnings.
        messages_payload = msg_res.json().get("messages", [])
        if isinstance(messages_payload, dict):
            raw_messages = messages_payload.get("messages", [])
        elif isinstance(messages_payload, list):
            raw_messages = messages_payload
        else:
            raw_messages = []
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


def fetch_contact_data_from_ghl(contact_id: str, location_id: str, access_token: str = None) -> dict:
    """
    Fetch complete contact data from GHL API for a specific contact_id.
    This ensures we have the correct name, phone, and other details for the contact.

    Returns dict with contact data or empty dict on failure.

    Fields returned: firstName, lastName, email, phone, address1, city, state, postalCode, etc.
    """
    if not contact_id or not location_id:
        logger.error("fetch_contact_data_from_ghl: Missing contact_id or location_id")
        return {}

    if not access_token:
        access_token = get_valid_token(location_id)
        if not access_token:
            logger.error(f"No valid token for contact fetch {location_id}/{contact_id}")
            return {}

    if access_token == 'DEMO':
        logger.info(f"DEMO mode: Skipping contact fetch for {contact_id}")
        return {}

    headers = {**GHL_HEADERS, "Authorization": f"Bearer {access_token}"}

    try:
        # Fetch contact details from GHL API
        url = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        contact_data = response.json().get("contact", {})

        if contact_data:
            logger.info(f"✅ FETCHED CONTACT DATA FROM GHL | contact_id={contact_id} | firstName={contact_data.get('firstName')} | phone={contact_data.get('phone')}")
            return contact_data
        else:
            logger.warning(f"⚠️ Contact fetch returned empty data for {contact_id}")
            return {}

    except requests.HTTPError as e:
        logger.error(f"❌ GHL contact fetch HTTP error {e.response.status_code} for {contact_id}: {e.response.text}")
        return {}
    except Exception as e:
        logger.error(f"❌ GHL contact fetch failed for {contact_id}: {e}", exc_info=True)
        return {}