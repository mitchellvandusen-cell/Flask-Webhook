# backfill_history.py
import os
import requests
import logging
from datetime import datetime
from dotenv import load_dotenv

# Import your existing functions
from memory import save_message
from db import get_db_connection  # only if needed for init

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === GHL CONFIG ===
GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID")

if not GHL_API_KEY or not GHL_LOCATION_ID:
    raise ValueError("Missing GHL_API_KEY or GHL_LOCATION_ID in .env")

HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version": "2021-07-28",
    "Content-Type": "application/json"
}

def fetch_all_conversation_contact_ids():
    """Fetch contact IDs from active conversations (these are the ones with messages)"""
    url = "https://services.leadconnectorhq.com/conversations/"
    params = {
        "locationId": GHL_LOCATION_ID,
        "limit": 100,
        "type": "sms"  # Only SMS conversations
    }
    all_contact_ids = []
    page = 1

    logger.info("Fetching contact IDs from SMS conversations...")

    while True:
        response = requests.get(url, headers=HEADERS, params=params)
        if response.status_code != 200:
            logger.error(f"Failed to fetch conversations (page {page}): {response.text}")
            break

        data = response.json()
        conversations = data.get("conversations", [])
        if not conversations:
            break

        for conv in conversations:
            contact_id = conv.get("contactId")
            if contact_id:
                all_contact_ids.append(contact_id)

        logger.info(f"Page {page}: {len(conversations)} conversations (total contacts: {len(set(all_contact_ids))})")

        # Pagination
        meta = data.get("meta", {})
        if not meta.get("hasMore"):
            break
        params["startAfterId"] = meta.get("lastId")

        page += 1

    unique_ids = list(set(all_contact_ids))
    logger.info(f"Found {len(unique_ids)} unique contacts with SMS conversations")
    return unique_ids


# === FETCH MESSAGES FOR ONE CONTACT ===
def fetch_ghl_messages(contact_id: str):
    """Get all messages for a single contact"""
    url = "https://services.leadconnectorhq.com/conversations/messages/"
    params = {
        "locationId": GHL_LOCATION_ID,
        "contactId": contact_id,
        "limit": 100
    }
    all_messages = []

    while True:
        response = requests.get(url, headers=HEADERS, params=params)
        if response.status_code != 200:
            logger.warning(f"Failed messages for {contact_id}: {response.text}")
            break
        
        data = response.json()
        messages = data.get("messages", [])
        all_messages.extend(messages)
        
        if len(messages) < params["limit"]:
            break
        params["startAfter"] = messages[-1]["id"]  # paginate messages too

    return all_messages


# === BACKFILL ONE CONTACT ===
def backfill_contact(contact_id: str, dry_run: bool = True):
    messages = fetch_ghl_messages(contact_id)
    if not messages:
        logger.info(f"No messages found for {contact_id}")
        return 0

    # Sort by dateAdded ascending (oldest first)
    messages.sort(key=lambda m: m.get("dateAdded", ""))

    saved = 0
    for msg in messages:
        text = msg.get("body", "").strip()
        if not text:
            continue

        direction = msg.get("direction", "").lower()
        msg_type = "lead" if direction == "inbound" else "agent"

        if dry_run:
            logger.info(f"[DRY RUN] Would save: {msg_type} | {text[:60]}...")
        else:
            save_message(contact_id, text, msg_type)
            saved += 1

    if dry_run:
        logger.info(f"[DRY RUN] Would have saved {len(messages)} messages for {contact_id}")
    else:
        logger.info(f"Saved {saved} messages for {contact_id}")

    return saved


# === MAIN BACKFILL LOOP ===
def backfill_all_contacts(contact_ids: list[str], dry_run: bool = True):
    total_saved = 0
    for i, cid in enumerate(contact_ids, 1):
        logger.info(f"Processing {i}/{len(contact_ids)}: {cid}")
        saved = backfill_contact(cid, dry_run=dry_run)
        total_saved += saved
    
    logger.info(f"Backfill complete. Total messages processed: {total_saved}")


# === RUN SCRIPT ===
if __name__ == "__main__":
    DRY_RUN = True  # <<< CHANGE TO False WHEN READY TO ACTUALLY SAVE >>>

    if DRY_RUN:
        logger.warning("=== DRY RUN MODE === No messages will be saved to DB")
    else:
        logger.warning("=== LIVE MODE === Messages WILL be saved to your database!")

    contact_ids = fetch_all_conversation_contact_ids()

    # Optional: limit to first N for testing
    # contact_ids = contact_ids[:10]

    backfill_all_contacts(contact_ids, dry_run=DRY_RUN)

    if DRY_RUN:
        logger.info("Dry run complete. Review logs above. When ready, set DRY_RUN = False and run again.")