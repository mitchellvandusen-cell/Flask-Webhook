"""Add quality_metrics JSONB column to call_history for voice AI quality monitoring.

Stores per-call quality data: TTFA (Time To First Audio), response latency,
turn counts, and per-turn timing breakdowns. Enables tracking voice AI
responsiveness and identifying latency issues.

Revision ID: 013_call_quality_metrics
Revises: 012_agency_company_token
Create Date: 2026-04-01
"""
from typing import Sequence, Union
from alembic import op

revision: str = '013_call_quality_metrics'
down_revision: Union[str, None] = '012_agency_company_token'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # JSONB column for flexible quality metrics storage
    op.execute("""
        ALTER TABLE call_history
        ADD COLUMN IF NOT EXISTS quality_metrics JSONB DEFAULT NULL;
    """)
    # Index for querying calls with quality data (partial index — only rows that have it)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_call_history_quality_metrics
        ON call_history (location_id, created_at DESC)
        WHERE quality_metrics IS NOT NULL;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_call_history_quality_metrics")
    op.execute("ALTER TABLE call_history DROP COLUMN IF EXISTS quality_metrics")
