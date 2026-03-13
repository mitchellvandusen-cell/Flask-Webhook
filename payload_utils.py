# payload_utils.py — Webhook payload normalisation utilities
#
# Converts any GoHighLevel webhook payload (marketplace, custom, or
# Conversation Provider) into a consistent snake_case dictionary.
# Used by blueprints/webhooks.py and available for any module that
# needs to parse GHL payloads.


def extract_field_flexible(payload: dict, field_name: str, search_nested: bool = True):
    """
    Extract a field from a webhook payload supporting all naming conventions:
    snake_case, camelCase, PascalCase, UPPERCASE, and space-separated variants.
    Also searches nested structures (extras, data, meta, contact, location).
    """
    base_name = field_name.replace("_", "").lower()
    words     = field_name.split("_")

    variations = [
        field_name,
        field_name.upper(),
        field_name.replace("_", " "),
        field_name.replace("_", " ").upper(),
        field_name.replace("_", " ").title(),
        base_name,
        base_name.upper(),
        "".join(w.capitalize() for w in words),
        words[0] + "".join(w.capitalize() for w in words[1:]),
    ]

    for var in variations:
        if var in payload:
            value = payload[var]
            if value is not None and str(value).strip():
                return value

    if search_nested:
        for nested_key in ["extras", "data", "meta", "contact", "location", "user", "calendar"]:
            if nested_key in payload and isinstance(payload[nested_key], dict):
                for var in variations:
                    if var in payload[nested_key]:
                        value = payload[nested_key][var]
                        if value is not None and str(value).strip():
                            return value

    return None


def normalize_payload_universal(payload: dict) -> dict:
    """Normalize any Lead Connector payload to a consistent snake_case dict."""
    id_fields   = ["contact_id", "location_id", "user_id", "calendar_id",
                   "appointment_id", "opportunity_id", "workflow_id", "company_id",
                   "conversation_id", "message_id", "task_id", "pipeline_id"]
    data_fields = ["first_name", "last_name", "full_name", "email", "phone",
                   "address", "city", "state", "zip", "country",
                   "age", "date_of_birth", "gender", "intent", "message",
                   "agent", "status", "type", "direction", "body"]

    normalized = {}
    for field in id_fields + data_fields:
        value = extract_field_flexible(payload, field, search_nested=True)
        if value is not None:
            normalized[field] = value

    # GHL-specific field aliases (GHL uses camelCase names that don't match snake_case variations)
    _GHL_ALIASES = {
        "zip": ["postalCode", "postal_code", "zipCode", "zip_code"],
        "address": ["address1", "streetAddress", "street_address"],
    }
    for target_field, aliases in _GHL_ALIASES.items():
        if target_field not in normalized:
            for alias in aliases:
                val = payload.get(alias)
                if val and str(val).strip():
                    normalized[target_field] = val
                    break
                # Also search nested structures
                for nk in ("contact", "data", "extras", "location"):
                    nested = payload.get(nk)
                    if isinstance(nested, dict):
                        val = nested.get(alias)
                        if val and str(val).strip():
                            normalized[target_field] = val
                            break
                if target_field in normalized:
                    break

    # Extract tags (array field)
    tags = None
    for tags_key in ("tags", "Tags", "TAGS"):
        if isinstance(payload.get(tags_key), list):
            tags = payload[tags_key]
            break
    if tags is None:
        for nested_key in ("contact", "data", "extras"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict) and isinstance(nested.get("tags"), list):
                tags = nested["tags"]
                break
    if tags is not None:
        normalized["tags"] = tags

    # Extract date fields (GHL sends dateAdded, dateCreated, etc.)
    for date_field in ("date_added", "date_created", "date_imported"):
        val = extract_field_flexible(payload, date_field, search_nested=True)
        if val:
            normalized[date_field] = val
            break
    if "date_added" not in normalized:
        for camel_key in ("dateAdded", "dateCreated", "dateImported", "createdAt"):
            val = payload.get(camel_key)
            if not val:
                for nk in ("contact", "data", "extras"):
                    nested = payload.get(nk)
                    if isinstance(nested, dict) and nested.get(camel_key):
                        val = nested[camel_key]
                        break
            if val:
                normalized["date_added"] = val
                break

    # Extract custom fields (array)
    custom_fields = None
    for cf_key in ("customFields", "customField", "custom_fields"):
        val = payload.get(cf_key)
        if isinstance(val, list):
            custom_fields = val
            break
    if custom_fields is None:
        for nk in ("contact", "data", "extras"):
            nested = payload.get(nk)
            if isinstance(nested, dict):
                for cf_key in ("customFields", "customField", "custom_fields"):
                    val = nested.get(cf_key)
                    if isinstance(val, list):
                        custom_fields = val
                        break
                if custom_fields:
                    break
    if custom_fields:
        normalized["custom_fields"] = custom_fields

    # Extract source (lead origin)
    source = extract_field_flexible(payload, "source", search_nested=True)
    if source:
        normalized["source"] = source

    normalized["_original_payload"] = payload
    normalized["_is_marketplace"]   = payload.get("isMarketplaceAction", False)
    return normalized
