"""
InsuranceGrokBot Voice WebSocket Service

Standalone FastAPI/uvicorn server handling:
  - Twilio Media Stream ↔ xAI Realtime API WebSocket bridge
  - Live call listen/monitor WebSocket

Runs on port 8081 (or VOICE_PORT env var) alongside Flask on 8080.
All HTTP routes stay in Flask. Call state shared via Redis.

Deploy: uvicorn voice_server:app --host 0.0.0.0 --port 8081 --workers 1
  (single worker — call_listeners is in-process dict, do NOT scale horizontally)
"""

import os
import logging
import asyncio
import concurrent.futures
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

# Cap DB pool before importing db so pool initializes with safe limits.
# Flask uses DB_POOL_MAX=20; we cap FastAPI at 10 → total 30 < Postgres default 100.
os.environ.setdefault('DB_POOL_MIN', '2')
os.environ.setdefault('DB_POOL_MAX', '10')

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from voice.async_stream import handle_voice_stream
from voice.async_listen import handle_listen_stream

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    """Bump default thread pool so DSP + DB calls don't starve each other."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=100)
    )

    # Initialize token encryption so the voice server can decrypt OAuth tokens
    # stored in the DB. The key auto-loads from the app_settings table (same key
    # the Flask app generated). Without this, session enrichment fails because
    # subscriber tokens can't be decrypted.
    try:
        from token_encryption import initialize_encryption
        initialize_encryption()
    except Exception as e:
        logger.warning(f"Token encryption init failed (non-fatal): {e}")

    # Attach error feed handler for event-driven monitoring
    try:
        import redis as _redis
        _r = _redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
        _r.ping()
        from error_feed import attach_error_handler
        attach_error_handler("voice-server", lambda: _r)
    except Exception:
        pass  # non-fatal — monitoring degrades to polling

    port = os.getenv("PORT", os.getenv("VOICE_PORT", "8081"))
    logger.info(f"Voice server started — port={port}, thread pool: 100 workers")
    yield


app = FastAPI(title="InsuranceGrokBot Voice Service", version="1.0.0", lifespan=lifespan)


@app.websocket("/voice/stream")
async def ws_voice_stream(websocket: WebSocket):
    """Twilio Media Stream ↔ xAI Realtime API bridge."""
    try:
        await handle_voice_stream(websocket)
    except WebSocketDisconnect:
        logger.info("Twilio stream disconnected")
    except Exception as e:
        logger.error(f"Voice stream error: {e}", exc_info=True)


@app.websocket("/voice/listen-stream")
async def ws_listen_stream(websocket: WebSocket):
    """Live call listen/monitor WebSocket for dashboard."""
    try:
        await handle_listen_stream(websocket)
    except WebSocketDisconnect:
        logger.info("Listen stream disconnected")
    except Exception as e:
        logger.error(f"Listen stream error: {e}", exc_info=True)


# ── Screen Share Signaling WebSocket ──────────────────────────────────────────
# Relays SDP offers/answers and ICE candidates between agent and viewer.
# No video passes through — just lightweight JSON signaling messages.

_share_connections = {}  # session_id → {"agent": WebSocket|None, "viewer": WebSocket|None}


@app.websocket("/voice/share/signal/{session_id}")
async def ws_share_signal(websocket: WebSocket, session_id: str):
    """WebRTC signaling relay for screen sharing sessions."""
    role = websocket.query_params.get("role", "")
    if role not in ("agent", "viewer"):
        await websocket.close(code=4000, reason="role must be agent or viewer")
        return

    # Verify session exists in Redis
    try:
        import redis as _rd
        r = _rd.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
        raw = r.get(f"screenshare:{session_id}")
        if not raw:
            await websocket.close(code=4001, reason="Session not found or expired")
            return
    except Exception as e:
        logger.error(f"[ShareSignal] Redis check failed: {e}")
        await websocket.close(code=4002, reason="Server error")
        return

    await websocket.accept()

    # Register connection
    if session_id not in _share_connections:
        _share_connections[session_id] = {"agent": None, "viewer": None}
    _share_connections[session_id][role] = websocket

    peer_role = "viewer" if role == "agent" else "agent"
    logger.info(f"[ShareSignal] {role} connected to session {session_id[:12]}")

    # Notify peer that we connected
    peer = _share_connections[session_id].get(peer_role)
    if peer:
        try:
            await peer.send_json({"type": "peer_joined", "role": role})
        except Exception:
            pass

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            # Relay signaling messages to the peer
            peer = _share_connections.get(session_id, {}).get(peer_role)
            if peer:
                try:
                    await peer.send_json(data)
                except Exception:
                    logger.warning(f"[ShareSignal] Failed to relay {msg_type} to {peer_role}")

            if msg_type == "end":
                break

    except WebSocketDisconnect:
        logger.info(f"[ShareSignal] {role} disconnected from session {session_id[:12]}")
    except Exception as e:
        logger.error(f"[ShareSignal] Error in {role} for session {session_id[:12]}: {e}")
    finally:
        # Clean up connection
        if session_id in _share_connections:
            _share_connections[session_id][role] = None
            # Notify peer of disconnect
            peer = _share_connections[session_id].get(peer_role)
            if peer:
                try:
                    await peer.send_json({"type": "peer_left", "role": role})
                except Exception:
                    pass
            # Clean up dict if both gone
            if not _share_connections[session_id]["agent"] and not _share_connections[session_id]["viewer"]:
                del _share_connections[session_id]


@app.get("/health")
async def health():
    return {"status": "ok", "service": "voice"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", os.getenv("VOICE_PORT", "8081"))),
        log_level="info",
    )
