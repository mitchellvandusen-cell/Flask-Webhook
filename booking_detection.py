# booking_detection.py — Calendar booking intent detection
#
# Detects when a lead wants to book an appointment based on their message,
# recent conversation context, and conversation stage. Extracts time references
# and cross-references them against times the bot offered.

import logging
import re
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


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
        period = match.group(3).lower().replace(".", "")  # "p.m." -> "pm"
        if "pm" in period and h != 12:
            h += 12
        elif "am" in period and h == 12:
            h = 0

        # Look for day context near this match (within 30 chars)
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
            "hour": h,
            "minute": m,
            "original": match.group().strip(),
            "day_hint": day_hint
        })

    # Pass 2: Match times WITHOUT am/pm like "7:30", "at about 7:30"
    # Negative lookahead avoids re-matching times already caught with AM/PM
    bare_time_pattern = r'(\d{1,2}):(\d{2})(?!\s*(?:pm|p\.m\.|am|a\.m\.))'
    for match in re.finditer(bare_time_pattern, text_lower):
        h = int(match.group(1))
        m = int(match.group(2))

        # Infer AM/PM from business hours context
        # Insurance sales calls typically happen during business hours
        if 1 <= h <= 7:
            h += 12  # Assume PM (1:00-7:59 -> 13:00-19:59)
        elif h == 12:
            h = 12  # Noon
        # 8-11 stays as-is (morning hours)

        # Look for day context near this match (within 30 chars)
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
            "hour": h,
            "minute": m,
            "original": match.group().strip(),
            "day_hint": day_hint
        })

    return results


def _match_lead_time_to_bot_times(lead_msg: str, bot_msg: str) -> Optional[str]:
    """
    When the lead says a time like "2pm", "4", or "the 2 one", find the matching
    time from the bot's offered times and return a full time string WITH the day.

    This is critical: if bot said "Tuesday at 2pm or Wednesday at 10am" and lead
    says "2pm", we need to return "2:00 pm tuesday" so the booking code books
    the right date, not today.

    Returns a formatted time string like "4:00 pm tuesday" or None if no match.
    """
    if not lead_msg or not bot_msg:
        return None

    lead_lower = lead_msg.lower().strip()
    bot_times = _extract_times_from_text(bot_msg)

    if not bot_times:
        return None

    # --- Step 1: Parse the lead's time (with or without AM/PM) ---
    lead_times = _extract_times_from_text(lead_msg)
    lead_hour = None
    lead_minute = 0

    if lead_times:
        # Lead gave explicit time like "2pm" or "4:30 pm"
        lead_hour = lead_times[0]["hour"]
        lead_minute = lead_times[0]["minute"]
    else:
        # Try bare number like "4" or "the 2 one"
        bare_num_match = re.search(r'\b(\d{1,2})\b', lead_lower)
        if bare_num_match:
            lead_num = int(bare_num_match.group(1))
            # Infer AM/PM from bot's offered context
            if 1 <= lead_num <= 7:
                lead_hour = lead_num + 12  # assume PM for business hours
            elif 8 <= lead_num <= 11:
                lead_hour = lead_num  # assume AM
            elif lead_num == 12:
                lead_hour = 12  # noon
            else:
                lead_hour = lead_num

    if lead_hour is None:
        return None

    # --- Step 2: Match against bot's offered times ---
    for bt in bot_times:
        # Match hour: compare both 24h and 12h representations
        bot_hour_12 = bt["hour"] % 12 or 12
        lead_hour_12 = lead_hour % 12 or 12
        hour_match = (lead_hour == bt["hour"]) or (lead_hour_12 == bot_hour_12)
        minute_match = (lead_minute == bt["minute"]) or (lead_minute == 0 and bt["minute"] == 0)

        if hour_match and minute_match:
            period = "am" if bt["hour"] < 12 else "pm"
            minute_str = f":{bt['minute']:02d}" if bt["minute"] else ":00"
            day_part = f" {bt['day_hint']}" if bt.get("day_hint") else ""
            result = f"{bot_hour_12}{minute_str} {period}{day_part}"
            logger.info(f"📅 TIME MATCH: Lead said '{lead_msg}' -> matched to bot's '{bt['original']}' (day={bt.get('day_hint','none')}) -> booking '{result}'")
            return result

    # --- Step 3: Fuzzy match (within 30 min) ---
    for bt in bot_times:
        diff = abs(bt["hour"] * 60 + bt["minute"] - (lead_hour * 60 + lead_minute))
        if diff <= 30:
            bot_hour_12 = bt["hour"] % 12 or 12
            period = "am" if bt["hour"] < 12 else "pm"
            minute_str = f":{bt['minute']:02d}" if bt["minute"] else ":00"
            day_part = f" {bt['day_hint']}" if bt.get("day_hint") else ""
            result = f"{bot_hour_12}{minute_str} {period}{day_part}"
            logger.info(f"📅 TIME FUZZY MATCH: Lead said '{lead_msg}' -> close to bot's '{bt['original']}' -> booking '{result}'")
            return result

    # --- Step 4: No match but lead gave valid time — carry forward first bot day_hint ---
    if lead_times:
        # Lead said "2pm" but no bot time matched — still use bot's day context
        # if the lead didn't mention their own day
        lead_has_day = lead_times[0].get("day_hint", "")
        if not lead_has_day and bot_times:
            # Use the day_hint from the closest bot time
            first_day = bot_times[0].get("day_hint", "")
            if first_day:
                h12 = lead_hour % 12 or 12
                period = "am" if lead_hour < 12 else "pm"
                result = f"{h12}:{lead_minute:02d} {period} {first_day}"
                logger.info(f"📅 TIME DAY INHERIT: Lead said '{lead_msg}' -> no exact match, inheriting day '{first_day}' -> booking '{result}'")
                return result

    return None


def detect_booking_request(message: str, recent_exchanges: list, stage: str) -> Tuple[bool, Optional[str]]:
    """
    Context-aware booking detection.
    Returns (is_booking_request, extracted_time_string)

    Key insight: If bot just offered times and lead responds with ANY acceptance,
    that's a booking request even without explicit "book" keywords.
    """
    logger.info(f"🔍 BOOKING DETECTION START | message='{message}' | stage='{stage}' | exchanges_count={len(recent_exchanges)}")

    if not message:
        logger.warning("🚫 BOOKING DETECTION: No message provided")
        return False, None

    msg_lower = message.lower().strip()

    # === CONTEXT CHECK: Did bot just offer time slots? ===
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    last_bot_msg = bot_msgs[-1]['text'].lower() if bot_msgs else ""
    last_bot_msg_original = bot_msgs[-1]['text'] if bot_msgs else ""

    logger.debug(f"🔍 BOOKING CONTEXT | last_bot_msg_preview='{last_bot_msg[:100]}'...")

    # Detect if bot offered ACTUAL appointment times in last message
    # First: extract structured times (e.g., "2:00 pm", "9 am") - strongest signal
    bot_time_structs = _extract_times_from_text(last_bot_msg_original)

    # If structured times found, bot definitely offered times
    # If not, check for strong time-offering phrases (NOT loose words like "am", "does", "work")
    if bot_time_structs:
        bot_offered_times = True
    else:
        # Only match phrases that clearly indicate time slot offers
        strong_time_phrases = [
            "i've got", "how about", "works for you", "free at", "open at",
            "available at", "slot", "what time works", "when works",
            "schedule for", "book for", "set up for"
        ]
        # Also match day+time combos (e.g., "tomorrow morning", "friday afternoon")
        day_words = ["tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        time_words = ["morning", "afternoon", "evening"]
        has_day_time = any(d in last_bot_msg for d in day_words) and any(t in last_bot_msg for t in time_words)
        bot_offered_times = has_day_time or any(phrase in last_bot_msg for phrase in strong_time_phrases)

    # === EXPLICIT BOOKING KEYWORDS (works anytime) ===
    explicit_booking_keywords = [
        "book", "schedule", "set up", "setup", "appointment",
        "let's do", "lets do", "i'll take", "ill take",
        "sign me up", "put me down", "lock it in", "lock me in"
    ]
    has_explicit_intent = any(kw in msg_lower for kw in explicit_booking_keywords)

    # === TIME PATTERNS ===
    time_patterns = [
        r'\d{1,2}:\d{2}\s*(am|pm|a\.m\.|p\.m\.)?',  # 9:00 am, 2:30pm
        r'\d{1,2}\s*(am|pm|a\.m\.|p\.m\.)',          # 9am, 2pm
        r'\b\d{1,2}\b(?=\s|$|,|\.|!)',               # Just "2" or "9" (when context is clear)
        r'tomorrow',
        r'today',
        r'monday|tuesday|wednesday|thursday|friday|saturday|sunday',
        r'morning|afternoon|evening',
    ]

    time_match = None
    for pattern in time_patterns:
        match = re.search(pattern, msg_lower)
        if match:
            time_match = match.group()
            break

    has_time_reference = time_match is not None

    # === ACCEPTANCE PHRASES (only valid if bot offered times) ===
    # IMPORTANT: Use word-boundary regex to prevent substring false positives
    # e.g., "k" in "think" was causing false bookings on hostile messages
    acceptance_phrases = [
        r"\byes\b", r"\byeah\b", r"\byep\b", r"\byup\b", r"\bsure\b",
        r"\bok\b", r"\bokay\b", r"\bsounds good\b", r"\bperfect\b",
        r"\bgreat\b", r"\bthat works\b", r"\bworks for me\b",
        r"\bi can do\b", r"\bi'm free\b", r"\bim free\b", r"\bgood for me\b",
        r"\blet's do it\b", r"\blets do it\b", r"\bgo for it\b",
        r"\bfine\b", r"\bcool\b", r"\bbet\b", r"\balright\b"
    ]
    is_acceptance = any(re.search(phrase, msg_lower) for phrase in acceptance_phrases)

    # Additional guard: long messages with question marks are NOT simple acceptance
    # Real acceptance is short ("yes", "sounds good", "ok cool")
    is_hostile_or_question = len(msg_lower) > 60 or msg_lower.count("?") >= 1
    if is_acceptance and is_hostile_or_question:
        logger.info(f"🔍 ACCEPTANCE OVERRIDDEN: Message too long ({len(msg_lower)} chars) or has questions ({msg_lower.count('?')}q) | msg='{message[:80]}'")
        is_acceptance = False

    # Log detection signals
    logger.info(f"🔍 BOOKING SIGNALS | bot_offered_times={bot_offered_times} | has_explicit_intent={has_explicit_intent} | has_time_reference={has_time_reference} | is_acceptance={is_acceptance}")

    # === NEGATIVE TIME REJECTION ===
    # "I can't do 2pm" or "2pm doesn't work" should NOT trigger booking at that time.
    # The lead is declining, not accepting. Let the LLM handle the response naturally.
    rejection_phrases = [
        "can't do", "cant do", "cannot do",
        "won't work", "wont work", "will not work",
        "doesn't work", "doesnt work", "does not work",
        "not available", "not free", "unavailable",
        "can't make", "cant make", "cannot make",
        "won't be able", "wont be able",
        "busy then", "busy at",
        "no good", "not going to work", "not gonna work",
    ]
    has_rejection = any(phrase in msg_lower for phrase in rejection_phrases)

    if has_rejection and not is_acceptance and not has_explicit_intent:
        logger.info(f"🚫 BOOKING REJECTION: Lead declined time/availability | msg='{message[:50]}'")
        return False, None

    # === HELPER: Resolve time string using bot context ===
    def _resolve_time_with_context(lead_message: str, use_bot_first: bool = False) -> str:
        """
        Resolve a booking time string by cross-referencing lead's message with bot's offered times.
        If use_bot_first=True, extract the first time from bot's message (for simple acceptance).
        """
        if use_bot_first and bot_time_structs:
            # Simple acceptance: pick the first time the bot offered
            bt = bot_time_structs[0]
            h12 = bt["hour"] % 12 or 12
            period = "am" if bt["hour"] < 12 else "pm"
            day_part = f" {bt['day_hint']}" if bt.get("day_hint") else ""
            resolved = f"{h12}:{bt['minute']:02d} {period}{day_part}"
            logger.info(f"📅 RESOLVED (first offered): '{resolved}' from bot times")
            return resolved

        # Try to match lead's bare number against bot's offered times
        if bot_time_structs:
            matched = _match_lead_time_to_bot_times(lead_message, last_bot_msg_original)
            if matched:
                return matched

        # Fallback: return the lead's message as-is for ghl_calendar to parse
        return lead_message

    # === DECISION LOGIC ===

    # Case 0: Lead provides BOTH a specific day AND a specific time unprompted
    # e.g., "Next week tues at 11:00am please", "Thursday at 2pm", "Monday 9:30 am"
    # This is a strong booking signal regardless of what the bot offered.
    day_name_pattern = (
        r'\b(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday'
        r'|mon|tues|tue|wed|thurs|thu|fri|sat|sun)\b'
    )
    # Also match ordinal dates like "the 17th", "march 17", "on the 20th"
    ordinal_date_pattern = r'\b(?:the\s+)?\d{1,2}(?:st|nd|rd|th)\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}\b'
    has_day_reference = bool(re.search(day_name_pattern, msg_lower)) or bool(re.search(ordinal_date_pattern, msg_lower))

    if has_day_reference and has_time_reference:
        resolved = _resolve_time_with_context(message)
        logger.info(f"BOOKING CASE 0: Day + Time reference (unprompted) | msg='{message[:80]}' | resolved='{resolved}'")
        return True, resolved

    # Case 1: Explicit booking request with time (always book)
    if has_explicit_intent and has_time_reference:
        resolved = _resolve_time_with_context(message)
        logger.info(f"BOOKING CASE 1: Explicit + Time | msg='{message[:50]}' | resolved='{resolved}'")
        return True, resolved

    # Case 2: Bot offered times + lead mentions time reference
    if bot_offered_times and has_time_reference:
        resolved = _resolve_time_with_context(message)
        logger.info(f"BOOKING CASE 2: Bot offered + Time reference | msg='{message[:50]}' | resolved='{resolved}'")
        return True, resolved

    # Case 3: Bot offered times + simple acceptance (grab FIRST time from bot's msg)
    # GUARD: Only trigger if bot actually had PARSEABLE times (not just vague phrases)
    # AND the lead's message is genuinely a short acceptance (not a hostile question)
    if bot_offered_times and is_acceptance and not has_time_reference:
        if not bot_time_structs:
            logger.info(f"🚫 BOOKING CASE 3 SKIPPED: bot_offered_times=True but no parseable times in bot msg | msg='{message[:60]}'")
        else:
            resolved = _resolve_time_with_context(message, use_bot_first=True)
            logger.info(f"BOOKING CASE 3: Bot offered + Simple acceptance | resolved='{resolved}'")
            return True, resolved

    # Case 4: Stage is BOOKING + acceptance — ONLY if bot offered specific times recently
    # This prevents random messages from triggering bookings just because stage is "booking"
    # e.g., "Back to work I go" should never book just because conversation was in booking stage
    if stage == "booking" and is_acceptance and bot_offered_times:
        if has_time_reference:
            resolved = _resolve_time_with_context(message)
        else:
            resolved = _resolve_time_with_context(message, use_bot_first=True)
        logger.info(f"BOOKING CASE 4: Booking stage + Bot offered times + Acceptance | resolved='{resolved}'")
        return True, resolved

    # Case 5: Explicit "that time works" / "works for me"
    time_acceptance_phrases = ["that time", "that works", "works for me", "good time", "that's fine"]
    if bot_offered_times and any(phrase in msg_lower for phrase in time_acceptance_phrases):
        resolved = _resolve_time_with_context(message, use_bot_first=True)
        logger.info(f"BOOKING CASE 5: Time acceptance phrase | resolved='{resolved}'")
        return True, resolved

    logger.info(f"🚫 BOOKING DETECTION: No cases matched | msg='{message}'")
    logger.debug(f"   Reasons: bot_offered={bot_offered_times}, explicit={has_explicit_intent}, time_ref={has_time_reference}, acceptance={is_acceptance}, stage={stage}")
    return False, None
