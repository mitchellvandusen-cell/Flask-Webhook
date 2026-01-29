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

    # 🚨 CRITICAL DEBUG LOGGING
    logger.critical(f"🔍 SALES DIRECTOR | contact_id={contact_id} | first_name={first_name}")

    # 1. GATHER INTELLIGENCE
    recent_exchanges = get_recent_messages(contact_id, limit=10)

    # Update narrative with FULL conversation context (bot + lead messages)
    run_narrative_observer(contact_id, message, recent_exchanges)

    story_narrative = get_narrative(contact_id)
    known_facts = get_known_facts(contact_id)

    # 🚨 DEBUG: Check what narrative was retrieved
    logger.critical(f"🔍 NARRATIVE CHECK | contact_id={contact_id} | narrative_preview={story_narrative[:100] if story_narrative else 'EMPTY'}")

    # 2. ANALYZE
    logic: LogicSignal = analyze_logic_flow(recent_exchanges)
    profile_str, profile_ctx = build_comprehensive_profile(story_narrative, known_facts, first_name, age, address)

    # 🚨 DEBUG: Check profile output
    logger.critical(f"🔍 PROFILE BUILT | contact_id={contact_id} | profile_preview={profile_str[:150] if profile_str else 'EMPTY'}")

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
        # Personalization details
        has_name = bool(first_name)
        has_age = bool(age)
        has_location = bool(address)

        personalization_note = ""
        if has_name:
            personalization_note += f"\nUSE FIRST NAME: '{first_name}' naturally in your opening."
        if has_age or has_location:
            personalization_note += f"\nYou know their context ({age if has_age else ''} {address if has_location else ''}). Use this to feel less cold."

        directive = f"""INITIAL OUTREACH

First contact. They looked at life insurance before but don't remember you.

{personalization_note if personalization_note else "No name available."}

Your approach: Check in naturally, like you're following up on something they started.

Reference that they were looking at life insurance. Ask where they ended up with it.

Keep it brief. Natural. One question. Get them to reply.

No formal greetings. No self-introductions. Sound like a real person checking in."""
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
        directive = """APPOINTMENT BOOKED IN CALENDAR

Confirm the specific time. Mention they'll get calendar invite. STOP.

DO NOT ask for phone number (you're texting them!), email, or any contact info.

Example: "You're all set for Friday at 10am. Calendar invite coming your way!"

Then STOP. Conversation is over."""
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
        directive = """BOOKING

They agreed to a call. Offer specific times.

Keep it simple. Get a time."""
        framework = "BOOKING"

    # === QUALIFYING (The Simple Formula) ===
    else:
        full_context = (story_narrative + " " + " ".join(known_facts)).lower()

        # DETECT EMOTIONAL/VULNERABLE SITUATIONS
        vulnerable_keywords = ["struggling", "tough", "hard", "stress", "worried", "scared", "cancer",
                               "sick", "diagnosis", "lost job", "divorce", "single parent", "alone"]
        is_vulnerable = any(keyword in full_context for keyword in vulnerable_keywords)

        # If they're in a vulnerable state, lead with empathy
        if is_vulnerable:
            directive = """VULNERABLE SITUATION

They're going through something tough.

Acknowledge it first. Show you get it. Then continue gently.

Don't rush. Build trust. They'll open up when they feel understood."""

        # Do they have coverage already?
        elif logic.has_coverage:
            expertise_note = ""
            if company_ctx:
                expertise_note = "\n\n🧠 SHOW EXPERTISE - Company context available. Use it to build credibility:\n\"[Company name] is solid\" or \"Been around forever\" or relevant insight about the carrier.\nThen ask the smart question."

            directive = f"""THEY HAVE COVERAGE

Make them question if it's enough. But don't assume or attack.

Ask questions that make them realize they should know more about what they have.

Understanding approach. Not judgmental.{expertise_note if expertise_note else ""}"""

        # Don't know their situation yet
        elif not logic.mentioned_goal:
            context_note = ""
            if "kids" in full_context or "children" in full_context or "mortgage" in full_context:
                context_note = "\n\n💡 They mentioned kids/mortgage - SHOW you understand their situation before asking:\n\"Two kids and a mortgage - that's exactly who this is for.\"\nThen: \"Would your family need to take out a loan if something happened?\""

            directive = f"""DISCOVER THEIR SITUATION

Don't know their situation yet. Find out.

Open-ended questions. Let them tell you what would happen.{context_note if context_note else ""}

Don't answer for them. They need to realize the problem themselves."""

        # They told you the problem - confirm they don't want that
        elif logic.mentioned_goal and not logic.mentioned_obstacle:
            directive = """CONFIRM THE GAP

They told you what would happen. Make them think about it.

Understanding approach. Not attacking.

Help them see why it matters. Then offer to help."""

        # They see the gap - ask if they want help
        else:
            directive = """OFFER HELP

They see the gap. Ask if they want help fixing it.

Don't offer times yet. Get agreement first. Then next message offer specific times."""

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
