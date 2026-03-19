"""Add performance indexes flagged by code review.

Addresses specific bottlenecks identified during scalability audit:

1. workflow_runs(status, next_execute_at) — Prevents full table scan in
   process_pending_delays() cron job. Without this, every 60-second cron
   cycle does a sequential scan as workflow_runs grows.

2. call_history(location_id, phone) — Required for _check_cooldown_and_daily_max()
   which runs on every dial attempt. Without this, dialer cooldown checks
   do a full table scan of call_history.

3. call_history(location_id, created_at DESC) — Required for predictive stats
   queries that look at 7-day call history for Erlang-C calculations.

4. webhook_logs(location_id, status) — Required for get_token_failed_webhook_logs()
   which scans for auth failures during recovery.

5. contact_messages(contact_id, created_at DESC) — Required for loading
   conversation history in the AI pipeline (last 20-30 messages).

Revision ID: 003_performance_indexes
Revises: 002_full_schema
Create Date: 2026-03-19
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003_performance_indexes"
down_revision: Union[str, None] = "002_full_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Workflow cron job: SELECT ... WHERE status = 'running' AND next_execute_at <= NOW()
    # Without this: full table scan every 60 seconds as workflow_runs grows
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wf_runs_status_next_exec
        ON workflow_runs (status, next_execute_at)
        WHERE status = 'running'
    """)

    # Dialer cooldown check: SELECT ... WHERE location_id = %s AND phone = ANY(%s)
    # Without this: full scan of call_history on every multi-dial batch
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_call_history_loc_phone
        ON call_history (location_id, phone)
    """)

    # Predictive stats: SELECT ... WHERE location_id = %s AND created_at > NOW() - 7 days
    # Without this: full scan for Erlang-C calculations
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_call_history_loc_created
        ON call_history (location_id, created_at DESC)
    """)

    # Webhook recovery: SELECT ... WHERE status LIKE '%auth%' AND location_id = %s
    # Without this: full scan during failed webhook recovery cron
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_webhook_logs_loc_status
        ON webhook_logs (location_id, status)
    """)

    # AI pipeline: SELECT ... WHERE contact_id = %s ORDER BY created_at DESC LIMIT 30
    # Without this: sequential scan on contact_messages for every webhook
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_contact_messages_contact_created
        ON contact_messages (contact_id, created_at DESC)
    """)

    # GHL action loops cron: SELECT ... WHERE status = 'active' AND next_execute_at <= NOW()
    # Composite index for the cron job that processes pending action loops
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_ghl_loops_active_next
        ON ghl_action_loops (status, next_execute_at)
        WHERE status = 'active'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_wf_runs_status_next_exec")
    op.execute("DROP INDEX IF EXISTS idx_call_history_loc_phone")
    op.execute("DROP INDEX IF EXISTS idx_call_history_loc_created")
    op.execute("DROP INDEX IF EXISTS idx_webhook_logs_loc_status")
    op.execute("DROP INDEX IF EXISTS idx_contact_messages_contact_created")
    op.execute("DROP INDEX IF EXISTS idx_ghl_loops_active_next")
