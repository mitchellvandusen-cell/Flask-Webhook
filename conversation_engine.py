# conversation_engine.py - The Logic Signal Processor (Left Brain)
from enum import Enum
from dataclasses import dataclass
from typing import List

class ConversationStage(Enum):
    INITIAL_OUTREACH = "initial_outreach"
    DISCOVERY = "discovery"
    CONSEQUENCE = "consequence"
    OBJECTION_HANDLING = "objection"
    QUALIFICATION = "qualification"
    CLOSING = "closing"

@dataclass
class LogicSignal:
    stage: ConversationStage
    last_move_type: str  # "deflection", "agreement", "rejection", "question", "statement", "objection"
    gap_signal: bool     # True if they admitted a problem/pain
    voss_no_signal: bool # True if they answered "No" to a "No-Oriented Question"

# === PATTERN LIBRARIES ===
NO_ORIENTED_PATTERNS = ["opposed", "ridiculous", "bad idea", "give up", "too much", "impossible"]
DEFLECTION_PATTERNS = ["how much", "cost", "price", "send info", "email me", "who are you", "is this free"]
PROBLEM_INDICATORS = ["worried", "concerned", "afraid", "mortgage", "kids", "spouse", "debt", "expire", "no coverage"]

# "Hard Out" triggers (The only things that stop us)
DNC_PATTERNS = ["stop", "remove", "unsubscribe", "don't call", "wrong person", "spam", "cease"]

# "Soft Out" triggers (The start of the sale)
OBJECTION_PATTERNS = ["not interested", "no thanks", "im good", "i'm good", "all set", "have insurance", "busy"]

def analyze_logic_flow(recent_exchanges: List[dict]) -> LogicSignal:
    """
    Pure logic analysis. Detects the mechanics of the conversation.
    """
    if not recent_exchanges:
        return LogicSignal(ConversationStage.INITIAL_OUTREACH, "none", False, False)

    # 1. Extract Raw Text
    lead_msgs = [m for m in recent_exchanges if m['role'] == 'lead']
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    
    last_lead_text = lead_msgs[-1]['text'].lower() if lead_msgs else ""
    last_bot_text = bot_msgs[-1]['text'].lower() if bot_msgs else ""
    
    # 2. Detect Bot's Last Move
    bot_asked_no_oriented = any(p in last_bot_text for p in NO_ORIENTED_PATTERNS) and "?" in last_bot_text

    # 3. Detect Lead's Last Move
    move_type = "statement"
    voss_no_signal = False
    
    # PRIORITY 1: Check for DNC (Hard Stop)
    if any(x in last_lead_text for x in DNC_PATTERNS):
        move_type = "rejection" # Only true rejection

    # PRIORITY 2: Check for Voss Agreement ("No" to "Are you opposed?")
    elif bot_asked_no_oriented and ("no" in last_lead_text or "not " in last_lead_text or "nope" in last_lead_text):
        move_type = "agreement"
        voss_no_signal = True
        
    # PRIORITY 3: Check for Soft Objections ("Not Interested")
    elif any(x in last_lead_text for x in OBJECTION_PATTERNS):
        move_type = "objection"

    # PRIORITY 4: Check for Deflection (Answering a question with a question)
    elif "?" in last_lead_text and any(p in last_lead_text for p in DEFLECTION_PATTERNS):
        move_type = "deflection"
        
    # PRIORITY 5: Check for Explicit Agreement
    elif any(x in last_lead_text for x in ["yes", "sure", "ok", "sounds good", "book", "schedule"]):
        move_type = "agreement"

    # 4. Detect Gap Signal
    gap_signal = any(p in last_lead_text for p in PROBLEM_INDICATORS)

    # 5. Determine Stage
    stage = ConversationStage.DISCOVERY # Default
    
    if move_type == "rejection":
        stage = ConversationStage.OBJECTION_HANDLING # Actually an exit, but handled here logic-wise
    elif move_type == "objection" or move_type == "deflection":
        stage = ConversationStage.OBJECTION_HANDLING
    elif move_type == "agreement" or any(k in last_lead_text for k in ["time", "call", "appointment"]):
        stage = ConversationStage.CLOSING
    elif gap_signal:
        stage = ConversationStage.CONSEQUENCE
    elif not lead_msgs: 
        stage = ConversationStage.INITIAL_OUTREACH

    return LogicSignal(stage, move_type, gap_signal, voss_no_signal)