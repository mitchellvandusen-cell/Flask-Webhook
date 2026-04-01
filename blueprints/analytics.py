# blueprints/analytics.py — Conversion analytics API endpoints
#
# Routes:
#   GET /api/analytics/conversions   — Aggregated conversion stats
#   GET /api/analytics/funnel        — Stage progression funnel
#   GET /api/analytics/events        — Paginated event log

import logging

from flask import Blueprint, request, jsonify
from flask_login import current_user
from ghl_auth import jwt_or_session_required

from db import (get_db_connection, return_db_connection,
                get_conversion_stats, get_conversion_events)

logger = logging.getLogger(__name__)

analytics_bp = Blueprint('analytics', __name__)


@analytics_bp.route('/api/analytics/conversions')
@jwt_or_session_required
def conversion_stats():
    """Aggregated conversion metrics for the current user's location."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 503
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT location_id, timezone FROM subscribers WHERE email = %s",
            (current_user.email,)
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Not found"}), 404
        location_id = row[0]
        tz_name = row[1] or 'America/Chicago'
    except Exception as e:
        logger.error(f"conversion_stats subscriber lookup failed: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        return_db_connection(conn)

    period = request.args.get('period', 'week')
    stats = get_conversion_stats(location_id, period=period, tz_name=tz_name)
    return jsonify(stats)


@analytics_bp.route('/api/analytics/funnel')
@jwt_or_session_required
def conversion_funnel():
    """Stage progression funnel — how contacts move through stages."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 503
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT location_id, timezone FROM subscribers WHERE email = %s",
            (current_user.email,)
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Not found"}), 404
        location_id = row[0]
        tz_name = row[1] or 'America/Chicago'
    except Exception as e:
        logger.error(f"conversion_funnel lookup failed: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        return_db_connection(conn)

    period = request.args.get('period', 'week')
    stats = get_conversion_stats(location_id, period=period, tz_name=tz_name)
    # Return just the funnel-relevant data
    return jsonify({
        "period": period,
        "stage_funnel": stats.get("stage_funnel", {}),
        "booking_rate": stats.get("booking_rate", 0),
        "objection_win_rate": stats.get("objection_win_rate", 0),
        "objection_breakdown": stats.get("objection_breakdown", {}),
        "avg_messages_to_booking": stats.get("avg_messages_to_booking"),
    })


@analytics_bp.route('/api/analytics/events')
@jwt_or_session_required
def conversion_events():
    """Paginated conversion event log."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 503
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT location_id FROM subscribers WHERE email = %s",
            (current_user.email,)
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Not found"}), 404
        location_id = row[0]
    except Exception as e:
        logger.error(f"conversion_events lookup failed: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        return_db_connection(conn)

    limit = min(int(request.args.get('limit', 100)), 500)
    offset = int(request.args.get('offset', 0))
    event_type = request.args.get('type')

    events = get_conversion_events(
        location_id, limit=limit, offset=offset, event_type=event_type
    )
    return jsonify({"events": events, "limit": limit, "offset": offset})
