# crm_adapters - Multi-CRM Integration Layer
# Provides a unified interface for booking, contacts, and messaging across CRM platforms.
# Default: GoHighLevel (GHL). Additional: Zapier, Salesforce, HubSpot, Pipedrive, Zoho, Insureio.

from crm_adapters.factory import get_crm_adapter, get_adapter_for_subscriber
from crm_adapters.base import CRMAdapter

__all__ = ["get_crm_adapter", "get_adapter_for_subscriber", "CRMAdapter"]
