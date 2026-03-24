"""Normalize timezones + add stripe_status to subscribers.

1. Replace spaces with underscores in timezone columns (pytz requires
   'America/New_York' not 'America/New York').
2. Add stripe_status column to subscribers — agency_billing already has
   this but subscribers was missing it. Needed so Stripe webhook can
   record cancellation/past_due status without destroying stripe_customer_id.

Revision ID: 009_normalize_timezones
Revises: 008_support_actions_log
Create Date: 2026-03-24
"""
from typing import Sequence, Union
from alembic import op

revision: str = '009_normalize_timezones'
down_revision: Union[str, None] = '008_support_actions_log'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fix timezone values with spaces
    op.execute("""
        UPDATE subscribers
        SET timezone = REPLACE(timezone, ' ', '_')
        WHERE timezone LIKE '% %'
    """)
    op.execute("""
        UPDATE agency_billing
        SET timezone = REPLACE(timezone, ' ', '_')
        WHERE timezone LIKE '% %'
    """)

    # Add stripe_status to subscribers (agency_billing already has it)
    op.execute("""
        ALTER TABLE subscribers
        ADD COLUMN IF NOT EXISTS stripe_status TEXT
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE subscribers DROP COLUMN IF EXISTS stripe_status")
