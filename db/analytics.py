# db/analytics.py — Conversion analytics data layer
#
# Provides event logging and aggregation queries for the conversion
# analytics pipeline.  All functions follow the project pattern:
# get_db_connection() / return_db_connection() in try/finally.

import json
import logging
from datetime import datetime, timedelta

from db_legacy import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# WRITE: Log a single conversion event
# ─────────────────────────────────────────────────────────────

def log_conversion_event(location_id, contact_id, event_type,
                         event_data=None, source=None):
    """Insert one row into conversion_events.

    Lightweight — designed to be sprinkled into hot paths (webhook
    pipeline, call status callbacks) without adding latency.
    """
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO conversion_events
                   (location_id, contact_id, event_type, event_data, source)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            location_id,
            contact_id,
            event_type,
            json.dumps(event_data or {}),
            source,
        ))
        conn.commit()
    except Exception as e:
        logger.debug(f"log_conversion_event failed: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            return_db_connection(conn)


# ─────────────────────────────────────────────────────────────
# READ: Aggregated conversion statistics
# ─────────────────────────────────────────────────────────────

def _period_start(period, tz_name='America/Chicago'):
    """Return a UTC-aware start timestamp for the given period name."""
    import pytz
    try:
        user_tz = pytz.timezone(tz_name)
    except Exception:
        user_tz = pytz.timezone('America/Chicago')

    now = datetime.now(user_tz)
    if period == 'today':
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == 'week':
        start = now - timedelta(days=7)
    elif period == 'month':
        start = now - timedelta(days=30)
    else:  # 'all'
        start = datetime(2000, 1, 1, tzinfo=pytz.utc)
        return start

    return start.astimezone(pytz.utc)


def get_conversion_stats(location_id, period='week', tz_name='America/Chicago'):
    """Return aggregated conversion metrics for a single location.

    Returns dict with:
      - booking_rate, objection_overcome_rate
      - stage funnel counts
      - avg messages/calls to booking
      - conversion by source
      - daily trend
    """
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        start = _period_start(period, tz_name)

        # ── Event counts by type ──
        cur.execute("""
            SELECT event_type, COUNT(*) AS cnt
            FROM conversion_events
            WHERE location_id = %s AND created_at >= %s
            GROUP BY event_type
        """, (location_id, start))
        counts = {r[0]: r[1] for r in cur.fetchall()}

        bookings_confirmed = counts.get('booking_confirmed', 0)
        bookings_attempted = counts.get('booking_attempted', 0)
        objections_detected = counts.get('objection_detected', 0)
        objections_overcome = counts.get('objection_overcome', 0)
        calls_connected = counts.get('call_connected', 0)
        calls_completed = counts.get('call_completed', 0)
        opt_outs = counts.get('opt_out', 0)
        stage_advances = counts.get('stage_advanced', 0)

        booking_rate = round(
            bookings_confirmed / bookings_attempted * 100, 1
        ) if bookings_attempted else 0.0

        objection_win_rate = round(
            objections_overcome / objections_detected * 100, 1
        ) if objections_detected else 0.0

        # ── Stage funnel (from stage_advanced events) ──
        cur.execute("""
            SELECT event_data->>'to_stage' AS stage, COUNT(*) AS cnt
            FROM conversion_events
            WHERE location_id = %s AND created_at >= %s
              AND event_type = 'stage_advanced'
            GROUP BY stage
            ORDER BY cnt DESC
        """, (location_id, start))
        stage_funnel = {r[0]: r[1] for r in cur.fetchall()}

        # ── Conversion by source ──
        cur.execute("""
            SELECT COALESCE(source, 'unknown') AS src, COUNT(*) AS cnt
            FROM conversion_events
            WHERE location_id = %s AND created_at >= %s
              AND event_type = 'booking_confirmed'
            GROUP BY src
        """, (location_id, start))
        conversion_by_source = {r[0]: r[1] for r in cur.fetchall()}

        # ── Objection type breakdown ──
        cur.execute("""
            SELECT event_data->>'objection_type' AS otype, COUNT(*) AS cnt
            FROM conversion_events
            WHERE location_id = %s AND created_at >= %s
              AND event_type = 'objection_detected'
              AND event_data->>'objection_type' IS NOT NULL
            GROUP BY otype
            ORDER BY cnt DESC
        """, (location_id, start))
        objection_breakdown = {r[0]: r[1] for r in cur.fetchall()}

        # ── Daily conversion trend ──
        cur.execute("""
            SELECT DATE(created_at AT TIME ZONE 'UTC' AT TIME ZONE %s) AS day,
                   COUNT(*) FILTER (WHERE event_type = 'booking_confirmed')   AS bookings,
                   COUNT(*) FILTER (WHERE event_type = 'booking_attempted')   AS attempts,
                   COUNT(*) FILTER (WHERE event_type = 'objection_detected')  AS objections,
                   COUNT(*) FILTER (WHERE event_type = 'call_connected')      AS calls,
                   COUNT(*) FILTER (WHERE event_type = 'opt_out')             AS opt_outs
            FROM conversion_events
            WHERE location_id = %s AND created_at >= %s
            GROUP BY day
            ORDER BY day
        """, (tz_name, location_id, start))
        daily = [
            {
                "day": str(r[0]),
                "bookings": r[1],
                "attempts": r[2],
                "objections": r[3],
                "calls": r[4],
                "opt_outs": r[5],
            }
            for r in cur.fetchall()
        ]

        # ── Avg messages to booking (contacts that booked) ──
        cur.execute("""
            SELECT AVG(msg_count)::float AS avg_msgs
            FROM (
                SELECT ce.contact_id,
                       COUNT(cm.*) AS msg_count
                FROM conversion_events ce
                LEFT JOIN contact_messages cm
                  ON cm.contact_id = ce.contact_id
                 AND cm.timestamp <= ce.created_at
                WHERE ce.location_id = %s
                  AND ce.created_at >= %s
                  AND ce.event_type = 'booking_confirmed'
                GROUP BY ce.contact_id
            ) sub
        """, (location_id, start))
        row = cur.fetchone()
        avg_messages_to_booking = round(row[0], 1) if row and row[0] else None

        cur.close()
        return {
            "period": period,
            "booking_rate": booking_rate,
            "bookings_confirmed": bookings_confirmed,
            "bookings_attempted": bookings_attempted,
            "objection_win_rate": objection_win_rate,
            "objections_detected": objections_detected,
            "objections_overcome": objections_overcome,
            "calls_connected": calls_connected,
            "calls_completed": calls_completed,
            "opt_outs": opt_outs,
            "stage_advances": stage_advances,
            "stage_funnel": stage_funnel,
            "conversion_by_source": conversion_by_source,
            "objection_breakdown": objection_breakdown,
            "daily_trend": daily,
            "avg_messages_to_booking": avg_messages_to_booking,
            "event_counts": counts,
        }
    except Exception as e:
        logger.error(f"get_conversion_stats failed: {e}")
        return {}
    finally:
        return_db_connection(conn)


def get_conversion_events(location_id, limit=100, offset=0, event_type=None):
    """Paginated event log for a single location."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        if event_type:
            cur.execute("""
                SELECT id, contact_id, event_type, event_data, source,
                       created_at
                FROM conversion_events
                WHERE location_id = %s AND event_type = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (location_id, event_type, limit, offset))
        else:
            cur.execute("""
                SELECT id, contact_id, event_type, event_data, source,
                       created_at
                FROM conversion_events
                WHERE location_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (location_id, limit, offset))
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "id": r[0],
                "contact_id": r[1],
                "event_type": r[2],
                "event_data": r[3] if isinstance(r[3], dict) else json.loads(r[3] or '{}'),
                "source": r[4],
                "created_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"get_conversion_events failed: {e}")
        return []
    finally:
        return_db_connection(conn)


def get_conversion_stats_multi(location_ids, period='week', tz_name='America/Chicago'):
    """Aggregated conversion stats across multiple locations (agency use)."""
    if not location_ids:
        return {}
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        start = _period_start(period, tz_name)
        placeholders = ','.join(['%s'] * len(location_ids))

        cur.execute(f"""
            SELECT event_type, COUNT(*) AS cnt
            FROM conversion_events
            WHERE location_id IN ({placeholders}) AND created_at >= %s
            GROUP BY event_type
        """, (*location_ids, start))
        counts = {r[0]: r[1] for r in cur.fetchall()}

        bookings_confirmed = counts.get('booking_confirmed', 0)
        bookings_attempted = counts.get('booking_attempted', 0)
        objections_detected = counts.get('objection_detected', 0)
        objections_overcome = counts.get('objection_overcome', 0)

        booking_rate = round(
            bookings_confirmed / bookings_attempted * 100, 1
        ) if bookings_attempted else 0.0

        objection_win_rate = round(
            objections_overcome / objections_detected * 100, 1
        ) if objections_detected else 0.0

        # Daily trend across all locations
        cur.execute(f"""
            SELECT DATE(created_at AT TIME ZONE 'UTC' AT TIME ZONE %s) AS day,
                   COUNT(*) FILTER (WHERE event_type = 'booking_confirmed')   AS bookings,
                   COUNT(*) FILTER (WHERE event_type = 'booking_attempted')   AS attempts,
                   COUNT(*) FILTER (WHERE event_type = 'objection_detected')  AS objections,
                   COUNT(*) FILTER (WHERE event_type = 'call_connected')      AS calls
            FROM conversion_events
            WHERE location_id IN ({placeholders}) AND created_at >= %s
            GROUP BY day
            ORDER BY day
        """, (tz_name, *location_ids, start))
        daily = [
            {"day": str(r[0]), "bookings": r[1], "attempts": r[2],
             "objections": r[3], "calls": r[4]}
            for r in cur.fetchall()
        ]

        # Per-agent (location) breakdown
        cur.execute(f"""
            SELECT location_id,
                   COUNT(*) FILTER (WHERE event_type = 'booking_confirmed') AS bookings,
                   COUNT(*) FILTER (WHERE event_type = 'booking_attempted') AS attempts,
                   COUNT(*) FILTER (WHERE event_type = 'objection_detected') AS objections,
                   COUNT(*) FILTER (WHERE event_type = 'call_connected') AS calls
            FROM conversion_events
            WHERE location_id IN ({placeholders}) AND created_at >= %s
            GROUP BY location_id
        """, (*location_ids, start))
        per_agent = [
            {"location_id": r[0], "bookings": r[1], "attempts": r[2],
             "objections": r[3], "calls": r[4]}
            for r in cur.fetchall()
        ]

        cur.close()
        return {
            "period": period,
            "booking_rate": booking_rate,
            "bookings_confirmed": bookings_confirmed,
            "bookings_attempted": bookings_attempted,
            "objection_win_rate": objection_win_rate,
            "objections_detected": objections_detected,
            "objections_overcome": objections_overcome,
            "event_counts": counts,
            "daily_trend": daily,
            "per_agent": per_agent,
        }
    except Exception as e:
        logger.error(f"get_conversion_stats_multi failed: {e}")
        return {}
    finally:
        return_db_connection(conn)


__all__ = [
    "log_conversion_event",
    "get_conversion_stats",
    "get_conversion_events",
    "get_conversion_stats_multi",
]
