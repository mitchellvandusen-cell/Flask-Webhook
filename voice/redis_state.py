"""
voice/redis_state.py — Redis-backed call state for cross-service sharing.

Replaces the in-memory dicts in call_state.py with Redis SET/GET operations.
Both Flask (sync) and FastAPI (async) import from this module.

Redis key patterns:
    call:{call_sid}         — active call state (JSON, TTL 3600s)
    xfer:{call_sid}         — transfer/takeover request (JSON, TTL 30s)
    overflow:{location_id}  — overflow transfer alerts (JSON list, TTL 60s)

All values are JSON-serialized via json.dumps/json.loads (not Redis hashes)
because call dicts contain nested data that Redis hashes can't store.
"""

import asyncio
import json
import logging
import os
import time

import redis

logger = logging.getLogger(__name__)

# TTLs
ACTIVE_CALL_TTL = 3600    # 1 hour (matches old reaper NON_TERMINAL_MAX_AGE)
TRANSFER_REQUEST_TTL = 30  # Short-lived signal
OVERFLOW_ALERT_TTL = 60    # Polled by frontend every 2s

# Terminal statuses (used by callers for filtering)
TERMINAL_STATUSES = frozenset({
    "completed", "busy", "no-answer", "failed", "canceled", "transferred"
})

# ── Redis connection (lazy, reconnects on failure) ───────────────────────────

_redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
_redis_conn = None


def _get_redis() -> redis.Redis:
    """Get or create a Redis connection for call state operations."""
    global _redis_conn
    if _redis_conn is not None:
        try:
            _redis_conn.ping()
            return _redis_conn
        except (redis.ConnectionError, redis.TimeoutError, OSError):
            _redis_conn = None

    _redis_conn = redis.from_url(
        _redis_url,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
        decode_responses=True,
    )
    return _redis_conn


# ═════════════════════════════════════════════════════════════════════════════
# SYNC API — Called by Flask HTTP routes
# ═════════════════════════════════════════════════════════════════════════════

# ── active_calls ─────────────────────────────────────────────────────────────

def set_active_call(call_sid: str, data: dict) -> None:
    """Store call state in Redis with 1-hour TTL."""
    r = _get_redis()
    data["_created_at"] = data.get("_created_at", time.time())
    r.set(f"call:{call_sid}", json.dumps(data, default=str), ex=ACTIVE_CALL_TTL)


def get_active_call(call_sid: str) -> dict | None:
    """Retrieve call state from Redis. Returns None if not found."""
    r = _get_redis()
    raw = r.get(f"call:{call_sid}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def update_active_call(call_sid: str, **fields) -> bool:
    """Merge fields into existing call state. Returns False if call not found."""
    data = get_active_call(call_sid)
    if data is None:
        return False
    data.update(fields)
    r = _get_redis()
    # Preserve remaining TTL
    ttl = r.ttl(f"call:{call_sid}")
    if ttl and ttl > 0:
        r.set(f"call:{call_sid}", json.dumps(data, default=str), ex=ttl)
    else:
        r.set(f"call:{call_sid}", json.dumps(data, default=str), ex=ACTIVE_CALL_TTL)
    return True


def delete_active_call(call_sid: str) -> None:
    """Remove call state from Redis."""
    r = _get_redis()
    r.delete(f"call:{call_sid}")


def get_all_active_calls() -> dict:
    """Get all active calls. Returns {call_sid: data_dict}.

    Uses SCAN to iterate call:* keys (safe for production, no KEYS blocking).
    """
    r = _get_redis()
    result = {}
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor, match="call:*", count=100)
        if keys:
            values = r.mget(keys)
            for key, val in zip(keys, values):
                if val:
                    try:
                        sid = key[5:]  # strip "call:" prefix
                        result[sid] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        pass
        if cursor == 0:
            break
    return result


def get_active_calls_for_location(location_id: str) -> dict:
    """Get all active calls for a specific location. Returns {call_sid: data_dict}."""
    all_calls = get_all_active_calls()
    return {
        sid: data for sid, data in all_calls.items()
        if data.get("_location_id") == location_id
    }


# ── transfer_requests ────────────────────────────────────────────────────────

def set_transfer_request(call_sid: str, data: dict) -> None:
    """Store a transfer/takeover request with 30-second TTL."""
    r = _get_redis()
    r.set(f"xfer:{call_sid}", json.dumps(data, default=str), ex=TRANSFER_REQUEST_TTL)


def get_transfer_request(call_sid: str) -> dict | None:
    """Retrieve a transfer request. Returns None if expired or not set."""
    r = _get_redis()
    raw = r.get(f"xfer:{call_sid}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def delete_transfer_request(call_sid: str) -> None:
    """Remove a transfer request from Redis."""
    r = _get_redis()
    r.delete(f"xfer:{call_sid}")


# ── overflow_transfer_alerts ─────────────────────────────────────────────────

def add_overflow_alert(location_id: str, alert: dict) -> None:
    """Add an overflow transfer alert for a location."""
    r = _get_redis()
    key = f"overflow:{location_id}"
    r.rpush(key, json.dumps(alert, default=str))
    r.expire(key, OVERFLOW_ALERT_TTL)


def get_overflow_alerts(location_id: str) -> list:
    """Get all pending overflow alerts for a location."""
    r = _get_redis()
    key = f"overflow:{location_id}"
    raw_items = r.lrange(key, 0, -1)
    alerts = []
    for raw in raw_items:
        try:
            alerts.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            pass
    return alerts


def dismiss_overflow_alert(location_id: str, call_sid: str) -> None:
    """Remove a specific overflow alert by call_sid."""
    r = _get_redis()
    key = f"overflow:{location_id}"
    alerts = get_overflow_alerts(location_id)
    r.delete(key)
    remaining = [a for a in alerts if a.get("call_sid") != call_sid]
    if remaining:
        pipe = r.pipeline()
        for a in remaining:
            pipe.rpush(key, json.dumps(a, default=str))
        pipe.expire(key, OVERFLOW_ALERT_TTL)
        pipe.execute()


# ═════════════════════════════════════════════════════════════════════════════
# ASYNC API — Called by FastAPI WebSocket handlers
# ═════════════════════════════════════════════════════════════════════════════

async def async_set_active_call(call_sid: str, data: dict) -> None:
    await asyncio.to_thread(set_active_call, call_sid, data)


async def async_get_active_call(call_sid: str) -> dict | None:
    return await asyncio.to_thread(get_active_call, call_sid)


async def async_update_active_call(call_sid: str, **fields) -> bool:
    return await asyncio.to_thread(update_active_call, call_sid, **fields)


async def async_delete_active_call(call_sid: str) -> None:
    await asyncio.to_thread(delete_active_call, call_sid)


async def async_get_transfer_request(call_sid: str) -> dict | None:
    return await asyncio.to_thread(get_transfer_request, call_sid)


async def async_set_transfer_request(call_sid: str, data: dict) -> None:
    await asyncio.to_thread(set_transfer_request, call_sid, data)


async def async_delete_transfer_request(call_sid: str) -> None:
    await asyncio.to_thread(delete_transfer_request, call_sid)


async def async_add_overflow_alert(location_id: str, alert: dict) -> None:
    await asyncio.to_thread(add_overflow_alert, location_id, alert)


async def async_get_overflow_alerts(location_id: str) -> list:
    return await asyncio.to_thread(get_overflow_alerts, location_id)
