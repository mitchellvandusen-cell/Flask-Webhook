# sales_director.py - Conversational Intelligence Engine
# "Talk like a human, think like a strategist"

import logging
from conversation_engine import analyze_logic_flow, LogicSignal, ConversationStage
from individual_profile import build_comprehensive_profile
from underwriting import get_underwriting_context
from insurance_companies import get_company_context, find_company_in_message, normalize_company_name
from memory import get_recent_messages, get_known_facts, get_narrative, run_narrative_observer

logger = logging.getLogger(__name__)

def generate_strategic_directive(contact_id: str, message: str, first_name: str, age: str, address: str) -> dict:
    """
    Generate conversational guidance based on where they are in the conversation.
    Focus: Situation → Goal → Obstacles → Book
    """

    # 1. GATHER INTELLIGENCE
    run_narrative_observer(contact_id, message)

    recent_exchanges = get_recent_messages(contact_id, limit=10)
    story_narrative = get_narrative(contact_id)
    known_facts = get_known_facts(contact_id)

    # 2. ANALYZE CONVERSATION
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

    # 3. BUILD GUIDANCE
    directive = ""
    framework = ""

    # === INITIAL OUTREACH ===
    if logic.stage == ConversationStage.INITIAL_OUTREACH:
        first_name_instruction = f"Use '{first_name}' in your opening." if first_name else "No first name."
        directive = f"""INITIAL OUTREACH - Natural opener

{first_name_instruction}

Approach options (pick what fits):
- Curiosity hook: "Quick question about your life insurance situation..."
- Pattern interrupt: "Noticed something about your coverage..."
- Direct: "Are you still looking at life insurance or did that get handled?"

Rules:
- NO "Hey/Hi/Hello" greetings
- Mention "life insurance" naturally
- One question max
- Brief (1-2 sentences)

Identity frame: Position yourself as someone who helps people not make dumb mistakes with their coverage."""
        framework = "INITIAL OUTREACH"

        return {
            "profile_str": profile_str,
            "tactical_narrative": f"STRATEGY: {framework}\n\n{directive}",
            "stage": logic.stage.value,
            "underwriting_context": underwriting_ctx,
            "company_context": company_ctx,
            "known_facts": known_facts,
            "story_narrative": story_narrative,
            "recent_exchanges": recent_exchanges
        }

    # === BOOKED ===
    elif logic.stage == ConversationStage.BOOKED:
        directive = """APPOINTMENT BOOKED - Confirm and stop

Simple confirmation:
- "Perfect, see you [time]"
- "You're all set for [time]"
- Then STOP talking

DO NOT:
- Continue selling
- Ask more questions
- Give more info unless they ask"""
        framework = "POST-CLOSE"

        return {
            "profile_str": profile_str,
            "tactical_narrative": f"STRATEGY: {framework}\n\n{directive}",
            "stage": logic.stage.value,
            "underwriting_context": underwriting_ctx,
            "company_context": company_ctx,
            "known_facts": known_facts,
            "story_narrative": story_narrative,
            "recent_exchanges": recent_exchanges
        }

    # === BOOKING ===
    elif logic.stage == ConversationStage.BOOKING:
        directive = """BOOKING - Offer specific times

They've shown interest or agreed to a call.
NOW offer specific times.

Simple and direct:
- "I have Tuesday at 2pm or Thursday at 5pm. Which works?"
- "What's better for you - morning or afternoon?"
- "This week or next week?"

Keep it brief. Make it easy to say yes to one.

DON'T:
- Re-sell them
- Add more info
- Ask more discovery questions

Just get a time on the books."""
        framework = "BOOKING"

    # === QUALIFYING ===
    else:
        # Check what we know
        full_context = (story_narrative + " " + " ".join(known_facts)).lower()

        # Determine what to find out
        if logic.has_coverage:
            # They have coverage - seed doubt
            directive = """QUALIFYING - They have existing coverage

Your job: Seed doubt. Make them question what they have.

Smart questions (not interrogative, purposeful):
- "Is there a waiting period on that policy, or does it pay out right away?"
- "Does that policy have living benefits or just death benefit?"
- "Is that through work? Have you thought about what happens if you switch jobs?"
- "How long have you had that? Rates have dropped a lot in the last few years."
- "Term or permanent?"

Don't attack their policy. Just make them think.

Identity frame (SCARF):
- Status: "Most people with your level of responsibility [business/family] have layered coverage..."
- Certainty: "The worst thing is thinking you're covered and finding out later there's a gap..."
- Fairness: "Is it fair to your family if this thing doesn't do what you think it does?"

Goal: Get them to question their current setup."""

        elif not logic.mentioned_goal:
            # Don't know goal yet
            directive = """QUALIFYING - Find out who/what they're protecting

You need to know: Who or what are they trying to protect?

Natural ways to ask (make sense, not weird):
- "What made you start looking at this?"
- "Who are you trying to make sure is taken care of?"
- "What's the main thing you're worried about leaving behind?"

If they mention something specific:
Them: "I have two kids and a mortgage"
You: "Got it. Is the mortgage covered if something happens, or would that fall on your wife?"

DON'T tie appointment to this message. Just ask the question. Let them answer.

MAYBE use identity frame if natural:
- "Sounds like you're someone who thinks ahead. That's refreshing. What made you start looking into this?"

Goal: Understand what they care about protecting. One step at a time."""

        elif not logic.mentioned_obstacle:
            # Don't know why they haven't acted
            directive = """QUALIFYING - Find out what's stopped them

You know they need it. You know what they're protecting. Now find out: Why haven't they done it yet?

Natural ways to ask:
- "What's kept you from getting this handled already?"
- "Is it just been busy or is there something specific holding you back?"
- "Have you looked at options before, or is this the first time?"

Accusation audit (if they seem hesitant):
- "You probably think this is expensive..."
- "You're probably thinking you don't have time for this..."

Rationale question (subtle):
- "Why not just keep what you have and hope for the best?"

DON'T offer appointment times yet. Just find out what's blocking them.

Goal: Understand what's been blocking them. Then NEXT message you can offer help."""

        else:
            # We know enough - ask if they want help
            directive = """QUALIFYING - See if they want help

You know:
- Their situation
- Their goal
- Their obstacles

Now see if they WANT help fixing it. Don't assume.

Natural flow:
- "Want me to help you figure out what makes sense for your situation?"
- "Would you be opposed to a quick call to lock this down?"
- "Should we jump on a call to knock this out?"

DON'T offer specific times yet.
Wait for them to say yes first.
THEN next message you offer times.

Be natural. One step at a time.

Goal: Get them to say yes to a call. Times come AFTER they agree."""

        framework = "QUALIFYING"

    return {
        "profile_str": profile_str,
        "tactical_narrative": f"STRATEGY: {framework}\n\n{directive}",
        "stage": logic.stage.value,
        "underwriting_context": underwriting_ctx,
        "company_context": company_ctx,
        "known_facts": known_facts,
        "story_narrative": story_narrative,
        "recent_exchanges": recent_exchanges
    }
