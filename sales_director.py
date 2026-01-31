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

    logger.info(f"🔍 SALES DIRECTOR | contact_id={contact_id} | first_name={first_name}")

    # 1. GATHER INTELLIGENCE
    # Get ALL messages for narrative observer (unlimited memory)
    all_messages = get_recent_messages(contact_id, limit=None)

    # Single LLM call: updates narrative AND extracts new facts
    # The narrator reads the full conversation, understands meaning, and produces:
    # 1. A flowing story paragraph (who this person is, what they want, where things stand)
    # 2. Discrete facts extracted from what the lead actually said/confirmed
    observer_result = run_narrative_observer(contact_id, message, all_messages)
    story_narrative = observer_result["narrative"]

    # Get all known facts (existing DB facts + any new ones the narrator just extracted)
    known_facts = get_known_facts(contact_id)

    # Get recent 10 for logic flow analysis
    recent_exchanges = get_recent_messages(contact_id, limit=10)

    logger.debug(f"🔍 NARRATIVE CHECK | contact_id={contact_id} | narrative_preview={story_narrative[:100] if story_narrative else 'EMPTY'}")

    # 2. ANALYZE
    logic: LogicSignal = analyze_logic_flow(recent_exchanges)
    profile_str, profile_ctx = build_comprehensive_profile(story_narrative, known_facts, first_name, age, address)

    logger.debug(f"🔍 PROFILE BUILT | contact_id={contact_id} | profile_preview={profile_str[:150] if profile_str else 'EMPTY'}")

    # Underwriting & Company Context
    underwriting_ctx = ""
    if "health" in message.lower() or "medic" in message.lower() or profile_ctx.get("health_issues"):
        underwriting_ctx = get_underwriting_context(message)

    company_ctx = ""
    raw_company = find_company_in_message(message)
    if raw_company:
        normalized = normalize_company_name(raw_company)
        if normalized:
            company_data = get_company_context(normalized)
            if company_data and isinstance(company_data, dict):
                parts = [f"Lead mentioned: {company_data.get('name', raw_company)}"]
                if company_data.get("is_guaranteed_issue"):
                    parts.append("This is a guaranteed issue carrier (limited coverage, higher cost per dollar). Opportunity to show better options.")
                if company_data.get("is_bundled"):
                    parts.append("This carrier typically bundles life with auto/home. Coverage is often minimal add-on, not standalone.")
                if company_data.get("is_employer_provider"):
                    parts.append("Common employer group plan provider. Coverage usually ends when they leave the job.")
                company_ctx = " ".join(parts)

    # 3. BUILD DIRECTIVE
    directive = ""
    framework = ""

    # === INITIAL OUTREACH ===
    if logic.stage == ConversationStage.INITIAL_OUTREACH:
        # Personalization details
        has_name = bool(first_name)
        has_age = bool(age)
        # PRIVACY: Do NOT expose address to bot - only for backend context
        # has_location = bool(address)

        personalization_note = ""
        if has_name:
            personalization_note += f"\nUSE FIRST NAME: '{first_name}' naturally in your opening."
        if has_age:
            personalization_note += f"\nYou know their age ({age}). Use this to feel less cold, but DON'T mention their location/address."

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
                expertise_note = f"\n\nCompany context: {company_ctx}"

            directive = f"""THEY HAVE COVERAGE

They've mentioned having coverage. Check the conversation recap to see what's already been discussed about it. Do not re-ask questions that were already answered.

If their coverage hasn't been explored yet, get curious about it. If it has, move forward.{expertise_note if expertise_note else ""}"""

        # Don't know their situation yet
        elif not logic.mentioned_goal:
            directive = """CONTINUE THE CONVERSATION

Read the conversation recap. Respond to what the lead just said. Move things forward naturally based on where the conversation is.

If you don't know their situation yet, ask about it. If you already asked and they answered, build on that answer. The recap has what's been covered."""

        # They told you the problem - confirm they don't want that
        elif logic.mentioned_goal and not logic.mentioned_obstacle:
            directive = """THEY'VE SHARED THEIR SITUATION

They've told you about who they're protecting or what they need. Check the recap for exactly what was said.

Help them think about why it matters. Then offer to help. Don't repeat questions they already answered."""

        # They see the gap - ask if they want help
        else:
            directive = """READY TO MOVE FORWARD

They understand the gap. If you haven't offered to help yet, do it. If you already did, move toward booking.

Check the recap. Don't repeat yourself."""

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
