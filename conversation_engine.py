"""
Three-Layer Conversation Architecture
=====================================

Layer 1: Base Model (Grok) - Can't change, guided via prompts
Layer 2: Conversation Policy / State Machine (This file) - Track stage, validate responses
Layer 3: Playbook / Knowledge Library (playbook.py) - Ideal replies for common situations

This module implements Layer 2: The conversation state machine and policy engine.
"""

import re
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class ConversationStage(Enum):
    """Explicit conversation stages with clear objectives"""
    INITIAL_OUTREACH = "initial_outreach"  # First contact, get them talking
    DISCOVERY = "discovery"                 # Uncover pain points, family, coverage gaps
    OBJECTION_HANDLING = "objection"        # Address concerns, resistance
    QUALIFICATION = "qualification"         # Determine fit, health questions for GI
    CLOSING = "closing"                     # Offer appointment times, book


@dataclass
class ConversationState:
    """
    Tracks conversation state per contact.
    This is the source of truth - not the LLM.
    """
    contact_id: str
    first_name: str
    
    # Current stage
    stage: ConversationStage = ConversationStage.INITIAL_OUTREACH
    
    # What we know (facts extracted from lead messages only)
    facts: Dict[str, Any] = field(default_factory=lambda: {
        "family": {"spouse": None, "kids": None, "dependents": None},
        "coverage": {"has_any": None, "type": None, "amount": None, "employer": None, "guaranteed_issue": None, "carrier": None},
        "health": {"conditions": [], "details": []},
        "age": None,
        "employment": None,
        "motivating_goal": None,
        "blockers": []
    })
    
    # What questions have been asked (prevents repeats)
    questions_asked: List[str] = field(default_factory=list)
    
    # What topics the lead has answered (prevents re-asking)
    topics_answered: List[str] = field(default_factory=list)
    
    # Resistance tracking
    soft_dismissive_count: int = 0
    hard_dismissive: bool = False
    
    # Exchange count (for stage progression)
    exchange_count: int = 0
    
    # Flags for stage completion
    has_engaged: bool = False          # They responded with something meaningful
    has_shared_problem: bool = False   # They've shared a pain point or concern
    has_shown_interest: bool = False   # They've indicated interest in learning more
    ready_to_close: bool = False       # They've asked about next steps or shown buying signals


def detect_stage(state: ConversationState, current_message: str, conversation_history: List[str]) -> ConversationStage:
    """
    Deterministic stage detection based on conversation signals.
    Returns the appropriate stage based on:
    - What facts we know
    - How many exchanges have occurred
    - What signals the lead has given
    """
    msg_lower = current_message.lower()
    
    # HARD RULES: These override everything
    
    # If hard dismissive, we're done
    if state.hard_dismissive:
        return ConversationStage.OBJECTION_HANDLING
    
    # If they give a time/date, go straight to closing
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
    
    # Strong buying signals -> CLOSING
    buying_signals = [
        r"what.*next", r"how.*work", r"what.*cost", r"how much",
        r"sign me up", r"let'?s do (it|this)", r"i'?m ready", r"sounds good",
        r"i'?m interested", r"tell me more", r"what do i need",
        r"when can we", r"can we set", r"i want to"
    ]
    if any(re.search(p, msg_lower) for p in buying_signals):
        state.has_shown_interest = True
        return ConversationStage.CLOSING
    
    # Objection/resistance signals -> OBJECTION_HANDLING
    objection_patterns = [
        r"not interested", r"no thanks", r"i'?m good", r"already have",
        r"don'?t need", r"not right now", r"too expensive", r"can'?t afford",
        r"think about it", r"not sure", r"maybe later", r"i'?ll pass"
    ]
    if any(re.search(p, msg_lower) for p in objection_patterns):
        return ConversationStage.OBJECTION_HANDLING
    
    # Health conditions mentioned -> QUALIFICATION
    health_patterns = [
        r"diabetes", r"heart", r"cancer", r"copd", r"stroke",
        r"health (issues?|problems?|conditions?)", r"can'?t qualify",
        r"guaranteed issue", r"colonial penn", r"globe life"
    ]
    if any(re.search(p, msg_lower) for p in health_patterns):
        return ConversationStage.QUALIFICATION
    
    # SOFT RULES: Based on conversation progression
    
    # First 1-2 exchanges: INITIAL_OUTREACH
    if state.exchange_count <= 2 and not state.has_engaged:
        return ConversationStage.INITIAL_OUTREACH
    
    # Once they've engaged, move to DISCOVERY
    if state.exchange_count >= 2 and not state.has_shared_problem:
        return ConversationStage.DISCOVERY
    
    # After 3+ exchanges with some facts known, check for closing readiness
    if state.exchange_count >= 3:
        # Do we know enough to close?
        has_family_info = state.facts["family"]["spouse"] is not None or state.facts["family"]["kids"] is not None
        has_coverage_info = state.facts["coverage"]["has_any"] is not None
        has_motivation = state.facts["motivating_goal"] is not None
        
        # If we have 2+ facts, we can start moving toward closing
        facts_known = sum([has_family_info, has_coverage_info, has_motivation])
        if facts_known >= 2:
            return ConversationStage.CLOSING
    
    # Default to DISCOVERY
    return ConversationStage.DISCOVERY


def extract_facts_from_message(state: ConversationState, message: str) -> Dict[str, Any]:
    """
    Extract structured facts from a lead message.
    Updates the state with new facts.
    Returns dict of newly extracted facts.
    """
    msg_lower = message.lower()
    new_facts = {}
    
    # Family detection
    if re.search(r'wife|husband|spouse|married', msg_lower):
        state.facts["family"]["spouse"] = True
        new_facts["spouse"] = True
        if "marital_status" not in state.topics_answered:
            state.topics_answered.append("marital_status")
    
    kids_match = re.search(r'(\d+)\s*kids?', msg_lower)
    if kids_match:
        state.facts["family"]["kids"] = int(kids_match.group(1))
        new_facts["kids"] = int(kids_match.group(1))
        if "kids" not in state.topics_answered:
            state.topics_answered.append("kids")
    
    if re.search(r'children|family|dependents', msg_lower):
        state.facts["family"]["dependents"] = True
        new_facts["dependents"] = True
    
    # Coverage detection
    coverage_match = re.search(r'(\d+)k?\s*(through|from|at|via)\s*work', msg_lower)
    if coverage_match:
        state.facts["coverage"]["has_any"] = True
        state.facts["coverage"]["employer"] = True
        state.facts["coverage"]["amount"] = coverage_match.group(1) + "k"
        new_facts["employer_coverage"] = coverage_match.group(1) + "k"
        if "coverage" not in state.topics_answered:
            state.topics_answered.append("coverage")
    
    if re.search(r'(employer|work|job)\s*(coverage|policy|insurance)', msg_lower):
        state.facts["coverage"]["employer"] = True
        new_facts["employer_coverage"] = True
        if "coverage_source" not in state.topics_answered:
            state.topics_answered.append("coverage_source")
    
    if re.search(r'colonial\s*penn|globe\s*life|aarp|guaranteed\s*(issue|acceptance)', msg_lower):
        state.facts["coverage"]["guaranteed_issue"] = True
        new_facts["guaranteed_issue"] = True
    
    if re.search(r'no\s*(coverage|insurance|policy)|don\'?t have|nothing', msg_lower):
        state.facts["coverage"]["has_any"] = False
        new_facts["no_coverage"] = True
    
    # Health conditions
    health_conditions = {
        "diabetes": r'diabet(es|ic)',
        "heart": r'heart\s*(attack|disease|condition|problems?)|cardiac',
        "cancer": r'cancer',
        "copd": r'copd|emphysema|lung\s*(disease|condition)',
        "stroke": r'stroke'
    }
    for condition, pattern in health_conditions.items():
        if re.search(pattern, msg_lower):
            if condition not in state.facts["health"]["conditions"]:
                state.facts["health"]["conditions"].append(condition)
                new_facts[f"health_{condition}"] = True
    
    # Motivation detection
    motivation_patterns = [
        (r'wife.*(bug|ask|want|nag|push)', "spouse pressure"),
        (r'(protect|take care of|provide for).*(family|wife|kids|children)', "family protection"),
        (r'(new|just had).*(baby|kid|child)', "new baby"),
        (r'(bought|buying|mortgage|house)', "new home"),
        (r'(retire|retiring|retirement)', "retirement planning"),
        (r'(job|work|employer).*(change|switch|leave|quit)', "job change")
    ]
    for pattern, motivation in motivation_patterns:
        if re.search(pattern, msg_lower):
            state.facts["motivating_goal"] = motivation
            new_facts["motivation"] = motivation
            if "motivation" not in state.topics_answered:
                state.topics_answered.append("motivation")
            break
    
    return new_facts


def detect_dismissive(message: str) -> tuple:
    """
    Detect if message is dismissive (soft or hard).
    Returns (is_soft_dismissive, is_hard_dismissive)
    """
    msg_lower = message.lower()
    
    soft_dismissive_phrases = [
        "not telling you", "none of your business", "why do you need",
        "thats personal", "that's personal", "too personal",
        "dont want to say", "don't want to say", "not your concern",
        "mind your own", "private", "why should i tell you",
        "what does that matter", "why does that matter"
    ]
    
    hard_dismissive_phrases = [
        "leave me alone", "stop texting", "stop messaging", "stop contacting",
        "remove me", "unsubscribe", "take me off", "do not contact",
        "dont call", "don't call", "never contact"
    ]
    
    is_soft = any(phrase in msg_lower for phrase in soft_dismissive_phrases)
    is_hard = any(phrase in msg_lower for phrase in hard_dismissive_phrases)
    
    return is_soft, is_hard


def get_stage_objectives(stage: ConversationStage) -> Dict[str, Any]:
    """
    Returns explicit objectives and constraints for each stage.
    This tells the LLM exactly what to do and NOT do.
    """
    objectives = {
        ConversationStage.INITIAL_OUTREACH: {
            "goal": "Get them talking. Find out what originally got them looking.",
            "do": [
                "Ask open-ended curiosity question about what got them looking",
                "Reference they looked at insurance before (acknowledge they're busy)",
                "Keep it casual, friendly, not salesy"
            ],
            "dont": [
                "Ask about income or specific policy details",
                "Push for appointment yet",
                "Ask multiple questions in one message"
            ],
            "success_signal": "They share any reason they were looking (family, work, etc.)"
        },
        ConversationStage.DISCOVERY: {
            "goal": "Uncover pain points and coverage gaps. Build emotional connection.",
            "do": [
                "Ask about family situation if unknown",
                "Probe current coverage source (work, own policy, none)",
                "Identify gaps (does it follow you? enough coverage?)",
                "Use consequence questions (what happens if...)"
            ],
            "dont": [
                "Re-ask questions they already answered",
                "Jump to closing before understanding their situation",
                "Be pushy or salesy"
            ],
            "success_signal": "They share a problem or concern you can solve"
        },
        ConversationStage.OBJECTION_HANDLING: {
            "goal": "Address concerns with empathy. Redirect, don't push.",
            "do": [
                "Acknowledge their concern (tactical empathy)",
                "Use calibrated questions to understand",
                "Reference what they previously shared",
                "Offer value, not pressure"
            ],
            "dont": [
                "Argue or dismiss their objection",
                "Push harder when they resist",
                "Ignore what they said"
            ],
            "success_signal": "They soften or show willingness to continue"
        },
        ConversationStage.QUALIFICATION: {
            "goal": "Determine if they qualify for better products than guaranteed issue.",
            "do": [
                "Ask sensitive health questions conversationally",
                "Cross-reference underwriting guide",
                "Give honest verdicts (some conditions are tough)",
                "Explain why traditional coverage might be better"
            ],
            "dont": [
                "Promise they will qualify",
                "Make medical claims",
                "Rush through health questions"
            ],
            "success_signal": "You have enough health info to match carriers"
        },
        ConversationStage.CLOSING: {
            "goal": "Book the appointment. Offer specific times.",
            "do": [
                "Offer two specific time options (binary choice)",
                "Reference the value you can provide",
                "Make it easy to say yes",
                "Confirm the appointment clearly"
            ],
            "dont": [
                "Ask open-ended 'when works for you'",
                "Keep discovering instead of closing",
                "Let them off the hook without an attempt"
            ],
            "success_signal": "They agree to a specific time"
        }
    }
    return objectives.get(stage, objectives[ConversationStage.DISCOVERY])


def build_state_from_history(contact_id: str, first_name: str, conversation_history: List, current_message: str) -> ConversationState:
    """
    Build conversation state from history.
    This reconstructs state from the conversation thread.
    
    Handles both string-based history (prefixed with "You:", "Lead:") and 
    dict-based history (with "direction" and "body" keys from GHL).
    """
    state = ConversationState(contact_id=contact_id, first_name=first_name)
    
    # Normalize conversation history to handle both formats
    normalized_history = []
    for msg in conversation_history:
        if isinstance(msg, dict):
            # Dict-based format from GHL: {"direction": "inbound/outbound", "body": "..."}
            direction = msg.get("direction", "")
            body = msg.get("body", "")
            if direction == "outbound":
                normalized_history.append(f"You: {body}")
            elif direction == "inbound":
                normalized_history.append(f"Lead: {body}")
        elif isinstance(msg, str):
            # String-based format already prefixed
            normalized_history.append(msg)
    
    # Count exchanges and extract facts from lead messages
    for msg in normalized_history:
        msg_stripped = msg.strip()
        
        if msg_stripped.startswith(("You:", "Agent:", "Mitchell:", "Devon:", "Rep:")):
            # Agent message - track questions asked
            state.questions_asked.append(msg_stripped)
        elif msg_stripped.startswith("Lead:"):
            # Lead message - extract facts and count exchange
            lead_content = msg_stripped[5:].strip()
            state.exchange_count += 1
            extract_facts_from_message(state, lead_content)
            
            # Check for dismissive
            is_soft, is_hard = detect_dismissive(lead_content)
            if is_soft:
                state.soft_dismissive_count += 1
            if is_hard:
                state.hard_dismissive = True
            
            # Check for engagement
            if len(lead_content) > 10:  # More than a one-word response
                state.has_engaged = True
    
    # Extract facts from current message
    extract_facts_from_message(state, current_message)
    is_soft, is_hard = detect_dismissive(current_message)
    if is_soft:
        state.soft_dismissive_count += 1
    if is_hard:
        state.hard_dismissive = True
    
    # Determine stage based on state
    state.stage = detect_stage(state, current_message, normalized_history)
    
    return state


def format_state_for_prompt(state: ConversationState) -> str:
    """
    Format state as explicit instructions for the LLM prompt.
    This is the key integration between state machine and LLM.
    """
    objectives = get_stage_objectives(state.stage)
    
    # Build known facts section
    known_facts = []
    if state.facts["family"]["spouse"]:
        known_facts.append("Has spouse/partner")
    if state.facts["family"]["kids"]:
        known_facts.append(f"Has {state.facts['family']['kids']} kids")
    if state.facts["coverage"]["employer"]:
        known_facts.append(f"Has employer coverage" + (f" ({state.facts['coverage']['amount']})" if state.facts["coverage"].get("amount") else ""))
    if state.facts["coverage"]["guaranteed_issue"]:
        known_facts.append("Has guaranteed issue product (Colonial Penn, Globe Life, etc.)")
    if state.facts["coverage"]["has_any"] == False:
        known_facts.append("Currently has NO coverage")
    if state.facts["health"]["conditions"]:
        known_facts.append(f"Health conditions: {', '.join(state.facts['health']['conditions'])}")
    if state.facts["motivating_goal"]:
        known_facts.append(f"Motivation: {state.facts['motivating_goal']}")
    
    # Build DO NOT ASK section
    do_not_ask = []
    if "marital_status" in state.topics_answered:
        do_not_ask.append("Whether they're married (you already know)")
    if "kids" in state.topics_answered:
        do_not_ask.append("How many kids they have (you already know)")
    if "coverage" in state.topics_answered:
        do_not_ask.append("If they have coverage (you already know)")
    if "coverage_source" in state.topics_answered:
        do_not_ask.append("Where their coverage is from (you already know)")
    if "motivation" in state.topics_answered:
        do_not_ask.append("What got them looking (you already know)")
    
    prompt = f"""
=== CONVERSATION STATE (Source of Truth) ===
Stage: {state.stage.value.upper()}
Exchange Count: {state.exchange_count}
Dismissive Count: {state.soft_dismissive_count}

=== YOUR OBJECTIVE THIS MESSAGE ===
{objectives['goal']}

=== DO ===
{chr(10).join(f"- {item}" for item in objectives['do'])}

=== DO NOT ===
{chr(10).join(f"- {item}" for item in objectives['dont'])}

=== SUCCESS SIGNAL ===
{objectives['success_signal']}
"""
    
    if known_facts:
        prompt += f"""
=== FACTS YOU ALREADY KNOW (Reference these, don't re-ask) ===
{chr(10).join(f"- {fact}" for fact in known_facts)}
"""
    
    if do_not_ask:
        prompt += f"""
=== DO NOT ASK ABOUT (Already answered) ===
{chr(10).join(f"- {item}" for item in do_not_ask)}
"""
    
    return prompt


class PolicyEngine:
    """
    Validates LLM responses against stage rules.
    Rejects bad responses and provides correction guidance.
    """
    
    @staticmethod
    def validate_response(response: str, state: ConversationState, reflection_scores: Optional[Dict[str, int]] = None) -> tuple:
        """
        Validate a response against stage rules.
        Returns (is_valid, error_reason, correction_guidance)
        
        Validation layers:
        1. Universal rules (length, format)
        2. Stage-specific rules (what's allowed in each stage)
        3. State-based rules (don't repeat questions, use known facts)
        4. Self-reflection scores (if any score <6, regenerate)
        """
        response_lower = response.lower()
        
        # === UNIVERSAL RULES ===
        
        # Check for em dashes
        if "—" in response or "–" in response:
            return False, "Contains em dashes", "Replace em dashes with commas or periods."
        
        # Check response length (SMS should be 15-40 words)
        word_count = len(response.split())
        if word_count > 50:
            return False, f"Too long ({word_count} words)", "Shorten to 15-40 words for SMS."
        
        if word_count < 5:
            return False, f"Too short ({word_count} words)", "Response should be at least 5 words."
        
        # Check for multiple questions
        question_count = response.count("?")
        if question_count > 1:
            return False, f"Too many questions ({question_count})", "Ask only ONE question per message."
        
        # Check for bad survey-style questions
        bad_questions = [
            "what's the main thing you're hoping to get",
            "what would be ideal for you",
            "what are you hoping to achieve",
            "what's on your mind about insurance"
        ]
        for bad_q in bad_questions:
            if bad_q in response_lower:
                return False, f"Used bad survey-style question", "Ask specific, conversational questions instead of generic ones."
        
        # === SELF-REFLECTION SCORE CHECK ===
        if reflection_scores:
            for metric, score in reflection_scores.items():
                if score < 6:
                    return False, f"Low {metric} score ({score}/10)", f"Improve {metric} before sending."
        
        # === STAGE-SPECIFIC RULES ===
        
        if state.stage == ConversationStage.INITIAL_OUTREACH:
            # Should not ask about income, policy details, or health yet
            bad_topics = ["income", "how much do you make", "budget", "health", "medical", "conditions", "diabetes", "heart"]
            for topic in bad_topics:
                if topic in response_lower:
                    return False, f"Asked about {topic} too early", "In initial outreach, only ask what got them looking."
            
            # Should not push for appointment yet
            if any(t in response_lower for t in ["tonight", "tomorrow", "set up a call", "schedule"]):
                return False, "Pushed for appointment too early", "In initial outreach, focus on getting them to share their situation first."
        
        elif state.stage == ConversationStage.DISCOVERY:
            # Should be asking discovery questions, not closing
            pass  # Discovery is flexible
        
        elif state.stage == ConversationStage.CLOSING:
            # Must include time options
            time_indicators = ["today", "tomorrow", "morning", "afternoon", "evening", 
                              "monday", "tuesday", "wednesday", "thursday", "friday",
                              "pm", "am", "o'clock", "6:30", "10:15", "2pm", "11am"]
            has_time = any(t in response_lower for t in time_indicators)
            if not has_time and question_count > 0:
                return False, "In closing but no specific times offered", "Offer two specific time slots (e.g., '2pm today or 11am tomorrow?')"
        
        # === STATE-BASED RULES ===
        
        # Check for re-asking answered topics
        topic_patterns = {
            "marital_status": ["are you married", "do you have a spouse", "do you have a wife", "do you have a husband"],
            "kids": ["do you have kids", "how many kids", "do you have children"],
            "coverage": ["do you have coverage", "do you have any coverage", "are you currently covered", "do you have insurance"],
            "coverage_source": ["is that through work", "through your employer", "through your job"],
            "motivation": ["what got you looking", "what made you look", "what originally got you"]
        }
        
        for topic in state.topics_answered:
            if topic in topic_patterns:
                for pattern in topic_patterns[topic]:
                    if pattern in response_lower:
                        return False, f"Re-asked about {topic}", f"You already know about {topic}. Reference what they told you instead of asking again."
        
        # Check for repeating recent questions
        for prev_question in state.questions_asked[-3:]:
            prev_lower = prev_question.lower()
            # Check for semantic similarity
            key_phrases = re.findall(r'what (got|made|originally|was)', prev_lower)
            for phrase in key_phrases:
                if phrase in response_lower and "?" in response:
                    return False, "Repeated a similar question", "Ask a different type of question or move forward."
        
        return True, None, None
    
    @staticmethod
    def get_regeneration_prompt(error_reason: str, correction_guidance: str) -> str:
        """
        Returns a prompt to fix a bad response.
        """
        return f"""

=== YOUR PREVIOUS RESPONSE WAS REJECTED ===
Reason: {error_reason}
Fix: {correction_guidance}

Generate a new response that fixes this issue. Do NOT repeat the same mistake.
"""


def parse_reflection(response: str) -> Optional[Dict[str, Any]]:
    """
    Parse <reflection> tags from LLM response if present.
    Returns scores and improvements, or None if no reflection.
    """
    import re
    reflection_match = re.search(r'<reflection>(.*?)</reflection>', response, re.DOTALL)
    if not reflection_match:
        return None
    
    reflection_text = reflection_match.group(1).strip()
    
    # Parse scores
    scores = {}
    for metric in ["relevance", "coherence", "effectiveness"]:
        score_match = re.search(rf'{metric}[:\s]*(\d+)', reflection_text, re.IGNORECASE)
        if score_match:
            scores[metric] = int(score_match.group(1))
    
    # Parse improvement
    improvement_match = re.search(r'improv\w*[:\s]*(.*?)(?:\.|$)', reflection_text, re.IGNORECASE)
    improvement = improvement_match.group(1).strip() if improvement_match else None
    
    return {
        "scores": scores,
        "improvement": improvement,
        "raw": reflection_text
    }


def strip_reflection(response: str) -> str:
    """Remove <reflection> tags from response before sending to user."""
    return re.sub(r'<reflection>.*?</reflection>', '', response, flags=re.DOTALL).strip()
