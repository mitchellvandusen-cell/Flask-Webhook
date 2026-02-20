# twilio_provisioning.py — White-label Twilio sub-account provisioning
#
# Architecture: One master Twilio account (yours) creates sub-accounts per subscriber.
# Users never see Twilio. They sign up, pay, and the dialer just works.
#
# Sub-account flow:
#   1. Subscriber signs up / activates voice
#   2. Backend creates a Twilio sub-account under the master
#   3. Backend buys a phone number on the sub-account
#   4. Backend creates a TwiML App for webhooks
#   5. Credentials stored in voice_config (internal, never shown in UI)
#
# CNAM / Spam Protection:
#   - SHAKEN/STIR attestation is automatic on Twilio
#   - CNAM registration via Twilio Trust Hub / Business Profiles
#   - A2P 10DLC registration for verified calling

import os
import json
import logging
import time

from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioRestException
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

logger = logging.getLogger("twilio_provisioning")

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
    Uses master credentials but targets the sub-account for API calls."""
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, sub_account_sid)


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
            kwargs["in_region"] = state
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
                          sub_account_sid: str = "") -> str:
    """
    Generate a Twilio Access Token with Voice grant for browser calling.
    Uses API Key auth (required for Access Tokens).
    """
    if not TWILIO_API_KEY_SID or not TWILIO_API_KEY_SECRET:
        raise ValueError("TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET must be set for browser calling")

    account_sid = sub_account_sid or TWILIO_ACCOUNT_SID

    logger.info(f"[generate_voice_token] account_sid={account_sid} api_key={TWILIO_API_KEY_SID} identity={identity} twiml_app={twiml_app_sid}")

    token = AccessToken(
        account_sid,
        TWILIO_API_KEY_SID,
        TWILIO_API_KEY_SECRET,
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
                          custom_params: dict = None) -> dict:
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

        if machine_detection:
            kwargs["machine_detection"] = machine_detection
            kwargs["machine_detection_timeout"] = 4
            kwargs["async_amd"] = True
            kwargs["async_amd_status_callback"] = f"{webhook_base_url}/voice/amd-status"
            kwargs["async_amd_status_callback_method"] = "POST"

        # Pass custom params as URL params so they arrive in the TwiML webhook
        if custom_params:
            url_params = "&".join(f"{k}={v}" for k, v in custom_params.items())
            kwargs["url"] = f"{webhook_base_url}/voice/outbound-twiml?{url_params}"

        call = client.calls.create(**kwargs)
        logger.info(f"Outbound call created: {call.sid} to {to} from {from_number}")
        return {
            "call_sid": call.sid,
            "status": call.status,
            "to": call.to,
            "from": call.from_,
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
            url=f"{webhook_base_url}/voice/transfer-twiml?transfer_to={transfer_to}",
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
                                contact_phone: str) -> dict:
    """
    Register a business profile for CNAM / caller ID on Twilio.
    This creates a Trust Hub Customer Profile for the sub-account.
    """
    client = get_sub_account_client(sub_account_sid)
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

    result = {
        "twilio_sub_account_sid": sub_sid,
        "twilio_auth_token": sub_account["auth_token"],
        "twilio_twiml_app_sid": twiml_app_sid,
        "twilio_phone_number": "",
        "twilio_number_sid": "",
    }

    logger.info(f"Subscriber provisioned: {subscriber_email} -> sub_account={sub_sid} (no number — user buys their own)")
    return result
