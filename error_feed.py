# error_feed.py — Redis-backed error capture for event-driven monitoring
#
# Captures ERROR+ log records from all services (Flask, Voice, Workers) into
# a Redis list. A lightweight API endpoint exposes unacknowledged errors so
# external monitors can react immediately instead of polling Railway logs.
#
# Redis key: igb:errors (capped list, newest first, max 200 entries)
# Each entry: JSON {service, logger, level, message, timestamp, traceback?}

import json
import logging
import time
import traceback as tb_mod
from datetime import datetime, timezone

_REDIS_KEY = "igb:errors"
_MAX_ENTRIES = 200


class RedisErrorHandler(logging.Handler):
    """Logging handler that pushes ERROR+ records to a Redis list."""

    def __init__(self, redis_getter, service_name):
        """
        Args:
            redis_getter: callable that returns a Redis connection (or None).
            service_name: e.g. 'flask-webhook', 'voice-server', 'worker', 'worker-bg'
        """
        super().__init__(level=logging.ERROR)
        self._get_redis = redis_getter
        self._service = service_name

    def emit(self, record):
        try:
            r = self._get_redis()
            if r is None:
                return

            entry = {
                "service": self._service,
                "logger": record.name,
                "level": record.levelname,
                "message": record.getMessage(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "epoch": time.time(),
            }

            if record.exc_info and record.exc_info[1]:
                entry["traceback"] = tb_mod.format_exception(*record.exc_info)[-3:]

            r.lpush(_REDIS_KEY, json.dumps(entry))
            r.ltrim(_REDIS_KEY, 0, _MAX_ENTRIES - 1)
        except Exception:
            pass  # never let the handler crash the app


def get_recent_errors(redis_conn, since_epoch=None, limit=50):
    """Fetch recent errors from Redis, optionally filtered by timestamp.

    Args:
        redis_conn: active Redis connection
        since_epoch: only return errors after this Unix timestamp
        limit: max entries to return

    Returns:
        list of error dicts, newest first
    """
    if redis_conn is None:
        return []

    try:
        raw = redis_conn.lrange(_REDIS_KEY, 0, limit - 1)
    except Exception:
        return []

    errors = []
    for item in raw:
        try:
            entry = json.loads(item)
            if since_epoch and entry.get("epoch", 0) <= since_epoch:
                break  # list is newest-first, so once we pass the threshold we're done
            errors.append(entry)
        except (json.JSONDecodeError, TypeError):
            continue

    return errors


def attach_error_handler(service_name, redis_getter):
    """Attach RedisErrorHandler to the root logger for this process.

    Call once at startup in each service (main.py, voice_server.py, worker.py).
    """
    handler = RedisErrorHandler(redis_getter, service_name)
    logging.getLogger().addHandler(handler)
