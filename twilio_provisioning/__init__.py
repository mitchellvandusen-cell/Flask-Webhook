"""
Twilio provisioning package.

White-label sub-account provisioning, phone number management,
Trust Hub operations (CNAM, Voice Integrity, A2P, SHAKEN/STIR).
"""

from .client import (
    get_master_client,
    get_sub_account_client,
    get_sub_account_client_native,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE_NUMBER,
    TWILIO_API_KEY_SID,
    TWILIO_API_KEY_SECRET,
)

from .sub_accounts import (
    create_sub_account,
    suspend_sub_account,
    reactivate_sub_account,
    create_twiml_app,
    update_twiml_app,
    create_api_key,
    is_master_account,
)

from .phone_numbers import (
    search_available_numbers,
    buy_phone_number,
    list_phone_numbers,
    release_phone_number,
    update_phone_number,
    set_primary_phone_number,
)

from .voice import (
    provision_subscriber,
    generate_voice_access_token,
    start_call,
    update_call_status,
)

from .spam_protection import (
    register_cnam_protection,
    register_spam_protection,
    check_spam_protection_status,
)

from .trust_hub import (
    submit_business_profile_for_review,
    get_business_profile_status,
    create_cnam_trust_product,
    get_cnam_status,
    submit_cnam_for_review,
    create_a2p_brand,
    get_a2p_brand_status,
    create_a2p_campaign,
    get_a2p_campaign_status,
    create_voice_integrity_trust_product,
    get_voice_integrity_status,
    submit_voice_integrity_for_review,
    create_shaken_stir_trust_product,
    get_shaken_stir_status,
)

__all__ = [
    # Client factories
    "get_master_client",
    "get_sub_account_client",
    "get_sub_account_client_native",
    # Credentials
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "TWILIO_API_KEY_SID",
    "TWILIO_API_KEY_SECRET",
    # Sub-accounts
    "create_sub_account",
    "suspend_sub_account",
    "reactivate_sub_account",
    "create_twiml_app",
    "update_twiml_app",
    "create_api_key",
    "is_master_account",
    # Phone numbers
    "search_available_numbers",
    "buy_phone_number",
    "list_phone_numbers",
    "release_phone_number",
    "update_phone_number",
    "set_primary_phone_number",
    # Voice
    "provision_subscriber",
    "generate_voice_access_token",
    "start_call",
    "update_call_status",
    # Spam protection
    "register_cnam_protection",
    "register_spam_protection",
    "check_spam_protection_status",
    # Trust Hub
    "submit_business_profile_for_review",
    "get_business_profile_status",
    "create_cnam_trust_product",
    "get_cnam_status",
    "submit_cnam_for_review",
    "create_a2p_brand",
    "get_a2p_brand_status",
    "create_a2p_campaign",
    "get_a2p_campaign_status",
    "create_voice_integrity_trust_product",
    "get_voice_integrity_status",
    "submit_voice_integrity_for_review",
    "create_shaken_stir_trust_product",
    "get_shaken_stir_status",
]
