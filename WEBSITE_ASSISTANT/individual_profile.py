# individual_profile.py - SaaS Buyer Profiler
import re
from typing import List, Dict, Tuple

def build_comprehensive_profile(story_narrative: str, known_facts: List[str]) -> Tuple[str, Dict]:
    full_text = " ".join(known_facts + [story_narrative]).lower()
    
    profile = {
        "role": "unknown",         # individual vs agency
        "lead_volume": "unknown",  # low / high
        "current_tech": [],        # ghl, hubspot, etc
        "pain_type": "unknown",    # cost, time, conversion
        "vibe": "neutral"
    }

    # 1. Role Detection
    if any(x in full_text for x in ["agency", "team", "sub account", "clients", "staff", "employees"]):
        profile["role"] = "agency_owner"
    elif any(x in full_text for x in ["solo", "just me", "independent", "broker"]):
        profile["role"] = "individual"

    # 2. Tech Stack
    techs = ["ghl", "highlevel", "gohighlevel", "hubspot", "salesforce", "clickfunnels", "zapier"]
    profile["current_tech"] = [t for t in techs if t in full_text]

    # 3. Lead Volume (Proxy for Agency Pro)
    if any(x in full_text for x in ["thousands", "database", "huge list", "old leads", "1000", "500"]):
        profile["lead_volume"] = "high"

    # 4. Vibe Check
    if any(x in full_text for x in ["scam", "bot", "fake", "expensive"]):
        profile["vibe"] = "skeptical"
    elif any(x in full_text for x in ["ready", "buy", "sign up", "card"]):
        profile["vibe"] = "ready_to_buy"

    # Narrative Construction
    narrative = f"""
BUYER PROFILE:
- Role: {profile['role'].upper()}
- Volume: {profile['lead_volume'].upper()}
- Tech: {', '.join(profile['current_tech']) or 'Unknown'}
- Vibe: {profile['vibe'].upper()}

CONTEXT:
{story_narrative}
"""
    return narrative, profile