import json
import os
import logging
import struct
import base64
import asyncio
import audioop

import numpy as np
import soxr
import scipy.signal
import websockets

# Async wrappers for CPU-bound audio DSP — used by FastAPI voice server
# to offload transcoding to the thread pool, keeping the event loop free.

logger = logging.getLogger("voice_bridge.audio")

# XAI Realtime API
XAI_REALTIME_URL = "wss://api.x.ai/v1/realtime"
XAI_API_KEY = os.getenv("XAI_API_KEY", "")

# Voice options: official XAI Grok Voice Agent voices
VOICE_OPTIONS = {
    "ara": "Ara",
    "eve": "Eve",
    "leo": "Leo",
    "rex": "Rex",
    "sal": "Sal",
    "mika": "Mika",
    "vale": "Vale",
}

# Default voice
DEFAULT_VOICE = "Ara"

# Audio transcoding: Twilio mulaw 8kHz <-> xAI PCM 16kHz
# Twilio Media Streams send mulaw-encoded audio at 8000 Hz.
# xAI Realtime API expects/produces PCM 16-bit at 16000 Hz.
TWILIO_SAMPLE_RATE = 8000   # Twilio Media Streams
XAI_SAMPLE_RATE = 16000     # xAI Realtime API

# Master Twilio credentials (from .env) -- white-label
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

# ──────────────────────────────────────────────────────────────
# SOFTWARE VOICEMAIL DETECTION (transcription-based)
# Catches voicemail MUCH faster than Twilio AMD by analyzing
# xAI's real-time transcriptions. Twilio AMD with DetectMessageEnd
# waits for the entire greeting to finish → AI talks to voicemail
# for 30+ seconds. This fires on the FIRST voicemail phrase.
# ──────────────────────────────────────────────────────────────
_VM_PHRASES = [
    "at the tone",
    "leave a message",
    "leave your message",
    "record your message",
    "after the beep",
    "leave your name",
    "not available right now",
    "is not available",
    "can't come to the phone",
    "cannot come to the phone",
    "unable to take your call",
    "reached the voicemail",
    "reached the voice mail",
    "voicemail box",
    "mailbox is full",
    "please leave a detailed message",
    "press pound when finished",
    "when you have finished recording",
    "when you've finished recording",
    "not here right now",
    "please try again later",
    "leave a brief message",
]

# Events to log from XAI
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'session.updated'
]

# Pre-compute Butterworth low-pass filter coefficients (phone-line warmth EQ).
# Rolling off above 2800 Hz adds phone-line warmth and lowers perceived pitch
# by removing the bright, synthetic shimmer that screams "AI" to the human ear.
_WARMTH_B, _WARMTH_A = scipy.signal.butter(N=4, Wn=2800, fs=XAI_SAMPLE_RATE, btype='low')


def _is_voicemail_phrase(text: str) -> bool:
    """Check if transcribed text contains definitive voicemail indicators."""
    if not text:
        return False
    lower = text.lower()
    return any(phrase in lower for phrase in _VM_PHRASES)


def _mulaw_to_pcm16(mulaw_bytes: bytes) -> bytes:
    """Convert mulaw 8kHz audio (from Twilio) to PCM16 16kHz (for xAI)."""
    # 1. mulaw -> PCM16 at 8kHz
    pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
    # 2. Anti-aliased resample 8kHz -> 16kHz via soxr (high-quality sinc interpolation)
    samples_8k = np.frombuffer(pcm_8k, dtype=np.int16).astype(np.float32)
    samples_16k = soxr.resample(samples_8k, TWILIO_SAMPLE_RATE, XAI_SAMPLE_RATE)
    return np.int16(np.clip(samples_16k, -32768, 32767)).tobytes()


def _pcm16_to_mulaw(pcm16_bytes: bytes) -> bytes:
    """Convert PCM16 16kHz audio (from xAI) to mulaw 8kHz (for Twilio).

    Pipeline: low-pass EQ → anti-aliased downsample → u-law encode.
    This eliminates the metallic/tinny high-pitched robotic sound caused by
    naive linear-interpolation downsampling (audioop.ratecv aliasing).
    """
    samples = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float64)

    # 1. Low-pass filter: cut harsh AI brightness above 2800 Hz
    filtered = scipy.signal.lfilter(_WARMTH_B, _WARMTH_A, samples)

    # 2. Anti-aliased downsample 16kHz → 8kHz via soxr (polyphase sinc)
    downsampled = soxr.resample(filtered.astype(np.float32), XAI_SAMPLE_RATE, TWILIO_SAMPLE_RATE)

    # 3. PCM16 → u-law
    return audioop.lin2ulaw(np.int16(np.clip(downsampled, -32768, 32767)).tobytes(), 2)


def _pcm16_to_wav(pcm_data, sample_rate=16000):
    """Wrap raw PCM16 bytes in a WAV container for browser playback."""
    data_size = len(pcm_data)
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, 1, sample_rate,
        sample_rate * 2, 2, 16,
        b'data', data_size
    )
    return header + pcm_data


async def _generate_voice_preview(voice_name):
    """Connect to XAI Realtime API and generate a short voice sample.
    Uses L16 PCM 16kHz — same format as live calls for consistent audio quality."""
    audio_chunks = []

    try:
        async with websockets.connect(
            XAI_REALTIME_URL,
            additional_headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            close_timeout=5,
        ) as ws:
            session_config = {
                "type": "session.update",
                "session": {
                    "voice": voice_name,
                    "instructions": "You are a friendly voice assistant. Say exactly what is requested, nothing more.",
                    "audio": {
                        "output": {"format": {"type": "audio/pcm", "rate": 16000}},
                    },
                }
            }
            await ws.send(json.dumps(session_config))

            item = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": "Say exactly this: 'Hi there! I'm your AI voice assistant. I can help with life insurance questions and booking appointments. How can I help you today?'"
                    }]
                }
            }
            await ws.send(json.dumps(item))
            await ws.send(json.dumps({"type": "response.create"}))

            # Collect audio response with deadline
            deadline = asyncio.get_running_loop().time() + 15
            async for message in ws:
                if asyncio.get_running_loop().time() > deadline:
                    break
                data = json.loads(message)
                event_type = data.get('type', '')

                if event_type in ('response.audio.delta', 'response.output_audio.delta'):
                    if 'delta' in data:
                        audio_chunks.append(base64.b64decode(data['delta']))
                elif event_type == 'response.done':
                    break
                elif event_type == 'error':
                    err = data.get('error', data)
                    logger.error(f"Voice preview XAI error: code={err.get('code')} msg={err.get('message')} full={data}")
                    break
                elif event_type == 'session.updated':
                    logger.info(f"Voice preview session updated: {data.get('session', {}).get('audio', {})}")

    except Exception as e:
        logger.error(f"Voice preview generation failed: {e}")
        return None

    return b''.join(audio_chunks) if audio_chunks else None


# ── Async wrappers for FastAPI voice server ──────────────────────────────────

async def async_mulaw_to_pcm16(mulaw_bytes: bytes) -> bytes:
    """Async wrapper — offloads CPU-bound resampling to thread pool."""
    return await asyncio.to_thread(_mulaw_to_pcm16, mulaw_bytes)


async def async_pcm16_to_mulaw(pcm16_bytes: bytes) -> bytes:
    """Async wrapper — offloads CPU-bound filter + resampling to thread pool."""
    return await asyncio.to_thread(_pcm16_to_mulaw, pcm16_bytes)
