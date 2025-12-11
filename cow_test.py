#!/usr/bin/env python3
"""
cow_test.py - Adversarial Training System
Learns what breaks Grok, then exploits weaknesses
No limits. Infinite scenarios. Adaptive difficulty.
"""

import requests
import time
import random
import os
import json
from datetime import datetime

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://localhost:5000/")
MEMORY_FILE = "cow_memory.json"

# =============================================================================
# ADAPTIVE MEMORY - Learns what worked against Grok
# =============================================================================
def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r') as f:
            return json.load(f)
    return {"weak_spots": [], "strong_spots": [], "ghost_triggers": [], "book_triggers": [], "runs": 0}

def save_memory(memory):
    with open(MEMORY_FILE, 'w') as f:
        json.dump(memory, f, indent=2)

# =============================================================================
# MASSIVE RESPONSE LIBRARY - Every possible human response
# =============================================================================
OPENERS = [
    # Hostile
    "who is this", "how did you get my number", "wrong number", "stop", "unsubscribe",
    "leave me alone", "not interested", "im good", "nah", "no thanks", "pass",
    "dont text me", "im reporting this", "this is harassment", "blocked",
    # Confused
    "what", "huh", "???", "who", "what are you talking about", "i dont understand",
    "what plan", "what insurance", "i never signed up for anything", "wrong person",
    # Dismissive
    "yeah no", "nope", "already got it", "im covered", "dont need it", "waste of time",
    "not now", "maybe later", "ill think about it", "send me info", "just email me",
    # Curious
    "what is this about", "tell me more", "how much", "whats the catch", "is this legit",
    "how does it work", "what company", "are you a bot", "is this automated",
    # Busy
    "bad time", "at work", "in a meeting", "driving", "call me later", "not a good time",
    "super busy", "slammed right now", "maybe next week", "text me tomorrow",
    # Warm
    "oh hey", "yeah i remember", "been meaning to look into this", "good timing actually",
    "my wife just mentioned this", "we were just talking about this", "perfect timing",
]

OBJECTIONS = [
    # Price
    "too expensive", "cant afford it", "money is tight", "not in the budget",
    "saw cheaper online", "found better rates", "thats way too much", "ridiculous price",
    "im broke", "just lost my job", "economy sucks", "maybe when i get a raise",
    # Already covered
    "got it through work", "my job covers me", "have life insurance already",
    "my spouse has a policy", "we have coverage", "all set", "fully covered",
    "bundled with my car insurance", "state farm covers me", "got whole life already",
    # Dont need it
    "im young and healthy", "nothing will happen to me", "im invincible",
    "no dependents", "no kids", "not married", "no one depends on me",
    "ill deal with it later", "not a priority", "got other things to worry about",
    # Trust issues
    "insurance is a scam", "you people are all the same", "been burned before",
    "dont trust salespeople", "whats the catch", "sounds too good to be true",
    "my friend got screwed", "read bad reviews", "companies never pay out",
    # Spouse/partner
    "need to ask my wife", "gotta talk to my husband", "we decide together",
    "let me check with my partner", "ill run it by the spouse", "not my call",
    # Procrastination
    "let me think about it", "need time to decide", "not ready yet",
    "call me in a month", "after the holidays", "when things settle down",
    "too much going on", "bad timing", "revisit this later",
    # Vague deflection
    "maybe", "we'll see", "possibly", "idk", "not sure", "hmm",
    "interesting", "ok", "sure", "whatever", "if you say so",
]

INFORMATION_REVEALS = [
    # Family
    "got 2 kids", "have 3 children", "pregnant right now", "baby on the way",
    "kids are 5 and 8", "teenagers at home", "daughter in college", "son just graduated",
    "married 10 years", "just got engaged", "divorced last year", "single parent",
    "wife doesnt work", "husband is disabled", "taking care of my parents",
    # Financial
    "mortgage is 300k", "owe 400k on the house", "just bought a house", "renting now",
    "make about 80k", "decent income", "paycheck to paycheck", "got some savings",
    "have debt", "student loans", "credit cards maxed", "financially stable",
    # Health
    "got diabetes", "high blood pressure", "had a heart attack", "cancer survivor",
    "take medication", "overweight", "smoke occasionally", "quit smoking last year",
    "healthy as can be", "work out daily", "never been sick", "family history of heart disease",
    "had a stent put in", "on blood thinners", "copd", "asthma since childhood",
    # Work
    "work in construction", "office job", "self employed", "own a business",
    "dangerous job", "truck driver", "nurse", "teacher", "military",
    "might switch jobs", "company downsizing", "retiring soon", "just started new job",
    # Coverage details
    "have 100k through work", "only 50k coverage", "policy expires next year",
    "paying 200 a month", "term policy", "whole life from parents", "no beneficiary set",
    # Motivation
    "wife keeps asking", "had a scare recently", "friend just died", "parent passed",
    "want to leave something", "worried about the future", "cant sleep thinking about it",
    "saw something on the news", "coworker had a heart attack", "reality check",
]

BUYING_SIGNALS = [
    # Direct interest
    "how do i sign up", "whats the next step", "lets do this", "im ready",
    "send me the application", "where do i sign", "take my money", "lets get started",
    # Scheduling
    "when can we talk", "whats your availability", "can you call me", "set up a time",
    "tomorrow works", "tonight is good", "this week sometime", "asap",
    # Positive responses
    "that makes sense", "never thought of it that way", "good point",
    "youre right", "i didnt know that", "thats concerning", "we should fix that",
    "my wife would kill me", "cant leave them with nothing", "need to be responsible",
    # Questions showing intent
    "what coverage do you recommend", "how much should i get", "term or whole life",
    "what are the options", "walk me through it", "explain the benefits",
    "can i add my spouse", "what about the kids", "does it cover accidents",
]

CURVEBALLS = [
    # Random topics
    "what do you think about crypto", "hows the weather", "you watch the game",
    "whats your name again", "where are you located", "is this a real person",
    # Testing the bot
    "say something funny", "prove youre not a robot", "whats 2+2",
    "do you have feelings", "are you ai", "chatgpt?",
    # Irrelevant
    "my dog just died", "having a rough day", "life sucks", "everything is falling apart",
    "just got back from vacation", "kids are driving me crazy", "in laws visiting",
    # Aggressive
    "this is bs", "youre wasting my time", "get a real job", "scam artist",
    "reported to ftc", "calling my lawyer", "do not contact me again",
    # Negotiating
    "what can you do for me", "any discounts", "match this rate", "throw in something extra",
    "loyalty discount", "bundle deal", "first month free",
]

GHOSTING_PATTERNS = [
    "", "...", "k", "ok", "sure", "maybe", "idk", "we'll see", "hmm", "interesting",
    "let me think", "ill get back to you", "noted", "thanks", "appreciate it",
]

# =============================================================================
# PERSONA ENGINE - Infinite personality variations
# =============================================================================
def generate_persona():
    moods = ["hostile", "skeptical", "busy", "curious", "warm", "desperate", "analytical", "emotional"]
    life_stages = ["young_single", "newlywed", "new_parent", "established_family", "empty_nester", "retiring", "health_declining"]
    trust_levels = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]
    ghost_chances = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    
    names = ["Mike", "Sarah", "John", "Lisa", "Chris", "Dave", "Karen", "Tom", "Amanda", "Raj",
             "Jessica", "Brandon", "Ashley", "Tyler", "Megan", "Justin", "Emily", "Ryan", "Nicole", "Kevin",
             "Brittany", "Josh", "Stephanie", "Andrew", "Jennifer", "Matt", "Lauren", "Eric", "Samantha", "Brian"]
    
    return {
        "name": random.choice(names),
        "mood": random.choice(moods),
        "life_stage": random.choice(life_stages),
        "trust": random.choice(trust_levels),
        "ghost_chance": random.choice(ghost_chances),
        "patience": random.randint(3, 20),  # how many turns before they bail
        "difficulty": random.uniform(0.5, 2.0),  # multiplier for difficulty
    }

def pick_response(persona, turn, agent_reply, stats, memory):
    """Adaptively pick response based on what has worked against Grok before"""
    
    # Check if we have weak spots to exploit
    weak_exploitation = random.random() < 0.3 and memory.get("weak_spots")
    if weak_exploitation:
        return random.choice(memory["weak_spots"])
    
    # First turn - opener
    if turn == 1:
        if persona["mood"] == "hostile":
            return random.choice([o for o in OPENERS if any(w in o for w in ["stop", "who", "wrong", "leave", "block"])])
        elif persona["mood"] == "warm":
            return random.choice([o for o in OPENERS if any(w in o for w in ["hey", "remember", "timing", "wife"])])
        elif persona["mood"] == "busy":
            return random.choice([o for o in OPENERS if any(w in o for w in ["work", "busy", "meeting", "later"])])
        else:
            return random.choice(OPENERS)
    
    # React to agent behavior
    agent_lower = agent_reply.lower()
    
    # If agent asked a question, sometimes dodge it
    if "?" in agent_reply and random.random() < 0.4 * persona["difficulty"]:
        return random.choice(OBJECTIONS + CURVEBALLS + GHOSTING_PATTERNS)
    
    # If agent showed empathy, sometimes soften
    empathy_words = ["understand", "hear you", "makes sense", "fair", "got it", "sorry"]
    if any(w in agent_lower for w in empathy_words):
        if random.random() < 0.6:
            return random.choice(INFORMATION_REVEALS + BUYING_SIGNALS)
        
    # If agent offered appointment, test their close
    if any(w in agent_lower for w in ["6:30", "10:15", "tonight", "tomorrow", "appointment", "call"]):
        if persona["trust"] > 50 and random.random() < 0.5:
            return random.choice(BUYING_SIGNALS)
        else:
            return random.choice(OBJECTIONS)
    
    # Random behavior based on mood
    roll = random.random()
    
    if persona["mood"] == "hostile":
        if roll < 0.5:
            return random.choice(OBJECTIONS)
        elif roll < 0.8:
            return random.choice(CURVEBALLS)
        else:
            return random.choice(GHOSTING_PATTERNS)
            
    elif persona["mood"] == "desperate":
        if roll < 0.6:
            return random.choice(INFORMATION_REVEALS)
        else:
            return random.choice(BUYING_SIGNALS)
            
    elif persona["mood"] == "analytical":
        questions = [
            "what are the exact terms", "whats the fine print", "breakdown the costs",
            "compare to other options", "what are the exclusions", "whats not covered",
            "how does the payout work", "what if i miss a payment", "cancellation policy",
        ]
        if roll < 0.5:
            return random.choice(questions)
        else:
            return random.choice(OBJECTIONS)
            
    elif persona["mood"] == "emotional":
        emotional = [
            "i just want my family protected", "cant stop thinking about it",
            "what if something happens to me", "my kids need me", "wife would be devastated",
            "saw my dad go through this", "dont want to leave them with nothing",
        ]
        if roll < 0.4:
            return random.choice(emotional)
        else:
            return random.choice(INFORMATION_REVEALS)
    
    # Default mixed behavior
    all_responses = OBJECTIONS + INFORMATION_REVEALS + CURVEBALLS
    if persona["trust"] > 40:
        all_responses += BUYING_SIGNALS
    
    return random.choice(all_responses)

# =============================================================================
# SCORING - Match our outcome_learning system
# =============================================================================
def score_outcome(stats, persona):
    """Score matching our vibe system"""
    score = 0
    vibe = "neutral"
    
    if stats["booked"]:
        vibe = "direction"
        score = 5.0  # 3.0 direction + 2.0 appointment bonus
    elif stats["ghosted"]:
        vibe = "ghosted"
        score = -1.0
    elif stats["needs_revealed"] > 0:
        vibe = "need"
        score = 4.0
    elif stats["info_shared"] > 0:
        vibe = "information"
        score = 2.0
    elif stats["positive_responses"] > 0:
        vibe = "direction"
        score = 3.0
    elif stats["objections"] > 0:
        vibe = "objection"
        score = 1.0
    elif stats["dismissals"] > 0:
        vibe = "dismissive"
        score = 0.5
    
    return vibe, score

# =============================================================================
# CONVERSATION ENGINE - No limits
# =============================================================================
def run_conversation(persona, memory, verbose=True):
    contact_id = f"cow_{persona['name'].lower()}_{int(time.time())}_{random.randint(1000,9999)}"
    
    stats = {
        "turns": 0,
        "booked": False,
        "ghosted": False,
        "needs_revealed": 0,
        "info_shared": 0,
        "positive_responses": 0,
        "objections": 0,
        "dismissals": 0,
        "questions_asked": 0,
        "empathy_shown": 0,
    }
    
    history = []
    payload = {
        "contact_id": contact_id,
        "first_name": persona["name"],
        "message": "initial outreach - send first message",
        "intent": "initial_outreach",
        "conversation_history": []
    }
    
    if verbose:
        print(f"\n{'='*60}")
        print(f"{persona['name']} | {persona['mood']} | Trust:{persona['trust']} | Ghost:{persona['ghost_chance']:.0%}")
        print(f"{'='*60}")
    
    # No hard limit - patience determines length
    max_turns = persona["patience"] + random.randint(-2, 5)
    
    while stats["turns"] < max_turns:
        stats["turns"] += 1
        
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=30)
            if r.status_code != 200:
                break
            
            data = r.json()
            agent_reply = data.get("reply", "").strip()
            
            if not agent_reply:
                break
            
            if verbose:
                print(f"  GROK: {agent_reply}")
            
            history.append({"direction": "outbound", "body": agent_reply})
            
            # Track agent behavior
            if "?" in agent_reply:
                stats["questions_asked"] += 1
            if any(w in agent_reply.lower() for w in ["understand", "hear", "makes sense", "fair", "got it"]):
                stats["empathy_shown"] += 1
            
            # Check for booking
            if "confirmation code" in agent_reply.lower() or "locked in" in agent_reply.lower():
                stats["booked"] = True
                memory["book_triggers"].append(history[-2]["body"] if len(history) > 1 else "initial")
                if verbose:
                    print("  *** BOOKED ***")
                break
            
            # Ghost check - adaptive based on what caused ghosts before
            ghost_roll = persona["ghost_chance"] * (1 - persona["trust"]/100)
            if stats["empathy_shown"] > 0:
                ghost_roll *= 0.7
            if random.random() < ghost_roll:
                stats["ghosted"] = True
                memory["ghost_triggers"].append(agent_reply[:100])
                if verbose:
                    print(f"  {persona['name']}: [GHOSTED]")
                break
            
            # Pick lead response
            lead_reply = pick_response(persona, stats["turns"], agent_reply, stats, memory)
            
            # Classify the response
            reply_lower = lead_reply.lower()
            if any(w in reply_lower for w in ["kids", "wife", "husband", "mortgage", "health", "job", "income"]):
                stats["info_shared"] += 1
            if any(w in reply_lower for w in ["worried", "scared", "need", "want", "ready", "lets do"]):
                stats["needs_revealed"] += 1
            if any(w in reply_lower for w in ["sounds good", "makes sense", "youre right", "good point", "when can"]):
                stats["positive_responses"] += 1
            if any(w in reply_lower for w in ["not interested", "too expensive", "dont need", "scam", "stop"]):
                stats["objections"] += 1
            if any(w in reply_lower for w in ["no", "nope", "pass", "nah", "leave me alone"]):
                stats["dismissals"] += 1
            
            if verbose:
                print(f"  {persona['name']}: {lead_reply}")
            
            history.append({"direction": "inbound", "body": lead_reply})
            
            # Update trust based on agent behavior
            if stats["empathy_shown"] > stats["questions_asked"]:
                persona["trust"] = min(100, persona["trust"] + 10)
            else:
                persona["trust"] = max(0, persona["trust"] - 5)
            
            payload["message"] = lead_reply
            payload["conversation_history"] = history[-12:]
            
            time.sleep(random.uniform(1.5, 4))
            
        except Exception as e:
            if verbose:
                print(f"  Error: {e}")
            break
    
    vibe, score = score_outcome(stats, persona)
    
    # Learn what worked/failed
    if stats["ghosted"] and history:
        memory["weak_spots"].append(history[-1]["body"] if history[-1]["direction"] == "inbound" else "")
    if stats["booked"] and len(history) > 2:
        memory["strong_spots"].append(history[-3]["body"])
    
    # Keep memory bounded
    memory["weak_spots"] = memory["weak_spots"][-50:]
    memory["strong_spots"] = memory["strong_spots"][-50:]
    memory["ghost_triggers"] = memory["ghost_triggers"][-30:]
    memory["book_triggers"] = memory["book_triggers"][-30:]
    
    if verbose:
        print(f"\n  RESULT: {vibe} | Score: {score} | Turns: {stats['turns']} | Booked: {stats['booked']}")
    
    return {
        "persona": persona["name"],
        "mood": persona["mood"],
        "vibe": vibe,
        "score": score,
        "turns": stats["turns"],
        "booked": stats["booked"],
        "ghosted": stats["ghosted"],
        "stats": stats
    }

# =============================================================================
# STAMPEDE - Unlimited cycles
# =============================================================================
def run_stampede(cycles=None, personas_per_cycle=10, verbose=True):
    """Run training. cycles=None means infinite."""
    
    memory = load_memory()
    memory["runs"] += 1
    
    print(f"\n{'#'*60}")
    print(f"# COW STAMPEDE #{memory['runs']}")
    print(f"# Cycles: {'INFINITE' if cycles is None else cycles}")
    print(f"# Personas per cycle: {personas_per_cycle}")
    print(f"# Weak spots known: {len(memory.get('weak_spots', []))}")
    print(f"{'#'*60}")
    
    all_results = []
    cycle = 0
    
    try:
        while cycles is None or cycle < cycles:
            cycle += 1
            print(f"\n{'='*60}")
            print(f"CYCLE {cycle}")
            print(f"{'='*60}")
            
            for i in range(personas_per_cycle):
                persona = generate_persona()
                
                # Adaptive difficulty - make it harder as Grok learns
                if memory["runs"] > 1:
                    persona["difficulty"] *= 1 + (memory["runs"] * 0.1)
                    persona["ghost_chance"] = min(0.95, persona["ghost_chance"] * 1.1)
                
                result = run_conversation(persona, memory, verbose)
                all_results.append(result)
            
            save_memory(memory)
            
            # Stats after each cycle
            booked = len([r for r in all_results if r["booked"]])
            ghosted = len([r for r in all_results if r["ghosted"]])
            total = len(all_results)
            
            print(f"\nCycle {cycle} complete | Total: {total} | Booked: {booked} ({booked/total*100:.1f}%) | Ghosted: {ghosted} ({ghosted/total*100:.1f}%)")
            
            if cycles is None:
                time.sleep(3)
    
    except KeyboardInterrupt:
        print("\n\nStopped by user")
    
    # Final report
    print(f"\n{'#'*60}")
    print("# FINAL REPORT")
    print(f"{'#'*60}")
    
    total = len(all_results)
    if total > 0:
        booked = len([r for r in all_results if r["booked"]])
        ghosted = len([r for r in all_results if r["ghosted"]])
        avg_score = sum(r["score"] for r in all_results) / total
        avg_turns = sum(r["turns"] for r in all_results) / total
        
        print(f"\nConversations: {total}")
        print(f"Booked: {booked} ({booked/total*100:.1f}%)")
        print(f"Ghosted: {ghosted} ({ghosted/total*100:.1f}%)")
        print(f"Avg Score: {avg_score:.2f}")
        print(f"Avg Turns: {avg_turns:.1f}")
        
        print("\nVibe Distribution:")
        vibes = {}
        for r in all_results:
            vibes[r["vibe"]] = vibes.get(r["vibe"], 0) + 1
        for vibe, count in sorted(vibes.items(), key=lambda x: -x[1]):
            print(f"  {vibe}: {count} ({count/total*100:.1f}%)")
    
    save_memory(memory)
    return all_results


if __name__ == "__main__":
    import sys
    
    cycles = None  # Infinite by default
    personas = 10
    verbose = True
    
    if "--quick" in sys.argv:
        cycles = 1
        personas = 5
    elif "--medium" in sys.argv:
        cycles = 5
    elif "--long" in sys.argv:
        cycles = 15
    elif "--rockstar" in sys.argv:
        cycles = 20
    
    if "--quiet" in sys.argv:
        verbose = False
    
    if "--cycles" in sys.argv:
        idx = sys.argv.index("--cycles")
        if idx + 1 < len(sys.argv):
            cycles = int(sys.argv[idx + 1])
    
    print("Starting COW TEST - Press Ctrl+C to stop")
    print(f"Mode: {'Infinite' if cycles is None else f'{cycles} cycles'}")
    
    run_stampede(cycles=cycles, personas_per_cycle=personas, verbose=verbose)
