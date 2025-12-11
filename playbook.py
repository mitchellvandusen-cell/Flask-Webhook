"""
Playbook / Knowledge Library
============================

Layer 3 of the three-layer architecture.
Contains ideal responses for common situations, organized by stage.
Used for:
1. Few-shot examples in prompts
2. Template fallbacks when LLM fails validation
3. Pattern matching for common scenarios
"""

import random
from typing import Dict, List, Optional, Any
from conversation_engine import ConversationStage


# Stage-specific response templates
STAGE_TEMPLATES = {
    ConversationStage.INITIAL_OUTREACH: {
        "opener": [
            "Hey {first_name}, saw you looked at life insurance a while back. What originally got you thinking about it?",
            "Hi {first_name}, noticed you were considering coverage before. Was there something specific that had you looking?",
            "{first_name}, just following up. What was going on that had you exploring life insurance back then?"
        ],
        "re_engage_cold": [
            "Hey {first_name}, still thinking about coverage?",
            "Hi {first_name}, anything change since you last looked at insurance?"
        ]
    },
    
    ConversationStage.DISCOVERY: {
        "ask_family": [
            "Got it. Who would you want that coverage to protect?",
            "Makes sense. Is there anyone depending on your income right now?",
            "Totally. Who would be most impacted if something happened to you?"
        ],
        "ask_coverage_source": [
            "Do you have anything in place right now, even through work?",
            "Is your current coverage through your employer or your own policy?"
        ],
        "probe_employer_gap": [
            "Does that follow you if you switch jobs or retire?",
            "What happens to that coverage if you leave?",
            "Is that tied to your job or is it yours to keep?"
        ],
        "probe_amount_gap": [
            "What would you want that to cover first, the house or income replacement?",
            "Would that be enough to cover the mortgage and a few years of income?",
            "How long would that last your family if you weren't there?"
        ],
        "probe_term_gap": [
            "How many years left on that policy?",
            "What's the plan when it runs out?",
            "Does it build any cash value or is it straight term?"
        ]
    },
    
    ConversationStage.OBJECTION_HANDLING: {
        "first_resistance": [
            "Sounds like that felt too nosy. My bad. Just curious, what got you thinking about coverage back then?",
            "Fair enough, didn't mean to pry. What was going on that had you looking in the first place?",
            "Got it, no need to get into details. Was there something specific that made you start looking?",
            "Totally fair. I get it. Out of curiosity, what had you considering coverage back then?"
        ],
        "second_resistance_family": [
            "I hear you. You mentioned your wife earlier. How would you want her taken care of if something happened?",
            "Got it. You said your wife has been asking about this. What would you want covered for her?",
            "Fair enough. Earlier you mentioned your wife is worried. What specifically concerns her?"
        ],
        "second_resistance_generic": [
            "I hear you. Just trying to help figure out what makes sense. Is there a better time to chat?",
            "Got it. No pressure at all. Would a quick call work better than texting?",
            "Fair enough. I'll keep it brief. Is there anything specific you want me to look into?"
        ],
        "soft_exit": [
            "No worries at all. I'll check back in a bit.",
            "Totally understand. I'll circle back another time.",
            "Got it. I'll reach out again down the road. Have a good one."
        ],
        "hard_exit": [
            "Got it. Take care.",
            "No problem. Have a good one."
        ],
        "not_interested": [
            "No worries. Out of curiosity, was it timing or something else?",
            "Totally get it. Did something change or just not the right fit?"
        ],
        "already_covered": [
            "Nice, good to have something in place. Is that through work or your own policy?",
            "Good to hear. Just curious, does it follow you if you switch jobs?"
        ],
        "cant_afford": [
            "Totally fair. Out of curiosity, what do you think it would cost?",
            "I hear you. Have you actually seen rates recently? Sometimes people are surprised."
        ],
        "think_about_it": [
            "Totally get it. What specifically would you want to think through?",
            "Makes sense. What questions would help you decide?"
        ]
    },
    
    ConversationStage.QUALIFICATION: {
        "diabetes_probe": [
            "Got it on the diabetes. Is that controlled with pills or insulin?",
            "Okay, diabetes. Do you know your most recent A1C?"
        ],
        "heart_probe": [
            "When you say heart issues, was that a full heart attack or a stent?",
            "Got it. How long ago was that? Are you stable now?"
        ],
        "cancer_probe": [
            "Sorry to hear that. What type of cancer, and how long ago?",
            "Are you in remission now? Any ongoing treatment?"
        ],
        "gi_pivot": [
            "Based on what you told me, you might not need guaranteed issue. Some carriers work with {condition}. Want me to dig into it?",
            "That's actually manageable with the right carrier. Worth exploring options that don't have the waiting period. Interested?"
        ],
        "tough_case_honest": [
            "I'll be straight with you. That's a tougher case. Not impossible, but limited options. Want me to see what's out there?",
            "Honestly, that narrows things down. But there might still be something better than what you have. Worth a look?"
        ]
    },
    
    ConversationStage.CLOSING: {
        "offer_times": [
            "Let me dig into this for you. Free at 2pm today or 11am tomorrow?",
            "I can help you find the right fit. How's 6:30 tonight or 10:15 tomorrow?",
            "Let me see what we can do. What works better, 2pm today or 11am tomorrow?",
            "I can look into options for you. Free for a quick call at 3pm or 10am tomorrow?"
        ],
        "after_gap_discovery": [
            "That's a big gap. Let me help you figure out what makes sense. Free at 2pm today or 11am tomorrow?",
            "Yeah that's worth addressing. I can dig into options. How's 6pm tonight or 10am tomorrow?"
        ],
        "after_interest_signal": [
            "Great, let's set something up. What works better, 2pm today or 11am tomorrow?",
            "Perfect. I've got 3pm today or 10:30 tomorrow. Which works?"
        ],
        "confirm_booking": [
            "You're all set for {time}. I'll send a calendar invite. Talk soon.",
            "Perfect, locked in for {time}. You'll get a confirmation. Looking forward to it."
        ]
    }
}


# Few-shot examples for prompting
FEW_SHOT_EXAMPLES = [
    {
        "stage": ConversationStage.INITIAL_OUTREACH,
        "lead_message": "yeah I looked at it a while ago",
        "agent_response": "Nice. What was going on at the time that had you looking?",
        "reflection": "Relevance: 9/10 - Directly advances discovery. Coherence: 9/10 - Builds on their acknowledgment. Effectiveness: 8/10 - Open question invites sharing."
    },
    {
        "stage": ConversationStage.DISCOVERY,
        "lead_message": "I have 50k through work",
        "agent_response": "Got it. Does that follow you if you switch jobs?",
        "reflection": "Relevance: 10/10 - Identifies key gap. Coherence: 10/10 - Directly addresses their coverage. Effectiveness: 9/10 - Socratic approach lets them realize the gap."
    },
    {
        "stage": ConversationStage.DISCOVERY,
        "lead_message": "My wife keeps asking about it",
        "agent_response": "Makes sense. What specifically is she worried about?",
        "reflection": "Relevance: 9/10 - Digs into motivation. Coherence: 9/10 - References wife. Effectiveness: 8/10 - Gets to the real concern."
    },
    {
        "stage": ConversationStage.OBJECTION_HANDLING,
        "lead_message": "I'm not really interested right now",
        "agent_response": "Totally get it. Was it timing or something else that changed?",
        "reflection": "Relevance: 8/10 - Redirects softly. Coherence: 9/10 - Acknowledges their position. Effectiveness: 7/10 - Opens door for re-engagement."
    },
    {
        "stage": ConversationStage.CLOSING,
        "lead_message": "Yeah I should probably look into it",
        "agent_response": "Let me help you figure it out. Free at 2pm today or 11am tomorrow?",
        "reflection": "Relevance: 10/10 - Advances to appointment. Coherence: 10/10 - Responds to their interest. Effectiveness: 9/10 - Binary choice makes it easy to say yes."
    }
]


# Scenario matching for automatic template selection
SCENARIO_MATCHERS = {
    "employer_coverage_mentioned": {
        "patterns": [r'through work', r'employer', r'job', r'company'],
        "response_key": "probe_employer_gap",
        "stage": ConversationStage.DISCOVERY
    },
    "spouse_mentioned": {
        "patterns": [r'wife', r'husband', r'spouse'],
        "response_key": "ask_family",
        "stage": ConversationStage.DISCOVERY
    },
    "not_interested": {
        "patterns": [r'not interested', r'no thanks', r'im good', r"i'm good"],
        "response_key": "not_interested",
        "stage": ConversationStage.OBJECTION_HANDLING
    },
    "already_covered": {
        "patterns": [r'already have', r'covered', r'got insurance', r'all set'],
        "response_key": "already_covered",
        "stage": ConversationStage.OBJECTION_HANDLING
    },
    "cant_afford": {
        "patterns": [r"can't afford", r'too expensive', r'no money', r'budget'],
        "response_key": "cant_afford",
        "stage": ConversationStage.OBJECTION_HANDLING
    },
    "interest_signal": {
        "patterns": [r'sounds good', r'interested', r'tell me more', r'should look into'],
        "response_key": "offer_times",
        "stage": ConversationStage.CLOSING
    }
}


def get_template_response(stage: ConversationStage, situation: str, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Get a template response for a given stage and situation.
    Returns None if no matching template.
    """
    context = context or {}
    first_name = context.get("first_name", "")
    
    if stage not in STAGE_TEMPLATES:
        return None
    
    stage_templates = STAGE_TEMPLATES[stage]
    if situation not in stage_templates:
        return None
    
    templates = stage_templates[situation]
    response = random.choice(templates)
    
    # Format with context
    try:
        response = response.format(
            first_name=first_name,
            condition=context.get("condition", "your condition"),
            time=context.get("time", "")
        )
    except KeyError:
        pass
    
    return response


def match_scenario(message: str) -> Optional[Dict[str, Any]]:
    """
    Match a message to a known scenario.
    Returns scenario info or None.
    """
    import re
    msg_lower = message.lower()
    
    for scenario_name, scenario in SCENARIO_MATCHERS.items():
        for pattern in scenario["patterns"]:
            if re.search(pattern, msg_lower):
                return {
                    "name": scenario_name,
                    "response_key": scenario["response_key"],
                    "stage": scenario["stage"]
                }
    
    return None


def get_few_shot_examples(stage: ConversationStage, limit: int = 2) -> str:
    """
    Get few-shot examples formatted for prompting.
    Filters by stage and returns formatted string.
    """
    relevant_examples = [ex for ex in FEW_SHOT_EXAMPLES if ex["stage"] == stage]
    if not relevant_examples:
        relevant_examples = FEW_SHOT_EXAMPLES[:limit]
    else:
        relevant_examples = relevant_examples[:limit]
    
    formatted = []
    for ex in relevant_examples:
        formatted.append(f"""Lead: "{ex['lead_message']}"
You: "{ex['agent_response']}"
<reflection>{ex['reflection']}</reflection>""")
    
    return "\n\n".join(formatted)


def get_resistance_template(dismissive_count: int, has_spouse: bool = False) -> Optional[str]:
    """
    Get the appropriate resistance response based on count.
    Returns template or None.
    """
    if dismissive_count == 1:
        return get_template_response(
            ConversationStage.OBJECTION_HANDLING, 
            "first_resistance"
        )
    elif dismissive_count == 2:
        if has_spouse:
            return get_template_response(
                ConversationStage.OBJECTION_HANDLING,
                "second_resistance_family"
            )
        else:
            return get_template_response(
                ConversationStage.OBJECTION_HANDLING,
                "second_resistance_generic"
            )
    elif dismissive_count >= 3:
        return get_template_response(
            ConversationStage.OBJECTION_HANDLING,
            "soft_exit"
        )
    
    return None


def get_hard_exit_template() -> Optional[str]:
    """Get hard exit template for complete disengagement."""
    return get_template_response(ConversationStage.OBJECTION_HANDLING, "hard_exit")


def get_closing_template(situation: str = "offer_times", context: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Get closing template with time offers."""
    return get_template_response(ConversationStage.CLOSING, situation, context or {})
