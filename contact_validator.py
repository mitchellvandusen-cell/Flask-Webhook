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


def search_contact_by_phone(location_id: str, phone: str) -> Optional[str]:
    """
    Search for a contact by phone number in a specific location.
    Returns contact_id if found.
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
            contact_id = contacts[0].get("id")
            logger.info(f"✅ Found contact by phone: {phone} → {contact_id}")
            return contact_id

    except Exception as e:
        logger.error(f"Error searching contact by phone: {e}")
        return None


def validate_and_resolve_contact(payload: Dict[str, Any]) -> Optional[str]:
    """
    Intelligent contact ID resolution with fallback chain.

    Returns valid contact_id or None if all methods fail.

    Fallback chain:
    1. Use payload contact_id if valid
    2. Search by first_name in location
    3. Search by address in location
    4. Search by phone in location
    5. Return None (cannot resolve)
    """

    # Extract all available data points
    contact_id = payload.get("contact_id")
    location_id = payload.get("location_id") or payload.get("location", {}).get("id")
    first_name = payload.get("first_name") or payload.get("contact", {}).get("first_name")
    address = payload.get("address") or payload.get("contact", {}).get("address1")
    phone = payload.get("phone") or payload.get("contact", {}).get("phone")

    logger.critical(f"🔍 CONTACT VALIDATION START | contact_id={contact_id} | location_id={location_id} | first_name={first_name} | has_address={bool(address)} | has_phone={bool(phone)}")

    # Step 1: Check if contact_id is already valid
    if contact_id and contact_id != "unknown" and len(str(contact_id).strip()) >= 5:
        logger.info(f"✅ Contact ID already valid: {contact_id}")
        return contact_id

    # If no location_id, we can't search GHL
    if not location_id:
        logger.error("❌ Cannot validate contact: no location_id in payload")
        return None

    # Step 2: Try searching by first_name
    if first_name:
        logger.info(f"🔍 Attempting contact search by first_name: {first_name}")
        resolved_id = search_contact_by_name(location_id, first_name)
        if resolved_id:
            logger.critical(f"✅ CONTACT RESOLVED BY NAME | original={contact_id} | resolved={resolved_id} | first_name={first_name}")
            return resolved_id

    # Step 3: Try searching by address
    if address:
        logger.info(f"🔍 Attempting contact search by address: {address}")
        resolved_id = search_contact_by_address(location_id, address)
        if resolved_id:
            logger.critical(f"✅ CONTACT RESOLVED BY ADDRESS | original={contact_id} | resolved={resolved_id}")
            return resolved_id

    # Step 4: Try searching by phone
    if phone:
        logger.info(f"🔍 Attempting contact search by phone: {phone}")
        resolved_id = search_contact_by_phone(location_id, phone)
        if resolved_id:
            logger.critical(f"✅ CONTACT RESOLVED BY PHONE | original={contact_id} | resolved={resolved_id}")
            return resolved_id

    # Step 5: All methods failed
    logger.critical(f"❌ CONTACT VALIDATION FAILED | All resolution methods exhausted | payload_keys={list(payload.keys())}")
    return None
