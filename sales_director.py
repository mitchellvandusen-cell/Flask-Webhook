# sales_director.py
# The decider: reads memory, profile, narrative → tells the texting agent the current situation
# Updated Jan 2026: light qualification → fast hand-off to real salesperson call
# Role: smart SDR / appointment setter — NOT full closer over text

import logging
from typing import Dict, Any, List

from conversation_engine import analyze_logic_flow, LogicSignal, ConversationStage
from individual_profile import build_comprehensive_profile
from underwriting import get_underwriting_context
from insurance_companies import find_company_in_message, normalize_company_name, get_company_context
from memory import (
    get_recent_messages,
    get_known_facts,
    get_narrative,
    run_narrative_observer
)

logger = logging.getLogger(__name__)


def generate_strategic_directive(
    contact_id: str,
    message: str,
    first_name: str,
    age: str | None,
    address: str | None = None
) -> Dict[str, Any]:
    """
    Returns lean context + tactical situation for the texting agent.
    Goal: qualify lightly → sense real interest / engagement → propose a call with real salesperson ASAP.
    Do NOT try to close or go deep over text — hand off to human quickly.
    """
    logger.info(f"Director | {contact_id} | msg='{message[:60]}'")

    # ─── 1. Fetch full history once (for narrative observer) ───
    all_msgs: List[Dict] = get_recent_messages(contact_id, limit=None)
    
    # Slice recent exchanges from the full list (cheap, no extra DB hit)
    recent_exchanges = all_msgs[-14:] if all_msgs else []  # last 14 messages max

    # ─── 2. Refresh narrative & facts (uses full history) ───
    observer = run_narrative_observer(contact_id, message, all_msgs)
    narrative = observer["narrative"] or ""
    known_facts = get_known_facts(contact_id)

    # ─── 3. Core logic signals (uses sliced recent history) ───
    logic: LogicSignal = analyze_logic_flow(recent_exchanges)

    # ─── 4. Profile ───
    profile_str, _ = build_comprehensive_profile(
        narrative, known_facts, first_name, age, address
    )

    # ─── 5. Underwriting & carrier (only when triggered) ───
    underwriting_ctx = ""
    full_lower = (message + narrative).lower()
    if any(kw in full_lower for kw in ["health", "medic", "condit", "prescrip", "doctor"]):
        underwriting_ctx = get_underwriting_context(message)

    company_ctx = ""
    if raw_co := find_company_in_message(message):
        norm = normalize_company_name(raw_co)
        if norm and (co_data := get_company_context(norm)):
            lines = [f"Lead mentioned carrier: {co_data.get('name', raw_co)}"]
            if co_data.get("is_guaranteed_issue"):
                lines.append("Guaranteed issue → limited coverage, higher cost.")
            if co_data.get("is_bundled"):
                lines.append("Often bundled with auto/home — usually minimal standalone.")
            if co_data.get("is_employer_provider"):
                lines.append("Likely group/employer plan — ends if they leave job.")
            company_ctx = " ".join(lines)

    # ─── 6. Tactical situation ───
    stage_value = logic.stage.value

    # Quick intent overrides — catch soft agreements early
    if any(word in full_lower for word in [
        "book", "schedule", "call", "talk", "meet", "appointment", "hop on", "quick call",
        "time work", "works for", "lets do", "sure", "yeah lets", "maybe", "probably",
        "interested", "tell me more", "numbers", "options", "when works", "set up"
    ]):
        stage_value = ConversationStage.BOOKING.value

    # Lock BOOKED if facts show success
    if any(kw in f.lower() for f in known_facts for kw in ["booked", "appointment at", "calendar"]):
        stage_value = ConversationStage.BOOKED.value

    tactical = ""

    if stage_value == ConversationStage.BOOKED.value:
        tactical = (
            "APPOINTMENT BOOKED\n"
            "Confirm the time naturally. Mention calendar invite coming.\n"
            "Stop selling. End warmly. Conversation is basically done — hand-off complete."
        )

    elif stage_value == ConversationStage.BOOKING.value:
        tactical = (
            "They are showing real interest — enough to justify a live call with the actual salesperson.\n"
            "Offer 2–3 specific times that work.\n"
            "Keep it short, confident, normal — like a busy person setting up a quick chat.\n"
            "Vibe: 'Cool, let's hop on a quick call so I can run your real numbers. I have tomorrow 11a, 2p, or Thursday 10a — which works?'"
        )

    elif stage_value == ConversationStage.INITIAL_OUTREACH.value:
        if recent_exchanges and not message:  # outbound follow-up (ghosting)
            tactical = (
                "FOLLOW-UP — no reply yet.\n"
                "Nudge casually. Don't repeat the opener.\n"
                "Goal: any response — see if they're worth qualifying further."
            )
        else:
            tactical = (
                "EARLY / FIRST CONTACT\n"
                "Casual check-in. Reference life insurance interest.\n"
                f"Use '{first_name}' if known. Ask one natural question.\n"
                "Sound like a real person texting — goal is to see if they engage at all."
            )

    else:  # QUALIFYING / DISCOVERY — intentionally light & fast-moving
        if logic.mentioned_goal and logic.mentioned_obstacle:
            tactical = (
                "They've shared why they need coverage and why what they have falls short.\n"
                "This is usually plenty of qualification — the real salesperson can take it from here.\n"
                "If they're responding well (decent length replies, asking questions back, not dodging),\n"
                "it's time to propose a quick call with the actual person who can help them properly.\n"
                "Don't go deeper over text — move to offering times soon."
            )
        elif logic.has_coverage:
            tactical = (
                "They mentioned some existing coverage.\n"
                "Quickly clarify the basics if unclear (term/group/amount?), then highlight common gaps.\n"
                "Transition fast: 'This is probably easier to sort properly on a short call with the team.'\n"
                "Goal is NOT full analysis here — goal is to get to a live conversation."
            )
        else:
            tactical = (
                "Still early — checking for real interest.\n"
                "Ask 1–2 thoughtful questions about protection needs, family, or current setup.\n"
                "Pay attention to reply quality: longer answers / questions back = green light to suggest a call soon.\n"
                "Short/evasive = slow down or circle back later.\n"
                "Remember: you're just the warm-up guy. The real sale happens on the call with a human."
            )

        # Brief empathy gate — never linger
        vulnerable = any(kw in full_lower for kw in [
            "cancer", "sick", "divorce", "lost job", "scared", "worried", "single mom", "alone", "diagnosis"
        ])
        if vulnerable:
            tactical = (
                "They're dealing with something tough.\n"
                "Acknowledge briefly, then keep moving toward 'a quick call can help sort this'.\n"
            ) + tactical

    # ─── Final output ───
    return {
        "profile_str": profile_str.strip(),
        "tactical_narrative": tactical.strip(),
        "stage": stage_value,
        "underwriting_context": underwriting_ctx.strip(),
        "company_context": company_ctx.strip(),
        "known_facts": known_facts,
        "story_narrative": narrative.strip(),
        "recent_exchanges": recent_exchanges,  # sliced — cheap & sufficient
    }