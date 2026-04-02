# message_utils.py — Message batching and processing utilities
#
# Handles combining rapid-fire lead messages into a single coherent message
# so the bot responds to the full context rather than each fragment separately.

import logging

from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)


def collect_unanswered_lead_messages(contact_id: str, current_message: str) -> str:
    """
    Collects all consecutive unanswered lead messages (sent within the last 60s)
    and combines them into one message string.

    This handles the case where a lead sends 3 rapid messages:
      "hey" / "yeah im looking" / "for my wife and kids"
    Instead of responding to each separately, we combine them:
      "hey. yeah im looking. for my wife and kids"

    Returns the combined message (or original if only one message).
    """
    conn = get_db_connection()
    if not conn:
        return current_message

    try:
        cur = conn.cursor()
        # Get recent lead messages that have no bot reply after them.
        # We look at the last 60 seconds of lead messages, walking backward
        # until we hit a bot message (which means everything before it was already answered).
        cur.execute("""
            SELECT message_type, message_text, created_at
            FROM contact_messages
            WHERE contact_id = %s
              AND created_at >= NOW() - INTERVAL '120 seconds'
            ORDER BY created_at DESC
            LIMIT 10
        """, (contact_id,))
        rows = cur.fetchall()

        if not rows:
            return current_message

        # Collect consecutive lead messages from the end (most recent first)
        unanswered = []
        for row in rows:
            msg_type = row['message_type'] if isinstance(row, dict) else row[0]
            msg_text = row['message_text'] if isinstance(row, dict) else row[1]
            if msg_type == 'lead':
                unanswered.append(msg_text.strip())
            else:
                # Hit a bot message — everything before this was already answered
                break

        if len(unanswered) <= 1:
            return current_message

        # Reverse to chronological order and combine
        unanswered.reverse()
        combined = ". ".join(unanswered)
        logger.info(f"📦 BATCHED {len(unanswered)} lead messages into one | contact={contact_id} | combined='{combined[:100]}'")
        return combined

    except Exception as e:
        logger.error(f"Message batching failed for {contact_id}: {e}")
        return current_message
    finally:
        if 'cur' in locals():
            cur.close()
        if conn:
            return_db_connection(conn)
