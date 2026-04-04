"""Add SMS consent tracking columns to subscribers.

Stores explicit opt-in consent for receiving SMS messages from Omnisconn.
Required for A2P 10DLC campaign registration proof-of-consent documentation.

Revision ID: 015_sms_consent
Revises: 014_conversion_events
Create Date: 2026-04-03
"""
from typing import Sequence, Union
from alembic import op

revision: str = '015_sms_consent'
down_revision: Union[str, None] = '014_conversion_events'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE subscribers
            ADD COLUMN IF NOT EXISTS sms_consent_at  TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS sms_consent_ip  VARCHAR(45);
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE subscribers
            DROP COLUMN IF EXISTS sms_consent_at,
            DROP COLUMN IF EXISTS sms_consent_ip;
    """)
