# booking_detection.py — Enterprise Calendar Booking Detection (LLM-Powered)
#
# Architecture:
#   Tier 1: Fast regex pre-filter — eliminates ~90% of non-booking messages (no LLM cost)
#   Tier 2: LLM micro-prompt (grok-4-1-fast-reasoning) — precise intent classification + time extraction
#   Tier 3: Regex fallback — if LLM unavailable (API down, no key, timeout)
#
# Why LLM instead of regex:
#   Regex failed on "Next week tues at 11:00am please" because the bot hadn't offered
#   specific times. Regex can't understand conversational context. An LLM reads the
#   conversation and naturally understands "I want to meet Tuesday at 11" is a booking
#   request regardless of what the bot said.
#
# Cost: ~$0.001 per classification (~200 input tokens, ~60 output tokens)
# Latency: ~500-1000ms (only when pre-filter passes, ~10-20% of messages)
# Model: grok-4-1-fast-reasoning

import dataclasses
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# xAI client (same pattern as lead_intelligence.py)
XAI_API_KEY = os.getenv("XAI_API_KEY")
_XAI_KEY_BOOKING = os.getenv("XAI_API_KEY_BOOKING") or XAI_API_KEY
BOOKING_MODEL = "grok-4-1-fast-reasoning"

_client = None
if _XAI_KEY_BOOKING:
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=_XAI_KEY_BOOKING, base_url="https://api.x.ai/v1")
    except Exception:
        pass


@dataclasses.dataclass
class BookingDetectionResult:
    """Result of booking intent detection."""
    action: str  # "book" | "offer_slots" | "none"
    time_string: Optional[str] = None  # Parseable time for ghl_calendar.py (e.g., "11:00 am tuesday march 17")
    reason: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# TIER 1: FAST PRE-FILTER (regex, ~0ms)
# ═══════════════════════════════════════════════════════════════════════════════

def _has_scheduling_signals(message: str, recent_exchanges: list, stage: str) -> bool:
    """
    Fast pre-filter. Returns True if the message MIGHT be booking-related.
    High recall, but requires scheduling CONTEXT — bare times alone ("I work at 2pm")
    don't trigger without a booking keyword or bot-offered times in the conversation.
    False positives here cost one cheap LLM call (~$0.001).
    """
    if not message:
        return False

    msg = message.lower()

    # Stage is already booking/booked — always check
    if stage in ("booking", "booked"):
        return True

    # Check if bot's last message had time offers — any response could be acceptance
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    bot_offered_times = False
    if bot_msgs:
        last_bot = bot_msgs[-1]['text'].lower()
        bot_offered_times = bool(re.search(r'\d{1,2}\s*(?:am|pm)', last_bot)) or \
                            bool(re.search(r'\d{1,2}:\d{2}', last_bot))
        if bot_offered_times:
            # Bot offered times — even simple acceptance words should trigger
            acceptance_words = [
                "yes", "yeah", "sure", "ok", "okay", "yep", "yup",
                "perfect", "great", "fine", "cool", "bet", "alright",
                "please", "absolutely", "definitely",
            ]
            if any(re.search(r'\b' + re.escape(w) + r'\b', msg) for w in acceptance_words):
                return True

    # Explicit booking intent keywords — always trigger
    booking_intent_keywords = [
        "book", "schedule", "appointment", "calendar",
        "slot", "set up", "sign me up", "lock it in", "lock me in",
        "let's do", "lets do", "put me down",
        "what time", "what day", "when can", "when do", "when are",
        "are you free", "are you available",
        "works for me", "sounds good", "that works",
        "o'clock", "oclock",
    ]
    has_booking_intent = any(w in msg for w in booking_intent_keywords)

    if has_booking_intent:
        return True

    # Day/time words that imply scheduling — but ONLY when combined with each other
    # or with booking intent. "Tomorrow" alone or "2pm" alone is NOT enough.
    day_words = [
        "tomorrow", "today",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "mon ", "tues", "tue ", "wed ", "thurs", "thu ", "fri ", "sat ", "sun ",
        "next week", "this week",
        "morning", "afternoon", "evening",
    ]
    has_day_ref = any(w in msg for w in day_words)
    has_time_pattern = bool(re.search(r'\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)\b', msg)) or \
                       bool(re.search(r'\d{1,2}:\d{2}', msg))
    has_ordinal_date = bool(re.search(r'\d{1,2}(?:st|nd|rd|th)\b', msg))

    # Day + Time together = strong scheduling signal (e.g., "Tuesday at 2pm")
    if has_day_ref and has_time_pattern:
        return True

    # Day + ordinal = strong scheduling signal (e.g., "the 17th")
    if has_day_ref and has_ordinal_date:
        return True

    # Time pattern + available/meet/free = scheduling context
    scheduling_context = ["available", "meet", "free", "open"]
    if has_time_pattern and any(w in msg for w in scheduling_context):
        return True

    # Bot offered times earlier in conversation (not just last message) — time pattern triggers
    if bot_offered_times and (has_time_pattern or has_day_ref or has_ordinal_date):
        return True

    # Ordinal date with month name (e.g., "march 17th") — always scheduling
    if has_ordinal_date and re.search(r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', msg):
        return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# TIER 2: LLM CLASSIFICATION (grok-4-1-fast-reasoning, ~$0.001/call)
# ═══════════════════════════════════════════════════════════════════════════════

def _format_exchanges_for_llm(recent_exchanges: list, message: str, max_exchanges: int = 6) -> str:
    """Format recent conversation for the LLM prompt."""
    lines = []
    recent = recent_exchanges[-max_exchanges:] if len(recent_exchanges) > max_exchanges else recent_exchanges
    for ex in recent:
        role = "Agent" if ex['role'] == 'assistant' else "Customer"
        lines.append(f"{role}: {ex['text']}")
    lines.append(f"Customer: {message}")
    return "\n".join(lines)


def _llm_classify_booking(message: str, recent_exchanges: list, timezone: str) -> Optional[BookingDetectionResult]:
    """
    Tier 2: Use LLM to classify booking intent with full conversational context.
    Returns BookingDetectionResult or None (signal to fall back to regex).
    """
    if not _client:
        logger.warning("⚠️ BOOKING LLM: No xAI client — falling back to regex")
        return None

    try:
        now = datetime.now(ZoneInfo(timezone))
    except Exception:
        now = datetime.now()
    today_str = now.strftime("%A, %B %d, %Y")

    conversation = _format_exchanges_for_llm(recent_exchanges, message)

    prompt = f"""You are a booking intent classifier for an insurance agent's AI texting bot.

TODAY: {today_str}
TIMEZONE: {timezone}

CONVERSATION:
{conversation}

Analyze the customer's LATEST message. Return ONLY valid JSON:
{{"action": "book" | "offer_slots" | "none", "time_string": "<date and time>" or null, "reason": "<one sentence>"}}

DEFINITIONS:
- "book": Customer gave a specific DAY/DATE **AND** TIME for an appointment.
  Examples: "Tuesday at 2pm", "tomorrow 11am", "the 17th at 9:30", "next week wednesday at 4".
  ALSO "book" when the agent offered specific times and the customer clearly accepted:
  - If agent offered ONE time and customer said "yes"/"sure"/"ok" → book that time
  - If customer said a specific time like "4pm" or "the second one" → book that time
  - If agent offered MULTIPLE times and customer said "yes" without specifying which → use "offer_slots" to clarify
- "offer_slots": Customer wants to schedule but didn't give both day AND time.
  Examples: "sometime next week", "Thursday works", "what times do you have?", "when are you free?", "afternoon works".
  Also use when it's ambiguous which offered time the customer accepted.
- "offer_slots" ALSO when: Customer REJECTED the offered times ("none of those work", "can't do either of those",
  "those don't work for me") — use offer_slots so the system fetches new availability.
  Also when customer gives multiple options ("I can do 2pm or 4pm") — they are offering choices, not confirming one.
- "none": Not about scheduling at all. Actively declining. General conversation.
  Examples: "how much is it?", "I have 2 kids", "not interested", "I work until 5", "I need to think about it",
  "let me check my schedule and get back to you".

TIME STRING FORMAT (only for "book"):
Include day AND time: "11:00 am tuesday" or "2:00 pm march 17" or "9:30 am tomorrow".
If customer accepted an agent-offered time, extract THAT time from the agent's message.
NEVER guess a time. If unsure of the exact time, use "offer_slots"."""

    try:
        resp = _client.chat.completions.create(
            model=BOOKING_MODEL,
            messages=[
                {"role": "system", "content": "Return ONLY valid JSON. No markdown, no explanation."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=150,
            timeout=8,
        )

        raw = (resp.choices[0].message.content or "").strip()
        # Strip reasoning model artifacts (<thinking> tags)
        raw = re.sub(r'<thinking>[\s\S]*?</thinking>', '', raw).strip()
        raw = re.sub(r'</?(?:thinking|reply|output|response)>', '', raw).strip()
        # Strip markdown code fences
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)

        result = json.loads(raw)
        action = result.get("action", "none")
        time_string = result.get("time_string")
        reason = result.get("reason", "")

        # Validate action
        if action not in ("book", "offer_slots", "none"):
            logger.warning(f"⚠️ BOOKING LLM: Invalid action '{action}' — defaulting to none")
            action = "none"

        # Safety: "book" without a time string → downgrade to offer_slots
        if action == "book" and not time_string:
            logger.warning("⚠️ BOOKING LLM: action=book but no time_string — downgrading to offer_slots")
            action = "offer_slots"

        logger.info(f"📅 BOOKING LLM | action={action} | time={time_string} | reason={reason} | msg='{message[:60]}'")
        return BookingDetectionResult(action=action, time_string=time_string, reason=reason)

    except json.JSONDecodeError as e:
        logger.warning(f"⚠️ BOOKING LLM: JSON parse error: {e}")
        return None  # Fall back to regex
    except Exception as e:
        logger.warning(f"⚠️ BOOKING LLM: API error: {e}")
        return None  # Fall back to regex


# ═══════════════════════════════════════════════════════════════════════════════
# TIER 3: REGEX FALLBACK (if LLM unavailable)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_time_portion(message: str) -> Optional[str]:
    """
    Extract only the scheduling-relevant portion from a conversational message.
    E.g., "yeah that works lets do tuesday at 11" → "tuesday at 11"
    Returns None if no time-relevant content found.
    """
    msg = message.lower().strip()
    # Pattern: day reference + optional "at" + time
    day_time = re.search(
        r'((?:next\s+(?:week\s+)?)?'
        r'(?:tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday'
        r'|mon|tues?|wed|thurs?|fri|sat|sun)'
        r'(?:\s+(?:the\s+)?\d{1,2}(?:st|nd|rd|th)?)?'  # optional ordinal
        r'(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)?)',
        msg
    )
    # Pattern: time reference like "11:00 am", "2pm"
    time_only = re.search(
        r'(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.))',
        msg
    )
    # Pattern: month + day like "march 17"
    month_day = re.search(
        r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2})',
        msg
    )

    parts = []
    if day_time:
        parts.append(day_time.group(1).strip())
    if month_day and (not day_time or month_day.group(1) not in day_time.group(1)):
        parts.append(month_day.group(1).strip())
    if time_only and not any(time_only.group(1) in p for p in parts):
        parts.append(time_only.group(1).strip())

    return " ".join(parts) if parts else None


def _extract_times_from_text(text: str) -> list:
    """
    Extract all time references from a text string.
    Returns list of dicts: [{"hour": 14, "minute": 0, "original": "2:00 PM", "day_hint": "tomorrow"}, ...]
    """
    results = []
    if not text:
        return results

    text_lower = text.lower()

    # Match times with am/pm like "9:00 am", "4:30pm", "2 pm", "10am"
    time_pattern = r'(\d{1,2}):?(\d{2})?\s*(pm|p\.m\.|am|a\.m\.)'
    for match in re.finditer(time_pattern, text_lower):
        h = int(match.group(1))
        m = int(match.group(2) or 0)
        period = match.group(3).lower().replace(".", "")
        if "pm" in period and h != 12:
            h += 12
        elif "am" in period and h == 12:
            h = 0

        context_start = max(0, match.start() - 30)
        context_end = min(len(text_lower), match.end() + 30)
        context = text_lower[context_start:context_end]
        day_hint = ""
        if "tomorrow" in context:
            day_hint = "tomorrow"
        for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
            if day in context:
                day_hint = day
                break

        results.append({
            "hour": h, "minute": m,
            "original": match.group().strip(), "day_hint": day_hint
        })

    # Pass 2: bare times like "7:30" without AM/PM
    bare_time_pattern = r'(\d{1,2}):(\d{2})(?!\s*(?:pm|p\.m\.|am|a\.m\.))'
    for match in re.finditer(bare_time_pattern, text_lower):
        h = int(match.group(1))
        m = int(match.group(2))
        if 1 <= h <= 7:
            h += 12
        elif h == 12:
            h = 12

        context_start = max(0, match.start() - 30)
        context_end = min(len(text_lower), match.end() + 30)
        context = text_lower[context_start:context_end]
        day_hint = ""
        if "tomorrow" in context:
            day_hint = "tomorrow"
        for day in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
            if day in context:
                day_hint = day
                break

        results.append({
            "hour": h, "minute": m,
            "original": match.group().strip(), "day_hint": day_hint
        })

    return results


def _match_lead_time_to_bot_times(lead_msg: str, bot_msg: str) -> Optional[str]:
    """Cross-reference lead's time against bot's offered times to get full time+day string."""
    if not lead_msg or not bot_msg:
        return None

    lead_lower = lead_msg.lower().strip()
    bot_times = _extract_times_from_text(bot_msg)
    if not bot_times:
        return None

    lead_times = _extract_times_from_text(lead_msg)
    lead_hour = None
    lead_minute = 0

    if lead_times:
        lead_hour = lead_times[0]["hour"]
        lead_minute = lead_times[0]["minute"]
    else:
        bare_num_match = re.search(r'\b(\d{1,2})\b', lead_lower)
        if bare_num_match:
            lead_num = int(bare_num_match.group(1))
            if 1 <= lead_num <= 7:
                lead_hour = lead_num + 12
            elif 8 <= lead_num <= 11:
                lead_hour = lead_num
            elif lead_num == 12:
                lead_hour = 12
            else:
                lead_hour = lead_num

    if lead_hour is None:
        return None

    # Exact match
    for bt in bot_times:
        bot_hour_12 = bt["hour"] % 12 or 12
        lead_hour_12 = lead_hour % 12 or 12
        hour_match = (lead_hour == bt["hour"]) or (lead_hour_12 == bot_hour_12)
        minute_match = (lead_minute == bt["minute"]) or (lead_minute == 0 and bt["minute"] == 0)
        if hour_match and minute_match:
            period = "am" if bt["hour"] < 12 else "pm"
            minute_str = f":{bt['minute']:02d}" if bt["minute"] else ":00"
            day_part = f" {bt['day_hint']}" if bt.get("day_hint") else ""
            return f"{bot_hour_12}{minute_str} {period}{day_part}"

    # Fuzzy match (within 30 min)
    for bt in bot_times:
        diff = abs(bt["hour"] * 60 + bt["minute"] - (lead_hour * 60 + lead_minute))
        if diff <= 30:
            bot_hour_12 = bt["hour"] % 12 or 12
            period = "am" if bt["hour"] < 12 else "pm"
            minute_str = f":{bt['minute']:02d}" if bt["minute"] else ":00"
            day_part = f" {bt['day_hint']}" if bt.get("day_hint") else ""
            return f"{bot_hour_12}{minute_str} {period}{day_part}"

    # Day inherit: lead gave time, bot gave day context
    if lead_times:
        lead_has_day = lead_times[0].get("day_hint", "")
        if not lead_has_day and bot_times:
            first_day = bot_times[0].get("day_hint", "")
            if first_day:
                h12 = lead_hour % 12 or 12
                period = "am" if lead_hour < 12 else "pm"
                return f"{h12}:{lead_minute:02d} {period} {first_day}"

    return None


def _regex_fallback(message: str, recent_exchanges: list, stage: str) -> BookingDetectionResult:
    """
    Tier 3: Regex-based fallback when LLM is unavailable.
    Preserves the original detection logic for graceful degradation.
    """
    msg_lower = message.lower().strip()

    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    last_bot_msg = bot_msgs[-1]['text'].lower() if bot_msgs else ""
    last_bot_msg_original = bot_msgs[-1]['text'] if bot_msgs else ""

    bot_time_structs = _extract_times_from_text(last_bot_msg_original)
    if bot_time_structs:
        bot_offered_times = True
    else:
        strong_time_phrases = [
            "i've got", "how about", "works for you", "free at", "open at",
            "available at", "slot", "what time works", "when works",
            "schedule for", "book for", "set up for"
        ]
        day_words = ["tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        time_words = ["morning", "afternoon", "evening"]
        has_day_time = any(d in last_bot_msg for d in day_words) and any(t in last_bot_msg for t in time_words)
        bot_offered_times = has_day_time or any(phrase in last_bot_msg for phrase in strong_time_phrases)

    explicit_booking_keywords = [
        "book", "schedule", "set up", "setup", "appointment",
        "let's do", "lets do", "i'll take", "ill take",
        "sign me up", "put me down", "lock it in", "lock me in"
    ]
    has_explicit_intent = any(kw in msg_lower for kw in explicit_booking_keywords)

    time_patterns = [
        r'\d{1,2}:\d{2}\s*(am|pm|a\.m\.|p\.m\.)?',
        r'\d{1,2}\s*(am|pm|a\.m\.|p\.m\.)',
        r'\b\d{1,2}\b(?=\s|$|,|\.|!)',
        r'tomorrow', r'today',
        r'monday|tuesday|wednesday|thursday|friday|saturday|sunday',
        r'morning|afternoon|evening',
    ]
    has_time_reference = any(re.search(p, msg_lower) for p in time_patterns)

    acceptance_phrases = [
        r"\byes\b", r"\byeah\b", r"\byep\b", r"\byup\b", r"\bsure\b",
        r"\bok\b", r"\bokay\b", r"\bsounds good\b", r"\bperfect\b",
        r"\bgreat\b", r"\bthat works\b", r"\bworks for me\b",
        r"\bi can do\b", r"\bi'm free\b", r"\bim free\b", r"\bgood for me\b",
        r"\blet's do it\b", r"\blets do it\b", r"\bgo for it\b",
        r"\bfine\b", r"\bcool\b", r"\bbet\b", r"\balright\b"
    ]
    is_acceptance = any(re.search(phrase, msg_lower) for phrase in acceptance_phrases)
    if is_acceptance and (len(msg_lower) > 60 or msg_lower.count("?") >= 1):
        is_acceptance = False

    rejection_phrases = [
        "can't do", "cant do", "cannot do", "won't work", "wont work",
        "doesn't work", "doesnt work", "does not work",
        "not available", "not free", "unavailable",
        "can't make", "cant make", "cannot make",
        "busy then", "busy at", "no good", "not going to work",
    ]
    if any(phrase in msg_lower for phrase in rejection_phrases) and not is_acceptance and not has_explicit_intent:
        return BookingDetectionResult(action="none", reason="Rejection detected (regex)")

    def _resolve(use_bot_first=False):
        if use_bot_first and bot_time_structs:
            bt = bot_time_structs[0]
            h12 = bt["hour"] % 12 or 12
            period = "am" if bt["hour"] < 12 else "pm"
            day_part = f" {bt['day_hint']}" if bt.get("day_hint") else ""
            return f"{h12}:{bt['minute']:02d} {period}{day_part}"
        if bot_time_structs:
            matched = _match_lead_time_to_bot_times(message, last_bot_msg_original)
            if matched:
                return matched
        # Extract only the time-relevant portion from the message, not the whole thing.
        # This prevents ghl_calendar.py from receiving conversational text like
        # "yeah that works lets do tuesday at 11" as a time_string.
        time_extract = _extract_time_portion(message)
        return time_extract if time_extract else message

    # Day + Time unprompted
    day_name_pattern = (
        r'\b(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday'
        r'|mon|tues|tue|wed|thurs|thu|fri|sat|sun)\b'
    )
    ordinal_date_pattern = r'\b(?:the\s+)?\d{1,2}(?:st|nd|rd|th)\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}\b'
    has_day_ref = bool(re.search(day_name_pattern, msg_lower)) or bool(re.search(ordinal_date_pattern, msg_lower))

    if has_day_ref and has_time_reference:
        return BookingDetectionResult(action="book", time_string=_resolve(), reason="Day+Time (regex fallback)")

    if has_explicit_intent and has_time_reference:
        return BookingDetectionResult(action="book", time_string=_resolve(), reason="Explicit+Time (regex fallback)")

    if bot_offered_times and has_time_reference:
        return BookingDetectionResult(action="book", time_string=_resolve(), reason="Bot offered+Time (regex fallback)")

    if bot_offered_times and is_acceptance and bot_time_structs:
        return BookingDetectionResult(action="book", time_string=_resolve(use_bot_first=True), reason="Bot offered+Acceptance (regex fallback)")

    if stage == "booking" and is_acceptance and bot_offered_times:
        ts = _resolve() if has_time_reference else _resolve(use_bot_first=True)
        return BookingDetectionResult(action="book", time_string=ts, reason="Stage booking+Acceptance (regex fallback)")

    time_accept = ["that time", "that works", "works for me", "good time", "that's fine"]
    if bot_offered_times and any(phrase in msg_lower for phrase in time_accept):
        return BookingDetectionResult(action="book", time_string=_resolve(use_bot_first=True), reason="Time acceptance (regex fallback)")

    # Check for offer_slots signals (wants to schedule but no specific time)
    scheduling_interest = [
        "what times", "when are you", "when do you", "when can we",
        "what days", "are you available", "are you free",
        "sometime", "let me check", "next week", "this week",
    ]
    if any(phrase in msg_lower for phrase in scheduling_interest):
        return BookingDetectionResult(action="offer_slots", reason="Scheduling interest (regex fallback)")

    if has_explicit_intent and not has_time_reference:
        return BookingDetectionResult(action="offer_slots", reason="Explicit intent, no time (regex fallback)")

    return BookingDetectionResult(action="none", reason="No booking signals (regex fallback)")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def detect_booking_request(
    message: str,
    recent_exchanges: list,
    stage: str,
    timezone: str = "America/Chicago",
) -> BookingDetectionResult:
    """
    Enterprise booking detection with 3-tier architecture.

    Returns BookingDetectionResult:
      action="book"        → Execute booking with time_string
      action="offer_slots" → Fetch and offer calendar slots
      action="none"        → Not a booking request
    """
    if not message:
        return BookingDetectionResult(action="none", reason="No message")

    logger.info(f"📅 BOOKING DETECT START | msg='{message[:80]}' | stage={stage}")

    # Tier 1: Fast pre-filter
    if not _has_scheduling_signals(message, recent_exchanges, stage):
        logger.info(f"📅 BOOKING PREFILTER: No scheduling signals | msg='{message[:60]}'")
        return BookingDetectionResult(action="none", reason="No scheduling signals")

    logger.info(f"📅 BOOKING PREFILTER PASSED — sending to LLM | msg='{message[:60]}'")

    # Tier 2: LLM classification
    llm_result = _llm_classify_booking(message, recent_exchanges, timezone)
    if llm_result is not None:
        return llm_result

    # Tier 3: Regex fallback (LLM unavailable)
    logger.info("📅 BOOKING REGEX FALLBACK (LLM unavailable)")
    return _regex_fallback(message, recent_exchanges, stage)
