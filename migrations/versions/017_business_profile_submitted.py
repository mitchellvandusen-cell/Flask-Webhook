"""Add business_profile_submitted boolean to subscribers.

Explicit gate for onboarding wizard — replaces the fragile
voice_config.trust_hub.business_name check.  Backfills TRUE for
every subscriber that already has a non-empty business_name in
their trust_hub JSONB.

Revision ID: 017_biz_profile_submitted
Revises: 016_twilio_sub_acct_creds
Create Date: 2026-04-06
"""
from typing import Sequence, Union
from alembic import op

revision: str = '017_biz_profile_submitted'
down_revision: Union[str, None] = '016_twilio_sub_acct_creds'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE subscribers
            ADD COLUMN IF NOT EXISTS business_profile_submitted
                BOOLEAN NOT NULL DEFAULT FALSE;
    """)

    # Backfill: anyone who already completed the profile gets TRUE
    op.execute("""
        UPDATE subscribers
        SET business_profile_submitted = TRUE
        WHERE voice_config->'trust_hub'->>'business_name' IS NOT NULL
          AND TRIM(voice_config->'trust_hub'->>'business_name') != '';
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE subscribers
            DROP COLUMN IF EXISTS business_profile_submitted;
    """)
