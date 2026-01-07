# memory.py - Final Minimal Version

import os
import logging
from typing import List, Dict
from psycopg2.extras import execute_values

from db import get_db_connection

logger = logging.getLogger(__name__)

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
    """
    Get recent messages for conversation context
    Returns: [{'role': 'lead' or 'assistant', 'text': '...'}]
    """
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
        """, (contact_id, limit * 2))  # Get more, return last N

        rows = cur.fetchall()
        messages = []
        for msg_type, text in reversed(rows):  # Reverse to chronological
            role = "lead" if msg_type == "lead" else "assistant"
            messages.append({"role": role, "text": text.strip()})

        return messages[-limit:]  # Final limit
    except Exception as e:
        logger.error(f"Error fetching recent messages: {e}")
        return []
    finally:
        conn.close()


# ===================================
# FACT STORAGE & RETRIEVAL (Core Memory)
# ===================================

# You need this table (run once):
# CREATE TABLE contact_facts (
#     id SERIAL PRIMARY KEY,
#     contact_id TEXT NOT NULL,
#     fact_text TEXT NOT NULL,
#     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#     UNIQUE(contact_id, fact_text)  -- prevent duplicates
# );

def save_new_facts(contact_id: str, facts: List[str]):
    """Save new facts extracted by Grok"""
    if not facts or not contact_id:
        return

    cleaned_facts = [f.strip() for f in facts if f.strip()]
    if not cleaned_facts:
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
        """, [(contact_id, fact) for fact in cleaned_facts])
        conn.commit()
        logger.info(f"Saved {len(cleaned_facts)} new facts for {contact_id}")
    except Exception as e:
        logger.error(f"Error saving facts: {e}")
        conn.rollback()
    finally:
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
        return [row[0] for row in rows] if rows else []
    except Exception as e:
        logger.error(f"Error fetching known facts: {e}")
        return []
    finally:
        conn.close()