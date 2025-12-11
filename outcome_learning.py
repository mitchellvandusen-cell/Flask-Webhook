"""
Outcome-Based Learning System

This module implements a self-improving pattern system that learns from actual
conversation outcomes rather than pre-defined rules.

Two Pattern Banks:
1. Forward Patterns - What works when leads are engaging (moving toward appointment)
2. Recovery Patterns - What works when leads are objecting/dismissive (getting back on track)

Scoring Scale:
+0.5 = Got any reply
+1.0 = Reply over 4 words
+2.0 = Reply with information
+3.0 = Reply with direction
+4.0 = Reply with direction + need/buying motivation
-1.0 = No reply (burned)

Conversation-level bonus: +0.5 to all patterns when appointment is booked
"""

import re
import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from enum import Enum
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class PatternBank(Enum):
    FORWARD = "forward"
    RECOVERY = "recovery"


class VibeClassification(Enum):
    OBJECTION = "objection"
    DISMISSIVE = "dismissive"
    NEUTRAL = "neutral"
    INFORMATION = "information"
    DIRECTION = "direction"
    NEED = "need"
    BOOKING_READY = "booking_ready"


@contextmanager
def get_db_connection():
    """Get a database connection using DATABASE_URL."""
    conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def init_tables():
    """Create tables if they don't exist."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS response_patterns (
                    id SERIAL PRIMARY KEY,
                    bank VARCHAR(20) NOT NULL,
                    trigger_category VARCHAR(50) NOT NULL,
                    trigger_example TEXT NOT NULL,
                    response_used TEXT NOT NULL,
                    score FLOAT DEFAULT 0.0,
                    times_used INTEGER DEFAULT 0,
                    times_successful INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_used_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contact_history (
                    id SERIAL PRIMARY KEY,
                    contact_id VARCHAR(100) UNIQUE NOT NULL,
                    last_agent_message TEXT,
                    last_agent_message_at TIMESTAMP,
                    last_lead_response TEXT,
                    last_lead_response_at TIMESTAMP,
                    last_vibe VARCHAR(30),
                    was_burned BOOLEAN DEFAULT FALSE,
                    burn_count INTEGER DEFAULT 0,
                    patterns_used TEXT,
                    appointment_booked BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS outcome_tracker (
                    id SERIAL PRIMARY KEY,
                    contact_id VARCHAR(100) NOT NULL,
                    pattern_id INTEGER REFERENCES response_patterns(id),
                    agent_message TEXT NOT NULL,
                    agent_message_at TIMESTAMP DEFAULT NOW(),
                    lead_response TEXT,
                    lead_response_at TIMESTAMP,
                    outcome_score FLOAT,
                    vibe_classification VARCHAR(30),
                    scored BOOLEAN DEFAULT FALSE
                )
            """)
            
            cur.execute("SELECT COUNT(*) FROM response_patterns")
            count = cur.fetchone()[0]
            if count == 0:
                initialize_seed_patterns_sql(cur)
    
    logger.info("Outcome learning tables initialized")


NEGATIVE_WORDS = [
    "no", "not", "don't", "cant", "can't", "won't", "stop", "remove", "unsubscribe",
    "busy", "later", "maybe", "idk", "nah", "nope", "good", "fine", "okay", "ok",
    "covered", "have insurance", "all set", "not interested", "no thanks"
]

NEED_WORDS = [
    "worried", "worry", "concern", "scared", "afraid", "should", "need", "want",
    "family", "wife", "husband", "kids", "children", "daughter", "son", "baby",
    "mortgage", "house", "debt", "retire", "retirement", "future", "protect",
    "what if", "happen to me", "pass away", "die", "death"
]

DIRECTION_WORDS = [
    "how much", "what kind", "tell me", "explain", "options", "rates", "cost",
    "when", "where", "who", "why", "which", "looking for", "thinking about",
    "considering", "interested in", "want to know", "curious"
]

INFORMATION_INDICATORS = [
    r"\d+k",
    r"\$\d+",
    r"\d+ years",
    r"\d+ kids",
    r"married",
    r"single",
    r"wife|husband|spouse",
    r"work|job|employer",
    r"term|whole life|universal",
    r"state farm|allstate|geico|colonial penn|globe life"
]


def classify_vibe(message: str) -> VibeClassification:
    """
    Classify the vibe/intent of a lead's message.
    Returns classification that determines which pattern bank to use
    and how to score the previous response.
    """
    if not message:
        return VibeClassification.NEUTRAL
    
    msg_lower = message.lower().strip()
    word_count = len(message.split())
    
    has_question = "?" in message
    has_negative = any(neg in msg_lower for neg in NEGATIVE_WORDS)
    has_need = any(need in msg_lower for need in NEED_WORDS)
    has_direction = any(dir_word in msg_lower for dir_word in DIRECTION_WORDS)
    has_info = any(re.search(pattern, msg_lower) for pattern in INFORMATION_INDICATORS)
    
    if any(phrase in msg_lower for phrase in ["stop texting", "remove me", "unsubscribe", "leave me alone"]):
        return VibeClassification.DISMISSIVE
    
    if has_need and (has_direction or has_info or word_count > 6):
        return VibeClassification.NEED
    
    if has_direction or has_question:
        return VibeClassification.DIRECTION
    
    if has_info and word_count > 4:
        return VibeClassification.INFORMATION
    
    if has_negative and word_count < 5:
        return VibeClassification.OBJECTION
    
    if word_count < 4 and not has_question:
        return VibeClassification.OBJECTION
    
    if has_info:
        return VibeClassification.INFORMATION
    
    return VibeClassification.NEUTRAL


def calculate_outcome_score(lead_response: str, vibe: VibeClassification) -> float:
    """
    Calculate the outcome score for a response based on lead's reply.
    
    +0.5 = Got any reply
    +1.0 = Reply over 4 words
    +2.0 = Reply with information
    +3.0 = Reply with direction
    +4.0 = Reply with direction + need/buying motivation
    """
    if not lead_response:
        return -1.0
    
    word_count = len(lead_response.split())
    
    if vibe == VibeClassification.DISMISSIVE:
        return 0.0
    
    if vibe == VibeClassification.NEED:
        return 4.0
    
    if vibe == VibeClassification.DIRECTION:
        return 3.0
    
    if vibe == VibeClassification.INFORMATION:
        return 2.0
    
    if word_count > 4:
        return 1.0
    
    return 0.5


def get_trigger_category(message: str, vibe: VibeClassification) -> str:
    """Categorize the trigger message for pattern matching."""
    msg_lower = message.lower()
    
    if vibe in [VibeClassification.OBJECTION, VibeClassification.DISMISSIVE]:
        if any(phrase in msg_lower for phrase in ["not interested", "no thanks", "nah", "nope"]):
            return "not_interested"
        if any(phrase in msg_lower for phrase in ["busy", "bad time", "call later", "not now"]):
            return "bad_timing"
        if any(phrase in msg_lower for phrase in ["have insurance", "covered", "all set", "good on"]):
            return "has_coverage"
        if any(phrase in msg_lower for phrase in ["too expensive", "cost", "afford", "money"]):
            return "price_objection"
        if any(phrase in msg_lower for phrase in ["who is this", "who are you", "what company"]):
            return "unknown_sender"
        return "general_objection"
    
    if "work" in msg_lower or "employer" in msg_lower or "job" in msg_lower:
        return "employer_coverage"
    if any(word in msg_lower for word in ["wife", "husband", "spouse", "married"]):
        return "has_spouse"
    if any(word in msg_lower for word in ["kid", "child", "son", "daughter", "baby"]):
        return "has_kids"
    if any(word in msg_lower for word in ["health", "diabetes", "heart", "cancer", "condition"]):
        return "health_concerns"
    if any(word in msg_lower for word in ["how much", "cost", "rate", "price", "afford"]):
        return "asking_price"
    if any(word in msg_lower for word in ["when", "time", "schedule", "available", "call"]):
        return "scheduling"
    
    return "general_engagement"


def get_pattern_bank(vibe: VibeClassification) -> PatternBank:
    """Determine which pattern bank to use based on vibe."""
    if vibe in [VibeClassification.OBJECTION, VibeClassification.DISMISSIVE]:
        return PatternBank.RECOVERY
    return PatternBank.FORWARD


def find_matching_patterns(trigger_category: str, bank: PatternBank, limit: int = 3) -> List[Dict]:
    """Find the top matching patterns for a trigger category."""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM response_patterns 
                WHERE trigger_category = %s AND bank = %s
                ORDER BY score DESC
                LIMIT %s
            """, (trigger_category, bank.value, limit))
            patterns = cur.fetchall()
            
            if not patterns:
                cur.execute("""
                    SELECT * FROM response_patterns 
                    WHERE bank = %s
                    ORDER BY score DESC
                    LIMIT %s
                """, (bank.value, limit))
                patterns = cur.fetchall()
            
            return patterns


def format_patterns_for_prompt(patterns: List[Dict]) -> str:
    """Format patterns for injection into Grok's prompt."""
    if not patterns:
        return ""
    
    lines = ["=== PROVEN RESPONSES (these have worked before) ==="]
    for i, p in enumerate(patterns, 1):
        success_rate = (p['times_successful'] / p['times_used'] * 100) if p['times_used'] > 0 else 0
        trigger_preview = p['trigger_example'][:50] if len(p['trigger_example']) > 50 else p['trigger_example']
        lines.append(f"{i}. When lead said something like: \"{trigger_preview}\"")
        lines.append(f"   This worked (score {p['score']:.1f}, {success_rate:.0f}% success): \"{p['response_used']}\"")
    lines.append("=== Adapt these to fit the current situation, don't copy exactly ===\n")
    
    return "\n".join(lines)


def get_contact_context(contact_id: str) -> Optional[Dict]:
    """Get the contact's history and burn status."""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM contact_history WHERE contact_id = %s
            """, (contact_id,))
            return cur.fetchone()


def format_burn_context(contact: Optional[Dict]) -> str:
    """Format burn history for prompt injection."""
    if not contact or not contact.get('was_burned'):
        return ""
    
    lines = ["\n=== IMPORTANT: PREVIOUS ATTEMPTS FAILED ==="]
    lines.append(f"This lead has been burned {contact['burn_count']} time(s).")
    if contact.get('last_agent_message'):
        msg_preview = contact['last_agent_message'][:100]
        lines.append(f"Last message that got no reply: \"{msg_preview}...\"")
    lines.append("Try a completely different approach. Be more curious, less salesy.")
    lines.append("=== END BURN CONTEXT ===\n")
    
    return "\n".join(lines)


def record_agent_message(contact_id: str, message: str, pattern_id: Optional[int] = None) -> int:
    """Record that the agent sent a message (to track if it gets burned)."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO contact_history (contact_id, last_agent_message, last_agent_message_at, was_burned)
                VALUES (%s, %s, NOW(), TRUE)
                ON CONFLICT (contact_id) DO UPDATE SET
                    last_agent_message = EXCLUDED.last_agent_message,
                    last_agent_message_at = EXCLUDED.last_agent_message_at,
                    was_burned = TRUE,
                    updated_at = NOW()
            """, (contact_id, message))
            
            cur.execute("""
                INSERT INTO outcome_tracker (contact_id, agent_message, pattern_id)
                VALUES (%s, %s, %s)
                RETURNING id
            """, (contact_id, message, pattern_id))
            tracker_id = cur.fetchone()[0]
            
            return tracker_id


def record_lead_response(contact_id: str, message: str) -> Tuple[float, VibeClassification]:
    """
    Record a lead's response and score the previous agent message.
    Returns the outcome score and vibe classification.
    """
    vibe = classify_vibe(message)
    score = calculate_outcome_score(message, vibe)
    
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE contact_history SET
                    last_lead_response = %s,
                    last_lead_response_at = NOW(),
                    last_vibe = %s,
                    was_burned = FALSE,
                    updated_at = NOW()
                WHERE contact_id = %s
            """, (message, vibe.value, contact_id))
            
            cur.execute("""
                SELECT id, pattern_id FROM outcome_tracker
                WHERE contact_id = %s AND scored = FALSE
                ORDER BY agent_message_at DESC
                LIMIT 1
            """, (contact_id,))
            pending = cur.fetchone()
            
            if pending:
                cur.execute("""
                    UPDATE outcome_tracker SET
                        lead_response = %s,
                        lead_response_at = NOW(),
                        outcome_score = %s,
                        vibe_classification = %s,
                        scored = TRUE
                    WHERE id = %s
                """, (message, score, vibe.value, pending['id']))
                
                if pending['pattern_id']:
                    old_score_result = cur.execute("""
                        SELECT score FROM response_patterns WHERE id = %s
                    """, (pending['pattern_id'],))
                    
                    cur.execute("""
                        UPDATE response_patterns SET
                            times_used = times_used + 1,
                            times_successful = times_successful + CASE WHEN %s >= 1.0 THEN 1 ELSE 0 END,
                            score = (score * 0.7) + (%s * 0.3),
                            last_used_at = NOW()
                        WHERE id = %s
                    """, (score, score, pending['pattern_id']))
                    logger.info(f"Pattern {pending['pattern_id']} score updated based on outcome {score}")
    
    return score, vibe


def save_new_pattern(
    trigger_message: str,
    response_used: str,
    vibe: VibeClassification,
    initial_score: float = 1.0
) -> int:
    """
    Save a new pattern that showed promise.
    Called when a response gets a good outcome and we want to remember it.
    """
    bank = get_pattern_bank(vibe)
    trigger_category = get_trigger_category(trigger_message, vibe)
    
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id FROM response_patterns
                WHERE trigger_category = %s AND response_used = %s
            """, (trigger_category, response_used))
            existing = cur.fetchone()
            
            if existing:
                cur.execute("""
                    UPDATE response_patterns SET
                        score = GREATEST(score, %s),
                        last_used_at = NOW()
                    WHERE id = %s
                """, (initial_score, existing['id']))
                return existing['id']
            
            cur.execute("""
                SELECT id, score FROM response_patterns
                WHERE trigger_category = %s AND bank = %s
                ORDER BY score ASC
            """, (trigger_category, bank.value))
            category_patterns = cur.fetchall()
            
            if len(category_patterns) >= 3:
                if initial_score > category_patterns[0]['score']:
                    cur.execute("DELETE FROM response_patterns WHERE id = %s", (category_patterns[0]['id'],))
                    logger.info(f"Replaced weak pattern (score {category_patterns[0]['score']:.2f}) with new one")
                else:
                    return category_patterns[0]['id']
            
            cur.execute("""
                INSERT INTO response_patterns 
                (bank, trigger_category, trigger_example, response_used, score, times_used, times_successful)
                VALUES (%s, %s, %s, %s, %s, 1, %s)
                RETURNING id
            """, (bank.value, trigger_category, trigger_message[:200], response_used, initial_score, 
                  1 if initial_score >= 1.0 else 0))
            new_id = cur.fetchone()['id']
            
            logger.info(f"Saved new {bank.value} pattern for '{trigger_category}' with score {initial_score}")
            return new_id


def mark_appointment_booked(contact_id: str):
    """Mark that an appointment was booked. Gives bonus to all patterns used."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE contact_history SET appointment_booked = TRUE WHERE contact_id = %s
            """, (contact_id,))
            
            cur.execute("""
                SELECT pattern_id FROM outcome_tracker 
                WHERE contact_id = %s AND pattern_id IS NOT NULL
            """, (contact_id,))
            pattern_ids = [row[0] for row in cur.fetchall()]
            
            for pattern_id in pattern_ids:
                cur.execute("""
                    UPDATE response_patterns SET score = score + 0.5 WHERE id = %s
                """, (pattern_id,))
                logger.info(f"Appointment bonus: Pattern {pattern_id} score boosted")


def check_for_burns() -> int:
    """Check for messages that never got replies (burned)."""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, contact_id, pattern_id FROM outcome_tracker
                WHERE scored = FALSE 
                AND agent_message_at < NOW() - INTERVAL '24 hours'
            """)
            burned = cur.fetchall()
            
            for outcome in burned:
                cur.execute("""
                    UPDATE outcome_tracker SET
                        outcome_score = -1.0,
                        scored = TRUE
                    WHERE id = %s
                """, (outcome['id'],))
                
                if outcome['pattern_id']:
                    cur.execute("""
                        UPDATE response_patterns SET
                            times_used = times_used + 1,
                            score = (score * 0.7) + (-1.0 * 0.3)
                        WHERE id = %s
                    """, (outcome['pattern_id'],))
                    logger.info(f"Burn penalty applied to pattern {outcome['pattern_id']}")
                
                cur.execute("""
                    UPDATE contact_history SET
                        burn_count = burn_count + 1
                    WHERE contact_id = %s
                """, (outcome['contact_id'],))
            
            return len(burned)


def get_learning_context(contact_id: str, current_message: str) -> str:
    """
    Get the full learning context to inject into Grok's prompt.
    Combines pattern suggestions and burn history.
    """
    vibe = classify_vibe(current_message)
    bank = get_pattern_bank(vibe)
    trigger_category = get_trigger_category(current_message, vibe)
    
    patterns = find_matching_patterns(trigger_category, bank)
    pattern_text = format_patterns_for_prompt(patterns)
    
    contact = get_contact_context(contact_id)
    burn_text = format_burn_context(contact)
    
    context_parts = []
    
    if burn_text:
        context_parts.append(burn_text)
    
    if pattern_text:
        context_parts.append(pattern_text)
    
    context_parts.append(f"\n[Detected vibe: {vibe.value} | Using {bank.value} approach | Category: {trigger_category}]\n")
    
    return "\n".join(context_parts)


def initialize_seed_patterns_sql(cur):
    """Initialize the pattern database with seed patterns."""
    seed_patterns = [
        ("recovery", "not_interested", "not interested", "Fair enough. Was it the timing or something else?", 2.0),
        ("recovery", "has_coverage", "I already have insurance", "Got it. Does that follow you if you switch jobs or retire?", 2.5),
        ("recovery", "bad_timing", "bad time right now", "No worries. When would be better to circle back?", 1.5),
        ("recovery", "price_objection", "too expensive", "Totally get it. What would make it worth looking at?", 2.0),
        ("recovery", "unknown_sender", "who is this", "This is {agent_name}, following up on the life insurance info you requested. What originally got you looking?", 2.0),
        ("recovery", "general_objection", "yeah im good", "No problem. Out of curiosity, what made you look into it originally?", 2.0),
        ("forward", "employer_coverage", "I have coverage through work", "Got it. What's the plan if you switch jobs or retire and that doesn't follow you?", 3.0),
        ("forward", "has_spouse", "my wife keeps asking about it", "Smart. What's the main thing she's worried about if something happened?", 3.0),
        ("forward", "has_kids", "I have two kids", "Got it. What would you want covered first, their education or keeping the house?", 3.0),
        ("forward", "health_concerns", "I have diabetes", "Okay. Is that controlled with pills or insulin, and do you know your A1C?", 2.5),
        ("forward", "asking_price", "how much does it cost", "Depends on a few things. What kind of coverage amount were you thinking, and are you in decent health?", 2.5),
        ("forward", "scheduling", "when can we talk", "I have 6:30 tonight or 10:15 tomorrow morning. Which works better?", 4.0),
        ("forward", "general_engagement", "yeah I've been thinking about it", "What's the main thing on your mind about it?", 2.0),
    ]
    
    for bank, category, trigger, response, score in seed_patterns:
        cur.execute("""
            INSERT INTO response_patterns 
            (bank, trigger_category, trigger_example, response_used, score, times_used, times_successful)
            VALUES (%s, %s, %s, %s, %s, 5, 3)
        """, (bank, category, trigger, response, score))
    
    logger.info(f"Initialized {len(seed_patterns)} seed patterns")
