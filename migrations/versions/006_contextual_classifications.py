"""Add context_text column to learned_classifications for contextual classification memory.

Stores the conversational context (last 2 messages before the classified message)
so TF-IDF can distinguish ambiguous words like "okay" in different conversational
positions. Same message with different context = different classification.

Revision ID: 006_contextual_classifications
Revises: 005_learned_classifications
Create Date: 2026-03-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "006_contextual_classifications"
down_revision: Union[str, None] = "005_learned_classifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE learned_classifications
        ADD COLUMN IF NOT EXISTS context_text TEXT
    """)

    # Partial index for fast lookup of contextual entries
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_lc_context
        ON learned_classifications (message_hash)
        WHERE context_text IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_lc_context")
    op.execute("ALTER TABLE learned_classifications DROP COLUMN IF EXISTS context_text")
