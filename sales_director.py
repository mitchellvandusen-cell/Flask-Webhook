# sales_director.py
# The decider: reads memory, profile, narrative → tells the texting agent the current situation
# Updated Feb 2026: Message Context Awareness + Objection Handling Framework

import logging
from typing import Dict, Any, List

from conversation_engine import (
    analyze_logic_flow, LogicSignal, ConversationStage,
    MessageContext, ObjectionType, ObjectionNature
)
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
    Understands three message contexts (cold outbound, follow-up, inbound reply)
    and classifies objections for appropriate handling guidance.
    """
    logger.info(f"Director | {contact_id} | msg='{message[:60]}'")

    # ─── 1. INTELLIGENCE GATHERING (Single DB Fetch) ───
    all_msgs: List[Dict] = get_recent_messages(contact_id, limit=None)
    recent_exchanges = all_msgs[-14:] if all_msgs else []

    # ─── 2. REFRESH NARRATIVE & FACTS ───
    observer = run_narrative_observer(contact_id, message, all_msgs)
    narrative = observer["narrative"] or ""
    known_facts = get_known_facts(contact_id)

    # ─── 3. ANALYZE LOGIC FLOW (now includes message context + objection detection) ───
    logic: LogicSignal = analyze_logic_flow(recent_exchanges, message=message)

    logger.info(
        f"Director signals | {contact_id} | "
        f"context={logic.message_context.value} | stage={logic.stage.value} | "
        f"objection={logic.objection_type.value}/{logic.objection_nature.value} | "
        f"consecutive_bot={logic.consecutive_bot_messages} | lead_count={logic.conversation_count}"
    )

    # ─── 4. BUILD PROFILE ───
    profile_str, _ = build_comprehensive_profile(
        narrative, known_facts, first_name, age, address
    )

    # ─── 5. CONTEXTUAL INTELLIGENCE (Underwriting & Carriers) ───
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
                lines.append("Guaranteed issue = limited coverage, higher cost.")
            if co_data.get("is_bundled"):
                lines.append("Often bundled with auto/home = usually minimal standalone.")
            if co_data.get("is_employer_provider"):
                lines.append("Likely group/employer plan = ends if they leave job.")
            company_ctx = " ".join(lines)

    # ─── 6. DETERMINE FINAL STAGE ───
    stage_value = logic.stage.value

    # A. Quick Intent Overrides — BUT only if there's no active objection
    #    "not interested" contains "interested", "maybe later" contains "maybe"
    #    Don't accidentally override objection handling with loose keyword matches
    if logic.objection_type == ObjectionType.NONE and logic.message_context == MessageContext.INBOUND_REPLY:
        if any(word in full_lower for word in [
            "book", "schedule", "appointment", "hop on", "quick call",
            "time work", "works for", "lets do", "yeah lets",
            "tell me more", "when works", "set up", "set something up"
        ]):
            stage_value = ConversationStage.BOOKING.value

    # B. Fact-Based Override (If backend knows they booked, lock it)
    if any(kw in f.lower() for f in known_facts for kw in ["booked", "appointment at", "calendar"]):
        stage_value = ConversationStage.BOOKED.value

    # ─── 7. GENERATE TACTICAL DIRECTIVE ───
    tactical = _build_tactical_guidance(logic, stage_value, first_name, full_lower)

    # ─── 8. FINAL OUTPUT ───
    return {
        "profile_str": profile_str.strip(),
        "tactical_narrative": tactical.strip(),
        "stage": stage_value,
        "underwriting_context": underwriting_ctx.strip(),
        "company_context": company_ctx.strip(),
        "known_facts": known_facts,
        "story_narrative": narrative.strip(),
        "recent_exchanges": recent_exchanges,
    }


# ═══════════════════════════════════════════════════
# TACTICAL GUIDANCE BUILDER
# ═══════════════════════════════════════════════════

def _build_tactical_guidance(logic: LogicSignal, stage_value: str, first_name: str, full_lower: str) -> str:
    """
    Build the tactical narrative that tells the LLM what to do right now.
    Uses message context, stage, and objection signals.
    """

    # ═══════════════════════════════════════════════════
    # CONTEXT 1: COLD OUTBOUND — First ever contact
    # ═══════════════════════════════════════════════════
    if logic.message_context == MessageContext.COLD_OUTBOUND:
        return (
            "SITUATION: COLD OUTBOUND. This is your first ever message to this person.\n"
            "They have never heard from you before. There is zero conversation history.\n\n"
            "This person went online at some point and entered their information looking for "
            "life insurance quotes. Could have been last week, could have been months ago. "
            "You purchased that lead.\n\n"
            "Your opening needs to be a natural, casual status check. You are checking in "
            "to see if they ever got that handled. You are not pitching. You are not selling. "
            "You are a real person sending a quick text to see where things stand.\n\n"
            "CRITICAL: Every single lead must get a UNIQUE opening message. "
            "Never use the same phrasing twice. Vary your structure, word choice, and angle "
            "every single time. Mix up how you reference the topic. Mix up your question. "
            "No two leads should ever receive the same cold outreach.\n\n"
            f"Use their name '{first_name}' if known. Reference life insurance naturally. "
            "One question only. Keep it brief. Sound like a human, not a bot.\n\n"
            "Goal: get them to reply. That is it. Nothing else matters on the first message."
        )

    # ═══════════════════════════════════════════════════
    # CONTEXT 2: FOLLOW-UP — No reply from lead
    # ═══════════════════════════════════════════════════
    if logic.message_context == MessageContext.FOLLOW_UP_NO_REPLY:
        return _build_followup_guidance(logic)

    # ═══════════════════════════════════════════════════
    # CONTEXT 3: INBOUND REPLY — Lead responded
    # ═══════════════════════════════════════════════════

    # --- BOOKED ---
    if stage_value == ConversationStage.BOOKED.value:
        return (
            "APPOINTMENT BOOKED.\n"
            "Confirm the time naturally. Mention calendar invite coming.\n"
            "Stop selling. End warmly. Conversation is done, hand-off complete."
        )

    # --- BOOKING ---
    if stage_value == ConversationStage.BOOKING.value:
        return (
            "READY TO BOOK.\n"
            "They are showing real interest, enough to justify a live call.\n"
            "Offer 2 to 3 specific times from the available calendar slots.\n"
            "Keep it short, confident, and normal. Like a busy person setting up a quick chat.\n"
            "Do not over-explain. Just offer times and ask which works."
        )

    # --- OBJECTION HANDLING ---
    if stage_value == ConversationStage.OBJECTION_HANDLING.value:
        return _build_objection_guidance(logic)

    # --- QUALIFYING / DISCOVERY ---
    return _build_qualifying_guidance(logic, first_name, full_lower)


# ═══════════════════════════════════════════════════
# FOLLOW-UP GUIDANCE (No reply scenarios)
# ═══════════════════════════════════════════════════

def _build_followup_guidance(logic: LogicSignal) -> str:
    """
    Follow-up guidance for unanswered messages.
    Always creative, always different. Humor mode at 5+.
    """
    n = logic.consecutive_bot_messages

    base = (
        "SITUATION: FOLLOW-UP. The lead has NOT responded.\n"
        "There is no inbound message. This is an outbound follow-up attempt.\n"
        "Do NOT treat this as if they said something. They did not.\n"
        "Do NOT repeat your previous message or opening.\n\n"
    )

    if n >= 5:
        return base + (
            f"You have sent {n} messages with zero response. Time for humor.\n"
            "Send something genuinely funny. A clean joke. A self-aware comment. "
            "Something completely unexpected that has nothing to do with insurance.\n"
            "Make them smile or laugh. That is the entire goal.\n"
            "Keep it to one or two sentences. Be creative. Be original.\n"
            "Do not sell. Do not mention insurance. Do not guilt trip.\n"
            "Just be a real human being who is funny and worth replying to.\n"
            "Goal: get ANY response. Even 'lol' is a win."
        )

    return base + (
        f"This is follow-up number {n + 1}.\n"
        "Be creative. Every follow-up must be completely different from the last.\n"
        "Different angle, different topic, different tone. Never repeat an approach.\n"
        "Read the conversation recap to see what you already said, then do something new.\n"
        "Keep it short, one to two sentences. Make it easy for them to reply.\n"
        "Goal: get them to respond. Any response is a win."
    )


# ═══════════════════════════════════════════════════
# OBJECTION HANDLING GUIDANCE
# ═══════════════════════════════════════════════════

def _build_objection_guidance(logic: LogicSignal) -> str:
    """
    Build tactical guidance for handling the detected objection.
    Follows the framework: acknowledge, reframe, question.
    Differentiates between fear-based and logistical objections.
    """
    obj = logic.objection_type
    nature = logic.objection_nature

    # Header with the core principle
    header = (
        "OBJECTION DETECTED. Do not argue. Do not pitch. Do not get defensive.\n\n"
        "Core approach: Acknowledge what they said genuinely. Then shift from 'you vs them' "
        "to 'both of you looking at their situation together.' Then ask a question that "
        "moves the conversation forward. Let them arrive at their own conclusion.\n\n"
    )

    if nature == ObjectionNature.FEAR_BASED:
        header += (
            "This is a FEAR-BASED objection. They are resisting emotionally, not logically. "
            "Facts and features will not work here. Do not try to convince them with logic. "
            "Instead, use questions that help them examine their own situation honestly. "
            "Be patient. Be curious. Let them talk themselves through it. "
            "If they push back again, cycle back to the same core idea from a slightly "
            "different angle, a bit more direct each time, but never aggressive.\n\n"
        )
    elif nature == ObjectionNature.LOGISTICAL:
        header += (
            "This is a LOGISTICAL objection. They have a practical concern, something about "
            "money, existing arrangements, or timing. These need concrete acknowledgment. "
            "Do not dismiss their practical reality. Validate it, then help them see whether "
            "their current arrangement actually addresses what they need it to. "
            "Often what they think is a logistical barrier is actually a gap they have not examined.\n\n"
        )

    # Specific objection guidance
    if obj == ObjectionType.NOT_INTERESTED:
        return header + (
            "OBJECTION: Not interested / No.\n"
            "They said some version of 'no.' This almost always masks something deeper. "
            "Nobody goes online, enters their personal information looking for life insurance quotes, "
            "and then genuinely has zero interest. Something prompted that original search.\n\n"
            "Acknowledge their response. Do not fight it. Then make their disinterest the "
            "reason you are reaching out. They looked into this before. Something was on their mind. "
            "Did that situation change? Did they get it handled another way? Or did life just get busy?\n\n"
            "Ask one question that reframes around what originally motivated them. "
            "Do not ask 'why not?' That is combative. Instead, get curious about whether "
            "the thing that made them look in the first place ever got resolved.\n\n"
        )

    if obj == ObjectionType.SPOUSE_PARTNER:
        return header + (
            "OBJECTION: Need to talk to spouse/partner.\n"
            "This is usually about not wanting to make a decision alone, not about actually "
            "needing the partner's permission. Respect it completely. Do not minimize it.\n\n"
            "Get on their side. Acknowledge that this is absolutely a decision worth discussing together. "
            "Then reframe the next step as information gathering, not committing. "
            "A quick call is not signing anything. It is getting the actual numbers and details "
            "so they have something real to discuss with their partner, instead of guessing.\n\n"
            "Position it as: would it not be better to bring actual information to that conversation "
            "instead of going in blind? The call gives them what they need to have that discussion properly.\n\n"
            "If they still want to wait, offer to schedule a time after they have had that conversation. "
            "Give them a specific timeframe to reconnect. Do not leave it open-ended.\n\n"
        )

    if obj == ObjectionType.PRICE_MONEY:
        return header + (
            "OBJECTION: Price / Money / Too expensive.\n"
            "First, figure out which kind of money objection this is. There are two:\n\n"
            "1. Value objection (fear-based): They are not sure this is worth the money. "
            "They do not see the ROI or urgency. This is really about not understanding what "
            "happens to their family financially if something happens to them. "
            "The conversation here is about the cost of NOT having coverage versus the cost of having it. "
            "What does their family's financial situation look like if they are gone tomorrow? "
            "That is not a scare tactic. It is the entire point of life insurance.\n\n"
            "2. Cash flow objection (logistical): They literally cannot fit another bill right now. "
            "This is real and you respect it. But also know that there are more flexible options "
            "than most people realize. Coverage amounts, term lengths, and structures vary widely. "
            "A real advisor on a call can find something that actually fits their budget.\n\n"
            "In either case, you cannot quote prices over text. That is a hard rule. "
            "Acknowledge the concern, then position the call as the way to find out what "
            "options actually exist in their price range. No commitment, just real numbers.\n\n"
        )

    if obj == ObjectionType.ALREADY_COVERED:
        return header + (
            "OBJECTION: Already have life insurance / Already covered.\n"
            "Do not challenge this. Do not start listing features they might be missing. "
            "Do not quiz them on coverage amounts. That is combative.\n\n"
            "Instead, get genuinely curious. Most people who say they are covered have no idea "
            "what they actually have, how much it covers, or whether it is enough. "
            "They know they have 'something' but could not tell you the details.\n\n"
            "Acknowledge and respect their position. Then let them try to back it up. "
            "Get curious about what kind of coverage, how long they have had it, "
            "whether it is through work or personal. Let THEM explain it.\n\n"
            "If it is employer/group coverage, know that it usually ends if they leave the job, "
            "and the amount is often far less than what a family actually needs. "
            "You do not say this directly. You ask questions that let them discover it. "
            "most employer coverage doesn't have living benefits. Living benefits are benefits "
            "that allow the policy owner to access thier policy if the insured gets chronically ill, terminally ill, or critically ill.\n\n"
        )

    if obj == ObjectionType.BUSY_TIMING:
        return header + (
            "OBJECTION: Busy / Bad timing / Call back later.\n"
            "Respect the timing completely. Do not push through a busy signal.\n\n"
            "Acknowledge it briefly, then offer a specific alternative time. "
            "Do not say 'when is a better time?' because that puts the work on them and "
            "they will never follow through. Instead, suggest a specific day or window.\n\n"
            "You can also gently surface that the thing they were looking into has not gone away "
            "just because today is hectic. The gap between 'I will handle it later' and "
            "actually handling it is where families end up unprotected. But say this lightly, "
            "not as a guilt trip.\n\n"
            "If they give you a specific callback time, take it and confirm you will reach out then. "
            "If they are vague ('yeah sometime later'), pin it down. "
            "Later this week? Next Monday? Give them something concrete to agree to."
        )

    # Generic fallback (should rarely hit)
    return header + (
        "They have raised a concern. Acknowledge it first. "
        "Then ask a question that helps you understand what is really behind it. "
        "Move the conversation forward by getting curious, not by pushing."
    )


# ═══════════════════════════════════════════════════
# QUALIFYING GUIDANCE
# ═══════════════════════════════════════════════════

def _build_qualifying_guidance(logic: LogicSignal, first_name: str, full_lower: str) -> str:
    """Build tactical guidance for the qualifying/discovery stage."""

    # Brief empathy gate for vulnerable situations
    empathy_prefix = ""
    vulnerable = any(kw in full_lower for kw in [
        "cancer", "sick", "divorce", "lost job", "scared", "worried",
        "single mom", "alone", "diagnosis"
    ])
    if vulnerable:
        empathy_prefix = (
            "They are dealing with something tough. "
            "Acknowledge it briefly and genuinely before anything else. "
            "Then keep moving toward 'a quick call can help sort this out.'\n\n"
        )

    if logic.mentioned_goal and logic.mentioned_obstacle:
        return empathy_prefix + (
            "QUALIFYING: They have shared why they need coverage and what is holding them back.\n"
            "This is enough qualification. The real salesperson can take it from here.\n"
            "If they are responding with decent length replies and asking questions back, "
            "it is time to propose a quick call. Do not go deeper over text. Move to offering times."
        )

    if logic.has_coverage:
        return empathy_prefix + (
            "QUALIFYING: They mentioned existing coverage.\n"
            "Get curious about what they have. Do not quiz or poke holes.\n"
            "If it is group/employer, know that it usually ends when they leave the job and does not have living benefits "
            "and amounts are often far below what a family needs. Let them discover this through your questions.\n"
            "Transition toward a call when you have a sense of their situation."
        )

    if logic.needs_coverage or logic.mentioned_goal:
        return empathy_prefix + (
            "QUALIFYING: They have expressed some interest or mentioned who they are protecting.\n"
            "Good momentum. Ask about what they have now, their family situation, or their goals.\n"
            "One question at a time. Let them lead the conversation.\n"
            "Once you have a basic picture, suggest hopping on a call to look at real options."
        )

    return empathy_prefix + (
        "QUALIFYING: Still early. Checking for real interest.\n"
        "Ask 1 thoughtful question about their protection needs, family, or current setup.\n"
        "Pay attention to how they reply. Longer answers and questions back = green light "
        "to suggest a call soon. Short or evasive = slow down, stay curious.\n"
        "You are the warm-up. The real sale happens on the call with a human advisor."
    )
