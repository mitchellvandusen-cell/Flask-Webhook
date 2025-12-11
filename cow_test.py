#!/usr/bin/env python3
"""
cow_test.py - Self-Training Simulation for NEPQ Bot
10 personas x 5 cycles = 50 conversations
Integrates with outcome_learning.py for pattern storage
"""

import requests
import time
import random
import os
from datetime import datetime

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://localhost:5000/")

PERSONAS = [
    {"id": "mike", "name": "Mike", "mood": "PISSED", "trust": 0, "ghost": 0.85},
    {"id": "sarah", "name": "Sarah", "mood": "ANNOYED", "trust": 15, "ghost": 0.65},
    {"id": "john", "name": "John", "mood": "SHOPPER", "trust": 60, "ghost": 0.1},
    {"id": "lisa", "name": "Lisa", "mood": "QUOTER", "trust": 30, "ghost": 0.4},
    {"id": "chris", "name": "Chris", "mood": "BUYER", "trust": 90, "ghost": 0.05},
    {"id": "dave", "name": "Dave", "mood": "BUSY→READY", "trust": 50, "ghost": 0.3},
    {"id": "karen", "name": "Karen", "mood": "ZERO TRUST", "trust": 5, "ghost": 0.75},
    {"id": "tom", "name": "Tom", "mood": "GHOSTER", "trust": 10, "ghost": 0.92},
    {"id": "amanda", "name": "Amanda", "mood": "WARM", "trust": 80, "ghost": 0.1},
    {"id": "raj", "name": "Raj", "mood": "HEALTH DECLINE", "trust": 70, "ghost": 0.2},
]

RESPONSES = {
    "mike": {
        "greet": ["WHO IS THIS", "Wrong number"],
        "push": ["I'M REPORTING YOU", "Stop texting me"],
        "empathy": ["...60 seconds", "fine what"],
        "soft": ["Wife keeps nagging, kids are 9, 12, 15", "mortgage is 400k"]
    },
    "sarah": {
        "greet": ["Not a good time", "Kid screaming"],
        "push": ["This is rude", "Im busy"],
        "empathy": ["Rough day... go ahead", "ok what"],
        "soft": ["Daycare is killing me", "husband works nights"]
    },
    "john": {
        "greet": ["Rates for 500k term?", "how much"],
        "push": ["Saw $27/mo online", "just give me a number"],
        "empathy": ["Tell me about living benefits", "that sounds good"],
        "soft": ["Wife wants 750k after baby", "we just had twins"]
    },
    "lisa": {
        "greet": ["Send $25k quote", "email me info"],
        "push": ["Just email rates", "I dont do calls"],
        "empathy": ["Waiting periods scare me", "ok tell me more"],
        "soft": ["Scared of burial costs", "mom just passed"]
    },
    "chris": {
        "greet": ["Just had a baby, need coverage NOW", "ready to start"],
        "push": ["No whole life", "term only"],
        "empathy": ["Lets do this", "when can we talk"],
        "soft": ["Wife wants 1 million", "we have 3 kids under 5"]
    },
    "dave": {
        "greet": ["Yeah that was me 4mo ago, got slammed", "been meaning to"],
        "push": ["Still busy", "maybe next month"],
        "empathy": ["Perfect timing, got a promotion", "ok im listening"],
        "soft": ["Need to protect bonus", "wife doesnt work"]
    },
    "karen": {
        "greet": ["How did you get my number?", "who is this"],
        "push": ["Ive been burned before", "insurance is a scam"],
        "empathy": ["...no pressure right?", "ok but dont push me"],
        "soft": ["Ex left me with nothing", "kids need protection"]
    },
    "tom": {
        "greet": ["...", "k"],
        "push": ["...", "nah"],
        "empathy": ["maybe", "idk"],
        "soft": ["health aint great", "got diabetes"]
    },
    "amanda": {
        "greet": ["Hey I remember filling that out", "oh yeah that"],
        "push": ["Can we make it quick?", "im at work"],
        "empathy": ["Youre right, we keep putting it off", "that makes sense"],
        "soft": ["Husband finally agrees", "we have a new house"]
    },
    "raj": {
        "greet": ["Had a stent, thought I was uninsurable", "can you even help me"],
        "push": ["Most agents hung up", "whats the point"],
        "empathy": ["Wait you can help?", "really?"],
        "soft": ["Kids tuition coming", "need to leave them something"]
    },
}

def classify_vibe(message: str, has_need: bool, booked: bool) -> tuple:
    """Classify message vibe and return (vibe, score) matching our system"""
    msg_lower = message.lower()
    
    if booked or "locked in" in msg_lower or "confirmation" in msg_lower:
        return ("direction", 3.0)  # Will get +2.0 appointment bonus later
    
    if has_need:
        return ("need", 4.0)
    
    if any(w in msg_lower for w in ["sounds good", "tell me more", "ok", "yeah", "sure", "when can"]):
        return ("direction", 3.0)
    
    if any(w in msg_lower for w in ["wife", "kids", "mortgage", "health", "family", "baby"]):
        return ("information", 2.0)
    
    if any(w in msg_lower for w in ["not interested", "too expensive", "dont need", "already have"]):
        return ("objection", 1.0)
    
    if any(w in msg_lower for w in ["stop", "no", "leave me alone", "reporting"]):
        return ("dismissive", 0.5)
    
    return ("neutral", 1.5)


def run_conversation(persona: dict, cycle: int, verbose: bool = True):
    """Run a single simulated conversation"""
    contact_id = f"cow_{persona['id']}_{cycle}_{int(time.time())}"
    trust = persona["trust"]
    turns = 0
    stats = {"questions": 0, "empathy": 0, "offers": 0, "needs": 0, "booked": False, "ghosted": False}
    conversation_history = []
    
    payload = {
        "contact_id": contact_id,
        "first_name": persona["name"],
        "message": "initial outreach - send first message",
        "intent": "initial_outreach",
        "conversation_history": []
    }
    
    if verbose:
        print(f"\n{'='*60}")
        print(f"PERSONA: {persona['name']} ({persona['mood']}) | Trust: {trust} | Ghost: {persona['ghost']:.0%}")
        print(f"{'='*60}")
    
    while turns < 12:
        turns += 1
        
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=25)
            if r.status_code != 200:
                if verbose:
                    print(f"  Turn {turns}: Webhook error {r.status_code}")
                break
            
            data = r.json()
            agent_reply = data.get("reply", "").strip() or "[empty]"
            
            if verbose:
                print(f"  Turn {turns} AGENT: {agent_reply}")
            
            conversation_history.append({"direction": "outbound", "body": agent_reply})
            
            if "?" in agent_reply:
                stats["questions"] += 1
            if any(x in agent_reply.lower() for x in ["understand", "sorry", "fair", "get it", "makes sense", "hear you"]):
                stats["empathy"] += 1
            if any(x in agent_reply.lower() for x in ["6:30", "10:15", "tonight", "tomorrow", "appointment", "works better"]):
                stats["offers"] += 1
            if "confirmation code" in agent_reply.lower() or "locked in" in agent_reply.lower():
                stats["booked"] = True
                if verbose:
                    print(f"  *** APPOINTMENT BOOKED! ***")
                break
            
            bucket = "greet" if turns == 1 else ("empathy" if stats["empathy"] > 0 else "push")
            candidates = RESPONSES[persona["id"]].get(bucket, RESPONSES[persona["id"]]["greet"])
            lead_reply = random.choice(candidates)
            
            ghost_chance = persona["ghost"] * (1 - trust / 100) * (0.7 if stats["empathy"] > 0 else 1.0)
            if random.random() < ghost_chance:
                stats["ghosted"] = True
                if verbose:
                    print(f"  Turn {turns} {persona['name']}: [GHOSTED]")
                break
            
            if trust > 40 and random.random() < 0.4:
                soft_addition = random.choice(RESPONSES[persona["id"]]["soft"])
                lead_reply = f"{lead_reply}. {soft_addition}"
                stats["needs"] += 1
            
            if verbose:
                print(f"  Turn {turns} {persona['name']}: {lead_reply}")
            
            conversation_history.append({"direction": "inbound", "body": lead_reply})
            
            trust += 15 if stats["empathy"] > 0 else -5
            trust = max(0, min(100, trust))
            
            payload["message"] = lead_reply
            payload["conversation_history"] = conversation_history[-10:]
            
            time.sleep(random.uniform(2, 5))
            
        except Exception as e:
            if verbose:
                print(f"  Error: {e}")
            break
    
    vibe, score = classify_vibe(
        conversation_history[-1]["body"] if conversation_history else "",
        stats["needs"] > 0,
        stats["booked"]
    )
    
    if stats["ghosted"]:
        vibe = "ghosted"
        score = -1.0
    
    if stats["booked"]:
        score += 2.0  # Appointment bonus
    
    final_score = score + (stats["empathy"] * 0.5) - (stats["questions"] * 0.1 if stats["questions"] > 3 else 0)
    
    result = {
        "persona": persona["name"],
        "mood": persona["mood"],
        "turns": turns,
        "vibe": vibe,
        "score": round(final_score, 1),
        "booked": stats["booked"],
        "ghosted": stats["ghosted"],
        "needs_revealed": stats["needs"],
        "empathy_shown": stats["empathy"],
        "offers_made": stats["offers"]
    }
    
    if verbose:
        print(f"\n  RESULT: Vibe={vibe} Score={final_score:.1f} Booked={stats['booked']} Ghosted={stats['ghosted']}")
    
    return result


def run_stampede(cycles: int = 5, verbose: bool = True):
    """Run the full training simulation"""
    print(f"\n{'#'*60}")
    print(f"# COW STAMPEDE: {len(PERSONAS)} personas x {cycles} cycles = {len(PERSONAS)*cycles} conversations")
    print(f"# URL: {WEBHOOK_URL}")
    print(f"{'#'*60}")
    
    all_results = []
    
    for cycle in range(1, cycles + 1):
        print(f"\n{'='*60}")
        print(f"CYCLE {cycle}/{cycles}")
        print(f"{'='*60}")
        
        shuffled = PERSONAS.copy()
        random.shuffle(shuffled)
        
        for persona in shuffled:
            result = run_conversation(persona, cycle, verbose)
            all_results.append(result)
        
        if cycle < cycles:
            print(f"\nCycle {cycle} complete. Waiting 5s before next cycle...")
            time.sleep(5)
    
    print(f"\n{'#'*60}")
    print("# FINAL RESULTS")
    print(f"{'#'*60}")
    
    booked = [r for r in all_results if r["booked"]]
    ghosted = [r for r in all_results if r["ghosted"]]
    engaged = [r for r in all_results if r["vibe"] in ("direction", "need", "information")]
    
    print(f"\nTotal Conversations: {len(all_results)}")
    print(f"Appointments Booked: {len(booked)} ({len(booked)/len(all_results)*100:.1f}%)")
    print(f"Ghosted: {len(ghosted)} ({len(ghosted)/len(all_results)*100:.1f}%)")
    print(f"Engaged: {len(engaged)} ({len(engaged)/len(all_results)*100:.1f}%)")
    print(f"Average Score: {sum(r['score'] for r in all_results)/len(all_results):.2f}")
    
    print("\nBY PERSONA:")
    for persona in PERSONAS:
        p_results = [r for r in all_results if r["persona"] == persona["name"]]
        p_booked = len([r for r in p_results if r["booked"]])
        p_ghosted = len([r for r in p_results if r["ghosted"]])
        p_avg = sum(r["score"] for r in p_results) / len(p_results) if p_results else 0
        print(f"  {persona['name']:8} ({persona['mood']:12}): Booked={p_booked} Ghosted={p_ghosted} AvgScore={p_avg:.1f}")
    
    print("\nBY VIBE DISTRIBUTION:")
    vibes = {}
    for r in all_results:
        vibes[r["vibe"]] = vibes.get(r["vibe"], 0) + 1
    for vibe, count in sorted(vibes.items(), key=lambda x: -x[1]):
        print(f"  {vibe:12}: {count} ({count/len(all_results)*100:.1f}%)")
    
    return all_results


if __name__ == "__main__":
    import sys
    
    cycles = 5
    verbose = True
    
    if "--quick" in sys.argv:
        cycles = 1
        print("Quick mode: 1 cycle only")
    
    if "--quiet" in sys.argv:
        verbose = False
        print("Quiet mode: minimal output")
    
    results = run_stampede(cycles=cycles, verbose=verbose)
    
    print(f"\nDone! Your agent processed {len(results)} conversations.")
    print("Patterns are now stored in the outcome_learning database.")
