import logging
import requests
import csv
import io
import re
logger = logging.getLogger(__name__)
# === LIVE UNDERWRITING GUIDES FROM GOOGLE SHEETS ===
WHOLE_LIFE_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1599052257&single=true&output=csv"
TERM_IUL_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1023819925&single=true&output=csv"
UHL_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTysHNk28dg31uTaucHDWi6hLBSs13L1J6V_s71MSygV5gyrwsJuALLvWIg9b-aKg/pub?gid=1225036935&single=true&output=csv"

def fetch_underwriting_data(url):
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return list(csv.reader(io.StringIO(response.text)))
    except Exception as e:
        logger.error(f"Failed to fetch underwriting data from {url}: {e}")
        return []

logger.info("Success accessing Underwriting Link")
WHOLE_LIFE_DATA = fetch_underwriting_data(WHOLE_LIFE_SHEET_URL)
TERM_IUL_DATA = fetch_underwriting_data(TERM_IUL_SHEET_URL)
UHL_DATA = fetch_underwriting_data(UHL_SHEET_URL)

UNDERWRITING_DATA = WHOLE_LIFE_DATA + TERM_IUL_DATA + UHL_DATA

def search_underwriting(condition, product_hint=""):
    if not UNDERWRITING_DATA:
        return []
    condition_lower = condition.lower()
    hint_lower = product_hint.lower()
    results = []
    for row in UNDERWRITING_DATA:
        if not row or len(row) < 2:
            continue
        row_text = " ".join(str(cell).strip() for cell in row if cell).lower()
        if condition_lower in row_text:
            score = 5
            if hint_lower and hint_lower in row_text:
                score += 5
            results.append((score, row))
    results.sort(reverse=True, key=lambda x: x[0])
    return [row for _, row in results[:6]]


HEALTH_TRIGGER_PATTERNS = [
    r"\b(i'?m|I have|had|diagnosed|take|taking|on|use|using)\b.*\b(med|medication|pill|condition|disease|issue|problem|surgery|attack|stroke|cancer|diabetes|heart|copd|oxygen|dialysis|transplant)\b",
    r"\bhealth\b.*\b(question|issue|problem|condition)\b",
    r"\b(take|taking|on)\b.*\b(medication|pill|drug)\b",
    r"\bdiagnosed\b|\bdiagnosis\b",
    r"\bhad a\b.*\b(attack|stroke|surgery)\b",
]

def get_underwriting_context(message: str) -> str:
    """
    Check if message mentions health/conditions and return appropriate context.
    Returns default string if no health mention.
    """
    if not message:
        return "No health conditions mentioned."

    lower_msg = message.lower()
    
    if any(re.search(pattern, lower_msg) for pattern in HEALTH_TRIGGER_PATTERNS):
        # Extract potential condition/med
        words = re.split(r'\W+', lower_msg)
        potential_conditions = [w for w in words if len(w) > 4 and w not in ["have", "taking", "take", "diagnosed", "health"]]
        
        if potential_conditions:
            primary = potential_conditions[0]
            matches = search_underwriting(primary)  # your existing function
            
            if matches:
                guide_text = " ".join([" ".join(str(cell) for cell in row if cell) for row in matches]).lower()
                
                if any(kw in guide_text for kw in ["decline", "uninsurable", "postpone"]):
                    return f"""
KNOCKOUT CONDITION DETECTED: '{primary}' likely declined.
DO NOT book standard appointment.
Respond empathetically and offer final expense or close gracefully.
"""
                elif any(kw in guide_text for kw in ["within", "year", "months", "since", "waiting period"]):
                    return f"""
TIME-BASED CONDITION: '{primary}' has waiting period.
ASK: "When was that diagnosed/happened?"
DO NOT book until confirmed.
"""
                else:
                    return f"""
CONDITION '{primary}' mentioned — possible coverage.
Ask clarifying questions: timing, control, medications.
"""
            else:
                return f"""
Condition/medication '{primary}' not in current guide.
Be cautious. Ask: "I'll need more info on that — can you tell me more?"
"""
    
    return "No health conditions mentioned."