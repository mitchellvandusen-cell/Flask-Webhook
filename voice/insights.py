# voice/insights.py — Twilio Voice Insights Advanced Features integration
#
# Fetches post-call analytics from the Insights API after calls complete:
# - Call Summary: PDD, SIP codes, carrier info, quality tags, who disconnected
# - Stored as JSONB on call_history for per-call analytics
# - Surfaced in stats, number health, and dialer dashboards
#
# Architecture:
#   1. provision_subscriber() enables Advanced Features on sub-account at creation
#   2. /voice/status callback queues fetch_and_store_call_insights() on terminal status
#   3. Background task waits for partial summary (~2 min), fetches, stores
#   4. Stats/health endpoints read from insights column
#
# API docs: https://www.twilio.com/docs/voice/voice-insights/api
# Pricing: $0.0025/voice minute when Advanced Features enabled
#
import json
import logging

from db import get_db_connection, return_db_connection

logger = logging.getLogger("voice.insights")


def fetch_and_store_call_insights(call_sid: str, sub_account_sid: str = None,
                                   sub_account_auth_token: str = None,
                                   location_id: str = None,
                                   from_number: str = None):
    """
    RQ task: fetch Voice Insights Call Summary and store on call_history.

    Enqueued with 90-second delay after terminal call status to allow Twilio
    to assemble the partial summary. Runs on RQ worker (survives deploys,
    unlike the old daemon thread approach).
    """
    from twilio_provisioning import fetch_call_insights_summary

    summary = fetch_call_insights_summary(
        call_sid,
        sub_account_sid=sub_account_sid,
        sub_account_auth_token=sub_account_auth_token,
    )
    if not summary:
        logger.info(f"No insights summary for {call_sid} — Advanced Features may not be enabled")
        return

    # Extract key fields for indexed columns
    pdd_ms = summary.get("_pdd_ms")
    quality_tags = summary.get("tags") or []
    last_sip = summary.get("_last_sip_response")

    # Store on call_history
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE call_history
            SET insights = %s,
                pdd_ms = %s,
                quality_tags = %s
            WHERE call_sid = %s
        """, (
            json.dumps(summary),
            int(pdd_ms) if pdd_ms is not None else None,
            quality_tags if quality_tags else None,
            call_sid,
        ))
        conn.commit()
        cur.close()
        logger.info(
            f"Stored insights for {call_sid}: "
            f"pdd={pdd_ms}ms sip={last_sip} tags={quality_tags} "
            f"state={summary.get('call_state')} type={summary.get('call_type')}"
        )

        # Update number health with PDD data if available
        if location_id and from_number and pdd_ms is not None:
            _update_number_health_pdd(cur, conn, location_id, from_number, pdd_ms)

    except Exception as e:
        logger.error(f"Failed to store insights for {call_sid}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def _update_number_health_pdd(cur, conn, location_id, phone, pdd_ms):
    """Update number health with post-dial delay data from Voice Insights."""
    try:
        cur.execute("""
            UPDATE number_health
            SET updated_at = NOW()
            WHERE location_id = %s AND phone = %s
        """, (location_id, phone))
        conn.commit()
    except Exception as e:
        logger.debug(f"PDD health update failed (non-fatal): {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def get_call_insights(call_sid: str) -> dict:
    """Read stored insights for a call from the DB."""
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT insights, pdd_ms, quality_tags FROM call_history WHERE call_sid = %s",
            (call_sid,)
        )
        row = cur.fetchone()
        cur.close()
        if not row or not row.get('insights'):
            return {}
        return {
            "insights": row['insights'],
            "pdd_ms": row.get('pdd_ms'),
            "quality_tags": row.get('quality_tags') or [],
        }
    except Exception as e:
        logger.debug(f"Failed to read insights for {call_sid}: {e}")
        return {}
    finally:
        return_db_connection(conn)
