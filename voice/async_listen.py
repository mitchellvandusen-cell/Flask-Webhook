"""
voice/async_listen.py - Async WebSocket handler for live call listen/monitor.

Native async replacement for run_listen_stream() in voice/stream.py.
Runs inside FastAPI/uvicorn — no thread blocking.
"""

import json
import logging
import asyncio

from fastapi import WebSocket

from voice.redis_state import (
    async_get_active_call,
    async_call_exists,
)

logger = logging.getLogger("voice_bridge.async_listen")

# In-process listener registry: maps call_sid → set of asyncio.Queue
# Stays in-process because it's WebSocket-to-WebSocket audio relay
# on the same FastAPI instance (no cross-process sharing needed).
# Populated by async_stream.py when Twilio audio arrives.
async_call_listeners: dict = {}  # { call_sid: set(asyncio.Queue, ...) }

_LISTEN_TERMINAL = frozenset(
    ('completed', 'failed', 'canceled', 'transferred', 'no-answer', 'busy')
)


async def handle_listen_stream(websocket: WebSocket):
    """
    Async WebSocket handler for live call listening.
    Accepts call_sid from query params or first message.
    Forwards mulaw audio chunks to the browser for playback.
    """
    await websocket.accept()

    listener_queue = asyncio.Queue(maxsize=500)
    call_sid = None

    try:
        # Try to get call_sid from query params first
        call_sid = websocket.query_params.get('call_sid', '')

        if not call_sid:
            # Wait for init message from browser: { "call_sid": "CAxxxxxx" }
            try:
                init_msg = await asyncio.wait_for(
                    websocket.receive_text(), timeout=10.0
                )
                init_data = json.loads(init_msg)
                call_sid = init_data.get('call_sid', '')
            except asyncio.TimeoutError:
                logger.warning("Listen stream: timeout waiting for init message")
                await websocket.send_text(json.dumps({"error": "Timeout waiting for call_sid"}))
                return
            except Exception as e:
                logger.warning(f"Listen stream: error receiving init: {e}")
                return

        if not call_sid:
            await websocket.send_text(json.dumps({"error": "call_sid required"}))
            return

        logger.info(f"Listen stream: init received, call_sid={call_sid[:16]}")

        # Verify call exists in Redis
        if not await async_call_exists(call_sid):
            logger.warning(f"Listen stream: {call_sid[:16]} not in active_calls")
            await websocket.send_text(json.dumps({"error": "Call not found or already ended"}))
            return

        call_data = await async_get_active_call(call_sid)
        call_status = (call_data or {}).get('status', '')
        if call_status in _LISTEN_TERMINAL:
            logger.warning(f"Listen stream: {call_sid[:16]} already in terminal state {call_status}")
            await websocket.send_text(json.dumps({"error": f"Call already ended ({call_status})"}))
            return

        # Register this listener
        if call_sid not in async_call_listeners:
            async_call_listeners[call_sid] = set()
        async_call_listeners[call_sid].add(listener_queue)
        logger.info(f"Live listen started for call {call_sid[:16]} (listeners: {len(async_call_listeners[call_sid])})")

        await websocket.send_text(json.dumps({"status": "listening", "call_sid": call_sid}))

        # Forward audio chunks to browser
        chunks_sent = 0
        while True:
            try:
                chunk = await asyncio.wait_for(listener_queue.get(), timeout=2.0)
                # None sentinel = stream ended, exit immediately
                if chunk is None:
                    logger.info(f"Listen stream: sentinel received for {call_sid[:16]}, closing")
                    await websocket.send_text(json.dumps({"status": "call_ended"}))
                    break
                await websocket.send_text(json.dumps({"audio": chunk}))
                chunks_sent += 1
                if chunks_sent == 1:
                    logger.info(f"Listen stream: first audio chunk sent for {call_sid[:16]}")
            except asyncio.TimeoutError:
                # Check if call is still active in Redis
                call_data = await async_get_active_call(call_sid)
                if not call_data or call_data.get('status', '') in _LISTEN_TERMINAL:
                    logger.info(f"Listen stream: call {call_sid[:16]} ended, closing")
                    await websocket.send_text(json.dumps({"status": "call_ended"}))
                    break
                # Send keepalive
                try:
                    await websocket.send_text(json.dumps({"keepalive": True}))
                except Exception:
                    logger.debug(f"Listen stream: keepalive failed for {call_sid[:16]}")
                    break
            except Exception as e:
                logger.debug(f"Listen stream: loop error for {call_sid[:16]}: {e}")
                break

    except Exception as e:
        logger.warning(f"Listen stream: unexpected error: {e}")
    finally:
        # Unregister listener
        if call_sid and call_sid in async_call_listeners:
            async_call_listeners[call_sid].discard(listener_queue)
            if not async_call_listeners[call_sid]:
                del async_call_listeners[call_sid]
        logger.info(f"Live listen ended for call {call_sid[:16] if call_sid else 'none'}")
