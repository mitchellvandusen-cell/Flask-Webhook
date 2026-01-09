# individual_profile.py - Ultimate Adaptive Comprehensive Identity Builder (2026 Final Version)

import re
from typing import List, Optional

def build_comprehensive_profile(
    story_narrative: str, 
    known_facts: List[str], 
    first_name: Optional[str] = None,
    age: Optional[str] = None,
    address: Optional[str] = None
) -> str:
    """
    Builds a robust human identity profile using both the evolving narrative
    and raw structured facts. Includes safety checks to prevent crashes on bad data.
    """
    # SAFETY: Handle None types gracefully to prevent crashes
    narrative_safe = (story_narrative or "")
    facts_safe = (known_facts or [])
    
    # Normalize name
    name = (first_name or "the lead").strip().capitalize()
    if len(name.split()) > 1:
        name = name.split()[0]  # First name for warmth
    
    profile_sections = {
        "demographics": [],
        "family": [],
        "health": [],
        "coverage": [],
        "motivations": []
    }

    # === Age Logic (Safe) ===
    if age:
        try:
            # Extract first distinct number (handles "45 years", "approx 50")
            matches = re.findall(r'\d+', str(age))
            if matches:
                age_int = int(matches[0])
                # Logic check: Don't accidentally grab a year like "1980" as an age
                if 18 <= age_int <= 120:
                    if age_int >= 60: 
                        profile_sections["demographics"].append(f"{name} is {age_int}, likely focused on legacy.")
                    else: 
                        profile_sections["demographics"].append(f"{name} is {age_int} years old.")
        except Exception: 
            pass # Never crash on age parsing

    # === Address/Location Logic (Enhanced) ===
    if address:
        try:
            # Robust split by comma, newline, or just spaces if no commas exist
            if "," in address:
                parts = [p.strip() for p in address.split(',') if p.strip()]
            else:
                parts = [address.strip()]

            # Smart extraction: If we have multiple parts, grab the second to last (usually State)
            if len(parts) >= 2:
                location = parts[-2] # Usually City or State
                profile_sections["demographics"].append(f"Located in {location}.")
            elif len(parts) == 1:
                profile_sections["demographics"].append(f"Located in {parts[0]}.")
        except Exception:
            pass

    # === Keyword Scraping (Redundancy Safety Net) ===
    # SAFETY: Combine safely ensuring no NoneTypes
    full_text = (" ".join(facts_safe) + " " + narrative_safe).lower()
    
    if "divorced" in full_text: 
        profile_sections["family"].append("Navigating post-divorce responsibilities.")
    
    # Health checks
    if any(x in full_text for x in ["diabetes", "heart", "cancer", "stroke", "copd"]): 
        profile_sections["health"].append("Has specific health history that requires sensitive underwriting.")
    
    # Coverage Patterns
    if any(k in full_text for k in ["term", "whole", "policy", "state farm", "metlife", "prudential", "mutual"]):
        profile_sections["coverage"].append("Existing coverage mentions detected; clarify portability and expiration.")

    # === BUILD THE STORY ===
    # Join distinct sections only
    narrative_body = " ".join(profile_sections["demographics"] + 
                              profile_sections["family"] + 
                              profile_sections["health"] + 
                              profile_sections["coverage"])
    
    return f"""FULL HUMAN IDENTITY:
Basics: {narrative_body if narrative_body else "No specific demographics confirmed yet."}

Evolving Story & Nuance: 
{narrative_safe if narrative_safe.strip() else "Building rapport and identifying the primary gap."}

Instructions for {name}:
• Use this 'Quiet Intuition' to avoid re-asking questions.
• Reference family dynamics or specific insurance companies mentioned above.
• The narrative captures the 'grey areas'—the brother-in-law, the hesitation, the timing.
"""