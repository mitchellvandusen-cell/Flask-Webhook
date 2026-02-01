# conversation_engine.py - Simplified Conversational Logic
# Updated Jan 2026: LLM-assisted intent detection for better nuance & progression

import logging
import re
import json
from enum import Enum
from dataclasses import dataclass
from typing import List, Dict

from openai import OpenAI  # assuming this is already imported globally in your project

logger = logging.getLogger(__name__)

# Assuming client is initialized globally somewhere (e.g. in memory.py or main app)
# If not, you can add it here or pass it in

class ConversationStage(Enum):
    INITIAL_OUTREACH = "initial_outreach"  # First contact or very early
    QUALIFYING       = "qualifying"        # Discovering situation, goal, obstacles
    BOOKING          = "booking"           # Offering / confirming times
    BOOKED           = "booked"            # Appointment confirmed

@dataclass
class LogicSignal:
    stage: ConversationStage
    has_coverage: bool           # Mentioned existing policy/coverage
    needs_coverage: bool         # Expressed need/interest
    mentioned_goal: bool         # Talked about who/what they're protecting
    mentioned_obstacle: bool     # Revealed barrier to action
    ready_to_book: bool          # Agreement to call/meet/book/next step
    resistance: bool             # Strong pushback / opt-out signals
    conversation_count: int      # Number of lead messages (depth)

def _has_word(text: str, keyword: str) -> bool:
    """Word-boundary safe keyword match."""
    return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text, re.IGNORECASE))


def analyze_logic_flow(messages: List[Dict[str, str]]) -> LogicSignal:
    """
    Analyze recent conversation to produce LogicSignal.
    Uses a small Grok call for accurate intent detection + fallback to keywords.
    """
    if not messages:
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

    # Split messages
    lead_msgs = [m['text'].lower() for m in messages if m['role'] == 'lead']
    bot_msgs  = [m['text'].lower() for m in messages if m['role'] == 'assistant']

    conversation_count = len(lead_msgs)
    all_lead_text = " ".join(lead_msgs)
    recent_lead_text = " ".join(lead_msgs[-4:]) if lead_msgs else ""  # last 4 replies for booking signals

    # ─── Primary: Small Grok intent classification ───
    cls = {}
    if client:  # assuming global client from xAI
        prompt = f"""You are classifying short lead text messages in a sales conversation about life insurance.

Lead messages (most recent at bottom):
{chr(10).join([f"[{i+1}] {msg}" for i, msg in enumerate(lead_msgs[-8:])])}

Return ONLY valid JSON with these exact keys (true/false):

{{
  "has_coverage": bool,           // they mention having any existing life insurance/policy/coverage
  "needs_coverage": bool,         // expressed need, want, interest, looking, thinking about coverage
  "mentioned_goal": bool,         // protecting family, kids, spouse, mortgage, business, future, etc.
  "mentioned_obstacle": bool,     // barrier like busy, expensive, health issue, not sure, complicated
  "ready_to_book": bool,          // agreed to call/meet/talk/book/time works/yes/lets do it/sure/next step
  "resistance": bool              // strong opt-out: stop, unsubscribe, not interested, leave me alone
}}

Be accurate and context-aware. "I have something through work" → has_coverage: true
"My wife would be screwed without me" → mentioned_goal: true
"Yeah let's talk next week" → ready_to_book: true
"""

        try:
            response = client.chat.completions.create(
                model="grok-4-1-fast-reasoning",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.15,
                max_tokens=200,
                timeout=8.0
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```json"):
                raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
            cls = json.loads(raw)
            logger.debug(f"LLM intent classification succeeded: {cls}")
        except Exception as e:
            logger.warning(f"LLM intent classification failed: {e}. Falling back to keywords.")
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
        booking_keywords = ["yes", "sure", "ok", "sounds good", "let's do", "book", "schedule", "appointment", "call", "talk", "meet", "time work", "works for me"]
        stop_keywords = ["stop", "unsubscribe", "remove", "leave me alone", "do not contact"]

        cls = {
            "has_coverage":     any(_has_word(all_lead_text, kw) for kw in coverage_keywords),
            "needs_coverage":   any(_has_word(all_lead_text, kw) for kw in need_keywords),
            "mentioned_goal":   any(_has_word(all_lead_text, kw) for kw in goal_keywords),
            "mentioned_obstacle": any(_has_word(all_lead_text, kw) for kw in obstacle_keywords),
            "ready_to_book":    any(_has_word(recent_lead_text, kw) for kw in booking_keywords),
            "resistance":       any(_has_word(recent_lead_text, kw) for kw in stop_keywords),
        }

    # ─── Booking confirmed by bot recently? ───
    booking_confirmed = False
    if bot_msgs:
        recent_bot = " ".join(bot_msgs[-3:])  # last 3 bot messages
        confirmation_patterns = [
            "all set", "you're booked", "appointment is", "calendar invite",
            "confirmed", "see you at", "looking forward", "locked in", "set for"
        ]
        booking_confirmed = any(p in recent_bot for p in confirmation_patterns)

    # ─── Stage logic ───
    stage = ConversationStage.QUALIFYING

    if booking_confirmed:
        stage = ConversationStage.BOOKED
    elif cls.get("ready_to_book", False) and conversation_count >= 2:
        stage = ConversationStage.BOOKING
    elif (cls.get("needs_coverage", False) or cls.get("mentioned_goal", False)) and conversation_count >= 2:
        stage = ConversationStage.BOOKING
    elif conversation_count == 0:
        stage = ConversationStage.INITIAL_OUTREACH

    return LogicSignal(
        stage=stage,
        has_coverage=cls.get("has_coverage", False),
        needs_coverage=cls.get("needs_coverage", False),
        mentioned_goal=cls.get("mentioned_goal", False),
        mentioned_obstacle=cls.get("mentioned_obstacle", False),
        ready_to_book=cls.get("ready_to_book", False),
        resistance=cls.get("resistance", False),
        conversation_count=conversation_count
    )