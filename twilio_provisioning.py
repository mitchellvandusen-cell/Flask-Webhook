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
#   - CNAM registration via Twilio Trust Hub / Business Profiles

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


def get_master_client() -> TwilioClient:
    """Get or create the master Twilio client."""
    global _master_client
    if _master_client is None:
        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
            raise ValueError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in environment")
        _master_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _master_client


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
        # No native credentials available — fall back to master-credential client.
        # This is the legacy behaviour and may return master-account resources for
        # Messaging/TrustHub endpoints. Log a warning so it's visible.
        logger.warning(
            f"[get_sub_account_client_native] No auth token provided for {sub_account_sid}; "
            "falling back to master-credential client. Messaging/TrustHub API calls may "
            "return master-account resources. Provide sub_account_auth_token to fix this."
        )
        return get_sub_account_client(sub_account_sid)
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
                                sub_account_auth_token: str = "") -> dict:
    """
    Register a business profile for CNAM / caller ID on Twilio.
    This creates a Trust Hub Customer Profile for the sub-account.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    results = {"steps": [], "errors": []}

    try:
        # Step 1: Create a Customer Profile (Trust Hub)
        customer_profile = client.trusthub.v1.customer_profiles.create(
            friendly_name=business_name,
            email=contact_email,
            policy_sid="RNb0d4771c2c98518d916a3d4cd70a8f8b",  # Twilio's Business Profile Policy SID
            status_callback=None,
        )
        results["steps"].append({"name": "customer_profile", "status": "ok", "sid": customer_profile.sid})
        logger.info(f"Created Trust Hub customer profile: {customer_profile.sid}")

    except TwilioRestException as e:
        logger.error(f"Trust Hub registration failed: {e}")
        results["errors"].append(str(e))
        # Non-fatal — CNAM can still work without full Trust Hub

    try:
        # Step 2: Register CNAM for numbers (Twilio handles CNAM through the
        # Outgoing Caller ID, which is auto-set with verified business profiles)
        # For now, set the friendly name on all numbers as a proxy for CNAM
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

    return results


def get_spam_protection_status(sub_account_sid: str) -> dict:
    """Get current spam/CNAM protection status for a sub-account."""
    client = get_sub_account_client(sub_account_sid)
    try:
        numbers = client.incoming_phone_numbers.list()
        total = len(numbers)
        protected = sum(1 for n in numbers if n.friendly_name and len(n.friendly_name) > 0)

        return {
            "numbers_total": total,
            "numbers_protected": protected,
            "stir_shaken": "active",  # Twilio auto-manages STIR/SHAKEN
            "numbers": [
                {
                    "phone": n.phone_number,
                    "sid": n.sid,
                    "friendly_name": n.friendly_name,
                    "status": "active",
                }
                for n in numbers
            ],
        }
    except TwilioRestException as e:
        logger.error(f"Failed to get spam protection status: {e}")
        return {"numbers_total": 0, "numbers_protected": 0}


def get_cnam_monitor(sub_account_sid: str) -> list:
    """
    Get CNAM status for all numbers on a sub-account.
    Returns a list of dicts with phone, sid, friendly_name (the CNAM we set),
    and cnam_enabled flag.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        numbers = client.incoming_phone_numbers.list()
        result = []
        for n in numbers:
            result.append({
                "phone": n.phone_number,
                "sid": n.sid,
                "friendly_name": n.friendly_name or "",
                "cnam_enabled": bool(n.friendly_name and len(n.friendly_name.strip()) > 0),
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
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        # ── Step 1: Secondary Customer Profile ──
        profile = client.trusthub.v1.customer_profiles.create(
            friendly_name=f"A2P Brand: {business_name}",
            email=contact_email,
            policy_sid="RNb0d4771c2c98518d916a3d4cd70a8f8b",
        )
        logger.info(f"Created A2P Customer Profile: {profile.sid}")

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
                object_sid=profile.sid,
            )

        # ── Step 6: Submit TrustProduct for evaluation ──
        client.trusthub.v1.trust_products(trust_product.sid).update(
            status="pending-review",
        )
        logger.info(f"Submitted TrustProduct {trust_product.sid} for review")

        # ── Step 7: Register Brand ──
        # customer_profile_bundle_sid = Customer Profile SID (BU...)
        # a2p_profile_bundle_sid      = TrustProduct SID (BU...)  ← NOT the profile!
        brand = client.messaging.v1.brand_registrations.create(
            customer_profile_bundle_sid=profile.sid,
            a2p_profile_bundle_sid=trust_product.sid,
        )

        logger.info(f"Created A2P Brand: {brand.sid} status={brand.status}")
        return {
            "brand_sid": brand.sid,
            "status": brand.status,
            "profile_sid": profile.sid,
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
    if existing_profile_sid:
        try:
            profile = client.trusthub.v1.customer_profiles(existing_profile_sid).fetch()
            status = getattr(profile, "status", "")
            logger.info(f"[VoiceIntegrity] Existing profile {existing_profile_sid} status: {status}")
            if status in ("twilio-approved", "in-review", "pending-review"):
                return existing_profile_sid
            logger.warning(
                f"[VoiceIntegrity] Existing profile {existing_profile_sid} status '{status}' "
                "is not approved; will search for another."
            )
        except TwilioRestException as e:
            logger.warning(f"[VoiceIntegrity] Could not fetch profile {existing_profile_sid}: {e}")

    # ── Discover approved profiles on the sub-account ──
    try:
        profiles = client.trusthub.v1.customer_profiles.list(
 status="twilio-approved", limit=20)
        if profiles:
            best = profiles[0]
            logger.info(
                f"[VoiceIntegrity] Discovered approved profile: {best.sid} "
                f"({getattr(best, 'friendly_name', '')})"
            )
            return best.sid
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

    return profile_sid


def _find_primary_profile_sid() -> str:
    """
    Find the Primary Business Profile SID on the master Twilio account.
    This is created once in the Twilio Console and reused for all sub-accounts.
    Returns the SID (BU...) or empty string if not found.
    """
    try:
        master = get_master_client()
        profiles = master.trusthub.v1.customer_profiles.list(
            status="twilio-approved", limit=50)
        for p in profiles:
            # Primary profiles are typically named "Primary" or are the first approved one
            fname = getattr(p, "friendly_name", "")
            if "primary" in fname.lower() or "business" in fname.lower():
                logger.info(f"[VoiceIntegrity] Found Primary Profile: {p.sid} ({fname})")
                return p.sid
        # Fall back to the first approved profile on master
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
    Create a Voice Integrity Trust Product following the ISV/Subaccounts flow.

    Per Twilio docs:
      https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/
      voice-integrity-onboarding/voice-integrity-trust-hub-api-isvs-subaccounts

    Flow:
      1. Find or create a Secondary Customer Profile on the sub-account
         (linked to the Primary Business Profile on the master account)
      2. Create EndUser with voice_integrity_information type
      3. Create Voice Integrity Trust Product (with VI-specific policy SID)
      4. Link Secondary Profile → Trust Product (EntityAssignment)
      5. Link EndUser → Trust Product (EntityAssignment)

    Returns dict with trust_product_sid, profile_sid, end_user_sid, status.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    try:
        # ── Step 1: Secondary Customer Profile ──
        primary_sid = _find_primary_profile_sid()
        profile_sid = _find_or_create_secondary_profile(
            client=client,
            sub_account_sid=sub_account_sid,
            business_name=business_name,
            contact_email=contact_email,
            existing_profile_sid=existing_profile_sid,
            primary_profile_sid=primary_sid,
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

        # ── Step 4: Link Secondary Profile → Trust Product ──
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
                    if e.code == 20409:
                        logger.info(f"[VoiceIntegrity] {pn_sid} already on profile (20409)")
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
            # 20409 = already assigned — treat as success
            if e.code == 20409:
                assigned += 1
                logger.info(f"[VoiceIntegrity] {pn_sid} already assigned (20409)")
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
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        # Run evaluation to validate completeness before submitting
        try:
            evaluation = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_evaluations.create(policy_sid=VOICE_INTEGRITY_POLICY_SID)
            eval_status = getattr(evaluation, "status", "unknown")
            logger.info(f"[VoiceIntegrity] Evaluation for {trust_product_sid}: {eval_status}")
            if eval_status == "noncompliant":
                # Surface evaluation failures instead of proceeding to a guaranteed rejection
                eval_results = getattr(evaluation, "results", None) or []
                error_details = []
                for r in eval_results:
                    if isinstance(r, dict) and r.get("status") == "noncompliant":
                        error_details.append(r.get("friendly_name", r.get("requirement_key", "unknown")))
                detail_msg = ", ".join(error_details) if error_details else "evaluation returned noncompliant"
                raise TwilioRestException(
                    status=400, uri="", msg=f"Voice Integrity evaluation failed: {detail_msg}")
        except TwilioRestException:
            raise  # re-raise evaluation noncompliant errors
        except Exception as eval_err:
            # Non-Twilio errors during evaluation — log but allow submission attempt
            logger.warning(f"[VoiceIntegrity] Evaluation check failed (proceeding): {eval_err}")

        tp = client.trusthub.v1.trust_products(trust_product_sid).update(
            status="pending-review",
        )
        logger.info(f"[VoiceIntegrity] Submitted {trust_product_sid} for review → {tp.status}")
        return {"trust_product_sid": tp.sid, "status": tp.status}
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

        return {
            "trust_product_sid": tp.sid,
            "status": tp.status,
            "friendly_name": getattr(tp, "friendly_name", ""),
            "date_created": tp.date_created.isoformat() if tp.date_created else "",
            "date_updated": tp.date_updated.isoformat() if tp.date_updated else "",
            "assigned_numbers": assigned_numbers,
            "assigned_count": len(assigned_numbers),
        }
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Status check failed: {e}")
        raise


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
