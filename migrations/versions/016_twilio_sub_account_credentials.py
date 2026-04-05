"""Add dedicated Twilio sub-account credential columns to subscribers.

Stores twilio_sub_account_sid (plaintext) and twilio_sub_account_auth_token
(encrypted via Fernet) as proper columns instead of buried in voice_config JSONB.
Trust Hub operations read from these columns — no more digging through JSONB
or accidentally falling back to master credentials.

Backfills from existing voice_config data for all provisioned subscribers.

Revision ID: 016_twilio_sub_acct_creds
Revises: 015_sms_consent
Create Date: 2026-04-05
"""
from typing import Sequence, Union
from alembic import op

revision: str = '016_twilio_sub_acct_creds'
down_revision: Union[str, None] = '015_sms_consent'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add dedicated columns
    op.execute("""
        ALTER TABLE subscribers
            ADD COLUMN IF NOT EXISTS twilio_sub_account_sid    TEXT,
            ADD COLUMN IF NOT EXISTS twilio_sub_account_auth_token TEXT;
    """)

    # Index for fast lookups by sub-account SID
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_subscribers_twilio_sub_account_sid
        ON subscribers (twilio_sub_account_sid)
        WHERE twilio_sub_account_sid IS NOT NULL;
    """)

    # Backfill from voice_config JSONB for existing provisioned subscribers.
    # Auth token stored unencrypted initially (same as voice_config).
    # Encryption will be applied by the application layer on next refresh.
    op.execute("""
        UPDATE subscribers
        SET twilio_sub_account_sid = voice_config->>'twilio_sub_account_sid',
            twilio_sub_account_auth_token = voice_config->>'twilio_auth_token'
        WHERE voice_config->>'twilio_sub_account_sid' IS NOT NULL
          AND voice_config->>'twilio_sub_account_sid' != ''
          AND twilio_sub_account_sid IS NULL;
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS idx_subscribers_twilio_sub_account_sid;
    """)
    op.execute("""
        ALTER TABLE subscribers
            DROP COLUMN IF EXISTS twilio_sub_account_sid,
            DROP COLUMN IF EXISTS twilio_sub_account_auth_token;
    """)
