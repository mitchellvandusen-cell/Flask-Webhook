"""
voice/redis_state.py - Redis-backed call state for shared Flask + FastAPI access.

Replaces the in-memory dicts from call_state.py with Redis-backed storage.
Both Flask (sync) and FastAPI (async) import from here.

Redis key patterns:
  call:{call_sid}              → JSON string (active call data, TTL 3600s)
  xfer:{call_sid}              → JSON string (transfer request, TTL 30s)
  overflow:{location_id}       → Redis List of JSON strings (TTL 60s per entry)
  call_sids_by_loc:{location_id} → Redis Set of call_sids (TTL 3600s)
  warmxfer:{call_sid}          → JSON string (warm transfer state, TTL 300s)
"""

import json
import logging
import os
import asyncio

import redis

logger = logging.getLogger("voice_bridge.redis_state")

_redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
_redis_client = None

ACTIVE_CALL_TTL = 3600       # 1 hour
TRANSFER_REQUEST_TTL = 30    # 30 seconds
OVERFLOW_ALERT_TTL = 60      # 60 seconds
WARM_TRANSFER_TTL = 300      # 5 minutes


def _get_redis():
    """Get or create a Redis connection for call state."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            _redis_url,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
            decode_responses=True,
        )
    return _redis_client


# ── active_calls (sync API for Flask) ────────────────────────────────────────

def set_active_call(call_sid: str, data: dict) -> None:
    """Store active call data in Redis."""
    r = _get_redis()
    pipe = r.pipeline()
    pipe.set(f"call:{call_sid}", json.dumps(data, default=str), ex=ACTIVE_CALL_TTL)
    # Track call_sid by location for efficient location-based queries
    location_id = data.get('_location_id', '')
    if location_id:
        loc_key = f"call_sids_by_loc:{location_id}"
        pipe.sadd(loc_key, call_sid)
        pipe.expire(loc_key, ACTIVE_CALL_TTL)
    pipe.execute()


def get_active_call(call_sid: str) -> dict | None:
    """Get active call data from Redis. Returns None if not found."""
    r = _get_redis()
    raw = r.get(f"call:{call_sid}")
    return json.loads(raw) if raw else None


_LUA_UPDATE_CALL = """
local key = KEYS[1]
local ttl = tonumber(ARGV[1])
local raw = redis.call('GET', key)
if not raw then return nil end
local data = cjson.decode(raw)
for i = 2, #ARGV, 2 do
    data[ARGV[i]] = cjson.decode(ARGV[i + 1])
end
redis.call('SET', key, cjson.encode(data), 'EX', ttl)
return 1
"""
_lua_update_call_script = None


def _get_lua_update_call():
    global _lua_update_call_script
    if _lua_update_call_script is None:
        _lua_update_call_script = _get_redis().register_script(_LUA_UPDATE_CALL)
    return _lua_update_call_script


def update_active_call(call_sid: str, **fields) -> None:
    """Atomically update specific fields on an active call via Lua script.
    No-op if call doesn't exist. Thread-safe — eliminates GET+SET race condition
    when concurrent Twilio status callbacks update the same call record."""
    if not fields:
        return
    r = _get_redis()
    # Build ARGV: [ttl, key1, json_val1, key2, json_val2, ...]
    argv = [str(ACTIVE_CALL_TTL)]
    for k, v in fields.items():
        argv.append(k)
        argv.append(json.dumps(v, default=str))
    try:
        _get_lua_update_call()(keys=[f"call:{call_sid}"], args=argv, client=r)
    except Exception:
        # Lua not supported (e.g. Redis Cluster shard redirect) — fall back to GET+SET
        raw = r.get(f"call:{call_sid}")
        if not raw:
            return
        data = json.loads(raw)
        data.update(fields)
        r.set(f"call:{call_sid}", json.dumps(data, default=str), ex=ACTIVE_CALL_TTL)


def delete_active_call(call_sid: str) -> None:
    """Remove an active call from Redis."""
    r = _get_redis()
    raw = r.get(f"call:{call_sid}")
    if raw:
        data = json.loads(raw)
        location_id = data.get('_location_id', '')
        if location_id:
            r.srem(f"call_sids_by_loc:{location_id}", call_sid)
    r.delete(f"call:{call_sid}")


def call_exists(call_sid: str) -> bool:
    """Check if a call exists in Redis without deserializing."""
    r = _get_redis()
    return r.exists(f"call:{call_sid}") > 0


def get_active_calls_for_location(location_id: str) -> dict:
    """Get all active calls for a location. Returns {call_sid: data}."""
    r = _get_redis()
    loc_key = f"call_sids_by_loc:{location_id}"
    call_sids = r.smembers(loc_key)
    if not call_sids:
        return {}
    result = {}
    pipe = r.pipeline()
    for sid in call_sids:
        pipe.get(f"call:{sid}")
    values = pipe.execute()
    for sid, raw in zip(call_sids, values):
        if raw:
            data = json.loads(raw)
            # Only include if location matches (defensive)
            if data.get('_location_id', '') == location_id:
                result[sid] = data
        else:
            # Call expired — clean up stale set member
            r.srem(loc_key, sid)
    return result


def get_all_active_calls() -> dict:
    """Get ALL active calls across all locations. Returns {call_sid: data}.
    Used by operations that need to iterate all calls (e.g., voice_tools transfer lookup).
    """
    r = _get_redis()
    result = {}
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor, match="call:*", count=100)
        if keys:
            pipe = r.pipeline()
            for key in keys:
                pipe.get(key)
            values = pipe.execute()
            for key, raw in zip(keys, values):
                if raw:
                    sid = key.replace("call:", "", 1)
                    result[sid] = json.loads(raw)
        if cursor == 0:
            break
    return result


# ── transfer_requests (sync API for Flask) ───────────────────────────────────

def set_transfer_request(call_sid: str, data: dict) -> None:
    """Store a transfer/takeover request in Redis."""
    r = _get_redis()
    r.set(f"xfer:{call_sid}", json.dumps(data, default=str), ex=TRANSFER_REQUEST_TTL)


def get_transfer_request(call_sid: str) -> dict | None:
    """Get a transfer request from Redis."""
    r = _get_redis()
    raw = r.get(f"xfer:{call_sid}")
    return json.loads(raw) if raw else None


def delete_transfer_request(call_sid: str) -> None:
    """Remove a transfer request from Redis."""
    r = _get_redis()
    r.delete(f"xfer:{call_sid}")


def transfer_request_exists(call_sid: str) -> bool:
    """Check if a transfer request exists."""
    r = _get_redis()
    return r.exists(f"xfer:{call_sid}") > 0


# ── overflow_transfer_alerts (sync API for Flask) ────────────────────────────

def add_overflow_alert(location_id: str, alert: dict) -> None:
    """Add an overflow transfer alert for a location."""
    r = _get_redis()
    key = f"overflow:{location_id}"
    r.rpush(key, json.dumps(alert, default=str))
    r.expire(key, OVERFLOW_ALERT_TTL)


def get_overflow_alerts(location_id: str) -> list:
    """Get all overflow alerts for a location."""
    r = _get_redis()
    key = f"overflow:{location_id}"
    raw_list = r.lrange(key, 0, -1)
    return [json.loads(item) for item in raw_list] if raw_list else []


def set_overflow_alerts(location_id: str, alerts: list) -> None:
    """Replace all overflow alerts for a location (used for pruning)."""
    r = _get_redis()
    key = f"overflow:{location_id}"
    pipe = r.pipeline()
    pipe.delete(key)
    for alert in alerts:
        pipe.rpush(key, json.dumps(alert, default=str))
    if alerts:
        pipe.expire(key, OVERFLOW_ALERT_TTL)
    pipe.execute()


# ── warm_transfer state (sync API for Flask) ─────────────────────────────────

def set_warm_transfer(call_sid: str, data: dict) -> None:
    """Store warm transfer state in Redis."""
    r = _get_redis()
    r.set(f"warmxfer:{call_sid}", json.dumps(data, default=str), ex=WARM_TRANSFER_TTL)


def get_warm_transfer(call_sid: str) -> dict | None:
    """Get warm transfer state from Redis."""
    r = _get_redis()
    raw = r.get(f"warmxfer:{call_sid}")
    return json.loads(raw) if raw else None


def update_warm_transfer(call_sid: str, **fields) -> None:
    """Update specific fields on an existing warm transfer state."""
    data = get_warm_transfer(call_sid)
    if data is None:
        return
    data.update(fields)
    set_warm_transfer(call_sid, data)


def delete_warm_transfer(call_sid: str) -> None:
    """Remove warm transfer state from Redis."""
    r = _get_redis()
    r.delete(f"warmxfer:{call_sid}")


# ── Async API (for FastAPI — wraps sync via asyncio.to_thread) ───────────────

async def async_set_active_call(call_sid: str, data: dict) -> None:
    await asyncio.to_thread(set_active_call, call_sid, data)


async def async_get_active_call(call_sid: str) -> dict | None:
    return await asyncio.to_thread(get_active_call, call_sid)


async def async_update_active_call(call_sid: str, **fields) -> None:
    await asyncio.to_thread(update_active_call, call_sid, **fields)


async def async_delete_active_call(call_sid: str) -> None:
    await asyncio.to_thread(delete_active_call, call_sid)


async def async_call_exists(call_sid: str) -> bool:
    return await asyncio.to_thread(call_exists, call_sid)


async def async_get_transfer_request(call_sid: str) -> dict | None:
    return await asyncio.to_thread(get_transfer_request, call_sid)


async def async_set_transfer_request(call_sid: str, data: dict) -> None:
    await asyncio.to_thread(set_transfer_request, call_sid, data)


async def async_delete_transfer_request(call_sid: str) -> None:
    await asyncio.to_thread(delete_transfer_request, call_sid)


async def async_transfer_request_exists(call_sid: str) -> bool:
    return await asyncio.to_thread(transfer_request_exists, call_sid)
