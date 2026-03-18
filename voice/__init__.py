# voice/ package — modular voice bridge
#
# This package contains the decomposed voice_bridge.py, split into focused modules.
# The unified Blueprint `voice_bp` is assembled here by registering all sub-blueprints.

import logging
from flask import Blueprint, request, jsonify
from flask_login import current_user

logger = logging.getLogger("voice")

# ── Master blueprint (preserves the /voice/* URL prefix expected by frontend) ──
voice_bp = Blueprint('voice', __name__)


# ── SMS Bot tier gate ──
# Routes that sms_bot tier CAN access (contacts, intelligence, SMS sending).
# Everything else (dialer, numbers, A2P, setup, recordings, stats) is blocked.
_SMS_BOT_ALLOWED_PREFIXES = (
    '/voice/contact',           # contact data, send-sms, messages, notes, intelligence
    '/voice/contacts',          # contact list, tags, sync, export
    '/voice/ping',              # health check
)


@voice_bp.before_request
def _gate_sms_bot_tier():
    """Block sms_bot tier from all voice/dialer features.

    SMS Bot users pay $99.98/mo for AI texting only. They should not be able
    to provision Twilio, buy numbers, make calls, register A2P, or access
    any dialer/voice functionality. Only contact data and SMS-related
    endpoints are allowed.
    """
    # Skip for unauthenticated requests (webhooks, TwiML callbacks)
    if not current_user or not current_user.is_authenticated:
        return None

    tier = getattr(current_user, 'subscription_tier', None) or 'individual'
    if tier != 'sms_bot':
        return None

    path = request.path
    if any(path.startswith(prefix) for prefix in _SMS_BOT_ALLOWED_PREFIXES):
        return None

    logger.info(f"SMS Bot tier blocked from voice route: {path} (user={current_user.email})")
    return jsonify({
        "error": "This feature requires a Power Dialer plan or higher.",
        "upgrade_required": True,
        "current_tier": "sms_bot",
    }), 403

# ── Import sub-blueprints ──
from voice.twiml_routes import twiml_bp
from voice.outbound import outbound_bp
from voice.dialer import dialer_bp
from voice.call_history import call_history_bp
from voice.recordings import recordings_bp
from voice.setup import setup_bp
from voice.numbers import numbers_bp
from voice.a2p import a2p_bp
from voice.contacts import contacts_bp
from voice.stats import stats_bp
from voice.intelligence import intelligence_bp

# ── Re-export WebSocket handlers needed by main.py ──
from voice.stream import run_voice_stream, run_listen_stream

# ── Re-export helper used by main.py SSE routes ──
from voice.dialer import _background_contact_sync

# ── Register all sub-blueprints onto the master ──
_sub_blueprints = [
    twiml_bp,
    outbound_bp,
    dialer_bp,
    call_history_bp,
    recordings_bp,
    setup_bp,
    numbers_bp,
    a2p_bp,
    contacts_bp,
    stats_bp,
    intelligence_bp,
]

for bp in _sub_blueprints:
    voice_bp.register_blueprint(bp)
