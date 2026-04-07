"""Twilio client factories and authentication utilities.

Handles master account and sub-account client creation,
auth token refresh, and the _trusthub_update_status workaround.
"""


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
    account_sid: str,
    auth_token: str,
) -> dict:
    """
    Update a TrustHub resource status via direct HTTP POST.

    Bypasses the twilio-python SDK's .update() method which has known issues
    with TrustHub status enums — the SDK can silently drop the Status parameter,
    causing the resource to stay in 'draft' while returning HTTP 200.

    REQUIRES explicit sub-account credentials. No fallback to master —
    master account Trust Hub is managed via Twilio Console only.

    Args:
        resource_type: "TrustProducts" or "CustomerProfiles"
        resource_sid: The BU... SID of the resource
        target_status: Status to set (e.g. "pending-review")
        account_sid: Sub-account SID (REQUIRED — no default)
        auth_token: Sub-account auth token (REQUIRED — no default)

    Returns:
        dict: Full JSON response from Twilio API
    """
    if not account_sid or not auth_token:
        raise ValueError(
            f"[TrustHub] _trusthub_update_status requires explicit sub-account "
            f"credentials. Got account_sid={'SET' if account_sid else 'EMPTY'}, "
            f"auth_token={'SET' if auth_token else 'EMPTY'}. "
            f"Never fall back to master credentials for Trust Hub operations."
        )
    import requests as _requests
    url = f"https://trusthub.twilio.com/v1/{resource_type}/{resource_sid}"
    sid = account_sid
    token = auth_token
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


def _normalize_phone_e164(phone: str) -> str:
    """Normalize a phone number to E.164 format (+1XXXXXXXXXX).

    Handles common user input formats: (555) 123-4567, 555-123-4567,
    5551234567, +15551234567, 1-555-123-4567, etc.
    """
    import re
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits[0] == '1':
        return f"+{digits}"
    elif digits.startswith('+'):
        return digits
    return f"+{digits}" if digits else phone


def _ensure_sub_account_auth_token(sub_account_sid: str, sub_account_auth_token: str) -> str:
    """Ensure we have a valid auth token for a sub-account.

    Fetches a fresh token from the Twilio API because sub-account auth tokens
    can rotate (e.g., console access, API regeneration). Uses a 60-second
    in-memory cache so multiple Trust Hub calls in the same request don't
    each hit Twilio.

    If the fresh token differs from the stored one, persists it back to
    the DB so future calls use the correct token.
    """
    if not sub_account_sid:
        raise ValueError(
            "sub_account_sid is required. Every subscriber must have a Twilio sub-account."
        )

    # Check short-lived cache first (avoids repeated Twilio API calls per request)
    cached = _auth_token_cache.get(sub_account_sid)
    if cached:
        cached_token, cached_at = cached
        if time.time() - cached_at < _AUTH_TOKEN_CACHE_TTL:
            return cached_token

    # Fetch fresh token from Twilio — stored tokens can rotate
    try:
        master = get_master_client()
        acct = master.api.accounts(sub_account_sid).fetch()
        fresh_token = acct.auth_token or ""
        if fresh_token:
            # Cache it for 60s
            _auth_token_cache[sub_account_sid] = (fresh_token, time.time())
            if fresh_token != sub_account_auth_token:
                logger.info(
                    f"[TrustHub] Auth token for sub-account {sub_account_sid} "
                    f"{'recovered' if not sub_account_auth_token else 'refreshed (was stale)'} "
                    "from Twilio API"
                )
                _persist_refreshed_auth_token(sub_account_sid, fresh_token)
            return fresh_token
    except Exception as e:
        logger.warning(f"[TrustHub] Could not fetch fresh auth token for {sub_account_sid}: {e}")

    # Fall back to stored token if Twilio API fetch failed
    if sub_account_auth_token:
        logger.warning(
            f"[TrustHub] Using stored auth token for {sub_account_sid} "
            "(could not verify freshness)"
        )
        return sub_account_auth_token

    raise ValueError(
        f"Sub-account {sub_account_sid} has no auth token. "
        "Re-provision voice to fix this."
    )


def _persist_refreshed_auth_token(sub_account_sid: str, fresh_token: str):
    """Persist a refreshed sub-account auth token to BOTH dedicated column
    and voice_config JSONB.

    Updates:
      1. subscribers.twilio_sub_account_auth_token (dedicated column)
      2. voice_config->>'twilio_auth_token' (JSONB for backward compat)
    """
    try:
        from db_legacy import get_db_connection, return_db_connection
        conn = get_db_connection()
        if not conn:
            logger.warning("[TrustHub] No DB connection — could not persist refreshed auth token")
            return
        try:
            cur = conn.cursor()
            # Update both: dedicated column + voice_config JSONB
            cur.execute("""
                UPDATE subscribers
                SET twilio_sub_account_auth_token = %s,
                    voice_config = COALESCE(voice_config, '{}'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE ctid = (
                    SELECT ctid FROM subscribers
                    WHERE twilio_sub_account_sid = %s
                       OR voice_config->>'twilio_sub_account_sid' = %s
                    LIMIT 1
                )
            """, (fresh_token,
                  json.dumps({"twilio_auth_token": fresh_token}),
                  sub_account_sid, sub_account_sid))
            conn.commit()
            rows = cur.rowcount
            cur.close()
            if rows > 0:
                logger.info(f"[TrustHub] Persisted refreshed auth token for {sub_account_sid}")
            else:
                logger.warning(f"[TrustHub] No subscriber found with sub_account_sid={sub_account_sid}")
        except Exception as e:
            conn.rollback()
            logger.warning(f"[TrustHub] Could not persist refreshed auth token: {e}")
        finally:
            return_db_connection(conn)
    except ImportError:
        logger.warning("[TrustHub] db_legacy not available — skipping token persistence")


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

    ALWAYS refreshes the auth token from Twilio to handle rotation.
    Uses a short-lived cache (60s) to avoid excessive API calls when
    multiple Trust Hub operations happen in the same request.
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    return TwilioClient(sub_account_sid, sub_account_auth_token)


# Short-lived cache for _ensure_sub_account_auth_token to avoid
# repeated Twilio API calls within the same request cycle.
_auth_token_cache = {}  # {sub_account_sid: (token, timestamp)}
_AUTH_TOKEN_CACHE_TTL = 60  # seconds
