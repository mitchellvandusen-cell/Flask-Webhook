"""Baseline migration — stamps existing schema as the starting point.

This migration does NOT create any tables. The existing init_db() function
has already created all 30+ tables via CREATE TABLE IF NOT EXISTS. This
migration simply marks the current state as the Alembic baseline.

All FUTURE schema changes must be added as new Alembic migrations,
not as ALTER TABLE statements in init_db().

Revision ID: 001_baseline
Revises: None
Create Date: 2026-03-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Nothing to do — existing schema was created by init_db().

    This migration exists solely to establish the Alembic version baseline.
    All 30+ tables (subscribers, agency_billing, contact_messages, etc.)
    already exist in the database from prior init_db() runs.

    IMPORTANT: After this baseline is applied, ALL new schema changes
    must go through Alembic migrations. Do not add new ALTER TABLE
    statements to init_db().
    """
    pass


def downgrade() -> None:
    """Cannot downgrade past the baseline — this is the beginning of history."""
    raise RuntimeError(
        "Cannot downgrade past the baseline migration. "
        "The baseline represents the schema state before Alembic was adopted."
    )
