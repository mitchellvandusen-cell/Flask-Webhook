# conversation_engine.py - The Logic Signal Processor (Left Brain)
# "It's not about what you asked. It's about what they answered."

import logging
import difflib
from enum import Enum
from dataclasses import dataclass
from typing import List

logger = logging.getLogger(__name__)

class ConversationStage(Enum):
    INITIAL_OUTREACH = "initial_outreach"
    DISCOVERY = "discovery"          # NEPQ Situation / Gap Selling Current State
    CONSEQUENCE = "consequence"      # NEPQ Consequence / Gap Selling Pain
    RESISTANCE = "resistance"        # Chris Voss (Skepticism/Trust Issues)
    OBJECTION_HANDLING = "objection" # Handling "No", "Not Interested", "Send Info"
    CLOSING = "closing"              # Booking the appointment

@dataclass
class LogicSignal:
    stage: ConversationStage
    last_move_type: str   # "deflection", "agreement", "rejection", "question", "statement", "pain_admission"
    gap_signal: bool      # True if they admitted a problem
    pain_score: int       # 0-3 (Intensity of pain words)
    depth_score: int      # 0-5 (Quality/Length of their answer)
    voss_no_signal: bool  # True if they answered "No" to a "No-Oriented Question"

# ==========================================
# === EXHAUSTIVE PATTERN LIBRARIES ===
# ==========================================

# 1. CRITICAL PAIN (Gap Selling - The "Black Hole" Problems)
CRITICAL_PAIN_PATTERNS = [
    # --- IMMEDIATE INSOLVENCY & CASH FREEZE ---
    "If I died tonight, my wife wouldn't even be able to access the bank accounts to buy groceries",
    "My family would be planning a funeral while checking the sofa cushions for change",
    "We honestly don’t have enough sitting around for a funeral",
    "My parents would have to put my burial on a credit card",
    "The business accounts would freeze immediately meaning none of my employees would get paid",
    "We are paper rich but if something happens to me my family can't eat the equity in this house",
    "My husband has no idea how to pay the bills he’d likely default within the first month",
    "I’m the only one with the passwords and the access if I go the entire financial system of this house goes with me",
    "We’d have to start a GoFundMe just to get my body back to my home state",
    "They would have to choose between a casket and next month's rent",
    "My business partner would lock the accounts and my family wouldn’t see a dime for years during probate",
    "My wife would be at the pawn shop selling her wedding ring to pay the electric bill",
    "We live paycheck to paycheck if I die on a Tuesday they are destitute by Friday",
    "The estate taxes alone would wipe out every dollar of cash we have in the bank",

    # --- HOUSING & DISPLACEMENT ---
    "Without my paycheck the For Sale sign goes up in the front yard within 30 days",
    "My wife and kids would have to move back in with her mother and I know that environment is toxic",
    "We just refinanced there is no way a single income covers this new mortgage payment",
    "They would be evicted before my obituary is even printed",
    "My kids would lose their backyard their bedrooms and their school district all in the same month",
    "We live in a high-cost area if I die they are priced out of this city entirely",
    "My siblings would probably fight over the house and force a sale kicking my spouse out",
    "My wife would have to pack up 20 years of memories into a U-Haul because she can't make the mortgage",
    "They would have to move into a one-bedroom apartment in a bad part of town",
    "We’d lose the family farm it’s been in the bloodline for generations and my debt would kill it",
    "The reverse mortgage comes due immediately if they can't pay it off they are homeless",

    # --- CAREER & LIFESTYLE COLLAPSE ---
    "She’s been a stay-at-home mom for ten years she is essentially unemployable in today's market",
    "I take care of everything at home if I’m gone he’d have to quit his job just to manage the kids",
    "We’d go from upper middle class to poverty line overnight",
    "The club memberships the travel the lessons all of that stops the day I stop breathing",
    "My business partner would dissolve the company and my family would be left with nothing but the debts",
    "He would have to cash out his 401k just to keep the lights on destroying his own retirement",
    "My wife would be scrubbing toilets or waiting tables at night just to feed the kids",
    "I’d be leaving her a legacy of stress panic and bill collectors",
    "She would have to go back to work the day after the funeral there’s no time to cry when the rent is due",
    "My spouse would burn through our retirement savings in two years just to survive now",

    # --- CHILDREN & EDUCATION IMPACT ---
    "The college fund would become the grocery fund immediately",
    "My oldest son would have to drop out of university to come home and work",
    "My daughter would basically become the mother to her younger siblings because my husband would be working all the time",
    "They would end up in state childcare or questionable daycares because we couldn't afford the nanny anymore",
    "My kids would have to give up travel sports music lessons and tutoring",
    "I don’t want my children to be the ones who used to have money before their dad died",
    "If we both go the kids end up in foster care because neither of our families has the money to take them in",
    "My special needs child would end up in a state institution because no one else can afford his therapy",
    "My daughter would have to take out massive student loans that will cripple her for decades",
    "They would be the poor kids at school the ones with the worn-out clothes and the sad stories",

    # --- BURDEN ON EXTENDED FAMILY ---
    "My parents are retired if I die they’d have to spend their life savings raising my grandchildren",
    "My sister is barely getting by she can't afford to feed three extra mouths if my kids get sent to her",
    "We are the safety net for the rest of the family if I die the safety net breaks for everyone",
    "I take care of my aging mother if I’m gone she goes into a state-run nursing home",

    # --- RELATIONSHIP & EMOTIONAL FALLOUT ---
    "The grief would be bad enough but the stress of being broke would destroy my husband",
    "My wife would probably remarry the first guy who offered financial stability whether he was a good man or not",
    "My family would tear each other apart fighting over the few assets I do have",
    "I don't want my spouse to be angry at me for leaving them in a mess while they are mourning me",
    "She would be signing bankruptcy papers while planning my memorial service",
    "I don't want a stranger raising my children just because he could afford to pay the bills",
    "My brother and sister would sue my wife for my share of the inheritance",
    "My ex-wife would come after the estate for child support leaving my current family with nothing"
]

# 2. SOFT PAIN (Gap Selling - The "Worry" & "Responsibility" Problems)
SOFT_PAIN_PATTERNS = [
    # --- UNCERTAINTY & DOUBT (Formal) ---
    "I have some insurance through work but I honestly don't think it is enough to cover everything",
    "I’m not 100% sure if my current policy would actually pay off the whole mortgage",
    "I have a policy from years ago but I realized with inflation it might not go very far anymore",
    "I’ve been thinking that what I have set up might leave them a little short",
    "I just want to double check that I’m not underinsured because expenses have gone up",
    "I have coverage but I don't know the details and I’m worried it might expire soon",
    "I think I’m covered but I’d rather be safe than sorry",
    "I was just looking to see if I could get a little bit more just in case",
    "My wife keeps asking me if we have enough and I honestly don't have a good answer for her",
    "I have a term policy but I’m worried about what happens when the term ends",
    "I’m not sure if my current insurance covers natural causes or just accidental death",

    # --- UNCERTAINTY (Casual/Chat Syntax) ---
    "idk if what i have is enough honestly",
    "i have something thru work but idk if its good",
    "just checking to see if i need more coverage",
    "im not sure if my policy is still active",
    "thinking i might need a little more insurance",
    "worried my family wont have enough if i die",
    "work pays for some but prob not enough",
    "dont know if 100k is enough these days",
    "just want to see if i can get a better deal",

    # --- LIFE EVENT TRIGGERS (New Need) ---
    "We just had a new baby so I figured it was time to finally get this sorted out",
    "I just bought a new house and the mortgage is huge so I need to cover that debt",
    "I recently got married and I want to make sure my spouse is protected if something happens",
    "We are expecting our first child soon and reality is starting to hit me",
    "I just got divorced and I need to set up a new policy for my kids since the old one is gone",
    "My kids are getting older and starting college so I want to make sure their tuition is secured",
    "I just turned 50 and I know if I wait any longer it’s going to get too expensive",
    "My dad passed away recently and it made me realize I don't have anything in place for myself",
    "I had a health scare recently and it made me start thinking about getting my affairs in order",
    "My wife just quit her job to stay home with the kids so we are down to one income now",
    "We just took out a loan for a business and I need to make sure it doesn't fall on my family",

    # --- EMPLOYMENT & PORTABILITY CONCERNS ---
    "I’m planning on retiring soon and I know I will lose my work insurance when I leave",
    "I want something that is personal to me and not tied to my job in case I get laid off",
    "I’m changing jobs soon and I don't want to have a gap in my coverage while I switch",
    "I only have accidental coverage through my union and I know I need real life insurance",
    "My employer pays for 1x my salary but that wouldn't last my family more than a year",
    "I’m worried about relying on my job for insurance because the economy is shaky right now",
    "losing my benefits soon so need my own plan",

    # --- MORTGAGE PROTECTION (Specific Soft Pain) ---
    "I just want to make sure the house is paid off so they always have a roof over their heads",
    "My main concern is the mortgage because that is our biggest bill every month",
    "I don't want them to have to worry about making the house payment if I’m not around",
    "I want to set it up so the deed is clear and free for my wife",
    "We have about 20 years left on the mortgage and I want to cover that specific timeline",
    "just want to cover the house note",

    # --- DUTY, LOVE & LEGACY (The "Right Thing To Do") ---
    "I just want to make sure they don't have to worry about money while they are grieving",
    "I don't want to be a burden on my children when I get old",
    "I want to leave a little something for my grandkids so they have a head start in life",
    "I want to make sure my funeral costs are covered so my family doesn't have to pay for it",
    "It’s just about peace of mind for me knowing that everything is taken care of",
    "I want to make sure my wife can keep the same lifestyle she has now without stressing",
    "I just want to be responsible and not leave a financial mess behind me",
    "I want to ensure my kids have a nest egg to start their lives with or buy their first home",
    "I promised my wife I would handle this and I haven't done it yet",
    "I want to leave a legacy not a liability",
    "just want peace of mind really",

    # --- MISCONCEPTIONS & PARTIAL COVERAGE ---
    "I have the VA benefits but I know that takes a long time to pay out",
    "I have mortgage protection but I need something for living expenses",
    "I have a burial policy but I need something for income replacement",
    "I thought I had enough but my friend told me I need 10x my income",

    # --- SHOPPING & OPTIMIZATION ---
    "I’m just shopping around to see if I can get a better rate than what I’m paying now",
    "I saw an ad for a lower price and wanted to see if I could save some money on my premiums",
    "I’m just curious what it would cost to add another $100k to what I already have",
    "I want to see what options are out there before I commit to anything",
    "I’m looking for a policy that builds cash value so I can use it later for retirement",
    "I just want to get a quote to see if it fits in my budget right now",
    "I’m comparing term versus whole life to see which one makes more sense for my situation",
    "My rates just went up on my current policy so I’m looking for alternatives"
]

# 3. DEFLECTION (Straight Line - The "Off-Track", "Stall" & "Frame Control" Moves)
DEFLECTION_PATTERNS = [
    # --- PRICE PIVOTS (The "Commoditizer") ---
    "I just want to know how much this is going to cost me right now",
    "Can you just give me a ballpark number without all the twenty questions",
    "I’m just looking for a quote right now I don't want to have a conversation",
    "Just tell me the price and I’ll let you know if I’m interested",
    "How much is it per month roughly just give me a range",
    "I don't have time for a consultation just send me the rates please",
    "Is this going to be expensive because I’m on a really tight budget",
    "What is the cheapest option you have available for someone my age",
    "I’m just price shopping at the moment not buying anything yet",
    "Skip the sales pitch and just give me the numbers",
    "how much is it though",
    "whats the damage going to be price wise",
    "just give me the price please",

    # --- "SEND ME INFO" (The "Brush Off") ---
    "Can you just email me the information so I can look at it later",
    "I’d prefer if you just sent me a text with the details instead of calling",
    "Do you have a website I can look at instead of talking to you",
    "Just send me a PDF or a brochure to my email address",
    "I work weird hours so it’s better if you just mail me something",
    "I cant talk right now just text me the info and ill look at it",
    "send me the details via email",
    "just email me the quote",
    "can you text me a link instead",
    "just send me the overarching details",

    # --- TIMING & BUSYNESS (The "Kick the Can") ---
    "I’m actually driving right now so I can’t really text back",
    "I’m walking into a meeting can you try me again next week",
    "I’m about to sit down for dinner with my family",
    "I’m at work right now and I can’t be on my phone",
    "Now isn't really a good time for me to get into this",
    "I’m busy right now can we do this later",
    "call me back in a few days",
    "im at work",
    "im driving cant text",
    "busy right now",
    "catch me later",

    # --- PROCRASTINATION (The "stall") ---
    "I need to talk to my spouse before I answer any more questions",
    "I need to do some research on your company first before I proceed",
    "I’m not looking to make a decision today I’m just looking around",
    "Let me think about it and I’ll get back to you later",
    "I’ll let you know if I decide to move forward with this",
    "I need to check my budget before I commit to a call",
    "I’ll reach out to you when I’m ready",
    "let me think on it",
    "ill get back to you",
    "i need to sleep on it",

    # --- SKEPTICISM & IDENTITY (The "Trust Gap") ---
    "Are you a real person or a robot",
    "Is this an automated message or a human being",
    "How did you get my phone number I didn't sign up for this",
    "Who is this again and what company are you with",
    "I thought I was signing up for something else entirely",
    "I didn't request any information about life insurance",
    "I don't remember filling out a form online",
    "stop texting me if you are a bot",
    "who is this",
    "how did you get my number",
    "is this a scam",
    "where did you get my info",

    # --- CONFUSION & AMNESIA (The "Disconnect") ---
    "I think I already took care of this with another agent",
    "I’m pretty sure I have coverage already I don't need more",
    "I spoke to someone yesterday about this why are you texting me",
    "I thought I already finished this application",
    "i have insurance already thanks",
    "im already covered",
    "wrong number",
    "you have the wrong person",
    "i didnt ask for this"
]

# 4. NO-ORIENTED TRIGGERS (Chris Voss)
# The engine looks for these in the BOT'S previous text.
# If found, a "No" from the user = AGREEMENT/SUCCESS.
NO_ORIENTED_PATTERNS = [
    # --- TIME & AVAILABILITY ("Is it a bad time?") ---
    "Is now a bad time to chat",
    "Am I catching you at a bad time",
    "Is it a horrible time to talk for a minute",
    "Are you in the middle of something right now",
    "Am I interrupting anything important",
    "Is this a terrible time to connect",
    "Do you want me to stop texting you",
    "Would it be a bother if I sent you the details",
    "Is it impossible for you to talk right now",
    "Are you too busy to handle this right now",

    # --- PERMISSION & OPPOSITION ("Are you against?") ---
    "Would you be opposed to seeing a quick quote",
    "Are you against seeing if we can beat your current rate",
    "Would you be against hopping on a brief call",
    "Are you opposed to having a 5 minute conversation",
    "Do you disagree with the idea of protecting your income",
    "Would you be opposed to me sending over the information",
    "Are you against taking a look at the options",
    "Would it be a problem if I asked you a few questions",
    "Are you against saving money on your premiums",
    "Do you have a reason not to look at this",
    "Is there any reason we shouldn't proceed",
    "Are you against at least seeing what is available",

    # --- LOGIC & REASONABILITY ("Is it ridiculous?") ---
    "Is it ridiculous to think you might need more coverage",
    "Does it sound unreasonable to just check your options",
    "Is it silly to worry about what happens if you pass away",
    "Would it be crazy to suggest we look at this together",
    "Is it totally unreasonable to protect your mortgage",
    "Do you think it's a waste of time to compare rates",
    "Is it ridiculous to ask how your family would pay the bills",
    "Does it sound crazy to want peace of mind",
    "Is it asking too much to just get a baseline quote",
    "Is it unreasonable to expect the unexpected",
    "Does it seem impossible to fit this into your budget",

    # --- ACTION & RISK ("Is it a bad idea?") ---
    "Would it be a bad idea to keep this file open for a few days",
    "Is it a bad idea to just see what you qualify for",
    "Would it be a terrible idea to schedule a quick call",
    "Do you think it's a bad idea to have a backup plan",
    "Is it a mistake to look at this now before you get older",
    "Would it hurt to just get the information",
    "Is it a bad idea to be prepared just in case",
    "Do you regret looking into this",
    "Would it be a mistake to at least compare the numbers",

    # --- RE-ENGAGEMENT ("Have you given up?") ---
    "Have you given up on finding life insurance",
    "Have you given up on protecting your family",
    "Did you give up on this project entirely",
    "Are you done looking for coverage",
    "Have you decided to stop looking for options",
    "Did you decide against moving forward",
    "Have you deferred this for another time",
    "Are you closing the door on this for now",
    "Have you decided it's not worth fixing"
]

# 6. SOFT OUTS (Objections - The "No", "Not Now", & "Not Me" Moves)
OBJECTION_PATTERNS = [
    # --- NOT INTERESTED (The "Hard" Soft Out) ---
    "I am not interested in getting any more insurance right now",
    "We are not looking to add any more bills to our monthly expenses",
    "Please take me off your calling list I am not interested",
    "I’m actually all set and don't need anything else",
    "We decided to pass on this for the time being",
    "I’m going to decline moving forward with this",
    "No thank you I am not interested",
    "I’m good thanks for checking though",
    "I’ll pass on this opportunity",
    "We aren't buying anything right now",
    "not interested",
    "remove me",
    "im good",
    "hard pass",

    # --- ALREADY COVERED (The "Status Quo" Objection) ---
    "I already have a policy through my job that covers everything",
    "I have plenty of life insurance already established",
    "We just bought a policy last month so we are good",
    "I have coverage with State Farm and I’m happy with them",
    "My parents actually set this up for me years ago",
    "I’m fully covered I don't need any more",
    "I have a million dollar policy I don't need this",
    "i have insurance",
    "im already covered",
    "we are set",

    # --- MONEY & BUDGET (The "Affordability" Objection) ---
    "I really can't afford to pay for anything extra right now",
    "Our budget is extremely tight with inflation the way it is",
    "I don't have any money for life insurance",
    "It is just too expensive for me at this moment",
    "We are trying to cut expenses not add them",
    "I’m broke right now call me when I win the lottery",
    "I can't take on another payment",
    "too expensive",
    "no money",
    "cant afford it",

    # --- SPOUSE / AUTHORITY (The "Hands Tied" Objection) ---
    "My wife handles all the bills and she said no",
    "My husband doesn't believe in life insurance",
    "I spoke to my spouse and they don't want to proceed",
    "My partner deals with the finances and they aren't interested",
    "My son handles my affairs and he said I don't need it",
    "I can't make a decision without my wife's approval",
    "spouse said no",
    "wife said no",
    "husband said no",

    # --- TIMING & PRIORITY (The "Kick the Can" Objection) ---
    "This just isn't a priority for us right now",
    "I have too much going on to deal with this",
    "Maybe next year but right now is not the time",
    "I’m dealing with some health issues so I can't do this now",
    "Call me back in six months when things slow down",
    "I’m not ready to look at this yet",
    "not right now",
    "bad timing",
    "later",

    # --- SKEPTICISM & HOSTILITY (The "Trust" Objection) ---
    "I don't do business with people who text me out of the blue",
    "I don't trust insurance companies you guys are all scams",
    "I hate salespeople stop bothering me",
    "I never signed up for this leave me alone",
    "lose my number",
    "stop texting me",
    "how did you get my data",
    "i dont know you"
]

# 5. LOOP PATTERNS (The "Broken Record" Detector)
# Phrases the bot tends to repeat when stuck in a stage.
LOOP_PATTERNS = [
    # --- STUCK DIGGING FOR PAIN (Consequence Loop) ---
    "what would happen", "how would you handle", "what would that look like",
    "what would that mean", "what is the impact", "how would your family",
    "who would be responsible", "what is the plan", "how would they pay",
    "financial impact", "consequences of that", "describe for me",
    "what does that do to", "how would you feel", "what if something happens",
    "gap in coverage", "check for gaps", "coverage is enough",

    # --- STUCK IN EMPATHY/AGREEMENT (The "Bot Nod" Loop) ---
    "got it", "understood", "makes sense", "totally get it", "i hear you",
    "appreciate you sharing", "thanks for sharing", "glad you told me",
    "good to know", "fair enough", "i understand", "sounds good",
    "ok i see", "right i see", "absolutely",

    # --- STUCK IN CONFIRMATION (The "Tie-Down" Loop) ---
    "does that make sense", "is that fair", "sound reasonable",
    "are you okay with that", "does that sound like a plan",
    "are you with me", "do you agree", "how does that sound",

    # --- STUCK IN VOSS/PERMISSION (The "No-Oriented" Loop) ---
    "are you opposed", "is it a bad idea", "reason not to",
    "against seeing", "opposed to looking", "bad time to",
    "ridiculous to think", "unreasonable to"
]

# ==========================================
# === HELPER: FUZZY MATCHING (70% RULE) ===
# ==========================================

def is_fuzzy_match(user_text: str, patterns: List[str], threshold: float = 0.70) -> bool:
    """
    Returns True if user_text matches ANY pattern in the list with >= threshold similarity.
    Uses SequenceMatcher for robust phrase detection.
    """
    user_text_clean = user_text.lower().strip()
    
    # 1. Fast Pass: Substring check (matches if user types exact substring)
    # This saves CPU cycles on obvious matches.
    for pattern in patterns:
        if pattern.lower() in user_text_clean:
            return True

    # 2. Slow Pass: Fuzzy Logic (Levenshtein/Ratcliff-Obershelp)
    # Only runs if direct match failed.
    for pattern in patterns:
        pattern_clean = pattern.lower()
        # Calculate similarity ratio
        ratio = difflib.SequenceMatcher(None, user_text_clean, pattern_clean).ratio()
        if ratio >= threshold:
            # DEBUG: Uncomment to see what triggered the match
            # logger.debug(f"Fuzzy Match Hit: '{user_text}' ~= '{pattern}' (Score: {ratio:.2f})")
            return True
            
    return False


# ==========================================
# === LOGIC ENGINE ===
# ==========================================

def analyze_logic_flow(recent_exchanges: List[dict]) -> LogicSignal:
    """
    Analyzes the 'Mechanics' of the conversation based on the LEAD'S RESPONSE,
    using fuzzy matching for intent detection and density checks for loops.
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

    # 3. Loop Detection (SMART DENSITY CHECK)
    # Get last 3 bot messages to check for repetitive phrasing
    recent_bot_msgs = [m['text'].lower() for m in bot_msgs[-3:]]
    
    # Count how many of the last 3 messages contain a "Loop Pattern"
    loop_hit_count = 0
    for msg in recent_bot_msgs:
        if any(pattern in msg for pattern in LOOP_PATTERNS):
            loop_hit_count += 1
            
    # Also check for LITERAL repetition (Bot sent exact same message twice)
    literal_repeat = False
    if len(recent_bot_msgs) >= 2:
        if recent_bot_msgs[-1] in recent_bot_msgs[-2] or recent_bot_msgs[-2] in recent_bot_msgs[-1]:
            literal_repeat = True

    # TRIGGER: If 2+ recent messages match loop patterns OR literal repeat occurred
    is_looping = (loop_hit_count >= 2) or literal_repeat

    # 4. Analyze Lead's Move (Using Fuzzy Logic 70%)
    move_type = "statement"
    voss_no_signal = False
    gap_signal = False
    pain_score = 0
    
    # Check for Pain Signals using Fuzzy Match
    if is_fuzzy_match(last_lead_text, CRITICAL_PAIN_PATTERNS, threshold=0.7):
        pain_score = 3
        gap_signal = True
        move_type = "pain_admission"
    elif is_fuzzy_match(last_lead_text, SOFT_PAIN_PATTERNS, threshold=0.7):
        pain_score = 1
        gap_signal = True
        move_type = "pain_admission"

    # Check for No-Oriented Question Context
    bot_asked_no_oriented = any(p in last_bot_text for p in NO_ORIENTED_PATTERNS) and "?" in last_bot_text
    
    # Check for Agreement/Objection/Deflection
    if any(x in last_lead_text for x in ["yes", "sure", "ok", "sounds good", "book", "schedule"]):
        move_type = "agreement"
    elif bot_asked_no_oriented and ("no" in last_lead_text or "not " in last_lead_text):
        move_type = "agreement"
        voss_no_signal = True
    elif is_fuzzy_match(last_lead_text, OBJECTION_PATTERNS, threshold=0.7):
        move_type = "objection"
    elif "?" in last_lead_text and is_fuzzy_match(last_lead_text, DEFLECTION_PATTERNS, threshold=0.7):
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
    
    return LogicSignal(stage, move_type, gap_signal, pain_score, depth_score, voss_no_signal)