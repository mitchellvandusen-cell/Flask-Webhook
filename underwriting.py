# underwriting.py - Live Carrier Underwriting Engine (Flawless 2026)
import logging
import requests
import csv
import io
import re
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

# === LIVE GOOGLE SHEET SOURCES ===
SHEET_URLS = {
    "whole_life": "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1599052257&single=true&output=csv",
    "term_iul": "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1023819925&single=true&output=csv",
    "uhl": "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1225036935&single=true&output=csv"
}

# Cache (in-memory, TTL 60 min)
_CACHE: dict = {
    "rules": [],
    "last_updated": None,
    "ttl_seconds": 3600  # 60 minutes
}

def refresh_underwriting_data(force: bool = False) -> List[str]:
    """
    Fetches and merges all underwriting sheets into one searchable list.
    Returns cached data if fresh, or refreshes if expired/forced.
    """
    now = datetime.now()
    if not force and _CACHE["rules"] and _CACHE["last_updated"]:
        age = (now - _CACHE["last_updated"]).total_seconds()
        if age < _CACHE["ttl_seconds"]:
            logger.debug(f"Underwriting cache hit (age: {age:.0f}s)")
            return _CACHE["rules"]

    combined_rules = []
    try:
        for source_name, url in SHEET_URLS.items():
            resp = requests.get(url, timeout=12)
            resp.raise_for_status()

            reader = csv.reader(io.StringIO(resp.text))
            rows = list(reader)

            # Skip header if it looks like one
            if rows and any("condition" in str(cell).lower() for cell in rows[0]):
                rows = rows[1:]

            for row in rows:
                if any(cell.strip() for cell in row):  # skip empty
                    rule_str = f"[{source_name.upper()}] " + " | ".join(str(cell).strip() for cell in row if cell.strip())
                    combined_rules.append(rule_str)

        _CACHE["rules"] = combined_rules
        _CACHE["last_updated"] = now
        logger.info(f"Underwriting data refreshed: {len(combined_rules)} rules loaded")

    except requests.RequestException as e:
        logger.error(f"Underwriting fetch failed: {e}")
        # Return old cache if available
        return _CACHE["rules"]
    except Exception as e:
        logger.error(f"Unexpected underwriting refresh error: {e}")
        return _CACHE["rules"]

    return combined_rules

def get_underwriting_context(message: str) -> str:
    """
    Detects health-related keywords in message and returns relevant carrier rules.
    Returns empty string if no health context detected.
    """
    if not message or len(message.strip()) < 5:
        return ""

    msg_lower = message.lower().strip()

    # Expanded, realistic health triggers (common insurance conditions)
    health_triggers = {
        "diabetes": "diabetes|diabetic|sugar|insulin",
        "cancer": "cancer|tumor|chemo|oncology",
        "heart": "heart|cardiac|attack|chf|angina",
        "stroke": "stroke|cva|tia",
        "copd": "copd|emphysema|chronic bronchitis",
        "blood pressure": "blood pressure|hypertension|high bp",
        "kidney": "kidney|renal|dialysis",
        "liver": "liver|cirrhosis|hepatitis",
        "medication": "taking|meds|prescription|drug",
        "diagnosed": "diagnosed|diagnosis"
    }

    detected = []
    for condition, regex in health_triggers.items():
        if re.search(regex, msg_lower):
            detected.append(condition)

    if not detected:
        return ""

    # Refresh data only if needed
    rules = refresh_underwriting_data()

    relevant_rules = []
    for rule in rules:
        rule_lower = rule.lower()
        for cond in detected:
            if cond in rule_lower:
                relevant_rules.append(rule)
                break  # One match per rule is enough

    if not relevant_rules:
        return f"[UNDERWRITING NOTE] Lead mentioned health issue ({', '.join(detected)}). No specific carrier rules found in sheets. Ask for diagnosis date, treatment, and severity."

    # Format top matches (limit to 5 to save tokens)
    context_lines = relevant_rules[:5]
    context = "\n".join(context_lines)

    return f"""
[LIVE UNDERWRITING DATA RETRIEVED]
Detected conditions: {', '.join(detected).title()}
Relevant carrier rules:
{context}

INSTRUCTIONS FOR RESPONSE:
1. Compare lead's situation (from message + narrative) to rules above.
2. If timeframes match 'Decline' or 'Postpone', pivot to Guaranteed Issue or simplified issue options.
3. If timeframe unclear, ask calmly: "How long ago was that diagnosed, and are you still treating it?"
4. Stay empathetic — never scare, always offer hope/solutions.
5. Keep reply natural and under 35 words.
"""