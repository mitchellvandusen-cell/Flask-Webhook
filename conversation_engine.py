# conversation_engine.py - Conversational Logic with Objection Handling
# Updated Feb 2026: Message context detection, objection classification, cycling framework

import os
import logging
import re
import json
from enum import Enum
from dataclasses import dataclass
from typing import List, Dict, Tuple

from openai import OpenAI

logger = logging.getLogger(__name__)

# === API Client ===
XAI_API_KEY = os.getenv("XAI_API_KEY")
client = None
if XAI_API_KEY:
    client = OpenAI(
        api_key=XAI_API_KEY,
        base_url="https://api.x.ai/v1"
    )

# ===================================
# ENUMS
# ===================================

class ConversationStage(Enum):
    INITIAL_OUTREACH    = "initial_outreach"
    QUALIFYING          = "qualifying"
    OBJECTION_HANDLING  = "objection_handling"
    BOOKING             = "booking"
    BOOKED              = "booked"

class MessageContext(Enum):
    COLD_OUTBOUND       = "cold_outbound"        # First ever contact, no conversation history
    FOLLOW_UP_NO_REPLY  = "follow_up_no_reply"   # Nth outbound attempt, lead hasn't responded
    INBOUND_REPLY       = "inbound_reply"         # Lead actually sent a message

class ObjectionType(Enum):
    NONE                = "none"
    NOT_INTERESTED      = "not_interested"        # "no thanks", "not interested", "pass"
    SPOUSE_PARTNER      = "spouse_partner"         # "need to talk to my wife", "check with husband"
    PRICE_MONEY         = "price_money"            # "too expensive", "can't afford it"
    ALREADY_COVERED     = "already_covered"        # "already have life insurance", "I'm covered"
    BUSY_TIMING         = "busy_timing"            # "busy right now", "call back later"

class ObjectionNature(Enum):
    NONE                = "none"
    FEAR_BASED          = "fear_based"             # Emotional resistance, avoidance, uncertainty
    LOGISTICAL          = "logistical"             # Practical concern: money movement, scheduling, existing arrangements


# ===================================
# DATA
# ===================================

class BuyingSignalType(Enum):
    NONE                = "none"
    ASKING_PRICE        = "asking_price"         # "how much", "what does it cost", "price"
    ASKING_OPTIONS      = "asking_options"        # "what are my options", "what's available"
    ASKING_DETAILS      = "asking_details"        # "how does that work", "what's the difference"
    REQUESTING_COVERAGE = "requesting_coverage"   # "I want 7000", "I need 50k", "looking for term"
    COMPARING           = "comparing"             # "which is better", "term vs whole life"
    READY_SIGNAL        = "ready_signal"          # "let's do it", "sign me up", "what's next"


class ProductType(Enum):
    UNKNOWN         = "unknown"
    TERM            = "term"
    WHOLE_LIFE      = "whole_life"
    IUL             = "iul"
    FINAL_EXPENSE   = "final_expense"
    GROUP_EMPLOYER  = "group_employer"
    GUARANTEED_ISSUE = "guaranteed_issue"


@dataclass
class InsuranceContext:
    """Structured insurance product analysis based on what the lead said."""
    requested_amount: int = 0              # Dollar amount they mentioned (e.g. 7000)
    requested_product: ProductType = ProductType.UNKNOWN
    lead_age: int = 0                      # Age if known
    amount_below_minimum: bool = False     # True if requested amount < product minimum
    product_mismatch: bool = False         # True if product doesn't fit their situation
    needs_clarification: bool = False      # True if bot should ask before assuming
    guidance_note: str = ""                # Specific expert guidance for the bot


@dataclass
class LogicSignal:
    stage: ConversationStage
    message_context: MessageContext
    has_coverage: bool
    needs_coverage: bool
    mentioned_goal: bool
    mentioned_obstacle: bool
    ready_to_book: bool
    resistance: bool
    conversation_count: int
    objection_type: ObjectionType
    objection_nature: ObjectionNature
    consecutive_bot_messages: int
    articulated_impact: bool
    buying_signal: BuyingSignalType = BuyingSignalType.NONE
    insurance_context: InsuranceContext = None
    too_deep_for_text: bool = False        # True = stop texting details, book appointment


# ===================================
# HELPERS
# ===================================

def _has_word(text: str, keyword: str) -> bool:
    """Word-boundary safe keyword match."""
    return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text, re.IGNORECASE))


def determine_message_context(message: str, messages: List[Dict[str, str]]) -> Tuple[MessageContext, int]:
    """
    Determine the conversation context based on whether there's an inbound message
    and what the history looks like.

    Returns (context, consecutive_bot_message_count).

    Three situations:
    1. COLD_OUTBOUND: No inbound message AND no conversation history (or no bot messages sent yet).
       This is the very first contact attempt.
    2. FOLLOW_UP_NO_REPLY: No inbound message BUT conversation history exists with bot messages.
       The lead hasn't responded to previous outreach.
    3. INBOUND_REPLY: There IS an inbound message. The lead actually said something.
    """
    has_message = bool(message and message.strip())

    if has_message:
        return MessageContext.INBOUND_REPLY, 0

    if not messages:
        return MessageContext.COLD_OUTBOUND, 0

    # Count consecutive bot messages at end of history (no lead response between them)
    consecutive = 0
    for msg in reversed(messages):
        if msg['role'] == 'assistant':
            consecutive += 1
        else:
            break

    # If bot has never messaged, this is still a cold outbound
    has_any_bot = any(m['role'] == 'assistant' for m in messages)
    if not has_any_bot:
        return MessageContext.COLD_OUTBOUND, 0

    return MessageContext.FOLLOW_UP_NO_REPLY, consecutive


def detect_objection_keywords(text: str) -> Tuple[ObjectionType, ObjectionNature]:
    """
    Keyword-based fallback for objection detection when LLM is unavailable.
    Checks the most recent lead text for common objection patterns.
    """
    if not text:
        return ObjectionType.NONE, ObjectionNature.NONE

    text_lower = text.lower().strip()

    # Order matters: check most specific patterns first

    not_interested_kw = [
        "not interested", "no thanks", "no thank you", "dont want", "don't want",
        "nah im good", "nah i'm good", "no i'm good", "no im good",
        "don't need", "dont need", "not for me",
        "not looking", "no need", "i'll pass", "ill pass",
    ]

    # "i'm good" / "im good" are ONLY objections when they are the entire message
    # or clearly dismissive (short message, no positive context).
    # "yeah im good with 3pm" or "im good to go" are NOT objections.
    im_good_patterns = ["i'm good", "im good"]
    positive_context = ["good with", "good to", "good for", "good on that", "good let"]
    if any(p in text_lower for p in im_good_patterns):
        has_positive = any(p in text_lower for p in positive_context)
        is_short = len(text_lower.split()) <= 4
        if not has_positive and is_short:
            not_interested_kw.append(text_lower)  # match itself so the check below hits

    spouse_kw = [
        "talk to my wife", "talk to my husband", "ask my spouse", "ask my wife",
        "ask my husband", "check with my", "run it by my", "discuss with my",
        "talk to my partner", "other half", "talk it over", "spouse"
    ]

    price_kw = [
        "too expensive", "can't afford", "cant afford", "too much money",
        "don't have the money", "out of my budget", "pricey", "costly",
        "not in my budget", "too much", "how much does"
    ]

    already_kw = [
        "already have", "already got", "i'm covered", "im covered",
        "have insurance", "have a policy", "taken care of", "all set",
        "set with insurance", "good on insurance", "squared away",
        "have something through", "through my job", "through work",
        "employer", "work provides"
    ]

    busy_kw = [
        "busy right now", "call back later", "another time", "not a good time",
        "bad time", "not now", "maybe later", "call me back", "reach out later",
        "try again later", "not the best time", "in a meeting", "at work",
        "can you call back", "text me later"
    ]

    if any(kw in text_lower for kw in not_interested_kw):
        return ObjectionType.NOT_INTERESTED, ObjectionNature.FEAR_BASED

    if any(kw in text_lower for kw in spouse_kw):
        return ObjectionType.SPOUSE_PARTNER, ObjectionNature.FEAR_BASED

    if any(kw in text_lower for kw in price_kw):
        return ObjectionType.PRICE_MONEY, ObjectionNature.LOGISTICAL

    if any(kw in text_lower for kw in already_kw):
        return ObjectionType.ALREADY_COVERED, ObjectionNature.LOGISTICAL

    if any(kw in text_lower for kw in busy_kw):
        return ObjectionType.BUSY_TIMING, ObjectionNature.FEAR_BASED

    return ObjectionType.NONE, ObjectionNature.NONE


# ===================================
# BUYING SIGNAL DETECTION
# ===================================

def detect_buying_signal(text: str) -> BuyingSignalType:
    """Detect buying signals from lead messages. These indicate interest and should push toward booking."""
    if not text:
        return BuyingSignalType.NONE
    t = text.lower()

    ready_kw = [
        "sign me up", "let's do it", "lets do it", "i'm ready", "im ready",
        "what's next", "whats next", "how do i start", "let's get started",
        "i want to move forward", "ready to go"
    ]
    if any(kw in t for kw in ready_kw):
        return BuyingSignalType.READY_SIGNAL

    price_kw = [
        "how much", "what does it cost", "what would it cost", "price",
        "premium", "monthly payment", "per month", "a month",
        "what am i looking at", "what's the cost", "whats the cost",
        "what would i pay", "affordable", "budget", "cheapest"
    ]
    if any(kw in t for kw in price_kw):
        return BuyingSignalType.ASKING_PRICE

    # Check for specific coverage amount requests (e.g. "I want 7000", "need 50k", "looking for 100000")
    amount_pattern = re.search(r'(?:want|need|looking for|get|interested in)\s*\$?(\d[\d,]*)\s*(?:k|K|thousand)?', t)
    if amount_pattern:
        return BuyingSignalType.REQUESTING_COVERAGE

    option_kw = [
        "what are my options", "what options", "what's available", "whats available",
        "what can i get", "what kind of", "types of", "what type",
        "what plans", "what policies"
    ]
    if any(kw in t for kw in option_kw):
        return BuyingSignalType.ASKING_OPTIONS

    compare_kw = [
        "which is better", "term vs", "vs whole", "difference between",
        "term or whole", "iul vs", "compared to", "which one"
    ]
    if any(kw in t for kw in compare_kw):
        return BuyingSignalType.COMPARING

    detail_kw = [
        "how does that work", "how does it work", "tell me more",
        "explain", "what does that mean", "what's the catch", "whats the catch",
        "what are living benefits", "what is iul", "what is term"
    ]
    if any(kw in t for kw in detail_kw):
        return BuyingSignalType.ASKING_DETAILS

    return BuyingSignalType.NONE


# ===================================
# INSURANCE PRODUCT VALIDATION
# ===================================

# Coverage minimums by product type (industry standard)
PRODUCT_MINIMUMS = {
    ProductType.FINAL_EXPENSE:    (1000, 50000),     # $1k - $50k (Transamerica allows up to $100k)
    ProductType.TERM:             (50000, None),      # $50k minimum, no practical max
    ProductType.IUL:              (50000, None),      # $50k minimum
    ProductType.WHOLE_LIFE:       (10000, None),      # Varies by carrier
    ProductType.GUARANTEED_ISSUE: (5000, 25000),      # $5k - $25k typical
}

# Age-based product recommendations
AGE_PRODUCT_GUIDANCE = {
    # (min_age, max_age): (best_products, warning_products, avoid_products)
    (0, 39):   (["term", "iul", "whole_life"], [], []),
    (40, 49):  (["term", "iul", "whole_life"], [], []),
    (50, 59):  (["term", "whole_life", "final_expense"], ["iul"], []),
    (60, 67):  (["final_expense", "whole_life"], ["term"], ["iul"]),
    (68, 120): (["final_expense", "whole_life"], [], ["term", "iul"]),
}


def _parse_dollar_amount(text: str) -> int:
    """Extract a dollar amount from text. Returns 0 if none found."""
    t = text.lower().replace(',', '')
    # Match patterns like "$7000", "7000", "7k", "50,000", "100 thousand"
    m = re.search(r'\$?(\d+)\s*(?:k|K|thousand)', t)
    if m:
        return int(m.group(1)) * 1000
    m = re.search(r'\$?(\d{3,})', t)  # 3+ digits = likely a dollar amount
    if m:
        return int(m.group(1))
    return 0


def _detect_product_type(text: str) -> ProductType:
    """Detect which insurance product type the lead is asking about."""
    t = text.lower()
    if any(kw in t for kw in ["final expense", "burial", "funeral"]):
        return ProductType.FINAL_EXPENSE
    if any(kw in t for kw in ["iul", "indexed universal", "index universal"]):
        return ProductType.IUL
    if any(kw in t for kw in ["whole life", "permanent"]):
        return ProductType.WHOLE_LIFE
    if any(kw in t for kw in ["term life", "term policy", "term insurance", "a term",
                                "10 year", "15 year", "20 year", "30 year"]):
        return ProductType.TERM
    if any(kw in t for kw in ["through work", "through my job", "employer", "group"]):
        return ProductType.GROUP_EMPLOYER
    if any(kw in t for kw in ["guaranteed issue", "no exam", "no questions"]):
        return ProductType.GUARANTEED_ISSUE
    return ProductType.UNKNOWN


def analyze_insurance_context(text: str, all_lead_text: str, age: int = 0) -> InsuranceContext:
    """
    Analyze the lead's messages for insurance product context.
    Returns structured guidance the bot MUST follow (not optional prompt hints).
    """
    ctx = InsuranceContext()
    ctx.lead_age = age

    # Parse requested amount
    ctx.requested_amount = _parse_dollar_amount(text)
    if ctx.requested_amount == 0:
        ctx.requested_amount = _parse_dollar_amount(all_lead_text)

    # Detect product type
    ctx.requested_product = _detect_product_type(text)
    if ctx.requested_product == ProductType.UNKNOWN:
        ctx.requested_product = _detect_product_type(all_lead_text)

    # Validate amount against product minimums
    if ctx.requested_amount > 0:
        if ctx.requested_amount < 50000 and ctx.requested_product in (ProductType.TERM, ProductType.IUL):
            ctx.amount_below_minimum = True
            ctx.needs_clarification = True
            ctx.guidance_note = (
                f"Lead requested ${ctx.requested_amount:,} in {ctx.requested_product.value}. "
                f"Term and IUL policies typically start at $50,000 minimum. "
                f"For amounts under $50,000, final expense or whole life may be more appropriate. "
                f"Ask the lead what they are trying to accomplish with the coverage "
                f"before suggesting a different product. Do not assume."
            )
        elif ctx.requested_amount < 1000:
            ctx.amount_below_minimum = True
            ctx.needs_clarification = True
            ctx.guidance_note = (
                f"Lead requested ${ctx.requested_amount:,} which is below the minimum "
                f"for any standard life insurance product. Final expense starts at $1,000. "
                f"Clarify what they are looking for. They may mean monthly payment, not coverage amount."
            )
        elif ctx.requested_amount <= 50000 and ctx.requested_product == ProductType.UNKNOWN:
            ctx.guidance_note = (
                f"Lead requested ${ctx.requested_amount:,}. At this amount, final expense "
                f"or whole life is the right product category. Term and IUL start at $50,000."
            )

    # Age-based product mismatch detection
    if age > 0:
        if age >= 68 and ctx.requested_product == ProductType.TERM:
            ctx.product_mismatch = True
            ctx.needs_clarification = True
            ctx.guidance_note = (
                f"Lead is {age} and asking about term insurance. At {age}, term premiums "
                f"are extremely expensive and most carriers have age limits. Final expense "
                f"or whole life is usually more realistic at this age. Ask what they are "
                f"trying to protect against before recommending a different product. "
                f"If they truly want term, acknowledge the cost reality honestly."
            )
        elif age >= 60 and ctx.requested_product == ProductType.IUL:
            ctx.product_mismatch = True
            ctx.needs_clarification = True
            ctx.guidance_note = (
                f"Lead is {age} and asking about IUL. At {age}+, IUL premiums are very high "
                f"and there is not enough time for cash value to compound meaningfully. "
                f"Final expense or whole life is more practical. But ask what drew them to IUL "
                f"before redirecting. They may have a specific need that IUL addresses."
            )

    # Depth guard: if the conversation is getting too technical for text
    depth_triggers = [
        "medical underwriting", "simplified issue", "guaranteed issue vs",
        "what carriers", "which company", "illustration", "cash value projection",
        "rate class", "preferred plus", "preferred best", "standard plus",
        "tobacco rate", "table rating", "flat extra", "exclusion rider",
        "convertibility", "return of premium", "decreasing term",
        "modified whole life", "graded benefit", "level benefit"
    ]
    combined = (text + " " + all_lead_text).lower()
    depth_hits = sum(1 for trigger in depth_triggers if trigger in combined)
    if depth_hits >= 2:
        ctx.guidance_note = (
            "DEPTH GUARD: This conversation is getting into underwriting details, "
            "carrier comparisons, or product specifics that cannot be properly addressed "
            "over text. These details require a licensed advisor reviewing their actual "
            "situation. Push firmly but warmly toward booking an appointment. "
            "The right answer to technical insurance questions is a call, not a text."
        )

    return ctx


# ===================================
# MAIN ANALYSIS
# ===================================

def analyze_logic_flow(messages: List[Dict[str, str]], message: str = "") -> LogicSignal:
    """
    Analyze recent conversation to produce LogicSignal.
    Uses a small Grok call for accurate intent + objection detection, keyword fallback.

    Args:
        messages: List of conversation exchanges [{'role': 'lead'/'assistant', 'text': str}]
        message: The current inbound message (empty string for outbound triggers)
    """
    # Determine message context first
    msg_context, consecutive_bot = determine_message_context(message, messages)

    if not messages:
        return LogicSignal(
            stage=ConversationStage.INITIAL_OUTREACH,
            message_context=msg_context,
            has_coverage=False,
            needs_coverage=False,
            mentioned_goal=False,
            mentioned_obstacle=False,
            ready_to_book=False,
            resistance=False,
            conversation_count=0,
            objection_type=ObjectionType.NONE,
            objection_nature=ObjectionNature.NONE,
            consecutive_bot_messages=consecutive_bot,
            articulated_impact=False
        )

    # Split messages by role
    lead_msgs = [m['text'].lower() for m in messages if m['role'] == 'lead']
    bot_msgs  = [m['text'].lower() for m in messages if m['role'] == 'assistant']

    conversation_count = len(lead_msgs)
    all_lead_text = " ".join(lead_msgs)
    recent_lead_text = " ".join(lead_msgs[-4:]) if lead_msgs else ""

    # ─── Primary: LLM intent + objection classification ───
    cls = {}
    if client and lead_msgs:
        prompt = f"""You are classifying lead messages in a life insurance sales conversation.

Lead messages (most recent at bottom):
{chr(10).join([f"[{i+1}] {msg}" for i, msg in enumerate(lead_msgs[-8:])])}

Return ONLY valid JSON with these exact keys:

{{
  "has_coverage": bool,
  "needs_coverage": bool,
  "mentioned_goal": bool,
  "mentioned_obstacle": bool,
  "ready_to_book": bool,
  "resistance": bool,
  "articulated_impact": bool,
  "objection_type": str,
  "objection_nature": str
}}

INTENT FIELDS (true/false):
- has_coverage: they mention having any existing life insurance/policy/coverage
- needs_coverage: expressed need, want, interest, looking, thinking about coverage
- mentioned_goal: protecting family, kids, spouse, mortgage, business, future, etc.
- mentioned_obstacle: barrier like busy, expensive, health issue, not sure, complicated
- ready_to_book: agreed to call/meet/talk/book/time works/yes/lets do it/sure/next step
- resistance: strong opt-out: stop, unsubscribe, remove, leave me alone, do not contact
- articulated_impact: the lead has expressed WHY coverage matters to them personally, what would happen to their family without it, the consequences of the gap, or emotional weight behind their need. Not just mentioning a goal but explaining why it is important to them or what would happen if they did not address it

OBJECTION FIELDS (based on the MOST RECENT lead message only):
- objection_type: one of "none", "not_interested", "spouse_partner", "price_money", "already_covered", "busy_timing"
  "not_interested" = any form of no, pass, not interested, don't need this
  "spouse_partner" = needs to consult spouse, partner, or family member before deciding
  "price_money" = concerns about cost, affordability, budget, expense
  "already_covered" = claims to already have life insurance or be covered
  "busy_timing" = too busy, bad time, call back later, not now
  "none" = no objection, or message is positive/neutral/engaged

- objection_nature: one of "none", "fear_based", "logistical"
  "fear_based" = emotional resistance, avoidance, uncertainty, deflecting the decision
  "logistical" = practical concern about money movement, scheduling, or existing arrangements
  "none" = no objection present

Context clues:
"I already have something through work" = already_covered + logistical
"I don't think I need it" = not_interested + fear_based
"I can't afford that right now" = price_money (could be logistical OR fear_based depending on tone)
"Let me talk to my wife first" = spouse_partner + fear_based
"I'm slammed this week" = busy_timing + fear_based
"Yeah sounds good" = none + none (positive response)
"""

        try:
            response = client.chat.completions.create(
                model="grok-4-1-fast-reasoning",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.15,
                max_tokens=300,
                timeout=8.0
            )
            raw = response.choices[0].message.content.strip()
            # Strip reasoning tags that can wrap the JSON output
            raw = re.sub(r'<thinking>[\s\S]*?</thinking>', '', raw).strip()
            raw = re.sub(r'</?(?:thinking|reply|output|response)>', '', raw).strip()
            if raw.startswith("```json"):
                raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
            elif raw.startswith("```"):
                raw = raw.split("```", 1)[1].split("```", 1)[0].strip()
            cls = json.loads(raw)
            logger.debug(f"LLM intent+objection classification: {cls}")
        except Exception as e:
            logger.warning(f"LLM classification failed: {e}. Falling back to keywords.")
            cls = {}

    # ─── Fallback: keyword-based if LLM fails or unavailable ───
    if not cls or not isinstance(cls, dict):
        coverage_keywords = [
            "policy", "coverage", "term", "whole life", "iul", "universal", "group",
            "state farm", "farmers", "allstate", "have insurance", "already have"
        ]
        need_keywords = ["need", "want", "looking", "interested", "thinking about", "protect", "mortgage"]
        goal_keywords = ["family", "kids", "wife", "husband", "spouse", "children", "business", "parents"]
        obstacle_keywords = ["busy", "expensive", "too much", "later", "not sure", "confused", "health", "complicated"]
        booking_keywords = [
            "yes", "sure", "ok", "sounds good", "let's do", "book", "schedule",
            "appointment", "call", "talk", "meet", "time work", "works for me"
        ]
        stop_keywords = ["stop", "unsubscribe", "remove", "leave me alone", "do not contact"]

        obj_type, obj_nature = detect_objection_keywords(recent_lead_text)

        impact_keywords = [
            "important to me", "worry", "worried", "scared", "what if",
            "my family would", "kids would", "they'd have to", "couldn't afford",
            "leaves them", "left with nothing", "i need to make sure", "peace of mind",
            "keep me up", "can't sleep", "burden", "devastating", "struggle"
        ]

        cls = {
            "has_coverage":       any(_has_word(all_lead_text, kw) for kw in coverage_keywords),
            "needs_coverage":     any(_has_word(all_lead_text, kw) for kw in need_keywords),
            "mentioned_goal":     any(_has_word(all_lead_text, kw) for kw in goal_keywords),
            "mentioned_obstacle": any(_has_word(all_lead_text, kw) for kw in obstacle_keywords),
            "ready_to_book":      any(_has_word(recent_lead_text, kw) for kw in booking_keywords),
            "resistance":         any(_has_word(recent_lead_text, kw) for kw in stop_keywords),
            "articulated_impact": any(kw in all_lead_text for kw in impact_keywords),
            "objection_type":     obj_type.value,
            "objection_nature":   obj_nature.value,
        }

    # ─── Parse objection from classification ───
    obj_type_str = cls.get("objection_type", "none")
    obj_nature_str = cls.get("objection_nature", "none")

    try:
        objection_type = ObjectionType(obj_type_str)
    except ValueError:
        objection_type = ObjectionType.NONE

    try:
        objection_nature = ObjectionNature(obj_nature_str)
    except ValueError:
        objection_nature = ObjectionNature.NONE

    # ─── Booking confirmed by bot recently? ───
    booking_confirmed = False
    if bot_msgs:
        recent_bot = " ".join(bot_msgs[-3:])
        confirmation_patterns = [
            "all set", "you're booked", "appointment is", "calendar invite",
            "confirmed", "see you at", "looking forward", "locked in", "set for"
        ]
        booking_confirmed = any(p in recent_bot for p in confirmation_patterns)

    # ─── Buying signal detection (structured, not prompt-based) ───
    buying_signal = detect_buying_signal(message) if message else BuyingSignalType.NONE
    if buying_signal == BuyingSignalType.NONE and recent_lead_text:
        buying_signal = detect_buying_signal(recent_lead_text)

    # ─── Insurance product context analysis ───
    ins_ctx = analyze_insurance_context(message or "", all_lead_text)

    # ─── Depth guard: too many technical questions = book appointment ───
    too_deep = bool(ins_ctx.guidance_note and "DEPTH GUARD" in ins_ctx.guidance_note)

    # ─── Stage logic (priority order) ───
    stage = ConversationStage.QUALIFYING

    if booking_confirmed:
        # Already booked — nothing else matters
        stage = ConversationStage.BOOKED

    elif cls.get("ready_to_book", False) and conversation_count >= 2:
        # Explicit readiness to book overrides objections
        stage = ConversationStage.BOOKING

    elif too_deep and msg_context == MessageContext.INBOUND_REPLY:
        # Too deep in technical details for text — push to booking
        stage = ConversationStage.BOOKING

    elif objection_type != ObjectionType.NONE and msg_context == MessageContext.INBOUND_REPLY:
        # Active objection from an inbound reply — handle it before pushing forward
        stage = ConversationStage.OBJECTION_HANDLING

    elif buying_signal in (BuyingSignalType.ASKING_PRICE, BuyingSignalType.REQUESTING_COVERAGE,
                           BuyingSignalType.READY_SIGNAL) and msg_context == MessageContext.INBOUND_REPLY:
        # Strong buying signal = push to booking. Asking about price, requesting
        # specific coverage, or saying "let's do it" are all appointment triggers.
        stage = ConversationStage.BOOKING

    elif buying_signal in (BuyingSignalType.ASKING_OPTIONS, BuyingSignalType.COMPARING,
                           BuyingSignalType.ASKING_DETAILS) and conversation_count >= 1:
        # Moderate buying signal with some conversation = move toward booking
        # These show real interest even without full gap/impact qualification
        stage = ConversationStage.BOOKING

    elif (cls.get("needs_coverage", False) or cls.get("mentioned_goal", False)) and cls.get("articulated_impact", False) and conversation_count >= 2:
        # Gap found AND lead has expressed why it matters — ready to book
        stage = ConversationStage.BOOKING

    elif (cls.get("needs_coverage", False) or cls.get("mentioned_goal", False)) and conversation_count >= 2:
        # Gap found but lead hasn't expressed why it matters yet — stay qualifying
        stage = ConversationStage.QUALIFYING

    elif conversation_count == 0:
        stage = ConversationStage.INITIAL_OUTREACH

    return LogicSignal(
        stage=stage,
        message_context=msg_context,
        has_coverage=cls.get("has_coverage", False),
        needs_coverage=cls.get("needs_coverage", False),
        mentioned_goal=cls.get("mentioned_goal", False),
        mentioned_obstacle=cls.get("mentioned_obstacle", False),
        ready_to_book=cls.get("ready_to_book", False),
        resistance=cls.get("resistance", False),
        conversation_count=conversation_count,
        objection_type=objection_type,
        objection_nature=objection_nature,
        consecutive_bot_messages=consecutive_bot,
        articulated_impact=cls.get("articulated_impact", False),
        buying_signal=buying_signal,
        insurance_context=ins_ctx,
        too_deep_for_text=too_deep,
    )
