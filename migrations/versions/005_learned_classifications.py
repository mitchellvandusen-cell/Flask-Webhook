"""Add learned_classifications table for embedding-based classification memory.

Stores message embeddings + their objection/stage classifications so the system
learns from every conversation and gets smarter over time. Uses pgvector for
fast similarity search when available, falls back to JSONB + Python cosine.

Revision ID: 005_learned_classifications
Revises: 004_email_unsubscribe
Create Date: 2026-03-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "005_learned_classifications"
down_revision: Union[str, None] = "004_email_unsubscribe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Try to enable pgvector (graceful if not available)
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception:
        pass

    op.execute("""
        CREATE TABLE IF NOT EXISTS learned_classifications (
            id SERIAL PRIMARY KEY,
            message_hash TEXT NOT NULL,
            message_text TEXT NOT NULL,
            embedding JSONB,
            objection_type TEXT NOT NULL,
            objection_nature TEXT,
            stage TEXT,
            confidence FLOAT DEFAULT 0.7,
            location_id TEXT,
            source TEXT DEFAULT 'llm',
            confirmation_count INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_lc_hash ON learned_classifications (message_hash)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_lc_confidence ON learned_classifications (confidence)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_lc_type ON learned_classifications (objection_type)
    """)

    # pgvector column (embedding_vec) is created dynamically at runtime
    # by classification_memory._ensure_vector_column() after the first
    # embedding API call reveals the actual dimensions. This avoids
    # hardcoding a dimension that may not match the model's output.


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS learned_classifications")
