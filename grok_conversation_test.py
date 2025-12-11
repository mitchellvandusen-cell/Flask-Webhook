#!/usr/bin/env python3
"""
GROK CONVERSATION TEST
Tests the bot with actual multi-turn conversations, not just single messages.
This tests how it handles each message AND the conversation as a whole.

Run: python grok_conversation_test.py
"""

import requests
import time
import json
from datetime import datetime

URL = "https://InsuranceGrokBot.replit.app/grok"

# Define realistic conversation flows
CONVERSATION_SCENARIOS = [
    {
        "name": "Mike",
        "scenario": "Employer Coverage Flow",
        "messages": [
            "Hey who is this",
            "I have coverage through work",
            "Yeah its like 2x my salary I think",
            "I dont know what happens if I leave",
            "Yeah I guess thats a problem",
            "Sure tell me more",
        ]
    },
    {
        "name": "Sarah", 
        "scenario": "Colonial Penn Rescue",
        "messages": [
            "Hi",
            "I already got coverage through Colonial Penn",
            "Yeah I have diabetes",
            "Just pills, metformin",
            "A1C is around 7.2",
            "Really? I didnt know that",
            "Yeah that sounds good",
        ]
    },
    {
        "name": "Tom",
        "scenario": "Skeptic to Believer",
        "messages": [
            "Who is this",
            "Not interested",
            "Ive been burned by insurance agents before",
            "They sold me something I didnt need",
            "I have term through work now",
            "10 years left I think",
            "What do you mean what happens after",
            "I didnt think about that",
            "When can we talk",
        ]
    },
    {
        "name": "Lisa",
        "scenario": "New Parent Urgency",
        "messages": [
            "Hey",
            "Just had a baby need coverage",
            "We dont have anything right now",
            "My husband works but no benefits",
            "How much would it cost",
            "Probably around 500k",
            "Yeah lets set something up",
        ]
    },
    {
        "name": "Dave",
        "scenario": "Hard Exit",
        "messages": [
            "???",
            "I told you Im not interested",
            "Stop texting me",
        ]
    },
    {
        "name": "Karen",
        "scenario": "Price Shopper",
        "messages": [
            "Hi",
            "How much is this gonna cost",
            "I dont know maybe 250k",
            "Its just me no kids",
            "I have term through my job",
            "What do you mean it disappears",
            "Hmm I didnt know that",
            "Yeah maybe we should talk",
        ]
    },
    {
        "name": "Chris",
        "scenario": "Health Condition Qualification",
        "messages": [
            "Hey",
            "I had a heart attack 4 years ago",
            "Im stable now just on blood thinners",
            "No other issues",
            "I thought I couldnt get coverage",
            "Really? Without a waiting period?",
            "Lets do it",
        ]
    },
]


def run_conversation(scenario):
    """Run a single conversation scenario with full history tracking."""
    name = scenario["name"]
    title = scenario["scenario"]
    messages = scenario["messages"]
    
    print("\n" + "=" * 70)
    print(f"SCENARIO: {title}")
    print(f"Lead: {name}")
    print("=" * 70)
    
    conversation_history = []
    
    for i, message in enumerate(messages, 1):
        print(f"\n--- Exchange {i}/{len(messages)} ---")
        print(f"[{name}]: {message}")
        
        payload = {
            "firstName": name,
            "message": message,
            "conversationHistory": conversation_history
        }
        
        try:
            start = time.time()
            r = requests.post(URL, json=payload, timeout=60)
            elapsed = time.time() - start
            
            if r.status_code == 200:
                reply = r.json().get("reply", "")
                print(f"[BOT]:  {reply}")
                print(f"        ({elapsed:.1f}s)")
                
                # Build conversation history like real system would
                conversation_history.append(f"Lead: {message}")
                conversation_history.append(f"You: {reply}")
            else:
                print(f"[ERROR]: Status {r.status_code}")
                print(r.text[:200])
                
        except requests.exceptions.Timeout:
            print("[ERROR]: Request timed out")
        except Exception as e:
            print(f"[ERROR]: {e}")
        
        # Simulate human reading/typing delay
        time.sleep(2)
    
    print(f"\n{'='*70}")
    print(f"CONVERSATION COMPLETE: {title}")
    print(f"Total exchanges: {len(messages)}")
    print(f"Final history length: {len(conversation_history)} messages")
    print("=" * 70)


def main():
    print("\n" + "#" * 70)
    print("# GROK CONVERSATION TEST")
    print(f"# Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"# Endpoint: {URL}")
    print(f"# Scenarios: {len(CONVERSATION_SCENARIOS)}")
    print("#" * 70)
    
    # Test health endpoint first
    print("\nTesting health endpoint...")
    try:
        r = requests.get(URL.replace("/grok", "/health"), timeout=10)
        print(f"Health check: {r.status_code}")
    except:
        print("Health check failed - endpoint may be cold starting")
    
    # Run each scenario
    for i, scenario in enumerate(CONVERSATION_SCENARIOS, 1):
        print(f"\n\n{'*' * 70}")
        print(f"RUNNING SCENARIO {i}/{len(CONVERSATION_SCENARIOS)}")
        print("*" * 70)
        
        run_conversation(scenario)
        
        # Pause between scenarios
        if i < len(CONVERSATION_SCENARIOS):
            print("\n[Waiting 5 seconds before next scenario...]\n")
            time.sleep(5)
    
    print("\n\n" + "#" * 70)
    print("# ALL SCENARIOS COMPLETE")
    print(f"# Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("#" * 70 + "\n")


if __name__ == "__main__":
    main()
