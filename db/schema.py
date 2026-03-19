# db/schema.py — Database schema initialization via Alembic
#
# ALL schema is now defined in Alembic migrations (migrations/versions/).
# The legacy init_db() with 1,400 lines of raw DDL is no longer called.
#
# To create a new migration:
#   alembic revision -m "add column X to table Y"
#
# To apply pending migrations:
#   alembic upgrade head
#
# Production pre-deploy command (Railway):
#   python -m alembic upgrade head

import os
import logging

logger = logging.getLogger(__name__)


def init_db():
    """
    Initialize the database schema via Alembic migrations.

    Runs `alembic upgrade head` which applies all pending migrations.
    On a fresh database, this creates the entire schema (002_full_schema).
    On an existing database, this applies only new migrations.

    This replaces the legacy init_db() that had 1,400 lines of raw
    CREATE TABLE / ALTER TABLE statements.
    """
    try:
        from alembic.config import Config
        from alembic import command

        alembic_cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "alembic.ini"
        )
        if os.path.exists(alembic_cfg_path):
            alembic_cfg = Config(alembic_cfg_path)
            command.upgrade(alembic_cfg, "head")
            logger.info("Alembic migrations applied successfully")
        else:
            logger.error(f"alembic.ini not found at {alembic_cfg_path}")
    except ImportError:
        logger.warning(
            "Alembic not installed. Install with: pip install alembic. "
            "Schema will not be initialized."
        )
    except Exception as e:
        # Log but don't crash — the pre-deploy command should have already
        # applied migrations. This is a safety net, not the primary path.
        logger.error(f"Alembic migration failed: {e}", exc_info=True)


__all__ = ["init_db"]
