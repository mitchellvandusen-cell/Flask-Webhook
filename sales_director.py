# sales_director.py - The Executive Sales Brain (2026)
# "Give the bot a script, it parrots. Give it a theory, it persuades."

from conversation_engine import analyze_logic_flow, LogicSignal, ConversationStage
from individual_profile import build_comprehensive_profile
from underwriting import get_underwriting_context
from insurance_companies import get_company_context, find_company_in_message, normalize_company_name
from memory import get_recent_messages, get_known_facts, get_narrative, run_narrative_observer

def generate_strategic_directive(contact_id: str, message: str, first_name: str, age: str, address: str) -> dict:
    
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

    # 3. CONTEXTUAL ANALYSIS
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    last_bot_text = bot_msgs[-1]['text'].lower() if bot_msgs else ""
    just_asked_consequence = any(x in last_bot_text for x in ["happen", "worry", "concern", "impact", "leave them"])
    
    # 4. EXECUTIVE SYNTHESIS (THEORY SELECTION)
    directive = ""
    framework = "NEPQ"
    
    # --- LEVEL 1: IMMEDIATE CLOSING TRIGGERS ---
    
    # SCENARIO A: HIGH PAIN VALIDATED
    if logic.pain_score >= 2:
        directive = (
            "CRITICAL PAIN ADMITTED. STOP DISCOVERY.\n"
            "OBJECTIVE: Book the Appointment as the only logical relief to their pain.\n"
            "THEORY: 'Compassionate Prescription' (NEPQ/Gap Selling).\n"
            "DEFINITION: Acknowledge the emotional weight of their admission using a 'Label' (Voss). "
            "Then, prescribe the appointment not as a sales call, but as a necessary 'triage' step to fix the specific problem they just admitted. "
            "Do not ask IF they want to meet; tell them WHEN."
        )
        framework = "COMPASSIONATE CLOSE"

    # SCENARIO B: SUCCESSFUL CONSEQUENCE (Transition)
    elif just_asked_consequence and logic.depth_score > 2:
        directive = (
            "GAP INTERNALIZED. The lead has visualized the negative future.\n"
            "OBJECTIVE: Pivot from Problem Awareness to Solution Awareness.\n"
            "THEORY: 'The Bridge Question' (NEPQ).\n"
            "DEFINITION: Construct a question that asks for consent to discuss the solution. "
            "Link their admitted 'Consequence' directly to the 'New Opportunity' (the appointment). "
            "Psychological Goal: Move them from 'Dread' to 'Hope' without being pushy."
        )
        framework = "NEPQ (Transition)"

    # SCENARIO C: WEAK ANSWER (The Probe)
    elif just_asked_consequence and logic.depth_score <= 2:
        directive = (
            "SURFACE LEVEL ANSWER. They are answering logically, not emotionally.\n"
            "OBJECTIVE: Force them to visualize the reality.\n"
            "THEORY: 'Clarifying Probe' (Gap Selling).\n"
            "DEFINITION: Ask a question that demands specificity. Challenge their vague answer by asking 'What does that look like specifically?' or 'How would that physically affect [Family Member]?' "
            "Psychological Goal: Move them from Intellectual understanding to Emotional realization."
        )
        framework = "GAP SELLING (Probe)"

    # SCENARIO D: VOSS AGREEMENT
    elif logic.voss_no_signal:
        directive = (
            "AGREEMENT SIGNAL RECEIVED.\n"
            "OBJECTIVE: Secure the commitment without triggering 'Buyer's Remorse'.\n"
            "THEORY: 'Illusion of Control' (Chris Voss).\n"
            "DEFINITION: Ask a 'How' or 'What' question regarding the logistics of the meeting (e.g., timing). "
            "This forces the lead to expend mental energy solving *your* scheduling problem, implicitly accepting the premise that the meeting is happening."
        )
        framework = "CHRIS VOSS (Closing)"

    # SCENARIO E: CLOSING STAGE
    elif logic.stage == ConversationStage.CLOSING:
        directive = (
            "GREEN LIGHT.\n"
            "OBJECTIVE: Finalize the time slot.\n"
            "THEORY: 'The Double Bind' (Ericksonian Hypnosis / Straight Line).\n"
            "DEFINITION: Present two distinct options (A or B) that both result in the desired outcome (The Appointment). "
            "This bypasses the 'Yes/No' decision center of the brain and engages the 'Selection' center."
        )
        framework = "ASSUMPTIVE CLOSE"

    # --- LEVEL 2: OBJECTION HANDLING ---

    elif logic.last_move_type == "deflection":
        directive = (
            "DEFLECTION DETECTED.\n"
            "OBJECTIVE: Maintain High Status and regain frame control.\n"
            "THEORY: 'Deflect & Pivot' (Straight Line).\n"
            "DEFINITION: Briefly acknowledge their statement to satisfy social norms (The Deflect), "
            "then immediately ask a totally unrelated Intelligence Gathering question to pull them back to your line (The Pivot). "
            "Do not justify or explain yourself."
        )
        framework = "STRAIGHT LINE (Looping)"

    # --- LEVEL 3: DISCOVERY ---

    elif logic.gap_signal:
        directive = (
            "GAP DETECTED.\n"
            "OBJECTIVE: Future Pace the Pain.\n"
            "THEORY: 'Consequence Question' (NEPQ).\n"
            "DEFINITION: Ask a question that forces the lead to simulate a future where this problem remains unsolved. "
            "Focus on the *Ramifications* of the problem, not the problem itself. Who else is affected? What financial ruin occurs?"
        )
        framework = "NEPQ (Consequence)"

    else:
        directive = (
            "DISCOVERY MODE.\n"
            "OBJECTIVE: Establish the 'Current State'.\n"
            "THEORY: 'Situation Question' (NEPQ).\n"
            "DEFINITION: Ask a specific, neutral question about their current setup to gather objective data. "
            "This builds the 'Baseline' against which you will later contrast the 'Gap'. Avoid emotional triggers yet."
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