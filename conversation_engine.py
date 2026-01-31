# conversation_engine.py - Simplified Conversational Logic
# "Less thinking, more listening"

import logging
from enum import Enum
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

class ConversationStage(Enum):
    INITIAL_OUTREACH = "initial_outreach"  # First contact
    QUALIFYING = "qualifying"               # Find situation, goal, obstacles
    BOOKING = "booking"                     # Getting appointment
    BOOKED = "booked"                       # Done

@dataclass
class LogicSignal:
    stage: ConversationStage
    has_coverage: bool           # Mentioned existing policy
    needs_coverage: bool         # Expressed need/interest
    mentioned_goal: bool         # Talked about who/what protecting
    mentioned_obstacle: bool     # Revealed why they haven't acted
    ready_to_book: bool          # Agreement signals
    resistance: bool             # Pushback/objections
    conversation_count: int      # Exchange depth

def analyze_logic_flow(messages: List[dict]) -> LogicSignal:
    """
    Simplified conversation analysis.
    Just track: Do they have coverage? Do they need it? Are they ready to book?
    """

    if not messages or len(messages) == 0:
        return LogicSignal(
            stage=ConversationStage.INITIAL_OUTREACH,
            has_coverage=False,
            needs_coverage=False,
            mentioned_goal=False,
            mentioned_obstacle=False,
            ready_to_book=False,
            resistance=False,
            conversation_count=0
        )

    # Separate lead and bot messages
    lead_msgs = [m for m in messages if m['role'] == 'lead']
    bot_msgs = [m for m in messages if m['role'] == 'assistant']
    conversation_count = len(lead_msgs)

    # Combine all lead text for analysis
    all_lead_text = " ".join([m['text'].lower() for m in lead_msgs])
    last_lead_text = lead_msgs[-1]['text'].lower() if lead_msgs else ""

    # === SIMPLE SIGNAL DETECTION ===

    # Coverage signals
    coverage_keywords = ["policy", "coverage", "term", "whole life", "iul", "universal", "work policy",
                        "group", "state farm", "farmers", "allstate", "have insurance", "already have",
                        "$100k", "$250k", "$500k", "million"]
    has_coverage = any(keyword in all_lead_text for keyword in coverage_keywords)

    # Need signals
    need_keywords = ["need", "want", "looking", "interested", "thinking about", "family", "kids",
                    "wife", "husband", "protect", "mortgage", "business", "future", "worry"]
    needs_coverage = any(keyword in all_lead_text for keyword in need_keywords)

    # Goal signals (who/what protecting)
    goal_keywords = ["family", "kids", "children", "wife", "husband", "spouse", "mortgage",
                    "business", "partner", "parents", "funeral", "debt", "college"]
    mentioned_goal = any(keyword in all_lead_text for keyword in goal_keywords)

    # Obstacle signals (why they haven't acted)
    obstacle_keywords = ["busy", "expensive", "too much", "later", "thinking", "not sure",
                        "confused", "don't understand", "complicated", "health", "been meaning"]
    mentioned_obstacle = any(keyword in all_lead_text for keyword in obstacle_keywords)

    # Booking signals
    booking_keywords = ["yes", "sure", "ok", "sounds good", "let's do it", "book", "schedule",
                       "appointment", "call", "zoom", "meet", "when", "available", "time"]
    ready_to_book = any(keyword in last_lead_text for keyword in booking_keywords)

    # Hard stop signals only — actual opt-out requests
    # "not interested" etc. are objection smokescreens, not real stops
    stop_keywords = ["stop", "unsubscribe", "remove me", "take me off",
                     "leave me alone", "do not contact"]
    resistance = any(keyword in last_lead_text for keyword in stop_keywords)

    # === STAGE DETECTION ===

    # Check if booking was already confirmed (bot said "all set", "booked", etc.)
    booking_confirmed = False
    if bot_msgs:
        recent_bot_text = " ".join([m['text'].lower() for m in bot_msgs[-2:]])
        confirmation_patterns = ["all set", "booked", "calendar invite",
                                 "confirmed", "appointment is", "see you at",
                                 "looking forward"]
        booking_confirmed = any(p in recent_bot_text for p in confirmation_patterns)

    # Initial outreach
    if conversation_count == 0:
        stage = ConversationStage.INITIAL_OUTREACH

    # Booked — only when bot already confirmed a booking
    elif booking_confirmed:
        stage = ConversationStage.BOOKED

    # Booking — they're ready or warm enough, offer times
    elif ready_to_book and conversation_count >= 2:
        stage = ConversationStage.BOOKING

    elif (needs_coverage or mentioned_goal) and conversation_count >= 2:
        stage = ConversationStage.BOOKING

    # Still qualifying
    else:
        stage = ConversationStage.QUALIFYING

    return LogicSignal(
        stage=stage,
        has_coverage=has_coverage,
        needs_coverage=needs_coverage,
        mentioned_goal=mentioned_goal,
        mentioned_obstacle=mentioned_obstacle,
        ready_to_book=ready_to_book,
        resistance=resistance,
        conversation_count=conversation_count
    )
