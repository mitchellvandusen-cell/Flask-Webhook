# conversation_engine.py - The Logic Signal Processor (Left Brain)
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
    OBJECTION_HANDLING = "objection" # FIXED: Mapped to "objection" value, matches logic usage
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
    "nothing left", "broke", "die", "death", "burial", "burden", "bankruptcy",
    "lose everything", "financial ruin", "financially ruined", "can't pay", "unable to pay", 
    "struggle to pay", "hard time paying", "insufficient funds", "no money left",
    "spouse left alone", "kids left alone", "family left alone", "children left alone", 
    "no income", "no support", "no help", "no backup", "no safety net",
    "crippled", "disabled", "paralyzed", "serious injury", "critical illness", "terminal illness", 
    "long term care", "Financially devastated", "financial devastation",
    "medical bills", "medical debt", "overwhelming debt", "unmanageable debt", "drowning in debt", 
    "financial hardship", "financially struggling", "lost my job", "fired from my job",
    "only I work", "wife stays home", "stay at home mom", "kids would have to move schools", 
    "that would be awful", "I dont want them to pay anything", "spouse would have to be a stripper",
    "I dont want to think about that", "no savings", "no 401k", "lost pension", "no survivorship"
]

# 2. SOFT PAIN (Gap Selling - The "Worry" Problems)
SOFT_PAIN_PATTERNS = [
    "I dont think its enough coverage", "it might not be enough", "not sure if it's enough","never thought about that",
    "I dont want my kids to struggle...", "what if something happens...", "ive thought about that...", "I was looking because...",
    "could lose my job", "could get sick", "could get hurt", "what if i...", "i worry about...", "i'm concerned about...",
    "i'm afraid that...", "if something happens...", "i don't want to burden...", "i don't want to leave my family...", 
    "i want to make sure...", "i want to protect...", "they would struggle", "they would have a hard time", 
    "they would be stressed", "they would have to sell the house", "they would have to move",
    "she/he would have to...", "my family would...", "my kids would...", "my spouse would...", 
    "I need insurance because...", "Im looking for insurance because...", 
    "my current plan doesn't", "my current plan is not", "i don't have enough", 
    "my coverage is about to expire", "I have grand kids", "i want to leave something", 
    "i want to provide for", "i want to make sure they are taken care of",
    "I just bought a house", "new baby", "getting married", "newlywed", "recently married", 
    "just had a baby", "just got married", "I just had a child", "I got divorced",
    "recently divorced", "my spouse just...", "my partner just...", "my wife just...", 
    "my husband just...", "my ex just...", "my kids are growing up", "my children are growing up",
    "i want to plan ahead"
]

# 3. DEFLECTION (Straight Line - The "Off-Track" Moves)
DEFLECTION_PATTERNS = [
    "how much", "cost", "price", "quote", "send info", "email me", 
    "send me something", "is this free", "just tell me", "call me later", "i'll call you", "not right now",
    "i'll think about it", "i'll get back to you", "i'll let you know", "i'll decide later", "i'll consider",
    "bot", "waste of time", "fake news", "(story not pertaining to life insurance)", "I went hunting", "who is this?",
    "how did you get this number?", "i dont remember", "no i didnt", "show me"
]

# 4. NO-ORIENTED TRIGGERS (Chris Voss)
NO_ORIENTED_PATTERNS = [
    "opposed", "ridiculous", "would it be a bad idea", "Would you be against", "is it a bad time",
    "do you disagree", "are you against", "would you be opposed", "You'll think im crazy but...", 
    "is it silly to think", "any reason this wouldn't make sense?", "Is is unreasonable to..."
]

# 6. SOFT OUTS (Objections)
OBJECTION_PATTERNS = [
    "not interested", "I need to think about it", "let me talk to my spouse", "now is a bad time", "im busy", 
    "take me off your list", "I already have it", "too expensive", "not a priority", "call me later", "send me info",
    "i'm good", "i'm covered", "i'm fine", "i'm set", "i'm okay", "no thanks", "spouse won't agree", "no money", "Im young",
    "i don't need it", "i do not need it", "call me back", "not the right time", "i have a policy", "i have coverage", "i'm covered",
    "im not making any decisions right now", "I need to talk to my lawyer", "i need to talk to my financial advisor", 
    "my son/daughter handles it", "I don't have the budget", "i don't have time", "i'm not interested", "no thanks", 
    "insurance is a scam", "i don't trust insurance", "i hate salespeople",
    "i'm just looking", "i'm shopping around", "i'm comparing options", "I just wanted a quote", "Last guy was pushy", 
    "i don't like pressure", "i'm skeptical", "I dont need this right now", "not in a position to decide", "i'm not ready", 
    "i need more time", "whats ethos", "Who did?", "Who are you?", "I didn't say you could text me",
    "lose my number", "I need to do that, call me later"
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
    
    # 2. Response Depth and Subtext Inference
    words = last_lead_text.split()
    depth_score = min(len(words), 2)

    subtext_score = 0
    if not last_lead_text.strip():
        subtext_score = -1

    # 3. Loop Detection (FIXED: Added commas)
    recent_bot_questions = [m['text'].lower() for m in bot_msgs[-4:] if '?' in m['text']]
    is_looping = (
        len(recent_bot_questions) >= 2 and
        any(word in " ".join(recent_bot_questions) for word in [
            "worry", "concern", "afraid", "scared", "happen if", "impact", "what happens", "how would", 
            "how would you handle?", "what would that do to your family?", "what would that mean", 
            "would it be important to you?", "gap", "check gap", "good rates", "got it", "understood on that", 
            "how would...", "...would it be...", "...check for...", "...are you opposed to...", 
            "...is that okay...", "...what would that mean for...", "...how would you handle..."
        ])
    )

    # 4. Analyze Lead's Move
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
    
    if any(x in last_lead_text for x in ["yes", "sure", "ok", "sounds good", "book", "schedule"]):
        move_type = "agreement"
    elif bot_asked_no_oriented and ("no" in last_lead_text or "not " in last_lead_text):
        move_type = "agreement"
        voss_no_signal = True
    elif any(x in last_lead_text for x in OBJECTION_PATTERNS):
        move_type = "objection"
    elif "?" in last_lead_text and any(p in last_lead_text for p in DEFLECTION_PATTERNS):
        move_type = "deflection"

    # 5. Determine Conversation Stage
    stage = ConversationStage.DISCOVERY  # true default

    if move_type == "rejection":
        stage = ConversationStage.OBJECTION_HANDLING
    elif move_type == "objection" or move_type == "deflection":
        stage = ConversationStage.OBJECTION_HANDLING
    elif move_type == "agreement" or any(k in last_lead_text for k in ["time", "call", "appointment"]):
        stage = ConversationStage.CLOSING
    
    elif is_looping:
        stage = ConversationStage.RESISTANCE 
    
    elif (gap_signal or pain_score > 0 or (depth_score > 1 and subtext_score >= 0)) and not is_looping:
        stage = ConversationStage.CONSEQUENCE
    
    elif subtext_score < 0: 
        stage = ConversationStage.RESISTANCE 
    
    if not lead_msgs or len(lead_msgs) == 0:
        stage = ConversationStage.INITIAL_OUTREACH

    # LOGIC FIX: Removed the "Fake History Loop" that was forcing RESISTANCE mode incorrectly.
    
    return LogicSignal(stage, move_type, gap_signal, pain_score, depth_score, voss_no_signal)