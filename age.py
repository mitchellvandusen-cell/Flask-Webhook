# age.py
from datetime import date, datetime
import logging

logger = logging.getLogger(__name__)

def calculate_age_from_dob(date_of_birth: str) -> str:
    """
    Calculate age from DOB string. 
    Handles 'YYYY-MM-DD', ISO timestamps, and 'MM/DD/YYYY'.
    Returns age as string or "unknown" on failure.
    """
    if not date_of_birth or not isinstance(date_of_birth, str):
        return "unknown"

    # 1. Clean the string (remove timestamps if present)
    # GHL often sends "1990-05-01T00:00:00Z"
    clean_dob = date_of_birth.split("T")[0].strip()

    # 2. Try common formats
    formats = [
        "%Y-%m-%d",  # 1990-05-01
        "%m/%d/%Y",  # 05/01/1990
        "%Y/%m/%d",  # 1990/05/01
        "%d-%m-%Y"   # 01-05-1990
    ]

    for fmt in formats:
        try:
            dob_dt = datetime.strptime(clean_dob, fmt).date()
            today = date.today()
            age = today.year - dob_dt.year
            
            # 3. Adjust if birthday hasn't happened yet this year
            if (today.month, today.day) < (dob_dt.month, dob_dt.day):
                age -= 1
            
            return str(age)
        except (ValueError, IndexError):
            continue

    logger.debug(f"Could not parse DOB format: '{date_of_birth}'")
    return "unknown"