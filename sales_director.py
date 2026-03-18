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
from lead_intelligence import get_cached_temperature
from memory import (
    get_recent_messages,
    get_known_facts,
    get_known_facts_with_age,
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
    # ── Enriched CRM contact data ──
    last_name: str | None = None,
    company_name: str | None = None,
    tags: list | None = None,
    notes: list | None = None,
    custom_fields: list | None = None,
    source: str | None = None,
    city: str | None = None,
    state: str | None = None,
    gender: str | None = None,
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
    recent_exchanges = all_msgs[-30:] if all_msgs else []

    # ─── 2. REFRESH NARRATIVE & FACTS ───
    observer = run_narrative_observer(contact_id, message, all_msgs)
    narrative = observer["narrative"] or ""
    known_facts = get_known_facts(contact_id)
    known_facts_temporal = get_known_facts_with_age(contact_id)

    # ─── 3. ANALYZE LOGIC FLOW (now includes message context + objection detection) ───
    logic: LogicSignal = analyze_logic_flow(recent_exchanges, message=message, age=age_int)

    logger.info(
        f"Director signals | {contact_id} | "
        f"context={logic.message_context.value} | stage={logic.stage.value} | "
        f"objection={logic.objection_type.value}/{logic.objection_nature.value} | "
        f"buying_signal={logic.buying_signal.value} | "
        f"impact={logic.articulated_impact} | "
        f"consecutive_bot={logic.consecutive_bot_messages} | rapport_turns={logic.consecutive_rapport_turns} | "
        f"lead_count={logic.conversation_count}"
    )
    if logic.insurance_context and logic.insurance_context.guidance_note:
        logger.info(f"Director insurance_ctx | {contact_id} | {logic.insurance_context.guidance_note[:200]}")

    # ─── 4. BUILD PROFILE ───
    # Pass temporal facts for staleness awareness; profile builder handles both formats
    # Include CRM contact card data so the bot starts with what the agent already knows
    profile_str, _ = build_comprehensive_profile(
        narrative, known_facts_temporal or known_facts, first_name, age, address,
        last_name=last_name,
        company_name=company_name,
        tags=tags,
        notes=notes,
        custom_fields=custom_fields,
        source=source,
        city=city,
        state=state,
        gender=gender,
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

    # ─── 6b. EXTRACT OBJECTION LOG FROM NARRATIVE ───
    # The narrative observer now captures every objection + the angle used.
    # Extract it so we can inject it structurally into tactical guidance.
    objection_log = _extract_objection_log(narrative)

    # ─── 7. GENERATE TACTICAL DIRECTIVE ───
    tactical = _build_tactical_guidance(logic, stage_value, first_name, full_lower, bot_settings or {}, lead_type=lead_type, objection_log=objection_log)

    # ─── 7b. INJECT AGE-BASED PRODUCT DIRECTIVE ───
    # Hardcoded — not a soft hint. This overrides any generic product assumptions.
    age_directive = _build_age_directive(age_int)
    if age_directive:
        tactical = tactical + "\n" + age_directive

    # ─── 7c. INJECT LEAD TEMPERATURE CONTEXT ───
    # Read cached AI temperature from lead_intelligence (zero AI cost, DB read only).
    # This tells the bot whether the lead is warming or cooling so it can adjust intensity.
    temperature_ctx = ""
    try:
        cached = get_cached_temperature(contact_id)
        if cached and cached.get("temperature"):
            temp = cached["temperature"]
            score = cached.get("score", 0)
            eng = cached.get("engagement_level", 0)
            if temp in ("hot", "warm"):
                temperature_ctx = (
                    f"\nLEAD TEMPERATURE: {temp.upper()} (score {score}/100, engagement {eng}/3). "
                    f"This lead is showing real interest. Maintain momentum. "
                    f"Do not slow down the conversation or over-qualify. "
                    f"Move toward booking when the moment is right."
                )
            elif temp == "cool":
                temperature_ctx = (
                    f"\nLEAD TEMPERATURE: COOL (score {score}/100, engagement {eng}/3). "
                    f"Interest is fading. This lead is drifting. "
                    f"Try a different angle or bring fresh energy. "
                    f"Do not repeat what you have already tried."
                )
            elif temp == "cold":
                temperature_ctx = (
                    f"\nLEAD TEMPERATURE: COLD (score {score}/100, engagement {eng}/3). "
                    f"This lead has shown little or no interest. "
                    f"Be respectful of their position. A completely unexpected angle "
                    f"is your best shot. If they have explicitly said stop, respect it."
                )
            if temperature_ctx:
                tactical = tactical + temperature_ctx
    except Exception as e:
        logger.debug(f"Temperature context unavailable for {contact_id}: {e}")

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
# OBJECTION LOG EXTRACTION
# ═══════════════════════════════════════════════════

def _extract_objection_log(narrative: str) -> List[str]:
    """
    Extract the OBJECTION_LOG section from the structured narrative.
    Returns list of "Objection: X > Angle: Y" strings.
    Returns empty list if no objection log found or narrative uses legacy format.
    """
    if not narrative or "OBJECTION_LOG:" not in narrative:
        return []

    try:
        log_section = narrative.split("OBJECTION_LOG:", 1)[1]
        # Stop at the next section header if one exists
        for marker in ["FACTS:", "SITUATION:", "EMOTIONAL_ARC:"]:
            if marker in log_section:
                log_section = log_section.split(marker, 1)[0]
        log_section = log_section.strip()

        if log_section.upper() == "NONE":
            return []

        entries = []
        for line in log_section.split("\n"):
            line = line.strip().lstrip("-•* ")
            if line and len(line) > 5 and line.upper() != "NONE":
                entries.append(line)
        return entries
    except Exception:
        return []


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

def _build_tactical_guidance(logic: LogicSignal, stage_value: str, first_name: str, full_lower: str, bot_settings: dict = None, lead_type: str = "default", objection_log: List[str] = None) -> str:
    """
    Build the tactical narrative that tells the LLM what to do right now.
    Uses message context, stage, objection signals, lead type, and objection history.
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
            "Confirm the time naturally. Ask if they see the invite in their email.\n"
            "Stop selling. End warmly. Conversation is done."
        )

    # --- BOOKING ---
    if stage_value == ConversationStage.BOOKING.value:

        # Lead DIRECTLY asked to book — honor their intent immediately.
        # Don't pitch, don't qualify further, don't "build impact." They told
        # you what they want. Confirm the time and lock it in.
        if logic.ready_to_book:
            booking_base = (
                "LEAD REQUESTED BOOKING DIRECTLY.\n"
                "They explicitly asked to schedule, book, or meet. This is their idea, "
                "not yours. Do NOT continue qualifying or selling. Do NOT ask why they "
                "want coverage or try to build impact. They already decided.\n\n"
                "If they gave a specific day/time: confirm it and lock it in.\n"
                "If they said 'let's book' without a time: offer 2-3 available slots.\n"
                "Keep it short. Match their energy. They are ready — just handle the logistics.\n\n"
            )
        else:
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
        return _build_objection_guidance(logic, bot_settings or {}, objection_log=objection_log or [])

    # --- RAPPORT ---
    if stage_value == ConversationStage.RAPPORT.value:
        return _build_rapport_guidance(logic, first_name)

    # --- QUALIFYING / DISCOVERY ---
    return _build_qualifying_guidance(logic, first_name, full_lower)


# ═══════════════════════════════════════════════════
# COLD OUTBOUND — Lead type aware
# ═══════════════════════════════════════════════════

def _build_cold_outbound_guidance(first_name: str, lead_type: str = "default") -> str:
    """
    Build cold outbound guidance based on lead type.
    Pure situational context — no templates, no example phrases, no scripted openings.
    The LLM decides how to open based on the information provided.
    """
    name_line = f"Their name is '{first_name}'. " if first_name else "You do not have their name. "

    common_context = (
        "Every message you send must be unique. Never repeat structure, wording, or angle "
        "across different leads. One question maximum. Brief.\n\n"
        f"{name_line}"
        "Goal: get a reply. Nothing else matters on the first message."
    )

    if lead_type == "fresh":
        return (
            "SITUATION: FRESH LEAD. They just requested information about life insurance. "
            "They filled out a form or requested a quote very recently. They are expecting "
            "to hear from someone. This is not a cold text from a stranger.\n\n"
            "KEY CONTEXT: They are actively thinking about this right now. Your timing "
            "advantage is everything. They know why you are contacting them. They do not "
            "know it was specifically YOU, but they know someone would reach out.\n\n"
            + common_context
        )

    if lead_type == "re-engage":
        return (
            "SITUATION: DORMANT LEAD. They showed interest at some point in the past, "
            "at least 30 days ago, possibly months. Life got in the way. They may or may "
            "not remember the original interaction.\n\n"
            "KEY CONTEXT: Time has passed. Their situation may have changed. There may be "
            "new options, market changes, or life events that make this relevant again. "
            "They have history with this topic even if they forgot about it.\n\n"
            + common_context
        )

    if lead_type == "aged":
        return (
            "SITUATION: AGED LEAD. This person expressed interest in life insurance at "
            "least 30 days ago, possibly months. That lead was purchased. They may not "
            "remember any form they filled out.\n\n"
            "KEY CONTEXT: They will not recognize your name or number. They may not "
            "remember ever looking into this. You are essentially a stranger to them, "
            "but they did voluntarily provide their information at some point.\n\n"
            + common_context
        )

    if lead_type == "very-old":
        return (
            "SITUATION: VERY OLD LEAD. This person filled out a form over 3 months ago. "
            "They almost certainly do not remember it. They have "
            "likely been contacted by multiple agents already.\n\n"
            "KEY CONTEXT: Anything that sounds like a standard insurance outreach will be "
            "dismissed immediately. They have heard it all. They are numb to it. You need "
            "to come from a completely unexpected angle. Pure value, pure curiosity, or "
            "something they have genuinely never heard before. If it sounds like something "
            "another agent would send, do not send it.\n\n"
            + common_context
        )

    # Default — unknown lead age
    return (
        "SITUATION: COLD OUTBOUND. First message to this person. Zero conversation history.\n\n"
        "KEY CONTEXT: This person expressed interest in life insurance at some point, "
        "could have been recent, could have been months ago. You do not know. "
        "They may or may not remember. You are a stranger to them.\n\n"
        + common_context
    )


# ═══════════════════════════════════════════════════
# FOLLOW-UP GUIDANCE (No reply scenarios)
# ═══════════════════════════════════════════════════

def _build_followup_guidance(logic: LogicSignal, bot_settings: dict = None) -> str:
    """
    Follow-up guidance for unanswered messages.
    Pure context about the situation — LLM decides approach.
    """
    n = logic.consecutive_bot_messages
    settings = bot_settings or {}
    humor_enabled = settings.get("humor_enabled", True)

    base = (
        "SITUATION: FOLLOW-UP. The lead has NOT responded.\n"
        "There is no inbound message. This is an outbound follow-up attempt.\n"
        "Do NOT treat this as if they said something. They did not.\n"
        "Do NOT repeat your previous message or approach.\n\n"
    )

    if n >= 5:
        humor_note = " Humor is available as a tool." if humor_enabled else " Humor is disabled by the agent."
        return base + (
            f"You have sent {n} messages with zero response.{humor_note}\n"
            f"Every previous approach failed. You need something completely different "
            f"from anything you have already tried. Read the conversation history "
            f"and do the opposite of what you have been doing.\n"
            f"IMPORTANT: When they DO finally respond, their response is about your "
            f"MESSAGE to them. Read what you sent and what they said back. Do not "
            f"force an insurance conversation if they are commenting on your texting "
            f"behavior. Acknowledge what they said, be human about it, and let the "
            f"conversation develop naturally from there.\n"
            f"Goal: get ANY response."
        )

    return base + (
        f"This is follow-up number {n + 1}.\n"
        "Every follow-up must be completely different from the last. "
        "Read the conversation to see what you already tried.\n"
        "Goal: get them to respond."
    )


# ═══════════════════════════════════════════════════
# OBJECTION HANDLING GUIDANCE
# ═══════════════════════════════════════════════════

def _build_objection_guidance(logic: LogicSignal, bot_settings: dict = None, objection_log: List[str] = None) -> str:
    """
    Build tactical guidance for handling the detected objection.
    Pure situational context — no angle playbooks, no scripted approaches.
    The LLM has the psychology frameworks in its system prompt and the
    conversation history. It can figure out what angle to take.

    objection_log: Structural record of every past objection + angle used,
    extracted from the narrative observer. This is NOT a prompt hint — the LLM
    MUST read this list and choose a different angle every time.
    """
    obj = logic.objection_type
    nature = logic.objection_nature
    settings = bot_settings or {}
    persistence = settings.get("objection_persistence", 3)
    log = objection_log or []

    persistence_note = ""
    if persistence <= 2:
        persistence_note = (
            f"\nPERSISTENCE: Low ({persistence}). If this is objection #{persistence}+, "
            "exit gracefully. Do not keep pushing.\n"
        )
    elif persistence >= 4:
        persistence_note = (
            f"\nPERSISTENCE: High ({persistence}). Keep finding new angles even after "
            f"multiple objections. Only exit after {persistence}+ distinct attempts.\n"
        )

    # Build structural objection history block
    history_block = ""
    if log:
        history_block = (
            "\n=== OBJECTION HISTORY (ANGLES ALREADY TRIED — DO NOT REPEAT) ===\n"
            + "\n".join(f"  {i+1}. {entry}" for i, entry in enumerate(log))
            + f"\n\nThis is attempt #{len(log) + 1}. You MUST use a completely different "
            f"angle from the {len(log)} listed above. If you repeat any of these approaches, "
            f"even rephrased, the lead will disengage.\n"
        )

    header = (
        "OBJECTION DETECTED.\n\n"
        "Read the conversation history. Know what angles you already tried. "
        "Never repeat an approach. Every response must come from a different direction.\n"
        f"{persistence_note}"
        f"{history_block}\n"
    )

    # ─── TWO-PHASE OBJECTION FRAMEWORK ───
    # Phase 1 (ALWAYS FIRST): Solve the logistical reality.
    # Every objection has a practical component. Can they afford it? Does their
    # spouse actually care? Is their coverage enough? Is their schedule really that
    # packed? Solve the logistics FIRST. Do not skip to emotional persuasion.
    #
    # Phase 2 (ONLY IF LOGISTICS RESOLVED): Address the fear underneath.
    # If the money works, the spouse would be on board, the coverage has real gaps,
    # the schedule is open — and they are STILL saying the same objection — then the
    # objection is not logistical. It is fear. Now you address the fear: stories,
    # perspective shifts, challenging the belief that staying where they are is safe.

    # Determine if we are still in Phase 1 or have moved to Phase 2
    # Phase 2 triggers when the SAME objection category appears 2+ times in the log.
    # The narrative observer writes free-text log entries, so we match against multiple
    # keywords per objection type to catch natural language variations.
    _OBJECTION_LOG_KEYWORDS = {
        ObjectionType.NOT_INTERESTED: ["not interested", "no thanks", "not for me", "pass", "decline", "dismissal", "disengag"],
        ObjectionType.SPOUSE_PARTNER: ["spouse", "partner", "wife", "husband", "consult", "family member", "advisor", "check with"],
        ObjectionType.PRICE_MONEY:    ["price", "money", "expensive", "afford", "cost", "budget", "cash flow", "value"],
        ObjectionType.ALREADY_COVERED: ["already covered", "already have", "already has", "existing coverage", "have insurance", "covered", "all set"],
        ObjectionType.THINK_ABOUT_IT: ["think about", "sleep on", "not ready", "get back", "need time", "decision", "consider", "rain check"],
        ObjectionType.BUSY_TIMING:    ["busy", "timing", "not a good time", "call back", "later", "schedule", "swamped", "slammed"],
    }
    match_keywords = _OBJECTION_LOG_KEYWORDS.get(obj, [obj.value.replace("_", " ")])
    same_objection_count = 0
    if log:
        for entry in log:
            entry_lower = entry.lower()
            if any(kw in entry_lower for kw in match_keywords):
                same_objection_count += 1
    in_phase_2 = same_objection_count >= 2
    in_phase_3 = same_objection_count >= 4  # Graceful exit after 4+ same objection

    if in_phase_3:
        header += (
            "=== PHASE 3: GRACEFUL EXIT — LEAVE THE DOOR OPEN ===\n"
            "They have said the same thing FOUR or more times. You have addressed the "
            "logistics AND the fear. They are not moving today.\n\n"
            "DO NOT: Push again. Do not try another angle. Do not ask another question "
            "about their situation. They have given you their answer for now.\n\n"
            "INSTEAD: Acknowledge their position warmly and without pressure. Tell them "
            "you understand. Let them know you are here if anything changes — no pressure, "
            "no timeline, no guilt. End the exchange with genuine respect for their decision.\n\n"
            "A top closer knows that LETTING GO gracefully is what brings them back in two weeks "
            "when they are lying awake at night thinking about what you said. Pushing a fifth "
            "time burns the bridge permanently.\n\n"
            "Send ONE short message. Warm, human, zero pressure. Then stop.\n\n"
        )
    elif in_phase_2:
        header += (
            "=== PHASE 2: FEAR-BASED HANDLING ===\n"
            "You have already addressed the logistical side of this objection in "
            "previous messages. The practical concern has been explored or resolved. "
            "They are STILL saying the same thing. This is no longer a logistics problem.\n\n"
            "The real blocker is fear — fear of making a mistake, fear of change, fear of "
            "commitment, fear of being sold to. Your job now is to help them see that "
            "staying where they are is not the safe choice they think it is.\n\n"
            "USE: Stories, analogies, perspective shifts. Help them see what happens if "
            "they do nothing. Make the cost of inaction concrete and personal. Challenge "
            "the assumption that not deciding IS a decision — and it is the riskiest one.\n\n"
            "DO NOT: Repeat logistical arguments. Do not re-explain pricing, coverage gaps, "
            "or scheduling. They already heard that. They need a reason to move, not more information.\n\n"
        )
    else:
        header += (
            "=== PHASE 1: LOGISTICAL RESOLUTION ===\n"
            "Before ANY emotional persuasion, solve the practical side of this objection. "
            "Every objection has a logistical component. Address it directly and honestly.\n"
            "Do NOT skip to stories, perspective shifts, or emotional appeals yet. "
            "The lead needs to see that the logistics work BEFORE they can address their feelings.\n\n"
        )

    if obj == ObjectionType.NOT_INTERESTED:
        if in_phase_2:
            return header + (
                "OBJECTION: Not interested (repeated — fear phase).\n"
                "The logistics have been explored. They know this is relevant to their situation "
                "but they are still resisting. The fear is usually: fear of being sold to, fear "
                "of making a wrong decision, or fear that acting means admitting vulnerability.\n\n"
                "Help them see that 'not interested' is not a plan — it is avoidance. "
                "The thing they are avoiding does not go away because they ignored it. "
                "Use what you know about their situation to make inaction feel concrete.\n\n"
                "Keep it to 1-2 sentences. Be genuine, not manipulative. If they have said no "
                "3+ times, a sincere 'fair enough, just wanted to make sure you were covered' "
                "is more powerful than another angle."
            )
        return header + (
            "OBJECTION: Not interested.\n"
            "They said some form of no. This is NOT a stop signal. This is the most common "
            "objection in sales and it almost never means the door is actually closed.\n\n"
            "PHASE 1 — LOGISTICAL CHECK:\n"
            "1. FIRST, acknowledge exactly what they said. Do not skip this. If they said "
            "'no longer interested', say something that shows you heard THOSE words.\n"
            "2. Do NOT immediately pitch again. Do NOT list benefits. Do NOT get defensive.\n"
            "3. Ask ONE question about their actual situation — not about insurance, about THEM. "
            "Has anything changed? Do they have something in place? Are they covered? "
            "The question should reveal whether they actually have the logistics handled "
            "or whether 'not interested' is masking 'I don't know enough to decide.'\n"
            "4. Keep it short. 1-2 sentences max. The lighter the touch, the better.\n\n"
            "WHAT NEVER TO SAY:\n"
            "- 'I understand, but...' (dismissive)\n"
            "- 'Just one more thing...' (pushy)\n"
            "- 'Are you sure?' (condescending)\n"
            "- 'What if I told you...' (salesy)\n"
            "- Anything that sounds like you did not hear them"
        )

    if obj == ObjectionType.SPOUSE_PARTNER:
        if in_phase_2:
            return header + (
                "OBJECTION: Spouse/partner (repeated — fear phase).\n"
                "You have already explored whether their spouse is actually involved. "
                "They are still deferring. This is not about their spouse — it is about "
                "their own fear of making a decision alone. Help them see that gathering "
                "information is not making a commitment. They can bring everything they "
                "learn to that conversation and be the person who came prepared."
            )
        return header + (
            "OBJECTION: Need to consult someone else (spouse, partner, family, advisor).\n\n"
            "PHASE 1 — LOGISTICAL CHECK:\n"
            "Respect who they want to consult. Ask logistical questions first:\n"
            "- Has their spouse/partner expressed an opinion on this before?\n"
            "- Would gathering the information now help make that conversation more productive?\n"
            "- Is there a specific concern their spouse would have (cost, timing, need)?\n"
            "Figure out if their spouse would actually object, or if 'need to talk to my wife' "
            "means 'I am not comfortable deciding alone.' Both are valid. Handle accordingly."
        )

    if obj == ObjectionType.PRICE_MONEY:
        if in_phase_2:
            return header + (
                "OBJECTION: Price/money (repeated — fear phase).\n"
                "You have explored the budget reality. They know options exist within their range "
                "but they are still saying it is too expensive. This is a value problem, not a "
                "cash flow problem. They do not believe the protection is worth what it costs.\n\n"
                "Help them see the cost of NOT having it. What does their family face financially "
                "if something happens? The premium they are resisting is a fraction of the problem "
                "they are leaving unresolved. Make the alternative concrete."
            )
        return header + (
            "OBJECTION: Price or money concern.\n\n"
            "PHASE 1 — LOGISTICAL CHECK:\n"
            "Two completely different problems hide behind 'too expensive':\n"
            "1. CASH FLOW: They genuinely cannot afford it. Ask about their budget range. "
            "There may be options they do not know about — term is often cheaper than people think.\n"
            "2. VALUE: They can afford it but do not see the worth. This means the impact has "
            "not landed yet. They need to feel what the gap costs, not what the policy costs.\n\n"
            "Figure out which one it is BEFORE addressing it. Do not assume.\n"
            "HARD RULE: You cannot quote specific dollar amounts over text."
        )

    if obj == ObjectionType.ALREADY_COVERED:
        if in_phase_2:
            return header + (
                "OBJECTION: Already covered (repeated — fear phase).\n"
                "You have asked about their coverage and they have either described it or deflected. "
                "If they described it and gaps are obvious, they know the gaps exist but are choosing "
                "to ignore them. The fear is: 'If I admit my coverage is not enough, I have to do "
                "something about it.' Help them see that knowing about a gap and ignoring it is "
                "worse than not knowing. Their family is the one who pays for that decision."
            )
        return header + (
            "OBJECTION: Already have coverage.\n\n"
            "PHASE 1 — LOGISTICAL CHECK:\n"
            "Do not challenge it. Do not list features they might be missing. "
            "Most people who say this cannot explain what they actually have.\n"
            "Ask logistical questions:\n"
            "- Is it through an employer or personal? (employer = may not be portable)\n"
            "- Do they know the coverage amount? (most guess wrong)\n"
            "- Does it cover their mortgage, their family's needs for how many years?\n"
            "The gap usually reveals itself when they try to describe their coverage. "
            "Let them discover it themselves — do not tell them they are wrong."
        )

    if obj == ObjectionType.THINK_ABOUT_IT:
        if in_phase_2:
            return header + (
                "OBJECTION: Think about it (repeated — fear phase).\n"
                "They have been 'thinking' for a while now. Thinking is not the problem. "
                "The problem is that thinking feels safer than deciding. Every day they 'think' "
                "is a day their family is unprotected and a day they are older (and premiums are higher). "
                "Help them see that delay IS a decision — the most expensive one they can make."
            )
        return header + (
            "OBJECTION: Need to think about it.\n\n"
            "PHASE 1 — LOGISTICAL CHECK:\n"
            "Could be genuine processing time or decision avoidance. Ask what specifically "
            "they need to think about:\n"
            "- Is it the cost? (You can explore options on a call)\n"
            "- Is it whether they need it? (Their situation says yes or no — help them evaluate)\n"
            "- Is it comparing options? (A 15-minute call can answer that faster than weeks of research)\n"
            "If they have been thinking about this for months, thinking is not the problem. "
            "Figure out what is actually holding them back."
        )

    if obj == ObjectionType.BUSY_TIMING:
        if in_phase_2:
            return header + (
                "OBJECTION: Busy/timing (repeated — fear phase).\n"
                "You have offered flexible scheduling. They are still saying they are too busy. "
                "Everyone is busy. The question is not whether they have time — it is whether this "
                "matters enough to make time. If their family's financial protection is not worth "
                "15 minutes, that tells you something about how they are prioritizing this. "
                "Help them feel the weight of that prioritization without being preachy."
            )
        return header + (
            "OBJECTION: Busy or bad timing.\n\n"
            "PHASE 1 — LOGISTICAL CHECK:\n"
            "Respect the timing. Ask logistical questions:\n"
            "- When would be a better time? Anchor a specific day/time.\n"
            "- Would a 10-minute call work better than texting back and forth?\n"
            "- Is there a specific event making this week bad (work deadline, family event)?\n"
            "If they give a specific time, lock it in. If they stay vague ('maybe later', 'sometime'), "
            "that is not a timing objection — it is avoidance wearing a scheduling mask."
        )

    return header + (
        "They raised a concern. Figure out the logistical reality first — "
        "what is the practical barrier? Only after that is resolved, address the fear underneath."
    )


# ═══════════════════════════════════════════════════
# RAPPORT GUIDANCE
# ═══════════════════════════════════════════════════

def _build_rapport_guidance(logic: LogicSignal, first_name: str) -> str:
    """
    Build tactical guidance for the rapport stage.

    Rapport is NOT filler. It is the trust-building layer that separates
    a human sales conversation from a bot interrogation. The lead said
    something personal, conversational, or off-topic — acknowledge it,
    connect as a person, then naturally steer back toward qualifying.

    Max 2 consecutive rapport turns before hard pivot to qualifying.
    """
    turns = logic.consecutive_rapport_turns

    if turns == 0:
        # First rapport turn — lean into it, be a real person
        return (
            "RAPPORT: They said something personal or conversational. This is gold.\n\n"
            "WHAT TO DO:\n"
            "1. Respond to what THEY said. Not what you want to talk about. What THEY said.\n"
            "2. Match their energy. If they're joking, be light. If they're sharing something "
            "real, acknowledge it genuinely.\n"
            "3. Share something briefly about yourself or find common ground. You are a person, "
            "not a question machine. One sentence of genuine connection goes further than "
            "five qualifying questions.\n"
            "4. End with a natural bridge — a question or comment that connects what they said "
            "back to their life situation. Not a hard pivot to insurance. A bridge.\n\n"
            "WHAT NOT TO DO:\n"
            "- Do not ignore what they said and ask a qualifying question\n"
            "- Do not say 'that's great, anyway about your coverage...'\n"
            "- Do not treat this as wasted time. This IS the sale.\n"
            "- Do not be fake. If you cannot relate, ask them more about it.\n\n"
            "The goal is not to stay in rapport forever. The goal is to earn enough trust "
            "that when you DO ask about their situation, they actually answer honestly."
        )

    # turns == 1 — second rapport turn, start bridging
    return (
        "RAPPORT (turn 2): Good — you built some connection. Now bridge back.\n\n"
        "WHAT TO DO:\n"
        "1. Acknowledge what they just said naturally (one sentence, not a speech).\n"
        "2. Use what you learned about them to transition into a qualifying question. "
        "Connect it to something they told you. If they mentioned kids, ask about protecting them. "
        "If they talked about work stress, ask what keeps them up at night. If they shared a hobby, "
        "find the thread that connects to their life situation.\n"
        "3. The transition should feel like a conversation flowing, not a subject change.\n\n"
        "WHAT NOT TO DO:\n"
        "- Do not stay in rapport for a third turn. You have built enough trust.\n"
        "- Do not make a hard subject change. The bridge should feel natural.\n"
        "- Do not ask a generic qualifying question. Use what they gave you.\n\n"
        "You earned some trust. Now use it. Ask something real about their situation "
        "that connects to what they just told you."
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
