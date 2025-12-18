"""
Three-Layer Conversation Architecture
=====================================

Layer 1: Base Model (Grok) - Can't change, guided via prompts
Layer 2: Conversation Policy / State Machine (This file) - Track stage, validate responses
Layer 3: Playbook / Knowledge Library (playbook.py) - Ideal replies for common situations

This module implements Layer 2: The conversation state machine and policy engine.
"""

import re
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)

class ConversationStage(Enum):
    INITIAL_OUTREACH = "initial_outreach"
    DISCOVERY = "discovery"
    OBJECTION_HANDLING = "objection"
    QUALIFICATION = "qualification"
    CLOSING = "closing"

@dataclass
class ConversationState:
    contact_id: str
    first_name: str
    
    stage: ConversationStage = ConversationStage.INITIAL_OUTREACH
    
    facts: Dict[str, Any] = field(default_factory=lambda: {
        "family": {"spouse": None, "kids": None, "dependents": None},
        "coverage": {"has_any": None, "type": None, "amount": None, "employer": None, "guaranteed_issue": None, "carrier": None},
        "health": {"conditions": [], "details": []},
        "age": None,
        "employment": None,
        "motivating_goal": None,
        "blockers": []
    })
    
    questions_asked: List[str] = field(default_factory=list)
    topics_asked: List[str] = field(default_factory=list)  # ← ONLY THIS — no topics_answered
    
    soft_dismissive_count: int = 0
    hard_dismissive: bool = False
    exchange_count: int = 0
    
    has_engaged: bool = False
    has_shared_problem: bool = False
    has_shown_interest: bool = False
    ready_to_close: bool = False

def detect_stage(state: ConversationState, current_message: str, conversation_history: List[str]) -> ConversationStage:
    msg_lower = current_message.lower()
    
    if state.hard_dismissive:
        return ConversationStage.OBJECTION_HANDLING
    
    time_patterns = [
        r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
        r'\b(tomorrow|today|tonight|this week|next week)\b',
        r'\b\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)\b',
        r'\b(morning|afternoon|evening)\b',
        r'works for me', r'that time', r'set it up', r'book it', r'schedule'
    ]
    if any(re.search(p, msg_lower) for p in time_patterns):
        state.ready_to_close = True
        return ConversationStage.CLOSING
    
    buying_signals = [
        r"what.*next", r"how.*work", r"what.*cost", r"how much",
        r"sign me up", r"let'?s do (it|this)", r"i'?m ready", r"sounds good",
        r"i'?m interested", r"tell me more", r"what do i need",
        r"when can we", r"can we set", r"i want to"
    ]
    if any(re.search(p, msg_lower) for p in buying_signals):
        state.has_shown_interest = True
        return ConversationStage.CLOSING
    
    objection_patterns = [
        r"not interested", r"no thanks", r"i'?m good", r"already have",
        r"don'?t need", r"not right now", r"too expensive", r"can'?t afford"
    ]
    if any(re.search(p, msg_lower) for p in objection_patterns):
        return ConversationStage.OBJECTION_HANDLING
    
    health_patterns = [
        r"diabetes", r"heart", r"cancer", r"copd", r"stroke",
        r"health (issues?|problems?|conditions?)"
    ]
    if any(re.search(p, msg_lower) for p in health_patterns):
        return ConversationStage.QUALIFICATION
    
    if state.exchange_count <= 2 and not state.has_engaged:
        return ConversationStage.INITIAL_OUTREACH
    
    if state.exchange_count >= 2 and not state.has_shared_problem:
        return ConversationStage.DISCOVERY
    
    if state.exchange_count >= 3:
        has_family_info = state.facts["family"]["spouse"] is not None or state.facts["family"]["kids"] is not None
        has_coverage_info = state.facts["coverage"]["has_any"] is not None
        has_motivation = state.facts["motivating_goal"] is not None
        facts_known = sum([has_family_info, has_coverage_info, has_motivation])
        if facts_known >= 2:
            return ConversationStage.CLOSING
    
    return ConversationStage.DISCOVERY

def extract_facts_from_message(state: ConversationState, message: str) -> Dict[str, Any]:
    # SOLUTION: Safe nested initialization
    state.facts = state.facts or {}
    state.facts.setdefault("coverage", {"has_any": None, "type": None, "amount": None, "employer": None, "guaranteed_issue": None, "carrier": None})
    state.facts.setdefault("family", {"spouse": None, "kids": None, "dependents": None})
    state.facts.setdefault("health", {"conditions": [], "details": []})

    msg_lower = message.lower()
    new_facts = {}
    
    # Family
    if re.search(r'wife|husband|spouse|married', msg_lower):
        state.facts["family"]["spouse"] = True
        new_facts["spouse"] = True
        add_to_qualification_array(state.contact_id, "topics_asked", "marital_status")
    
    kids_match = re.search(r'(\d+)\s*kids?', msg_lower)
    if kids_match:
        state.facts["family"]["kids"] = int(kids_match.group(1))
        new_facts["kids"] = int(kids_match.group(1))
        add_to_qualification_array(state.contact_id, "topics_asked", "kids")
    
    # Coverage
    if re.search(r'(employer|work|job)\s*(coverage|policy|insurance)', msg_lower):
        state.facts["coverage"]["employer"] = True
        new_facts["employer_coverage"] = True
        add_to_qualification_array(state.contact_id, "topics_asked", "coverage_source")
    
    if re.search(r'no\s*(coverage|insurance|policy)|don\'?t have|nothing', msg_lower):
        state.facts["coverage"]["has_any"] = False
        new_facts["no_coverage"] = True
        add_to_qualification_array(state.contact_id, "topics_asked", "coverage")
    
    # Health
    health_conditions = {
        "diabetes": r'diabet(es|ic)',
        "heart": r'heart',
        "cancer": r'cancer',
        "copd": r'copd|emphysema',
        "stroke": r'stroke'
    }
    for condition, pattern in health_conditions.items():
        if re.search(pattern, msg_lower):
            if condition not in state.facts["health"]["conditions"]:
                state.facts["health"]["conditions"].append(condition)
                new_facts[f"health_{condition}"] = True
            add_to_qualification_array(state.contact_id, "topics_asked", "health")
    
    # Motivation
    motivation_patterns = [
        (r'(protect|take care of).*(family|wife|kids)', "family protection"),
        (r'(new|just had).*(baby|kid)', "new baby"),
        (r'(bought|buying).*(house|mortgage)', "new home"),
        (r'retire|retirement', "retirement planning")
    ]
    for pattern, motivation in motivation_patterns:
        if re.search(pattern, msg_lower):
            state.facts["motivating_goal"] = motivation
            new_facts["motivation"] = motivation
            add_to_qualification_array(state.contact_id, "topics_asked", "motivation")
            break
    
    return new_facts

def detect_dismissive(message: str) -> tuple:
    msg_lower = message.lower()
    soft = any(p in msg_lower for p in ["not telling", "personal", "why do you need", "none of your business"])
    hard = any(p in msg_lower for p in ["stop", "unsubscribe", "remove me", "do not contact"])
    return soft, hard

def get_stage_objectives(stage: ConversationStage) -> Dict[str, Any]:
    # (your original get_stage_objectives — unchanged)
    # ... (keep your full function)

    return objectives.get(stage, objectives[ConversationStage.DISCOVERY])

# (keep build_state_from_history, format_state_for_prompt, PolicyEngine, parse_reflection, strip_reflection unchanged)

# Only change in PolicyEngine.validate_response — use topics_asked

class PolicyEngine:
    @staticmethod
    def validate_response(response: str, state: ConversationState, reflection_scores: Optional[Dict[str, int]] = None) -> tuple:
        response_lower = response.lower()
        
        # Universal rules (length, em dashes, etc.) — unchanged
        
        # Re-asking check using topics_asked
        topic_patterns = {
            "marital_status": ["are you married", "do you have a spouse"],
            "kids": ["do you have kids", "how many kids"],
            "coverage": ["do you have coverage", "are you currently covered"],
            "coverage_source": ["through work", "through your employer"],
            "motivation": ["what got you", "why did you", "what made you look"]
        }
        
        for topic in state.topics_asked:
            if topic in topic_patterns:
                for pattern in topic_patterns[topic]:
                    if pattern in response_lower:
                        return False, f"Re-asked about {topic}", "You already know this — reference it instead"
        
        # (rest of validation unchanged)

        return True, None, None
