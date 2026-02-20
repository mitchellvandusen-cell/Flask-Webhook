# voice_bridge.py - Telnyx <-> XAI Grok Voice Agent Bridge
# Real-time bidirectional audio streaming via WebSocket
# Architecture: Lead <-> Telnyx Media Streaming <-> This Bridge <-> XAI Realtime API
#
# Audio format: L16 (Linear PCM 16-bit) 16 kHz PASSTHROUGH — zero conversion.
# Telnyx streaming_start configured with stream_bidirectional_codec=L16 at 16 kHz.
# xAI configured with audio/pcm at 16 kHz for both input and output.
#   Telnyx → Bridge: base64 L16 16kHz  →  forward as-is  →  xAI input_audio_buffer.append
#   xAI → Bridge:   base64 L16 16kHz delta  →  forward as-is  →  Telnyx media event
# No transcoding, no resampling — just routing base64 strings.
# XAI endpoint: wss://api.x.ai/v1/realtime

import json
import os
import logging
import threading
import time
import asyncio
import struct
import base64
import audioop   # kept for recording-playback helpers (_mulaw_to_wav)
import numpy as np
import websockets
import requests as http_requests
from flask import Blueprint, request, Response, jsonify, render_template
from flask_login import login_required, current_user

from db import get_db_connection, return_db_connection, log_webhook_event
from ghl_api import get_valid_token, fetch_targeted_ghl_history

# In-memory call status tracking for the dialer queue
# { call_sid: { "status": "...", "duration": 0, "contact_id": "...", "phone": "...", "name": "..." } }
_active_calls = {}

# Transfer / takeover signaling: set by HTTP endpoints, read by WebSocket bridge
# { call_sid: {"type": "transfer"|"takeover", "target": "+1...", "reason": "..."} }
_transfer_requests = {}

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

# Audio: L16 (PCM 16-bit) 16 kHz passthrough — both Telnyx and xAI use the same format.
# Telnyx streaming_start sets stream_bidirectional_codec=L16 at 16 kHz.
# xAI session configured with audio/pcm at 16 kHz input & output.
TELNYX_SAMPLE_RATE = 8000   # kept for _mulaw_to_wav / voice-preview WAV wrapping

# Telnyx REST API base URL
TELNYX_API_BASE = "https://api.telnyx.com/v2"


# ──────────────────────────────────────────────────────────────
# TELNYX CALL CONTROL HELPERS
# ──────────────────────────────────────────────────────────────

def _call_control_command(call_control_id: str, api_key: str, action: str, params: dict) -> bool:
    """
    Send a Call Control API command (answer, streaming_start, hangup, etc.) to a
    specific call leg.  Returns True on HTTP 2xx, False otherwise.
    """
    try:
        resp = http_requests.post(
            f"{TELNYX_API_BASE}/calls/{call_control_id}/actions/{action}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json=params,
            timeout=8,
        )
        if not resp.ok:
            logger.warning(f"Call Control '{action}' failed: {resp.status_code} {resp.text[:200]}")
        return resp.ok
    except Exception as e:
        logger.error(f"Call Control '{action}' error: {e}")
        return False


def _encode_client_state(data: dict) -> str:
    """Base64-encode a dict into a Telnyx client_state string."""
    import base64
    return base64.b64encode(json.dumps(data).encode()).decode()


def _decode_client_state(s: str) -> dict:
    """Decode a Telnyx client_state string back to a dict."""
    import base64
    try:
        return json.loads(base64.b64decode(s.encode()).decode())
    except Exception:
        return {}


def _build_texml_stream(stream_url: str, params: dict) -> str:
    """
    Build a Telnyx TeXML Response that opens a bidirectional L16 16kHz media stream.
    """
    param_xml = ''.join(
        f'<Parameter name="{k}" value="{v}"/>' for k, v in params.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
          '<Connect>'
            f'<Stream url="{stream_url}"'
            f' bidirectionalMode="rtp"'
            f' audio_codec="L16"'
            f' sample_rate="16000">'
              f'{param_xml}'
            '</Stream>'
          '</Connect>'
        '</Response>'
    )

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
    """Look up subscriber whose voice_config.telnyx_phone_number matches the given number."""
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
                  REPLACE(REPLACE(voice_config->>'telnyx_phone_number', '+', ''), '1', '') LIKE %s
                  OR voice_config->>'telnyx_phone_number' = %s
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

def build_voice_system_prompt(subscriber, contact_name="there", contact_id=None, context=None):
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
    call_context = "FRESH OUTBOUND CALL"
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
    if previous_call_count > 0 or has_sms_history:
        call_context = f"FOLLOW-UP CALL — you've contacted this person {previous_call_count} time(s) by phone before"
        if has_sms_history:
            call_context += f" and there are {len(recent_exchanges)} SMS exchanges in history"
    # Note: inbound detection happens at the greeting level, not here

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

    prompt = f"""You are {voice_bot_name}, a life insurance advisor on a live phone call. You speak like a real, experienced human advisor — natural, warm, direct, and conversational.

YOUR VOICE PERSONALITY: {voice_personality}

=== MAXIMUM HUMAN SOUNDING — HIGHEST PRIORITY (REPEAT & OBEY) ===
Sound like a real person on a slightly scratchy phone line — warm, mid-to-low pitched, slightly imperfect, never synthetic, never bright or thin. This overrides everything else when there's a conflict.

Pitch center: Comfortably mid-to-low adult range (120–180 Hz chest register) — speak from the chest, never nasal or head-voice dominant. Telephony bandwidth cuts the highs, so lean slightly deeper and warmer than feels natural to compensate. The listener hears you as fuller and more grounded when you do.

Never rise in pitch at the end of statements unless genuinely questioning. Never sound "up-talky" or artificially bright. Statements land flat or falling — confidence, not uncertainty.

Pitch: Mid-to-low natural adult range. Avoid high, thin, bright, or rising at the end of statements unless genuinely asking a question.

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
# ROUTE: Twilio calls this when an inbound call arrives
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/inbound', methods=['POST'])
def voice_inbound():
    """
    Handle all Call Control webhook events for inbound calls.

    Telnyx Voice API Application sends JSON events; we respond 200 OK and
    issue Call Control commands as needed:
      call.initiated  → answer the call (encodes metadata into client_state)
      call.answered   → start bidirectional L16 media stream to our WebSocket
      all others      → acknowledge with 200 OK
    """
    payload    = request.get_json(silent=True) or {}
    data       = payload.get('data', {})
    event_type = data.get('event_type', '')
    call_pl    = data.get('payload', {})
    call_ctrl  = call_pl.get('call_control_id', '')

    logger.info(f"📞 Voice inbound event: {event_type} ctrl={call_ctrl[:16] if call_ctrl else 'none'}")

    if event_type == 'call.initiated':
        direction = call_pl.get('direction', 'incoming')
        if direction != 'incoming':
            # Outbound leg initiated — update _active_calls to 'ringing' for faster polling
            _out_sid = call_pl.get('call_control_id', call_pl.get('call_leg_id', ''))
            if _out_sid and _out_sid in _active_calls:
                _active_calls[_out_sid]['status'] = 'ringing'
            return jsonify({'result': 'ok'}), 200

        caller = call_pl.get('from', 'Unknown')
        called = call_pl.get('to',   'Unknown')

        subscriber = _get_subscriber_by_phone(called)
        if not subscriber:
            logger.warning(f"No subscriber for {called}; letting call timeout")
            return jsonify({'result': 'ok'}), 200

        vc      = subscriber.get('voice_config') or {}
        api_key = vc.get('telnyx_api_key', '')
        if not api_key:
            logger.warning("Inbound call but no Telnyx API key configured")
            return jsonify({'result': 'ok'}), 200

        # Encode metadata into client_state — echoed back in call.answered and
        # the WebSocket start event so the bridge knows location/direction.
        client_state = _encode_client_state({
            'location_id':  subscriber.get('location_id', ''),
            'caller':       caller,
            'called':       called,
            'direction':    'inbound',
            'contact_id':   '',
            'contact_name': 'there',
        })

        _call_control_command(call_ctrl, api_key, 'answer', {
            'client_state': client_state,
        })
        logger.info(f"📞 Answered inbound call: {caller} -> {called}")

    elif event_type == 'call.answered':
        # Immediately update _active_calls so dialer polling picks up 'in-progress' status
        _answered_sid = call_pl.get('call_control_id', call_pl.get('call_leg_id', ''))
        if _answered_sid and _answered_sid in _active_calls:
            _active_calls[_answered_sid]['status'] = 'in-progress'

        # Retrieve metadata we encoded at answer time
        client_state_raw = call_pl.get('client_state', '')
        meta             = _decode_client_state(client_state_raw)
        location_id      = meta.get('location_id', '')
        dial_mode        = meta.get('dial_mode', 'ai')

        if not location_id:
            logger.warning("call.answered: no location_id in client_state")
            return jsonify({'result': 'ok'}), 200

        subscriber = _get_subscriber_by_location(location_id)
        if not subscriber:
            return jsonify({'result': 'ok'}), 200

        vc      = subscriber.get('voice_config') or {}
        api_key = vc.get('telnyx_api_key', '')
        if not api_key:
            return jsonify({'result': 'ok'}), 200

        host = request.host

        # Auto-start recording if enabled
        auto_record = meta.get('auto_record', vc.get('auto_record', True))
        if auto_record:
            _call_control_command(call_ctrl, api_key, 'record_start', {
                'format': 'mp3',
                'channels': 'dual',
                'play_beep': False,
                'record_type': 'audio',
                'custom_recording_url': f'https://{host}/voice/recording-status',
            })

        # Auto-start transcription if enabled
        auto_transcribe = meta.get('auto_transcribe', vc.get('auto_transcribe', False))
        if auto_transcribe:
            _call_control_command(call_ctrl, api_key, 'transcription_start', {
                'language': 'en',
                'transcription_engine': 'A',
                'webhook_url': f'https://{host}/voice/transcription',
            })

        # For human VoIP calls, skip AI streaming — the browser handles the audio directly
        if dial_mode == 'human':
            logger.info(f"📞 Human VoIP call answered for {location_id} — recording started, no AI stream")
            return jsonify({'result': 'ok'}), 200

        # For AMD-enabled outbound AI calls, Telnyx plays probe audio immediately after
        # call.answered to detect answering machines.  Starting bidirectional streaming
        # at the same time conflicts — Telnyx kills our stream to run its probes.
        # Defer streaming to call.machine.detection.ended once AMD finishes.
        if meta.get('use_amd', False):
            logger.info(f"📞 AMD call answered for {location_id} — deferring stream until AMD resolves")
            return jsonify({'result': 'ok'}), 200

        # For AI calls, start bidirectional media streaming to the AI bridge.
        # Run in a background thread so the webhook returns immediately (no blocked
        # gunicorn worker).  Telnyx occasionally returns 422/90046 "Failed to connect
        # to destination" when the call state hasn't fully settled; a single retry
        # after a short backoff resolves it in practice.
        stream_params = {
            'stream_url':                         f'wss://{host}/voice/stream',
            'stream_track':                       'inbound_track',
            'stream_bidirectional_mode':          'rtp',
            'stream_bidirectional_codec':         'L16',
            'stream_bidirectional_sampling_rate': 16000,
            'client_state':                       client_state_raw,
        }

        def _start_streaming(ctrl, key, params, loc_id):
            time.sleep(0.3)   # brief settle — reduced from 0.8s for faster AI response
            ok = _call_control_command(ctrl, key, 'streaming_start', params)
            if ok:
                logger.info(f"📞 AI streaming started for {loc_id}")
                return
            # Telnyx 422/90046 — call state transition not complete; retry once
            logger.warning(f"📞 streaming_start failed for {loc_id}; retrying in 0.8s")
            time.sleep(0.8)
            ok = _call_control_command(ctrl, key, 'streaming_start', params)
            if ok:
                logger.info(f"📞 AI streaming started for {loc_id} (retry ok)")
            else:
                logger.error(f"📞 streaming_start failed after retry for {loc_id} — call has no audio bridge")

        threading.Thread(
            target=_start_streaming,
            args=(call_ctrl, api_key, stream_params, location_id),
            daemon=True,
        ).start()

    elif event_type in ('call.machine.detection.ended', 'call.machine.premium.detection.ended'):
        # AMD result: human / machine / not_sure / human_residence / human_business / silence / fax_detected
        client_state_raw = call_pl.get('client_state', '')
        meta             = _decode_client_state(client_state_raw)
        amd_result       = call_pl.get('result', '')
        location_id      = meta.get('location_id', '')
        dial_attempt     = int(meta.get('dial_attempt', 1))
        max_attempts     = int(meta.get('max_dial_attempts', 2))

        logger.info(f"📞 AMD result={amd_result} attempt={dial_attempt}/{max_attempts} loc={location_id}")

        machine_results = {'machine_words_present', 'machine_stop', 'machine_silence_after_words', 'machine', 'fax_detected'}
        if amd_result in machine_results and dial_attempt < max_attempts and location_id:
            subscriber = _get_subscriber_by_location(location_id)
            if subscriber:
                vc      = subscriber.get('voice_config') or {}
                api_key = vc.get('telnyx_api_key', '')
                if api_key and call_ctrl:
                    # Hang up the machine-answered call
                    _call_control_command(call_ctrl, api_key, 'hangup', {})
                    # Schedule redial in a background thread after a short pause
                    def _redial():
                        time.sleep(3)
                        try:
                            telnyx_number = vc.get('telnyx_phone_number', '')
                            telnyx_conn   = vc.get('telnyx_connection_id', '')
                            called_phone  = meta.get('called', '')
                            host_url      = request.host
                            new_state     = _encode_client_state({
                                **meta,
                                'dial_attempt': dial_attempt + 1,
                            })
                            call_payload = {
                                "connection_id":      telnyx_conn,
                                "to":                 called_phone,
                                "from":               telnyx_number,
                                "webhook_url":        f"https://{host_url}/voice/inbound",
                                "webhook_url_method": "POST",
                                "client_state":       new_state,
                                "answering_machine_detection": "premium",
                                "answering_machine_detection_config": {
                                    "total_analysis_time_millis": 3500,
                                },
                            }
                            r = http_requests.post(
                                f"{TELNYX_API_BASE}/calls",
                                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                                json=call_payload,
                                timeout=10,
                            )
                            logger.info(f"📞 Redial attempt {dial_attempt+1}: {r.status_code}")
                        except Exception as ex:
                            logger.error(f"Redial failed: {ex}")
                    threading.Thread(target=_redial, daemon=True).start()

        else:
            # Human or not_sure — AMD finished without detecting a machine.
            # It is now safe to start bidirectional streaming; AMD probe audio is done.
            if location_id and call_ctrl:
                subscriber = _get_subscriber_by_location(location_id)
                if subscriber:
                    vc      = subscriber.get('voice_config') or {}
                    api_key = vc.get('telnyx_api_key', '')
                    if api_key:
                        host              = request.host
                        stream_params_amd = {
                            'stream_url':                         f'wss://{host}/voice/stream',
                            'stream_track':                       'inbound_track',
                            'stream_bidirectional_mode':          'rtp',
                            'stream_bidirectional_codec':         'L16',
                            'stream_bidirectional_sampling_rate': 16000,
                            'client_state':                       client_state_raw,
                        }
                        _amd_result = amd_result  # capture for closure
                        def _start_amd_stream(ctrl, key, params, loc_id, result):
                            ok = _call_control_command(ctrl, key, 'streaming_start', params)
                            if ok:
                                logger.info(f"📞 AI streaming started after AMD ({result}) for {loc_id}")
                                return
                            time.sleep(1.5)
                            ok = _call_control_command(ctrl, key, 'streaming_start', params)
                            if ok:
                                logger.info(f"📞 AI streaming started after AMD ({result}) for {loc_id} (retry ok)")
                            else:
                                logger.error(f"📞 streaming_start failed after AMD for {loc_id}")
                        threading.Thread(
                            target=_start_amd_stream,
                            args=(call_ctrl, api_key, stream_params_amd, location_id, _amd_result),
                            daemon=True,
                        ).start()

    elif event_type == 'call.recording.saved':
        # Recording completed — persist URL to call_history
        telnyx_pl      = call_pl  # already the payload dict
        call_sid_rec   = telnyx_pl.get('call_control_id', telnyx_pl.get('call_leg_id', ''))
        recording_id   = telnyx_pl.get('recording_id', '')
        pub_urls       = telnyx_pl.get('public_recording_urls', {})
        mp3_url        = pub_urls.get('mp3', '') or telnyx_pl.get('recording_urls', {}).get('mp3', '')
        duration_ms    = telnyx_pl.get('duration_millis', 0)
        duration_secs  = int(duration_ms / 1000)

        if call_sid_rec and mp3_url:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE call_history
                        SET recording_url = %s, recording_sid = %s,
                            duration = COALESCE(NULLIF(duration, 0), %s)
                        WHERE call_sid = %s
                    """, (mp3_url, recording_id, duration_secs, call_sid_rec))
                    conn.commit()
                    cur.close()
                    logger.info(f"🎙️ Recording saved for call {call_sid_rec}: {mp3_url}")
                except Exception as e:
                    logger.error(f"Failed to persist recording URL: {e}")
                    conn.rollback()
                finally:
                    return_db_connection(conn)

    elif event_type == 'call.hangup':
        # Call ended — immediately update _active_calls so dialer polling picks it up
        hangup_cause = call_pl.get('hangup_cause', 'normal_clearing')
        sip_code     = call_pl.get('sip_hangup_cause', '')
        duration_s   = int(call_pl.get('duration_seconds', 0) or 0)
        call_sid_h   = call_pl.get('call_control_id', call_pl.get('call_leg_id', ''))

        # Map hangup causes to user-friendly statuses
        if hangup_cause in ('normal_clearing', 'normal_unspecified'):
            final_status = 'completed'
        elif hangup_cause in ('user_busy', 'call_rejected'):
            final_status = 'busy'
        elif hangup_cause in ('no_answer', 'originator_cancel', 'timeout'):
            final_status = 'no-answer'
        else:
            final_status = 'completed'

        if call_sid_h and call_sid_h in _active_calls:
            _active_calls[call_sid_h]['status'] = final_status
            _active_calls[call_sid_h]['duration'] = duration_s

        # Also persist to DB
        if call_sid_h:
            try:
                update_call_history_status(call_sid_h, final_status, duration_s)
            except Exception:
                pass

        logger.info(f"📞 Call hangup: {call_sid_h} cause={hangup_cause} sip={sip_code} dur={duration_s}s status={final_status}")

    # All other events (streaming.started, streaming.stopped, etc.)
    # just need a 200 OK acknowledgement.
    return jsonify({'result': 'ok'}), 200


@voice_bp.route('/voice/outbound-answer', methods=['POST'])
def voice_outbound_answer():
    """
    Backward-compatibility alias — now delegates to voice_inbound.
    All Call Control events (inbound and outbound) are handled at /voice/inbound.
    """
    return voice_inbound()


# ──────────────────────────────────────────────────────────────
# ROUTE: Trigger an outbound call
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/outbound-call', methods=['POST'])
def trigger_outbound_call():
    """
    API endpoint to initiate an outbound AI voice call.
    Called by CRM automations (webhook) or the dashboard.

    Expected JSON payload:
    {
        "location_id": "abc123",
        "phone": "+15551234567",
        "first_name": "John",
        "contact_id": "contact_abc"
    }
    """
    data = request.json or {}
    location_id = data.get('location_id', '')
    lead_phone = data.get('phone', '')
    lead_name = data.get('first_name', 'there')
    contact_id = data.get('contact_id', '')

    if not location_id or not lead_phone:
        return jsonify({"error": "location_id and phone are required"}), 400

    # Look up subscriber
    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return jsonify({"error": "Subscriber not found"}), 404

    voice_config = subscriber.get("voice_config") or {}
    if not voice_config.get("enabled"):
        return jsonify({"error": "Voice is not enabled for this account"}), 400

    # Telnyx credentials — stored in voice_config as:
    #   telnyx_api_key        — Telnyx API key (from telnyx.com Mission Control)
    #   telnyx_connection_id  — Voice API Application Connection ID (from Telnyx dashboard)
    #   telnyx_phone_number   — E.164 Telnyx number (+1...)
    telnyx_api_key       = voice_config.get("telnyx_api_key", "")
    telnyx_connection_id = voice_config.get("telnyx_connection_id", "")
    telnyx_number        = voice_config.get("telnyx_phone_number", "")

    if not all([telnyx_api_key, telnyx_connection_id, telnyx_number]):
        return jsonify({"error": "Telnyx credentials not fully configured (need telnyx_api_key, telnyx_connection_id, telnyx_phone_number)"}), 400

    try:
        host         = request.host
        # All events (inbound + outbound) go to the single app webhook URL.
        webhook_url  = f"https://{host}/voice/inbound"
        # Metadata travels as client_state (base64 JSON); Telnyx echoes it in
        # every subsequent webhook event and in the WebSocket start message.
        client_state = _encode_client_state({
            'location_id':  location_id,
            'caller':       telnyx_number,
            'called':       lead_phone,
            'direction':    'outbound',
            'contact_id':   contact_id,
            'contact_name': lead_name,
        })

        # Telnyx Call Control API — create outbound call
        # Docs: https://developers.telnyx.com/api/call-control/create-call
        resp = http_requests.post(
            f"{TELNYX_API_BASE}/calls",
            headers={
                "Authorization": f"Bearer {telnyx_api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "connection_id":      telnyx_connection_id,
                "to":                 lead_phone,
                "from":               telnyx_number,
                "webhook_url":        webhook_url,
                "webhook_url_method": "POST",
                "client_state":       client_state,
            },
            timeout=10,
        )
        if not resp.ok:
            telnyx_errors = resp.json().get('errors', [{}])
            telnyx_msg = telnyx_errors[0].get('detail', resp.text) if telnyx_errors else resp.text
            logger.error(f"Telnyx outbound call error {resp.status_code}: {resp.text}")
            return jsonify({"error": f"Telnyx {resp.status_code}: {telnyx_msg}"}), 400
        call_data    = resp.json().get("data", {})
        call_leg_id  = call_data.get("call_leg_id", "")
        call_ctrl_id = call_data.get("call_control_id", call_leg_id)

        logger.info(f"📞 Telnyx outbound call initiated: {telnyx_number} -> {lead_phone} (ctrl_id={call_ctrl_id})")

        # Persist to call_history DB (use call_control_id as the call identifier)
        save_call_to_history(
            location_id=location_id,
            call_sid=call_ctrl_id,
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
                details={"call_control_id": call_ctrl_id, "to": lead_phone, "from": telnyx_number}
            )
        except Exception:
            pass

        return jsonify({"status": "calling", "call_sid": call_ctrl_id})

    except Exception as e:
        logger.error(f"Failed to initiate Telnyx outbound call: {e}")
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# ROUTE: Call status webhook
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/status', methods=['POST'])
def voice_status():
    """
    Telnyx posts call status events here as JSON.
    Telnyx event_type values: call.initiated, call.answered, call.hangup
    Also handles legacy Twilio form-POST for any in-flight Twilio calls.
    """
    # Telnyx sends JSON; Twilio sends form data
    payload     = request.get_json(silent=True) or {}
    telnyx_data = payload.get('data', {})
    telnyx_pl   = telnyx_data.get('payload', {})

    if telnyx_data:
        # Telnyx event
        event_type  = telnyx_data.get('event_type', '')
        call_sid    = telnyx_pl.get('call_control_id', telnyx_pl.get('call_leg_id', ''))
        duration    = str(telnyx_pl.get('duration_seconds', '0'))
        # Map Telnyx event_type → Twilio-style status string for DB consistency
        _status_map = {
            'call.initiated': 'initiated',
            'call.ringing':   'ringing',
            'call.answered':  'answered',
            'call.hangup':    'completed',
            'call.failed':    'failed',
        }
        call_status = _status_map.get(event_type, event_type)
    else:
        # Legacy Twilio form POST
        call_sid    = request.form.get('CallSid', '')
        call_status = request.form.get('CallStatus', '')
        duration    = request.form.get('CallDuration', '0')

    logger.info(f"📞 Call status: SID={call_sid} status={call_status} duration={duration}s")

    # Track status in memory for dialer queue polling
    if call_sid in _active_calls:
        _active_calls[call_sid]["status"] = call_status
        _active_calls[call_sid]["duration"] = int(duration or 0)

    # Persist to call_history DB
    if call_sid:
        try:
            update_call_history_status(call_sid, call_status, duration)
        except Exception as e:
            logger.debug(f"call_history update note: {e}")

    return '', 204


# ──────────────────────────────────────────────────────────────
# WEBSOCKET BRIDGE: The core audio relay
# ──────────────────────────────────────────────────────────────

async def handle_voice_stream(ws):
    """
    Core WebSocket handler: bridges Telnyx Media Streaming <-> XAI Realtime API.
    Called by flask-sock for each new Telnyx stream connection.

    Audio flow — L16 (PCM 16-bit) 16 kHz passthrough, zero conversion:
        Lead speaks  → Telnyx (L16 16kHz base64) → forward as-is → xAI (audio/pcm 16kHz)
        xAI responds → (L16 16kHz base64 delta)  → forward as-is → Telnyx (media event)

    Telnyx streaming_start configured with L16 codec at 16 kHz.
    xAI configured with audio/pcm 16 kHz for both input and output.
    No transcoding, no resampling — just routing base64 strings.

    ws: the Telnyx-side WebSocket (flask-sock)
    """
    logger.info("🎙️ Voice stream WebSocket connected (Telnyx)")

    # Wait for the 'start' event from Telnyx to get metadata
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
        # Telnyx may send a 'connected' event before the 'start' event
        start_msg = ws.receive()
        start_data = json.loads(start_msg)

    if start_data.get('event') == 'start':
        start_block = start_data.get('start', {})
        stream_sid  = (
            start_data.get('streamSid') or       # Twilio top-level
            start_data.get('stream_id') or        # Telnyx top-level ← was missing
            start_block.get('streamSid', '') or   # Twilio inside start block
            start_block.get('stream_id', '')      # Telnyx inside start block (fallback)
        )

        # Call Control streaming_start passes metadata via client_state (base64 JSON).
        # TeXML <Parameter> tags arrive as customParameters — support both.
        custom_params     = start_block.get('customParameters', {})
        client_state_raw  = start_block.get('client_state', '') or custom_params.get('client_state', '')
        client_state_meta = _decode_client_state(client_state_raw) if client_state_raw else {}

        # Prefer client_state values; fall back to customParameters for TeXML compat
        call_sid = (
            start_block.get('callControlId') or
            start_block.get('call_control_id') or
            client_state_meta.get('call_sid', '') or
            custom_params.get('callSid', '')
        )
        caller       = client_state_meta.get('caller',       '') or custom_params.get('caller', '') or start_block.get('from', '')
        called       = client_state_meta.get('called',       '') or custom_params.get('called', '') or start_block.get('to', '')
        direction    = client_state_meta.get('direction',    'inbound') or custom_params.get('direction', 'inbound')
        location_id  = client_state_meta.get('location_id', '') or custom_params.get('locationId', '')
        contact_id   = client_state_meta.get('contact_id',  '') or custom_params.get('contactId', '')
        contact_name = client_state_meta.get('contact_name','there') or custom_params.get('contactName', 'there')
        logger.info(f"🎙️ Stream started: SID={stream_sid} dir={direction} loc={location_id}")
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
    minimal_prompt = f"""You are {voice_bot_name}, a life insurance advisor on a live phone call.

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

            # Configure the XAI session — L16 (PCM) 16kHz both directions, matching Telnyx
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

            # Greeting: inject as assistant message so xAI speaks it directly
            # without needing to "interpret" a user command — much faster first response
            if direction == "outbound" or greeting:
                # Create assistant message with the greeting text
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
                # Now create a user message to set context for natural follow-up
                context_msg = {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"[Call just connected. You already said your greeting. Wait for {contact_name if contact_name != 'there' else 'the person'} to respond. When they do, continue the conversation naturally.]"
                            }
                        ]
                    }
                }
                await xai_ws.send(json.dumps(context_msg))
                # Generate the spoken greeting
                await xai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "instructions": f"Say this naturally in your own voice, like a real person would: {greeting}"
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
            ai_chunks_sent           = 0     # count of 20 ms PCM chunks sent → Telnyx
            call_active              = True
            _pending_transfer        = False  # set True when AI requests transfer; cleared on response.done

            # ── Telnyx -> XAI: L16 16kHz passthrough ──
            async def receive_from_telnyx():
                """Relay Telnyx → xAI. L16 16kHz base64 passthrough — no decoding."""
                nonlocal stream_sid, call_active
                try:
                    while call_active:
                        # ── Check for immediate takeover (agent barge-in) ──
                        # This runs on every loop iteration so takeover is near-instant
                        if call_sid and call_sid in _transfer_requests:
                            req = _transfer_requests.get(call_sid, {})
                            if req.get('type') == 'takeover':
                                transfer_info = _transfer_requests.pop(call_sid, {})
                                target = transfer_info.get('target', '')
                                logger.info(f"🔄 Immediate takeover in receive_from_telnyx: {call_sid} -> {target}")

                                t_api_key = (subscriber.get('voice_config') or {}).get('telnyx_api_key', '')
                                if t_api_key and target:
                                    # 1. Kill AI audio loop
                                    call_active = False
                                    # 2. Stop bidirectional streaming
                                    _call_control_command(call_sid, t_api_key, 'streaming_stop', {})
                                    # 3. Brief settle
                                    await asyncio.sleep(0.3)
                                    # 4. Transfer call to agent
                                    _call_control_command(call_sid, t_api_key, 'transfer', {'to': target})
                                    logger.info(f"🔄 Takeover transfer executed: {call_sid} -> {target}")
                                    if call_sid in _active_calls:
                                        _active_calls[call_sid]['status'] = 'transferred'
                                break  # exit the Telnyx receive loop

                        message = await asyncio.get_event_loop().run_in_executor(
                            None, ws.receive
                        )
                        if message is None:
                            logger.info("🎙️ Telnyx stream ended (None received)")
                            call_active = False
                            break

                        data = json.loads(message)

                        if data['event'] == 'media':
                            # L16 passthrough — forward Telnyx base64 directly to xAI
                            await xai_ws.send(json.dumps({
                                "type":  "input_audio_buffer.append",
                                "audio": data['media']['payload'],
                            }))

                        elif data['event'] == 'stop':
                            logger.info(f"🎙️ Telnyx stream stopped: {stream_sid}")
                            call_active = False
                            break

                except Exception as e:
                    logger.info(f"🎙️ Telnyx receive ended: {e}")
                    call_active = False

            # ── xAI -> Telnyx: L16 16kHz passthrough ──
            async def receive_from_xai():
                """Relay xAI → Telnyx. L16 16kHz base64 passthrough — no encoding."""
                nonlocal last_assistant_item, response_start_timestamp, ai_chunks_sent, call_active, _pending_transfer

                def _send_audio_to_telnyx(raw_b64: str):
                    """Forward xAI L16 (PCM 16kHz) base64 directly to Telnyx — no conversion."""
                    nonlocal ai_chunks_sent
                    ws.send(json.dumps({
                        "event":     "media",
                        "stream_id": stream_sid,
                        "media":     {"payload": raw_b64},
                    }))
                    ai_chunks_sent += 1

                try:
                    async for xai_message in xai_ws:
                        if not call_active:
                            break

                        response   = json.loads(xai_message)
                        event_type = response.get('type', '')

                        if event_type in LOG_EVENT_TYPES:
                            logger.info(f"🎙️ XAI event: {event_type}")

                        # xAI PCMU → passthrough → Telnyx (both event name variants)
                        if event_type in ('response.audio.delta', 'response.output_audio.delta') \
                                and 'delta' in response:
                            _send_audio_to_telnyx(response['delta'])
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

                            # Clear Telnyx's audio buffer
                            ws.send(json.dumps({"event": "clear", "stream_id": stream_sid}))
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

                                # Get Telnyx API key for transfer commands
                                t_api_key = (subscriber.get('voice_config') or {}).get('telnyx_api_key', '')
                                if t_api_key and target:
                                    # 1. Stop the AI audio stream
                                    call_active = False

                                    # 2. Stop bidirectional streaming on Telnyx
                                    _call_control_command(call_sid, t_api_key, 'streaming_stop', {})
                                    logger.info(f"🔄 Streaming stopped for {call_sid}")

                                    # 3. Brief pause to let stream teardown settle
                                    await asyncio.sleep(0.5)

                                    # 4. Transfer the live call to the agent's number
                                    transfer_ok = _call_control_command(call_sid, t_api_key, 'transfer', {
                                        'to': target,
                                    })

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

                                    t_api_key = (subscriber.get('voice_config') or {}).get('telnyx_api_key', '')
                                    if t_api_key and target:
                                        call_active = False
                                        _call_control_command(call_sid, t_api_key, 'streaming_stop', {})
                                        await asyncio.sleep(0.5)
                                        _call_control_command(call_sid, t_api_key, 'transfer', {
                                            'to': target,
                                        })
                                        logger.info(f"🔄 Takeover transfer to {target}")
                                        if call_sid in _active_calls:
                                            _active_calls[call_sid]['status'] = 'transferred'

                except websockets.exceptions.ConnectionClosed:
                    logger.info("🎙️ XAI WebSocket closed")
                    call_active = False
                except Exception as e:
                    logger.error(f"🎙️ XAI receive error: {e}")
                    call_active = False

            # Run Telnyx↔xAI audio bridge + background context enrichment concurrently
            await asyncio.gather(
                receive_from_telnyx(),   # Telnyx → xAI
                receive_from_xai(),      # xAI → Telnyx
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



# ──────────────────────────────────────────────────────────────
# ROUTE: Test voice connection (health check)
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/test', methods=['POST'])
def test_voice_connection():
    """Test that XAI and Telnyx credentials are valid."""
    data = request.json or {}
    location_id = data.get('location_id', '')

    results = {"xai": False, "telnyx": False, "errors": []}

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

    # Test Telnyx API key — list outbound voice profiles as a lightweight auth check
    if location_id:
        subscriber = _get_subscriber_by_location(location_id)
        if subscriber:
            voice_config = subscriber.get("voice_config") or {}
            telnyx_api_key = voice_config.get("telnyx_api_key", "")
            if telnyx_api_key:
                try:
                    r = http_requests.get(
                        f"{TELNYX_API_BASE}/outbound_voice_profiles?page[size]=1",
                        headers={"Authorization": f"Bearer {telnyx_api_key}"},
                        timeout=10,
                    )
                    results["telnyx"] = r.status_code == 200
                    if r.status_code != 200:
                        results["errors"].append(f"Telnyx API returned {r.status_code} — check your API Key")
                except Exception as e:
                    results["errors"].append(f"Telnyx connection failed: {str(e)}")
            else:
                results["errors"].append("Telnyx API Key not configured")

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
    Initiate an outbound call to a specific contact.
    Used by the call panel. Returns call_sid for status tracking.
    """
    data = request.json or {}
    contact_id    = data.get('contact_id', '')
    phone         = data.get('phone', '')
    first_name    = data.get('first_name', 'there')
    dial_mode     = data.get('dial_mode', 'ai')        # 'ai' or 'human'
    dial_attempt  = int(data.get('dial_attempt', 1))   # retry attempt number

    if not phone:
        return jsonify({"error": "Phone number is required"}), 400

    # Get subscriber info
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

    # For AI mode, voice must be enabled; human mode always allowed if credentials exist
    if dial_mode == 'ai' and not voice_config.get('enabled'):
        return jsonify({"error": "Voice AI is not enabled. Enable it in the Voice tab."}), 400

    telnyx_api_key       = voice_config.get('telnyx_api_key', '')
    telnyx_connection_id = voice_config.get('telnyx_connection_id', '')
    telnyx_number        = voice_config.get('telnyx_phone_number', '')

    # Local presence: pick a number from the pool matching the destination area code
    local_presence_enabled = voice_config.get('local_presence', False)
    if local_presence_enabled:
        dest_area = phone.lstrip('+').lstrip('1')[:3] if phone else ''
        local_pool = voice_config.get('local_presence_numbers', [])
        for lp_num in local_pool:
            lp_area = lp_num.lstrip('+').lstrip('1')[:3]
            if lp_area == dest_area:
                telnyx_number = lp_num
                break

    if not all([telnyx_api_key, telnyx_connection_id, telnyx_number]):
        missing = []
        if not telnyx_api_key:
            missing.append("API Key")
        if not telnyx_connection_id:
            missing.append("Connection ID")
        if not telnyx_number:
            missing.append("Phone Number")
        return jsonify({"error": f"Telnyx credentials not configured (missing: {', '.join(missing)})"}), 400

    # Dialer settings
    max_dial_attempts = int(voice_config.get('dial_attempts', 2))
    auto_record       = voice_config.get('auto_record', True)
    auto_transcribe   = voice_config.get('auto_transcribe', False)
    use_amd           = dial_mode == 'ai'  # AMD only useful for AI calls

    try:
        host        = request.host
        webhook_url = f"https://{host}/voice/inbound"
        client_state = _encode_client_state({
            'location_id':      location_id,
            'caller':           telnyx_number,
            'called':           phone,
            'direction':        'outbound',
            'contact_id':       contact_id,
            'contact_name':     first_name,
            'dial_mode':        dial_mode,
            'dial_attempt':     dial_attempt,
            'max_dial_attempts': max_dial_attempts,
            'auto_record':      auto_record,
            'auto_transcribe':  auto_transcribe,
            'use_amd':          use_amd,
        })

        call_payload = {
            "connection_id":      telnyx_connection_id,
            "to":                 phone,
            "from":               telnyx_number,
            "webhook_url":        webhook_url,
            "webhook_url_method": "POST",
            "client_state":       client_state,
        }

        # Enable AMD for AI outbound calls — premium ML-based detection (fastest)
        if use_amd:
            call_payload["answering_machine_detection"] = "premium"
            call_payload["answering_machine_detection_config"] = {
                "total_analysis_time_millis": 3500,
            }

        resp = http_requests.post(
            f"{TELNYX_API_BASE}/calls",
            headers={
                "Authorization": f"Bearer {telnyx_api_key}",
                "Content-Type":  "application/json",
            },
            json=call_payload,
            timeout=10,
        )
        if not resp.ok:
            telnyx_errors = resp.json().get('errors', [{}])
            telnyx_msg = telnyx_errors[0].get('detail', resp.text) if telnyx_errors else resp.text
            logger.error(f"Telnyx dial error {resp.status_code}: {resp.text}")
            hint = ""
            if resp.status_code == 403:
                hint = " — Check: (1) API key has outbound call permissions, (2) Telnyx account is funded, (3) Connection ID matches the phone number's assigned connection in Telnyx portal"
            return jsonify({"error": f"Telnyx {resp.status_code}: {telnyx_msg}{hint}"}), 400
        call_data    = resp.json().get("data", {})
        call_ctrl_id = call_data.get("call_control_id", call_data.get("call_leg_id", ""))

        # Track this call for the dialer queue
        _active_calls[call_ctrl_id] = {
            "status":     "initiated",
            "duration":   0,
            "contact_id": contact_id,
            "phone":      phone,
            "name":       first_name,
            "dial_mode":  dial_mode,
            "attempt":    dial_attempt,
        }

        # Persist to call_history DB
        save_call_to_history(
            location_id=location_id,
            call_sid=call_ctrl_id,
            phone=phone,
            contact_id=contact_id,
            contact_name=first_name,
            direction='outbound',
            status='initiated'
        )

        logger.info(f"📞 Dialer call [{dial_mode}]: {telnyx_number} -> {phone} ({first_name}) attempt={dial_attempt} ctrl_id={call_ctrl_id}")
        return jsonify({"status": "calling", "call_sid": call_ctrl_id, "dial_mode": dial_mode})

    except Exception as e:
        logger.error(f"Dialer call failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/call-status/<call_sid>', methods=['GET'])
@login_required
def get_call_status(call_sid):
    """Poll call status for the dialer queue."""
    if call_sid in _active_calls:
        info = _active_calls[call_sid]
        # Clean up completed calls after returning status
        if info["status"] in ("completed", "busy", "no-answer", "failed", "canceled"):
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
    """Hang up the currently active call by call_control_id."""
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

    api_key = (subscriber.get('voice_config') or {}).get('telnyx_api_key', '')
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured"}), 400

    success = _call_control_command(call_sid, api_key, 'hangup', {})
    if call_sid in _active_calls:
        _active_calls[call_sid]['status'] = 'canceled'

    if success:
        return jsonify({"status": "hung_up"})
    return jsonify({"error": "Hangup failed — call may have already ended"}), 400


# ──────────────────────────────────────────────────────────────
# ROUTE: Recording status callback from Telnyx
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/recording-status', methods=['POST'])
def recording_status_callback():
    """
    Telnyx posts call.recording.saved events here (JSON).
    Also handles legacy Twilio form-POST for any in-flight recordings.
    """
    # Try JSON (Telnyx) first
    payload = request.get_json(silent=True) or {}
    telnyx_data = payload.get('data', {})

    if telnyx_data and telnyx_data.get('event_type') == 'call.recording.saved':
        telnyx_pl = telnyx_data.get('payload', {})
        call_sid = telnyx_pl.get('call_control_id', telnyx_pl.get('call_leg_id', ''))
        recording_url = telnyx_pl.get('public_recording_urls', {}).get('mp3', '') or telnyx_pl.get('recording_urls', {}).get('mp3', '')
        recording_sid = telnyx_pl.get('recording_id', call_sid)
        recording_duration = str(int(telnyx_pl.get('duration_millis', 0) / 1000))
        recording_status = 'completed'
    else:
        # Legacy Twilio form POST
        call_sid = request.form.get('CallSid', '')
        recording_sid = request.form.get('RecordingSid', '')
        recording_url = request.form.get('RecordingUrl', '')
        recording_status = request.form.get('RecordingStatus', '')
        recording_duration = request.form.get('RecordingDuration', '0')

    logger.info(f"🎙️ Recording callback: SID={call_sid} rec={recording_sid} status={recording_status} dur={recording_duration}s")

    if recording_status == 'completed' and (recording_url or recording_sid):
        # For Telnyx: use the direct public MP3 URL
        # For Twilio: use our proxy URL
        store_url = recording_url if recording_url else f"/voice/recording/{recording_sid}"

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
                logger.info(f"🎙️ Recording saved for call {call_sid}: {store_url}")
            except Exception as e:
                logger.error(f"Failed to save recording: {e}")
                conn.rollback()
            finally:
                return_db_connection(conn)

    return '', 204


@voice_bp.route('/voice/transcription', methods=['POST'])
def transcription_webhook():
    """
    Telnyx posts call.transcription.* events here.
    Accumulates transcript segments and persists them to call_history on call end.
    """
    payload    = request.get_json(silent=True) or {}
    data       = payload.get('data', {})
    event_type = data.get('event_type', '')
    pl         = data.get('payload', {})

    call_sid   = pl.get('call_control_id', pl.get('call_leg_id', ''))
    transcript = pl.get('transcription_data', {}).get('transcript', '')

    if not call_sid or not transcript:
        return '', 204

    logger.info(f"📝 Transcription [{event_type}] {call_sid}: {transcript[:80]}")

    if event_type in ('call.transcription.completed', 'call.transcription.partial'):
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
                logger.debug(f"Transcription save note: {e}")
                conn.rollback()
            finally:
                return_db_connection(conn)

    return '', 204


@voice_bp.route('/voice/recording/<recording_sid>', methods=['GET'])
@login_required
def stream_recording(recording_sid):
    """
    Proxy a recording URL. For Telnyx recordings stored as direct MP3 URLs,
    look up the URL from the DB and redirect. Falls back to Telnyx API download.
    """
    subscriber, vc, api_key = _get_current_subscriber_voice()

    # First try to find the recording URL in our DB
    conn = get_db_connection()
    recording_url = None
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT recording_url FROM call_history WHERE recording_sid = %s LIMIT 1",
                (recording_sid,)
            )
            row = cur.fetchone()
            cur.close()
            if row and row[0] and row[0].startswith('http'):
                recording_url = row[0]
        except Exception:
            pass
        finally:
            return_db_connection(conn)

    if recording_url:
        # Redirect to the direct Telnyx MP3 URL (time-limited signed URL)
        from flask import redirect
        return redirect(recording_url)

    # Fallback: try to fetch from Telnyx recordings API
    if api_key:
        try:
            resp = http_requests.get(
                f"{TELNYX_API_BASE}/recordings/{recording_sid}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                rec_data = resp.json().get('data', {})
                mp3_url = rec_data.get('download_urls', {}).get('mp3', '')
                if mp3_url:
                    from flask import redirect
                    return redirect(mp3_url)
        except Exception as e:
            logger.error(f"Recording fetch failed: {e}")

    return jsonify({"error": "Recording not found"}), 404


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
    Executes the transfer DIRECTLY via Telnyx API — does NOT rely on the
    WebSocket loop to detect the request (which could be blocked on ws.receive).
    Steps: 1) Stop AI streaming, 2) Transfer call to agent's number.
    """
    data = request.json or {}
    call_sid = data.get('call_sid', '')
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    # Look up subscriber voice config for transfer number
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

    target = data.get('target') or voice_cfg.get('transfer_number', '')
    if not target:
        return jsonify({"error": "No transfer number configured. Set a Transfer Number in Voice Settings first."}), 400

    # Normalize target
    if not target.startswith('+'):
        target = '+1' + target.lstrip('1') if len(target.replace('-','').replace(' ','')) <= 10 else '+' + target

    api_key = voice_cfg.get('telnyx_api_key', '')
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured"}), 400

    # Verify the call is actually active
    if call_sid not in _active_calls:
        return jsonify({"error": "Call not found or already ended"}), 404

    call_info = _active_calls[call_sid]
    if call_info.get('status') in ('completed', 'failed', 'transferred', 'no-answer'):
        return jsonify({"error": f"Call already in terminal state: {call_info.get('status')}"}), 400

    logger.info(f"🔄 Takeover: executing direct transfer for call {call_sid} -> {target}")

    # Also signal the WebSocket bridge to stop the AI audio loop
    _transfer_requests[call_sid] = {
        'type': 'takeover',
        'target': target,
        'reason': 'Agent initiated live takeover',
    }

    # Step 1: Stop the AI audio stream on this call
    stop_ok = _call_control_command(call_sid, api_key, 'streaming_stop', {})
    if stop_ok:
        logger.info(f"🔄 Takeover: streaming stopped for {call_sid}")
    else:
        logger.warning(f"🔄 Takeover: streaming_stop failed for {call_sid} (may already be stopped)")

    # Step 2: Brief pause to let Telnyx process the stream stop
    time.sleep(0.4)

    # Step 3: Transfer the live call to the agent's number
    transfer_ok = _call_control_command(call_sid, api_key, 'transfer', {'to': target})
    if transfer_ok:
        logger.info(f"🔄 Takeover: call {call_sid} transferred to {target}")
        _active_calls[call_sid]['status'] = 'transferred'
        return jsonify({"status": "transferred", "call_sid": call_sid, "target": target})
    else:
        logger.error(f"🔄 Takeover: transfer FAILED for call {call_sid} -> {target}")
        # Clean up the signal
        _transfer_requests.pop(call_sid, None)
        return jsonify({"error": "Transfer failed — the call may have ended. Check that your Transfer Number is in +1XXXXXXXXXX format."}), 400


# ──────────────────────────────────────────────────────────────
# ROUTE: Live transfer to another phone number
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/transfer', methods=['POST'])
@login_required
def voice_transfer():
    """
    Transfer an active call to another phone number via Telnyx call control.
    Uses the transfer command to bridge the caller to the target number.
    """
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

    # Get API key
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

    api_key = voice_cfg.get('telnyx_api_key', '')
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured"}), 400

    # Execute Telnyx call control transfer
    success = _call_control_command(call_sid, api_key, 'transfer', {"to": transfer_to})
    if success:
        _active_calls[call_sid]['status'] = 'transferred'
        logger.info(f"📞 Live transfer: call {call_sid} -> {transfer_to}")
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
    """Get subscriber, voice_config, and Telnyx API key for the logged-in user."""
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
        api_key = vc.get('telnyx_api_key', '')
        return subscriber, vc, api_key or None
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
# MAGIC WAND: One-shot Telnyx infrastructure provisioning
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/automate-setup', methods=['POST'])
@login_required
def automate_telnyx_setup():
    """
    Takes a raw Telnyx API key and provisions the full telephony stack:
      1. Outbound Voice Profile  — grants permission to dial out
      2. Call Control Application — AI webhook routing
      3. SIP Credential Connection — browser WebRTC dialer
    All generated IDs are saved to voice_config so downstream routes
    (setup_voip, /numbers/buy, etc.) work immediately with no manual config.
    """
    data = request.json or {}
    api_key = data.get('api_key', '').strip()

    if not api_key:
        return jsonify({"error": "Telnyx API Key is required"}), 400

    subscriber, vc, _ = _get_current_subscriber_voice()
    location_id = subscriber.get('location_id', 'unknown')
    host = request.host
    webhook_url = f"https://{host}/voice/inbound"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _list_or_create(list_url, create_url, create_json, step_name):
        """
        GET list_url first. If any records exist, reuse the first one.
        Only POST to create_url if the account has none at all.
        Returns the resource ID as a string, raises ValueError on failure.
        """
        list_resp = http_requests.get(list_url, headers=headers, timeout=10)
        if list_resp.status_code == 200:
            items = list_resp.json().get('data', [])
            if items:
                found_id = str(items[0]['id'])
                logger.info(f"Found existing {step_name}: {found_id} ({len(items)} total)")
                return found_id

        logger.info(f"No existing {step_name} found — creating")
        create_resp = http_requests.post(create_url, headers=headers, json=create_json, timeout=15)
        if create_resp.status_code not in (200, 201):
            logger.error(f"{step_name} create failed {create_resp.status_code}: {create_resp.text}")
            raise ValueError(f"{step_name} creation failed ({create_resp.status_code}): {create_resp.text[:400]}")
        new_id = str(create_resp.json()['data']['id'])
        logger.info(f"Created {step_name}: {new_id}")
        return new_id

    try:
        vc['telnyx_api_key'] = api_key

        # 1. Outbound Voice Profile — use whatever exists in the account, else create one
        ovp_id = _list_or_create(
            list_url=f"{TELNYX_API_BASE}/outbound_voice_profiles",
            create_url=f"{TELNYX_API_BASE}/outbound_voice_profiles",
            create_json={
                "name": f"GrokBot_{location_id[:24]}",
                "traffic_type": "Conversational",
                "service_plan": "us",
                "enabled": True,
                "whitelisted_destinations": ["US"],
            },
            step_name="Outbound Voice Profile",
        )
        vc['telnyx_outbound_profile_id'] = ovp_id
        _save_voice_config(current_user.email, vc)

        # 2. Call Control Application — use whatever exists, else create one
        call_control_id = _list_or_create(
            list_url=f"{TELNYX_API_BASE}/call_control_applications",
            create_url=f"{TELNYX_API_BASE}/call_control_applications",
            create_json={
                "application_name": f"GrokBot_AI_{location_id[:24]}",
                "webhook_event_url": webhook_url,
                "webhook_api_version": "2",
                "outbound_voice_profile_id": ovp_id,
                "first_command_timeout": True,
                "first_command_timeout_secs": 30,
                "webhook_timeout_secs": 10,
                "anchor_site": "Latency",
            },
            step_name="Call Control Application",
        )
        vc['telnyx_connection_id'] = call_control_id
        _save_voice_config(current_user.email, vc)

        # 3. SIP Credential Connection — use whatever exists, else create one
        sip_connection_id = _list_or_create(
            list_url=f"{TELNYX_API_BASE}/credential_connections",
            create_url=f"{TELNYX_API_BASE}/credential_connections",
            create_json={
                "connection_name": f"GrokBot_WebRTC_{location_id[:20]}",
                "outbound_voice_profile_id": ovp_id,
            },
            step_name="SIP Credential Connection",
        )
        vc['telnyx_sip_connection_id'] = sip_connection_id
        _save_voice_config(current_user.email, vc)

        # 4. Auto-detect phone number — fetch existing numbers from the Telnyx
        #    account and populate telnyx_phone_number so the user doesn't have to
        #    paste it manually.  This also feeds the Numbers and Trust Hub tabs.
        phone_number = vc.get('telnyx_phone_number', '').strip()
        numbers_list = []
        if not phone_number:
            try:
                num_resp = http_requests.get(
                    f"{TELNYX_API_BASE}/phone_numbers",
                    headers=headers,
                    params={"page[size]": 50},
                    timeout=10,
                )
                if num_resp.status_code == 200:
                    numbers_list = num_resp.json().get('data', [])
                    if numbers_list:
                        phone_number = numbers_list[0].get('phone_number', '')
                        if phone_number:
                            vc['telnyx_phone_number'] = phone_number
                            _save_voice_config(current_user.email, vc)
                            logger.info(f"Auto-populated Telnyx phone number: {phone_number}")
            except Exception as e:
                logger.warning(f"Could not auto-detect Telnyx numbers: {e}")

        return jsonify({
            "status": "success",
            "message": "Telnyx infrastructure provisioned!",
            "telnyx_connection_id": call_control_id,
            "telnyx_phone_number": phone_number,
            "numbers_count": len(numbers_list),
        })

    except ValueError as e:
        _save_voice_config(current_user.email, vc)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Telnyx automation error: {e}", exc_info=True)
        _save_voice_config(current_user.email, vc)
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


# ──────────────────────────────────────────────────────────────
# BROWSER VoIP: Telnyx WebRTC SDK support
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/setup-voip', methods=['POST'])
@login_required
def setup_voip():
    """
    One-time setup: create a Telnyx Telephony Credential for browser-based VoIP.
    Telephony credentials use Telnyx's WebRTC/SIP infrastructure directly — they
    do NOT use the Call Control Application ID (which is only for outbound AI calls).
    Stores telnyx_credential_id in voice_config.
    """
    subscriber, vc, api_key = _get_current_subscriber_voice()
    location_id = subscriber.get('location_id', 'unknown') if subscriber else 'unknown'
    logger.info(f"[setup-voip] request from location_id={location_id}")
    if not api_key:
        logger.warning(f"[setup-voip] No Telnyx API key for location_id={location_id}")
        return jsonify({"error": "Telnyx API key not configured"}), 400
    logger.info(f"[setup-voip] API key present (prefix={api_key[:8]}…)")

    cred_id = vc.get('telnyx_credential_id', '')
    sip_conn_id = str(vc.get('telnyx_sip_connection_id', '')).strip()
    logger.info(f"[setup-voip] stored cred_id={cred_id!r}  sip_connection_id={sip_conn_id!r}")

    try:
        # If we have a stored credential ID, verify it still exists
        if cred_id:
            logger.info(f"[setup-voip] Verifying stored credential {cred_id}")
            check = http_requests.get(
                f"{TELNYX_API_BASE}/telephony_credentials/{cred_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=8,
            )
            logger.info(f"[setup-voip] Credential check status: {check.status_code}")
            if check.status_code == 404:
                logger.info(f"[setup-voip] Stored credential {cred_id} no longer exists — will recreate")
                cred_id = ''
                vc.pop('telnyx_credential_id', None)
            elif check.status_code == 200:
                logger.info(f"[setup-voip] Credential {cred_id} is valid — reusing")

        if not cred_id:
            # Telnyx requires connection_id when creating a telephony credential —
            # it ties the WebRTC client to a specific SIP/Call Control connection.
            # Use the dedicated SIP credential connection (created during automate-setup).
            # telnyx_sip_connection_id  → browser WebRTC dialer (credential_connections)
            # telnyx_connection_id      → AI call routing (call_control_applications)
            # They must stay separate; mixing them causes Telnyx 422 errors.
            # Force to str: values stored as integers in the DB must be strings for Telnyx.
            connection_id = sip_conn_id
            if not connection_id:
                logger.error(f"[setup-voip] No sip_connection_id in voice_config for location_id={location_id} — run automate-setup first")
                return jsonify({"error": "Browser dialer not configured. Please reconnect your Telnyx API key via the setup wizard."}), 400

            logger.info(f"[setup-voip] Creating telephony credential with connection_id={connection_id}")
            resp = http_requests.post(
                f"{TELNYX_API_BASE}/telephony_credentials",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "name": f"agent_{location_id}",
                    "connection_id": connection_id,
                },
                timeout=10,
            )
            logger.info(f"[setup-voip] Create credential response: {resp.status_code}")
            if resp.status_code not in (200, 201):
                # Telnyx can return errors as a list OR a dict depending on the
                # validation path — guard against both to avoid KeyError: 0.
                try:
                    err_data = resp.json().get('errors', [])
                    if isinstance(err_data, list) and err_data and isinstance(err_data[0], dict):
                        msg = err_data[0].get('detail', resp.text)
                    else:
                        msg = str(err_data) if err_data else resp.text
                except Exception:
                    msg = resp.text
                logger.error(f"[setup-voip] Credential creation failed {resp.status_code}: {msg}")
                return jsonify({"error": f"Telnyx credential creation failed: {msg}"}), 400
            cred_data = resp.json().get('data', {})
            cred_id = cred_data.get('id', '')
            if not cred_id:
                logger.error(f"[setup-voip] No credential ID in Telnyx response: {resp.text[:200]}")
                return jsonify({"error": "No credential ID returned from Telnyx"}), 500

            vc['telnyx_credential_id'] = cred_id
            _save_voice_config(current_user.email, vc)
            logger.info(f"[setup-voip] Created Telnyx telephony credential: {cred_id}")

        logger.info(f"[setup-voip] Ready — returning credential_id={cred_id}")
        return jsonify({"status": "ready", "credential_id": cred_id})

    except Exception as e:
        logger.error(f"[setup-voip] Unexpected error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/token', methods=['POST'])
@login_required
def generate_voice_token():
    """Generate a short-lived Telnyx JWT token for browser-based calling."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    if not api_key:
        return jsonify({"error": "Telnyx API key not configured"}), 400

    cred_id = vc.get('telnyx_credential_id', '')
    location_id = subscriber.get('location_id', 'unknown') if subscriber else 'unknown'
    logger.info(f"[voice/token] request from location_id={location_id}  cred_id={cred_id!r}")
    if not cred_id:
        logger.warning(f"[voice/token] No credential_id stored — user needs to run setup-voip first")
        return jsonify({"error": "Browser calling not set up. Click Setup VoIP first."}), 400

    try:
        logger.info(f"[voice/token] Requesting token for cred_id={cred_id}")
        resp = http_requests.post(
            f"{TELNYX_API_BASE}/telephony_credentials/{cred_id}/token",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=10,
        )
        logger.info(f"[voice/token] Telnyx token response: {resp.status_code}  body_len={len(resp.text)}")
        if resp.status_code not in (200, 201):
            if resp.status_code == 404:
                # Credential deleted — clear it so user can re-setup
                logger.warning(f"[voice/token] Credential {cred_id} not found on Telnyx — clearing stored ID")
                vc.pop('telnyx_credential_id', None)
                _save_voice_config(current_user.email, vc)
                return jsonify({"error": "Browser calling not set up. Click Setup VoIP first."}), 400
            logger.error(f"[voice/token] Token fetch failed {resp.status_code}: {resp.text[:300]}")
            return jsonify({"error": f"Token generation failed: {resp.text}"}), 400

        # Telnyx returns the JWT as plain text, not JSON
        token = resp.text.strip()
        if not token:
            logger.error(f"[voice/token] Empty token body returned from Telnyx")
            return jsonify({"error": "Empty token returned from Telnyx"}), 500
        identity = f"agent_{location_id}"
        logger.info(f"[voice/token] Token issued successfully — len={len(token)}  identity={identity}")
        return jsonify({"token": token, "identity": identity})

    except Exception as e:
        logger.error(f"[voice/token] Unexpected error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/voip-answer', methods=['POST'])
def voip_answer():
    """
    Legacy TwiML endpoint — no longer used with Telnyx WebRTC.
    Telnyx WebRTC calls go through the SIP connection directly; events arrive at /voice/inbound.
    """
    return jsonify({'result': 'ok'}), 200


# ──────────────────────────────────────────────────────────────
# TRUST HUB: Phone number management + carrier health (Telnyx)
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/numbers', methods=['GET'])
@login_required
def list_telnyx_numbers():
    """List all Telnyx phone numbers on the account with rich health info."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not api_key:
        logger.warning("list_telnyx_numbers: no API key configured")
        return jsonify({"error": "Telnyx API key not configured. Go to Settings and click Connect Telnyx."}), 400

    # Fetch nicknames from voice_config
    nicknames = vc.get('number_nicknames', {})  # { "+1234": "Main Line" }
    primary_number = vc.get('telnyx_phone_number', '')

    try:
        # Paginate through ALL numbers
        all_numbers = []
        page = 1
        while True:
            resp = http_requests.get(
                f"{TELNYX_API_BASE}/phone_numbers",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"page[size]": 250, "page[number]": page},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"list_telnyx_numbers: Telnyx returned {resp.status_code}: {resp.text[:300]}")
                return jsonify({"error": f"Telnyx API error {resp.status_code}. Check your API key in Settings."}), 400
            body = resp.json()
            batch = body.get('data', [])
            all_numbers.extend(batch)
            # Check if more pages exist
            meta = body.get('meta', {})
            total_pages = meta.get('total_pages', 1)
            if page >= total_pages or not batch:
                break
            page += 1

        logger.info(f"list_telnyx_numbers: fetched {len(all_numbers)} numbers across {page} page(s)")

        result = []
        for n in all_numbers:
            raw_features = n.get('features') or []
            features = {}
            for f in raw_features:
                if isinstance(f, dict):
                    features[f.get('name', '')] = f.get('enabled', False)
            tags = n.get('tags') or []
            phone = n.get('phone_number', '')
            number_id = n.get('id', '')
            connection_id = n.get('connection_id', '')
            is_primary = phone == primary_number
            nickname = nicknames.get(phone, tags[0] if tags else '')
            result.append({
                "sid": number_id,
                "phone": phone,
                "nickname": nickname,
                "is_primary": is_primary,
                "capabilities": {
                    "voice": features.get('voice', False),
                    "sms": features.get('sms', False),
                    "mms": features.get('mms', False),
                    "fax": features.get('fax', False),
                },
                "status": n.get('status', 'active'),
                "connection_id": connection_id,
                "cnam_listed": n.get('cnam_listing_enabled', False),
                "emergency_enabled": n.get('emergency_enabled', False),
                "created_at": n.get('created_at', ''),
                "billing_group_id": n.get('billing_group_id', ''),
                "number_type": n.get('phone_number_type', 'local'),
            })

        return jsonify({"numbers": result, "total": len(result)})

    except Exception as e:
        logger.error(f"Failed to list Telnyx numbers: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/search', methods=['GET'])
@login_required
def search_available_numbers():
    """Search for available Telnyx phone numbers to purchase."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured"}), 400

    area_code = request.args.get('area_code', '')
    state = request.args.get('state', '')
    contains = request.args.get('contains', '')

    try:
        params = {
            "filter[country_code]": "US",
            "filter[number_type]": "local",
            "filter[features][]": "voice",
            "page[size]": 20,
        }
        if area_code:
            params["filter[npa]"] = area_code
        if state:
            params["filter[administrative_area]"] = state
        if contains:
            params["filter[phone_number][contains]"] = contains

        resp = http_requests.get(
            f"{TELNYX_API_BASE}/available_phone_numbers",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=15,
        )
        if resp.status_code != 200:
            return jsonify({"error": f"Telnyx API error: {resp.status_code} {resp.text}"}), 400

        data = resp.json().get('data', [])
        result = []
        for n in data:
            features = n.get('features', [])
            feat_names = [f.get('name', '') for f in features] if isinstance(features, list) else []
            result.append({
                "phone": n.get('phone_number', ''),
                "friendly_name": n.get('phone_number', ''),
                "locality": n.get('locality', ''),
                "region": n.get('region', ''),
                "monthly_cost": n.get('cost_information', {}).get('monthly_cost', '1.00'),
                "capabilities": {
                    "voice": 'voice' in feat_names,
                    "sms": 'sms' in feat_names,
                },
            })

        return jsonify({"numbers": result, "total": len(result)})

    except Exception as e:
        logger.error(f"Number search failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/buy', methods=['POST'])
@login_required
def buy_telnyx_number():
    """Purchase a Telnyx phone number and configure it for voice."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured"}), 400

    data = request.json or {}
    phone_number = data.get('phone_number', '')
    if not phone_number:
        return jsonify({"error": "Phone number is required"}), 400

    # str() coercion: IDs stored as int in DB must be strings for Telnyx API
    connection_id = str(vc.get('telnyx_connection_id', '')).strip()
    try:
        order_payload = {
            "phone_numbers": [{"phone_number": phone_number}],
        }
        if connection_id:
            order_payload["connection_id"] = connection_id

        resp = http_requests.post(
            f"{TELNYX_API_BASE}/number_orders",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=order_payload,
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            return jsonify({"error": f"Purchase failed: {resp.text}"}), 400

        order_data = resp.json().get('data', {})
        ordered_nums = order_data.get('phone_numbers', [{}])
        purchased_phone = ordered_nums[0].get('phone_number', phone_number) if ordered_nums else phone_number
        order_id = order_data.get('id', '')

        logger.info(f"Purchased Telnyx number: {purchased_phone} (order: {order_id})")

        # Auto-enable CNAM if spam protection is active
        trust_hub = vc.get('trust_hub', {})
        cnam_applied = False
        if trust_hub.get('auto_cnam') and trust_hub.get('business_name'):
            try:
                # The number needs a moment to provision; get its ID from the order
                num_id = ordered_nums[0].get('id', '') if ordered_nums else ''
                if num_id:
                    import time
                    time.sleep(2)  # Brief delay for Telnyx provisioning
                    cnam_resp = http_requests.patch(
                        f"{TELNYX_API_BASE}/phone_numbers/{num_id}",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"cnam_listing_enabled": True},
                        timeout=10,
                    )
                    cnam_applied = cnam_resp.status_code == 200
                    logger.info(f"Auto-CNAM for {purchased_phone}: {'ok' if cnam_applied else cnam_resp.status_code}")
            except Exception as e:
                logger.warning(f"Auto-CNAM failed for {purchased_phone}: {e}")

        return jsonify({
            "status": "purchased",
            "phone": purchased_phone,
            "sid": order_id,
            "cnam_applied": cnam_applied,
        })

    except Exception as e:
        logger.error(f"Number purchase failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/release', methods=['POST'])
@login_required
def release_telnyx_number():
    """Release (cancel) a Telnyx phone number."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured"}), 400

    data = request.json or {}
    phone_sid = data.get('sid', '')
    phone_number = data.get('phone_number', '')

    if not phone_sid and not phone_number:
        return jsonify({"error": "Number ID or phone number is required"}), 400

    try:
        # Telnyx uses the number ID (UUID) or we look it up by phone number
        number_id = phone_sid
        if not number_id and phone_number:
            # Look up the number ID
            resp = http_requests.get(
                f"{TELNYX_API_BASE}/phone_numbers",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"filter[phone_number]": phone_number},
                timeout=10,
            )
            if resp.status_code == 200:
                items = resp.json().get('data', [])
                if items:
                    number_id = items[0].get('id', '')

        if not number_id:
            return jsonify({"error": "Number not found"}), 404

        resp = http_requests.delete(
            f"{TELNYX_API_BASE}/phone_numbers/{number_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code not in (200, 204):
            return jsonify({"error": f"Release failed: {resp.text}"}), 400

        logger.info(f"Released Telnyx number: {number_id}")
        return jsonify({"status": "released"})

    except Exception as e:
        logger.error(f"Number release failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/trust-hub', methods=['GET'])
@login_required
def get_trust_hub_status():
    """
    Get number health and carrier trust status for all Telnyx numbers.
    Shows STIR/SHAKEN status, CNAM registration, carrier registration info,
    and spam remediation guidance — Wavv-style trust dashboard.
    """
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured. Go to Settings and click Connect Telnyx."}), 400

    # Trust hub registration stored in voice_config
    trust_hub = vc.get('trust_hub', {})
    business_name = trust_hub.get('business_name', '')
    ein = trust_hub.get('ein', '')

    result = {
        "stir_shaken": {
            "status": "telnyx_managed",
            "attestation": "A",
            "note": "Telnyx automatically handles STIR/SHAKEN attestation for verified business numbers. Full (A) attestation means carriers trust your calls.",
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
            "description": "CNAM (Caller Name) displays your business name on recipient phones. Enable per-number in the Numbers tab.",
            "telnyx_portal": "https://portal.telnyx.com/#/app/numbers",
        },
    }

    try:
        resp = http_requests.get(
            f"{TELNYX_API_BASE}/phone_numbers",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"page[size]": 250},
            timeout=15,
        )
        if resp.status_code == 200:
            for n in resp.json().get('data', []):
                result["numbers"].append({
                    "phone": n.get('phone_number', ''),
                    "id": n.get('id', ''),
                    "status": n.get('status', 'active'),
                    "cnam_listed": n.get('cnam_listing_enabled', False),
                    "emergency_enabled": n.get('emergency_enabled', False),
                    "connection_id": n.get('connection_id', ''),
                })
        else:
            logger.warning(f"trust-hub: Telnyx phone_numbers returned {resp.status_code}")

        return jsonify(result)

    except Exception as e:
        logger.error(f"Trust hub check failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/<number_id>/cnam', methods=['POST'])
@login_required
def toggle_cnam(number_id):
    """Toggle CNAM listing for a specific Telnyx phone number."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured"}), 400

    data = request.json or {}
    enable = data.get('enable', True)

    try:
        resp = http_requests.patch(
            f"{TELNYX_API_BASE}/phone_numbers/{number_id}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"cnam_listing_enabled": enable},
            timeout=10,
        )
        if resp.status_code != 200:
            return jsonify({"error": f"Failed to update CNAM: {resp.text[:300]}"}), 400

        return jsonify({"status": "ok", "cnam_listed": enable})
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

    vc['telnyx_phone_number'] = phone
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
    2. Creates Telnyx Business Identity via API
    3. Enables CNAM on ALL phone numbers with business name
    4. Stores profile so future number purchases auto-get CNAM
    """
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured. Go to Settings and click Connect Telnyx."}), 400

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

    results = {"steps": [], "errors": []}

    # ── Step 1: Save business profile to voice_config ──
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
        'auto_cnam': True,  # Flag: auto-enable CNAM on future numbers
    })
    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)
    results["steps"].append({"name": "save_profile", "status": "ok"})

    # ── Step 2: Create Telnyx Business Identity ──
    # CNAM display name is max 15 chars
    cnam_name = business_name[:15].strip()
    biz_identity_id = trust_hub.get('telnyx_business_identity_id')

    try:
        biz_payload = {
            "business_name": business_name,
            "address": {
                "street": street or "N/A",
                "city": city or "N/A",
                "state": state or "N/A",
                "postal_code": zip_code or "00000",
                "country": "US",
            },
        }
        if contact_name:
            first, *last_parts = contact_name.split(' ', 1)
            last = last_parts[0] if last_parts else ''
            biz_payload["contacts"] = [{
                "first_name": first,
                "last_name": last,
                "email": contact_email or current_user.email,
                "phone_number": contact_phone or '',
            }]

        if biz_identity_id:
            # Update existing
            resp = http_requests.patch(
                f"{TELNYX_API_BASE}/business_identities/{biz_identity_id}",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=biz_payload,
                timeout=15,
            )
        else:
            # Create new
            resp = http_requests.post(
                f"{TELNYX_API_BASE}/business_identities",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=biz_payload,
                timeout=15,
            )

        if resp.status_code in (200, 201):
            biz_data = resp.json().get('data', {})
            new_id = biz_data.get('id', '')
            if new_id:
                trust_hub['telnyx_business_identity_id'] = new_id
                vc['trust_hub'] = trust_hub
                _save_voice_config(current_user.email, vc)
            results["steps"].append({"name": "business_identity", "status": "ok", "id": new_id})
        else:
            # Non-fatal: CNAM can still work without business identity
            err_text = resp.text[:300]
            logger.warning(f"Business identity creation returned {resp.status_code}: {err_text}")
            results["steps"].append({"name": "business_identity", "status": "skipped", "reason": f"Telnyx returned {resp.status_code}"})
    except Exception as e:
        logger.error(f"Business identity creation failed: {e}")
        results["steps"].append({"name": "business_identity", "status": "skipped", "reason": str(e)})

    # ── Step 3: Enable CNAM on ALL phone numbers ──
    cnam_success = 0
    cnam_fail = 0
    try:
        # Fetch all numbers
        nums_resp = http_requests.get(
            f"{TELNYX_API_BASE}/phone_numbers",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"page[size]": 250},
            timeout=15,
        )
        if nums_resp.status_code == 200:
            numbers = nums_resp.json().get('data', [])
            for num in numbers:
                num_id = num.get('id', '')
                already_enabled = num.get('cnam_listing_enabled', False)
                if not num_id:
                    continue
                if already_enabled:
                    cnam_success += 1
                    continue
                try:
                    patch_resp = http_requests.patch(
                        f"{TELNYX_API_BASE}/phone_numbers/{num_id}",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"cnam_listing_enabled": True},
                        timeout=10,
                    )
                    if patch_resp.status_code == 200:
                        cnam_success += 1
                    else:
                        cnam_fail += 1
                        logger.warning(f"CNAM enable failed for {num_id}: {patch_resp.status_code}")
                except Exception as e:
                    cnam_fail += 1
                    logger.warning(f"CNAM enable error for {num_id}: {e}")

            results["steps"].append({
                "name": "cnam_all_numbers",
                "status": "ok",
                "enabled": cnam_success,
                "failed": cnam_fail,
                "total": len(numbers),
            })
        else:
            results["steps"].append({"name": "cnam_all_numbers", "status": "error", "reason": f"Could not fetch numbers: {nums_resp.status_code}"})
    except Exception as e:
        logger.error(f"CNAM bulk enable failed: {e}")
        results["steps"].append({"name": "cnam_all_numbers", "status": "error", "reason": str(e)})

    # ── Step 4: Mark auto-protection enabled ──
    trust_hub['protection_active'] = True
    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)
    results["steps"].append({"name": "auto_protect", "status": "ok"})

    has_errors = any(s.get('status') == 'error' for s in results["steps"])
    return jsonify({
        "status": "partial" if has_errors else "ok",
        "results": results,
        "cnam_name": cnam_name,
        "numbers_protected": cnam_success,
        "numbers_failed": cnam_fail,
    })


@voice_bp.route('/voice/spam-protection/status', methods=['GET'])
@login_required
def spam_protection_status():
    """Get current spam protection registration status."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not api_key:
        return jsonify({"error": "Telnyx API key not configured"}), 400

    trust_hub = (vc or {}).get('trust_hub', {})
    protection_active = trust_hub.get('protection_active', False)
    business_name = trust_hub.get('business_name', '')

    # Count protected numbers (CNAM enabled)
    protected = 0
    total = 0
    numbers_detail = []
    try:
        resp = http_requests.get(
            f"{TELNYX_API_BASE}/phone_numbers",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"page[size]": 250},
            timeout=15,
        )
        if resp.status_code == 200:
            for n in resp.json().get('data', []):
                total += 1
                cnam = n.get('cnam_listing_enabled', False)
                if cnam:
                    protected += 1
                numbers_detail.append({
                    "phone": n.get('phone_number', ''),
                    "id": n.get('id', ''),
                    "cnam_enabled": cnam,
                    "status": n.get('status', 'active'),
                })
    except Exception as e:
        logger.warning(f"Spam protection status check failed: {e}")

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
        "numbers_protected": protected,
        "numbers_total": total,
        "numbers": numbers_detail,
        "stir_shaken": "active",  # Telnyx auto-manages
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
