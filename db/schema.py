# db/schema.py — Database schema initialization & Alembic migration runner
#
# In production, schema changes should go through Alembic migrations.
# The init_db() function is preserved for backward compatibility but
# new schema changes MUST use Alembic (see migrations/ directory).
#
# To create a new migration:
#   alembic revision --autogenerate -m "description"
#
# To apply pending migrations:
#   alembic upgrade head

import os
import logging
from db_legacy import init_db as _legacy_init_db

logger = logging.getLogger(__name__)


def init_db():
    """
    Initialize the database schema.

    Calls the legacy init_db() which runs CREATE TABLE IF NOT EXISTS
    and ALTER TABLE ADD COLUMN statements. This is preserved for
    backward compatibility during the Alembic transition.

    Once all environments are on Alembic, this function will be replaced
    with: alembic.command.upgrade(alembic_cfg, "head")
    """
    _legacy_init_db()

    # Run any pending Alembic migrations after legacy init
    try:
        from alembic.config import Config
        from alembic import command

        alembic_cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
        if os.path.exists(alembic_cfg_path):
            alembic_cfg = Config(alembic_cfg_path)
            command.upgrade(alembic_cfg, "head")
            logger.info("Alembic migrations applied successfully")
    except ImportError:
        logger.debug("Alembic not installed, skipping migration check")
    except Exception as e:
        logger.warning(f"Alembic migration check failed (non-fatal): {e}")


__all__ = ["init_db"]
