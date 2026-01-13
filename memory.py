# memory.py - Complete Version with Redundancy (2026)
# Handles message storage, fact redundancy, and evolving narrative observer

import os
import logging
from typing import List, Dict, Optional
from openai import OpenAI
from db import get_db_connection
from psycopg2.extras import execute_values
from datetime import datetime

logger = logging.getLogger(__name__)

XAI_API_KEY = os.getenv("XAI_API_KEY")

client = None
if XAI_API_KEY:
    client = OpenAI(
        api_key=XAI_API_KEY,
        base_url="https://api.x.ai/v1",
        # No proxies — not needed and breaks newer versions
        # If you ever need proxies, use: http_client=httpx.Client(proxies=...)
    )

# ===================================
# MESSAGE STORAGE & RETRIEVAL
# ===================================

def save_message(contact_id: str, message_text: str, message_type: str = "lead") -> bool:
    """
    Save a single message to the database with deduplication.
    Returns True on success, False on failure or invalid input.
    """
    if not contact_id or not message_text or not message_text.strip():
        logger.warning(f"Invalid save_message call: contact_id={contact_id}, text_length={len(message_text or '')}")
        return False

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in save_message")
        return False

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contact_messages (contact_id, message_type, message_text, created_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT DO NOTHING
        """, (contact_id, message_type, message_text.strip()))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"save_message failed for {contact_id}: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

def get_recent_messages(contact_id: str, limit: int = 8) -> List[Dict[str, str]]:
    """
    Fetch the most recent messages for context (lead + assistant).
    Returns list of {'role': 'lead'/'assistant', 'text': str}, newest last.
    """
    if not contact_id:
        return []

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in get_recent_messages")
        return []

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT message_type, message_text
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (contact_id, limit * 2))

        rows = cur.fetchall()
        messages = []
        for msg_type, text in reversed(rows):
            role = "lead" if msg_type == "lead" else "assistant"
            messages.append({"role": role, "text": text.strip()})

        return messages[-limit:]
    except Exception as e:
        logger.error(f"get_recent_messages failed for {contact_id}: {e}")
        return []
    finally:
        if conn:
            cur.close()
            conn.close()

# ===================================
# FACT STORAGE (Structured Redundancy)
# ===================================

def save_new_facts(contact_id: str, facts: List[str]) -> int:
    """
    Save multiple new facts in bulk with deduplication.
    Returns number of facts actually inserted.
    """
    if not contact_id or not facts:
        return 0

    clean_facts = [f.strip() for f in facts if f and f.strip()]
    if not clean_facts:
        return 0

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in save_new_facts")
        return 0

    inserted = 0
    try:
        cur = conn.cursor()
        execute_values(cur, """
            INSERT INTO contact_facts (contact_id, fact_text)
            VALUES %s 
            ON CONFLICT (contact_id, fact_text) DO NOTHING
        """, [(contact_id, f) for f in clean_facts])
        
        inserted = cur.rowcount
        conn.commit()
        if inserted > 0:
            logger.info(f"Saved {inserted} new facts for {contact_id}")
        return inserted
    except Exception as e:
        logger.error(f"save_new_facts failed for {contact_id}: {e}")
        conn.rollback()
        return 0
    finally:
        if conn:
            cur.close()
            conn.close()

def get_known_facts(contact_id: str) -> List[str]:
    """Return all known facts as a clean list of strings."""
    if not contact_id:
        return []

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in get_known_facts")
        return []

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT fact_text 
            FROM contact_facts 
            WHERE contact_id = %s 
            ORDER BY created_at
        """, (contact_id,))
        rows = cur.fetchall()
        return [row[0] if isinstance(row, tuple) else row['fact_text'] for row in rows]
    except Exception as e:
        logger.error(f"get_known_facts failed for {contact_id}: {e}")
        return []
    finally:
        if conn:
            cur.close()
            conn.close()

# ===================================
# NARRATIVE OBSERVER (Evolving Story)
# ===================================

def get_narrative(contact_id: str) -> str:
    """Fetch the current narrative story for a contact."""
    if not contact_id:
        return ""

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in get_narrative")
        return ""

    try:
        cur = conn.cursor()
        cur.execute("SELECT story_narrative FROM contact_narratives WHERE contact_id = %s", (contact_id,))
        row = cur.fetchone()
        return row[0] if row and isinstance(row, tuple) else (row['story_narrative'] if row else "")
    except Exception as e:
        logger.error(f"get_narrative failed for {contact_id}: {e}")
        return ""
    finally:
        if conn:
            cur.close()
            conn.close()

def update_narrative(contact_id: str, new_story: str) -> bool:
    """Update or insert the narrative story with timestamp."""
    if not contact_id or not new_story or not new_story.strip():
        logger.warning(f"Invalid update_narrative: contact={contact_id}, story_length={len(new_story or '')}")
        return False

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in update_narrative")
        return False

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contact_narratives (contact_id, story_narrative, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (contact_id) 
            DO UPDATE SET story_narrative = EXCLUDED.story_narrative, updated_at = CURRENT_TIMESTAMP
        """, (contact_id, new_story.strip()))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"update_narrative failed for {contact_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

def run_narrative_observer(contact_id: str, lead_message: str) -> str:
    """
    The 'Invisible Bot' that evolves the contact's life story.
    Only runs Grok if the message has meaningful content.
    Returns updated narrative (or current if failed/skipped).
    """
    if not contact_id or not lead_message or not lead_message.strip():
        logger.warning(f"Skipping observer: invalid input contact={contact_id}, msg_length={len(lead_message or '')}")
        return get_narrative(contact_id) or ""

    current_story = get_narrative(contact_id) or "Brand new lead. No history yet."

    # Skip trivial messages to save API cost & prevent narrative bloat
    if len(lead_message.strip()) < 5 or lead_message.strip().lower() in {"ok", "yes", ".", "k", "cool", "thanks"}:
        logger.debug(f"Observer skipped (trivial message): {contact_id}")
        return current_story

    observer_prompt = f"""
You are a Narrative Observer. Dissect ONLY the new lead message to update their life story.

CURRENT STORY (keep and evolve):
{current_story}

NEW LEAD MESSAGE:
"{lead_message}"

TASK:
- Rewrite the full narrative as a flowing, human-readable paragraph (max 150 words).
- Extract specific entities (insurance companies, coverage amounts, family members, health issues, etc.).
- Capture hints & subtext (hesitation, family influence, financial stress).
- Apply meaning — don't just list; connect dots.
- Stay focused on the person's situation, emotions, and story.
- Do NOT add assumptions or fabricate details.

OUTPUT ONLY the updated narrative paragraph.
"""

    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[{"role": "system", "content": observer_prompt}],
            temperature=0.3,  # Low for factual consistency
            max_tokens=250,
            timeout=15.0  # Prevent hanging
        )
        updated_story = response.choices[0].message.content.strip()

        if len(updated_story) < 20:
            logger.warning(f"Narrative update too short: {contact_id}")
            return current_story

        if update_narrative(contact_id, updated_story):
            logger.info(f"Narrative updated for {contact_id} ({len(updated_story)} chars)")
            return updated_story
        else:
            return current_story

    except Exception as e:
        logger.error(f"Narrative observer failed for {contact_id}: {e}", exc_info=True)
        return current_story