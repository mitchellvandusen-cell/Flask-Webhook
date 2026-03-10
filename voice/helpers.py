import json
import logging

from flask_login import current_user

from db import get_db_connection, return_db_connection
from voice.call_state import active_calls

logger = logging.getLogger("voice_bridge.helpers")


def _get_subscriber_by_phone(phone_number):
    """Look up subscriber whose voice_config.twilio_phone_number matches the given number."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        # Normalize: strip +1 prefix variants for matching
        normalized = phone_number.lstrip('+')
        if normalized.startswith('1') and len(normalized) == 11:
            normalized = normalized[1:]

        cur.execute("""
            SELECT * FROM subscribers
            WHERE voice_config IS NOT NULL
              AND voice_config->>'enabled' = 'true'
              AND (
                  REGEXP_REPLACE(voice_config->>'twilio_phone_number', '^\+?1', '') = %s
                  OR voice_config->>'twilio_phone_number' = %s
              )
            LIMIT 1
        """, (normalized, phone_number))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error looking up subscriber by number: {e}")
        return None
    finally:
        return_db_connection(conn)


def _get_subscriber_by_location(location_id):
    """Look up subscriber by location_id."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE location_id = %s LIMIT 1", (location_id,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error looking up subscriber by location: {e}")
        return None
    finally:
        return_db_connection(conn)


def _get_current_subscriber_voice():
    """Get subscriber, voice_config, and sub_account_sid for the logged-in user."""
    conn = get_db_connection()
    if not conn:
        return None, None, None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None, None, None
        subscriber = dict(row)
        vc = subscriber.get('voice_config') or {}
        sub_sid = vc.get('twilio_sub_account_sid', '')
        return subscriber, vc, sub_sid or None
    except Exception as e:
        logger.error(f"_get_current_subscriber_voice: {e}")
        return None, None, None
    finally:
        return_db_connection(conn)


def _verify_call_ownership(call_sid: str) -> bool:
    """Check that the call_sid belongs to the current user's location.
    Returns True if the call is owned by the current user, False otherwise."""
    info = active_calls.get(call_sid)
    if not info:
        return False
    call_location = info.get('_location_id', '')
    if not call_location:
        return True  # legacy entries without location_id — allow for backward compat
    subscriber, _, _ = _get_current_subscriber_voice()
    if not subscriber:
        return False
    return subscriber.get('location_id', '') == call_location


def _save_voice_config(email, voice_config):
    """Persist voice_config JSON for a subscriber."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE subscribers SET voice_config = %s WHERE email = %s",
            (json.dumps(voice_config), email)
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"Failed to save voice_config: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        return_db_connection(conn)
