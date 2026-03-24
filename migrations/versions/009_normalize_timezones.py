"""Normalize stored timezone strings: replace spaces with underscores.

Timezones like 'America/New York' are invalid in pytz — they must be
'America/New_York'. This migration fixes all existing bad values in
subscribers and agency_billing.

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


def downgrade() -> None:
    pass
