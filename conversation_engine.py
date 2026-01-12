# conversation_engine.py - The Logic Signal Processor (Left Brain)
# "It's not about what you asked. It's about what they answered."

from enum import Enum
from dataclasses import dataclass
from typing import List

class ConversationStage(Enum):
    INITIAL_OUTREACH = "initial_outreach"
    DISCOVERY = "discovery"          # NEPQ Situation / Gap Selling Current State
    CONSEQUENCE = "consequence"      # NEPQ Consequence / Gap Selling Pain
    RESISTANCE = "resistance"        # Chris Voss (Skepticism/Trust Issues)
    OBJECTION = "objection"          # Straight Line (Smokescreens)
    CLOSING = "closing"              # Booking the appointment

@dataclass
class LogicSignal:
    stage: ConversationStage
    last_move_type: str   # "deflection", "agreement", "rejection", "question", "statement", "pain_admission"
    gap_signal: bool      # True if they admitted a problem
    pain_score: int       # 0-3 (Intensity of pain words)
    depth_score: int      # 0-5 (Quality/Length of their answer)
    voss_no_signal: bool  # True if they answered "No" to a "No-Oriented Question"

# === PATTERN LIBRARIES ===

# 1. CRITICAL PAIN (Gap Selling - The "Black Hole" Problems)
CRITICAL_PAIN_PATTERNS = [
    "homeless", "lose the house", "lose my home", "street", "devastated", 
    "nothing left", "broke", "die", "death", "burial", "burden", "bankruptcy"
]

# 2. SOFT PAIN (Gap Selling - The "Worry" Problems)
SOFT_PAIN_PATTERNS = [
    "worried", "concerned", "afraid", "scared", "expire", "lapsing", 
    "too expensive", "can't afford", "fixed income", "debt", "mortgage", 
    "spouse", "kids", "family", "no coverage", "gap"
]

# 3. DEFLECTION (Straight Line - The "Off-Track" Moves)
DEFLECTION_PATTERNS = [
    "how much", "cost", "price", "quote", "send info", "email me", 
    "send me something", "is this free", "just tell me"
]

# 4. NO-ORIENTED TRIGGERS (Chris Voss)
NO_ORIENTED_PATTERNS = [
    "opposed", "ridiculous", "bad idea", "give up", "too much", "impossible", "deferred"
]

# 5. HARD OUTS (DNC)
DNC_PATTERNS = ["stop", "remove", "unsubscribe", "don't call", "wrong person", "cease"]

# 6. SOFT OUTS (Objections)
OBJECTION_PATTERNS = [
    "not interested", "no thanks", "im good", "i'm good", "all set", 
    "have insurance", "busy", "later", "pass"
]

def analyze_logic_flow(recent_exchanges: List[dict]) -> LogicSignal:
    """
    Analyzes the 'Mechanics' of the conversation based on the LEAD'S RESPONSE.
    """
    if not recent_exchanges:
        return LogicSignal(ConversationStage.INITIAL_OUTREACH, "none", False, 0, 0, False)

    # 1. Extract Context
    lead_msgs = [m for m in recent_exchanges if m['role'] == 'lead']
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    
    last_lead_text = lead_msgs[-1]['text'].lower() if lead_msgs else ""
    last_bot_text = bot_msgs[-1]['text'].lower() if bot_msgs else ""
    
    # 2. Calculate Response Depth (Did they actually answer?)
    # A short answer ("idk", "maybe") is low depth. A sentence is high depth.
    words = last_lead_text.split()
    depth_score = len(words)
    if depth_score > 5: depth_score = 5 # Cap at 5

    # 3. Analyze Lead's Move
    move_type = "statement"
    voss_no_signal = False
    gap_signal = False
    pain_score = 0
    
    # --- STEP A: Pain Calculation ---
    if any(p in last_lead_text for p in CRITICAL_PAIN_PATTERNS):
        pain_score = 3 # Critical
        gap_signal = True
        move_type = "pain_admission"
    elif any(p in last_lead_text for p in SOFT_PAIN_PATTERNS):
        pain_score = 1 # Moderate
        gap_signal = True
        move_type = "pain_admission"

    # --- STEP B: Move Classification ---
    
    # Check for Voss Agreement ("No" to "Are you opposed?")
    bot_asked_no_oriented = any(p in last_bot_text for p in NO_ORIENTED_PATTERNS) and "?" in last_bot_text
    
    if any(x in last_lead_text for x in DNC_PATTERNS):
        move_type = "rejection" 

    elif bot_asked_no_oriented and ("no" in last_lead_text or "not " in last_lead_text):
        move_type = "agreement"
        voss_no_signal = True
        
    elif any(x in last_lead_text for x in OBJECTION_PATTERNS):
        move_type = "objection"

    elif "?" in last_lead_text and any(p in last_lead_text for p in DEFLECTION_PATTERNS):
        move_type = "deflection"
        
    elif any(x in last_lead_text for x in ["yes", "sure", "ok", "sounds good", "book", "schedule"]):
        move_type = "agreement"

    # 4. Determine Conversation Stage
    # Logic: The stage is determined by the QUALITY of the interaction, not just the count.
    
    stage = ConversationStage.DISCOVERY # Default
    
    if move_type == "rejection":
        stage = ConversationStage.OBJECTION_HANDLING
        
    elif move_type == "objection" or move_type == "deflection":
        stage = ConversationStage.OBJECTION_HANDLING
        
    elif move_type == "agreement" or any(k in last_lead_text for k in ["time", "call", "appointment"]):
        stage = ConversationStage.CLOSING
        
    # CRITICAL: Only move to CONSEQUENCE stage if they admitted pain OR gave a thoughtful answer to a gap question
    elif (gap_signal or pain_score > 0):
        stage = ConversationStage.CONSEQUENCE
        
    elif not lead_msgs: 
        stage = ConversationStage.INITIAL_OUTREACH

    return LogicSignal(stage, move_type, gap_signal, pain_score, depth_score, voss_no_signal)