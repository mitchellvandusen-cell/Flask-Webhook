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
        from alembic.script import ScriptDirectory

        alembic_cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "alembic.ini"
        )
        if os.path.exists(alembic_cfg_path):
            alembic_cfg = Config(alembic_cfg_path)

            # Debug: log what Alembic sees before running
            script = ScriptDirectory.from_config(alembic_cfg)
            heads = script.get_heads()
            all_revs = [rev.revision for rev in script.walk_revisions()]
            logger.info(f"Alembic: script head(s)={heads}, all revisions={all_revs}")

            # Check current DB version using psycopg2 directly (avoids circular import)
            try:
                import psycopg2
                db_url = os.getenv("DATABASE_URL")
                if db_url:
                    dbg_conn = psycopg2.connect(db_url)
                    dbg_cur = dbg_conn.cursor()
                    dbg_cur.execute("SELECT version_num FROM alembic_version")
                    db_versions = [r[0] for r in dbg_cur.fetchall()]
                    # Also check if 003 indexes exist
                    dbg_cur.execute("SELECT indexname FROM pg_indexes WHERE indexname = 'idx_call_history_loc_phone'")
                    idx_exists = dbg_cur.fetchone() is not None
                    dbg_cur.close()
                    dbg_conn.close()
                    logger.info(f"Alembic: DB version(s)={db_versions}, 003 index exists={idx_exists}")
            except Exception as dbg_err:
                logger.info(f"Alembic: could not read DB version: {dbg_err}")

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
