# sales_director.py
# The decider: reads memory, profile, narrative → tells the texting agent the current situation
# Updated Feb 2026: Message Context Awareness + Objection Handling Framework

import re
import logging
from typing import Dict, Any, List

from conversation_engine import (
    analyze_logic_flow, LogicSignal, ConversationStage,
    MessageContext, ObjectionType, ObjectionNature,
    BuyingSignalType, InsuranceContext, ProductType
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
    address: str | None = None,
    bot_settings: dict = None,
    lead_type: str = "default",
) -> Dict[str, Any]:
    """
    Returns lean context + tactical situation for the texting agent.
    Understands three message contexts (cold outbound, follow-up, inbound reply)
    and classifies objections for appropriate handling guidance.

    lead_type controls how the bot approaches the lead:
      "fresh"     — speed-to-lead, they just requested info
      "re-engage" — cold/dormant 30+ day lead, circling back
      "aged"      — purchased aged lead, 30+ days minimum
      "default"   — legacy behavior (aged internet lead assumption)
    """
    logger.info(f"Director | {contact_id} | msg='{message[:60]}'")

    # ─── 0. PARSE AGE TO INT (used throughout the directive) ───
    age_int = 0
    if age and str(age) != "unknown":
        try:
            age_int = int(re.search(r'\d+', str(age)).group())
        except (AttributeError, ValueError):
            age_int = 0

    # ─── 1. INTELLIGENCE GATHERING (Single DB Fetch) ───
    all_msgs: List[Dict] = get_recent_messages(contact_id, limit=None)
    recent_exchanges = all_msgs[-14:] if all_msgs else []

    # ─── 2. REFRESH NARRATIVE & FACTS ───
    observer = run_narrative_observer(contact_id, message, all_msgs)
    narrative = observer["narrative"] or ""
    known_facts = get_known_facts(contact_id)

    # ─── 3. ANALYZE LOGIC FLOW (now includes message context + objection detection) ───
    logic: LogicSignal = analyze_logic_flow(recent_exchanges, message=message, age=age_int)

    logger.info(
        f"Director signals | {contact_id} | "
        f"context={logic.message_context.value} | stage={logic.stage.value} | "
        f"objection={logic.objection_type.value}/{logic.objection_nature.value} | "
        f"buying_signal={logic.buying_signal.value} | "
        f"impact={logic.articulated_impact} | "
        f"consecutive_bot={logic.consecutive_bot_messages} | lead_count={logic.conversation_count}"
    )
    if logic.insurance_context and logic.insurance_context.guidance_note:
        logger.info(f"Director insurance_ctx | {contact_id} | {logic.insurance_context.guidance_note[:200]}")

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
            "works for me", "lets do", "yeah lets",
            "set up", "set something up"
        ]):
            stage_value = ConversationStage.BOOKING.value

    # B. Fact-Based Override (If backend knows they booked, lock it)
    if any(kw in f.lower() for f in known_facts for kw in ["booked", "appointment at", "calendar"]):
        stage_value = ConversationStage.BOOKED.value

    # ─── 7. GENERATE TACTICAL DIRECTIVE ───
    tactical = _build_tactical_guidance(logic, stage_value, first_name, full_lower, bot_settings or {}, lead_type=lead_type)

    # ─── 7b. INJECT AGE-BASED PRODUCT DIRECTIVE ───
    # Hardcoded — not a soft hint. This overrides any generic product assumptions.
    age_directive = _build_age_directive(age_int)
    if age_directive:
        tactical = tactical + "\n" + age_directive

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
# AGE-BASED PRODUCT DIRECTIVE
# ═══════════════════════════════════════════════════

def _build_age_directive(age_int: int) -> str:
    """
    Returns a MANDATORY product focus directive based on age.
    This is hardcoded logic — not a suggestion. The LLM must follow it.
    Appended to every tactical narrative when age is known.
    """
    if age_int <= 0:
        return ""

    if age_int <= 54:
        return (
            "\n=== AGE-BASED PRODUCT RULE (MANDATORY) ===\n"
            f"This lead is {age_int} years old — WORKING-AGE bracket.\n"
            "Products to focus on: Term Life, IUL, Whole Life.\n"
            "NEVER bring up final expense, burial insurance, or end-of-life coverage. "
            "That framing is tone-deaf at this age and will kill the conversation.\n"
            "Frame everything around: income replacement, protecting a spouse/kids, "
            "mortgage payoff, living benefits (critical/chronic illness riders), "
            "and the cost of waiting (rates increase with every birthday).\n"
            "Employer coverage gaps are fair game — this person is likely still working."
        )
    elif age_int <= 64:
        return (
            "\n=== AGE-BASED PRODUCT RULE (MANDATORY) ===\n"
            f"This lead is {age_int} years old — TRANSITION bracket (55-64).\n"
            "Products to focus on: Final Expense, Whole Life, short-term Term (10-15yr).\n"
            "IUL is only appropriate if they mention being in excellent health — "
            "the cash value runway is too short otherwise.\n"
            "This person may be semi-retired. Do NOT assume full-time employment. "
            "Do not lead with employer/group coverage — they may no longer have it.\n"
            "Frame coverage around: protecting a spouse, covering end-of-life costs, "
            "leaving something for family, and not being a financial burden."
        )
    elif age_int <= 75:
        return (
            "\n=== AGE-BASED PRODUCT RULE (MANDATORY) ===\n"
            f"This lead is {age_int} years old — SENIOR bracket (65-75).\n"
            "Products to focus on: Final Expense, Whole Life.\n"
            "NEVER suggest term insurance at this age — premiums are prohibitive and "
            "most carriers have age cutoffs. Do not mention IUL — the math does not work.\n"
            "CRITICAL: This person is RETIRED. Never mention work policies, employer plans, "
            "group coverage, or anything job-related. That is irrelevant to their life.\n"
            "Frame everything around: protecting a spouse, covering funeral/burial costs, "
            "leaving something for children/grandchildren, and not burdening family financially."
        )
    else:
        return (
            "\n=== AGE-BASED PRODUCT RULE (MANDATORY) ===\n"
            f"This lead is {age_int} years old — LATE SENIOR bracket (76+).\n"
            "Realistic products: Guaranteed Issue Life, Final Expense (select carriers only).\n"
            "NEVER suggest term, IUL, or standard whole life — these are not options at this age.\n"
            "CRITICAL: This person is RETIRED. Never mention work, employer, or group coverage.\n"
            "Be realistic and compassionate. The core emotional driver is leaving something "
            "behind and not burdening family with funeral or final expenses."
        )


# ═══════════════════════════════════════════════════
# TACTICAL GUIDANCE BUILDER
# ═══════════════════════════════════════════════════

def _build_tactical_guidance(logic: LogicSignal, stage_value: str, first_name: str, full_lower: str, bot_settings: dict = None, lead_type: str = "default") -> str:
    """
    Build the tactical narrative that tells the LLM what to do right now.
    Uses message context, stage, objection signals, and lead type.
    """

    # ═══════════════════════════════════════════════════
    # CONTEXT 1: COLD OUTBOUND — First ever contact
    # ═══════════════════════════════════════════════════
    if logic.message_context == MessageContext.COLD_OUTBOUND:
        return _build_cold_outbound_guidance(first_name, lead_type)

    # ═══════════════════════════════════════════════════
    # CONTEXT 2: FOLLOW-UP — No reply from lead
    # ═══════════════════════════════════════════════════
    if logic.message_context == MessageContext.FOLLOW_UP_NO_REPLY:
        return _build_followup_guidance(logic, bot_settings or {})

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
        booking_base = (
            "READY TO BOOK.\n"
            "They are showing real interest, enough to justify a live call.\n"
            "Offer 2 to 3 specific times from the available calendar slots.\n"
            "Keep it short, confident, and normal. Like a busy person setting up a quick chat.\n"
            "Do not over-explain. Just offer times and ask which works.\n\n"
        )

        # Buying signal context — tell the bot WHY we're booking
        if logic.buying_signal == BuyingSignalType.ASKING_PRICE:
            booking_base += (
                "TRIGGER: They asked about pricing. You CANNOT quote prices over text. "
                "That is a hard rule. Acknowledge their question, let them know a quick call "
                "is where they get real numbers specific to their situation, and offer times.\n"
            )
        elif logic.buying_signal == BuyingSignalType.REQUESTING_COVERAGE:
            booking_base += (
                "TRIGGER: They requested a specific coverage amount. That is a strong buying signal. "
                "Acknowledge what they want, and transition to the call where an advisor can "
                "find the best option for that exact need.\n"
            )
        elif logic.too_deep_for_text:
            booking_base += (
                "TRIGGER: The conversation is getting into technical details (underwriting, "
                "carrier specifics, product comparisons) that cannot be properly handled over text. "
                "Acknowledge their great questions, and firmly but warmly redirect to a call "
                "where a licensed advisor can walk through everything properly.\n"
            )

        # Insurance context injection (product mismatch, amount issues)
        ins = logic.insurance_context
        if ins and ins.guidance_note and "DEPTH GUARD" not in ins.guidance_note:
            booking_base += f"\nINSURANCE CONTEXT: {ins.guidance_note}\n"

        return booking_base

    # --- OBJECTION HANDLING ---
    if stage_value == ConversationStage.OBJECTION_HANDLING.value:
        return _build_objection_guidance(logic, bot_settings or {})

    # --- QUALIFYING / DISCOVERY ---
    return _build_qualifying_guidance(logic, first_name, full_lower)


# ═══════════════════════════════════════════════════
# COLD OUTBOUND — Lead type aware
# ═══════════════════════════════════════════════════

def _build_cold_outbound_guidance(first_name: str, lead_type: str = "default") -> str:
    """
    Build cold outbound guidance based on lead type.
    The lead_type is determined from GHL tags set by the agent's workflow.
    """
    name_line = f"Use their name '{first_name}' if known. " if first_name else ""

    common_rules = (
        "CRITICAL: Every single lead must get a UNIQUE opening message. "
        "Never use the same phrasing twice. Vary your structure, word choice, and angle "
        "every single time. Mix up how you reference the topic. Mix up your question. "
        "No two leads should ever receive the same cold outreach.\n\n"
        f"{name_line}Reference life insurance naturally. "
        "One question only. Keep it brief. Sound like a human, not a bot.\n\n"
        "Goal: get them to reply. That is it. Nothing else matters on the first message."
    )

    if lead_type == "fresh":
        return (
            "SITUATION: SPEED-TO-LEAD. This is a FRESH lead who JUST requested information.\n"
            "They actively filled out a form or requested a quote recently. They are expecting "
            "to hear from someone. This is not a surprise text from a stranger.\n\n"
            "Your timing advantage is everything. They are still thinking about this RIGHT NOW. "
            "Be the first person to actually have a real conversation with them, not a generic "
            "auto-reply or a sales pitch.\n\n"
            "Your opening should acknowledge that they just looked into this. Be warm, direct, "
            "and helpful. You are the person who can actually answer their questions and help "
            "them figure out the right fit. Do not be overly casual or vague. They know why "
            "you are texting. Get to the point while still being human.\n\n"
            "Do NOT say 'I saw you were looking at...' or 'I noticed you filled out a form.' "
            "That is creepy. Instead, frame it as you being available to help with whatever "
            "they were looking into. Keep it natural.\n\n"
            + common_rules
        )

    if lead_type == "re-engage":
        return (
            "SITUATION: RE-ENGAGEMENT. This is a DORMANT lead you are circling back to.\n"
            "They showed interest at some point in the past (30+ days ago, possibly months). "
            "Life got in the way, they got busy, or they just never followed through. "
            "They may or may not remember the original interaction.\n\n"
            "Your approach is a genuine check-in. You are not pretending this is the first time. "
            "Acknowledge that time has passed without making it awkward. Frame it as 'circling "
            "back' or 'checking in' because the topic came to mind, not because you are "
            "running through a call list.\n\n"
            "Do NOT rehash what happened before. Do NOT say 'we spoke X months ago.' Just "
            "be natural about it. Lead with something relevant, maybe a change in the market, "
            "a new option that was not available before, or just genuine curiosity about whether "
            "their situation has changed. Give them a reason to re-engage that feels current, "
            "not like you are dusting off an old file.\n\n"
            + common_rules
        )

    if lead_type == "aged":
        return (
            "SITUATION: AGED LEAD. This person entered their information looking for life "
            "insurance quotes at some point, but it was at least 30 days ago, possibly months. "
            "You purchased that lead. They may not remember filling out any form.\n\n"
            "Your opening needs to be a natural, casual status check. You are checking in "
            "to see if they ever got that handled. You are not pitching. You are not selling. "
            "You are a real person sending a quick text to see where things stand.\n\n"
            "They will likely not recognize your name or number. That is expected. Do not "
            "over-explain who you are or how you got their info. Just be direct and human "
            "about it. If they ask how you got their number, be honest that they filled out "
            "a form online at some point.\n\n"
            + common_rules
        )

    # Default — legacy behavior (backward compatible)
    return (
        "SITUATION: COLD OUTBOUND. This is your first ever message to this person.\n"
        "They have never heard from you before. There is zero conversation history.\n\n"
        "This person went online at some point and entered their information looking for "
        "life insurance quotes. Could have been last week, could have been months ago. "
        "You purchased that lead.\n\n"
        "Your opening needs to be a natural, casual status check. You are checking in "
        "to see if they ever got that handled. You are not pitching. You are not selling. "
        "You are a real person sending a quick text to see where things stand.\n\n"
        + common_rules
    )


# ═══════════════════════════════════════════════════
# FOLLOW-UP GUIDANCE (No reply scenarios)
# ═══════════════════════════════════════════════════

def _build_followup_guidance(logic: LogicSignal, bot_settings: dict = None) -> str:
    """
    Follow-up guidance for unanswered messages.
    Always creative, always different.
    At 5+ unanswered: alternate between humor and insurance value.
    Humor can be disabled via bot_settings.humor_enabled.
    """
    n = logic.consecutive_bot_messages
    settings = bot_settings or {}
    humor_enabled = settings.get("humor_enabled", True)

    base = (
        "SITUATION: FOLLOW-UP. The lead has NOT responded.\n"
        "There is no inbound message. This is an outbound follow-up attempt.\n"
        "Do NOT treat this as if they said something. They did not.\n"
        "Do NOT repeat your previous message or opening.\n\n"
    )

    if n >= 5:
        # Alternate: odd = humor, even = insurance value
        is_humor_turn = (n % 2 == 1) and humor_enabled

        if is_humor_turn:
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
        else:
            return base + (
                f"You have sent {n} messages with zero response.\n"
                "Share something genuinely useful — a quick tip, a surprising fact about "
                "coverage, a real-world scenario where insurance saved someone, or a timely "
                "reminder (open enrollment, rate changes, seasonal risks).\n"
                "Keep it short, one to two sentences. Make it feel like helpful info, "
                "not a sales pitch.\n"
                "Be warm and conversational, not pushy.\n"
                "Goal: re-engage them with something valuable."
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

def _build_objection_guidance(logic: LogicSignal, bot_settings: dict = None) -> str:
    """
    Build tactical guidance for handling the detected objection.
    Follows the framework: acknowledge, reframe, question.
    Differentiates between fear-based and logistical objections.
    Respects objection_persistence setting for graceful exits.
    """
    obj = logic.objection_type
    nature = logic.objection_nature
    settings = bot_settings or {}
    persistence = settings.get("objection_persistence", 3)

    # Header with the core principle
    persistence_note = ""
    if persistence <= 2:
        persistence_note = (
            f"\nPERSISTENCE: Low ({persistence}). If this is the {persistence}th+ time they have "
            "objected, gracefully exit. Say something warm like 'Totally understand, no pressure at all. "
            "If anything changes, feel free to reach back out.' Do NOT keep pushing.\n"
        )
    elif persistence >= 4:
        persistence_note = (
            f"\nPERSISTENCE: High ({persistence}). Keep finding new angles even if they have objected "
            "multiple times. Be creative. Each attempt must come from a genuinely different direction. "
            "Only exit gracefully after {persistence}+ distinct attempts.\n"
        )

    header = (
        "OBJECTION DETECTED. Do not argue. Do not pitch. Do not get defensive.\n\n"
        "Core approach: Acknowledge what they said genuinely. Then shift from 'you vs them' "
        "to 'both of you looking at their situation together.' Then ask a question that "
        "moves the conversation forward. Let them arrive at their own conclusion.\n"
        f"{persistence_note}\n"
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
            "They said some version of no. This almost always masks something deeper. "
            "They are in a conversation about life insurance right now, which means something "
            "put this topic on their radar. People do not end up here by accident. "
            "Something prompted the original interest.\n\n"

            "YOU MUST PICK A FRESH ANGLE EVERY TIME. Read the conversation history carefully. "
            "Do NOT repeat an angle you already used. Say it in YOUR words every time. "
            "Never use canned phrases. Never sound scripted. Every response must feel like "
            "the first time you have ever said it.\n\n"

            "=== ANGLE PLAYBOOK (pick ONE you have NOT used yet) ===\n\n"

            "ANGLE 1 — ORIGINAL TRIGGER:\n"
            "Something put life insurance on their mind at some point. Get curious about whether "
            "that thing ever got resolved, or if life just got in the way. "
            "The psychology: people act on triggers. The trigger does not vanish just because they "
            "are saying no right now.\n\n"

            "ANGLE 2 — NORMALIZE THE REACTION:\n"
            "Agree with their resistance. Make it clear that most people react the same way. "
            "Remove all pressure. Then plant one unexpected thought that creates curiosity. "
            "The psychology: when you stop pushing, people stop defending. Curiosity fills the gap.\n\n"

            "ANGLE 3 — LIFE EVENT PROBE:\n"
            "Skip insurance entirely. Ask about what is going on in their life. Any recent changes — "
            "job, family, home. People who went through a life event have a raw reason to think "
            "about protection. If they mention one, the conversation opens naturally without you "
            "ever having to sell.\n\n"

            "ANGLE 4 — FAMILY DEPENDENTS:\n"
            "Shift the focus from them to the people who rely on them. Ask who handles things "
            "financially if they are not around tomorrow. Not as fear-mongering — as a real, "
            "direct question that most people avoid but privately think about. "
            "The psychology: people make decisions for their kids that they will not make for themselves.\n\n"

            "ANGLE 5 — COST OF DELAY:\n"
            "Rates increase with every birthday. Health changes are unpredictable. Share one "
            "real fact about how age and health impact pricing. Frame it as useful information, "
            "not a scare tactic. The psychology: loss aversion. Losing a lower rate hurts more "
            "than gaining coverage feels good.\n\n"

            "ANGLE 6 — BREAK A MISCONCEPTION:\n"
            "Challenge what they think they know. Most people wildly overestimate the cost, "
            "think the process is complicated, or believe they are too young to need it. "
            "Correct ONE specific misconception with a real fact. "
            "The psychology: new information forces re-evaluation of a closed decision.\n\n"

            "ANGLE 7 — SOCIAL PROOF:\n"
            "Reference people in similar situations who felt the same way and ended up glad "
            "they followed through. Make it casual and conversational, not a testimonial. "
            "The psychology: people trust peer behavior more than expert advice. "
            "If someone like them did it and was happy, the mental barrier drops.\n\n"

            "ANGLE 8 — DIAGNOSE THE REAL OBJECTION:\n"
            "Help them figure out what their actual concern is. Is it that they do not think "
            "they need it, or is it timing, money, overwhelm, or something else? "
            "Let them tell you. Whatever they say next is the real objection — "
            "and now you know exactly how to address it.\n\n"

            "ANGLE 9 — EXISTING COVERAGE PROBE:\n"
            "Ask whether they have anything in place right now, even through work. "
            "If yes, that opens a conversation about gaps. If no, they just told you "
            "their family has zero protection. Either answer moves things forward "
            "without you having to push.\n\n"

            "ANGLE 10 — LOWER THE COMMITMENT:\n"
            "Shrink the ask. Instead of a call or a meeting, offer a two-minute overview. "
            "Just information, no obligation. People who will not commit to a big thing "
            "will often commit to a small thing, and once the conversation starts, "
            "it usually continues on its own.\n\n"

            "ANGLE 11 — COMPLIMENT THEIR INITIATIVE:\n"
            "Flip the dynamic. Acknowledge that most people never even look into this, "
            "so the fact that they did at some point shows they are thinking ahead. "
            "Then get curious about what stopped them from acting on it. "
            "The psychology: people live up to the identity you assign them.\n\n"

            "ANGLE 12 — RAW HONESTY:\n"
            "Drop the smooth approach entirely. Be blunt and human. Nobody is excited about "
            "life insurance — but ask the direct question about whether their family would be "
            "financially okay without them. Some people respond to directness more than finesse. "
            "The psychology: pattern interruption. They expect a pitch. Honesty disarms.\n\n"

            "ANGLE 13 — EMPLOYER COVERAGE GAP:\n"
            "Most people think work coverage is enough. It almost never is. Employer plans "
            "typically cover 1-2x salary — that is less than two years of bills. And it ends "
            "the day they leave the job. Help them see the math without lecturing them.\n\n"

            "ANGLE 14 — DEBT AND OBLIGATIONS:\n"
            "Connect insurance to something concrete — mortgage, car payments, student loans, "
            "kids future education costs. Debt does not die with a person. Someone inherits "
            "that burden. Make the abstract real by tying it to something specific in their life.\n\n"

            "ANGLE 15 — TIMELY RELEVANCE:\n"
            "Tie the conversation to something current — time of year, recent events, "
            "something in the news, a seasonal pattern. Make it feel like there is a reason "
            "you are bringing this up right now, not randomly.\n\n"

            "=== RULES ===\n"
            "- Pick ONE angle per message. Never combine angles.\n"
            "- Read conversation history. NEVER repeat an angle you already tried.\n"
            "- Keep it 1-3 sentences max.\n"
            "- Say it in your own words. Never use canned phrases or scripts.\n"
            "- Never ask 'why not' — that is combative.\n"
            "- Leave space for them to respond. Do not monologue.\n"
            "- Be warm and curious, never desperate or pushy.\n"
        )

    if obj == ObjectionType.SPOUSE_PARTNER:
        return header + (
            "OBJECTION: Need to talk to spouse, partner, family member, or third party.\n"
            "This could be a spouse, partner, daughter, son, parent, accountant, lawyer, "
            "financial advisor, or anyone they defer decisions to. Respect it completely. "
            "Never minimize who they want to consult. Never imply they do not need permission.\n\n"

            "YOU MUST PICK A FRESH ANGLE EVERY TIME. Read the conversation history carefully. "
            "Do NOT repeat an angle you already used. Say it in YOUR words every time. "
            "Never use canned phrases. Never sound scripted.\n\n"

            "=== ANGLE PLAYBOOK (pick ONE you have NOT used yet) ===\n\n"

            "ANGLE 1 — INFORMATION GATHERING REFRAME:\n"
            "Separate information gathering from decision making. A quick call is not committing to "
            "anything — it is getting the actual details, numbers, and options so they have "
            "something concrete to bring to that conversation instead of going in with assumptions. "
            "The psychology: the next step feels smaller when it is about learning, not deciding.\n\n"

            "ANGLE 2 — ARM THEM FOR THE CONVERSATION:\n"
            "Position yourself as helping them prepare for that discussion. When they sit down with "
            "their spouse, accountant, or whoever, they will want real numbers and specifics. "
            "Help them gather what they need so the conversation is productive and informed, "
            "not vague and hypothetical.\n\n"

            "ANGLE 3 — INCLUDE THE OTHER PERSON:\n"
            "Offer to include the other person directly. If it is a spouse, they can both join a call. "
            "If it is a financial advisor or accountant, offer to provide them with the details directly. "
            "This shows you have nothing to hide and removes the game of telephone.\n\n"

            "ANGLE 4 — VALIDATE AND ANCHOR A FOLLOW-UP:\n"
            "Fully agree that this is a decision worth discussing. Then anchor to a specific time to "
            "reconnect after they have had the conversation. Do not leave it open-ended. "
            "The psychology: vague follow-ups die. Specific dates survive.\n\n"

            "ANGLE 5 — EXPLORE THE REAL CONCERN:\n"
            "Sometimes consulting someone else is the real objection. Sometimes it masks "
            "their own uncertainty. Get curious about what specifically they want to discuss with "
            "the other person. If they cannot articulate it, the real concern is internal "
            "and you can help them work through it right now.\n\n"

            "ANGLE 6 — THE JOINT DECISION ANGLE:\n"
            "If it is a spouse or partner, acknowledge that this is inherently a decision that "
            "affects both of them. Ask about what the other person values — financial security, "
            "keeping the mortgage paid, the kids' education. Understanding the other person's "
            "priorities lets you frame the conversation in terms that resonate with both.\n\n"

            "ANGLE 7 — WHAT THEY ALREADY KNOW:\n"
            "Ask what they have figured out so far on their own. What do they already know they want? "
            "What questions do they still have? Getting them to articulate their own position "
            "often reveals they are further along than they think and may not need external "
            "validation as much as they assumed.\n\n"

            "ANGLE 8 — TIME SENSITIVITY:\n"
            "Without being pushy, surface that rates and insurability are time-sensitive. "
            "Health changes, birthdays, and life events can all affect what is available to them. "
            "Getting the information now does not lock them in, but it gives them a snapshot "
            "at their current age and health while they take time to decide.\n\n"

            "=== RULES ===\n"
            "- Pick ONE angle per message. Never combine angles.\n"
            "- Read conversation history. NEVER repeat an angle you already tried.\n"
            "- Keep it 1-3 sentences max.\n"
            "- Say it in your own words. Never use canned phrases or scripts.\n"
            "- NEVER minimize who they want to consult. Full respect always.\n"
            "- Be warm and curious, never desperate or pushy.\n"
        )

    if obj == ObjectionType.PRICE_MONEY:
        return header + (
            "OBJECTION: Price / Money / Too expensive / Cannot afford it.\n"
            "There are two kinds of money objections: value (they do not see the worth) and "
            "cash flow (they genuinely cannot fit another bill). Both are valid. Never dismiss either.\n\n"

            "YOU MUST PICK A FRESH ANGLE EVERY TIME. Read the conversation history carefully. "
            "Do NOT repeat an angle you already used. Say it in YOUR words every time. "
            "Never use canned phrases. Never sound scripted.\n\n"

            "IMPORTANT: You CANNOT quote specific prices over text. That is a hard rule. "
            "Every angle should move toward a call where they can get real numbers.\n\n"

            "=== ANGLE PLAYBOOK (pick ONE you have NOT used yet) ===\n\n"

            "ANGLE 1 — COST OF NOT HAVING IT:\n"
            "Reframe from the cost of the policy to the cost of not having one. "
            "What does their family's financial situation look like without their income? "
            "The monthly premium is a fraction of what their family would lose. "
            "The psychology: loss aversion. The pain of losing is stronger than the pain of paying.\n\n"

            "ANGLE 2 — CHEAPER THAN THEY THINK:\n"
            "Most people wildly overestimate the cost of life insurance. The average person thinks it "
            "costs 3-5x more than it actually does. Without quoting a specific number, convey that "
            "they might be pleasantly surprised. A quick call gives them real numbers for their "
            "specific situation — no commitment, just information.\n\n"

            "ANGLE 3 — FLEXIBLE OPTIONS:\n"
            "Coverage is not one-size-fits-all. There are different amounts, term lengths, and "
            "structures that fit different budgets. Something is better than nothing. "
            "An advisor on a call can find what actually fits their financial reality, "
            "not a generic plan that does not match their situation.\n\n"

            "ANGLE 4 — THE DAILY COST BREAKDOWN:\n"
            "People think in monthly bills but respond to daily costs. When something costs less per "
            "day than a cup of coffee, it shifts perception. Do not make up a number, but convey "
            "that the daily cost of coverage for someone their age is often shockingly low. "
            "A call gives them the exact math.\n\n"

            "ANGLE 5 — WHAT THEY ARE ALREADY SPENDING:\n"
            "People spend money on subscriptions, dining out, and things they do not think about. "
            "Life insurance is often less than entertainment expenses they would not miss. "
            "This is not about guilt-tripping their spending — it is about showing where this "
            "fits in the hierarchy of things they pay for without thinking.\n\n"

            "ANGLE 6 — AGE AND HEALTH WINDOW:\n"
            "Rates are based on current age and health. Every year they wait, it gets more expensive. "
            "A health change can make them uninsurable entirely. Getting a quote now does not commit "
            "them to anything, but it locks in what their options look like today. "
            "The psychology: scarcity. This specific window closes.\n\n"

            "ANGLE 7 — SEPARATE THE QUOTE FROM THE COMMITMENT:\n"
            "Shrink the ask. They do not have to buy anything. They do not have to commit to anything. "
            "Getting a personalized quote is free and takes minutes. Once they have real numbers, "
            "they can decide on their own terms. Remove every ounce of pressure from the next step.\n\n"

            "ANGLE 8 — DEBT AND OBLIGATIONS MATH:\n"
            "Ask about their mortgage, car payments, kids' education costs, or other obligations. "
            "These do not disappear when someone dies. Someone inherits that burden. "
            "Connecting the cost of insurance to the cost of their actual obligations makes "
            "the premium feel like protection, not an expense.\n\n"

            "ANGLE 9 — THE VALUE COMPARISON:\n"
            "Compare the value of what they get to other things in their financial life. "
            "Insurance is the only product that guarantees a payout to your family when they need it most. "
            "No investment, savings account, or retirement plan does what life insurance does. "
            "It is not competing with their budget — it is protecting everything else in it.\n\n"

            "ANGLE 10 — START SMALL:\n"
            "If budget is tight, there are starter options that provide a base layer of protection "
            "at a minimal cost. They can always increase coverage later when finances allow. "
            "The worst outcome is having zero protection while waiting for the perfect time. "
            "Something beats nothing every time.\n\n"

            "=== RULES ===\n"
            "- Pick ONE angle per message. Never combine angles.\n"
            "- Read conversation history. NEVER repeat an angle you already tried.\n"
            "- Keep it 1-3 sentences max.\n"
            "- Say it in your own words. Never use canned phrases or scripts.\n"
            "- NEVER quote specific dollar amounts over text.\n"
            "- Never shame their financial situation. Full respect.\n"
            "- Be warm and curious, never desperate or pushy.\n"
        )

    if obj == ObjectionType.ALREADY_COVERED:
        return header + (
            "OBJECTION: Already have life insurance / Already covered.\n"
            "Do not challenge this. Do not start listing features they might be missing. "
            "Do not quiz them on coverage amounts. That is combative.\n\n"

            "YOU MUST PICK A FRESH ANGLE EVERY TIME. Read the conversation history carefully. "
            "Do NOT repeat an angle you already used. Say it in YOUR words every time. "
            "Never use canned phrases. Never sound scripted.\n\n"

            "=== ANGLE PLAYBOOK (pick ONE you have NOT used yet) ===\n\n"

            "ANGLE 1 — SOURCE CURIOSITY:\n"
            "Most people who say they are covered cannot tell you the details. "
            "They know they have something but not where it came from, how much it is, or what it covers. "
            "Get curious about whether it is through work or personal. Let them try to explain it. "
            "Whatever they say reveals the actual situation.\n\n"

            "ANGLE 2 — EMPLOYER COVERAGE REALITY:\n"
            "Employer plans typically cover 1-2x salary with no portability. It disappears the day "
            "they leave the job. Most people do not realize this. Help them understand what their "
            "work plan actually provides without attacking it. "
            "The psychology: people defend what they have until they see the math themselves.\n\n"

            "ANGLE 3 — LIVING BENEFITS GAP:\n"
            "Most employer plans and older policies lack living benefits — the ability to access the "
            "policy while alive if diagnosed with a chronic, terminal, or critical illness. "
            "This is one of the biggest differences between older and modern policies, and most people "
            "have never heard of it. Share the concept as useful information, not a sales pitch.\n\n"

            "ANGLE 4 — COVERAGE AMOUNT REALITY:\n"
            "Financial advisors recommend 10-12x annual income in coverage. Most people have a fraction "
            "of that. Someone making 60k with a 100k policy has less than two years of bill coverage. "
            "Get curious about their coverage amount and let the math speak for itself. Do not lecture.\n\n"

            "ANGLE 5 — POLICY AGE CHECK:\n"
            "If the policy is old, their rate was locked at a younger age, which is great. But their "
            "life has likely changed — new responsibilities, higher income, more debt, more dependents. "
            "A policy from a decade ago may no longer match their current situation. "
            "Position it as a checkup, not a replacement.\n\n"

            "ANGLE 6 — LAYERING:\n"
            "Insurance is not all-or-nothing. Many people keep their existing coverage and add a "
            "separate policy to fill gaps. A work plan handles the base, a personal plan covers "
            "the rest. This removes the threat of replacing anything they already have. "
            "The psychology: zero perceived risk opens the door to a conversation.\n\n"

            "ANGLE 7 — LIFE CHANGES SINCE PURCHASE:\n"
            "Coverage needs change with life events. A policy purchased when single and renting "
            "is a completely different equation when married with kids and a mortgage. "
            "Get curious about what their life looked like when they first got the policy versus now.\n\n"

            "ANGLE 8 — TERM VS WHOLE:\n"
            "Most people do not know if they have term or whole life. If term, it expires — "
            "a 20-year term bought at 30 leaves them uninsured at 50 when their health may prevent "
            "requalifying. If whole life, premiums are higher but it builds cash value. "
            "Asking which type naturally opens the conversation without being pushy.\n\n"

            "ANGLE 9 — BENEFICIARY AUDIT:\n"
            "A surprising number of people have an outdated beneficiary — an ex-spouse, a deceased "
            "parent, or nobody at all. Ask who is currently listed. This is a genuine service "
            "question that has nothing to do with selling a new policy. If it is wrong, they need "
            "to fix it regardless.\n\n"

            "ANGLE 10 — SECOND OPINION:\n"
            "Position yourself as a free comparison. People get second opinions on medical diagnoses, "
            "car repairs, home renovations. Why not on the thing protecting their family? "
            "Frame it as a quick sanity check, not a replacement pitch. Zero obligation, just clarity.\n\n"

            "ANGLE 11 — COMPLIMENT AND PROBE:\n"
            "Acknowledge that having coverage at all puts them ahead of most people. "
            "Compliment the responsibility. Then get curious about whether their plan has been "
            "reviewed recently. Most people set it and forget it. Advisors recommend reviewing "
            "every 2-3 years or after any major life change.\n\n"

            "ANGLE 12 — DEBT SURVIVAL:\n"
            "Debt does not die with a person. Mortgage, car loans, student debt, credit cards — "
            "someone inherits that burden. Most people insure their income but forget about "
            "outstanding obligations. Ask whether their coverage accounts for their total debt load. "
            "This is not fear-mongering — it is practical math.\n\n"

            "=== RULES ===\n"
            "- Pick ONE angle per message. Never combine angles.\n"
            "- Read conversation history. NEVER repeat an angle you already tried.\n"
            "- Keep it 1-3 sentences max.\n"
            "- Say it in your own words. Never use canned phrases or scripts.\n"
            "- Never tell them their coverage is not enough. Let them discover the gap.\n"
            "- Respect what they have. You are adding value, not replacing.\n"
            "- Be warm and curious, never desperate or pushy.\n"
        )

    if obj == ObjectionType.THINK_ABOUT_IT:
        return header + (
            "OBJECTION: Need to think about it / Not sure yet / Big decision.\n"
            "This is one of the most common stall tactics, but it is not always stalling. "
            "Sometimes they genuinely need processing time. Sometimes they are avoiding a decision. "
            "Your job is to figure out which it is without being pushy about it.\n\n"

            "YOU MUST PICK A FRESH ANGLE EVERY TIME. Read the conversation history carefully. "
            "Do NOT repeat an angle you already used. Say it in YOUR words every time. "
            "Never use canned phrases. Never sound scripted.\n\n"

            "=== ANGLE PLAYBOOK (pick ONE you have NOT used yet) ===\n\n"

            "ANGLE 1 — ISOLATE THE UNCERTAINTY:\n"
            "Help them figure out what specifically they need to think about. Is it the cost? "
            "The timing? Whether they need it at all? Whether they trust the process? "
            "When someone cannot name what they are unsure about, the uncertainty is emotional, "
            "not logical. If they CAN name it, you can address that specific concern right now.\n\n"

            "ANGLE 2 — VALIDATE AND SHRINK THE NEXT STEP:\n"
            "Fully agree that it is worth thinking about. Then make the next step feel tiny. "
            "They do not need to decide anything. A quick conversation just gives them the "
            "information they need to make a smart decision on their own timeline. "
            "The psychology: people avoid big commitments but accept small ones.\n\n"

            "ANGLE 3 — WHAT WOULD MAKE IT A YES:\n"
            "Get curious about what would need to be true for them to feel good about moving forward. "
            "This forces them to articulate their actual criteria instead of sitting in vague uncertainty. "
            "Whatever they say becomes the roadmap for the rest of the conversation.\n\n"

            "ANGLE 4 — THE INFORMATION ADVANTAGE:\n"
            "Thinking with real information is better than thinking with assumptions. "
            "Most people try to decide without knowing what their actual options or costs are. "
            "A quick call gives them the specifics so they can think about something concrete "
            "instead of something abstract.\n\n"

            "ANGLE 5 — TIME IS A FACTOR:\n"
            "Without being pushy, surface that insurance pricing is based on current age and health. "
            "Thinking about it is fine, but the numbers they would be thinking about change over time. "
            "Getting a snapshot now gives them something accurate to consider. "
            "The psychology: they can still think — but now they are thinking with real data.\n\n"

            "ANGLE 6 — WHAT HAPPENED LAST TIME:\n"
            "Most people who say they need to think about it have said the same thing before — "
            "to another agent, to themselves, to a website. Ask how long this has been on their mind. "
            "If they have been thinking about it for months or years, the thinking is not the problem. "
            "The problem is they never had someone walk them through it properly.\n\n"

            "ANGLE 7 — THE DECISION ITSELF IS SMALL:\n"
            "Life insurance feels like a massive decision because it deals with heavy topics. "
            "But the actual decision is straightforward — a monthly amount that ensures their "
            "family is protected. Help them separate the emotional weight from the practical simplicity. "
            "The psychology: reframing complexity reduces paralysis.\n\n"

            "ANGLE 8 — ANCHOR A FOLLOW-UP:\n"
            "If they truly want time, respect that completely. But anchor to something specific — "
            "a date, a day of the week, a timeframe. Vague follow-ups disappear. "
            "Specific ones survive. Do not leave it as an open loop. "
            "Confirm when you will check back and keep the door open warmly.\n\n"

            "=== RULES ===\n"
            "- Pick ONE angle per message. Never combine angles.\n"
            "- Read conversation history. NEVER repeat an angle you already tried.\n"
            "- Keep it 1-3 sentences max.\n"
            "- Say it in your own words. Never use canned phrases or scripts.\n"
            "- Respect their need for time. Never make them feel rushed.\n"
            "- Be warm and curious, never desperate or pushy.\n"
        )

    if obj == ObjectionType.BUSY_TIMING:
        return header + (
            "OBJECTION: Busy / Bad timing / Call back later / Not now.\n"
            "Respect the timing completely. Do not push through a busy signal.\n\n"

            "YOU MUST PICK A FRESH ANGLE EVERY TIME. Read the conversation history carefully. "
            "Do NOT repeat an angle you already used. Say it in YOUR words every time. "
            "Never use canned phrases. Never sound scripted.\n\n"

            "=== ANGLE PLAYBOOK (pick ONE you have NOT used yet) ===\n\n"

            "ANGLE 1 — SPECIFIC ALTERNATIVE:\n"
            "Acknowledge they are busy and suggest a specific alternative time. Do not ask "
            "when is better — that puts the work on them and they will never follow through. "
            "Propose a day and window. People accept specific offers more than open invitations.\n\n"

            "ANGLE 2 — THE 60-SECOND VERSION:\n"
            "Shrink the ask to almost nothing. You are not asking for a full consultation. "
            "A quick text exchange or 60-second overview might be all they need right now. "
            "If they are truly busy, a tiny commitment is still a commitment.\n\n"

            "ANGLE 3 — PIN DOWN THE VAGUE:\n"
            "If they say something vague like later, sometime, or eventually, help them narrow it. "
            "This week or next? Morning or afternoon? Making the vague specific is the difference "
            "between a follow-up that happens and one that does not.\n\n"

            "ANGLE 4 — THE ISSUE DOES NOT WAIT:\n"
            "Without guilt-tripping, surface that their schedule being busy does not pause "
            "the need they originally looked into. Life keeps going regardless of their calendar. "
            "Frame it lightly — not as pressure, but as a reason to carve out a small window soon.\n\n"

            "ANGLE 5 — TEXT-BASED PROGRESS:\n"
            "They do not have to get on a call right now. Offer to handle some of the preliminary "
            "stuff over text while they are on the go. Name, basic situation, what they are looking for. "
            "That way when they do have a free moment, the real conversation starts further along.\n\n"

            "ANGLE 6 — RESPECT AND ANCHOR:\n"
            "Fully respect the timing. Confirm that you will check back at a specific time. "
            "Make the follow-up feel like a courtesy, not a chase. "
            "The psychology: when someone feels respected, they are more likely to re-engage.\n\n"

            "=== RULES ===\n"
            "- Pick ONE angle per message. Never combine angles.\n"
            "- Read conversation history. NEVER repeat an angle you already tried.\n"
            "- Keep it 1-3 sentences max.\n"
            "- Say it in your own words. Never use canned phrases or scripts.\n"
            "- Always respect their time. Never guilt-trip about being busy.\n"
            "- Be warm and brief, never desperate or pushy.\n"
        )

    # Generic fallback (should rarely hit)
    return header + (
        "They have raised a concern. Acknowledge it first. "
        "Then ask a question that helps you understand what is really behind it. "
        "Move the conversation forward by getting curious, not by pushing. "
        "Say it in your own words. Keep it 1-3 sentences. Do not sound scripted."
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

    # Insurance context injection — product expertise
    insurance_note = ""
    ins = logic.insurance_context
    if ins and ins.guidance_note and "DEPTH GUARD" not in ins.guidance_note:
        insurance_note = f"\nEXPERT KNOWLEDGE: {ins.guidance_note}\nUse this knowledge when responding but do not dump it all at once. Ask clarifying questions first.\n\n"

    # ── GAP FOUND + IMPACT ARTICULATED = ready to book ──
    if (logic.mentioned_goal or logic.needs_coverage) and logic.articulated_impact:
        return empathy_prefix + insurance_note + (
            "QUALIFYING COMPLETE: They have told you what they need AND why it matters to them.\n"
            "They have expressed the personal weight of this decision. That is your green light.\n"
            "Move to booking a call. You have what you need. The real advisor takes it from here.\n"
            "Transition naturally to offering specific times."
        )

    # ── GAP FOUND + OBSTACLE KNOWN but no impact yet ──
    if logic.mentioned_goal and logic.mentioned_obstacle:
        if not logic.articulated_impact:
            return empathy_prefix + insurance_note + (
                "QUALIFYING: They have shared a goal and an obstacle, but they have not yet expressed "
                "why solving this actually matters to them personally.\n"
                "Before you push toward booking, you need them to feel the weight of the gap. "
                "Ask about the impact. What happens if this does not get handled. What does that "
                "look like for their family, their situation, their peace of mind. "
                "Make them explain to themselves why this is important enough to act on. "
                "Once they articulate that, they are ready for the call and they know it."
            )
        return empathy_prefix + insurance_note + (
            "QUALIFYING COMPLETE: They have the goal, the obstacle, and the why.\n"
            "Move to booking. Offer times."
        )

    # ── EXISTING COVERAGE MENTIONED ──
    if logic.has_coverage:
        return empathy_prefix + insurance_note + (
            "QUALIFYING: They mentioned existing coverage.\n"
            "Get curious about what they have. Do not quiz or poke holes.\n"
            "Use your knowledge of policy types to ask questions that reveal gaps naturally. "
            "If it is group or employer coverage, the gaps are usually obvious once they start describing it. "
            "If they discover a gap, your next move is to ask how important it is to them "
            "that the gap gets addressed. What would it look like if it did not. "
            "Get them to articulate the impact before you suggest a call."
        )

    # ── INTEREST OR GOAL MENTIONED but no impact yet ──
    if logic.needs_coverage or logic.mentioned_goal:
        return empathy_prefix + insurance_note + (
            "QUALIFYING: They have expressed interest or mentioned who they are protecting.\n"
            "Good momentum, but do not rush to booking yet.\n"
            "You know what they want. Now you need to understand how important it is to them. "
            "Ask about the impact of not having this in place. What happens to the people they "
            "mentioned if this does not get handled. What does that situation actually look like. "
            "Let them sit with that for a moment. When they tell you why it matters, "
            "that is when you move to a call. Not before."
        )

    # ── EARLY STAGE ──
    return empathy_prefix + insurance_note + (
        "QUALIFYING: Still early. Checking for real interest.\n"
        "Ask one thoughtful question about their protection needs, family, or current setup.\n"
        "Pay attention to how they reply. Longer answers and questions back means there is real engagement. "
        "Short or evasive means slow down, stay curious, find a different angle.\n"
        "You are the warm-up. The real sale happens on the call with a human advisor."
    )
