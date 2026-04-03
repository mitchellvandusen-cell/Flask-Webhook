"""
Omnisconn Voice WebSocket Service

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


app = FastAPI(title="Omnisconn Voice Service", version="1.0.0", lifespan=lifespan)


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
