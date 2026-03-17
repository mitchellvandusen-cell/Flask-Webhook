import json
import os
import logging
from datetime import datetime, timedelta

import pytz
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from ghl_auth import jwt_or_session_required

from db import get_db_connection, return_db_connection
from voice.helpers import _get_current_subscriber_voice

logger = logging.getLogger("voice_bridge.stats")

stats_bp = Blueprint('voice_stats', __name__)


@stats_bp.route('/voice/stats')
@jwt_or_session_required
def get_dialer_stats():
    """Return aggregated call statistics for the current user's dialer."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, timezone FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        location_id = row['location_id']

        # Use the subscriber's configured timezone (falls back to Chicago)
        tz_name = row.get('timezone') or 'America/Chicago'
        try:
            user_tz = pytz.timezone(tz_name)
        except Exception:
            user_tz = pytz.timezone('America/Chicago')



        period = request.args.get('period', 'month')
        now = datetime.now(user_tz)
        if period == 'today':
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'week':
            start_date = now - timedelta(days=7)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        else:
            start_date = datetime(2000, 1, 1, tzinfo=pytz.utc)

        # Convert start_date to UTC for SQL comparison (created_at is stored as UTC)
        if start_date.tzinfo is not None:
            start_date_utc = start_date.astimezone(pytz.utc)
        else:
            start_date_utc = pytz.utc.localize(start_date)

        # Core KPIs
        cur.execute("""
            SELECT
                COUNT(*)                                                      AS total_calls,
                COUNT(*) FILTER (WHERE direction = 'outbound')                AS outbound_calls,
                COUNT(*) FILTER (WHERE direction = 'inbound')                 AS inbound_calls,
                COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected_calls,
                COUNT(*) FILTER (WHERE ring_confirmed = TRUE)                 AS ring_confirmed_calls,
                COALESCE(AVG(duration) FILTER (WHERE duration > 0), 0)        AS avg_duration,
                COALESCE(SUM(duration), 0)                                    AS total_duration,
                COUNT(*) FILTER (WHERE duration >   6)                        AS over_6s,
                COUNT(*) FILTER (WHERE duration >=  60)                       AS over_1min,
                COUNT(*) FILTER (WHERE duration >= 120)                       AS over_2min,
                COUNT(*) FILTER (WHERE duration >= 300)                       AS over_5min,
                COUNT(*) FILTER (WHERE duration >= 600)                       AS over_10min,
                COUNT(DISTINCT contact_id)                                    AS unique_contacts,
                -- Voice Insights Advanced metrics
                COALESCE(AVG(pdd_ms) FILTER (WHERE pdd_ms IS NOT NULL), 0)    AS avg_pdd_ms,
                COUNT(*) FILTER (WHERE pdd_ms IS NOT NULL)                    AS calls_with_insights,
                COUNT(*) FILTER (WHERE pdd_ms > 6000)                         AS high_pdd_calls,
                COUNT(*) FILTER (WHERE quality_tags IS NOT NULL AND array_length(quality_tags, 1) > 0) AS tagged_calls
            FROM call_history
            WHERE location_id = %s AND created_at >= %s
        """, (location_id, start_date_utc))
        r = cur.fetchone()
        total           = r['total_calls'] or 0
        outbound        = r['outbound_calls'] or 0
        inbound         = r['inbound_calls'] or 0
        connected       = r['connected_calls'] or 0
        ring_confirmed  = r['ring_confirmed_calls'] or 0
        avg_pdd_ms      = float(r['avg_pdd_ms'] or 0)
        calls_with_insights = r['calls_with_insights'] or 0
        high_pdd_calls  = r['high_pdd_calls'] or 0
        tagged_calls    = r['tagged_calls'] or 0
        avg_dur         = float(r['avg_duration'] or 0)
        total_dur       = int(r['total_duration'] or 0)
        over_6s         = r['over_6s'] or 0
        over_1min       = r['over_1min'] or 0
        over_2min       = r['over_2min'] or 0
        over_5min       = r['over_5min'] or 0
        over_10min      = r['over_10min'] or 0
        unique_contacts = r['unique_contacts'] or 0
        connect_rate    = round(connected / total * 100, 1) if total else 0.0

        # Days in period (for "per day" averages)
        if period == 'today':
            days = 1
        elif period == 'week':
            days = 7
        elif period == 'month':
            days = 30
        else:
            cur.execute("SELECT MIN(created_at) AS first_call FROM call_history WHERE location_id = %s", (location_id,))
            first = cur.fetchone()['first_call']
            if first and first.tzinfo is None:
                first = pytz.utc.localize(first)
            days = max(1, (now - first).days) if first else 1

        # Prior period comparison (skip for 'all')
        prior = None
        if period != 'all':
            period_len  = now - start_date
            prior_end   = start_date_utc
            prior_start = start_date_utc - period_len
            cur.execute("""
                SELECT
                    COUNT(*)                                                      AS total_calls,
                    COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected_calls,
                    COALESCE(SUM(duration), 0)                                    AS total_duration,
                    COALESCE(AVG(duration) FILTER (WHERE duration > 0), 0)        AS avg_duration
                FROM call_history
                WHERE location_id = %s AND created_at >= %s AND created_at < %s
            """, (location_id, prior_start, prior_end))
            pr          = cur.fetchone()
            p_total     = pr['total_calls'] or 0
            p_connected = pr['connected_calls'] or 0
            p_dur       = int(pr['total_duration'] or 0)
            p_avg_dur   = float(pr['avg_duration'] or 0)
            p_rate      = round(p_connected / p_total * 100, 1) if p_total else 0.0

            def _pct_delta(curr, prev):
                if prev == 0:
                    return None
                return round((curr - prev) / prev * 100, 1)

            prior = {
                "total_calls":     p_total,
                "connected_calls": p_connected,
                "connect_rate":    p_rate,
                "total_duration":  p_dur,
                "avg_duration":    p_avg_dur,
                # % change for counts; absolute pp difference for rate
                "delta_calls":     _pct_delta(total, p_total),
                "delta_connected": _pct_delta(connected, p_connected),
                "delta_rate":      round(connect_rate - p_rate, 1),
                "delta_duration":  _pct_delta(total_dur, p_dur),
            }

        # Disposition breakdown
        dispositions = {}
        try:
            cur.execute("""
                SELECT
                    COALESCE(NULLIF(TRIM(disposition), ''), 'none') AS disp,
                    COUNT(*) AS cnt
                FROM call_history
                WHERE location_id = %s AND created_at >= %s
                  AND disposition IS NOT NULL AND TRIM(disposition) != ''
                GROUP BY disp
                ORDER BY cnt DESC
            """, (location_id, start_date_utc))
            dispositions = {row['disp']: row['cnt'] for row in cur.fetchall()}
        except Exception:
            conn.rollback()

        # Daily call volume with talk time (in subscriber's local timezone)
        cur.execute("""
            SELECT DATE(created_at AT TIME ZONE 'UTC' AT TIME ZONE %s) AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected,
                   COALESCE(SUM(duration), 0) AS total_secs
            FROM call_history
            WHERE location_id = %s AND created_at >= %s
            GROUP BY day ORDER BY day
        """, (tz_name, location_id, start_date_utc))
        daily = [
            {"day": str(row['day']), "calls": row['calls'], "connected": row['connected'], "total_secs": row['total_secs']}
            for row in cur.fetchall()
        ]

        # Hourly distribution (in subscriber's local timezone)
        cur.execute("""
            SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC' AT TIME ZONE %s)::int AS hr, COUNT(*) AS calls
            FROM call_history
            WHERE location_id = %s AND created_at >= %s
            GROUP BY hr ORDER BY hr
        """, (tz_name, location_id, start_date_utc))
        hourly_map = {row['hr']: row['calls'] for row in cur.fetchall()}
        hourly = [{"hour": h, "calls": hourly_map.get(h, 0)} for h in range(24)]

        # Top 5 most-called contacts
        cur.execute("""
            SELECT contact_id, contact_name, COUNT(*) AS cnt,
                   MAX(created_at) AS last_called
            FROM call_history
            WHERE location_id = %s AND created_at >= %s
            GROUP BY contact_id, contact_name
            ORDER BY cnt DESC LIMIT 5
        """, (location_id, start_date_utc))
        top_contacts = [
            {"id": row['contact_id'], "name": row['contact_name'] or "Unknown", "count": row['cnt'], "last_called": str(row['last_called'])}
            for row in cur.fetchall()
        ]

        # STIR/SHAKEN attestation breakdown
        stir_stats = {"A": 0, "B": 0, "C": 0, "none": 0}
        try:
            cur.execute("""
                SELECT COALESCE(NULLIF(TRIM(stir_status), ''), 'none') AS level,
                       COUNT(*) AS cnt
                FROM call_history
                WHERE location_id = %s AND created_at >= %s
                  AND direction = 'outbound'
                GROUP BY level
            """, (location_id, start_date_utc))
            for row in cur.fetchall():
                lvl = row['level'].upper() if row['level'] != 'none' else 'none'
                stir_stats[lvl] = stir_stats.get(lvl, 0) + row['cnt']
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        # Voice Insights quality tag breakdown
        quality_tag_breakdown = {}
        try:
            cur.execute("""
                SELECT tag, COUNT(*) AS cnt
                FROM call_history, unnest(quality_tags) AS tag
                WHERE location_id = %s AND created_at >= %s
                  AND quality_tags IS NOT NULL
                GROUP BY tag ORDER BY cnt DESC
            """, (location_id, start_date_utc))
            quality_tag_breakdown = {row['tag']: row['cnt'] for row in cur.fetchall()}
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        # Source breakdown from synced GHL conversations
        source_breakdown = {"dialer": total, "ghl_native": 0, "wavv": 0, "unknown": 0}
        try:
            cur.execute("""
                SELECT source, COUNT(*) AS cnt
                FROM crm_conversations
                WHERE location_id = %s AND message_type IN ('call', 'voicemail')
                  AND date_added >= %s::text
                GROUP BY source
            """, (location_id, start_date_utc.isoformat()))
            for row in cur.fetchall():
                src = row['source'] or 'unknown'
                if src in source_breakdown:
                    source_breakdown[src] += row['cnt']
                else:
                    source_breakdown[src] = row['cnt']
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        cur.close()
        return jsonify({
            "period":          period,
            "timezone":        tz_name,
            "total_calls":     total,
            "outbound_calls":  outbound,
            "inbound_calls":   inbound,
            "connected_calls": connected,
            "ring_confirmed":  ring_confirmed,
            "ring_rate":       round(ring_confirmed / total * 100, 1) if total else 0.0,
            "connect_rate":    connect_rate,
            "avg_duration":    round(avg_dur, 1),
            "total_duration":  total_dur,
            "over_6s":         over_6s,
            "over_1min":       over_1min,
            "over_2min":       over_2min,
            "over_5min":       over_5min,
            "over_10min":      over_10min,
            "unique_contacts": unique_contacts,
            "calls_per_day":   round(total / days, 1),
            "daily":           daily,
            "hourly":          hourly,
            "top_contacts":    top_contacts,
            "prior":           prior,
            "dispositions":    dispositions,
            "stir_attestation": stir_stats,
            "source_breakdown": source_breakdown,
            # Voice Insights Advanced metrics
            "voice_insights": {
                "avg_pdd_ms":         round(avg_pdd_ms, 0),
                "high_pdd_calls":     high_pdd_calls,
                "calls_with_insights": calls_with_insights,
                "tagged_calls":       tagged_calls,
                "quality_tags":       quality_tag_breakdown,
            },
        })
    except Exception as e:
        logger.error(f"get_dialer_stats failed: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)
