# conversation_engine.py - The Logic Signal Processor (Left Brain)
# "It's not about what you asked. It's about what they answered."
import logging
from enum import Enum
from dataclasses import dataclass
from typing import List
logger = logging.getLogger(__name__)
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
    Analyzes the 'Mechanics' of the conversation based on the LEAD'S RESPONSE,
    with subtext inference and loop breaking to avoid rigid stage locking.
    """
    if not recent_exchanges:
        return LogicSignal(ConversationStage.INITIAL_OUTREACH, "none", False, 0, 0, False)

    # 1. Extract Context
    lead_msgs = [m for m in recent_exchanges if m['role'] == 'lead']
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    
    last_lead_text = lead_msgs[-1]['text'].lower() if lead_msgs else ""
    last_bot_text = bot_msgs[-1]['text'].lower() if bot_msgs else ""
    
    # 2. Response Depth and Subtext Inference
    words = last_lead_text.split()
    depth_score = min(len(words), 5)  # cleaner cap

    subtext_score = 0
    if not last_lead_text.strip():           # empty/minimal
        subtext_score = -1                   # infer disinterest → advance faster
    elif len(words) < 3:                     # very short reply
        subtext_score = 1                    # infer impatience → signal to move on

    # 3. Loop Detection (broadened slightly for safety)
    recent_bot_questions = [m['text'].lower() for m in bot_msgs[-4:] if '?' in m['text']]
    is_looping = (
        len(recent_bot_questions) >= 2 and
        any(word in " ".join(recent_bot_questions) for word in [
            "worry", "concern", "afraid", "scared", "happen if", "impact", "what happens", "how would"
        ])
    )

    # 4. Analyze Lead's Move (unchanged — your classification is solid)
    move_type = "statement"
    voss_no_signal = False
    gap_signal = False
    pain_score = 0
    
    if any(p in last_lead_text for p in CRITICAL_PAIN_PATTERNS):
        pain_score = 3
        gap_signal = True
        move_type = "pain_admission"
    elif any(p in last_lead_text for p in SOFT_PAIN_PATTERNS):
        pain_score = 1
        gap_signal = True
        move_type = "pain_admission"

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

    # 5. Determine Conversation Stage (Gemini fix applied — fallback at end)
    stage = ConversationStage.DISCOVERY  # true default

    if move_type == "rejection":
        stage = ConversationStage.OBJECTION_HANDLING
    elif move_type == "objection" or move_type == "deflection":
        stage = ConversationStage.OBJECTION_HANDLING
    elif move_type == "agreement" or any(k in last_lead_text for k in ["time", "call", "appointment"]):
        stage = ConversationStage.CLOSING
    
    elif is_looping:
        stage = ConversationStage.RESISTANCE  # force empathy pivot
    
    elif (gap_signal or pain_score > 0 or (depth_score > 1 and subtext_score >= 0)) and not is_looping:
        stage = ConversationStage.CONSEQUENCE
    
    elif subtext_score < 0: 
        stage = ConversationStage.RESISTANCE  # safer than direct CLOSING on silent leads
    
    # Final fallback: no real engagement yet → outreach
    if not lead_msgs or len(lead_msgs) == 0:
        stage = ConversationStage.INITIAL_OUTREACH

    # Optional debug log (uncomment when testing)
    contact_id = recent_exchanges[0].get('contact_id', 'unknown') if recent_exchanges else 'unknown'
    logger.debug(
        f"analyze_logic_flow | contact_id=unknown | "  # add contact_id if passed in future
        f"stage={stage.value} | move={move_type} | "
        f"pain={pain_score} | depth={depth_score} | subtext={subtext_score} | "
        f"looping={is_looping} | lead_msgs_count={len(lead_msgs)} | "
        f"last_lead_text='{last_lead_text[:50]}...'"
    )

    return LogicSignal(stage, move_type, gap_signal, pain_score, depth_score, voss_no_signal)