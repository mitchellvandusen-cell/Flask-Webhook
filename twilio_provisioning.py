# twilio_provisioning.py — White-label Twilio sub-account provisioning
#
# Architecture: One master Twilio account (yours) creates sub-accounts per subscriber.
# Users never see Twilio. They sign up, pay, and the dialer just works.
#
# Sub-account flow:
#   1. Subscriber signs up / activates voice
#   2. Backend creates a Twilio sub-account under the master
#   3. Backend creates a TwiML App for webhooks
#   4. User buys their own phone number via the dashboard
#   5. Credentials stored in voice_config (internal, never shown in UI)
#
# CNAM / Spam Protection:
#   - SHAKEN/STIR attestation is automatic on Twilio
#   - CNAM registration via Twilio Trust Hub CNAM Trust Product
#     (not friendly_name — that's just a Twilio-internal label)

import os
import json
import logging
import time
from urllib.parse import quote

from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioRestException
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

logger = logging.getLogger("twilio_provisioning")

US_STATE_ABBREVS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

# Master Twilio credentials (from .env)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")  # master fallback number
TWILIO_API_KEY_SID = os.getenv("TWILIO_API_KEY_SID", "")
TWILIO_API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET", "")

# Cache the master client
_master_client = None


def _trusthub_update_status(
    resource_type: str,
    resource_sid: str,
    target_status: str,
    account_sid: str = "",
    auth_token: str = "",
) -> dict:
    """
    Update a TrustHub resource status via direct HTTP POST.

    Bypasses the twilio-python SDK's .update() method which has known issues
    with TrustHub status enums — the SDK can silently drop the Status parameter,
    causing the resource to stay in 'draft' while returning HTTP 200.

    Args:
        resource_type: "TrustProducts" or "CustomerProfiles"
        resource_sid: The BU... SID of the resource
        target_status: Status to set (e.g. "pending-review")
        account_sid: Account SID for auth (defaults to master)
        auth_token: Auth token for auth (defaults to master)

    Returns:
        dict: Full JSON response from Twilio API
    """
    import requests as _requests
    url = f"https://trusthub.twilio.com/v1/{resource_type}/{resource_sid}"
    sid = account_sid or TWILIO_ACCOUNT_SID
    token = auth_token or TWILIO_AUTH_TOKEN
    resp = _requests.post(
        url,
        data={"Status": target_status},
        auth=(sid, token),
    )
    logger.info(
        f"[TrustHub] POST {url} Status={target_status} → "
        f"HTTP {resp.status_code}: {resp.text[:500]}"
    )
    if resp.status_code not in (200, 201):
        raise TwilioRestException(
            status=resp.status_code, uri=url,
            msg=f"TrustHub status update failed: HTTP {resp.status_code} — {resp.text[:300]}"
        )
    return resp.json()


def get_master_client() -> TwilioClient:
    """Get or create the master Twilio client."""
    global _master_client
    if _master_client is None:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            raise ValueError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in environment")
        _master_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _master_client


def _ensure_sub_account_auth_token(sub_account_sid: str, sub_account_auth_token: str) -> str:
    """Ensure we have a valid auth token for a sub-account.

    If the token is missing from voice_config (e.g., older provisioned accounts),
    attempt to recover it from the Twilio API.  Returns the token string or raises
    ValueError if it cannot be obtained.

    All Trust Hub operations are ISV-only (sub-accounts). The master account
    handles its own Trust Hub via the Twilio Console.
    """
    if is_master_account(sub_account_sid):
        raise ValueError(
            "Trust Hub operations are ISV-only — the master account manages "
            "its own Business Profile, CNAM, and Voice Integrity via the Twilio Console. "
            "This function should only be called for sub-accounts."
        )

    if sub_account_auth_token:
        return sub_account_auth_token

    # Try to recover from Twilio
    try:
        master = get_master_client()
        acct = master.api.accounts(sub_account_sid).fetch()
        token = acct.auth_token or ""
        if token:
            logger.info(
                f"[TrustHub] Recovered auth token for sub-account {sub_account_sid} "
                "from Twilio API"
            )
            return token
    except Exception as e:
        logger.warning(f"[TrustHub] Could not fetch auth token for {sub_account_sid}: {e}")

    raise ValueError(
        f"Sub-account {sub_account_sid} has no auth token. "
        "TrustHub API calls require native sub-account credentials. "
        "Re-provision voice to fix this."
    )


def get_sub_account_client(sub_account_sid: str) -> TwilioClient:
    """Get a Twilio client authenticated for a sub-account.
    Uses master credentials but targets the sub-account for API calls.
    ONLY use this for Core REST API calls (api.twilio.com/2010-04-01/Accounts/{account_sid}/...)
    which support path-based account scoping via the `account_sid` parameter.
    For Messaging/TrustHub APIs, use get_sub_account_client_native() instead."""
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, sub_account_sid)


def get_sub_account_client_native(sub_account_sid: str,
                                   sub_account_auth_token: str) -> TwilioClient:
    """Get a Twilio client using the sub-account's OWN credentials.

    REQUIRED for Messaging API (messaging.twilio.com/v1) and TrustHub API
    (trusthub.twilio.com/v1). These APIs authenticate via HTTP Basic auth and
    scope resources to the `username` account — NOT to a path-based account_sid.
    Using master credentials here would return master-account resources instead of
    the subscriber's sub-account resources, causing cross-account contamination.
    """
    if not sub_account_auth_token:
        # Try to recover the auth token from Twilio before falling back.
        # Silently using master credentials causes TrustHub to see the master
        # account (a direct customer) and reject ISV operations on sub-accounts.
        sub_account_auth_token = _ensure_sub_account_auth_token(
            sub_account_sid, sub_account_auth_token
        )
    return TwilioClient(sub_account_sid, sub_account_auth_token)


# ──────────────────────────────────────────────────────────────
# SUB-ACCOUNT MANAGEMENT
# ──────────────────────────────────────────────────────────────

def create_sub_account(friendly_name: str) -> dict:
    """
    Create a Twilio sub-account for a subscriber.
    Returns dict with sub-account details.
    """
    client = get_master_client()
    try:
        account = client.api.accounts.create(friendly_name=friendly_name)
        logger.info(f"Created Twilio sub-account: {account.sid} ({friendly_name})")
        return {
            "sid": account.sid,
            "auth_token": account.auth_token,
            "friendly_name": account.friendly_name,
            "status": account.status,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to create sub-account: {e}")
        raise


def suspend_sub_account(sub_account_sid: str) -> bool:
    """Suspend a sub-account (e.g. when subscription lapses)."""
    client = get_master_client()
    try:
        client.api.accounts(sub_account_sid).update(status="suspended")
        logger.info(f"Suspended sub-account: {sub_account_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to suspend sub-account {sub_account_sid}: {e}")
        return False


def reactivate_sub_account(sub_account_sid: str) -> bool:
    """Re-activate a suspended sub-account."""
    client = get_master_client()
    try:
        client.api.accounts(sub_account_sid).update(status="active")
        logger.info(f"Reactivated sub-account: {sub_account_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to reactivate sub-account {sub_account_sid}: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# TwiML APP MANAGEMENT
# ──────────────────────────────────────────────────────────────

def create_twiml_app(sub_account_sid: str, webhook_base_url: str) -> dict:
    """
    Create a TwiML Application for call handling on a sub-account.
    This routes inbound calls and status callbacks to our server.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        app = client.applications.create(
            friendly_name="GrokBot Voice",
            voice_url=f"{webhook_base_url}/voice/inbound",
            voice_method="POST",
            status_callback=f"{webhook_base_url}/voice/status",
            status_callback_method="POST",
        )
        logger.info(f"Created TwiML App: {app.sid} for sub-account {sub_account_sid}")
        return {
            "twiml_app_sid": app.sid,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to create TwiML App: {e}")
        raise


def update_twiml_app(sub_account_sid: str, twiml_app_sid: str,
                      webhook_base_url: str) -> bool:
    """
    Update a TwiML Application's voice_url and status_callback to point
    to the current server.  Called during token generation so the TwiML
    app always reaches the live server even after URL changes.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        client.applications(twiml_app_sid).update(
            voice_url=f"{webhook_base_url}/voice/inbound",
            voice_method="POST",
            status_callback=f"{webhook_base_url}/voice/status",
            status_callback_method="POST",
        )
        logger.info(f"Updated TwiML App {twiml_app_sid} voice_url -> {webhook_base_url}/voice/inbound")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to update TwiML App {twiml_app_sid}: {e}")
        return False


def create_api_key(account_sid: str) -> dict:
    """
    Create an API Key on a specific account (master or sub-account).
    Required for generating AccessTokens — the API key must belong to
    the same account that will be used in the token.
    """
    client = get_sub_account_client(account_sid)
    try:
        key = client.new_keys.create(friendly_name="GrokBot VoIP Key")
        logger.info(f"Created API Key: {key.sid} for account {account_sid}")
        return {
            "api_key_sid": key.sid,
            "api_key_secret": key.secret,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to create API Key on {account_sid}: {e}")
        raise


# ──────────────────────────────────────────────────────────────
# PHONE NUMBER MANAGEMENT
# ──────────────────────────────────────────────────────────────

def search_available_numbers(number_type: str = "local", area_code: str = "",
                              state: str = "", city: str = "",
                              zip_code: str = "", contains: str = "",
                              sms_enabled: bool = True,
                              country: str = "US",
                              limit: int = 20) -> list:
    """
    Search for available phone numbers to purchase.
    Mirrors Twilio's search filters: type (local/toll_free/mobile),
    area code, state, city, zip, contains pattern.
    """
    client = get_master_client()
    try:
        kwargs = {"limit": limit, "voice_enabled": True}
        if sms_enabled:
            kwargs["sms_enabled"] = True
        if area_code:
            kwargs["area_code"] = area_code
        if state:
            normalized = US_STATE_ABBREVS.get(state.strip().lower(), state.strip())
            kwargs["in_region"] = normalized
        if city:
            kwargs["in_locality"] = city
        if zip_code:
            kwargs["in_postal_code"] = zip_code
        if contains:
            kwargs["contains"] = contains

        available = client.available_phone_numbers(country)
        if number_type == "toll_free":
            numbers = available.toll_free.list(**kwargs)
        elif number_type == "mobile":
            if country == "US":
                logger.info("Mobile numbers unavailable for US; falling back to local")
                numbers = available.local.list(**kwargs)
            else:
                numbers = available.mobile.list(**kwargs)
        else:
            numbers = available.local.list(**kwargs)

        return [
            {
                "phone": n.phone_number,
                "friendly_name": n.friendly_name,
                "locality": n.locality or "",
                "region": n.region or "",
                "postal_code": getattr(n, "postal_code", "") or "",
                "capabilities": {
                    "voice": n.capabilities.get("voice", False),
                    "sms": n.capabilities.get("SMS", False),
                    "mms": n.capabilities.get("MMS", False),
                },
            }
            for n in numbers
        ]
    except TwilioRestException as e:
        logger.error(f"Number search failed: {e}")
        return []


def buy_phone_number(sub_account_sid: str, phone_number: str,
                      webhook_base_url: str, twiml_app_sid: str = "") -> dict:
    """
    Purchase a phone number on a sub-account and configure it for voice.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        kwargs = {
            "phone_number": phone_number,
            "voice_url": f"{webhook_base_url}/voice/inbound",
            "voice_method": "POST",
            "status_callback": f"{webhook_base_url}/voice/status",
            "status_callback_method": "POST",
        }
        if twiml_app_sid:
            kwargs["voice_application_sid"] = twiml_app_sid

        number = client.incoming_phone_numbers.create(**kwargs)
        logger.info(f"Purchased number {number.phone_number} (SID: {number.sid}) on sub-account {sub_account_sid}")
        return {
            "sid": number.sid,
            "phone": number.phone_number,
            "friendly_name": number.friendly_name,
        }
    except TwilioRestException as e:
        logger.error(f"Number purchase failed: {e}")
        raise


def list_phone_numbers(sub_account_sid: str) -> list:
    """List all phone numbers on a sub-account."""
    client = get_sub_account_client(sub_account_sid)
    try:
        numbers = client.incoming_phone_numbers.list()
        return [
            {
                "sid": n.sid,
                "phone": n.phone_number,
                "friendly_name": n.friendly_name,
                "capabilities": {
                    "voice": n.capabilities.get("voice", False),
                    "sms": n.capabilities.get("sms", False),
                    "mms": n.capabilities.get("mms", False),
                    "fax": n.capabilities.get("fax", False),
                },
                "status": "active",
                "voice_url": n.voice_url or "",
                "created_at": n.date_created.isoformat() if n.date_created else "",
            }
            for n in numbers
        ]
    except TwilioRestException as e:
        logger.error(f"Failed to list numbers: {e}")
        return []


def release_phone_number(sub_account_sid: str, number_sid: str) -> bool:
    """Release a phone number from a sub-account."""
    client = get_sub_account_client(sub_account_sid)
    try:
        client.incoming_phone_numbers(number_sid).delete()
        logger.info(f"Released number {number_sid} from sub-account {sub_account_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to release number: {e}")
        return False


def update_phone_number_webhooks(sub_account_sid: str, number_sid: str,
                                   webhook_base_url: str) -> bool:
    """Update webhooks on an existing phone number."""
    client = get_sub_account_client(sub_account_sid)
    try:
        client.incoming_phone_numbers(number_sid).update(
            voice_url=f"{webhook_base_url}/voice/inbound",
            voice_method="POST",
            status_callback=f"{webhook_base_url}/voice/status",
            status_callback_method="POST",
        )
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to update number webhooks: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# BROWSER CALLING — Twilio Client JS SDK tokens
# ──────────────────────────────────────────────────────────────

def generate_voice_token(identity: str, twiml_app_sid: str,
                          sub_account_sid: str = "",
                          api_key_sid: str = "",
                          api_key_secret: str = "") -> str:
    """
    Generate a Twilio Access Token with Voice grant for browser calling.
    Uses per-subscriber API Key if provided, otherwise falls back to env vars.
    The API key MUST belong to the same account as account_sid.
    """
    # Use per-subscriber API key if available, otherwise fall back to env vars
    key_sid = api_key_sid or TWILIO_API_KEY_SID
    key_secret = api_key_secret or TWILIO_API_KEY_SECRET

    if not key_sid or not key_secret:
        raise ValueError("No API key available — set TWILIO_API_KEY_SID/SECRET or provision per-subscriber keys")

    account_sid = sub_account_sid or TWILIO_ACCOUNT_SID

    logger.info(f"[generate_voice_token] account_sid={account_sid} api_key={key_sid} identity={identity} twiml_app={twiml_app_sid}")

    token = AccessToken(
        account_sid,
        key_sid,
        key_secret,
        identity=identity,
        ttl=3600,  # 1 hour
    )

    voice_grant = VoiceGrant(
        outgoing_application_sid=twiml_app_sid,
        incoming_allow=True,
    )
    token.add_grant(voice_grant)

    jwt_token = token.to_jwt()
    # Some SDK versions return bytes — ensure we return a string
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode('utf-8')
    return jwt_token


# ──────────────────────────────────────────────────────────────
# CALL MANAGEMENT — Twilio REST API
# ──────────────────────────────────────────────────────────────

def create_outbound_call(sub_account_sid: str, to: str, from_number: str,
                          webhook_base_url: str, status_callback_events: list = None,
                          machine_detection: str = None,
                          custom_params: dict = None,
                          ring_timeout: int = None) -> dict:
    """
    Create an outbound call via Twilio REST API.
    Returns call details including call_sid.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        kwargs = {
            "to": to,
            "from_": from_number,
            "url": f"{webhook_base_url}/voice/outbound-twiml",
            "method": "POST",
            "status_callback": f"{webhook_base_url}/voice/status",
            "status_callback_method": "POST",
            "status_callback_event": status_callback_events or [
                "initiated", "ringing", "answered", "completed",
            ],
            "record": False,  # We handle recording separately
        }

        # Configurable ring timeout (Twilio enforces server-side)
        if ring_timeout and ring_timeout > 0:
            kwargs["timeout"] = min(max(ring_timeout, 15), 120)  # Twilio range: 15-600, sane max 120

        if machine_detection:
            kwargs["machine_detection"] = machine_detection
            kwargs["machine_detection_timeout"] = 4
            kwargs["async_amd"] = True
            kwargs["async_amd_status_callback"] = f"{webhook_base_url}/voice/amd-status"
            kwargs["async_amd_status_callback_method"] = "POST"

        # Pass custom params as URL params so they arrive in the TwiML webhook
        if custom_params:
            url_params = "&".join(f"{k}={quote(str(v))}" for k, v in custom_params.items())
            kwargs["url"] = f"{webhook_base_url}/voice/outbound-twiml?{url_params}"

        call = client.calls.create(**kwargs)
        logger.info(f"Outbound call created: {call.sid} to {to} from {from_number}")
        return {
            "call_sid": call.sid,
            "status": call.status,
            "to": to,
            "from": from_number,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to create outbound call: {e}")
        raise


def hangup_call(sub_account_sid: str, call_sid: str) -> bool:
    """Hang up an active call."""
    client = get_sub_account_client(sub_account_sid)
    try:
        client.calls(call_sid).update(status="completed")
        logger.info(f"Hung up call: {call_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to hang up call {call_sid}: {e}")
        return False


def transfer_call(sub_account_sid: str, call_sid: str,
                   transfer_to: str, webhook_base_url: str) -> bool:
    """
    Transfer a live call by updating the call's URL to a transfer TwiML.
    Twilio will fetch the new TwiML and execute the <Dial> to the target.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        client.calls(call_sid).update(
            url=f"{webhook_base_url}/voice/transfer-twiml?transfer_to={quote(transfer_to, safe='')}",
            method="POST",
        )
        logger.info(f"Transfer initiated: {call_sid} -> {transfer_to}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to transfer call {call_sid}: {e}")
        return False


def start_recording(sub_account_sid: str, call_sid: str,
                     webhook_base_url: str) -> str:
    """Start recording a call. Returns recording SID."""
    client = get_sub_account_client(sub_account_sid)
    try:
        recording = client.calls(call_sid).recordings.create(
            recording_channels="dual",
            recording_status_callback=f"{webhook_base_url}/voice/recording-status",
            recording_status_callback_method="POST",
        )
        logger.info(f"Recording started: {recording.sid} for call {call_sid}")
        return recording.sid
    except TwilioRestException as e:
        logger.error(f"Failed to start recording for {call_sid}: {e}")
        return ""


def get_recording_url(sub_account_sid: str, recording_sid: str) -> str:
    """Get the MP3 download URL for a recording."""
    # Twilio recording URLs follow a predictable pattern
    return f"https://api.twilio.com/2010-04-01/Accounts/{sub_account_sid}/Recordings/{recording_sid}.mp3"


# ──────────────────────────────────────────────────────────────
# SPAM PROTECTION / TRUST HUB
# ──────────────────────────────────────────────────────────────

def register_business_profile(sub_account_sid: str, business_name: str,
                                ein: str, street: str, city: str,
                                state: str, zip_code: str,
                                contact_name: str, contact_email: str,
                                contact_phone: str,
                                sub_account_auth_token: str = "",
                                business_type: str = "",
                                website: str = "",
                                contact_title: str = "") -> dict:
    """
    Register a business profile for CNAM / spam protection on a Twilio sub-account.

    ISV/Subaccounts flow:
      1. Find or create a Secondary Customer Profile (correct ISV policy)
      2. Create EndUser (customer_profile_business_information) with biz details
      3. Create Authorized Representative EndUser
      4. Create Address on the sub-account
      5. Assign all entities to the Secondary Profile (EntityAssignments)
      6. Link Secondary Profile to Primary Business Profile on master account
      7. Assign phone numbers to the profile
      8. Run evaluation and submit for review
      9. Set friendly_name on all numbers (internal label only — real CNAM
         registration requires a separate CNAM Trust Product via create_cnam_trust_product())

    The Secondary Profile is reusable across A2P, Voice Integrity, SHAKEN/STIR, CNAM.
    """
    results = {"steps": [], "errors": []}
    profile_sid = ""

    # All Trust Hub operations are ISV-only (sub-accounts).
    # Master account manages its own profiles via the Twilio Console.
    try:
        sub_account_auth_token = _ensure_sub_account_auth_token(
            sub_account_sid, sub_account_auth_token
        )
    except ValueError as e:
        logger.error(f"[SpamProtection] {e}")
        results["errors"].append(str(e))
        return results

    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    # ── Step 1: Secondary Customer Profile (ISV flow) ──
    # Per Twilio ISV docs: create a Secondary Customer Profile on the sub-account,
    # linked to the master's Primary Business Profile.
    # https://www.twilio.com/docs/trust-hub/trusthub-rest-api/api-create-secondary-customer-profile
    try:
        primary_profile_sid = _find_primary_profile_sid()
        profile_sid = _find_or_create_secondary_profile(
            client=client,
            sub_account_sid=sub_account_sid,
            business_name=business_name,
            contact_email=contact_email,
            primary_profile_sid=primary_profile_sid,
            sub_account_auth_token=sub_account_auth_token,
        )
        results["steps"].append({
            "name": "secondary_profile",
            "status": "ok",
            "sid": profile_sid,
        })
        logger.info(f"[SpamProtection] Secondary Profile: {profile_sid}")
    except Exception as e:
        logger.error(f"[SpamProtection] Customer Profile failed: {e}")
        results["errors"].append(f"Customer Profile: {e}")
        # Can't proceed without a profile — still do CNAM at minimum

    # ── Step 2: Create EndUser (business info) ──
    end_user_sid = ""
    if profile_sid:
        try:
            # Normalize state to 2-letter abbreviation
            state_upper = (state or "").strip().upper()
            if len(state_upper) > 2:
                state_upper = US_STATE_ABBREVS.get(state_upper.lower(), state_upper[:2])

            # Use user-provided business_type, fallback to inference from EIN
            resolved_biz_type = business_type or ("Corporation" if ein else "Partnership")

            end_user = client.trusthub.v1.end_users.create(
                friendly_name=f"Business: {business_name}",
                type="customer_profile_business_information",
                attributes={
                    "business_name": business_name,
                    "business_identity": "isv_reseller_or_partner",
                    "business_type": resolved_biz_type,
                    "business_industry": "INSURANCE",
                    "business_registration_identifier": "EIN",
                    "business_registration_number": ein,
                    "business_regions_of_operation": "USA_AND_CANADA",
                    "website_url": website or "",
                    "social_media_profile_urls": "",
                },
            )
            end_user_sid = end_user.sid
            results["steps"].append({
                "name": "end_user_business",
                "status": "ok",
                "sid": end_user_sid,
            })
            logger.info(f"[SpamProtection] Created business EndUser: {end_user_sid}")
        except TwilioRestException as e:
            logger.error(f"[SpamProtection] Business EndUser creation failed: {e}")
            results["errors"].append(f"Business EndUser: {e}")

    # ── Step 3: Create Authorized Representative EndUser ──
    auth_rep_sid = ""
    if profile_sid and contact_name:
        try:
            # Split contact name into first/last
            name_parts = contact_name.strip().split(None, 1)
            first_name = name_parts[0] if name_parts else contact_name
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            # Use user-provided title, fallback to "Owner"
            resolved_title = contact_title or "Owner"

            auth_rep = client.trusthub.v1.end_users.create(
                friendly_name=f"Auth Rep: {contact_name}",
                type="authorized_representative_1",
                attributes={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": contact_email,
                    "phone_number": contact_phone,
                    "business_title": resolved_title,
                    "job_position": resolved_title,
                },
            )
            auth_rep_sid = auth_rep.sid
            results["steps"].append({
                "name": "auth_representative",
                "status": "ok",
                "sid": auth_rep_sid,
            })
            logger.info(f"[SpamProtection] Created Auth Rep: {auth_rep_sid}")
        except TwilioRestException as e:
            logger.error(f"[SpamProtection] Auth Rep creation failed: {e}")
            results["errors"].append(f"Auth Rep: {e}")

    # ── Step 4: Create Address ──
    address_sid = ""
    if profile_sid and street:
        try:
            address = client.addresses.create(
                friendly_name=f"{business_name} Address",
                customer_name=business_name,
                street=street,
                city=city,
                region=state_upper if 'state_upper' in dir() else state,
                postal_code=zip_code,
                iso_country="US",
            )
            address_sid = address.sid
            results["steps"].append({
                "name": "address",
                "status": "ok",
                "sid": address_sid,
            })
            logger.info(f"[SpamProtection] Created Address: {address_sid}")
        except TwilioRestException as e:
            logger.error(f"[SpamProtection] Address creation failed: {e}")
            results["errors"].append(f"Address: {e}")

    # ── Step 5: EntityAssignments — link entities to profile ──
    if profile_sid:
        for entity_name, entity_sid in [
            ("end_user_business", end_user_sid),
            ("auth_representative", auth_rep_sid),
            ("address", address_sid),
        ]:
            if not entity_sid:
                continue
            try:
                client.trusthub.v1.customer_profiles(profile_sid) \
                    .customer_profiles_entity_assignments.create(
                        object_sid=entity_sid,
                    )
                logger.info(f"[SpamProtection] Assigned {entity_name} ({entity_sid}) → profile {profile_sid}")
            except TwilioRestException as e:
                if e.code == 20409:
                    logger.info(f"[SpamProtection] {entity_name} already assigned (20409)")
                else:
                    logger.warning(f"[SpamProtection] EntityAssignment for {entity_name} failed: {e}")
                    results["errors"].append(f"Assign {entity_name}: {e}")

    # ── Step 6: Assign phone numbers to profile ──
    if profile_sid:
        try:
            numbers = client.incoming_phone_numbers.list()
            nums_assigned = 0
            for num in numbers:
                try:
                    client.trusthub.v1.customer_profiles(profile_sid) \
                        .customer_profiles_channel_endpoint_assignment.create(
                            channel_endpoint_type="phone-number",
                            channel_endpoint_sid=num.sid,
                        )
                    nums_assigned += 1
                except TwilioRestException as e:
                    if e.code == 20409:
                        nums_assigned += 1  # already assigned
                    else:
                        logger.warning(f"[SpamProtection] Number assign failed {num.phone_number}: {e}")
            results["steps"].append({
                "name": "assign_numbers",
                "status": "ok",
                "assigned": nums_assigned,
                "total": len(numbers),
            })
        except Exception as e:
            logger.error(f"[SpamProtection] Number assignment failed: {e}")
            results["errors"].append(f"Number assignment: {e}")

    # ── Step 7: Evaluate and submit Secondary Profile for review ──
    # Per Twilio ISV docs: evaluate with Secondary Customer Profile policy,
    # then submit for Twilio review.
    if profile_sid:
        try:
            eval_result = client.trusthub.v1.customer_profiles(profile_sid) \
                .customer_profiles_evaluations.create(
                    policy_sid=SECONDARY_CUSTOMER_PROFILE_POLICY_SID,
                )
            eval_status = getattr(eval_result, "status", "unknown")
            logger.info(f"[SpamProtection] Evaluation: {eval_status}")

            if eval_status == "noncompliant":
                eval_results = getattr(eval_result, "results", None) or []
                noncompliant = [r.get("friendly_name", "unknown")
                                for r in eval_results
                                if isinstance(r, dict) and r.get("status") == "noncompliant"]
                results["steps"].append({
                    "name": "evaluation",
                    "status": "noncompliant",
                    "issues": noncompliant,
                })
                logger.warning(f"[SpamProtection] Evaluation noncompliant: {noncompliant}")
                # Still proceed — CNAM via friendly_name will work regardless
            else:
                # Submit for review — use direct HTTP to bypass SDK enum issues
                profile_resp = _trusthub_update_status(
                    "CustomerProfiles", profile_sid, "pending-review",
                    sub_account_sid, sub_account_auth_token,
                )
                results["steps"].append({
                    "name": "submit_review",
                    "status": "ok",
                    "profile_status": profile_resp.get("status", "unknown"),
                })
                logger.info(
                    f"[SpamProtection] Submitted profile {profile_sid} for review → "
                    f"{profile_resp.get('status', 'unknown')}"
                )
        except TwilioRestException as e:
            logger.warning(f"[SpamProtection] Evaluation/submit failed: {e}")
            results["errors"].append(f"Submit for review: {e}")

    # ── Step 8: Set friendly_name on all numbers ──
    # NOTE: This sets the Twilio-internal label only. Real CNAM registration
    # with carriers requires a separate CNAM Trust Product (see create_cnam_trust_product).
    # friendly_name does NOT propagate to carrier CNAM databases.
    # ONLY set friendly_name if a profile was actually created — otherwise
    # the label gives a false impression of "protection" in the dashboard.
    if profile_sid:
        try:
            numbers = client.incoming_phone_numbers.list()
            cnam_success = 0
            for num in numbers:
                try:
                    client.incoming_phone_numbers(num.sid).update(
                        friendly_name=business_name[:15],
                    )
                    cnam_success += 1
                except Exception as e:
                    logger.warning(f"CNAM update failed for {num.phone_number}: {e}")

            results["steps"].append({
                "name": "cnam_all_numbers",
                "status": "ok",
                "enabled": cnam_success,
                "total": len(numbers),
            })

        except TwilioRestException as e:
            logger.error(f"CNAM registration failed: {e}")
            results["errors"].append(str(e))
    else:
        logger.warning("[SpamProtection] Skipping friendly_name update — no profile created")
        results["steps"].append({
            "name": "cnam_all_numbers",
            "status": "skipped",
            "reason": "No Customer Profile created — cannot set CNAM labels",
        })

    # Add profile_sid to results for caller to save
    if profile_sid:
        results["profile_sid"] = profile_sid
    if end_user_sid:
        results["end_user_sid"] = end_user_sid

    return results


def get_spam_protection_status(sub_account_sid: str) -> dict:
    """Get current spam/CNAM protection status for a sub-account.

    Note: 'protected' count only includes numbers whose friendly_name was
    explicitly set to a business/personal CNAM name — NOT numbers that still
    have Twilio's default friendly_name (which is the phone number itself).
    Setting friendly_name does NOT constitute real CNAM registration.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        numbers = client.incoming_phone_numbers.list()
        total = len(numbers)
        nums_list = []
        protected = 0
        for n in numbers:
            fn = (n.friendly_name or "").strip()
            phone = n.phone_number or ""
            # A number has a CNAM name only if friendly_name is not the phone number default
            is_cnam = bool(fn) and fn != phone and not fn.startswith('+')
            if is_cnam:
                protected += 1
            nums_list.append({
                "phone": phone,
                "sid": n.sid,
                "friendly_name": fn,
                "status": "active",
            })

        return {
            "numbers_total": total,
            "numbers_protected": protected,
            "stir_shaken": "active",  # Twilio auto-manages STIR/SHAKEN
            "numbers": nums_list,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to get spam protection status: {e}")
        return {"numbers_total": 0, "numbers_protected": 0}


def get_cnam_monitor(sub_account_sid: str) -> list:
    """
    Get CNAM status for all numbers on a sub-account.
    Returns a list of dicts with phone, sid, friendly_name (the CNAM we set),
    and cnam_enabled flag.

    Note: friendly_name is just a Twilio-internal label. It does NOT mean
    the number has real CNAM registration with carriers. A number's
    friendly_name is always set (Twilio defaults it to the phone number).
    cnam_enabled is True only if the friendly_name was explicitly set to
    a business/personal name (not the phone number default).
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        numbers = client.incoming_phone_numbers.list()
        result = []
        for n in numbers:
            fn = (n.friendly_name or "").strip()
            phone = n.phone_number or ""
            # friendly_name is a real CNAM name only if it's not the phone number itself
            is_cnam = bool(fn) and fn != phone and not fn.startswith('+')
            result.append({
                "phone": phone,
                "sid": n.sid,
                "friendly_name": fn,
                "cnam_enabled": is_cnam,
                "date_created": str(n.date_created) if n.date_created else "",
            })
        return result
    except TwilioRestException as e:
        logger.error(f"CNAM monitor failed for {sub_account_sid}: {e}")
        return []


def update_cnam_for_number(sub_account_sid: str, number_sid: str,
                           business_name: str) -> dict:
    """Update CNAM (friendly_name) for a single phone number."""
    client = get_sub_account_client(sub_account_sid)
    try:
        cnam_name = business_name[:15].strip() if business_name else ""
        updated = client.incoming_phone_numbers(number_sid).update(
            friendly_name=cnam_name,
        )
        return {
            "status": "ok",
            "phone": updated.phone_number,
            "sid": updated.sid,
            "friendly_name": updated.friendly_name,
        }
    except TwilioRestException as e:
        logger.error(f"CNAM update failed for {number_sid}: {e}")
        return {"status": "error", "error": str(e)}


def cnam_lookup(phone_number: str) -> dict:
    """
    Look up CNAM (caller name) for a phone number using Twilio Lookup API v2.
    This queries what carriers see as the caller name for a given number.
    Uses master account credentials (lookup is not sub-account scoped).
    """
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        result = client.lookups.v2.phone_numbers(phone_number).fetch(
            fields="caller_name"
        )
        caller_name_info = result.caller_name or {}
        return {
            "phone": result.phone_number or phone_number,
            "caller_name": caller_name_info.get("caller_name", ""),
            "caller_type": caller_name_info.get("caller_type", ""),
            "error_code": caller_name_info.get("error_code"),
            "valid": result.valid if hasattr(result, 'valid') else True,
            "national_format": getattr(result, 'national_format', ''),
        }
    except TwilioRestException as e:
        logger.error(f"CNAM lookup failed for {phone_number}: {e}")
        return {
            "phone": phone_number,
            "caller_name": "",
            "caller_type": "",
            "error_code": e.code if hasattr(e, 'code') else None,
            "error": str(e),
        }


# ──────────────────────────────────────────────────────────────
# CNAM TRUST PRODUCT — CALLER ID NAME REGISTRATION
# ──────────────────────────────────────────────────────────────
#
# Twilio CNAM (Caller ID Name) registration via Trust Hub.
# Registers a display name with US CNAM authoritative databases so
# recipients see the business name on incoming calls.
#
# Flow:
#   1. Customer Profile (Primary for master, Secondary for sub-accounts)
#   2. Create CNAM Trust Product (policy: RNf3db3cd1fe25fcfd3c3ded065c8fea53)
#   3. Create EndUser type=cnam_information with cnam_display_name
#   4. Link Profile → Trust Product (EntityAssignment)
#   5. Link EndUser → Trust Product (EntityAssignment)
#   6. Assign phone numbers to Trust Product (ChannelEndpointAssignment)
#   7. Evaluate → submit for review
#   8. After approval: 48-72h for carrier propagation
#
# Requirements:
#   - Only US standard long-code numbers (no toll-free, no CA numbers)
#   - Display name: max 15 chars, starts with letter, letters/numbers/periods/commas/spaces only
#   - Phone numbers must be assigned to the Customer Profile first
#
# ISV/sub-account: Secondary Profile linked to Primary, all on sub-account client.
# Master account (direct customer): uses Primary Business Profile directly.
#
# State stored in voice_config["cnam"] JSONB.

# CNAM Trust Product policy SID — static across all Twilio accounts.
# https://www.twilio.com/docs/voice/brand-your-calls-using-cnam
CNAM_TRUST_PRODUCT_POLICY_SID = "RNf3db3cd1fe25fcfd3c3ded065c8fea53"


def validate_cnam_display_name(name: str) -> tuple:
    """
    Validate a CNAM display name per Twilio's requirements.
    Returns (is_valid, cleaned_name_or_error_message).

    Rules:
      - Max 15 characters
      - Must start with a letter
      - Only letters, numbers, periods, commas, and spaces
      - Must not be generic (city/state names) — caller responsibility
    """
    import re
    name = name.strip()
    if not name:
        return False, "CNAM display name is required"
    if len(name) > 15:
        return False, f"CNAM display name must be 15 characters or fewer (got {len(name)})"
    if not name[0].isalpha():
        return False, "CNAM display name must start with a letter"
    if not re.match(r'^[A-Za-z0-9., ]+$', name):
        return False, "CNAM display name can only contain letters, numbers, periods, commas, and spaces"
    return True, name


def create_cnam_trust_product(
    sub_account_sid: str,
    business_name: str,
    cnam_display_name: str,
    contact_email: str,
    sub_account_auth_token: str = "",
    existing_profile_sid: str = "",
) -> dict:
    """
    Create a CNAM Trust Product for caller ID name registration (ISV sub-account flow).

    ISV flow per Twilio docs:
      1. Find or create Secondary Customer Profile on sub-account
      2. Create CNAM Trust Product (policy_sid=RNf3db3cd1fe25fcfd3c3ded065c8fea53)
      3. Create CNAM EndUser with display name
      4. Link Profile → Trust Product (EntityAssignment)
      5. Link EndUser → Trust Product (EntityAssignment)

    Master account manages its own CNAM via the Twilio Console.
    Returns dict with trust_product_sid, profile_sid, end_user_sid, status, cnam_display_name.
    """
    # Validate display name
    valid, result = validate_cnam_display_name(cnam_display_name)
    if not valid:
        raise ValueError(result)

    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    try:
        # ── Step 1: Secondary Customer Profile (ISV flow) ──
        # Per Twilio ISV docs: find or create a Secondary Customer Profile
        # on the sub-account, linked to the master's Primary Business Profile.
        primary_sid = _find_primary_profile_sid()
        profile_sid = _find_or_create_secondary_profile(
            client=client,
            sub_account_sid=sub_account_sid,
            business_name=business_name,
            contact_email=contact_email,
            existing_profile_sid=existing_profile_sid,
            primary_profile_sid=primary_sid,
            sub_account_auth_token=sub_account_auth_token,
        )

        # ── Step 2: CNAM Trust Product ──
        trust_product = client.trusthub.v1.trust_products.create(
            friendly_name=f"CNAM: {business_name}",
            email=contact_email,
            policy_sid=CNAM_TRUST_PRODUCT_POLICY_SID,
        )
        logger.info(f"[CNAM] Created Trust Product: {trust_product.sid}")

        # ── Step 3: CNAM EndUser ──
        end_user = client.trusthub.v1.end_users.create(
            friendly_name=f"CNAM EndUser: {cnam_display_name}",
            type="cnam_information",
            attributes={
                "cnam_display_name": cnam_display_name.upper(),
            },
        )
        logger.info(f"[CNAM] Created EndUser: {end_user.sid}")

        # ── Step 4: Link Profile → Trust Product ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=profile_sid,
            )
        logger.info(f"[CNAM] Linked profile {profile_sid} → {trust_product.sid}")

        # ── Step 5: Link EndUser → Trust Product ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=end_user.sid,
            )
        logger.info(f"[CNAM] Linked EndUser {end_user.sid} → {trust_product.sid}")

        return {
            "trust_product_sid": trust_product.sid,
            "profile_sid": profile_sid,
            "end_user_sid": end_user.sid,
            "status": "draft",
            "cnam_display_name": cnam_display_name.upper(),
            "business_name": business_name,
        }

    except TwilioRestException as e:
        logger.error(f"[CNAM] Trust Product creation failed: {e}")
        raise


def assign_numbers_to_cnam(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sids: list,
    sub_account_auth_token: str = "",
    profile_sid: str = "",
) -> dict:
    """
    Assign phone numbers to a CNAM Trust Product.

    Numbers must first be assigned to the Customer Profile (required by Twilio),
    then to the Trust Product. Only US standard long-code numbers are eligible
    (no toll-free, no CA numbers).

    Handles conflict resolution: if a number is already assigned to a different
    CNAM Trust Product (e.g. old rejected one), unassigns it and retries.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    assigned = 0
    failed = []

    for pn_sid in phone_number_sids:
        try:
            # Assign to Customer Profile first (required before Trust Product)
            if profile_sid:
                try:
                    client.trusthub.v1.customer_profiles(profile_sid) \
                        .customer_profiles_channel_endpoint_assignment.create(
                            channel_endpoint_type="phone-number",
                            channel_endpoint_sid=pn_sid,
                        )
                    logger.info(f"[CNAM] Assigned {pn_sid} to profile {profile_sid}")
                except TwilioRestException as e:
                    if e.code == 20409 or e.status == 409:
                        logger.info(f"[CNAM] {pn_sid} already on profile (code={e.code})")
                    else:
                        raise

            # Then assign to CNAM Trust Product
            client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment.create(
                    channel_endpoint_type="phone-number",
                    channel_endpoint_sid=pn_sid,
                )
            assigned += 1
            logger.info(f"[CNAM] Assigned {pn_sid} to {trust_product_sid}")
        except TwilioRestException as e:
            if e.code == 20409 or e.status == 409:
                # Already assigned — check if to this TP or a different one
                import re as _re
                err_str = str(e)
                conflict_match = _re.search(r'(BU[a-f0-9A-F]{32})', err_str)
                conflict_sid = conflict_match.group(1) if conflict_match else None

                if conflict_sid and conflict_sid == trust_product_sid:
                    assigned += 1
                    logger.info(f"[CNAM] {pn_sid} already assigned to target TP (ok)")
                elif conflict_sid and conflict_sid != trust_product_sid:
                    # Assigned to different TP — unassign and retry
                    logger.info(f"[CNAM] {pn_sid} stuck on {conflict_sid}, unassigning and retrying")
                    try:
                        unassign_numbers_from_trust_product(
                            sub_account_sid, conflict_sid, [pn_sid], sub_account_auth_token)
                        client.trusthub.v1.trust_products(trust_product_sid) \
                            .trust_products_channel_endpoint_assignment.create(
                                channel_endpoint_type="phone-number",
                                channel_endpoint_sid=pn_sid,
                            )
                        assigned += 1
                        logger.info(f"[CNAM] Retry succeeded: {pn_sid} → {trust_product_sid}")
                    except TwilioRestException as retry_err:
                        failed.append({"sid": pn_sid, "error": str(retry_err)})
                        logger.warning(f"[CNAM] Retry failed for {pn_sid}: {retry_err}")
                else:
                    if e.code == 20409:
                        assigned += 1
                        logger.info(f"[CNAM] {pn_sid} already assigned (20409)")
                    else:
                        failed.append({"sid": pn_sid, "error": str(e)})
            else:
                failed.append({"sid": pn_sid, "error": str(e)})
                logger.warning(f"[CNAM] Failed to assign {pn_sid}: {e}")

    return {"assigned": assigned, "failed": failed, "total": len(phone_number_sids)}


def submit_cnam_for_review(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Submit the CNAM Trust Product for Twilio review.
    Runs an evaluation first to validate all required entities are attached,
    then sets status to pending-review.
    After approval, CNAM display name propagates to carrier databases (48-72h).
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        # Run evaluation first
        try:
            evaluation = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_evaluations.create(policy_sid=CNAM_TRUST_PRODUCT_POLICY_SID)
            eval_status = getattr(evaluation, "status", "unknown")
            logger.info(f"[CNAM] Evaluation for {trust_product_sid}: {eval_status}")
            if eval_status == "noncompliant":
                eval_results = getattr(evaluation, "results", None) or []
                error_details = []
                for r in eval_results:
                    if isinstance(r, dict) and r.get("status") == "noncompliant":
                        error_details.append(r.get("friendly_name", r.get("requirement_key", "unknown")))
                detail_msg = ", ".join(error_details) if error_details else "evaluation returned noncompliant"
                raise TwilioRestException(
                    status=400, uri="", msg=f"CNAM evaluation failed: {detail_msg}")
        except TwilioRestException:
            raise
        except Exception as eval_err:
            logger.warning(f"[CNAM] Evaluation check failed (proceeding): {eval_err}")

        auth_sid = sub_account_sid or TWILIO_ACCOUNT_SID
        auth_token = sub_account_auth_token or TWILIO_AUTH_TOKEN
        resp_data = _trusthub_update_status(
            "TrustProducts", trust_product_sid, "pending-review",
            auth_sid, auth_token,
        )
        tp_status = resp_data.get("status", "unknown")
        logger.info(f"[CNAM] Submitted {trust_product_sid} for review → {tp_status}")
        return {"trust_product_sid": resp_data.get("sid", trust_product_sid), "status": tp_status}
    except TwilioRestException as e:
        logger.error(f"[CNAM] Submit for review failed: {e}")
        raise


def discover_cnam_trust_product(
    sub_account_sid: str,
    sub_account_auth_token: str = "",
) -> dict | None:
    """
    Discover an existing CNAM Trust Product on the account by scanning
    Trust Hub for products matching the CNAM policy SID.
    Returns dict with trust_product_sid, status, friendly_name, cnam_display_name,
    assigned_numbers, or None if no CNAM product exists.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        products = client.trusthub.v1.trust_products.list(
            policy_sid=CNAM_TRUST_PRODUCT_POLICY_SID, limit=10
        )
        if not products:
            return None

        # Find the best product — prefer twilio-approved, then any non-draft
        best = None
        for tp in products:
            if tp.status == "twilio-approved":
                best = tp
                break
            if best is None or tp.status != "draft":
                best = tp
        if not best:
            return None

        # Fetch assigned numbers
        assigned_numbers = []
        try:
            assignments = client.trusthub.v1.trust_products(best.sid) \
                .trust_products_channel_endpoint_assignment.list(limit=100)
            assigned_numbers = [a.channel_endpoint_sid for a in assignments]
        except Exception as e:
            logger.warning(f"[CNAM] Could not list assigned numbers during discovery: {e}")

        # Extract CNAM display name from the friendly_name (format: "CNAM US ...")
        # or from EndUser attributes if available
        cnam_display_name = ""
        try:
            entity_assignments = client.trusthub.v1.trust_products(best.sid) \
                .trust_products_entity_assignments.list(limit=20)
            for ea in entity_assignments:
                obj_sid = ea.object_sid
                if obj_sid and obj_sid.startswith("IT"):
                    try:
                        eu = client.trusthub.v1.end_users(obj_sid).fetch()
                        attrs = eu.attributes or {}
                        if attrs.get("cnam_display_name"):
                            cnam_display_name = attrs["cnam_display_name"]
                            break
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[CNAM] Could not read EndUser for display name: {e}")

        # Fallback: extract from friendly_name
        if not cnam_display_name:
            fn = getattr(best, "friendly_name", "") or ""
            if fn.startswith("CNAM:"):
                cnam_display_name = fn[5:].strip()[:15]

        logger.info(
            f"[CNAM] Discovered Trust Product {best.sid} (status={best.status}, "
            f"display_name={cnam_display_name!r}, {len(assigned_numbers)} numbers)"
        )
        return {
            "trust_product_sid": best.sid,
            "status": best.status,
            "friendly_name": getattr(best, "friendly_name", ""),
            "cnam_display_name": cnam_display_name,
            "assigned_numbers": assigned_numbers,
            "assigned_count": len(assigned_numbers),
            "date_created": best.date_created.isoformat() if best.date_created else "",
        }
    except TwilioRestException as e:
        logger.error(f"[CNAM] Discovery failed for {sub_account_sid}: {e}")
        return None


def get_cnam_trust_product_status(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Check the current status of a CNAM Trust Product.
    Statuses: draft, pending-review, in-review, twilio-approved, twilio-rejected.
    When rejected, fetches evaluation failure reasons.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        tp = client.trusthub.v1.trust_products(trust_product_sid).fetch()

        # List assigned numbers
        assigned_numbers = []
        try:
            assignments = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment.list(limit=100)
            assigned_numbers = [a.channel_endpoint_sid for a in assignments]
        except Exception as e:
            logger.warning(f"[CNAM] Could not list assigned numbers: {e}")

        result = {
            "trust_product_sid": tp.sid,
            "status": tp.status,
            "friendly_name": getattr(tp, "friendly_name", ""),
            "date_created": tp.date_created.isoformat() if tp.date_created else "",
            "date_updated": tp.date_updated.isoformat() if tp.date_updated else "",
            "assigned_numbers": assigned_numbers,
            "assigned_count": len(assigned_numbers),
        }

        # If rejected, fetch failure reasons
        if tp.status == "twilio-rejected":
            try:
                evals = client.trusthub.v1.trust_products(trust_product_sid) \
                    .trust_products_evaluations.list(limit=5)
                if evals:
                    latest = evals[0]
                    eval_results = getattr(latest, "results", None) or []
                    failure_reasons = []
                    for r in eval_results:
                        if isinstance(r, dict) and r.get("status") in ("noncompliant", "failed"):
                            reason = r.get("friendly_name") or r.get("requirement_key") or "Unknown"
                            failure_reasons.append(reason)
                    result["failure_reasons"] = failure_reasons
            except Exception as e:
                logger.warning(f"[CNAM] Could not fetch rejection reasons: {e}")

        return result
    except TwilioRestException as e:
        logger.error(f"[CNAM] Status check failed for {trust_product_sid}: {e}")
        raise


# ──────────────────────────────────────────────────────────────
# A2P 10DLC — BRAND & CAMPAIGN REGISTRATION
# ──────────────────────────────────────────────────────────────
#
# Twilio A2P 10DLC flow:
#   1. Create a Trust Product (Brand) under the master account
#   2. Submit Brand for vetting (Twilio charges a one-time fee)
#   3. Create a Messaging Service on the sub-account
#   4. Associate phone number(s) with the Messaging Service
#   5. Create a Campaign under the Brand and link to Messaging Service
#
# OR — import an already-approved Brand/Campaign from another provider
# (e.g., GHL/LeadConnector) using the BrandRegistration + external
# campaign import flow.
#
# All state is stored in voice_config["a2p"] JSONB.

# ── Valid A2P use-case categories for campaign registration ──
A2P_USE_CASES = [
    "2FA", "ACCOUNT_NOTIFICATION", "CUSTOMER_CARE", "DELIVERY_NOTIFICATION",
    "FRAUD_ALERT", "HIGHER_EDUCATION", "LOW_VOLUME", "MARKETING",
    "MIXED", "POLLING_VOTING", "PUBLIC_SERVICE_ANNOUNCEMENT",
    "SECURITY_ALERT", "SOLE_PROPRIETOR",
]


def create_a2p_brand(sub_account_sid: str,
                     business_name: str, ein: str,
                     street: str, city: str, state: str, zip_code: str,
                     contact_email: str, contact_phone: str,
                     business_type: str = "private_profit",
                     stock_exchange: str = "NONE",
                     stock_ticker: str = "",
                     website: str = "",
                     vertical: str = "INSURANCE",
                     sub_account_auth_token: str = "") -> dict:
    """
    Register a new A2P 10DLC Brand via Twilio's Trust Hub + Brand
    Registration API.  Follows the ISV onboarding flow:

    1. Create Secondary Customer Profile
    2. Create EndUser (us_a2p_messaging_profile_information) with biz details
    3. Create TrustProduct (A2P Messaging Profile Bundle)
    4. Attach EndUser → TrustProduct
    5. Attach CustomerProfile → TrustProduct
    6. Submit TrustProduct for evaluation
    7. Create BrandRegistration (customer_profile_bundle_sid = profile,
       a2p_profile_bundle_sid = trust_product)

    Twilio docs:
      https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/onboarding-isv-api
    """
    # Each sub-account registers its own Trust Hub profile and brand so that
    # their business identity appears in Twilio — not the master account's identity.
    # MUST use sub-account's own credentials for TrustHub/Messaging APIs.
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        # ── Step 1: Secondary Customer Profile (ISV pattern) ──
        # Reuse existing approved Secondary Profile or create new one with
        # correct policy SID and linkage to Primary Business Profile.
        primary_sid = _find_primary_profile_sid()
        profile_sid = _find_or_create_secondary_profile(
            client, sub_account_sid, business_name, contact_email,
            primary_profile_sid=primary_sid,
            sub_account_auth_token=sub_account_auth_token,
        )
        logger.info(f"Created/reused A2P Secondary Customer Profile: {profile_sid}")

        # ── Step 2: EndUser with business information ──
        end_user = client.trusthub.v1.end_users.create(
            friendly_name=f"{business_name} A2P EndUser",
            type="us_a2p_messaging_profile_information",
            attributes={
                "company_type": business_type,
                "stock_exchange": stock_exchange,
                "stock_ticker": stock_ticker,
                "brand_name": business_name,
                "ein": ein,
                "ein_issuing_country": "US",
                "street": street,
                "city": city,
                "state": state,
                "postal_code": zip_code,
                "country": "US",
                "website": website or "",
                "vertical": vertical,
                "phone_number": contact_phone,
                "email": contact_email,
            },
        )
        logger.info(f"Created A2P EndUser: {end_user.sid}")

        # ── Step 3: TrustProduct (A2P Messaging Profile Bundle) ──
        trust_product = client.trusthub.v1.trust_products.create(
            friendly_name=f"A2P Profile: {business_name}",
            email=contact_email,
            policy_sid="RNb0d4771c2c98518d916a3d4cd70a8f8b",
        )
        logger.info(f"Created A2P TrustProduct: {trust_product.sid}")

        # ── Step 4: Attach EndUser → TrustProduct ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=end_user.sid,
            )

        # ── Step 5: Attach CustomerProfile → TrustProduct ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=profile_sid,
            )

        # ── Step 6: Submit TrustProduct for evaluation ──
        _trusthub_update_status(
            "TrustProducts", trust_product.sid, "pending-review",
            sub_account_sid, sub_account_auth_token,
        )
        logger.info(f"Submitted TrustProduct {trust_product.sid} for review")

        # ── Step 7: Register Brand ──
        # customer_profile_bundle_sid = Customer Profile SID (BU...)
        # a2p_profile_bundle_sid      = TrustProduct SID (BU...)  ← NOT the profile!
        brand = client.messaging.v1.brand_registrations.create(
            customer_profile_bundle_sid=profile_sid,
            a2p_profile_bundle_sid=trust_product.sid,
        )

        logger.info(f"Created A2P Brand: {brand.sid} status={brand.status}")
        return {
            "brand_sid": brand.sid,
            "status": brand.status,
            "profile_sid": profile_sid,
            "trust_product_sid": trust_product.sid,
            "end_user_sid": end_user.sid,
            "business_name": business_name,
        }
    except TwilioRestException as e:
        logger.error(f"A2P Brand registration failed: {e}")
        raise


def get_a2p_brand_status(brand_sid: str, sub_account_sid: str = "",
                          sub_account_auth_token: str = "") -> dict:
    """Check the vetting status of an A2P Brand.
    Always uses sub-account client when sub_account_sid is provided."""
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P brand status — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        brand = client.messaging.v1.brand_registrations(brand_sid).fetch()
        return {
            "brand_sid": brand.sid,
            "status": brand.status,
            "brand_score": getattr(brand, "brand_score", None),
            "brand_feedback": getattr(brand, "brand_feedback", None),
            "errors": getattr(brand, "errors", []),
        }
    except TwilioRestException as e:
        logger.error(f"Failed to fetch brand status: {e}")
        raise


def create_messaging_service(sub_account_sid: str,
                              friendly_name: str,
                              sub_account_auth_token: str = "") -> dict:
    """
    Create a Messaging Service on the sub-account.
    This is required to associate phone numbers with an A2P campaign.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        svc = client.messaging.v1.services.create(
            friendly_name=friendly_name,
            inbound_request_url="",  # We handle SMS via GHL, not Twilio inbound
            inbound_method="POST",
            use_inbound_webhook_on_number=True,
        )
        logger.info(f"Created Messaging Service: {svc.sid} on {sub_account_sid}")
        return {"messaging_service_sid": svc.sid}
    except TwilioRestException as e:
        logger.error(f"Failed to create Messaging Service: {e}")
        raise


def add_phone_to_messaging_service(sub_account_sid: str,
                                    messaging_service_sid: str,
                                    phone_number_sid: str,
                                    sub_account_auth_token: str = "") -> bool:
    """Associate a phone number with a Messaging Service."""
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        client.messaging.v1.services(messaging_service_sid).phone_numbers.create(
            phone_number_sid=phone_number_sid,
        )
        logger.info(f"Added {phone_number_sid} to MessagingService {messaging_service_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to add phone to messaging service: {e}")
        raise


def create_a2p_campaign(messaging_service_sid: str,
                         brand_registration_sid: str,
                         description: str,
                         use_case: str = "LOW_VOLUME",
                         sample_messages: list = None,
                         has_embedded_links: bool = False,
                         has_embedded_phone: bool = False,
                         message_flow: str = "",
                         sub_account_sid: str = "",
                         sub_account_auth_token: str = "") -> dict:
    """
    Create an A2P 10DLC Campaign and associate it with a Messaging Service.

    Twilio endpoint: POST /v1/Services/{MessagingServiceSid}/UsAppToPerson
    This is the final step — once the campaign is approved, the number can
    send 10DLC-compliant SMS.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P campaign creation — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    if not sample_messages:
        sample_messages = [
            "Hi {name}, this is {agent} from {agency}. I wanted to follow up on your insurance inquiry.",
            "Thanks for your interest! I have a few options that might work for you. When is a good time to chat?",
        ]
    if not message_flow:
        message_flow = (
            "Consumers opt-in by filling out an online form requesting "
            "an insurance quote. An agent replies via SMS to schedule "
            "a consultation."
        )

    try:
        campaign = client.messaging.v1.services(
            messaging_service_sid
        ).us_app_to_person.create(
            brand_registration_sid=brand_registration_sid,
            description=description[:4096],
            message_flow=message_flow[:2048],
            message_samples=sample_messages[:5],
            us_app_to_person_usecase=use_case,
            has_embedded_links=has_embedded_links,
            has_embedded_phone=has_embedded_phone,
            opt_in_message="Reply YES to confirm you'd like to receive messages from us.",
            opt_out_message="Reply STOP to unsubscribe. You will no longer receive messages from us.",
            help_message="Reply HELP for support. Msg & data rates may apply.",
            opt_in_keywords=["START", "YES"],
            opt_out_keywords=["STOP", "UNSUBSCRIBE", "CANCEL"],
            help_keywords=["HELP", "INFO"],
        )
        logger.info(
            f"Created A2P Campaign: {campaign.sid} "
            f"status={campaign.campaign_status} "
            f"on MessagingService {messaging_service_sid}"
        )
        return {
            "campaign_sid": campaign.sid,
            "campaign_status": campaign.campaign_status,
            "messaging_service_sid": messaging_service_sid,
            "use_case": use_case,
        }
    except TwilioRestException as e:
        logger.error(f"A2P Campaign creation failed: {e}")
        raise


def get_a2p_campaign_status(messaging_service_sid: str,
                             campaign_sid: str,
                             sub_account_sid: str = "",
                             sub_account_auth_token: str = "") -> dict:
    """Check the approval status of an A2P Campaign."""
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P campaign status — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        campaign = client.messaging.v1.services(
            messaging_service_sid
        ).us_app_to_person(campaign_sid).fetch()
        return {
            "campaign_sid": campaign.sid,
            "campaign_status": campaign.campaign_status,
            "description": getattr(campaign, "description", ""),
            "use_case": getattr(campaign, "us_app_to_person_usecase", ""),
            "errors": getattr(campaign, "errors", []),
        }
    except TwilioRestException as e:
        logger.error(f"Failed to fetch campaign status: {e}")
        raise


def list_messaging_services(sub_account_sid: str,
                             sub_account_auth_token: str = "") -> list:
    """List all Messaging Services on a sub-account."""
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        services = client.messaging.v1.services.list(limit=50)
        return [
            {
                "sid": s.sid,
                "friendly_name": s.friendly_name,
                "date_created": s.date_created.isoformat() if s.date_created else "",
            }
            for s in services
        ]
    except TwilioRestException as e:
        logger.error(f"Failed to list messaging services: {e}")
        return []


def list_messaging_service_phone_numbers(messaging_service_sid: str,
                                          sub_account_sid: str = "",
                                          sub_account_auth_token: str = "") -> list:
    """
    List all phone numbers associated with a Messaging Service.
    Returns list of {sid, phone_number} dicts — these are the numbers
    actually registered for A2P via this messaging service.
    The `sid` is the PN... IncomingPhoneNumber SID.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for listing messaging service phone numbers")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        numbers = client.messaging.v1.services(
            messaging_service_sid
        ).phone_numbers.list(limit=400)
        return [
            {
                "sid": n.sid,
                "phone_number": getattr(n, "phone_number", ""),
            }
            for n in numbers
        ]
    except TwilioRestException as e:
        logger.error(f"Failed to list phone numbers on MS {messaging_service_sid}: {e}")
        return []


# ──────────────────────────────────────────────────────────────
# A2P DISCOVERY — Sync existing registrations from Twilio
# ──────────────────────────────────────────────────────────────

def discover_a2p_brands(sub_account_sid: str = "",
                         sub_account_auth_token: str = "") -> list:
    """
    Discover all existing A2P Brand Registrations on a sub-account.
    Returns list of {brand_sid, status, brand_score, ...} dicts.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P brand discovery — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        brands = client.messaging.v1.brand_registrations.list(limit=100)
        results = []
        for b in brands:
            results.append({
                "brand_sid": b.sid,
                "status": b.status,
                "brand_score": getattr(b, "brand_score", None),
                "brand_feedback": getattr(b, "brand_feedback", None),
                "a2p_profile_bundle_sid": getattr(b, "a2p_profile_bundle_sid", ""),
                "customer_profile_bundle_sid": getattr(b, "customer_profile_bundle_sid", ""),
                "date_created": b.date_created.isoformat() if b.date_created else "",
                "date_updated": b.date_updated.isoformat() if b.date_updated else "",
            })
        logger.info(f"Discovered {len(results)} A2P brands on sub-account {sub_account_sid}")
        return results
    except TwilioRestException as e:
        logger.error(f"Failed to discover A2P brands: {e}")
        return []


def discover_a2p_campaigns(messaging_service_sid: str,
                            sub_account_sid: str = "",
                            sub_account_auth_token: str = "") -> list:
    """
    Discover all existing A2P campaigns on a Messaging Service.
    Returns list of {campaign_sid, campaign_status, description, use_case} dicts.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P campaign discovery — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        campaigns = client.messaging.v1.services(
            messaging_service_sid
        ).us_app_to_person.list(limit=50)
        results = []
        for c in campaigns:
            results.append({
                "campaign_sid": c.sid,
                "campaign_status": c.campaign_status,
                "description": getattr(c, "description", ""),
                "use_case": getattr(c, "us_app_to_person_usecase", ""),
                "brand_registration_sid": getattr(c, "brand_registration_sid", ""),
                "date_created": c.date_created.isoformat() if c.date_created else "",
            })
        logger.info(f"Discovered {len(results)} A2P campaigns on MS {messaging_service_sid}")
        return results
    except TwilioRestException as e:
        logger.error(f"Failed to discover campaigns on {messaging_service_sid}: {e}")
        return []


def discover_trust_hub_profiles(sub_account_sid: str = "",
                                 sub_account_auth_token: str = "") -> list:
    """
    Discover all Trust Hub Customer Profiles on a sub-account.
    Returns list of {profile_sid, status, friendly_name} dicts.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for Trust Hub profile discovery — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        profiles = client.trusthub.v1.customer_profiles.list(limit=100)
        results = []
        for p in profiles:
            results.append({
                "profile_sid": p.sid,
                "status": p.status,
                "friendly_name": getattr(p, "friendly_name", ""),
                "date_created": p.date_created.isoformat() if p.date_created else "",
            })
        logger.info(f"Discovered {len(results)} Trust Hub profiles")
        return results
    except TwilioRestException as e:
        logger.error(f"Failed to discover Trust Hub profiles: {e}")
        return []


def discover_full_a2p_status(sub_account_sid: str,
                              sub_account_auth_token: str = "") -> dict:
    """
    Comprehensive A2P discovery: finds brands, messaging services, and campaigns.
    Always queries the sub-account directly — never the master account.
    Returns dict ready to merge into voice_config['a2p'].
    """
    result = {
        "brands": [],
        "messaging_services": [],
        "campaigns": [],
        "best_brand": None,
        "best_campaign": None,
    }

    # 1. Discover brands on the sub-account
    brands = discover_a2p_brands(sub_account_sid, sub_account_auth_token)
    result["brands"] = brands

    # Find the best brand (prefer APPROVED, then most recent)
    approved_brands = [b for b in brands if b.get("status", "").upper() == "APPROVED"]
    if approved_brands:
        result["best_brand"] = approved_brands[0]
    elif brands:
        result["best_brand"] = brands[0]

    # 2. Discover messaging services on the sub-account
    ms_list = list_messaging_services(sub_account_sid, sub_account_auth_token) if sub_account_sid else []
    result["messaging_services"] = ms_list

    # 3. Discover campaigns on each messaging service
    for ms in ms_list:
        campaigns = discover_a2p_campaigns(ms["sid"], sub_account_sid, sub_account_auth_token)
        for c in campaigns:
            c["messaging_service_sid"] = ms["sid"]
        result["campaigns"].extend(campaigns)

    # Find the best campaign (prefer VERIFIED/APPROVED)
    good_statuses = ("VERIFIED", "APPROVED", "IN_PROGRESS")
    good_campaigns = [c for c in result["campaigns"]
                      if c.get("campaign_status", "").upper() in good_statuses]
    if good_campaigns:
        result["best_campaign"] = good_campaigns[0]
    elif result["campaigns"]:
        result["best_campaign"] = result["campaigns"][0]

    return result


# ──────────────────────────────────────────────────────────────
# VOICE INTEGRITY (NUMBER INTEGRITY)
# ──────────────────────────────────────────────────────────────
#
# Twilio Voice Integrity registers phone numbers with carrier spam
# analytics engines (AT&T/Hiya, T-Mobile/CallHub, Verizon) to
# remediate spam labels and improve call answer rates.
#
# Flow (ISV/sub-account):
#   1. Create or reuse an approved Customer Profile (Business Profile)
#   2. Create a Voice Integrity Trust Product (policy_sid specific to VI)
#   3. Link Customer Profile → Trust Product (EntityAssignment)
#   4. Assign phone numbers → Trust Product (ChannelEndpointAssignment)
#   5. Submit Trust Product for review (status → pending-review)
#   6. Twilio reviews + registers with carriers (24–48 hours)
#
# State stored in voice_config["number_integrity"] JSONB.

# Voice Integrity Trust Product policy SID — different from A2P/SHAKEN.
# This is Twilio's static Voice Integrity policy (same across all accounts).
# Used ONLY for TrustProducts, NOT for CustomerProfiles.
VOICE_INTEGRITY_POLICY_SID = "RN5b3660f9598883b1df4e77f77acefba0"

# Secondary Customer Profile policy SID — used when creating a profile
# on a sub-account that links back to the master's Primary Business Profile.
# Same across all accounts. See:
# https://www.twilio.com/docs/trust-hub/trusthub-rest-api/api-create-secondary-customer-profile
SECONDARY_CUSTOMER_PROFILE_POLICY_SID = "RNdfbf3fae0e1107f8aded0e7cead80bf5"


def check_secondary_profile_status(
    sub_account_sid: str,
    sub_account_auth_token: str,
    profile_sid: str = "",
) -> dict:
    """
    Check the approval status of the Secondary Customer Profile on a sub-account.

    ISV Trust Hub requires an approved Secondary Customer Profile before
    Trust Products (Voice Integrity, CNAM, A2P) can be submitted. This function
    verifies the profile exists and is approved, preventing wasted API calls
    and stuck Trust Products.

    Returns:
        dict with keys:
            - approved (bool): True if profile is twilio-approved
            - status (str): Current profile status
            - profile_sid (str): The checked profile SID
            - message (str): Human-readable status message
    """
    if is_master_account(sub_account_sid):
        # Master account manages its own profiles via Twilio Console — always approved
        return {
            "approved": True,
            "status": "twilio-approved",
            "profile_sid": "",
            "message": "Master account — profiles managed via Twilio Console.",
        }

    if not profile_sid:
        return {
            "approved": False,
            "status": "missing",
            "profile_sid": "",
            "message": "No Secondary Customer Profile found. Register in Spam Protection first.",
        }

    try:
        client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
        profile = client.trusthub.v1.customer_profiles(profile_sid).fetch()
        status = getattr(profile, "status", "unknown")
        logger.info(f"[TrustHub] Secondary Profile {profile_sid} status: {status}")

        if status == "twilio-approved":
            return {
                "approved": True,
                "status": status,
                "profile_sid": profile_sid,
                "message": "Secondary Customer Profile approved.",
            }
        elif status in ("in-review", "pending-review"):
            return {
                "approved": False,
                "status": status,
                "profile_sid": profile_sid,
                "message": "Your business profile is still under review by Twilio. "
                           "This typically takes 1-3 business days. You can proceed "
                           "with other registrations once it's approved.",
            }
        elif status == "twilio-rejected":
            # Get rejection reasons if available
            reasons = []
            try:
                evals = client.trusthub.v1.customer_profiles(profile_sid) \
                    .customer_profiles_evaluations.list(limit=1)
                if evals:
                    results = getattr(evals[0], "results", []) or []
                    reasons = [
                        r.get("friendly_name", r.get("requirement_key", "unknown"))
                        for r in results
                        if isinstance(r, dict) and r.get("status") == "noncompliant"
                    ]
            except Exception:
                pass
            reason_str = f" Reasons: {', '.join(reasons)}" if reasons else ""
            return {
                "approved": False,
                "status": status,
                "profile_sid": profile_sid,
                "message": f"Your business profile was rejected by Twilio.{reason_str} "
                           "Please update your Spam Protection registration and resubmit.",
            }
        else:
            return {
                "approved": False,
                "status": status,
                "profile_sid": profile_sid,
                "message": f"Your business profile is in '{status}' state. "
                           "Please complete Spam Protection registration first.",
            }
    except TwilioRestException as e:
        logger.warning(f"[TrustHub] Could not fetch profile {profile_sid}: {e}")
        return {
            "approved": False,
            "status": "error",
            "profile_sid": profile_sid,
            "message": f"Could not verify profile status: {str(e)}",
        }


def is_master_account(sub_account_sid: str) -> bool:
    """
    Detect if a subscriber is using the master Twilio account directly
    (i.e., the platform owner's own business) vs a sub-account (ISV customer).

    Master account: uses Primary Business Profile for Voice Integrity (direct customer flow).
    Sub-account:    uses Secondary Customer Profile linked to Primary (ISV flow).
    """
    return sub_account_sid == TWILIO_ACCOUNT_SID


# Carrier analytics engines that Voice Integrity registers with
VOICE_INTEGRITY_CARRIERS = [
    {"key": "att", "name": "AT&T / Hiya", "icon": "fa-signal",
     "description": "Registers with Hiya analytics to clear spam labels on AT&T devices."},
    {"key": "tmobile", "name": "T-Mobile / CallHub", "icon": "fa-tower-cell",
     "description": "Registers with T-Mobile CallHub for verified caller status."},
    {"key": "verizon", "name": "Verizon", "icon": "fa-shield-halved",
     "description": "Registers with Verizon spam analytics to prevent spam flagging."},
]


def _find_or_create_secondary_profile(
    client,
    sub_account_sid: str,
    business_name: str,
    contact_email: str,
    existing_profile_sid: str = "",
    primary_profile_sid: str = "",
    sub_account_auth_token: str = "",
) -> str:
    """
    Find or create a Secondary Customer Profile on the sub-account.

    ISV/Subaccounts flow per Twilio docs:
      https://www.twilio.com/docs/trust-hub/trusthub-rest-api/api-create-secondary-customer-profile

    1. If existing_profile_sid provided and valid, reuse it.
    2. Otherwise discover approved profiles on the sub-account.
    3. If none found, create a new Secondary Profile with the correct policy
       and link it to the Primary Business Profile on the master account.

    Returns the profile SID (BU...).
    """
    # ── Try reusing an existing profile ──
    # IMPORTANT: Never reuse the Primary Business Profile as a Secondary Profile.
    # The Primary is on the master account and has a different policy. Twilio
    # evaluations will reject Trust Products linked to the wrong profile type.
    if existing_profile_sid and existing_profile_sid != primary_profile_sid:
        try:
            profile = client.trusthub.v1.customer_profiles(existing_profile_sid).fetch()
            status = getattr(profile, "status", "")
            policy = getattr(profile, "policy_sid", "")
            logger.info(f"[VoiceIntegrity] Existing profile {existing_profile_sid} status: {status}, policy: {policy}")
            if status in ("twilio-approved", "in-review", "pending-review"):
                # Verify it's actually a Secondary profile (correct policy), not a Primary
                if policy and policy != SECONDARY_CUSTOMER_PROFILE_POLICY_SID:
                    logger.warning(
                        f"[VoiceIntegrity] Profile {existing_profile_sid} has policy {policy}, "
                        f"expected Secondary policy {SECONDARY_CUSTOMER_PROFILE_POLICY_SID}. Skipping."
                    )
                else:
                    return existing_profile_sid
            else:
                logger.warning(
                    f"[VoiceIntegrity] Existing profile {existing_profile_sid} status '{status}' "
                    "is not approved; will search for another."
                )
        except TwilioRestException as e:
            logger.warning(f"[VoiceIntegrity] Could not fetch profile {existing_profile_sid}: {e}")
    elif existing_profile_sid == primary_profile_sid:
        logger.info(f"[VoiceIntegrity] Skipping existing_profile_sid — it's the Primary Business Profile")

    # ── Discover approved Secondary profiles on the sub-account ──
    # Skip the Primary Business Profile and any profiles with the wrong policy.
    try:
        profiles = client.trusthub.v1.customer_profiles.list(
            status="twilio-approved", limit=20)
        for p in profiles:
            # Skip the Primary Business Profile
            if p.sid == primary_profile_sid:
                continue
            # Prefer profiles with the Secondary Customer Profile policy
            policy = getattr(p, "policy_sid", "")
            if policy and policy != SECONDARY_CUSTOMER_PROFILE_POLICY_SID:
                logger.info(f"[VoiceIntegrity] Skipping profile {p.sid} (policy={policy}, not Secondary)")
                continue
            logger.info(
                f"[VoiceIntegrity] Discovered approved Secondary profile: {p.sid} "
                f"({getattr(p, 'friendly_name', '')})"
            )
            return p.sid
    except Exception as e:
        logger.warning(f"[VoiceIntegrity] Profile discovery failed: {e}")

    # ── Create a new Secondary Customer Profile ──
    # Uses the Secondary Customer Profile policy, NOT the Voice Integrity policy.
    profile = client.trusthub.v1.customer_profiles.create(
        friendly_name=f"Secondary Profile: {business_name}",
        email=contact_email,
        policy_sid=SECONDARY_CUSTOMER_PROFILE_POLICY_SID,
    )
    profile_sid = profile.sid
    logger.info(f"[VoiceIntegrity] Created Secondary Customer Profile: {profile_sid}")

    # ── Link to Primary Business Profile on master account ──
    if primary_profile_sid:
        try:
            client.trusthub.v1.customer_profiles(profile_sid) \
                .customer_profiles_entity_assignments.create(
                    object_sid=primary_profile_sid,
                )
            logger.info(
                f"[VoiceIntegrity] Linked Secondary {profile_sid} → "
                f"Primary {primary_profile_sid}"
            )
        except TwilioRestException as e:
            # 20409 = already assigned
            if e.code == 20409:
                logger.info(f"[VoiceIntegrity] Secondary already linked to Primary (20409)")
            else:
                logger.warning(f"[VoiceIntegrity] Could not link to Primary: {e}")

    # ── Submit the Secondary Profile for review ──
    # Per Twilio ISV docs Step 2: the Secondary Business Profile must be
    # submitted for review before the Trust Product can transition from draft.
    # https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/
    # voice-integrity-onboarding/voice-integrity-trust-hub-api-isvs-subaccounts
    try:
        # Run evaluation first
        try:
            eval_result = client.trusthub.v1.customer_profiles(profile_sid) \
                .customer_profiles_evaluations.create(
                    policy_sid=SECONDARY_CUSTOMER_PROFILE_POLICY_SID,
                )
            eval_status = getattr(eval_result, "status", "unknown")
            logger.info(f"[VoiceIntegrity] Secondary Profile evaluation: {eval_status}")
            if eval_status == "noncompliant":
                eval_results = getattr(eval_result, "results", None) or []
                noncompliant = [
                    r.get("friendly_name", r.get("requirement_key", "unknown"))
                    for r in eval_results
                    if isinstance(r, dict) and r.get("status") == "noncompliant"
                ]
                logger.warning(f"[VoiceIntegrity] Secondary Profile evaluation noncompliant: {noncompliant}")
                # Proceed anyway — Twilio may still allow review submission
        except Exception as eval_err:
            logger.warning(f"[VoiceIntegrity] Secondary Profile evaluation failed: {eval_err}")

        profile_resp = _trusthub_update_status(
            "CustomerProfiles", profile_sid, "pending-review",
            sub_account_sid, sub_account_auth_token,
        )
        logger.info(
            f"[VoiceIntegrity] Submitted Secondary Profile {profile_sid} for review → "
            f"{profile_resp.get('status', 'unknown')}"
        )
    except TwilioRestException as e:
        # Profile submission failed — log but don't block entirely,
        # the Trust Product submission will surface the real error.
        logger.warning(f"[VoiceIntegrity] Secondary Profile review submission failed: {e}")

    return profile_sid


def _find_primary_profile_sid() -> str:
    """
    Find the Primary Business Profile SID on the master Twilio account.
    This is created once in the Twilio Console and reused for all sub-accounts.
    Returns the SID (BU...) or empty string if not found.

    Checks TWILIO_PRIMARY_PROFILE_SID env var first for instant lookup,
    falls back to API discovery if not set.
    """
    # Fast path: env var set by operator
    env_sid = os.getenv("TWILIO_PRIMARY_PROFILE_SID", "").strip()
    if env_sid and env_sid.startswith("BU"):
        return env_sid

    # Fallback: discover via API
    try:
        master = get_master_client()
        profiles = master.trusthub.v1.customer_profiles.list(
            status="twilio-approved", limit=50)
        for p in profiles:
            fname = getattr(p, "friendly_name", "")
            if "primary" in fname.lower() or "business" in fname.lower():
                logger.info(f"[VoiceIntegrity] Found Primary Profile: {p.sid} ({fname})")
                return p.sid
        if profiles:
            logger.info(f"[VoiceIntegrity] Using first master profile as Primary: {profiles[0].sid}")
            return profiles[0].sid
    except Exception as e:
        logger.warning(f"[VoiceIntegrity] Could not find Primary Profile on master: {e}")
    return ""


def create_voice_integrity_trust_product(
    sub_account_sid: str,
    business_name: str,
    contact_email: str,
    sub_account_auth_token: str = "",
    existing_profile_sid: str = "",
    business_employee_count: str = "1",
    average_call_volume: str = "500",
    use_case: str = "Lead Management",
) -> dict:
    """
    Create a Voice Integrity Trust Product (ISV sub-account flow).

    Per Twilio ISV docs:
      https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/
      voice-integrity-onboarding/voice-integrity-trust-hub-api-isvs-subaccounts

    ISV flow:
      1. Find or create Secondary Customer Profile on sub-account
      2. Create EndUser (voice_integrity_information) with use case + call volume
      3. Create Voice Integrity Trust Product (policy_sid=RN5b3660f9598883b1df4e77f77acefba0)
      4. Link Profile → Trust Product (EntityAssignment)
      5. Link EndUser → Trust Product (EntityAssignment)

    Master account manages its own Voice Integrity via the Twilio Console.
    Returns dict with trust_product_sid, profile_sid, end_user_sid, status.
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    try:
        # ── Step 1: Secondary Customer Profile (ISV flow) ──
        # Per Twilio ISV docs: find or create a Secondary Customer Profile
        # on the sub-account, linked to the master's Primary Business Profile.
        # https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/
        # voice-integrity-onboarding/voice-integrity-trust-hub-api-isvs-subaccounts
        primary_sid = _find_primary_profile_sid()
        profile_sid = _find_or_create_secondary_profile(
            client=client,
            sub_account_sid=sub_account_sid,
            business_name=business_name,
            contact_email=contact_email,
            existing_profile_sid=existing_profile_sid,
            primary_profile_sid=primary_sid,
            sub_account_auth_token=sub_account_auth_token,
        )

        # ── Step 2: EndUser with voice_integrity_information ──
        end_user = client.trusthub.v1.end_users.create(
            friendly_name=f"Voice Integrity EndUser: {business_name}",
            type="voice_integrity_information",
            attributes={
                "use_case": use_case,
                "business_employee_count": str(int(business_employee_count)),
                "average_business_day_call_volume": str(int(average_call_volume)),
                "notes": f"Insurance agency outbound dialer for {business_name}",
            },
        )
        logger.info(f"[VoiceIntegrity] Created EndUser: {end_user.sid}")

        # ── Step 3: Voice Integrity Trust Product ──
        trust_product = client.trusthub.v1.trust_products.create(
            friendly_name=f"Voice Integrity: {business_name}",
            email=contact_email,
            policy_sid=VOICE_INTEGRITY_POLICY_SID,
        )
        logger.info(f"[VoiceIntegrity] Created Trust Product: {trust_product.sid}")

        # ── Step 4: Link Profile → Trust Product ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=profile_sid,
            )
        logger.info(f"[VoiceIntegrity] Linked profile {profile_sid} → {trust_product.sid}")

        # ── Step 5: Link EndUser → Trust Product ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=end_user.sid,
            )
        logger.info(f"[VoiceIntegrity] Linked EndUser {end_user.sid} → {trust_product.sid}")

        return {
            "trust_product_sid": trust_product.sid,
            "profile_sid": profile_sid,
            "end_user_sid": end_user.sid,
            "status": "draft",
            "business_name": business_name,
        }

    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Trust Product creation failed: {e}")
        raise


def unassign_numbers_from_trust_product(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sids: list,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Remove phone number ChannelEndpointAssignments from a Trust Product.

    Twilio only allows a phone number to be assigned to one Trust Product
    at a time. When re-registering after rejection, the old assignments
    must be removed before numbers can be assigned to a new Trust Product.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    removed = 0
    failed = []

    # Fetch all current assignments on the old trust product
    try:
        assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_channel_endpoint_assignment.list(limit=200)
    except TwilioRestException as e:
        logger.warning(f"[VoiceIntegrity] Could not list assignments on {trust_product_sid}: {e}")
        return {"removed": 0, "failed": [], "total": len(phone_number_sids)}

    # Build lookup: channel_endpoint_sid → assignment SID
    assignment_map = {a.channel_endpoint_sid: a.sid for a in assignments}

    target_sids = set(phone_number_sids) if phone_number_sids else set(assignment_map.keys())

    for pn_sid in target_sids:
        assign_sid = assignment_map.get(pn_sid)
        if not assign_sid:
            continue  # not assigned to this trust product
        try:
            client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment(assign_sid).delete()
            removed += 1
            logger.info(f"[VoiceIntegrity] Unassigned {pn_sid} from {trust_product_sid}")
        except TwilioRestException as e:
            failed.append({"sid": pn_sid, "error": str(e)})
            logger.warning(f"[VoiceIntegrity] Failed to unassign {pn_sid}: {e}")

    return {"removed": removed, "failed": failed, "total": len(target_sids)}


def assign_numbers_to_voice_integrity(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sids: list,
    sub_account_auth_token: str = "",
    profile_sid: str = "",
) -> dict:
    """
    Assign phone numbers to a Voice Integrity Trust Product.

    If profile_sid is provided, numbers are first assigned to the Customer Profile
    (required by Twilio for proper carrier registration), then to the Trust Product.
    Returns dict with assigned count and any failures.
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    assigned = 0
    failed = []

    for pn_sid in phone_number_sids:
        try:
            # Assign to Customer Profile first (if provided)
            if profile_sid:
                try:
                    client.trusthub.v1.customer_profiles(profile_sid) \
                        .customer_profiles_channel_endpoint_assignment.create(
                            channel_endpoint_type="phone-number",
                            channel_endpoint_sid=pn_sid,
                        )
                    logger.info(f"[VoiceIntegrity] Assigned {pn_sid} to profile {profile_sid}")
                except TwilioRestException as e:
                    # 20409 or HTTP 409 (code 70003) = already assigned to this profile — fine
                    if e.code == 20409 or e.status == 409:
                        logger.info(f"[VoiceIntegrity] {pn_sid} already on profile (code={e.code}, status={e.status})")
                    else:
                        raise

            # Then assign to Trust Product
            client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment.create(
                    channel_endpoint_type="phone-number",
                    channel_endpoint_sid=pn_sid,
                )
            assigned += 1
            logger.info(f"[VoiceIntegrity] Assigned {pn_sid} to {trust_product_sid}")
        except TwilioRestException as e:
            # 20409 or HTTP 409 (code 70003) = already assigned to a Trust Product.
            # If it's assigned to THIS TP, treat as success.
            # If it's assigned to ANOTHER TP (e.g. old rejected one), unassign and retry.
            if e.code == 20409 or e.status == 409:
                # Extract the conflicting BU SID from the error message
                # Twilio may say "already assigned to BU..." or "already mapped to a trust_product_sid: BU..."
                import re as _re
                err_str = str(e)
                conflict_match = _re.search(r'(?:already (?:assigned|mapped) to (?:a trust_product_sid: )?)(BU[a-f0-9A-F]+)', err_str)
                if not conflict_match:
                    # Fallback: find any BU SID in the error message
                    conflict_match = _re.search(r'(BU[a-f0-9]{32})', err_str)
                conflict_sid = conflict_match.group(1) if conflict_match else None

                if conflict_sid and conflict_sid == trust_product_sid:
                    # Already assigned to our target — treat as success
                    assigned += 1
                    logger.info(f"[VoiceIntegrity] {pn_sid} already assigned to target TP (ok)")
                elif conflict_sid and conflict_sid != trust_product_sid:
                    # Assigned to a different Trust Product — unassign from it and retry
                    logger.info(f"[VoiceIntegrity] {pn_sid} stuck on {conflict_sid}, unassigning and retrying")
                    try:
                        unassign_numbers_from_trust_product(
                            sub_account_sid, conflict_sid, [pn_sid], sub_account_auth_token)
                        # Retry assignment to our target TP
                        client.trusthub.v1.trust_products(trust_product_sid) \
                            .trust_products_channel_endpoint_assignment.create(
                                channel_endpoint_type="phone-number",
                                channel_endpoint_sid=pn_sid,
                            )
                        assigned += 1
                        logger.info(f"[VoiceIntegrity] Retry succeeded: {pn_sid} → {trust_product_sid}")
                    except TwilioRestException as retry_err:
                        failed.append({"sid": pn_sid, "error": str(retry_err)})
                        logger.warning(f"[VoiceIntegrity] Retry failed for {pn_sid}: {retry_err}")
                else:
                    # Can't determine conflict — treat 20409 as success, others as failure
                    if e.code == 20409:
                        assigned += 1
                        logger.info(f"[VoiceIntegrity] {pn_sid} already assigned (20409)")
                    else:
                        failed.append({"sid": pn_sid, "error": str(e)})
                        logger.warning(f"[VoiceIntegrity] Failed to assign {pn_sid}: {e}")
            else:
                failed.append({"sid": pn_sid, "error": str(e)})
                logger.warning(f"[VoiceIntegrity] Failed to assign {pn_sid}: {e}")

    return {"assigned": assigned, "failed": failed, "total": len(phone_number_sids)}


def submit_voice_integrity_for_review(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Submit the Voice Integrity Trust Product for Twilio review.
    Runs an evaluation first to validate all required entities are attached,
    then sets status to pending-review.
    After approval, numbers are registered with carrier analytics (24–48h).
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        # ── Check linked Customer Profile status first ──
        # Per Twilio ISV docs, the Secondary Customer Profile must be approved
        # (or at least submitted) before the Trust Product can transition.
        try:
            assignments = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_entity_assignments.list(limit=20)
            for a in assignments:
                obj_sid = getattr(a, "object_sid", "")
                if obj_sid.startswith("BU"):
                    # This is the linked Customer Profile — check its status
                    try:
                        profile = client.trusthub.v1.customer_profiles(obj_sid).fetch()
                        profile_status = getattr(profile, "status", "unknown")
                        logger.info(
                            f"[VoiceIntegrity] Linked profile {obj_sid} status: {profile_status}"
                        )
                        if profile_status == "draft":
                            # Profile was never submitted — submit it now
                            logger.warning(
                                f"[VoiceIntegrity] Profile {obj_sid} still in draft, "
                                "submitting for review first"
                            )
                            p_auth_sid = sub_account_sid or TWILIO_ACCOUNT_SID
                            p_auth_token = sub_account_auth_token or TWILIO_AUTH_TOKEN
                            profile_resp = _trusthub_update_status(
                                "CustomerProfiles", obj_sid, "pending-review",
                                p_auth_sid, p_auth_token,
                            )
                            logger.info(
                                f"[VoiceIntegrity] Profile {obj_sid} submitted → "
                                f"{profile_resp.get('status', 'unknown')}"
                            )
                    except TwilioRestException as profile_err:
                        logger.warning(
                            f"[VoiceIntegrity] Could not check/submit profile {obj_sid}: "
                            f"{profile_err}"
                        )
        except Exception as assign_err:
            logger.warning(f"[VoiceIntegrity] Could not check profile status: {assign_err}")

        # ── Run evaluation to validate completeness before submitting ──
        eval_compliant = False
        try:
            evaluation = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_evaluations.create(policy_sid=VOICE_INTEGRITY_POLICY_SID)
            eval_status = getattr(evaluation, "status", "unknown")
            eval_results = getattr(evaluation, "results", None) or []
            logger.info(
                f"[VoiceIntegrity] Evaluation for {trust_product_sid}: "
                f"status={eval_status}, results={eval_results}"
            )
            if eval_status == "compliant":
                eval_compliant = True
            elif eval_status == "noncompliant":
                # Surface evaluation failures instead of proceeding to a guaranteed rejection
                error_details = []
                for r in eval_results:
                    if isinstance(r, dict) and r.get("status") == "noncompliant":
                        error_details.append(r.get("friendly_name", r.get("requirement_key", "unknown")))
                detail_msg = ", ".join(error_details) if error_details else "evaluation returned noncompliant"
                raise TwilioRestException(
                    status=400, uri="", msg=f"Voice Integrity evaluation failed: {detail_msg}")
            else:
                logger.warning(
                    f"[VoiceIntegrity] Unexpected evaluation status '{eval_status}' — "
                    "attempting submission anyway"
                )
        except TwilioRestException:
            raise  # re-raise evaluation noncompliant errors
        except Exception as eval_err:
            # Non-Twilio errors during evaluation — log but allow submission attempt
            logger.warning(f"[VoiceIntegrity] Evaluation check failed (proceeding): {eval_err}")

        # ── Submit for review ──
        # Use direct HTTP POST instead of SDK .update() — the twilio-python SDK
        # has casing issues with TrustHub status enums that silently drop the
        # Status parameter, leaving the Trust Product in 'draft'.
        auth_sid = sub_account_sid or TWILIO_ACCOUNT_SID
        auth_token = sub_account_auth_token or TWILIO_AUTH_TOKEN
        resp_data = _trusthub_update_status(
            "TrustProducts", trust_product_sid, "pending-review",
            auth_sid, auth_token,
        )
        tp_status = resp_data.get("status", "unknown")
        tp_sid = resp_data.get("sid", trust_product_sid)
        logger.info(f"[VoiceIntegrity] Submitted {trust_product_sid} for review → {tp_status}")

        # ── Verify the status actually changed ──
        if tp_status == "draft":
            logger.error(
                f"[VoiceIntegrity] Trust Product {trust_product_sid} STILL in draft after "
                f"POST Status=pending-review. Evaluation was: "
                f"{'compliant' if eval_compliant else 'not confirmed compliant'}. "
                f"Full response: {resp_data}"
            )
            raise TwilioRestException(
                status=400, uri="",
                msg="Voice Integrity submission failed: Trust Product remained in 'draft' status "
                    "after API submission. Please check the Twilio Console for details or "
                    "contact support."
            )

        return {"trust_product_sid": tp_sid, "status": tp_status}
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Submit for review failed: {e}")
        raise


def get_voice_integrity_status(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Check the current status of a Voice Integrity Trust Product.
    Statuses: draft, pending-review, in-review, twilio-approved, twilio-rejected.
    When rejected, attempts to fetch the evaluation failure reasons.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        tp = client.trusthub.v1.trust_products(trust_product_sid).fetch()
        # List assigned numbers
        assigned_numbers = []
        try:
            assignments = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment.list(limit=100)
            assigned_numbers = [a.channel_endpoint_sid for a in assignments]
        except Exception as e:
            logger.warning(f"[VoiceIntegrity] Could not list assigned numbers: {e}")

        result = {
            "trust_product_sid": tp.sid,
            "status": tp.status,
            "friendly_name": getattr(tp, "friendly_name", ""),
            "date_created": tp.date_created.isoformat() if tp.date_created else "",
            "date_updated": tp.date_updated.isoformat() if tp.date_updated else "",
            "assigned_numbers": assigned_numbers,
            "assigned_count": len(assigned_numbers),
        }

        # If rejected, try to fetch evaluation failure reasons
        if tp.status == "twilio-rejected":
            try:
                evals = client.trusthub.v1.trust_products(trust_product_sid) \
                    .trust_products_evaluations.list(limit=5)
                if evals:
                    latest = evals[0]
                    eval_results = getattr(latest, "results", None) or []
                    failure_reasons = []
                    for r in eval_results:
                        if isinstance(r, dict) and r.get("status") in ("noncompliant", "failed"):
                            reason = r.get("friendly_name") or r.get("requirement_key") or "Unknown"
                            failure_reasons.append(reason)
                    result["failure_reasons"] = failure_reasons
                    result["evaluation_status"] = getattr(latest, "status", "")
            except Exception as eval_err:
                logger.warning(f"[VoiceIntegrity] Could not fetch rejection reasons: {eval_err}")

        return result
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Status check failed: {e}")
        raise


def update_voice_integrity_end_user(
    sub_account_sid: str,
    end_user_sid: str,
    sub_account_auth_token: str = "",
    business_employee_count: str = "",
    average_call_volume: str = "",
) -> dict:
    """
    Update the EndUser attributes on a Voice Integrity Trust Product.
    Useful for correcting data after a rejection.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        attrs = {}
        if business_employee_count:
            attrs["business_employee_count"] = str(business_employee_count)
        if average_call_volume:
            attrs["average_business_day_call_volume"] = str(average_call_volume)

        if not attrs:
            return {"status": "ok", "message": "No attributes to update"}

        # Fetch current attributes and merge
        end_user = client.trusthub.v1.end_users(end_user_sid).fetch()
        current_attrs = getattr(end_user, "attributes", {}) or {}
        current_attrs.update(attrs)

        updated = client.trusthub.v1.end_users(end_user_sid).update(
            attributes=current_attrs,
        )
        logger.info(f"[VoiceIntegrity] Updated EndUser {end_user_sid}: {attrs}")
        return {
            "status": "ok",
            "end_user_sid": updated.sid,
            "attributes": getattr(updated, "attributes", {}),
        }
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] EndUser update failed: {e}")
        raise


def resubmit_voice_integrity(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
    business_name: str = "",
    contact_email: str = "",
    existing_profile_sid: str = "",
    business_employee_count: str = "1",
    average_call_volume: str = "500",
) -> dict:
    """
    Resubmit a rejected Voice Integrity registration.

    Twilio does NOT allow resetting a twilio-rejected Trust Product back to draft.
    The correct approach is to create a brand new Trust Product, re-link all the
    existing entities (Secondary Profile, EndUser, numbers), and submit fresh.

    The old rejected Trust Product is abandoned (Twilio doesn't allow deletion).
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    # ── Step 1: Gather existing entities from the old Trust Product ──
    old_assigned_numbers = []
    old_entity_sids = []
    try:
        # Get assigned phone numbers
        assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_channel_endpoint_assignment.list(limit=100)
        old_assigned_numbers = [a.channel_endpoint_sid for a in assignments]
        logger.info(f"[VoiceIntegrity] Old product has {len(old_assigned_numbers)} numbers")

        # Get entity assignments (profile, end_user)
        entity_assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_entity_assignments.list(limit=100)
        old_entity_sids = [ea.object_sid for ea in entity_assignments]
        logger.info(f"[VoiceIntegrity] Old product has {len(old_entity_sids)} entity assignments")
    except Exception as e:
        logger.warning(f"[VoiceIntegrity] Could not read old product entities: {e}")

    # ── Step 2: Create a new Trust Product ──
    try:
        new_tp = client.trusthub.v1.trust_products.create(
            friendly_name=f"Voice Integrity: {business_name or sub_account_sid}",
            policy_sid=VOICE_INTEGRITY_POLICY_SID,
            email=contact_email or "support@insurancegrokbot.com",
        )
        new_tp_sid = new_tp.sid
        logger.info(f"[VoiceIntegrity] Created new Trust Product: {new_tp_sid} (replacing {trust_product_sid})")
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] New Trust Product creation failed: {e}")
        raise

    # ── Step 3: Re-link existing entities to the new Trust Product ──
    for entity_sid in old_entity_sids:
        try:
            client.trusthub.v1.trust_products(new_tp_sid) \
                .trust_products_entity_assignments.create(object_sid=entity_sid)
            logger.info(f"[VoiceIntegrity] Re-linked entity {entity_sid} → {new_tp_sid}")
        except TwilioRestException as e:
            if e.code == 20409:
                logger.info(f"[VoiceIntegrity] Entity {entity_sid} already linked (20409)")
            else:
                logger.warning(f"[VoiceIntegrity] Entity re-link failed for {entity_sid}: {e}")

    # ── Step 4: Re-assign phone numbers ──
    for pn_sid in old_assigned_numbers:
        try:
            client.trusthub.v1.trust_products(new_tp_sid) \
                .trust_products_channel_endpoint_assignment.create(
                    channel_endpoint_type="phone-number",
                    channel_endpoint_sid=pn_sid,
                )
            logger.info(f"[VoiceIntegrity] Re-assigned number {pn_sid} → {new_tp_sid}")
        except TwilioRestException as e:
            if e.code == 20409:
                logger.info(f"[VoiceIntegrity] Number {pn_sid} already assigned (20409)")
            else:
                logger.warning(f"[VoiceIntegrity] Number re-assign failed for {pn_sid}: {e}")

    # ── Step 5: Check linked profile status ──
    for entity_sid in old_entity_sids:
        if entity_sid.startswith("BU"):
            try:
                profile = client.trusthub.v1.customer_profiles(entity_sid).fetch()
                profile_status = getattr(profile, "status", "unknown")
                logger.info(f"[VoiceIntegrity] Linked profile {entity_sid} status: {profile_status}")
                if profile_status == "draft":
                    logger.warning(f"[VoiceIntegrity] Profile {entity_sid} in draft, submitting first")
                    _trusthub_update_status(
                        "CustomerProfiles", entity_sid, "pending-review",
                        sub_account_sid, sub_account_auth_token,
                    )
            except Exception as pe:
                logger.warning(f"[VoiceIntegrity] Profile check/submit failed for {entity_sid}: {pe}")

    # ── Step 6: Evaluate and submit ──
    try:
        evaluation = client.trusthub.v1.trust_products(new_tp_sid) \
            .trust_products_evaluations.create(policy_sid=VOICE_INTEGRITY_POLICY_SID)
        eval_status = getattr(evaluation, "status", "unknown")
        eval_results = getattr(evaluation, "results", None) or []
        logger.info(
            f"[VoiceIntegrity] New product evaluation: status={eval_status}, results={eval_results}"
        )

        if eval_status == "noncompliant":
            error_details = []
            for r in eval_results:
                if isinstance(r, dict) and r.get("status") == "noncompliant":
                    error_details.append(r.get("friendly_name", r.get("requirement_key", "unknown")))
            detail_msg = ", ".join(error_details) if error_details else "evaluation returned noncompliant"
            raise TwilioRestException(
                status=400, uri="",
                msg=f"Voice Integrity evaluation failed on new product: {detail_msg}. "
                    f"New product SID: {new_tp_sid}")
    except TwilioRestException:
        raise
    except Exception as eval_err:
        logger.warning(f"[VoiceIntegrity] Evaluation check failed (proceeding): {eval_err}")

    # Direct HTTP POST to bypass SDK enum/serialization issues with Status
    auth_sid = sub_account_sid or TWILIO_ACCOUNT_SID
    auth_token = sub_account_auth_token or TWILIO_AUTH_TOKEN
    resp_data = _trusthub_update_status(
        "TrustProducts", new_tp_sid, "pending-review",
        auth_sid, auth_token,
    )
    tp_status = resp_data.get("status", "unknown")
    logger.info(f"[VoiceIntegrity] Submitted new product {new_tp_sid} for review → {tp_status}")

    # Verify status actually changed
    if tp_status == "draft":
        logger.error(
            f"[VoiceIntegrity] New Trust Product {new_tp_sid} STILL in draft after "
            f"POST Status=pending-review. Full response: {resp_data}"
        )
        raise TwilioRestException(
            status=400, uri="",
            msg="Voice Integrity submission failed: Trust Product remained in 'draft' status. "
                "Please check the Twilio Console for details or contact support."
        )

    return {
        "trust_product_sid": new_tp_sid,
        "old_trust_product_sid": trust_product_sid,
        "status": tp_status,
        "assigned_numbers": old_assigned_numbers,
    }


def remove_number_from_voice_integrity(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sid: str,
    sub_account_auth_token: str = "",
) -> bool:
    """Remove a phone number from a Voice Integrity Trust Product."""
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_channel_endpoint_assignment.list(limit=100)
        for a in assignments:
            if a.channel_endpoint_sid == phone_number_sid:
                client.trusthub.v1.trust_products(trust_product_sid) \
                    .trust_products_channel_endpoint_assignment(a.sid).delete()
                logger.info(f"[VoiceIntegrity] Removed {phone_number_sid} from {trust_product_sid}")
                return True
        logger.warning(f"[VoiceIntegrity] {phone_number_sid} not found on {trust_product_sid}")
        return False
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Remove number failed: {e}")
        raise


def discover_voice_integrity_products(
    sub_account_sid: str,
    sub_account_auth_token: str = "",
) -> list:
    """
    Discover existing Voice Integrity Trust Products on a sub-account.
    Returns list of {trust_product_sid, status, friendly_name} dicts.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        products = client.trusthub.v1.trust_products.list(limit=100)
        results = []
        for p in products:
            fn = getattr(p, "friendly_name", "") or ""
            if "voice integrity" in fn.lower():
                results.append({
                    "trust_product_sid": p.sid,
                    "status": p.status,
                    "friendly_name": fn,
                    "date_created": p.date_created.isoformat() if p.date_created else "",
                })
        logger.info(f"[VoiceIntegrity] Discovered {len(results)} Voice Integrity products")
        return results
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Discovery failed: {e}")
        return []


# ──────────────────────────────────────────────────────────────
# FULL PROVISIONING
# ──────────────────────────────────────────────────────────────

def provision_master(webhook_base_url: str) -> dict:
    """
    Provision the platform owner (super_admin).
    No sub-account needed — uses the master Twilio account directly.
    Creates a TwiML app and uses the existing master phone number.
    """
    master_sid = TWILIO_ACCOUNT_SID
    master_phone = TWILIO_PHONE_NUMBER

    # 1. Create TwiML App on master account
    twiml_app = create_twiml_app(master_sid, webhook_base_url)
    twiml_app_sid = twiml_app["twiml_app_sid"]

    # 2. If master phone number exists, configure its webhooks
    number_sid = ""
    if master_phone:
        client = get_master_client()
        try:
            numbers = client.incoming_phone_numbers.list(phone_number=master_phone)
            if numbers:
                num = numbers[0]
                num.update(
                    voice_url=f"{webhook_base_url}/voice/inbound",
                    voice_method="POST",
                    voice_application_sid=twiml_app_sid,
                    status_callback=f"{webhook_base_url}/voice/status",
                    status_callback_method="POST",
                )
                number_sid = num.sid
                logger.info(f"Configured master number {master_phone} with TwiML app {twiml_app_sid}")
        except TwilioRestException as e:
            logger.error(f"Failed to configure master number: {e}")

    result = {
        "twilio_sub_account_sid": master_sid,
        "twilio_auth_token": TWILIO_AUTH_TOKEN,
        "twilio_twiml_app_sid": twiml_app_sid,
        "twilio_api_key_sid": TWILIO_API_KEY_SID,
        "twilio_api_key_secret": TWILIO_API_KEY_SECRET,
        "twilio_phone_number": master_phone,
        "twilio_number_sid": number_sid,
    }

    logger.info(f"Master account provisioned: sid={master_sid} phone={master_phone}")
    return result


def provision_subscriber(subscriber_email: str, location_id: str,
                          webhook_base_url: str) -> dict:
    """
    Provision a sub-user (customer):
    1. Create sub-account under master
    2. Create TwiML App
    3. Create API Key on sub-account (for AccessToken generation)

    The user buys their own phone number afterwards via the Numbers tab.
    Returns all the IDs needed for voice_config.
    """
    friendly_name = f"GrokBot_{location_id[:30]}_{subscriber_email[:20]}"

    # 1. Create sub-account
    sub_account = create_sub_account(friendly_name)
    sub_sid = sub_account["sid"]

    # 2. Create TwiML App
    twiml_app = create_twiml_app(sub_sid, webhook_base_url)
    twiml_app_sid = twiml_app["twiml_app_sid"]

    # 3. Create API Key on the sub-account (required for valid AccessTokens)
    api_key = create_api_key(sub_sid)

    # 4. Enable Voice Insights Advanced Features (non-fatal if fails)
    try:
        enable_voice_insights_advanced(sub_sid)
    except Exception as e:
        logger.warning(f"Voice Insights enable failed (non-fatal): {e}")

    result = {
        "twilio_sub_account_sid": sub_sid,
        "twilio_auth_token": sub_account["auth_token"],
        "twilio_twiml_app_sid": twiml_app_sid,
        "twilio_api_key_sid": api_key["api_key_sid"],
        "twilio_api_key_secret": api_key["api_key_secret"],
        "twilio_phone_number": "",
        "twilio_number_sid": "",
    }

    logger.info(f"Subscriber provisioned: {subscriber_email} -> sub_account={sub_sid} (no number — user buys their own)")
    return result


# ──────────────────────────────────────────────────────────────
# VOICE INSIGHTS ADVANCED FEATURES
# ──────────────────────────────────────────────────────────────

def enable_voice_insights_advanced(sub_account_sid: str) -> bool:
    """
    Enable Voice Insights Advanced Features on a sub-account.

    Uses the master account credentials with SubaccountSid parameter,
    as documented at:
    https://www.twilio.com/docs/voice/voice-insights/api/call/voice-insights-settings-resource

    Advanced Features ($0.0025/min) unlock:
    - Call Summary API (PDD, SIP codes, carrier info, quality tags)
    - Call Events API (SIP signaling timeline)
    - Call Metrics API (jitter, packet loss, MOS time-series)
    - Event Streams integration
    """
    client = get_master_client()
    try:
        settings = client.insights.v1.settings().update(
            advanced_features=True,
            subaccount_sid=sub_account_sid,
        )
        logger.info(f"Voice Insights Advanced enabled for {sub_account_sid}: advanced={settings.advanced_features}")
        return True
    except Exception as e:
        logger.error(f"Failed to enable Voice Insights Advanced for {sub_account_sid}: {e}")
        return False


def get_voice_insights_settings(sub_account_sid: str = None) -> dict:
    """Check Voice Insights settings for an account."""
    client = get_master_client()
    try:
        kwargs = {}
        if sub_account_sid:
            kwargs['subaccount_sid'] = sub_account_sid
        settings = client.insights.v1.settings().fetch(**kwargs)
        return {
            "advanced_features": settings.advanced_features,
            "voice_trace": settings.voice_trace,
        }
    except Exception as e:
        logger.warning(f"Failed to fetch Voice Insights settings: {e}")
        return {"advanced_features": False, "voice_trace": False}


def fetch_call_insights_summary(call_sid: str, sub_account_sid: str = None,
                                 sub_account_auth_token: str = None) -> dict:
    """
    Fetch the Voice Insights Call Summary for a completed call.

    Returns the full summary including:
    - properties.pdd_ms (post-dial delay)
    - properties.last_sip_response_num
    - carrier_edge metrics (jitter, packet loss)
    - call_state, call_type, tags
    - from/to carrier info
    - trust data (branded calling, verified caller)

    API: GET https://insights.twilio.com/v1/Voice/{CallSid}/Summary

    The summary is partial within ~10 min of call end, complete within ~30 min.
    """
    if sub_account_sid and sub_account_auth_token:
        client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    elif sub_account_sid:
        client = get_sub_account_client(sub_account_sid)
    else:
        client = get_master_client()

    try:
        summary = client.insights.v1.calls(call_sid).summary().fetch()

        # Extract the key fields from the summary object
        result = {
            "call_sid": summary.call_sid,
            "call_type": summary.call_type,
            "call_state": summary.call_state,
            "processing_state": summary.processing_state,
            "duration": summary.duration,
            "connect_duration": summary.connect_duration,
            "start_time": str(summary.start_time) if summary.start_time else None,
            "end_time": str(summary.end_time) if summary.end_time else None,
            "tags": summary.tags or [],
            "attributes": summary.attributes or {},
            "properties": summary.properties or {},
            "carrier_edge": summary.carrier_edge or {},
            "client_edge": summary.client_edge or {},
            "sdk_edge": summary.sdk_edge or {},
            "sip_edge": summary.sip_edge or {},
            "trust": getattr(summary, 'trust', None) or {},
            "from_info": getattr(summary, 'from_', None) or {},
            "to_info": getattr(summary, 'to', None) or {},
            "annotation": summary.annotation or {},
        }

        # Extract commonly-accessed fields for quick lookups
        props = result["properties"] or {}
        result["_pdd_ms"] = props.get("pdd_ms")
        result["_last_sip_response"] = props.get("last_sip_response_num")
        result["_disconnected_by"] = props.get("disconnected_by")

        return result
    except Exception as e:
        logger.warning(f"Failed to fetch call insights for {call_sid}: {e}")
        return {}


def fetch_call_insights_events(call_sid: str, sub_account_sid: str = None,
                                sub_account_auth_token: str = None,
                                edge: str = None) -> list:
    """
    Fetch Call Insights Events (SIP signaling timeline) for a call.

    API: GET https://insights.twilio.com/v1/Voice/{CallSid}/Events

    Returns list of events with: edge, group, level, name, timestamp.
    Optional edge filter: carrier_edge, sip_edge, sdk_edge, client_edge.
    """
    if sub_account_sid and sub_account_auth_token:
        client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    elif sub_account_sid:
        client = get_sub_account_client(sub_account_sid)
    else:
        client = get_master_client()

    try:
        kwargs = {}
        if edge:
            kwargs['edge'] = edge
        events = client.insights.v1.calls(call_sid).events.list(**kwargs)
        return [
            {
                "edge": e.edge,
                "group": e.group,
                "level": e.level,
                "name": e.name,
                "timestamp": str(e.timestamp) if e.timestamp else None,
            }
            for e in events
        ]
    except Exception as e:
        logger.warning(f"Failed to fetch call events for {call_sid}: {e}")
        return []
