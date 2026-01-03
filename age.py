# age.py
from datetime import date
import logging

logger = logging.getLogger(__name__)

def calculate_age_from_dob(date_of_birth: str) -> str:
    """
    Calculate age from DOB string in 'YYYY-MM-DD' format.
    Returns age as string or "unknown" on failure.
    """
    if not date_of_birth:
        return "unknown"

    try:
        dob_parts = date_of_birth.split("-")
        if len(dob_parts) < 3:
            return "unknown"

        birth_year = int(dob_parts[0])
        birth_month = int(dob_parts[1])
        birth_day = int(dob_parts[2])

        today = date.today()
        age = today.year - birth_year
        # Adjust if birthday hasn't happened yet this year
        if (today.month, today.day) < (birth_month, birth_day):
            age -= 1

        return str(age)
    except (ValueError, IndexError) as e:
        logger.debug(f"Invalid DOB format '{date_of_birth}': {e}")
        return "unknown"