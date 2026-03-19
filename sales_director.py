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
        "Never repeat an approach. Every response must come from a different direction.\n\n"
        "EMOTIONAL ANCHORING REMINDER: Check CONVERSATION MEMORY for EMOTIONAL_ARC. "
        "If they told you about their family, their fears, their kids, their situation — "
        "USE IT. Bring their own words back when they object. That is your strongest card. "
        "Do not waste it on generic responses when you have specific, personal ammunition.\n"
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
        ObjectionType.NOT_INTERESTED: ["not interested", "no thanks", "not for me", "pass", "decline", "dismissal", "disengag", "don't care", "dont care", "done", "over it", "whatever"],
        ObjectionType.SPOUSE_PARTNER: ["spouse", "partner", "wife", "husband", "consult", "family member", "advisor", "check with", "handles", "decides", "takes care"],
        ObjectionType.PRICE_MONEY:    ["price", "money", "expensive", "afford", "cost", "budget", "cash flow", "value"],
        ObjectionType.ALREADY_COVERED: ["already covered", "already have", "already has", "existing coverage", "have insurance", "covered", "all set"],
        ObjectionType.THINK_ABOUT_IT: ["think about", "sleep on", "not ready", "get back", "need time", "decision", "consider", "rain check", "maybe", "not sure", "idk", "we'll see"],
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
            "=== PHASE 3: THE TAKEAWAY — PLANT THE SEED ===\n"
            "They have said the same thing FOUR or more times. You tried the logistics. "
            "You tried the emotional truth. They are not moving today. That is okay.\n\n"
            "NOW you use the most counterintuitive tool in your arsenal: you let go. "
            "Genuinely. Not as a tactic. Because pushing a fifth time does not show persistence, "
            "it shows desperation, and they can feel it.\n\n"
            "Send ONE message that does three things:\n"
            "1. Acknowledge their position with real respect. Not 'I understand' (empty). "
            "Something that shows you actually heard them across this whole conversation.\n"
            "2. Leave one specific thought with them — not a pitch, not a benefit. Something "
            "personal to their situation that they will think about later. If they told you about "
            "their kids, their spouse, their fear — leave it connected to that. Plant it and walk away.\n"
            "3. Make it clear the door is open with zero pressure. They know how to reach you.\n\n"
            "This message is what they remember at 2 AM when they cannot sleep. "
            "Make it worth remembering. Then stop.\n\n"
        )
    elif in_phase_2:
        header += (
            "=== PHASE 2: THE REAL CONVERSATION — FEAR ===\n"
            "The logistics have been handled. You explored the practical side. "
            "They are STILL saying the same thing. Which means it was never about the logistics.\n\n"
            "Underneath every repeated objection in life insurance is the same thing: "
            "this person does not want to sit with the reality that they could die and the "
            "people they love would be left to deal with the consequences. That is terrifying. "
            "So they say 'not interested' or 'too expensive' or 'I need to think about it' "
            "because those are easier than saying 'I am scared of what happens if I face this.'\n\n"
            "Your job is NOT to scare them. It is to help them see that the thing they are "
            "avoiding does not go away because they stopped talking about it. Their family "
            "is unprotected right now, today, while they are making this decision. Delay is not "
            "neutral. Delay is a choice that their family pays for.\n\n"
            "USE THEIR OWN WORDS. Check the EMOTIONAL_ARC. If they mentioned kids, a spouse, "
            "a sick parent, financial stress — bring it back. Not to guilt them. To remind them "
            "of what they already told you matters. They sold themselves earlier in this conversation. "
            "Hold them to it.\n\n"
            "Make it about love, not death. Make it about the kind of person they want to be "
            "for the people counting on them. One or two sentences. Be real. Be direct.\n\n"
            "DO NOT: Repeat logistical arguments. Do not re-explain pricing or coverage. "
            "They do not need more information. They need a reason to feel something.\n\n"
        )
    else:
        header += (
            "=== PHASE 1: SOLVE THE PRACTICAL PROBLEM ===\n"
            "Before any emotional depth, solve the logistical side of this objection. "
            "Every objection has a practical component. Can they actually afford it? Does their "
            "spouse actually care? Is their coverage actually enough? Is their schedule really "
            "that packed? Answer the practical question first.\n\n"
            "Do NOT skip to stories or emotional appeals. If the logistics genuinely do not work, "
            "no amount of emotion will fix it. But most of the time, the logistics DO work — "
            "the person just has not been shown that yet. Show them.\n\n"
        )

    if obj == ObjectionType.NOT_INTERESTED:
        if in_phase_2:
            # ─── Phase 2: They already answered the fork (price vs approval). ───
            # Now we know WHICH path they went down. Follow that thread deeper.
            # Check the OBJECTION_LOG and conversation history to see if they
            # revealed price or approval as the real issue on the first "not interested."
            return header + (
                "OBJECTION: Not interested (repeated — follow the fork they chose).\n\n"
                "On their first 'not interested' you asked the fork question: was it more "
                "about the price or about getting approved. Check the conversation history "
                "and OBJECTION_LOG to see which path they went down.\n\n"
                "IF THEY SAID PRICE / TOO EXPENSIVE / COST:\n"
                "They told you it is about money. Do NOT argue that it is affordable. Get curious "
                "about what 'too expensive' actually means to them. There are two very different "
                "realities hiding behind 'too expensive':\n"
                "- They genuinely cannot swing it right now (real budget constraint)\n"
                "- They do not see the value yet, so any price feels like too much\n"
                "Your move: 'Is it that you genuinely cannot swing it right now, or is it more that "
                "you are not sure it is worth it?' That one question separates real from perceived. "
                "If it is real, respect it and plant a seed about rates going up with age. "
                "If it is perceived, the value has not landed yet — go back to their situation. "
                "What happens to the people they mentioned if this stays unhandled?\n\n"
                "IF THEY SAID APPROVAL / HEALTH / COULD NOT GET APPROVED:\n"
                "They already tried and got denied, or they believe they cannot qualify. This is "
                "a person who WANTED coverage and was told no. That is not disinterest — that is "
                "frustration and defeat. They are protecting themselves from another rejection.\n"
                "Your move: acknowledge the frustration first. 'That is a rough spot to be in.' "
                "Then: 'Do you mind me asking what happened?' Get the story. Was it a specific "
                "health condition? Was it the wrong product type? Many people who got denied for "
                "one type of policy qualify for another. Guaranteed issue, simplified issue, graded "
                "benefit — there are paths. But do not pitch solutions yet. Understand what happened "
                "first, then gently let them know there might be options they have not seen.\n\n"
                "IF THEY DID NOT ANSWER THE FORK (deflected or changed subject):\n"
                "They avoided the question, which itself is information. They do not want to reveal "
                "the real reason yet. Do not re-ask the same fork. Instead, use EMOTIONAL_ARC if "
                "you have anything personal. Make the alternative specific. What does 'not interested' "
                "actually look like for THEIR family? Keep it to 1-2 sentences. Be genuine. "
                "Not manipulative. Not preachy."
            )
        return header + (
            "OBJECTION: Not interested.\n\n"
            "THE FORK TECHNIQUE — Agree first, then redirect.\n\n"
            "Step 1: AGREE. Do not fight it. Do not argue. Do not ask 'why not?' or try to "
            "overcome it head-on. When you agree, their wall drops because there is nothing "
            "to push against. Something like 'Yeah totally fair' or 'No I get it' or 'Hey "
            "that is okay.' Short. Casual. Zero resistance from you.\n\n"
            "Step 2: THE FORK. Immediately after agreeing, ask the fork question that splits "
            "their 'not interested' into two specific paths:\n\n"
            "The question: Was it more that the price was too high, or did you have a tough "
            "time getting approved?\n\n"
            "Variations (use your own natural wording, but always present these two options):\n"
            "- 'Was it more the cost or was it a health thing?'\n"
            "- 'Was it more a budget thing or did you run into trouble qualifying?'\n"
            "- 'Was it the price that turned you off or was it more getting through underwriting?'\n\n"
            "WHY THIS WORKS: 'Not interested' is a brick wall. You cannot climb a wall. "
            "But you can turn a wall into a fork in the road. Both paths lead back to a real "
            "conversation. If they say price, you now know the real objection and can work it. "
            "If they say approval, you are talking to someone who WANTED coverage and was told no — "
            "that is a completely different person than someone who does not care. Either answer "
            "opens the conversation back up.\n\n"
            "CRITICAL: Do NOT skip the agree step. If you jump straight to the fork without "
            "agreeing first, it sounds like a scripted rebuttal and they will shut down harder.\n\n"
            "CRITICAL: Keep this to 1-2 sentences total. The agree and the fork should feel "
            "like one natural breath: 'No for sure, totally get it. Was it more the cost or "
            "did you have a hard time getting approved?'"
        )

    if obj == ObjectionType.SPOUSE_PARTNER:
        if in_phase_2:
            # Miner's consequence question — insurance-specific
            return header + (
                "OBJECTION: Spouse/partner (repeated).\n"
                "You already explored the spouse angle. They keep deferring. Time for the "
                "consequence question — make doing nothing feel real, not abstract.\n\n"
                "Insurance-specific play from Jeremy Miner:\n"
                "'What will [spouse] have to do though if you did pass away, God forbid, "
                "and [she/he] gets stuck with having to pay the mortgage and all the other "
                "bills but doesn't have the money to even do that?'\n\n"
                "Use what you know from EMOTIONAL_ARC — kids, mortgage, income. Make it "
                "specific to THEIR situation. Then follow with:\n"
                "'Are you willing to settle for that and take that risk?'\n\n"
                "If they STILL say they need to talk to spouse after the consequence question, "
                "stop pushing. Set the appointment: 'No problem. What if you both hopped on "
                "the call together? That way you can look at the numbers side by side instead "
                "of trying to explain it secondhand.'\n\n"
                "The goal is the appointment. Getting the spouse ON the call is even better "
                "than having the lead relay information poorly."
            )
        return header + (
            "OBJECTION: Need to consult someone else.\n"
            "Respect the relationship. Do not undermine their spouse. That backfires.\n\n"
            "STEP 1: Disarm. 'Yeah that is not a problem.'\n\n"
            "STEP 2: The Miner question — tie the spouse to what THEY want:\n"
            "'How does your [wife/husband] feel about having stronger financial protection "
            "for the house and other expenses if something happens to you?'\n"
            "This is NOT asking for permission. It is getting THEM to think about whether "
            "the spouse would want this. The answer is always yes.\n\n"
            "STEP 3: Position the call as HELPING their spouse conversation:\n"
            "'What if you came to that conversation with all the numbers already figured out? "
            "That way it is a real conversation instead of both of you guessing.'\n\n"
            "You are making them the hero who did the homework, not asking them to commit "
            "to anything. A 10-minute call to gather information is not a purchase decision.\n\n"
            "REMEMBER: This bot books appointments. The spouse does not need to approve "
            "an appointment — they need to approve a purchase. Those are different things. "
            "Make that distinction if needed: 'This isn't signing anything — it is just "
            "getting the info so you two can make a smart decision together.'"
        )

    if obj == ObjectionType.PRICE_MONEY:
        if in_phase_2:
            return header + (
                "OBJECTION: Price/money (repeated).\n"
                "You explored the budget. They are still stuck on cost. This is no longer about "
                "whether they have the money. This is about whether they see the value.\n\n"
                "Miner's 'which is more risky' question — adapted for insurance:\n"
                "'You tell me which is more risky — spending [what they think it costs] a month "
                "so your family is completely taken care of? Or doing nothing, and if something "
                "happens they are stuck with the mortgage, the bills, and no way to cover it?'\n\n"
                "Use what you know from EMOTIONAL_ARC. Kids, spouse, mortgage — make the cost "
                "of doing nothing SPECIFIC to them. Not generic.\n\n"
                "If they genuinely cannot afford it, respect that. But redirect to the call: "
                "'That is exactly why the call matters. Most people think this costs 10x what it "
                "actually costs. You might be surprised — takes 10 minutes to find out.'\n\n"
                "HARD RULE: Still no specific dollar amounts over text. The call is WHERE they "
                "learn the real numbers."
            )
        return header + (
            "OBJECTION: Price or money concern.\n"
            "This rarely comes up in appointment booking because you are NOT selling a policy "
            "over text — you are booking a call. But if it does:\n\n"
            "STEP 1: Clarify. Miner says always find out what 'too expensive' actually means. "
            "Two very different problems hide behind it:\n"
            "- REAL CASH FLOW: They genuinely cannot swing it right now\n"
            "- VALUE PROBLEM: They can afford it but do not see why it is worth it\n\n"
            "One question separates them: 'Is it that you genuinely cannot swing it right now, "
            "or is it more that you are not sure it is worth it?'\n\n"
            "STEP 2: Either way, redirect to the call. Most people think life insurance costs "
            "way more than it actually does. The call gives them real numbers for their situation "
            "instead of guessing. That is the pitch: 'It takes 10 minutes to see what it would "
            "actually cost for your situation. You might be surprised.'\n\n"
            "REMEMBER: You are not selling a policy. You are booking a 10-minute call. "
            "Price concerns about the APPOINTMENT do not exist — the call is free. If they "
            "are worried about the insurance cost, the call IS the answer to that worry.\n\n"
            "HARD RULE: You cannot quote specific dollar amounts over text."
        )

    if obj == ObjectionType.ALREADY_COVERED:
        if in_phase_2:
            # They told you who they have or dodged. Now dig into the gap.
            return header + (
                "OBJECTION: Already covered (repeated — follow the thread).\n"
                "By now you should know what kind of coverage they have. Use that.\n\n"
                "IF THEY HAVE A KNOWN CARRIER (Mutual of Omaha, Foresters, Transamerica, "
                "Americo, Colonial Penn, Globe Life, AARP):\n"
                "These carriers serve higher-risk clients. If the lead is healthy, they are "
                "probably overpaying. 'Those guys are solid but they take a lot of higher-risk "
                "clients so the pricing is usually higher for healthy people like you.' Then: "
                "'What are you paying right now if you don't mind me asking?'\n"
                "Whatever they say: 'Yeah that is exactly what I figured. Healthy people usually "
                "pay more than they need to with those carriers. I can run you a quick comparison "
                "— takes 10 minutes — what time works better for you tomorrow?'\n\n"
                "IF THEY HAVE EMPLOYER / GROUP / 'THROUGH WORK' COVERAGE:\n"
                "'Do you know what happens to that when you retire or switch jobs?'\n"
                "Most do not. That coverage dies the day they leave. And workplace policies are "
                "usually 1-2x salary — that covers maybe one year of bills. Ask: 'How many years "
                "would that actually cover your family for?' Let them do the math.\n\n"
                "IF THEY HAVE TERM AND ARE HEALTHY:\n"
                "'How many years are left on it?' If they have 20+ years left, genuinely "
                "acknowledge it is solid. But: 'What is your plan when it expires? Because rates "
                "at [their age + remaining years] are going to be very different.' Plant the seed.\n\n"
                "IF THEY HAVE GUARANTEED ISSUE (no exam, Colonial Penn, Globe Life):\n"
                "'Those usually have a 2-3 year waiting period. How long ago did you get it?'\n"
                "GI policies pay nothing for natural causes in the first 2-3 years. If they are "
                "healthy, they qualify for real coverage at a fraction of the cost."
            )
        return header + (
            "OBJECTION: Already have coverage.\n\n"
            "THE CARRIER QUESTION — Get curious, not adversarial.\n\n"
            "Do NOT challenge it. Do NOT say 'but is it enough?' That puts them on defense.\n\n"
            "ONE QUESTION: 'Oh nice! Who did you end up going with?'\n\n"
            "This is casual, non-threatening, and it opens a conversation. Their answer tells "
            "you everything:\n"
            "- If they name a carrier → you can assess fit, pricing, gaps\n"
            "- If they say 'through work' → employer coverage = biggest gap in America\n"
            "- If they say 'I forget' or 'not sure' → they do not actually know what they have\n"
            "- If they dodge → the 'coverage' is probably minimal or nonexistent\n\n"
            "Variations:\n"
            "- 'Cool, who did you go with?'\n"
            "- 'Good to hear. What kind of policy did you land on?'\n"
            "- 'Nice, is that something you got on your own or through work?'\n\n"
            "The carrier question is your gateway. It feels like small talk but it opens "
            "every door — carrier gaps, employer portability, GI waiting periods, term "
            "expiration. Let THEIR answer guide you, do not pre-load a pitch.\n\n"
            "REMEMBER: You are booking an appointment, not selling a policy. The call is "
            "where you actually review their coverage. 'I can do a quick comparison for you "
            "— takes 5 minutes — just to make sure you are not overpaying.'"
        )

    if obj == ObjectionType.THINK_ABOUT_IT:
        if in_phase_2:
            return header + (
                "OBJECTION: Think about it (repeated).\n"
                "They keep stalling. Miner says: 'Let me think about it' is NEVER the real "
                "objection. Nobody goes home and thinks about life insurance for hours. There "
                "is a real concern hiding behind it — they just do not want to tell you.\n\n"
                "You already tried isolating what they need to think about. Now use Miner's "
                "'which is more risky' close:\n"
                "'You tell me which is more risky — spending 10 minutes on a call to see "
                "where you actually stand? Or keeping things the way they are and hoping "
                "nothing happens in the meantime?'\n\n"
                "If they shared anything in EMOTIONAL_ARC — kids, spouse, mortgage, health "
                "scare — use it: 'You mentioned [person/thing]. Are they covered right now "
                "while you are thinking it over?'\n\n"
                "If they STILL stall after this, Miner says set the appointment anyway: "
                "'No problem, what is your timeframe on getting back to me in the next day "
                "or two?' Then book a specific time. Do NOT let them vaguely say 'I will "
                "call you back.' That means never."
            )
        return header + (
            "OBJECTION: Need to think about it.\n\n"
            "CRITICAL: 'Think about it' comes in MANY disguises. All of these are the same "
            "objection:\n"
            "- 'Let me think about it' / 'I need to think on it'\n"
            "- 'Send me an email' / 'Send me some info' / 'Send me the details'\n"
            "- 'Let me look it over' / 'Let me look into it'\n"
            "- 'I will get back to you' / 'I will let you know'\n"
            "- 'Not ready yet' / 'Not sure yet' / 'Maybe down the road'\n"
            "- 'Can you send me a proposal' / 'Send me a quote'\n"
            "ALL of these mean the same thing: 'I do not have a compelling enough reason "
            "to act NOW.'\n\n"
            "Miner's approach — DISARM then ISOLATE:\n\n"
            "STEP 1: Disarm. 'Yeah that is not a problem.' Takes the sales pressure out. "
            "Their guard drops.\n\n"
            "STEP 2: Isolate what is behind it. NOT 'what do you need to think about' — "
            "that is a logical trap and they know it. Instead use Miner's softer version:\n"
            "'What were you wanting to go over in your mind? Just so I know what questions "
            "you might have when we talk.'\n"
            "This reframes 'thinking about it' into 'having questions' — which leads to "
            "a follow-up call, not a dead end.\n\n"
            "STEP 3: Whatever they reveal IS the real objection. Handle THAT:\n"
            "- If cost → price objection. 'The call gives you real numbers instead of guessing.'\n"
            "- If need → gap question. 'That is exactly what the call covers — where you stand.'\n"
            "- If trust → they got burned before. Acknowledge it.\n"
            "- If they cannot articulate it → they are avoiding. Gently: 'Most people feel "
            "that way until they actually sit down and look at the numbers. Takes 10 minutes.'\n\n"
            "NEVER send info and wait. That is a dead end. Miner: 'If you simply agree to "
            "send information without a commitment for the next step, you will most likely "
            "never hear from that prospect again.'"
        )

    if obj == ObjectionType.BUSY_TIMING:
        if in_phase_2:
            return header + (
                "OBJECTION: Busy/timing (repeated).\n"
                "You offered flexible scheduling. They keep dodging. This is no longer about "
                "their schedule — it is avoidance wearing a watch.\n\n"
                "Two moves left:\n\n"
                "1. Make it impossibly small: 'It is literally 10 minutes. Shorter than your "
                "lunch break. Then you know exactly where you stand and you never have to think "
                "about it again.'\n\n"
                "2. If they STILL dodge, name it: 'Sounds like the timing might not be the real "
                "thing. Is there something else holding you back?' Give them permission to say "
                "the real reason. Whatever they reveal IS the actual objection — handle THAT.\n\n"
                "Miner: 'When a prospect keeps saying they are too busy, they have a real concern "
                "but they are just not wanting to tell you what that is. It is a knee jerk response.'"
            )
        return header + (
            "OBJECTION: Busy or bad timing.\n\n"
            "This is the most common objection in appointment booking. People ARE actually busy. "
            "Respect it. But do NOT let them off with vague promises.\n\n"
            "MINER'S STATUS FRAME — Position yourself as busy too:\n\n"
            "STEP 1: Agree casually. 'Yeah no problem.' / 'Totally get it.'\n\n"
            "STEP 2: Anchor a specific time. Vague is death. 'Later' means never. "
            "'Call me next week' means forgotten. Miner's play:\n"
            "'What time works better for you — mornings or evenings?' or offer two specific "
            "slots: 'I have some time tomorrow at 10:15 or 2:30 — which works better?'\n\n"
            "STEP 3: If they stay vague — 'maybe later', 'sometime', 'I will call you' — "
            "use the Miner status frame: 'It might be tough to randomly catch me with my "
            "schedule. If you have your calendar handy I can find a time that works for both "
            "of us — that way neither of us has to chase each other down.'\n\n"
            "WHY THIS WORKS: It positions you as someone whose time is valuable. You are "
            "busy too. You have other clients. You are doing THEM a favor by finding a slot. "
            "This triggers them to view you as an expert, not a salesperson begging for time.\n\n"
            "CRITICAL: When they give you a day/time — LOCK IT IN immediately. Stop talking. "
            "Book it. Do not keep selling after they said yes to the time.\n\n"
            "If they absolutely cannot commit, make it tiny: 'Even 5 minutes works — just enough "
            "to see if this is worth a longer conversation.'"
        )

    return header + (
        "They raised a concern you do not have a specific playbook for. That is fine. "
        "The framework is the same:\n"
        "1. Acknowledge what they said. Mirror their words.\n"
        "2. Ask one question that reveals whether this is a real practical barrier or "
        "a way to avoid the conversation.\n"
        "3. If practical — solve it. If emotional — make doing nothing feel specific.\n\n"
        "Use what you know about them from EMOTIONAL_ARC. The more personal your response, "
        "the harder it is to dismiss."
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
