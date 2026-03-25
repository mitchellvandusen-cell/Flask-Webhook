"""Add company-level OAuth token columns to subscribers + clean up agency_billing.

Stores the GHL Company-scoped OAuth token on the subscriber row for
agency owners. The Company token enables cross-location API calls:
  - GET /locations/search (list all sub-accounts)
  - GET /users/ per location (list all users across the agency)

Also removes max_seats and active_seats from agency_billing since
seat management is handled separately via location_users.

Revision ID: 012_agency_company_token
Revises: 011_two_factor_auth
Create Date: 2026-03-25
"""
from typing import Sequence, Union
from alembic import op

revision: str = '012_agency_company_token'
down_revision: Union[str, None] = '011_two_factor_auth'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Company token on subscribers (agency owners)
    op.execute("""
        ALTER TABLE subscribers
        ADD COLUMN IF NOT EXISTS company_access_token TEXT,
        ADD COLUMN IF NOT EXISTS company_refresh_token TEXT,
        ADD COLUMN IF NOT EXISTS company_token_expires_at TIMESTAMPTZ;
    """)
    # Clean up agency_billing — seats handled by location_users now
    op.execute("""
        ALTER TABLE agency_billing
        DROP COLUMN IF EXISTS max_seats,
        DROP COLUMN IF EXISTS active_seats;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE subscribers
        DROP COLUMN IF EXISTS company_access_token,
        DROP COLUMN IF EXISTS company_refresh_token,
        DROP COLUMN IF EXISTS company_token_expires_at;
    """)
    op.execute("""
        ALTER TABLE agency_billing
        ADD COLUMN IF NOT EXISTS max_seats INTEGER DEFAULT 10,
        ADD COLUMN IF NOT EXISTS active_seats INTEGER DEFAULT 0;
    """)
