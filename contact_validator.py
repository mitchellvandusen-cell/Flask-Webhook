# contact_validator.py - Intelligent Contact ID Resolution
# When GHL sends incomplete data, this module cross-references multiple fields to find the correct contact

import os
import logging
import requests
from typing import Optional, Dict, Any
from db import get_db_connection, return_db_connection
from token_encryption import decrypt_token

logger = logging.getLogger(__name__)

GHL_API_BASE = "https://services.leadconnectorhq.com"


def get_location_access_token(location_id: str) -> Optional[str]:
    """
    Fetch the access token for a specific location from the database.
    This is needed to make GHL API calls.
    """
    if not location_id:
        return None

    conn = get_db_connection()
    if not conn:
        logger.error("DB connection failed in get_location_access_token")
        return None

    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT access_token
            FROM subscribers
            WHERE location_id = %s
              AND access_token IS NOT NULL
            LIMIT 1
        """, (location_id,))
        row = cur.fetchone()

        if row:
            raw_token = row[0] if isinstance(row, tuple) else row.get('access_token')
            return decrypt_token(raw_token) if raw_token else None
        return None
    except Exception as e:
        logger.error(f"Failed to get access token for location {location_id}: {e}")
        return None
    finally:
        if cur:
            cur.close()
        if conn:
            return_db_connection(conn)


def search_contact_by_name(location_id: str, first_name: str) -> Optional[str]:
    """
    Search for a contact by first name in a specific location.
    Returns contact_id if found (and only one match).
    """
    if not location_id or not first_name:
        return None

    access_token = get_location_access_token(location_id)
    if not access_token:
        logger.warning(f"No access token for location {location_id}, cannot search by name")
        return None

    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Version": "2021-07-28"
        }

        # Search for contacts with this first name
        response = requests.get(
            f"{GHL_API_BASE}/contacts/",
            headers=headers,
            params={"locationId": location_id, "query": first_name},
            timeout=10
        )

        if response.status_code != 200:
            logger.warning(f"GHL search failed: {response.status_code}")
            return None

        data = response.json()
        contacts = data.get("contacts", [])

        # Only return if we have exactly ONE match (to avoid ambiguity)
        if len(contacts) == 1:
            contact_id = contacts[0].get("id")
            logger.info(f"✅ Found contact by name: {first_name} → {contact_id}")
            return contact_id
        elif len(contacts) > 1:
            logger.warning(f"Multiple contacts found for name '{first_name}' in location {location_id}")
            return None
        else:
            logger.warning(f"No contacts found for name '{first_name}'")
            return None

    except Exception as e:
        logger.error(f"Error searching contact by name: {e}")
        return None



def search_contact_by_phone(location_id: str, phone: str, expected_first_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Search for a contact by phone number in a specific location.
    Returns dict with contact_id and matched contact data if found.

    If expected_first_name is provided, validates that the found contact matches.
    """
    if not location_id or not phone:
        return None

    access_token = get_location_access_token(location_id)
    if not access_token:
        return None

    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Version": "2021-07-28"
        }

        # Clean phone number
        phone_clean = ''.join(filter(str.isdigit, phone))

        response = requests.get(
            f"{GHL_API_BASE}/contacts/",
            headers=headers,
            params={"locationId": location_id, "query": phone_clean},
            timeout=10
        )

        if response.status_code != 200:
            return None

        data = response.json()
        contacts = data.get("contacts", [])

        if len(contacts) >= 1:
            contact = contacts[0]
            contact_id = contact.get("id")
            contact_first_name = contact.get("firstName", "").strip().lower()

            # If we have expected_first_name, validate it matches
            if expected_first_name:
                expected_lower = expected_first_name.strip().lower()
                if contact_first_name and contact_first_name == expected_lower:
                    logger.critical(f"✅ VALIDATED MATCH (99% CONFIDENCE) | phone={phone} + expected_name='{expected_first_name}' MATCHES ghl_name='{contact.get('firstName')}' → contact_id={contact_id}")
                    return {"contact_id": contact_id, "validated": True, "match_method": "phone+name"}
                elif contact_first_name:
                    logger.critical(f"🚨 NAME MISMATCH DETECTED | phone={phone} matched but NAMES DON'T MATCH | expected_name='{expected_first_name}' | ghl_name='{contact.get('firstName')}' | contact_id={contact_id} | This indicates payload has WRONG contact_id or WRONG name")
                    return None  # Name doesn't match - wrong contact
                else:
                    logger.warning(f"⚠️ Phone matched but GHL contact has no firstName | phone={phone} → contact_id={contact_id}")
                    return {"contact_id": contact_id, "validated": False, "match_method": "phone_only"}
            else:
                logger.info(f"✅ Found contact by phone (no name validation requested) | phone={phone} → contact_id={contact_id}")
                return {"contact_id": contact_id, "validated": False, "match_method": "phone_only"}

    except Exception as e:
        logger.error(f"Error searching contact by phone: {e}")
        return None


def extract_phone_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    """
    Extract phone number from various webhook payload structures.
    GHL sends phone in different formats depending on webhook type.
    """
    # Try multiple locations where phone might be
    phone = (
        payload.get("phone") or
        payload.get("contactPhone") or
        payload.get("contact", {}).get("phone") or
        payload.get("message", {}).get("contactPhone") or
        payload.get("message", {}).get("phone")
    )

    if phone:
        # Clean and validate phone number
        phone_clean = ''.join(filter(str.isdigit, str(phone)))
        if len(phone_clean) >= 10:  # Valid phone should have at least 10 digits
            return phone_clean

    return None


def validate_and_resolve_contact(payload: Dict[str, Any]) -> Optional[str]:
    """
    Intelligent contact ID resolution using PAYLOAD DATA as source of truth.

    CRM-aware: uses GHL API for GHL subscribers, CRM provider for others
    (HubSpot, Salesforce, etc.).

    PRIORITY ORDER:
    1. Use payload contact_id if valid
    2. Phone + First Name (99% match - PRIMARY RESOLUTION METHOD)
    3. Phone only (if no first_name available)
    4. First Name + Location ID (fallback, but ambiguous for common names)
    5. Return None (cannot resolve)
    """

    # Extract all available data points from payload (SOURCE OF TRUTH)
    contact_id = payload.get("contact_id")
    location_id = payload.get("location_id") or payload.get("location", {}).get("id")
    first_name = payload.get("first_name") or payload.get("contact", {}).get("first_name") or payload.get("contact", {}).get("firstName")
    address = payload.get("address") or payload.get("contact", {}).get("address1")
    phone = extract_phone_from_payload(payload)
    crm_source = payload.get("_crm_source", "ghl")

    logger.critical(f"🔍 CONTACT VALIDATION START | contact_id={contact_id} | location_id={location_id} | first_name={first_name} | phone={phone} | has_address={bool(address)} | crm_source={crm_source}")

    # Step 1: Check if contact_id is already valid
    if contact_id and contact_id != "unknown" and len(str(contact_id).strip()) >= 5:
        logger.critical(f"✅ CONTACT_ID VALID - NO RESOLUTION NEEDED | contact_id={contact_id}")
        return contact_id

    # If no location_id, we can't search any CRM
    if not location_id:
        logger.critical("❌ VALIDATION FAILED: no location_id in payload")
        return None

    # For non-GHL CRMs, use the provider's contact resolver
    if crm_source and crm_source.lower() not in ("ghl", "gohighlevel", ""):
        try:
            from crm_providers import get_provider
            provider = get_provider(crm_source)
            if provider:
                from db import get_subscriber_info_hybrid
                subscriber = get_subscriber_info_hybrid(location_id)
                crm_config = (subscriber or {}).get("crm_config") or {}
                token = crm_config.get("access_token", "")
                email = payload.get("email", "")
                result = provider.resolve_contact(
                    phone=phone, name=first_name, email=email, token=token,
                    location_id=location_id,
                )
                if result and result.get("id"):
                    logger.critical(f"✅ CONTACT RESOLVED via {crm_source} provider | contact_id={result['id']}")
                    return result["id"]
        except Exception as e:
            logger.error(f"CRM provider contact resolution failed: {e}")
        # Fall through to GHL methods as fallback

    # Step 2: PRIMARY METHOD - Phone + First Name (99% match)
    if phone and first_name:
        logger.critical(f"🔍 PRIMARY RESOLUTION METHOD: Phone + First Name | phone={phone} | first_name={first_name}")
        result = search_contact_by_phone(location_id, phone, expected_first_name=first_name)
        if result and result.get("validated"):
            resolved_id = result["contact_id"]
            logger.critical(f"✅ CONTACT RESOLVED (99% MATCH) | Method=Phone+Name | phone={phone} + first_name={first_name} → contact_id={resolved_id}")
            return resolved_id
        elif result:
            logger.critical(f"🚨 DATA MISMATCH | Phone matched but NAME VALIDATION FAILED | expected_name={first_name}")
        else:
            logger.warning(f"⚠️ No contact found with phone={phone} | Trying other methods")

    # Step 3: Phone only (if no first_name or name validation failed)
    if phone:
        logger.critical(f"🔍 SECONDARY RESOLUTION METHOD: Phone only | phone={phone}")
        result = search_contact_by_phone(location_id, phone, expected_first_name=None)
        if result:
            resolved_id = result["contact_id"]
            logger.critical(f"✅ CONTACT RESOLVED BY PHONE | Method=Phone | phone={phone} → contact_id={resolved_id}")
            return resolved_id

    # Step 4: FALLBACK - First Name + Location ID (warn about ambiguity)
    if first_name:
        logger.critical(f"🔍 FALLBACK RESOLUTION METHOD: First Name only | first_name={first_name}")
        resolved_id = search_contact_by_name(location_id, first_name)
        if resolved_id:
            logger.critical(f"✅ CONTACT RESOLVED BY NAME (HIGH AMBIGUITY RISK) | first_name={first_name} → contact_id={resolved_id}")
            return resolved_id

    # Step 5: All methods failed
    logger.critical(f"❌ CONTACT VALIDATION FAILED - ALL METHODS EXHAUSTED | contact_id={contact_id} | location_id={location_id} | first_name={first_name} | phone={phone}")
    return None
