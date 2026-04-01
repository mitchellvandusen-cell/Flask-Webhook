# llm_caller.py - Message generation with LLM fallback chain
#
# Architecture:
#   conversation_engine.py (reasoning) -> classifies intent, objections, stage
#   memory.py (reasoning)              -> analyzes conversation, builds narrative
#   sales_director.py                  -> builds tactical guidance
#   prompt.py                          -> assembles system prompt with all context
#   llm_caller.py (NON-reasoning)      -> writes the actual text message
#
# All the thinking is already done by the time this file runs.
# The LLM's only job here is to write 1-3 sentences of natural text.
# Non-reasoning model = no thinking tokens = no reasoning leak = no risk.
#
# Fallback chain: xAI (primary) -> OpenAI (fallback 1) -> Gemini (fallback 2)
# Falls back on timeout, 5xx, connection errors. Never retries on 4xx.
import logging
import os
import time

from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError
from reply_sanitizer import sanitize_reply

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# ═══ MODEL MAPPING ═════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

MODEL_MAP = {
    "xai": {
        "fast": "grok-4-1-fast-non-reasoning",
        "reasoning": "grok-3",
    },
    "openai": {
        "fast": "gpt-4o-mini",
        "reasoning": "gpt-4o",
    },
    "gemini": {
        "fast": "gemini-2.5-flash",
        "reasoning": "gemini-2.5-pro",
    },
}

# Message generation: non-reasoning (fast, direct output, no thinking tokens)
GENERATION_MODEL = MODEL_MAP["xai"]["fast"]

# ═══════════════════════════════════════════════════════════════════════════════
# ═══ PROVIDER CHAIN ════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

# Default timeout per provider (seconds)
_LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "30"))


def _build_provider_chain():
    """
    Build the ordered list of fallback providers.
    Only includes providers whose API key env var is set.
    Returns list of dicts: [{name, client, model_tier}]
    """
    providers = []

    # Primary: xAI (always first if key is set)
    xai_key = os.getenv("XAI_API_KEY")
    if xai_key:
        providers.append({
            "name": "xai",
            "client": OpenAI(
                api_key=xai_key,
                base_url="https://api.x.ai/v1",
                timeout=_LLM_TIMEOUT,
            ),
        })

    # Check if fallback is explicitly disabled
    fallback_enabled = os.getenv("LLM_FALLBACK_ENABLED", "").lower()
    if fallback_enabled == "false":
        return providers

    # Fallback 1: OpenAI
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        providers.append({
            "name": "openai",
            "client": OpenAI(
                api_key=openai_key,
                timeout=_LLM_TIMEOUT,
            ),
        })

    # Fallback 2: Google Gemini (OpenAI-compatible endpoint)
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        providers.append({
            "name": "gemini",
            "client": OpenAI(
                api_key=gemini_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                timeout=_LLM_TIMEOUT,
            ),
        })

    return providers


# Build chain once at module load. Providers with missing keys are excluded.
_provider_chain = _build_provider_chain()

# Log available providers at startup
_provider_names = [p["name"] for p in _provider_chain]
if _provider_names:
    logger.info(f"LLM provider chain: {' -> '.join(_provider_names)}")
else:
    logger.warning("LLM provider chain: NO PROVIDERS CONFIGURED (missing XAI_API_KEY)")


def _get_model_for_provider(provider_name, model_tier="fast"):
    """Get the appropriate model name for a provider and tier."""
    return MODEL_MAP.get(provider_name, MODEL_MAP["xai"]).get(model_tier, MODEL_MAP["xai"]["fast"])


def _is_retriable(error):
    """
    Determine if an error should trigger fallback to the next provider.
    Returns True for: timeouts, connection errors, 5xx server errors, 429 rate limits.
    Returns False for: 4xx client errors (except 429) - these won't work on fallback either.
    """
    if isinstance(error, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(error, APIStatusError):
        # 429 (rate limit) and 5xx (server error) are retriable
        return error.status_code == 429 or error.status_code >= 500
    # Other exceptions (network, etc.) are retriable
    return True


def _call_with_fallback(messages, temperature, max_tokens, model_tier="fast",
                        caller_client=None):
    """
    Try the LLM call across the provider chain until one succeeds.

    If caller_client is provided AND it's an xAI client, it's used as the primary.
    On failure, falls back to the next provider in the chain.

    Returns (content_str, provider_name) or raises the last exception if all fail.
    """
    # Build the attempt list: start with caller's client (if provided), then chain
    attempts = []

    if caller_client is not None:
        # Use the caller's client as the first attempt (tagged as xai since
        # callers always pass an xAI client)
        attempts.append({"name": "xai", "client": caller_client})

    # Add chain providers, skipping xai if we already have the caller's client
    for p in _provider_chain:
        if caller_client is not None and p["name"] == "xai":
            continue  # Already using caller's xAI client
        attempts.append(p)

    if not attempts:
        raise RuntimeError("No LLM providers configured. Set XAI_API_KEY env var.")

    last_error = None
    for i, provider in enumerate(attempts):
        provider_name = provider["name"]
        model = _get_model_for_provider(provider_name, model_tier)

        try:
            t0 = time.monotonic()
            response = provider["client"].chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            content = (response.choices[0].message.content or "").strip()
            if i == 0:
                logger.debug(f"LLM [{provider_name}/{model}]: {len(content)} chars in {elapsed_ms}ms")
            else:
                logger.info(
                    f"LLM fallback to {provider_name}/{model} succeeded: "
                    f"{len(content)} chars in {elapsed_ms}ms"
                )
            return content, provider_name

        except Exception as e:
            last_error = e

            if not _is_retriable(e):
                # 4xx errors (bad request, auth) won't work on another provider
                logger.error(
                    f"LLM [{provider_name}/{model}] non-retriable error: {e}",
                    exc_info=True,
                )
                raise

            # Retriable error -- log and try next provider
            remaining = len(attempts) - i - 1
            if remaining > 0:
                logger.warning(
                    f"LLM [{provider_name}/{model}] failed: {e}. "
                    f"Falling back ({remaining} provider(s) remaining)"
                )
            else:
                logger.error(
                    f"LLM [{provider_name}/{model}] failed: {e}. "
                    f"No more fallback providers.",
                    exc_info=True,
                )

    # All providers exhausted
    raise last_error


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ PUBLIC API ════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def generate_clean_reply(
    client: OpenAI,
    system_prompt: str = None,
    user_message: str = None,
    bot_name: str = "Mitch",
    max_tokens: int = 200,
    temperature: float = 0.85,
    full_messages: list = None,
) -> str:
    """
    Generate a text message using the non-reasoning model with automatic fallback.

    The non-reasoning model outputs the message directly -- no thinking tokens,
    no chain-of-thought, no reasoning_content field. Just the response.
    This eliminates the entire class of reasoning-leak bugs.

    The sanitizer still runs as a safety net but should almost never trigger.

    Fallback behavior: if the primary provider (xAI) fails with a retriable error
    (timeout, 5xx, connection error, rate limit), automatically tries the next
    configured provider (OpenAI, then Gemini). Requires OPENAI_API_KEY and/or
    GEMINI_API_KEY env vars to be set. Without them, behavior is identical to
    before (single provider, no fallback).

    Args:
        client: OpenAI client instance (used as primary; fallback uses internal clients)
        full_messages: Pre-built messages array (overrides system_prompt/user_message).
                       Use this when you need multi-turn conversation history.

    Returns the clean text message, or empty string if generation fails.
    """
    if full_messages:
        messages = full_messages
    else:
        messages = [{"role": "system", "content": system_prompt}]
        if user_message:
            messages.append({"role": "user", "content": user_message})

    try:
        content, provider_used = _call_with_fallback(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model_tier="fast",
            caller_client=client,
        )

        # Safety net: sanitize even though non-reasoning model shouldn't need it
        clean = sanitize_reply(content)
        if clean:
            return clean

        # Non-reasoning model produced contaminated output (extremely unlikely)
        # Retry once with a tighter prompt
        logger.warning(
            f"NON-REASONING MODEL ({provider_used}) produced suspect output "
            f"({len(content)} chars). Retrying with minimal prompt."
        )

        retry_messages = [
            {
                "role": "system",
                "content": (
                    f"You are {bot_name}, a life insurance advisor texting a lead.\n"
                    "Write a short, casual text message. 1-3 sentences.\n"
                    "No emojis. No dashes. Just plain text like a real person.\n"
                    "Output ONLY the message."
                ),
            },
            {
                "role": "user",
                "content": f"Context: {content[:400]}\n\nWrite the text message.",
            },
        ]

        retry_content, retry_provider = _call_with_fallback(
            messages=retry_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model_tier="fast",
            caller_client=client,
        )

        retry_clean = sanitize_reply(retry_content)

        if retry_clean:
            logger.info(f"RETRY SUCCEEDED ({retry_provider}): '{retry_clean[:80]}...'")
            return retry_clean

        logger.error("BOTH LLM CALLS FAILED to produce clean response")
        return ""

    except Exception as e:
        logger.error(f"LLM call failed (all providers exhausted): {e}", exc_info=True)
        return ""
