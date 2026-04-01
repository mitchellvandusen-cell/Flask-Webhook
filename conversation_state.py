# conversation_state.py — Deterministic Conversation State Tracker
#
# Replaces the LLM-based narrative observer with a pure-Python state machine
# that maintains a structured narrative from raw message analysis.
#
# Why: The narrative observer was an LLM call (~$0.003, ~2s latency) that read
# all messages and produced SITUATION / EMOTIONAL_ARC / OBJECTION_LOG /
# CONVERSATIONAL_THREAD sections. But ALL the information it extracted already
# exists in the raw messages + keyword engine + classification memory.
# This module produces the same structured output deterministically.
#
# What the response-generation LLM needs from us:
#   1. SITUATION: What stage is the conversation at, what was last discussed
#   2. CONVERSATIONAL_THREAD: What the lead is CURRENTLY talking about
#   3. EMOTIONAL_ARC: Moments of vulnerability, grief, family concern
#   4. OBJECTION_LOG: Every objection + the angle used to handle it
#   5. New facts extracted from the latest message
#
# All of these can be built from: messages, keyword matching, and spaCy NLP.

import re
import logging
from typing import List, Dict, Tuple, Optional
from conversation_engine import (
    detect_objection_keywords, ObjectionType, ObjectionNature,
    detect_buying_signal, BuyingSignalType,
    determine_message_context, MessageContext,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# EMOTIONAL ARC DETECTION — keyword patterns for vulnerability
# ═══════════════════════════════════════════════════════════════════

_EMOTIONAL_KEYWORDS = {
    "grief": [
        "passed away", "died", "lost my", "lost him", "lost her",
        "funeral", "burial", "grieving", "mourning", "rip",
        "miss him", "miss her", "miss them", "he's gone", "she's gone",
    ],
    "family_fear": [
        "what happens to", "what would happen", "who takes care",
        "my kids would", "my wife would", "my husband would",
        "they'd be", "she'd be", "he'd be",
        "leave them with nothing", "burden on", "can't sleep",
        "keep me up", "keeps me up", "worried about",
        "terrif", "afraid", "scared", "devastating",
    ],
    "personal_resolve": [
        "gotta make sure", "need to protect", "owe it to",
        "can't leave them", "want to make sure", "have to get this done",
        "for my kids", "for my wife", "for my husband", "for my family",
        "do the right thing", "step up",
    ],
    "health_vulnerability": [
        "diagnosed", "cancer", "heart attack", "stroke", "diabetes",
        "hospital", "surgery", "chemo", "terminal", "disability",
        "can't work", "on disability", "health scare",
    ],
    "financial_stress": [
        "paycheck to paycheck", "barely making", "can't afford",
        "lost my job", "laid off", "behind on", "debt",
        "no savings", "broke", "struggling",
    ],
}

# ═══════════════════════════════════════════════════════════════════
# CONVERSATIONAL THREAD DETECTION — what is the lead talking about?
# ═══════════════════════════════════════════════════════════════════

# Pre-compiled at module load — runs on every inbound message hot path
_THREAD_PATTERNS = [
    (re.compile(r"(?:too many|stop|quit|enough)\s+(?:text|message|sms)"), "Lead is commenting on bot texting frequency"),
    (re.compile(r"(?:you\s+text|texting\s+(?:me|too))"), "Lead is commenting on bot texting behavior"),
    (re.compile(r"(?:who\s+(?:is\s+)?this|who\s+are\s+you|how\s+(?:did\s+)?you\s+get)"), "Lead is asking who the bot is or how they got their number"),
    (re.compile(r"(?:through\s+(?:my\s+)?(?:work|job|employer)|group\s+(?:plan|policy|coverage))"), "Lead is describing their existing coverage through work"),
    (re.compile(r"(?:have\s+(?:a\s+)?(?:policy|coverage|insurance)|with\s+\w+\s+(?:insurance|life))"), "Lead is describing their existing coverage"),
    (re.compile(r"(?:my\s+(?:wife|husband|spouse|kids|children|daughter|son|family|mom|dad|parent))"), "Lead is sharing family information"),
    (re.compile(r"(?:how\s+much|cost|price|rate|premium|afford|expensive|cheap)"), "Lead is asking about pricing"),
    (re.compile(r"(?:book\s+(?:a|an|the)|schedule\s+(?:a|an)|appointment|what\s+time(?:s)?|when\s+(?:can|are|do)\s+you)"), "Lead is discussing scheduling"),
]

# Fact extraction is handled by spaCy in tasks.py — not duplicated here.
# conversation_state only tracks emotional arc, objection log, and thread.


def _detect_emotional_moments(text: str) -> List[str]:
    """Extract emotional moments from a lead message."""
    text_lower = text.lower()
    moments = []
    for category, phrases in _EMOTIONAL_KEYWORDS.items():
        for phrase in phrases:
            if phrase in text_lower:
                # Capture the surrounding context (up to 60 chars around the match)
                idx = text_lower.index(phrase)
                start = max(0, idx - 20)
                end = min(len(text), idx + len(phrase) + 30)
                context = text[start:end].strip()
                if len(context) > 10:
                    moments.append(f"[{category}] \"{context}\"")
                break  # One match per category per message
    return moments


def _detect_thread(message: str, last_bot_msg: str = "") -> str:
    """Detect what the lead is currently talking about."""
    msg_lower = message.lower()

    for compiled_pattern, description in _THREAD_PATTERNS:
        if compiled_pattern.search(msg_lower):
            return description

    # If the bot asked a question, the lead is probably answering it
    if last_bot_msg and last_bot_msg.strip().endswith("?"):
        # Extract the question topic
        q = last_bot_msg.strip()
        if len(q) > 20:
            return f"Lead is responding to bot's question: \"{q[-60:]}\""

    # Default: general conversation
    if len(message.split()) <= 3:
        return "Lead gave a short response — may need clarification"
    return "Lead is in general conversation"


def build_narrative_from_state(
    messages: List[Dict[str, str]],
    current_message: str,
    existing_narrative: str,
    existing_facts: List[str],
    objection_type: ObjectionType = ObjectionType.NONE,
    objection_nature: ObjectionNature = ObjectionNature.NONE,
    stage: str = "qualifying",
    buying_signal: BuyingSignalType = BuyingSignalType.NONE,
) -> Dict:
    """
    Build a structured narrative from conversation state — NO LLM call.

    Produces the same 5-section output as the old LLM narrative observer:
    - SITUATION: Where things stand right now
    - CONVERSATIONAL_THREAD: What the lead is currently talking about
    - EMOTIONAL_ARC: Accumulated emotional moments (never forgotten)
    - OBJECTION_LOG: Every objection + angle used
    - New facts extracted from the latest message

    Returns: {"narrative": str, "new_facts": List[str]}
    """
    result = {"narrative": existing_narrative or "", "new_facts": []}

    if not messages and not current_message:
        return result

    # ── Parse existing narrative to carry forward emotional arc + objection log ──
    existing_emotional_arc = []
    existing_objection_log = []

    if existing_narrative:
        # Extract EMOTIONAL_ARC entries from previous narrative
        if "EMOTIONAL_ARC:" in existing_narrative:
            arc_section = existing_narrative.split("EMOTIONAL_ARC:", 1)[1]
            for marker in ["OBJECTION_LOG:", "FACTS:", "SITUATION:", "CONVERSATIONAL_THREAD:"]:
                if marker in arc_section:
                    arc_section = arc_section.split(marker, 1)[0]
            for line in arc_section.strip().split("\n"):
                line = line.strip().lstrip("-•* ")
                if line and line.upper() != "NONE" and len(line) > 5:
                    existing_emotional_arc.append(line)

        # Extract OBJECTION_LOG entries from previous narrative
        if "OBJECTION_LOG:" in existing_narrative:
            log_section = existing_narrative.split("OBJECTION_LOG:", 1)[1]
            for marker in ["FACTS:", "SITUATION:", "EMOTIONAL_ARC:", "CONVERSATIONAL_THREAD:"]:
                if marker in log_section:
                    log_section = log_section.split(marker, 1)[0]
            for line in log_section.strip().split("\n"):
                line = line.strip().lstrip("-•* ")
                if line and line.upper() != "NONE" and len(line) > 5:
                    existing_objection_log.append(line)

    # ── Analyze current message ──
    lead_msgs = [m for m in messages if m.get('role') == 'lead']
    bot_msgs = [m for m in messages if m.get('role') == 'assistant']
    last_bot = bot_msgs[-1]['text'] if bot_msgs else ""
    msg_context, consecutive_bot = determine_message_context(current_message, messages)

    # ── 1. SITUATION ──
    lead_count = len(lead_msgs) + (1 if current_message else 0)
    bot_count = len(bot_msgs)

    if lead_count == 0 and bot_count == 0:
        situation = "First contact. No conversation yet."
    elif lead_count == 0:
        situation = f"Bot has sent {bot_count} message(s). Lead has not responded yet."
    else:
        # Build situation from what we know
        situation_parts = []

        # What stage
        stage_labels = {
            "initial_outreach": "Initial outreach — first contact attempt",
            "qualifying": "Discovery phase — learning about lead's situation",
            "rapport": "Building rapport — lead is chatting, not about insurance yet",
            "objection_handling": "Handling objection — lead pushed back",
            "booking": "Moving to booking — lead shows interest",
            "booked": "Appointment booked — confirmed",
        }
        clean_stage = stage.split(":")[0] if ":" in stage else stage
        situation_parts.append(stage_labels.get(clean_stage, f"Stage: {stage}"))

        # Exchange count
        situation_parts.append(f"{lead_count} lead messages, {bot_count} bot messages exchanged")

        # What's happening right now
        if current_message:
            if objection_type != ObjectionType.NONE:
                situation_parts.append(f"Lead's latest message is a {objection_type.value.replace('_', ' ')} objection")
            elif buying_signal != BuyingSignalType.NONE:
                situation_parts.append(f"Lead showing buying signal: {buying_signal.value.replace('_', ' ')}")
            elif msg_context == MessageContext.INBOUND_REPLY:
                situation_parts.append("Lead replied — respond to what they said")
        elif msg_context == MessageContext.FOLLOW_UP_NO_REPLY:
            situation_parts.append(f"Lead has not responded. {consecutive_bot} consecutive bot messages with no reply")

        situation = ". ".join(situation_parts) + "."

    # ── 2. CONVERSATIONAL_THREAD ──
    thread = "No conversation yet."
    if current_message:
        thread = _detect_thread(current_message, last_bot)
    elif lead_msgs:
        thread = _detect_thread(lead_msgs[-1]['text'], last_bot)

    # ── 3. EMOTIONAL_ARC — accumulate, never forget ──
    emotional_arc = list(existing_emotional_arc)  # carry forward ALL previous
    if current_message:
        new_moments = _detect_emotional_moments(current_message)
        for moment in new_moments:
            # Deduplicate against existing
            if not any(moment.lower() in existing.lower() for existing in emotional_arc):
                emotional_arc.append(moment)

    # Also scan recent lead messages if this is first time building arc
    if not emotional_arc and lead_msgs:
        for msg in lead_msgs[-5:]:
            moments = _detect_emotional_moments(msg['text'])
            for moment in moments:
                if not any(moment.lower() in existing.lower() for existing in emotional_arc):
                    emotional_arc.append(moment)

    # ── 4. OBJECTION_LOG — accumulate with type tags ──
    objection_log = list(existing_objection_log)  # carry forward ALL previous

    if objection_type != ObjectionType.NONE and current_message:
        # Format: [TYPE] Objection: what they said > Angle: how bot last responded
        obj_tag = f"[{objection_type.value.upper()}]"
        lead_said = current_message[:80].strip()

        # Find the angle the bot used (its last message before this objection)
        angle = "awaiting response"
        if last_bot:
            angle = last_bot[:80].strip()

        new_entry = f"{obj_tag} Objection: {lead_said} > Angle: {angle}"

        # Deduplicate: don't add if the same objection text already exists
        if not any(lead_said.lower() in entry.lower() for entry in objection_log):
            objection_log.append(new_entry)

    # Fact extraction is handled by spaCy in tasks.py — not duplicated here.

    # ── Assemble narrative ──
    arc_str = "\n".join(f"- {m}" for m in emotional_arc) if emotional_arc else "NONE"
    log_str = "\n".join(f"- {e}" for e in objection_log) if objection_log else "NONE"

    narrative = f"""SITUATION:
{situation}

CONVERSATIONAL_THREAD:
{thread}

EMOTIONAL_ARC:
{arc_str}

OBJECTION_LOG:
{log_str}"""

    result["narrative"] = narrative
    return result
