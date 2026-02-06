# crm_adapters/factory.py - CRM Adapter Factory
# Creates the correct adapter based on subscriber's crm_type setting.
# Default is GHL (GoHighLevel) for backward compatibility.

import logging
from typing import Optional
from crm_adapters.base import CRMAdapter

logger = logging.getLogger(__name__)

# Registry of supported CRM adapters
# Maps crm_type string -> (module_path, class_name)
CRM_REGISTRY = {
    "ghl": ("crm_adapters.ghl_adapter", "GHLAdapter"),
    "gohighlevel": ("crm_adapters.ghl_adapter", "GHLAdapter"),
    "zapier": ("crm_adapters.zapier_adapter", "ZapierAdapter"),
    "salesforce": ("crm_adapters.salesforce_adapter", "SalesforceAdapter"),
    "hubspot": ("crm_adapters.hubspot_adapter", "HubSpotAdapter"),
    "pipedrive": ("crm_adapters.pipedrive_adapter", "PipedriveAdapter"),
    "zoho": ("crm_adapters.zoho_adapter", "ZohoAdapter"),
    "insureio": ("crm_adapters.insureio_adapter", "InsureioAdapter"),
}

# Human-readable names for the UI
CRM_DISPLAY_NAMES = {
    "ghl": "GoHighLevel (Lead Connector)",
    "zapier": "Zapier (Universal Webhook)",
    "salesforce": "Salesforce",
    "hubspot": "HubSpot",
    "pipedrive": "Pipedrive",
    "zoho": "Zoho CRM",
    "insureio": "Insureio",
}

# Configuration fields required for each CRM
CRM_CONFIG_FIELDS = {
    "ghl": {
        "description": "GoHighLevel / Lead Connector (default)",
        "fields": [
            {"key": "access_token", "label": "Access Token", "type": "password", "required": True, "help": "OAuth or Private Integration Token from GHL"},
            {"key": "calendar_id", "label": "Calendar ID", "type": "text", "required": True, "help": "Select from your GHL calendars"},
        ]
    },
    "zapier": {
        "description": "Connect via Zapier webhooks to route data to any app",
        "fields": [
            {"key": "webhook_url", "label": "Catch Hook URL", "type": "url", "required": True, "help": "Your Zapier Catch Hook URL (e.g., https://hooks.zapier.com/hooks/catch/...)"},
            {"key": "message_webhook_url", "label": "Message Hook URL (optional)", "type": "url", "required": False, "help": "Separate hook for outbound messages"},
            {"key": "booking_webhook_url", "label": "Booking Hook URL (optional)", "type": "url", "required": False, "help": "Separate hook for appointment bookings"},
        ]
    },
    "salesforce": {
        "description": "Salesforce CRM with OAuth 2.0",
        "fields": [
            {"key": "instance_url", "label": "Instance URL", "type": "url", "required": True, "help": "e.g., https://na1.salesforce.com"},
            {"key": "access_token", "label": "Access Token", "type": "password", "required": True, "help": "OAuth Bearer token"},
            {"key": "refresh_token", "label": "Refresh Token", "type": "password", "required": False, "help": "For automatic token renewal"},
            {"key": "client_id", "label": "Client ID", "type": "text", "required": False, "help": "Connected App Client ID"},
            {"key": "client_secret", "label": "Client Secret", "type": "password", "required": False, "help": "Connected App Client Secret"},
        ]
    },
    "hubspot": {
        "description": "HubSpot CRM with Private App or OAuth",
        "fields": [
            {"key": "access_token", "label": "Access Token", "type": "password", "required": True, "help": "Private App token or OAuth access token"},
            {"key": "owner_id", "label": "Owner ID (optional)", "type": "text", "required": False, "help": "HubSpot owner ID for assigning meetings"},
            {"key": "refresh_token", "label": "Refresh Token (OAuth only)", "type": "password", "required": False, "help": "Only needed for OAuth apps"},
            {"key": "client_id", "label": "Client ID (OAuth only)", "type": "text", "required": False, "help": "Only needed for OAuth apps"},
            {"key": "client_secret", "label": "Client Secret (OAuth only)", "type": "password", "required": False, "help": "Only needed for OAuth apps"},
        ]
    },
    "pipedrive": {
        "description": "Pipedrive CRM with API token or OAuth",
        "fields": [
            {"key": "company_domain", "label": "Company Domain", "type": "text", "required": True, "help": "Your Pipedrive subdomain (e.g., 'mycompany' from mycompany.pipedrive.com)"},
            {"key": "api_token", "label": "API Token", "type": "password", "required": True, "help": "From Pipedrive Settings > Personal Preferences > API"},
            {"key": "owner_id", "label": "User ID (optional)", "type": "text", "required": False, "help": "Pipedrive user ID for assigning activities"},
        ]
    },
    "zoho": {
        "description": "Zoho CRM with OAuth 2.0",
        "fields": [
            {"key": "access_token", "label": "Access Token", "type": "password", "required": True, "help": "Zoho OAuth access token"},
            {"key": "refresh_token", "label": "Refresh Token", "type": "password", "required": True, "help": "Required - Zoho tokens expire every hour"},
            {"key": "client_id", "label": "Client ID", "type": "text", "required": True, "help": "From Zoho API Console"},
            {"key": "client_secret", "label": "Client Secret", "type": "password", "required": True, "help": "From Zoho API Console"},
            {"key": "data_center", "label": "Data Center", "type": "select", "required": False, "help": "Your Zoho data center region",
             "options": ["com", "eu", "in", "com.au", "jp", "zohocloud.ca"]},
        ]
    },
    "insureio": {
        "description": "Insureio insurance CRM for lead management",
        "fields": [
            {"key": "api_key", "label": "API Key", "type": "password", "required": True, "help": "Insureio API key from your account settings"},
            {"key": "brand_id", "label": "Brand/Profile ID", "type": "text", "required": True, "help": "Required for lead creation"},
            {"key": "subdomain", "label": "Subdomain", "type": "text", "required": False, "help": "Your organization subdomain (e.g., 'myagency')"},
            {"key": "booking_webhook_url", "label": "Booking Webhook URL (optional)", "type": "url", "required": False, "help": "External URL for booking notifications (e.g., Calendly webhook)"},
        ]
    },
}


def get_crm_adapter(crm_type: str, subscriber_data: dict) -> CRMAdapter:
    """
    Create and return the appropriate CRM adapter.

    Args:
        crm_type: One of the keys in CRM_REGISTRY (e.g., "ghl", "salesforce", "zapier")
        subscriber_data: Dict with credentials and config for the subscriber

    Returns:
        CRMAdapter instance

    Falls back to GHL adapter if crm_type is unknown.
    """
    crm_type_lower = (crm_type or "ghl").lower().strip()

    if crm_type_lower not in CRM_REGISTRY:
        logger.warning(f"Unknown CRM type '{crm_type}', falling back to GHL adapter")
        crm_type_lower = "ghl"

    module_path, class_name = CRM_REGISTRY[crm_type_lower]

    try:
        import importlib
        module = importlib.import_module(module_path)
        adapter_class = getattr(module, class_name)
        adapter = adapter_class(subscriber_data)
        logger.info(f"Created {adapter.CRM_NAME} adapter for location={subscriber_data.get('location_id', 'unknown')}")
        return adapter
    except Exception as e:
        logger.error(f"Failed to create {crm_type} adapter: {e}. Falling back to GHL.")
        from crm_adapters.ghl_adapter import GHLAdapter
        return GHLAdapter(subscriber_data)


def get_adapter_for_subscriber(subscriber_data: dict) -> CRMAdapter:
    """
    Convenience function: reads crm_type from subscriber_data and creates adapter.
    Default is "ghl" if crm_type not set (backward compatible).
    """
    crm_type = subscriber_data.get("crm_type", "ghl") or "ghl"
    return get_crm_adapter(crm_type, subscriber_data)


def list_available_crms() -> list:
    """Return list of available CRM integrations for the UI."""
    return [
        {
            "id": crm_id,
            "name": CRM_DISPLAY_NAMES.get(crm_id, crm_id),
            "config": CRM_CONFIG_FIELDS.get(crm_id, {}),
        }
        for crm_id in CRM_DISPLAY_NAMES
    ]
