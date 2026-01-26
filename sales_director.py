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

        # DETECT EMOTIONAL/VULNERABLE SITUATIONS
        vulnerable_keywords = ["struggling", "tough", "hard", "stress", "worried", "scared", "cancer",
                               "sick", "diagnosis", "lost job", "divorce", "single parent", "alone"]
        is_vulnerable = any(keyword in full_context for keyword in vulnerable_keywords)

        # If they're in a vulnerable state, lead with empathy
        if is_vulnerable:
            directive = """EMOTIONAL SITUATION - LEAD WITH EMPATHY

They're going through something tough. Acknowledge it FIRST before moving forward.

Pattern:
1. Acknowledge: "That's tough. I can't imagine dealing with all that."
2. Validate: "Makes sense you're thinking about this now though."
3. Continue gently: "Mind if I ask - what are you most worried about?"

Examples:
Them: "I'm a single mom, money's tight, just got diagnosed with diabetes"
You: "That's a lot on your plate. Dealing with health stuff while trying to keep everything together."
Then: "I get why you're looking into this. What worries you most about your kids if something happened?"

Them: "Lost my job, trying to figure out what to do"
You: "Man, that's rough. Job market's brutal right now."
Then: "At least you're thinking ahead though. You have any coverage through COBRA or anything?"

DON'T rush to close. Build trust first. They'll buy when they feel understood."""

        # Do they have coverage already?
        elif logic.has_coverage:
            expertise_note = ""
            if company_ctx:
                expertise_note = "\n\n🧠 SHOW EXPERTISE - Company context available. Use it to build credibility:\n\"[Company name] is solid\" or \"Been around forever\" or relevant insight about the carrier.\nThen ask the smart question."

            directive = f"""SEED DOUBT (BUT DON'T ASSUME)

They have coverage. Make them question it. But DON'T make assumptions about what they pay.

Smart questions:
- "Is there a waiting period on that?"
- "Living benefits or just death benefit?"
- "Work policy? What happens if you change jobs?"
- "How long you had it?"
- "You know what the death benefit is off the top of your head?"

UNDERSTANDING approach (not attacking):
- "Most people don't look at their policy after they get it. Normal thing. When's the last time you actually reviewed it?"
- "[Company]'s solid. You know if it covers what you need today or just what you needed back then?"

DON'T say:
❌ "Rates have dropped" (then contradicts with rates going up)
❌ "You okay paying more?" (we don't know what they pay)
❌ "You okay not knowing?" (too attacking)

Point: Get them to realize they should actually know what they have.{expertise_note}"""

        # Don't know their situation yet
        elif not logic.mentioned_goal:
            context_note = ""
            if "kids" in full_context or "children" in full_context or "mortgage" in full_context:
                context_note = "\n\n💡 They mentioned kids/mortgage - SHOW you understand their situation before asking:\n\"Two kids and a mortgage - that's exactly who this is for.\"\nThen: \"Would your family need to take out a loan if something happened?\""

            directive = f"""OPEN-ENDED QUESTION

Let them tell you the problem. Don't answer for them.

Simple questions:
- "Would your family need to take out a loan or go into debt if something happened?"
- "What would happen to your family financially if something happened tomorrow?"
- "Who's this for - your family, your business, or both?"

ENGAGE first (when context allows):
If they mentioned specifics (kids, mortgage, business), acknowledge it:
"Two kids and a mortgage? Makes sense you're looking into this."
Then ask the open-ended question.

Open-ended. Let THEM realize the problem.{context_note}"""

        # They told you the problem - confirm they don't want that
        elif logic.mentioned_goal and not logic.mentioned_obstacle:
            directive = """UNDERSTANDING CHECK (NOT ATTACKING)

They told you what would happen. Now make them think about it - but with understanding, not judgment.

FINESSED approach (understanding it's normal to not know, but we should know):
Them: "My wife would probably need to take out a loan"
You: "Yeah, most people don't think about it until it's too late. That something you'd want her dealing with?"

Them: "My kids would be stuck with the mortgage"
You: "Makes sense why you're looking into this then. That's a lot to leave on them, right?"

Them: "I don't know what would happen"
You: "That's pretty common actually. Most people don't. Worth figuring out though, don't you think?"

DON'T say:
❌ "Are you okay with that?" (too blunt/attacking)
❌ "You okay not knowing?" (sounds judgmental)

DO say:
✓ "That's a lot to leave on them, right?"
✓ "That something you'd want them dealing with?"
✓ "Worth figuring out though, don't you think?"

Tone: Understanding it's normal, but also understanding they should know.
Let them realize it themselves. Then offer help."""

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
