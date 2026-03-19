"""
voice/call_state.py - Redis-backed call state and TwiML helpers.

Call state (active_calls, transfer_requests, overflow_transfer_alerts) is
stored in Redis via voice/redis_state.py so that both Flask and FastAPI
can read/write the same state.

call_listeners stays in-process (WebSocket-to-WebSocket audio relay on
the same machine — no cross-process sharing needed).

custom_field_defs stays in-process (per-location GHL field cache).
"""

import json
import os
import logging
import threading
import base64
import queue as _queue_module
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_quoteattr

import twilio_provisioning

# Re-export Redis state functions so existing imports still work
from voice.redis_state import (
    set_active_call,
    get_active_call,
    update_active_call,
    delete_active_call,
    call_exists,
    get_active_calls_for_location,
    get_all_active_calls,
    set_transfer_request,
    get_transfer_request,
    delete_transfer_request,
    transfer_request_exists,
    add_overflow_alert,
    get_overflow_alerts,
    set_overflow_alerts,
)

logger = logging.getLogger("voice_bridge.call_state")

# ── In-process state (NOT shared via Redis) ──────────────────────────────────

# Live listen: maps call_sid → set of queue.Queue objects (one per listener)
# Audio chunks (mulaw base64 strings) are put into each queue by the voice stream.
# Stays in-process because it's WebSocket-to-WebSocket audio relay on the same machine.
call_listeners: dict = {}  # { call_sid: set(queue.Queue, ...) }

# Simple in-memory cache for GHL custom field definitions: { location_id: {field_id: field_name} }
# Populated on first contact detail fetch per location; GHL field definitions rarely change.
custom_field_defs: dict = {}

# ── Concurrent voice stream limit (backpressure for gunicorn's 40 threads) ──
# Reserve ~10 threads for HTTP traffic; allow max 30 concurrent voice streams.
MAX_VOICE_STREAMS = int(os.getenv("MAX_VOICE_STREAMS", "30"))
voice_stream_semaphore = threading.Semaphore(MAX_VOICE_STREAMS)

# ── Terminal statuses ────────────────────────────────────────────────────────
TERMINAL_STATUSES = frozenset({"completed", "busy", "no-answer", "failed", "canceled", "transferred"})

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
