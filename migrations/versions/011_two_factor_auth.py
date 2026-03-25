"""Add two-factor authentication columns to subscribers.

two_factor_enabled: whether 2FA is active for this user
two_factor_phone: verified phone number for SMS codes

Revision ID: 011_two_factor_auth
Revises: 010_consolidate_agency_billing
Create Date: 2026-03-25
"""
from typing import Sequence, Union
from alembic import op

revision: str = '011_two_factor_auth'
down_revision: Union[str, None] = '010_consolidate_agency_billing'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE subscribers
        ADD COLUMN IF NOT EXISTS two_factor_enabled BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS two_factor_phone TEXT
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE subscribers DROP COLUMN IF EXISTS two_factor_enabled")
    op.execute("ALTER TABLE subscribers DROP COLUMN IF EXISTS two_factor_phone")
