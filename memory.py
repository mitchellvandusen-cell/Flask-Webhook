"""
NLP Memory System using spaCy
Stores all messages per contact_id, parses with NLP, and extracts topics/entities.
"""
import os
import logger
import json
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

import spacy
import psycopg2
from psycopg2.extras import RealDictCursor
# EMERGENCY FIX — auto-download medium model + create missing DB stuff
import subprocess
import sys
import os

from db import get_db_connection
# Auto-install + download the good spaCy model (with vectors)
try:
    import spacy
    if not spacy.util.is_package("en_core_web_md"):
        print("Downloading en_core_web_md (with vectors)...")
        subprocess.check_call([sys.executable, "-m", "spacy", "download", "en_core_web_md"])
    nlp = spacy.load("en_core_web_md")
    print("spaCy medium model loaded — perfect similarity checks")
except Exception as e:
    print("spaCy fallback:", e)
    nlp = spacy.load("en_core_web_sm")

nlp = None

def get_nlp():
    """Lazy load spaCy model - use 'md' for vector similarity, 'sm' as fallback"""
    global nlp
    if nlp is None:
        try:
            # Try to load medium model first (has word vectors for similarity)
            nlp = spacy.load("en_core_web_md")
            logger.info("spaCy en_core_web_md model loaded (with vectors)")
        except OSError:
            try:
                # Fall back to small model (no vectors, similarity will be limited)
                nlp = spacy.load("en_core_web_sm")
                logger.warning("spaCy en_core_web_sm loaded (no vectors - similarity limited)")
            except Exception as e:
                logger.error(f"Failed to load any spaCy model: {e}")
                return None
        except Exception as e:
            logger.error(f"Failed to load spaCy model: {e}")
            return None
    return nlp


TOPIC_PATTERNS = {
    "coverage_status": {
        "has_coverage": [r"(have|got|already).*(coverage|policy|insurance|protected)"],
        "no_coverage": [r"(don'?t|dont|no).*(coverage|policy|insurance)"],
    },
    "policy_type": {
        "term": [r"term\s*(life|policy|insurance|plan)"],
        "whole_life": [r"whole\s*life"],
        "iul": [r"\biul\b|indexed\s*universal"],
        "guaranteed_issue": [r"guaranteed\s*(issue|acceptance)"],
    },
    "policy_source": {
        "employer": [r"(through|from|at|via).*(work|job|employer|company)"],
        "personal": [r"(my own|personal|private|individual).*(policy|coverage)"],
        "not_employer": [r"(not|isn'?t).*(through|from).*(work|job|employer)"],
    },
    "family": {
        "married": [r"wife|husband|spouse|married"],
        "single": [r"single|not\s*married|divorced|widowed"],
        "has_kids": [r"\d+\s*kids?", r"my kids|children|child"],
        "no_kids": [r"no\s*kids|don'?t\s*have\s*kids"],
    },
    "health": {
        "diabetes": [r"diabetes|diabetic|a1c|insulin|metformin"],
        "heart": [r"heart\s*(attack|disease|condition)|cardiac|stent"],
        "cancer": [r"cancer|tumor|chemo|remission"],
        "healthy": [r"healthy|good health|no (health )?issues"],
        "tobacco": [r"smok(e|er|ing)|tobacco|cigarette|vape"],
    },
    "motivation": {
        "add_coverage": [r"(add|more|additional).*(coverage|protection)"],
        "mortgage": [r"cover.*(mortgage|house)|pay\s*off.*(mortgage|house)"],
        "final_expense": [r"final\s*expense|funeral|burial"],
        "family_protection": [r"protect.*(family|wife|kids)"],
        "family_death": [r"(mom|dad|parent).*(died|passed)"],
    },
    "objections": {
        "too_expensive": [r"(can'?t|don'?t)\s*afford|too\s*expensive|cost"],
        "not_interested": [r"not\s*interested|don'?t\s*need|no thanks"],
        "already_covered": [r"already\s*(have|got|covered|set)"],
        "need_time": [r"think\s*about|talk\s*to.*(spouse|wife|husband)"],
    },
    "buying_signals": {
        "ready_to_buy": [r"(let'?s|ready|want)\s*(to\s*)?(do|get|start|move forward)"],
        "appointment_interest": [r"(sounds|works|let'?s).*(good|great|do it|meet)"],
        "price_inquiry": [r"how\s*much|what.*(cost|price|rate)"],
    },
    "carrier": {
        "state_farm": [r"state\s*farm"],
        "allstate": [r"allstate"],
        "metlife": [r"metlife|met\s*life"],
        "prudential": [r"prudential"],
        "northwestern": [r"northwestern"],
        "colonial_penn": [r"colonial\s*penn"],
        "globe_life": [r"globe\s*life"],
    },
}

import re

def extract_topics_with_spacy(text: str, nlp_model) -> Dict[str, Any]:
    """Extract topics and entities from text using spaCy"""
    doc = nlp_model(text)
    
    result = {
        "entities": [],
        "topics": {},
        "noun_phrases": [],
        "key_terms": [],
    }
    
    for ent in doc.ents:
        result["entities"].append({
            "text": ent.text,
            "label": ent.label_,
            "start": ent.start_char,
            "end": ent.end_char,
        })
    
    for chunk in doc.noun_chunks:
        result["noun_phrases"].append(chunk.text)
    
    text_lower = text.lower()
    for category, patterns in TOPIC_PATTERNS.items():
        for topic_name, regex_list in patterns.items():
            for pattern in regex_list:
                if re.search(pattern, text_lower):
                    if category not in result["topics"]:
                        result["topics"][category] = []
                    result["topics"][category].append(topic_name)
                    break
    
    for token in doc:
        if token.pos_ in ["NOUN", "PROPN", "ADJ"] and not token.is_stop and len(token.text) > 2:
            result["key_terms"].append(token.text.lower())
    
    result["key_terms"] = list(set(result["key_terms"]))[:20]
    
    return result


def save_message(contact_id: str, message_text: str, message_type: str = "lead") -> Optional[int]:
    """Save a message to the database and parse it with NLP"""
    if not contact_id or not message_text:
        return None
    
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO contact_messages (contact_id, message_type, message_text, created_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (contact_id, message_text, message_type, created_at) DO NOTHING
            RETURNING id
        """, (contact_id, message_type, message_text))
        
        result = cur.fetchone()
        if not result:
            conn.commit()
            return None
        
        message_id = result[0]
        
        nlp_model = get_nlp()
        if nlp_model:
            parsed = extract_topics_with_spacy(message_text, nlp_model)
            
            for entity in parsed.get("entities", []):
                cur.execute("""
                    INSERT INTO parsed_entities (contact_id, message_id, entity_type, entity_text, entity_label)
                    VALUES (%s, %s, %s, %s, %s)
                """, (contact_id, message_id, entity.get("label", "UNKNOWN"), 
                      entity.get("text", ""), entity.get("label", "")))
            
            for category, topic_list in parsed.get("topics", {}).items():
                for topic_name in topic_list:
                    full_topic = f"{category}:{topic_name}"
                    cur.execute("""
                        INSERT INTO topic_breakdown (contact_id, topic_name, source_message_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (contact_id, topic_name) 
                        DO UPDATE SET 
                            times_mentioned = topic_breakdown.times_mentioned + 1,
                            last_mentioned_at = CURRENT_TIMESTAMP
                    """, (contact_id, full_topic, message_id))
            
            cur.execute("""
                UPDATE contact_messages SET parsed_at = CURRENT_TIMESTAMP WHERE id = %s
            """, (message_id,))
        
        conn.commit()
        logger.debug(f"Saved message for contact {contact_id}: {message_text[:50]}...")
        return message_id
        
    except Exception as e:
        logger.error(f"Error saving message: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            conn.close()


def get_contact_messages(contact_id: str, limit: int = 50) -> List[Dict]:
    """Get all messages for a contact"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, message_type, message_text, created_at, parsed_at
            FROM contact_messages 
            WHERE contact_id = %s 
            ORDER BY created_at DESC
            LIMIT %s
        """, (contact_id, limit))
        
        return [dict(row) for row in cur.fetchall()]
        
    except Exception as e:
        logger.error(f"Error getting messages: {e}")
        return []
    finally:
        if conn:
            conn.close()


def get_topic_breakdown(contact_id: str) -> Dict[str, List[Dict]]:
    """Get topic breakdown for a contact, organized by category"""
    conn = get_db_connection()
    if not conn:
        return {}
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT topic_name, topic_value, times_mentioned, 
                   first_mentioned_at, last_mentioned_at, confidence
            FROM topic_breakdown 
            WHERE contact_id = %s 
            ORDER BY times_mentioned DESC
        """, (contact_id,))
        
        rows = cur.fetchall()
        
        breakdown = {}
        for row in rows:
            topic_name = row["topic_name"]
            if ":" in topic_name:
                category, topic = topic_name.split(":", 1)
            else:
                category = "general"
                topic = topic_name
            
            if category not in breakdown:
                breakdown[category] = []
            
            breakdown[category].append({
                "topic": topic,
                "times_mentioned": row["times_mentioned"],
                "first_mentioned": str(row["first_mentioned_at"]),
                "last_mentioned": str(row["last_mentioned_at"]),
                "confidence": row["confidence"],
            })
        
        return breakdown
        
    except Exception as e:
        logger.error(f"Error getting topic breakdown: {e}")
        return {}
    finally:
        if conn:
            conn.close()


def get_contact_entities(contact_id: str) -> List[Dict]:
    """Get all extracted entities for a contact"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT entity_type, entity_text, entity_label, confidence, created_at
            FROM parsed_entities 
            WHERE contact_id = %s 
            ORDER BY created_at DESC
        """, (contact_id,))
        
        return [dict(row) for row in cur.fetchall()]
        
    except Exception as e:
        logger.error(f"Error getting entities: {e}")
        return []
    finally:
        if conn:
            conn.close()


def get_contact_nlp_summary(contact_id: str) -> Dict[str, Any]:
    """Get complete NLP summary for a contact"""
    messages = get_contact_messages(contact_id)
    topics = get_topic_breakdown(contact_id)
    entities = get_contact_entities(contact_id)
    
    lead_messages = [m for m in messages if m.get("message_type") == "lead"]
    agent_messages = [m for m in messages if m.get("message_type") == "agent"]
    
    return {
        "contact_id": contact_id,
        "total_messages": len(messages),
        "lead_messages": len(lead_messages),
        "agent_messages": len(agent_messages),
        "topics": topics,
        "entities": entities[:20],
        "recent_messages": messages[:10],
    }


def format_nlp_for_prompt(contact_id: str) -> str:
    """Format NLP data for injection into LLM prompt"""
    topics = get_topic_breakdown(contact_id)
    
    if not topics:
        return ""
    
    sections = ["=== NLP MEMORY (Topics Discussed) ==="]
    
    for category, topic_list in topics.items():
        topic_strs = [t["topic"] for t in topic_list[:5]]
        if topic_strs:
            sections.append(f"{category.upper()}: {', '.join(topic_strs)}")
    
    return "\n".join(sections)


def get_topics_already_discussed(contact_id: str) -> List[str]:
    """Get list of topic names already discussed with this contact"""
    topics = get_topic_breakdown(contact_id)
    
    discussed = []
    for category, topic_list in topics.items():
        for t in topic_list:
            discussed.append(f"{category}:{t['topic']}")
            discussed.append(t['topic'])
    
    return list(set(discussed))


def check_vector_similarity(proposed_response: str, recent_agent_messages: List[str], threshold: float = 0.85) -> Tuple[bool, float, Optional[str]]:
    """
    Check if a proposed response is too similar to recent agent messages using spaCy vectors.
    
    Args:
        proposed_response: The response the agent is about to send
        recent_agent_messages: List of recent agent messages (typically last 5)
        threshold: Similarity threshold (0.85 = 85% similar, should be blocked)
    
    Returns:
        Tuple of (is_too_similar, highest_similarity_score, most_similar_message)
    """
    nlp_model = get_nlp()
    if not nlp_model or not recent_agent_messages:
        return (False, 0.0, None)
    
    try:
        proposed_doc = nlp_model(proposed_response.lower().strip())
        
        highest_sim = 0.0
        most_similar = None
        
        for msg in recent_agent_messages[-5:]:  # Check last 5 messages
            if not msg or len(msg.strip()) < 10:
                continue
            
            msg_doc = nlp_model(msg.lower().strip())
            
            # Get vector similarity
            similarity = proposed_doc.similarity(msg_doc)
            
            if similarity > highest_sim:
                highest_sim = similarity
                most_similar = msg
        
        is_too_similar = highest_sim >= threshold
        
        if is_too_similar:
            logger.warning(f"VECTOR_SIMILARITY: Blocked response (sim={highest_sim:.2f} >= {threshold})")
            logger.debug(f"Proposed: {proposed_response[:80]}...")
            logger.debug(f"Similar to: {most_similar[:80]}...")
        
        return (is_too_similar, highest_sim, most_similar)
        
    except Exception as e:
        logger.error(f"Vector similarity check failed: {e}")
        return (False, 0.0, None)


def get_recent_agent_messages(contact_id: str, limit: int = 5) -> List[str]:
    """Get recent agent messages for a contact (for similarity checking)"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT message_text FROM contact_messages 
            WHERE contact_id = %s AND message_type = 'agent'
            ORDER BY created_at DESC
            LIMIT %s
        """, (contact_id, limit))
        
        return [row[0] for row in cur.fetchall()]
        
    except Exception as e:
        logger.error(f"Error getting recent agent messages: {e}")
        return []
    finally:
        if conn:
            conn.close()


def validate_response_uniqueness(contact_id: str, proposed_response: str, threshold: float = 0.85) -> Tuple[bool, str]:
    """
    Full validation: check if proposed response is unique enough to send.
    
    Returns:
        Tuple of (is_valid, reason)
        - is_valid: True if response can be sent, False if too similar
        - reason: Explanation of why blocked (or "OK" if valid)
    """
    recent_messages = get_recent_agent_messages(contact_id, limit=5)
    
    if not recent_messages:
        return (True, "OK - no prior messages to compare")
    
    is_similar, sim_score, similar_msg = check_vector_similarity(
        proposed_response, recent_messages, threshold
    )
    
    if is_similar:
        return (False, f"Too similar to recent message (similarity={sim_score:.2f}): {similar_msg[:50]}...")
    
    return (True, f"OK - unique enough (max similarity={sim_score:.2f})")

# add_to_qualification_array function — defined here so it's available
def add_to_qualification_array(contact_id: str, field: str, value: str):
    """Add a value to an array field in contact_qualification (topics_asked, blockers, etc.)"""
    if not contact_id or not field or not value:
        return False
    
    allowed_fields = [
        "topics_asked", "key_quotes", "blockers",
        "health_conditions", "health_details"
    ]
    
    if field not in allowed_fields:
        logger.warning(f"Invalid array field '{field}' for add_to_qualification_array")
        return False
    
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        cur = conn.cursor()
        
        # UPSERT: add value to array if not present
        cur.execute(f"""
            INSERT INTO contact_qualification (contact_id, {field})
            VALUES (%s, ARRAY[%s]::TEXT[])
            ON CONFLICT (contact_id) 
            DO UPDATE SET 
                {field} = (
                    SELECT ARRAY(
                        SELECT DISTINCT unnest({field} || EXCLUDED.{field})
                    )
                ),
                updated_at = CURRENT_TIMESTAMP
        """, (contact_id, value))
        
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Added '{value}' to {field} for contact {contact_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to add to {field}: {e}")
        return False

# (keep parse_reflection and strip_reflection if you use them)