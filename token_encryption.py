# token_encryption.py — AES encryption for OAuth tokens at rest
#
# Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography library.
# Tokens are encrypted before DB writes and decrypted on reads.
#
# Setup:
#   1. pip install cryptography
#   2. Generate a key:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   3. Set TOKEN_ENCRYPTION_KEY=<key> in .env
#
# If TOKEN_ENCRYPTION_KEY is not set, encryption is bypassed (passthrough mode)
# so existing plaintext tokens continue to work during migration.

import os
import logging

logger = logging.getLogger(__name__)

_fernet = None
_ENCRYPTION_ENABLED = False

try:
    from cryptography.fernet import Fernet, InvalidToken

    _key = os.getenv("TOKEN_ENCRYPTION_KEY")
    if _key:
        _fernet = Fernet(_key.encode() if isinstance(_key, str) else _key)
        _ENCRYPTION_ENABLED = True
        logger.info("Token encryption enabled (Fernet AES-128-CBC)")
    else:
        logger.warning(
            "TOKEN_ENCRYPTION_KEY not set — tokens stored in plaintext. "
            "Generate a key: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
except ImportError:
    logger.warning(
        "cryptography library not installed — token encryption disabled. "
        "Run: pip install cryptography"
    )

    class InvalidToken(Exception):
        pass


# Encrypted tokens are prefixed so we can distinguish them from plaintext
# during migration (existing tokens are plaintext, new ones are encrypted).
_ENC_PREFIX = "enc::"


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
        logger.error("Encrypted token found but TOKEN_ENCRYPTION_KEY not set — cannot decrypt")
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
