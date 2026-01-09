# memory.py - Complete Version with Redundancy (2026)

import os
import logging
from typing import List, Dict
from openai import OpenAI
from db import get_db_connection
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)
client = OpenAI(
    base_url="https://api.x.ai/v1",
    api_key=os.getenv("XAI_API_KEY")
)

# ===================================
# MESSAGE STORAGE & RETRIEVAL
# ===================================

def save_message(contact_id: str, message_text: str, message_type: str = "lead") -> bool:
    """Save a raw message to the database"""
    if not contact_id or not message_text.strip():
        return False

    conn = get_db_connection()
    if not conn:
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
        logger.error(f"Error saving message: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def get_recent_messages(contact_id: str, limit: int = 8) -> List[Dict[str, str]]:
    """Get recent messages for conversation context"""
    conn = get_db_connection()
    if not conn:
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
        logger.error(f"Error fetching recent messages: {e}")
        return []
    finally:
        conn.close()

# ===================================
# FACT STORAGE (Structured Redundancy)
# ===================================

def save_new_facts(contact_id: str, facts: List[str]):
    """Save multiple new facts extracted by Grok in one efficient query"""
    if not facts or not contact_id: 
        return
        
    conn = get_db_connection()
    if not conn: 
        return
        
    try:
        cur = conn.cursor()
        execute_values(cur, """
            INSERT INTO contact_facts (contact_id, fact_text)
            VALUES %s 
            ON CONFLICT (contact_id, fact_text) DO NOTHING
        """, [(contact_id, f.strip()) for f in facts if f.strip()])
        
        conn.commit()
    except Exception as e:
        logger.error(f"Error saving facts: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

def get_known_facts(contact_id: str) -> List[str]:
    """Return all known facts as simple bullet strings"""
    conn = get_db_connection()
    if not conn:
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
        # Handle RealDictCursor vs standard tuple return
        return [row['fact_text'] if isinstance(row, dict) else row[0] for row in rows] if rows else []
    except Exception as e:
        logger.error(f"Error fetching known facts: {e}")
        return []
    finally:
        conn.close()

# ===================================
# NARRATIVE OBSERVER (Evolving Story)
# ===================================

def get_narrative(contact_id: str) -> str:
    conn = get_db_connection()
    if not conn: return ""
    try:
        cur = conn.cursor()
        cur.execute("SELECT story_narrative FROM contact_narratives WHERE contact_id = %s", (contact_id,))
        row = cur.fetchone()
        return row['story_narrative'] if row else ""
    except: return ""
    finally: conn.close()
    
def update_narrative(contact_id: str, new_story: str):
    conn = get_db_connection()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contact_narratives (contact_id, story_narrative, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (contact_id) 
            DO UPDATE SET story_narrative = EXCLUDED.story_narrative, updated_at = CURRENT_TIMESTAMP
        """, (contact_id, new_story))
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating narrative: {e}")
    finally: conn.close()

def run_narrative_observer(contact_id: str, lead_message: str):
    """
    The 'Invisible Bot' that dissects all words, nuances, and subtext
    to update the contact's life story.
    """
    current_story = get_narrative(contact_id)
    
    # This prompt ensures we capture the "Whole Story" not just keywords
    observer_prompt = f"""
    You are a Narrative Observer. Dissect the new message from the lead to update their life story.
    
    CURRENT STORY:
    {current_story if current_story else "Brand new lead. No history yet."}
    
    NEW MESSAGE FROM LEAD:
    "{lead_message}"
    
    TASK:
    Rewrite the narrative to include nuances, grey areas, and specific details.
    - Extract entities (State Farm, 250k, Brother-in-law).
    - Capture "Hints": If they beat around the bush or mention family influence, include it.
    - Capture Meaning: Don't just list words; apply meaning (e.g., 'Brother-in-law is a gatekeeper/influencer').
    - Maintain a flowing, human-readable paragraph (max 150 words).
    - Keep it focused on the person's situation and story.
    
    OUTPUT ONLY THE UPDATED NARRATIVE PARAGRAPH.
    """

    try:
        response = client.chat.completions.create(
            model="grok-beta", # Or your preferred model
            messages=[{"role": "system", "content": observer_prompt}],
            temperature=0.3
        )
        updated_story = response.choices[0].message.content.strip()
        update_narrative(contact_id, updated_story)
        logger.info(f"Narrative updated for {contact_id}")
        return updated_story
    except Exception as e:
        logger.error(f"Observer Error: {e}")
        return current_story