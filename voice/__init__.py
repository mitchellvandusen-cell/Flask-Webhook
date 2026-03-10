# voice/ package — modular voice bridge
#
# This package contains the decomposed voice_bridge.py, split into focused modules.
# The unified Blueprint `voice_bp` is assembled here by registering all sub-blueprints.

from flask import Blueprint

# ── Master blueprint (preserves the /voice/* URL prefix expected by frontend) ──
voice_bp = Blueprint('voice', __name__)

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
