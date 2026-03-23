# ghl_api.py - Lead Connector OAuth & API Helpers (Flawless 2026)
import requests
import logging
import os
import time as _time
from datetime import datetime, timedelta
from db import get_subscriber_info_hybrid, update_subscriber_token
from token_encryption import encrypt_token, decrypt_token

logger = logging.getLogger(__name__)

GHL_TOKEN_URL = "https://services.leadconnectorhq.com/oauth/token"
GHL_HEADERS = {"Version": "2021-04-15", "Content-Type": "application/json"}

# user_type values GHL accepts — Location for sub-accounts, Company for agency-level
_USER_TYPES = ["Location", "Company"]


def _get_env_with_fallback(*names):
    """Try multiple env var names, return first non-empty value."""
    for name in names:
        val = os.getenv(name)
        if val:
            return val
    return None


def _load_oauth_credentials():
    """Load both marketplace and private OAuth credential sets from environment.
    Returns (marketplace_id, marketplace_secret, private_id, private_secret)."""
    marketplace_id = _get_env_with_fallback("GHL_CLIENT_ID")
    marketplace_secret = _get_env_with_fallback("GHL_CLIENT_SECRET")
    private_id = _get_env_with_fallback("PRIVATE_APP_CLIENT_ID", "GHL_PRIVATE_CLIENT_ID")
    private_secret = _get_env_with_fallback("PRIVATE_APP_SECRET_ID", "GHL_PRIVATE_CLIENT_SECRET")
    return marketplace_id, marketplace_secret, private_id, private_secret


# Cached at module load — env vars don't change at runtime
_OAUTH_CREDS_AVAILABLE = None
_NO_CREDS_LOGGED = False  # Track whether we've already logged the missing-creds error

def has_oauth_credentials():
    """Check if ANY GHL OAuth credentials are configured in environment.
    Result is cached after first call since env vars don't change at runtime."""
    global _OAUTH_CREDS_AVAILABLE
    if _OAUTH_CREDS_AVAILABLE is None:
        m_id, m_sec, p_id, p_sec = _load_oauth_credentials()
        _OAUTH_CREDS_AVAILABLE = bool((m_id and m_sec) or (p_id and p_sec))
    return _OAUTH_CREDS_AVAILABLE


def _build_cred_sets(oauth_app_type, marketplace_id, marketplace_secret, private_id, private_secret):
    """Build ordered list of credential sets to try based on stored app_type.
    Primary credentials first, then fallback."""
    cred_sets = []
    if oauth_app_type == 'private':
        if private_id and private_secret:
            cred_sets.append({"id": private_id, "secret": private_secret,
                              "label": "PRIVATE APP", "type": "private"})
        if marketplace_id and marketplace_secret:
            cred_sets.append({"id": marketplace_id, "secret": marketplace_secret,
                              "label": "MARKETPLACE (fallback)", "type": "marketplace"})
    else:
        if marketplace_id and marketplace_secret:
            cred_sets.append({"id": marketplace_id, "secret": marketplace_secret,
                              "label": "MARKETPLACE", "type": "marketplace"})
        if private_id and private_secret:
            cred_sets.append({"id": private_id, "secret": private_secret,
                              "label": "PRIVATE APP (fallback)", "type": "private"})
    return cred_sets


def _attempt_token_refresh(location_id, refresh_token, cred, oauth_app_type):
    """Try refreshing a token with a single credential set, trying both user_types.
    Returns (new_access_token, new_refresh_token, expires_in, used_type) on success,
    or (None, None, None, error_reason) on failure.
    error_reason: 'auth_error', 'network_error', 'server_error', 'no_access_token'"""

    for user_type in _USER_TYPES:
        payload = {
            "client_id": cred["id"],
            "client_secret": cred["secret"],
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "user_type": user_type
        }

        last_err = None
        for attempt in range(2):
            try:
                resp = requests.post(GHL_TOKEN_URL, data=payload, timeout=10)
                resp.raise_for_status()
                data = resp.json()

                new_access = data.get('access_token')
                new_refresh = data.get('refresh_token')
                expires_in = data.get('expires_in', 86400)

                if not new_access:
                    logger.error(f"Refresh response missing access_token (user_type={user_type}): "
                                f"{resp.text[:200]}")
                    return None, None, None, 'no_access_token'

                logger.info(f"✅ Token refreshed for {location_id} using {cred['label']} "
                           f"(user_type={user_type})")
                return new_access, new_refresh, expires_in, cred["type"]

            except requests.HTTPError as e:
                status = e.response.status_code if e.response else 0
                err_text = e.response.text[:300] if e.response else 'no body'

                if status in (400, 401, 403):
                    # Auth error with this user_type — try the other user_type
                    # before giving up on this credential set
                    logger.debug(f"Token refresh {status} with {cred['label']}, "
                                f"user_type={user_type}: {err_text}")
                    last_err = 'auth_error'
                    break  # Try next user_type
                else:
                    # 5xx or other — retry same request
                    last_err = 'server_error'
                    logger.warning(f"Token refresh attempt {attempt+1}/2 HTTP {status} "
                                  f"({cred['label']}, user_type={user_type})")
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = 'network_error'
                logger.warning(f"Token refresh attempt {attempt+1}/2 network error "
                              f"({cred['label']}, user_type={user_type}): {e}")

            if attempt == 0:
                _time.sleep(2)

    # Both user_types exhausted for this credential set
    return None, None, None, last_err or 'auth_error'


def get_valid_token(location_id: str, subscriber: dict = None) -> str | None:
    """
    Returns a valid Bearer access token or None on failure.

    Enterprise-grade token lifecycle:
    1. Return cached token if not expired (5-min buffer)
    2. Refresh using stored oauth_app_type credentials
    3. Fallback to alternate credential set if primary fails
    4. Try both Location and Company user_types for each credential set
    5. Last resort: return existing access_token even if expired
    6. Auto-correct oauth_app_type in DB if fallback creds succeed

    Args:
        location_id: GHL location ID
        subscriber: Optional pre-fetched subscriber dict (avoids redundant DB query)
    """
    if location_id in {'DEMO', 'DEMO_LOC', 'TEST_LOCATION_456'}:
        logger.debug(f"Internal Mode: Skipping auth for {location_id}")
        return 'DEMO'

    sub = subscriber or get_subscriber_info_hybrid(location_id)
    if not sub:
        logger.error(f"No subscriber config for {location_id}")
        return None

    raw_access = sub.get('access_token') or sub.get('crm_api_key')
    raw_refresh = sub.get('refresh_token')
    expires_at = sub.get('token_expires_at')
    oauth_app_type = sub.get('oauth_app_type', 'marketplace')

    # Decrypt tokens (handles both encrypted and legacy plaintext transparently)
    access_token = decrypt_token(raw_access) if raw_access else None
    refresh_token = decrypt_token(raw_refresh) if raw_refresh else None

    # Persistent token (no refresh_token — e.g. API key users)
    if not refresh_token:
        if access_token:
            logger.debug(f"Using persistent token for {location_id}")
            return access_token
        logger.error(f"No access_token or refresh_token for {location_id}")
        return None

    # Convert expires_at to datetime if it's a string
    if expires_at:
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            except Exception as e:
                logger.warning(f"Could not parse expires_at: {expires_at} | {e}")
                expires_at = None

    # Check expiry with buffer (5-min safety margin)
    # Handle timezone-aware vs naive datetime comparison safely
    if expires_at:
        try:
            now = datetime.now()
            if hasattr(expires_at, 'tzinfo') and expires_at.tzinfo is not None:
                expires_at = expires_at.replace(tzinfo=None)
            if expires_at > now + timedelta(minutes=5):
                return access_token
        except TypeError:
            logger.warning(f"Timezone comparison error for {location_id}, proceeding to refresh")

    # --- Token needs refresh ---
    marketplace_id, marketplace_secret, private_id, private_secret = _load_oauth_credentials()
    cred_sets = _build_cred_sets(oauth_app_type, marketplace_id, marketplace_secret,
                                 private_id, private_secret)

    if not cred_sets:
        global _NO_CREDS_LOGGED
        # Log ERROR only once to avoid spamming; subsequent calls use WARNING
        _log = logger.error if not _NO_CREDS_LOGGED else logger.warning
        _log(f"No OAuth credentials configured for app_type={oauth_app_type} | "
             f"GHL_CLIENT_ID={'set' if marketplace_id else 'MISSING'} | "
             f"GHL_CLIENT_SECRET={'set' if marketplace_secret else 'MISSING'} | "
             f"PRIVATE_APP_CLIENT_ID={'set' if private_id else 'MISSING'} | "
             f"PRIVATE_APP_SECRET_ID={'set' if private_secret else 'MISSING'}")
        _NO_CREDS_LOGGED = True
        if access_token:
            logger.debug(f"Returning possibly-expired token for {location_id} (no creds to refresh)")
            return access_token
        return None

    # Try each credential set with both user_types
    for cred_idx, cred in enumerate(cred_sets):
        logger.info(f"🔄 Refreshing token for {location_id} using {cred['label']} "
                    f"(app_type={oauth_app_type})")

        try:
            new_access, new_refresh, expires_in, result_type = _attempt_token_refresh(
                location_id, refresh_token, cred, oauth_app_type)

            if new_access:
                # Success — persist new tokens to DB (encrypted at rest)
                # CRITICAL: DB write must succeed or the single-use refresh_token is lost
                fix_type = result_type if result_type != oauth_app_type else None
                if fix_type:
                    logger.warning(f"🔧 Auto-correcting oauth_app_type for {location_id}: "
                                  f"{oauth_app_type} → {fix_type}")
                db_saved = update_subscriber_token(
                    location_id,
                    encrypt_token(new_access),
                    encrypt_token(new_refresh) if new_refresh else None,
                    expires_in,
                    oauth_app_type=fix_type,
                )
                if not db_saved:
                    logger.error(f"🚨 CRITICAL: Token refreshed but DB write failed for "
                                f"{location_id} — returning new token but refresh_token "
                                f"may be lost on next cycle")
                return new_access

            # Failed with this cred set
            if result_type == 'auth_error' and cred_idx < len(cred_sets) - 1:
                logger.warning(f"Auth error with {cred['label']} — trying fallback credentials")
                continue
            elif result_type == 'auth_error':
                logger.error(f"Token refresh auth error with {cred['label']} (no more fallbacks)")
            else:
                logger.error(f"Token refresh failed with {cred['label']}: {result_type}")
                if cred_idx < len(cred_sets) - 1:
                    continue  # Try next cred set on network/server errors too

        except Exception as e:
            logger.error(f"Token refresh unexpected error for {location_id}: {e}", exc_info=True)
            if cred_idx < len(cred_sets) - 1:
                continue

    # All credential sets exhausted — last resort: return existing token
    if access_token:
        logger.warning(f"⚠️ All refresh attempts failed for {location_id} — "
                      f"returning existing token as last resort (may be expired)")
        return access_token

    logger.error(f"Token refresh failed for {location_id}: all credential sets exhausted, "
                f"no existing token")
    return None


def get_valid_token_with_status(location_id: str, subscriber: dict = None,
                                force_refresh: bool = False) -> tuple:
    """
    Like get_valid_token but returns (token, was_refreshed, error_reason) tuple.
    Callers can use this to decide whether to create alerts or retry.

    Args:
        location_id: GHL location ID
        subscriber: Optional pre-fetched subscriber dict
        force_refresh: If True, skip expiry check and always attempt a fresh
                       refresh. Use this when the current token was rejected
                       by GHL (401/403) even though it hasn't expired per our DB.

    Returns:
        (token, True, None)       — Fresh token from successful refresh
        (token, False, None)      — Cached token, still valid
        (token, False, 'expired') — Returning expired token as last resort
        (None, False, reason)     — Complete failure, no usable token
    """
    if location_id in {'DEMO', 'DEMO_LOC', 'TEST_LOCATION_456'}:
        return 'DEMO', False, None

    sub = subscriber or get_subscriber_info_hybrid(location_id)
    if not sub:
        logger.error(f"No subscriber config for {location_id}")
        return None, False, 'no_subscriber'

    raw_access = sub.get('access_token') or sub.get('crm_api_key')
    raw_refresh = sub.get('refresh_token')
    expires_at = sub.get('token_expires_at')
    oauth_app_type = sub.get('oauth_app_type', 'marketplace')

    # Decrypt tokens (handles both encrypted and legacy plaintext transparently)
    access_token = decrypt_token(raw_access) if raw_access else None
    refresh_token = decrypt_token(raw_refresh) if raw_refresh else None

    # Persistent token (no refresh_token)
    if not refresh_token:
        if access_token:
            return access_token, False, None
        return None, False, 'no_tokens'

    # Skip expiry check when force_refresh is set — the token was rejected by GHL
    if not force_refresh:
        # Parse expires_at
        if expires_at and isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            except Exception:
                expires_at = None

        # Check expiry
        token_expired = True
        if expires_at:
            try:
                now = datetime.now()
                if hasattr(expires_at, 'tzinfo') and expires_at.tzinfo is not None:
                    expires_at = expires_at.replace(tzinfo=None)
                if expires_at > now + timedelta(minutes=5):
                    token_expired = False
            except TypeError:
                pass

        if not token_expired:
            return access_token, False, None
    else:
        logger.info(f"🔄 Force-refresh requested for {location_id} — skipping expiry check")

    # --- Token needs refresh ---
    marketplace_id, marketplace_secret, private_id, private_secret = _load_oauth_credentials()
    cred_sets = _build_cred_sets(oauth_app_type, marketplace_id, marketplace_secret,
                                 private_id, private_secret)

    if not cred_sets:
        global _NO_CREDS_LOGGED
        _log = logger.error if not _NO_CREDS_LOGGED else logger.warning
        _log(f"No OAuth credentials configured for app_type={oauth_app_type} | "
             f"GHL_CLIENT_ID={'set' if marketplace_id else 'MISSING'} | "
             f"GHL_CLIENT_SECRET={'set' if marketplace_secret else 'MISSING'} | "
             f"PRIVATE_APP_CLIENT_ID={'set' if private_id else 'MISSING'} | "
             f"PRIVATE_APP_SECRET_ID={'set' if private_secret else 'MISSING'}")
        _NO_CREDS_LOGGED = True
        if access_token:
            return access_token, False, 'no_credentials'
        return None, False, 'no_credentials'

    last_error = None
    for cred_idx, cred in enumerate(cred_sets):
        logger.info(f"🔄 Refreshing token for {location_id} using {cred['label']} "
                    f"(app_type={oauth_app_type})")

        try:
            new_access, new_refresh, expires_in, result_type = _attempt_token_refresh(
                location_id, refresh_token, cred, oauth_app_type)

            if new_access:
                fix_type = result_type if result_type != oauth_app_type else None
                if fix_type:
                    logger.warning(f"🔧 Auto-correcting oauth_app_type for {location_id}: "
                                  f"{oauth_app_type} → {fix_type}")
                db_saved = update_subscriber_token(
                    location_id,
                    encrypt_token(new_access),
                    encrypt_token(new_refresh) if new_refresh else None,
                    expires_in,
                    oauth_app_type=fix_type,
                )
                if not db_saved:
                    logger.error(f"🚨 CRITICAL: Token refreshed but DB write failed for "
                                f"{location_id} — refresh_token may be lost on next cycle")
                return new_access, True, None

            last_error = result_type
            if cred_idx < len(cred_sets) - 1:
                continue

        except Exception as e:
            logger.error(f"Token refresh unexpected error: {e}", exc_info=True)
            last_error = 'exception'
            if cred_idx < len(cred_sets) - 1:
                continue

    # All failed — last resort
    if access_token:
        logger.warning(f"⚠️ All refresh attempts failed for {location_id} — "
                      f"returning existing token as last resort")
        return access_token, False, 'expired'

    return None, False, last_error or 'all_creds_exhausted'


def refresh_tokens_proactively(buffer_minutes: int = 60):
    """
    Proactively refresh tokens that will expire within buffer_minutes.
    Call this from a cron job (e.g. every 15 minutes) to prevent token expiry
    from blocking webhook processing.

    Default buffer is 60 minutes — tokens are refreshed a full hour before
    expiry so they never lapse, even if a cron cycle is delayed.

    Returns dict with counts: {refreshed, failed, skipped, errors}
    """
    from db import get_db_connection, return_db_connection
    stats = {"refreshed": 0, "failed": 0, "skipped": 0, "errors": 0}

    conn = get_db_connection()
    if not conn:
        logger.error("Proactive refresh: cannot get DB connection")
        return stats

    try:
        cur = conn.cursor()
        # Find tokens expiring soon from BOTH subscribers and agency_billing tables.
        # Agency owners have tokens in agency_billing — without this UNION their
        # tokens would never be proactively refreshed.
        cur.execute("""
            SELECT location_id, email, oauth_app_type, access_token, refresh_token,
                   token_expires_at
            FROM subscribers
            WHERE refresh_token IS NOT NULL
              AND token_expires_at IS NOT NULL
              AND token_expires_at < NOW() + make_interval(mins => %s)
              AND token_expires_at > NOW() - interval '7 days'
            UNION ALL
            SELECT location_id, agency_email AS email, oauth_app_type, access_token,
                   refresh_token, token_expires_at
            FROM agency_billing
            WHERE refresh_token IS NOT NULL
              AND token_expires_at IS NOT NULL
              AND token_expires_at < NOW() + make_interval(mins => %s)
              AND token_expires_at > NOW() - interval '7 days'
        """, (buffer_minutes, buffer_minutes))
        expiring = cur.fetchall()
        cur.close()
    except Exception as e:
        logger.error(f"Proactive refresh query failed: {e}")
        return stats
    finally:
        return_db_connection(conn)

    if not expiring:
        logger.info(f"Proactive refresh: no tokens expiring within {buffer_minutes} minutes")
        return stats

    logger.info(f"Proactive refresh: found {len(expiring)} tokens to refresh")

    marketplace_id, marketplace_secret, private_id, private_secret = _load_oauth_credentials()

    for row in expiring:
        loc_id = row['location_id']
        oauth_type = row.get('oauth_app_type', 'marketplace')
        refresh_tok = decrypt_token(row['refresh_token']) if row['refresh_token'] else None

        cred_sets = _build_cred_sets(oauth_type, marketplace_id, marketplace_secret,
                                     private_id, private_secret)
        if not cred_sets:
            stats["skipped"] += 1
            continue

        refreshed = False
        for cred in cred_sets:
            new_access, new_refresh, expires_in, result_type = _attempt_token_refresh(
                loc_id, refresh_tok, cred, oauth_type)
            if new_access:
                fix_type = result_type if result_type != oauth_type else None
                db_saved = update_subscriber_token(
                    loc_id,
                    encrypt_token(new_access),
                    encrypt_token(new_refresh) if new_refresh else None,
                    expires_in,
                    oauth_app_type=fix_type,
                )
                if db_saved:
                    stats["refreshed"] += 1
                    refreshed = True
                else:
                    logger.error(f"🚨 Proactive refresh: token refreshed but DB write "
                                f"failed for {loc_id}")
                    stats["errors"] += 1
                logger.info(f"✅ Proactive refresh success: {loc_id}")
                break

        if not refreshed:
            stats["failed"] += 1
            logger.warning(f"⚠️ Proactive refresh failed: {loc_id} (app_type={oauth_type})")

        # Brief pause between refreshes to avoid rate limiting
        _time.sleep(0.5)

    logger.info(f"Proactive refresh complete: {stats}")
    return stats


def fetch_targeted_ghl_history(contact_id: str, location_id: str, access_token: str = None, limit: int = 20) -> list:
    """
    Fetches messages for the specific contact's conversation.
    Returns list of {'role': str, 'text': str, 'timestamp': str} or empty on failure.
    Handles malformed API responses gracefully (e.g., strings instead of dicts).
    """
    if not access_token:
        access_token = get_valid_token(location_id)
        if not access_token:
            logger.error(f"No valid token for history fetch {location_id}/{contact_id}")
            return []
    if access_token == 'DEMO':
        return []

    headers = {**GHL_HEADERS, "Authorization": f"Bearer {access_token}"}
    _token_refreshed = False

    try:
        # Step 1: Find conversation ID
        search_url = f"https://services.leadconnectorhq.com/conversations/search?locationId={location_id}&contactId={contact_id}"
        search_res = requests.get(search_url, headers=headers, timeout=10)

        # Auto-retry on 401/403 with a force-refreshed token
        if search_res.status_code in (401, 403) and not _token_refreshed:
            logger.warning(f"GHL history fetch auth failure (HTTP {search_res.status_code}) — force-refreshing token for {location_id}")
            fresh_token, was_refreshed, _err = get_valid_token_with_status(location_id, force_refresh=True)
            if fresh_token and was_refreshed:
                access_token = fresh_token
                headers["Authorization"] = f"Bearer {fresh_token}"
                _token_refreshed = True
                logger.info(f"Token force-refreshed for {location_id} — retrying history fetch")
                search_res = requests.get(search_url, headers=headers, timeout=10)

        search_res.raise_for_status()
        convos = search_res.json().get("conversations", [])

        if not convos:
            logger.warning(f"No conversation found for {contact_id} in {location_id}")
            return []

        convo_id = convos[0]["id"]

        # Step 2: Fetch messages
        msg_url = f"https://services.leadconnectorhq.com/conversations/{convo_id}/messages?limit={limit}"
        msg_res = requests.get(msg_url, headers=headers, timeout=10)
        msg_res.raise_for_status()

        # GHL returns either:
        #   {"messages": [...]}                               (older format — flat list)
        #   {"messages": {"messages": [...], "lastMessageId": ..., "nextPage": ...}}  (newer format — nested)
        # Iterating a dict yields its keys (strings), not message dicts, hence the
        # "Skipping invalid message item (not dict): lastMessageId" warnings.
        messages_payload = msg_res.json().get("messages", [])
        if isinstance(messages_payload, dict):
            raw_messages = messages_payload.get("messages", [])
        elif isinstance(messages_payload, list):
            raw_messages = messages_payload
        else:
            raw_messages = []
        formatted_history = []

        for m in raw_messages:
            # Safety: skip if not a dict
            if not isinstance(m, dict):
                logger.warning(f"Skipping invalid message item (not dict): {m}")
                continue

            # Safe key access
            direction = m.get("direction", "inbound")
            message_text = m.get("body", m.get("text", "[No text]"))
            timestamp = m.get("dateAdded", m.get("created_at", "Unknown"))

            role = "assistant" if direction == "outbound" else "lead"
            formatted_history.append({
                "role": role,
                "text": str(message_text).strip(),
                "timestamp": timestamp
            })

        logger.info(f"Fetched {len(formatted_history)} valid messages for {contact_id}")
        return formatted_history[::-1]  # oldest first

    except requests.RequestException as e:
        logger.error(f"GHL history fetch failed {location_id}/{contact_id}: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected history error {location_id}/{contact_id}: {e}", exc_info=True)
        return []


def fetch_contact_data_from_ghl(contact_id: str, location_id: str, access_token: str = None) -> dict:
    """
    Fetch complete contact data from GHL API for a specific contact_id.
    This ensures we have the correct name, phone, and other details for the contact.

    Returns dict with contact data or empty dict on failure.

    Fields returned: firstName, lastName, email, phone, address1, city, state, postalCode, etc.
    """
    if not contact_id or not location_id:
        logger.error("fetch_contact_data_from_ghl: Missing contact_id or location_id")
        return {}

    if not access_token:
        access_token = get_valid_token(location_id)
        if not access_token:
            logger.error(f"No valid token for contact fetch {location_id}/{contact_id}")
            return {}

    if access_token == 'DEMO':
        logger.info(f"DEMO mode: Skipping contact fetch for {contact_id}")
        return {}

    headers = {**GHL_HEADERS, "Authorization": f"Bearer {access_token}"}
    _token_refreshed = False

    try:
        # Fetch contact details from GHL API
        url = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
        response = requests.get(url, headers=headers, timeout=10)

        # Auto-retry on 401/403 with a force-refreshed token
        if response.status_code in (401, 403) and not _token_refreshed:
            logger.warning(f"GHL contact fetch auth failure (HTTP {response.status_code}) — force-refreshing token for {location_id}")
            fresh_token, was_refreshed, _err = get_valid_token_with_status(location_id, force_refresh=True)
            if fresh_token and was_refreshed:
                headers["Authorization"] = f"Bearer {fresh_token}"
                _token_refreshed = True
                logger.info(f"Token force-refreshed for {location_id} — retrying contact fetch")
                response = requests.get(url, headers=headers, timeout=10)

        response.raise_for_status()

        contact_data = response.json().get("contact", {})

        if contact_data:
            logger.info(f"✅ FETCHED CONTACT DATA FROM GHL | contact_id={contact_id} | firstName={contact_data.get('firstName')} | phone={contact_data.get('phone')}")
            return contact_data
        else:
            logger.warning(f"⚠️ Contact fetch returned empty data for {contact_id}")
            return {}

    except requests.HTTPError as e:
        logger.error(f"❌ GHL contact fetch HTTP error {e.response.status_code} for {contact_id}: {e.response.text}")
        return {}
    except Exception as e:
        logger.error(f"❌ GHL contact fetch failed for {contact_id}: {e}", exc_info=True)
        return {}


# ── Generic GHL API helpers ──────────────────────────────────────────────────

def fetch_all_ghl_items(base_url, headers, item_key='locations', max_pages=50):
    """
    Handle Lead Connector pagination — fetches all items across pages.
    Prevents onboarding failures when agencies have >20 locations.
    Returns a flat list of all items.
    """
    items = []
    url = base_url
    page_count = 0

    while url and page_count < max_pages:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if not resp.ok:
                if resp.status_code in (401, 403):
                    logger.warning(
                        f"SCOPE MISSING: /{item_key}/ returned {resp.status_code} — "
                        f"'{item_key}.readonly' scope likely not granted. "
                        f"Falling back to token-based data."
                    )
                else:
                    logger.error(
                        f"Failed to fetch {item_key} (page {page_count + 1}): "
                        f"{resp.status_code} {resp.text[:300]}"
                    )
                break

            data = resp.json()
            batch = data.get(item_key, [])
            items.extend(batch)
            page_count += 1

            logger.info(f"Fetched {len(batch)} {item_key} from page {page_count} (total: {len(items)})")

            meta = data.get('meta', {})
            next_url = meta.get('nextPageUrl') or data.get('nextPageUrl')

            if next_url:
                url = next_url if next_url.startswith('http') else f"https://services.leadconnectorhq.com{next_url}"
            else:
                url = None

        except Exception as e:
            logger.error(f"Pagination error fetching {item_key} (page {page_count + 1}): {e}")
            break

    logger.info(f"Pagination complete: {len(items)} total {item_key} fetched across {page_count} pages")
    return items


def ghl_api_call(method, url, headers=None, data=None, timeout=15, label="GHL API"):
    """
    Make a GHL API call with 1 automatic retry on transient errors (5xx, timeout, connection).
    Returns (response, error_message). On success error_message is None.
    """
    last_err = None
    for attempt in range(2):
        try:
            if method == 'POST':
                resp = requests.post(url, data=data, headers=headers, timeout=timeout)
            else:
                resp = requests.get(url, headers=headers, timeout=timeout)

            if resp.status_code < 500:
                return resp, None

            last_err = f"{label} returned {resp.status_code}"
            logger.warning(f"{label} attempt {attempt + 1}/2 got {resp.status_code}, body={resp.text[:300]}")
        except requests.Timeout:
            last_err = f"{label} timed out after {timeout}s"
            logger.warning(f"{label} attempt {attempt + 1}/2 timed out")
        except requests.ConnectionError as e:
            last_err = f"{label} connection error: {e}"
            logger.warning(f"{label} attempt {attempt + 1}/2 connection error: {e}")
        except Exception as e:
            last_err = f"{label} unexpected error: {e}"
            logger.warning(f"{label} attempt {attempt + 1}/2 unexpected error: {e}")

    return None, last_err
