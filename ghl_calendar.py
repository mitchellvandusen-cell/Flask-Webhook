from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
import re
import os
import logging
import requests

logger = logging.getLogger(__name__)

api_key = os.getenv("GHL_API_KEY")
location_id = os.getenv("GHL_LOCATION_ID")
cal_id = os.getenv("GHL_CALENDAR_ID")

def make_json_serializable(obj):
    """Convert datetime objects to ISO strings for JSON serialization"""
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat() if obj else None
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_serializable(item) for item in obj]
    return obj
# === YOUR FIXED CONSTANTS ===
CALENDAR_ID = "S4knucFaXO769HDFlRtv"
GHL_USER_ID = "BhWQCdIwX0Ci0OiRAewU"          # Your userId - never changes
CACHE_TTL = 1800  # 30 minutes

cache = {}  # Simple in-memory cache

def get_cached_data(key):
    if key in cache:
        cached = cache[key]
        if (datetime.now(timezone.utc) - cached['time']) < timedelta(seconds=CACHE_TTL):
            return cached['data']
    return None

def set_cache(key, data):
    cache[key] = {'data': data, 'time': datetime.now(timezone.utc)}

def consolidated_calendar_op(
    operation: str,
    contact_id: str = None,
    first_name: str = None,
    selected_time: str = None,
    appointment_id: str = None,
    reason: str = None,
    calendar_id: str = None
) -> any:
    api_key = os.environ.get("GHL_API_KEY")
    location_id = os.environ.get("GHL_LOCATION_ID")
    cal_id = calendar_id or os.environ.get("GHL_CALENDAR_ID")
    CALENDAR_TIMEZONE = "America/Chicago"  # Hardcoded - matches your calendar

    if not all([api_key, location_id, cal_id]):
        logger.error("Missing credentials")
        return "let me look at my calendar" if operation == "fetch_slots" else False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }

    local_tz = ZoneInfo(CALENDAR_TIMEZONE)

    # === FETCH FREE SLOTS ===
    slots_key = f"ghl_slots_{cal_id}"
    slots = get_cached_data(slots_key)

    if not slots or operation in ["book", "fetch_slots"]:
        url = f"https://services.leadconnectorhq.com/calendars/{cal_id}/free-slots"

        now_utc = datetime.now(timezone.utc)
        start_ts = int(now_utc.timestamp() * 1000)
        end_ts = int((now_utc + timedelta(days=29)).timestamp() * 1000)  # ← Fixed: safe under 31

        params = {
            "startDate": start_ts,
            "endDate": end_ts,
            "timezone": CALENDAR_TIMEZONE
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()

            slots = []
            if isinstance(data, dict):
                for entry in data.values():
                    if isinstance(entry, dict) and "slots" in entry:
                        slots.extend(entry["slots"])
                    elif isinstance(entry, list):
                        slots.extend(entry)
            elif isinstance(data, list):
                slots = data

            set_cache(slots_key, slots)
            logger.info(f"Fetched {len(slots)} slots")
        except Exception as e:
            logger.error(f"Slots error: {e}")
            slots = []

    # === PARSE TIME ===
    start_time_iso = end_time_iso = None
    if selected_time and operation in ["book", "reschedule", "block"]:
        time_str = selected_time.lower().strip()
        now_local = datetime.now(local_tz)

        target_date = (now_local + timedelta(days=1)).date() if "tomorrow" in time_str else now_local.date()

        hour, minute = 14, 0
        match = re.search(r'(\d{1,2}):?(\d{2})?\s*(pm|p\.m\.|am|a\.m\.|o\'?clock)?', time_str)
        if match:
            h = int(match.group(1))
            m = int(match.group(2) or 0)
            period = (match.group(3) or "").lower()
            if period in ["pm", "p.m."] and h != 12:
                h += 12
            elif period in ["am", "a.m."] and h == 12:
                h = 0
            hour, minute = h, m

        hour = max(8, min(16, hour))

        start_dt = datetime.combine(target_date, time(hour, minute), tzinfo=local_tz)
        end_dt = start_dt + timedelta(minutes=30)

        # === HARD 2-DAY LIMIT ===
        max_date = (now_local + timedelta(days=2)).date()
        if start_dt.date() > max_date:
            logger.info("Booking too far ahead - blocked")
            return False

        start_time_iso = start_dt.isoformat()
        end_time_iso = end_dt.isoformat()

    # === BOOK ===
        if operation == "book":
            if not (contact_id and start_time_iso):
                logger.warning("Booking failed: missing contact_id or time")
                return False

        payload = {
            "calendarId": cal_id,
            "contactId": contact_id,
            "startTime": start_time_iso,
            "endTime": end_time_iso,
            "title": f"Life Insurance Review with {first_name or 'Contact'}",
            "appointmentStatus": "confirmed",
            "assignedUserId": GHL_USER_ID,           # Remove this line if you disable the assignment setting
            "selectedTimezone": CALENDAR_TIMEZONE,
        }

        url = "https://services.leadconnectorhq.com/calendars/events/appointments"
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code in [200, 201]:
                logger.info(f"SUCCESS: Appointment booked for {contact_id} at {start_time_iso}")
                return True
            else:
                logger.error(f"Booking failed {response.status_code}: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Booking exception: {e}")
            return False

    elif operation == "reschedule":
        if not (appointment_id and start_time_iso):
            return False

        payload = {
            "startTime": start_time_iso,
            "endTime": end_time_iso,
            "appointmentStatus": "confirmed",
            "assignedUserId": GHL_USER_ID,
        }

        url = f"https://services.leadconnectorhq.com/calendars/events/appointments/{appointment_id}"
        try:
            response = requests.put(url, json=payload, headers=headers, timeout=30)
            if response.status_code in [200, 204]:
                logger.info(f"Rescheduled appointment {appointment_id}")
                return True
            else:
                logger.error(f"Reschedule failed: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Reschedule exception: {e}")
            return False

    elif operation == "block":
        if not start_time_iso:
            return False

        payload = {
            "calendarId": cal_id,
            "startTime": start_time_iso,
            "endTime": end_time_iso,
            "title": reason or "Personal / Blocked Time",
        }

        url = "https://services.leadconnectorhq.com/calendars/events/block-slots"
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code in [200, 201]:
                logger.info(f"Time blocked: {start_time_iso}")
                return True
            else:
                logger.error(f"Block failed: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Block exception: {e}")
            return False

    elif operation == "fetch_slots":
        if not slots:
            return "let me look at my calendar"

        parsed_slots = []
        for slot in slots:
            try:
                start_str = (
                    slot.get("startTime") or
                    slot.get("start") or
                    (slot if isinstance(slot, str) else None)
                )
                if not start_str:
                    continue
                if start_str.endswith("Z"):
                    start_str = start_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(start_str).astimezone(local_tz)
                if 8 <= dt.hour < 17:  # Your open hours: 8am to 5pm
                    parsed_slots.append(dt)
            except Exception:
                continue

        if not parsed_slots:
            return "let me look at my calendar"

        parsed_slots.sort()
        now_local = datetime.now(local_tz)

        # Morning: 8:00–11:59, Afternoon: 1:00–4:59
        morning_slots = [s for s in parsed_slots if 8 <= s.hour < 12]
        afternoon_slots = [s for s in parsed_slots if 13 <= s.hour < 17]

        def pick_best(slots_list, max_picks=2):
            if not slots_list:
                return []
            picked = [slots_list[0]]
            for s in slots_list[1:]:
                if len(picked) >= max_picks:
                    break
                if (s - picked[-1]).total_seconds() >= 3600:  # At least 1 hour apart
                    picked.append(s)
            return picked

        morning_picks = pick_best(morning_slots)
        afternoon_picks = pick_best(afternoon_slots)

        def format_slot(dt):
            time_str = dt.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
            day = "tomorrow" if dt.date() == (now_local + timedelta(days=1)).date() else dt.strftime("%A")
            return f"{time_str} {day}"

        formatted = []
        if morning_picks:
            formatted.append(" or ".join(format_slot(s) for s in morning_picks) + " morning")
        if afternoon_picks:
            formatted.append(" or ".join(format_slot(s) for s in afternoon_picks) + " afternoon")

        if not formatted:
            return "let me look at my calendar"

        response_text = "I've got " + (", or ".join(formatted) if len(formatted) > 1 else formatted[0])
        return response_text

    return False