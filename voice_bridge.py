# voice_bridge.py — Backward-compatibility shim
#
# The voice bridge has been decomposed into the voice/ package.
# This file re-exports the public symbols that main.py and other modules expect.
# All logic now lives in voice/*.py submodules.

from voice import voice_bp, run_voice_stream, run_listen_stream  # noqa: F401
from voice.dialer import _background_contact_sync  # noqa: F401
