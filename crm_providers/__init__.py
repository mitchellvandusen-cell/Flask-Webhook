# crm_providers/__init__.py — CRM Provider Registry
#
# Central registry for all CRM providers. Each provider is a self-contained
# package that handles inbound webhooks, data sync, activity logging,
# token lifecycle, contact resolution, and outbound operations.

import logging
from typing import Optional

from crm_providers.base import CRMProvider, CRMEvent, SyncResult

logger = logging.getLogger(__name__)

# Registry: crm_type -> (module_path, class_name)
# Lazy-loaded to avoid circular imports and unnecessary module loading.
PROVIDER_REGISTRY = {
    "ghl": ("crm_providers.ghl", "GHLProvider"),
    "gohighlevel": ("crm_providers.ghl", "GHLProvider"),
    "hubspot": ("crm_providers.hubspot", "HubSpotProvider"),
}

# Cache instantiated providers (they're stateless singletons)
_provider_cache = {}


def get_provider(crm_type: str = None) -> CRMProvider:
    """
    Get the CRM provider for the given type.

    Args:
        crm_type: One of the keys in PROVIDER_REGISTRY (e.g. "ghl", "hubspot").
                  Defaults to "ghl" if None or unknown.

    Returns:
        CRMProvider instance (cached singleton).
    """
    crm_type = (crm_type or "ghl").lower().strip()

    if crm_type in _provider_cache:
        return _provider_cache[crm_type]

    if crm_type not in PROVIDER_REGISTRY:
        logger.warning(f"Unknown CRM provider '{crm_type}', falling back to GHL")
        crm_type = "ghl"

    module_path, class_name = PROVIDER_REGISTRY[crm_type]

    try:
        import importlib
        module = importlib.import_module(module_path)
        provider_class = getattr(module, class_name)
        provider = provider_class()
        _provider_cache[crm_type] = provider
        logger.info(f"Loaded CRM provider: {provider}")
        return provider
    except Exception as e:
        logger.error(f"Failed to load CRM provider '{crm_type}': {e}. Falling back to GHL.")
        # Fall back to GHL provider
        if crm_type != "ghl":
            return get_provider("ghl")
        raise


def get_provider_for_subscriber(subscriber: dict) -> CRMProvider:
    """
    Convenience: get provider based on subscriber's crm_type.
    Default is "ghl" for backward compatibility.
    """
    crm_type = subscriber.get("crm_type", "ghl") or "ghl"
    return get_provider(crm_type)


def list_providers() -> list:
    """List all registered provider types."""
    seen = set()
    result = []
    for key, (module_path, class_name) in PROVIDER_REGISTRY.items():
        if class_name not in seen:
            seen.add(class_name)
            result.append({"type": key, "module": module_path, "class": class_name})
    return result


__all__ = [
    "CRMProvider",
    "CRMEvent",
    "SyncResult",
    "get_provider",
    "get_provider_for_subscriber",
    "list_providers",
]
