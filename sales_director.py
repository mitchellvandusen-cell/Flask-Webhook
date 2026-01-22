# sales_director.py - Simplified Life Insurance Sales Logic
# "Keep it stupid simple"

import logging
from conversation_engine import analyze_logic_flow, LogicSignal, ConversationStage
from individual_profile import build_comprehensive_profile
from underwriting import get_underwriting_context
from insurance_companies import get_company_context, find_company_in_message, normalize_company_name
from memory import get_recent_messages, get_known_facts, get_narrative, run_narrative_observer

logger = logging.getLogger(__name__)

def generate_strategic_directive(contact_id: str, message: str, first_name: str, age: str, address: str) -> dict:
    """
    Simple formula:
    1. Why are you looking?
    2. How fucked would your family be?
    3. Do you want that?
    4. Book appointment
    """

    # 1. GATHER INTELLIGENCE
    run_narrative_observer(contact_id, message)

    recent_exchanges = get_recent_messages(contact_id, limit=10)
    story_narrative = get_narrative(contact_id)
    known_facts = get_known_facts(contact_id)

    # 2. ANALYZE
    logic: LogicSignal = analyze_logic_flow(recent_exchanges)
    profile_str, profile_ctx = build_comprehensive_profile(story_narrative, known_facts, first_name, age, address)

    # Underwriting & Company Context
    underwriting_ctx = ""
    if "health" in message.lower() or "medic" in message.lower() or profile_ctx.get("health_issues"):
        underwriting_ctx = get_underwriting_context(message)

    company_ctx = ""
    raw_company = find_company_in_message(message)
    if raw_company:
        normalized = normalize_company_name(raw_company)
        if normalized:
            company_ctx = get_company_context(normalized)

    # 3. BUILD DIRECTIVE
    directive = ""
    framework = ""

    # === INITIAL OUTREACH ===
    if logic.stage == ConversationStage.INITIAL_OUTREACH:
        first_name_instruction = f"Use '{first_name}' in your opening." if first_name else "No first name."
        directive = f"""INITIAL OUTREACH

{first_name_instruction}

Options:
- "Quick question about your life insurance - are you still looking or did that get handled?"
- "Noticed you were looking at life insurance. Still figuring that out?"

Rules:
- NO "Hey/Hi/Hello"
- Mention life insurance
- Brief (1-2 sentences)
- One question max"""
        framework = "INITIAL"

        return {
            "profile_str": profile_str,
            "tactical_narrative": f"{framework}\n\n{directive}",
            "stage": logic.stage.value,
            "underwriting_context": underwriting_ctx,
            "company_context": company_ctx,
            "known_facts": known_facts,
            "story_narrative": story_narrative,
            "recent_exchanges": recent_exchanges
        }

    # === BOOKED ===
    elif logic.stage == ConversationStage.BOOKED:
        directive = """APPOINTMENT BOOKED

Simple confirmation:
- "Perfect, see you Tuesday at 2pm"
- "You're all set for Thursday at 5pm"

Then STOP. Don't keep talking."""
        framework = "BOOKED"

        return {
            "profile_str": profile_str,
            "tactical_narrative": f"{framework}\n\n{directive}",
            "stage": logic.stage.value,
            "underwriting_context": underwriting_ctx,
            "company_context": company_ctx,
            "known_facts": known_facts,
            "story_narrative": story_narrative,
            "recent_exchanges": recent_exchanges
        }

    # === BOOKING (They agreed to call) ===
    elif logic.stage == ConversationStage.BOOKING:
        directive = """BOOKING - Offer times

They agreed to a call. NOW offer specific times.

- "I have Tuesday at 2pm or Thursday at 5pm. Which works?"
- "Morning or afternoon better for you?"

Keep it simple. Get a time."""
        framework = "BOOKING"

    # === QUALIFYING (The Simple Formula) ===
    else:
        full_context = (story_narrative + " " + " ".join(known_facts)).lower()

        # Do they have coverage already?
        if logic.has_coverage:
            directive = """SEED DOUBT

They have coverage. Make them question it.

Smart questions:
- "Is there a waiting period on that?"
- "Living benefits or just death benefit?"
- "Work policy? What happens if you change jobs?"
- "How long you had that? Rates have dropped..."

Point: Get them to realize what they have might not be enough."""

        # Do we know why they're looking?
        elif not logic.mentioned_goal:
            directive = """WHY ARE YOU LOOKING?

Find out why they're looking. What are they trying to protect?

Simple questions:
- "What made you start looking at this?"
- "Who are you trying to make sure is taken care of?"

Just ask. Let them answer. One step at a time."""

        # Do they understand the consequence?
        elif logic.mentioned_goal and not logic.mentioned_obstacle:
            directive = """POINT OUT THE CONSEQUENCE

They told you their goal. Now point out what happens if they don't fix it.

Example flow:
Them: "I want to protect my daughter"
You: "Can she pay for everything if something happened tomorrow?"

OR

Them: "Want to make sure my mortgage is covered"
You: "Is it covered now, or would that fall on your wife?"

Point: Make them realize the gap."""

        # They see the gap - ask if they want help
        else:
            directive = """ASK IF THEY WANT HELP

They know why they're looking. They see the consequence.

Now ask if they want help:
- "Want me to help you figure out what makes sense?"
- "Would you be opposed to a quick call to lock this down?"

DON'T offer times yet.
Wait for them to say yes.
THEN next message offer times.

Keep it simple."""

        framework = "QUALIFYING"

    return {
        "profile_str": profile_str,
        "tactical_narrative": f"{framework}\n\n{directive}",
        "stage": logic.stage.value,
        "underwriting_context": underwriting_ctx,
        "company_context": company_ctx,
        "known_facts": known_facts,
        "story_narrative": story_narrative,
        "recent_exchanges": recent_exchanges
    }
