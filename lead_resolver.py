# lead_resolver.py — Smart lead type detection
# Combines GHL tags + date imported + custom fields to determine true lead freshness.
# Tags alone are unreliable (a "fresh" tag from 90 days ago is lying).

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_lead_type(
    tags: list | None = None,
    date_added: str | None = None,
    custom_fields: list | None = None,
    source: str | None = None,
) -> dict:
    """
    Determine the true lead type by cross-referencing tags with actual dates.

    Returns:
        {
            "lead_type": "fresh" | "aged" | "re-engage" | "very-old" | "default",
            "lead_vendor": str or "",
            "days_since_import": int or None,
            "confidence": "high" | "medium" | "low",
            "reason": str,  # human-readable explanation for logging
        }
    """
    tags = tags or []
    custom_fields = custom_fields or []

    # --- Extract lead_vendor from custom fields ---
    lead_vendor = _extract_lead_vendor(custom_fields)

    # --- Parse tag signal ---
    tag_signal = _parse_tag_signal(tags)

    # --- Calculate days since import ---
    days = _days_since(date_added)

    # --- Cross-reference tag + date for true lead type ---
    lead_type, confidence, reason = _resolve(tag_signal, days)

    result = {
        "lead_type": lead_type,
        "lead_vendor": lead_vendor,
        "days_since_import": days,
        "confidence": confidence,
        "reason": reason,
    }

    logger.info(
        f"LeadResolver | type={lead_type} | conf={confidence} | days={days} | "
        f"tag_signal={tag_signal} | vendor={lead_vendor or 'none'} | reason={reason}"
    )

    return result


# ── Tag parsing ──────────────────────────────────────────────────────────────

def _parse_tag_signal(tags: list) -> str:
    """
    Read all tags and determine the tag-based signal.
    Priority: re-engage > fresh > aged > none
    (re-engage is always intentional, so it wins over stale tags)
    """
    if not tags:
        return "none"

    tags_lower = " ".join(str(t).lower() for t in tags)

    # re-engage is always an explicit agent action, highest priority
    if "re-engage" in tags_lower or "reengage" in tags_lower or "re engage" in tags_lower:
        return "re-engage"
    if "fresh" in tags_lower:
        return "fresh"
    if "aged" in tags_lower:
        return "aged"

    return "none"


# ── Date calculation ─────────────────────────────────────────────────────────

def _days_since(date_str: str | None) -> int | None:
    """Parse a GHL date string and return days since that date, or None."""
    if not date_str or not str(date_str).strip():
        return None

    date_str = str(date_str).strip()

    # GHL sends various formats: ISO 8601, with/without timezone
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",       # 2026-01-15T14:30:00.000Z
        "%Y-%m-%dT%H:%M:%SZ",           # 2026-01-15T14:30:00Z
        "%Y-%m-%dT%H:%M:%S.%f%z",       # 2026-01-15T14:30:00.000+00:00
        "%Y-%m-%dT%H:%M:%S%z",          # 2026-01-15T14:30:00+00:00
        "%Y-%m-%dT%H:%M:%S",            # 2026-01-15T14:30:00
        "%Y-%m-%d",                      # 2026-01-15
    ):
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - dt
            return max(0, delta.days)
        except ValueError:
            continue

    logger.debug(f"LeadResolver | Could not parse date: {date_str}")
    return None


# ── Core resolution logic ────────────────────────────────────────────────────

def _resolve(tag_signal: str, days: int | None) -> tuple[str, str, str]:
    """
    Cross-reference tag signal with actual import age.
    Returns (lead_type, confidence, reason).
    """
    # ── re-engage tag: always trust it ──
    # An agent explicitly tagged this for re-engagement
    if tag_signal == "re-engage":
        return ("re-engage", "high", "agent tagged for re-engagement")

    # ── Have both tag and date: cross-reference ──
    if days is not None and tag_signal != "none":

        if tag_signal == "fresh":
            if days <= 3:
                return ("fresh", "high", f"fresh tag confirmed by import {days}d ago")
            elif days <= 30:
                return ("fresh", "medium", f"fresh tag but imported {days}d ago, likely still relevant")
            elif days <= 90:
                return ("aged", "medium", f"fresh tag is stale, imported {days}d ago, treating as aged")
            else:
                return ("very-old", "high", f"fresh tag is very stale, imported {days}d ago")

        if tag_signal == "aged":
            if days <= 7:
                return ("aged", "high", f"recently purchased aged lead, imported {days}d ago")
            elif days <= 90:
                return ("aged", "medium", f"aged lead sitting in CRM for {days}d")
            else:
                return ("very-old", "high", f"aged lead imported {days}d ago, very old now")

    # ── Have tag but no date: trust tag with lower confidence ──
    if tag_signal != "none" and days is None:
        if tag_signal == "fresh":
            return ("fresh", "low", "fresh tag present but no date to verify")
        if tag_signal == "aged":
            return ("aged", "low", "aged tag present but no date to verify")

    # ── Have date but no tag: infer from age alone ──
    if days is not None and tag_signal == "none":
        if days <= 3:
            return ("fresh", "medium", f"no tag but imported {days}d ago, likely fresh")
        elif days <= 30:
            return ("default", "low", f"no tag, imported {days}d ago, could be fresh or aged")
        elif days <= 90:
            return ("aged", "medium", f"no tag, imported {days}d ago, treating as aged")
        else:
            return ("very-old", "medium", f"no tag, imported {days}d ago, very old")

    # ── Neither tag nor date: default ──
    return ("default", "low", "no tag and no date available")


# ── Custom field extraction ──────────────────────────────────────────────────

# Common GHL custom field names for lead vendor/source (case-insensitive)
_VENDOR_FIELD_NAMES = {
    "lead_vendor", "leadvendor", "lead vendor",
    "vendor", "lead_source", "leadsource", "lead source",
    "lead_provider", "leadprovider", "lead provider",
}


def _extract_lead_vendor(custom_fields: list) -> str:
    """
    Extract lead vendor from GHL custom fields array.
    GHL custom fields are [{id, value, fieldKey?}, ...].
    """
    if not custom_fields:
        return ""

    for cf in custom_fields:
        if not isinstance(cf, dict):
            continue

        # Check fieldKey (snake_case name GHL assigns)
        field_key = str(cf.get("fieldKey") or cf.get("field_key") or cf.get("key") or "").lower().strip()
        # Check human-readable name if available
        field_name = str(cf.get("name") or "").lower().strip()

        if field_key in _VENDOR_FIELD_NAMES or field_name in _VENDOR_FIELD_NAMES:
            value = str(cf.get("value") or "").strip()
            if value:
                return value

    return ""
