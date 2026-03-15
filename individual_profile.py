# individual_profile.py - Right Brain: Who Is This Person?
# Builds a person dossier from known facts. Not the conversation — the PERSON.
# Family, job, coverage, health, personality, what drives them.

import re
import logging
from typing import List, Optional, Dict, Tuple, Union

logger = logging.getLogger(__name__)

# Situational facts that go stale — these describe temporary states, not identity
SITUATIONAL_KEYWORDS = [
    "busy", "slammed", "swamped", "traveling", "vacation", "out of town",
    "laid off", "just got", "just started", "looking for work", "between jobs",
    "moving", "just moved", "switching", "this week", "this month",
    "pregnant", "expecting", "due in", "recovering", "surgery",
]

# Durable facts that never go stale — these describe who the person IS
DURABLE_KEYWORDS = [
    "kids", "children", "wife", "husband", "spouse", "married", "divorced",
    "veteran", "retired", "self-employed", "business owner", "works at",
    "diabetic", "diabetes", "cancer", "smoker", "non-smoker",
    "home owner", "homeowner", "mortgage", "rents",
]


def build_comprehensive_profile(
    story_narrative: str,
    known_facts: Union[List[str], List[Dict]],
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
    # Handle both plain string lists and temporal dict lists
    if known_facts and isinstance(known_facts[0], dict):
        facts_safe = [f for f in known_facts if f and f.get("text", "").strip()]
        full_text = " ".join([f.get("text", "") for f in facts_safe] + [narrative_safe]).lower()
    else:
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
    age_int = 0
    if name:
        identity_parts.append(name)
    if age:
        try:
            age_int = int(re.search(r'\d+', str(age)).group())
            if 18 <= age_int <= 120:
                identity_parts.append(f"age {age_int}")
        except (AttributeError, ValueError):
            age_int = 0

    identity_line = ", ".join(identity_parts) if identity_parts else ""

    # ─── Age bracket → mandatory product focus note ───
    # This is hardcoded logic, not a soft prompt hint.
    # The LLM must read this and respect it when choosing product language.
    age_bracket_note = ""
    if 18 <= age_int <= 54:
        age_bracket_note = (
            f"AGE BRACKET ({age_int}): WORKING-AGE. "
            "Relevant products: Term Life, IUL, Whole Life. "
            "Final expense and burial insurance are NOT appropriate — do not bring them up. "
            "Frame coverage around income replacement, family protection, mortgage payoff, "
            "and living benefits. Employer group coverage gaps are fair game to discuss."
        )
    elif 55 <= age_int <= 64:
        age_bracket_note = (
            f"AGE BRACKET ({age_int}): TRANSITION. "
            "Relevant products: Final Expense, Whole Life, short-term Term (10-15yr). "
            "IUL is only viable if they are in excellent health — cash value runway is limited. "
            "This person may be semi-retired. Do not assume full-time employment or heavy work coverage. "
            "Frame coverage around protecting a spouse, covering end-of-life costs, "
            "and not leaving a financial burden on family."
        )
    elif 65 <= age_int <= 75:
        age_bracket_note = (
            f"AGE BRACKET ({age_int}): SENIOR. "
            "Relevant products: Final Expense, Whole Life. "
            "CRITICAL: Term and IUL are NOT appropriate at this age — do not suggest them. "
            "This person is RETIRED. Never mention work coverage, employer plans, or group policies. "
            "Frame conversations around protecting a spouse, covering funeral/burial costs, "
            "leaving something for children or grandchildren, and not burdening family financially."
        )
    elif age_int >= 76:
        age_bracket_note = (
            f"AGE BRACKET ({age_int}): LATE SENIOR. "
            "Realistic products: Guaranteed Issue Life, Final Expense (limited carriers accept this age). "
            "CRITICAL: Term, IUL, and standard whole life are NOT options. "
            "This person is RETIRED. Never mention work coverage, term, or IUL. "
            "Be realistic and compassionate. The core driver is leaving something behind "
            "and not burdening family with funeral/final costs."
        )

    # Format facts as the core of the dossier, with temporal relevance
    if facts_safe:
        # Check if we received temporal facts (dicts with 'text' and 'days_ago')
        if facts_safe and isinstance(facts_safe[0], dict):
            fact_lines = []
            for f in facts_safe:
                text = f.get("text", f) if isinstance(f, dict) else f
                days = f.get("days_ago", 0) if isinstance(f, dict) else 0
                is_situational = any(kw in text.lower() for kw in SITUATIONAL_KEYWORDS)
                if is_situational and days > 30:
                    fact_lines.append(f"- {text} (learned {days}d ago, may be outdated)")
                else:
                    fact_lines.append(f"- {text}")
            facts_block = "\n".join(fact_lines)
        else:
            facts_block = "\n".join(f"- {f}" for f in facts_safe)
    else:
        facts_block = "Nothing confirmed yet. Still learning about this person."

    # Health note for the LLM (only if relevant)
    health_note = ""
    if profile_context["health_issues"]:
        health_note = f"\nHealth mentions: {', '.join(profile_context['health_issues'])}. Tread carefully, stay empathetic."

    age_bracket_line = f"\n{age_bracket_note}" if age_bracket_note else ""

    final_profile = f"""WHO THIS PERSON IS:
{identity_line}
{facts_block}
{health_note}{age_bracket_line}
This is everything confirmed about the lead so far. Use it as quiet intuition. Adapt to who they are. Never re-ask things listed here."""

    return final_profile, profile_context