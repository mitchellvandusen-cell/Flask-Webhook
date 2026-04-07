"""Trust Hub submodule - CNAM, A2P, Voice Integrity, SHAKEN/STIR."""

from .base import (
    POLICY_SECONDARY_CUSTOMER_PROFILE,
    POLICY_CNAM_TRUST_PRODUCT,
    POLICY_VOICE_INTEGRITY_TRUST_PRODUCT,
    POLICY_A2P_BRAND,
)

from .business_profile import (
    submit_business_profile_for_review,
    get_business_profile_status,
)

from .cnam import (
    create_cnam_trust_product,
    get_cnam_status,
    submit_cnam_for_review,
)

from .a2p import (
    create_a2p_brand,
    get_a2p_brand_status,
    create_a2p_campaign,
    get_a2p_campaign_status,
)

from .voice_integrity import (
    create_voice_integrity_trust_product,
    get_voice_integrity_status,
    submit_voice_integrity_for_review,
)

from .shaken_stir import (
    create_shaken_stir_trust_product,
    get_shaken_stir_status,
)

__all__ = [
    # Policy SIDs
    "POLICY_SECONDARY_CUSTOMER_PROFILE",
    "POLICY_CNAM_TRUST_PRODUCT",
    "POLICY_VOICE_INTEGRITY_TRUST_PRODUCT",
    "POLICY_A2P_BRAND",
    # Business Profile
    "submit_business_profile_for_review",
    "get_business_profile_status",
    # CNAM
    "create_cnam_trust_product",
    "get_cnam_status",
    "submit_cnam_for_review",
    # A2P
    "create_a2p_brand",
    "get_a2p_brand_status",
    "create_a2p_campaign",
    "get_a2p_campaign_status",
    # Voice Integrity
    "create_voice_integrity_trust_product",
    "get_voice_integrity_status",
    "submit_voice_integrity_for_review",
    # SHAKEN/STIR
    "create_shaken_stir_trust_product",
    "get_shaken_stir_status",
]
