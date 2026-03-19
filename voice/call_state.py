"""
voice/call_state.py — Call state, TwiML helpers, and constants.

State is backed by Redis (via redis_state.py) for cross-service sharing
between Flask and FastAPI. The in-memory dicts and daemon reaper have
been removed — Redis TTLs handle cleanup automatically.

For backward compatibility, this module re-exports the Redis functions
and constants so existing code that does `from voice.call_state import active_calls`
can be migrated incrementally.
"""

import json
import os
import logging
import base64
import queue as _queue_module
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_quoteattr

import twilio_provisioning

# Re-export Redis state functions so callers can import from here
from voice.redis_state import (
    set_active_call,
    get_active_call,
    update_active_call,
    delete_active_call,
    get_all_active_calls,
    get_active_calls_for_location,
    set_transfer_request,
    get_transfer_request,
    delete_transfer_request,
    add_overflow_alert,
    get_overflow_alerts,
    dismiss_overflow_alert,
    TERMINAL_STATUSES,
)

logger = logging.getLogger("voice_bridge.call_state")

# ── In-process state (NOT shared via Redis) ──────────────────────────────────

# Live listen: maps call_sid → set of queue.Queue objects (one per listener)
# Audio chunks are put into each queue by the voice stream.
# This MUST stay in-process — audio frames are too high-volume for Redis.
call_listeners: dict = {}  # { call_sid: set(queue.Queue, ...) }

# Simple in-memory cache for GHL custom field definitions.
# Populated on first contact detail fetch per location; rarely changes.
custom_field_defs: dict = {}

# ── Concurrent voice stream limit ────────────────────────────────────────────
# For Flask/gunicorn: threading.Semaphore limits concurrent voice streams.
# For FastAPI: async_stream.py uses asyncio.Semaphore instead.
import threading
MAX_VOICE_STREAMS = int(os.getenv("MAX_VOICE_STREAMS", "30"))
voice_stream_semaphore = threading.Semaphore(MAX_VOICE_STREAMS)


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
