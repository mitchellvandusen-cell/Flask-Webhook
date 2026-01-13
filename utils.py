# utils.py
from datetime import datetime, date, time
from typing import Any
import re
def make_json_serializable(obj: Any) -> Any:
    """Convert datetime objects to ISO strings for JSON serialization"""
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat() if obj else None
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_serializable(item) for item in obj]
    return obj

def clean_ai_reply(text):
    if not text:
        return ""
    
    # 1. Replace Em-dashes (—) and En-dashes (–) with a comma + space
    text = re.sub(r'[—–]', ', ', text)
    
    # 2. Replace " - " (hyphen with spaces) with comma + space
    # We do this specifically so we don't break words like "long-term"
    text = text.replace(' - ', ', ')
    
    # 3. Clean up any accidental double spaces or double commas created
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace(',,', ',')
    text = text.replace(' ,', ',')
    
    return text