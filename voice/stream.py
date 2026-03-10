"""
voice/stream.py - Core WebSocket bridge: Twilio Media Streams <-> xAI Realtime API.

Contains handle_voice_stream (async), run_voice_stream, and run_listen_stream.
Extracted from voice_bridge.py.
"""

import json
import os
import logging
import threading
import time
import asyncio
import base64
import re
import queue as _queue_module

import httpx
import websockets

from db import get_db_connection, return_db_connection, log_webhook_event, deduct_ai_minutes
from ghl_logger import log_call_to_ghl
from voice.audio import (
    XAI_REALTIME_URL,
    XAI_API_KEY,
    XAI_SAMPLE_RATE,
    TWILIO_SAMPLE_RATE,
    LOG_EVENT_TYPES,
    VOICE_OPTIONS,
    DEFAULT_VOICE,
    _mulaw_to_pcm16,
    _pcm16_to_mulaw,
    _is_voicemail_phrase,
)
from voice.call_state import (
    active_calls,
    transfer_requests,
    call_listeners,
    voice_stream_semaphore,
    MAX_VOICE_STREAMS,
    _twilio_hangup,
    _twilio_transfer,
    _decode_client_state,
)
from voice.voice_prompt import build_voice_system_prompt
from voice.voice_tools import get_voice_tools, execute_voice_tool
from voice.helpers import _get_subscriber_by_location, _get_subscriber_by_phone
from voice.call_history_helpers import save_call_to_history, save_call_transcript
import twilio_provisioning

logger = logging.getLogger("voice_bridge.stream")


async def handle_voice_stream(ws):
    """
    Core WebSocket handler: bridges Twilio Media Streams <-> XAI Realtime API.
    Called by flask-sock for each new Twilio stream connection.

    Audio flow — mulaw 8kHz (Twilio) <-> PCM16 16kHz (xAI) with transcoding:
        Lead speaks  -> Twilio (mulaw 8kHz base64) -> transcode -> xAI (PCM16 16kHz)
        xAI responds -> (PCM16 16kHz base64 delta)  -> transcode -> Twilio (mulaw 8kHz)

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

        # Belt-and-suspenders: if this is an outbound call and the media stream
        # is connected, the call is definitely in-progress. Force the status
        # update in case out-of-order Twilio callbacks haven't set it yet.
        if call_sid and call_sid in active_calls:
            cur = active_calls[call_sid].get('status', '')
            if cur in ('initiated', 'ringing'):
                active_calls[call_sid]['status'] = 'in-progress'
                logger.info(f"Stream forced status to in-progress for {call_sid[:16]} (was '{cur}')")
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
        _display_name = contact_name if contact_name not in ("there", "Manual", "") else "this person"
        direction_context = f"CALL TYPE: You are CALLING {_display_name}. This is an OUTBOUND call \u2014 you initiated it. You called them. You are the caller, they are the person you dialed. Do NOT act like they called you. You reached out to share something valuable."
    else:
        direction_context = "CALL TYPE: This is an INBOUND call \u2014 they called you. Respond to why they called. Be helpful and direct."

    minimal_prompt = f"""You are {voice_bot_name}, a life insurance advisor on a live phone call.

{direction_context}

VOICE: You sound like a real person who's been doing this for years. Casual, warm, direct. Use contractions \u2014 "I'm", "you're", "can't", "gonna", "wanna", "kinda". Keep responses to 1-2 sentences max. One question per turn. No jargon. Never say "Great question" or "I appreciate you asking" or "I'd be happy to help" \u2014 just respond like a human would.

MESSY REALISM: Sound like you're thinking in real time. Use natural fillers mid-thought: "um", "uh", "you know", "I mean", "like". Add [breath] before important points. Use [short pause] after fillers. Let sentences trail or self-correct occasionally: "The thing is\u2014 I mean, what most people miss is..." Use [sigh softly] for empathy, [small chuckle] for light moments, [tsk] for casual emphasis. Aim for 2-3 cues per response minimum.

PACE: You talk like you're on the phone with a friend. Short sentences. Natural pauses. Don't monologue. React to what they say before moving on. "Yeah", "Right", "Got it", "Mm" \u2014 quick acknowledgments, then your actual response.

STYLE: You've had this exact conversation hundreds of times. You already know the answers. You recognize their situation fast. You don't over-explain. You don't ask permission \u2014 "do you have a minute" or "is now a good time" are banned. Just talk. If they don't stop you, keep going.

NEVER say you're an AI, a bot, or a virtual assistant. You are {voice_bot_name}. NEVER give specific pricing or dollar amounts \u2014 pivot to booking instead.
{f"CUSTOM INSTRUCTIONS: {custom_voice_instructions}" if custom_voice_instructions else ""}
{f"CALL SCRIPT REFERENCE (use naturally, never read verbatim): {call_script[:2000]}" if call_script else ""}
Every word you output is spoken aloud. Allowed inline cues: [pause], [short pause], [long-pause], [breath], [inhale], [exhale], [sigh], [sigh softly], [laugh], [small chuckle], [lip-smack], [tsk]. Allowed wrapper tags: <emphasis>, <slow>, <fast>, <soft>, <whisper>, <loud>, <higher-pitch>, <lower-pitch>, <build-intensity>. Output ONLY what {voice_bot_name} would say. Nothing else."""

    # Build greeting -- short, casual, natural. NOT a script to read verbatim.
    greeting = voice_config.get("greeting", "").strip()
    if not greeting:
        if direction == "outbound" and contact_name not in ("there", "Manual", ""):
            greeting = f"Hey {contact_name}, it's {voice_bot_name}. How's it going?"
        elif direction == "outbound":
            greeting = f"Hey, it's {voice_bot_name}. I was hoping to catch you for a quick second."
        else:
            greeting = f"Hey, this is {voice_bot_name}. What's going on?"

    logger.info(f"Fast-connecting to XAI Realtime API (voice={voice_name})")

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

            # Configure the XAI session -- PCM 16kHz both directions
            # Aggressive VAD settings for fast turn-taking (~2s response time target)
            session_config = {
                "type": "session.update",
                "session": {
                    "voice": voice_name,
                    "instructions": minimal_prompt,
                    "temperature": 1.1,
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.4,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
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
            logger.info("XAI session configured (fast VAD, natural prompt)")

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
                # Immediately trigger audio generation -- no extra context messages
                await xai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "instructions": f"Say this naturally, like a real person: {greeting}"
                    }
                }))

            # -- Background: build full prompt with sales context + calendar --
            async def enrich_session():
                """Load full sales director context and calendar, then update the session."""
                try:
                    full_prompt = await asyncio.get_running_loop().run_in_executor(
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
                    logger.info("Session enriched with full sales context + calendar")
                except Exception as e:
                    logger.warning(f"Session enrichment failed (using minimal prompt): {e}")

            # Connection state
            last_assistant_item      = None
            response_start_timestamp = None
            ai_chunks_sent           = 0     # count of 20 ms PCM chunks sent -> Twilio
            call_active              = True
            _pending_transfer        = False  # set True when AI requests transfer; cleared on response.done
            _pending_hangup          = False  # set True when AI calls end_call tool; hangs up on response.done

            # -- Twilio -> XAI: mulaw 8kHz -> PCM16 16kHz --
            async def receive_from_twilio():
                """Relay Twilio -> xAI. Transcode mulaw 8kHz to PCM16 16kHz."""
                nonlocal stream_sid, call_active
                try:
                    while call_active:
                        # Check for immediate takeover (agent barge-in)
                        # Only mute AI audio here -- the REST route handles the actual
                        # Twilio redirect to avoid double-fire race conditions.
                        if call_sid and call_sid in transfer_requests:
                            req = transfer_requests.get(call_sid, {})
                            if req.get('type') == 'takeover':
                                logger.info(f"Instant AI audio cutoff (Twilio loop): {call_sid}")
                                # Flush buffered AI audio from Twilio's pipeline
                                try:
                                    ws.send(json.dumps({"event": "clear", "streamSid": stream_sid}))
                                except Exception:
                                    pass
                                call_active = False
                                break

                        try:
                            message = await asyncio.wait_for(
                                asyncio.get_running_loop().run_in_executor(None, ws.receive),
                                timeout=35  # slightly longer than xAI ping cycle (20+10)
                            )
                        except asyncio.TimeoutError:
                            logger.warning(f"Twilio ws.receive() timed out after 35s \u2014 ending stream {stream_sid}")
                            call_active = False
                            break
                        if message is None:
                            logger.info("Twilio stream ended (None received)")
                            call_active = False
                            break

                        data = json.loads(message)

                        if data['event'] == 'media':
                            # Forward to live listeners (lead audio)
                            if call_sid in call_listeners:
                                payload = data['media']['payload']
                                for lq in list(call_listeners.get(call_sid, set())):
                                    try:
                                        lq.put_nowait(payload)
                                    except Exception:
                                        pass

                            # Transcode mulaw 8kHz -> PCM16 16kHz for xAI
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

            # -- xAI -> Twilio: PCM16 16kHz -> mulaw 8kHz --
            async def receive_from_xai():
                """Relay xAI -> Twilio. Transcode PCM16 16kHz to mulaw 8kHz."""
                nonlocal last_assistant_item, response_start_timestamp, ai_chunks_sent, call_active, _pending_transfer, _pending_hangup

                def _send_audio_to_twilio(raw_b64: str):
                    """Transcode xAI PCM16 16kHz -> mulaw 8kHz and send to Twilio."""
                    nonlocal ai_chunks_sent
                    pcm16_bytes = base64.b64decode(raw_b64)
                    mulaw_bytes = _pcm16_to_mulaw(pcm16_bytes)
                    mulaw_b64 = base64.b64encode(mulaw_bytes).decode('ascii')

                    # Forward AI audio to live listeners
                    if call_sid in call_listeners:
                        for lq in list(call_listeners.get(call_sid, set())):
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

                        # -- Instant takeover check in XAI relay --
                        # Without this, AI audio keeps streaming to the caller
                        # during the gap between takeover signal and Twilio redirect.
                        if call_sid and call_sid in transfer_requests:
                            req = transfer_requests.get(call_sid, {})
                            if req.get('type') == 'takeover':
                                logger.info(f"Instant AI audio cutoff (XAI relay): {call_sid}")
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
                            logger.info(f"XAI event: {event_type}")

                        # xAI PCM16 -> mulaw transcode -> Twilio (both event name variants)
                        if event_type in ('response.audio.delta', 'response.output_audio.delta') \
                                and 'delta' in response:
                            _send_audio_to_twilio(response['delta'])
                            item_id = response.get("item_id")
                            if item_id and item_id != last_assistant_item:
                                response_start_timestamp = ai_chunks_sent * 20  # ~20 ms per chunk
                                last_assistant_item = item_id

                        # Speech interruption: user started talking while AI was speaking
                        elif event_type == 'input_audio_buffer.speech_started':
                            logger.info("Speech interruption detected")
                            if last_assistant_item:
                                # Estimate how much audio the AI has played (chunks x ~20 ms each)
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

                            logger.info(f"Voice tool call: {tool_name} (call_id={call_id_tool})")

                            # For transfer_to_agent, set signal directly using our call_sid
                            if tool_name == 'transfer_to_agent':
                                try:
                                    t_args = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
                                except Exception:
                                    t_args = {}
                                t_reason = t_args.get('reason', 'lead requested transfer')
                                t_number = (subscriber.get('voice_config') or {}).get('transfer_number', '')
                                if call_sid and t_number:
                                    transfer_requests[call_sid] = {
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
                            if call_sid and call_sid in transfer_requests:
                                # Wait for the AI to finish speaking the handoff message
                                # by listening for response.done before transferring
                                _pending_transfer = True

                            # If end_call was requested, let the AI say its goodbye
                            # (response.done), then hang up via Twilio REST
                            if tool_name == 'end_call':
                                logger.info(f"end_call queued \u2014 waiting for AI goodbye before hangup")
                                _pending_hangup = True

                        # Transcription: user speech -> text
                        elif event_type == 'conversation.item.input_audio_transcription.completed':
                            transcript_text = response.get('transcript', '').strip()
                            if transcript_text:
                                call_transcript.append({"role": "lead", "text": transcript_text})
                                logger.info(f"Lead said: {transcript_text[:80]}")

                                # -- Software voicemail detection (outbound AI calls) --
                                # Fires on first voicemail phrase -- WAY faster than
                                # Twilio AMD which waits for entire greeting to end.
                                # Respects voicemail_drop setting: OFF = hang up, ON = let AI talk.
                                if direction == 'outbound' and not voice_config.get('voicemail_drop', False):
                                    if _is_voicemail_phrase(transcript_text):
                                        logger.info(f"VM detected (software): '{transcript_text[:60]}' \u2014 hanging up")
                                        # Flush any buffered AI audio from Twilio pipeline
                                        try:
                                            ws.send(json.dumps({"event": "clear", "streamSid": stream_sid}))
                                        except Exception:
                                            pass
                                        # Mark as no-answer so dialer retries
                                        if call_sid in active_calls:
                                            active_calls[call_sid]['_amd_result'] = 'no-answer'
                                        # Hang up via Twilio REST
                                        vm_sub_sid = voice_config.get('twilio_sub_account_sid', '')
                                        if vm_sub_sid and call_sid:
                                            try:
                                                _twilio_hangup(call_sid, vm_sub_sid)
                                            except Exception as e:
                                                logger.warning(f"VM hangup failed: {e}")
                                        call_active = False
                                        break

                        # Transcription: AI response -> text
                        elif event_type == 'response.audio_transcript.done':
                            transcript_text = response.get('transcript', '').strip()
                            if transcript_text:
                                call_transcript.append({"role": "assistant", "text": transcript_text})
                                logger.info(f"AI said: {transcript_text[:80]}")

                        # Error handling
                        elif event_type == 'error':
                            error_msg = response.get('error', {})
                            logger.error(f"XAI error: {error_msg}")

                        # response.done -- AI finished generating a response
                        elif event_type == 'response.done':
                            # Check for pending transfer or takeover
                            if _pending_transfer and call_sid and call_sid in transfer_requests:
                                transfer_info = transfer_requests.pop(call_sid, {})
                                target = transfer_info.get('target', '')
                                t_type = transfer_info.get('type', 'transfer')
                                reason = transfer_info.get('reason', '')
                                logger.info(f"Executing {t_type}: call {call_sid} -> {target} (reason: {reason})")

                                # Get voice service for transfer commands
                                t_sub_sid = (subscriber.get('voice_config') or {}).get('twilio_sub_account_sid', '')
                                if t_sub_sid and target:
                                    call_active = False
                                    await asyncio.sleep(0.3)
                                    # Transfer via Twilio REST (stops media stream automatically)
                                    host_h = active_calls.get(call_sid, {}).get('_host', '') or os.getenv('RENDER_EXTERNAL_HOSTNAME', '')
                                    transfer_ok = _twilio_transfer(call_sid, t_sub_sid, target, f"https://{host_h}" if host_h else '')

                                    if transfer_ok:
                                        logger.info(f"Call transferred to {target}")
                                        # Update call status
                                        if call_sid in active_calls:
                                            active_calls[call_sid]['status'] = 'transferred'
                                    else:
                                        logger.error(f"Transfer failed for {call_sid}")

                                _pending_transfer = False

                            # end_call: AI has finished its goodbye -- hang up now
                            if _pending_hangup:
                                _pending_hangup = False
                                hangup_sub_sid = (subscriber.get('voice_config') or {}).get('twilio_sub_account_sid', '')
                                logger.info(f"Executing end_call hangup for {call_sid}")
                                if hangup_sub_sid and call_sid:
                                    try:
                                        _twilio_hangup(call_sid, hangup_sub_sid)
                                        logger.info(f"Hangup sent \u2014 call {call_sid[:16]} ended by AI")
                                    except Exception as e:
                                        logger.error(f"Hangup failed: {e}")
                                if call_sid in active_calls:
                                    active_calls[call_sid]['status'] = 'completed'
                                call_active = False

                            # Takeover (human barge-in) is handled by the instant
                            # cutoff at the top of this loop + REST route redirect.
                            # No duplicate Twilio transfer here.

                except websockets.exceptions.ConnectionClosed:
                    logger.info("XAI WebSocket closed")
                    call_active = False
                except Exception as e:
                    logger.error(f"XAI receive error: {e}")
                    call_active = False

            # Run Twilio<->xAI audio bridge + background context enrichment concurrently.
            # Use asyncio.wait with FIRST_EXCEPTION so that when one loop ends,
            # we force-cancel the sibling tasks instead of leaving them blocked.
            twilio_task = asyncio.create_task(receive_from_twilio())
            xai_task    = asyncio.create_task(receive_from_xai())
            enrich_task = asyncio.create_task(enrich_session())
            try:
                done, pending = await asyncio.wait(
                    [twilio_task, xai_task, enrich_task],
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                # Log any exceptions from completed tasks
                for t in done:
                    if t.exception() is not None:
                        logger.warning(f"Voice bridge task {t.get_name()} raised: {t.exception()}")
            finally:
                # Force-cancel any remaining tasks so threads aren't leaked
                for t in [twilio_task, xai_task, enrich_task]:
                    if not t.done():
                        t.cancel()
                # Await cancellation to ensure clean shutdown
                await asyncio.gather(twilio_task, xai_task, enrich_task, return_exceptions=True)

    except websockets.exceptions.InvalidStatusCode as e:
        logger.error(f"XAI connection rejected (status {e.status_code}): {e}")
    except Exception as e:
        logger.error(f"Voice bridge error: {e}")
    finally:
        logger.info(f"Voice stream ended: SID={stream_sid}")
        # Save transcript to call_history if we have any
        if call_sid and call_transcript:
            try:
                save_call_transcript(call_sid, call_transcript)
                logger.info(f"Saved transcript ({len(call_transcript)} turns) for call {call_sid}")
            except Exception as e:
                logger.error(f"Failed to save transcript: {e}")

            # -- AI Auto-Callback Detection --
            # If enabled, analyze transcript for callback requests in background
            if subscriber and subscriber.get('voice_config', {}).get('auto_callback', False):
                try:
                    from voice.call_history import _analyze_callback_from_transcript
                    location_id_cb = subscriber.get('location_id', '')
                    tz_str = subscriber.get('voice_config', {}).get('timezone', 'America/New_York')
                    threading.Thread(
                        target=_analyze_callback_from_transcript,
                        args=(call_sid, call_transcript, location_id_cb, contact_id, tz_str),
                        daemon=True,
                        name=f"auto-callback-{call_sid[:12]}"
                    ).start()
                    logger.info(f"Auto-callback analysis queued for {call_sid}")
                except Exception as e:
                    logger.error(f"Failed to queue auto-callback analysis: {e}")

        # Mark call as completed if still showing in-progress
        # (Twilio status callback may arrive later, but this prevents
        #  stale in-progress entries that allow intercept on ended calls)
        if call_sid and call_sid in active_calls:
            cur_status = active_calls[call_sid].get('status', '')
            if cur_status in ('ringing', 'queued', 'initiated', 'in-progress'):
                active_calls[call_sid]['status'] = 'completed'
        # Clean up any leftover transfer request and listener queues
        if call_sid:
            transfer_requests.pop(call_sid, None)
            call_listeners.pop(call_sid, None)
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
    Uses a semaphore to enforce max concurrent voice streams (backpressure).
    """
    if not voice_stream_semaphore.acquire(blocking=False):
        logger.error(f"Voice stream rejected: {MAX_VOICE_STREAMS} concurrent streams already active")
        try:
            ws.send(json.dumps({"error": "Server at voice capacity, please try again shortly"}))
        except Exception:
            pass
        return
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(handle_voice_stream(ws))
        finally:
            loop.close()
    finally:
        voice_stream_semaphore.release()


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
        # Client sends this immediately in onopen, so plain receive() is fine.
        # If connection drops before message arrives, receive() returns None.
        try:
            init_msg = ws.receive()
        except Exception as e:
            logger.warning(f"Listen stream: receive() error waiting for init: {e}")
            init_msg = None

        if not init_msg:
            logger.warning("Listen stream: connection closed before init message arrived")
            return

        init_data = json.loads(init_msg)
        call_sid = init_data.get('call_sid') or ''
        logger.info(f"Listen stream: init received, call_sid={call_sid[:16] if call_sid else 'EMPTY'}")

        if not call_sid:
            logger.warning("Listen stream: call_sid missing in init message")
            try:
                ws.send(json.dumps({"error": "call_sid required"}))
            except Exception:
                pass
            return

        if call_sid not in active_calls:
            logger.warning(f"Listen stream: {call_sid[:16]} not in active_calls")
            try:
                ws.send(json.dumps({"error": "Call not found or already ended"}))
            except Exception:
                pass
            return

        call_status = active_calls.get(call_sid, {}).get('status', '')
        logger.info(f"Listen stream: {call_sid[:16]} status={call_status}")
        if call_status in ('completed', 'failed', 'canceled', 'transferred', 'no-answer'):
            logger.warning(f"Listen stream: {call_sid[:16]} already in terminal state {call_status}")
            try:
                ws.send(json.dumps({"error": f"Call already ended ({call_status})"}))
            except Exception:
                pass
            return

        # Register this listener
        if call_sid not in call_listeners:
            call_listeners[call_sid] = set()
        call_listeners[call_sid].add(listener_queue)
        logger.info(f"Live listen started for call {call_sid[:16]} (listeners: {len(call_listeners[call_sid])})")

        ws.send(json.dumps({"status": "listening", "call_sid": call_sid}))

        # Forward audio chunks to browser
        chunks_sent = 0
        while True:
            try:
                # Block for up to 2 seconds waiting for audio
                chunk = listener_queue.get(timeout=2)
                ws.send(json.dumps({"audio": chunk}))
                chunks_sent += 1
                if chunks_sent == 1:
                    logger.info(f"Listen stream: first audio chunk sent for {call_sid[:16]}")
            except _queue_module.Empty:
                # Check if call is still active
                cur_status = active_calls.get(call_sid, {}).get('status', '')
                if call_sid not in active_calls or cur_status in ('completed', 'failed', 'canceled', 'transferred'):
                    logger.info(f"Listen stream: call {call_sid[:16]} ended (status={cur_status}), closing")
                    try:
                        ws.send(json.dumps({"status": "call_ended"}))
                    except Exception:
                        pass
                    break
                # Send keepalive
                try:
                    ws.send(json.dumps({"keepalive": True}))
                except Exception:
                    logger.debug(f"Listen stream: keepalive failed for {call_sid[:16]}, client disconnected")
                    break
            except Exception as e:
                logger.debug(f"Listen stream: loop error for {call_sid[:16]}: {e}")
                break

    except Exception as e:
        logger.warning(f"Listen stream: unexpected error: {e}")
    finally:
        # Unregister listener
        if call_sid and call_sid in call_listeners:
            call_listeners[call_sid].discard(listener_queue)
            if not call_listeners[call_sid]:
                del call_listeners[call_sid]
        logger.info(f"Live listen ended for call {call_sid[:16] if call_sid else 'none'}")
