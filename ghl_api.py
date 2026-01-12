# ghl_api.py
import requests
import logging

logger = logging.getLogger(__name__)

def fetch_targeted_ghl_history(contact_id: str, location_id: str, api_key: str, limit: int = 20):
    """
    Finds the specific conversation ID for a contact and fetches ONLY their messages.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
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
            # Map GHL direction to AI roles
            role = "assistant" if m.get("direction") == "outbound" else "lead"
            history.append({
                "role": role,
                "text": m.get("body", "") or "[Non-text message]",
                "timestamp": m.get("dateAdded") 
            })
            
        # Reverse because GHL returns newest first, but AI needs oldest first
        return history[::-1]
        
    except Exception as e:
        logger.error(f"❌ Targeted Fetch Error: {e}")
        return []