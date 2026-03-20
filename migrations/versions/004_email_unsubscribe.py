"""Add email_unsubscribed column to subscribers and agency_billing.

Tracks whether a user has opted out of marketing/reminder emails.
Used by email sending functions to suppress outbound emails for
users who have unsubscribed.

Revision ID: 004_email_unsubscribe
Revises: 003_performance_indexes
Create Date: 2026-03-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "004_email_unsubscribe"
down_revision: Union[str, None] = "003_performance_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE subscribers
        ADD COLUMN IF NOT EXISTS email_unsubscribed BOOLEAN DEFAULT FALSE
    """)

    op.execute("""
        ALTER TABLE agency_billing
        ADD COLUMN IF NOT EXISTS email_unsubscribed BOOLEAN DEFAULT FALSE
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE subscribers DROP COLUMN IF EXISTS email_unsubscribed")
    op.execute("ALTER TABLE agency_billing DROP COLUMN IF EXISTS email_unsubscribed")
