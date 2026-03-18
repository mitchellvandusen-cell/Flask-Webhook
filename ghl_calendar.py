# ghl_calendar.py - Lead Connector Calendar Slots & Booking (Flawless 2026)
# OAuth: locationId in URL path (/v2/locations/{id}/...)
# PIT: locationId as query param or in request body
import logging
import os
import requests
import time as time_module
import threading
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import re

logger = logging.getLogger(__name__)

# GHL Calendar API Endpoints
# OAuth: requires /v2/locations/{location_id}/ prefix in URL path
# PIT: uses base URL (no location prefix), locationId as query param where needed
# Source: https://marketplace.gohighlevel.com/docs/ghl/calendars/

# GHL Calendar Free Slots Endpoints (try in order until one works)
# Pattern 1: v2 with locationId in path
GHL_V2_FREE_SLOTS_URL = "https://services.leadconnectorhq.com/v2/locations/{location_id}/calendars/{cal_id}/free-slots"
# Pattern 2: locationId in path (no v2 prefix)
GHL_LOC_FREE_SLOTS_URL = "https://services.leadconnectorhq.com/locations/{location_id}/calendars/{cal_id}/free-slots"
# Pattern 3: no locationId in path (PIT style)
GHL_V1_FREE_SLOTS_URL = "https://services.leadconnectorhq.com/calendars/{cal_id}/free-slots"

# Calendar list endpoints
GHL_V2_CALENDARS_LIST = "https://services.leadconnectorhq.com/v2/locations/{location_id}/calendars"
GHL_V1_CALENDARS_LIST = "https://services.leadconnectorhq.com/calendars/"

# Booking endpoint (same for all token types)
GHL_BOOK_URL = "https://services.leadconnectorhq.com/calendars/events/appointments"


def detect_token_type(access_token: str) -> dict:
    """
    Detects if the token is OAuth or Private Integration Token (PIT).

    OAuth tokens use v2 endpoints: /v2/locations/{locationId}/...
    PIT tokens use v1 endpoints: /calendars/... with locationId as query param

    Detection is by prefix only (no HTTP call) to avoid latency.
    """
    if access_token.startswith("pit-"):
        logger.info(f"🔑 PIT token detected — using v1 endpoints")
        return {"is_oauth": False, "version": "v1"}

    logger.info(f"🔑 OAuth token detected — using v2 endpoints")
    return {"is_oauth": True, "version": "v2"}


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
    logger.debug(f"Token type detection: starts_with_pit={access_token.startswith('pit-')}")

    is_private_key = access_token.startswith("pit-")

    if is_private_key:
        logger.info(f"✅ PRIVATE INTEGRATION TOKEN (pit-...)")
        logger.info(f"   Will use v1 endpoints (company-scoped, no locationId in paths)")
    else:
        logger.info(f"Calling /oauth/token/me...")
        try:
            me_resp = requests.get(
                "https://services.leadconnectorhq.com/oauth/token/me",
                headers=headers,
                timeout=10
            )

            logger.info(f"   Token check response status: {me_resp.status_code}")

            if me_resp.status_code == 200:
                me_data = me_resp.json()
                logger.debug(f"   Token type: {me_data.get('type', 'UNKNOWN')}, "
                             f"locationId: {me_data.get('locationId', 'N/A')}")
                logger.info(f"✅ Token Info Retrieved:")
                logger.info(f"   Type: {me_data.get('type', 'UNKNOWN')}")
                logger.info(f"   Location ID from token: {me_data.get('locationId', 'N/A')}")
                logger.info(f"   Company ID: {me_data.get('companyId', 'N/A')}")
                logger.info(f"   User ID: {me_data.get('userId', 'N/A')}")

                token_location = me_data.get('locationId')
                if token_location and token_location != location_id:
                    logger.error(f"⚠️ MISMATCH! Token is for location '{token_location}' but you're trying to use location '{location_id}'")
                    logger.error(f"⚠️ THIS IS WHY THE CALENDAR ENDPOINT IS FAILING!")
                elif token_location == location_id:
                    logger.info(f"✅ Token location matches requested location: {location_id}")
                else:
                    logger.warning(f"⚠️ Token has no locationId - might be agency-scoped token")
            else:
                logger.error(f"❌ Failed to get token info: {me_resp.status_code} - {me_resp.text[:500]}")
                is_private_key = True  # Assume private key if /oauth/token/me fails
        except Exception as e:
            logger.error(f"❌ Token scope check EXCEPTION: {e}", exc_info=True)
            is_private_key = True

    # ═══════════════════════════════════════════════════════════════════════
    # 2️⃣ LIST ALL CALENDARS (v2 Endpoint with location path)
    # ═══════════════════════════════════════════════════════════════════════
    logger.info("\n2️⃣ LISTING ALL CALENDARS...")
    calendar_found = False
    try:
        # Select endpoint based on token type
        if is_private_key:
            cal_list_url = GHL_V1_CALENDARS_LIST
            cal_params = {"locationId": location_id}
            logger.info(f"   Using PIT calendars endpoint: {cal_list_url} with locationId param")
        else:
            cal_list_url = GHL_V2_CALENDARS_LIST.format(location_id=location_id)
            cal_params = {}
            logger.info(f"   Using OAuth v2 calendars endpoint: {cal_list_url}")

        cal_resp = requests.get(
            cal_list_url,
            headers=headers,
            params=cal_params,
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
    selected_time: str = None,
    contact_phone: str = None,
    contact_state: str = None,
    contact_city: str = None,
    contact_zip: str = None,
    contact_address: str = None,
) -> any:
    """
    Unified calendar operation: fetch slots or book appointment.
    Returns formatted string (slots) or bool (booking success).
    Demo-safe: returns placeholder on demo mode.

    MULTI-TENANT: All credentials come from subscriber_data (database).
    No hardcoded tokens or env vars.

    TIMEZONE-AWARE: When contact_state is provided, the customer's timezone
    is resolved from their state. Offered slots are displayed in the
    customer's local time. Booking requests are interpreted in the customer's
    timezone and converted to the agent's calendar timezone for GHL.

    ENDPOINT FALLBACK for fetch_slots:
    - Try v2 first: GET /v2/locations/{locationId}/calendars/{calendarId}/free-slots
    - If v2 fails, try v1: GET /calendars/{calendarId}/free-slots
    This handles both OAuth tokens and PIT tokens automatically.
    """
    location_id = subscriber_data.get("location_id")
    cal_id = subscriber_data.get("calendar_id")
    crm_user_id = subscriber_data.get("crm_user_id")
    local_tz_str = subscriber_data.get("timezone", "America/Chicago")
    access_token = subscriber_data.get("access_token")

    # Resolve customer timezone with multi-level fallback chain:
    #   1. State field (most reliable — from contact address)
    #   2. City field → state lookup (major US cities)
    #   3. Zip code prefix → timezone (US zip code ranges)
    #   4. Address field → parse state from full address string
    #   5. Phone area code (least reliable — people keep numbers when they move)
    #   6. Agent timezone (absolute last resort)
    customer_tz_str = None
    resolved_state = None
    tz_source = None

    _FULL_STATE_NAMES = {
        "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
        "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
        "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
        "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
        "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
        "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
        "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
        "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
        "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
        "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
        "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
        "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
        "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
        "PUERTO RICO": "PR", "GUAM": "GU", "VIRGIN ISLANDS": "VI",
    }

    def _state_str_to_tz(state_str):
        """Convert state string (abbrev or full name) to (tz_str, state_abbr) or (None, None)."""
        if not state_str:
            return None, None
        from voice.predictive_engine import _STATE_TO_TZ
        s = state_str.strip().upper()
        abbr = _FULL_STATE_NAMES.get(s, s)
        if len(abbr) == 2:
            tz = _STATE_TO_TZ.get(abbr)
            if tz:
                return tz, abbr
        return None, None

    # --- Fallback 1: State field ---
    if contact_state:
        try:
            customer_tz_str, resolved_state = _state_str_to_tz(contact_state)
            if customer_tz_str:
                tz_source = f"state field ({resolved_state})"
        except Exception:
            pass

    # --- Fallback 2: City → state (top ~120 US cities) ---
    if not customer_tz_str and contact_city:
        try:
            _CITY_TO_STATE = {
                "NEW YORK": "NY", "LOS ANGELES": "CA", "CHICAGO": "IL", "HOUSTON": "TX",
                "PHOENIX": "AZ", "PHILADELPHIA": "PA", "SAN ANTONIO": "TX", "SAN DIEGO": "CA",
                "DALLAS": "TX", "SAN JOSE": "CA", "AUSTIN": "TX", "JACKSONVILLE": "FL",
                "FORT WORTH": "TX", "COLUMBUS": "OH", "CHARLOTTE": "NC", "SAN FRANCISCO": "CA",
                "INDIANAPOLIS": "IN", "SEATTLE": "WA", "DENVER": "CO", "WASHINGTON": "DC",
                "NASHVILLE": "TN", "OKLAHOMA CITY": "OK", "EL PASO": "TX", "BOSTON": "MA",
                "PORTLAND": "OR", "LAS VEGAS": "NV", "MEMPHIS": "TN", "LOUISVILLE": "KY",
                "BALTIMORE": "MD", "MILWAUKEE": "WI", "ALBUQUERQUE": "NM", "TUCSON": "AZ",
                "FRESNO": "CA", "MESA": "AZ", "SACRAMENTO": "CA", "ATLANTA": "GA",
                "KANSAS CITY": "MO", "COLORADO SPRINGS": "CO", "OMAHA": "NE", "RALEIGH": "NC",
                "MIAMI": "FL", "LONG BEACH": "CA", "VIRGINIA BEACH": "VA", "OAKLAND": "CA",
                "MINNEAPOLIS": "MN", "TULSA": "OK", "TAMPA": "FL", "ARLINGTON": "TX",
                "NEW ORLEANS": "LA", "CLEVELAND": "OH", "BAKERSFIELD": "CA",
                "AURORA": "CO", "ANAHEIM": "CA", "HONOLULU": "HI", "SANTA ANA": "CA",
                "RIVERSIDE": "CA", "CORPUS CHRISTI": "TX", "LEXINGTON": "KY",
                "PITTSBURGH": "PA", "ANCHORAGE": "AK", "STOCKTON": "CA", "CINCINNATI": "OH",
                "ST PAUL": "MN", "SAINT PAUL": "MN", "TOLEDO": "OH", "GREENSBORO": "NC",
                "NEWARK": "NJ", "PLANO": "TX", "HENDERSON": "NV", "LINCOLN": "NE",
                "BUFFALO": "NY", "JERSEY CITY": "NJ", "CHULA VISTA": "CA",
                "FORT WAYNE": "IN", "ORLANDO": "FL", "ST PETERSBURG": "FL",
                "SAINT PETERSBURG": "FL", "CHANDLER": "AZ", "LAREDO": "TX",
                "NORFOLK": "VA", "DURHAM": "NC", "MADISON": "WI", "LUBBOCK": "TX",
                "IRVINE": "CA", "WINSTON SALEM": "NC", "GLENDALE": "AZ", "GARLAND": "TX",
                "HIALEAH": "FL", "RENO": "NV", "CHESAPEAKE": "VA", "IRVING": "TX",
                "SCOTTSDALE": "AZ", "BATON ROUGE": "LA", "RICHMOND": "VA", "SPOKANE": "WA",
                "FREMONT": "CA", "BOISE": "ID", "SALT LAKE CITY": "UT", "DES MOINES": "IA",
                "BIRMINGHAM": "AL", "ROCHESTER": "NY", "MODESTO": "CA", "LITTLE ROCK": "AR",
                "TACOMA": "WA", "OXNARD": "CA", "KNOXVILLE": "TN", "AKRON": "OH",
                "SHREVEPORT": "LA", "MOBILE": "AL", "MONTGOMERY": "AL", "HUNTSVILLE": "AL",
                "GRAND RAPIDS": "MI", "AUGUSTA": "GA", "SAVANNAH": "GA",
                "CHARLESTON": "SC", "COLUMBIA": "SC", "JACKSON": "MS",
                "TALLAHASSEE": "FL", "PENSACOLA": "FL", "NAPLES": "FL",
                "FORT LAUDERDALE": "FL", "WEST PALM BEACH": "FL", "SARASOTA": "FL",
            }
            city_upper = contact_city.strip().upper()
            state_from_city = _CITY_TO_STATE.get(city_upper)
            if state_from_city:
                customer_tz_str, resolved_state = _state_str_to_tz(state_from_city)
                if customer_tz_str:
                    tz_source = f"city ({contact_city} -> {resolved_state})"
        except Exception:
            pass

    # --- Fallback 3: Zip code prefix → timezone ---
    if not customer_tz_str and contact_zip:
        try:
            zip_digits = ''.join(c for c in contact_zip if c.isdigit())
            if len(zip_digits) >= 3:
                prefix = int(zip_digits[:3])
                # US zip prefix → timezone (USPS zip ranges, covers ~95% of contacts)
                if prefix < 5:
                    customer_tz_str = "America/Puerto_Rico"  # 000-004: PR
                elif prefix < 10:
                    customer_tz_str = "America/New_York"     # 005-009: NY/MA
                elif prefix < 27:
                    customer_tz_str = "America/New_York"     # 010-269: Northeast/Mid-Atlantic
                elif prefix < 35:
                    customer_tz_str = "America/New_York"     # 270-349: NC/SC/GA/FL (Eastern)
                elif prefix < 37:
                    customer_tz_str = "America/Chicago"      # 350-369: AL
                elif prefix < 39:
                    customer_tz_str = "America/Chicago"      # 370-389: TN/MS
                elif prefix < 40:
                    customer_tz_str = "America/New_York"     # 390-399: GA
                elif prefix < 48:
                    customer_tz_str = "America/New_York"     # 400-479: KY/OH/IN
                elif prefix < 50:
                    customer_tz_str = "America/Detroit"      # 480-499: MI
                elif prefix < 60:
                    customer_tz_str = "America/Chicago"      # 500-599: IA/WI/MN/SD/ND/MT
                elif prefix < 70:
                    customer_tz_str = "America/Chicago"      # 600-699: IL/MO/KS/NE
                elif prefix < 80:
                    customer_tz_str = "America/Chicago"      # 700-799: LA/AR/OK/TX
                elif prefix < 85:
                    customer_tz_str = "America/Denver"       # 800-849: CO/WY/ID/UT
                elif prefix < 86:
                    customer_tz_str = "America/Phoenix"      # 850-859: AZ
                elif prefix < 90:
                    customer_tz_str = "America/Denver"       # 860-899: AZ/NM/NV
                elif prefix < 97:
                    customer_tz_str = "America/Los_Angeles"  # 900-969: CA/HI
                else:
                    customer_tz_str = "America/Los_Angeles"  # 970-999: OR/WA/AK
                if customer_tz_str:
                    tz_source = f"zip code ({contact_zip})"
        except Exception:
            pass

    # --- Fallback 4: Parse state from full address string ---
    if not customer_tz_str and contact_address:
        try:
            addr_upper = contact_address.strip().upper()
            # Pattern: "FL 33602" or "Florida 33602" (state before zip)
            state_match = re.search(r'\b([A-Z]{2})\s+\d{5}', addr_upper)
            if state_match:
                customer_tz_str, resolved_state = _state_str_to_tz(state_match.group(1))
                if customer_tz_str:
                    tz_source = f"address ({resolved_state})"
            # Pattern: ", Florida" or ", FL" at end
            if not customer_tz_str:
                state_match = re.search(r',\s*([A-Za-z ]+?)(?:\s+\d{5}|\s*$)', contact_address)
                if state_match:
                    customer_tz_str, resolved_state = _state_str_to_tz(state_match.group(1).strip())
                    if customer_tz_str:
                        tz_source = f"address ({resolved_state})"
        except Exception:
            pass

    # --- Fallback 5: Phone area code ---
    if not customer_tz_str and contact_phone:
        try:
            from voice.predictive_engine import area_code_to_timezone, area_code_to_state
            customer_tz_str = area_code_to_timezone(contact_phone)
            if customer_tz_str:
                resolved_state = area_code_to_state(contact_phone) or "??"
                tz_source = f"phone area code ({resolved_state})"
        except Exception:
            pass

    # --- Fallback 6: Agent timezone ---
    if not customer_tz_str:
        customer_tz_str = local_tz_str
        tz_source = "agent timezone (last resort)"

    logger.info(f"📅 CUSTOMER TIMEZONE: {customer_tz_str} via {tz_source}")

    if not cal_id:
        logger.error(f"Missing calendar_id for calendar op (loc={location_id})")
        return "CALENDAR_UNAVAILABLE" if operation == "fetch_slots" else False

    if not access_token:
        logger.error(f"Missing access_token for calendar op (loc={location_id})")
        return "CALENDAR_UNAVAILABLE" if operation == "fetch_slots" else False

    # Demo mode short-circuit
    if access_token == 'DEMO':
        if operation == "fetch_slots":
            return "I have tomorrow morning or afternoon open. What works better for you?"
        if operation == "book":
            logger.info(f"DEMO MODE: Simulated booking for {contact_id}")
            return True

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }

    local_tz = ZoneInfo(local_tz_str)           # Agent's timezone (for GHL API)
    customer_tz = ZoneInfo(customer_tz_str)      # Customer's timezone (for display & interpretation)
    tz_differ = local_tz_str != customer_tz_str
    if tz_differ:
        logger.info(f"📅 TIMEZONE AWARE: Customer={customer_tz_str} | Agent={local_tz_str}")

    # === FETCH SLOTS (with endpoint fallback) ===
    if operation in ["fetch_slots", "book"]:
        slots_key = f"ghl_slots_{cal_id}_{crm_user_id or 'default'}"
        slots = get_cached_data(slots_key)

        if not slots:
            now_utc = datetime.now(timezone.utc)
            start_ts = int(now_utc.timestamp() * 1000)
            end_ts = int((now_utc + timedelta(days=14)).timestamp() * 1000)

            params = {
                "startDate": start_ts,
                "endDate": end_ts,
                "timezone": local_tz_str,
            }
            if crm_user_id:
                params["userId"] = crm_user_id

            # Three endpoint patterns to try (in order)
            endpoints = [
                ("v2", GHL_V2_FREE_SLOTS_URL.format(location_id=location_id, cal_id=cal_id)),
                ("loc", GHL_LOC_FREE_SLOTS_URL.format(location_id=location_id, cal_id=cal_id)),
                ("v1", GHL_V1_FREE_SLOTS_URL.format(cal_id=cal_id)),
            ]

            resp = None
            used_endpoint = None

            for endpoint_name, url in endpoints:
                try:
                    logger.info(f"📅 Trying {endpoint_name} free-slots endpoint: {url}")
                    resp = requests.get(url, headers=headers, params=params, timeout=20)
                    logger.info(f"   {endpoint_name} response status: {resp.status_code}")

                    if resp.status_code in [200, 201]:
                        used_endpoint = endpoint_name
                        break  # Success - stop trying other endpoints

                    # If 422 with userId, retry without it before moving to next endpoint
                    if resp.status_code == 422 and "userId" in params:
                        logger.warning(f"⚠️ {endpoint_name} 422 with userId — retrying without userId")
                        retry_params = {k: v for k, v in params.items() if k != "userId"}
                        resp = requests.get(url, headers=headers, params=retry_params, timeout=20)
                        logger.info(f"   {endpoint_name} retry response status: {resp.status_code}")
                        if resp.status_code in [200, 201]:
                            used_endpoint = endpoint_name
                            break

                    # Log failure and continue to next endpoint
                    logger.warning(f"⚠️ {endpoint_name} endpoint failed ({resp.status_code})")

                except Exception as e:
                    logger.warning(f"⚠️ {endpoint_name} endpoint error: {e}")
                    continue

            # Parse response if any endpoint succeeded
            if resp and resp.status_code in [200, 201]:
                try:
                    data = resp.json()
                    logger.debug(f"   Slots response ({used_endpoint}): {str(data)[:300]}")

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
                    logger.info(f"✅ Fetched {len(slots)} slots using {used_endpoint} endpoint")
                except Exception as parse_err:
                    logger.error(f"❌ Failed to parse slots response: {parse_err}")
                    slots = []
            else:
                logger.error(f"❌ All endpoints failed for calendar {cal_id}")
                slots = []

        if operation == "fetch_slots":
            if not slots:
                return "CALENDAR_UNAVAILABLE"

            parsed_slots = []
            for slot in slots:
                try:
                    start_str = slot.get("startTime") or slot.get("start") or (slot if isinstance(slot, str) else None)
                    if not start_str:
                        continue
                    # Normalize timezone suffixes
                    if start_str.endswith("Z"):
                        start_str = start_str.replace("Z", "+00:00")
                    # Display in CUSTOMER's timezone so times are meaningful to them
                    dt = datetime.fromisoformat(start_str).astimezone(customer_tz)
                    if 9 <= dt.hour < 19:
                        parsed_slots.append(dt)
                except Exception:
                    continue

            if not parsed_slots:
                return "CALENDAR_UNAVAILABLE"

            parsed_slots.sort()
            now_customer = datetime.now(customer_tz)

            morning = [s for s in parsed_slots if 9 <= s.hour < 12]
            afternoon = [s for s in parsed_slots if 12 <= s.hour < 19]

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
                day = "tomorrow" if dt.date() == (now_customer + timedelta(days=1)).date() else dt.strftime("%A")
                time_str = dt.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
                return f"{time_str} {day}"

            options = []
            if morning_picks:
                options.append(" or ".join(format_slot(s) for s in morning_picks))
            if afternoon_picks:
                options.append(" or ".join(format_slot(s) for s in afternoon_picks))

            if not options:
                return "CALENDAR_UNAVAILABLE"

            result = "I've got " + (", or ".join(options) if len(options) > 1 else options[0])
            # Add timezone label when customer is in a different timezone than agent
            if tz_differ:
                # Use short timezone abbreviation (e.g., "EST", "PST", "CT")
                tz_label = now_customer.strftime("%Z") or customer_tz_str.split("/")[-1]
                result += f" (your time, {tz_label})"
                logger.info(f"📅 SLOTS DISPLAYED in customer tz {customer_tz_str} (agent tz: {local_tz_str})")
            return result

    # === BOOK APPOINTMENT ===
    if operation == "book" and selected_time and contact_id:
        # Detect token type for logging
        token_info = detect_token_type(access_token)
        token_version = token_info.get("version", "unknown")

        # Run debug check only when DEBUG logging is enabled (skipped in production)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("PRE-BOOKING DEBUG CHECK - Verifying calendar and contact exist...")
            try:
                calendar_exists = ghl_debug_check(access_token, location_id, cal_id, contact_id)
                if not calendar_exists:
                    logger.error(f"❌ DEBUG CHECK FAILED: Calendar '{cal_id}' not found in location '{location_id}'. ABORTING BOOKING.")
                    return False
            except Exception as debug_err:
                logger.warning(f"⚠️ Debug check threw error (continuing anyway): {debug_err}")

        time_str = selected_time.lower().strip()
        logger.info(f"📅 BOOKING TIME PARSE | raw selected_time='{selected_time[:100]}'")
        # Use customer's timezone to interpret their requested time
        # (when they say "11am" they mean 11am in THEIR timezone)
        now_local = datetime.now(customer_tz)

        # Determine target date from context
        target_date = now_local.date()

        # Day abbreviation map for matching "tues", "thurs", etc.
        day_abbrevs = {
            "mon": "monday", "tue": "tuesday", "tues": "tuesday",
            "wed": "wednesday", "thu": "thursday", "thurs": "thursday",
            "fri": "friday", "sat": "saturday", "sun": "sunday",
        }
        # Expand abbreviations in time_str for matching
        time_str_expanded = time_str
        for abbr, full in sorted(day_abbrevs.items(), key=lambda x: -len(x[0])):
            # Word-boundary replacement to avoid partial matches
            time_str_expanded = re.sub(r'\b' + abbr + r'\b', full, time_str_expanded)

        # "next week" flag — shifts matching to 7+ days out
        next_week = "next week" in time_str_expanded

        if "tomorrow" in time_str_expanded:
            target_date = (now_local + timedelta(days=1)).date()
        else:
            # Check for ordinal date like "the 17th", "march 17"
            ordinal_match = re.search(r'(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)', time_str_expanded)
            month_date_match = re.search(
                r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?'
                r'|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
                r'\s+(\d{1,2})', time_str_expanded)

            if month_date_match:
                # Explicit month + day like "march 17"
                month_names = {
                    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
                    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
                    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
                    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
                }
                m_name = month_date_match.group(1).lower()
                m_num = month_names.get(m_name, now_local.month)
                d_num = int(month_date_match.group(2))
                try:
                    candidate = now_local.date().replace(month=m_num, day=d_num)
                    if candidate < now_local.date():
                        candidate = candidate.replace(year=candidate.year + 1)
                    target_date = candidate
                except ValueError:
                    pass  # Invalid date, fall through to day-name matching
                logger.info(f"📅 DATE PARSED (month+day): {target_date} from '{month_date_match.group()}'")
            elif ordinal_match:
                # "the 17th" — find next occurrence of that day-of-month
                target_day = int(ordinal_match.group(1))
                for offset in range(0, 32):
                    candidate = (now_local + timedelta(days=offset)).date()
                    if candidate.day == target_day:
                        target_date = candidate
                        break
                logger.info(f"📅 DATE PARSED (ordinal): {target_date} from '{ordinal_match.group()}'")
            else:
                # Check for day names (search up to 14 days ahead)
                search_range = 14
                # If "next week", start searching from next Monday
                start_offset = 0
                if next_week:
                    days_until_monday = (7 - now_local.weekday()) % 7
                    if days_until_monday == 0:
                        days_until_monday = 7  # If today is Monday, "next week" = next Monday
                    start_offset = days_until_monday

                for day_offset in range(start_offset, search_range):
                    check_date = (now_local + timedelta(days=day_offset)).date()
                    day_name = check_date.strftime("%A").lower()
                    if day_name in time_str_expanded:
                        target_date = check_date
                        break

        # Parse time - try pattern WITH am/pm first (most reliable)
        hour, minute = None, 0
        # Pattern 1: Time with explicit AM/PM (e.g., "4:00 pm", "9am", "2:30 p.m.")
        match_with_period = re.search(r'(\d{1,2}):?(\d{2})?\s*(pm|p\.m\.|am|a\.m\.)', time_str)
        if match_with_period:
            h = int(match_with_period.group(1))
            m = int(match_with_period.group(2) or 0)
            period = match_with_period.group(3).lower().replace(".", "")  # "p.m." -> "pm"
            if "pm" in period and h != 12:
                h += 12
            elif "am" in period and h == 12:
                h = 0
            hour, minute = h, m
            logger.info(f"📅 TIME PARSED (with AM/PM): {hour}:{minute:02d} from '{match_with_period.group()}'")

        # Pattern 2: Time without AM/PM (e.g., "4", "4:30") - needs inference
        if hour is None:
            match_bare = re.search(r'(\d{1,2}):?(\d{2})?', time_str)
            if match_bare:
                h = int(match_bare.group(1))
                m = int(match_bare.group(2) or 0)
                # Infer AM/PM: hours 1-7 default to PM (business context, afternoon appointments)
                # Hours 8-11 default to AM, 12+ stays as-is
                if 1 <= h <= 7:
                    logger.info(f"📅 TIME INFER: Bare hour {h} -> assuming PM (afternoon) -> {h + 12}:00")
                    h += 12
                elif h == 0:
                    h = 9  # Midnight makes no sense, default to 9 AM
                hour, minute = h, m
                logger.info(f"📅 TIME PARSED (inferred): {hour}:{minute:02d} from '{match_bare.group()}'")

        # If no time could be parsed, ABORT booking — never guess a time
        if hour is None:
            logger.error(f"🚨 BOOKING ABORTED: Could not parse any time from '{time_str[:60]}' — refusing to guess")
            return False

        hour = max(9, min(19, hour))

        # Build start_dt in CUSTOMER's timezone (they said "11am" meaning their 11am)
        start_dt = datetime.combine(target_date, time(hour, minute), tzinfo=customer_tz)
        end_dt = start_dt + timedelta(minutes=30)

        # --- SLOT VALIDATION: Cross-reference against actual available slots ---
        # If the requested datetime is in the past or not actually available,
        # search cached slots for the nearest future slot matching the requested time.
        slot_matched = False
        if slots:
            parsed_avail = []
            for s in slots:
                try:
                    ss = s.get("startTime") or s.get("start") or (s if isinstance(s, str) else None)
                    if not ss:
                        continue
                    if ss.endswith("Z"):
                        ss = ss.replace("Z", "+00:00")
                    # Parse slots into customer's timezone for apples-to-apples comparison
                    parsed_avail.append(datetime.fromisoformat(ss).astimezone(customer_tz))
                except Exception:
                    continue

            # Check if requested start_dt is actually among available slots (within 5 min tolerance)
            for avail_dt in parsed_avail:
                if abs((avail_dt - start_dt).total_seconds()) < 300:
                    slot_matched = True
                    start_dt = avail_dt  # snap to exact slot time
                    end_dt = start_dt + timedelta(minutes=30)
                    logger.info(f"📅 SLOT MATCH: Exact match found at {start_dt}")
                    break

            if not slot_matched:
                logger.warning(f"📅 SLOT MISMATCH: {start_dt} not in available slots — searching for nearest match at {hour}:{minute:02d}")
                # Find the nearest future slot that matches the requested hour (within 30 min)
                candidates = []
                for avail_dt in parsed_avail:
                    if avail_dt <= now_local:
                        continue  # skip past slots
                    time_diff_minutes = abs(avail_dt.hour * 60 + avail_dt.minute - (hour * 60 + minute))
                    if time_diff_minutes <= 30:
                        candidates.append(avail_dt)

                if candidates:
                    candidates.sort()
                    start_dt = candidates[0]
                    end_dt = start_dt + timedelta(minutes=30)
                    slot_matched = True
                    logger.info(f"📅 SLOT REMAP: Nearest matching slot found at {start_dt} (originally requested {target_date} {hour}:{minute:02d})")
                else:
                    # No matching slot found — ABORT rather than silently booking a random time
                    logger.warning(f"📅 SLOT MISMATCH ABORT: No slot near {hour}:{minute:02d} on {target_date} — refusing to book a different time")
                    return False

        if not slot_matched and start_dt <= now_local:
            logger.warning(f"📅 TIME IN PAST: {start_dt} has already passed, shifting to tomorrow")
            target_date = (now_local + timedelta(days=1)).date()
            start_dt = datetime.combine(target_date, time(hour, minute), tzinfo=customer_tz)
            end_dt = start_dt + timedelta(minutes=30)

        if start_dt.date() > (now_local + timedelta(days=14)).date():
            logger.error(f"🚨 BOOKING BLOCKED: Time more than 14 days ahead | requested={start_dt} | contact={contact_id}")
            return False

        # Convert from customer timezone to agent timezone for GHL calendar API
        # GHL books in the agent's calendar timezone, so we must convert
        start_dt_agent = start_dt.astimezone(local_tz)
        end_dt_agent = end_dt.astimezone(local_tz)
        if tz_differ:
            logger.info(f"📅 TIMEZONE CONVERT: Customer {start_dt.strftime('%I:%M %p %Z')} → Agent {start_dt_agent.strftime('%I:%M %p %Z')}")

        # Both PIT and OAuth use the same booking endpoint
        booking_url = GHL_BOOK_URL  # Same URL for all token types
        payload = {
            "calendarId": cal_id,
            "locationId": location_id,
            "contactId": contact_id,
            "startTime": start_dt_agent.isoformat(),
            "endTime": end_dt_agent.isoformat(),
            "title": f"Life Insurance Review {first_name or 'Lead'}",
            "appointmentStatus": "confirmed",
            "selectedTimezone": local_tz_str,
            "meetingLocationType": "custom",
            "meetingLocationId": "custom_0",
        }
        # Only include assignedUserId if set (avoids 422 "not part of calendar team")
        if crm_user_id:
            payload["assignedUserId"] = crm_user_id
        logger.info(f"📅 Using booking endpoint: {booking_url}")

        # ROBUST BOOKING with 3 retries and detailed error logging
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"📅 BOOKING ATTEMPT {attempt}/{max_attempts} ({token_version})")
                logger.info(f"   Contact: {contact_id}")
                logger.info(f"   Customer time: {start_dt.strftime('%Y-%m-%d %I:%M %p %Z')} | Agent/GHL time: {start_dt_agent.strftime('%Y-%m-%d %I:%M %p %Z')}")
                logger.info(f"   URL: {booking_url}")
                logger.info(f"   Payload: {payload}")

                resp = requests.post(booking_url, json=payload, headers=headers, timeout=30)

                # DETAILED RESPONSE LOGGING
                logger.debug(f"   Full URL sent: {resp.request.url}")
                logger.info(f"   📬 Response Status: {resp.status_code}")
                logger.debug(f"   📬 Response Body: {resp.text[:500]}")

                if resp.status_code in [200, 201]:
                    # VERIFY the booking was actually created
                    try:
                        result_data = resp.json()
                        event_id = result_data.get('id') or result_data.get('event', {}).get('id')
                        logger.debug(f"   Parsed event_id: {event_id}")

                        # Return the actual booked time (in customer's timezone) so the LLM can confirm it
                        booked_time_str = start_dt.strftime("%I:%M %p on %A, %B %-d").lstrip("0")
                        if event_id:
                            logger.info(f"✅ BOOKING CONFIRMED ({token_version}) | contact={contact_id} | time={start_dt} | event_id={event_id}")
                            return booked_time_str
                        else:
                            logger.warning(f"⚠️ BOOKING CREATED BUT NO EVENT ID | contact={contact_id} | response={resp.text[:200]}")
                            return booked_time_str  # Assume success if 200/201 even without event_id
                    except Exception as parse_err:
                        logger.warning(f"⚠️ BOOKING CREATED BUT PARSE FAILED | contact={contact_id} | error={parse_err}")
                        logger.debug(f"   Raw response: {resp.text[:500]}")
                        booked_time_str = start_dt.strftime("%I:%M %p on %A, %B %-d").lstrip("0")
                        return booked_time_str  # Assume success if 200/201 even if parse fails

                else:
                    error_details = {
                        "status_code": resp.status_code,
                        "response_text": resp.text[:500],
                        "contact_id": contact_id,
                        "calendar_id": cal_id,
                        "requested_time": start_dt.isoformat(),
                        "payload": payload
                    }

                    # 422 with "not part of calendar team" → retry without assignedUserId
                    if resp.status_code == 422 and "not part of calendar team" in resp.text:
                        if "assignedUserId" in payload:
                            logger.warning(f"⚠️ assignedUserId '{payload['assignedUserId']}' not on calendar team — retrying without it")
                            del payload["assignedUserId"]
                            continue  # retry this attempt with the fixed payload

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
                    elif resp.status_code == 422:
                        logger.error(f"🚨 BOOKING FAILED: UNPROCESSABLE ENTITY | {error_details}")
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