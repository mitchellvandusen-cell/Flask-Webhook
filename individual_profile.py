# individual_profile.py - Right Brain: Who Is This Person?
# Builds a person dossier from known facts. Not the conversation — the PERSON.
# Family, job, coverage, health, personality, what drives them.

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
    RIGHT BRAIN — Who is this person?

    Takes the known facts (extracted by the narrator) and intake data,
    and builds two things:
    1. A person dossier string for the LLM prompt (who they are, their life,
       their coverage situation, their personality)
    2. A profile_context dict for sales_director routing (health flags,
       underwriting risk, etc.)

    This is NOT a conversation recap. The narrator handles that.
    This is a CHARACTER SHEET — everything we know about the human.
    """
    narrative_safe = (story_narrative or "").strip()
    facts_safe = [f.strip() for f in known_facts if f and f.strip()]
    full_text = " ".join(facts_safe + [narrative_safe]).lower()

    # ─── 1. Profile Context Dict (for sales_director routing) ───
    profile_context: Dict[str, any] = {
        "health_issues": [],
        "underwriting_risk_level": "low",
        "family_driver": False,
        "high_value_potential": False,
    }

    # Health / Underwriting flags (sales_director uses these for underwriting routing)
    health_signals = {
        "diabetes": "medium", "cancer": "high", "heart": "high", "stroke": "high",
        "sick": "medium", "illness": "medium", "hospital": "medium", "smoker": "medium"
    }
    for word, risk in health_signals.items():
        if word in full_text:
            profile_context["health_issues"].append(word)
            if risk == "high":
                profile_context["underwriting_risk_level"] = "high"
            elif risk == "medium" and profile_context["underwriting_risk_level"] != "high":
                profile_context["underwriting_risk_level"] = "medium"

    profile_context["family_driver"] = any(x in full_text for x in ["wife", "husband", "kids", "child", "spouse", "family"])
    high_value_signals = ["business", "estate", "wealth", "asset", "inheritance", "executive"]
    profile_context["high_value_potential"] = any(s in full_text for s in high_value_signals)

    # ─── 2. Build the Person Dossier (what Grok sees) ───
    name = (first_name or "").strip().split()[0].capitalize() if first_name else ""

    # Start with intake data
    identity_parts = []
    if name:
        identity_parts.append(name)
    if age:
        try:
            age_int = int(re.search(r'\d+', str(age)).group())
            if 18 <= age_int <= 120:
                identity_parts.append(f"age {age_int}")
        except (AttributeError, ValueError):
            pass

    identity_line = ", ".join(identity_parts) if identity_parts else ""

    # Format facts as the core of the dossier
    if facts_safe:
        facts_block = "\n".join(f"- {f}" for f in facts_safe)
    else:
        facts_block = "Nothing confirmed yet. Still learning about this person."

    # Health note for the LLM (only if relevant)
    health_note = ""
    if profile_context["health_issues"]:
        health_note = f"\nHealth mentions: {', '.join(profile_context['health_issues'])}. Tread carefully, stay empathetic."

    final_profile = f"""WHO THIS PERSON IS:
{identity_line}
{facts_block}
{health_note}
This is everything confirmed about the lead so far. Use it as quiet intuition. Adapt to who they are. Never re-ask things listed here."""

    return final_profile, profile_context