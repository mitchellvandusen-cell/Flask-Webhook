# individual_profile.py - The Emotional Context Processor (Right Brain)
import re
from typing import List, Optional, Dict, Tuple

def build_comprehensive_profile(
    story_narrative: str, 
    known_facts: List[str], 
    first_name: Optional[str] = None,
    age: Optional[str] = None,
    address: Optional[str] = None
) -> Tuple[str, Dict]:
    """
    Returns TWO things:
    1. The readable Narrative String (for the Prompt).
    2. The Profile Context Dictionary (for the Sales Director).
    """
    # SAFETY: Handle None types
    narrative_safe = (story_narrative or "")
    facts_safe = (known_facts or [])
    full_text = (" ".join(facts_safe) + " " + narrative_safe).lower()
    
    # 1. Build The Context Dictionary (Emotional Flags)
    profile_context = {
        "is_skeptical": any(x in full_text for x in ["skeptic", "scam", "hate", "angry", "rude", "burned", "spam", "stop"]),
        "is_analytical": any(x in full_text for x in ["price", "cost", "details", "policy", "fine print", "numbers"]),
        "has_gap": any(x in full_text for x in ["gap", "need", "problem", "worry", "concern", "fear", "mortgage", "kids", "expire"]),
        "high_value": any(x in full_text for x in ["business", "estate", "wealth", "asset"]),
        "health_issues": any(x in full_text for x in ["diabetes", "cancer", "heart", "stroke", "sick", "illness"]),
        "veteran": "veteran" in full_text or "military" in full_text,
        "family_driver": any(x in full_text for x in ["wife", "husband", "kids", "child", "spouse"])
    }

    # 2. Build the Narrative String (Same as before, simplified for brevity)
    name = (first_name or "the lead").strip().capitalize()
    if len(name.split()) > 1: name = name.split()[0]
    
    profile_sections = []
    
    # Age/Location logic...
    if age:
        try:
            matches = re.findall(r'\d+', str(age))
            if matches:
                age_int = int(matches[0])
                if 18 <= age_int <= 120:
                    profile_sections.append(f"{name} is {age_int} years old.")
        except: pass
        
    if address:
        if "," in address: parts = address.split(',')
        else: parts = [address]
        if len(parts) >= 1: profile_sections.append(f"Located in {parts[-1].strip()}.")

    # Keyword logic...
    if "divorced" in full_text: profile_sections.append("Navigating post-divorce.")
    if profile_context["health_issues"]: profile_sections.append("Has health history requiring underwriting care.")
    
    narrative_body = " ".join(profile_sections)
    
    final_string = f"""FULL HUMAN IDENTITY:
Basics: {narrative_body if narrative_body else "No specific demographics confirmed yet."}

Evolving Story & Nuance: 
{narrative_safe if narrative_safe.strip() else "Building rapport and identifying the primary gap."}

Instructions for {name}:
• Use this 'Quiet Intuition'.
• Reference family/health details naturally.
• Current Emotional State: {'Skeptical/Guarded' if profile_context['is_skeptical'] else 'Open/Neutral'}.
"""
    return final_string, profile_context