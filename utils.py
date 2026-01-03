# utils.py
from datetime import datetime, date, time
from typing import Any

def make_json_serializable(obj: Any) -> Any:
    """Convert datetime objects to ISO strings for JSON serialization"""
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat() if obj else None
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_serializable(item) for item in obj]
    return obj