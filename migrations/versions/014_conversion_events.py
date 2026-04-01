"""Add conversion_events table for conversion analytics pipeline.

Tracks key funnel events (stage advances, bookings, objections, calls,
opt-outs) per contact per location.  All downstream analytics queries
read from this table — no LLM cost, pure SQL aggregation.

Revision ID: 014_conversion_events
Revises: 013_call_quality_metrics
Create Date: 2026-04-01
"""
from typing import Sequence, Union
from alembic import op

revision: str = '014_conversion_events'
down_revision: Union[str, None] = '013_call_quality_metrics'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS conversion_events (
            id          SERIAL PRIMARY KEY,
            location_id VARCHAR NOT NULL,
            contact_id  VARCHAR NOT NULL,
            event_type  VARCHAR NOT NULL,
            event_data  JSONB DEFAULT '{}',
            source      VARCHAR,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    # Query pattern: dashboard stats filtered by location + time range
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_conversion_events_location_created
        ON conversion_events (location_id, created_at);
    """)
    # Query pattern: per-contact event history
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_conversion_events_location_contact
        ON conversion_events (location_id, contact_id);
    """)
    # Query pattern: aggregate by event type
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_conversion_events_type
        ON conversion_events (event_type);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS conversion_events;")
