# individual_profile.py - Emotional & Contextual Profile Builder (Right Brain)
# Produces rich, nuanced profile for Grok to use as "Quiet Intuition"

import re
import logging
from typing import List, Optional, Dict, Tuple

logger = logging.getLogger(__name__)

def build_comprehensive_profile(
    story_narrative: str,
    known_facts: List[str],
    first_name: Optional[str] = None,
    age: Optional[str] = None,
    address: Optional[str] = None
) -> Tuple[str, Dict]:
    """
    Returns:
    1. Human-readable narrative string (for prompt / memory)
    2. Rich profile context dict (for sales_director / tactical decisions)
    """
    narrative_safe = (story_narrative or "").strip()
    facts_safe = [f.strip() for f in known_facts if f and f.strip()]
    full_text = " ".join(facts_safe + [narrative_safe]).lower()

    # ─── 1. Build Emotional & Contextual Flags (Nuanced, not binary) ───
    profile_context: Dict[str, any] = {
        # Skepticism / Trust (0–3 scale)
        "skepticism_level": 0,
        "skeptical_keywords": [],
        
        # Analytical / Price-focused
        "analytical_level": 0,
        "analytical_keywords": [],
        
        # Gap / Need awareness
        "gap_awareness": 0,  # 0=none, 1=mentioned, 2=emotional, 3=urgent
        "gap_keywords": [],
        
        # High-value indicators
        "high_value_potential": False,
        
        # Health / Underwriting flags
        "health_issues_detected": [],
        "underwriting_risk_level": "low",  # low / medium / high
        
        # Demographic & Life Drivers
        "family_driver": False,
        "veteran_status": False,
        "divorce_status": False,
        
        # Current Emotional Vibe (summary)
        "current_vibe": "neutral",
        "vibe_confidence": 0.0
    }

    # ─── 2. Scan for keywords with intensity & context ───
    # Skepticism (higher intensity if multiple or strong words)
    skeptic_words = {
        "scam": 3, "fraud": 3, "ripped off": 3, "pushy": 2, "hate": 2, "angry": 2,
        "rude": 2, "burned": 2, "spam": 1, "stop": 1, "skeptic": 1
    }
    for word, intensity in skeptic_words.items():
        if word in full_text:
            profile_context["skepticism_level"] = max(profile_context["skepticism_level"], intensity)
            profile_context["skeptical_keywords"].append(word)

    # Analytical / Price focus
    analytical_words = ["price", "cost", "quote", "premium", "details", "policy", "fine print", "numbers", "compare"]
    for word in analytical_words:
        if word in full_text:
            profile_context["analytical_level"] += 1
            profile_context["analytical_keywords"].append(word)
    profile_context["analytical_level"] = min(profile_context["analytical_level"], 3)

    # Gap / Need
    gap_words = {
        "gap": 2, "need": 2, "problem": 2, "worry": 2, "concern": 2, "fear": 2,
        "expire": 2, "lapsing": 2, "no coverage": 3, "kids": 2, "spouse": 2,
        "mortgage": 2, "debt": 1
    }
    for word, intensity in gap_words.items():
        if word in full_text:
            profile_context["gap_awareness"] = max(profile_context["gap_awareness"], intensity)
            profile_context["gap_keywords"].append(word)

    # High-value signals
    high_value_signals = ["business", "estate", "wealth", "asset", "inheritance", "executive", "high net worth"]
    profile_context["high_value_potential"] = any(s in full_text for s in high_value_signals)

    # Health / Underwriting
    health_signals = {
        "diabetes": "medium", "cancer": "high", "heart": "high", "stroke": "high",
        "sick": "medium", "illness": "medium", "hospital": "medium", "smoker": "medium"
    }
    detected = []
    for word, risk in health_signals.items():
        if word in full_text:
            detected.append(word)
            if risk == "high":
                profile_context["underwriting_risk_level"] = "high"
            elif risk == "medium" and profile_context["underwriting_risk_level"] != "high":
                profile_context["underwriting_risk_level"] = "medium"
    profile_context["health_issues_detected"] = detected

    # Family & Life Drivers
    profile_context["family_driver"] = any(x in full_text for x in ["wife", "husband", "kids", "child", "spouse", "family"])
    profile_context["veteran_status"] = any(x in full_text for x in ["veteran", "military", "va"])
    profile_context["divorce_status"] = "divorced" in full_text or "divorce" in full_text

    # ─── 3. Current Vibe Summary (for prompt & sales director) ───
    if profile_context["skepticism_level"] >= 2:
        profile_context["current_vibe"] = "skeptical/guarded"
        profile_context["vibe_confidence"] = 0.8
    elif profile_context["gap_awareness"] >= 2:
        profile_context["current_vibe"] = "concerned/aware"
        profile_context["vibe_confidence"] = 0.7
    elif profile_context["analytical_level"] >= 2:
        profile_context["current_vibe"] = "analytical/price-sensitive"
        profile_context["vibe_confidence"] = 0.6
    else:
        profile_context["current_vibe"] = "open/neutral"
        profile_context["vibe_confidence"] = 0.5

    # ─── 4. Build Readable Narrative String ───
    name = (first_name or "the lead").strip().capitalize()
    if len(name.split()) > 1:
        name = name.split()[0]

    profile_sections = []

    # Basics
    basics = []
    if age:
        try:
            age_int = int(re.search(r'\d+', str(age)).group())
            if 18 <= age_int <= 120:
                basics.append(f"{name} is {age_int} years old")
        except:
            pass
    if address:
        city_state = address.split(',')[-1].strip() if ',' in address else address.strip()
        if city_state:
            basics.append(f"located in {city_state}")
    if basics:
        profile_sections.append(f"Basics: {', '.join(basics)}.")

    # Life context
    life_context = []
    if profile_context["divorce_status"]:
        life_context.append("navigating life after divorce")
    if profile_context["family_driver"]:
        life_context.append("family-oriented (mentions spouse/kids)")
    if profile_context["veteran_status"]:
        life_context.append("veteran / military background")
    if life_context:
        profile_sections.append(f"Life Context: {'; '.join(life_context)}.")

    # Health / Risk
    if profile_context["health_issues_detected"]:
        profile_sections.append(
            f"Health Notes: Mentioned {', '.join(profile_context['health_issues_detected'])}. "
            f"Underwriting risk: {profile_context['underwriting_risk_level']}."
        )

    # Evolving story (use narrative_safe as foundation)
    narrative_body = " ".join(profile_sections)
    final_narrative = f"""FULL HUMAN IDENTITY:
{narrative_body or "No confirmed demographics yet — still building rapport."}

EVOLVING STORY & NUANCE:
{narrative_safe or "Building trust and identifying primary gap."}

CURRENT VIBE:
{profile_context['current_vibe'].title()} (confidence: {profile_context['vibe_confidence']:.1f})
- Skepticism: {profile_context['skepticism_level']}/3
- Gap Awareness: {profile_context['gap_awareness']}/3
- Analytical Focus: {profile_context['analytical_level']}/3

INSTRUCTIONS FOR {name.upper()}:
• Use this as quiet intuition — reference naturally, never re-ask knowns.
• Adapt tone to current vibe (e.g., more labels if skeptical).
• Prioritize empathy and flow over rigid probing.
"""

    return final_narrative, profile_context