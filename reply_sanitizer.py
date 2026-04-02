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

# Hard identity reveals — block on a SINGLE match. These must never reach a lead.
IDENTITY_MARKERS = [
    # AI/bot self-identification
    "i'm an ai",
    "i am an ai",
    "i'm a bot",
    "i am a bot",
    "i'm a virtual",
    "i am a virtual",
    "virtual assistant",
    "automated message",
    "automated system",
    "i'm an automated",
    "i am an automated",
    "as an ai",
    "artificial intelligence",
    "language model",
    "i'm not a real person",
    "i am not a real person",
    "i'm not human",
    "i am not human",
    "i'm just a program",
    "i am just a program",
    "i'm simulated",
    "i'm a text bot",
    "i'm an sms bot",
    "text bot",
    "sms bot",
    "voice bot",
    "voice agent system",
    "voice assistant",
    "chatbot",
    "chat bot",
    # Technical failure language that breaks the human persona
    "technical issue",
    "technical error",
    "system error",
    "server error",
    "api error",
    "processing error",
    "system is down",
    "system had an issue",
    "calendar is unavailable",
    "calendar is temporarily",
    "booking system",
    "scheduling system",
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

    # 2. Strip emojis — the prompt forbids them, but LLMs sometimes slip them in.
    #    Matches all Unicode emoji ranges (emoticons, symbols, flags, modifiers).
    cleaned = re.sub(
        r'[\U0001F600-\U0001F64F'   # Emoticons
        r'\U0001F300-\U0001F5FF'    # Misc Symbols and Pictographs
        r'\U0001F680-\U0001F6FF'    # Transport and Map
        r'\U0001F1E0-\U0001F1FF'    # Flags
        r'\U00002702-\U000027B0'    # Dingbats
        r'\U0000FE00-\U0000FE0F'    # Variation Selectors
        r'\U0001F900-\U0001F9FF'    # Supplemental Symbols
        r'\U0001FA00-\U0001FA6F'    # Chess Symbols / Extended-A
        r'\U0001FA70-\U0001FAFF'    # Symbols Extended-A continued
        r'\U00002600-\U000026FF'    # Misc symbols (sun, cloud, etc.)
        r'\U0000200D'               # Zero Width Joiner
        r'\U00002328'               # Keyboard
        r'\U000023CF'               # Eject
        r'\U000023E9-\U000023F3'    # Media controls
        r'\U000023F8-\U000023FA'    # Media controls 2
        r'\U0000231A-\U0000231B]+', # Watch/hourglass
        '', cleaned
    ).strip()

    if not cleaned:
        logger.warning("Reply was emoji-only — blocked")
        return ""

    # 3. Check for contamination markers
    lower = cleaned.lower()

    # 3a. Hard identity reveals — block on a single match
    found_identity = [m for m in IDENTITY_MARKERS if m in lower]
    if found_identity:
        logger.warning(f"BOT IDENTITY REVEAL BLOCKED ({found_identity[0]}): '{cleaned[:200]}...'")
        return ""

    # 3b. System-prompt / reasoning leaks — require 2+ markers to avoid false positives
    found_markers = [m for m in CONTAMINATION_MARKERS if m in lower]

    if len(found_markers) >= 2:
        logger.warning(f"REASONING CONTAMINATION DETECTED ({len(found_markers)} markers): {found_markers[:5]}")
        logger.warning(f"BLOCKED MESSAGE: '{cleaned[:200]}...'")
        return ""

    # 4. Check if the reply STARTS with a reasoning opener
    lower_stripped = lower.lstrip()
    for opener in REASONING_OPENERS:
        if lower_stripped.startswith(opener):
            # Could be reasoning. Check if it also has at least 1 contamination marker
            if found_markers:
                logger.warning(f"REASONING OPENER + MARKER: starts with '{opener}', marker={found_markers[0]}")
                logger.warning(f"BLOCKED MESSAGE: '{cleaned[:200]}...'")
                return ""
            break

    # 5. Check message length — real texts are short, reasoning dumps are long
    #    A 500+ char SMS that also has a contamination marker is almost certainly reasoning
    if len(cleaned) > 500 and found_markers:
        logger.warning(f"LONG MESSAGE + MARKER: {len(cleaned)} chars, marker={found_markers[0]}")
        logger.warning(f"BLOCKED MESSAGE: '{cleaned[:200]}...'")
        return ""

    # 6. Check for surrender language — bot should NEVER give up on a lead.
    #    These phrases signal the bot is walking away from an objection instead
    #    of handling it. If detected, block the message so the LLM retries.
    SURRENDER_PHRASES = [
        # Remove from list
        "i'll remove you from",
        "ill remove you from",
        "i will remove you from",
        "remove you from my list",
        "remove you from our list",
        "take you off my list",
        "take you off our list",
        "take you off the list",
        # Won't bother / won't reach out
        "i won't bother you",
        "i wont bother you",
        "i will not bother you",
        "won't reach out again",
        "wont reach out again",
        "will not reach out again",
        "won't contact you again",
        "wont contact you again",
        # Leave alone
        "i'll leave you alone",
        "ill leave you alone",
        "i will leave you alone",
        "i'll let you be",
        "ill let you be",
        # Sorry/apology surrender
        "sorry for the inconvenience",
        "sorry to have bothered",
        "sorry for bothering",
        "sorry to bother",
        "apologies for the",
        # Respect decision (surrender framing)
        "i respect your decision",
        "respect your wishes",
        "i understand your decision",
        "completely understand, take",
        "completely understand. take",
        # Goodbye / farewell
        "best of luck",
        "good luck with everything",
        "good luck to you",
        "all the best to you",
        "wish you all the best",
        "wishing you the best",
        "take care of yourself",
        "have a great rest of",
        # Concession + close
        "sounds like you're all set",
        "sounds like you are all set",
        "sounds like you're covered",
        "sounds like you are covered",
        "glad you're taken care of",
        "glad you are taken care of",
        "glad you have something",
        # No further contact
        "i'll stop texting",
        "ill stop texting",
        "i will stop texting",
        "i'll stop messaging",
        "ill stop messaging",
        "i will stop messaging",
        "won't text you again",
        "wont text you again",
        "will not text you again",
        "won't message you again",
        "wont message you again",
        "no more messages",
        "no further messages",
        # Final goodbye framing
        "this will be my last",
        "this is my last message",
        "last time reaching out",
        "final message",
    ]
    for phrase in SURRENDER_PHRASES:
        if phrase in lower:
            logger.warning(f"SURRENDER LANGUAGE BLOCKED: '{phrase}' found in reply — bot tried to give up")
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
