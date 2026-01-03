import re 
import random
"""
NEPQ Knowledge Base - Everything the bot knows, organized by topic.
The bot reads this FIRST before responding, then uses trigger words
to pull relevant sections into its response context.
"""

# =============================================================================
# CORE KNOWLEDGE SECTIONS
# =============================================================================

PRODUCT_KNOWLEDGE = {
    "term_life": {
        "what_it_is": "Temporary coverage for 10-30 years. Pure death benefit, no cash value.",
        "problems": [
            "Expires when you're older and premiums skyrocket",
            "97% of term policies never pay out",
            "No living benefits - only pays if you die",
            "Can't access money while alive"
        ],
        "questions_to_ask": [
            "How many years are left on that term?",
            "What happens when it expires?",
            "Did they show you what renewal rates look like?"
        ],
        "pivot": "Most people don't realize term is like renting - you pay for years and end up with nothing."
    },
    
    "whole_life": {
        "what_it_is": "Permanent coverage with cash value that grows slowly.",
        "problems": [
            "Very expensive for the death benefit",
            "Cash value grows at 2-4% - barely beats inflation",
            "Takes 10-15 years to build meaningful cash value",
            "Surrender charges if you cancel early"
        ],
        "questions_to_ask": [
            "Does that one have living benefits, or just the death benefit?",
            "Do you know what the cash value is right now?",
            "Can you access that money if you got sick?"
        ],
        "pivot": "Whole life is great for some people, but most don't realize there are policies with living benefits now."
    },
    
    "guaranteed_issue": {
        "what_it_is": "No medical questions, guaranteed approval. Usually final expense.",
        "carriers": ["Colonial Penn", "Globe Life", "AARP", "Gerber Life"],
        "problems": [
            "2-3 year waiting period before full payout",
            "Very expensive per dollar of coverage",
            "Low coverage amounts ($5K-$25K typically)",
            "Return of premium only if death in waiting period"
        ],
        "questions_to_ask": [
            "How long ago did you get that?",
            "Did they explain the waiting period?",
            "What's the monthly premium vs coverage amount?"
        ],
        "pivot": "Those are designed for people who can't qualify for anything else. With your health, you probably have better options."
    },
    
    "employer_coverage": {
        "what_it_is": "Group life insurance through work, usually 1-2x salary.",
        "problems": [
            "Disappears when you leave the job",
            "Can't take it with you (not portable)",
            "Coverage amount usually too low",
            "No living benefits",
            "Rates go up as you age if you convert"
        ],
        "questions_to_ask": [
            "Do you know what happens to that when you retire or switch jobs?",
            "Is that your only coverage?",
            "How much is the death benefit?"
        ],
        "pivot": "Work coverage is a nice bonus but it's not something you own. What's your backup plan?"
    },
    
    "living_benefits": {
        "what_it_is": "Ability to access death benefit while alive if diagnosed with chronic, critical, or terminal illness.",
        "benefits": [
            "Access 50-100% of death benefit if terminally ill",
            "Use for medical bills, lost income, bucket list",
            "Chronic illness rider for long-term care needs",
            "Critical illness for heart attack, stroke, cancer",
            "Don't have to die to use the policy"
        ],
        "hook": "Most people have life insurance that only pays when they die. The new policies pay while you're alive too."
    }
}

HEALTH_CONDITIONS = {
    "diabetes": {
        "type_2_controlled": "Standard rates possible with A1C under 7.5, no complications",
        "type_2_uncontrolled": "Substandard rates, may need guaranteed issue",
        "type_1": "Limited carriers, higher rates, but still insurable",
        "response": "Good news, with that you've got way more options than a guaranteed-issue policy. Want me to check?"
    },
    "heart": {
        "history": "Depends on how recent and severity. Many carriers write after 1-2 years stable.",
        "stent": "Usually insurable 6-12 months post-procedure if stable",
        "bypass": "Longer wait, but options exist",
        "response": "Heart history doesn't automatically disqualify you. When was the last procedure?"
    },
    "cancer": {
        "in_remission": "Most carriers want 2-5 years cancer-free depending on type",
        "active": "Very limited options, may need guaranteed issue",
        "response": "Cancer history matters for timing. How long have you been in remission?"
    },
    "general": {
        "response": "With that you've got way more options than a guaranteed-issue policy. Want me to check what you qualify for?"
    }
}

OBJECTION_HANDLERS = {
    "not_interested": {
        "meaning": "Usually means 'I don't see the value' or 'bad timing'",
        "responses": [
            "Fair enough. Was it more the price or just couldn't find the right fit last time?",
            "No problem. Quick question though - do you have something in place already?",
            "Got it. Just curious, what made you look into it before?"
        ]
    },
    "already_covered": {
        "meaning": "Could be real coverage, could be employer coverage, could be trying to end convo",
        "responses": [
            "Nice. Where'd you end up going?",
            "Cool, who'd you go with?",
            "Good to hear. What kind of policy did you land on?",
            "Oh nice, through who?"
        ],
        "follow_up": "Probe to understand WHAT they have, then identify gaps",
        "responses": [
            "Did you find this company yourself? or did someone help you?"
        ],
    },
    "too_expensive": {
        "meaning": "May not understand value, may have been quoted wrong product",
        "responses": [
            "What were they quoting you for coverage?",
            "Was that for term or permanent?",
            "Sometimes the wrong policy gets quoted. What did they show you?"
        ]
    },
    "need_to_think": {
        "meaning": "Usually means unresolved objection or not enough urgency",
        "responses": [
            "Totally get it. What's the main thing you're weighing?",
            "Of course. What would help you decide?",
            "Makes sense. Is it the coverage or the cost you're thinking about?"
        ]
    },
    "spouse_decision": {
        "meaning": "May be real, may be stall tactic",
        "responses": [
            "Smart to include them. Would a quick 3-way call work better?",
            "For sure. What questions do you think they'd have?",
        ]
    }
}

BUYING_SIGNALS = {
    "direct": [
        "yes", "sure", "okay", "ok", "yeah", "yep", "perfect", "works",
        "let's do it", "sign me up", "sounds good", "i'm in", "when can we talk"
    ],
    "indirect": [
        "how much", "what would it cost", "what are my options",
        "can you look into it", "send me info", "tell me more"
    ],
    "response_template": "Perfect. {slots}, which works better?"
}

PENSION_SURVIVORSHIP = {
    "triggers": [
        r"\b(pension|retirement).*?(husband|wife|spouse|survivor|surviving)\b",
        r"\b(husband|wife|spouse).*?(pension|retirement)\b",
        r"\b(survivor benefit|pension continues?|pension after death)\b",
        r"\bmy (husband|wife).*pension\b",
        r"\b(pension|retirement).*(when|after).*(die|dies|death)\b"
    ],
    "meaning": "They think a pension = life insurance. It usually doesn't. Most pensions pay 0-55% to spouse and die with them unless a costly survivor option was elected.",
    "responses": [
        "Got it — is that the full pension continuing, or the survivor benefit that reduces it?",
        "Just to clarify — does the pension keep paying full amount if something happens to them, or is it the reduced survivor option?",
        "Most pensions drop to 50% or nothing for the spouse unless you elected (and paid for) the survivor benefit. Is yours set up that way?",
        "Quick question — did they take the joint & survivor option (which lowers the monthly check), or is it single life only?",
        "That’s common — but 80% of private pensions pay $0 to the spouse unless you specifically chose (and paid for) the survivor election. Did they do that?"
    ],
    "follow_up": [
        "Because if it’s single life, it dies with them — nothing for you.",
        "The survivor option usually cuts the monthly payment 10-20% while they’re alive.",
        "Federal pensions pay 50% automatically. Most private ones pay zero unless elected.",
        "Even if it pays something, it’s usually half — and not inflation-adjusted."
    ]
}

CONVERSATION_STAGES = {
    "initial_outreach": {
        "goal": "Pattern interrupt, spark curiosity",
        "approach": "Reference they were looking before, mention new developments",
        "avoid": "Don't pitch, don't ask for appointment yet"
    },
    "discovery": {
        "goal": "Understand current situation and find gaps",
        "approach": "Ask about current coverage, family, concerns",
        "avoid": "Don't solve problems yet, just uncover them"
    },
    "consequence": {
        "goal": "Help them feel the weight of the gap",
        "approach": "What happens if questions, future-pace the problem",
        "avoid": "Don't be doom and gloom, be matter-of-fact"
    },
    "closing": {
        "goal": "Secure the appointment",
        "approach": "Offer two specific times, make it easy",
        "avoid": "Don't ask if they want to meet, assume they do"
    }
}

# =============================================================================
# TRIGGER WORD MAPPINGS
# =============================================================================

TRIGGER_TO_KNOWLEDGE = {
    "TERM": ["product_knowledge.term_life"],
    "PERMANENT": ["product_knowledge.whole_life"],
    "GI": ["product_knowledge.guaranteed_issue"],
    "EMPLOYER": ["product_knowledge.employer_coverage"],
    "LIVING_BENEFITS": ["product_knowledge.living_benefits"],
    "DIABETES": ["health_conditions.diabetes"],
    "HEART": ["health_conditions.heart"],
    "CANCER": ["health_conditions.cancer"],
    "HEALTH": ["health_conditions.general"],
    "NOT_INTERESTED": ["objection_handlers.not_interested"],
    "ALREADY_COVERED": ["objection_handlers.already_covered"],
    "TOO_EXPENSIVE": ["objection_handlers.too_expensive"],
    "NEED_TO_THINK": ["objection_handlers.need_to_think"],
    "SPOUSE": ["objection_handlers.spouse_decision"],
    "BUYING_SIGNAL": ["buying_signals"],
    "PRICE": ["buying_signals"]
}

# =============================================================================
# KNOWLEDGE RETRIEVAL FUNCTIONS
# =============================================================================

def get_all_knowledge():
    """Returns the complete knowledge base as a formatted string for LLM context."""
    sections = []
    
    sections.append("=== PRODUCT KNOWLEDGE ===")
    for product, info in PRODUCT_KNOWLEDGE.items():
        sections.append(f"\n{product.upper()}:")
        sections.append(f"  What it is: {info['what_it_is']}")
        if 'problems' in info:
            sections.append(f"  Problems: {', '.join(info['problems'][:3])}")
        if 'pivot' in info:
            sections.append(f"  Pivot: {info['pivot']}")
    
    sections.append("\n=== HEALTH CONDITIONS ===")
    for condition, info in HEALTH_CONDITIONS.items():
        sections.append(f"\n{condition.upper()}: {info.get('response', '')}")
    
    sections.append("\n=== OBJECTION HANDLERS ===")
    for objection, info in OBJECTION_HANDLERS.items():
        sections.append(f"\n{objection.upper()}:")
        sections.append(f"  Meaning: {info['meaning']}")
        sections.append(f"  Responses: {info['responses'][0]}")
    
    return "\n".join(sections)


def get_relevant_knowledge(triggers_found):
    """Given a list of triggered patterns, return the relevant knowledge sections."""
    relevant = {}
    
    for trigger in triggers_found:
        if trigger in TRIGGER_TO_KNOWLEDGE:
            for path in TRIGGER_TO_KNOWLEDGE[trigger]:
                parts = path.split('.')
                if parts[0] == 'product_knowledge' and len(parts) > 1:
                    key = parts[1]
                    if key in PRODUCT_KNOWLEDGE:
                        relevant[f"product_{key}"] = PRODUCT_KNOWLEDGE[key]
                elif parts[0] == 'health_conditions' and len(parts) > 1:
                    key = parts[1]
                    if key in HEALTH_CONDITIONS:
                        relevant[f"health_{key}"] = HEALTH_CONDITIONS[key]
                elif parts[0] == 'objection_handlers' and len(parts) > 1:
                    key = parts[1]
                    if key in OBJECTION_HANDLERS:
                        relevant[f"objection_{key}"] = OBJECTION_HANDLERS[key]
                elif parts[0] == 'buying_signals':
                    relevant['buying_signals'] = BUYING_SIGNALS
    
    return relevant


def format_knowledge_for_prompt(knowledge_dict):
    """Format retrieved knowledge into a string for injection into the LLM prompt."""
    if not knowledge_dict:
        return ""
    
    lines = ["RELEVANT KNOWLEDGE FOR THIS MESSAGE:"]
    
    for key, info in knowledge_dict.items():
        lines.append(f"\n[{key.upper()}]")
        if isinstance(info, dict):
            for k, v in info.items():
                if isinstance(v, list):
                    lines.append(f"  {k}: {', '.join(str(x) for x in v[:3])}")
                else:
                    lines.append(f"  {k}: {v}")
    
    return "\n".join(lines)


def identify_triggers(message):
    """Scan message for trigger words and return list of matched trigger categories."""
    import re
    m = message.lower().strip()
    
    triggers_found = []
    
    trigger_patterns = {
        "TERM": r"(term.*life|term.*policy|10.?year|15.?year|20.?year|30.?year)",
        "PERMANENT": r"(whole.*life|permanent|cash.*value|iul|universal.*life|indexed)",
        "GI": r"(guaranteed.*issue|no.*exam|colonial.*penn|globe.*life|aarp|no.*health|no questions|final.*expense|burial)",
        "EMPLOYER": r"(through.*work|employer|job.*covers|group.*insurance|company.*pays|benefits|work.*policy)",
        "LIVING_BENEFITS": r"(living.*benefit|chronic|critical|terminal|accelerated)",
        "DIABETES": r"(diabetes|a1c|insulin|blood.*sugar)",
        "HEART": r"(heart|stent|bypass|cardiac|cardiovascular)",
        "CANCER": r"(cancer|tumor|oncolog|remission|chemo)",
        "HEALTH": r"(copd|oxygen|stroke|blood.*pressure|high bp|hypertension|kidney|liver)",
        "NOT_INTERESTED": r"(not.*interested|no thanks|pass|not for me)",
        "ALREADY_COVERED": r"(covered|all set|already have|got.*covered|taken care|handled|found.*policy)",
        "TOO_EXPENSIVE": r"(expensive|cant.*afford|too much|cost too|pricey)",
        "NEED_TO_THINK": r"(think.*about|need.*time|not sure|consider|sleep on)",
        "SPOUSE": r"(wife|husband|spouse|partner|talk.*to|ask.*them)",
        "BUYING_SIGNAL": r"^\s*(yes|sure|okay|ok|yeah|yep|perfect|works|lets do it|let's do it|sounds good)[!.,?\s]*$",
        "PRICE": r"(how.*much|quote|rate|price|cost|premium)"
    }
    
    for trigger_name, pattern in trigger_patterns.items():
        if re.search(pattern, m):
            triggers_found.append(trigger_name)
    
    return triggers_found
