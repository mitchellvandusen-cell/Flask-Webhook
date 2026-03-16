# individual_profile.py - Right Brain: Who Is This Person?
# Builds a person dossier from known facts + CRM contact data.
# Not the conversation — the PERSON.
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

# Custom field keys that should NOT be shown to the LLM (internal/technical)
_EXCLUDED_CUSTOM_FIELD_KEYS = {
    "lead_vendor", "leadvendor", "lead vendor",
    "lead_source", "leadsource", "lead source",
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "gclid", "fbclid", "ip_address", "ip address",
    "stripe_customer_id", "subscription_id",
}


def build_comprehensive_profile(
    story_narrative: str,
    known_facts: Union[List[str], List[Dict]],
    first_name: Optional[str] = None,
    age: Optional[str] = None,
    address: Optional[str] = None,
    # ── Enriched contact data from CRM ──
    last_name: Optional[str] = None,
    company_name: Optional[str] = None,
    tags: Optional[List[str]] = None,
    notes: Optional[List[Dict]] = None,
    custom_fields: Optional[List[Dict]] = None,
    source: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    gender: Optional[str] = None,
) -> Tuple[str, Dict]:
    """
    RIGHT BRAIN — Who is this person?

    Takes the known facts (extracted by the narrator), intake data, and CRM
    contact card fields, and builds two things:
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

    # Company name from CRM also signals high-value potential
    if company_name and company_name.strip():
        profile_context["high_value_potential"] = True

    # ─── 2. Build the Person Dossier (what Grok sees) ───
    name = (first_name or "").strip().split()[0].capitalize() if first_name else ""
    last = (last_name or "").strip().capitalize() if last_name else ""

    # Start with intake data
    identity_parts = []
    age_int = 0
    if name:
        full_name = f"{name} {last}" if last else name
        identity_parts.append(full_name)
    if age:
        try:
            age_int = int(re.search(r'\d+', str(age)).group())
            if 18 <= age_int <= 120:
                identity_parts.append(f"age {age_int}")
        except (AttributeError, ValueError):
            age_int = 0

    # Gender context (only if available, helps with pronoun/product framing)
    if gender:
        g = gender.strip().lower()
        if g in ("m", "male"):
            identity_parts.append("male")
        elif g in ("f", "female"):
            identity_parts.append("female")

    identity_line = ", ".join(identity_parts) if identity_parts else ""

    # ─── CRM context lines (company, location, source) ───
    crm_lines = []
    if company_name and company_name.strip():
        crm_lines.append(f"- Works at: {company_name.strip()}")
    if city and state:
        crm_lines.append(f"- Location: {city.strip()}, {state.strip()}")
    elif state:
        crm_lines.append(f"- State: {state.strip()}")
    elif city:
        crm_lines.append(f"- City: {city.strip()}")
    if source and source.strip():
        crm_lines.append(f"- Lead source: {source.strip()}")

    # ─── Tags (filter out internal/system tags, keep descriptive ones) ───
    tag_lines = []
    if tags:
        # Skip tags that look like internal system tags
        _system_prefixes = ("utm_", "ghl_", "lc_", "api_", "webhook_")
        descriptive_tags = [
            t.strip() for t in tags
            if t and t.strip()
            and not t.strip().lower().startswith(_system_prefixes)
            and len(t.strip()) > 1
        ]
        if descriptive_tags:
            tag_lines.append(f"- CRM tags: {', '.join(descriptive_tags)}")

    # ─── Custom fields (filter out technical/internal ones) ───
    cf_lines = []
    if custom_fields:
        for cf in custom_fields:
            if not isinstance(cf, dict):
                continue
            val = cf.get("value", "")
            name_key = cf.get("name", "") or cf.get("fieldKey", "")
            if not val or not name_key:
                continue
            # Skip internal/technical fields
            if name_key.lower().strip() in _EXCLUDED_CUSTOM_FIELD_KEYS:
                continue
            # Skip very long values (probably notes or JSON)
            val_str = str(val).strip()
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            if val_str:
                cf_lines.append(f"- {name_key}: {val_str}")

    # ─── Agent notes from contact card ───
    notes_block = ""
    if notes:
        # Take most recent 3 notes, strip HTML, limit length
        note_lines = []
        for n in notes[:3]:
            if not isinstance(n, dict):
                continue
            body = (n.get("body") or "").strip()
            if not body:
                continue
            # Truncate long notes
            if len(body) > 300:
                body = body[:300] + "..."
            note_lines.append(f"  - {body}")
        if note_lines:
            notes_block = "\nAGENT NOTES (written by the insurance agent about this lead):\n" + "\n".join(note_lines)

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

    # ─── Assemble CRM data section (only if we have any) ───
    crm_section = ""
    all_crm = crm_lines + tag_lines + cf_lines
    if all_crm:
        crm_section = "\n" + "\n".join(all_crm)

    final_profile = f"""WHO THIS PERSON IS:
{identity_line}
{facts_block}{crm_section}{notes_block}
{health_note}{age_bracket_line}
This is everything confirmed about the lead so far. Use it as quiet intuition. Adapt to who they are. Never re-ask things listed here."""

    return final_profile, profile_context
