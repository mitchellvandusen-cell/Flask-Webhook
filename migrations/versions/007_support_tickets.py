"""Add support_tickets table for AI support bot issue tracking.

Stores customer-reported issues auto-created by the AI support bot,
with conversation logs, severity classification, and admin review workflow.

Revision ID: 007_support_tickets
Revises: 006_contextual_classifications
Create Date: 2026-03-21
"""
from typing import Sequence, Union

from alembic import op

revision: str = "007_support_tickets"
down_revision: Union[str, None] = "006_contextual_classifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            id SERIAL PRIMARY KEY,
            email TEXT,
            location_id TEXT,
            issue_summary TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'technical',
            severity TEXT NOT NULL DEFAULT 'medium',
            conversation_log JSONB,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            reviewed_at TIMESTAMPTZ,
            resolved_at TIMESTAMPTZ,
            admin_notes TEXT
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_support_tickets_status
        ON support_tickets (status)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_support_tickets_severity
        ON support_tickets (severity)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_support_tickets_created
        ON support_tickets (created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_support_tickets_created")
    op.execute("DROP INDEX IF EXISTS idx_support_tickets_severity")
    op.execute("DROP INDEX IF EXISTS idx_support_tickets_status")
    op.execute("DROP TABLE IF EXISTS support_tickets")
