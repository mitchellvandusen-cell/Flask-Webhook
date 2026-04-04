# utils.py
from datetime import datetime, date, time
from typing import Any
import re
import os
import hmac as _hmac
import hashlib
import base64
import logging

_ghl_sig_log = logging.getLogger("ghl.webhook.verify")

# GHL's published public keys — static, safe to hardcode.
# Source: https://marketplace.gohighlevel.com/docs/webhook/WebhookIntegrationGuide
#
# ED25519 — for X-GHL-Signature (current, required after July 1 2026)
_GHL_ED25519_PEM = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg=
-----END PUBLIC KEY-----"""

# RSA-SHA256 — for X-WH-Signature (legacy, deprecated July 1 2026)
_GHL_RSA_PEM = b"""-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAokvo/r9tVgcfZ5DysOSC
Frm602qYV0MaAiNnX9O8KxMbiyRKWeL9JpCpVpt4XHIcBOK4u3cLSqJGOLaPuXw6
dO0t6Q/ZVdAV5Phz+ZtzPL16iCGeK9po6D6JHBpbi989mmzMryUnQJezlYJ3DVfB
csedpinheNnyYeFXolrJvcsjDtfAeRx5ByHQmTnSdFUzuAnC9/GepgLT9SM4nCpv
uxmZMxrJt5Rw+VUaQ9B8JSvbMPpez4peKaJPZHBbU3OdeCVx5klVXXZQGNHOs8gF
3kvoV5rTnXV0IknLBXlcKKAQLZcY/Q9rG6Ifi9c+5vqlvHPCUJFT5XUGG5RKgOKU
J062fRtN+rLYZUV+BjafxQauvC8wSWeYja63VSUruvmNj8xkx2zE/Juc+yjLjTXp
IocmaiFeAO6fUtNjDeFVkhf5LNb59vECyrHD2SQIrhgXpO4Q3dVNA5rw576PwTzN
h/AMfHKIjE4xQA1SZuYJmNnmVZLIZBlQAF9Ntd03rfadZ+yDiOXCCs9FkHibELhC
HULgCsnuDJHcrGNd5/Ddm5hxGQ0ASitgHeMZ0kcIOwKDOzOU53lDza6/Y09T7sYJ
PQe7z0cvj7aE4B+Ax1ZoZGPzpJlZtGXCsu9aTEGEnKzmsFqwcSsnw3JB31IGKAyk
T1hhTiaCeIY/OwwwNUY2yvcCAwEAAQ==
-----END PUBLIC KEY-----"""


def verify_ghl_webhook_signature(request) -> bool | None:
    """
    Verify GHL webhook authenticity using GHL's published public keys.

    GHL signs webhook bodies with their private key; we verify with their public key.
    This is asymmetric signing — NOT HMAC with a shared secret.

    Priority order:
      1. ED25519 via X-GHL-Signature  (current — only header after July 1 2026)
      2. RSA-SHA256 via X-WH-Signature (legacy — deprecated July 1 2026)

    Returns:
        True  — signature present and valid, proceed
        False — signature present but INVALID, caller should return 401
        None  — no signature header present, caller decides
                (safe default: allow and log debug)
    """
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from cryptography.exceptions import InvalidSignature

    body = request.get_data()  # raw bytes — same for both checks

    # ── 1. ED25519 (current, required after July 1 2026) ──────────────────
    ed_sig = request.headers.get("X-GHL-Signature", "")
    if ed_sig:
        try:
            pub_key   = load_pem_public_key(_GHL_ED25519_PEM)
            sig_bytes = base64.b64decode(ed_sig)
            pub_key.verify(sig_bytes, body)
            return True
        except InvalidSignature:
            _ghl_sig_log.warning("GHL ED25519 (X-GHL-Signature) mismatch — rejecting")
            return False
        except Exception as exc:
            _ghl_sig_log.error("GHL ED25519 verification error: %s", exc)
            return False

    # ── 2. RSA-SHA256 (legacy, deprecated July 1 2026) ────────────────────
    rsa_sig = request.headers.get("X-WH-Signature", "")
    if rsa_sig:
        try:
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives import hashes
            pub_key   = load_pem_public_key(_GHL_RSA_PEM)
            sig_bytes = base64.b64decode(rsa_sig)
            pub_key.verify(sig_bytes, body, padding.PKCS1v15(), hashes.SHA256())
            return True
        except InvalidSignature:
            _ghl_sig_log.warning("GHL RSA (X-WH-Signature) mismatch — rejecting")
            return False
        except Exception as exc:
            _ghl_sig_log.error("GHL RSA verification error: %s", exc)
            return False

    # No verifiable signature header present
    return None
def make_json_serializable(obj: Any) -> Any:
    """Convert datetime objects to ISO strings for JSON serialization"""
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat() if obj else None
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_serializable(item) for item in obj]
    return obj

def clean_ai_reply(text):
    if not text:
        return ""
    
    # 1. Replace Em-dashes (—) and En-dashes (–) with a comma + space
    text = re.sub(r'[—–]', ', ', text)
    
    # 2. Replace " - " (hyphen with spaces) with comma + space
    # We do this specifically so we don't break words like "long-term"
    text = text.replace(' - ', ', ')
    
    # 3. Clean up any accidental double spaces or double commas created
    text = re.sub(r'\s+', ' ', text).strip()
    text = text.replace(',,', ',')
    text = text.replace(' ,', ',')
    
    return text