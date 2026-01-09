# sales_director.py - The Executive Sales Brain (2026)
from conversation_engine import analyze_logic_flow, LogicSignal, ConversationStage
from individual_profile import build_comprehensive_profile
from underwriting import get_underwriting_context
from memory import get_recent_messages, get_known_facts, get_narrative, run_narrative_observer

def generate_strategic_directive(contact_id: str, message: str, first_name: str, age: str, address: str) -> dict:
    """
    The Master Function.
    1. Aggregates all data.
    2. Runs Logic Engine (Left Brain).
    3. Runs Profile Engine (Right Brain).
    4. Synthesizes a 'Strategic Directive' for the Prompt.
    """
    
    # 1. GATHER INTELLIGENCE
    # Critical: Run Observer FIRST to update the "Story"
    run_narrative_observer(contact_id, message) 
    
    recent_exchanges = get_recent_messages(contact_id, limit=10)
    story_narrative = get_narrative(contact_id)
    known_facts = get_known_facts(contact_id)
    
    # 2. PROCESS HEMISPHERES
    logic: LogicSignal = analyze_logic_flow(recent_exchanges)
    
    profile_str, profile_ctx = build_comprehensive_profile(
        story_narrative, known_facts, first_name, age, address
    )
    
    # Only pull underwriting if health is actually mentioned
    underwriting_ctx = ""
    if "health" in message.lower() or "medic" in message.lower() or profile_ctx["health_issues"]:
        underwriting_ctx = get_underwriting_context(message)
    
    # 3. EXECUTIVE SYNTHESIS
    directive = ""
    framework = "NEPQ"
    
    # --- VOSS PARADOX (Highest Success) ---
    if logic.voss_no_signal:
        directive = "SUCCESS: Lead gave a 'Protective No' (Agreement). Proceed immediately to booking."
        framework = "CHRIS VOSS (Closing)"
        
    # --- OBJECTION: 'NOT INTERESTED' / 'I'M GOOD' ---
    # This is where the Salesperson Mindset kicks in.
    elif logic.last_move_type == "objection":
        if profile_ctx["is_skeptical"]:
            directive = "SKEPTICAL RESISTANCE. Do not fight. Use a Voss Label: 'It seems like you've been pestered by agents before.' Then soft pivot."
            framework = "CHRIS VOSS (De-escalation)"
        else:
            directive = "SMOKESCREEN DETECTED. They said 'Not Interested' but haven't given a reason. Use NEPQ Curiosity: 'Fair enough. Was it just bad timing, or did you not see anything you liked last time?' Do NOT accept the 'No'."
            framework = "NEPQ (Curiosity Probe)"

    # --- OBJECTION: DEFLECTION ---
    elif logic.last_move_type == "deflection":
        directive = "DEFLECTION. Lead is trying to take control. Answer briefly, then LOOP BACK to your previous question. Do not get lost in their questions."
        framework = "STRAIGHT LINE (Looping)"
        
    # --- GAP DISCOVERY ---
    elif logic.gap_signal or (logic.stage == ConversationStage.CONSEQUENCE):
        directive = "PAIN POINT FOUND. Use NEPQ Consequence Questions. 'What happens to [Family/House] if you don't fix this?' Make them feel the gap."
        framework = "NEPQ (Consequence)"
        
    # --- CLOSING ---
    elif logic.stage == ConversationStage.CLOSING:
        directive = "GREEN LIGHT. Offer 2 specific times immediately. 'Would 2pm or 4pm be a bad idea?'"
        framework = "ASSUMPTIVE CLOSE"
        
    # --- DEFAULT DISCOVERY ---
    else:
        directive = "CONTINUE DISCOVERY. We have not found the 'Gap' yet. Ask a probing question about Current State vs Future State."
        framework = "NEPQ (Discovery)"

    return {
        "profile_str": profile_str,
        "tactical_narrative": f"STRATEGY: {framework}\nORDER: {directive}",
        "stage": logic.stage.value,
        "underwriting_context": underwriting_ctx,
        "known_facts": known_facts,
        "recent_exchanges": recent_exchanges
    }