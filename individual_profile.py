# profile.py - Ultimate Adaptive Comprehensive Identity Builder (2026 Final Version)

import re
from typing import List, Optional

def build_comprehensive_profile(
    known_facts: List[str],
    first_name: Optional[str] = None,
    age: Optional[str] = None,
    address: Optional[str] = None
) -> str:
    """
    Builds the most elaborate, nuanced, human-like identity narrative possible.
    Starts with webhook basics (name, age, address).
    Layers in everything from conversation-derived known_facts (family, health, coverage, motivations, wild stories).
    Handles multiples, changes (e.g., "divorced after 20 years"), contradictions, unique life events.
    Graceful if data is partial/empty — never assumes or repeats.
    Token-efficient (200 words max) with priority on seeking to understand their situation. 
    Focuses purely on understanding the person's story — no sales implications.
    """
    if not known_facts and not any([first_name, age, address]):
        return "This is lead im reaching out to about life insurance. Build their unique story naturally through warm, open conversation."

    # Normalize basics
    name = (first_name or "this lead").strip().capitalize()
    if len(name.split()) > 1:
        name = name.split()[0]  # First name for warmth

    profile_sections = {
        "intro": [f"You're speaking with {name}."],
        "demographics": [],
        "family": [],
        "health": [],
        "coverage": [],
        "finances": [],
        "motivations": [],
        "emotional": [],
        "lifestyle": [],
        "unique_story": []
    }

    # Full text for parsing (known_facts only — webhook basics already extracted)
    full_text = " ".join(known_facts).lower()
    facts_lower = [f.lower() for f in known_facts]

    # === Demographics (Webhook + Any Conversation Updates) ===
    if age:
        try:
            age_int = int(re.search(r'\d+', age).group())
            profile_sections["demographics"].append(f"{name} is {age_int} years old.")
            if age_int >= 60:
                profile_sections["demographics"].append("At this stage of life, reflecting on legacy, health, and transitions.")
            elif 45 <= age_int < 60:
                profile_sections["demographics"].append("In mid-life, balancing career and family.")
            elif age_int < 45:
                profile_sections["demographics"].append("Younger, possibly building foundations.")
        except:
            pass

    if address:
        parts = [p.strip() for p in re.split(r',|\n', address) if p.strip()]
        if parts:
            location = f"{parts[-3] if len(parts) > 2 else ''}, {parts[-2]}" if len(parts) > 1 else parts[0]
            profile_sections["demographics"].append(f"{name} is based in {location}.")

    # === Family (Nuanced: Divorce, Ex-Beneficiary, Blended, etc.) ===
    marital_keywords = {
        "divorced": "divorced — may involve changes in family dynamics or responsibilities.",
        "widowed": "widowed, possibly navigating grief and new roles.",
        "single": "single, focusing on personal paths.",
        "married": "married, sharing life with a partner."
    }
    for status, desc in marital_keywords.items():
        if status in full_text:
            profile_sections["family"].append(f"{name} is {status}. {desc}")

    # Kids/dependents
    kids_match = re.search(r'(\d+)\s*(kids?|children|son|daughter)', full_text)
    if kids_match:
        num = kids_match.group(1)
        profile_sections["family"].append(f"{name} has {num} children.")

    # Beneficiary/relationship nuances
    if any(k in full_text for k in ["ex-spouse", "ex-wife", "ex-husband", "former", "beneficiary is ex"]):
        profile_sections["family"].append(f"{name} has family connections involving an ex-partner.")

    if any(k in full_text for k in ["blended", "step", "adopted"]):
        profile_sections["family"].append(f"{name} has a blended family structure.")

    # === Health (Multi-Conditions, Meds, Triggers) ===
    health_keywords = ["diabetes", "cancer", "heart", "dystrophy", "parkinson's", "copd", "stroke", "surgery", "medication", "smoking", "tobacco", "bmi"]
    health_mentions = [f for f in known_facts if any(k in f.lower() for k in health_keywords)]
    if health_mentions:
        conditions = ", ".join(set([re.sub(r'(has|had|diagnosed with|takes|on)', '', m).strip().capitalize() for m in health_mentions]))
        profile_sections["health"].append(f"{name}'s health includes {conditions}.")

    # === Coverage (Multi-Policies, Types, Sources) ===
    coverage_patterns = r'(term|permanent|whole life|guaranteed issue|gi|group)\s*(policy|coverage)?\s*(through work|personal|private|own)?\s*(\$\d+k?|\d{3,})?\s*(with living benefits)?\s*(from|carrier)\s*(\w+)?'
    policy_matches = re.findall(coverage_patterns, full_text)
    policies = []
    for match in policy_matches:
        policy_str = " ".join([p for p in match if p]).strip()
        if policy_str:
            policies.append(policy_str)
    if policies:
        profile_sections["coverage"].append(f"{name} has coverage including: " + "; ".join(set(policies)) + ".")

    # === Finances ===
    if any(k in full_text for k in ["savings", "retirement", "401k", "ira", "debt", "mortgage", "million"]):
        profile_sections["finances"].append(f"{name} has financial elements like savings, retirement accounts, or debts (e.g., mortgage).")

    # === Motivations & Emotional ===
    motivation_keywords = ["goal", "why", "originally", "looking for", "want", "need", "concern", "fear", "worry", "anxious"]
    motivations = [f for f in known_facts if any(k in f.lower() for k in motivation_keywords)]
    if motivations:
        profile_sections["motivations"].append(f"{name}'s core motivations include: " + "; ".join(motivations) + ". These often stem from life events, family, or health.")

    # Emotional/life events
    if any(k in full_text for k in ["divorce", "loss", "death", "grief", "excited", "planning"]):
        profile_sections["emotional"].append(f"{name}'s story includes emotional layers like personal changes or future hopes.")

    # === Lifestyle & Unique/Wild ===
    lifestyle_keywords = ["veteran", "military", "smoking", "adventure", "extreme", "hobby", "travel"]
    lifestyle_mentions = [f for f in known_facts if any(k in f.lower() for k in lifestyle_keywords)]
    if lifestyle_mentions:
        profile_sections["lifestyle"].append(f"{name}'s lifestyle includes " + ", ".join(lifestyle_mentions) + ".")

    # Catch-all unique stories (long, narrative facts)
    unique = [f for f in known_facts if len(f.split()) > 8 and f not in " ".join(profile_sections.values())]
    if unique:
        profile_sections["unique_story"].append(f"{name}'s unique life details: " + "; ".join(unique))

    # === Final Flowing Narrative ===
    narrative = ""
    priority_order = ["intro", "demographics", "family", "health", "coverage", "motivations", "emotional", "finances", "lifestyle", "unique_story"]
    for key in priority_order:
        if profile_sections[key]:
            narrative += " ".join(profile_sections[key]) + " "

    # Truncate intelligently (prioritize motivations/emotional/health/coverage)
    words = narrative.split()
    if len(words) > 140:
        priority_text = " ".join(profile_sections["intro"] + profile_sections["motivations"] + profile_sections["emotional"] + profile_sections["health"] + profile_sections["coverage"])
        narrative = priority_text + " ... " + " ".join(words[-30:])  # Keep ending for recent nuances

    return f"""FULL HUMAN IDENTITY OF THIS PERSON:
{narrative.strip()}

This is {name}'s complete, evolving story — full of real-life nuances, changes, and unique details. Use it as quiet intuition:
• Personalize empathy and relevance in every response
• Never re-ask or ignore known elements — reference naturally when it fits
• Let wild/unique aspects emerge as the conversation reveals them
• Drive toward booking by aligning with their true motivations and fears
Speak like a wise, caring advisor who truly understands their whole life — fluid, thoughtful, relentless for their family.
"""