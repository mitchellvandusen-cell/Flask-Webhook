# conversation_engine.py - SaaS Sales Logic Processor
import logging
import difflib
from enum import Enum
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

class ConversationStage(Enum):
    INITIAL_OUTREACH = "initial_outreach"
    DISCOVERY = "discovery"          # Finding the "broken" sales process
    CONSEQUENCE = "consequence"      # The cost of dead leads / wasted time
    RESISTANCE = "resistance"        # Skepticism about AI/Bots
    OBJECTION_HANDLING = "objection" # Price / "I have a bot already"
    CLOSING = "closing"              # Push to Plan Recommendation

@dataclass
class LogicSignal:
    stage: ConversationStage
    last_move_type: str   
    gap_signal: bool      
    pain_score: int       
    depth_score: int      
    voss_no_signal: bool  

# ==========================================
# === SAAS SALES PATTERN LIBRARIES ===
# ==========================================

# 1. CRITICAL PAIN (The "Bleeding" Problems)
CRITICAL_PAIN_PATTERNS = [
    # --- LEAD WASTAGE ---
    "leads are dead", "not answering", "ghosting me", "no one picks up",
    "contact rate is terrible", "low response rate", "waste of money",
    "burning cash on ads", "leads are garbage", "old leads", "dusty leads",
    "can't get a hold of anyone", "calling is a waste of time",
    
    # --- SMS / TECH FAILURE ---
    "sms going to spam", "texts getting blocked", "carrier violations",
    "drip campaigns dont work", "people hate my texts", "stop replies",
    "my current bot is stupid", "chatgpt makes up stuff", "hallucinating",
    
    # --- TIME & BURNOUT ---
    "chasing leads all day", "tired of dialing", "burnout", "wasting my day",
    "no time to sell", "stuck setting appointments", "hate cold calling",
    "missed appointments", "no shows", "low show rate", "calendar is empty"
]

# 2. SOFT PAIN (The "Wants")
SOFT_PAIN_PATTERNS = [
    # --- DESIRE FOR AUTOMATION ---
    "want to automate", "looking for ai", "need a setter", "want to scale",
    "looking for better tools", "ghl is too hard", "need something simpler",
    "want more bookings", "want to fill my calendar", "reactivate leads",
    "heard about this", "curious how it works", "want to save time",
    "looking for efficiency", "need help following up"
]

# 3. DEFLECTION (The "Just Show Me" Moves)
DEFLECTION_PATTERNS = [
    "how much is it", "what is the price", "cost", "pricing",
    "is this a real person", "are you a bot", "just show me a demo",
    "send me the link", "i just want to sign up", "where do i buy",
    "is this free", "trial", "test it out", "whats the catch"
]

# 4. OBJECTION (The "No" Moves)
OBJECTION_PATTERNS = [
    "too expensive", "i have a bot", "i use highlevel", "i use hubspot",
    "not interested", "just looking", "im broke", "cant afford it",
    "dont trust ai", "ai is a scam", "maybe later", "not right now"
]

# 5. LOOP PATTERNS (SaaS Specific)
LOOP_PATTERNS = [
    "how many leads", "what kind of leads", "automation", "follow up",
    "what would it mean", "impact on your agency", "booking rate",
    "does that make sense", "fair enough", "got it"
]

# 6. NO-ORIENTED TRIGGERS
NO_ORIENTED_PATTERNS = [
    "opposed to seeing", "bad idea", "against automating", "reason not to",
    "give up on", "closing the door"
]

def is_fuzzy_match(user_text: str, patterns: List[str], threshold: float = 0.70) -> bool:
    user_text_clean = user_text.lower().strip()
    for pattern in patterns:
        if pattern.lower() in user_text_clean: return True
        if difflib.SequenceMatcher(None, user_text_clean, pattern).ratio() >= threshold: return True
    return False

def analyze_logic_flow(recent_exchanges: List[dict]) -> LogicSignal:
    if not recent_exchanges:
        return LogicSignal(ConversationStage.INITIAL_OUTREACH, "none", False, 0, 0, False)

    lead_msgs = [m for m in recent_exchanges if m['role'] == 'lead']
    bot_msgs = [m for m in recent_exchanges if m['role'] == 'assistant']
    last_lead_text = lead_msgs[-1]['text'].lower() if lead_msgs else ""
    last_bot_text = bot_msgs[-1]['text'].lower() if bot_msgs else ""

    # Loop Detection
    recent_bot_msgs = [m['text'].lower() for m in bot_msgs[-3:]]
    loop_hit_count = sum(1 for m in recent_bot_msgs if any(p in m for p in LOOP_PATTERNS))
    is_looping = loop_hit_count >= 2

    # Logic Moves
    move_type = "statement"
    gap_signal = False
    pain_score = 0
    voss_no_signal = False

    if is_fuzzy_match(last_lead_text, CRITICAL_PAIN_PATTERNS, 0.7):
        pain_score = 3
        gap_signal = True
        move_type = "pain_admission"
    elif is_fuzzy_match(last_lead_text, SOFT_PAIN_PATTERNS, 0.7):
        pain_score = 1
        gap_signal = True
        move_type = "pain_admission"

    bot_asked_no = any(p in last_bot_text for p in NO_ORIENTED_PATTERNS)
    if bot_asked_no and ("no" in last_lead_text or "not " in last_lead_text):
        move_type = "agreement"
        voss_no_signal = True
    elif is_fuzzy_match(last_lead_text, ["yes", "sure", "ok", "ready", "buy"], 0.8):
        move_type = "agreement"
    elif is_fuzzy_match(last_lead_text, DEFLECTION_PATTERNS, 0.7):
        move_type = "deflection"
    elif is_fuzzy_match(last_lead_text, OBJECTION_PATTERNS, 0.7):
        move_type = "objection"

    # Stage Setting
    stage = ConversationStage.DISCOVERY
    if move_type in ["objection", "deflection"]: stage = ConversationStage.OBJECTION_HANDLING
    elif move_type == "agreement": stage = ConversationStage.CLOSING
    elif is_looping: stage = ConversationStage.RESISTANCE
    elif gap_signal: stage = ConversationStage.CONSEQUENCE
    
    if not lead_msgs: stage = ConversationStage.INITIAL_OUTREACH

    return LogicSignal(stage, move_type, gap_signal, pain_score, min(len(last_lead_text.split()), 5), voss_no_signal)