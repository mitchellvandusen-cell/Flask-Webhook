# sales_director.py - The Executive Sales Brain (2026)
from conversation_engine import analyze_logic_flow, LogicSignal, ConversationStage
from individual_profile import build_comprehensive_profile
from underwriting import get_underwriting_context
from memory import get_recent_messages, get_known_facts, get_narrative, run_narrative_observer

def generate_strategic_directive(contact_id: str, message: str, first_name: str, age: str, address: str) -> dict:
    """
    The Master Function.
    1. Aggregates all data (History, Narrative, Facts).
    2. Runs Logic Engine (Left Brain).
    3. Runs Profile Engine (Right Brain).
    4. Synthesizes a 'Strategic Directive'.
    """
    
    # 1. GATHER INTELLIGENCE (The Senses)
    # Run Observer first to ensure narrative is fresh
    run_narrative_observer(contact_id, message)
    
    # Fetch Data ONCE
    recent_exchanges = get_recent_messages(contact_id, limit=10)
    story_narrative = get_narrative(contact_id)
    known_facts = get_known_facts(contact_id)
    
    # 2. PROCESS HEMISPHERES
    # Left Brain (Logic)
    logic: LogicSignal = analyze_logic_flow(recent_exchanges)
    
    # Right Brain (Context & Profile)
    profile_str, profile_ctx = build_comprehensive_profile(
        story_narrative, known_facts, first_name, age, address
    )
    
    # Underwriting (Technical Brain)
    # Only run if health is relevant to save processing
    underwriting_ctx = ""
    if "health" in message.lower() or "medic" in message.lower() or profile_ctx.get("health_issues"):
        underwriting_ctx = get_underwriting_context(message)
    
    # 3. EXECUTIVE SYNTHESIS
    directive = ""
    framework = "NEPQ"
    
    # VOSS PARADOX (Highest Priority)
    if logic.voss_no_signal:
        directive = "SUCCESS: Lead gave a 'Protective No' (Agreement). Proceed immediately to booking."
        framework = "CHRIS VOSS (Closing)"
        
    # RESISTANCE / SKEPTICISM
    elif profile_ctx.get("is_skeptical") and logic.stage != ConversationStage.CLOSING:
        directive = "HIGH RESISTANCE. Do not pitch. Use a Chris Voss 'Accusation Audit' (e.g., 'You probably think I'm just another salesperson'). Disarm them first."
        framework = "CHRIS VOSS (De-escalation)"
        
    # DEFLECTION
    elif logic.last_move_type == "deflection":
        directive = "DEFLECTION. Answer briefly, then LOOP BACK to your previous question. Do not get lost in their interrogation."
        framework = "STRAIGHT LINE (Looping)"
        
    # NOT INTERESTED (The Hunter Mindset)
    elif logic.last_move_type == "objection":
        directive = "SMOKESCREEN. They said 'Not Interested' but haven't given a reason. Use NEPQ Curiosity: 'Fair enough. Was it just bad timing?' Do NOT accept the 'No'."
        framework = "NEPQ (Curiosity Probe)"

    # GAP DISCOVERY
    elif logic.gap_signal or (logic.stage == ConversationStage.CONSEQUENCE):
        directive = "PAIN POINT FOUND. Use NEPQ Consequence Questions. 'What happens to [Family] if you don't fix this?'"
        framework = "NEPQ (Consequence)"
        
    # CLOSING
    elif logic.stage == ConversationStage.CLOSING:
        directive = "GREEN LIGHT. Offer 2 specific times immediately. 'Would 2pm or 4pm be a bad idea?'"
        framework = "ASSUMPTIVE CLOSE"
        
    # DEFAULT
    else:
        directive = "CONTINUE DISCOVERY. Ask a probing question about Current State vs Future State."
        framework = "NEPQ (Discovery)"

    # 4. RETURN THE COMPLETE PACKAGE
    return {
        "profile_str": profile_str,
        "tactical_narrative": f"STRATEGY: {framework}\nORDER: {directive}",
        "stage": logic.stage.value,
        "underwriting_context": underwriting_ctx,
        "known_facts": known_facts,       # Passed back so main.py doesn't need to fetch
        "story_narrative": story_narrative, # Passed back so main.py doesn't need to fetch
        "recent_exchanges": recent_exchanges
    }