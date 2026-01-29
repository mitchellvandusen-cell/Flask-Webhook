# ghl_calendar.py - Lead Connector Calendar Slots & Booking (Flawless 2026)
# CRITICAL: Uses webhook's locationId for all API calls (location-specific paths)
import logging
import os
import requests
import time as time_module
import threading
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import re

logger = logging.getLogger(__name__)

# GHL Calendar API v2 Endpoints - REQUIRE /locations/{location_id}/ prefix
# These endpoints work with BOTH OAuth Access Tokens AND Private Integration Tokens (PIT)
# Source: https://marketplace.gohighlevel.com/docs/ghl/calendars/
# CRITICAL: All sub-account resources must include /locations/{location_id}/ in path
GHL_CALENDARS_LIST = "https://services.leadconnectorhq.com/locations/{location_id}/calendars"
GHL_FREE_SLOTS_URL = "https://services.leadconnectorhq.com/locations/{location_id}/calendars/{cal_id}/free-slots"
GHL_CREATE_APPOINTMENT_URL = "https://services.leadconnectorhq.com/locations/{location_id}/calendars/events/appointments"
GHL_CALENDAR_EVENTS_URL = "https://services.leadconnectorhq.com/locations/{location_id}/calendars/events"


def detect_token_type(access_token: str) -> dict:
    """
    Detects if the token is OAuth or Personal Integration Token (PIT).

    NOTE: Both token types use the SAME v2 API paths (/locations/{locationId}/...)
    This function is kept for logging/debugging purposes only.

    Personal Integration Tokens start with 'pit-'.
    OAuth tokens are obtained through OAuth 2.0 flow.

    Returns:
        {
            "is_oauth": True/False,
            "location_id": "xxx" (if OAuth),
            "company_id": "yyy",
            "version": "v2"
        }
    """
    logger.debug(f"🔍 Token detection: first 10 chars = {access_token[:10]}...")

    # FAST PATH: Check for private integration token prefix
    if access_token.startswith("pit-"):
        logger.info(f"🔑 PRIVATE INTEGRATION TOKEN DETECTED (pit-...)")
        logger.info(f"   ✅ Using v2 API endpoints with /locations/{{locationId}}/ path")
        return {
            "is_oauth": False,
            "location_id": None,
            "company_id": None,
            "version": "v2"
        }

    # OAUTH PATH: Verify by calling /v2/me endpoint
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-04-15"
    }

    try:
        logger.debug(f"   Calling /v2/me to verify OAuth token...")
        me_resp = requests.get(
            "https://services.leadconnectorhq.com/v2/me",
            headers=headers,
            timeout=10
        )

        logger.debug(f"   /v2/me response: status={me_resp.status_code}")

        if me_resp.status_code == 200:
            me_data = me_resp.json()
            logger.debug(f"   /v2/me body: {me_data}")
            logger.info(f"✅ OAUTH TOKEN DETECTED")
            logger.info(f"   Location ID: {me_data.get('locationId')}")
            logger.info(f"   Company ID: {me_data.get('companyId')}")
            logger.info(f"   User ID: {me_data.get('userId')}")
            logger.info(f"   ✅ Using v2 API endpoints with /locations/{{locationId}}/ path")
            return {
                "is_oauth": True,
                "location_id": me_data.get('locationId'),
                "company_id": me_data.get('companyId'),
                "version": "v2"
            }
        else:
            # Not an OAuth token, likely an old-style API key
            logger.info(f"⚙️ TOKEN TYPE UNKNOWN - /v2/me returned {me_resp.status_code}")
            logger.info(f"   ✅ Using v2 API endpoints with /locations/{{locationId}}/ path")
            logger.debug(f"   Response: {me_resp.text[:200]}")
            return {
                "is_oauth": False,
                "location_id": None,
                "company_id": None,
                "version": "v2"
            }
    except Exception as e:
        logger.warning(f"⚠️ Token detection failed, using v2 endpoints: {e}")
        return {
            "is_oauth": False,
            "location_id": None,
            "company_id": None,
            "version": "v2"
        }


def ghl_debug_check(access_token: str, location_id: str, calendar_id: str, contact_id: str):
    """
    🔍 COMPREHENSIVE GHL BOOKING DEBUG (Token-Type Aware)

    Checks:
    1. Token scope (private key vs OAuth)
    2. Lists all calendars (using correct endpoint for token type)
    3. Verifies calendar_id exists
    4. Verifies contact_id exists

    This function helps pinpoint exactly why booking is failing with 404 errors.
    """
    logger.info("=" * 80)
    logger.info("🔍 GHL DEBUG CHECK STARTED")
    logger.info(f"   Location ID: {location_id}")
    logger.info(f"   Calendar ID: {calendar_id}")
    logger.info(f"   Contact ID:  {contact_id}")
    logger.info("=" * 80)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-04-15"
    }

    # ═══════════════════════════════════════════════════════════════════════
    # 1️⃣ DETECT TOKEN TYPE FIRST
    # ═══════════════════════════════════════════════════════════════════════
    logger.info("\n1️⃣ DETECTING TOKEN TYPE...")
    logger.critical(f"[FORCED LOG] Token first 20 chars: {access_token[:20]}...")

    is_private_key = access_token.startswith("pit-")

    if is_private_key:
        logger.critical(f"[FORCED LOG] ✅ PRIVATE INTEGRATION TOKEN (pit-...)")
        logger.critical(f"[FORCED LOG] Will use v1 endpoints (company-scoped, no locationId in paths)")
    else:
        logger.critical(f"[FORCED LOG] Calling /oauth/token/me...")
        try:
            me_resp = requests.get(
                "https://services.leadconnectorhq.com/oauth/token/me",
                headers=headers,
                timeout=10
            )

            logger.critical(f"[FORCED LOG] Token check response status: {me_resp.status_code}")

            if me_resp.status_code == 200:
                me_data = me_resp.json()
                logger.critical(f"[FORCED LOG] Full token response: {me_data}")
                logger.info(f"✅ Token Info Retrieved:")
                logger.info(f"   Type: {me_data.get('type', 'UNKNOWN')}")
                logger.info(f"   Location ID from token: {me_data.get('locationId', 'N/A')}")
                logger.info(f"   Company ID: {me_data.get('companyId', 'N/A')}")
                logger.info(f"   User ID: {me_data.get('userId', 'N/A')}")

                token_location = me_data.get('locationId')
                if token_location and token_location != location_id:
                    logger.critical(f"⚠️ MISMATCH! Token is for location '{token_location}' but you're trying to use location '{location_id}'")
                    logger.critical(f"⚠️ THIS IS WHY THE CALENDAR ENDPOINT IS FAILING!")
                elif token_location == location_id:
                    logger.info(f"✅ Token location matches requested location: {location_id}")
                else:
                    logger.warning(f"⚠️ Token has no locationId - might be agency-scoped token")
            else:
                logger.critical(f"❌ Failed to get token info: {me_resp.status_code} - {me_resp.text[:500]}")
                is_private_key = True  # Assume private key if /oauth/token/me fails
        except Exception as e:
            logger.critical(f"❌ Token scope check EXCEPTION: {e}", exc_info=True)
            is_private_key = True

    # ═══════════════════════════════════════════════════════════════════════
    # 2️⃣ LIST ALL CALENDARS (v2 Endpoint with location path)
    # ═══════════════════════════════════════════════════════════════════════
    logger.info("\n2️⃣ LISTING ALL CALENDARS...")
    calendar_found = False
    try:
        # v2 endpoint requires /locations/{locationId}/ in path (not query param)
        cal_list_url = GHL_CALENDARS_LIST.format(location_id=location_id)
        logger.info(f"   Using v2 calendars endpoint: {cal_list_url}")

        cal_resp = requests.get(
            cal_list_url,
            headers=headers,
            timeout=15
        )

        logger.info(f"   Response status: {cal_resp.status_code}")

        if cal_resp.status_code == 200:
            cal_data = cal_resp.json()

            # v2 response structure (same for both OAuth and PIT)
            calendars = cal_data.get("calendars", []) if isinstance(cal_data, dict) else []

            logger.info(f"✅ Found {len(calendars)} calendars:")

            if not calendars:
                logger.warning(f"⚠️ NO CALENDARS FOUND!")

            for idx, cal in enumerate(calendars, 1):
                cal_id = cal.get('id', 'UNKNOWN')
                cal_name = cal.get('name', 'UNNAMED')
                is_active = cal.get('isActive', False)
                cal_location = cal.get('locationId', 'N/A')

                is_match = "🎯 THIS ONE" if cal_id == calendar_id else ""

                logger.info(f"   [{idx}] {cal_id} {is_match}")
                logger.info(f"       Name: {cal_name}")
                logger.info(f"       Active: {is_active}")
                logger.info(f"       Location: {cal_location}")

                if cal_id == calendar_id:
                    calendar_found = True
                    logger.info(f"       ✅ FOUND! This is your calendar.")

                    # For private keys, check if calendar's location matches
                    if is_private_key and cal_location != location_id:
                        logger.warning(f"       ⚠️ Calendar is in location '{cal_location}' but you specified '{location_id}'")

            if not calendar_found:
                logger.critical(f"❌ CALENDAR '{calendar_id}' NOT FOUND!")
                logger.critical(f"   Available calendar IDs: {[c.get('id') for c in calendars]}")
                logger.critical(f"   📋 Here are the calendars that DO exist:")
                for cal in calendars:
                    logger.critical(f"      - {cal.get('id')} | {cal.get('name')} | Location: {cal.get('locationId', 'N/A')}")
                logger.critical(f"   ⚠️ YOUR CALENDAR '{calendar_id}' IS NOT IN THIS LIST!")
            else:
                logger.info(f"✅ Calendar '{calendar_id}' found")

        elif cal_resp.status_code == 404:
            logger.critical(f"❌ 404 ERROR - Location or calendars not found")
            logger.critical(f"   This might mean:")
            logger.critical(f"      1. The locationId '{location_id}' is incorrect or doesn't exist")
            logger.critical(f"      2. Token lacks calendar read permissions")
            logger.critical(f"      3. No calendars exist in this location")
        elif cal_resp.status_code == 403:
            logger.critical(f"❌ FORBIDDEN! Token lacks permission to list calendars")
        else:
            logger.error(f"❌ Failed to list calendars: {cal_resp.status_code} - {cal_resp.text[:200]}")
    except Exception as e:
        logger.error(f"❌ Calendar listing failed: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════════════════
    # 3️⃣ CHECK IF CONTACT EXISTS IN LOCATION
    # ═══════════════════════════════════════════════════════════════════════
    logger.info("\n3️⃣ CHECKING IF CONTACT EXISTS IN LOCATION...")
    try:
        contact_url = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
        logger.info(f"   Fetching: {contact_url}")

        contact_resp = requests.get(
            contact_url,
            headers=headers,
            timeout=10
        )

        if contact_resp.status_code == 200:
            contact_data = contact_resp.json()
            contact = contact_data.get('contact', {})
            first_name = contact.get('firstName', 'N/A')
            last_name = contact.get('lastName', 'N/A')
            contact_location = contact.get('locationId', 'N/A')
            email = contact.get('email', 'N/A')
            phone = contact.get('phone', 'N/A')

            logger.info(f"✅ Contact Found:")
            logger.info(f"   Name: {first_name} {last_name}")
            logger.info(f"   Email: {email}")
            logger.info(f"   Phone: {phone}")
            logger.info(f"   Contact's Location ID: {contact_location}")

            if contact_location != location_id:
                logger.error(f"❌ LOCATION MISMATCH! Contact is in location '{contact_location}' but you're trying to book in location '{location_id}'!")
            else:
                logger.info(f"✅ Contact location matches: {location_id}")

        elif contact_resp.status_code == 404:
            logger.error(f"❌ CONTACT '{contact_id}' NOT FOUND!")
        elif contact_resp.status_code == 403:
            logger.error(f"❌ FORBIDDEN! Token lacks permission to access contact '{contact_id}'")
        else:
            logger.error(f"❌ Failed to get contact: {contact_resp.status_code} - {contact_resp.text[:200]}")
    except Exception as e:
        logger.error(f"❌ Contact check failed: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════════════════
    # 4️⃣ FINAL SUMMARY
    # ═══════════════════════════════════════════════════════════════════════
    logger.info("\n" + "=" * 80)
    logger.info("🔍 DEBUG CHECK COMPLETE - SUMMARY:")
    logger.info(f"   Calendar Found: {'✅ YES' if calendar_found else '❌ NO'}")
    logger.info("=" * 80 + "\n")

    return calendar_found

CACHE_TTL = 1800  # 30 minutes
cache = {}  # Simple in-memory cache with thread safety
cache_lock = threading.Lock()  # Thread-safe cache access

def get_cached_data(key: str):
    with cache_lock:
        if key in cache:
            cached = cache[key]
            age = (datetime.now(timezone.utc) - cached['time']).total_seconds()
            if age < CACHE_TTL:
                return cached['data']
            else:
                # Delete expired entry to prevent memory leak
                del cache[key]
    return None

def set_cache(key: str, data):
    with cache_lock:
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
            return "I've got tomorrow morning or afternoon, let me know what works!"
        if operation == "book":
            logger.info(f"DEMO MODE: Simulated booking for {contact_id}")
            return True

    # 🔍 DETECT TOKEN TYPE (OAuth v2 or Private API Key v1)
    token_info = detect_token_type(access_token)
    is_oauth = token_info["is_oauth"]
    token_version = token_info["version"]

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
            # v2 endpoint requires /locations/{locationId}/ in path
            url = GHL_FREE_SLOTS_URL.format(location_id=location_id, cal_id=cal_id)
            logger.info(f"📅 Using v2 free-slots endpoint: {url}")

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
                logger.debug(f"   Fetching slots with params: {params}")
                resp = requests.get(url, headers=headers, params=params, timeout=20)
                logger.debug(f"   Slots response status: {resp.status_code}")

                resp.raise_for_status()
                data = resp.json()
                logger.debug(f"   Slots response body (first 300 chars): {str(data)[:300]}")

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
                    logger.info(f"✅ Fetched {len(slots)} slots for {cal_id} using {token_version}")
            except requests.HTTPError as e:
                logger.error(f"❌ Calendar fetch HTTP error ({token_version}): {e}")
                logger.error(f"   Response text: {e.response.text[:200] if hasattr(e, 'response') else 'N/A'}")
                slots = []
            except Exception as e:
                logger.error(f"❌ Calendar fetch error ({token_version}): {e}")
                logger.debug(f"   Error details: {str(e)}", exc_info=True)
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
        # 🔍 RUN DEBUG CHECK BEFORE BOOKING
        logger.info("\n" + "🔍" * 40)
        logger.info("PRE-BOOKING DEBUG CHECK - Verifying calendar and contact exist...")
        logger.info("🔍" * 40)
        try:
            calendar_exists = ghl_debug_check(access_token, location_id, cal_id, contact_id)
            if not calendar_exists:
                logger.error(f"❌ DEBUG CHECK FAILED: Calendar '{cal_id}' not found in location '{location_id}'. ABORTING BOOKING.")
                return False
        except Exception as debug_err:
            logger.warning(f"⚠️ Debug check threw error (continuing anyway): {debug_err}")

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
            logger.error(f"🚨 BOOKING BLOCKED: Time too far ahead | requested={start_dt} | contact={contact_id}")
            return False

        # v2 endpoint requires /locations/{locationId}/ in path
        booking_url = GHL_CREATE_APPOINTMENT_URL.format(location_id=location_id)
        payload = {
            "calendarId": cal_id,  # ✅ Required in body
            "locationId": location_id,  # ✅ Required in body
            "contactId": contact_id,
            "startTime": start_dt.isoformat(),
            "endTime": end_dt.isoformat(),
            "title": f"Life Insurance Review {first_name or 'Lead'}",
            "appointmentStatus": "confirmed",
            "assignedUserId": crm_user_id or None,
            "selectedTimezone": local_tz_str,
            "meetingLocationType": "custom",
            "meetingLocationId": "custom_0",
        }
        logger.info(f"📅 Using v2 booking endpoint: {booking_url}")

        # ROBUST BOOKING with 3 retries and detailed error logging
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"📅 BOOKING ATTEMPT {attempt}/{max_attempts} ({token_version})")
                logger.info(f"   Contact: {contact_id}")
                logger.info(f"   Time: {start_dt}")
                logger.info(f"   URL: {booking_url}")
                logger.info(f"   Payload: {payload}")

                resp = requests.post(booking_url, json=payload, headers=headers, timeout=30)

                # DETAILED RESPONSE LOGGING
                logger.info(f"   📬 Response Status: {resp.status_code}")
                logger.debug(f"   📬 Response Body: {resp.text[:500]}")

                if resp.status_code in [200, 201]:
                    # VERIFY the booking was actually created
                    try:
                        result_data = resp.json()
                        event_id = result_data.get('id') or result_data.get('event', {}).get('id')
                        logger.debug(f"   Parsed event_id: {event_id}")

                        if event_id:
                            logger.info(f"✅ BOOKING CONFIRMED ({token_version}) | contact={contact_id} | time={start_dt} | event_id={event_id}")
                            return True
                        else:
                            logger.warning(f"⚠️ BOOKING CREATED BUT NO EVENT ID | contact={contact_id} | response={resp.text[:200]}")
                            return True  # Assume success if 200/201 even without event_id
                    except Exception as parse_err:
                        logger.warning(f"⚠️ BOOKING CREATED BUT PARSE FAILED | contact={contact_id} | error={parse_err}")
                        logger.debug(f"   Raw response: {resp.text[:500]}")
                        return True  # Assume success if 200/201 even if parse fails

                else:
                    error_details = {
                        "status_code": resp.status_code,
                        "response_text": resp.text[:500],
                        "contact_id": contact_id,
                        "calendar_id": cal_id,
                        "requested_time": start_dt.isoformat(),
                        "payload": payload
                    }

                    if resp.status_code == 400:
                        logger.error(f"🚨 BOOKING FAILED: BAD REQUEST (calendar_id invalid or time unavailable) | {error_details}")
                    elif resp.status_code == 401:
                        logger.error(f"🚨 BOOKING FAILED: UNAUTHORIZED (token expired or invalid) | {error_details}")
                    elif resp.status_code == 403:
                        logger.error(f"🚨 BOOKING FAILED: FORBIDDEN (insufficient permissions) | {error_details}")
                    elif resp.status_code == 404:
                        logger.error(f"🚨 BOOKING FAILED: NOT FOUND (calendar_id or contact_id doesn't exist) | {error_details}")
                        # Re-run debug check to see what's missing
                        logger.error("🔍 RE-RUNNING DEBUG CHECK TO IDENTIFY WHAT'S MISSING...")
                        try:
                            ghl_debug_check(access_token, location_id, cal_id, contact_id)
                        except Exception as recheck_err:
                            logger.error(f"Debug recheck failed: {recheck_err}")
                    elif resp.status_code == 409:
                        logger.error(f"🚨 BOOKING FAILED: CONFLICT (time slot already booked) | {error_details}")
                    elif resp.status_code == 429:
                        logger.error(f"🚨 BOOKING FAILED: RATE LIMIT (too many requests) | {error_details}")
                    elif resp.status_code >= 500:
                        logger.error(f"🚨 BOOKING FAILED: SERVER ERROR (Lead Connector API issue) | {error_details}")
                    else:
                        logger.error(f"🚨 BOOKING FAILED: UNKNOWN ERROR | {error_details}")

                    # Don't retry on client errors (400-499) except 429 (rate limit)
                    if 400 <= resp.status_code < 500 and resp.status_code != 429:
                        logger.error(f"❌ BOOKING ABORTED: Client error, not retrying | contact={contact_id}")
                        return False

            except requests.Timeout:
                logger.error(f"🚨 BOOKING TIMEOUT (attempt {attempt}/{max_attempts}) | contact={contact_id} | time={start_dt}")
            except requests.ConnectionError as conn_err:
                logger.error(f"🚨 BOOKING CONNECTION ERROR (attempt {attempt}/{max_attempts}) | contact={contact_id} | error={conn_err}")
            except Exception as e:
                logger.error(f"🚨 BOOKING EXCEPTION (attempt {attempt}/{max_attempts}) | contact={contact_id} | error={e}", exc_info=True)

            # Wait before retry (exponential backoff)
            if attempt < max_attempts:
                wait_time = 2 ** attempt  # 2s, 4s
                logger.info(f"⏳ Retrying in {wait_time}s...")
                time_module.sleep(wait_time)

        # All attempts failed
        logger.error(f"❌ BOOKING FAILED AFTER {max_attempts} ATTEMPTS | contact={contact_id} | time={start_dt}")
        return False

    return False