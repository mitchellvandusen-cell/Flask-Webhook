"""
voice/call_state.py - In-memory call state, reaper, and TwiML helpers.

Extracted from voice_bridge.py.  All dict names are public (no underscore
prefix) because they live in their own module.
"""

import json
import os
import logging
import threading
import time
import base64
import queue as _queue_module
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_quoteattr

import twilio_provisioning

logger = logging.getLogger("voice_bridge.call_state")

# ── In-memory state dicts ────────────────────────────────────────────────────

# In-memory call status tracking for the dialer queue
# { call_sid: { "status": "...", "duration": 0, "contact_id": "...", "phone": "...", "name": "..." } }
active_calls = {}

# Transfer / takeover signaling: set by HTTP endpoints, read by WebSocket bridge
# { call_sid: {"type": "transfer"|"takeover", "target": "+1...", "reason": "..."} }
transfer_requests = {}

# Live listen: maps call_sid → set of queue.Queue objects (one per listener)
# Audio chunks (mulaw base64 strings) are put into each queue by the voice stream
call_listeners: dict = {}  # { call_sid: set(queue.Queue, ...) }

# Simple in-memory cache for GHL custom field definitions: { location_id: {field_id: field_name} }
# Populated on first contact detail fetch per location; GHL field definitions rarely change.
custom_field_defs: dict = {}

# ── Concurrent voice stream limit (backpressure for gunicorn's 40 threads) ──
# Reserve ~10 threads for HTTP traffic; allow max 30 concurrent voice streams.
MAX_VOICE_STREAMS = int(os.getenv("MAX_VOICE_STREAMS", "30"))
voice_stream_semaphore = threading.Semaphore(MAX_VOICE_STREAMS)

# ── Periodic reaper for stale active_calls / transfer_requests / call_listeners ──
REAPER_INTERVAL = 300   # seconds (5 minutes)
TERMINAL_STATUSES = frozenset({"completed", "busy", "no-answer", "failed", "canceled", "transferred"})

NON_TERMINAL_MAX_AGE = 3600  # 1 hour — reap non-terminal entries stuck this long


def _reap_stale_calls():
    """Remove entries stuck in a terminal state for more than 5 minutes,
    and entries stuck in non-terminal states for more than 1 hour."""
    while True:
        time.sleep(REAPER_INTERVAL)
        try:
            now = time.monotonic()
            stale = []
            for sid, info in list(active_calls.items()):
                if info.get("status") in TERMINAL_STATUSES:
                    # Tag first-seen monotonic time so we know how long it's been terminal
                    if "_terminal_since" not in info:
                        info["_terminal_since"] = now
                    elif now - info["_terminal_since"] > REAPER_INTERVAL:
                        stale.append(sid)
                else:
                    # Non-terminal entries (initiated/ringing/in-progress) stuck too long
                    if "_created_at" not in info:
                        info["_created_at"] = now
                    elif now - info["_created_at"] > NON_TERMINAL_MAX_AGE:
                        logger.warning(f"Reaping non-terminal call {sid[:16]} stuck in '{info.get('status')}' for >1hr")
                        stale.append(sid)
            for sid in stale:
                active_calls.pop(sid, None)
                transfer_requests.pop(sid, None)
                call_listeners.pop(sid, None)
            if stale:
                logger.debug(f"Reaped {len(stale)} stale call entries from active_calls")
        except Exception:
            pass  # reaper must never crash


_reaper_thread = threading.Thread(target=_reap_stale_calls, daemon=True)
_reaper_thread.start()


# ── TwiML helpers ─────────────────────────────────────────────────────────────

def _twilio_hangup(call_sid: str, sub_account_sid: str) -> bool:
    """Hang up a call via Twilio REST API."""
    return twilio_provisioning.hangup_call(sub_account_sid, call_sid)


def _twilio_transfer(call_sid: str, sub_account_sid: str, transfer_to: str, webhook_base_url: str) -> bool:
    """Transfer a call via Twilio REST API (redirect to transfer TwiML)."""
    return twilio_provisioning.transfer_call(sub_account_sid, call_sid, transfer_to, webhook_base_url)


def _encode_client_state(data: dict) -> str:
    """Base64-encode a dict for passing as custom parameters."""
    return base64.b64encode(json.dumps(data).encode()).decode()


def _decode_client_state(s: str) -> dict:
    """Decode a base64 client_state string back to a dict."""
    try:
        return json.loads(base64.b64decode(s.encode()).decode())
    except Exception:
        return {}


def _build_twiml_stream(stream_url: str, params: dict) -> str:
    """
    Build a TwiML Response that opens a bidirectional mulaw 8kHz media stream.
    All values are XML-escaped to prevent injection.
    """
    param_xml = ''.join(
        f'<Parameter name={xml_quoteattr(str(k))} value={xml_quoteattr(str(v))}/>' for k, v in params.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
          '<Connect>'
            f'<Stream url={xml_quoteattr(stream_url)}>'
              f'{param_xml}'
            '</Stream>'
          '</Connect>'
        '</Response>'
    )
