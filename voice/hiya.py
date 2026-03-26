"""
Hiya Connect API integration for branded calling display.

Registers subscribers' businesses and phone numbers with Hiya so their
business name (and optionally logo) shows on the recipient's screen when
calling. Complements Twilio Voice Integrity (which handles spam label
remediation via AT&T/Hiya, T-Mobile/FirstOrion, Verizon/TNS).

Coverage when registered with Hiya Connect:
  - AT&T (spam scoring via Hiya — label removal + business name display)
  - Verizon (display partner)
  - T-Mobile (display partner)
  - Samsung phones globally (Hiya app preinstalled on ~200M Samsung devices)

Required env vars (Connect API):
  HIYA_CONNECT_APP_ID      — App ID from Hiya Connect console (developer.hiya.com)
  HIYA_CONNECT_APP_SECRET  — App Secret from Hiya Connect console

Optional env vars (Protect API — requires signed service agreement with Hiya):
  HIYA_PROTECT_APP_ID      — Protect API App ID
  HIYA_PROTECT_APP_SECRET  — Protect API App Secret

Authentication: HTTP Basic Auth (base64-encoded APP_ID:APP_SECRET) for
Connect and Protect APIs. The Audio Intelligence API (deepfake detection)
uses a separate Bearer token and is a different product.

Brand vetting: 48–72 hours after POST /v2/managed-brand. Phone numbers
can be submitted immediately; display activates once the brand is approved.
"""

import os
import base64
import logging

import requests

logger = logging.getLogger("voice_bridge.hiya")

HIYA_CONNECT_BASE = "https://connect.api.hiyaapi.com"
HIYA_PROTECT_BASE = "https://protect.api.hiyaapi.com"

# Loaded at import time; available once env vars are set on Railway
HIYA_CONNECT_APP_ID = os.getenv("HIYA_CONNECT_APP_ID", "")
HIYA_CONNECT_APP_SECRET = os.getenv("HIYA_CONNECT_APP_SECRET", "")
HIYA_PROTECT_APP_ID = os.getenv("HIYA_PROTECT_APP_ID", "")
HIYA_PROTECT_APP_SECRET = os.getenv("HIYA_PROTECT_APP_SECRET", "")

# Hiya-accepted industry values for brand registration
INDUSTRY_INSURANCE = "Insurance"
INDUSTRY_DEFAULT = "Professional Services"

# Hiya-accepted call purpose values
CALL_PURPOSE_SALES = "Sales"
CALL_PURPOSE_SERVICE = "Service Delivery"
CALL_PURPOSE_NOTIFICATIONS = "Notifications/Scheduling"


# ─── Credential helpers ────────────────────────────────────────────────────────

def is_connect_configured() -> bool:
    """Return True if Hiya Connect credentials are set in env."""
    return bool(HIYA_CONNECT_APP_ID and HIYA_CONNECT_APP_SECRET)


def is_protect_configured() -> bool:
    """Return True if Hiya Protect credentials are set in env."""
    return bool(HIYA_PROTECT_APP_ID and HIYA_PROTECT_APP_SECRET)


def _connect_headers() -> dict:
    creds = base64.b64encode(
        f"{HIYA_CONNECT_APP_ID}:{HIYA_CONNECT_APP_SECRET}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _protect_headers() -> dict:
    creds = base64.b64encode(
        f"{HIYA_PROTECT_APP_ID}:{HIYA_PROTECT_APP_SECRET}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _parse_e164(phone_e164: str) -> tuple[str, str]:
    """
    Split E.164 number into (country_code, national_number).
    '+15551234567' → ('1', '5551234567')
    """
    digits = phone_e164.lstrip("+")
    if len(digits) == 11 and digits.startswith("1"):
        return "1", digits[1:]
    # Fallback: assume US
    return "1", digits[-10:]


# ─── Connect API: Managed Brands ──────────────────────────────────────────────

def register_brand(
    business_name: str,
    contact_email: str,
    contact_name: str,
    contact_phone: str,
    website: str,
    street: str,
    city: str,
    state: str,
    zip_code: str,
    country: str = "US",
    industry: str = INDUSTRY_INSURANCE,
    call_purpose: str = CALL_PURPOSE_SALES,
) -> dict:
    """
    Register a business as a Managed Brand with Hiya Connect.

    Returns a dict with:
      brand_id    — Hiya brand ID (save this; needed to register numbers)
      status      — e.g. "PENDING_REVIEW", "VERIFIED"
      raw         — full Hiya API response

    Vetting takes 48–72 hours. Phone numbers can be submitted immediately
    with the brand_id; branded display activates once the brand is approved.
    """
    if not is_connect_configured():
        raise RuntimeError("Hiya Connect credentials not configured. Set HIYA_CONNECT_APP_ID and HIYA_CONNECT_APP_SECRET.")

    payload = {
        "businessName": business_name,
        "contactEmail": contact_email,
        "contactName": contact_name,
        "contactPhone": contact_phone,
        "website": website,
        "address": {
            "streetAddress": street,
            "city": city,
            "state": state,
            "postalCode": zip_code,
            "country": country,
        },
        "industry": industry,
        "callPurpose": call_purpose,
    }

    resp = requests.post(
        f"{HIYA_CONNECT_BASE}/v2/managed-brand",
        json=payload,
        headers=_connect_headers(),
        timeout=15,
    )

    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = ""
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500]
        raise RuntimeError(f"Hiya brand registration failed ({resp.status_code}): {body}") from e

    data = resp.json()
    # Hiya may use different field names — handle both camelCase and snake_case
    brand_id = (
        data.get("id")
        or data.get("brandId")
        or data.get("brand_id")
        or data.get("managedBrandId")
    )

    return {
        "brand_id": brand_id,
        "status": data.get("status", "PENDING_REVIEW"),
        "raw": data,
    }


def get_brand(brand_id: str) -> dict | None:
    """Fetch a specific brand by iterating the list (Hiya has no GET-by-id endpoint)."""
    if not is_connect_configured():
        return None

    resp = requests.get(
        f"{HIYA_CONNECT_BASE}/v2/managed-brand",
        params={"page": 1, "size": 100},
        headers=_connect_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    brands = data if isinstance(data, list) else data.get("content", data.get("brands", []))
    for b in brands:
        bid = b.get("id") or b.get("brandId") or b.get("brand_id")
        if bid == brand_id:
            return b
    return None


def list_brands(page: int = 1, size: int = 100) -> list:
    """List all registered Managed Brands."""
    if not is_connect_configured():
        return []

    resp = requests.get(
        f"{HIYA_CONNECT_BASE}/v2/managed-brand",
        params={"page": page, "size": size},
        headers=_connect_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("content", data.get("brands", []))


# ─── Connect API: Phone Numbers ────────────────────────────────────────────────

def register_number(
    phone_e164: str,
    brand_id: str,
    display_name: str,
    call_reason: str | None = None,
    logo_id: str | None = None,
    do_not_originate: bool = False,
    city: str | None = None,
    state: str | None = None,
    country: str = "US",
) -> dict:
    """
    Register a single phone number for branded calling display under a brand.

    phone_e164   — E.164 number, e.g. '+15551234567'
    brand_id     — Returned by register_brand()
    display_name — Business name to show (up to 15 chars for best compat)
    call_reason  — e.g. "Insurance Sales" (shown on screen where supported)
    logo_id      — Optional Hiya logo ID (from upload_logo())

    Returns the raw Hiya API response dict. Vetting takes 48–72 hours.
    """
    if not is_connect_configured():
        raise RuntimeError("Hiya Connect credentials not configured.")

    ccc, national = _parse_e164(phone_e164)

    payload = {
        "originatingPhone": phone_e164,
        "ccc": int(ccc),
        "national": national,
        "displayName": display_name,
        "managedBrand": brand_id,
        "doNotOriginate": do_not_originate,
    }
    if call_reason:
        payload["callReason"] = call_reason
    if logo_id:
        payload["logoId"] = logo_id
    if city:
        payload["city"] = city
    if state:
        payload["state"] = state
    if country:
        payload["country"] = country

    resp = requests.post(
        f"{HIYA_CONNECT_BASE}/v1/phone",
        json=payload,
        headers=_connect_headers(),
        timeout=15,
    )

    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = ""
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500]
        raise RuntimeError(f"Hiya number registration failed ({resp.status_code}) for {phone_e164}: {body}") from e

    try:
        return resp.json()
    except Exception:
        return {"status": resp.status_code, "phone": phone_e164}


def register_numbers_batch(
    phone_e164_list: list,
    brand_id: str,
    display_name: str,
    call_reason: str | None = None,
    logo_id: str | None = None,
    state: str | None = None,
    city: str | None = None,
) -> dict:
    """
    Register multiple phone numbers under a brand.
    Returns {"registered": [...], "failed": [...], "total": N}
    """
    registered = []
    failed = []

    for phone in phone_e164_list:
        try:
            result = register_number(
                phone_e164=phone,
                brand_id=brand_id,
                display_name=display_name,
                call_reason=call_reason,
                logo_id=logo_id,
                state=state,
                city=city,
            )
            registered.append({"phone": phone, "result": result})
            logger.info(f"[Hiya] Registered {phone} under brand {brand_id}")
        except Exception as e:
            failed.append({"phone": phone, "error": str(e)})
            logger.warning(f"[Hiya] Failed to register {phone}: {e}")

    return {
        "registered": registered,
        "failed": failed,
        "total": len(phone_e164_list),
    }


def delete_number(phone_e164: str) -> bool:
    """Remove a phone number from Hiya branded calling."""
    if not is_connect_configured():
        return False

    ccc, national = _parse_e164(phone_e164)
    resp = requests.delete(
        f"{HIYA_CONNECT_BASE}/v1/phone/{ccc}/{national}",
        headers=_connect_headers(),
        timeout=15,
    )
    return resp.status_code in (200, 204)


def get_registered_numbers(brand_id: str | None = None, page: int = 0, size: int = 100) -> list:
    """
    List all numbers registered for branded calling.
    Optionally filter by brand_id.
    """
    if not is_connect_configured():
        return []

    params = {"page": page, "size": size}
    if brand_id:
        params["managedBrand"] = brand_id

    resp = requests.get(
        f"{HIYA_CONNECT_BASE}/v1/phone",
        params=params,
        headers=_connect_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("content", data.get("phones", []))


# ─── Protect API: Number Reputation ───────────────────────────────────────────

def check_reputation(phone_e164_list: list) -> dict:
    """
    Query Hiya Protect for spam labeling status of phone numbers.

    Requires HIYA Protect credentials (signed service agreement with Hiya).
    Returns per-number reputation data including spamLabelingStatus and
    report card grades (maturity, desirability, engagement, reaction).

    spamLabelingStatus values:
      'flagged'    — actively labeled as spam
      'mixed_high' — inconsistent labeling, high spam rate
      'mixed_low'  — inconsistent labeling, low spam rate
      'unflagged'  — clean, no spam labels

    Max 100 numbers per call.
    """
    if not is_protect_configured():
        raise RuntimeError(
            "Hiya Protect credentials not configured. "
            "This API requires a signed service agreement — contact api@hiya.com."
        )

    # Hiya wants E.164 numbers URL-encoded; requests handles encoding
    phones = phone_e164_list[:100]

    resp = requests.get(
        f"{HIYA_PROTECT_BASE}/v1/business/reputation",
        params=[("phones", p) for p in phones],
        headers=_protect_headers(),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def register_business_protect(
    business_name: str,
    contact_email: str,
    website: str,
    street: str,
    city: str,
    state: str,
    zip_code: str,
    country: str = "US",
    duns_number: str | None = None,
) -> dict:
    """
    Register a business with Hiya Protect API for reputation management.
    Requires a signed service agreement with Hiya.
    """
    if not is_protect_configured():
        raise RuntimeError("Hiya Protect credentials not configured.")

    payload = {
        "businessName": business_name,
        "contactEmail": contact_email,
        "website": website,
        "address": {
            "streetAddress": street,
            "city": city,
            "state": state,
            "postalCode": zip_code,
            "country": country,
        },
    }
    if duns_number:
        payload["dunsNumber"] = duns_number

    resp = requests.post(
        f"{HIYA_PROTECT_BASE}/v1/business/business",
        json=payload,
        headers=_protect_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_protect_numbers(phones: list | None = None, job_name: str | None = None, page: int = 0) -> dict:
    """List numbers registered with Hiya Protect."""
    if not is_protect_configured():
        raise RuntimeError("Hiya Protect credentials not configured.")

    params = {"page": page}
    if phones:
        for p in phones:
            params.setdefault("phones", []).append(p)
    elif job_name:
        params["jobName"] = job_name

    resp = requests.get(
        f"{HIYA_PROTECT_BASE}/v1/business/phone",
        params=params,
        headers=_protect_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def register_numbers_protect(business_id: str, phone_e164_list: list) -> dict:
    """
    Register phone numbers with Hiya Protect under a registered business.
    Returns 202 (queued). Verification may take up to 1 day.
    """
    if not is_protect_configured():
        raise RuntimeError("Hiya Protect credentials not configured.")

    resp = requests.post(
        f"{HIYA_PROTECT_BASE}/v1/business/{business_id}/phone",
        json={"phones": phone_e164_list},
        headers=_protect_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"status": resp.status_code, "accepted": len(phone_e164_list)}
