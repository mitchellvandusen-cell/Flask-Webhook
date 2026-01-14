# sales_director.py - SaaS Strategy Director
from conversation_engine import analyze_logic_flow, ConversationStage
from individual_profile import build_comprehensive_profile

def generate_strategic_directive(contact_id, message, story_narrative, known_facts):
    logic = analyze_logic_flow([{'role': 'lead', 'text': message}])
    profile_narrative, profile = build_comprehensive_profile(story_narrative, known_facts)
    
    directive = ""
    framework = "NEPQ"
    recommended_plan = "Individual" # Default

    # --- PLAN LOGIC ---
    if profile['role'] == "agency_owner":
        if profile['lead_volume'] == "high":
            recommended_plan = "Agency Pro (Enterprise)"
            value_prop = "Whitelabeling + Unlimited Sub-Accounts"
        else:
            recommended_plan = "Agency Starter"
            value_prop = "10 Sub-Accounts for your team"
    else:
        recommended_plan = "Individual Plan"
        value_prop = "Personal AI Setter"

    # --- STRATEGY ---
    if logic.stage == ConversationStage.CLOSING:
        directive = f"BUYER SIGNAL. Stop selling. Recommend the {recommended_plan} because {value_prop}. Push for Checkout Link."
        framework = "ASSUMPTIVE CLOSE"
    
    elif logic.last_move_type == "deflection" or "price" in message.lower():
        directive = f"PRICE PIVOT. Acknowledge price concern, but anchor it against the cost of 'Dead Leads'. Then pitch {recommended_plan}."
        framework = "VALUE ANCHORING"

    elif logic.gap_signal:
        directive = "PAIN FOUND. Agitate the cost of missed appointments. Contrast 'Drip Campaigns' (Old Way) vs 'AI Conversation' (New Way)."
        framework = "GAP SELLING"
    
    else:
        directive = "DISCOVERY. Find out if they are an Individual or Agency. Ask about their current lead follow-up process."

    return {
        "tactical_narrative": f"STRATEGY: {framework}\nRECOMMENDATION: {recommended_plan}\nDIRECTIVE: {directive}",
        "stage": logic.stage.value,
        "profile": profile
    }