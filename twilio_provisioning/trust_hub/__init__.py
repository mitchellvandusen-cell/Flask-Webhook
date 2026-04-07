"""Trust Hub submodule - CNAM, A2P, Voice Integrity, SHAKEN/STIR."""

from .base import (
    SECONDARY_CUSTOMER_PROFILE_POLICY_SID,
    CNAM_TRUST_PRODUCT_POLICY_SID,
    VOICE_INTEGRITY_POLICY_SID,
    is_master_account,
    check_secondary_profile_status,
)

from .business_profile import (
    register_business_profile,
)

from .cnam import (
    validate_cnam_display_name,
    create_cnam_trust_product,
    update_cnam_display_name,
    assign_numbers_to_cnam,
    submit_cnam_for_review,
    discover_cnam_trust_product,
    get_cnam_trust_product_status,
)

from .a2p import (
    create_a2p_brand,
    get_a2p_brand_status,
    create_messaging_service,
    add_phone_to_messaging_service,
    create_a2p_campaign,
    get_a2p_campaign_status,
    list_messaging_services,
    list_messaging_service_phone_numbers,
    discover_a2p_brands,
    discover_a2p_campaigns,
    discover_trust_hub_profiles,
    discover_full_a2p_status,
)

from .voice_integrity import (
    create_voice_integrity_trust_product,
    assign_numbers_to_voice_integrity,
    submit_voice_integrity_for_review,
    get_voice_integrity_status,
    update_voice_integrity_end_user,
    resubmit_voice_integrity,
    remove_number_from_voice_integrity,
    discover_voice_integrity_products,
)

from .shaken_stir import (
    create_shaken_stir_trust_product,
    assign_numbers_to_shaken_stir,
    submit_shaken_stir_for_review,
    get_shaken_stir_status,
    resubmit_shaken_stir,
    remove_number_from_shaken_stir,
    discover_shaken_stir_products,
)

__all__ = [
    # Helpers
    "is_master_account",
    # Policy SIDs
    "SECONDARY_CUSTOMER_PROFILE_POLICY_SID",
    "CNAM_TRUST_PRODUCT_POLICY_SID",
    "VOICE_INTEGRITY_POLICY_SID",
    # Business Profile
    "register_business_profile",
    # CNAM
    "validate_cnam_display_name",
    "create_cnam_trust_product",
    "update_cnam_display_name",
    "assign_numbers_to_cnam",
    "submit_cnam_for_review",
    "discover_cnam_trust_product",
    "get_cnam_trust_product_status",
    # A2P
    "create_a2p_brand",
    "get_a2p_brand_status",
    "create_messaging_service",
    "add_phone_to_messaging_service",
    "create_a2p_campaign",
    "get_a2p_campaign_status",
    "list_messaging_services",
    "list_messaging_service_phone_numbers",
    "discover_a2p_brands",
    "discover_a2p_campaigns",
    "discover_trust_hub_profiles",
    "discover_full_a2p_status",
    # Voice Integrity
    "create_voice_integrity_trust_product",
    "assign_numbers_to_voice_integrity",
    "submit_voice_integrity_for_review",
    "get_voice_integrity_status",
    "update_voice_integrity_end_user",
    "resubmit_voice_integrity",
    "remove_number_from_voice_integrity",
    "discover_voice_integrity_products",
    # SHAKEN/STIR
    "create_shaken_stir_trust_product",
    "assign_numbers_to_shaken_stir",
    "submit_shaken_stir_for_review",
    "get_shaken_stir_status",
    "resubmit_shaken_stir",
    "remove_number_from_shaken_stir",
    "discover_shaken_stir_products",
]
