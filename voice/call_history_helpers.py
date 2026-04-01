import json
import logging

from db import get_db_connection, return_db_connection

logger = logging.getLogger("voice_bridge.call_history_helpers")


def save_call_to_history(location_id, call_sid, phone, contact_id=None,
                         contact_name=None, direction='outbound', status='initiated',
                         from_number=None):
    """Save a new call record to the call_history table.

    Args:
        from_number: The outbound caller ID (our number). Critical for tracking
                     Voice Insights signals per caller ID in number_health scoring.
    """
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO call_history (location_id, contact_id, contact_name, phone,
                                      direction, call_sid, status, from_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (call_sid) DO NOTHING
        """, (location_id, contact_id, contact_name, phone, direction, call_sid, status,
              from_number))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to save call history: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def update_call_history_status(call_sid, status, duration=0, stir_status=None):
    """Update call status, duration, and STIR/SHAKEN attestation in call_history."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        ended_clause = ", ended_at = NOW()" if status in ('completed', 'busy', 'no-answer', 'failed', 'canceled') else ""
        stir_clause = ", stir_status = %s" if stir_status else ""
        params = [status, int(duration or 0)]
        if stir_status:
            params.append(stir_status)
        params.append(call_sid)
        cur.execute(f"""
            UPDATE call_history
            SET status = %s, duration = %s{stir_clause}{ended_clause}
            WHERE call_sid = %s
        """, params)
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to update call history: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def mark_ring_confirmed(call_sid):
    """Mark a call as ring-confirmed (SIP 180 received — lead's phone is legitimately ringing)."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("UPDATE call_history SET ring_confirmed = TRUE WHERE call_sid = %s", (call_sid,))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.debug(f"Failed to mark ring_confirmed for {call_sid}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def save_call_quality_metrics(call_sid, metrics_dict):
    """Save quality metrics JSONB to call_history.

    Args:
        call_sid: The Twilio call SID.
        metrics_dict: Dict with avg_ttfa_ms, max_ttfa_ms, min_ttfa_ms,
                      turn_count, avg_response_duration_ms, and turns list.
    """
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE call_history SET quality_metrics = %s WHERE call_sid = %s
        """, (json.dumps(metrics_dict), call_sid))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to save quality metrics for {call_sid}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def save_call_transcript(call_sid, transcript):
    """Save transcript JSON to call_history."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE call_history SET transcript = %s WHERE call_sid = %s
        """, (json.dumps(transcript), call_sid))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to save transcript: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)
