# sales_director.py
# The decider: reads memory, profile, narrative → tells the texting agent the current situation
# Updated Feb 2026: Message Context Awareness + Objection Handling Framework

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
        return _build_objection_guidance(logic)

    # --- QUALIFYING / DISCOVERY ---
    return _build_qualifying_guidance(logic, first_name, full_lower)


# ═══════════════════════════════════════════════════
# FOLLOW-UP GUIDANCE (No reply scenarios)
# ═══════════════════════════════════════════════════

def _build_followup_guidance(logic: LogicSignal) -> str:
    """
    Follow-up guidance for unanswered messages.
    Always creative, always different.
    At 5+ unanswered: alternate between humor and insurance value.
    Joke → insurance topic → joke → insurance topic (not all jokes).
    """
    n = logic.consecutive_bot_messages

    base = (
        "SITUATION: FOLLOW-UP. The lead has NOT responded.\n"
        "There is no inbound message. This is an outbound follow-up attempt.\n"
        "Do NOT treat this as if they said something. They did not.\n"
        "Do NOT repeat your previous message or opening.\n\n"
    )

    if n >= 5:
        # Alternate: odd = humor, even = insurance value
        is_humor_turn = (n % 2 == 1)

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
                "Your last message was humorous. Now pivot back to insurance value.\n"
                "Share something genuinely useful — a quick tip, a surprising fact about "
                "coverage, a real-world scenario where insurance saved someone, or a timely "
                "reminder (open enrollment, rate changes, seasonal risks).\n"
                "Keep it short, one to two sentences. Make it feel like helpful info, "
                "not a sales pitch.\n"
                "Be warm and conversational, not pushy.\n"
                "Goal: re-engage them with something valuable. Then humor again next time."
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
            "They said some version of no. This almost always masks something deeper. "
            "Nobody goes online, enters their personal information looking for life insurance quotes, "
            "and then genuinely has zero interest. Something prompted that original search.\n\n"

            "YOU MUST PICK A FRESH ANGLE EVERY TIME. Read the conversation history carefully. "
            "Do NOT repeat an angle you already used. Say it in YOUR words every time. "
            "Never use canned phrases. Never sound scripted. Every response must feel like "
            "the first time you have ever said it.\n\n"

            "=== ANGLE PLAYBOOK (pick ONE you have NOT used yet) ===\n\n"

            "ANGLE 1 — ORIGINAL TRIGGER:\n"
            "They searched for life insurance at some point. Something was on their mind. "
            "Get curious about whether that thing ever got resolved, or if life just got in the way. "
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
