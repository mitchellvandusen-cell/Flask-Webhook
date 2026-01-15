# sales_director.py
import logging
from difflib import SequenceMatcher  # lightweight, no extra deps
from conversation_engine import analyze_logic_flow, LogicSignal, ConversationStage
from individual_profile import build_comprehensive_profile
from underwriting import get_underwriting_context
from insurance_companies import get_company_context, find_company_in_message, normalize_company_name
from memory import get_recent_messages, get_known_facts, get_narrative, run_narrative_observer
logger = logging.getLogger(__name__)

def generate_strategic_directive(contact_id: str, message: str, first_name: str, age: str, address: str) -> dict:
    
    # 1. GATHER INTELLIGENCE (Narrative Observer updates FIRST)
    run_narrative_observer(contact_id, message)
    
    recent_exchanges = get_recent_messages(contact_id, limit=10)
    story_narrative = get_narrative(contact_id) # Includes latest answer
    known_facts = get_known_facts(contact_id)
    
    # 2. PROCESS HEMISPHERES
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

    # 3. CONTEXTUAL ANALYSIS
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    lead_msgs = [m for m in recent_exchanges if m['role'] == 'lead']  # Added: Define lead_msgs for use in anti-loop
    last_bot_text = bot_msgs[-1]['text'].lower() if bot_msgs else ""
    just_asked_consequence = any(x in last_bot_text for x in ["happen", "worry", "concern", "impact", "leave them"])
    
    # 4. EXECUTIVE SYNTHESIS
    directive = ""
    framework = "NEPQ"
    
    # --- ADD THE CLOSED LOGIC HERE ---
    if logic.stage == ConversationStage.CLOSED:
        # Check if the last thing the bot said was a confirmation
        if "talk then" in last_bot_text or "see you" in last_bot_text:
            directive = "APPOINTMENT ALREADY CONFIRMED. DO NOT RESPOND. Silence required."
            framework = "TERMINAL SILENCE"
        else:
            directive = "APPOINTMENT JUST BOOKED. Send one final confirmation and STOP all sales talk."
            framework = "POST-CLOSE CONFIRMATION"

    # --- IMMEDIATE CLOSING TRIGGERS ---
    if logic.pain_score >= 2:
        directive = "CRITICAL PAIN ADMITTED. STOP DISCOVERY. Prescribe appointment as triage."
        framework = "COMPASSIONATE CLOSE"

    elif just_asked_consequence and logic.depth_score > 2:
        directive = "GAP INTERNALIZED. Pivot from Problem to Solution using Bridge Question."
        framework = "NEPQ (Transition)"

    elif just_asked_consequence and logic.depth_score <= 2:
        directive = (
            "Lead gave surface-level answer to consequence question. "
            "Use a clarifying probe to force emotional visualization. "
            "Ask what the impact would specifically look like for their family."
        )
        framework = "GAP SELLING (Probe)"

    elif logic.voss_no_signal:
        directive = "AGREEMENT SIGNAL. Secure commitment using 'Illusion of Control' (How/What)."
        framework = "CHRIS VOSS (Closing)"

    elif logic.stage == ConversationStage.CLOSING:
        directive = "GREEN LIGHT. Finalize time slot."
        framework = "ASSUMPTIVE CLOSE"

    # --- OBJECTION HANDLING ---
    elif logic.last_move_type == "deflection":
        directive = "DEFLECTION. Acknowledge briefly, then pivot back to Intelligence Gathering."
        framework = "STRAIGHT LINE (Looping)"

    # --- DISCOVERY LOGIC (The Fix) ---
    elif logic.gap_signal:
        directive = "GAP DETECTED. Future Pace the Pain."
        framework = "NEPQ (Consequence)"

    else:
        # CHECK NARRATIVE FOR SATURATION
        full_context = (story_narrative + " " + " ".join(known_facts)).lower()
        has_type = any(x in full_context for x in ["term", "whole", "iul", "group", "work"])
        has_amount = any(x in full_context for x in ["$", "amount", "coverage", "benefit", "mil"])
        has_expiry = any(x in full_context for x in ["year", "expire", "renew", "permanent"])

        if (has_type + has_amount + has_expiry) >= 2:
            directive = "MOST POLICY BASICS KNOWN. Pivot to challenging quality / revealing gap."
            framework = "GAP SELLING (Quality Challenge)"
        else:
            directive = (
                "DISCOVERY MODE. Ask **only** about the MISSING piece (Type, Amount, or Expiration). "
                "DO NOT re-ask anything already in narrative or facts. "
                "If unsure what’s missing, make a light statement reframing what you do know instead of questioning."
            )
            framework = "NEPQ (Situation)"
            
    # === SOFTEN DIRECTIVES FOR LOW SUBTEXT ===
    # Check if lead message is empty/minimal right before returning
    if not message.strip():
        directive += "\nSUBTEXT GUIDE: Minimal input—treat as neutral; lightly reframe prior point as guide to progress, not new probe."
    # ────────────────────────────────────────────────────────────────
    # ANTI-LOOP / STUCK FALLBACK (expanded & general)
    # ────────────────────────────────────────────────────────────────

    bot_recent_questions = [
        m['text'].lower()
        for m in bot_msgs[-5:]           # look back up to 5 bot messages
        if '?' in m['text']              # only consider actual questions
    ]

    if len(bot_recent_questions) >= 2:
        # Simple repetition: same or very similar question asked ≥2 times recently

        last_q = bot_recent_questions[-1]
        prev_qs = bot_recent_questions[:-1]

        similar_count = sum(
            SequenceMatcher(None, last_q, prev).ratio() > 0.65
            for prev in prev_qs
        )

        if similar_count >= 1:  # found at least one near-duplicate
            directive = (
                "POTENTIAL REPETITION DETECTED — bot has asked similar discovery/probe questions recently. "
                "Do NOT ask another question in the same vein. "
                "Instead: 1) Empathize / label the lead's emotional state, "
                "2) Lightly reframe what is already known from narrative/facts, "
                "3) Pivot toward solution awareness, value angle, or soft booking attempt. "
                "Use a statement or No-Oriented question if needed — avoid open probes."
            )
            framework = "ANTI-LOOP PIVOT"
            logger.warning(f"ANTI-LOOP TRIGGERED | contact={contact_id} | reason=similarity={similar_count} | last_q={last_q[:50]}")

        # Bonus: Keyword-based escape hatches for very common loop patterns
        elif any(word in " ".join(bot_recent_questions) for word in [
            "worry", "concern", "afraid", "scared", "happen if", "impact", "leave them",
            "what happens", "how would", "tell me more about", "opposed", "gap", "protect"  # Expanded for Grok persistence
        ]) and len(bot_recent_questions) >= 3:
            directive = (
                "MULTIPLE EMOTIONAL PROBES DETECTED — risk of discovery fatigue. "
                "Assume partial gap awareness already exists. "
                "Reframe known pain points empathetically, then guide toward next step "
                "(solution discussion or soft booking validation). "
                "For Grok-4-1: Prioritize concise, non-repetitive reframes to break persistence loops."
            )
            framework = "ANTI-LOOP EMOTIONAL FATIGUE PIVOT"
            logger.warning(f"ANTI-LOOP TRIGGERED | contact={contact_id} | reason=emotional_probes x{len(bot_recent_questions)}")

        # Lead fatigue check (only runs if already in question-heavy mode)
        lead_recent = [m['text'].strip() for m in recent_exchanges[-6:] if m['role'] == 'lead']
        if len(lead_recent) >= 3 and all(len(txt.split()) <= 3 for txt in lead_recent[-3:]):
            directive += (
                "\nLEAD REPLIES VERY SHORT — possible fatigue or disinterest. "
                "Keep next message ultra-brief, empathetic, and action-oriented. "
                "No questions unless Voss-style for closure."
            )
            logger.warning(f"ANTI-LOOP TRIGGERED | contact={contact_id} | reason=short_replies (last 3 words: {[len(t.split()) for t in lead_recent[-3:]]})")

        # New: Theme repetition (beyond phrasing)
        theme_keywords = ["gap", "worry", "concern", "family", "expire", "protect", "coverage", "opposed", "annoy", "frustrat"]  # Expanded to catch annoyance loops
        theme_count = sum(any(kw in q for kw in theme_keywords) for q in bot_recent_questions)
        if theme_count >= len(bot_recent_questions) * 0.6:  # 60%+ questions hit themes
            directive = (
                "THEME REPETITION DETECTED — bot stuck on gaps/worries. "
                "Do NOT ask questions. Use a neutral statement to reframe value or disengage softly. "
                "For Grok-4-1: Use fast reasoning to detect subtext and pivot decisively."
            )
            framework = "ANTI-LOOP THEME PIVOT"
            logger.warning(f"ANTI-LOOP TRIGGERED | contact={contact_id} | reason=theme_count={theme_count}")

        # New: Cumulative resistance
        lead_recent_moves = [analyze_logic_flow([m]).last_move_type for m in lead_msgs[-5:]]  # Optimized: Only get move_type, not full signal
        resistance_count = sum(1 for move in lead_recent_moves if move in ["rejection", "objection", "deflection"])
        if resistance_count >= 3:
            directive = (
                "HIGH RESISTANCE — lead showing repeated disinterest. "
                "Acknowledge, provide value opt-in, and disengage. No questions. "
                "Set stage to RESISTANCE for prompt override."
            )
            framework = "DISENGAGE"
            logic.stage = ConversationStage.RESISTANCE  # Override stage to force prompt handling
            logger.warning(f"ANTI-LOOP TRIGGERED | contact={contact_id} | reason=resistance_count={resistance_count}")
    else:
        # No significant question history → no loop risk, skip checks
        pass
    return {
        "profile_str": profile_str,
        "tactical_narrative": f"STRATEGY: {framework}\nTACTICAL ORDER: {directive}",
        "stage": logic.stage.value,
        "underwriting_context": underwriting_ctx,
        "company_context": company_ctx,
        "known_facts": known_facts,
        "story_narrative": story_narrative,
        "recent_exchanges": recent_exchanges
    }