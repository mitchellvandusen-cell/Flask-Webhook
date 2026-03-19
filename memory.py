# memory.py - Complete Version with Redundancy (2026)
# Handles message storage, fact redundancy, and evolving narrative observer

import os
import re
import logging
from typing import List, Dict, Optional
from openai import OpenAI
from db import get_db_connection, return_db_connection
from psycopg2.extras import execute_values
from datetime import datetime

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

def save_message(contact_id: str, message_text: str, message_type: str = "lead", stage: str = None) -> bool:
    """
    Save a single message to the database with deduplication.
    stage: conversation stage at the time of this message (e.g. 'qualifying', 'objection_handling').
           Stamped on assistant messages so stage history can be reconstructed on next turn.
    Returns True on success, False on failure or invalid input.
    """
    if not contact_id or not message_text or not message_text.strip():
        logger.warning(f"Invalid save_message call: contact_id={contact_id}, text_length={len(message_text or '')}")
        return False

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in save_message")
        return False

    cur = None
    try:
        cur = conn.cursor()

        # CRASH FIX: Added ON CONFLICT DO NOTHING
        # This allows the lead to repeat themselves without crashing the worker
        cur.execute("""
            INSERT INTO contact_messages (contact_id, message_type, message_text, stage, created_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT DO NOTHING
        """, (contact_id, message_type, message_text.strip(), stage))

        conn.commit()
        return True
    except Exception as e:
        logger.error(f"save_message failed for {contact_id}: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if cur:
            cur.close()
        return_db_connection(conn)

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

    cur = None
    try:
        cur = conn.cursor()
        if limit is None:
            # Fetch ALL messages for narrative observer (unlimited memory)
            cur.execute("""
                SELECT message_type, message_text, stage
                FROM contact_messages
                WHERE contact_id = %s
                ORDER BY created_at DESC
            """, (contact_id,))
        else:
            # Fetch limited messages for logic flow
            cur.execute("""
                SELECT message_type, message_text, stage
                FROM contact_messages
                WHERE contact_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (contact_id, limit * 2))

        rows = cur.fetchall()
        messages = []
        for row in reversed(rows):
            role = "lead" if row['message_type'] == "lead" else "assistant"
            entry = {"role": role, "text": row['message_text'].strip()}
            if row.get('stage'):
                entry["stage"] = row['stage']
            messages.append(entry)

        # If limit specified, return last N messages; otherwise return all
        return messages[-limit:] if limit else messages
    except Exception as e:
        logger.error(f"get_recent_messages failed for {contact_id}: {e}")
        return []
    finally:
        if cur:
            cur.close()
        return_db_connection(conn)

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
    cur = None
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
        if cur:
            cur.close()
        return_db_connection(conn)

def get_known_facts(contact_id: str) -> List[str]:
    """Return all known facts as a clean list of strings."""
    if not contact_id:
        return []

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in get_known_facts")
        return []

    cur = None
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
        if cur:
            cur.close()
        return_db_connection(conn)


def get_known_facts_with_age(contact_id: str) -> List[Dict[str, any]]:
    """
    Return all known facts with their age in days.
    Used by individual_profile.py for temporal relevance weighting.
    Returns list of {'text': str, 'days_ago': int}.
    """
    if not contact_id:
        return []

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in get_known_facts_with_age")
        return []

    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT fact_text,
                   EXTRACT(DAY FROM (CURRENT_TIMESTAMP - created_at))::int AS days_ago
            FROM contact_facts
            WHERE contact_id = %s
            ORDER BY created_at
        """, (contact_id,))
        rows = cur.fetchall()
        result = []
        for row in rows:
            if isinstance(row, tuple):
                result.append({"text": row[0], "days_ago": row[1] or 0})
            else:
                result.append({"text": row['fact_text'], "days_ago": row.get('days_ago', 0)})
        return result
    except Exception as e:
        logger.error(f"get_known_facts_with_age failed for {contact_id}: {e}")
        return []
    finally:
        if cur:
            cur.close()
        return_db_connection(conn)

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

    cur = None
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
        if cur:
            cur.close()
        return_db_connection(conn)

def update_narrative(contact_id: str, new_story: str) -> bool:
    """Update or insert the narrative story with timestamp."""
    if not contact_id or not new_story or not new_story.strip():
        logger.warning(f"Invalid update_narrative: contact={contact_id}, story_length={len(new_story or '')}")
        return False

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in update_narrative")
        return False

    cur = None
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
        if cur:
            cur.close()
        return_db_connection(conn)

def _clean_llm_output(raw: str) -> str:
    """Strip thinking tags and other LLM reasoning artifacts from output."""
    # Strip <thinking>...</thinking> blocks (reasoning model artifacts)
    cleaned = re.sub(r'<thinking>[\s\S]*?</thinking>', '', raw)
    # Strip any remaining XML-like tags
    cleaned = re.sub(r'</?(?:thinking|reply|output|response)>', '', cleaned)
    return cleaned.strip()




def run_narrative_observer(contact_id: str, lead_message: str, recent_messages: List[Dict[str, str]] = None) -> dict:
    """
    LEFT BRAIN — The Conversation Recap.

    Reads the previous recap plus RECENT messages and produces:
    1. An updated narrative recap with three layers:
       - SITUATION: Current snapshot of where things stand (what to do next)
       - EMOTIONAL_ARC: Key emotional moments that must never be forgotten
       - OBJECTION_LOG: Every objection raised and the angle used to handle it
    2. Discrete facts extracted from what the lead said.

    The narrative prevents looping. If Grok reads "Bot already asked about coverage
    and lead said he has something through work", Grok won't ask again.
    The emotional arc preserves WHY this person is looking into coverage.
    The objection log prevents repeating the same handling angle twice.

    Returns dict with:
        - "narrative": full structured narrative string
        - "new_facts": list of newly extracted fact strings
    """
    result = {"narrative": "", "new_facts": []}

    if not contact_id:
        logger.warning(f"Skipping observer: no contact_id")
        result["narrative"] = get_narrative(contact_id) or ""
        return result

    current_story = get_narrative(contact_id) or "First contact. No conversation yet."
    existing_facts = get_known_facts(contact_id)

    # Build conversation context from all messages
    conversation_context = ""
    for msg in (recent_messages or []):
        role_label = "Bot" if msg['role'] == 'assistant' else "Lead"
        conversation_context += f"{role_label}: {msg['text']}\n"

    if lead_message and lead_message.strip():
        conversation_context += f"Lead: {lead_message}\n"

    if not conversation_context.strip():
        result["narrative"] = current_story
        return result

    existing_str = "\n".join(f"- {f}" for f in existing_facts) if existing_facts else "None yet."

    observer_prompt = f"""You are a conversation note-taker for a life insurance sales conversation. Read the previous recap and the recent messages, then write an updated structured summary and extract any new facts.

PREVIOUS RECAP:
{current_story}

ALREADY KNOWN FACTS:
{existing_str}

RECENT MESSAGES:
{conversation_context}

Output EXACTLY five sections, nothing else. No reasoning, no thinking, no commentary. Just the raw content for each section.

SITUATION:
Write 2-4 sentences about where this conversation stands RIGHT NOW. What does the lead want. What stage are we at. What should happen next. What questions have been answered and what is still unknown. Maximum 80 words.

CONVERSATIONAL_THREAD:
Capture what the lead is CURRENTLY talking about and what they are responding to. This is critical for conversational coherence. If the lead is commenting on the bot's texting behavior (e.g. "you text too much", "the one about sending too many texts"), note that they are discussing the bot's messages, NOT insurance. If the lead is answering a specific question, note which question they are answering. If the lead is making small talk, confused, or off-topic, note that. If the lead asked a question that has not been answered yet, note the unanswered question. This section tells the bot what the lead is ACTUALLY talking about so it can respond appropriately instead of forcing an insurance pivot. Maximum 40 words.

EMOTIONAL_ARC:
Preserve every emotionally significant moment from the conversation. If the lead mentioned grief, family loss, fear, health scares, financial stress, divorce, kids they are worried about, a dying parent, or ANY personal vulnerability, capture it here with the exact context. Also capture strong positive moments: excitement about coverage, relief at finding help, gratitude. If the previous recap already has emotional arc entries, carry them forward and add any new ones. One line per moment, maximum 15 words each. If no emotional moments exist, write NONE.

OBJECTION_LOG:
List every objection the lead has raised AND how the bot responded to it. CRITICAL FORMAT — each entry MUST start with a type tag in square brackets, followed by the objection and angle:
"[TYPE] Objection: [what they said] > Angle: [how bot handled it]"

Valid type tags (use EXACTLY one per entry):
[NOT_INTERESTED] — any form of no, dismissal, disengagement
[SPOUSE_PARTNER] — deferring to spouse, family, advisor
[PRICE_MONEY] — cost, affordability, budget concerns
[ALREADY_COVERED] — claims existing coverage
[BUSY_TIMING] — scheduling, bad timing
[THINK_ABOUT_IT] — stalling, delaying, "send me info"
[HEALTH_CONCERN] — health conditions, believes they cannot qualify
[TRUST_ISSUE] — distrust of insurance, bad past experience, loyalty to another agent/relative

Examples:
[PRICE_MONEY] Objection: too expensive > Angle: asked if money was real barrier or perceived
[SPOUSE_PARTNER] Objection: need to ask my wife > Angle: asked if spouse was on board would they want it
[HEALTH_CONCERN] Objection: I have diabetes > Angle: explained carriers specialize in health issues
[TRUST_ISSUE] Objection: my nephew sells insurance > Angle: positioned as second opinion not replacement

If the previous recap already has objection entries, carry them forward (preserving their type tags) and add new ones. If no objections, write NONE.

FACTS:
List any NEW facts about the lead. One fact per line. Maximum 10 words per fact. Short fragments only (e.g. "Has 2 kids", "Works at FedEx", "Wants term life"). Do not repeat facts from ALREADY KNOWN FACTS. Do NOT extract street addresses, zip codes, or specific locations as facts. General city/state is acceptable. If no new facts, write NONE."""

    try:
        if not client:
            result["narrative"] = current_story
            return result

        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[{"role": "system", "content": observer_prompt}],
            temperature=0.3,
            max_tokens=750,
            timeout=15.0
        )
        raw_output = response.choices[0].message.content.strip()

        # Strip reasoning model artifacts (<thinking> tags, etc.)
        raw_output = _clean_llm_output(raw_output)

        # Parse the four sections
        narrative_part = raw_output
        facts_part = ""

        # Extract FACTS section first (always last)
        if "FACTS:" in raw_output:
            parts = raw_output.split("FACTS:", 1)
            narrative_part = parts[0].strip()
            facts_part = parts[1].strip()

        # Clean section labels from narrative — keep the structure intact
        # The sections (SITUATION:, EMOTIONAL_ARC:, OBJECTION_LOG:) are the structure
        # Just remove the RECAP: label if present (legacy format)
        if narrative_part.startswith("RECAP:"):
            narrative_part = narrative_part[len("RECAP:"):].strip()

        # Validate: must have at least the SITUATION section
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
        if facts_part and facts_part.upper().strip() != "NONE":
            new_facts = []
            for line in facts_part.split("\n"):
                line = line.strip()
                # Remove bullet/numbering prefixes (e.g. "- ", "• ", "1. ", "2) ")
                # but NOT leading content numbers (e.g. "2 kids" must stay intact)
                line = re.sub(r'^[-•*]\s*', '', line)           # bullet chars
                line = re.sub(r'^\d+[.)]\s*', '', line)         # numbering like "1. " or "2) "
                line = line.strip()
                if line and len(line) > 3 and line.upper() != "NONE":
                    new_facts.append(line)

            if new_facts:
                saved = save_new_facts(contact_id, new_facts)
                if saved > 0:
                    logger.info(f"Extracted {saved} new facts for {contact_id}: {new_facts}")
                result["new_facts"] = new_facts

        return result

    except Exception as e:
        logger.error(f"Narrative observer failed for {contact_id}: {e}", exc_info=True)
        result["narrative"] = current_story
        return result