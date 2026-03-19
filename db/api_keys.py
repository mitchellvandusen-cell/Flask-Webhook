# db/api_keys.py — API key & training token management
#
# Handles generation, storage, lookup, and revocation of external API keys
# (sk_live_ prefix) and training tokens (trn_ prefix).

from db_legacy import (
    generate_api_key,
    generate_webhook_secret,
    api_key_prefix,
    create_api_key_for_user,
    revoke_api_key,
    generate_training_token,
    create_training_token_for_user,
    revoke_training_token,
    get_subscriber_by_training_token,
    save_outbound_webhook_url,
    get_subscriber_by_api_key,
    log_api_usage,
    get_api_request_count,
)

__all__ = [
    "generate_api_key",
    "generate_webhook_secret",
    "api_key_prefix",
    "create_api_key_for_user",
    "revoke_api_key",
    "generate_training_token",
    "create_training_token_for_user",
    "revoke_training_token",
    "get_subscriber_by_training_token",
    "save_outbound_webhook_url",
    "get_subscriber_by_api_key",
    "log_api_usage",
    "get_api_request_count",
]
