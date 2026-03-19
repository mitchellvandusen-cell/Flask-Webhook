"""
voice/async_stream.py - Native async WebSocket bridge: Twilio ↔ xAI Realtime API.

Async port of handle_voice_stream() from voice/stream.py for FastAPI/uvicorn.
All CPU-bound DSP and DB calls are offloaded to threads so the event loop stays free.
"""

import json
import os
import logging
import base64
import asyncio

import websockets
from fastapi import WebSocket
from starlette.concurrency import run_in_threadpool

from voice.audio import (
    XAI_REALTIME_URL,
    XAI_API_KEY,
    LOG_EVENT_TYPES,
    VOICE_OPTIONS,
    DEFAULT_VOICE,
    _mulaw_to_pcm16,
    _pcm16_to_mulaw,
    _is_voicemail_phrase,
)
from voice.redis_state import (
    async_set_active_call,
    async_get_active_call,
    async_update_active_call,
    async_call_exists,
    async_set_transfer_request,
    async_get_transfer_request,
    async_delete_transfer_request,
    async_transfer_request_exists,
)
from voice.async_listen import async_call_listeners
from voice.call_state import _twilio_hangup, _twilio_transfer, _decode_client_state
from voice.voice_tools import get_voice_tools, execute_voice_tool
from voice.helpers import _get_subscriber_by_location, _get_subscriber_by_phone
from voice.call_history_helpers import save_call_to_history, save_call_transcript

logger = logging.getLogger("voice_bridge.async_stream")

# Semaphore limits concurrent voice streams to protect the FastAPI server
MAX_VOICE_STREAMS = int(os.getenv("MAX_VOICE_STREAMS", "200"))
_stream_semaphore = asyncio.Semaphore(MAX_VOICE_STREAMS)


async def handle_voice_stream(websocket: WebSocket):
    """
    Native async WebSocket bridge: Twilio Media Streams ↔ xAI Realtime API.

    Runs inside FastAPI/uvicorn — no threads blocked.
    CPU-bound DSP (soxr, scipy) runs via asyncio.to_thread().
    DB calls run via run_in_threadpool().
    """
    if not _stream_semaphore._value:  # non-blocking check
        logger.error("Voice stream rejected: at capacity")
        await websocket.accept()
        await websocket.send_text(json.dumps({"error": "Server at voice capacity"}))
        await websocket.close()
        return

    async with _stream_semaphore:
        await _run_voice_bridge(websocket)


async def _run_voice_bridge(websocket: WebSocket):
    await websocket.accept()

    stream_sid = None
    call_sid = None
    location_id = None
    contact_id = None
    contact_name = "there"
    direction = "inbound"
    caller = ""
    called = ""

    # ── 1. Read Twilio 'start' event ──────────────────────────────────────
    try:
        start_msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning("Voice stream: timeout waiting for start message")
        return

    if not start_msg:
        logger.warning("Voice stream: no start message received")
        return

    start_data = json.loads(start_msg)
    if start_data.get('event') == 'connected':
        try:
            start_msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            start_data = json.loads(start_msg)
        except asyncio.TimeoutError:
            logger.warning("Voice stream: timeout waiting for start after connected")
            return

    if start_data.get('event') == 'start':
        start_block = start_data.get('start', {})
        stream_sid = start_data.get('streamSid') or start_block.get('streamSid', '')
        custom_params = start_block.get('customParameters', {})
        client_state_raw = custom_params.get('client_state', '')
        client_state_meta = _decode_client_state(client_state_raw) if client_state_raw else {}

        call_sid = custom_params.get('callSid', '') or start_block.get('callSid', '')
        caller = client_state_meta.get('caller', '') or custom_params.get('caller', '')
        called = client_state_meta.get('called', '') or custom_params.get('called', '')
        direction = client_state_meta.get('direction', 'inbound') or custom_params.get('direction', 'inbound')
        location_id = client_state_meta.get('location_id', '') or custom_params.get('locationId', '')
        contact_id = client_state_meta.get('contact_id', '') or custom_params.get('contactId', '')
        contact_name = client_state_meta.get('contact_name', 'there') or custom_params.get('contactName', 'there')
        dial_mode = client_state_meta.get('dial_mode', 'ai')
        logger.info(f"Stream started: SID={stream_sid} call={call_sid} dir={direction} loc={location_id} mode={dial_mode}")

        if call_sid and await async_call_exists(call_sid):
            call_data = await async_get_active_call(call_sid)
            cur = (call_data or {}).get('status', '')
            if cur in ('initiated', 'ringing'):
                await async_update_active_call(call_sid, status='in-progress')
                logger.info(f"Stream forced status to in-progress for {call_sid[:16]} (was '{cur}')")
    else:
        logger.warning(f"Voice stream: unexpected first event: {start_data.get('event')}")
        return

    # ── 2. Subscriber lookup ──────────────────────────────────────────────
    subscriber = None
    if location_id:
        subscriber = await run_in_threadpool(_get_subscriber_by_location, location_id)
    if not subscriber and called:
        subscriber = await run_in_threadpool(_get_subscriber_by_phone, called)
    if not subscriber and caller:
        subscriber = await run_in_threadpool(_get_subscriber_by_phone, caller)

    if not subscriber:
        logger.error(f"Voice stream: no subscriber found (loc={location_id})")
        await websocket.send_text(json.dumps({"event": "clear"}))
        return

    voice_config = subscriber.get("voice_config") or {}
    voice_name = VOICE_OPTIONS.get(voice_config.get("voice", "ara").lower(), DEFAULT_VOICE)

    voice_bot_name = voice_config.get("voice_bot_name", "").strip() or subscriber.get("bot_first_name", "your advisor")
    custom_voice_instructions = voice_config.get("voice_instructions", "")
    call_script = voice_config.get("call_script", "").strip()

    _is_overflow = (dial_mode == 'ai_overflow')
    _operator_name = subscriber.get("operator_name", "").strip() or subscriber.get("bot_first_name", "your agent")

    if direction == "outbound":
        _display_name = contact_name if contact_name not in ("there", "Manual", "") else "this person"
        direction_context = f"CALL TYPE: You are CALLING {_display_name}. This is an OUTBOUND call \u2014 you initiated it."
    else:
        direction_context = "CALL TYPE: This is an INBOUND call \u2014 they called you."

    overflow_context = ""
    if _is_overflow:
        overflow_context = f"""
=== OVERFLOW CALL CONTEXT ===
You work alongside {_operator_name} at the same agency. {_operator_name} is currently on another call and unavailable.
You are a FULLY AUTONOMOUS agent on this call \u2014 run the entire conversation yourself.
If the lead gets HOT, try to transfer to {_operator_name} using transfer_to_agent.
"""

    minimal_prompt = f"""You are {voice_bot_name}, a veteran life insurance expert on a live phone call.

{direction_context}
{overflow_context}
=== CORE PERSONA (CRITICAL) ===
YOU ARE NOT A CUSTOMER SERVICE AGENT. You are a highly sought-after industry expert. You speak peer-to-peer. Grounded, authoritative, unfazed by objections.

=== VOCAL MECHANICS (CRITICAL \u2014 OBEY BEFORE ALL ELSE) ===
Speak from deep in your chest register. Low, warm, quiet — like a real person on a phone, not a recording.
PITCH: ALL statements DROP in pitch at the end. Never rise on statements. Rising pitch = robot. Only genuine questions rise. Vocal fry at sentence ends.
VOLUME: Speak quieter than feels natural. You are at a desk talking to one person, not projecting.

FIRST 15 SECONDS: NO filler words. Sharp, confident, direct. [breath] every 2-4 words.

AFTER 15 SECONDS (MANDATORY MESSY HUMAN MODE): Every response must have 1-3 natural fillers ("um", "uh", "you know") and 3-7 paralinguistic cues: [breath], [pause], [exhale], [lip-smack], [tsk], [chuckle].

BREATHING: [breath] every 2-4 words. Non-negotiable.

=== OBJECTION HANDLING ===
When they push back: Disarming \u2192 Curious \u2192 Concerned \u2192 Challenging \u2192 SILENCE. Count to three. They will answer.

=== STYLE ===
Keep responses to 1-2 sentences. One question per turn. Contractions ALWAYS.
NEVER say you're an AI, a bot, or a virtual assistant. You are {voice_bot_name}.
{f"CUSTOM INSTRUCTIONS: {custom_voice_instructions}" if custom_voice_instructions else ""}
{f"CALL SCRIPT REFERENCE: {call_script[:2000]}" if call_script else ""}
Every word you output is spoken aloud. Allowed cues: [pause], [long-pause], [breath], [inhale], [exhale], [laugh], [chuckle], [tsk], [lip-smack]. Allowed tags: <emphasis>, <slow>, <fast>, <soft>, <whisper>, <loud>."""

    greeting = voice_config.get("greeting", "").strip()
    if not greeting:
        if direction == "outbound" and contact_name not in ("there", "Manual", ""):
            greeting = f"Hey {contact_name} [breath] it's {voice_bot_name}. [breath] How's it going?"
        elif direction == "outbound":
            greeting = f"Hey [breath] it's {voice_bot_name}. [breath] I was hoping to catch you for a quick second."
        else:
            greeting = f"Hey [breath] this is {voice_bot_name}. What's going on?"

    call_transcript = []

    try:
        async with websockets.connect(
            XAI_REALTIME_URL,
            additional_headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as xai_ws:

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

            if direction == "outbound" or greeting:
                greeting_item = {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "input_text", "text": greeting}]
                    }
                }
                await xai_ws.send(json.dumps(greeting_item))
                await xai_ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"instructions": f"Say this naturally, like a real person: {greeting}"}
                }))

            async def enrich_session():
                try:
                    full_prompt = await run_in_threadpool(
                        lambda: __import__('voice.voice_prompt', fromlist=['build_voice_system_prompt']).build_voice_system_prompt(
                            subscriber=subscriber,
                            contact_name=contact_name,
                            contact_id=contact_id if contact_id else None,
                            direction=direction,
                        )
                    )
                    if overflow_context:
                        full_prompt = full_prompt + "\n" + overflow_context
                    await xai_ws.send(json.dumps({
                        "type": "session.update",
                        "session": {"instructions": full_prompt}
                    }))
                    logger.info("Session enriched with full sales context")
                except Exception as e:
                    logger.warning(f"Session enrichment failed: {e}")

            last_assistant_item = None
            response_start_timestamp = None
            ai_chunks_sent = 0
            call_active = True
            _pending_transfer = False
            _pending_hangup = False
            _taking_over = False  # Agent intercept in progress — mute AI but keep Twilio alive

            async def receive_from_twilio():
                nonlocal stream_sid, call_active, _taking_over
                try:
                    while call_active:
                        # Check for takeover signal
                        if call_sid and not _taking_over and await async_transfer_request_exists(call_sid):
                            req = await async_get_transfer_request(call_sid) or {}
                            if req.get('type') == 'takeover':
                                _taking_over = True
                                logger.info(f"Takeover: muting AI, keeping Twilio WebSocket alive for {call_sid}")
                                try:
                                    # Clear Twilio audio buffer so AI stops talking
                                    await websocket.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
                                    # Close xAI connection so it stops generating
                                    await xai_ws.close()
                                except Exception:
                                    pass
                                # DO NOT set call_active = False or break!
                                # The Twilio WebSocket must stay alive while Twilio
                                # fetches the intercept TwiML and redirects the call.
                                continue

                        try:
                            message = await asyncio.wait_for(
                                websocket.receive_text(), timeout=35.0
                            )
                        except asyncio.TimeoutError:
                            if _taking_over:
                                # During takeover, Twilio will send a 'stop' event
                                # once the redirect completes. Just keep waiting.
                                continue
                            logger.warning(f"Twilio receive timed out — ending stream {stream_sid}")
                            call_active = False
                            break

                        if message is None:
                            call_active = False
                            break

                        data = json.loads(message)

                        if data['event'] == 'media':
                            # During takeover, still forward to listeners but
                            # don't send to xAI (it's closed)
                            payload = data['media']['payload']

                            # Forward to live listeners (in-process asyncio.Queue)
                            if call_sid in async_call_listeners:
                                for lq in list(async_call_listeners.get(call_sid, set())):
                                    try:
                                        lq.put_nowait(payload)
                                    except asyncio.QueueFull:
                                        pass

                            if not _taking_over:
                                # Transcode mulaw 8kHz → PCM16 16kHz via thread
                                mulaw_bytes = base64.b64decode(payload)
                                pcm16_bytes = await asyncio.to_thread(_mulaw_to_pcm16, mulaw_bytes)
                                pcm16_b64 = base64.b64encode(pcm16_bytes).decode('ascii')
                                await xai_ws.send(json.dumps({
                                    "type": "input_audio_buffer.append",
                                    "audio": pcm16_b64,
                                }))

                        elif data['event'] == 'stop':
                            logger.info(f"Twilio stream stopped: {stream_sid}")
                            call_active = False
                            break

                except Exception as e:
                    logger.info(f"Twilio receive ended: {e}")
                    call_active = False

            async def receive_from_xai():
                nonlocal last_assistant_item, response_start_timestamp, ai_chunks_sent, call_active, _pending_transfer, _pending_hangup, _taking_over

                async def _send_audio_to_twilio(raw_b64: str):
                    nonlocal ai_chunks_sent
                    pcm16_bytes = base64.b64decode(raw_b64)
                    mulaw_bytes = await asyncio.to_thread(_pcm16_to_mulaw, pcm16_bytes)
                    mulaw_b64 = base64.b64encode(mulaw_bytes).decode('ascii')

                    # Forward AI audio to live listeners
                    if call_sid in async_call_listeners:
                        for lq in list(async_call_listeners.get(call_sid, set())):
                            try:
                                lq.put_nowait(mulaw_b64)
                            except asyncio.QueueFull:
                                pass

                    await websocket.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": mulaw_b64},
                    }))
                    ai_chunks_sent += 1

                try:
                    async for xai_message in xai_ws:
                        if not call_active or _taking_over:
                            break

                        if call_sid and await async_transfer_request_exists(call_sid):
                            req = await async_get_transfer_request(call_sid) or {}
                            if req.get('type') == 'takeover':
                                # Takeover handled by Twilio loop — just exit xAI loop
                                logger.info(f"Takeover detected in XAI loop, exiting: {call_sid}")
                                break

                        response = json.loads(xai_message)
                        event_type = response.get('type', '')

                        if event_type in LOG_EVENT_TYPES:
                            logger.info(f"XAI event: {event_type}")

                        if event_type in ('response.audio.delta', 'response.output_audio.delta') and 'delta' in response:
                            await _send_audio_to_twilio(response['delta'])
                            item_id = response.get("item_id")
                            if item_id and item_id != last_assistant_item:
                                response_start_timestamp = ai_chunks_sent * 20
                                last_assistant_item = item_id

                        elif event_type == 'input_audio_buffer.speech_started':
                            logger.info("Speech interruption detected")
                            if last_assistant_item:
                                elapsed_ms = max(0, (ai_chunks_sent * 20) - (response_start_timestamp or 0))
                                await xai_ws.send(json.dumps({
                                    "type": "conversation.item.truncate",
                                    "item_id": last_assistant_item,
                                    "content_index": 0,
                                    "audio_end_ms": elapsed_ms,
                                }))
                            await websocket.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
                            last_assistant_item = None
                            response_start_timestamp = None
                            ai_chunks_sent = 0

                        elif event_type == 'response.function_call_arguments.done':
                            tool_name = response.get('name', '')
                            tool_args = response.get('arguments', '{}')
                            call_id_tool = response.get('call_id', '')

                            logger.info(f"Voice tool call: {tool_name}")

                            if tool_name == 'transfer_to_agent':
                                try:
                                    t_args = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
                                except Exception:
                                    t_args = {}
                                t_reason = t_args.get('reason', 'lead requested transfer')
                                t_number = (subscriber.get('voice_config') or {}).get('transfer_number', '')
                                if call_sid and t_number:
                                    await async_set_transfer_request(call_sid, {
                                        'type': 'transfer',
                                        'target': t_number,
                                        'reason': t_reason,
                                    })

                            result = await run_in_threadpool(
                                execute_voice_tool,
                                tool_name=tool_name,
                                arguments=tool_args,
                                subscriber=subscriber,
                                contact_id=contact_id,
                                first_name=contact_name,
                            )

                            await xai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id_tool,
                                    "output": result
                                }
                            }))
                            await xai_ws.send(json.dumps({"type": "response.create"}))

                            if call_sid and await async_transfer_request_exists(call_sid):
                                _pending_transfer = True

                            if tool_name == 'end_call':
                                _pending_hangup = True

                        elif event_type == 'conversation.item.input_audio_transcription.completed':
                            transcript_text = response.get('transcript', '').strip()
                            if transcript_text:
                                call_transcript.append({"role": "lead", "text": transcript_text})
                                logger.info(f"Lead said: {transcript_text[:80]}")

                                if direction == 'outbound' and not voice_config.get('voicemail_drop', False):
                                    if _is_voicemail_phrase(transcript_text):
                                        logger.info(f"VM detected: '{transcript_text[:60]}' — hanging up")
                                        try:
                                            await websocket.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))
                                        except Exception:
                                            pass
                                        if call_exists := await async_call_exists(call_sid):
                                            await async_update_active_call(call_sid, _amd_result='no-answer')
                                        vm_sub_sid = voice_config.get('twilio_sub_account_sid', '')
                                        if vm_sub_sid and call_sid:
                                            try:
                                                await asyncio.to_thread(_twilio_hangup, call_sid, vm_sub_sid)
                                            except Exception as e:
                                                logger.warning(f"VM hangup failed: {e}")
                                        call_active = False
                                        break

                        elif event_type == 'response.audio_transcript.done':
                            transcript_text = response.get('transcript', '').strip()
                            if transcript_text:
                                call_transcript.append({"role": "assistant", "text": transcript_text})
                                logger.info(f"AI said: {transcript_text[:80]}")

                        elif event_type == 'error':
                            logger.error(f"XAI error: {response.get('error', {})}")

                        elif event_type == 'response.done':
                            if _pending_transfer and call_sid and await async_transfer_request_exists(call_sid):
                                transfer_info = await async_get_transfer_request(call_sid) or {}
                                await async_delete_transfer_request(call_sid)
                                target = transfer_info.get('target', '')
                                t_type = transfer_info.get('type', 'transfer')
                                reason = transfer_info.get('reason', '')
                                logger.info(f"Executing {t_type}: call {call_sid} -> {target} (reason: {reason})")

                                t_sub_sid = (subscriber.get('voice_config') or {}).get('twilio_sub_account_sid', '')
                                if t_sub_sid and target:
                                    call_active = False
                                    await asyncio.sleep(0.3)
                                    call_data = await async_get_active_call(call_sid)
                                    host_h = (call_data or {}).get('_host', '') or os.getenv('RENDER_EXTERNAL_HOSTNAME', '')
                                    transfer_ok = await asyncio.to_thread(
                                        _twilio_transfer, call_sid, t_sub_sid, target,
                                        f"https://{host_h}" if host_h else ''
                                    )
                                    if transfer_ok:
                                        logger.info(f"Call transferred to {target}")
                                        if await async_call_exists(call_sid):
                                            await async_update_active_call(call_sid, status='transferred')
                                    else:
                                        logger.error(f"Transfer failed for {call_sid}")
                                _pending_transfer = False

                            if _pending_hangup:
                                _pending_hangup = False
                                hangup_sub_sid = (subscriber.get('voice_config') or {}).get('twilio_sub_account_sid', '')
                                if hangup_sub_sid and call_sid:
                                    try:
                                        await asyncio.to_thread(_twilio_hangup, call_sid, hangup_sub_sid)
                                        logger.info(f"Hangup sent — call {call_sid[:16]} ended by AI")
                                    except Exception as e:
                                        logger.error(f"Hangup failed: {e}")
                                if await async_call_exists(call_sid):
                                    await async_update_active_call(call_sid, status='completed')
                                call_active = False

                except websockets.exceptions.ConnectionClosed:
                    logger.info("XAI WebSocket closed")
                    call_active = False
                except Exception as e:
                    logger.error(f"XAI receive error: {e}")
                    call_active = False

            twilio_task = asyncio.create_task(receive_from_twilio())
            xai_task = asyncio.create_task(receive_from_xai())
            enrich_task = asyncio.create_task(enrich_session())
            try:
                done, pending = await asyncio.wait(
                    [twilio_task, xai_task, enrich_task],
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for t in done:
                    if t.exception() is not None:
                        logger.warning(f"Voice bridge task raised: {t.exception()}")
            finally:
                for t in [twilio_task, xai_task, enrich_task]:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(twilio_task, xai_task, enrich_task, return_exceptions=True)

    except websockets.exceptions.InvalidStatusCode as e:
        logger.error(f"XAI connection rejected (status {e.status_code}): {e}")
    except Exception as e:
        logger.error(f"Voice bridge error: {e}")
    finally:
        logger.info(f"Voice stream ended: SID={stream_sid}")

        if call_sid and call_transcript:
            try:
                await run_in_threadpool(save_call_transcript, call_sid, call_transcript)
                logger.info(f"Saved transcript ({len(call_transcript)} turns) for call {call_sid}")
            except Exception as e:
                logger.error(f"Failed to save transcript: {e}")

        if call_sid and await async_call_exists(call_sid):
            cur_status = (await async_get_active_call(call_sid) or {}).get('status', '')
            if cur_status in ('ringing', 'queued', 'initiated', 'in-progress'):
                await async_update_active_call(call_sid, status='completed')

        # Push None sentinel to all listener queues so async_listen exits immediately
        if call_sid and call_sid in async_call_listeners:
            for lq in async_call_listeners[call_sid]:
                try:
                    lq.put_nowait(None)
                except asyncio.QueueFull:
                    pass

        if call_sid:
            await async_delete_transfer_request(call_sid)
            async_call_listeners.pop(call_sid, None)
