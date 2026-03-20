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
    RAPPORT             = "rapport"
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
    THINK_ABOUT_IT      = "think_about_it"         # "let me think about it", "need to think", "not sure yet"
    HEALTH_CONCERN      = "health_concern"         # "I have diabetes", "I probably can't qualify", "too old"
    TRUST_ISSUE         = "trust_issue"            # "insurance is a scam", "got burned before", "my nephew sells"

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
    consecutive_rapport_turns: int = 0     # How many recent lead messages had zero qualifying content


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


def _count_consecutive_rapport_turns(messages: List[Dict[str, str]]) -> int:
    """
    Count how many recent consecutive lead messages had ZERO qualifying content.
    Walks backward through lead messages. Stops counting when a lead message
    contains qualifying keywords (insurance, coverage, family, goals, etc.).

    Uses word-boundary matching to avoid false positives (e.g. "needlessly"
    matching "need"). Objection keywords are excluded — those are handled by
    the objection engine, not the rapport detector.
    """
    # Multi-word phrases checked via substring (safe, no false positive risk)
    _QUALIFYING_PHRASES = [
        "whole life", "final expense", "life insurance", "term life",
        "looking for", "interested in", "sign me up", "what's next",
        "sounds good", "let's do", "how much", "what does it cost",
        "think about it", "already have", "not interested", "no thanks",
        "happens to me", "happens if", "what if i", "coverage gap",
    ]
    # Single words checked via word boundary regex (prevents substring false positives)
    _QUALIFYING_WORDS = [
        "policy", "coverage", "insurance", "premium", "rates", "quote",
        "iul", "universal", "beneficiary", "funeral", "burial", "estate",
        "protect", "mortgage", "family", "kids", "wife", "husband",
        "spouse", "children", "parents", "dependents",
        "book", "schedule", "appointment",
        "expensive", "afford",
        # Health / underwriting (qualifying-relevant, not rapport)
        "sick", "medical", "physical", "exam", "approval", "health",
        "diagnosis", "condition", "medication", "doctor", "underwriting",
    ]
    # Pre-compile word boundary patterns for single words
    _QUALIFYING_WORD_PATTERNS = [re.compile(r'\b' + re.escape(w) + r'\b') for w in _QUALIFYING_WORDS]

    count = 0
    for msg in reversed(messages):
        if msg['role'] != 'lead':
            continue
        text = msg['text'].lower()
        # Check multi-word phrases via substring (safe — phrases are specific enough)
        has_phrase = any(p in text for p in _QUALIFYING_PHRASES)
        # Check single words via word boundary regex (prevents "needlessly" → "need")
        has_word = any(pat.search(text) for pat in _QUALIFYING_WORD_PATTERNS) if not has_phrase else False
        if has_phrase or has_word:
            break
        count += 1
    return count


def detect_objection_keywords(text: str) -> Tuple[ObjectionType, ObjectionNature]:
    """
    Keyword-based fallback for objection detection when LLM is unavailable.

    PHILOSOPHY: Think like a closer. The ONLY responses that are NOT objections
    are: answering a question, asking a question, expressing interest, agreeing
    to something, or providing information. EVERYTHING ELSE is some form of
    objection — and every objection is an opportunity.

    The keyword lists are intentionally broad. It is far better to classify a
    neutral message as an objection (the LLM will handle it gracefully) than to
    let a real objection slip through undetected (the LLM gives up).
    """
    if not text:
        return ObjectionType.NONE, ObjectionNature.NONE

    text_lower = text.lower().strip()
    word_count = len(text_lower.split())

    # ── NOT INTERESTED — any form of "no", dismissal, disengagement ──
    # A closer hears ALL of these as "I haven't been given a good enough reason yet"
    not_interested_kw = [
        # Direct "not interested" variants
        "not interested", "no longer interested", "no interest",
        "never interested", "wasn't interested", "lost interest",
        # Flat no / dismissals
        "no thanks", "no thank you", "no thx", "nty",
        "don't want", "dont want", "do not want", "not wanting",
        "don't need", "dont need", "do not need",
        "not for me", "not for us", "not my thing",
        "not looking", "no longer looking", "wasn't looking",
        "no need", "no desire",
        # Pass / decline
        "i'll pass", "ill pass", "i pass", "gonna pass", "going to pass",
        "hard pass", "i decline", "i refuse", "count me out",
        # Dismissive closers
        "nah", "nope", "naw", "hell no", "heck no", "absolutely not",
        "definitely not", "certainly not", "no way", "not a chance",
        "not happening", "never", "not ever", "no sir", "no ma'am",
        "no maam", "miss me with", "foh",
        # "I'm good" / "I'm fine" (dismissive, not confirmatory)
        # Handled with context check below
        # Go away / leave me alone (softer than TCPA "do not contact")
        "leave me alone", "go away", "buzz off", "get lost", "kick rocks",
        "lose my number", "delete my number", "take me off your list",
        "take me off the list", "off your list", "off the list",
        "stop texting", "stop calling", "stop messaging", "stop contacting",
        "quit texting", "quit calling", "quit messaging",
        "enough already", "enough with", "give it a rest",
        # Soft exits
        "not anymore", "not any more", "not at this time",
        "not right now for me", "not something i need",
        "i don't think so", "i dont think so", "don't think so",
        "i think i'm ok", "i think im ok", "think i'm ok",
        "all good here", "we're all good", "we're good",
        "not today", "no not today", "maybe some other lifetime",
        "i have no interest", "zero interest",
        "please don't", "please stop", "please dont",
        # Apathy / don't care
        "don't care", "dont care", "do not care", "couldn't care less",
        "could not care less", "i could care less", "who cares",
        "couldn't be bothered", "could not be bothered",
        # Done / finished
        "i'm done", "im done", "we're done", "done with this",
        "done talking", "over it", "over this",
    ]

    # "i'm good" / "im good" / "i'm fine" — context-sensitive
    # "yeah im good with 3pm" = positive.  "im good" by itself = dismissal.
    # "yeah I'm good on all that thanks" = dismissal (7 words, still a brush-off)
    im_good_patterns = ["i'm good", "im good", "i'm fine", "im fine",
                        "we're good", "were good", "we good"]
    positive_context = ["good with", "good to go", "good to proceed", "good for",
                        "good on that", "good on the", "good let",
                        "fine with", "fine to", "fine for",
                        "good to schedule", "good to book", "good to meet"]
    if any(p in text_lower for p in im_good_patterns):
        has_positive = any(p in text_lower for p in positive_context)
        if not has_positive and word_count <= 10:
            not_interested_kw.append(text_lower)

    # ── SPOUSE / THIRD PARTY — deferring decision to someone else ──
    # A closer hears: "I'm not confident enough to decide alone"
    spouse_kw = [
        # Spouse / partner
        "talk to my wife", "talk to my husband", "ask my wife", "ask my husband",
        "check with my wife", "check with my husband", "run it by my wife",
        "run it by my husband", "discuss with my wife", "discuss with my husband",
        "talk to my partner", "ask my partner", "check with my partner",
        "my wife would", "my husband would", "wife says", "husband says",
        "wife thinks", "husband thinks", "other half",
        "wife handles", "husband handles", "wife deals with", "husband deals with",
        "wife decides", "husband decides", "wife takes care", "husband takes care",
        "wife manages", "husband manages",
        "spouse", "wifey", "hubby", "significant other",
        "talk it over with", "talk it over first",
        # Family
        "talk to my family", "ask my family", "check with my family",
        "talk to my daughter", "talk to my son", "talk to my mom", "talk to my dad",
        "ask my daughter", "ask my son", "ask my mom", "ask my dad",
        "ask my parents", "talk to my parents", "check with my parents",
        "ask my kids", "talk to my kids", "run it by my family",
        "my daughter handles", "my son handles", "my kids handle",
        # Professional advisors
        "ask my accountant", "check with my accountant", "run it by my accountant",
        "talk to my accountant", "my accountant", "my cpa",
        "ask my lawyer", "talk to my lawyer", "check with my lawyer",
        "ask my attorney", "talk to my attorney", "check with my attorney",
        "my financial advisor", "my advisor", "my financial planner",
        "ask my agent", "talk to my agent", "check with my agent",
        "my insurance guy", "my insurance gal", "my insurance person",
        "my insurance agent", "my broker",
        # Generic deferral
        "need to check with", "need to talk to", "need to ask",
        "want to check with", "want to talk to", "want to ask",
        "gotta check with", "gotta talk to", "gotta ask",
        "let me check with", "let me talk to", "let me ask",
        "consult with", "get their opinion", "get his opinion", "get her opinion",
        "see what they think", "see what he thinks", "see what she thinks",
        "get permission", "need approval", "need their ok",
    ]

    # ── PRICE / MONEY — any cost concern ──
    # A closer hears: "You haven't shown me why it's worth it yet"
    price_kw = [
        "too expensive", "too pricey", "too costly", "too much money",
        "too much per month", "too rich for my blood",
        "can't afford", "cant afford", "cannot afford",
        "don't have the money", "dont have the money", "no money",
        "out of my budget", "not in my budget", "over my budget",
        "on a fixed income", "fixed income", "tight budget",
        "money is tight", "funds are tight", "finances are tight",
        "barely making ends meet", "barely getting by",
        "living paycheck to paycheck", "paycheck to paycheck",
        "can barely pay", "can't even pay", "struggling financially",
        "cost too much", "costs too much", "that's a lot",
        "that's expensive", "thats expensive", "sounds expensive",
        "more than i expected", "more than i thought",
        "wasn't expecting that", "sticker shock",
        "don't have that kind of", "that's steep",
        "i don't have that", "where would i find",
        "not worth it", "not worth the money", "waste of money",
        "throwing money away", "rather save", "rather keep my money",
        "can't justify", "cant justify", "hard to justify",
        "what's the cheapest", "anything cheaper", "is there a cheaper",
        "minimum amount", "bare minimum",
    ]

    # ── ALREADY COVERED — claims existing protection ──
    # A closer hears: "I haven't been shown the gaps in what I have"
    already_kw = [
        "already have", "already got", "already covered", "already taken care of",
        "i'm covered", "im covered", "we're covered", "were covered",
        "have insurance", "have a policy", "got a policy",
        "have life insurance", "got life insurance",
        "taken care of", "all taken care of",
        "all set", "i'm set", "im set", "we're set", "were set",
        "set with insurance", "good on insurance",
        "squared away", "all squared away",
        "have something through", "through my job", "through work",
        "employer provides", "work provides", "job provides",
        "company provides", "employer covers", "work covers",
        "group policy", "group plan", "group coverage",
        "my union", "union provides", "union covers",
        "va covers", "va provides", "veterans", "military covers",
        "tricare", "sgli",
        "already working with someone", "already have an agent",
        "already have a guy", "already have a gal", "already have someone",
        "my agent handles", "my broker handles",
        "just got a policy", "just bought", "recently purchased",
        "just renewed", "just signed up",
        "happy with what i have", "satisfied with", "content with",
        # Chose/selected/went with a carrier or policy (completed purchasing decision)
        "chose my", "chosen my", "i chose", "i have chosen",
        "went with", "i went with", "signed with", "signed up with",
        "picked a", "picked my", "selected a", "selected my",
        "found someone", "found an agent", "found a guy", "found a broker",
        "moved forward with", "going with", "decided to go with",
        "got a guy", "got an agent", "got a broker",
        "have a policyholder", "chosen my policyholder",
    ]

    # ── THINK ABOUT IT — stalling, delaying decision ──
    # A closer hears: "I don't have a compelling enough reason to act NOW"
    think_kw = [
        "think about it", "think on it", "think it over", "think about this",
        "let me think", "need to think", "gotta think", "got to think",
        "have to think", "wanna think", "want to think",
        "sleep on it", "sit on it", "sit with it", "mull it over",
        "consider it", "need to consider", "let me consider",
        "need some time", "give me some time", "give me a few days",
        "give me a week", "give me time",
        "not sure yet", "not sure about", "still not sure",
        "not ready", "not ready yet", "not quite ready",
        "not ready to commit", "not ready to decide",
        "need to process", "lot to think about", "lot to consider",
        "big decision", "huge decision", "major decision",
        "need a minute", "need a day", "need a few days",
        "need a week", "need a couple days",
        "let me sit with", "let me process", "let me digest",
        "no rush right", "no rush", "what's the rush", "whats the rush",
        "no hurry", "plenty of time", "i've got time",
        "not in a hurry", "not in a rush",
        "get back to you", "i'll get back", "ill get back",
        "let me get back", "circle back", "follow up later",
        "maybe down the road", "maybe in the future",
        "maybe next year", "maybe next month", "maybe someday",
        "down the line", "down the road", "in the future",
        "sometime later", "eventually", "when i'm ready",
        "when the time is right", "when things settle",
        "not a priority", "not a priority right now",
        "other priorities", "bigger fish to fry",
        "a lot going on", "lot on my plate", "lot going on",
        "too much going on", "dealing with a lot",
        "just not right now", "timing isn't right", "bad timing",
        "maybe later on", "maybe some other time", "some other time",
        "rain check", "take a rain check",
        "i'll let you know", "ill let you know", "let you know",
        "i'll reach out", "ill reach out", "reach out when",
        "don't call me i'll call you", "dont call me ill call you",
        # Disguised think-about-it — all mean "I don't have a compelling reason to act now"
        "send me an email", "send me email", "email me the info",
        "email me the details", "email me about", "just email me",
        "send me a quote", "send me the quote", "just send me a quote",
        "send me a proposal", "send me the proposal",
        "send me something", "send me some information",
        "let me look it over", "let me look into it", "let me look at it",
        "let me check it out", "let me review", "let me do some research",
        "let me read up on", "let me read about",
        "i need to do my research", "need to do some research",
        "need to look into it", "need to look into this",
        "i want to look into", "want to look into this",
        "i'll check it out", "ill check it out",
        "let me see what's out there", "let me compare",
        "let me shop around", "want to shop around",
        "send me the link", "just send me the link",
        # Noncommittal / ambivalent (when standalone or very short)
        "not sure", "i don't know", "i dont know", "idk",
        "we'll see", "we will see", "who knows",
    ]

    # ── BUSY / TIMING — can't talk now ──
    # A closer hears: "You caught me at a bad moment, but the door isn't closed"
    busy_kw = [
        "busy right now", "busy at the moment", "busy today",
        "really busy", "super busy", "slammed", "swamped",
        "call back later", "call back tomorrow", "call back next week",
        "text back later", "text me later", "text me tomorrow",
        "call me back", "call me later", "call me tomorrow",
        "call me next week", "call me monday", "call me after",
        "reach out later", "reach out tomorrow", "reach out next week",
        "try again later", "try me later", "try me tomorrow",
        "try again tomorrow", "try again next week",
        "another time", "some other time",
        "not a good time", "not the best time", "bad time",
        "not now", "can't talk", "cant talk", "can't right now",
        "cant right now", "not right now",
        "in a meeting", "at work", "at the office", "on the job",
        "driving", "in the car", "behind the wheel",
        "with a client", "with a customer", "with a patient",
        "in the middle of something", "in the middle of dinner",
        "eating", "at dinner", "at lunch",
        "about to go", "heading out", "on my way out",
        "about to leave", "gotta go", "gotta run", "got to go",
        "at the gym", "working out", "exercising",
        "with my kids", "with the kids", "with family",
        "on vacation", "traveling", "out of town",
        "maybe later", "maybe tomorrow", "maybe next week",
        "can you call back", "could you call back",
        "hit me up later", "hmu later",
        "catch me later", "catch me tomorrow",
    ]

    # ── Negation & context guards ──
    # Prevents false positives when the lead NEGATES the objection keyword
    # or uses it in a positive/different context.

    # Negation patterns that flip objection meaning
    _NEGATION_PREFIXES = [
        "not too ", "isn't too ", "isnt too ", "it's not ", "its not ",
        "don't need to ", "dont need to ", "don't have to ", "dont have to ",
        "no need to ", "i don't ", "i dont ", "won't need to ", "wont need to ",
        "not really ", "not that ", "never too ",
    ]

    # Context suffixes that flip the meaning of otherwise-objection phrases.
    # "not for me" is an objection. "not for me to decide" is a deferral.
    # "I'm good" is a dismissal. "I'm good with that time" is acceptance.
    # "we're good" is a dismissal. "we're good to go" is acceptance.
    _CONTEXT_OVERRIDES = [
        # (keyword, suffix_that_cancels_it)
        ("not for me", [" to decide", " to say", " to judge", " to handle"]),
        ("we're good", [" to go", " to proceed", " with that", " with this", " on that", " for now let"]),
        ("were good", [" to go", " to proceed", " with that", " with this", " on that"]),
        ("we good", [" to go", " to proceed", " with that", " with this", " on that"]),
    ]
    _CONTEXT_OVERRIDE_MAP = {}
    for _co_kw, _co_suffixes in _CONTEXT_OVERRIDES:
        _CONTEXT_OVERRIDE_MAP[_co_kw] = _co_suffixes

    def _keyword_match(keywords):
        """Check if any keyword is present WITHOUT being negated or context-overridden."""
        for kw in keywords:
            if kw not in text_lower:
                continue
            # Check if this keyword appearance is negated
            idx = text_lower.index(kw)
            prefix = text_lower[:idx]
            negated = any(prefix.endswith(neg) for neg in _NEGATION_PREFIXES)
            if negated:
                continue
            # Check context overrides — suffix after the keyword changes its meaning
            if kw in _CONTEXT_OVERRIDE_MAP:
                suffix = text_lower[idx + len(kw):]
                if any(suffix.startswith(s) for s in _CONTEXT_OVERRIDE_MAP[kw]):
                    continue  # Not actually an objection
            return True
        return False

    # ── Context guards for specific false positive patterns ──

    # "not interested in X, I want Y" — buying signal, not dismissal
    # "my wife loves/agrees/supports/wants" — supportive spouse, not obstacle
    # "I already have X but need more" — coverage gap, not objection
    _BUYING_THROUGH_OBJECTION = [
        # "not interested in X, I want Y" / "not interested in X, what about Y?" patterns
        (r'not interested in\s+\w+.*(?:i want|but|prefer|rather|instead|looking for|what about|how about|can you show|tell me about|do you have)', ObjectionType.NOT_INTERESTED),
        # "not looking for X, more interested in Y" — narrowing, not rejecting
        (r'not looking for\s+\w+.*(?:i want|but|more interested|rather|instead|what about|how about)', ObjectionType.NOT_INTERESTED),
        # spouse is supportive
        (r'(?:wife|husband|spouse|partner)\s+(?:loves?|agrees?|supports?|wants?|said yes|is on board|thinks?\s+(?:it\'?s?\s+)?(?:great|good|a good))', ObjectionType.SPOUSE_PARTNER),
        # has coverage but needs more
        (r'(?:already have|have insurance|have a policy|have coverage).*(?:but|however|need more|not enough|doesn\'t cover|gaps?|is it enough|worried)', ObjectionType.ALREADY_COVERED),
        # "think about it all the time" / "think about my family" / "I think about my wife and what she'd do" — expressing concern, not stalling
        (r'think(?:s|ing)? about (?:it |this )?(?:all the time|every day|constantly|a lot|my family|my kids|my wife|my husband|my children|my spouse|them|her|him|what (?:would|she|he|they|my))', ObjectionType.THINK_ABOUT_IT),
        # "busy protecting" / "busy working on" — commitment, not scheduling conflict
        (r'busy (?:protect|working on|taking care|making sure|getting|building)', ObjectionType.BUSY_TIMING),
        # "done researching, ready to move forward" — action, not dismissal
        (r'(?:done|finished|over)\s+(?:research|looking|shopping|comparing).*(?:ready|want to|let\'?s|move forward|go ahead)', ObjectionType.NOT_INTERESTED),
        # health concern + buying intent = buying signal, not objection
        # "I have diabetes but I want coverage" / "I know I have health issues, what are my options?"
        (r'(?:have|had|diabetes|cancer|heart|health).*(?:but i want|but i need|what are my options|can i still|is there|what do you recommend|help me)', ObjectionType.HEALTH_CONCERN),
        # "I was denied before but I'm still looking" — persistence through rejection
        (r'(?:denied|turned down|rejected).*(?:but|still|looking|want|need|trying)', ObjectionType.HEALTH_CONCERN),
        # trust concern + positive signal = buying through trust
        # "I don't trust most agents but you seem different" / "my buddy sells but I want to compare"
        (r"(?:don't trust|dont trust|got burned).*(?:but|you seem|your approach|still want|still need|want to compare)", ObjectionType.TRUST_ISSUE),
        (r'(?:nephew|buddy|cousin|friend|uncle|brother|sister)\s+sells.*(?:but|compare|want to see|second opinion|shop around)', ObjectionType.TRUST_ISSUE),
    ]

    # Check if the message matches any buying-through-objection pattern
    suppressed_types = set()
    for pattern, obj_type in _BUYING_THROUGH_OBJECTION:
        if re.search(pattern, text_lower):
            suppressed_types.add(obj_type)

    # ── HEALTH CONCERN — believes they cannot qualify ──
    # These people often WANT coverage but think they can't get it
    health_kw = [
        "i have diabetes", "i'm diabetic", "im diabetic", "type 2", "type 1",
        "i had cancer", "cancer survivor", "had chemo", "in remission",
        "heart attack", "had a stroke", "heart condition", "heart disease",
        "high blood pressure", "hypertension", "on medication",
        "pre-existing", "preexisting", "pre existing",
        "can't qualify", "cant qualify", "won't qualify", "wont qualify",
        "probably can't get", "probably cant get", "probably won't get",
        "can i even get", "could i even get", "would i even qualify",
        "they won't insure", "they wont insure", "uninsurable",
        "too old for", "too old to get", "at my age",
        "health issues", "health problems", "health conditions",
        "i take medication", "i take meds", "on meds", "on pills",
        "copd", "emphysema", "dialysis", "kidney disease",
        "liver disease", "cirrhosis", "hepatitis",
        "mental health", "depression medication", "anxiety medication",
        "i was denied", "got denied", "been denied", "been turned down",
        "turned down before", "rejected for insurance", "couldn't get approved",
        # Common prescription drug names that indicate health conditions
        # Diabetes: metformin, glipizide, glyburide, jardiance, ozempic, mounjaro
        # Blood pressure: lisinopril, amlodipine, losartan, hydrochlorothiazide
        # Cholesterol: atorvastatin, simvastatin
        # Heart/blood: eliquis, warfarin, plavix, nitroglycerin
        # Mental health: sertraline/zoloft, lexapro, prozac, xanax, wellbutrin, trazodone, gabapentin
        "metformin", "lisinopril", "atorvastatin", "amlodipine", "losartan",
        "levothyroxine", "gabapentin", "simvastatin",
        "sertraline", "zoloft", "lexapro", "prozac", "xanax", "wellbutrin",
        "trazodone", "glipizide", "glyburide", "ozempic", "mounjaro",
        "eliquis", "warfarin", "plavix", "nitroglycerin",
        "insulin pump", "cpap machine", "oxygen tank",
    ]

    # ── TRUST ISSUE — distrust, bad experience, or personal loyalty ──
    trust_kw = [
        "insurance is a scam", "insurance scam", "all a scam", "it's a scam",
        "got burned", "been burned", "got screwed", "been screwed",
        "ripped off", "rip off", "ripped me off",
        "bad experience", "bad agent", "terrible experience",
        "don't trust", "dont trust", "do not trust", "can't trust", "cant trust",
        "my last agent", "previous agent",
        "never pays out", "never pay out",
        "they never pay", "insurance never covers",
        "my nephew sells", "my buddy sells", "my cousin sells",
        "my friend sells", "my neighbor sells", "my uncle sells",
        "my brother sells", "my sister sells",
        "know someone who sells", "know a guy who sells", "know a gal who sells",
        "family member sells", "friend in the business",
        "already have a guy", "already have someone",
        "my nephew is an agent", "my buddy is an agent", "my cousin is an agent",
        "my friend is an agent",
    ]

    # ── Check order: COMPOUND PRIORITY HIERARCHY ──
    # Price is ALWAYS #1 — taking money off the table unlocks everything else
    # Then health (they want it but doubt they can get it)
    # Then specific types, then broad dismissal last

    if ObjectionType.PRICE_MONEY not in suppressed_types and _keyword_match(price_kw):
        return ObjectionType.PRICE_MONEY, ObjectionNature.LOGISTICAL

    if ObjectionType.HEALTH_CONCERN not in suppressed_types and _keyword_match(health_kw):
        return ObjectionType.HEALTH_CONCERN, ObjectionNature.LOGISTICAL

    if ObjectionType.SPOUSE_PARTNER not in suppressed_types and _keyword_match(spouse_kw):
        return ObjectionType.SPOUSE_PARTNER, ObjectionNature.FEAR_BASED

    if ObjectionType.ALREADY_COVERED not in suppressed_types and _keyword_match(already_kw):
        return ObjectionType.ALREADY_COVERED, ObjectionNature.LOGISTICAL

    if ObjectionType.TRUST_ISSUE not in suppressed_types and _keyword_match(trust_kw):
        return ObjectionType.TRUST_ISSUE, ObjectionNature.FEAR_BASED

    if ObjectionType.THINK_ABOUT_IT not in suppressed_types and _keyword_match(think_kw):
        return ObjectionType.THINK_ABOUT_IT, ObjectionNature.FEAR_BASED

    if ObjectionType.BUSY_TIMING not in suppressed_types and _keyword_match(busy_kw):
        return ObjectionType.BUSY_TIMING, ObjectionNature.FEAR_BASED

    # Check not_interested LAST — broadest category
    if ObjectionType.NOT_INTERESTED not in suppressed_types and _keyword_match(not_interested_kw):
        return ObjectionType.NOT_INTERESTED, ObjectionNature.FEAR_BASED

    # ── CATCH-ALL: short negative/dismissive responses ──
    # A closer's mindset: if it's short and not clearly positive or a question,
    # it's some form of resistance. Better to handle it than to ignore it.

    # Tier 1: Very short (≤5 words, not a question) — catch single-word dismissals
    if word_count <= 5 and not text_lower.endswith("?"):
        dismissal_patterns = [
            r'^no+$', r'^nah+$', r'^nope+$', r'^naw+$',
            r'^bye', r'^goodbye', r'^later$', r'^whatever$',
            r'^lol no', r'^lmao no', r'^ha+\s+no',
            r'^not really', r'^hardly', r'^doubt it',
            r'^why would i', r'^why should i',
            r'^no\s+i', r'^nah\s+i',
            r'^maybe$',         # bare "maybe" by itself = think_about_it
        ]
        # "maybe" alone is think_about_it, not not_interested
        if re.search(r'^maybe$', text_lower):
            return ObjectionType.THINK_ABOUT_IT, ObjectionNature.FEAR_BASED
        if any(re.search(p, text_lower) for p in dismissal_patterns):
            return ObjectionType.NOT_INTERESTED, ObjectionNature.FEAR_BASED

    # Tier 2: Medium-length (6-12 words, not a question) — catch dismissals
    # that the keyword lists miss because they use phrasing not in our lists.
    # These are broader regex patterns that only fire on medium-length messages
    # to avoid false positives on long engaged replies.
    if 6 <= word_count <= 12 and not text_lower.endswith("?"):
        medium_dismissals = [
            r'\bwhatever\b.*\b(?:man|dude|bro|lady|bye|done|peace)\b',
            r'\bdone\b.*\b(?:talking|with this|with you|here|texting)\b',
            r'\bdon\'?t\s+care\b',
            r'\bcouldn\'?t\s+care\s+less\b',
            r'\bnot\s+(?:gonna|going\s+to)\s+(?:happen|buy|do\s+it|sign\s+up)\b',
            r'^(?:lol|lmao|haha)\s+(?:no|nah|nope|bye|whatever)',
        ]
        if any(re.search(p, text_lower) for p in medium_dismissals):
            return ObjectionType.NOT_INTERESTED, ObjectionNature.FEAR_BASED

    return ObjectionType.NONE, ObjectionNature.NONE


# ===================================
# BUYING SIGNAL DETECTION
# ===================================

def detect_buying_signal(text: str) -> BuyingSignalType:
    """Detect buying signals from lead messages. These indicate interest
    and should push toward booking.

    IMPORTANT: Only match insurance-related buying signals. Generic phrases
    like "how much experience do you have?" or "what kind of dog?" must NOT
    trigger. Multi-word phrases are preferred over single-word substring
    matches to prevent false positives.
    """
    if not text:
        return BuyingSignalType.NONE
    t = text.lower()

    # ── Insurance context guard ──
    # For ambiguous phrases ("how much", "what kind of"), require insurance
    # context words nearby to avoid matching non-insurance questions.
    _INSURANCE_CONTEXT = [
        "insurance", "coverage", "policy", "premium", "term", "whole life",
        "iul", "universal", "final expense", "burial", "plan", "protect",
        "life insurance", "beneficiary", "death benefit", "quote",
    ]
    has_insurance_context = any(w in t for w in _INSURANCE_CONTEXT)

    ready_kw = [
        "sign me up", "let's do it", "lets do it", "i'm ready", "im ready",
        "what's next", "whats next", "how do i start", "let's get started",
        "i want to move forward", "ready to go", "ready to move forward",
    ]
    if any(kw in t for kw in ready_kw):
        return BuyingSignalType.READY_SIGNAL

    # Price keywords — "how much" requires insurance context to avoid
    # matching "how much experience do you have?" or "how much time?"
    price_kw_strong = [
        "what does it cost", "what would it cost", "what's the cost",
        "whats the cost", "what would i pay", "what am i looking at",
        "monthly payment", "per month", "a month",
        "how much is the", "how much does the", "how much for",
        "how much would", "how much is it", "how much does it",
        "send me a quote", "can you quote", "get a quote",
    ]
    price_kw_context = ["how much", "price", "premium", "affordable", "budget", "cheapest"]

    if any(kw in t for kw in price_kw_strong):
        return BuyingSignalType.ASKING_PRICE
    if has_insurance_context and any(kw in t for kw in price_kw_context):
        return BuyingSignalType.ASKING_PRICE

    # Coverage amount requests (e.g. "I want 7000", "need 50k")
    amount_pattern = re.search(r'(?:want|need|looking for|get|interested in)\s*\$?(\d[\d,]*)\s*(?:k|K|thousand)?', t)
    if amount_pattern:
        return BuyingSignalType.REQUESTING_COVERAGE

    # Options keywords — "what kind of" requires insurance context
    # to avoid "what kind of dog do you have?"
    option_kw_strong = [
        "what are my options", "what options do i have", "what's available",
        "whats available", "what can i get", "what plans", "what policies",
        "what companies do you work with", "what carriers",
        "what would you recommend",
    ]
    option_kw_context = ["what kind of", "types of", "what type"]

    if any(kw in t for kw in option_kw_strong):
        return BuyingSignalType.ASKING_OPTIONS
    if has_insurance_context and any(kw in t for kw in option_kw_context):
        return BuyingSignalType.ASKING_OPTIONS

    compare_kw = [
        "which is better", "term vs", "vs whole", "difference between",
        "term or whole", "iul vs", "compared to", "which one should i",
    ]
    if any(kw in t for kw in compare_kw):
        return BuyingSignalType.COMPARING

    detail_kw = [
        "how does that work", "how does it work", "tell me more",
        "explain", "what does that mean", "what's the catch", "whats the catch",
        "what are living benefits", "what is iul", "what is term",
        "how does the process", "what's the process", "whats the process",
        "what do i need to qualify", "do i qualify",
        "can you explain",
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

def _parse_llm_json(raw: str) -> dict:
    """Parse JSON from LLM output, stripping reasoning tags and markdown fencing."""
    raw = re.sub(r'<thinking>[\s\S]*?</thinking>', '', raw).strip()
    raw = re.sub(r'</?(?:thinking|reply|output|response)>', '', raw).strip()
    if raw.startswith("```json"):
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif raw.startswith("```"):
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()
    return json.loads(raw)


# ===================================
# CLASSIFICATION PROMPT (shared across tiers)
# ===================================

_CLASSIFICATION_PROMPT = """You are classifying lead messages in a life insurance sales conversation.

Conversation stage history (most recent at bottom — shows where we have been):
{stage_history}
NOTE: Stages like "objection_handling:already_covered" mean the bot detected an already_covered objection on that turn. Use this history to understand what objections were already raised. If you see the SAME objection type repeated, the lead is stuck on that issue.

Lead messages (most recent at bottom):
{lead_messages}

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
- ready_to_book: the lead is EXPLICITLY asking to schedule or book an appointment. Examples: "can we book a call?", "let's schedule something", "when are you available?", "I'd like to set up a meeting", "what times do you have?", "sign me up", "let's do it", "I'm ready to go". CRITICAL: generic agreement words like "yes", "sure", "ok", "sounds good" are NOT ready_to_book UNLESS the lead is clearly accepting a specific appointment time you offered. "Yes" answering a qualifying question = false. "Yes that time works" after you offered a slot = true
- resistance: strong opt-out: stop, unsubscribe, remove, leave me alone, do not contact, opt out, lose my number, take me off (NOTE: "not interested" is NOT resistance — it is an objection to be handled)
- articulated_impact: the lead has expressed WHY coverage matters to them personally, what would happen to their family without it, the consequences of the gap, or emotional weight behind their need. Not just mentioning a goal but explaining why it is important to them or what would happen if they did not address it

OBJECTION FIELDS (based on the MOST RECENT lead message only):
- objection_type: one of "not_interested", "spouse_partner", "price_money", "already_covered", "busy_timing", "think_about_it", "health_concern", "trust_issue", "none"

COMPOUND OBJECTION PRIORITY: When a message contains MULTIPLE objections, return the MOST IMPORTANT one using this hierarchy (highest priority first): price_money > health_concern > spouse_partner > already_covered > trust_issue > think_about_it > busy_timing > not_interested. Price is ALWAYS #1 because taking money off the table unlocks every other objection. Example: "too expensive and I need to ask my wife" = price_money (not spouse_partner).

CLASSIFICATION MINDSET — Think like a top sales closer:
The ONLY messages that are "none" are: answering your question, asking their own question, expressing genuine interest, agreeing to something, or providing information you asked for. EVERYTHING ELSE is an objection — and every objection is an opportunity.

CRITICAL FALSE POSITIVE RULES — these OVERRIDE the closer mindset:
- "not interested in X, I want Y" / "not interested in X, what about Y?" = NONE (buying signal — they are telling you what they DO want)
- "my wife/husband loves/agrees/supports/wants/is on board" = NONE (supportive spouse)
- "my wife handles the insurance" / "my husband takes care of that" = spouse_partner (deferral — NOT not_interested)
- "already have some but need more" / "have coverage but it's not enough" = NONE (coverage gap = buying signal)
- "think about it all the time" / "think about my family" = NONE (concern, NOT stalling)
- "busy protecting my family" = NONE (commitment, NOT timing)
- "it's not too expensive" / "actually pretty affordable" = NONE (positive price reaction)
- "it's not for me to decide" / "not for me to say" = spouse_partner (deferral, NOT not_interested)
- "done researching, ready to move forward" = NONE (action, NOT dismissal)
- When the lead NEGATES an objection keyword ("not too expensive", "don't need to ask anyone"), that is NOT an objection
- "yes" / "sure" / "ok" answering YOUR question = NONE (providing info, not booking)
- "I have diabetes but I want coverage" / "I know I have health issues, what are my options?" = health_concern (NOT not_interested — they WANT coverage, they doubt they can GET it)
- "my buddy/nephew/cousin sells insurance" with no existing policy = trust_issue (loyalty objection, NOT already_covered unless they have active coverage through that person)
- "I have chosen my policyholder" / "I went with someone else" / "I found an agent" / "I signed with [carrier]" = already_covered (NOT not_interested — they made a purchasing decision, they did not just say "no")
- Any message that mentions CHOOSING, SELECTING, GOING WITH, or SIGNING WITH a carrier/agent/policy = already_covered. Even if combined with "thank you" or "goodbye" language, the reason is the coverage decision, not bare rejection.

OBJECTION TYPE DEFINITIONS:
  "not_interested" = flat rejection with NO REASON GIVEN. The lead does not explain WHY they are declining — just says no. "no thanks", "I'm good", "nah", "pass", "whatever", "bye", "don't care", "I'm done", sarcastic dismissals. CRITICAL: if they give ANY reason (already have coverage, chose someone else, too expensive, busy, need to think), that is a DIFFERENT objection type — not not_interested. "not_interested" is ONLY for bare rejections without explanation. If ambiguous ("hmm", "maybe", "idk"), prefer "think_about_it" — thoughtful leads deserve patience.
  "spouse_partner" = deferring to ANY third party: spouse, partner, family, accountant, lawyer, advisor, broker. "Let me check with...", "My wife/husband handles/decides...", "need to ask..."
  "price_money" = ANY concern about cost, affordability, budget, value. "Too expensive", "can't afford", "fixed income", "not worth it", "money is tight"
  "already_covered" = claims existing protection OR states they already chose/selected/went with a carrier or policy. "I'm covered", "already have", "all set", "taken care of", "I chose/went with/picked/signed with [carrier]", "I have chosen my [policy/carrier/provider/policyholder]", "I found someone", "I went with someone else", "moved forward with another [agent/company]". KEY: if they say they CHOSE or SELECTED a policy/carrier/agent — even if phrasing is unusual ("I have chosen my policyholder") — that is already_covered, NOT not_interested.
  "busy_timing" = can't engage RIGHT NOW: "busy", "at work", "driving", "call back later", "not a good time"
  "think_about_it" = stalling/delaying: "need to think", "sleep on it", "not ready", "get back to you", "rain check", "maybe" (standalone), "I'll let you know", "send me an email", "send me info", "send me a quote", "let me look it over", "let me look into it", "let me do some research", "send me the details", "let me shop around". ALL disguised versions of "I don't have a compelling reason to act now"
  "health_concern" = lead believes they CANNOT qualify due to health, age, or medical history. "I have diabetes", "I had cancer", "they won't insure me", "I probably can't qualify", "I'm too old for this", "I take medication for...", "I have a pre-existing condition". This is NOT not_interested — these people often WANT coverage but believe they cannot get it. Educating them on guaranteed issue, simplified issue, and graded benefit options is the correct response.
  "trust_issue" = distrust of insurance industry, bad past experience, or loyalty to a personal relationship. "Insurance is a scam", "I got burned before", "my last agent screwed me", "I don't trust insurance companies", "my nephew/buddy/cousin sells insurance". This is NOT not_interested — these people have an emotional barrier, not a lack of need.
  "none" = genuinely positive, engaged, asking/answering questions, providing info

- objection_nature: one of "fear_based", "logistical", "none"
  "fear_based" = emotional resistance, avoidance, fear of commitment/being sold to/making a mistake. MOST objections are fear-based.
  "logistical" = genuinely practical: real budget constraint, existing arrangement, scheduling conflict
  "none" = no objection (only when objection_type is also "none")

Context examples:
"I have diabetes, can I even get coverage?" = health_concern + logistical
"Insurance companies are all crooks" = trust_issue + fear_based
"My nephew is an agent" = trust_issue + fear_based
"Too expensive and I need to ask my wife" = price_money + logistical (price takes priority)
"I already have something through work" = already_covered + logistical
"I can't afford that right now" = price_money (could be either)
"Let me talk to my wife first" = spouse_partner + fear_based
"no" / "nah" / "nope" / "whatever" = not_interested + fear_based
"maybe" (by itself) = think_about_it + fear_based
"My wife handles the finances" = spouse_partner + fear_based
"Yeah sounds good" = none + none
"""


def _llm_classify(lead_msgs: List[str], model: str, timeout: float, stage_history: str = "") -> dict:
    """
    Run LLM classification on lead messages. Returns parsed dict or empty dict on failure.
    stage_history: formatted string of past stages so Grok knows the conversation arc.
    """
    if not client or not lead_msgs:
        return {}

    lead_messages_str = chr(10).join([f"[{i+1}] {msg}" for i, msg in enumerate(lead_msgs[-8:])])
    history_str = stage_history if stage_history else "No prior stage history (first interaction or new contact)."
    prompt = _CLASSIFICATION_PROMPT.format(lead_messages=lead_messages_str, stage_history=history_str)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=250,
            timeout=timeout
        )
        raw = response.choices[0].message.content.strip()
        cls = _parse_llm_json(raw)
        if isinstance(cls, dict) and "objection_type" in cls:
            logger.debug(f"LLM classification ({model}): {cls}")
            return cls
        logger.warning(f"LLM classification ({model}) returned invalid structure: {cls}")
        return {}
    except Exception as e:
        logger.warning(f"LLM classification ({model}) failed: {e}")
        return {}


# ===================================
# TIER 3: MINIMAL SAFETY NET (keywords — fires only when ALL LLM calls fail)
# ===================================

def _minimal_keyword_fallback(recent_lead_text: str, all_lead_text: str) -> dict:
    """
    Last-resort classification when all LLM calls fail.
    NOT designed to be comprehensive — just catches critical signals
    (TCPA stops, obvious objections, basic intent) so the bot doesn't
    send a completely wrong response type.

    This should fire <1% of the time in production.
    """
    # TCPA stop detection (legal requirement — must always work)
    stop_keywords = [
        "stop", "unsubscribe", "cancel", "remove me", "opt out",
        "do not contact", "don't contact", "do not call", "don't call",
        "do not text", "don't text", "do not message", "don't message",
    ]
    resistance = any(_has_word(recent_lead_text, kw) for kw in stop_keywords)

    # Basic objection detection via the keyword engine (preserved as safety net)
    obj_type, obj_nature = detect_objection_keywords(recent_lead_text)

    # Simple boolean signals
    coverage_kw = ["policy", "coverage", "insurance", "have insurance", "already have"]
    need_kw = ["need", "want", "looking", "interested", "protect"]
    goal_kw = ["family", "kids", "wife", "husband", "spouse", "children"]

    return {
        "has_coverage":       any(_has_word(all_lead_text, kw) for kw in coverage_kw),
        "needs_coverage":     any(_has_word(all_lead_text, kw) for kw in need_kw),
        "mentioned_goal":     any(_has_word(all_lead_text, kw) for kw in goal_kw),
        "mentioned_obstacle": False,  # too noisy without LLM context
        "ready_to_book":      False,  # too risky to guess — let booking_detection.py handle it
        "resistance":         resistance,
        "articulated_impact": False,  # requires understanding context, not just keywords
        "objection_type":     obj_type.value,
        "objection_nature":   obj_nature.value,
    }


# ===================================
# MAIN ANALYSIS
# ===================================

# LLM classification model hierarchy:
#   Tier 1: grok-4-1-fast-non-reasoning — best accuracy, ~500-1000ms, no thinking tags
#   Tier 2: grok-3-mini-fast — fastest, cheaper, still understands context
#   Tier 3: keyword safety net — no LLM cost, basic signals only
CLASSIFY_MODEL_PRIMARY = "grok-4-1-fast-non-reasoning"
CLASSIFY_MODEL_FALLBACK = "grok-3-mini-fast"


def analyze_logic_flow(messages: List[Dict[str, str]], message: str = "", age: int = 0) -> LogicSignal:
    """
    Analyze recent conversation to produce LogicSignal.

    Architecture (2026 — LLM-primary, keyword safety net):
      Tier 1: LLM classification (grok-4-1-fast-non-reasoning, 6s timeout)
      Tier 2: LLM retry (grok-3-mini-fast, 4s timeout) — if Tier 1 fails
      Tier 3: Minimal keyword safety net — TCPA + basic signals only (<1% of calls)

    The LLM understands full conversational context, negation, sarcasm, and nuance.
    Keywords cannot. The keyword fallback exists only for resilience when the API is down.

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

    # ─── Build stage history from stamped assistant messages ───
    # Each assistant reply carries the stage it was sent from. Reading these in order
    # gives Grok a clear arc: "qualifying → objection_handling → rapport → qualifying"
    # so it knows where the conversation has been, not just where it is right now.
    stage_entries = [
        m['stage'] for m in messages
        if m['role'] == 'assistant' and m.get('stage')
    ]
    if stage_entries:
        stage_history = " → ".join(stage_entries[-8:])  # last 8 turns is more than enough
    else:
        stage_history = ""

    # ─── Tier 1: Primary LLM classification (best model, 6s timeout) ───
    cls = _llm_classify(lead_msgs, CLASSIFY_MODEL_PRIMARY, timeout=6.0, stage_history=stage_history)

    # ─── Tier 2: Fallback LLM (faster model, 4s timeout) ───
    if not cls:
        logger.info("Tier 1 LLM failed. Trying Tier 2 fallback model.")
        cls = _llm_classify(lead_msgs, CLASSIFY_MODEL_FALLBACK, timeout=4.0, stage_history=stage_history)

    # ─── Tier 3: Minimal keyword safety net (API completely down) ───
    if not cls:
        logger.warning("Both LLM tiers failed. Using minimal keyword safety net.")
        cls = _minimal_keyword_fallback(recent_lead_text, all_lead_text)

    # ─── Parse objection from classification ───
    obj_type_str = cls.get("objection_type", "none")
    obj_nature_str = cls.get("objection_nature", "none")

    try:
        objection_type = ObjectionType(obj_type_str)
    except ValueError:
        logger.warning(f"LLM returned invalid objection_type '{obj_type_str}' — defaulting to NONE. "
                       f"Recent lead text: '{recent_lead_text[:80]}'. This may cause a real objection to be missed.")
        objection_type = ObjectionType.NONE

    try:
        objection_nature = ObjectionNature(obj_nature_str)
    except ValueError:
        logger.warning(f"LLM returned invalid objection_nature '{obj_nature_str}' — defaulting to NONE.")
        objection_nature = ObjectionNature.NONE

    # ═══════════════════════════════════════════════════════════════════
    # LLM-KEYWORD CROSS-VALIDATION
    # ═══════════════════════════════════════════════════════════════════
    # The LLM is primary, but it can miss. Keywords ALWAYS run as a
    # safety net. Two rules:
    #
    # 1. LLM says NONE but keywords found a real objection → UPGRADE
    #    to the keyword result. An objection that gets missed = the bot
    #    sends a qualifying question to someone who just said "no thanks."
    #    That's worse than over-classifying.
    #
    # 2. LLM says NOT_INTERESTED but keywords found a more specific type
    #    (already_covered, price_money, etc.) → PREFER the specific type.
    #    not_interested is the catch-all bucket. A specific type gets
    #    specific tactical guidance. Generic gets generic.
    #
    # 3. LLM says a specific type and keywords agree or disagree → TRUST
    #    the LLM. It has full conversational context, keywords don't.
    #
    # This ensures: every real objection reaches OBJECTION_HANDLING stage.
    # ═══════════════════════════════════════════════════════════════════

    kw_obj_type, kw_obj_nature = detect_objection_keywords(recent_lead_text)

    if kw_obj_type != ObjectionType.NONE:
        if objection_type == ObjectionType.NONE:
            # Rule 1: LLM missed it, keywords caught it → upgrade
            logger.info(
                f"CROSS-VALIDATION UPGRADE: LLM said 'none' but keywords detected "
                f"'{kw_obj_type.value}' | msg='{recent_lead_text[:80]}' | Upgrading to keyword result."
            )
            objection_type = kw_obj_type
            objection_nature = kw_obj_nature

        elif objection_type == ObjectionType.NOT_INTERESTED and kw_obj_type != ObjectionType.NOT_INTERESTED:
            # Rule 2: LLM said generic not_interested, keywords found specific type → prefer specific
            logger.info(
                f"CROSS-VALIDATION REFINE: LLM said 'not_interested' but keywords detected "
                f"more specific '{kw_obj_type.value}' | msg='{recent_lead_text[:80]}' | Using specific type."
            )
            objection_type = kw_obj_type
            objection_nature = kw_obj_nature
    elif objection_type == ObjectionType.NONE:
        # Neither LLM nor keywords found anything — truly no objection
        pass

    logger.info(
        f"Classification final | msg='{recent_lead_text[:60]}' | "
        f"llm={obj_type_str} | kw={kw_obj_type.value} | final={objection_type.value}"
    )

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
    ins_ctx = analyze_insurance_context(message or "", all_lead_text, age=age)

    # ─── Depth guard: too many technical questions = book appointment ───
    too_deep = bool(ins_ctx.guidance_note and "DEPTH GUARD" in ins_ctx.guidance_note)

    # ─── Rapport turn tracking ───
    rapport_turns = _count_consecutive_rapport_turns(messages) if messages else 0

    # ─── Check if current message has ANY qualifying substance ───
    has_qualifying_substance = (
        cls.get("has_coverage", False)
        or cls.get("needs_coverage", False)
        or cls.get("mentioned_goal", False)
        or cls.get("mentioned_obstacle", False)
        or cls.get("ready_to_book", False)
        or cls.get("articulated_impact", False)
        or buying_signal != BuyingSignalType.NONE
        or objection_type != ObjectionType.NONE
    )

    # ─── Stage logic (priority order) ───
    stage = ConversationStage.QUALIFYING

    if booking_confirmed:
        # Already booked — nothing else matters
        stage = ConversationStage.BOOKED

    elif cls.get("ready_to_book", False):
        # Lead directly asked to book/schedule/meet — honor it immediately.
        # No conversation depth requirement: if they say "can we book Monday?"
        # on the first reply, the bot should book, not keep qualifying.
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

    elif (cls.get("needs_coverage", False) or cls.get("mentioned_goal", False)) and conversation_count >= 8:
        # Conversation depth timeout: 8+ exchanges with a known gap but no
        # articulated impact. The bot has had plenty of chances to draw it out.
        # Continuing to ask "what would happen?" feels interrogative. Push to
        # booking — a call will uncover the rest naturally.
        stage = ConversationStage.BOOKING

    elif (cls.get("needs_coverage", False) or cls.get("mentioned_goal", False)) and conversation_count >= 2:
        # Gap found but lead hasn't expressed why it matters yet — stay qualifying
        stage = ConversationStage.QUALIFYING

    elif conversation_count == 0:
        stage = ConversationStage.INITIAL_OUTREACH

    elif (msg_context == MessageContext.INBOUND_REPLY
          and not has_qualifying_substance
          and conversation_count >= 1
          and rapport_turns < 2):
        # Lead is talking but NOT about insurance — this is rapport.
        # They're being human: chit-chat, personal stories, reactions.
        # Allow 1-2 rapport turns to build trust before pivoting back.
        stage = ConversationStage.RAPPORT

    # If rapport_turns >= 2 and no qualifying substance, stage stays QUALIFYING
    # (the default) which forces the bot to steer back to business.

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
        consecutive_rapport_turns=rapport_turns,
    )
