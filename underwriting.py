# underwriting.py - Live Carrier Data Engine
import logging
import requests
import csv
import io
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# === LIVE GOOGLE SHEET DATA SOURCES ===
SHEET_URLS = {
    "whole_life": "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1599052257&single=true&output=csv",
    "term_iul": "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1023819925&single=true&output=csv",
    "uhl": "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1225036935&single=true&output=csv"
}

# Memory Cache (Prevents hitting Google 1000 times/minute)
_CACHE = {
    "data": [],
    "last_updated": None
}

def refresh_underwriting_data():
    """Fetches and merges all underwriting sheets into one searchable knowledge base."""
    global _CACHE
    
    # Only refresh if cache is empty or older than 60 minutes
    if _CACHE["data"] and _CACHE["last_updated"]:
        if datetime.now() - _CACHE["last_updated"] < timedelta(minutes=60):
            return _CACHE["data"]

    combined_rules = []
    try:
        for source, url in SHEET_URLS.items():
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                # Parse CSV
                rows = list(csv.reader(io.StringIO(response.text)))
                # Skip header if it looks like one (simple check)
                if rows and "condition" in str(rows[0]).lower():
                    rows = rows[1:]
                
                for row in rows:
                    # Filter empty rows
                    if any(cell.strip() for cell in row):
                        combined_rules.append(f"[{source.upper()}] " + " | ".join(row))
        
        _CACHE["data"] = combined_rules
        _CACHE["last_updated"] = datetime.now()
        logger.info(f"Underwriting DB updated: {len(combined_rules)} rules loaded.")
    except Exception as e:
        logger.error(f"Failed to update underwriting data: {e}")
        # If fetch fails, return old cache or empty list to prevent crash
        return _CACHE["data"]

    return _CACHE["data"]

def get_underwriting_context(message: str) -> str:
    """
    Scans the message for medical keywords. 
    If found, searches the Live Sheet Data and returns specific rules.
    """
    if not message: 
        return ""

    msg_lower = message.lower()
    
    # 1. Detect if this is a health-related message
    health_triggers = ["diabetes", "cancer", "heart", "stroke", "copd", "blood pressure", 
                       "neuropathy", "kidney", "liver", "medication", "taking", "diagnosed"]
    
    detected_condition = None
    for trigger in health_triggers:
        if trigger in msg_lower:
            detected_condition = trigger
            break
    
    if not detected_condition:
        return ""

    # 2. Search the Live Data
    rules = refresh_underwriting_data()
    relevant_rules = []
    
    # Simple keyword match in the rules
    for rule in rules:
        if detected_condition in rule.lower():
            relevant_rules.append(rule)

    if not relevant_rules:
        return f"[UNDERWRITING NOTE] Lead mentioned '{detected_condition}'. No specific rule found in sheets. Ask for diagnosis date and treatment details."

    # 3. Format for the Bot
    # We limit to top 5 matching rules to save tokens
    context = "\n".join(relevant_rules[:5])
    
    return f"""
[LIVE UNDERWRITING DATA RETRIEVED]
Condition Detected: {detected_condition.upper()}
Carrier Rules Found:
{context}

INSTRUCTION: 
1. Compare the lead's situation to the rules above.
2. If timeframes (e.g., '2 years ago') match a 'Decline', pivot to Guaranteed Issue.
3. If no timeframe given, ASK: "How long has it been since that diagnosis/treatment?"
"""