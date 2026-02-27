# token_encryption.py — AES encryption for OAuth tokens at rest
#
# Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography library.
# Tokens are encrypted before DB writes and decrypted on reads.
#
# Key management (zero-config for Railway):
#   1. If TOKEN_ENCRYPTION_KEY env var is set, use it directly.
#   2. Otherwise, check the app_settings DB table for a stored key.
#   3. If no key exists anywhere, auto-generate one and persist to DB.
#   4. Subsequent deployments reuse the DB-stored key automatically.
#
# This means: deploy to Railway, and encryption just works. No manual setup.

import os
import logging

logger = logging.getLogger(__name__)

_fernet = None
_ENCRYPTION_ENABLED = False
_ENC_PREFIX = "enc::"

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False
    logger.warning(
        "cryptography library not installed — token encryption disabled. "
        "Run: pip install cryptography"
    )

    class InvalidToken(Exception):
        pass


def _load_key_from_db():
    """Load the encryption key from the app_settings table."""
    try:
        from db import get_db_connection, return_db_connection
        conn = get_db_connection()
        if not conn:
            return None
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM app_settings WHERE key = 'token_encryption_key'")
            row = cur.fetchone()
            cur.close()
            if row:
                return row['value']
            return None
        finally:
            return_db_connection(conn)
    except Exception as e:
        logger.debug(f"Could not load encryption key from DB: {e}")
        return None


def _save_key_to_db(key_str):
    """Persist a newly generated encryption key to the app_settings table."""
    try:
        from db import get_db_connection, return_db_connection
        conn = get_db_connection()
        if not conn:
            logger.error("Cannot persist encryption key — no DB connection")
            return False
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO app_settings (key, value)
                VALUES ('token_encryption_key', %s)
                ON CONFLICT (key) DO NOTHING
            """, (key_str,))
            conn.commit()
            cur.close()
            logger.info("Encryption key persisted to app_settings table")
            return True
        finally:
            return_db_connection(conn)
    except Exception as e:
        logger.error(f"Failed to persist encryption key to DB: {e}")
        return False


def initialize_encryption():
    """Bootstrap encryption key from env var, DB, or auto-generate.
    Called once at app startup after DB is initialized."""
    global _fernet, _ENCRYPTION_ENABLED

    if not _HAS_CRYPTO:
        return

    # Priority 1: env var (explicit override)
    key_str = os.getenv("TOKEN_ENCRYPTION_KEY")
    key_source = "env"

    # Priority 2: DB-stored key (auto-persisted from previous deploy)
    if not key_str:
        key_str = _load_key_from_db()
        key_source = "database"

    # Priority 3: auto-generate and persist to DB
    if not key_str:
        key_str = Fernet.generate_key().decode()
        key_source = "auto-generated"
        logger.info("No encryption key found — generating new key automatically")
        _save_key_to_db(key_str)

    try:
        _fernet = Fernet(key_str.encode() if isinstance(key_str, str) else key_str)
        _ENCRYPTION_ENABLED = True
        logger.info(f"Token encryption enabled (Fernet AES-128-CBC, key source: {key_source})")
    except Exception as e:
        logger.error(f"Failed to initialize Fernet with {key_source} key: {e}")
        _ENCRYPTION_ENABLED = False


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token for storage. Returns prefixed ciphertext or plaintext if
    encryption is not configured."""
    if not plaintext:
        return plaintext
    if not _ENCRYPTION_ENABLED:
        return plaintext
    try:
        encrypted = _fernet.encrypt(plaintext.encode()).decode()
        return f"{_ENC_PREFIX}{encrypted}"
    except Exception as e:
        logger.error(f"Token encryption failed: {e}")
        return plaintext


def decrypt_token(stored: str) -> str:
    """Decrypt a token from storage. Handles both encrypted (prefixed) and
    legacy plaintext tokens transparently."""
    if not stored:
        return stored
    if not stored.startswith(_ENC_PREFIX):
        # Legacy plaintext token — return as-is
        return stored
    if not _ENCRYPTION_ENABLED:
        logger.error("Encrypted token found but encryption not initialized — cannot decrypt")
        return None
    try:
        ciphertext = stored[len(_ENC_PREFIX):]
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.error("Token decryption failed — key mismatch or corrupted ciphertext")
        return None
    except Exception as e:
        logger.error(f"Token decryption error: {e}")
        return None


def is_encryption_enabled() -> bool:
    """Check if token encryption is active."""
    return _ENCRYPTION_ENABLED
