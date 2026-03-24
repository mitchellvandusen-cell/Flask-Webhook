# ghl_message.py - Send SMS via Lead Connector (Flawless 2026)
import logging
import os
import time as time_module
import requests
from datetime import datetime, timedelta
from db import get_db_connection, return_db_connection
from reply_sanitizer import is_safe_to_send
from ghl_api import has_oauth_credentials as _has_ghl_oauth_creds

logger = logging.getLogger(__name__)

GHL_MESSAGES_URL = "https://services.leadconnectorhq.com/conversations/messages"


def _lookup_conversation_id(contact_id: str, location_id: str, access_token: str) -> tuple:
    """
    Look up the GHL conversation ID for a contact via the Conversations Search API.
    Returns (conversation_id, possibly_refreshed_token) tuple.
    conversation_id is a string or None; token may be updated if a 401 refresh occurred.
    """
    _token_refreshed = False
    current_token = access_token
    try:
        resp = requests.get(
            "https://services.leadconnectorhq.com/conversations/search",
            params={"locationId": location_id, "contactId": contact_id},
            headers={
                "Authorization": f"Bearer {current_token}",
                "Version": "2021-04-15",
            },
            timeout=10,
        )

        # Auto-retry on 401/403 with a force-refreshed token
        if resp.status_code in (401, 403) and not _token_refreshed:
            try:
                from ghl_api import get_valid_token_with_status, has_oauth_credentials
                if not has_oauth_credentials():
                    logger.debug(f"Skipping token refresh in conversation lookup — no OAuth creds configured")
                    return None, current_token
                fresh_token, was_refreshed, _err = get_valid_token_with_status(location_id, force_refresh=True)
                if fresh_token and was_refreshed:
                    current_token = fresh_token
                    _token_refreshed = True
                    logger.info(f"Token force-refreshed for {location_id} — retrying conversationId lookup")
                    resp = requests.get(
                        "https://services.leadconnectorhq.com/conversations/search",
                        params={"locationId": location_id, "contactId": contact_id},
                        headers={
                            "Authorization": f"Bearer {current_token}",
                            "Version": "2021-04-15",
                        },
                        timeout=10,
                    )
            except Exception as _refresh_err:
                logger.warning(f"Token refresh failed during conversationId lookup: {_refresh_err}")

        resp.raise_for_status()
        convos = resp.json().get("conversations", [])
        if convos:
            cid = convos[0].get("id")
            if cid:
                logger.info(f"Resolved conversationId={cid} for contact={contact_id}")
                return cid, current_token
    except Exception as e:
        logger.warning(f"Failed to look up conversationId for {contact_id}: {e}")
    return None, current_token


def send_sms_via_ghl(
    contact_id: str,
    message: str,
    access_token: str,
    location_id: str,
    max_retries: int = 3,
    retry_delay: int = 5,
    conversation_id: str = None,
) -> tuple:
    """
    Sends an SMS via GoHighLevel Conversations API.

    Returns 3-tuple: (success, fail_reason, http_detail)
        (True,  None,         None)  — SMS sent successfully
        (True,  'duplicate',  None)  — Already sent recently (dedup)
        (False, 'auth',       {...}) — 401/403 auth failure (token expired/revoked)
        (False, 'rate_limit', {...}) — 429 rate limited after all retries
        (False, 'network',    {...}) — Network/timeout error after all retries
        (False, 'safety',     None)  — Blocked by safety filter
        (False, 'invalid',    None)  — Invalid contact_id or missing token
        (False, 'error',      {...}) — Other/unknown error

    http_detail dict (when present):
        {"status_code": int, "response_body": str, "attempts": int}
    """
    if not contact_id or contact_id == "unknown":
        logger.warning("Cannot send SMS: invalid contact_id")
        return False, 'invalid', None

    if not access_token or not location_id:
        logger.warning(f"Cannot send SMS: missing token or location_id for {contact_id}")
        return False, 'invalid', None

    # Demo mode short-circuit
    if access_token == 'DEMO':
        logger.info(f"DEMO MODE: Simulated SMS send to {contact_id} | msg='{message[:50]}...'")
        return True, None, None

    # SAFETY NET: Block messages contaminated with LLM reasoning/system prompt artifacts
    if not is_safe_to_send(message):
        logger.error(f"MESSAGE BLOCKED BY SAFETY NET for {contact_id} — contains system prompt artifacts")
        return False, 'safety', None

    # Duplicate prevention: check if same message sent in last 5 min
    conn = get_db_connection()
    if conn:
        cur = None
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT 1 FROM contact_messages
                WHERE contact_id = %s
                  AND message_type = 'assistant'
                  AND message_text = %s
                  AND created_at > NOW() - INTERVAL '5 minutes'
                LIMIT 1
            """, (contact_id, message.strip()))
            if cur.fetchone():
                logger.warning(f"SKIP DUPLICATE SMS: same message sent recently to {contact_id}")
                return True, 'duplicate', None  # Treat as success (already sent)
        except Exception as e:
            logger.error(f"Duplicate check failed: {e}")
        finally:
            if cur:
                cur.close()
            return_db_connection(conn)

    # NOTE: We do NOT bail here when OAuth env vars are missing.  The caller's
    # token may still be valid (not yet expired).  If the token IS expired,
    # the 401 handler below (line ~199) bails after a single request instead
    # of retrying 3 times — so the worst-case overhead is one fast HTTP call.

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }

    # Resolve conversationId so the message threads correctly in GHL (green bar).
    # Skip the lookup when OAuth creds are missing — saves a wasted HTTP call
    # with an expired token that can never be refreshed.
    if not conversation_id and _has_ghl_oauth_creds():
        conversation_id, access_token = _lookup_conversation_id(contact_id, location_id, access_token)
        # Update headers if token was refreshed during conversation lookup
        headers["Authorization"] = f"Bearer {access_token}"

    payload = {
        "type": "SMS",
        "contactId": contact_id,
        "message": message.strip(),
    }
    if conversation_id:
        payload["conversationId"] = conversation_id
    else:
        # Fallback: include locationId when conversationId not available
        payload["locationId"] = location_id

    last_failure = 'unknown'
    last_status = 0
    last_body = ''
    total_attempts = 0
    _token_refreshed = False  # Only attempt one token refresh per send call
    for attempt in range(1, max_retries + 1):
        total_attempts = attempt
        try:
            resp = requests.post(GHL_MESSAGES_URL, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()

            logger.info(f"SMS sent successfully to {contact_id} on attempt {attempt}")
            return True, None, None

        except requests.HTTPError as e:
            last_status = e.response.status_code if e.response else 0
            last_body = (e.response.text[:500] if e.response else 'No response')
            logger.warning(f"GHL SMS attempt {attempt} failed (HTTP {last_status}): {last_body}")
            if last_status == 429:  # Rate limit — longer wait
                last_failure = 'rate_limit'
                time_module.sleep(10)
            elif last_status in (401, 403):  # Auth issue — try one force-refresh then abort
                if not _token_refreshed:
                    # Skip refresh attempt if no OAuth credentials are configured
                    if not _has_ghl_oauth_creds():
                        logger.debug(f"Skipping token refresh — no OAuth creds configured for {location_id}")
                        return False, 'auth', {
                            "status_code": last_status,
                            "response_body": last_body,
                            "attempts": attempt,
                        }
                    logger.warning(f"Auth failure (HTTP {last_status}) — force-refreshing token for {location_id}")
                    try:
                        from ghl_api import get_valid_token_with_status
                        fresh_token, was_refreshed, _err = get_valid_token_with_status(location_id, force_refresh=True)
                        if fresh_token and was_refreshed:
                            access_token = fresh_token
                            headers["Authorization"] = f"Bearer {fresh_token}"
                            _token_refreshed = True
                            logger.info(f"Token force-refreshed for {location_id} — retrying SMS send")
                            continue  # retry with fresh token
                    except Exception as _refresh_err:
                        logger.error(f"Token refresh attempt failed for {location_id}: {_refresh_err}")
                logger.error(f"Auth failure (HTTP {last_status}) — no valid token available, aborting")
                return False, 'auth', {
                    "status_code": last_status,
                    "response_body": last_body,
                    "attempts": attempt,
                }
            elif 400 <= last_status < 500:
                # Any other 4xx (400, 404, 409, 422, etc.) — client error,
                # retrying with the same payload won't help
                last_failure = 'client_error'
                logger.warning(f"GHL SMS client error (HTTP {last_status}) for {contact_id} — not retrying: {last_body[:200]}")
                return False, last_failure, {
                    "status_code": last_status,
                    "response_body": last_body,
                    "attempts": attempt,
                }
            else:
                # 5xx server errors — retryable
                last_failure = 'server_error'
        except requests.RequestException as e:
            last_failure = 'network'
            last_body = str(e)[:500]
            logger.warning(f"GHL SMS attempt {attempt} network error: {e}")
        except Exception as e:
            # Catch-all for unexpected exceptions (SSL, encoding, etc.)
            last_failure = 'unexpected'
            last_body = str(e)[:500]
            logger.error(f"GHL SMS attempt {attempt} unexpected error: {e}", exc_info=True)

        if attempt < max_retries:
            time_module.sleep(retry_delay * attempt)  # Exponential backoff feel

    logger.error(f"Failed to send SMS to {contact_id} after {max_retries} attempts ({last_failure})")
    return False, last_failure, {
        "status_code": last_status,
        "response_body": last_body,
        "attempts": total_attempts,
    }
