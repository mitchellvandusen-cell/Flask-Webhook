# voice_bridge.py - Twilio <-> XAI Grok Voice Agent Bridge
# Real-time bidirectional audio streaming via WebSocket
# Architecture: Lead <-> Twilio Media Streams <-> This Bridge <-> XAI Realtime API
#
# Audio format: audio/pcmu (G.711 μ-law) — native to both Twilio and XAI, zero transcoding
# XAI endpoint: wss://api.x.ai/v1/realtime (OpenAI Realtime API compatible)

import json
import os
import logging
import threading
import time
import asyncio
import struct
import base64
import websockets
import requests as http_requests
from flask import Blueprint, request, Response, jsonify, render_template
from flask_login import login_required, current_user
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream, Dial
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

from db import get_db_connection, return_db_connection, log_webhook_event
from ghl_api import get_valid_token, fetch_targeted_ghl_history

# In-memory call status tracking for the dialer queue
# { call_sid: { "status": "...", "duration": 0, "contact_id": "...", "phone": "...", "name": "..." } }
_active_calls = {}
from ghl_calendar import consolidated_calendar_op
from memory import get_recent_messages, get_known_facts, get_narrative
from sales_director import generate_strategic_directive
from prompt import build_system_prompt

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


async def _generate_voice_preview(voice_name):
    """Connect to XAI Realtime API and generate a short voice sample."""
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
                    "output_modalities": ["audio"],
                    "audio": {
                        "output": {
                            "format": {"type": "audio/pcmu"},
                            "voice": voice_name,
                        }
                    },
                    "instructions": "You are a friendly voice assistant. Say exactly what is requested, nothing more.",
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
                    logger.error(f"Voice preview error: {data}")
                    break

    except Exception as e:
        logger.error(f"Voice preview generation failed: {e}")
        return None

    return b''.join(audio_chunks) if audio_chunks else None


# ──────────────────────────────────────────────────────────────
# HELPER: Look up subscriber by their Twilio phone number
# ──────────────────────────────────────────────────────────────

def _get_subscriber_by_twilio_number(phone_number):
    """Look up subscriber whose voice_config contains this Twilio number."""
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
        logger.error(f"Error looking up subscriber by Twilio number: {e}")
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
    Build a system prompt optimized for voice conversations.
    Uses the same sales_director logic but adapts output for spoken language.
    """
    bot_name = subscriber.get("bot_first_name", "Alex")
    timezone = subscriber.get("timezone", "America/Chicago")
    bot_settings = subscriber.get("bot_settings") or {}
    voice_config = subscriber.get("voice_config") or {}

    # Custom voice instructions from dashboard
    custom_voice_instructions = voice_config.get("voice_instructions", "")

    # If we have a contact_id, load full sales director context
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

    # Try to get calendar slots if in booking stage
    if stage in ("BOOKING", "QUALIFYING"):
        try:
            calendar_slots = consolidated_calendar_op(
                operation="fetch_slots",
                subscriber_data=subscriber,
            )
        except Exception as e:
            logger.warning(f"Voice: Could not fetch calendar slots: {e}")

    # Fetch GHL contact custom fields (beneficiary, etc.) for the AI to reference
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
                        contact_fields_str = "CONTACT CUSTOM FIELDS (from CRM — use these in conversation):\n" + "\n".join(field_lines)
        except Exception as e:
            logger.debug(f"Voice: Could not fetch contact custom fields: {e}")

    # Build the base prompt using the existing system
    base_prompt = build_system_prompt(
        bot_first_name=bot_name,
        timezone=timezone,
        profile_str=profile_str,
        tactical_narrative=tactical_narrative,
        known_facts=known_facts,
        story_narrative=story_narrative,
        stage=stage,
        recent_exchanges=recent_exchanges,
        message="",
        calendar_slots=calendar_slots or "",
        personal_website=subscriber.get("personal_website", ""),
        contracted_carriers=subscriber.get("contracted_carriers"),
        bot_settings=bot_settings,
    )

    # Voice-specific overlay — engineered for maximum naturalness
    voice_overlay = f"""

=== VOICE CONVERSATION MODE ===
You are on a LIVE PHONE CALL with a real person. You ARE a real person. Everything below overrides any text-chat habits.

## How You Sound — Be Human, Not a Bot
- ALWAYS use contractions: "I'm", "you're", "that's", "we'll", "wouldn't", "can't", "shouldn't", "it's". NEVER use "I am", "you are", "that is", "we will" — nobody talks like that on the phone.
- Use natural filler words sparingly: "so", "well", "actually", "honestly", "you know what", "I mean", "let me think...", "hmm". These make you sound real.
- Vary your acknowledgments EVERY turn — rotate through: "Got it", "Oh yeah", "Makes sense", "Sure thing", "Right right", "Ah okay", "Hmm", "Oh interesting", "Yeah totally", "Fair enough". NEVER use the same one twice in a row.
- Use casual discourse markers to transition: "so here's the thing", "basically", "anyway", "now the cool part is", "so what that means for you is", "here's what I'd suggest"
- Occasionally use slight hesitations for realism: "I... yeah I think that'd work" or "That's... actually a really good question"
- Your rhythm should feel like a real conversation — mix short punchy responses ("Oh totally.") with slightly longer ones when explaining something important.

## Brevity — THE Most Important Rule
- KEEP EVERY RESPONSE TO 1-3 SENTENCES MAXIMUM. People cannot scroll back on a phone call.
- Ask ONE question at a time, then stop and let them answer. Never stack questions.
- If you have multiple points, spread them across turns. One idea per response.
- When you catch yourself about to give a long explanation, stop and say something like "want me to break that down?" or just give the headline version.

## Active Listening & Mirroring
- Mirror the caller's energy: if they're upbeat, be upbeat. If they're chill, be chill. If they're worried, slow down and be reassuring.
- Reference what they just said before moving forward: "So you mentioned [X]..." or "Okay so the family plan..." — this shows you're actually listening.
- If they seem confused, rephrase in simpler words — don't just repeat yourself louder.
- If they're frustrated, acknowledge it once genuinely ("Yeah I totally get that, that's frustrating"), then move straight to solving it.
- Use {contact_name}'s name occasionally to build rapport — but not every single sentence.

## Things You Must NEVER Do
- NEVER use bullet points, numbered lists, emojis, asterisks, markdown, or any text formatting. This is a spoken phone call.
- NEVER start with "Certainly!", "Absolutely!", "Of course!" on every turn — vary it.
- NEVER say "Great question!" or "That's a great question" more than once per call.
- NEVER say "Is there anything else I can help you with?" robotically.
- NEVER give a monologue. If you're talking for more than 15 seconds, you've gone too long.
- NEVER read information like a list — weave it into natural conversation.
- NEVER say you're an AI, a virtual assistant, a language model, or a bot.
- NEVER use phrases like "as an AI" or "I don't have feelings" or "I'm just a program."
- NEVER use corporate jargon or overly formal language unless the caller does first.
- NEVER repeat the exact same phrase or sentence structure twice in the same call.

## Phone-Specific Rules
- Messages you receive may contain transcription errors. Interpret what the caller MEANT, don't respond to garbled literal text.
- Every word you output will be synthesized into audio. Write ONLY what should be spoken aloud.
- Spell out numbers naturally: "about two hundred and fifty thousand" not "$250,000", "two thirty in the afternoon" not "2:30 PM".
- For phone numbers, group naturally: "five five five, eight six seven, five three oh nine"
- For emails: "john at company dot com"
- Say "okay" not "OK", "appointment" not "appt"
- Handle interruptions gracefully — if the caller cuts you off, immediately stop your thought and address what they said. Do NOT try to finish your previous point.
- If there's an awkward pause, fill it naturally: "You still there?" or "Sorry, go ahead" — not robotic silence.

## Emotional Intelligence
- If they sound rushed: be concise, skip small talk, get to the point fast.
- If they sound nervous about cost: be reassuring, emphasize value and affordability.
- If they're chatty: engage warmly but gently steer back to purpose.
- If they sound skeptical: don't oversell, be straightforward and honest.
- If they mention a family member or personal situation: acknowledge it naturally ("Oh congrats on the new baby!" or "Yeah that makes total sense to want coverage with the new house").

{f"ADDITIONAL CUSTOM INSTRUCTIONS FROM AGENT: {custom_voice_instructions}" if custom_voice_instructions else ""}

{contact_fields_str}

## Final Reminder
YOU ARE ON A REAL PHONE CALL. Sound like a real person having a real conversation. Be warm, be brief, be human. Every word is spoken aloud.
"""

    return base_prompt + voice_overlay


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

    else:
        logger.warning(f"Unknown voice tool: {tool_name}")
        return f"Unknown tool: {tool_name}"


# ──────────────────────────────────────────────────────────────
# ROUTE: Twilio calls this when an inbound call arrives
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/inbound', methods=['POST'])
def voice_inbound():
    """
    Handle inbound call. Twilio POSTs here when someone calls the AI number.
    Returns TwiML that opens a Media Stream WebSocket to our bridge.
    """
    caller = request.form.get('From', 'Unknown')
    called = request.form.get('To', 'Unknown')
    call_sid = request.form.get('CallSid', '')

    logger.info(f"📞 Inbound voice call: {caller} -> {called} (SID: {call_sid})")

    # Look up subscriber by the called number (their Twilio number)
    subscriber = _get_subscriber_by_twilio_number(called)
    if not subscriber:
        logger.warning(f"No subscriber found for Twilio number: {called}")
        response = VoiceResponse()
        response.say("Sorry, this number is not currently configured. Please try again later.", voice="Polly.Matthew")
        response.hangup()
        return Response(str(response), mimetype='application/xml')

    voice_config = subscriber.get("voice_config") or {}
    bot_name = subscriber.get("bot_first_name", "your advisor")
    greeting = voice_config.get("greeting", f"Hi, this is {bot_name}. How can I help you today?")

    # Build TwiML: connect to our WebSocket stream
    response = VoiceResponse()
    host = request.host

    connect = Connect()
    stream = Stream(url=f'wss://{host}/voice/stream')
    stream.parameter(name='callSid', value=call_sid)
    stream.parameter(name='caller', value=caller)
    stream.parameter(name='called', value=called)
    stream.parameter(name='direction', value='inbound')
    stream.parameter(name='locationId', value=subscriber.get('location_id', ''))
    connect.append(stream)
    response.append(connect)

    logger.info(f"📞 Returning TwiML with stream URL: wss://{host}/voice/stream")
    return Response(str(response), mimetype='application/xml')


@voice_bp.route('/voice/outbound-answer', methods=['POST'])
def voice_outbound_answer():
    """
    Twilio calls this when the outbound call is answered.
    Returns TwiML that opens a Media Stream to our bridge.
    """
    call_sid = request.form.get('CallSid', '')
    caller = request.form.get('From', '')
    called = request.form.get('To', '')

    # Get custom parameters from the URL
    location_id = request.args.get('location_id', '')
    contact_id = request.args.get('contact_id', '')
    contact_name = request.args.get('name', 'there')

    logger.info(f"📞 Outbound call answered: {caller} -> {called} (SID: {call_sid})")

    host = request.host
    response = VoiceResponse()

    connect = Connect()
    stream = Stream(url=f'wss://{host}/voice/stream')
    stream.parameter(name='callSid', value=call_sid)
    stream.parameter(name='caller', value=caller)
    stream.parameter(name='called', value=called)
    stream.parameter(name='direction', value='outbound')
    stream.parameter(name='locationId', value=location_id)
    stream.parameter(name='contactId', value=contact_id)
    stream.parameter(name='contactName', value=contact_name)
    connect.append(stream)
    response.append(connect)

    return Response(str(response), mimetype='application/xml')


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

    twilio_sid = voice_config.get("twilio_account_sid", "")
    twilio_token = voice_config.get("twilio_auth_token", "")
    twilio_number = voice_config.get("twilio_phone_number", "")

    if not all([twilio_sid, twilio_token, twilio_number]):
        return jsonify({"error": "Twilio credentials not configured"}), 400

    try:
        client = TwilioClient(twilio_sid, twilio_token)

        # Build the URL Twilio will call when the lead answers
        host = request.host
        answer_url = (
            f"https://{host}/voice/outbound-answer"
            f"?location_id={location_id}"
            f"&contact_id={contact_id}"
            f"&name={lead_name}"
        )

        call = client.calls.create(
            to=lead_phone,
            from_=twilio_number,
            url=answer_url,
            method="POST",
            record=True,
            recording_channels="dual",
            recording_status_callback=f"https://{host}/voice/recording-status",
            recording_status_callback_method="POST",
            status_callback=f"https://{host}/voice/status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )

        logger.info(f"📞 Outbound call initiated: {twilio_number} -> {lead_phone} (SID: {call.sid})")

        # Persist to call_history DB
        save_call_to_history(
            location_id=location_id,
            call_sid=call.sid,
            phone=lead_phone,
            contact_id=contact_id,
            contact_name=lead_name,
            direction='outbound',
            status='initiated'
        )

        # Log the event
        try:
            log_webhook_event(
                location_id=location_id,
                contact_id=contact_id,
                event_type="voice_outbound_initiated",
                status="success",
                summary=f"Outbound call to {lead_name} ({lead_phone})",
                details={"call_sid": call.sid, "to": lead_phone, "from": twilio_number}
            )
        except Exception:
            pass

        return jsonify({"status": "calling", "call_sid": call.sid})

    except Exception as e:
        logger.error(f"Failed to initiate outbound call: {e}")
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# ROUTE: Call status webhook
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/status', methods=['POST'])
def voice_status():
    """Twilio posts call status updates here."""
    call_sid = request.form.get('CallSid', '')
    call_status = request.form.get('CallStatus', '')
    duration = request.form.get('CallDuration', '0')

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
    Core WebSocket handler: bridges Twilio Media Streams <-> XAI Realtime API.
    Called by flask-sock for each new Twilio stream connection.

    Audio flow:
        Lead speaks -> Twilio (μ-law) -> This bridge -> XAI (μ-law) -> processes
        XAI responds (μ-law) -> This bridge -> Twilio (μ-law) -> Lead hears

    ws: the Twilio-side WebSocket (flask-sock)
    """
    logger.info("🎙️ Voice stream WebSocket connected")

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
        # Some Twilio versions send a 'connected' event first
        start_msg = ws.receive()
        start_data = json.loads(start_msg)

    if start_data.get('event') == 'start':
        stream_sid = start_data['start']['streamSid']
        custom_params = start_data['start'].get('customParameters', {})
        call_sid = custom_params.get('callSid', '')
        caller = custom_params.get('caller', '')
        called = custom_params.get('called', '')
        direction = custom_params.get('direction', 'inbound')
        location_id = custom_params.get('locationId', '')
        contact_id = custom_params.get('contactId', '')
        contact_name = custom_params.get('contactName', 'there')
        logger.info(f"🎙️ Stream started: SID={stream_sid} dir={direction} loc={location_id}")
    else:
        logger.warning(f"Voice stream: Unexpected first event: {start_data.get('event')}")
        return

    # Look up subscriber
    subscriber = None
    if location_id:
        subscriber = _get_subscriber_by_location(location_id)
    if not subscriber and called:
        subscriber = _get_subscriber_by_twilio_number(called)
    if not subscriber and caller:
        subscriber = _get_subscriber_by_twilio_number(caller)

    if not subscriber:
        logger.error(f"Voice stream: No subscriber found (loc={location_id}, called={called})")
        ws.send(json.dumps({"event": "clear", "streamSid": stream_sid}))
        return

    voice_config = subscriber.get("voice_config") or {}
    voice_name = VOICE_OPTIONS.get(
        voice_config.get("voice", "ara").lower(),
        DEFAULT_VOICE
    )

    # Build MINIMAL prompt for instant connect (full context loaded async below)
    bot_name = subscriber.get("bot_first_name", "your advisor")
    bot_settings = subscriber.get("bot_settings") or {}
    custom_voice_instructions = voice_config.get("voice_instructions", "")
    minimal_prompt = f"""You are {bot_name}, a real person on a live phone call — a warm, sharp life insurance advisor who genuinely cares about helping people protect their families.

## How You Sound
- You talk like a real human. Use contractions always — "I'm", "you're", "that's", "we'll", "wouldn't", "can't". NEVER say "I am", "you are", "that is".
- Sprinkle in natural filler words sparingly: "so", "well", "actually", "honestly", "you know what", "I mean", "let me think..."
- Start responses with varied, natural acknowledgments — "Got it", "Oh yeah", "Makes sense", "Sure thing", "Right right", "Ah okay", "Hmm". NEVER repeat the same opener twice in a row.
- Use casual transitions: "so here's the thing", "basically", "anyway", "now the cool part is"
- Your pace is relaxed and conversational — like you're chatting with a friend over coffee, not reading a script.

## Response Rules
- KEEP EVERY RESPONSE TO 1-3 SENTENCES MAX. This is a phone call — short and punchy.
- Ask ONE question at a time, then shut up and listen.
- NEVER use bullet points, numbered lists, emojis, or any text formatting. This is spoken audio.
- Spell out numbers naturally: say "two thirty" not "2:30", "about four hundred bucks" not "$400".
- NEVER say you're an AI, a virtual assistant, or a bot. You are {bot_name}, a real advisor.

{f"CUSTOM INSTRUCTIONS: {custom_voice_instructions}" if custom_voice_instructions else ""}
Every single word you output will be spoken aloud through a voice engine on a real phone call. Sound like a human, not a machine."""

    # Build greeting for outbound calls — keep it casual and human
    greeting = voice_config.get("greeting", "")
    if not greeting:
        if direction == "outbound" and contact_name != "there":
            greeting = f"Hey {contact_name}! It's {bot_name}. I'm just giving you a quick call about that life insurance info you were looking into. You got a sec?"
        else:
            greeting = f"Hey there, thanks for calling in! This is {bot_name}. What's going on, how can I help?"

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

            # Configure the XAI session with MINIMAL prompt for fast greeting
            session_config = {
                "type": "session.update",
                "session": {
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcmu"},
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.5,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 500,
                            }
                        },
                        "output": {
                            "format": {"type": "audio/pcmu"},
                            "voice": voice_name,
                        }
                    },
                    "input_audio_transcription": {
                        "model": "whisper-1"
                    },
                    "instructions": minimal_prompt,
                    "tools": get_voice_tools(),
                }
            }
            await xai_ws.send(json.dumps(session_config))
            logger.info("🎙️ XAI session configured (minimal prompt, greeting sent)")

            # For outbound calls, have the AI speak first with the greeting
            if direction == "outbound" or greeting:
                initial_item = {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"[System: The call just connected. Say hi naturally — use this as a guide but make it your own, keep it casual and warm: '{greeting}']"
                            }
                        ]
                    }
                }
                await xai_ws.send(json.dumps(initial_item))
                await xai_ws.send(json.dumps({"type": "response.create"}))

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
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp = None
            call_active = True

            # ── Twilio -> XAI: Forward audio from the phone to XAI ──
            async def receive_from_twilio():
                nonlocal stream_sid, latest_media_timestamp, call_active
                try:
                    while call_active:
                        message = await asyncio.get_event_loop().run_in_executor(
                            None, ws.receive
                        )
                        if message is None:
                            logger.info("🎙️ Twilio stream ended (None received)")
                            call_active = False
                            break

                        data = json.loads(message)

                        if data['event'] == 'media':
                            latest_media_timestamp = int(data['media']['timestamp'])
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data['media']['payload']
                            }
                            await xai_ws.send(json.dumps(audio_append))

                        elif data['event'] == 'mark':
                            if mark_queue:
                                mark_queue.pop(0)

                        elif data['event'] == 'stop':
                            logger.info(f"🎙️ Twilio stream stopped: {stream_sid}")
                            call_active = False
                            break

                except Exception as e:
                    logger.info(f"🎙️ Twilio receive ended: {e}")
                    call_active = False

            # ── XAI -> Twilio: Forward audio from XAI to the phone ──
            async def receive_from_xai():
                nonlocal last_assistant_item, response_start_timestamp, call_active
                try:
                    async for xai_message in xai_ws:
                        if not call_active:
                            break

                        response = json.loads(xai_message)
                        event_type = response.get('type', '')

                        if event_type in LOG_EVENT_TYPES:
                            logger.info(f"🎙️ XAI event: {event_type}")

                        # Audio response from XAI -> send to Twilio
                        if event_type == 'response.audio.delta' and 'delta' in response:
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": response['delta']
                                }
                            }
                            ws.send(json.dumps(audio_delta))

                            # Track response timing for interruption handling
                            item_id = response.get("item_id")
                            if item_id and item_id != last_assistant_item:
                                response_start_timestamp = latest_media_timestamp
                                last_assistant_item = item_id

                            # Send mark for timing
                            if stream_sid:
                                mark_event = {
                                    "event": "mark",
                                    "streamSid": stream_sid,
                                    "mark": {"name": "responsePart"}
                                }
                                ws.send(json.dumps(mark_event))
                                mark_queue.append('responsePart')

                        # Also handle the newer event name variant
                        elif event_type == 'response.output_audio.delta' and 'delta' in response:
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": response['delta']
                                }
                            }
                            ws.send(json.dumps(audio_delta))

                            item_id = response.get("item_id")
                            if item_id and item_id != last_assistant_item:
                                response_start_timestamp = latest_media_timestamp
                                last_assistant_item = item_id

                            if stream_sid:
                                mark_event = {
                                    "event": "mark",
                                    "streamSid": stream_sid,
                                    "mark": {"name": "responsePart"}
                                }
                                ws.send(json.dumps(mark_event))
                                mark_queue.append('responsePart')

                        # Speech interruption: user started talking while AI was speaking
                        elif event_type == 'input_audio_buffer.speech_started':
                            logger.info("🎙️ Speech interruption detected")
                            if mark_queue and response_start_timestamp is not None and last_assistant_item:
                                elapsed = latest_media_timestamp - response_start_timestamp
                                # Truncate the AI's response
                                truncate_event = {
                                    "type": "conversation.item.truncate",
                                    "item_id": last_assistant_item,
                                    "content_index": 0,
                                    "audio_end_ms": elapsed
                                }
                                await xai_ws.send(json.dumps(truncate_event))

                            # Clear Twilio's audio buffer
                            ws.send(json.dumps({
                                "event": "clear",
                                "streamSid": stream_sid
                            }))
                            mark_queue.clear()
                            last_assistant_item = None
                            response_start_timestamp = None

                        # Tool / function calls
                        elif event_type == 'response.function_call_arguments.done':
                            tool_name = response.get('name', '')
                            tool_args = response.get('arguments', '{}')
                            call_id = response.get('call_id', '')

                            logger.info(f"🔧 Voice tool call: {tool_name} (call_id={call_id})")

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
                                    "call_id": call_id,
                                    "output": result
                                }
                            }
                            await xai_ws.send(json.dumps(tool_output))

                            # Trigger XAI to generate a response using the tool result
                            await xai_ws.send(json.dumps({"type": "response.create"}))

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

                except websockets.exceptions.ConnectionClosed:
                    logger.info("🎙️ XAI WebSocket closed")
                    call_active = False
                except Exception as e:
                    logger.error(f"🎙️ XAI receive error: {e}")
                    call_active = False

            # Run audio bridge + background enrichment concurrently
            await asyncio.gather(
                receive_from_twilio(),
                receive_from_xai(),
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
# ROUTE: Configure a Twilio number to point to our voice endpoints
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/configure-number', methods=['POST'])
def configure_twilio_number():
    """
    One-time setup: configure the user's Twilio number to route calls here.
    Called from the dashboard when they save their voice config.
    """
    data = request.json or {}
    location_id = data.get('location_id', '')
    twilio_sid = data.get('twilio_account_sid', '')
    twilio_token = data.get('twilio_auth_token', '')
    twilio_number = data.get('twilio_phone_number', '')

    if not all([location_id, twilio_sid, twilio_token, twilio_number]):
        return jsonify({"error": "All fields required"}), 400

    try:
        client = TwilioClient(twilio_sid, twilio_token)

        # Find the phone number SID
        numbers = client.incoming_phone_numbers.list(phone_number=twilio_number)
        if not numbers:
            return jsonify({"error": f"Phone number {twilio_number} not found in this Twilio account"}), 404

        phone_sid = numbers[0].sid
        host = request.host

        # Update the number to point to our voice endpoints
        numbers[0].update(
            voice_url=f"https://{host}/voice/inbound",
            voice_method="POST",
            status_callback=f"https://{host}/voice/status",
            status_callback_method="POST",
        )

        logger.info(f"✅ Configured Twilio number {twilio_number} (SID={phone_sid}) for voice")

        return jsonify({
            "status": "configured",
            "phone_sid": phone_sid,
            "voice_url": f"https://{host}/voice/inbound"
        })

    except Exception as e:
        logger.error(f"Failed to configure Twilio number: {e}")
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# ROUTE: Test voice connection (health check)
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/test', methods=['POST'])
def test_voice_connection():
    """Test that XAI and Twilio credentials are valid."""
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

    # Test Twilio credentials
    if location_id:
        subscriber = _get_subscriber_by_location(location_id)
        if subscriber:
            voice_config = subscriber.get("voice_config") or {}
            twilio_sid = voice_config.get("twilio_account_sid", "")
            twilio_token = voice_config.get("twilio_auth_token", "")
            if twilio_sid and twilio_token:
                try:
                    client = TwilioClient(twilio_sid, twilio_token)
                    account = client.api.accounts(twilio_sid).fetch()
                    results["twilio"] = account.status == "active"
                    if account.status != "active":
                        results["errors"].append(f"Twilio account status: {account.status}")
                except Exception as e:
                    results["errors"].append(f"Twilio validation failed: {str(e)}")
            else:
                results["errors"].append("Twilio credentials not configured")

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

    wav_data = _mulaw_to_wav(audio_data)
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
            # POST /contacts/search with pagination
            page = 1
            while page <= max_pages:
                search_body = {
                    "locationId": location_id,
                    "pageSize": page_limit,
                    "page": page,
                    "filters": []
                }
                if pipeline_id:
                    search_body["filters"].append({"field": "pipeline", "operator": "eq", "value": pipeline_id})
                if stage_id:
                    search_body["filters"].append({"field": "pipelineStage", "operator": "eq", "value": stage_id})
                if query:
                    search_body["query"] = query

                resp = http_requests.post(f"{GHL_API_BASE}/contacts/search", headers=headers, json=search_body, timeout=15)
                if resp.status_code != 200:
                    if not all_contacts:
                        return jsonify({"error": f"CRM returned {resp.status_code}"}), resp.status_code
                    break

                data = resp.json()
                contacts = data.get("contacts", [])
                if not contacts:
                    break
                all_contacts.extend(contacts)
                # Stop if we got fewer than page_limit (last page)
                if len(contacts) < page_limit:
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
    contact_id = data.get('contact_id', '')
    phone = data.get('phone', '')
    first_name = data.get('first_name', 'there')

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

    location_id = subscriber.get('location_id', '')
    voice_config = subscriber.get('voice_config') or {}

    if not voice_config.get('enabled'):
        return jsonify({"error": "Voice AI is not enabled. Enable it in the Voice tab."}), 400

    twilio_sid = voice_config.get('twilio_account_sid', '')
    twilio_token = voice_config.get('twilio_auth_token', '')
    twilio_number = voice_config.get('twilio_phone_number', '')

    if not all([twilio_sid, twilio_token, twilio_number]):
        return jsonify({"error": "Twilio credentials not configured"}), 400

    try:
        client = TwilioClient(twilio_sid, twilio_token)
        host = request.host
        answer_url = (
            f"https://{host}/voice/outbound-answer"
            f"?location_id={location_id}"
            f"&contact_id={contact_id}"
            f"&name={first_name}"
        )

        call = client.calls.create(
            to=phone,
            from_=twilio_number,
            url=answer_url,
            method="POST",
            timeout=30,
            record=True,
            recording_channels="dual",
            recording_status_callback=f"https://{host}/voice/recording-status",
            recording_status_callback_method="POST",
            status_callback=f"https://{host}/voice/status",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )

        # Track this call for the dialer queue
        _active_calls[call.sid] = {
            "status": "initiated",
            "duration": 0,
            "contact_id": contact_id,
            "phone": phone,
            "name": first_name
        }

        # Persist to call_history DB
        save_call_to_history(
            location_id=location_id,
            call_sid=call.sid,
            phone=phone,
            contact_id=contact_id,
            contact_name=first_name,
            direction='outbound',
            status='initiated'
        )

        logger.info(f"📞 Dialer call: {twilio_number} -> {phone} ({first_name}) SID={call.sid}")
        return jsonify({"status": "calling", "call_sid": call.sid})

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
# ROUTE: Recording status callback from Twilio
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/recording-status', methods=['POST'])
def recording_status_callback():
    """Twilio posts here when a call recording is ready."""
    call_sid = request.form.get('CallSid', '')
    recording_sid = request.form.get('RecordingSid', '')
    recording_url = request.form.get('RecordingUrl', '')
    recording_status = request.form.get('RecordingStatus', '')
    recording_duration = request.form.get('RecordingDuration', '0')

    logger.info(f"🎙️ Recording callback: SID={call_sid} rec={recording_sid} status={recording_status} dur={recording_duration}s")

    if recording_status == 'completed' and recording_url:
        # Use our proxy URL so browser can play without Twilio auth
        proxy_url = f"/voice/recording/{recording_sid}"

        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE call_history
                    SET recording_url = %s, recording_sid = %s, duration = %s
                    WHERE call_sid = %s
                """, (proxy_url, recording_sid, int(recording_duration or 0), call_sid))
                conn.commit()
                cur.close()
                logger.info(f"🎙️ Recording saved for call {call_sid}: {proxy_url}")
            except Exception as e:
                logger.error(f"Failed to save recording: {e}")
                conn.rollback()
            finally:
                return_db_connection(conn)

    return '', 204


@voice_bp.route('/voice/recording/<recording_sid>', methods=['GET'])
@login_required
def stream_recording(recording_sid):
    """
    Proxy a Twilio recording so the browser can play it without Twilio auth.
    Streams the MP3 directly through our server.
    """
    subscriber, vc, client = _get_current_subscriber_voice()
    if not client:
        return jsonify({"error": "Twilio credentials not configured"}), 400

    account_sid = vc.get('twilio_account_sid', '')
    auth_token = vc.get('twilio_auth_token', '')

    twilio_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording_sid}.mp3"

    try:
        resp = http_requests.get(twilio_url, auth=(account_sid, auth_token), stream=True, timeout=30)
        if resp.status_code != 200:
            return jsonify({"error": f"Twilio returned {resp.status_code}"}), resp.status_code

        return Response(
            resp.iter_content(chunk_size=8192),
            content_type='audio/mpeg',
            headers={'Content-Disposition': f'inline; filename="{recording_sid}.mp3"'}
        )
    except Exception as e:
        logger.error(f"Recording proxy failed: {e}")
        return jsonify({"error": str(e)}), 500


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

        cur.execute("""
            SELECT id, contact_id, contact_name, phone, direction, call_sid,
                   status, duration, recording_url, recording_sid, transcript,
                   started_at, ended_at, created_at
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
    """Get subscriber, voice_config, and Twilio client for the logged-in user."""
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
        sid = vc.get('twilio_account_sid', '')
        tok = vc.get('twilio_auth_token', '')
        if not sid or not tok:
            return subscriber, vc, None
        client = TwilioClient(sid, tok)
        return subscriber, vc, client
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
# BROWSER VoIP: Twilio Client JS SDK support
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/setup-voip', methods=['POST'])
@login_required
def setup_voip():
    """
    One-time setup: create a Twilio API Key and TwiML App for browser-based VoIP.
    Stores api_key_sid, api_key_secret, and twiml_app_sid in voice_config.
    """
    subscriber, vc, client = _get_current_subscriber_voice()
    if not client:
        return jsonify({"error": "Twilio credentials not configured"}), 400

    host = request.host
    try:
        # Create TwiML App if not exists
        twiml_app_sid = vc.get('twiml_app_sid', '')
        if not twiml_app_sid:
            app = client.applications.create(
                friendly_name='InsuranceGrokBot Dialer',
                voice_url=f'https://{host}/voice/voip-answer',
                voice_method='POST',
                status_callback=f'https://{host}/voice/status',
                status_callback_method='POST',
            )
            twiml_app_sid = app.sid
            logger.info(f"Created TwiML App: {twiml_app_sid}")
        else:
            # Update existing app URL in case host changed
            try:
                client.applications(twiml_app_sid).update(
                    voice_url=f'https://{host}/voice/voip-answer',
                    voice_method='POST',
                )
            except Exception:
                # App may have been deleted; create new one
                app = client.applications.create(
                    friendly_name='InsuranceGrokBot Dialer',
                    voice_url=f'https://{host}/voice/voip-answer',
                    voice_method='POST',
                    status_callback=f'https://{host}/voice/status',
                    status_callback_method='POST',
                )
                twiml_app_sid = app.sid

        # Create API Key if not exists
        api_key_sid = vc.get('api_key_sid', '')
        api_key_secret = vc.get('api_key_secret', '')
        if not api_key_sid or not api_key_secret:
            new_key = client.new_keys.create(friendly_name='InsuranceGrokBot Browser')
            api_key_sid = new_key.sid
            api_key_secret = new_key.secret
            logger.info(f"Created API Key: {api_key_sid}")

        # Save to voice_config
        vc['twiml_app_sid'] = twiml_app_sid
        vc['api_key_sid'] = api_key_sid
        vc['api_key_secret'] = api_key_secret
        _save_voice_config(current_user.email, vc)

        return jsonify({"status": "ready", "twiml_app_sid": twiml_app_sid})

    except Exception as e:
        logger.error(f"VoIP setup failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/token', methods=['POST'])
@login_required
def generate_voice_token():
    """Generate a short-lived Twilio Client access token for browser-based calling."""
    subscriber, vc, client = _get_current_subscriber_voice()
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    account_sid = vc.get('twilio_account_sid', '')
    api_key_sid = vc.get('api_key_sid', '')
    api_key_secret = vc.get('api_key_secret', '')
    twiml_app_sid = vc.get('twiml_app_sid', '')

    if not all([account_sid, api_key_sid, api_key_secret, twiml_app_sid]):
        return jsonify({"error": "Browser calling not set up. Click Setup VoIP first."}), 400

    try:
        # Create access token
        identity = f"agent_{subscriber.get('location_id', 'unknown')}"
        token = AccessToken(
            account_sid,
            api_key_sid,
            api_key_secret,
            identity=identity,
            ttl=3600,  # 1 hour
        )

        # Add Voice grant
        voice_grant = VoiceGrant(
            outgoing_application_sid=twiml_app_sid,
            incoming_allow=True,
        )
        token.add_grant(voice_grant)

        return jsonify({
            "token": token.to_jwt(),
            "identity": identity,
        })

    except Exception as e:
        logger.error(f"Token generation failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/voip-answer', methods=['POST'])
def voip_answer():
    """
    TwiML endpoint for browser-originated calls.
    Twilio hits this when the browser-based agent initiates an outbound call.
    """
    to_number = request.form.get('To', '')
    from_number = request.form.get('From', '')
    caller_id = request.form.get('CallerId', '') or request.values.get('callerId', '')
    call_sid = request.form.get('CallSid', '')
    contact_id = request.form.get('ContactId', '')
    contact_name = request.form.get('ContactName', '')

    logger.info(f"📞 VoIP call: {from_number} -> {to_number} (SID: {call_sid})")

    # Save to call history — look up subscriber by their Twilio phone number
    if call_sid and to_number:
        try:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT location_id FROM subscribers WHERE voice_config->>'twilio_phone_number' = %s LIMIT 1",
                        (caller_id,)
                    )
                    row = cur.fetchone()
                    loc_id = row[0] if row else ''
                    cur.close()
                finally:
                    return_db_connection(conn)
                save_call_to_history(
                    location_id=loc_id,
                    call_sid=call_sid,
                    phone=to_number,
                    contact_id=contact_id or None,
                    contact_name=contact_name or None,
                    direction='outbound-voip',
                    status='initiated'
                )
        except Exception as e:
            logger.debug(f"VoIP call history save note: {e}")

    response = VoiceResponse()

    if to_number:
        # Use provided caller ID or default
        dial = Dial(
            caller_id=caller_id if caller_id else from_number,
            record='record-from-answer-dual',
            recording_status_callback=f'https://{request.host}/voice/recording-status',
            recording_status_callback_method='POST',
            action=f'https://{request.host}/voice/status',
        )
        dial.number(to_number)
        response.append(dial)
    else:
        response.say("No phone number specified.", voice="Polly.Matthew")
        response.hangup()

    return Response(str(response), mimetype='application/xml')


# ──────────────────────────────────────────────────────────────
# TRUST HUB: Phone number management + STIR/SHAKEN
# ──────────────────────────────────────────────────────────────

@voice_bp.route('/voice/numbers', methods=['GET'])
@login_required
def list_twilio_numbers():
    """List all phone numbers in the user's Twilio account with status."""
    subscriber, vc, client = _get_current_subscriber_voice()
    if not client:
        return jsonify({"error": "Twilio credentials not configured"}), 400

    try:
        numbers = client.incoming_phone_numbers.list(limit=50)
        result = []
        for n in numbers:
            result.append({
                "sid": n.sid,
                "phone": n.phone_number,
                "friendly_name": n.friendly_name,
                "capabilities": {
                    "voice": n.capabilities.get("voice", False),
                    "sms": n.capabilities.get("sms", False),
                },
                "status": "active",
                "voice_url": n.voice_url or "",
            })

        return jsonify({"numbers": result, "total": len(result)})

    except Exception as e:
        logger.error(f"Failed to list Twilio numbers: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/search', methods=['GET'])
@login_required
def search_available_numbers():
    """Search for available Twilio phone numbers to purchase."""
    subscriber, vc, client = _get_current_subscriber_voice()
    if not client:
        return jsonify({"error": "Twilio credentials not configured"}), 400

    area_code = request.args.get('area_code', '')
    state = request.args.get('state', '')
    contains = request.args.get('contains', '')

    try:
        params = {"voice_enabled": True, "limit": 20}
        if area_code:
            params["area_code"] = area_code
        if state:
            params["in_region"] = state
        if contains:
            params["contains"] = contains

        available = client.available_phone_numbers("US").local.list(**params)
        result = []
        for n in available:
            result.append({
                "phone": n.phone_number,
                "friendly_name": n.friendly_name,
                "locality": n.locality or "",
                "region": n.region or "",
                "capabilities": {
                    "voice": n.capabilities.get("voice", False),
                    "sms": n.capabilities.get("sms", False),
                },
            })

        return jsonify({"numbers": result, "total": len(result)})

    except Exception as e:
        logger.error(f"Number search failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/buy', methods=['POST'])
@login_required
def buy_twilio_number():
    """Purchase a Twilio phone number and configure it for voice."""
    subscriber, vc, client = _get_current_subscriber_voice()
    if not client:
        return jsonify({"error": "Twilio credentials not configured"}), 400

    data = request.json or {}
    phone_number = data.get('phone_number', '')
    if not phone_number:
        return jsonify({"error": "Phone number is required"}), 400

    host = request.host
    try:
        number = client.incoming_phone_numbers.create(
            phone_number=phone_number,
            voice_url=f"https://{host}/voice/inbound",
            voice_method="POST",
            status_callback=f"https://{host}/voice/status",
            status_callback_method="POST",
            friendly_name="InsuranceGrokBot AI",
        )

        logger.info(f"Purchased number: {number.phone_number} (SID: {number.sid})")
        return jsonify({
            "status": "purchased",
            "phone": number.phone_number,
            "sid": number.sid,
        })

    except Exception as e:
        logger.error(f"Number purchase failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/numbers/release', methods=['POST'])
@login_required
def release_twilio_number():
    """Release (cancel) a Twilio phone number."""
    subscriber, vc, client = _get_current_subscriber_voice()
    if not client:
        return jsonify({"error": "Twilio credentials not configured"}), 400

    data = request.json or {}
    phone_sid = data.get('sid', '')
    if not phone_sid:
        return jsonify({"error": "Number SID is required"}), 400

    try:
        client.incoming_phone_numbers(phone_sid).delete()
        logger.info(f"Released number SID: {phone_sid}")
        return jsonify({"status": "released"})

    except Exception as e:
        logger.error(f"Number release failed: {e}")
        return jsonify({"error": str(e)}), 500


@voice_bp.route('/voice/trust-hub', methods=['GET'])
@login_required
def get_trust_hub_status():
    """
    Get STIR/SHAKEN and business identity status from Twilio.
    Returns registration status and any business profiles.
    """
    subscriber, vc, client = _get_current_subscriber_voice()
    if not client:
        return jsonify({"error": "Twilio credentials not configured"}), 400

    result = {
        "stir_shaken": {"status": "unknown", "profiles": []},
        "business_profiles": [],
    }

    try:
        # Check for SHAKEN/STIR business profiles
        try:
            trust_products = client.trusthub.v1.trust_products.list(limit=10)
            for tp in trust_products:
                result["business_profiles"].append({
                    "sid": tp.sid,
                    "friendly_name": tp.friendly_name,
                    "status": tp.status,
                    "policy_sid": tp.policy_sid,
                })
                if "shaken" in (tp.friendly_name or "").lower() or "stir" in (tp.friendly_name or "").lower():
                    result["stir_shaken"]["status"] = tp.status
                    result["stir_shaken"]["profiles"].append({
                        "sid": tp.sid,
                        "name": tp.friendly_name,
                        "status": tp.status,
                    })
        except Exception as e:
            logger.debug(f"Trust products check: {e}")
            result["stir_shaken"]["status"] = "not_configured"

        # If no STIR/SHAKEN profile found, check if any business identity exists
        if result["stir_shaken"]["status"] == "unknown":
            if result["business_profiles"]:
                result["stir_shaken"]["status"] = "business_registered"
            else:
                result["stir_shaken"]["status"] = "not_configured"

        return jsonify(result)

    except Exception as e:
        logger.error(f"Trust hub check failed: {e}")
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# CONTACT DETAIL: Full GHL contact info for in-call display
# ──────────────────────────────────────────────────────────────

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
            "customFields": contact.get("customFields", []),
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
        if resp.status_code != 200:
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
