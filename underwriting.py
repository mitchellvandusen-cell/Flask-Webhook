# underwriting.py - Live Carrier Underwriting Engine (Flawless 2026)
import logging
import requests
import csv
import io
import re
import os
import json
import threading
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

# === LIVE GOOGLE SHEET SOURCES ===
SHEET_URLS = {
    "whole_life": "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1599052257&single=true&output=csv",
    "term_iul": "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1023819925&single=true&output=csv",
    "uhl": "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1225036935&single=true&output=csv"
}

# Local fallback file path (used when Google Sheets is unreachable)
_FALLBACK_FILE = os.path.join(os.path.dirname(__file__), "underwriting_fallback.json")

# Cache (in-memory, TTL 60 min) with thread safety
_CACHE: dict = {
    "rules": [],
    "last_updated": None,
    "ttl_seconds": 3600  # 60 minutes
}
_cache_lock = threading.Lock()  # Thread-safe cache access

def refresh_underwriting_data(force: bool = False) -> List[str]:
    """
    Fetches and merges all underwriting sheets into one searchable list.
    Returns cached data if fresh, or refreshes if expired/forced.
    """
    now = datetime.now()

    # Check cache with lock
    with _cache_lock:
        if not force and _CACHE["rules"] and _CACHE["last_updated"]:
            age = (now - _CACHE["last_updated"]).total_seconds()
            if age < _CACHE["ttl_seconds"]:
                logger.debug(f"Underwriting cache hit (age: {age:.0f}s)")
                return _CACHE["rules"].copy()  # Return copy to prevent external modification

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

        # Update cache with lock
        with _cache_lock:
            _CACHE["rules"] = combined_rules
            _CACHE["last_updated"] = now
        logger.info(f"Underwriting data refreshed: {len(combined_rules)} rules loaded")

        # Save to local fallback so we survive Google Sheets outages
        try:
            with open(_FALLBACK_FILE, "w") as f:
                json.dump(combined_rules, f)
            logger.debug(f"Underwriting fallback saved ({len(combined_rules)} rules)")
        except Exception as fe:
            logger.debug(f"Could not save underwriting fallback: {fe}")

    except requests.RequestException as e:
        logger.error(f"Underwriting fetch failed: {e}")
        # Return old cache if available
        with _cache_lock:
            if _CACHE["rules"]:
                return _CACHE["rules"].copy()
        # Load from local fallback file
        return _load_fallback()
    except Exception as e:
        logger.error(f"Unexpected underwriting refresh error: {e}")
        with _cache_lock:
            if _CACHE["rules"]:
                return _CACHE["rules"].copy()
        return _load_fallback()

    return combined_rules


def _load_fallback() -> List[str]:
    """Load underwriting rules from local fallback JSON file."""
    try:
        if os.path.exists(_FALLBACK_FILE):
            with open(_FALLBACK_FILE, "r") as f:
                rules = json.load(f)
            logger.info(f"Loaded {len(rules)} underwriting rules from local fallback")
            with _cache_lock:
                _CACHE["rules"] = rules
                _CACHE["last_updated"] = datetime.now()
            return rules
    except Exception as e:
        logger.error(f"Failed to load underwriting fallback: {e}")
    return []

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
        "diabetes": r"\bdiabetes\b|\bdiabetic\b|\bblood sugar\b|\binsulin\b|\ba1c\b|\btype [12]\b",
        "cancer": r"\bcancer\b|\btumor\b|\bchemo\b|\boncology\b|\bremission\b|\bmalignant\b",
        "heart": r"\bheart (?:attack|disease|condition|failure|surgery|problem|issue|murmur)\b|\bcardiac\b|\bchf\b|\bangina\b|\bstent\b|\bbypass\b|\bpacemaker\b|\bheart\b(?=.*\b(?:doctor|hospital|diagnosed|surgery|medication|meds))",
        "stroke": r"\bstroke\b|\bcva\b|\btia\b",
        "copd": r"\bcopd\b|\bemphysema\b|\bchronic bronchitis\b|\basthma\b|\blung disease\b",
        "blood pressure": r"\bblood pressure\b|\bhypertension\b|\bhigh bp\b",
        "kidney": r"\bkidney\b|\brenal\b|\bdialysis\b",
        "liver": r"\bliver\b|\bcirrhosis\b|\bhepatitis\b",
        "mental health": r"\bdepression\b|\banxiety\b|\bbipolar\b|\bschizophren\b|\bptsd\b|\bmental health\b",
        "medication": r"\btaking meds\b|\bprescription\b|\bon medication\b|\btake medication\b|\bon meds\b|\btake meds\b|\bmetformin\b|\blisinopril\b|\batorvastatin\b|\bamlodipine\b|\blosartan\b|\bomeprazole\b|\blevothyroxine\b|\bgabapentin\b|\bhydrochlorothiazide\b|\bsimvastatin\b|\bsertraline\b|\bzoloft\b|\blexapro\b|\bprozac\b|\bxanax\b|\bwellbutrin\b|\btrazodone\b|\binsulin\b|\bglipizide\b|\bglyburide\b|\bjardiancе\b|\bozempic\b|\bmounjaro\b|\beliquis\b|\bwarfarin\b|\bplavix\b|\bnitroglycer\b",
        "obesity": r"\bobese\b|\bobesity\b|\bbmi\b|\boverweight\b",
        "sleep apnea": r"\bsleep apnea\b|\bcpap\b",
        "autoimmune": r"\blupus\b|\brheumatoid\b|\bms\b|\bmultiple sclerosis\b|\bcrohns?\b|\bcolitis\b",
        "diagnosed": r"\bdiagnosed\b|\bdiagnosis\b",
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
4. Stay empathetic, never scare, always offer hope/solutions.
5. Keep reply natural and under 35 words.
"""