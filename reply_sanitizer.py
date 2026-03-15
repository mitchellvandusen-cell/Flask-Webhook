# reply_sanitizer.py - Prevents LLM reasoning from reaching leads
# This is the LAST LINE OF DEFENSE before messages are sent.
import re
import logging

logger = logging.getLogger(__name__)

# Phrases that ONLY appear in system prompts or LLM reasoning, never in real texts
CONTAMINATION_MARKERS = [
    # System prompt section headers
    "tactical guidance",
    "critical privacy rule",
    "current stage:",
    "output rule",
    "conversation so far",
    "recent conversation:",
    "lead just said:",
    "no inbound message",
    "available slots:",
    "stop conditions",
    "forbidden phrases",
    "forbidden punctuation",
    "advanced psychology",
    "policy knowledge",
    "booking confirmation",
    "underwriting context",
    "company intel",
    # LLM reasoning patterns
    "first, the situation",
    "first, the instructions",
    "first, let me",
    "my output should",
    "my response should",
    "the system prompt",
    "the instructions say",
    "the guidance says",
    "previous messages:",
    "personality:",
    "for follow-up #",
    "i need to correct",
    "i need to output",
    "let me think",
    "let me analyze",
    "reasoning:",
    "the lead has not responded",
    "the lead hasn't responded",
    "this is a follow-up",
    "this is an outbound",
    "be creative, different angle",
    "keep it 1-3 sentences",
    "goal: get a reply",
    "goal: get any response",
    "do not sell",
    "do not mention insurance",
    "make them smile",
    "no emojis",
    # Memory / narrative observer leaks
    "recap:",
    "facts:",
    "known facts",
    "updated recap",
    "previous recap",
    "no new facts",
    "conversation stands",
    "awaiting the lead",
    "initial outreach phase",
    "lead intent:",
    "no details on current coverage",
    "no responses yet",
    "emotional_arc:",
    "objection_log:",
    "conversation memory",
    "lead temperature:",
    "angles already tried",
    # Bot self-reference as character
    "personality: ",
    "i'm playing the role",
    "as mitch,",
    "as the bot,",
]

# Quick check: if the reply starts with these, it's definitely reasoning
REASONING_OPENERS = [
    "first,",
    "okay, so",
    "okay so",
    "let me",
    "the situation",
    "the lead",
    "looking at",
    "based on the",
    "according to",
    "the instructions",
    "my previous response",
    "the guidance",
    "the tactical",
    "the system",
    "this is a follow",
    "for this follow",
    "for follow-up",
    "i should",
    "i need to",
    "the goal here",
    "the goal is",
    "recap:",
    "facts:",
]


def sanitize_reply(raw: str) -> str:
    """
    Strip LLM reasoning, chain-of-thought, and system prompt artifacts.
    Returns the clean message text, or empty string if the entire response
    is contaminated (caller should use a fallback).
    """
    if not raw or not raw.strip():
        return ""

    # 1. Strip tagged reasoning (<thinking>, <reply>, etc.)
    cleaned = re.sub(r'<thinking>[\s\S]*?</thinking>', '', raw)
    cleaned = re.sub(r'</?(?:thinking|reply|output|response)>', '', cleaned)
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    cleaned = cleaned.strip()

    if not cleaned:
        return ""

    # 2. Check for contamination markers
    lower = cleaned.lower()
    found_markers = [m for m in CONTAMINATION_MARKERS if m in lower]

    if len(found_markers) >= 2:
        logger.warning(f"REASONING CONTAMINATION DETECTED ({len(found_markers)} markers): {found_markers[:5]}")
        logger.warning(f"BLOCKED MESSAGE: '{cleaned[:200]}...'")
        return ""

    # 3. Check if the reply STARTS with a reasoning opener
    lower_stripped = lower.lstrip()
    for opener in REASONING_OPENERS:
        if lower_stripped.startswith(opener):
            # Could be reasoning. Check if it also has at least 1 contamination marker
            if found_markers:
                logger.warning(f"REASONING OPENER + MARKER: starts with '{opener}', marker={found_markers[0]}")
                logger.warning(f"BLOCKED MESSAGE: '{cleaned[:200]}...'")
                return ""
            break

    # 4. Check message length — real texts are short, reasoning dumps are long
    #    A 500+ char SMS that also has a contamination marker is almost certainly reasoning
    if len(cleaned) > 500 and found_markers:
        logger.warning(f"LONG MESSAGE + MARKER: {len(cleaned)} chars, marker={found_markers[0]}")
        logger.warning(f"BLOCKED MESSAGE: '{cleaned[:200]}...'")
        return ""

    return cleaned


def is_safe_to_send(message: str) -> bool:
    """
    Final gate before a message is sent to a lead via GHL.
    Returns False if the message contains system prompt artifacts.
    This is the ABSOLUTE LAST CHECK — if this returns False, the message
    must not be sent under any circumstances.
    """
    if not message or not message.strip():
        return False

    lower = message.lower()

    # Hard block: these strings should NEVER appear in a text to a lead
    HARD_BLOCKS = [
        "critical privacy rule",
        "tactical guidance",
        "system prompt",
        "output rule",
        "forbidden phrases",
        "forbidden punctuation",
        "stop conditions",
        "the instructions say",
        "my output should",
        "conversation so far ===",
        "conversation memory ===",
        "recent conversation:",
        "current stage:",
        "no inbound message",
        "=== ",
        "recap:",
        "facts:",
        "emotional_arc:",
        "objection_log:",
        "lead temperature:",
    ]

    for block in HARD_BLOCKS:
        if block in lower:
            logger.error(f"HARD BLOCK: '{block}' found in outbound message — MESSAGE KILLED")
            logger.error(f"KILLED MESSAGE: '{message[:300]}...'")
            return False

    return True
