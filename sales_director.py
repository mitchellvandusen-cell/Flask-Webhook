# sales_director.py
# The decider: reads memory, profile, narrative → tells the texting agent the current situation
# Updated Jan 2026: Single DB Call + Global Ghosting Protection + Momentum Logic

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
    """
    logger.info(f"Director | {contact_id} | msg='{message[:60]}'")

    # ─── 1. INTELLIGENCE GATHERING (Single DB Fetch) ───
    # We fetch ALL messages once because the Narrative Observer needs the full story.
    all_msgs: List[Dict] = get_recent_messages(contact_id, limit=None)
    
    # OPTIMIZATION: Slice the recent exchanges from the list we just fetched.
    # We do NOT call the database a second time.
    recent_exchanges = all_msgs[-14:] if all_msgs else []  # Last 14 messages for prompt context

    # ─── 2. REFRESH NARRATIVE & FACTS ───
    # Run the observer on the full history
    observer = run_narrative_observer(contact_id, message, all_msgs)
    narrative = observer["narrative"] or ""
    known_facts = get_known_facts(contact_id)

    # ─── 3. ANALYZE LOGIC FLOW ───
    # The logic engine only needs the recent context to determine current state
    logic: LogicSignal = analyze_logic_flow(recent_exchanges)

    # ─── 4. BUILD PROFILE ───
    profile_str, _ = build_comprehensive_profile(
        narrative, known_facts, first_name, age, address
    )

    # ─── 5. CONTEXTUAL INTELLIGENCE (Underwriting & Carriers) ───
    underwriting_ctx = ""
    full_lower = (message + narrative).lower()
    # Only run regex if relevant keywords appear (Optimization)
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

    # ─── 6. DETERMINE STAGE ───
    stage_value = logic.stage.value

    # A. Quick Intent Overrides (Catch soft agreements early)
    if any(word in full_lower for word in [
        "book", "schedule", "call", "talk", "meet", "appointment", "hop on", "quick call",
        "time work", "works for", "lets do", "sure", "yeah lets", "maybe", "probably",
        "interested", "tell me more", "numbers", "options", "when works", "set up"
    ]):
        stage_value = ConversationStage.BOOKING.value

    # B. Fact-Based Override (If backend knows they booked, lock it)
    if any(kw in f.lower() for f in known_facts for kw in ["booked", "appointment at", "calendar"]):
        stage_value = ConversationStage.BOOKED.value

    # ─── 7. GENERATE TACTICAL DIRECTIVE ───
    tactical = ""

    # === GLOBAL GHOSTING CHECK (The Critical Fix) ===
    # If this is an Outbound Trigger (empty message) and we have history,
    # it is ALWAYS a follow-up, regardless of whether the stage says "Qualifying" or "Initial".
    # This prevents the bot from "replying" to silence.
    if not message and len(recent_exchanges) > 0:
        tactical = (
            "FOLLOW-UP / RE-ENGAGE\n"
            "The lead has not responded to your previous message.\n"
            "Check the conversation recap. Nudge them gently.\n"
            "Do not repeat the exact same introduction.\n"
            "Goal: Get a response — see if they're worth qualifying further."
        )

    # === STAGE SPECIFIC LOGIC ===
    elif stage_value == ConversationStage.BOOKED.value:
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
        # True Initial Outreach (First ever contact)
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

    # ─── 8. FINAL OUTPUT (Single Return) ───
    return {
        "profile_str": profile_str.strip(),
        "tactical_narrative": tactical.strip(),
        "stage": stage_value,
        "underwriting_context": underwriting_ctx.strip(),
        "company_context": company_ctx.strip(),
        "known_facts": known_facts,
        "story_narrative": narrative.strip(),
        "recent_exchanges": recent_exchanges, # Passed back to tasks.py so it doesn't have to fetch again
    }