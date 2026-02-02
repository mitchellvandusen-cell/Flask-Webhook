# memory.py - Complete Version with Redundancy (2026)
# Handles message storage, fact redundancy, and evolving narrative observer

import os
import logging
from typing import List, Dict, Optional
from openai import OpenAI
from db import get_db_connection
from psycopg2.extras import execute_values
from datetime import datetime
import httpx

logger = logging.getLogger(__name__)

XAI_API_KEY = os.getenv("XAI_API_KEY")

client = None
if XAI_API_KEY:
    client = OpenAI(
        api_key=XAI_API_KEY,
        base_url="https://api.x.ai/v1"
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
        
        # CRASH FIX: Added ON CONFLICT DO NOTHING
        # This allows the lead to repeat themselves without crashing the worker
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

def get_recent_messages(contact_id: str, limit: int = None) -> List[Dict[str, str]]:
    """
    Fetch messages for context (lead + assistant).
    If limit=None, fetches ALL messages (for narrative observer).
    If limit=int, fetches recent N exchanges.
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
        if limit is None:
            # Fetch ALL messages for narrative observer (unlimited memory)
            cur.execute("""
                SELECT message_type, message_text
                FROM contact_messages
                WHERE contact_id = %s
                ORDER BY created_at DESC
            """, (contact_id,))
        else:
            # Fetch limited messages for logic flow
            cur.execute("""
                SELECT message_type, message_text
                FROM contact_messages
                WHERE contact_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (contact_id, limit * 2))

        rows = cur.fetchall()
        messages = []
        for row in reversed(rows):
            role = "lead" if row['message_type'] == "lead" else "assistant"
            messages.append({"role": role, "text": row['message_text'].strip()})

        # If limit specified, return last N messages; otherwise return all
        return messages[-limit:] if limit else messages
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
        logger.debug(f"🔍 QUERY NARRATIVE | contact_id={contact_id}")
        cur.execute("SELECT story_narrative FROM contact_narratives WHERE contact_id = %s", (contact_id,))
        row = cur.fetchone()
        result = row[0] if row and isinstance(row, tuple) else (row['story_narrative'] if row else "")

        logger.debug(f"🔍 NARRATIVE RETRIEVED | contact_id={contact_id} | has_narrative={bool(result)} | preview={result[:80] if result else 'NONE'}")
        return result
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

def run_narrative_observer(contact_id: str, lead_message: str, recent_messages: List[Dict[str, str]] = None) -> dict:
    """
    LEFT BRAIN — The Conversation Recap.

    Reads the full conversation and produces:
    1. A narrative recap of what has happened in this conversation — what was said,
       what was asked, what was answered, what was agreed to, where things stand NOW.
       This is session notes, not a person description.
    2. Discrete facts extracted from what the lead actually said or confirmed.
       These feed into the individual profile (right brain) separately.

    The narrative prevents looping. If Grok reads "Bot already asked about coverage
    and lead said he has something through work", Grok won't ask again.

    Returns dict with:
        - "narrative": conversation recap string
        - "new_facts": list of newly extracted fact strings
    """
    result = {"narrative": "", "new_facts": []}

    if not contact_id:
        logger.warning(f"Skipping observer: no contact_id")
        result["narrative"] = get_narrative(contact_id) or ""
        return result

    current_story = get_narrative(contact_id) or "First contact. No conversation yet."
    existing_facts = get_known_facts(contact_id)

    # Build conversation context (UNLIMITED - use EVERYTHING from database)
    conversation_context = ""
    if recent_messages and len(recent_messages) > 0:
        for msg in recent_messages:
            role_label = "Bot" if msg['role'] == 'assistant' else "Lead"
            conversation_context += f"{role_label}: {msg['text']}\n"

    if lead_message and lead_message.strip():
        conversation_context += f"Lead: {lead_message}\n"

    if not conversation_context.strip():
        result["narrative"] = current_story
        return result

    existing_str = "\n".join(f"- {f}" for f in existing_facts) if existing_facts else "None yet."

    observer_prompt = f"""You are a conversation note-taker. Your job is to read the full conversation and write a recap of what has happened so far, and pull out any new facts the lead revealed.

PREVIOUS RECAP:
{current_story}

ALREADY KNOWN FACTS:
{existing_str}

FULL CONVERSATION:
{conversation_context}

Produce two sections. Follow this format exactly:

RECAP:
Write a chronological recap of this conversation. What did the bot say, what did the lead say back, what questions were asked, what was answered, what was agreed to, what objections came up, and where does the conversation stand right now. This is a play-by-play of the conversation, not a description of the person.

Understand meaning and context. If the lead said "yeah" after the bot asked "still looking?", note that the lead confirmed they're still looking. If they said "nah I'm good" after being asked about scheduling, note that they declined to book.

Include everything from the previous recap. Add what's new. Never drop old details. The goal is that someone reading this recap knows exactly what has been discussed and what hasn't, so nothing gets repeated.

FACTS:
List any NEW facts about the lead that came out of the latest messages. Things they said or confirmed about themselves, their life, their coverage, their situation. Interpret meaning — if they mention "something through my job" that's employer-provided coverage. One fact per line. Only new facts not already in ALREADY KNOWN FACTS. If no new facts, write NONE."""

    try:
        if not client:
            result["narrative"] = current_story
            return result

        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[{"role": "system", "content": observer_prompt}],
            temperature=0.3,
            max_tokens=1000,
            timeout=15.0
        )
        raw_output = response.choices[0].message.content.strip()

        # Parse the two sections
        narrative_part = raw_output
        facts_part = ""

        if "FACTS:" in raw_output:
            parts = raw_output.split("FACTS:", 1)
            narrative_part = parts[0].strip()
            facts_part = parts[1].strip()

        # Clean narrative (remove the "RECAP:" label if present)
        if narrative_part.startswith("RECAP:"):
            narrative_part = narrative_part[len("RECAP:"):].strip()

        # Update narrative if valid
        if len(narrative_part) >= 20:
            if update_narrative(contact_id, narrative_part):
                logger.info(f"Narrative updated for {contact_id} ({len(narrative_part)} chars)")
                result["narrative"] = narrative_part
            else:
                result["narrative"] = current_story
        else:
            logger.warning(f"Narrative update too short: {contact_id}")
            result["narrative"] = current_story

        # Parse and save new facts
        if facts_part and facts_part.upper() != "NONE":
            new_facts = []
            for line in facts_part.split("\n"):
                line = line.strip().lstrip("-•* 0123456789.")
                if line and len(line) > 3 and line.upper() != "NONE":
                    new_facts.append(line)

            if new_facts:
                saved = save_new_facts(contact_id, new_facts)
                if saved > 0:
                    logger.info(f"📝 Extracted {saved} new facts for {contact_id}: {new_facts}")
                result["new_facts"] = new_facts

        return result

    except Exception as e:
        logger.error(f"Narrative observer failed for {contact_id}: {e}", exc_info=True)
        result["narrative"] = current_story
        return result