# blueprints/calendar.py — GHL Calendar API routes (fetch, slots, book, events CRUD)
#
# These are the Lead Connector / GoHighLevel calendar operations used by the
# dashboard Calendar tab.  NOT to be confused with blueprints/google_calendar.py
# which handles Google Calendar OAuth + Google Calendar events.
#
# Routes:
#   GET  /api/fetch-calendars              — List all GHL calendars for current location
#   GET  /api/calendar/slots               — Available appointment slots
#   POST /api/calendar/book                — Book an appointment for a contact
#   POST /api/calendar/create-event        — Create a new calendar event
#   GET  /api/calendar/events              — Fetch events for a date range
#   PUT  /api/calendar/events/<event_id>   — Update an event
#   DELETE /api/calendar/events/<event_id> — Delete an event

import logging
import requests
from datetime import datetime, timedelta, timezone

from flask import Blueprint, request
from flask import jsonify as flask_jsonify
from flask_login import login_required, current_user

from ghl_calendar import consolidated_calendar_op

logger = logging.getLogger(__name__)

calendar_bp = Blueprint('calendar', __name__)


# ── List calendars ───────────────────────────────────────────────────────────

@calendar_bp.route("/api/fetch-calendars", methods=["GET"])
@login_required
def fetch_calendars():
    """Fetch all calendars from Lead Connector for the current user's location."""
    from ghl_api import get_valid_token

    location_id = current_user.location_id
    if not location_id:
        return flask_jsonify({"error": "No location configured"}), 400

    raw_token = getattr(current_user, 'access_token', '')

    if raw_token == 'DEMO':
        return flask_jsonify({
            "calendars": [
                {"id": "demo_cal_1", "name": "Demo Calendar 1"},
                {"id": "demo_cal_2", "name": "Demo Calendar 2"},
            ]
        })

    access_token = get_valid_token(location_id)
    if not access_token:
        logger.warning(f"Calendar: No valid token for {location_id}")
        return flask_jsonify({"error": "CRM connection expired. Please reconnect in Bot Config."}), 401

    url = f"https://services.leadconnectorhq.com/calendars/?locationId={location_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code in (401, 403):
            logger.warning(f"Calendar: GHL returned {resp.status_code} for {location_id}")
            return flask_jsonify({"error": "CRM token expired. Please reconnect in Bot Config."}), 401

        resp.raise_for_status()
        data = resp.json()

        calendars = []
        for cal in data.get("calendars", []):
            calendars.append({
                "id": cal.get("id"),
                "name": cal.get("name", "Unnamed Calendar"),
            })

        logger.info(f"Calendar: Fetched {len(calendars)} calendars for {location_id}")
        return flask_jsonify({"calendars": calendars})

    except requests.exceptions.Timeout:
        logger.error(f"Calendar: GHL API timeout for {location_id}")
        return flask_jsonify({"error": "Calendar service timed out. Please try again."}), 504
    except requests.exceptions.RequestException as e:
        logger.error(f"Calendar: Failed to fetch for {location_id}: {e}")
        return flask_jsonify({"error": "Failed to fetch calendars from Lead Connector"}), 500


# ── Free slots ───────────────────────────────────────────────────────────────

@calendar_bp.route("/api/calendar/slots", methods=["GET"])
@login_required
def api_calendar_slots():
    """Fetch available appointment slots for the user's configured calendar."""
    from zoneinfo import ZoneInfo

    location_id = current_user.location_id
    access_token = current_user.access_token
    cal_id = request.args.get("calendar_id") or current_user.calendar_id

    if not location_id or not access_token:
        return flask_jsonify({"error": "Not connected to Lead Connector"}), 400
    if not cal_id:
        return flask_jsonify({"error": "No calendar configured. Set one in Bot Config."}), 400

    tz_str = getattr(current_user, "timezone", None) or "America/Chicago"

    # Demo mode
    if access_token == "DEMO":
        from datetime import time as _time
        local_tz = ZoneInfo(tz_str)
        now = datetime.now(local_tz)
        demo_slots = {}
        for d in range(3):
            day = now + timedelta(days=d)
            date_key = day.strftime("%Y-%m-%d")
            demo_slots[date_key] = [
                datetime.combine(day.date(), _time(9, 0), tzinfo=local_tz).isoformat(),
                datetime.combine(day.date(), _time(10, 30), tzinfo=local_tz).isoformat(),
                datetime.combine(day.date(), _time(13, 0), tzinfo=local_tz).isoformat(),
                datetime.combine(day.date(), _time(15, 0), tzinfo=local_tz).isoformat(),
            ]
        return flask_jsonify({"slots": demo_slots, "timezone": tz_str})

    local_tz = ZoneInfo(tz_str)
    now_utc = datetime.now(timezone.utc)
    days_ahead = min(int(request.args.get("days", 30)), 90)
    start_ts = int(now_utc.timestamp() * 1000)
    end_ts = int((now_utc + timedelta(days=days_ahead)).timestamp() * 1000)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }
    params = {"startDate": start_ts, "endDate": end_ts, "timezone": tz_str}
    crm_user_id = getattr(current_user, "crm_user_id", None)
    if crm_user_id:
        params["userId"] = crm_user_id

    GHL_BASE = "https://services.leadconnectorhq.com"
    endpoints = [
        f"{GHL_BASE}/v2/locations/{location_id}/calendars/{cal_id}/free-slots",
        f"{GHL_BASE}/locations/{location_id}/calendars/{cal_id}/free-slots",
        f"{GHL_BASE}/calendars/{cal_id}/free-slots",
    ]

    raw_slots = []
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)
            if resp.status_code in [200, 201]:
                data = resp.json()
                if isinstance(data, dict):
                    for entry in data.values():
                        if isinstance(entry, list):
                            raw_slots.extend(entry)
                        elif isinstance(entry, dict) and "slots" in entry:
                            raw_slots.extend(entry["slots"])
                elif isinstance(data, list):
                    raw_slots = data
                break
            if resp.status_code == 422 and "userId" in params:
                retry_params = {k: v for k, v in params.items() if k != "userId"}
                resp = requests.get(url, headers=headers, params=retry_params, timeout=20)
                if resp.status_code in [200, 201]:
                    data = resp.json()
                    if isinstance(data, dict):
                        for entry in data.values():
                            if isinstance(entry, list):
                                raw_slots.extend(entry)
                            elif isinstance(entry, dict) and "slots" in entry:
                                raw_slots.extend(entry["slots"])
                    break
        except Exception:
            continue

    # Parse and group by date
    grouped = {}
    for slot in raw_slots:
        try:
            start_str = slot.get("startTime") or slot.get("start") or (slot if isinstance(slot, str) else None)
            if not start_str:
                continue
            if start_str.endswith("Z"):
                start_str = start_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(start_str).astimezone(local_tz)
            if 9 <= dt.hour < 19:
                date_key = dt.strftime("%Y-%m-%d")
                if date_key not in grouped:
                    grouped[date_key] = []
                grouped[date_key].append(dt.isoformat())
        except Exception:
            continue

    return flask_jsonify({"slots": grouped, "timezone": tz_str})


# ── Book appointment ─────────────────────────────────────────────────────────

@calendar_bp.route("/api/calendar/book", methods=["POST"])
@login_required
def api_calendar_book():
    """Book an appointment for a contact."""
    from zoneinfo import ZoneInfo

    data = request.get_json(silent=True) or {}
    contact_id = data.get("contact_id")
    slot_time = data.get("slot_time")
    cal_id = data.get("calendar_id") or current_user.calendar_id

    if not contact_id or not slot_time:
        return flask_jsonify({"error": "contact_id and slot_time are required"}), 400
    if not cal_id:
        return flask_jsonify({"error": "No calendar configured"}), 400

    location_id = current_user.location_id
    access_token = current_user.access_token
    if not location_id or not access_token:
        return flask_jsonify({"error": "Not connected to Lead Connector"}), 400

    tz_str = getattr(current_user, "timezone", None) or "America/Chicago"
    subscriber_data = {
        "location_id": location_id,
        "calendar_id": cal_id,
        "crm_user_id": getattr(current_user, "crm_user_id", None),
        "timezone": tz_str,
        "access_token": access_token,
    }

    try:
        local_tz = ZoneInfo(tz_str)
        dt = datetime.fromisoformat(slot_time).astimezone(local_tz)
        time_str = dt.strftime("%-I:%M %p").lower()
        day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        today = datetime.now(local_tz).date()
        diff = (dt.date() - today).days
        if diff == 0:
            date_part = "today"
        elif diff == 1:
            date_part = "tomorrow"
        else:
            date_part = day_names[dt.weekday()]
        selected_time = f"{time_str} {date_part}"
    except Exception as e:
        logger.error(f"Failed to parse slot_time '{slot_time}': {e}")
        return flask_jsonify({"error": "Invalid slot_time format"}), 400

    first_name = data.get("first_name", "Lead")

    result = consolidated_calendar_op(
        operation="book",
        subscriber_data=subscriber_data,
        contact_id=contact_id,
        first_name=first_name,
        selected_time=selected_time,
    )

    if result:
        return flask_jsonify({"success": True, "message": f"Booked {selected_time}"})
    else:
        return flask_jsonify({"error": "Booking failed. The slot may no longer be available."}), 422


# ── Create event ─────────────────────────────────────────────────────────────

@calendar_bp.route("/api/calendar/create-event", methods=["POST"])
@login_required
def api_calendar_create_event():
    """Create a new calendar event/appointment in GHL CRM."""
    from ghl_api import get_valid_token
    from zoneinfo import ZoneInfo

    location_id = current_user.location_id
    if not location_id:
        return flask_jsonify({"error": "No location configured"}), 400

    raw_token = getattr(current_user, "access_token", "")
    if raw_token == "DEMO":
        return flask_jsonify({"success": True, "message": "Demo event created", "event_id": "demo_event"})

    access_token = get_valid_token(location_id)
    if not access_token:
        return flask_jsonify({"error": "CRM connection expired. Please reconnect in Bot Config."}), 401

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    date_str = data.get("date")
    start_time = data.get("start_time")
    end_time = data.get("end_time")
    cal_id = data.get("calendar_id") or getattr(current_user, "calendar_id", "")
    contact_id = data.get("contact_id")
    notes = (data.get("notes") or "").strip()

    if not title:
        return flask_jsonify({"error": "Title is required"}), 400
    if not date_str or not start_time or not end_time:
        return flask_jsonify({"error": "Date and times are required"}), 400
    if not cal_id:
        return flask_jsonify({"error": "No calendar selected"}), 400

    tz_str = getattr(current_user, "timezone", None) or "America/Chicago"
    local_tz = ZoneInfo(tz_str)

    try:
        start_dt = datetime.fromisoformat(f"{date_str}T{start_time}:00").replace(tzinfo=local_tz)
        end_dt = datetime.fromisoformat(f"{date_str}T{end_time}:00").replace(tzinfo=local_tz)
    except Exception as e:
        logger.error(f"Failed to parse event times: {e}")
        return flask_jsonify({"error": "Invalid date or time format"}), 400

    GHL_BASE = "https://services.leadconnectorhq.com"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }

    payload = {
        "calendarId": cal_id,
        "locationId": location_id,
        "title": title,
        "startTime": start_dt.isoformat(),
        "endTime": end_dt.isoformat(),
        "selectedTimezone": tz_str,
        "appointmentStatus": "confirmed",
    }
    if contact_id:
        payload["contactId"] = contact_id
    if notes:
        payload["notes"] = notes
    crm_user_id = getattr(current_user, "crm_user_id", None)
    if crm_user_id:
        payload["assignedUserId"] = crm_user_id

    url = f"{GHL_BASE}/calendars/events/appointments"

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)

        if resp.status_code in (401, 403):
            return flask_jsonify({"error": "CRM token expired. Please reconnect."}), 401

        if resp.status_code in (200, 201):
            result = resp.json()
            event_id = result.get("id") or result.get("event", {}).get("id", "")
            logger.info(f"Calendar event created: {event_id} for {location_id} ({title})")
            return flask_jsonify({"success": True, "message": f"{title} created", "event_id": event_id})

        # If 422 with assignedUserId issue, retry without it
        if resp.status_code == 422 and crm_user_id and "assignedUserId" in payload:
            del payload["assignedUserId"]
            retry_resp = requests.post(url, headers=headers, json=payload, timeout=20)
            if retry_resp.status_code in (200, 201):
                result = retry_resp.json()
                event_id = result.get("id") or result.get("event", {}).get("id", "")
                logger.info(f"Calendar event created (retry): {event_id} for {location_id} ({title})")
                return flask_jsonify({"success": True, "message": f"{title} created", "event_id": event_id})

        error_text = resp.text[:300]
        logger.error(f"GHL create event failed ({resp.status_code}): {error_text}")
        return flask_jsonify({"error": "Failed to create event in CRM"}), 422

    except requests.exceptions.Timeout:
        return flask_jsonify({"error": "CRM service timed out"}), 504
    except requests.exceptions.RequestException as e:
        logger.error(f"Calendar create event failed for {location_id}: {e}")
        return flask_jsonify({"error": "Failed to create event"}), 500


# ── List events ──────────────────────────────────────────────────────────────

@calendar_bp.route("/api/calendar/events", methods=["GET"])
@login_required
def api_calendar_events():
    """Fetch calendar events from GHL for a date range."""
    from ghl_api import get_valid_token
    from zoneinfo import ZoneInfo

    location_id = current_user.location_id
    if not location_id:
        return flask_jsonify({"error": "No location configured"}), 400

    cal_id = request.args.get("calendar_id") or getattr(current_user, "calendar_id", "")
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    if not cal_id:
        return flask_jsonify({"error": "No calendar selected"}), 400

    raw_token = getattr(current_user, "access_token", "")
    if raw_token == "DEMO":
        return flask_jsonify({"events": []})

    access_token = get_valid_token(location_id)
    if not access_token:
        return flask_jsonify({"error": "CRM connection expired. Please reconnect in Bot Config."}), 401

    tz_str = getattr(current_user, "timezone", None) or "America/Chicago"
    local_tz = ZoneInfo(tz_str)
    now = datetime.now(local_tz)

    if start_date:
        start_dt = datetime.fromisoformat(start_date + "T00:00:00").replace(tzinfo=local_tz)
    else:
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if end_date:
        end_dt = datetime.fromisoformat(end_date + "T23:59:59").replace(tzinfo=local_tz)
    else:
        next_month = start_dt.replace(day=28) + timedelta(days=4)
        end_dt = next_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)

    GHL_BASE = "https://services.leadconnectorhq.com"
    url = f"{GHL_BASE}/calendars/events"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }
    params = {
        "locationId": location_id,
        "calendarId": cal_id,
        "startTime": start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endTime": end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code in (401, 403):
            return flask_jsonify({"error": "CRM token expired. Please reconnect in Bot Config."}), 401
        resp.raise_for_status()
        data = resp.json()

        events = []
        raw_events = data.get("events", [])
        if not raw_events and isinstance(data, list):
            raw_events = data

        for ev in raw_events:
            start_time = ev.get("startTime") or ev.get("start")
            end_time_val = ev.get("endTime") or ev.get("end")
            events.append({
                "id": ev.get("id"),
                "title": ev.get("title") or "Appointment",
                "startTime": start_time,
                "endTime": end_time_val,
                "contactId": ev.get("contactId", ""),
                "status": ev.get("appointmentStatus") or ev.get("status", ""),
                "notes": ev.get("notes", ""),
                "calendarId": ev.get("calendarId", cal_id),
                "assignedUserId": ev.get("assignedUserId", ""),
            })

        logger.info(f"Calendar events: Fetched {len(events)} events for {location_id} ({start_date} to {end_date})")
        return flask_jsonify({"events": events, "timezone": tz_str})

    except requests.exceptions.Timeout:
        return flask_jsonify({"error": "Calendar service timed out"}), 504
    except requests.exceptions.RequestException as e:
        logger.error(f"Calendar events fetch failed for {location_id}: {e}")
        return flask_jsonify({"error": "Failed to fetch calendar events"}), 500


# ── Update event ─────────────────────────────────────────────────────────────

@calendar_bp.route("/api/calendar/events/<event_id>", methods=["PUT"])
@login_required
def api_calendar_event_update(event_id):
    """Update an existing calendar event/appointment in GHL."""
    from ghl_api import get_valid_token

    location_id = current_user.location_id
    if not location_id:
        return flask_jsonify({"error": "No location configured"}), 400

    access_token = get_valid_token(location_id)
    if not access_token:
        return flask_jsonify({"error": "CRM connection expired"}), 401

    body = request.get_json(silent=True) or {}

    payload = {}
    for field in ("startTime", "endTime", "title", "notes", "appointmentStatus"):
        if field in body:
            payload[field] = body[field]
    if "calendarId" in body:
        payload["calendarId"] = body["calendarId"]
    if "selectedTimezone" not in payload:
        payload["selectedTimezone"] = getattr(current_user, "timezone", None) or "America/Chicago"

    if not payload:
        return flask_jsonify({"error": "No fields to update"}), 400

    GHL_BASE = "https://services.leadconnectorhq.com"
    url = f"{GHL_BASE}/calendars/events/appointments/{event_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=20)
        if resp.status_code in (401, 403):
            return flask_jsonify({"error": "CRM token expired"}), 401
        if resp.status_code == 404:
            return flask_jsonify({"error": "Event not found"}), 404
        resp.raise_for_status()
        logger.info(f"Calendar event updated: {event_id} for {location_id}")
        return flask_jsonify({"success": True})
    except requests.exceptions.RequestException as e:
        logger.error(f"Calendar event update failed for {event_id}: {e}")
        return flask_jsonify({"error": "Failed to update event"}), 500


# ── Delete event ─────────────────────────────────────────────────────────────

@calendar_bp.route("/api/calendar/events/<event_id>", methods=["DELETE"])
@login_required
def api_calendar_event_delete(event_id):
    """Delete a calendar event/appointment from GHL."""
    from ghl_api import get_valid_token

    location_id = current_user.location_id
    if not location_id:
        return flask_jsonify({"error": "No location configured"}), 400

    access_token = get_valid_token(location_id)
    if not access_token:
        return flask_jsonify({"error": "CRM connection expired"}), 401

    GHL_BASE = "https://services.leadconnectorhq.com"
    url = f"{GHL_BASE}/calendars/events/{event_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
    }

    try:
        resp = requests.delete(url, headers=headers, timeout=20)
        if resp.status_code in (401, 403):
            return flask_jsonify({"error": "CRM token expired"}), 401
        if resp.status_code == 404:
            return flask_jsonify({"error": "Event not found"}), 404
        resp.raise_for_status()
        logger.info(f"Calendar event deleted: {event_id} for {location_id}")
        return flask_jsonify({"success": True})
    except requests.exceptions.RequestException as e:
        logger.error(f"Calendar event delete failed for {event_id}: {e}")
        return flask_jsonify({"error": "Failed to delete event"}), 500
