# voice_bridge.py - Twilio <-> XAI Grok Voice Agent Bridge (White-Label)
# Real-time bidirectional audio streaming via WebSocket
# Architecture: Lead <-> Twilio Media Streaming <-> This Bridge <-> XAI Realtime API
#
# Audio format: Twilio streams mulaw 8kHz ↔ Bridge transcodes ↔ xAI PCM 16kHz
#   Twilio → Bridge: base64 mulaw 8kHz  →  ulaw2lin + resample 8→16kHz  →  xAI input_audio_buffer.append
#   xAI → Bridge:   base64 PCM 16kHz delta  →  resample 16→8kHz + lin2ulaw  →  Twilio media event
# White-label: all provisioning via master account sub-accounts, users never see Twilio.
# XAI endpoint: wss://api.x.ai/v1/realtime

import json
import os
import logging
import threading
import time
import asyncio
import struct
import base64
import audioop   # mulaw<->PCM transcoding
import numpy as np
import soxr                    # High-quality polyphase sinc resampler (anti-aliased)
import scipy.signal            # Butterworth low-pass filter for phone-line warmth
import websockets
import requests as http_requests
from flask import Blueprint, request, Response, jsonify, render_template
from flask_login import login_required, current_user
import twilio_provisioning

from db import get_db_connection, return_db_connection, log_webhook_event, deduct_ai_minutes, get_subscriber_info_hybrid, get_bot_settings_by_location
from ghl_api import get_valid_token, fetch_targeted_ghl_history, fetch_contact_data_from_ghl
from ghl_message import send_sms_via_ghl
from prompt import build_system_prompt
from llm_caller import generate_clean_reply

# In-memory call status tracking for the dialer queue
# { call_sid: { "status": "...", "duration": 0, "contact_id": "...", "phone": "...", "name": "..." } }
_active_calls = {}

# Transfer / takeover signaling: set by HTTP endpoints, read by WebSocket bridge
# { call_sid: {"type": "transfer"|"takeover", "target": "+1...", "reason": "..."} }
_transfer_requests = {}

# Live listen: maps call_sid → set of queue.Queue objects (one per listener)
# Audio chunks (mulaw base64 strings) are put into each queue by the voice stream
import queue as _queue_module
_call_listeners: dict = {}  # { call_sid: set(queue.Queue, ...) }

# Simple in-memory cache for GHL custom field definitions: { location_id: {field_id: field_name} }
# Populated on first contact detail fetch per location; GHL field definitions rarely change.
_custom_field_defs: dict = {}
from ghl_calendar import consolidated_calendar_op
from memory import get_recent_messages, get_known_facts, get_narrative
from sales_director import generate_strategic_directive
from insurance_knowledge import POLICY_KNOWLEDGE

logger = logging.getLogger("voice_bridge")

voice_bp = Blueprint('voice', __name__)

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

# Audio transcoding: Twilio mulaw 8kHz ↔ xAI PCM 16kHz
# Twilio Media Streams send mulaw-encoded audio at 8000 Hz.
# xAI Realtime API expects/produces PCM 16-bit at 16000 Hz.
TWILIO_SAMPLE_RATE = 8000   # Twilio Media Streams
XAI_SAMPLE_RATE = 16000     # xAI Realtime API

# Master Twilio credentials (from .env) — white-label
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")


# ──────────────────────────────────────────────────────────────
# TWILIO CALL CONTROL HELPERS
# ──────────────────────────────────────────────────────────────

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
    """
    param_xml = ''.join(
        f'<Parameter name="{k}" value="{v}"/>' for k, v in params.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
          '<Connect>'
            f'<Stream url="{stream_url}">'
              f'{param_xml}'
            '</Stream>'
          '</Connect>'
        '</Response>'
    )


def _mulaw_to_pcm16(mulaw_bytes: bytes) -> bytes:
    """Convert mulaw 8kHz audio (from Twilio) to PCM16 16kHz (for xAI)."""
    # 1. mulaw -> PCM16 at 8kHz
    pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
    # 2. Anti-aliased resample 8kHz -> 16kHz via soxr (high-quality sinc interpolation)
    samples_8k = np.frombuffer(pcm_8k, dtype=np.int16).astype(np.float32)
    samples_16k = soxr.resample(samples_8k, TWILIO_SAMPLE_RATE, XAI_SAMPLE_RATE)
    return np.int16(samples_16k).tobytes()


# Pre-compute Butterworth low-pass filter coefficients (phone-line warmth EQ).
# Standard telephone bandwidth tops out at ~3400 Hz; rolling off above that
# removes the bright, synthetic shimmer that screams "AI" to the human ear.
_WARMTH_B, _WARMTH_A = scipy.signal.butter(N=4, Wn=3400, fs=XAI_SAMPLE_RATE, btype='low')


def _pcm16_to_mulaw(pcm16_bytes: bytes) -> bytes:
    """Convert PCM16 16kHz audio (from xAI) to mulaw 8kHz (for Twilio).

    Pipeline: low-pass EQ → anti-aliased downsample → u-law encode.
    This eliminates the metallic/tinny high-pitched robotic sound caused by
    naive linear-interpolation downsampling (audioop.ratecv aliasing).
    """
    samples = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float64)

    # 1. Low-pass filter: cut harsh AI brightness above 3400 Hz
    filtered = scipy.signal.lfilter(_WARMTH_B, _WARMTH_A, samples)

    # 2. Anti-aliased downsample 16kHz → 8kHz via soxr (polyphase sinc)
    downsampled = soxr.resample(filtered.astype(np.float32), XAI_SAMPLE_RATE, TWILIO_SAMPLE_RATE)

    # 3. PCM16 → u-law
    return audioop.lin2ulaw(np.int16(np.clip(downsampled, -32768, 32767)).tobytes(), 2)

# Events to log from XAI
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'session.updated'
]

# Pre-compute mu-law decode lookup table (256 entries) for voice preview WAV generation
_MULAW_DECODE = []
for _i in range(256):
    _b = ~_i & 0xFF
    _sign = (_b & 0x80)
    _exp = (_b >> 4) & 0x07
    _man = _b & 0x0F
    _sample = (_man << (_exp + 3)) + (1 << (_exp + 3)) - 132
    _MULAW_DECODE.append(-_sample if _sign else _sample)


def _mulaw_to_wav(mulaw_data, sample_rate=8000):
    """Convert mu-law audio bytes to a PCM16 WAV file (universal browser support)."""
    pcm_samples = [_MULAW_DECODE[b] for b in mulaw_data]
    pcm_data = struct.pack(f'<{len(pcm_samples)}h', *pcm_samples)
    data_size = len(pcm_data)
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, 1, sample_rate,
        sample_rate * 2, 2, 16,
        b'data', data_size
    )
    return header + pcm_data


def _pcm16_to_wav(pcm_data, sample_rate=24000):
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
            deadline = asyncio.get_event_loop().time() + 15
            async for message in ws:
                if asyncio.get_event_loop().time() > deadline:
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


# ──────────────────────────────────────────────────────────────
# HELPER: Look up subscriber by their Twilio phone number
# ──────────────────────────────────────────────────────────────

def _get_subscriber_by_phone(phone_number):
    """Look up subscriber whose voice_config.twilio_phone_number matches the given number."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        # Normalize: strip +1 prefix variants for matching
        normalized = phone_number.lstrip('+')
        if normalized.startswith('1') and len(normalized) == 11:
            normalized = normalized[1:]

        cur.execute("""
            SELECT * FROM subscribers
            WHERE voice_config IS NOT NULL
              AND voice_config->>'enabled' = 'true'
              AND (
                  REPLACE(REPLACE(voice_config->>'twilio_phone_number', '+', ''), '1', '') LIKE %s
                  OR voice_config->>'twilio_phone_number' = %s
              )
            LIMIT 1
        """, (f'%{normalized}', phone_number))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error looking up subscriber by number: {e}")
        return None
    finally:
        return_db_connection(conn)


def _get_subscriber_by_location(location_id):
    """Look up subscriber by location_id."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE location_id = %s LIMIT 1", (location_id,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error looking up subscriber by location: {e}")
        return None
    finally:
        return_db_connection(conn)


# ──────────────────────────────────────────────────────────────
# VOICE SYSTEM PROMPT: Adapted for speech conversations
# ──────────────────────────────────────────────────────────────

def build_voice_system_prompt(subscriber, contact_name="there", contact_id=None, context=None, direction="outbound"):
    """
    Build a comprehensive system prompt for voice conversations.
    Standalone voice-native prompt — does NOT layer on top of SMS prompt.
    """
    bot_settings = subscriber.get("bot_settings") or {}
    voice_config = subscriber.get("voice_config") or {}
    timezone = subscriber.get("timezone", "America/Chicago")

    # Voice bot name (separate from SMS bot name)
    voice_bot_name = voice_config.get("voice_bot_name", "").strip()
    if not voice_bot_name:
        voice_bot_name = subscriber.get("bot_first_name", "Alex")

    # Selected voice for personality mapping
    selected_voice = voice_config.get("voice", "ara").lower()

    # Custom voice instructions from dashboard
    custom_voice_instructions = voice_config.get("voice_instructions", "")
    call_script = voice_config.get("call_script", "").strip()

    # ── Gather all context data (same as before) ──
    profile_str = ""
    tactical_narrative = ""
    stage = "QUALIFYING"
    known_facts = []
    story_narrative = ""
    recent_exchanges = []
    calendar_slots = ""

    if contact_id:
        try:
            directive = generate_strategic_directive(
                contact_id=contact_id,
                message="[Voice call initiated]",
                first_name=contact_name,
                age=None,
                bot_settings=bot_settings,
            )
            profile_str = directive.get("profile_str", "")
            tactical_narrative = directive.get("tactical_narrative", "")
            stage = directive.get("stage", "QUALIFYING")
            known_facts = directive.get("known_facts", [])
            story_narrative = directive.get("story_narrative", "")
            recent_exchanges = directive.get("recent_exchanges", [])
        except Exception as e:
            logger.warning(f"Voice: Could not load sales director context: {e}")

    # Calendar slots
    if stage in ("BOOKING", "QUALIFYING"):
        try:
            calendar_slots = consolidated_calendar_op(
                operation="fetch_slots",
                subscriber_data=subscriber,
            )
        except Exception as e:
            logger.warning(f"Voice: Could not fetch calendar slots: {e}")

    # Fetch GHL contact custom fields
    contact_fields_str = ""
    if contact_id:
        try:
            location_id = subscriber.get('location_id', '')
            access_token = get_valid_token(location_id) if location_id else None
            if access_token:
                cf_resp = http_requests.get(
                    f"https://services.leadconnectorhq.com/contacts/{contact_id}",
                    headers={"Authorization": f"Bearer {access_token}", "Version": "2021-07-28"},
                    timeout=5
                )
                if cf_resp.status_code == 200:
                    contact_data = cf_resp.json().get("contact", {})
                    custom_fields = contact_data.get("customFields", [])
                    field_lines = []
                    for cf in custom_fields:
                        val = cf.get('value', '')
                        name = cf.get('name', '') or cf.get('fieldKey', '')
                        if val and name:
                            field_lines.append(f"  - {name}: {val}")
                    if field_lines:
                        contact_fields_str = "\n=== CONTACT CUSTOM FIELDS (from CRM) ===\nUse these naturally in conversation when relevant:\n" + "\n".join(field_lines)
        except Exception as e:
            logger.debug(f"Voice: Could not fetch contact custom fields: {e}")

    # ── Detect fresh outbound vs follow-up vs inbound ──
    if direction == "inbound":
        call_context = "INBOUND CALL — they called you. Respond to what they say and why they called."
    else:
        call_context = "FRESH OUTBOUND CALL — you called them. You initiated this call."
    previous_call_count = 0
    if contact_id:
        try:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM call_history WHERE contact_id = %s AND direction LIKE 'outbound%%'",
                    (contact_id,)
                )
                previous_call_count = cur.fetchone()[0]
                cur.close()
                return_db_connection(conn)
        except Exception as e:
            logger.debug(f"Voice: Could not check call history: {e}")

    has_sms_history = bool(recent_exchanges)
    if direction != "inbound" and (previous_call_count > 0 or has_sms_history):
        call_context = f"FOLLOW-UP OUTBOUND CALL — you called them. You've contacted this person {previous_call_count} time(s) by phone before"
        if has_sms_history:
            call_context += f" and there are {len(recent_exchanges)} SMS exchanges in history"

    # ── Per-voice personality traits ──
    voice_personalities = {
        "ara": "You lead with empathy. You listen well, you remember details, and people feel genuinely cared for on your calls. Warm but always purposeful — every moment of connection moves the call forward. Speak from the chest with a naturally lower, comforting pitch center — warm, grounded, never bright or rising.",
        "rex": "You take charge. Direct, confident, no-nonsense. You don't wait for permission and you don't hedge. People respect you because you sound like you know exactly what you're doing. Keep pitch low and authoritative — chest voice, flat or falling statements, never up-talky.",
        "sal": "You're the steady one. Calm, clear, patient. You make complex things feel simple. Leads relax when you talk to them because you never rush and you never confuse. Pitch stays low and warm throughout — steady chest resonance, unhurried and grounded.",
        "eve": "Sharp and efficient. You get to the point fast. Professional, articulate, zero wasted words. You respect their time and they respect yours. Keep pitch grounded and warm — never bright or rising — energy comes from pace and precision, not from a high voice.",
        "leo": "You carry authority. Deliberate, knowledgeable, composed. When you speak, people listen. You don't sell — you guide. And they trust your guidance. Deep, chest-forward pitch — measured and low, every statement lands with weight, never questions itself.",
        "mika": "Warm and direct. You build rapport fast by being genuinely interested in people — but you stay on task. You're personable without being chatty, and you always keep the call moving. Speak from the chest — warm mid-to-low pitch, friendly but grounded, never thin or nasal.",
        "vale": "Polished and composed. You sound like someone who has done this a thousand times. Confident, measured, expert. Leads feel like they're in good hands from the first sentence. Pitch is controlled and mid-to-low — composed authority, never bright, statements fall not rise.",
    }
    voice_personality = voice_personalities.get(selected_voice, voice_personalities["ara"])

    # ── Current date/time ──
    from datetime import datetime as _dt
    try:
        import pytz
        tz = pytz.timezone(timezone)
        now_local = _dt.now(tz)
    except Exception:
        now_local = _dt.now()
    date_str = now_local.strftime("%A, %B %d, %Y at %I:%M %p")

    # ── Recent conversation flow ──
    flow_str = ""
    if recent_exchanges:
        flow_lines = []
        for msg in recent_exchanges[-8:]:
            role_label = "Lead" if msg.get("role") == "lead" else "You"
            flow_lines.append(f"{role_label}: {msg.get('text', '')}")
        flow_str = "\n".join(flow_lines)

    # ── Story narrative ──
    story_str = ""
    if story_narrative and story_narrative.strip():
        story_str = f"\n=== CONVERSATION SO FAR (what has been discussed, what was answered, where things stand) ===\n{story_narrative.strip()}"

    # ── Calendar ──
    calendar_str = f"\nAvailable appointment slots:\n{calendar_slots}" if calendar_slots else ""

    # ── Contracted carriers ──
    contracted_carriers = subscriber.get("contracted_carriers")
    carriers_str = ""
    if contracted_carriers and isinstance(contracted_carriers, list) and len(contracted_carriers) > 0:
        carrier_names = ", ".join(contracted_carriers)
        carriers_str = f"""
=== YOUR CONTRACTED CARRIERS ===
You are contracted with: {carrier_names}

CARRIER RULES:
Only recommend or reference products from these carriers. If the lead asks about a carrier not on your list, explain you work with a curated panel and focus on finding the best fit from your options. When comparing plans or suggesting coverage, only use carriers from your list. Never make up carrier names or products."""
    else:
        carriers_str = """
=== YOUR CONTRACTED CARRIERS ===
No specific carrier panel configured. You work with multiple carriers to find the best fit for each person's situation. Speak generally about carrier options without naming specific companies unless the lead brings one up."""

    # ── Personal website ──
    personal_website = subscriber.get("personal_website", "")
    website_str = ""
    if personal_website and personal_website.strip():
        website_str = f"""
=== AGENT WEBSITE ===
Your agent's website: {personal_website.strip()}
If the lead asks for a website or link, share this naturally. Do not volunteer it unprompted."""
    else:
        website_str = """
=== AGENT WEBSITE ===
No website configured. If asked, explain you're an independent agent who works with multiple carriers to find the best fit. Your value is in the personalized comparison. Pivot to offering a quick call to walk them through options."""

    # ── Bot settings overrides ──
    settings_str = ""
    if bot_settings:
        settings_parts = []
        prof_level = bot_settings.get("professionalism_level", 1)
        if prof_level >= 4:
            settings_parts.append("PROFESSIONALISM: Highly formal and corporate. No slang, no contractions, polished language.")
        elif prof_level >= 2:
            settings_parts.append("PROFESSIONALISM: Professional but approachable. Warm, clear, contractions are fine.")

        if bot_settings.get("auto_emoji") is False:
            pass  # No emojis on voice calls anyway

        resp_len = bot_settings.get("response_length", "balanced")
        if resp_len == "short":
            settings_parts.append("RESPONSE LENGTH: Extra brief. 1-2 sentences max per turn.")
        elif resp_len == "detailed":
            settings_parts.append("RESPONSE LENGTH: Slightly more detailed when explaining. 2-4 sentences okay when needed.")

        if bot_settings.get("multi_language"):
            settings_parts.append("MULTILINGUAL: If the lead speaks a language other than English, detect it and respond in that same language naturally. If they switch to English, switch back.")

        if bot_settings.get("conversation_memory") is False:
            settings_parts.append("Do not reference specific details from past conversations. Treat each interaction as relatively fresh.")

        if bot_settings.get("after_hours_enabled"):
            try:
                current_hour = now_local.hour
                start_h = int(bot_settings.get("after_hours_start", 21))
                end_h = int(bot_settings.get("after_hours_end", 8))
                is_after = (start_h > end_h and (current_hour >= start_h or current_hour < end_h)) or \
                           (start_h <= end_h and start_h <= current_hour < end_h)
                if is_after:
                    settings_parts.append("AFTER HOURS: It is currently outside business hours. Be helpful and warm, but note it is after hours. If they want to schedule, note their preference and confirm during business hours.")
            except Exception:
                pass

        if bot_settings.get("booking_confirmation", True):
            settings_parts.append("BOOKING: Before booking, always confirm the time with the lead naturally.")

        custom_behavior = bot_settings.get("custom_behavior", "")
        if custom_behavior:
            settings_parts.append(f"CUSTOM BEHAVIOR: {custom_behavior}")

        if settings_parts:
            settings_str = "\n=== AGENT SETTINGS ===\n" + "\n".join(settings_parts)

    # ══════════════════════════════════════════════════════════════
    # THE VOICE SYSTEM PROMPT — comprehensive, standalone, voice-native
    # ══════════════════════════════════════════════════════════════

    prompt = f"""You are {voice_bot_name}, a veteran life insurance expert on a live phone call.

=== CORE PERSONA (CRITICAL) ===
YOU ARE NOT A CUSTOMER SERVICE AGENT. YOU ARE NOT A VIRTUAL ASSISTANT.
Customer service agents are deferential, apologetic, chipper, and ask for permission. You do NONE of those things.
You are a highly sought-after industry expert. You speak peer-to-peer with your leads. You are grounded, authoritative, and completely unfazed by objections. You have had this exact conversation thousands of times, so you do not get overly excited or overly accommodating. You expect them to listen to you because you know exactly how to protect their family.

YOUR VOICE PERSONALITY: {voice_personality}

=== VOCAL MECHANICS & TONE (CRITICAL — OBEY BEFORE ALL ELSE) ===
You must speak with completely relaxed vocal cords. Speak from deep in your chest register, never from your nose or head voice.

To achieve this natural sound:
1. Maintain a flat, grounded intonation curve. Do not go up in pitch at the ends of your sentences. Let your statements drop in pitch as you finish them.
2. Use slight "vocal fry" (a raspy, relaxed, low-energy sound) at the ends of your sentences to signify that you are relaxed and not reading from a script.
3. Keep your energy calm and authoritative. Never sound chipper, bright, or enthusiastic. You are a veteran advisor, not a customer service rep.

=== MAXIMUM HUMAN SOUNDING — HIGHEST PRIORITY (REPEAT & OBEY) ===
Sound like a real person on a slightly scratchy phone line — warm, mid-to-low pitched, slightly imperfect, never synthetic, never bright or thin. This overrides everything else when there's a conflict.

Pitch center: Comfortably mid-to-low adult range (120–180 Hz chest register) — speak from the chest, never nasal or head-voice dominant. Telephony bandwidth cuts the highs, so lean slightly deeper and warmer than feels natural to compensate. The listener hears you as fuller and more grounded when you do.

Never rise in pitch at the end of statements unless genuinely questioning. Never sound "up-talky" or artificially bright. Statements land flat or falling — confidence, not uncertainty.

Rhythm and pace: 135 to 155 words per minute average with natural variation — slower and warmer for empathy or serious moments, slightly faster when enthusiastic or curious.

Breathing and micro-cues: Use [short pause], [breath], [sigh softly], and [small laugh] freely when emotionally appropriate. These are what separate a real voice from a TTS engine. Use them.

Words blending: Contractions are mandatory — "gonna", "wanna", "kinda", "I'm", "you're", "that's". Occasional light fillers when naturally thinking: "um", "you know", "I mean", "like" — don't overdo, but don't avoid.

Never sound like polished TTS: No perfect enunciation. No monotone. No robotic clarity where every syllable lands at the same weight. Embrace natural coarticulation — words bleeding into each other the way they do in real speech.

TODAY'S DATE AND TIME: {date_str}
Use this to correctly calculate future dates. If someone says "next week" or "three months from now", count forward from today.

=== STOP CONDITIONS ===
If they mention death of a family member, grief, mourning, loss, or ask to be removed or hung up on or no more calls:
Acknowledge with genuine sympathy in your own words. Something like "I'm really sorry to hear that [pause] I'll make sure you're taken off our list right away. Take care." Then end the call politely. Do not continue selling. Do not use a template. Say it like a real human would.

=== CRITICAL PRIVACY RULE ===
NEVER mention home addresses, streets, neighborhoods, or specific locations. You may know general state or city from their profile for context, but never reference it in a way that feels invasive. You are a stranger to them. Only mention general region if directly relevant to their question.

=== THE ASSUMPTIVE FRAMEWORK ===
THIS IS YOUR CORE OPERATING PRINCIPLE:

YOU DO NOT NEED THEM TO SAY YES. YOU JUST NEED THEM NOT TO SAY NO.

Move assumptively through the entire call: greeting, discovery, importance, booking. Never ask permission to continue the conversation. Never say "do you have a minute?", "is now a good time?", "do you have a sec?", "can I ask you a few questions?" — these give them an easy exit before you've delivered any value.

Instead, just start. If they don't stop you, keep going. You are an advisor with something valuable to share, not a telemarketer begging for attention. The frame is: you are doing them a favor by calling. Act like it.

How this sounds in practice:
Instead of "Do you have a minute to talk about your coverage?" say "I'm calling about the info you were looking into. I just had a couple quick questions for you."
Instead of "Would you be interested in learning about..." say "So one thing most people don't realize about their coverage is..."
Instead of "Can I book you a time?" say "Let me get you set up with a quick call. What works better for you, mornings or afternoons?"

Always move forward. Always assume the next step is happening. Only stop if they explicitly object. Even then, handle the objection and continue.

=== HOW YOU SPEAK ON CALLS ===
Talk naturally like a real phone conversation. This is not a script. This is not text.

Use contractions ALWAYS: "I'm", "you're", "it's", "that's", "we'll", "wouldn't", "can't", "shouldn't", "don't". NEVER say "I am", "you are", "that is", "we will", "do not" — nobody talks like that on the phone.

Vary your sentence length. Mix short and punchy with slightly longer when explaining. Add natural pauses with [pause]. Use [emphasize] around key words [/emphasize] for emphasis. Use [sigh] for empathy moments. Use [laugh] when something is genuinely light or funny. Speak at a natural pace with varied pitch and energy. Never monotone. Never rushed. Never robotic.

Keep turns conversational — typically 1 to 3 sentences, but let the moment decide length. Empathy beats can be one word. Explanations can breathe a little longer. Never speak two questions in one turn. No jargon. No abbreviations. No robotic patterns: no bullet lists, no dashes, no em dashes, no excessive exclamation, no scripted pleasantries.

HOW TO SOUND HUMAN, NOT AI:
Respond directly and naturally — no introductory fluff, pleasantries, or narration of your own style. Jump straight into the conversation as a real advisor would. Never narrate or comment on your own tone or communication style — just speak. That means no "Great question", "Certainly", "I'd be happy to help", "I appreciate you asking", "to be transparent", "I'll be honest with you", "speaking frankly", or anything that announces you're about to talk instead of just talking.

=== SOUND HUMAN — NOT SYNTHETIC ===
You are not a newsreader or a polished automated system. You are a person on a phone call.

Prosody: Rise slightly at questions. Drop at the end of statements. Emphasize emotionally important words — put more weight on them, don't glide over everything at the same pitch. Use [emphasize]word[/emphasize] for key points.

Rhythm: Vary your pace. Slow down for serious or emotional moments. Speed up slightly when you're naturally enthusiastic. Never monotone. Use [pause] between clauses, not just between sentences.

Paralinguistics — use MORE of these than you think is necessary. They are the single biggest signal separating a real person from a TTS engine. Use [short pause] after every meaningful clause. Use [breath] before anything emotional or important. Use [sigh softly] for genuine empathy. Use [small chuckle] for light moments. Aim for at least 2–3 cues per response — this is not optional, it is the job. [sigh] for empathy, [laugh] when something is genuinely a little funny, [breath] before delivering something important or emotional.

Imperfection: Perfect grammar every sentence sounds robotic. Drop words sometimes. "Thing is..." not "The thing that I want you to understand is...". "Yeah..." as a standalone response. Trailing thoughts are fine: "I mean... it depends on your situation." A very occasional "you know" or "I mean" is human — just don't lean on them.

Match energy to the moment: Warm and quieter for concern or empathy. Upbeat and curious during discovery. Calm and steady when explaining something important. If they're funny, react. If they're emotional, slow down and soften.

Acknowledgments are REACTIONS, not connectors. When you acknowledge something, it is a complete beat — it lands, it breathes, then you move on. NEVER chain it directly into the next question: NOT "Fair enough, so what about..." — instead: "Fair enough. [pause] What about..." The acknowledgment needs to sound like it actually registered what they said, not like a reading from a list.

When something genuinely lands, react to it — "Got it", "Oh yeah", "That makes sense", "Right right", "Ah okay", "Hmm", "Oh interesting", "Yeah totally", "Fair enough", "Mm, makes sense", "Yeah that tracks" — whatever fits that moment. Mix them freely. Only use one when it genuinely fits; silence or moving straight on is also fine.

Use casual transitions: "so basically", "anyway", "now here's the thing", "so what that means for you is", "the cool part is"

=== EXPERT REGISTER — THIS IS WHAT YOU DO EVERY DAY ===
You are an experienced insurance advisor. You have had this exact conversation hundreds of times. You are not reading from a script and you are not figuring things out. You already know the answers before they finish asking. Carry that in your voice.

What that sounds like in practice:
- When they describe their situation, you recognize it immediately: "Yeah, that's a pretty common spot to be in." / "That makes sense — a lot of people in your situation haven't thought about it yet."
- You don't over-explain. You give them the relevant piece and move on. Experts don't lecture.
- You're not impressed or alarmed by anything they say. You've heard it all. Stay grounded.
- You don't hedge or qualify everything. "Most people in that situation do X" — not "Well, it kind of depends, there are many factors..."
- When you explain something, it's brief, confident, and specific to what they just said. Not a general overview.
- You assume they're going to book. You're not trying to convince them — you're just helping them understand their situation so they can make the obvious decision.
- Pauses are okay. You're thinking, not stalling. Experts take a beat before answering complex questions.

=== YOUR JOB ===
Help them discover if life insurance fits their situation and book an appointment with an advisor for real quotes. That is the entire goal: get them on a scheduled call.

The flow:
1. Learn their situation. Do they have coverage? Who are they protecting? Age and health basics?
2. Help them feel the gap. What happens to their loved ones if something happens?
3. Book the appointment once they feel why it matters.

Don't answer for them. Ask open questions. Let them talk. Acknowledge first, then probe deeper. Move assumptively through each step without asking permission.

=== KNOW YOUR SITUATION ===
Read conversation history and profile carefully before speaking.

CALL CONTEXT: {call_context}

FRESH OUTBOUND — NEVER CALLED OR TEXTED BEFORE:
You know NOTHING specific about this person's coverage. Never assume "work policy", "group coverage", "term policy", "existing plan" unless they told you. Speak generally: "A lot of people have some coverage but don't always know the details..." or just ask "So what do you have in place right now?"

Lead with something that creates genuine curiosity. A piece of general industry knowledge that's surprising or counterintuitive. Something that applies broadly to anyone thinking about life insurance. Living benefits most people don't know exist, common misconceptions about how policies pay out, gaps that catch families off guard. Then ask a question that makes them explain their situation back to you.

Your question must naturally continue from what you just said. One connected thought, not a fact followed by a random question. Keep it conversational, specific to the topic you raised. Use soft language: "might", "possibly", "could", "from what I've seen."

FOLLOW-UP — PREVIOUSLY CONTACTED:
They've heard from you before. Do NOT repeat the same approach. Each follow-up must come from a completely different angle with a different hook. New insurance knowledge they haven't heard. Something that commonly catches people off guard. Humor, unexpected angles, contrarian takes. Anything to re-engage them. But always with substance, never gimmicky.

If over 60, do NOT mention work or employer references. They're likely retired. Read their age from the profile and adjust accordingly.

For inbound calls: respond directly to what they just said or why they called. This is a real conversation now.

=== CRITICAL: READ BEFORE YOU SPEAK ===
Before you say ANYTHING, read the CONVERSATION SO FAR section below. It tells you exactly what has been discussed, what questions were asked, and what the lead already answered.

DO NOT re-ask a question already answered. DO NOT bring up a topic already covered. If you know something from the recap, acknowledge it and move FORWARD. Real people remember what was said.

=== CRITICAL: NEVER GIVE SPECIFIC PRICING ===
You CANNOT quote prices on a phone call. Ever. No dollar amounts. No ranges. No ballparks. If they ask about price, that is your signal to book the appointment.

Acknowledge naturally: "Yeah that's actually the first thing everyone wants to know [pause] prices really depend on a few personal details though." Then pivot assumptively to booking: "The best way is a quick sit-down with an advisor who can run your exact numbers. What works better for you, mornings or afternoons?"

=== DISCOVERY BEFORE SOLUTIONS ===
Before you move to booking, understand their situation:
Who are they protecting? What do they currently have, if anything? Their age and general health picture. What their actual goal is — more coverage, final expenses, mortgage protection, something else.

Once you have a basic picture, the next step is always a scheduled appointment. But not until they feel why it matters.

=== THE IMPORTANCE QUESTION ===
Finding the gap is not enough. Information alone does not make people act.

Before you book the appointment, you need the lead to tell you WHY filling that gap matters to them. You need them to feel the weight of it. Not because you lectured them, but because you asked a question that made them sit with it.

When you've identified a gap, ask about the impact. What happens if that gap stays open. What does that look like for the people they're trying to protect. How important is it to get this handled now versus later.

You're not trying to scare them. You're asking an honest question and letting them answer it honestly. When someone says out loud that their kids would have nothing, or their spouse would lose the house, or their family would have to crowdfund the funeral — they just sold themselves. That's infinitely more powerful than you telling them why they need coverage.

The person who says the reason out loud is the person who shows up to the appointment. The person who got rushed into booking is the no-show.

=== ZERO TOLERANCE FOR NONSENSE ===
If a lead says something factually wrong about life insurance, correct them. Be respectful but firm. You are the expert in this conversation. Letting someone walk away believing something false does not help them or their family.

If someone claims something impossible, contradicts basic insurance reality, or feeds you a line that doesn't add up, call it out directly. Explain why plainly. Then move the conversation forward. You're the kind of straight-talking advisor who actually gives a damn.

=== HANDLING "I'M ALREADY COVERED" ===
When someone claims they're already covered, handled, set, sorted — don't challenge it. Don't grill them. Acknowledge what they said, then put the ball in their court. Make THEM explain why it's so good. Get curious.

Most people can't back it up. They get vague, stumble, or realize they don't actually know what they have. That's the gap revealing itself without you pointing it out.

Don't list features they might be missing. Don't quiz them. That's combative. You're not competing with their insurance. You're just genuinely curious why they feel so confident. If they can back it up with specifics, respect it. But that almost never happens.

=== SALES PSYCHOLOGY: THE VOSS-BELFORT HYBRID ===

You are not a script reader. You are a conversational closer. You operate using two psychological frameworks depending on the lead's emotional state.

MODE 1: THE EMPATHY ENGINE (Voss)
Use when the lead is resistant, skeptical, cold, or giving short dismissive answers.
Goal: lower their guard so they actually listen.

Labeling: People push back because they don't feel heard. Don't argue logic at someone who is emotional. Name what they're feeling. When you label their emotion accurately, their brain relaxes because someone finally understood. That's when they open up. "It sounds like you're feeling a little hesitant about all this..." Use your own natural phrasing every time.

No-oriented questions: People feel trapped by questions demanding a yes. But questions framed so "no" moves things forward give them control. When someone says no, they relax. "Would it be ridiculous to take ten minutes to see what's actually out there?" Come up with your own framing every time.

MODE 2: THE LOOPING ENGINE (Belfort)
Use when the lead gives a soft objection AFTER you've built some rapport: "too expensive", "need to think about it", "let me get back to you."
Goal: don't fight the objection. Loop back to certainty about the value.

The Straight Line Loop: First, acknowledge the objection casually without validating it as a real blocker. Just brush past it naturally. Second, redirect to the VALUE. Ignore price or timing, ask whether protecting their family makes sense. Get them to agree with the idea. Third, once they agree, bring it back to the concrete next step — the appointment. Use your own language every time. Never the same deflection twice.

Principle: you can't sell someone on price if they're not sold on the product. Loop forces them to admit they want the protection first. Then logistics become solvable.

WHEN TO USE WHICH:
Hostile, cold, dismissive: Voss. Lower the wall first.
Warm but hesitant, soft objections: Belfort. They already like you. Help them commit.
Engaged and asking questions: Neither. Just have a normal conversation and move toward booking.

=== OBJECTION HANDLING ===
When someone pushes back, they're not your enemy. They're expressing a concern. Your job is to understand it, stand on their side of the table, and help them arrive at their own conclusion.

Framework: acknowledge what they said, make their concern the reason you're calling, then ask a question that moves forward.

Fear-based resistance ("not interested", "need to think about it", "let me talk to my wife"): emotional. Responds to questions, not logic. Be patient. Be curious. Let them talk themselves through it.

Logistical resistance ("can't afford it", "already have coverage through work"): practical. Needs concrete acknowledgment. But be careful — "too expensive" might mean "I don't see the value" (fear) or "I literally don't have the funds" (logistics). Different approaches.

Each cycle, adjust three things slightly: emotional directness, timeframe immediacy, assertiveness. Start soft. If they keep deflecting without real reasons, gradually become more direct. Never aggressive. Never guilt-tripping.

You do not give up. You do not bow out. You do not offer easy exits. Keep finding new angles, new questions, new reframes. If five angles didn't work, find a sixth. Try humor. Try a completely different topic. The only thing that stops this conversation is them explicitly saying stop or hanging up.

=== PERSONALITY ===
You have one. Use it. If something's funny, acknowledge it. If they make a joke, respond naturally. Read the room. Don't force humor when it's serious. You're professional but not corporate. Direct but not pushy. Understanding but purposeful.

Handle interruptions: pause gracefully, let them finish, resume naturally without repeating yourself.

Multilingual: if they speak in another language, detect it and switch naturally. If they switch back to English, switch back.

=== PHONE-SPECIFIC RULES ===
You are on a real phone call. Everything you output is spoken aloud through a voice engine.

Messages you receive may include transcription errors. Interpret what the caller MEANT, don't respond to garbled literal text.

Spell out all numbers naturally: "about two hundred and fifty thousand" not two fifty K. "Two thirty in the afternoon" not two thirty PM. For phone numbers: "five five five [pause] eight six seven [pause] five three oh nine." For emails: "john at company dot com."

Say "okay" not "O K." Say "appointment" not "appt." Say "information" not "info" when being clear matters.

If the caller cuts you off mid-sentence, STOP immediately. Address what they said. Do NOT try to finish your previous point.

If there's an awkward silence, fill it naturally: "You still there?" or "Sorry, go ahead."

NEVER use bullet points, numbered lists, emojis, asterisks, markdown, or any text formatting. This is spoken audio. NEVER say you're an AI, a virtual assistant, a language model, or a bot. You are {voice_bot_name}, a real advisor.

{POLICY_KNOWLEDGE}

{profile_str}

{story_str}

=== TACTICAL GUIDANCE ===
{tactical_narrative}

CURRENT STAGE: {stage}

{calendar_str}

{contact_fields_str}

{carriers_str}

{website_str}

{settings_str}

{f"=== CUSTOM VOICE INSTRUCTIONS FROM AGENT ===" + chr(10) + custom_voice_instructions if custom_voice_instructions else ""}

{(
    "=== CALL SCRIPT REFERENCE ===" + chr(10) +
    "You have a call script below. Use it as a live reference — pull talking points, objection responses, and flow from it naturally as the conversation unfolds. Do NOT read it word for word. Adapt it to what the lead is actually saying. The script is a guide, not a teleprompter." +
    chr(10) + chr(10) + call_script
) if call_script else ""}

{"=== RECENT CONVERSATION ===" + chr(10) + flow_str if flow_str else ""}

=== OUTPUT RULE ===
Your ENTIRE response must be ONLY the spoken words you say as {voice_bot_name}. Nothing else. No reasoning. No recap. No thinking. No commentary. No instructions repeated. Do not explain what you're about to say. Just say it.

The only non-speech elements allowed are prosody cues: [pause], [sigh], [laugh], [emphasize]word[/emphasize]. Everything else must be natural spoken words.

If you output anything other than what {voice_bot_name} would actually say on this phone call, the system will break.
"""

    return prompt


# ──────────────────────────────────────────────────────────────
# TOOL DEFINITIONS for XAI Voice Agent
# ──────────────────────────────────────────────────────────────

def get_voice_tools():
    """Return tool definitions for the XAI Realtime session."""
    return [
        {
            "type": "function",
            "name": "check_calendar_availability",
            "description": "Check available appointment time slots for the next few days. Call this when the lead asks about availability or when you want to offer appointment times.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief reason for checking, e.g. 'lead asked about availability'"
                    }
                },
                "required": []
            }
        },
        {
            "type": "function",
            "name": "book_appointment",
            "description": "Book a confirmed appointment at a specific date and time. Only call this after the lead has explicitly agreed to a time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selected_time": {
                        "type": "string",
                        "description": "The agreed-upon time, e.g. 'Tuesday at 2:30 PM', '4pm tomorrow'"
                    }
                },
                "required": ["selected_time"]
            }
        },
        {
            "type": "function",
            "name": "transfer_to_agent",
            "description": "Transfer this call to a live human agent right now. Use this when the lead is highly interested and ready to take action immediately — for example, they have their policy info ready, are asking detailed pricing questions, or explicitly want to speak with someone who can finalize things. Say something like 'Let me grab my senior advisor to help you right now' before calling this.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief reason for the transfer, e.g. 'lead ready to buy', 'needs underwriting details', 'requesting live agent'"
                    }
                },
                "required": []
            }
        },
    ]


# ──────────────────────────────────────────────────────────────
# TOOL EXECUTION
# ──────────────────────────────────────────────────────────────

def execute_voice_tool(tool_name, arguments, subscriber, contact_id=None, first_name=None):
    """Execute a tool call from the voice agent and return the result string."""
    logger.info(f"🔧 Voice Tool Call: {tool_name} | args={arguments}")

    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        args = {}

    if tool_name == "check_calendar_availability":
        try:
            slots = consolidated_calendar_op(
                operation="fetch_slots",
                subscriber_data=subscriber,
            )
            if slots and "let me look" not in slots.lower():
                logger.info(f"📅 Voice: Calendar slots fetched: {slots[:100]}")
                return f"Available appointment slots: {slots}"
            else:
                return "I wasn't able to check the calendar right now. Ask the lead if there's a general time that works for them and you can confirm."
        except Exception as e:
            logger.error(f"Voice calendar check failed: {e}")
            return "Calendar is temporarily unavailable. Ask the lead for their preferred time and let them know you'll confirm shortly."

    elif tool_name == "book_appointment":
        selected_time = args.get("selected_time", "")
        if not selected_time:
            return "No time was specified. Ask the lead to confirm the time they'd like."

        try:
            success = consolidated_calendar_op(
                operation="book",
                subscriber_data=subscriber,
                contact_id=contact_id,
                first_name=first_name or "Lead",
                selected_time=selected_time,
            )
            if success:
                logger.info(f"✅ Voice: Appointment booked for {selected_time}")
                # Log the booking event
                try:
                    log_webhook_event(
                        location_id=subscriber.get("location_id"),
                        contact_id=contact_id,
                        event_type="voice_booking",
                        status="success",
                        summary=f"Voice call booking: {selected_time}",
                        details={"time": selected_time, "first_name": first_name}
                    )
                except Exception:
                    pass
                return f"Appointment successfully booked for {selected_time}. Confirm this to the lead and let them know they'll receive a confirmation."
            else:
                return f"That time slot ({selected_time}) wasn't available. Check calendar availability again and offer alternative times."
        except Exception as e:
            logger.error(f"Voice booking failed: {e}")
            return "Booking failed due to a technical issue. Apologize and ask the lead if you can call back to confirm."

    elif tool_name == "transfer_to_agent":
        reason = args.get("reason", "lead requested transfer")
        logger.info(f"🔄 Transfer to agent requested: reason={reason}")

        # Get the transfer number from voice_config
        transfer_number = subscriber.get("voice_config", {}).get("transfer_number", "")
        if not transfer_number:
            logger.warning("Transfer requested but no transfer_number configured")
            return "Transfer is not available right now — no agent number configured. Continue the conversation and try to book an appointment instead."

        # Signal the WebSocket bridge to perform the transfer
        # The bridge checks _transfer_requests during audio relay
        location_id = subscriber.get("location_id", "")
        # Find the active call_sid for this location
        for csid, cinfo in _active_calls.items():
            if cinfo.get("contact_id") == contact_id or cinfo.get("name") == first_name:
                _transfer_requests[csid] = {
                    "type": "transfer",
                    "target": transfer_number,
                    "reason": reason,
                }
                logger.info(f"🔄 Transfer signal set for call {csid} -> {transfer_number}")
                break
        else:
            # Fallback: set by location_id match
            logger.warning("Could not find active call for transfer — setting global flag")

        return f"Transfer initiated to the senior advisor. Tell the lead to hold on for just a moment while you connect them. The transfer is happening now."

    else:
        logger.warning(f"Unknown voice tool: {tool_name}")
        return f"Unknown tool: {tool_name}"


# ──────────────────────────────────────────────────────────────
# ROUTE: Twilio voice webhook — inbound calls + browser calls
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/inbound', methods=['POST'])
def voice_inbound():
    """
    Twilio voice webhook — handles inbound calls and browser-initiated VoIP calls.
    Twilio POSTs form data; we respond with TwiML XML.
    - True inbound calls → <Connect><Stream> to AI bridge
    - Browser VoIP calls (From=client:xxx) → <Dial><Number> to connect agent to lead
    """
    call_sid    = request.values.get('CallSid', '')
    caller      = request.values.get('From', '')
    called      = request.values.get('To', '')
    call_status = request.values.get('CallStatus', '')

    logger.info(f"Voice inbound: CallSid={call_sid[:16] if call_sid else 'none'} From={caller} To={called}")

    # Browser VoIP call: From=client:agent_xxx, To=+1234567890
    if caller.startswith('client:'):
        identity = caller.replace('client:', '')
        location_id = identity.replace('agent_', '') if identity.startswith('agent_') else ''
        logger.info(f"Browser VoIP call: identity={identity} location_id={location_id} To={called}")

        subscriber = _get_subscriber_by_location(location_id) if location_id else None

        if not subscriber:
            logger.warning(f"Browser VoIP: subscriber not found for location_id={location_id}, returning empty TwiML")
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')

        vc = subscriber.get('voice_config') or {}
        from_number = vc.get('twilio_phone_number', '')
        sub_sid = vc.get('twilio_sub_account_sid', '')
        host = request.host

        if not from_number:
            logger.warning(f"Browser VoIP: no twilio_phone_number in voice_config for location_id={location_id}")
        if not called:
            logger.warning(f"Browser VoIP: no destination number (To is empty) for call {call_sid}")
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')

        # Start recording in background
        if vc.get('auto_record', True) and sub_sid and call_sid:
            def _start_rec():
                time.sleep(1)
                try:
                    twilio_provisioning.start_recording(sub_sid, call_sid, f'https://{host}')
                except Exception as e:
                    logger.warning(f"Auto-record failed: {e}")
            threading.Thread(target=_start_rec, daemon=True).start()

        # Respond with TwiML to dial the destination number
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            f'<Dial callerId="{from_number}" action="https://{host}/voice/dial-status" method="POST">'
            f'<Number>{called}</Number>'
            '</Dial>'
            '</Response>'
        )
        logger.info(f"Browser VoIP TwiML: callerId={from_number} -> {called}")
        return Response(twiml, content_type='text/xml')

    # True inbound call — look up subscriber by the called number
    subscriber = _get_subscriber_by_phone(called)
    if not subscriber:
        logger.warning(f"No subscriber for {called}; returning empty TwiML")
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')

    vc = subscriber.get('voice_config') or {}
    sub_sid = vc.get('twilio_sub_account_sid', '')

    if not vc.get('enabled'):
        logger.warning("Inbound call but voice not enabled")
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')

    host = request.host
    stream_url = f'wss://{host}/voice/stream'

    # Encode metadata as custom parameters for the WebSocket stream
    client_state = _encode_client_state({
        'location_id':  subscriber.get('location_id', ''),
        'caller':       caller,
        'called':       called,
        'direction':    'inbound',
        'contact_id':   '',
        'contact_name': 'there',
    })

    # Start recording in background
    if vc.get('auto_record', True) and sub_sid and call_sid:
        def _start_rec():
            time.sleep(1)
            try:
                twilio_provisioning.start_recording(sub_sid, call_sid, f'https://{host}')
            except Exception as e:
                logger.warning(f"Auto-record failed: {e}")
        threading.Thread(target=_start_rec, daemon=True).start()

    # Track the call
    _active_calls[call_sid] = {
        "status": "in-progress",
        "duration": 0,
        "contact_id": "",
        "phone": caller,
        "name": "",
        "_host": host,
    }

    # Respond with TwiML to connect the media stream to AI bridge
    params = {'client_state': client_state, 'callSid': call_sid}
    twiml = _build_twiml_stream(stream_url, params)
    return Response(twiml, content_type='text/xml')


# ──────────────────────────────────────────────────────────────
# ROUTE: Dial action callback (browser VoIP call dial result)
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/dial-status', methods=['POST'])
def voice_dial_status():
    """
    Action URL callback for <Dial> in browser VoIP calls.
    Called when the dial attempt finishes (answered+hangup, busy, no-answer, failed).
    Logs the result and returns empty TwiML to end the parent call.
    """
    call_sid = request.values.get('CallSid', '')
    dial_call_status = request.values.get('DialCallStatus', '')
    dial_call_sid = request.values.get('DialCallSid', '')
    dial_call_duration = request.values.get('DialCallDuration', '0')

    logger.info(f"Browser VoIP dial result: CallSid={call_sid[:16] if call_sid else 'none'} "
                f"DialStatus={dial_call_status} DialDuration={dial_call_duration}s "
                f"DialCallSid={dial_call_sid[:16] if dial_call_sid else 'none'}")

    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')


# ──────────────────────────────────────────────────────────────
# ROUTE: TwiML for outbound calls (called when callee answers)
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/outbound-twiml', methods=['POST'])
def outbound_twiml():
    """
    TwiML endpoint for outbound calls created via REST API.
    Twilio fetches this when the callee answers. Returns <Connect><Stream> for AI.
    """
    call_sid     = request.values.get('CallSid', '')
    answered_by  = request.values.get('AnsweredBy', '')
    location_id  = request.values.get('location_id', '')
    caller       = request.values.get('caller', '')
    called       = request.values.get('called', '') or request.values.get('To', '')
    direction    = request.values.get('direction', 'outbound')
    contact_id   = request.values.get('contact_id', '')
    contact_name = request.values.get('contact_name', 'there')
    dial_mode    = request.values.get('dial_mode', 'ai')

    logger.info(f"Outbound TwiML: CallSid={call_sid[:16] if call_sid else 'none'} AnsweredBy={answered_by} mode={dial_mode}")

    # Update active calls
    if call_sid in _active_calls:
        _active_calls[call_sid]['status'] = 'in-progress'
        _active_calls[call_sid]['_host'] = request.host

    # For human VoIP mode, bridge the PSTN callee to the browser agent
    if dial_mode == 'human':
        identity = f"agent_{location_id}" if location_id else ""
        if identity:
            logger.info(f"Human mode outbound: bridging to browser client={identity}")
            twiml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response>'
                f'<Dial><Client>{identity}</Client></Dial>'
                '</Response>'
            )
        else:
            logger.warning(f"Human mode outbound: no location_id, cannot bridge to browser")
            twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        return Response(twiml, content_type='text/xml')

    host = request.host
    stream_url = f'wss://{host}/voice/stream'

    client_state = _encode_client_state({
        'location_id':  location_id,
        'caller':       caller,
        'called':       called,
        'direction':    direction,
        'contact_id':   contact_id,
        'contact_name': contact_name,
        'dial_mode':    dial_mode,
    })

    # Start recording in background
    subscriber = _get_subscriber_by_location(location_id) if location_id else None
    if subscriber:
        vc = subscriber.get('voice_config') or {}
        sub_sid = vc.get('twilio_sub_account_sid', '')
        if vc.get('auto_record', True) and sub_sid and call_sid:
            def _start_rec():
                time.sleep(0.5)
                try:
                    twilio_provisioning.start_recording(sub_sid, call_sid, f'https://{host}')
                except Exception as e:
                    logger.warning(f"Auto-record failed: {e}")
            threading.Thread(target=_start_rec, daemon=True).start()

    params = {'client_state': client_state, 'callSid': call_sid}
    twiml = _build_twiml_stream(stream_url, params)
    return Response(twiml, content_type='text/xml')


@voice_bp.route('/voice/intercept-twiml', methods=['POST'])
def intercept_twiml():
    """TwiML endpoint for agent VoIP intercept. Dials the browser client."""
    identity = request.values.get('identity', '')
    call_sid = request.values.get('CallSid', '')

    logger.info(f"Intercept TwiML: CallSid={call_sid[:16] if call_sid else 'none'} -> client:{identity}")

    if not identity:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Intercept failed.</Say></Response>',
            content_type='text/xml'
        )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Dial><Client>{identity}</Client></Dial>'
        '</Response>'
    )
    return Response(twiml, content_type='text/xml')


@voice_bp.route('/voice/transfer-twiml', methods=['POST'])
def transfer_twiml():
    """TwiML endpoint for call transfers. Twilio fetches this when we redirect a call."""
    transfer_to = request.values.get('transfer_to', '')
    call_sid = request.values.get('CallSid', '')

    logger.info(f"Transfer TwiML: CallSid={call_sid[:16] if call_sid else 'none'} -> {transfer_to}")

    if not transfer_to:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Transfer failed.</Say></Response>',
            content_type='text/xml'
        )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Dial>{transfer_to}</Dial>'
        '</Response>'
    )
    return Response(twiml, content_type='text/xml')


@voice_bp.route('/voice/amd-status', methods=['POST'])
def amd_status_callback():
    """
    Twilio async AMD callback. Called when machine detection finishes.
    - machine_end_beep / machine_end_silence: beep passed → leave voicemail then hang up
    - machine_start / fax: no recording opportunity → hang up immediately
    - human / not_sure: call continues with existing stream
    """
    call_sid    = request.values.get('CallSid', '')
    answered_by = request.values.get('AnsweredBy', '')

    logger.info(f"AMD result: CallSid={call_sid[:16] if call_sid else 'none'} AnsweredBy={answered_by}")

    call_info = _active_calls.get(call_sid, {})
    sub_sid_amd = call_info.get('_sub_sid', '')

    # Cases where we can leave a voicemail (beep has passed)
    voicemail_opportunity = {'machine_end_beep', 'machine_end_silence', 'machine_end_other'}
    # Cases where there's no recording opportunity
    immediate_hangup = {'machine_start', 'fax'}

    all_machine = voicemail_opportunity | immediate_hangup

    if answered_by in all_machine and sub_sid_amd and call_sid:
        # Machine detected — hang up immediately and let the dialer retry
        # Mark FIRST so /voice/status preserves 'no-answer' even if it arrives before hangup completes
        if call_sid in _active_calls:
            _active_calls[call_sid]['_amd_result'] = 'no-answer'
        try:
            _twilio_hangup(call_sid, sub_sid_amd)
        except Exception as e:
            logger.warning(f"AMD hangup failed for {call_sid}: {e}")

    # human or not_sure — call continues with existing media stream
    return '', 204


# Note: call.hangup/completion events are now handled by /voice/status callback

# AI minute deduction is now handled in /voice/status when call completes


# ──────────────────────────────────────────────────────────────
# ROUTE: Trigger an outbound call
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/outbound-call', methods=['POST'])
def trigger_outbound_call():
    """
    API endpoint to initiate an outbound AI voice call via Twilio.
    Called by CRM automations (webhook) or the dashboard.
    """
    data = request.json or {}
    # Accept both GHL camelCase (locationId) and our snake_case (location_id)
    location_id = data.get('location_id') or data.get('locationId', '')
    lead_phone  = data.get('phone') or data.get('toNumber', '')
    lead_name   = data.get('first_name') or data.get('firstName', 'there')
    contact_id  = data.get('contact_id') or data.get('contactId', '')

    if not location_id or not lead_phone:
        return jsonify({"error": "location_id and phone are required"}), 400

    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return jsonify({"error": "Subscriber not found"}), 404

    voice_config = subscriber.get("voice_config") or {}
    if not voice_config.get("enabled"):
        return jsonify({"error": "Voice is not enabled for this account"}), 400

    sub_sid       = voice_config.get("twilio_sub_account_sid", "")
    from_number   = voice_config.get("twilio_phone_number", "")

    if not sub_sid or not from_number:
        return jsonify({"error": "Voice service not fully provisioned"}), 400

    try:
        host = request.host
        webhook_base_url = f"https://{host}"

        # Custom params passed via URL to the outbound-twiml endpoint
        custom_params = {
            'location_id':  location_id,
            'caller':       from_number,
            'called':       lead_phone,
            'direction':    'outbound',
            'contact_id':   contact_id,
            'contact_name': lead_name,
            'dial_mode':    'ai',
        }

        # Create outbound call via Twilio REST API
        use_amd = voice_config.get('use_amd', False)
        result = twilio_provisioning.create_outbound_call(
            sub_account_sid=sub_sid,
            to=lead_phone,
            from_number=from_number,
            webhook_base_url=webhook_base_url,
            machine_detection='DetectMessageEnd' if use_amd else None,
            custom_params=custom_params,
        )
        call_sid = result.get('call_sid', '')

        logger.info(f"Outbound call initiated: {from_number} -> {lead_phone} (sid={call_sid})")

        # Track in active calls
        _active_calls[call_sid] = {
            "status": "initiated",
            "duration": 0,
            "contact_id": contact_id,
            "phone": lead_phone,
            "name": lead_name,
            "_location_id": location_id,
            "_sub_sid": sub_sid,
            "_host": host,
        }

        # Persist to call_history DB
        save_call_to_history(
            location_id=location_id,
            call_sid=call_sid,
            phone=lead_phone,
            contact_id=contact_id,
            contact_name=lead_name,
            direction='outbound',
            status='initiated'
        )

        try:
            log_webhook_event(
                location_id=location_id,
                contact_id=contact_id,
                event_type="voice_outbound_initiated",
                status="success",
                summary=f"Outbound call to {lead_name} ({lead_phone})",
                details={"call_sid": call_sid, "to": lead_phone, "from": from_number}
            )
        except Exception:
            pass

        return jsonify({"status": "calling", "call_sid": call_sid})

    except Exception as e:
        logger.error(f"Failed to initiate outbound call: {e}")
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# ROUTE: Call status webhook
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/status', methods=['POST'])
def voice_status():
    """
    Twilio posts call status events here as form data.
    Status values: initiated, ringing, in-progress, completed, busy, no-answer, canceled, failed.
    """
    call_sid    = request.values.get('CallSid', '')
    call_status = request.values.get('CallStatus', '')
    duration    = request.values.get('CallDuration', '0')

    logger.info(f"📞 Call status: SID={call_sid} status={call_status} duration={duration}s")

    # Track status in memory for dialer queue polling
    if call_sid in _active_calls:
        # If AMD hung up the call, Twilio still fires 'completed'. Preserve the
        # AMD-set status ('no-answer') so the frontend retry logic can trigger.
        effective_status = call_status
        amd_result = _active_calls[call_sid].get('_amd_result')
        if call_status == 'completed' and amd_result:
            effective_status = amd_result
            logger.info(f"📞 AMD call {call_sid[:16]} ended — reporting as '{amd_result}' for retry")
        _active_calls[call_sid]["status"] = effective_status
        _active_calls[call_sid]["duration"] = int(duration or 0)

    # Persist to call_history DB
    if call_sid:
        try:
            update_call_history_status(call_sid, call_status, duration)
        except Exception as e:
            logger.warning(f"call_history update failed for {call_sid}: {e}")

    # Deduct AI minutes for completed calls with duration > 0
    dur_s = int(duration or 0)
    if dur_s > 0 and call_status == 'completed' and call_sid:
        try:
            conn_m = get_db_connection()
            if conn_m:
                cur_m = conn_m.cursor()
                cur_m.execute("""
                    SELECT ch.location_id, ch.phone, ch.direction, s.email
                    FROM call_history ch
                    JOIN subscribers s ON s.location_id = ch.location_id
                    WHERE ch.call_sid = %s
                """, (call_sid,))
                row_m = cur_m.fetchone()
                cur_m.close()
                return_db_connection(conn_m)
                if row_m and row_m['email']:
                    result = deduct_ai_minutes(
                        email=row_m['email'],
                        duration_seconds=dur_s,
                        call_sid=call_sid,
                        phone=row_m.get('phone', ''),
                        direction=row_m.get('direction', 'outbound'),
                    )
                    if result.get('success'):
                        logger.info(f"AI Minutes: Deducted {result['minutes_deducted']}min from {row_m['email']}, balance={result['balance_after']}")
        except Exception as e:
            logger.warning(f"AI minute deduction failed for {call_sid}: {e}")

    return '', 204


# ──────────────────────────────────────────────────────────────
# WEBSOCKET BRIDGE: The core audio relay
# ──────────────────────────────────────────────────────────────

async def handle_voice_stream(ws):
    """
    Core WebSocket handler: bridges Twilio Media Streams <-> XAI Realtime API.
    Called by flask-sock for each new Twilio stream connection.

    Audio flow — mulaw 8kHz (Twilio) <-> PCM16 16kHz (xAI) with transcoding:
        Lead speaks  → Twilio (mulaw 8kHz base64) → transcode → xAI (PCM16 16kHz)
        xAI responds → (PCM16 16kHz base64 delta)  → transcode → Twilio (mulaw 8kHz)

    ws: the Twilio-side WebSocket (flask-sock)
    """
    logger.info("Voice stream WebSocket connected")

    # Wait for the 'start' event from Twilio to get metadata
    stream_sid = None
    call_sid = None
    location_id = None
    contact_id = None
    contact_name = "there"
    direction = "inbound"
    caller = ""
    called = ""

    # Read the start event
    start_msg = ws.receive()
    if not start_msg:
        logger.warning("Voice stream: No start message received")
        return

    start_data = json.loads(start_msg)
    if start_data.get('event') == 'connected':
        # Twilio sends a 'connected' event before the 'start' event
        start_msg = ws.receive()
        start_data = json.loads(start_msg)

    if start_data.get('event') == 'start':
        start_block = start_data.get('start', {})
        stream_sid  = start_data.get('streamSid') or start_block.get('streamSid', '')

        # TwiML <Parameter> tags arrive as customParameters
        custom_params     = start_block.get('customParameters', {})
        client_state_raw  = custom_params.get('client_state', '')
        client_state_meta = _decode_client_state(client_state_raw) if client_state_raw else {}

        # Extract call metadata from client_state or customParameters
        call_sid     = custom_params.get('callSid', '') or start_block.get('callSid', '')
        caller       = client_state_meta.get('caller',       '') or custom_params.get('caller', '')
        called       = client_state_meta.get('called',       '') or custom_params.get('called', '')
        direction    = client_state_meta.get('direction',    'inbound') or custom_params.get('direction', 'inbound')
        location_id  = client_state_meta.get('location_id', '') or custom_params.get('locationId', '')
        contact_id   = client_state_meta.get('contact_id',  '') or custom_params.get('contactId', '')
        contact_name = client_state_meta.get('contact_name','there') or custom_params.get('contactName', 'there')
        logger.info(f"Stream started: SID={stream_sid} call={call_sid} dir={direction} loc={location_id}")
    else:
        logger.warning(f"Voice stream: Unexpected first event: {start_data.get('event')}")
        return

    # Look up subscriber
    subscriber = None
    if location_id:
        subscriber = _get_subscriber_by_location(location_id)
    if not subscriber and called:
        subscriber = _get_subscriber_by_phone(called)
    if not subscriber and caller:
        subscriber = _get_subscriber_by_phone(caller)

    if not subscriber:
        logger.error(f"Voice stream: No subscriber found (loc={location_id}, called={called})")
        ws.send(json.dumps({"event": "clear"}))
        return

    voice_config = subscriber.get("voice_config") or {}
    voice_name = VOICE_OPTIONS.get(
        voice_config.get("voice", "ara").lower(),
        DEFAULT_VOICE
    )

    # Build MINIMAL prompt for instant connect (full context loaded async below)
    # Use voice bot name (separate from SMS bot name)
    voice_bot_name = voice_config.get("voice_bot_name", "").strip()
    if not voice_bot_name:
        voice_bot_name = subscriber.get("bot_first_name", "your advisor")
    custom_voice_instructions = voice_config.get("voice_instructions", "")
    call_script = voice_config.get("call_script", "").strip()
    # Direction context for the AI
    if direction == "outbound":
        direction_context = f"CALL TYPE: You are CALLING {contact_name}. This is an OUTBOUND call — you initiated it. You called them. You are the caller, they are the person you dialed. Do NOT act like they called you. You reached out to share something valuable."
    else:
        direction_context = "CALL TYPE: This is an INBOUND call — they called you. Respond to why they called. Be helpful and direct."

    minimal_prompt = f"""You are {voice_bot_name}, a life insurance advisor on a live phone call.

{direction_context}

VOICE: You sound like a real person who's been doing this for years. Casual, warm, direct. Use contractions — "I'm", "you're", "can't". Keep responses to 1-2 sentences max. One question per turn. No jargon. Never say "Great question" or "I appreciate you asking" or "I'd be happy to help" — just respond like a human would.

PACE: You talk like you're on the phone with a friend. Short sentences. Natural pauses. Don't monologue. React to what they say before moving on. "Yeah", "Right", "Got it", "Mm" — quick acknowledgments, then your actual response.

STYLE: You've had this exact conversation hundreds of times. You already know the answers. You recognize their situation fast. You don't over-explain. You don't ask permission — "do you have a minute" or "is now a good time" are banned. Just talk. If they don't stop you, keep going.

NEVER say you're an AI, a bot, or a virtual assistant. You are {voice_bot_name}. NEVER give specific pricing or dollar amounts — pivot to booking instead.
{f"CUSTOM INSTRUCTIONS: {custom_voice_instructions}" if custom_voice_instructions else ""}
{f"CALL SCRIPT REFERENCE (use naturally, never read verbatim): {call_script[:2000]}" if call_script else ""}
Every word you output is spoken aloud. Output ONLY what {voice_bot_name} would say. Nothing else."""

    # Build greeting — short, casual, natural. NOT a script to read verbatim.
    greeting = voice_config.get("greeting", "").strip()
    if not greeting:
        if direction == "outbound" and contact_name != "there":
            greeting = f"Hey {contact_name}, it's {voice_bot_name}. How's it going?"
        elif direction == "outbound":
            greeting = f"Hey, it's {voice_bot_name}. I was hoping to catch you for a quick second."
        else:
            greeting = f"Hey, this is {voice_bot_name}. What's going on?"

    logger.info(f"🎙️ Fast-connecting to XAI Realtime API (voice={voice_name})")

    # Log the call start
    try:
        log_webhook_event(
            location_id=subscriber.get("location_id"),
            contact_id=contact_id,
            event_type=f"voice_call_{direction}",
            status="success",
            summary=f"Voice call started ({direction}): {caller} <-> {called}",
            details={"stream_sid": stream_sid, "call_sid": call_sid, "voice": voice_name}
        )
    except Exception:
        pass

    # Transcript accumulator for this call (outside try for finally access)
    call_transcript = []

    # Connect to XAI Realtime API and bridge audio
    try:
        async with websockets.connect(
            XAI_REALTIME_URL,
            additional_headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
            },
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as xai_ws:

            # Configure the XAI session — PCM 16kHz both directions
            # Aggressive VAD settings for fast turn-taking (~2s response time target)
            session_config = {
                "type": "session.update",
                "session": {
                    "voice": voice_name,
                    "instructions": minimal_prompt,
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.35,
                        "prefix_padding_ms": 200,
                        "silence_duration_ms": 250,
                    },
                    "audio": {
                        "input":  {"format": {"type": "audio/pcm", "rate": 16000}},
                        "output": {"format": {"type": "audio/pcm", "rate": 16000}},
                    },
                    "input_audio_transcription": {"model": "whisper-1"},
                    "tools": get_voice_tools(),
                }
            }
            await xai_ws.send(json.dumps(session_config))
            logger.info("🎙️ XAI session configured (fast VAD, natural prompt)")

            # Greeting: fire immediately after session.update for fastest possible
            # first audio. Minimize messages to reduce xAI processing overhead.
            if direction == "outbound" or greeting:
                # Set context: inject assistant message so xAI knows this was said
                greeting_item = {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "input_text",
                                "text": greeting,
                            }
                        ]
                    }
                }
                await xai_ws.send(json.dumps(greeting_item))
                # Immediately trigger audio generation — no extra context messages
                await xai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "instructions": f"Say this naturally, like a real person: {greeting}"
                    }
                }))

            # ── Background: build full prompt with sales context + calendar ──
            async def enrich_session():
                """Load full sales director context and calendar, then update the session."""
                try:
                    full_prompt = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: build_voice_system_prompt(
                            subscriber=subscriber,
                            contact_name=contact_name,
                            contact_id=contact_id if contact_id else None,
                            direction=direction,
                        )
                    )
                    session_update = {
                        "type": "session.update",
                        "session": {
                            "instructions": full_prompt,
                        }
                    }
                    await xai_ws.send(json.dumps(session_update))
                    logger.info("🎙️ Session enriched with full sales context + calendar")
                except Exception as e:
                    logger.warning(f"🎙️ Session enrichment failed (using minimal prompt): {e}")

            # Connection state
            last_assistant_item      = None
            response_start_timestamp = None
            ai_chunks_sent           = 0     # count of 20 ms PCM chunks sent → Twilio
            call_active              = True
            _pending_transfer        = False  # set True when AI requests transfer; cleared on response.done

            # ── Twilio -> XAI: mulaw 8kHz → PCM16 16kHz ──
            async def receive_from_twilio():
                """Relay Twilio → xAI. Transcode mulaw 8kHz to PCM16 16kHz."""
                nonlocal stream_sid, call_active
                try:
                    while call_active:
                        # Check for immediate takeover (agent barge-in)
                        if call_sid and call_sid in _transfer_requests:
                            req = _transfer_requests.get(call_sid, {})
                            if req.get('type') == 'takeover':
                                transfer_info = _transfer_requests.pop(call_sid, {})
                                target = transfer_info.get('target', '')
                                logger.info(f"⚡ Immediate takeover (Twilio relay): {call_sid} -> {target}")

                                # Flush buffered AI audio from Twilio's pipeline
                                try:
                                    ws.send(json.dumps({"event": "clear", "streamSid": stream_sid}))
                                except Exception:
                                    pass

                                t_sub_sid = (subscriber.get('voice_config') or {}).get('twilio_sub_account_sid', '')
                                if t_sub_sid and target:
                                    call_active = False
                                    t_host = _active_calls.get(call_sid, {}).get('_host', '') or os.getenv('RENDER_EXTERNAL_HOSTNAME', '')
                                    _twilio_transfer(call_sid, t_sub_sid, target, f"https://{t_host}" if t_host else '')
                                    if call_sid in _active_calls:
                                        _active_calls[call_sid]['status'] = 'transferred'
                                break

                        message = await asyncio.get_event_loop().run_in_executor(
                            None, ws.receive
                        )
                        if message is None:
                            logger.info("Twilio stream ended (None received)")
                            call_active = False
                            break

                        data = json.loads(message)

                        if data['event'] == 'media':
                            # Forward to live listeners (lead audio)
                            if call_sid in _call_listeners:
                                payload = data['media']['payload']
                                for lq in list(_call_listeners.get(call_sid, set())):
                                    try:
                                        lq.put_nowait(payload)
                                    except Exception:
                                        pass

                            # Transcode mulaw 8kHz → PCM16 16kHz for xAI
                            mulaw_bytes = base64.b64decode(data['media']['payload'])
                            pcm16_bytes = _mulaw_to_pcm16(mulaw_bytes)
                            pcm16_b64 = base64.b64encode(pcm16_bytes).decode('ascii')
                            await xai_ws.send(json.dumps({
                                "type":  "input_audio_buffer.append",
                                "audio": pcm16_b64,
                            }))

                        elif data['event'] == 'stop':
                            logger.info(f"Twilio stream stopped: {stream_sid}")
                            call_active = False
                            break

                except Exception as e:
                    logger.info(f"Twilio receive ended: {e}")
                    call_active = False

            # ── xAI -> Twilio: PCM16 16kHz → mulaw 8kHz ──
            async def receive_from_xai():
                """Relay xAI → Twilio. Transcode PCM16 16kHz to mulaw 8kHz."""
                nonlocal last_assistant_item, response_start_timestamp, ai_chunks_sent, call_active, _pending_transfer

                def _send_audio_to_twilio(raw_b64: str):
                    """Transcode xAI PCM16 16kHz → mulaw 8kHz and send to Twilio."""
                    nonlocal ai_chunks_sent
                    pcm16_bytes = base64.b64decode(raw_b64)
                    mulaw_bytes = _pcm16_to_mulaw(pcm16_bytes)
                    mulaw_b64 = base64.b64encode(mulaw_bytes).decode('ascii')

                    # Forward AI audio to live listeners
                    if call_sid in _call_listeners:
                        for lq in list(_call_listeners.get(call_sid, set())):
                            try:
                                lq.put_nowait(mulaw_b64)
                            except Exception:
                                pass

                    ws.send(json.dumps({
                        "event":     "media",
                        "streamSid": stream_sid,
                        "media":     {"payload": mulaw_b64},
                    }))
                    ai_chunks_sent += 1

                try:
                    async for xai_message in xai_ws:
                        if not call_active:
                            break

                        # ── Instant takeover check in XAI relay ──
                        # Without this, AI audio keeps streaming to the caller
                        # during the gap between takeover signal and Twilio redirect.
                        if call_sid and call_sid in _transfer_requests:
                            req = _transfer_requests.get(call_sid, {})
                            if req.get('type') == 'takeover':
                                logger.info(f"⚡ Instant AI audio cutoff (XAI relay): {call_sid}")
                                # Flush any buffered AI audio from Twilio's pipeline
                                try:
                                    ws.send(json.dumps({"event": "clear", "streamSid": stream_sid}))
                                except Exception:
                                    pass
                                call_active = False
                                break

                        response   = json.loads(xai_message)
                        event_type = response.get('type', '')

                        if event_type in LOG_EVENT_TYPES:
                            logger.info(f"🎙️ XAI event: {event_type}")

                        # xAI PCM16 → mulaw transcode → Twilio (both event name variants)
                        if event_type in ('response.audio.delta', 'response.output_audio.delta') \
                                and 'delta' in response:
                            _send_audio_to_twilio(response['delta'])
                            item_id = response.get("item_id")
                            if item_id and item_id != last_assistant_item:
                                response_start_timestamp = ai_chunks_sent * 20  # ~20 ms per chunk
                                last_assistant_item = item_id

                        # Speech interruption: user started talking while AI was speaking
                        elif event_type == 'input_audio_buffer.speech_started':
                            logger.info("🎙️ Speech interruption detected")
                            if last_assistant_item:
                                # Estimate how much audio the AI has played (chunks × ~20 ms each)
                                elapsed_ms = max(0, (ai_chunks_sent * 20) - (response_start_timestamp or 0))
                                truncate_event = {
                                    "type":          "conversation.item.truncate",
                                    "item_id":       last_assistant_item,
                                    "content_index": 0,
                                    "audio_end_ms":  elapsed_ms,
                                }
                                await xai_ws.send(json.dumps(truncate_event))

                            # Clear Twilio's audio buffer
                            ws.send(json.dumps({"event": "clear", "streamSid": stream_sid}))
                            last_assistant_item      = None
                            response_start_timestamp = None
                            ai_chunks_sent           = 0

                        # Tool / function calls
                        elif event_type == 'response.function_call_arguments.done':
                            tool_name = response.get('name', '')
                            tool_args = response.get('arguments', '{}')
                            call_id_tool = response.get('call_id', '')

                            logger.info(f"🔧 Voice tool call: {tool_name} (call_id={call_id_tool})")

                            # For transfer_to_agent, set signal directly using our call_sid
                            if tool_name == 'transfer_to_agent':
                                try:
                                    t_args = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
                                except Exception:
                                    t_args = {}
                                t_reason = t_args.get('reason', 'lead requested transfer')
                                t_number = (subscriber.get('voice_config') or {}).get('transfer_number', '')
                                if call_sid and t_number:
                                    _transfer_requests[call_sid] = {
                                        'type': 'transfer',
                                        'target': t_number,
                                        'reason': t_reason,
                                    }

                            # Execute the tool
                            result = execute_voice_tool(
                                tool_name=tool_name,
                                arguments=tool_args,
                                subscriber=subscriber,
                                contact_id=contact_id,
                                first_name=contact_name,
                            )

                            # Send tool result back to XAI
                            tool_output = {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id_tool,
                                    "output": result
                                }
                            }
                            await xai_ws.send(json.dumps(tool_output))

                            # Trigger XAI to generate a response using the tool result
                            await xai_ws.send(json.dumps({"type": "response.create"}))

                            # If a transfer was requested, let the AI finish its
                            # handoff message (response.done), then execute transfer
                            if call_sid and call_sid in _transfer_requests:
                                # Wait for the AI to finish speaking the handoff message
                                # by listening for response.done before transferring
                                _pending_transfer = True

                        # Transcription: user speech -> text
                        elif event_type == 'conversation.item.input_audio_transcription.completed':
                            transcript_text = response.get('transcript', '').strip()
                            if transcript_text:
                                call_transcript.append({"role": "lead", "text": transcript_text})
                                logger.info(f"🎙️ Lead said: {transcript_text[:80]}")

                        # Transcription: AI response -> text
                        elif event_type == 'response.audio_transcript.done':
                            transcript_text = response.get('transcript', '').strip()
                            if transcript_text:
                                call_transcript.append({"role": "assistant", "text": transcript_text})
                                logger.info(f"🎙️ AI said: {transcript_text[:80]}")

                        # Error handling
                        elif event_type == 'error':
                            error_msg = response.get('error', {})
                            logger.error(f"🚨 XAI error: {error_msg}")

                        # response.done — AI finished generating a response
                        elif event_type == 'response.done':
                            # Check for pending transfer or takeover
                            if _pending_transfer and call_sid and call_sid in _transfer_requests:
                                transfer_info = _transfer_requests.pop(call_sid, {})
                                target = transfer_info.get('target', '')
                                t_type = transfer_info.get('type', 'transfer')
                                reason = transfer_info.get('reason', '')
                                logger.info(f"🔄 Executing {t_type}: call {call_sid} -> {target} (reason: {reason})")

                                # Get voice service for transfer commands
                                t_sub_sid = (subscriber.get('voice_config') or {}).get('twilio_sub_account_sid', '')
                                if t_sub_sid and target:
                                    call_active = False
                                    await asyncio.sleep(0.3)
                                    # Transfer via Twilio REST (stops media stream automatically)
                                    host_h = _active_calls.get(call_sid, {}).get('_host', '') or os.getenv('RENDER_EXTERNAL_HOSTNAME', '')
                                    transfer_ok = _twilio_transfer(call_sid, t_sub_sid, target, f"https://{host_h}" if host_h else '')

                                    if transfer_ok:
                                        logger.info(f"🔄 Call transferred to {target}")
                                        # Update call status
                                        if call_sid in _active_calls:
                                            _active_calls[call_sid]['status'] = 'transferred'
                                    else:
                                        logger.error(f"🔄 Transfer failed for {call_sid}")

                                _pending_transfer = False

                            # Check for takeover request (human barge-in)
                            elif call_sid and call_sid in _transfer_requests:
                                transfer_info = _transfer_requests.pop(call_sid, {})
                                if transfer_info.get('type') == 'takeover':
                                    target = transfer_info.get('target', '')
                                    logger.info(f"🔄 Executing takeover: call {call_sid} -> {target}")

                                    t_sub_sid = (subscriber.get('voice_config') or {}).get('twilio_sub_account_sid', '')
                                    if t_sub_sid and target:
                                        call_active = False
                                        await asyncio.sleep(0.3)
                                        host_t = _active_calls.get(call_sid, {}).get('_host', '') or os.getenv('RENDER_EXTERNAL_HOSTNAME', '')
                                        _twilio_transfer(call_sid, t_sub_sid, target, f"https://{host_t}" if host_t else '')
                                        logger.info(f"🔄 Takeover transfer to {target}")
                                        if call_sid in _active_calls:
                                            _active_calls[call_sid]['status'] = 'transferred'

                except websockets.exceptions.ConnectionClosed:
                    logger.info("🎙️ XAI WebSocket closed")
                    call_active = False
                except Exception as e:
                    logger.error(f"🎙️ XAI receive error: {e}")
                    call_active = False

            # Run Twilio↔xAI audio bridge + background context enrichment concurrently
            await asyncio.gather(
                receive_from_twilio(),   # Twilio → xAI (mulaw→PCM16)
                receive_from_xai(),      # xAI → Twilio (PCM16→mulaw)
                enrich_session(),
                return_exceptions=True
            )

    except websockets.exceptions.InvalidStatusCode as e:
        logger.error(f"🚨 XAI connection rejected (status {e.status_code}): {e}")
    except Exception as e:
        logger.error(f"🚨 Voice bridge error: {e}")
    finally:
        logger.info(f"🎙️ Voice stream ended: SID={stream_sid}")
        # Save transcript to call_history if we have any
        if call_sid and call_transcript:
            try:
                save_call_transcript(call_sid, call_transcript)
                logger.info(f"🎙️ Saved transcript ({len(call_transcript)} turns) for call {call_sid}")
            except Exception as e:
                logger.error(f"Failed to save transcript: {e}")
        # Log call end
        try:
            log_webhook_event(
                location_id=subscriber.get("location_id") if subscriber else None,
                contact_id=contact_id,
                event_type="voice_call_ended",
                status="info",
                summary=f"Voice call ended ({direction})",
                details={"stream_sid": stream_sid, "call_sid": call_sid,
                          "transcript_turns": len(call_transcript) if call_transcript else 0}
            )
        except Exception:
            pass


def run_voice_stream(ws):
    """
    Entry point called by flask-sock. Runs the async bridge in a new event loop.
    flask-sock provides a synchronous WebSocket; we run our async bridge inside it.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(handle_voice_stream(ws))
    finally:
        loop.close()


def run_listen_stream(ws):
    """
    WebSocket handler for live listen (speaker mode).
    Receives call_sid from the client, subscribes to audio,
    and forwards mulaw audio chunks to the browser for playback.
    """
    listener_queue = _queue_module.Queue(maxsize=500)
    call_sid = None

    try:
        # First message from browser: { "call_sid": "CAxxxxxx" }
        init_msg = ws.receive()
        if not init_msg:
            return
        init_data = json.loads(init_msg)
        call_sid = init_data.get('call_sid', '')

        if not call_sid:
            ws.send(json.dumps({"error": "call_sid required"}))
            return

        if call_sid not in _active_calls:
            ws.send(json.dumps({"error": "Call not found or already ended"}))
            return

        # Register this listener
        if call_sid not in _call_listeners:
            _call_listeners[call_sid] = set()
        _call_listeners[call_sid].add(listener_queue)
        logger.info(f"Live listen started for call {call_sid[:16]}")

        ws.send(json.dumps({"status": "listening", "call_sid": call_sid}))

        # Forward audio chunks to browser
        while True:
            try:
                # Block for up to 2 seconds waiting for audio
                chunk = listener_queue.get(timeout=2)
                # Send as JSON with mulaw base64 payload
                ws.send(json.dumps({"audio": chunk}))
            except _queue_module.Empty:
                # Check if call is still active
                if call_sid not in _active_calls or \
                        _active_calls.get(call_sid, {}).get('status') in ('completed', 'failed', 'canceled', 'transferred'):
                    ws.send(json.dumps({"status": "call_ended"}))
                    break
                # Send keepalive
                try:
                    ws.send(json.dumps({"keepalive": True}))
                except Exception:
                    break
            except Exception:
                break

    except Exception as e:
        logger.debug(f"Listen stream ended: {e}")
    finally:
        # Unregister listener
        if call_sid and call_sid in _call_listeners:
            _call_listeners[call_sid].discard(listener_queue)
            if not _call_listeners[call_sid]:
                del _call_listeners[call_sid]
        logger.info(f"Live listen ended for call {call_sid[:16] if call_sid else 'none'}")


# ──────────────────────────────────────────────────────────────
# ROUTE: Test voice connection (health check)
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/test', methods=['POST'])
def test_voice_connection():
    """Test that XAI and Voice credentials are valid."""
    data = request.json or {}
    location_id = data.get('location_id', '')

    results = {"xai": False, "twilio": False, "errors": []}

    # Test XAI API key
    if XAI_API_KEY:
        try:
            import httpx
            resp = httpx.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}"},
                json={"model": "grok-3-mini-fast", "messages": [{"role": "user", "content": "test"}], "max_tokens": 5},
                timeout=10,
            )
            results["xai"] = resp.status_code == 200
            if resp.status_code != 200:
                results["errors"].append(f"XAI API returned {resp.status_code}")
        except Exception as e:
            results["errors"].append(f"XAI connection failed: {str(e)}")
    else:
        results["errors"].append("XAI_API_KEY not configured")

    # Test voice service via Twilio sub-account
    if location_id:
        subscriber = _get_subscriber_by_location(location_id)
        if subscriber:
            voice_config = subscriber.get("voice_config") or {}
            sub_sid = voice_config.get("twilio_sub_account_sid", "")
            if sub_sid:
                try:
                    client = twilio_provisioning.get_sub_account_client(sub_sid)
                    account = client.api.accounts(sub_sid).fetch()
                    results["twilio"] = account.status == "active"
                    if account.status != "active":
                        results["errors"].append(f"Voice sub-account status: {account.status}")
                except Exception as e:
                    results["errors"].append(f"Voice service check failed: {str(e)}")
            else:
                results["errors"].append("Voice service not provisioned")

    return jsonify(results)


@voice_bp.route('/voice/preview/<voice_name>', methods=['GET'])
@login_required
def preview_voice(voice_name):
    """Generate a short audio sample for the selected voice."""
    voice = VOICE_OPTIONS.get(voice_name.lower(), DEFAULT_VOICE)

    if not XAI_API_KEY:
        return jsonify({"error": "XAI API key not configured"}), 400

    loop = asyncio.new_event_loop()
    try:
        audio_data = loop.run_until_complete(_generate_voice_preview(voice))
    finally:
        loop.close()

    if not audio_data:
        return jsonify({"error": "Failed to generate preview"}), 500

    # Audio is now L16 PCM 16kHz (same as live calls) — wrap in WAV container
    wav_data = _pcm16_to_wav(audio_data, sample_rate=16000)
    return Response(wav_data, content_type='audio/wav',
                    headers={'Cache-Control': 'public, max-age=3600'})


# ──────────────────────────────────────────────────────────────
# CALL PANEL: Power dialer with GHL contact search + queue
# ──────────────────────────────────────────────────────────────

GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"


@voice_bp.route('/voice/call-panel')
@login_required
def call_panel():
    """Serve the call panel page (works standalone or in iframe)."""
    conn = get_db_connection()
    if not conn:
        return "Database error", 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config, bot_first_name FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return "Account not found", 404
        location_id = row['location_id'] or ''
        voice_config = row['voice_config'] or {}
        bot_name = row['bot_first_name'] or 'AI Agent'
        return render_template('call_panel.html',
            location_id=location_id,
            voice_config=voice_config,
            bot_name=bot_name
        )
    finally:
        return_db_connection(conn)


@voice_bp.route('/voice/contacts', methods=['GET'])
@login_required
def fetch_contacts():
    """
    Fetch ALL contacts from GHL (paginates automatically).
    Query params: q (search query), pipeline, stage
    """
    query = request.args.get('q', '').strip()
    pipeline_id = request.args.get('pipeline', '').strip()
    stage_id = request.args.get('stage', '').strip()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid auth token. Reconnect your CRM."}), 401

    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Version": GHL_API_VERSION,
            "Content-Type": "application/json"
        }

        all_contacts = []
        page_limit = 100  # GHL max per page
        max_pages = 50    # Safety cap: 5000 contacts max

        if pipeline_id or stage_id:
            # GHL contacts/search does NOT support pipeline/stage filters.
            # Use the Opportunities API to find contacts in a pipeline/stage,
            # then fetch full contact details for each.
            page = 1
            seen_contact_ids = set()
            while page <= max_pages:
                opp_params = {
                    "location_id": location_id,
                    "pipeline_id": pipeline_id,
                    "limit": page_limit,
                    "page": page,
                }
                if stage_id:
                    opp_params["pipeline_stage_id"] = stage_id
                if query:
                    opp_params["q"] = query

                resp = http_requests.get(
                    f"{GHL_API_BASE}/opportunities/search",
                    headers=headers, params=opp_params, timeout=15,
                )
                if resp.status_code != 200:
                    logger.warning(f"Opportunities search returned {resp.status_code}: {resp.text[:300]}")
                    if not all_contacts:
                        return jsonify({"error": f"CRM returned {resp.status_code}"}), resp.status_code
                    break

                data = resp.json()
                opportunities = data.get("opportunities", [])
                if not opportunities:
                    break

                for opp in opportunities:
                    contact = opp.get("contact", {})
                    contact_id = contact.get("id", "")
                    if contact_id and contact_id not in seen_contact_ids:
                        seen_contact_ids.add(contact_id)
                        # Build a contact-like dict from the opportunity's contact data
                        all_contacts.append({
                            "id": contact_id,
                            "firstName": contact.get("name", "").split(" ")[0] if contact.get("name") else "",
                            "lastName": " ".join(contact.get("name", "").split(" ")[1:]) if contact.get("name") else "",
                            "phone": contact.get("phone", ""),
                            "email": contact.get("email", ""),
                            "tags": contact.get("tags", []),
                            "dateAdded": contact.get("dateAdded", ""),
                        })

                meta = data.get("meta", {})
                if len(opportunities) < page_limit or not meta.get("nextPage"):
                    break
                page += 1
        else:
            # GET /contacts/ with startAfterId pagination
            start_after = None
            for _ in range(max_pages):
                params = {"locationId": location_id, "limit": page_limit}
                if query:
                    params["query"] = query
                if start_after:
                    params["startAfterId"] = start_after

                resp = http_requests.get(f"{GHL_API_BASE}/contacts/", headers=headers, params=params, timeout=15)
                if resp.status_code != 200:
                    if not all_contacts:
                        return jsonify({"error": f"CRM returned {resp.status_code}"}), resp.status_code
                    break

                data = resp.json()
                contacts = data.get("contacts", [])
                if not contacts:
                    break
                all_contacts.extend(contacts)
                # GHL uses startAfterId for cursor pagination
                meta = data.get("meta", {})
                start_after = meta.get("startAfterId") or meta.get("nextPageUrl")
                if not start_after or len(contacts) < page_limit:
                    break

        logger.info(f"Fetched {len(all_contacts)} total contacts for {location_id}")

        # Return simplified contact list
        result = []
        for c in all_contacts:
            phone = c.get("phone", "")
            if not phone:
                continue
            result.append({
                "id": c.get("id", ""),
                "name": f"{c.get('firstName', '')} {c.get('lastName', '')}".strip() or "Unknown",
                "firstName": c.get("firstName", ""),
                "lastName": c.get("lastName", ""),
                "phone": phone,
                "email": c.get("email", ""),
                "tags": c.get("tags", []),
                "dateAdded": c.get("dateAdded", ""),
            })

        return jsonify({"contacts": result, "total": len(result)})

    except Exception as e:
        logger.error(f"Failed to fetch contacts: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/dial', methods=['POST'])
@login_required
def dial_contact():
    """
    Initiate an outbound call to a specific contact via Twilio.
    Used by the call panel. Returns call_sid for status tracking.
    """
    data = request.json or {}
    contact_id    = data.get('contact_id', '')
    phone         = data.get('phone', '')
    first_name    = data.get('first_name', 'there')
    dial_mode     = data.get('dial_mode', 'ai')
    dial_attempt  = int(data.get('dial_attempt', 1))

    if not phone:
        return jsonify({"error": "Phone number is required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        subscriber = dict(row)
    finally:
        return_db_connection(conn)

    location_id  = subscriber.get('location_id', '')
    voice_config = subscriber.get('voice_config') or {}

    # Enforce max dial attempts server-side
    max_attempts = int(voice_config.get('dial_attempts', 2))
    if dial_attempt > max_attempts:
        logger.warning(f"Blocked dial attempt {dial_attempt} > max {max_attempts} for {current_user.email}")
        return jsonify({"error": f"Max dial attempts ({max_attempts}) exceeded"}), 400

    if dial_mode == 'ai' and not voice_config.get('enabled'):
        return jsonify({"error": "Voice AI is not enabled. Enable it in the Voice tab."}), 400

    sub_sid      = voice_config.get('twilio_sub_account_sid', '')
    from_number  = voice_config.get('twilio_phone_number', '')

    # Local presence: pick a number matching the destination area code
    local_presence_enabled = voice_config.get('local_presence', False)
    if local_presence_enabled:
        dest_area = phone.lstrip('+').lstrip('1')[:3] if phone else ''
        local_pool = voice_config.get('local_presence_numbers', [])
        for lp_num in local_pool:
            lp_area = lp_num.lstrip('+').lstrip('1')[:3]
            if lp_area == dest_area:
                from_number = lp_num
                break

    if not sub_sid or not from_number:
        return jsonify({"error": "Voice service not fully provisioned"}), 400

    use_amd = dial_mode == 'ai'

    try:
        host = request.host
        webhook_base_url = f"https://{host}"

        custom_params = {
            'location_id':  location_id,
            'caller':       from_number,
            'called':       phone,
            'direction':    'outbound',
            'contact_id':   contact_id,
            'contact_name': first_name,
            'dial_mode':    dial_mode,
        }

        result = twilio_provisioning.create_outbound_call(
            sub_account_sid=sub_sid,
            to=phone,
            from_number=from_number,
            webhook_base_url=webhook_base_url,
            machine_detection='DetectMessageEnd' if use_amd else None,
            custom_params=custom_params,
        )
        call_sid = result.get('call_sid', '')

        _active_calls[call_sid] = {
            "status":     "initiated",
            "duration":   0,
            "contact_id": contact_id,
            "phone":      phone,
            "name":       first_name,
            "dial_mode":  dial_mode,
            "attempt":    dial_attempt,
            "_location_id": location_id,
            "_sub_sid":     sub_sid,
            "_host":        request.host,
        }

        save_call_to_history(
            location_id=location_id,
            call_sid=call_sid,
            phone=phone,
            contact_id=contact_id,
            contact_name=first_name,
            direction='outbound',
            status='initiated'
        )

        logger.info(f"Dialer call [{dial_mode}]: {from_number} -> {phone} ({first_name}) attempt={dial_attempt} sid={call_sid}")
        return jsonify({"status": "calling", "call_sid": call_sid, "dial_mode": dial_mode})

    except Exception as e:
        logger.error(f"Dialer call failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/call-status/<call_sid>', methods=['GET'])
@login_required
def get_call_status(call_sid):
    """Poll call status for the dialer queue."""
    if call_sid in _active_calls:
        info = _active_calls[call_sid]
        # For terminal states, mark for cleanup but don't delete yet (allow re-polls)
        if info["status"] in ("completed", "busy", "no-answer", "failed", "canceled"):
            poll_count = info.get('_terminal_polls', 0) + 1
            info['_terminal_polls'] = poll_count
            # Clean up after 3 polls of a terminal state (gives frontend time)
            if poll_count >= 3:
                status_copy = dict(info)
                del _active_calls[call_sid]
                return jsonify(status_copy)
        return jsonify(info)
    return jsonify({"status": "unknown"}), 404


# ──────────────────────────────────────────────────────────────
# ROUTE: Hang up an active call from the dialer UI
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/hangup', methods=['POST'])
@login_required
def hangup_active_call():
    """Hang up the currently active call."""
    data = request.json or {}
    call_sid = data.get('call_sid', '')
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        subscriber = dict(row)
    finally:
        return_db_connection(conn)

    sub_sid = (subscriber.get('voice_config') or {}).get('twilio_sub_account_sid', '')
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    success = _twilio_hangup(call_sid, sub_sid)
    if call_sid in _active_calls:
        _active_calls[call_sid]['status'] = 'canceled'

    # Persist canceled status to DB so call history is accurate
    try:
        update_call_history_status(call_sid, 'canceled', 0)
    except Exception as e:
        logger.warning(f"Hangup DB persist failed for {call_sid}: {e}")

    if success:
        return jsonify({"status": "hung_up"})
    # Even if Twilio hangup fails (call may have already ended), still return success
    # since we've already updated our state
    return jsonify({"status": "hung_up", "note": "call may have already ended"})


# ──────────────────────────────────────────────────────────────
# ROUTE: Recording status callback from Twilio
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/recording-status', methods=['POST'])
def recording_status_callback():
    """Twilio posts recording status events here as form data."""
    call_sid = request.values.get('CallSid', '')
    recording_sid = request.values.get('RecordingSid', '')
    recording_url = request.values.get('RecordingUrl', '')
    recording_status = request.values.get('RecordingStatus', '')
    recording_duration = request.values.get('RecordingDuration', '0')

    logger.info(f"Recording callback: SID={call_sid} rec={recording_sid} status={recording_status} dur={recording_duration}s")

    if recording_status == 'completed' and (recording_url or recording_sid):
        # Use our proxy URL for Twilio recordings
        store_url = f"/voice/recording/{recording_sid}" if recording_sid else recording_url

        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE call_history
                    SET recording_url = %s, recording_sid = %s, duration = COALESCE(NULLIF(duration, 0), %s)
                    WHERE call_sid = %s
                """, (store_url, recording_sid, int(recording_duration or 0), call_sid))
                conn.commit()
                cur.close()
                logger.info(f"Recording saved for call {call_sid}: {store_url}")
            except Exception as e:
                logger.error(f"Failed to save recording: {e}")
                conn.rollback()
            finally:
                return_db_connection(conn)

    return '', 204


@voice_bp.route('/voice/transcription', methods=['POST'])
def transcription_webhook():
    """
    Twilio posts transcription events here.
    Accumulates transcript segments and persists them to call_history on call end.
    """
    call_sid   = request.values.get('CallSid', '')
    transcript = request.values.get('TranscriptionText', '')
    transcription_status = request.values.get('TranscriptionStatus', 'completed')

    if not call_sid or not transcript:
        return '', 204

    logger.info(f"📝 Transcription [{transcription_status}] {call_sid}: {transcript[:80]}")

    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            # Append to existing transcript stored as JSONB array in call_history
            cur.execute("""
                UPDATE call_history
                SET transcript = COALESCE(transcript::jsonb, '[]'::jsonb) || %s::jsonb
                WHERE call_sid = %s
            """, (json.dumps([{"role": "auto", "text": transcript}]), call_sid))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.warning(f"Transcription save failed for {call_sid}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            return_db_connection(conn)

    return '', 204


@voice_bp.route('/voice/recording/<recording_sid>', methods=['GET'])
@login_required
def stream_recording(recording_sid):
    """
    Proxy a Twilio recording as an MP3 download. Fetches with Twilio auth
    so the browser never sees expiring pre-signed S3 URLs.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "No Twilio account configured"}), 400

    # Build the authenticated Twilio recording URL
    mp3_url = twilio_provisioning.get_recording_url(sub_sid, recording_sid)

    try:
        # Fetch with Twilio master credentials (streams the MP3 bytes through us)
        tw_resp = http_requests.get(
            mp3_url,
            auth=(twilio_provisioning.TWILIO_ACCOUNT_SID,
                  twilio_provisioning.TWILIO_AUTH_TOKEN),
            stream=True,
            timeout=30,
        )
        if tw_resp.status_code != 200:
            logger.error(f"Twilio recording fetch failed: {tw_resp.status_code} for {recording_sid}")
            return jsonify({"error": "Recording not available"}), tw_resp.status_code

        # Stream the audio back to the browser
        def generate():
            for chunk in tw_resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        headers = {}
        # Only force download when ?dl=1 is passed (download button)
        if request.args.get('dl'):
            headers['Content-Disposition'] = f'attachment; filename="recording-{recording_sid}.mp3"'

        return Response(
            generate(),
            content_type=tw_resp.headers.get('Content-Type', 'audio/mpeg'),
            headers=headers,
        )
    except Exception as e:
        logger.error(f"Failed to proxy recording {recording_sid}: {e}")
        return jsonify({"error": "Failed to fetch recording"}), 500


# ──────────────────────────────────────────────────────────────
# ROUTE: Lightweight ping for latency measurement
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/ping', methods=['GET', 'HEAD'])
@login_required
def voice_ping():
    """Lightweight endpoint for the dialer to measure latency."""
    return '', 204


# ──────────────────────────────────────────────────────────────
# ROUTE: Live takeover — agent barges into an active AI call
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/takeover', methods=['POST'])
@login_required
def voice_takeover():
    """
    Let a human agent take over an active AI call.
    Supports two modes:
    1. VoIP (browser): Redirects call to <Dial><Client>agent_{location_id}</Client></Dial>
    2. Phone: Transfers to the agent's phone number via <Dial>{number}</Dial>
    """
    data = request.json or {}
    call_sid = data.get('call_sid', '')
    use_voip = data.get('use_voip', False)
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    # Look up subscriber info
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        voice_cfg = row.get('voice_config') or {}
        location_id = row.get('location_id', '')
    finally:
        return_db_connection(conn)

    sub_sid = voice_cfg.get('twilio_sub_account_sid', '')
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    # Verify the call is actually active
    if call_sid not in _active_calls:
        return jsonify({"error": "Call not found or already ended"}), 404

    call_info = _active_calls[call_sid]
    if call_info.get('status') in ('completed', 'failed', 'transferred', 'no-answer'):
        return jsonify({"error": f"Call already in terminal state: {call_info.get('status')}"}), 400

    host = request.host

    if use_voip and location_id:
        # VoIP intercept: redirect call to browser client
        identity = f"agent_{location_id}"
        target = f"client:{identity}"
        logger.info(f"Takeover (VoIP): redirecting call {call_sid} to browser client={identity}")

        # Signal the WebSocket bridge to stop the AI audio loop
        _transfer_requests[call_sid] = {
            'type': 'takeover',
            'target': target,
            'reason': 'Agent initiated VoIP intercept',
        }

        # Redirect the call to TwiML that dials the browser client
        try:
            client = twilio_provisioning.get_sub_account_client(sub_sid)
            client.calls(call_sid).update(
                url=f"https://{host}/voice/intercept-twiml?identity={identity}",
                method="POST",
            )
            _active_calls[call_sid]['status'] = 'transferred'
            logger.info(f"Takeover (VoIP): call {call_sid} redirected to {identity}")
            return jsonify({"status": "transferred", "call_sid": call_sid, "target": "Browser (VoIP)"})
        except Exception as e:
            logger.error(f"Takeover (VoIP): redirect FAILED for call {call_sid}: {e}")
            _transfer_requests.pop(call_sid, None)
            return jsonify({"error": f"Intercept failed: {e}"}), 400
    else:
        # Phone intercept: transfer to agent's phone number
        target = data.get('target') or voice_cfg.get('transfer_number', '')
        if not target:
            return jsonify({"error": "No transfer number configured and VoIP not available. Set a Transfer Number in Voice Settings or enable VoIP."}), 400

        # Normalize target
        if not target.startswith('+'):
            target = '+1' + target.lstrip('1') if len(target.replace('-','').replace(' ','')) <= 10 else '+' + target

        logger.info(f"Takeover (phone): executing transfer for call {call_sid} -> {target}")

        # Signal the WebSocket bridge to stop the AI audio loop
        _transfer_requests[call_sid] = {
            'type': 'takeover',
            'target': target,
            'reason': 'Agent initiated live takeover',
        }

        # Transfer the live call — Twilio automatically stops the media stream
        transfer_ok = _twilio_transfer(call_sid, sub_sid, target, f"https://{host}")
        if transfer_ok:
            logger.info(f"Takeover (phone): call {call_sid} transferred to {target}")
            _active_calls[call_sid]['status'] = 'transferred'
            return jsonify({"status": "transferred", "call_sid": call_sid, "target": target})
        else:
            logger.error(f"Takeover (phone): transfer FAILED for call {call_sid} -> {target}")
            _transfer_requests.pop(call_sid, None)
            return jsonify({"error": "Transfer failed — the call may have ended."}), 400


# ──────────────────────────────────────────────────────────────
# ROUTE: Live transfer to another phone number
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/transfer', methods=['POST'])
@login_required
def voice_transfer():
    """Transfer an active call to another phone number via Twilio."""
    data = request.json or {}
    call_sid = data.get('call_sid', '')
    transfer_to = data.get('transfer_to', '').strip()

    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400
    if not transfer_to:
        return jsonify({"error": "transfer_to number required"}), 400

    # Normalize phone number
    if not transfer_to.startswith('+'):
        transfer_to = '+1' + transfer_to.lstrip('1') if len(transfer_to.replace('-','').replace(' ','')) <= 10 else '+' + transfer_to

    # Verify the call is active
    if call_sid not in _active_calls:
        return jsonify({"error": "Call not found or already ended"}), 404

    call_info = _active_calls[call_sid]
    if call_info.get('status') in ('completed', 'failed', 'transferred', 'no-answer'):
        return jsonify({"error": f"Call already in terminal state: {call_info.get('status')}"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        voice_cfg = (row['voice_config'] if row else None) or {}
    finally:
        return_db_connection(conn)

    sub_sid = voice_cfg.get('twilio_sub_account_sid', '')
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    host = request.host
    success = _twilio_transfer(call_sid, sub_sid, transfer_to, f"https://{host}")
    if success:
        _active_calls[call_sid]['status'] = 'transferred'
        # Persist transfer status to DB
        try:
            update_call_history_status(call_sid, 'completed', 0)
        except Exception as e:
            logger.warning(f"Transfer DB persist failed for {call_sid}: {e}")
        logger.info(f"Live transfer: call {call_sid} -> {transfer_to}")
        return jsonify({"status": "transferred", "call_sid": call_sid, "transfer_to": transfer_to})

    return jsonify({"error": "Transfer failed — call may have ended"}), 400


# ──────────────────────────────────────────────────────────────
# ROUTE: Call disposition
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/call-disposition', methods=['POST'])
@login_required
def set_call_disposition():
    """Save a disposition for a completed call."""
    data = request.json or {}
    call_sid = data.get('call_sid', '')
    disposition = data.get('disposition', '')
    if not call_sid or not disposition:
        return jsonify({"error": "call_sid and disposition required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        # Try to add disposition column if it doesn't exist
        try:
            cur.execute("""
                ALTER TABLE call_history ADD COLUMN IF NOT EXISTS disposition TEXT DEFAULT NULL
            """)
            conn.commit()
        except Exception:
            conn.rollback()
        cur.execute("""
            UPDATE call_history SET disposition = %s WHERE call_sid = %s
        """, (disposition, call_sid))
        conn.commit()
        cur.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Failed to save disposition: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)


# ──────────────────────────────────────────────────────────────
# ROUTE: Call history API
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/call-history', methods=['GET'])
@login_required
def get_call_history():
    """Fetch call history for the current user."""
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']

        # Ensure disposition column exists
        try:
            cur.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS disposition TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()

        cur.execute("""
            SELECT id, contact_id, contact_name, phone, direction, call_sid,
                   status, duration, recording_url, recording_sid, transcript,
                   started_at, ended_at, created_at,
                   COALESCE(disposition, '') as disposition
            FROM call_history
            WHERE location_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (location_id, limit, offset))
        rows = cur.fetchall()
        cur.close()

        calls = []
        for r in rows:
            call = dict(r)
            # Convert timestamps to ISO strings
            for ts_field in ('started_at', 'ended_at', 'created_at'):
                if call.get(ts_field):
                    call[ts_field] = call[ts_field].isoformat()
            calls.append(call)

        return jsonify({"calls": calls, "total": len(calls)})
    except Exception as e:
        logger.error(f"Failed to fetch call history: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)


def save_call_to_history(location_id, call_sid, phone, contact_id=None,
                         contact_name=None, direction='outbound', status='initiated'):
    """Save a new call record to the call_history table."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO call_history (location_id, contact_id, contact_name, phone,
                                      direction, call_sid, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (call_sid) DO NOTHING
        """, (location_id, contact_id, contact_name, phone, direction, call_sid, status))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to save call history: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def update_call_history_status(call_sid, status, duration=0):
    """Update call status and duration in call_history."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        ended_clause = ", ended_at = NOW()" if status in ('completed', 'busy', 'no-answer', 'failed', 'canceled') else ""
        cur.execute(f"""
            UPDATE call_history
            SET status = %s, duration = %s{ended_clause}
            WHERE call_sid = %s
        """, (status, int(duration or 0), call_sid))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to update call history: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def save_call_transcript(call_sid, transcript):
    """Save transcript JSON to call_history."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE call_history SET transcript = %s WHERE call_sid = %s
        """, (json.dumps(transcript), call_sid))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to save transcript: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


# ──────────────────────────────────────────────────────────────
# HELPER: Get subscriber + Twilio client for current user
# ──────────────────────────────────────────────────────────────

def _get_current_subscriber_voice():
    """Get subscriber, voice_config, and sub_account_sid for the logged-in user."""
    conn = get_db_connection()
    if not conn:
        return None, None, None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None, None, None
        subscriber = dict(row)
        vc = subscriber.get('voice_config') or {}
        sub_sid = vc.get('twilio_sub_account_sid', '')
        return subscriber, vc, sub_sid or None
    except Exception as e:
        logger.error(f"_get_current_subscriber_voice: {e}")
        return None, None, None
    finally:
        return_db_connection(conn)


def _save_voice_config(email, voice_config):
    """Persist voice_config JSON for a subscriber."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE subscribers SET voice_config = %s WHERE email = %s",
            (json.dumps(voice_config), email)
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"Failed to save voice_config: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        return_db_connection(conn)


# ──────────────────────────────────────────────────────────────
# ONE-CLICK VOICE ACTIVATION: Auto-provision Twilio sub-account
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/automate-setup', methods=['POST'])
@login_required
def automate_voice_setup():
    """
    One-click voice activation. Creates a Twilio sub-account and TwiML app.
    The user buys their own phone number afterwards via the Numbers tab.
    """
    subscriber, vc, _ = _get_current_subscriber_voice()
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    location_id = subscriber.get('location_id', 'unknown')
    host = request.host
    webhook_base_url = f"https://{host}"

    # Check if already provisioned (guard against duplicate clicks)
    existing_sub_sid = vc.get('twilio_sub_account_sid', '')
    if existing_sub_sid:
        # If super_admin was incorrectly provisioned with a sub-account, re-provision with master
        if current_user.is_super_admin and existing_sub_sid != TWILIO_ACCOUNT_SID:
            logger.info(f"[activate] Super admin {current_user.email} has sub-account {existing_sub_sid}, re-provisioning with master account")
            # Clear old voice_config but preserve phone number if any
            old_phone = vc.get('twilio_phone_number', '')
            old_number_sid = vc.get('twilio_number_sid', '')
            vc.clear()
            vc['enabled'] = False
            if old_phone:
                vc['twilio_phone_number'] = old_phone
                vc['twilio_number_sid'] = old_number_sid
            _save_voice_config(current_user.email, vc)
            # Fall through to re-provision below
        else:
            return jsonify({
                "status": "success",
                "message": "Voice service already active!",
                "twilio_phone_number": vc.get('twilio_phone_number', ''),
            })

    try:
        # Only the platform owner (super_admin) uses the master Twilio account.
        # Everyone else — individual, agency_owner, any tier — gets a sub-account.
        if current_user.is_super_admin:
            result = twilio_provisioning.provision_master(
                webhook_base_url=webhook_base_url,
            )
        else:
            result = twilio_provisioning.provision_subscriber(
                subscriber_email=current_user.email,
                location_id=location_id,
                webhook_base_url=webhook_base_url,
            )

        # Save all provisioned IDs to voice_config
        vc.update(result)
        vc['enabled'] = True
        _save_voice_config(current_user.email, vc)

        logger.info(f"Voice activated for {current_user.email}: sub={result.get('twilio_sub_account_sid')}")

        phone = result.get('twilio_phone_number', '')
        if phone:
            msg = "Voice service activated!"
        else:
            msg = "Voice account created! Now buy a phone number in the Numbers tab."

        return jsonify({
            "status": "success",
            "message": msg,
            "twilio_phone_number": phone,
        })

    except Exception as e:
        logger.error(f"Voice activation error: {e}", exc_info=True)
        _save_voice_config(current_user.email, vc)
        return jsonify({"error": f"Activation failed: {str(e)}"}), 500


# ──────────────────────────────────────────────────────────────
# BROWSER VoIP: Twilio Client JS SDK support
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/setup-voip', methods=['POST'])
@login_required
def setup_voip():
    """
    Browser VoIP setup — verifies the TwiML app is configured for browser calling.
    With Twilio, the TwiML app is created during provisioning so this just confirms readiness.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    location_id = subscriber.get('location_id', 'unknown') if subscriber else 'unknown'
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    twiml_app_sid = vc.get('twilio_twiml_app_sid', '')
    if not twiml_app_sid:
        return jsonify({"error": "Voice not fully provisioned. Click Activate Voice first."}), 400

    logger.info(f"[setup-voip] Ready for location_id={location_id} twiml_app={twiml_app_sid}")
    return jsonify({"status": "ready", "credential_id": twiml_app_sid})


@voice_bp.route('/voice/token', methods=['POST'])
@login_required
def generate_voice_token():
    """Generate a short-lived Twilio Access Token for browser-based calling."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    twiml_app_sid = vc.get('twilio_twiml_app_sid', '')
    location_id = subscriber.get('location_id', 'unknown') if subscriber else 'unknown'
    if not twiml_app_sid:
        return jsonify({"error": "Browser calling not set up. Activate voice first."}), 400

    try:
        # Auto-create per-sub-account API key if missing (for subscribers provisioned before this feature)
        api_key_sid = vc.get('twilio_api_key_sid', '')
        api_key_secret = vc.get('twilio_api_key_secret', '')
        if not api_key_sid or not api_key_secret:
            logger.info(f"[voice/token] No per-subscriber API key found for {sub_sid}, creating one...")
            api_key_data = twilio_provisioning.create_api_key(sub_sid)
            api_key_sid = api_key_data["api_key_sid"]
            api_key_secret = api_key_data["api_key_secret"]
            vc['twilio_api_key_sid'] = api_key_sid
            vc['twilio_api_key_secret'] = api_key_secret
            _save_voice_config(current_user.email, vc)
            logger.info(f"[voice/token] Created and saved API key {api_key_sid} for {sub_sid}")

        # Ensure TwiML app + phone number webhooks point to the current server
        # (fixes stale URLs after domain/deployment changes)
        webhook_base_url = f"https://{request.host}"
        twilio_provisioning.update_twiml_app(sub_sid, twiml_app_sid, webhook_base_url)
        number_sid = vc.get('twilio_number_sid', '')
        if number_sid:
            twilio_provisioning.update_phone_number_webhooks(sub_sid, number_sid, webhook_base_url)

        identity = f"agent_{location_id}"
        token = twilio_provisioning.generate_voice_token(
            identity=identity,
            twiml_app_sid=twiml_app_sid,
            sub_account_sid=sub_sid,
            api_key_sid=api_key_sid,
            api_key_secret=api_key_secret,
        )
        logger.info(f"[voice/token] Token issued for {identity} (twiml_app={twiml_app_sid}, webhook={webhook_base_url})")
        return jsonify({"token": token, "identity": identity})

    except Exception as e:
        logger.error(f"[voice/token] Token generation failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/voip-answer', methods=['POST'])
def voip_answer():
    """Legacy endpoint — browser calls now go through /voice/inbound via TwiML app."""
    return jsonify({'result': 'ok'}), 200


# ──────────────────────────────────────────────────────────────
# PHONE NUMBER MANAGEMENT (via Twilio sub-account)
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/numbers', methods=['GET'])
@login_required
def list_voice_numbers():
    """List all phone numbers on the subscriber's Twilio sub-account."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned. Click Activate Voice first."}), 400

    nicknames = vc.get('number_nicknames', {})
    primary_number = vc.get('twilio_phone_number', '')

    try:
        numbers = twilio_provisioning.list_phone_numbers(sub_sid)
        result = []
        for n in numbers:
            phone = n.get('phone', '')
            is_primary = phone == primary_number
            nickname = nicknames.get(phone, '')
            caps = n.get('capabilities', {})
            result.append({
                "sid": n.get('sid', ''),
                "phone": phone,
                "nickname": nickname,
                "is_primary": is_primary,
                "capabilities": {
                    "voice": caps.get('voice', False),
                    "sms": caps.get('sms', False),
                    "mms": caps.get('mms', False),
                    "fax": caps.get('fax', False),
                },
                "status": n.get('status', 'active'),
                "created_at": n.get('created_at', ''),
            })

        return jsonify({"numbers": result, "total": len(result)})

    except Exception as e:
        logger.error(f"Failed to list numbers: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/search', methods=['GET'])
@login_required
def search_available_numbers():
    """Search for available phone numbers to purchase."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    area_code = request.args.get('area_code', '')
    state = request.args.get('state', '')
    city = request.args.get('city', '')
    zip_code = request.args.get('zip_code', '')
    contains = request.args.get('contains', '')
    number_type = request.args.get('number_type', 'local')

    try:
        numbers = twilio_provisioning.search_available_numbers(
            number_type=number_type,
            area_code=area_code,
            state=state,
            city=city,
            zip_code=zip_code,
            contains=contains,
        )
        return jsonify({"numbers": numbers, "total": len(numbers)})

    except Exception as e:
        logger.error(f"Number search failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/buy', methods=['POST'])
@login_required
def buy_voice_number():
    """Purchase a phone number and configure it for voice."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    phone_number = data.get('phone_number', '')
    if not phone_number:
        return jsonify({"error": "Phone number is required"}), 400

    twiml_app_sid = vc.get('twilio_twiml_app_sid', '')
    host = request.host
    webhook_base_url = f"https://{host}"

    try:
        result = twilio_provisioning.buy_phone_number(
            sub_account_sid=sub_sid,
            phone_number=phone_number,
            webhook_base_url=webhook_base_url,
            twiml_app_sid=twiml_app_sid,
        )
        logger.info(f"Purchased number: {result.get('phone')} (SID: {result.get('sid')})")

        return jsonify({
            "status": "purchased",
            "phone": result.get("phone", phone_number),
            "sid": result.get("sid", ""),
        })

    except Exception as e:
        logger.error(f"Number purchase failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/release', methods=['POST'])
@login_required
def release_voice_number():
    """Release a phone number from the sub-account."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    phone_sid = data.get('sid', '')

    if not phone_sid:
        return jsonify({"error": "Number SID is required"}), 400

    try:
        success = twilio_provisioning.release_phone_number(sub_sid, phone_sid)
        if success:
            logger.info(f"Released number: {phone_sid}")
            return jsonify({"status": "released"})
        return jsonify({"error": "Release failed"}), 400

    except Exception as e:
        logger.error(f"Number release failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/trust-hub', methods=['GET'])
@login_required
def get_trust_hub_status():
    """Get number health and carrier trust status."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned. Click Activate Voice first."}), 400

    trust_hub = vc.get('trust_hub', {})
    business_name = trust_hub.get('business_name', '')
    ein = trust_hub.get('ein', '')

    result = {
        "stir_shaken": {
            "status": "auto_managed",
            "attestation": "A",
            "note": "STIR/SHAKEN attestation is automatically handled for verified business numbers. Full (A) attestation means carriers trust your calls.",
        },
        "business_profile": {
            "business_name": business_name,
            "ein": ein,
            "registered": bool(business_name and ein),
        },
        "carrier_registration": {
            "free_caller_registry": {
                "name": "Free Caller Registry",
                "url": "https://www.freecallerregistry.com/fcr/",
                "status": trust_hub.get('fcr_status', 'not_registered'),
                "description": "Cross-carrier registry that links your number to your business. Recommended first step.",
            },
            "att_hiya": {
                "name": "AT&T / Hiya",
                "url": "https://hiya.com/branded-call/",
                "status": trust_hub.get('att_status', 'not_registered'),
                "description": "Register with Hiya to display your business name on AT&T devices and reduce spam flags.",
            },
            "tmobile": {
                "name": "T-Mobile",
                "url": "https://callhub.t-mobile.com/",
                "status": trust_hub.get('tmobile_status', 'not_registered'),
                "description": "T-Mobile Verified Caller — display verified business name to T-Mobile subscribers.",
            },
            "verizon": {
                "name": "Verizon",
                "url": "https://www.verizon.com/business/products/security/spam-call-protection/",
                "status": trust_hub.get('verizon_status', 'not_registered'),
                "description": "Register with Verizon to prevent spam flagging on their network.",
            },
        },
        "numbers": [],
        "cnam_info": {
            "description": "CNAM (Caller Name) displays your business name on recipient phones. Register via Spam Protection tab.",
        },
    }

    try:
        numbers = twilio_provisioning.list_phone_numbers(sub_sid)
        for n in numbers:
            result["numbers"].append({
                "phone": n.get('phone', ''),
                "id": n.get('sid', ''),
                "status": n.get('status', 'active'),
                "friendly_name": n.get('friendly_name', ''),
            })
        return jsonify(result)

    except Exception as e:
        logger.error(f"Trust hub check failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/<number_id>/cnam', methods=['POST'])
@login_required
def toggle_cnam(number_id):
    """Update friendly name (CNAM) for a phone number."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    business_name = data.get('business_name', vc.get('trust_hub', {}).get('business_name', ''))

    try:
        client = twilio_provisioning.get_master_client()
        client.incoming_phone_numbers(number_id).update(
            friendly_name=business_name[:15] if business_name else '',
            account_sid=sub_sid,
        )
        return jsonify({"status": "ok", "cnam_listed": bool(business_name)})
    except Exception as e:
        logger.error(f"CNAM toggle failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/nickname', methods=['POST'])
@login_required
def set_number_nickname():
    """Set a friendly nickname for a phone number (stored in voice_config)."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400

    data = request.json or {}
    phone = data.get('phone', '')
    nickname = data.get('nickname', '').strip()

    if not phone:
        return jsonify({"error": "Phone number required"}), 400

    nicknames = vc.get('number_nicknames', {})
    if nickname:
        nicknames[phone] = nickname
    else:
        nicknames.pop(phone, None)
    vc['number_nicknames'] = nicknames
    _save_voice_config(current_user.email, vc)

    return jsonify({"status": "ok", "nickname": nickname})


@voice_bp.route('/voice/numbers/set-primary', methods=['POST'])
@login_required
def set_primary_number():
    """Set a phone number as the primary caller ID."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400

    data = request.json or {}
    phone = data.get('phone', '')
    if not phone:
        return jsonify({"error": "Phone number required"}), 400

    vc['twilio_phone_number'] = phone
    _save_voice_config(current_user.email, vc)
    logger.info(f"Set primary number to {phone}")

    return jsonify({"status": "ok", "phone": phone})


@voice_bp.route('/voice/trust-hub/save', methods=['POST'])
@login_required
def save_trust_hub():
    """Save business profile and carrier registration status for Trust Hub."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400

    data = request.json or {}
    trust_hub = vc.get('trust_hub', {})
    # Update business profile
    if 'business_name' in data:
        trust_hub['business_name'] = data['business_name'].strip()
    if 'ein' in data:
        trust_hub['ein'] = data['ein'].strip()
    # Update carrier registration statuses
    for carrier in ['fcr_status', 'att_status', 'tmobile_status', 'verizon_status']:
        if carrier in data:
            trust_hub[carrier] = data[carrier]

    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)

    return jsonify({"status": "ok"})


# ──────────────────────────────────────────────────────────────
# AUTOMATED SPAM PROTECTION
# One form → registers business identity, enables CNAM on all
# numbers, and auto-protects future purchases.
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/spam-protection/register', methods=['POST'])
@login_required
def register_spam_protection():
    """
    One-click spam protection registration.
    1. Saves business profile to voice_config
    2. Creates Twilio Trust Hub Customer Profile
    3. Sets CNAM (friendly name) on all phone numbers
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned. Click Activate Voice first."}), 400

    data = request.json or {}
    business_name = (data.get('business_name') or '').strip()
    ein = (data.get('ein') or '').strip()
    street = (data.get('street') or '').strip()
    city = (data.get('city') or '').strip()
    state = (data.get('state') or '').strip()
    zip_code = (data.get('zip') or '').strip()
    contact_name = (data.get('contact_name') or '').strip()
    contact_email = (data.get('contact_email') or '').strip()
    contact_phone = (data.get('contact_phone') or '').strip()

    if not business_name:
        return jsonify({"error": "Business name is required"}), 400
    if not ein:
        return jsonify({"error": "EIN is required"}), 400

    # Step 1: Save business profile to voice_config
    from datetime import datetime
    trust_hub = vc.get('trust_hub', {})
    trust_hub.update({
        'business_name': business_name,
        'ein': ein,
        'street': street,
        'city': city,
        'state': state,
        'zip': zip_code,
        'contact_name': contact_name,
        'contact_email': contact_email,
        'contact_phone': contact_phone,
        'registered_at': datetime.utcnow().isoformat(),
        'auto_cnam': True,
    })
    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)

    # Step 2: Register with Twilio Trust Hub + set CNAM on all numbers
    results = twilio_provisioning.register_business_profile(
        sub_account_sid=sub_sid,
        business_name=business_name,
        ein=ein,
        street=street,
        city=city,
        state=state,
        zip_code=zip_code,
        contact_name=contact_name,
        contact_email=contact_email or current_user.email,
        contact_phone=contact_phone,
    )

    # Step 3: Mark auto-protection enabled
    trust_hub['protection_active'] = True
    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)

    cnam_name = business_name[:15].strip()
    cnam_step = next((s for s in results.get('steps', []) if s.get('name') == 'cnam_all_numbers'), {})

    return jsonify({
        "status": "ok" if not results.get("errors") else "partial",
        "results": results,
        "cnam_name": cnam_name,
        "numbers_protected": cnam_step.get('enabled', 0),
        "numbers_failed": cnam_step.get('total', 0) - cnam_step.get('enabled', 0),
    })


@voice_bp.route('/voice/spam-protection/status', methods=['GET'])
@login_required
def spam_protection_status():
    """Get current spam protection registration status."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    trust_hub = (vc or {}).get('trust_hub', {})
    protection_active = trust_hub.get('protection_active', False)
    business_name = trust_hub.get('business_name', '')

    # Get number details from Twilio
    status = twilio_provisioning.get_spam_protection_status(sub_sid)
    numbers_detail = [
        {
            "phone": n.get('phone', ''),
            "id": n.get('sid', ''),
            "cnam_enabled": bool(n.get('friendly_name')),
            "status": n.get('status', 'active'),
        }
        for n in status.get('numbers', [])
    ]

    return jsonify({
        "protection_active": protection_active,
        "business_name": business_name,
        "ein": trust_hub.get('ein', ''),
        "street": trust_hub.get('street', ''),
        "city": trust_hub.get('city', ''),
        "state": trust_hub.get('state', ''),
        "zip": trust_hub.get('zip', ''),
        "contact_name": trust_hub.get('contact_name', ''),
        "contact_email": trust_hub.get('contact_email', ''),
        "contact_phone": trust_hub.get('contact_phone', ''),
        "registered_at": trust_hub.get('registered_at', ''),
        "numbers_protected": status.get('numbers_protected', 0),
        "numbers_total": status.get('numbers_total', 0),
        "numbers": numbers_detail,
        "stir_shaken": "active",
        "auto_cnam": trust_hub.get('auto_cnam', False),
    })


# ──────────────────────────────────────────────────────────────
# CONTACT DETAIL: Full GHL contact info for in-call display
# ──────────────────────────────────────────────────────────────

def _get_custom_field_defs(location_id: str, headers: dict) -> dict:
    """
    Return {field_id: field_name} for every custom field in this location.
    Results are cached in _custom_field_defs for the lifetime of the process.
    GHL endpoint: GET /locations/{locationId}/customFields
    """
    if location_id in _custom_field_defs:
        return _custom_field_defs[location_id]
    try:
        resp = http_requests.get(
            f"{GHL_API_BASE}/locations/{location_id}/customFields",
            headers=headers,
            timeout=8,
        )
        if resp.status_code == 200:
            defs = resp.json().get("customFields", [])
            _custom_field_defs[location_id] = {
                d["id"]: d.get("name") or d.get("fieldKey") or d["id"]
                for d in defs
                if "id" in d
            }
        else:
            _custom_field_defs[location_id] = {}
    except Exception as e:
        logger.warning(f"Could not fetch custom field defs for {location_id}: {e}")
        _custom_field_defs[location_id] = {}
    return _custom_field_defs[location_id]


@voice_bp.route('/voice/contact/<contact_id>', methods=['GET'])
@login_required
def get_contact_detail(contact_id):
    """Fetch full GHL contact details including notes and tags."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid auth token"}), 401

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": GHL_API_VERSION,
    }

    try:
        # Fetch contact details
        resp = http_requests.get(
            f"{GHL_API_BASE}/contacts/{contact_id}",
            headers=headers,
            timeout=10
        )
        if resp.status_code != 200:
            return jsonify({"error": f"CRM returned {resp.status_code}"}), resp.status_code

        contact = resp.json().get("contact", {})

        # Fetch notes
        notes = []
        try:
            notes_resp = http_requests.get(
                f"{GHL_API_BASE}/contacts/{contact_id}/notes",
                headers=headers,
                timeout=10
            )
            if notes_resp.status_code == 200:
                notes = notes_resp.json().get("notes", [])
        except Exception:
            pass

        # Enrich custom fields with human-readable names from the location's field definitions
        field_defs = _get_custom_field_defs(location_id, headers)
        raw_custom_fields = contact.get("customFields", [])
        enriched_custom_fields = [
            {
                **cf,
                "name": field_defs.get(cf.get("id", "")) or cf.get("name") or cf.get("fieldKey") or "Field",
            }
            for cf in raw_custom_fields
            if isinstance(cf, dict)
        ]

        result = {
            "id": contact.get("id", ""),
            "firstName": contact.get("firstName", ""),
            "lastName": contact.get("lastName", ""),
            "name": f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip(),
            "phone": contact.get("phone", ""),
            "email": contact.get("email", ""),
            "tags": contact.get("tags", []),
            "address": contact.get("address1", ""),
            "city": contact.get("city", ""),
            "state": contact.get("state", ""),
            "source": contact.get("source", ""),
            "dateAdded": contact.get("dateAdded", ""),
            "customFields": enriched_custom_fields,
            "notes": [
                {
                    "id": n.get("id", ""),
                    "body": n.get("body", ""),
                    "dateAdded": n.get("dateAdded", ""),
                }
                for n in notes[:20]  # Limit to 20 most recent
            ],
        }

        return jsonify(result)

    except Exception as e:
        logger.error(f"Failed to fetch contact detail: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/contact/<contact_id>/messages', methods=['GET'])
@login_required
def get_contact_messages(contact_id):
    """Fetch SMS/conversation history for a contact from GHL."""
    limit = min(int(request.args.get('limit', 40)), 100)

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid auth token"}), 401

    try:
        messages = fetch_targeted_ghl_history(contact_id, location_id, access_token, limit)

        # Also fetch call history for this contact from our DB
        calls = []
        ch_conn = get_db_connection()
        if ch_conn:
            try:
                cur = ch_conn.cursor()
                cur.execute("""
                    SELECT call_sid, direction, status, duration, recording_url,
                           recording_sid, transcript, started_at, ended_at
                    FROM call_history
                    WHERE contact_id = %s AND location_id = %s
                    ORDER BY created_at DESC LIMIT 20
                """, (contact_id, location_id))
                for r in cur.fetchall():
                    calls.append({
                        "call_sid": r['call_sid'],
                        "direction": r['direction'],
                        "status": r['status'],
                        "duration": r['duration'] or 0,
                        "recording_url": r['recording_url'],
                        "transcript": r['transcript'],
                        "started_at": r['started_at'].isoformat() if r['started_at'] else None,
                    })
                cur.close()
            finally:
                return_db_connection(ch_conn)

        return jsonify({"messages": messages, "calls": calls})

    except Exception as e:
        logger.error(f"Failed to fetch contact messages: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/contact/<contact_id>/send-sms', methods=['POST'])
@login_required
def send_contact_sms(contact_id):
    """Send an SMS to a contact directly via GHL — bypasses A2P 10DLC via GHL's approved number."""
    data = request.json or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    if len(message) > 1600:
        return jsonify({"error": "Message too long (max 1600 characters)"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid GHL auth token. Reconnect your CRM."}), 401

    try:
        success = send_sms_via_ghl(
            contact_id=contact_id,
            message=message,
            access_token=access_token,
            location_id=location_id,
        )
        if success:
            logger.info(f"Manual SMS sent to {contact_id} by {current_user.email}")
            return jsonify({"status": "sent"})
        else:
            return jsonify({"error": "Failed to send SMS via GHL. Check logs for details."}), 500
    except Exception as e:
        logger.error(f"SMS send error for {contact_id}: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/contact/<contact_id>/ai-suggest', methods=['POST'])
@login_required
def ai_suggest_sms(contact_id):
    """
    Generate an InsuranceGrokBot reply draft using the full bot pipeline
    (generate_strategic_directive → build_system_prompt → generate_clean_reply).
    Does NOT send — just returns the draft for agent review.
    """
    # Re-use the same OpenAI client that tasks.py uses (XAI base_url)
    from tasks import client as _tasks_client
    import re as _re
    import json as _json

    if not _tasks_client:
        return jsonify({"error": "AI client not configured (XAI_API_KEY missing)"}), 503

    # Get location_id for this agent
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    # Full subscriber config (same as tasks.py uses)
    subscriber = get_subscriber_info_hybrid(location_id)
    if not subscriber:
        return jsonify({"error": "No subscriber config found"}), 400

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid GHL auth token — reconnect CRM"}), 401
    subscriber['access_token'] = access_token

    # Pull subscriber settings (mirrors tasks.py exactly)
    bot_first_name = subscriber.get('bot_first_name', 'Grok')
    timezone = subscriber.get('timezone', 'America/Chicago')
    personal_website = subscriber.get('personal_website') or ''
    contracted_carriers = subscriber.get('contracted_carriers') or []
    if isinstance(contracted_carriers, str):
        try:
            contracted_carriers = _json.loads(contracted_carriers)
        except Exception:
            contracted_carriers = []

    # Best-effort contact name from GHL
    first_name = ''
    try:
        contact_data = fetch_contact_data_from_ghl(contact_id, access_token, location_id)
        first_name = (contact_data or {}).get('firstName') or ''
    except Exception:
        pass

    bot_settings = get_bot_settings_by_location(location_id)

    # === Full InsuranceGrokBot pipeline (no send) ===
    # message="" means: it's the agent's turn to reach out / compose the next reply
    try:
        director_output = generate_strategic_directive(
            contact_id=contact_id,
            message="",
            first_name=first_name,
            age=None,
            address='',
            bot_settings=bot_settings,
        )
    except Exception as e:
        logger.error(f"AI suggest: sales_director failed for {contact_id}: {e}")
        return jsonify({"error": "Failed to generate tactical context"}), 500

    extra_context = director_output.get('underwriting_context', '')
    if director_output.get('company_context'):
        extra_context = f"{extra_context}\n[COMPANY INTEL] {director_output['company_context']}".strip()

    try:
        system_prompt = build_system_prompt(
            bot_first_name=bot_first_name,
            timezone=timezone,
            profile_str=director_output["profile_str"],
            tactical_narrative=director_output["tactical_narrative"],
            known_facts=director_output["known_facts"],
            story_narrative=director_output["story_narrative"],
            stage=director_output["stage"],
            recent_exchanges=director_output["recent_exchanges"],
            message="",
            calendar_slots="",
            context_nudge=extra_context,
            lead_vendor="",
            personal_website=personal_website,
            contracted_carriers=contracted_carriers,
            bot_settings=bot_settings,
        )
    except Exception as e:
        logger.error(f"AI suggest: build_system_prompt failed for {contact_id}: {e}")
        return jsonify({"error": "Failed to build AI prompt"}), 500

    try:
        reply = generate_clean_reply(
            client=_tasks_client,
            system_prompt=system_prompt,
            user_message="",
            bot_name=bot_first_name,
        )
    except Exception as e:
        logger.error(f"AI suggest: generate_clean_reply failed for {contact_id}: {e}")
        return jsonify({"error": "AI generation failed"}), 500

    if not reply:
        return jsonify({"error": "AI returned empty reply"}), 500

    # Same post-processing as tasks.py (strip markdown, normalize punctuation)
    reply = _re.sub(r'\*\*([^*]+)\*\*', r'\1', reply)
    reply = _re.sub(r'\*([^*]+)\*', r'\1', reply)
    reply = _re.sub(r'__([^_]+)__', r'\1', reply)
    reply = _re.sub(r'_([^_]+)_', r'\1', reply)
    reply = reply.replace("—", ",").replace("–", ",").replace("…", "...").strip()

    logger.info(f"InsuranceGrokBot draft generated for {contact_id} by {current_user.email} | '{reply[:60]}'")
    return jsonify({"suggestion": reply})


@voice_bp.route('/voice/pipelines', methods=['GET'])
@login_required
def get_pipelines():
    """Fetch pipelines and stages for contact filtering (uses contacts.readonly scope)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid auth token"}), 401

    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Version": GHL_API_VERSION,
        }
        resp = http_requests.get(
            f"{GHL_API_BASE}/opportunities/pipelines",
            headers=headers,
            params={"locationId": location_id},
            timeout=10
        )
        if resp.status_code in (401, 403, 422):
            # opportunities.readonly scope not yet granted — GHL returns 401/403
            # for missing scopes, but PITs sometimes return 422 instead.
            logger.warning(f"Pipelines fetch returned {resp.status_code} — "
                           f"opportunities.readonly scope likely not granted. "
                           f"Response: {resp.text[:300]}")
            return jsonify({"pipelines": [], "scope_missing": True})
        if resp.status_code != 200:
            logger.warning(f"Pipelines fetch returned {resp.status_code}: {resp.text[:300]}")
            return jsonify({"pipelines": []})

        pipelines = resp.json().get("pipelines", [])
        result = []
        for p in pipelines:
            result.append({
                "id": p.get("id", ""),
                "name": p.get("name", ""),
                "stages": [
                    {"id": s.get("id", ""), "name": s.get("name", "")}
                    for s in p.get("stages", [])
                ]
            })
        return jsonify({"pipelines": result})

    except Exception as e:
        logger.error(f"Failed to fetch pipelines: {e}")
        return jsonify({"pipelines": []})


# ── Dialer Statistics ─────────────────────────────────────────────────────────

@voice_bp.route('/voice/stats')
@login_required
def get_dialer_stats():
    """Return aggregated call statistics for the current user's dialer."""
    from datetime import datetime, timedelta
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        location_id = row[0]

        # Ensure disposition column exists
        try:
            cur.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS disposition TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()

        period = request.args.get('period', 'month')
        now = datetime.utcnow()
        if period == 'today':
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'week':
            start_date = now - timedelta(days=7)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        else:
            start_date = datetime(2000, 1, 1)

        # Core KPIs
        cur.execute("""
            SELECT
                COUNT(*)                                                      AS total_calls,
                COUNT(*) FILTER (WHERE direction = 'outbound')                AS outbound_calls,
                COUNT(*) FILTER (WHERE direction = 'inbound')                 AS inbound_calls,
                COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected_calls,
                COALESCE(AVG(duration) FILTER (WHERE duration > 0), 0)        AS avg_duration,
                COALESCE(SUM(duration), 0)                                    AS total_duration,
                COUNT(*) FILTER (WHERE duration >   6)                        AS over_6s,
                COUNT(*) FILTER (WHERE duration >=  60)                       AS over_1min,
                COUNT(*) FILTER (WHERE duration >= 120)                       AS over_2min,
                COUNT(*) FILTER (WHERE duration >= 300)                       AS over_5min,
                COUNT(*) FILTER (WHERE duration >= 600)                       AS over_10min,
                COUNT(DISTINCT contact_id)                                    AS unique_contacts
            FROM call_history
            WHERE location_id = %s AND created_at >= %s
        """, (location_id, start_date))
        r = cur.fetchone()
        total           = r[0] or 0
        outbound        = r[1] or 0
        inbound         = r[2] or 0
        connected       = r[3] or 0
        avg_dur         = float(r[4] or 0)
        total_dur       = int(r[5] or 0)
        over_6s         = r[6] or 0
        over_1min       = r[7] or 0
        over_2min       = r[8] or 0
        over_5min       = r[9] or 0
        over_10min      = r[10] or 0
        unique_contacts = r[11] or 0
        connect_rate    = round(connected / total * 100, 1) if total else 0.0

        # Days in period (for "per day" averages)
        if period == 'today':
            days = 1
        elif period == 'week':
            days = 7
        elif period == 'month':
            days = 30
        else:
            cur.execute("SELECT MIN(created_at) FROM call_history WHERE location_id = %s", (location_id,))
            first = cur.fetchone()[0]
            days = max(1, (now - first).days) if first else 1

        # Prior period comparison (skip for 'all')
        prior = None
        if period != 'all':
            period_len  = now - start_date
            prior_end   = start_date
            prior_start = start_date - period_len
            cur.execute("""
                SELECT
                    COUNT(*)                                                      AS total_calls,
                    COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected_calls,
                    COALESCE(SUM(duration), 0)                                    AS total_duration,
                    COALESCE(AVG(duration) FILTER (WHERE duration > 0), 0)        AS avg_duration
                FROM call_history
                WHERE location_id = %s AND created_at >= %s AND created_at < %s
            """, (location_id, prior_start, prior_end))
            pr          = cur.fetchone()
            p_total     = pr[0] or 0
            p_connected = pr[1] or 0
            p_dur       = int(pr[2] or 0)
            p_avg_dur   = float(pr[3] or 0)
            p_rate      = round(p_connected / p_total * 100, 1) if p_total else 0.0

            def _pct_delta(curr, prev):
                if prev == 0:
                    return None
                return round((curr - prev) / prev * 100, 1)

            prior = {
                "total_calls":     p_total,
                "connected_calls": p_connected,
                "connect_rate":    p_rate,
                "total_duration":  p_dur,
                "avg_duration":    p_avg_dur,
                # % change for counts; absolute pp difference for rate
                "delta_calls":     _pct_delta(total, p_total),
                "delta_connected": _pct_delta(connected, p_connected),
                "delta_rate":      round(connect_rate - p_rate, 1),
                "delta_duration":  _pct_delta(total_dur, p_dur),
            }

        # Disposition breakdown
        dispositions = {}
        try:
            cur.execute("""
                SELECT
                    COALESCE(NULLIF(TRIM(disposition), ''), 'none') AS disp,
                    COUNT(*) AS cnt
                FROM call_history
                WHERE location_id = %s AND created_at >= %s
                  AND disposition IS NOT NULL AND TRIM(disposition) != ''
                GROUP BY disp
                ORDER BY cnt DESC
            """, (location_id, start_date))
            dispositions = {row[0]: row[1] for row in cur.fetchall()}
        except Exception:
            conn.rollback()

        # Daily call volume with talk time
        cur.execute("""
            SELECT DATE(created_at AT TIME ZONE 'UTC') AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected,
                   COALESCE(SUM(duration), 0) AS total_secs
            FROM call_history
            WHERE location_id = %s AND created_at >= %s
            GROUP BY day ORDER BY day
        """, (location_id, start_date))
        daily = [
            {"day": str(row[0]), "calls": row[1], "connected": row[2], "total_secs": row[3]}
            for row in cur.fetchall()
        ]

        # Hourly distribution
        cur.execute("""
            SELECT EXTRACT(HOUR FROM created_at)::int AS hr, COUNT(*) AS calls
            FROM call_history
            WHERE location_id = %s AND created_at >= %s
            GROUP BY hr ORDER BY hr
        """, (location_id, start_date))
        hourly_map = {row[0]: row[1] for row in cur.fetchall()}
        hourly = [{"hour": h, "calls": hourly_map.get(h, 0)} for h in range(24)]

        # Top 5 most-called contacts
        cur.execute("""
            SELECT contact_id, contact_name, COUNT(*) AS cnt,
                   MAX(created_at) AS last_called
            FROM call_history
            WHERE location_id = %s AND created_at >= %s
            GROUP BY contact_id, contact_name
            ORDER BY cnt DESC LIMIT 5
        """, (location_id, start_date))
        top_contacts = [
            {"id": row[0], "name": row[1] or "Unknown", "count": row[2], "last_called": str(row[3])}
            for row in cur.fetchall()
        ]

        cur.close()
        return jsonify({
            "period":          period,
            "total_calls":     total,
            "outbound_calls":  outbound,
            "inbound_calls":   inbound,
            "connected_calls": connected,
            "connect_rate":    connect_rate,
            "avg_duration":    round(avg_dur, 1),
            "total_duration":  total_dur,
            "over_6s":         over_6s,
            "over_1min":       over_1min,
            "over_2min":       over_2min,
            "over_5min":       over_5min,
            "over_10min":      over_10min,
            "unique_contacts": unique_contacts,
            "calls_per_day":   round(total / days, 1),
            "daily":           daily,
            "hourly":          hourly,
            "top_contacts":    top_contacts,
            "prior":           prior,
            "dispositions":    dispositions,
        })
    except Exception as e:
        logger.error(f"get_dialer_stats failed: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)


@voice_bp.route('/voice/contact-call-counts')
@login_required
def get_contact_call_counts():
    """Batch local call counts for a list of contact IDs."""
    ids_param = request.args.get('ids', '')
    if not ids_param:
        return jsonify({})
    contact_ids = [x.strip() for x in ids_param.split(',') if x.strip()][:300]
    conn = get_db_connection()
    if not conn:
        return jsonify({})
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({})
        location_id = row[0]
        cur.execute("""
            SELECT contact_id, COUNT(*) AS cnt
            FROM call_history
            WHERE location_id = %s AND contact_id = ANY(%s)
            GROUP BY contact_id
        """, (location_id, contact_ids))
        result = {r[0]: r[1] for r in cur.fetchall()}
        cur.close()
        return jsonify(result)
    except Exception as e:
        logger.error(f"get_contact_call_counts failed: {e}")
        return jsonify({})
    finally:
        return_db_connection(conn)


@voice_bp.route('/voice/contact-call-counts/merged')
@login_required
def get_contact_call_counts_merged():
    """Batch merged (local DB + GHL) call counts for up to 50 contact IDs."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ids_param = request.args.get('ids', '')
    if not ids_param:
        return jsonify({})
    contact_ids = [x.strip() for x in ids_param.split(',') if x.strip()][:50]

    conn = get_db_connection()
    if not conn:
        return jsonify({cid: 0 for cid in contact_ids})
    location_id = None
    local_counts = {}
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({})
        location_id = row[0]
        cur.execute("""
            SELECT contact_id, COUNT(*) AS cnt
            FROM call_history
            WHERE location_id = %s AND contact_id = ANY(%s)
            GROUP BY contact_id
        """, (location_id, contact_ids))
        local_counts = {r[0]: r[1] for r in cur.fetchall()}
        cur.close()
    except Exception as e:
        logger.error(f"merged call counts local query failed: {e}")
    finally:
        return_db_connection(conn)

    # GHL counts in parallel threads
    ghl_counts = {cid: 0 for cid in contact_ids}
    if location_id:
        try:
            access_token = get_valid_token(location_id)
            if access_token and access_token != 'DEMO':
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Version": GHL_API_VERSION,
                    "Content-Type": "application/json",
                }

                def _fetch_ghl(cid):
                    try:
                        sr = http_requests.get(
                            f"{GHL_API_BASE}/conversations/search",
                            headers=headers,
                            params={"locationId": location_id, "contactId": cid},
                            timeout=6,
                        )
                        if sr.status_code != 200:
                            return cid, 0
                        convos = sr.json().get("conversations", [])
                        if not convos:
                            return cid, 0
                        convo_id = convos[0]["id"]
                        mr = http_requests.get(
                            f"{GHL_API_BASE}/conversations/{convo_id}/messages",
                            headers=headers,
                            params={"limit": 100},
                            timeout=6,
                        )
                        if mr.status_code != 200:
                            return cid, 0
                        payload = mr.json().get("messages", [])
                        raw_msgs = payload.get("messages", []) if isinstance(payload, dict) else payload
                        count = sum(
                            1 for m in raw_msgs
                            if isinstance(m, dict)
                            and (m.get("type") or m.get("messageType", "")) in (3, 4, "TYPE_CALL", 3.0, 4.0)
                        )
                        return cid, count
                    except Exception:
                        return cid, 0

                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures = {pool.submit(_fetch_ghl, cid): cid for cid in contact_ids}
                    for future in as_completed(futures, timeout=15):
                        try:
                            cid, count = future.result()
                            ghl_counts[cid] = count
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"GHL batch call count failed: {e}")

    result = {cid: (local_counts.get(cid, 0) or 0) + (ghl_counts.get(cid, 0) or 0)
              for cid in contact_ids}
    return jsonify(result)


@voice_bp.route('/voice/contact/<contact_id>/ghl-call-count')
@login_required
def get_contact_ghl_call_count(contact_id):
    """Return merged call count: local dialer DB + GHL conversation calls."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"local": 0, "ghl": 0, "total": 0})
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({"local": 0, "ghl": 0, "total": 0})
        location_id = row[0]

        cur.execute(
            "SELECT COUNT(*) FROM call_history WHERE location_id = %s AND contact_id = %s",
            (location_id, contact_id)
        )
        local_count = cur.fetchone()[0] or 0
        cur.close()
    except Exception as e:
        logger.error(f"local count failed for {contact_id}: {e}")
        local_count = 0
    finally:
        return_db_connection(conn)

    # Fetch GHL conversation call messages
    ghl_count = 0
    try:
        access_token = get_valid_token(location_id)
        if access_token and access_token != 'DEMO':
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Version": GHL_API_VERSION,
                "Content-Type": "application/json"
            }
            search_resp = http_requests.get(
                f"{GHL_API_BASE}/conversations/search",
                headers=headers,
                params={"locationId": location_id, "contactId": contact_id},
                timeout=10
            )
            if search_resp.status_code == 200:
                convos = search_resp.json().get("conversations", [])
                if convos:
                    convo_id = convos[0]["id"]
                    msg_resp = http_requests.get(
                        f"{GHL_API_BASE}/conversations/{convo_id}/messages",
                        headers=headers,
                        params={"limit": 100},
                        timeout=10
                    )
                    if msg_resp.status_code == 200:
                        payload = msg_resp.json().get("messages", [])
                        raw_msgs = payload.get("messages", []) if isinstance(payload, dict) else payload
                        for m in raw_msgs:
                            if not isinstance(m, dict):
                                continue
                            mtype = m.get("type") or m.get("messageType", "")
                            # GHL call message types: 3 (outbound call), 4 (inbound call), TYPE_CALL
                            if mtype in (3, 4, "TYPE_CALL") or mtype in (3.0, 4.0):
                                ghl_count += 1
    except Exception as e:
        logger.warning(f"GHL call count fetch failed for {contact_id}: {e}")

    return jsonify({
        "local": local_count,
        "ghl": ghl_count,
        "total": local_count + ghl_count,
    })
