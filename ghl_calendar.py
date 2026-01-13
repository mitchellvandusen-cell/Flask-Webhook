# ghl_calendar.py - Calendar Slots & Booking (Flawless 2026)
import logging
import os
import requests
import time as time_module
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import re

logger = logging.getLogger(__name__)

GHL_CALENDAR_URL = "https://services.leadconnectorhq.com/calendars/{cal_id}/free-slots"
GHL_BOOK_URL = "https://services.leadconnectorhq.com/calendars/events/appointments"

CACHE_TTL = 1800  # 30 minutes
cache = {}  # Simple in-memory cache — fine for single-worker RQ

def get_cached_data(key: str):
    if key in cache:
        cached = cache[key]
        if (datetime.now(timezone.utc) - cached['time']) < timedelta(seconds=CACHE_TTL):
            return cached['data']
    return None

def set_cache(key: str, data):
    cache[key] = {'data': data, 'time': datetime.now(timezone.utc)}

def consolidated_calendar_op(
    operation: str,
    subscriber_data: dict,
    contact_id: str = None,
    first_name: str = None,
    selected_time: str = None
) -> any:
    """
    Unified calendar operation: fetch slots or book appointment.
    Returns formatted string (slots) or bool (booking success).
    Demo-safe: returns placeholder on demo mode.
    """
    access_token = subscriber_data.get("access_token") or subscriber_data.get("crm_api_key")
    location_id = subscriber_data.get("location_id")
    cal_id = subscriber_data.get("calendar_id")
    crm_user_id = subscriber_data.get("crm_user_id")
    local_tz_str = subscriber_data.get("timezone", "America/Chicago")

    if not access_token or not cal_id:
        logger.error(f"Missing credentials for calendar op (loc={location_id})")
        return "let me look at my calendar" if operation == "fetch_slots" else False

    # Demo mode short-circuit
    if access_token == 'DEMO':
        if operation == "fetch_slots":
            return "I've got tomorrow morning or afternoon — let me know what works!"
        if operation == "book":
            logger.info(f"DEMO MODE: Simulated booking for {contact_id}")
            return True

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }

    local_tz = ZoneInfo(local_tz_str)

    # === FETCH SLOTS ===
    if operation in ["fetch_slots", "book"]:
        slots_key = f"ghl_slots_{cal_id}_{crm_user_id or 'default'}"
        slots = get_cached_data(slots_key)

        if not slots:
            url = GHL_CALENDAR_URL.format(cal_id=cal_id)
            now_utc = datetime.now(timezone.utc)
            start_ts = int(now_utc.timestamp() * 1000)
            end_ts = int((now_utc + timedelta(days=29)).timestamp() * 1000)

            params = {
                "startDate": start_ts,
                "endDate": end_ts,
                "timezone": local_tz_str
            }
            if crm_user_id:
                params["userId"] = crm_user_id

            try:
                resp = requests.get(url, headers=headers, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()

                slots = []
                if isinstance(data, dict):
                    for entry in data.values():
                        if isinstance(entry, list):
                            slots.extend(entry)
                        elif isinstance(entry, dict) and "slots" in entry:
                            slots.extend(entry["slots"])
                elif isinstance(data, list):
                    slots = data

                set_cache(slots_key, slots)
                logger.info(f"Fetched {len(slots)} slots for {cal_id}")
            except Exception as e:
                logger.error(f"Calendar fetch error: {e}")
                slots = []

        if operation == "fetch_slots":
            if not slots:
                return "let me look at my calendar"

            parsed_slots = []
            for slot in slots:
                try:
                    start_str = slot.get("startTime") or slot.get("start") or (slot if isinstance(slot, str) else None)
                    if not start_str:
                        continue
                    # Normalize timezone suffixes
                    if start_str.endswith("Z"):
                        start_str = start_str.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(start_str).astimezone(local_tz)
                    if 8 <= dt.hour < 17:
                        parsed_slots.append(dt)
                except Exception:
                    continue

            if not parsed_slots:
                return "let me look at my calendar"

            parsed_slots.sort()
            now_local = datetime.now(local_tz)

            morning = [s for s in parsed_slots if 8 <= s.hour < 12]
            afternoon = [s for s in parsed_slots if 13 <= s.hour < 17]

            def pick_best(slots_list, max_picks=2):
                if not slots_list:
                    return []
                picked = [slots_list[0]]
                for s in slots_list[1:]:
                    if len(picked) >= max_picks:
                        break
                    if (s - picked[-1]).total_seconds() >= 3600:  # 1-hour spread
                        picked.append(s)
                return picked

            morning_picks = pick_best(morning)
            afternoon_picks = pick_best(afternoon)

            def format_slot(dt):
                day = "tomorrow" if dt.date() == (now_local + timedelta(days=1)).date() else dt.strftime("%A")
                time_str = dt.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
                return f"{time_str} {day}"

            options = []
            if morning_picks:
                options.append(" or ".join(format_slot(s) for s in morning_picks) + " morning")
            if afternoon_picks:
                options.append(" or ".join(format_slot(s) for s in afternoon_picks) + " afternoon")

            if not options:
                return "let me look at my calendar"

            return "I've got " + (", or ".join(options) if len(options) > 1 else options[0])

    # === BOOK APPOINTMENT ===
    if operation == "book" and selected_time and contact_id:
        time_str = selected_time.lower().strip()
        now_local = datetime.now(local_tz)
        target_date = (now_local + timedelta(days=1)).date() if "tomorrow" in time_str else now_local.date()

        hour, minute = 14, 0
        match = re.search(r'(\d{1,2}):?(\d{2})?\s*(pm|p\.m\.|am|a\.m\.)?', time_str)
        if match:
            h = int(match.group(1))
            m = int(match.group(2) or 0)
            period = (match.group(3) or "").lower()
            if "pm" in period and h != 12:
                h += 12
            elif "am" in period and h == 12:
                h = 0
            hour, minute = h, m

        hour = max(8, min(16, hour))

        start_dt = datetime.combine(target_date, time(hour, minute), tzinfo=local_tz)
        end_dt = start_dt + timedelta(minutes=30)

        if start_dt.date() > (now_local + timedelta(days=2)).date():
            logger.warning("Booking request too far ahead")
            return False

        payload = {
            "calendarId": cal_id,
            "contactId": contact_id,
            "startTime": start_dt.isoformat(),
            "endTime": end_dt.isoformat(),
            "title": f"Life Insurance Review - {first_name or 'Lead'}",
            "appointmentStatus": "confirmed",
            "assignedUserId": crm_user_id or None,
            "selectedTimezone": local_tz_str,
        }

        try:
            resp = requests.post(GHL_BOOK_URL, json=payload, headers=headers, timeout=30)
            if resp.status_code in [200, 201]:
                logger.info(f"Appointment booked for {contact_id} at {start_dt}")
                return True
            else:
                logger.error(f"Booking failed ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Booking exception: {e}")
            return False

    return False