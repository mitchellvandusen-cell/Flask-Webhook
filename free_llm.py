# free_llm.py — Free/cheap LLM client selector
#
# Returns (client, model) for background tasks: lead intelligence, support
# chat, call history transcript.
#
# Priority order:
#   1. OpenRouter — free reasoning models (Step 3.5 Flash: $0, 256K ctx, MoE reasoning)
#                   Set OPENROUTER_API_KEY to activate.
#                   Get key: https://openrouter.ai (no credit card for free models)
#                   Note: free tier = ~20 RPM / 200 req/day — good for support chat,
#                   use Groq for high-volume lead intelligence batches.
#
#   2. Groq       — free tier, ultra-low latency, high RPM limits
#                   Set GROQ_API_KEY to activate.
#                   Get key: https://console.groq.com (no credit card)
#
#   3. xAI grok-3-mini-fast — cheapest xAI fallback (no extra key needed)
#
# Tiers:
#   "quality" — best reasoning (Step 3.5 Flash / llama-70b / grok-3-mini-fast)
#   "fast"    — simple extraction (Step 3.5 Flash / llama-8b  / grok-3-mini-fast)

import os
import logging

logger = logging.getLogger(__name__)

# OpenRouter
_OR_BASE_URL  = "https://openrouter.ai/api/v1"
_OR_FREE      = "stepfun/step-3.5-flash:free"   # $0, reasoning, 256K ctx

# Groq
_GROQ_BASE_URL  = "https://api.groq.com/openai/v1"
_GROQ_QUALITY   = "llama-3.3-70b-versatile"   # best Groq model, tool calling
_GROQ_FAST      = "llama-3.1-8b-instant"       # fast simple extraction

# xAI fallback
_XAI_BASE_URL   = "https://api.x.ai/v1"
_XAI_FALLBACK   = "grok-3-mini-fast"


def get_free_llm(tier: str = "quality"):
    """
    Return (client, model_name) for the cheapest available provider.

    Args:
        tier: "quality" — best available reasoning model
              "fast"    — smallest/fastest model for simple extraction tasks

    Returns:
        (OpenAI-compatible client, model_name) or (None, None) if no key set.
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.error("openai package not installed")
        return None, None

    # 1. OpenRouter (free reasoning model — best for quality tasks)
    or_key = os.getenv("OPENROUTER_API_KEY")
    if or_key:
        client = OpenAI(
            api_key=or_key,
            base_url=_OR_BASE_URL,
            default_headers={
                "HTTP-Referer": os.getenv("YOUR_DOMAIN", "https://omnisconn.com"),
                "X-Title": "Omnisconn",
            },
        )
        logger.debug(f"free_llm: using OpenRouter {_OR_FREE}")
        return client, _OR_FREE

    # 2. Groq (free, high RPM — best for high-volume batch tasks)
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        model = _GROQ_QUALITY if tier == "quality" else _GROQ_FAST
        client = OpenAI(api_key=groq_key, base_url=_GROQ_BASE_URL)
        logger.debug(f"free_llm: using Groq {model}")
        return client, model

    # 3. xAI fallback (cheapest paid option)
    xai_key = os.getenv("XAI_API_KEY")
    if xai_key:
        client = OpenAI(api_key=xai_key, base_url=_XAI_BASE_URL)
        logger.debug(f"free_llm: using xAI {_XAI_FALLBACK} (no free LLM key set)")
        return client, _XAI_FALLBACK

    logger.error("free_llm: no API key set (OPENROUTER_API_KEY / GROQ_API_KEY / XAI_API_KEY)")
    return None, None
