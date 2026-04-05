import json
import logging

from flask_login import current_user

from db import get_db_connection, return_db_connection
from voice.call_state import get_active_call

logger = logging.getLogger("voice_bridge.helpers")


def _get_subscriber_by_phone(phone_number):
    """Look up subscriber whose voice_config.twilio_phone_number matches the given number.
    Also checks location_users for seat user phone numbers."""
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
                  REGEXP_REPLACE(voice_config->>'twilio_phone_number', '^\\+?1', '') = %s
                  OR voice_config->>'twilio_phone_number' = %s
              )
            LIMIT 1
        """, (normalized, phone_number))
        row = cur.fetchone()
        if row:
            cur.close()
            return dict(row)

        # Also check seat users in location_users
        try:
            cur.execute("""
                SELECT lu.*, s.access_token, s.refresh_token, s.token_expires_at,
                       s.bot_first_name, s.timezone, s.subscription_tier, s.sms_send_via
                FROM location_users lu
                JOIN subscribers s ON s.location_id = lu.location_id
                WHERE lu.voice_config IS NOT NULL
                  AND lu.voice_activated = true
                  AND lu.is_active = true
                  AND (
                      REGEXP_REPLACE(lu.voice_config->>'twilio_phone_number', '^\\+?1', '') = %s
                      OR lu.voice_config->>'twilio_phone_number' = %s
                  )
                LIMIT 1
            """, (normalized, phone_number))
            seat_row = cur.fetchone()
            cur.close()
            if seat_row:
                result = dict(seat_row)
                result['_is_seat_user'] = True
                return result
        except Exception:
            pass

        cur.close()
        return None
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
    """Get subscriber, voice_config, and sub_account_sid for the logged-in user.
    Supports both regular subscribers and seat users (location_users table)."""
    conn = get_db_connection()
    if not conn:
        return None, None, None
    try:
        cur = conn.cursor()

        # Check if seat user first — they have their own voice_config
        if getattr(current_user, 'is_seat_user', False) and getattr(current_user, 'seat_user_id', None):
            cur.execute("""
                SELECT lu.voice_config, lu.location_id, lu.email, lu.full_name,
                       s.access_token, s.refresh_token, s.token_expires_at,
                       s.bot_first_name, s.timezone, s.subscription_tier,
                       s.sms_send_via,
                       s.twilio_sub_account_sid, s.twilio_sub_account_auth_token
                FROM location_users lu
                JOIN subscribers s ON s.location_id = lu.location_id
                WHERE lu.id = %s
            """, (current_user.seat_user_id,))
            row = cur.fetchone()
            cur.close()
            if not row:
                return None, None, None
            subscriber = dict(row)
            vc = subscriber.get('voice_config') or {}
            # Prefer dedicated column over voice_config JSONB
            sub_sid = subscriber.get('twilio_sub_account_sid') or vc.get('twilio_sub_account_sid', '')
            # Inject dedicated auth token for Trust Hub callers
            dedicated_token = subscriber.get('twilio_sub_account_auth_token') or ''
            if dedicated_token and dedicated_token != vc.get('twilio_auth_token', ''):
                vc['twilio_auth_token'] = dedicated_token
            return subscriber, vc, sub_sid or None

        cur.execute("SELECT * FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None, None, None
        subscriber = dict(row)
        vc = subscriber.get('voice_config') or {}
        # Prefer dedicated column over voice_config JSONB
        sub_sid = subscriber.get('twilio_sub_account_sid') or vc.get('twilio_sub_account_sid', '')
        # Inject dedicated auth token into voice_config for Trust Hub callers
        dedicated_token = subscriber.get('twilio_sub_account_auth_token') or ''
        if dedicated_token and dedicated_token != vc.get('twilio_auth_token', ''):
            vc['twilio_auth_token'] = dedicated_token
        return subscriber, vc, sub_sid or None
    except Exception as e:
        logger.error(f"_get_current_subscriber_voice: {e}")
        return None, None, None
    finally:
        return_db_connection(conn)


def _verify_call_ownership(call_sid: str) -> bool:
    """Check that the call_sid belongs to the current user's location.
    Returns True if the call is owned by the current user, False otherwise."""
    info = get_active_call(call_sid)
    if not info:
        return False
    call_location = info.get('_location_id', '')
    if not call_location:
        return True  # legacy entries without location_id — allow for backward compat
    subscriber, _, _ = _get_current_subscriber_voice()
    if not subscriber:
        return False
    return subscriber.get('location_id', '') == call_location


def _save_voice_config(email, voice_config, seat_user_id=None):
    """Persist voice_config JSON for a subscriber or seat user."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        if seat_user_id:
            cur.execute(
                "UPDATE location_users SET voice_config = %s WHERE id = %s",
                (json.dumps(voice_config), seat_user_id)
            )
        else:
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
