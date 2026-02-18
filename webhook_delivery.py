# webhook_delivery.py — Enterprise webhook delivery with HMAC signing + retries
#
# When an API subscriber sends a message via /api/v1/chat/completions,
# the bot processes it and delivers the reply to the subscriber's
# configured outbound_webhook_url using this module.

import time
import json
import hmac
import hashlib
import logging
import requests

logger = logging.getLogger("webhook_delivery")

# Retry config — exponential backoff
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds
TIMEOUT = 10  # seconds per attempt


def sign_payload(payload: dict, secret: str) -> str:
    """
    Generate HMAC-SHA256 signature for the payload.
    The subscriber can verify authenticity using their webhook_secret.

    Signature is computed over the raw JSON bytes (compact, sorted keys)
    so both sides produce the same digest.
    """
    if not secret:
        return ""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def deliver_webhook(url: str, payload: dict, secret: str = "",
                    max_retries: int = MAX_RETRIES) -> tuple:
    """
    Deliver a JSON payload to the subscriber's webhook URL.

    Features:
    - HMAC-SHA256 signature in X-Webhook-Signature header
    - Timestamp in X-Webhook-Timestamp for replay protection
    - Exponential backoff retries on network / 5xx failures
    - Timeout protection per request

    Returns:
        (success: bool, status_code: int or None, error: str or None)
    """
    if not url:
        return False, None, "No webhook URL configured"

    timestamp = str(int(time.time()))
    signature = sign_payload(payload, secret)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "GrokBot-Webhook/1.0",
        "X-Webhook-Timestamp": timestamp,
    }
    if signature:
        headers["X-Webhook-Signature"] = signature

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)

    last_error = None
    last_status = None

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                url,
                data=body,
                headers=headers,
                timeout=TIMEOUT,
            )
            last_status = resp.status_code

            if 200 <= resp.status_code < 300:
                logger.info(f"Webhook delivered: {url} -> {resp.status_code}")
                return True, resp.status_code, None

            # 4xx = client error on their end, don't retry (except 429)
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(f"Webhook rejected (no retry): {url} -> {last_error}")
                return False, resp.status_code, last_error

            # 5xx or 429 = transient, retry with backoff
            last_error = f"HTTP {resp.status_code}"
            logger.warning(f"Webhook attempt {attempt + 1}/{max_retries + 1} failed: {url} -> {last_error}")

        except requests.Timeout:
            last_error = f"Timeout after {TIMEOUT}s"
            logger.warning(f"Webhook timeout attempt {attempt + 1}/{max_retries + 1}: {url}")

        except requests.ConnectionError as e:
            last_error = f"Connection error: {str(e)[:100]}"
            logger.warning(f"Webhook connection error attempt {attempt + 1}/{max_retries + 1}: {url}")

        except Exception as e:
            last_error = f"Unexpected error: {str(e)[:100]}"
            logger.error(f"Webhook unexpected error: {url} -> {e}", exc_info=True)
            return False, None, last_error

        # Exponential backoff before retry
        if attempt < max_retries:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.info(f"Retrying webhook in {delay}s...")
            time.sleep(delay)

    logger.error(f"Webhook delivery failed after {max_retries + 1} attempts: {url} -> {last_error}")
    return False, last_status, last_error


def build_api_reply_payload(contact_id: str, reply: str, booking_made: bool = False,
                            metadata: dict = None) -> dict:
    """
    Build the outbound webhook payload for an API-sourced message reply.
    This is the JSON that gets POSTed to the subscriber's outbound_webhook_url.
    """
    payload = {
        "event": "message.reply",
        "contact_id": contact_id,
        "message": reply,
        "role": "assistant",
        "booking_made": booking_made,
        "timestamp": int(time.time()),
    }
    if metadata:
        payload["metadata"] = metadata
    return payload
