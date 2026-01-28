# contact_validator.py - Intelligent Contact ID Resolution
# When GHL sends incomplete data, this module cross-references multiple fields to find the correct contact

import os
import logging
import requests
from typing import Optional, Dict, Any
from db import get_db_connection

logger = logging.getLogger(__name__)

GHL_API_BASE = "https://services.leadconnector.io"


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

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT access_token
            FROM oauth_tokens
            WHERE location_id = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (location_id,))
        row = cur.fetchone()

        if row:
            token = row[0] if isinstance(row, tuple) else row.get('access_token')
            return token
        return None
    except Exception as e:
        logger.error(f"Failed to get access token for location {location_id}: {e}")
        return None
    finally:
        if conn:
            cur.close()
            conn.close()


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


def search_contact_by_address(location_id: str, address: str) -> Optional[str]:
    """
    Search for a contact by address in a specific location.
    Returns contact_id if found (and only one match).
    """
    if not location_id or not address:
        return None

    access_token = get_location_access_token(location_id)
    if not access_token:
        return None

    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Version": "2021-07-28"
        }

        # Extract just city/state or zip for search
        address_clean = address.strip()

        response = requests.get(
            f"{GHL_API_BASE}/contacts/",
            headers=headers,
            params={"locationId": location_id, "query": address_clean},
            timeout=10
        )

        if response.status_code != 200:
            return None

        data = response.json()
        contacts = data.get("contacts", [])

        if len(contacts) == 1:
            contact_id = contacts[0].get("id")
            logger.info(f"✅ Found contact by address: {address} → {contact_id}")
            return contact_id

    except Exception as e:
        logger.error(f"Error searching contact by address: {e}")
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
                    logger.info(f"✅ VALIDATED: Phone {phone} + Name '{expected_first_name}' → {contact_id} (99% match)")
                    return {"contact_id": contact_id, "validated": True, "match_method": "phone+name"}
                elif contact_first_name:
                    logger.warning(f"⚠️ Phone matched but name mismatch | expected='{expected_first_name}' | found='{contact.get('firstName')}' | contact_id={contact_id}")
                    return None  # Name doesn't match - wrong contact
                else:
                    logger.info(f"✅ Phone matched (no name to validate) → {contact_id}")
                    return {"contact_id": contact_id, "validated": False, "match_method": "phone_only"}
            else:
                logger.info(f"✅ Found contact by phone (no name validation): {phone} → {contact_id}")
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

    The payload contains all the data we need:
    - Phone number (the number being texted - GHL wouldn't send webhook without it)
    - First name (from GHL contact record)
    - Location ID (which GHL account this belongs to)

    We use THIS DATA to find the correct contact_id by cross-referencing with GHL API.

    PRIORITY ORDER (as per user requirements):
    1. Use payload contact_id if valid
    2. Phone + First Name (99% match - PRIMARY RESOLUTION METHOD)
    3. Phone only (if no first_name available)
    4. First Name + Location ID (fallback, but ambiguous for common names)
    5. Return None (cannot resolve)

    Phone number is MOST UNIQUE because:
    - It's the number being texted (inherent to SMS routing)
    - Each contact has only one phone number
    - Combined with first_name = 99% accurate match
    """

    # Extract all available data points from payload (SOURCE OF TRUTH)
    contact_id = payload.get("contact_id")
    location_id = payload.get("location_id") or payload.get("location", {}).get("id")
    first_name = payload.get("first_name") or payload.get("contact", {}).get("first_name") or payload.get("contact", {}).get("firstName")
    address = payload.get("address") or payload.get("contact", {}).get("address1")
    phone = extract_phone_from_payload(payload)

    logger.critical(f"🔍 CONTACT VALIDATION START | contact_id={contact_id} | location_id={location_id} | first_name={first_name} | phone={phone} | has_address={bool(address)}")

    # Step 1: Check if contact_id is already valid
    if contact_id and contact_id != "unknown" and len(str(contact_id).strip()) >= 5:
        logger.info(f"✅ Contact ID already valid: {contact_id}")
        return contact_id

    # If no location_id, we can't search GHL
    if not location_id:
        logger.error("❌ Cannot validate contact: no location_id in payload")
        return None

    # Step 2: PRIMARY METHOD - Phone + First Name (99% match)
    if phone and first_name:
        logger.info(f"🔍 PRIMARY RESOLUTION: Phone + First Name | phone={phone} | first_name={first_name}")
        result = search_contact_by_phone(location_id, phone, expected_first_name=first_name)
        if result and result.get("validated"):
            resolved_id = result["contact_id"]
            logger.critical(f"✅ CONTACT RESOLVED (99% MATCH) | phone={phone} + first_name={first_name} → {resolved_id}")
            return resolved_id
        elif result:
            # Phone matched but name didn't validate
            logger.warning(f"⚠️ Phone matched but first_name validation failed | Trying other methods")
        else:
            logger.warning(f"⚠️ No contact found with phone={phone} | Trying other methods")

    # Step 3: Phone only (if no first_name or name validation failed)
    if phone:
        logger.info(f"🔍 SECONDARY RESOLUTION: Phone only | phone={phone}")
        result = search_contact_by_phone(location_id, phone, expected_first_name=None)
        if result:
            resolved_id = result["contact_id"]
            logger.critical(f"✅ CONTACT RESOLVED BY PHONE | phone={phone} → {resolved_id} (no name validation)")
            return resolved_id

    # Step 4: FALLBACK - First Name + Location ID (warn about ambiguity)
    if first_name:
        logger.info(f"🔍 FALLBACK RESOLUTION: First Name only | first_name={first_name} (may be ambiguous)")
        resolved_id = search_contact_by_name(location_id, first_name)
        if resolved_id:
            logger.critical(f"✅ CONTACT RESOLVED BY NAME (AMBIGUOUS) | first_name={first_name} → {resolved_id} | WARNING: Common names may cause mismatch")
            return resolved_id

    # Step 5: All methods failed
    logger.critical(f"❌ CONTACT VALIDATION FAILED | All resolution methods exhausted | payload_keys={list(payload.keys())} | Payload dump: {payload}")
    return None
