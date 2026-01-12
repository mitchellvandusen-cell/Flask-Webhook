# ghl_api.py
import requests
import logging
import os
import time
from datetime import datetime, timedelta
from db import get_subscriber_info, update_subscriber_token

logger = logging.getLogger(__name__)

GHL_TOKEN_URL = "https://services.leadconnectorhq.com/oauth/token"

def get_valid_token(location_id: str) -> str:
    """
    Retrieves a valid access token.
    If the current one is expired (and we have a refresh token), it refreshes it first.
    If no refresh token exists, it assumes it's a 'Private Integration' (Persistent) token.
    """
    if location_id in ['DEMO', 'DEMO_ACCOUNT_SALES_ONLY', 'TEST_LOCATION_456']:
        return 'DEMO'

    sub = get_subscriber_info(location_id)
    if not sub:
        logger.error(f"No subscriber found for {location_id}")
        return None

    access_token = sub.get('access_token') or sub.get('crm_api_key')
    refresh_token = sub.get('refresh_token')
    expires_at = sub.get('token_expires_at')

    # 1. If no refresh token, treat as Persistent Token (Private Integration)
    if not refresh_token:
        return access_token

    # 2. Check Expiry (Buffer of 5 minutes)
    if expires_at and expires_at > datetime.now() + timedelta(minutes=5):
        return access_token

    # 3. Refresh the Token
    logger.info(f"🔄 Refreshing OAuth token for {location_id}...")
    
    payload = {
        "client_id": os.getenv("GHL_CLIENT_ID"),
        "client_secret": os.getenv("GHL_CLIENT_SECRET"),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "user_type": "Location" # or Company, but usually Location for marketplace
    }
    
    try:
        resp = requests.post(GHL_TOKEN_URL, data=payload, timeout=10)
        
        if resp.status_code == 200:
            data = resp.json()
            new_access = data['access_token']
            new_refresh = data['refresh_token']
            expires_in = data['expires_in'] # seconds
            
            update_subscriber_token(location_id, new_access, new_refresh, expires_in)
            logger.info(f"✅ Token refreshed for {location_id}")
            return new_access
        else:
            logger.error(f"❌ Token refresh failed: {resp.text}")
            # Fallback: return old token and hope grace period works, or fail
            return None
    except Exception as e:
        logger.error(f"Token refresh exception: {e}")
        return None

def fetch_targeted_ghl_history(contact_id: str, location_id: str, access_token: str = None, limit: int = 20):
    """
    Finds the specific conversation ID for a contact and fetches ONLY their messages.
    """
    # If token not passed, try to get it
    if not access_token:
        access_token = get_valid_token(location_id)
        if not access_token: return []

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }
    
    # STEP 1: Targeted Search for the conversation ID
    search_url = f"https://services.leadconnectorhq.com/conversations/search?locationId={location_id}&contactId={contact_id}"
    
    try:
        search_res = requests.get(search_url, headers=headers, timeout=10)
        search_data = search_res.json()
        conversations = search_data.get("conversations", [])
        
        if not conversations:
            logger.warning(f"⚠️ No conversation found for contact {contact_id}")
            return []
            
        convo_id = conversations[0].get("id")
        
        # STEP 2: Fetch only the messages for THIS conversation
        msg_url = f"https://services.leadconnectorhq.com/conversations/{convo_id}/messages?limit={limit}"
        msg_res = requests.get(msg_url, headers=headers, timeout=10)
        msg_data = msg_res.json()
        
        history = []
        for m in msg_data.get("messages", []):
            role = "assistant" if m.get("direction") == "outbound" else "lead"
            history.append({
                "role": role,
                "text": m.get("body", "") or "[Non-text message]",
                "timestamp": m.get("dateAdded") 
            })
            
        return history[::-1]
        
    except Exception as e:
        logger.error(f"❌ Targeted Fetch Error: {e}")
        return []