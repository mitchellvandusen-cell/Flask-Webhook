# crm_providers/hubspot/resolver.py — HubSpot Contact Resolver
#
# Search HubSpot contacts by phone, email, or name using the CRM v3 Search API.
# Used by contact_validator.py when crm_source == "hubspot" and by the workflow
# engine for merge field resolution.
#
# HubSpot search specifics:
#   - POST /crm/v3/objects/contacts/search (not GET)
#   - OR logic requires separate filterGroups (each filterGroup is ANDed internally)
#   - Phone search needs exact match OR contains (HubSpot stores E.164)
#   - Returns max 100 results per page

import logging
import re

import requests

logger = logging.getLogger(__name__)

HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_TIMEOUT = 15

# Properties to fetch when resolving contacts
RESOLVE_PROPERTIES = [
    "firstname", "lastname", "email", "phone", "company",
    "address", "city", "state", "zip", "lifecyclestage",
    "hs_lead_status", "hubspot_owner_id",
]


def _hs_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _normalize_phone(phone):
    """Strip to digits only, remove leading country code '1' for 11-digit US numbers."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _format_contact(raw):
    """Convert HubSpot contact response to canonical IGB contact dict."""
    props = raw.get("properties", {})
    return {
        "id": str(raw.get("id", "")),
        "firstName": props.get("firstname", ""),
        "lastName": props.get("lastname", ""),
        "email": props.get("email", ""),
        "phone": props.get("phone", ""),
        "company": props.get("company", ""),
        "address": props.get("address", ""),
        "city": props.get("city", ""),
        "state": props.get("state", ""),
        "zip": props.get("zip", ""),
        "lifecyclestage": props.get("lifecyclestage", ""),
        "hs_lead_status": props.get("hs_lead_status", ""),
        "hubspot_owner_id": props.get("hubspot_owner_id", ""),
    }


def resolve_contact(phone=None, name=None, email=None, access_token=None):
    """
    Search HubSpot for a contact by phone, email, or name.

    Uses OR logic across fields: a match on ANY provided field returns a result.
    Priority: email > phone > name (email is most unique in HubSpot).

    Args:
        phone: Phone number (any format — will be normalized)
        email: Email address
        name: First name or full name
        access_token: HubSpot API token

    Returns:
        dict with canonical contact fields, or None if not found.
    """
    if not access_token:
        return None
    if not any([phone, email, name]):
        return None

    # Build filter groups (each group is ORed)
    filter_groups = []

    if email:
        filter_groups.append({
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        })

    if phone:
        # Try multiple phone formats for matching
        clean = _normalize_phone(phone)
        if clean and len(clean) >= 10:
            # Exact match on stored phone
            filter_groups.append({
                "filters": [{
                    "propertyName": "phone",
                    "operator": "CONTAINS_TOKEN",
                    "value": clean[-10:],  # Last 10 digits
                }]
            })

    if name and not email and not phone:
        # Name-only search is a last resort — high ambiguity
        parts = name.strip().split(None, 1)
        filter_groups.append({
            "filters": [{
                "propertyName": "firstname",
                "operator": "EQ",
                "value": parts[0],
            }]
        })
        if len(parts) > 1:
            # Add last name filter to the SAME group (AND logic)
            filter_groups[-1]["filters"].append({
                "propertyName": "lastname",
                "operator": "EQ",
                "value": parts[1],
            })

    if not filter_groups:
        return None

    payload = {
        "filterGroups": filter_groups,
        "properties": RESOLVE_PROPERTIES,
        "sorts": [{"propertyName": "lastmodifieddate", "direction": "DESCENDING"}],
        "limit": 5,
    }

    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    try:
        resp = requests.post(
            url, headers=_hs_headers(access_token),
            json=payload, timeout=HUBSPOT_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.error(f"HubSpot contact search failed: {resp.status_code}")
            return None

        results = resp.json().get("results", [])
        if not results:
            return None

        # If phone was provided, validate the match
        if phone and len(results) > 1:
            clean = _normalize_phone(phone)
            for r in results:
                r_phone = _normalize_phone(r.get("properties", {}).get("phone", ""))
                if r_phone and r_phone[-10:] == clean[-10:]:
                    return _format_contact(r)

        return _format_contact(results[0])

    except requests.RequestException as e:
        logger.error(f"HubSpot contact resolve network error: {e}")
        return None


def resolve_contact_by_id(contact_id, access_token):
    """
    Fetch a specific HubSpot contact by ID.

    Returns canonical contact dict or None.
    """
    if not contact_id or not access_token:
        return None

    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    params = {"properties": ",".join(RESOLVE_PROPERTIES)}

    try:
        resp = requests.get(
            url, headers=_hs_headers(access_token),
            params=params, timeout=HUBSPOT_TIMEOUT,
        )
        if resp.status_code == 200:
            return _format_contact(resp.json())
        if resp.status_code == 404:
            logger.warning(f"HubSpot contact {contact_id} not found")
        else:
            logger.error(f"HubSpot contact fetch failed: {resp.status_code}")
    except requests.RequestException as e:
        logger.error(f"HubSpot contact fetch error: {e}")

    return None


def search_contacts_bulk(query, access_token, limit=20):
    """
    Full-text search across HubSpot contacts (name, email, phone).

    Used by the dialer and inbox for contact lookup.

    Returns:
        list of canonical contact dicts.
    """
    if not query or not access_token:
        return []

    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "query": query,
        "properties": RESOLVE_PROPERTIES,
        "limit": min(limit, 100),
    }

    try:
        resp = requests.post(
            url, headers=_hs_headers(access_token),
            json=payload, timeout=HUBSPOT_TIMEOUT,
        )
        if resp.status_code == 200:
            return [_format_contact(r) for r in resp.json().get("results", [])]
        logger.error(f"HubSpot bulk search failed: {resp.status_code}")
    except requests.RequestException as e:
        logger.error(f"HubSpot bulk search error: {e}")

    return []
