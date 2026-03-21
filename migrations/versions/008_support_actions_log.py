"""Add support_actions_log and support_log_cache tables.

support_actions_log: Audit trail for all write actions taken by the AI
support bot (Trust Hub resubmissions, CNAM updates, etc.). Every action
is logged with who, what, params, result, and whether the user consented.

support_log_cache: Rolling cache of Railway server logs and per-user
error logs for the support bot to read when diagnosing issues. Auto-
cleaned by cron (7-day TTL).

Revision ID: 008_support_actions_log
Revises: 007_support_tickets
Create Date: 2026-03-21
"""
from typing import Sequence, Union

from alembic import op

revision: str = "008_support_actions_log"
down_revision: Union[str, None] = "007_support_tickets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Audit log for support bot write actions ──────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS support_actions_log (
            id SERIAL PRIMARY KEY,
            email TEXT,
            location_id TEXT,
            sub_account_sid TEXT,
            action_type TEXT NOT NULL,
            action_params JSONB,
            action_result JSONB,
            consent_message TEXT,
            success BOOLEAN DEFAULT true,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_support_actions_created
        ON support_actions_log (created_at DESC)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_support_actions_location
        ON support_actions_log (location_id)
    """)

    # ── Rolling log cache for support bot diagnostics ────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS support_log_cache (
            id SERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            location_id TEXT,
            log_content TEXT NOT NULL,
            captured_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_support_log_cache_source
        ON support_log_cache (source, captured_at DESC)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_support_log_cache_location
        ON support_log_cache (location_id, captured_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_support_log_cache_location")
    op.execute("DROP INDEX IF EXISTS idx_support_log_cache_source")
    op.execute("DROP TABLE IF EXISTS support_log_cache")
    op.execute("DROP INDEX IF EXISTS idx_support_actions_location")
    op.execute("DROP INDEX IF EXISTS idx_support_actions_created")
    op.execute("DROP TABLE IF EXISTS support_actions_log")
