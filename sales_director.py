# sales_director.py - The Executive Sales Brain (2026)
# "Give a man a script, he closes one deal. Teach him the framework, he closes forever."

from conversation_engine import analyze_logic_flow, LogicSignal, ConversationStage
from individual_profile import build_comprehensive_profile
from underwriting import get_underwriting_context
from insurance_companies import get_company_context, find_company_in_message, normalize_company_name
from memory import get_recent_messages, get_known_facts, get_narrative, run_narrative_observer

def generate_strategic_directive(contact_id: str, message: str, first_name: str, age: str, address: str) -> dict:
    """
    The Master Function.
    Synthesizes data to break loops and drive the 'Appointment Close'.
    """
    
    # 1. GATHER INTELLIGENCE
    run_narrative_observer(contact_id, message)
    recent_exchanges = get_recent_messages(contact_id, limit=10)
    story_narrative = get_narrative(contact_id)
    known_facts = get_known_facts(contact_id)
    
    # 2. PROCESS HEMISPHERES
    logic: LogicSignal = analyze_logic_flow(recent_exchanges)
    profile_str, profile_ctx = build_comprehensive_profile(story_narrative, known_facts, first_name, age, address)
    
    # Underwriting
    underwriting_ctx = ""
    if "health" in message.lower() or "medic" in message.lower() or profile_ctx.get("health_issues"):
        underwriting_ctx = get_underwriting_context(message)
    
    # Competitor Check
    company_ctx = ""
    raw_company = find_company_in_message(message)
    if raw_company:
        normalized = normalize_company_name(raw_company)
        if normalized:
            company_ctx = get_company_context(normalized)

    # 3. LOOP DETECTION & PAIN ANALYSIS
    bot_last_moves = [m['text'] for m in recent_exchanges if m['role'] == 'assistant'][-3:]
    loop_detected = any("worry" in m.lower() or "happen" in m.lower() or "gap" in m.lower() for m in bot_last_moves)
    
    pain_keywords = ["homeless", "street", "lose the house", "devastated", "nothing left", "broke", "can't afford", "die"]
    pain_confirmed = any(pk in message.lower() for pk in pain_keywords)

    # 4. EXECUTIVE SYNTHESIS (STRATEGY SELECTION)
    directive = ""
    framework = "NEPQ"
    
    # --- SCENARIO A: EMERGENCY BREAK (Pain Confirmed) ---
    if pain_confirmed:
        directive = (
            "CRITICAL PAIN ADMITTED. STOP DISCOVERY.\n"
            "OBJECTIVE: Book the Appointment immediately to solve the pain.\n"
            "TACTIC: 'Compassionate Close' (NEPQ).\n"
            "DEFINITION: 1. Validate the emotion gently (e.g., 'That is a heavy weight to carry'). "
            "2. Pivot immediately to the appointment as the specific relief to that pain. "
            "Do NOT ask 'if' they want to meet. Ask 'when'."
        )
        framework = "COMPASSIONATE CLOSE"

    # --- SCENARIO B: LOOP BREAKER ---
    elif loop_detected and logic.stage == ConversationStage.CONSEQUENCE:
        directive = (
            "LOOP DETECTED. You are stuck in 'Problem Awareness'.\n"
            "OBJECTIVE: Force a transition to the Solution (Appointment).\n"
            "TACTIC: 'Transition Question' (NEPQ).\n"
            "DEFINITION: A question that asks the lead if they are open to seeing how we can specifically eliminate [The Problem] we just discussed. "
            "If they say yes, offer times immediately."
        )
        framework = "NEPQ (Transition)"

    # --- SCENARIO C: VOSS PARADOX (Agreement via No) ---
    elif logic.voss_no_signal:
        directive = (
            "SUCCESS: Lead gave a 'Protective No' (e.g., 'No, I'm not opposed'). This is total agreement.\n"
            "OBJECTIVE: Lock in the time.\n"
            "TACTIC: 'Calibrated How' (Chris Voss).\n"
            "DEFINITION: Ask a 'How' or 'What' question that presumes the appointment is happening. "
            "Example logic: 'How does [Time 1] fit with your calendar?' or 'What is the best way to get this on the books?'"
        )
        framework = "CHRIS VOSS (Closing)"
        
    # --- SCENARIO D: HIGH RESISTANCE / SKEPTICISM ---
    elif profile_ctx.get("is_skeptical") and logic.stage != ConversationStage.CLOSING:
        directive = (
            "HIGH RESISTANCE DETECTED. Do not pitch. Do not close.\n"
            "OBJECTIVE: Lower their guard.\n"
            "TACTIC: 'Accusation Audit' (Chris Voss).\n"
            "DEFINITION: List the worst things they are likely thinking about you (e.g., 'pushy', 'just wants a commission') "
            "and say them out loud first to disarm the amygdala. Follow with a Label."
        )
        framework = "CHRIS VOSS (De-escalation)"
        
    # --- SCENARIO E: DEFLECTION (Straight Line) ---
    elif logic.last_move_type == "deflection":
        directive = (
            "DEFLECTION DETECTED. The lead is avoiding the question to control the frame.\n"
            "OBJECTIVE: Regain control and move toward the Appointment.\n"
            "TACTIC: 'Straight Line Loop'.\n"
            "DEFINITION: 1. Acknowledge their comment briefly (Deflect the deflection). "
            "2. Pivot immediately back to the 'Intelligence Gathering' question you asked previously. "
            "Keep the line straight toward the goal."
        )
        framework = "STRAIGHT LINE (Looping)"
        
    # --- SCENARIO F: "NOT INTERESTED" (Smokescreen) ---
    elif logic.last_move_type == "objection":
        directive = (
            "OBJECTION DETECTED. They said 'No' but haven't given a reason.\n"
            "OBJECTIVE: Crack the smokescreen.\n"
            "TACTIC: 'Provoking Question' (Gap Selling).\n"
            "DEFINITION: A question that challenges the lead's premise that they 'don't need this' without being aggressive. "
            "Highlight the risk of their current status quo to provoke a realization."
        )
        framework = "GAP SELLING (Provoking)"

    # --- SCENARIO G: GAP DISCOVERY (Standard Flow) ---
    elif logic.gap_signal or (logic.stage == ConversationStage.CONSEQUENCE):
        directive = (
            "PAIN POINT IDENTIFIED.\n"
            "OBJECTIVE: Make the pain real so they WANT the appointment.\n"
            "TACTIC: 'Consequence Question' (NEPQ).\n"
            "DEFINITION: A question that forces the lead to verbalize the *personal* or *financial* ramifications "
            "of NOT solving the problem. Do not focus on the problem itself, but the *impact* of the problem on their family."
        )
        framework = "NEPQ (Consequence)"
        
    # --- SCENARIO H: CLOSING STAGE (THE APPOINTMENT) ---
    elif logic.stage == ConversationStage.CLOSING:
        directive = (
            "GREEN LIGHT. The Gap is established.\n"
            "OBJECTIVE: Book the Appointment. (The Appointment IS the Close).\n"
            "TACTIC: 'Double Bind' (Ericksonian Hypnosis / Straight Line).\n"
            "DEFINITION: Offer two specific time slots. "
            "Frame the choice not as 'Yes/No' to the meeting, but 'A or B' for the time. "
            "Assume the sale."
        )
        framework = "ASSUMPTIVE CLOSE"
        
    # --- SCENARIO I: DEFAULT DISCOVERY ---
    else:
        directive = (
            "DISCOVERY MODE.\n"
            "OBJECTIVE: Find the Gap.\n"
            "TACTIC: 'Situation Questions' (NEPQ) or 'Current State Probes' (Gap Selling).\n"
            "DEFINITION: Questions designed to extract 'Literal and Physical Facts' about their current situation (Current Coverage, Family Structure). "
            "Avoid emotional questions for now; focus on data gathering."
        )
        framework = "NEPQ (Situation)"

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